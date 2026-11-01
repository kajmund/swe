//! Host driver for the demo: effect dispatch loop, Sweycoin metering, pause
//! and resume.
//!
//! The kernel is host-paced: [`start_run`] and every [`WorkflowMachine`]
//! transition return effects, and the host answers them. This is the same shape
//! as `swe-gym/src/driver.rs`, with three demo-specific additions:
//!
//! 1. both `InvokeHandler` and `InvokeProvider` effects are dispatched
//!    (handlers are local pure functions, the provider is the deterministic mock);
//! 2. every dispatch is reserved and settled in the reused
//!    [`swe_budget::Ledger`], so per-action and total Sweycoin cost are real;
//! 3. when a spend checkpoint is configured, the host pauses instead of failing:
//!    it returns a [`Journal`] that can be written to disk and resumed in a
//!    fresh run, replaying persisted invocation results and the ledger.

use std::collections::{BTreeMap, HashMap, VecDeque};

use swe_budget::{Amount, Ledger};
use swe_native::{
    attempt_id, delivery_key, sha256_hex, start_run, Delivery, Effect, Journal as EngineJournal,
    JournalEvent, Json, RunStatus, StepOutcome, WorkflowMachine,
};

use crate::domain::Scenario;
use crate::evidence::{
    accept_handler, merge_evidence_handler, record_missing_handler, Evidence, StatusCounts,
};
use crate::journal::{Journal, JournalCompletedInvocation};
use crate::mock_provider::{MockSupplierFacts, PROVIDER_NAME};
use crate::operations::{
    build_abstain_handler, build_report_handler, plan_needs_handler, screen_compliance_handler,
    select_cheapest_handler,
};
use crate::{field_str, field_u64, obj, DemoError, DemoResult};

/// Ledger account that funds the investigation.
pub const ACCOUNT: &str = "investigation";

/// Fixed logical clock. A real host would pass wall-clock milliseconds; a fixed
/// value keeps runs and resume segments byte-for-byte deterministic.
const NOW_MS: u64 = 1_000;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Tariffs {
    pub handler_milli: Amount,
    pub provider_milli: Amount,
}

impl Default for Tariffs {
    fn default() -> Self {
        Self {
            handler_milli: 1_000,
            provider_milli: 2_000,
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ActionKind {
    Handler,
    Provider,
}

impl ActionKind {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Handler => "handler",
            Self::Provider => "provider",
        }
    }

    pub fn parse(value: &str) -> DemoResult<Self> {
        match value {
            "handler" => Ok(Self::Handler),
            "provider" => Ok(Self::Provider),
            other => Err(DemoError::new(format!("unknown action kind {other:?}"))),
        }
    }
}

/// One metered call, as settled in the ledger.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ActionCharge {
    pub invocation_id: String,
    pub action: String,
    pub kind: ActionKind,
    pub cost_milli: Amount,
}

impl ActionCharge {
    pub fn to_json(&self) -> Json {
        obj([
            ("invocation_id", Json::from(self.invocation_id.as_str())),
            ("action", Json::from(self.action.as_str())),
            ("kind", Json::from(self.kind.as_str())),
            ("cost_milli", Json::from_u64(self.cost_milli)),
        ])
    }

    pub fn from_json(value: &Json) -> DemoResult<Self> {
        Ok(Self {
            invocation_id: field_str(value, "invocation_id")?.to_string(),
            action: field_str(value, "action")?.to_string(),
            kind: ActionKind::parse(field_str(value, "kind")?)?,
            cost_milli: field_u64(value, "cost_milli")?,
        })
    }
}

/// One invocation the host answered, with the output the kernel consumed.
#[derive(Clone, Debug, PartialEq)]
pub struct InvocationLog {
    pub invocation_id: String,
    pub node_id: String,
    pub action: String,
    pub kind: ActionKind,
    pub output: Json,
}

impl InvocationLog {
    /// The `status` field of the handler output, when it has one.
    pub fn status(&self) -> Option<&str> {
        self.output.get("status").and_then(Json::as_str)
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct RunConfig {
    pub run_id: String,
    pub budget_milli: Amount,
    /// Absolute spend checkpoint for this segment. When the next metered action
    /// would exceed it, the host pauses and hands back a journal instead of
    /// failing the run.
    pub pause_at_spend_milli: Option<Amount>,
    pub tariffs: Tariffs,
}

impl RunConfig {
    pub fn new(run_id: impl Into<String>, budget_milli: Amount) -> Self {
        Self {
            run_id: run_id.into(),
            budget_milli,
            pause_at_spend_milli: None,
            tariffs: Tariffs::default(),
        }
    }

    pub fn with_pause_at_spend(mut self, spend_milli: Amount) -> Self {
        self.pause_at_spend_milli = Some(spend_milli);
        self
    }

    pub fn with_tariffs(mut self, tariffs: Tariffs) -> Self {
        self.tariffs = tariffs;
        self
    }
}

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct ResumeConfig {
    pub pause_at_spend_milli: Option<Amount>,
    /// The tariffs this host bills at now. A journal priced under different
    /// tariffs must not resume: it would silently reprice the second half.
    pub tariffs: Option<Tariffs>,
}

impl ResumeConfig {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn with_pause_at_spend(mut self, spend_milli: Amount) -> Self {
        self.pause_at_spend_milli = Some(spend_milli);
        self
    }

    /// Bill the resumed segment at these tariffs.
    pub fn with_tariffs(mut self, tariffs: Tariffs) -> Self {
        self.tariffs = Some(tariffs);
        self
    }
}

/// Report for one driver segment (a fresh run or a resume).
#[derive(Clone, Debug)]
pub struct SegmentReport {
    pub status: RunStatus,
    pub error: Option<String>,
    pub executed_invocations: usize,
    pub replayed_invocations: usize,
    pub charges: Vec<ActionCharge>,
    pub spent_milli: Amount,
    pub allocated_milli: Amount,
    pub available_milli: Amount,
    pub invocations: Vec<InvocationLog>,
}

/// A paused investigation: everything needed to resume is in the journal.
#[derive(Clone, Debug)]
pub struct PausedReport {
    pub journal: Journal,
    pub segment: SegmentReport,
}

/// Final report for a terminal investigation.
#[derive(Clone, Debug)]
pub struct RunReport {
    pub run_id: String,
    pub scenario: Scenario,
    pub status: RunStatus,
    pub error: Option<String>,
    pub conclusion: Option<Json>,
    pub evidence: Vec<Evidence>,
    pub charges: Vec<ActionCharge>,
    pub spent_milli: Amount,
    pub allocated_milli: Amount,
    pub available_milli: Amount,
    /// Unique invocations executed across the whole investigation. This does
    /// not grow when a segment is resumed from a journal.
    pub executed_invocations: usize,
    pub replayed_invocations: usize,
    pub handler_calls: usize,
    pub provider_calls: usize,
    pub ledger_events: usize,
    pub steps_used: u32,
    pub invocations: Vec<InvocationLog>,
    /// Engine journal events, in order (attempt, result, reservation, settlement).
    pub journal_events: Vec<&'static str>,
    /// Total settled in the engine journal; must equal the ledger's spend.
    pub journal_settled_milli: Amount,
    /// Attempts still open at the end: their outcome would be unknown.
    pub journal_open_attempts: usize,
    /// Charges still needing an operator decision.
    pub journal_pending_charges: usize,
    pub ledger: Ledger,
    pub tariffs: Tariffs,
}

impl RunReport {
    pub fn action_totals(&self) -> Vec<(String, Amount)> {
        let mut totals: BTreeMap<String, Amount> = BTreeMap::new();
        for charge in &self.charges {
            *totals.entry(charge.action.clone()).or_default() += charge.cost_milli;
        }
        totals.into_iter().collect()
    }

    pub fn action_total(&self, action: &str) -> Amount {
        self.charges
            .iter()
            .filter(|charge| charge.action == action)
            .map(|charge| charge.cost_milli)
            .sum()
    }

    pub fn charge_count(&self, action: &str) -> usize {
        self.charges
            .iter()
            .filter(|charge| charge.action == action)
            .count()
    }

    pub fn conclusion_str(&self, key: &str) -> Option<&str> {
        self.conclusion
            .as_ref()
            .and_then(|conclusion| conclusion.get(key))
            .and_then(Json::as_str)
    }

    pub fn evidence_status_counts(&self) -> StatusCounts {
        let mut counts = StatusCounts::default();
        for evidence in &self.evidence {
            match evidence.status {
                crate::evidence::EvidenceStatus::Verified => counts.verified += 1,
                crate::evidence::EvidenceStatus::Ambiguous => counts.ambiguous += 1,
                crate::evidence::EvidenceStatus::Rejected => counts.rejected += 1,
            }
        }
        counts
    }
}

/// Result of one [`run`] or [`resume`] call.
#[derive(Clone, Debug)]
pub enum InvestigationOutcome {
    Finished(Box<RunReport>),
    Paused(Box<PausedReport>),
}

impl InvestigationOutcome {
    pub fn is_paused(&self) -> bool {
        matches!(self, Self::Paused(_))
    }

    pub fn paused(&self) -> Option<&PausedReport> {
        match self {
            Self::Paused(report) => Some(report),
            Self::Finished(_) => None,
        }
    }

    pub fn finished(&self) -> Option<&RunReport> {
        match self {
            Self::Finished(report) => Some(report),
            Self::Paused(_) => None,
        }
    }

    pub fn into_finished(self) -> DemoResult<RunReport> {
        match self {
            Self::Finished(report) => Ok(*report),
            Self::Paused(_) => Err(DemoError::new(
                "investigation paused instead of finishing; resume it with the journal",
            )),
        }
    }
}

/// Start a fresh investigation and drive it until it terminates or hits the
/// configured spend checkpoint.
pub fn run(
    document: &Json,
    input: &Json,
    scenario: Scenario,
    config: &RunConfig,
) -> DemoResult<InvestigationOutcome> {
    let definition_sha256 = sha256_hex(document)?;
    let provider = MockSupplierFacts::new(scenario);
    let mut ledger = Ledger::new();
    ledger.allocate(
        &config.run_id,
        ACCOUNT,
        config.budget_milli,
        &format!("{}:allocate", config.run_id),
    )?;
    let (machine, outcome) = start_run(
        document.clone(),
        &definition_sha256,
        config.run_id.clone(),
        input.clone(),
        NOW_MS,
    )?;
    let ctx = SessionContext {
        run_id: config.run_id.clone(),
        document: document.clone(),
        input: input.clone(),
        definition_sha256,
        scenario,
        budget_milli: config.budget_milli,
        tariffs: config.tariffs,
        pause_at: config.pause_at_spend_milli,
        prior_charges: Vec::new(),
        prior_completed: Vec::new(),
        prior_journal: Vec::new(),
        prior_handler_calls: 0,
        prior_provider_calls: 0,
        replay: HashMap::new(),
    };
    let mut session = Session::new(machine, ledger, outcome, provider, ctx);
    let paused = session.drive()?;
    session.finish(paused)
}

/// Resume a paused investigation from its journal.
pub fn resume(journal: &Journal, config: &ResumeConfig) -> DemoResult<InvestigationOutcome> {
    journal.validate()?;
    if let Some(tariffs) = config.tariffs {
        let expected = crate::journal::tariff_sha256(&tariffs)?;
        if expected != journal.tariff_sha256 {
            return Err(DemoError::new(
                "journal cannot be resumed: this host bills at different tariffs",
            ));
        }
    }
    let ledger = Ledger::replay(&journal.ledger_events)?;
    let balance = ledger.balance(ACCOUNT);
    if balance.allocated != journal.budget_milli {
        return Err(DemoError::new(format!(
            "journal ledger allocates {} milli but the journal budget is {} milli",
            balance.allocated, journal.budget_milli
        )));
    }
    let provider = MockSupplierFacts::new(journal.scenario);
    let (machine, outcome) = start_run(
        journal.document.clone(),
        &journal.definition_sha256,
        journal.run_id.clone(),
        journal.input.clone(),
        journal.now_ms,
    )?;
    let replay = journal
        .completed
        .iter()
        .map(|completed| (completed.invocation_id.clone(), completed.output.clone()))
        .collect();
    let ctx = SessionContext {
        run_id: journal.run_id.clone(),
        document: journal.document.clone(),
        input: journal.input.clone(),
        definition_sha256: journal.definition_sha256.clone(),
        scenario: journal.scenario,
        budget_milli: journal.budget_milli,
        tariffs: config.tariffs.unwrap_or_else(|| journal.recorded_tariffs()),
        pause_at: config.pause_at_spend_milli,
        prior_charges: journal.charges.clone(),
        prior_completed: journal.completed.clone(),
        prior_journal: journal.journal_events.clone(),
        prior_handler_calls: journal.handler_calls,
        prior_provider_calls: journal.provider_calls,
        replay,
    };
    let mut session = Session::new(machine, ledger, outcome, provider, ctx);
    let paused = session.drive()?;
    session.finish(paused)
}

fn execute_handler(handler: &str, input: &Json) -> DemoResult<Json> {
    match handler {
        "reference.plan_needs" => plan_needs_handler(input),
        "reference.accept_evidence" => accept_handler(input),
        "reference.record_missing" => record_missing_handler(input),
        "reference.merge_evidence" => merge_evidence_handler(input),
        "reference.screen_compliance" => screen_compliance_handler(input),
        "reference.select_cheapest" => select_cheapest_handler(input),
        "reference.build_report" => build_report_handler(input),
        "reference.build_abstain" => build_abstain_handler(input),
        other => Err(DemoError::new(format!("unknown handler {other:?}"))),
    }
}

fn conclusion_from_output(output: &Json) -> Option<Json> {
    ["finalize", "abstain_screen", "abstain_price"]
        .iter()
        .find_map(|node| {
            output
                .get(node)
                .and_then(|value| value.get("result"))
                .cloned()
        })
}

fn evidence_from_output(output: &Json) -> DemoResult<Vec<Evidence>> {
    let Some(node) = output.get("evidence") else {
        return Ok(Vec::new());
    };
    let Some(result) = node.get("result") else {
        return Ok(Vec::new());
    };
    let Some(items) = result.get("evidence").and_then(Json::as_array) else {
        return Ok(Vec::new());
    };
    items.iter().map(Evidence::from_json).collect()
}

struct SessionContext {
    /// Engine journal events carried over from earlier segments.
    prior_journal: Vec<swe_native::JournalEvent>,
    run_id: String,
    document: Json,
    input: Json,
    definition_sha256: String,
    scenario: Scenario,
    budget_milli: Amount,
    tariffs: Tariffs,
    pause_at: Option<Amount>,
    prior_charges: Vec<ActionCharge>,
    prior_completed: Vec<JournalCompletedInvocation>,
    prior_handler_calls: usize,
    prior_provider_calls: usize,
    replay: HashMap<String, Json>,
}

struct Session {
    machine: WorkflowMachine,
    ledger: Ledger,
    pending: VecDeque<Effect>,
    provider: MockSupplierFacts,
    ctx: SessionContext,
    charges: Vec<ActionCharge>,
    invocations: Vec<InvocationLog>,
    completed: Vec<JournalCompletedInvocation>,
    replayed: usize,
    /// Engine journal: attempts, confirmed results and charges, in order.
    journal: EngineJournal,
    executed: usize,
    handler_calls: usize,
    provider_calls: usize,
}

impl Session {
    fn new(
        machine: WorkflowMachine,
        ledger: Ledger,
        outcome: StepOutcome,
        provider: MockSupplierFacts,
        ctx: SessionContext,
    ) -> Self {
        let completed = ctx.prior_completed.clone();
        let mut engine_journal = EngineJournal::new();
        for event in &ctx.prior_journal {
            let _ = engine_journal.append(event.clone());
        }
        Self {
            machine,
            ledger,
            pending: VecDeque::from(outcome.effects),
            provider,
            ctx,
            charges: Vec::new(),
            invocations: Vec::new(),
            journal: engine_journal,
            completed,
            replayed: 0,
            executed: 0,
            handler_calls: 0,
            provider_calls: 0,
        }
    }

    /// Answer effects until the run terminates (`Ok(false)`) or a spend
    /// checkpoint is reached (`Ok(true)`).
    fn drive(&mut self) -> DemoResult<bool> {
        loop {
            if self.machine.status().is_terminal() {
                return Ok(false);
            }
            if self.pending.is_empty() {
                let ticked = self.machine.tick(NOW_MS);
                self.pending.extend(ticked.effects);
                if self.machine.status().is_terminal() {
                    return Ok(false);
                }
                if self.pending.is_empty() {
                    return Err(DemoError::new(
                        "kernel stalled: run is running with no pending effects",
                    ));
                }
                continue;
            }
            let effect = self
                .pending
                .pop_front()
                .expect("pending effects were checked to be non-empty");
            let paused = match effect {
                Effect::InvokeHandler {
                    invocation_id,
                    node_id,
                    handler,
                    input,
                    ..
                } => self.dispatch(
                    &invocation_id,
                    &node_id,
                    &handler,
                    ActionKind::Handler,
                    &input,
                )?,
                Effect::InvokeProvider {
                    invocation_id,
                    node_id,
                    provider,
                    input,
                    ..
                } => {
                    if provider != PROVIDER_NAME {
                        return Err(DemoError::new(format!(
                            "unknown provider {provider:?}; only {PROVIDER_NAME:?} is registered"
                        )));
                    }
                    self.dispatch(
                        &invocation_id,
                        &node_id,
                        PROVIDER_NAME,
                        ActionKind::Provider,
                        &input,
                    )?
                }
                Effect::CancelInvocation { invocation_id } => {
                    return Err(DemoError::new(format!(
                        "unexpected cancel_invocation for {invocation_id}"
                    )));
                }
            };
            if paused {
                return Ok(true);
            }
        }
    }

    fn dispatch(
        &mut self,
        invocation_id: &str,
        node_id: &str,
        action: &str,
        kind: ActionKind,
        input: &Json,
    ) -> DemoResult<bool> {
        if let Some(stored) = self.ctx.replay.remove(invocation_id) {
            self.replayed += 1;
            self.invocations.push(InvocationLog {
                invocation_id: invocation_id.to_string(),
                node_id: node_id.to_string(),
                action: action.to_string(),
                kind,
                output: stored.clone(),
            });
            let outcome = self.machine.complete(invocation_id, stored, NOW_MS);
            self.pending.extend(outcome.effects);
            return Ok(false);
        }

        let cost = match kind {
            ActionKind::Handler => self.ctx.tariffs.handler_milli,
            ActionKind::Provider => self.ctx.tariffs.provider_milli,
        };
        if let Some(checkpoint) = self.ctx.pause_at {
            if self.ledger.balance(ACCOUNT).spent + cost > checkpoint {
                return Ok(true);
            }
        }

        // The engine journal keeps three facts separate: the attempt starts, the
        // result is confirmed, and only then is the cost settled. Every action
        // in this experiment is deterministic, so the delivery is Pure.
        let delivery = Delivery::Pure;
        let key = delivery_key(&self.ctx.run_id, invocation_id, action);
        let attempt = attempt_id(&key, 1);
        self.journal
            .append(JournalEvent::AttemptStarted {
                attempt_id: attempt.clone(),
                delivery_key: key.clone(),
                invocation_id: invocation_id.to_string(),
                action: action.to_string(),
                delivery,
                started_at_ms: NOW_MS,
            })
            .map_err(|error| DemoError::new(format!("journal rejected the attempt: {error}")))?;

        let reservation_id = self.ledger.reserve(
            &self.ctx.run_id,
            ACCOUNT,
            cost,
            &format!("{}:reserve:{invocation_id}", self.ctx.run_id),
        )?;
        self.journal
            .append(JournalEvent::ChargeReserved {
                charge_id: invocation_id.to_string(),
                attempt_id: attempt.clone(),
                delivery_key: key,
                amount_milli: cost,
                tariff_milli: cost,
                reserved_at_ms: NOW_MS,
            })
            .map_err(|error| DemoError::new(format!("journal rejected the charge: {error}")))?;

        let output = match kind {
            ActionKind::Handler => execute_handler(action, input)?,
            ActionKind::Provider => self.provider.fetch(input)?,
        };

        self.journal
            .append(JournalEvent::ResultConfirmed {
                attempt_id: attempt.clone(),
                outcome: swe_native::CompletionOutcome::Ok(output.clone()),
                confirmed_at_ms: NOW_MS,
            })
            .map_err(|error| DemoError::new(format!("journal rejected the result: {error}")))?;

        self.ledger.settle(
            reservation_id,
            cost,
            &format!("{}:settle:{invocation_id}", self.ctx.run_id),
        )?;
        self.journal
            .append(JournalEvent::ChargeSettled {
                charge_id: invocation_id.to_string(),
                amount_milli: cost,
                settled_at_ms: NOW_MS,
            })
            .map_err(|error| DemoError::new(format!("journal rejected the settlement: {error}")))?;

        self.charges.push(ActionCharge {
            invocation_id: invocation_id.to_string(),
            action: action.to_string(),
            kind,
            cost_milli: cost,
        });
        match kind {
            ActionKind::Handler => self.handler_calls += 1,
            ActionKind::Provider => self.provider_calls += 1,
        }
        self.executed += 1;
        self.completed.push(JournalCompletedInvocation {
            invocation_id: invocation_id.to_string(),
            node_id: node_id.to_string(),
            output: output.clone(),
        });
        self.invocations.push(InvocationLog {
            invocation_id: invocation_id.to_string(),
            node_id: node_id.to_string(),
            action: action.to_string(),
            kind,
            output: output.clone(),
        });
        let outcome = self.machine.complete(invocation_id, output, NOW_MS);
        self.pending.extend(outcome.effects);
        Ok(false)
    }

    fn finish(mut self, paused: bool) -> DemoResult<InvestigationOutcome> {
        let balance = self.ledger.balance(ACCOUNT);
        let status = self.machine.status();
        let error = self
            .machine
            .error()
            .map(|runtime_error| runtime_error.message.clone());
        let segment = SegmentReport {
            status: status.clone(),
            error: error.clone(),
            executed_invocations: self.executed,
            replayed_invocations: self.replayed,
            charges: self.charges.clone(),
            spent_milli: balance.spent,
            allocated_milli: balance.allocated,
            available_milli: balance.available(),
            invocations: self.invocations.clone(),
        };

        let mut charges = std::mem::take(&mut self.ctx.prior_charges);
        charges.extend(self.charges.iter().cloned());
        let journal = Journal {
            run_id: self.ctx.run_id.clone(),
            definition_sha256: self.ctx.definition_sha256.clone(),
            contract_version: crate::journal::CONTRACT_VERSION,
            tariff_sha256: crate::journal::tariff_sha256(&Tariffs {
                handler_milli: self.ctx.tariffs.handler_milli,
                provider_milli: self.ctx.tariffs.provider_milli,
            })?,
            recorded_dependency_versions: crate::journal::current_dependency_versions(),
            document: self.ctx.document.clone(),
            input: self.ctx.input.clone(),
            now_ms: NOW_MS,
            scenario: self.ctx.scenario,
            budget_milli: self.ctx.budget_milli,
            handler_cost_milli: self.ctx.tariffs.handler_milli,
            provider_cost_milli: self.ctx.tariffs.provider_milli,
            completed: self.completed.clone(),
            journal_events: self.journal.events().to_vec(),
            charges,
            ledger_events: self.ledger.events().to_vec(),
            handler_calls: self.ctx.prior_handler_calls + self.handler_calls,
            provider_calls: self.ctx.prior_provider_calls + self.provider_calls,
        };
        if paused {
            return Ok(InvestigationOutcome::Paused(Box::new(PausedReport {
                journal,
                segment,
            })));
        }

        let output = self.machine.output();
        let evidence = evidence_from_output(&output)?;
        let report = RunReport {
            run_id: self.ctx.run_id.clone(),
            scenario: self.ctx.scenario,
            status,
            error,
            conclusion: conclusion_from_output(&output),
            evidence,
            charges: journal.charges.clone(),
            spent_milli: balance.spent,
            allocated_milli: balance.allocated,
            available_milli: balance.available(),
            executed_invocations: self.ctx.prior_completed.len() + self.executed,
            replayed_invocations: self.replayed,
            handler_calls: journal.handler_calls,
            provider_calls: journal.provider_calls,
            ledger_events: journal.ledger_events.len(),
            steps_used: self.machine.steps_used,
            invocations: std::mem::take(&mut self.invocations),
            ledger: self.ledger,
            tariffs: self.ctx.tariffs,
            journal_events: self
                .journal
                .events()
                .iter()
                .map(|event| event.kind())
                .collect(),
            journal_settled_milli: self.journal.settled_total(),
            journal_open_attempts: self.journal.open_attempts().len(),
            journal_pending_charges: self.journal.pending_reconciliation().len(),
        };
        Ok(InvestigationOutcome::Finished(Box::new(report)))
    }
}
