//! Append-only execution journal: attempts, confirmed results and charges.
//!
//! The journal records what happened to every logical call, not just its final
//! state. Three questions stay separate because they have different answers:
//!
//! - **execution** — what may have happened at the external service;
//! - **confirmation** — what we actually know happened;
//! - **charging** — which cost is confirmed, and which still needs
//!   reconciliation.
//!
//! The log is append-only: no record is rewritten, and every state is derived
//! from the events. That is what makes "unconfirmed" a legitimate, visible
//! state instead of an assumption.
//!
//! Identity follows `docs/swe-standalone/09-contract-decision.md` §4.2: the
//! `delivery_key` names the logical delivery and survives retries, while the
//! `attempt_id` names one try. A retry keeps the key and gets a new attempt.

use std::collections::BTreeMap;
use std::fmt;

use crate::canonicalize::sha256_hex;
use crate::json::Json;

/// The journal's on-disk contract version.
pub const JOURNAL_SCHEMA_VERSION: u32 = 1;

/// Identity of one logical delivery: stable across retries.
///
/// This is the key an external tool must honour for at-least-once delivery to
/// yield at most one side effect.
pub fn delivery_key(run_id: &str, invocation_id: &str, action: &str) -> String {
    let payload = Json::object(vec![
        ("run_id".to_string(), Json::from(run_id)),
        ("invocation_id".to_string(), Json::from(invocation_id)),
        ("action".to_string(), Json::from(action)),
    ]);
    sha256_hex(&payload).unwrap_or_default()
}

/// Identity of one attempt at a delivery.
pub fn attempt_id(delivery: &str, attempt_no: u32) -> String {
    format!("{delivery}:{attempt_no}")
}

/// How the host intends to recover an interrupted delivery.
///
/// Declared per logical delivery, before the first attempt, because one run can
/// mix pure computations and irreversible external side effects.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Delivery {
    /// Deterministic function of the input: may be run again.
    Pure,
    /// The result is frozen: replay it, never re-run.
    Replayable,
    /// The tool honours the delivery key: at-least-once, at most one effect.
    ExternalIdempotent,
    /// An irreversible side effect with no key support: fail stop.
    ExternalNonIdempotent,
}

impl Delivery {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Pure => "pure",
            Self::Replayable => "replayable",
            Self::ExternalIdempotent => "external_idempotent",
            Self::ExternalNonIdempotent => "external_non_idempotent",
        }
    }

    pub fn parse(value: &str) -> Option<Self> {
        match value {
            "pure" => Some(Self::Pure),
            "replayable" => Some(Self::Replayable),
            "external_idempotent" => Some(Self::ExternalIdempotent),
            "external_non_idempotent" => Some(Self::ExternalNonIdempotent),
            _ => None,
        }
    }

    /// Whether an interrupted attempt can safely be re-run.
    pub fn may_retry(self) -> bool {
        matches!(self, Self::Pure | Self::ExternalIdempotent)
    }
}

/// The outcome of one completed call.
#[derive(Clone, Debug, PartialEq)]
pub enum CompletionOutcome {
    Ok(Json),
    Err { kind: String, message: String },
    Cancelled,
}

/// One record in the append-only log.
#[derive(Clone, Debug, PartialEq)]
pub enum JournalEvent {
    AttemptStarted {
        attempt_id: String,
        delivery_key: String,
        invocation_id: String,
        action: String,
        delivery: Delivery,
        started_at_ms: u64,
    },
    ResultConfirmed {
        attempt_id: String,
        outcome: CompletionOutcome,
        confirmed_at_ms: u64,
    },
    AttemptAbandoned {
        attempt_id: String,
        reason: String,
        abandoned_at_ms: u64,
    },
    ChargeReserved {
        charge_id: String,
        attempt_id: String,
        delivery_key: String,
        amount_milli: u64,
        tariff_milli: u64,
        reserved_at_ms: u64,
    },
    ChargeSettled {
        charge_id: String,
        amount_milli: u64,
        settled_at_ms: u64,
    },
    ChargeReleased {
        charge_id: String,
        reason: String,
        released_at_ms: u64,
    },
    ChargePendingReconciliation {
        charge_id: String,
        reason: String,
        at_ms: u64,
    },
}

impl JournalEvent {
    pub fn kind(&self) -> &'static str {
        match self {
            Self::AttemptStarted { .. } => "AttemptStarted",
            Self::ResultConfirmed { .. } => "ResultConfirmed",
            Self::AttemptAbandoned { .. } => "AttemptAbandoned",
            Self::ChargeReserved { .. } => "ChargeReserved",
            Self::ChargeSettled { .. } => "ChargeSettled",
            Self::ChargeReleased { .. } => "ChargeReleased",
            Self::ChargePendingReconciliation { .. } => "ChargePendingReconciliation",
        }
    }
}

/// Why a journal operation was refused.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum JournalError {
    /// A second settlement for one charge: money may not be taken twice.
    DuplicateSettlement(String),
    /// An event referred to an attempt or charge the log never opened.
    UnknownReference(String),
    /// The event contradicts the log (e.g. settling a released charge).
    Conflict(String),
    /// The journal is from another contract version.
    UnsupportedVersion(u32),
    /// The run's definition changed under a resume.
    DefinitionChanged { expected: String, found: String },
}

impl fmt::Display for JournalError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::DuplicateSettlement(charge) => {
                write!(f, "charge {charge} was already settled")
            }
            Self::UnknownReference(reference) => write!(f, "unknown reference {reference}"),
            Self::Conflict(detail) => write!(f, "journal conflict: {detail}"),
            Self::UnsupportedVersion(version) => {
                write!(f, "unsupported journal schema_version {version}")
            }
            Self::DefinitionChanged { expected, found } => write!(
                f,
                "journal definition {found} does not match the resumed definition {expected}"
            ),
        }
    }
}

impl std::error::Error for JournalError {}

/// Where a charge stands. Derived, never stored as a mutable field.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ChargeState {
    Reserved,
    Settled,
    Released,
    PendingReconciliation,
}

/// One open attempt and what is known about it.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct OpenAttempt {
    pub attempt_id: String,
    pub delivery_key: String,
    pub invocation_id: String,
    pub action: String,
    pub delivery: Delivery,
}

/// The append-only log plus the views derived from it.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct Journal {
    events: Vec<JournalEvent>,
    confirmed: BTreeMap<String, CompletionOutcome>,
    attempts: BTreeMap<String, OpenAttempt>,
    closed: BTreeMap<String, ()>,
    charges: BTreeMap<String, ChargeState>,
    charge_of: BTreeMap<String, String>,
}

impl Journal {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn events(&self) -> &[JournalEvent] {
        &self.events
    }

    /// Record one event. This is the only way to change state.
    pub fn append(&mut self, event: JournalEvent) -> Result<(), JournalError> {
        match &event {
            JournalEvent::AttemptStarted {
                attempt_id,
                delivery_key,
                invocation_id,
                action,
                delivery,
                ..
            } => {
                self.attempts.insert(
                    attempt_id.clone(),
                    OpenAttempt {
                        attempt_id: attempt_id.clone(),
                        delivery_key: delivery_key.clone(),
                        invocation_id: invocation_id.clone(),
                        action: action.clone(),
                        delivery: *delivery,
                    },
                );
            }
            JournalEvent::ResultConfirmed {
                attempt_id,
                outcome,
                ..
            } => {
                if !self.attempts.contains_key(attempt_id) {
                    return Err(JournalError::UnknownReference(attempt_id.clone()));
                }
                if self.closed.contains_key(attempt_id) {
                    return Err(JournalError::Conflict(format!(
                        "attempt {attempt_id} is already closed"
                    )));
                }
                self.confirmed.insert(attempt_id.clone(), outcome.clone());
                self.closed.insert(attempt_id.clone(), ());
            }
            JournalEvent::AttemptAbandoned { attempt_id, .. } => {
                if !self.attempts.contains_key(attempt_id) {
                    return Err(JournalError::UnknownReference(attempt_id.clone()));
                }
                self.closed.insert(attempt_id.clone(), ());
            }
            JournalEvent::ChargeReserved {
                charge_id,
                attempt_id,
                delivery_key,
                ..
            } => {
                if !self.attempts.contains_key(attempt_id) {
                    return Err(JournalError::UnknownReference(attempt_id.clone()));
                }
                // One logical delivery may reserve at most one charge, whatever
                // attempt it is on: a retry must not reserve a second cost for
                // the same delivery (09 §4.6).
                let already_reserved = self.events.iter().any(|event| {
                    matches!(
                        event,
                        JournalEvent::ChargeReserved { delivery_key: key, .. } if key == delivery_key
                    )
                });
                if already_reserved {
                    return Err(JournalError::Conflict(format!(
                        "delivery {delivery_key} already has a reservation"
                    )));
                }
                if self.charges.contains_key(charge_id) {
                    return Err(JournalError::Conflict(format!(
                        "charge {charge_id} was already reserved"
                    )));
                }
                self.charges
                    .insert(charge_id.clone(), ChargeState::Reserved);
                self.charge_of.insert(charge_id.clone(), attempt_id.clone());
            }
            JournalEvent::ChargeSettled { charge_id, .. } => {
                match self.charges.get(charge_id) {
                    Some(ChargeState::Settled) => {
                        return Err(JournalError::DuplicateSettlement(charge_id.clone()))
                    }
                    Some(ChargeState::Released) | Some(ChargeState::PendingReconciliation) => {
                        return Err(JournalError::Conflict(format!(
                            "charge {charge_id} cannot be settled after release or reconciliation"
                        )))
                    }
                    Some(ChargeState::Reserved) => {}
                    None => return Err(JournalError::UnknownReference(charge_id.clone())),
                }
                if let Some(attempt) = self.charge_of.get(charge_id) {
                    if !self.closed.contains_key(attempt) {
                        return Err(JournalError::Conflict(format!(
                            "charge {charge_id} cannot settle before its attempt is confirmed"
                        )));
                    }
                }
                self.charges.insert(charge_id.clone(), ChargeState::Settled);
            }
            JournalEvent::ChargeReleased { charge_id, .. } => {
                match self.charges.get(charge_id) {
                    Some(ChargeState::Settled) => {
                        return Err(JournalError::Conflict(format!(
                            "charge {charge_id} is already settled"
                        )))
                    }
                    Some(ChargeState::Released) => {
                        return Err(JournalError::Conflict(format!(
                            "charge {charge_id} was already released"
                        )))
                    }
                    Some(ChargeState::Reserved) => {}
                    // Releasing a charge whose cost is unknown would write that
                    // unknown off as zero, which the contract forbids.
                    Some(ChargeState::PendingReconciliation) => {
                        return Err(JournalError::Conflict(format!(
                            "charge {charge_id} is pending reconciliation; releasing it \
                             would write an unknown cost off as zero"
                        )))
                    }
                    None => return Err(JournalError::UnknownReference(charge_id.clone())),
                }
                self.charges
                    .insert(charge_id.clone(), ChargeState::Released);
            }
            JournalEvent::ChargePendingReconciliation { charge_id, .. } => {
                match self.charges.get(charge_id) {
                    Some(ChargeState::Settled) | Some(ChargeState::Released) => {
                        return Err(JournalError::Conflict(format!(
                            "charge {charge_id} is already closed"
                        )))
                    }
                    Some(ChargeState::Reserved) | Some(ChargeState::PendingReconciliation) => {}
                    None => return Err(JournalError::UnknownReference(charge_id.clone())),
                }
                self.charges
                    .insert(charge_id.clone(), ChargeState::PendingReconciliation);
            }
        }
        self.events.push(event);
        Ok(())
    }

    /// The confirmation for an attempt, if the log has one.
    pub fn confirmed(&self, attempt_id: &str) -> Option<&CompletionOutcome> {
        self.confirmed.get(attempt_id)
    }

    /// Attempts that were started and neither confirmed nor abandoned.
    ///
    /// The outcome of these is **unknown**: not "did not happen".
    pub fn open_attempts(&self) -> Vec<&OpenAttempt> {
        self.attempts
            .values()
            .filter(|attempt| !self.closed.contains_key(&attempt.attempt_id))
            .collect()
    }

    pub fn charge_state(&self, charge_id: &str) -> Option<ChargeState> {
        self.charges.get(charge_id).copied()
    }

    /// Total settled amount. Only settlement moves money.
    pub fn settled_total(&self) -> u64 {
        self.events
            .iter()
            .filter_map(|event| match event {
                JournalEvent::ChargeSettled { amount_milli, .. } => Some(*amount_milli),
                _ => None,
            })
            .sum()
    }

    /// Charges that still need an operator decision.
    pub fn pending_reconciliation(&self) -> Vec<&str> {
        self.charges
            .iter()
            .filter(|(_, state)| **state == ChargeState::PendingReconciliation)
            .map(|(id, _)| id.as_str())
            .collect()
    }
}

/// What a resume must do about one attempt that was still open.
#[derive(Clone, Debug, PartialEq)]
pub enum Recovery {
    /// The delivery is pure: compute it again, charging nothing new.
    Recompute { attempt_id: String },
    /// The delivery is replayable: only a confirmed result may be replayed, so
    /// an open attempt with no result is a hard error, never a re-run.
    ReplayRequiresResult { attempt_id: String },
    /// The tool honours the delivery key: retry with the same key.
    IdempotentRetry {
        attempt_id: String,
        delivery_key: String,
    },
    /// The outcome is unknown and the action is irreversible: stop.
    FailStop { attempt_id: String },
}

/// Decide, per open attempt, what a resume may do.
///
/// This is where "the outcome is unknown" becomes an explicit decision instead
/// of an accidental assumption. Nothing here retries an action that might have
/// happened, and nothing here declares an unknown cost to be zero.
pub fn plan_recovery(journal: &Journal) -> Vec<Recovery> {
    journal
        .open_attempts()
        .into_iter()
        .map(|attempt| match attempt.delivery {
            Delivery::Pure => Recovery::Recompute {
                attempt_id: attempt.attempt_id.clone(),
            },
            Delivery::Replayable => Recovery::ReplayRequiresResult {
                attempt_id: attempt.attempt_id.clone(),
            },
            Delivery::ExternalIdempotent => Recovery::IdempotentRetry {
                attempt_id: attempt.attempt_id.clone(),
                delivery_key: attempt.delivery_key.clone(),
            },
            Delivery::ExternalNonIdempotent => Recovery::FailStop {
                attempt_id: attempt.attempt_id.clone(),
            },
        })
        .collect()
}

/// Charges left open by a resume, with the reason they cannot be closed
/// automatically.
pub fn unresolved_charges(journal: &Journal) -> Vec<(&str, ChargeState)> {
    journal
        .charge_of
        .iter()
        .filter_map(|(charge_id, _)| {
            let state = journal.charge_state(charge_id)?;
            match state {
                ChargeState::Reserved | ChargeState::PendingReconciliation => {
                    Some((charge_id.as_str(), state))
                }
                ChargeState::Settled | ChargeState::Released => None,
            }
        })
        .collect()
}

/// Header written with a journal so a resume can verify it is reading the same
/// world the run was paused in.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct JournalHeader {
    pub schema_version: u32,
    pub run_id: String,
    pub definition_sha256: String,
    /// Version of the need/evidence/operation contracts in force.
    pub contract_version: u32,
    /// Versions of the providers and operations the run depended on.
    pub dependency_versions: BTreeMap<String, u32>,
}

impl JournalHeader {
    pub fn new(run_id: &str, definition_sha256: &str, contract_version: u32) -> Self {
        Self {
            schema_version: JOURNAL_SCHEMA_VERSION,
            run_id: run_id.to_string(),
            definition_sha256: definition_sha256.to_string(),
            contract_version,
            dependency_versions: BTreeMap::new(),
        }
    }

    pub fn with_dependency(mut self, name: &str, version: u32) -> Self {
        self.dependency_versions.insert(name.to_string(), version);
        self
    }

    /// Verify this header against the world a resume is happening in.
    ///
    /// Nothing is repaired or upgraded: a mismatch is an error, because
    /// reinterpreting old events under new rules would silently change what an
    /// investigation concluded.
    pub fn verify(
        &self,
        definition_sha256: &str,
        contract_version: u32,
        dependency_versions: &BTreeMap<String, u32>,
    ) -> Result<(), JournalError> {
        if self.schema_version != JOURNAL_SCHEMA_VERSION {
            return Err(JournalError::UnsupportedVersion(self.schema_version));
        }
        if self.definition_sha256 != definition_sha256 {
            return Err(JournalError::DefinitionChanged {
                expected: self.definition_sha256.clone(),
                found: definition_sha256.to_string(),
            });
        }
        if self.contract_version != contract_version {
            return Err(JournalError::UnsupportedVersion(self.contract_version));
        }
        // Compare in both directions. A dependency the journal recorded but the
        // world no longer has, and a dependency the world now needs but the
        // journal never recorded, are both reasons not to continue: either way
        // the resumed half would run under conditions the first half never saw.
        for (name, recorded) in &self.dependency_versions {
            match dependency_versions.get(name) {
                Some(current) if current == recorded => {}
                Some(current) => {
                    return Err(JournalError::Conflict(format!(
                        "dependency {name} changed from version {recorded} to {current}"
                    )))
                }
                None => {
                    return Err(JournalError::Conflict(format!(
                        "dependency {name} (version {recorded}) is no longer available"
                    )))
                }
            }
        }
        for name in dependency_versions.keys() {
            if !self.dependency_versions.contains_key(name) {
                return Err(JournalError::Conflict(format!(
                    "dependency {name} is required now but was not recorded in the journal"
                )));
            }
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn start(id: &str, delivery: Delivery) -> JournalEvent {
        JournalEvent::AttemptStarted {
            attempt_id: id.to_string(),
            delivery_key: "key".to_string(),
            invocation_id: "inv".to_string(),
            action: "act".to_string(),
            delivery,
            started_at_ms: 1,
        }
    }

    fn confirm(id: &str) -> JournalEvent {
        JournalEvent::ResultConfirmed {
            attempt_id: id.to_string(),
            outcome: CompletionOutcome::Ok(Json::from("ok")),
            confirmed_at_ms: 2,
        }
    }

    #[test]
    fn delivery_key_is_stable_across_attempts() {
        let one = delivery_key("run", "inv", "act");
        let two = delivery_key("run", "inv", "act");
        assert_eq!(one, two);
        assert_ne!(one, delivery_key("run", "inv", "other"));
        assert_eq!(attempt_id(&one, 1), format!("{one}:1"));
        assert_ne!(attempt_id(&one, 1), attempt_id(&one, 2));
    }

    #[test]
    fn the_delivery_key_is_a_pure_function_of_run_invocation_and_action() {
        // Pin the exact digest so any extra field folded into the key (for
        // example the attempt number, which would defeat retries) changes it.
        // Computed by the engine itself: this asserts the inputs, not a hash
        // implementation detail.
        let expected = sha256_hex(&Json::object(vec![
            ("run_id".to_string(), Json::from("run")),
            ("invocation_id".to_string(), Json::from("inv")),
            ("action".to_string(), Json::from("act")),
        ]))
        .unwrap();
        assert_eq!(delivery_key("run", "inv", "act"), expected);
        assert_eq!(expected.len(), 64);

        // Every input participates.
        assert_ne!(expected, delivery_key("other", "inv", "act"));
        assert_ne!(expected, delivery_key("run", "other", "act"));
        assert_ne!(expected, delivery_key("run", "inv", "other"));
    }

    #[test]
    fn a_retry_reuses_the_key_and_gets_a_new_attempt() {
        let key = delivery_key("run", "inv", "act");
        let first = attempt_id(&key, 1);
        let second = attempt_id(&key, 2);
        assert_ne!(first, second, "each try has its own identity");
        assert!(first.starts_with(&key) && second.starts_with(&key));
    }

    #[test]
    fn an_unconfirmed_attempt_is_open_not_confirmed() {
        let mut journal = Journal::new();
        journal
            .append(start("a1", Delivery::ExternalNonIdempotent))
            .unwrap();
        assert_eq!(journal.open_attempts().len(), 1);
        assert!(journal.confirmed("a1").is_none());
    }

    #[test]
    fn a_confirmed_attempt_leaves_the_open_set() {
        let mut journal = Journal::new();
        journal.append(start("a1", Delivery::Pure)).unwrap();
        journal.append(confirm("a1")).unwrap();
        assert!(journal.open_attempts().is_empty());
        assert!(journal.confirmed("a1").is_some());
    }

    #[test]
    fn an_abandoned_attempt_leaves_the_open_set_without_a_result() {
        let mut journal = Journal::new();
        journal.append(start("a1", Delivery::Pure)).unwrap();
        journal
            .append(JournalEvent::AttemptAbandoned {
                attempt_id: "a1".to_string(),
                reason: "cancelled".to_string(),
                abandoned_at_ms: 3,
            })
            .unwrap();
        assert!(journal.open_attempts().is_empty());
        assert!(journal.confirmed("a1").is_none());
    }

    #[test]
    fn a_charge_cannot_settle_twice() {
        let mut journal = Journal::new();
        journal.append(start("a1", Delivery::Pure)).unwrap();
        journal.append(confirm("a1")).unwrap();
        journal
            .append(JournalEvent::ChargeReserved {
                charge_id: "c1".to_string(),
                attempt_id: "a1".to_string(),
                delivery_key: "key".to_string(),
                amount_milli: 1_000,
                tariff_milli: 1_000,
                reserved_at_ms: 2,
            })
            .unwrap();
        let settle = JournalEvent::ChargeSettled {
            charge_id: "c1".to_string(),
            amount_milli: 1_000,
            settled_at_ms: 4,
        };
        journal.append(settle.clone()).unwrap();
        assert_eq!(
            journal.append(settle),
            Err(JournalError::DuplicateSettlement("c1".to_string()))
        );
        assert_eq!(journal.settled_total(), 1_000);
    }

    #[test]
    fn a_charge_cannot_settle_before_its_attempt_is_confirmed() {
        let mut journal = Journal::new();
        journal
            .append(start("a1", Delivery::ExternalNonIdempotent))
            .unwrap();
        journal
            .append(JournalEvent::ChargeReserved {
                charge_id: "c1".to_string(),
                attempt_id: "a1".to_string(),
                delivery_key: "key".to_string(),
                amount_milli: 500,
                tariff_milli: 500,
                reserved_at_ms: 2,
            })
            .unwrap();
        let outcome = journal.append(JournalEvent::ChargeSettled {
            charge_id: "c1".to_string(),
            amount_milli: 500,
            settled_at_ms: 3,
        });
        assert!(matches!(outcome, Err(JournalError::Conflict(_))));
        assert_eq!(journal.settled_total(), 0);
    }

    fn reserve(charge: &str, attempt: &str, delivery: &str) -> JournalEvent {
        JournalEvent::ChargeReserved {
            charge_id: charge.to_string(),
            attempt_id: attempt.to_string(),
            delivery_key: delivery.to_string(),
            amount_milli: 500,
            tariff_milli: 500,
            reserved_at_ms: 2,
        }
    }

    #[test]
    fn one_delivery_reserves_at_most_one_charge() {
        // A retry of the same logical delivery must not reserve a second cost.
        let mut journal = Journal::new();
        journal
            .append(start("a1", Delivery::ExternalIdempotent))
            .unwrap();
        journal
            .append(start("a2", Delivery::ExternalIdempotent))
            .unwrap();
        journal.append(reserve("c1", "a1", "key")).unwrap();
        let outcome = journal.append(reserve("c2", "a2", "key"));
        assert!(matches!(outcome, Err(JournalError::Conflict(_))));
        assert!(outcome
            .unwrap_err()
            .to_string()
            .contains("already has a reservation"));
    }

    #[test]
    fn different_deliveries_may_each_reserve() {
        let mut journal = Journal::new();
        journal.append(start("a1", Delivery::Pure)).unwrap();
        journal.append(start("a2", Delivery::Pure)).unwrap();
        journal.append(reserve("c1", "a1", "key-1")).unwrap();
        journal.append(reserve("c2", "a2", "key-2")).unwrap();
        assert_eq!(journal.settled_total(), 0);
    }

    #[test]
    fn an_unknown_cost_cannot_be_released_without_evidence() {
        // Writing an unknown cost off as zero is exactly what must not happen.
        let mut journal = Journal::new();
        journal
            .append(start("a1", Delivery::ExternalNonIdempotent))
            .unwrap();
        journal.append(reserve("c1", "a1", "key")).unwrap();
        journal
            .append(JournalEvent::ChargePendingReconciliation {
                charge_id: "c1".to_string(),
                reason: "side effect may have happened".to_string(),
                at_ms: 3,
            })
            .unwrap();
        let outcome = journal.append(JournalEvent::ChargeReleased {
            charge_id: "c1".to_string(),
            reason: "assume it was free".to_string(),
            released_at_ms: 4,
        });
        assert!(matches!(outcome, Err(JournalError::Conflict(_))));
        assert!(outcome.unwrap_err().to_string().contains("unknown cost"));
        // The charge stays in the operator queue.
        assert_eq!(journal.pending_reconciliation(), vec!["c1"]);
        assert_eq!(journal.settled_total(), 0);
    }

    #[test]
    fn unresolved_charges_is_the_operators_queue() {
        let mut journal = Journal::new();
        journal.append(start("a1", Delivery::Pure)).unwrap();
        journal.append(start("a2", Delivery::Pure)).unwrap();
        journal.append(start("a3", Delivery::Pure)).unwrap();
        journal.append(reserve("c1", "a1", "key-1")).unwrap();
        journal.append(reserve("c2", "a2", "key-2")).unwrap();
        journal.append(reserve("c3", "a3", "key-3")).unwrap();
        journal.append(confirm("a1")).unwrap();
        journal
            .append(JournalEvent::ChargeSettled {
                charge_id: "c1".to_string(),
                amount_milli: 500,
                settled_at_ms: 5,
            })
            .unwrap();
        journal
            .append(JournalEvent::ChargePendingReconciliation {
                charge_id: "c2".to_string(),
                reason: "unknown".to_string(),
                at_ms: 5,
            })
            .unwrap();
        let open = unresolved_charges(&journal);
        let ids: Vec<&str> = open.iter().map(|(id, _)| *id).collect();
        assert!(ids.contains(&"c2"), "a reconciling charge must be visible");
        assert!(
            ids.contains(&"c3"),
            "an untouched reservation must be visible"
        );
        assert!(!ids.contains(&"c1"), "a settled charge needs no attention");
    }

    #[test]
    fn an_unknown_outcome_becomes_pending_reconciliation_not_free() {
        let mut journal = Journal::new();
        journal
            .append(start("a1", Delivery::ExternalNonIdempotent))
            .unwrap();
        journal
            .append(JournalEvent::ChargeReserved {
                charge_id: "c1".to_string(),
                attempt_id: "a1".to_string(),
                delivery_key: "key".to_string(),
                amount_milli: 500,
                tariff_milli: 500,
                reserved_at_ms: 2,
            })
            .unwrap();
        journal
            .append(JournalEvent::ChargePendingReconciliation {
                charge_id: "c1".to_string(),
                reason: "side effect may have happened".to_string(),
                at_ms: 3,
            })
            .unwrap();
        assert_eq!(
            journal.charge_state("c1"),
            Some(ChargeState::PendingReconciliation)
        );
        assert_eq!(journal.pending_reconciliation(), vec!["c1"]);
        assert_eq!(journal.settled_total(), 0);
        // And it cannot later be quietly settled.
        assert!(journal
            .append(JournalEvent::ChargeSettled {
                charge_id: "c1".to_string(),
                amount_milli: 500,
                settled_at_ms: 4,
            })
            .is_err());
    }

    #[test]
    fn a_released_charge_stays_released() {
        let mut journal = Journal::new();
        journal.append(start("a1", Delivery::Pure)).unwrap();
        journal
            .append(JournalEvent::ChargeReserved {
                charge_id: "c1".to_string(),
                attempt_id: "a1".to_string(),
                delivery_key: "key".to_string(),
                amount_milli: 500,
                tariff_milli: 500,
                reserved_at_ms: 2,
            })
            .unwrap();
        journal
            .append(JournalEvent::ChargeReleased {
                charge_id: "c1".to_string(),
                reason: "no cost incurred".to_string(),
                released_at_ms: 3,
            })
            .unwrap();
        assert_eq!(journal.charge_state("c1"), Some(ChargeState::Released));
        assert_eq!(journal.settled_total(), 0);
    }

    #[test]
    fn events_for_unknown_attempts_are_refused() {
        let mut journal = Journal::new();
        assert_eq!(
            journal.append(confirm("nope")),
            Err(JournalError::UnknownReference("nope".to_string()))
        );
    }

    #[test]
    fn every_event_is_kept_in_order() {
        let mut journal = Journal::new();
        journal.append(start("a1", Delivery::Pure)).unwrap();
        journal.append(confirm("a1")).unwrap();
        let kinds: Vec<&str> = journal.events().iter().map(JournalEvent::kind).collect();
        assert_eq!(kinds, vec!["AttemptStarted", "ResultConfirmed"]);
    }

    #[test]
    fn resume_rejects_a_changed_definition() {
        let header = JournalHeader::new("run", "abc", 1);
        let outcome = header.verify("different", 1, &BTreeMap::new());
        assert!(matches!(
            outcome,
            Err(JournalError::DefinitionChanged { .. })
        ));
        assert!(header.verify("abc", 1, &BTreeMap::new()).is_ok());
    }

    #[test]
    fn resume_rejects_a_changed_contract_version() {
        let header = JournalHeader::new("run", "abc", 1);
        assert_eq!(
            header.verify("abc", 2, &BTreeMap::new()),
            Err(JournalError::UnsupportedVersion(1))
        );
    }

    #[test]
    fn resume_rejects_a_changed_or_missing_dependency() {
        let header = JournalHeader::new("run", "abc", 1).with_dependency("tool", 3);
        let mut same = BTreeMap::new();
        same.insert("tool".to_string(), 3);
        assert!(header.verify("abc", 1, &same).is_ok());

        let mut bumped = BTreeMap::new();
        bumped.insert("tool".to_string(), 4);
        assert!(matches!(
            header.verify("abc", 1, &bumped),
            Err(JournalError::Conflict(_))
        ));

        assert!(matches!(
            header.verify("abc", 1, &BTreeMap::new()),
            Err(JournalError::Conflict(_))
        ));
    }

    #[test]
    fn recovery_never_reruns_an_external_irreversible_attempt() {
        let mut journal = Journal::new();
        journal
            .append(start("a1", Delivery::ExternalNonIdempotent))
            .unwrap();
        journal.append(start("a2", Delivery::Pure)).unwrap();
        journal
            .append(start("a3", Delivery::ExternalIdempotent))
            .unwrap();
        journal.append(start("a4", Delivery::Replayable)).unwrap();
        let plan = plan_recovery(&journal);
        assert!(plan.contains(&Recovery::FailStop {
            attempt_id: "a1".to_string()
        }));
        assert!(plan.contains(&Recovery::Recompute {
            attempt_id: "a2".to_string()
        }));
        assert!(plan.contains(&Recovery::IdempotentRetry {
            attempt_id: "a3".to_string(),
            delivery_key: "key".to_string(),
        }));
        assert!(plan.contains(&Recovery::ReplayRequiresResult {
            attempt_id: "a4".to_string()
        }));
        assert_eq!(plan.len(), 4);
    }

    #[test]
    fn confirmed_attempts_are_not_part_of_recovery() {
        let mut journal = Journal::new();
        journal
            .append(start("a1", Delivery::ExternalNonIdempotent))
            .unwrap();
        journal.append(confirm("a1")).unwrap();
        assert!(plan_recovery(&journal).is_empty());
    }

    #[test]
    fn open_charges_survive_a_resume_as_unresolved() {
        let mut journal = Journal::new();
        journal
            .append(start("a1", Delivery::ExternalIdempotent))
            .unwrap();
        journal
            .append(JournalEvent::ChargeReserved {
                charge_id: "c1".to_string(),
                attempt_id: "a1".to_string(),
                delivery_key: "key".to_string(),
                amount_milli: 700,
                tariff_milli: 700,
                reserved_at_ms: 2,
            })
            .unwrap();
        let open = unresolved_charges(&journal);
        assert_eq!(open.len(), 1);
        assert_eq!(open[0].0, "c1");
        assert_eq!(open[0].1, ChargeState::Reserved);
        // It is neither settled nor silently written off.
        assert_eq!(journal.settled_total(), 0);
    }

    #[test]
    fn only_retryable_deliveries_may_be_rerun() {
        assert!(Delivery::Pure.may_retry());
        assert!(Delivery::ExternalIdempotent.may_retry());
        assert!(!Delivery::Replayable.may_retry());
        assert!(!Delivery::ExternalNonIdempotent.may_retry());
        for delivery in [
            Delivery::Pure,
            Delivery::Replayable,
            Delivery::ExternalIdempotent,
            Delivery::ExternalNonIdempotent,
        ] {
            assert_eq!(Delivery::parse(delivery.as_str()), Some(delivery));
        }
        assert_eq!(Delivery::parse("bogus"), None);
    }
}
