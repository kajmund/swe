//! Application-layer operation contracts, needs and deterministic comparison.
//!
//! The workflow never decides *what to fetch*. That comes from operation
//! contracts: an operation declares the facts it must have (`required_inputs`),
//! and the plan handler derives the information-need list from those contracts —
//! before any supplier data has been read. A later screening round can discover
//! an additional conditional fact it cannot satisfy; it reports that as a
//! `NeedMore` result and the investigation resolves the new need before
//! re-running the operation.

use swe_native::Json;

use crate::domain::Candidate;
use crate::evidence::{EvidenceSet, EvidenceStatus};
use crate::{as_u64, field, field_array, field_str, obj, DemoError, DemoResult};
use swe_native::OpOutcome;

pub const COMPLIANCE_FACTS: [&str; 3] = ["req_a", "req_b", "req_c"];
pub const PRICE_FACT: &str = "price";
pub const EXCEPTION_FACT: &str = "c_exception";
pub const SCREEN_OPERATION: &str = "screen_compliance";
pub const PRICE_OPERATION: &str = "select_cheapest";
/// Terminal conclusion when screening succeeds but nobody is compliant.
pub const NO_COMPLIANT_SUPPLIER: &str = "NO_COMPLIANT_SUPPLIER";

/// A fact a need refers to: `(supplier, fact)` with a stable id.
/// The engine's need identity, with the demo's supplier/fact mapping.
///
/// B2 made `swe_native::NeedRef` the single source of truth for need identity,
/// so this crate no longer defines its own: the supplier is the engine's
/// `subject` and the fact is the engine's `fact`.
pub use swe_native::NeedRef;

/// Build the demo's need reference: the supplier is the subject.
pub fn need_ref(supplier: impl Into<String>, fact: impl Into<String>) -> NeedRef {
    NeedRef::new(fact, supplier)
}

pub(crate) fn need_to_json_pub(need: &NeedRef) -> Json {
    obj([
        ("id", Json::from(need.id().as_str())),
        ("supplier", Json::from(need.subject.clone())),
        ("fact", Json::from(need.fact.clone())),
    ])
}

pub(crate) fn need_from_json_pub(value: &Json) -> DemoResult<NeedRef> {
    Ok(NeedRef::new(
        field_str(value, "fact")?,
        field_str(value, "supplier")?,
    ))
}

/// An information need derived from a contract (or discovered by an operation).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Need {
    pub reference: NeedRef,
    pub reason: String,
}

impl Need {
    pub fn new(reference: NeedRef, reason: impl Into<String>) -> Self {
        Self {
            reference,
            reason: reason.into(),
        }
    }

    pub fn to_json(&self) -> Json {
        let mut value = need_to_json_pub(&self.reference);
        if let Json::Object(object) = &mut value {
            object.insert("reason".to_string(), Json::from(self.reason.as_str()));
        }
        value
    }
}

/// One operation's declared inputs. This is what requirement planning reads.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct OperationContract {
    pub id: String,
    pub required: Vec<NeedRef>,
}

impl OperationContract {
    pub fn new(id: impl Into<String>, required: Vec<NeedRef>) -> Self {
        Self {
            id: id.into(),
            required,
        }
    }

    pub fn to_json(&self) -> Json {
        obj([
            ("id", Json::from(self.id.as_str())),
            (
                "required",
                Json::Array(self.required.iter().map(need_to_json_pub).collect()),
            ),
        ])
    }

    pub fn from_json(value: &Json) -> DemoResult<Self> {
        let required = field_array(value, "required")?
            .iter()
            .map(need_from_json_pub)
            .collect::<DemoResult<Vec<_>>>()?;
        Ok(Self {
            id: field_str(value, "id")?.to_string(),
            required,
        })
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct OperationRegistry {
    pub contracts: Vec<OperationContract>,
}

impl OperationRegistry {
    /// Derive the contracts for a candidate set. Nothing here depends on
    /// resolved supplier data; the registry is an application constant shaped by
    /// the candidate list.
    pub fn for_candidates(candidates: &[Candidate]) -> Self {
        let compliance = candidates
            .iter()
            .flat_map(|candidate| {
                COMPLIANCE_FACTS
                    .iter()
                    .map(move |fact| need_ref(&candidate.id, *fact))
            })
            .collect();
        let prices = candidates
            .iter()
            .map(|candidate| need_ref(&candidate.id, PRICE_FACT))
            .collect();
        Self {
            contracts: vec![
                OperationContract::new(SCREEN_OPERATION, compliance),
                OperationContract::new(PRICE_OPERATION, prices),
            ],
        }
    }

    pub fn to_json(&self) -> Json {
        Json::Array(
            self.contracts
                .iter()
                .map(OperationContract::to_json)
                .collect(),
        )
    }

    pub fn from_json(value: &Json) -> DemoResult<Self> {
        let items = value
            .as_array()
            .ok_or_else(|| DemoError::new("operations must be an array"))?;
        Ok(Self {
            contracts: items
                .iter()
                .map(OperationContract::from_json)
                .collect::<DemoResult<Vec<_>>>()?,
        })
    }

    pub fn contract(&self, operation: &str) -> DemoResult<&OperationContract> {
        self.contracts
            .iter()
            .find(|contract| contract.id == operation)
            .ok_or_else(|| DemoError::new(format!("operation {operation:?} was not planned")))
    }

    fn needs(&self, operation: &str) -> DemoResult<Vec<Need>> {
        let contract = self.contract(operation)?;
        Ok(contract
            .required
            .iter()
            .map(|reference| {
                Need::new(
                    reference.clone(),
                    format!("operation {operation} declares this fact as a required input"),
                )
            })
            .collect())
    }
}

/// The engine's gate, applied to the demo's evidence.
///
/// This is a projection, not a policy. It converts each piece of evidence into
/// the engine's [`swe_native::NeedStatus`] and asks
/// [`swe_native::OperationSpec::gate`]. The demo owns the mapping from its own
/// evidence vocabulary to the engine's; it does not own the rule.
///
/// B2 review debt: the demo used to carry a second `all_required`
/// implementation of the same rule, which is exactly the "two sources of truth"
/// this programme exists to remove.
pub fn gate_inputs(
    operation: &str,
    required: &[NeedRef],
    evidence: &EvidenceSet,
) -> DemoResult<Option<swe_native::GateFailure>> {
    let states: Vec<(NeedRef, swe_native::NeedStatus)> = evidence
        .to_vec()
        .iter()
        .map(|entry| {
            let need = need_ref(entry.supplier.clone(), entry.fact.clone());
            (need, engine_status(entry.status))
        })
        .collect();
    let spec = swe_native::OperationSpec::new(operation, required.to_vec());
    match spec.gate(&states) {
        Ok(()) => Ok(None),
        Err(swe_native::GateError::Unsatisfied(failure)) => Ok(Some(failure)),
        // An `EvidenceSet` is keyed by need id, so it cannot contain the same
        // need twice. If this ever fires, the demo's own bookkeeping is broken.
        Err(swe_native::GateError::DuplicateState { need }) => Err(DemoError::new(format!(
            "the evidence set produced two statuses for {}",
            need.id()
        ))),
    }
}

/// The demo's evidence vocabulary, mapped onto the engine's need states.
fn engine_status(status: EvidenceStatus) -> swe_native::NeedStatus {
    match status {
        EvidenceStatus::Verified => swe_native::NeedStatus::Resolved,
        EvidenceStatus::Ambiguous => swe_native::NeedStatus::Ambiguous,
        EvidenceStatus::Rejected => swe_native::NeedStatus::Unresolved,
    }
}

/// The handler-protocol rendering of a gate refusal.
///
/// Every field is read from the engine's [`swe_native::GateFailure`]; nothing
/// here decides anything.
pub fn gate_failure_json(failure: &swe_native::GateFailure) -> Json {
    obj([
        (
            "missing",
            Json::Array(
                failure
                    .missing
                    .iter()
                    .map(|input| {
                        obj([
                            ("need", need_to_json_pub(&input.need)),
                            ("status", Json::from(input.status.as_str())),
                            (
                                "reason",
                                Json::from(format!(
                                    "{}: {}",
                                    input.status.as_str(),
                                    input.need.id()
                                )),
                            ),
                        ])
                    })
                    .collect(),
            ),
        ),
        ("detail", Json::from(failure.to_string())),
    ])
}

/// Handler that plans the information needs from operation contracts.
pub fn plan_needs_handler(input: &Json) -> DemoResult<Json> {
    let candidates = Candidate::list_from_json(field(input, "candidates")?)?;
    let registry = OperationRegistry::for_candidates(&candidates);
    let compliance = registry.needs(SCREEN_OPERATION)?;
    let prices = registry.needs(PRICE_OPERATION)?;
    let total = compliance.len() + prices.len();
    Ok(obj([
        (
            "needs_compliance",
            Json::Array(compliance.iter().map(Need::to_json).collect()),
        ),
        (
            "needs_prices",
            Json::Array(prices.iter().map(Need::to_json).collect()),
        ),
        ("operations", registry.to_json()),
        ("required_inputs_total", Json::from_usize(total)),
    ]))
}

/// What an operation produced: the engine's typed outcome plus the demo's
/// payload for the handler protocol.
///
/// The engine owns the *semantics* — `Answer`, the singular `NeedMore(NeedRef)`
/// and `InsufficientEvidence(GateFailure)` (`swe_native::operations`). This
/// wrapper only carries the JSON the task handler returns alongside it, which
/// is application data the engine has no business knowing.
#[derive(Clone, Debug, PartialEq)]
pub struct OperationResult {
    pub outcome: OpOutcome,
    pub payload: Json,
}

impl OperationResult {
    pub fn answer(payload: Json) -> Self {
        let text = canonical_text(&payload);
        Self {
            outcome: OpOutcome::Answer(text),
            payload,
        }
    }

    pub fn need_more(need: swe_native::NeedRef) -> Self {
        Self {
            outcome: OpOutcome::NeedMore(need),
            payload: Json::Null,
        }
    }

    pub fn insufficient(failure: swe_native::GateFailure) -> Self {
        // Every field comes from the engine's failure; the demo only formats it.
        let payload = gate_failure_json(&failure);
        Self {
            outcome: OpOutcome::InsufficientEvidence(failure),
            payload,
        }
    }

    pub fn status(&self) -> &'static str {
        match self.outcome {
            OpOutcome::Answer(_) => "ok",
            OpOutcome::NeedMore(_) => "need_more",
            OpOutcome::InsufficientEvidence(_) => "insufficient",
        }
    }

    /// The flat JSON object a `task` handler returns.
    pub fn into_handler_json(self) -> Json {
        match self.outcome {
            OpOutcome::Answer(_) => with_status(self.payload, "ok", ("answer_text", Json::Null)),
            // The engine's protocol is singular: one need per round. The map
            // node that fetches it takes a list, so the host wraps the one need
            // rather than the engine returning many.
            OpOutcome::NeedMore(need) => obj([
                ("status", Json::from("need_more")),
                ("need", Json::from(need.id())),
                ("id", Json::from(need.id())),
                (
                    "needs",
                    Json::Array(vec![obj([
                        ("id", Json::from(need.id())),
                        ("supplier", Json::from(need.subject.clone())),
                        ("fact", Json::from(need.fact.clone())),
                        ("reason", Json::from("discovered by the operation")),
                    ])]),
                ),
                (
                    "reason",
                    Json::from(
                        "the operation discovered a conditional fact its contract does not cover",
                    ),
                ),
            ]),
            OpOutcome::InsufficientEvidence(failure) => {
                let reason = match self.payload.get("reason").and_then(Json::as_str) {
                    Some(text) => Json::from(text),
                    None => Json::from(failure.to_string()),
                };
                with_status(self.payload, "insufficient", ("reason", reason))
            }
        }
    }
}

/// Canonical text of a payload, used where the engine protocol needs a string.
fn canonical_text(value: &Json) -> String {
    String::from_utf8(swe_native::canonical_json_bytes(value).unwrap_or_default())
        .unwrap_or_default()
}

fn with_status(payload: Json, status: &str, extra: (&str, Json)) -> Json {
    match payload {
        Json::Object(mut map) => {
            map.insert("status".to_string(), Json::from(status));
            map.insert(extra.0.to_string(), extra.1);
            Json::Object(map)
        }
        other => other,
    }
}

enum Screening {
    Compliant(String),
    NonCompliant(String),
    NeedsException(Need),
    Insufficient {
        failure: swe_native::GateFailure,
        reason: String,
    },
}

/// Handler wrapper: serialises the typed [`OpOutcome`] for the kernel.
pub fn screen_compliance_handler(input: &Json) -> DemoResult<Json> {
    Ok(screen_compliance(input)?.into_handler_json())
}

/// Screening operation: ALL_REQUIRED gate, then the requirements rule.
///
/// Requirement C has a documented-exception path. When a candidate fails C the
/// operation discovers that it needs the exception fact, which is *not* part of
/// its static contract; it returns [`OpOutcome::NeedMore`] for it.
pub fn screen_compliance(input: &Json) -> DemoResult<OperationResult> {
    let candidates = Candidate::list_from_json(field(input, "candidates")?)?;
    let registry = OperationRegistry::from_json(field(input, "operations")?)?;
    let evidence = EvidenceSet::from_json(field(input, "evidence")?)?;
    let contract = registry.contract(SCREEN_OPERATION)?;
    if let Some(failure) = gate_inputs(SCREEN_OPERATION, &contract.required, &evidence)? {
        return Ok(OperationResult::insufficient(failure));
    }

    let mut needs = Vec::new();
    let mut compliant = Vec::new();
    let mut supplier_states = Vec::new();
    for candidate in &candidates {
        match screen_candidate(candidate, &evidence)? {
            Screening::Compliant(reason) => {
                compliant.push(candidate.id.clone());
                supplier_states.push(supplier_state(candidate, true, reason));
            }
            Screening::NonCompliant(reason) => {
                supplier_states.push(supplier_state(candidate, false, reason));
            }
            Screening::NeedsException(need) => needs.push(need),
            Screening::Insufficient { failure, reason } => {
                // The failure came from the engine inside `screen_candidate`;
                // nothing is re-decided here. The payload keeps the engine's
                // missing list and adds the demo's explanatory reason.
                let mut payload = gate_failure_json(&failure);
                if let Json::Object(object) = &mut payload {
                    object.insert("reason".to_string(), Json::from(reason));
                }
                return Ok(OperationResult {
                    outcome: OpOutcome::InsufficientEvidence(failure),
                    payload,
                });
            }
        }
    }

    if let Some(first) = needs.first() {
        // One need per round: the engine's protocol is singular, so the demo
        // resolves this one deterministically and the others surface later.
        return Ok(OperationResult::need_more(first.reference.clone()));
    }

    Ok(OperationResult::answer(obj([
        (
            "compliant",
            Json::Array(compliant.iter().map(|id| Json::from(id.as_str())).collect()),
        ),
        ("supplier_states", Json::Array(supplier_states)),
        ("gate", obj([("satisfied", Json::Bool(true))])),
    ])))
}

fn screen_candidate(candidate: &Candidate, evidence: &EvidenceSet) -> DemoResult<Screening> {
    let req_a = required_bool(evidence, &candidate.id, "req_a")?;
    let req_b = required_bool(evidence, &candidate.id, "req_b")?;
    let req_c = required_bool(evidence, &candidate.id, "req_c")?;
    if !req_a {
        return Ok(Screening::NonCompliant(
            "requirement A is not met".to_string(),
        ));
    }
    if !req_b {
        return Ok(Screening::NonCompliant(
            "requirement B is not met".to_string(),
        ));
    }
    if req_c {
        return Ok(Screening::Compliant(
            "meets requirements A, B and C".to_string(),
        ));
    }
    let exception = need_ref(&candidate.id, EXCEPTION_FACT);
    let exception_id = exception.id();
    // Whether the discovered fact is usable is the engine's decision, not this
    // function's. Ask it, and report its verdict.
    let refusal = gate_inputs(SCREEN_OPERATION, std::slice::from_ref(&exception), evidence)?;
    match (evidence.get(&exception_id), refusal) {
        (None, _) => Ok(Screening::NeedsException(Need::new(
            exception,
            format!(
                "requirement C is not met for {}; the screening rule needs the documented exception before it can decide",
                candidate.name
            ),
        ))),
        (Some(_), Some(failure)) => Ok(Screening::Insufficient {
            reason: format!(
                "INSUFFICIENT_EVIDENCE: the engine refused the discovered fact {}: {}",
                exception_id, failure
            ),
            failure,
        }),
        (Some(entry), None) => {
            if entry.value.as_bool() == Some(true) {
                Ok(Screening::Compliant(
                    "meets requirements A and B; requirement C is covered by a granted exception"
                        .to_string(),
                ))
            } else {
                Ok(Screening::NonCompliant(
                    "requirement C is not met and no exception is granted".to_string(),
                ))
            }
        }
    }
}

fn required_bool(evidence: &EvidenceSet, supplier: &str, fact: &str) -> DemoResult<bool> {
    let need = need_ref(supplier, fact);
    evidence.verified_bool(&need.id()).ok_or_else(|| {
        DemoError::new(format!(
            "ALL_REQUIRED gate passed but {} is not a verified boolean",
            need.id()
        ))
    })
}

fn supplier_state(candidate: &Candidate, compliant: bool, reason: String) -> Json {
    obj([
        ("supplier", Json::from(candidate.id.as_str())),
        ("name", Json::from(candidate.name.as_str())),
        ("compliant", Json::Bool(compliant)),
        ("reason", Json::from(reason.as_str())),
    ])
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct RankedSupplier {
    pub id: String,
    pub name: String,
    pub price_sek: u64,
}

impl RankedSupplier {
    pub fn to_json(&self) -> Json {
        obj([
            ("id", Json::from(self.id.as_str())),
            ("name", Json::from(self.name.as_str())),
            ("price_sek", Json::from_u64(self.price_sek)),
        ])
    }
}

/// Deterministic comparison: lowest price among compliant suppliers, tie-broken
/// by supplier id so the result never depends on map ordering.
pub fn cheapest_compliant(
    candidates: &[Candidate],
    compliant: &[String],
    evidence: &EvidenceSet,
) -> DemoResult<Vec<RankedSupplier>> {
    let mut ranked = Vec::new();
    for candidate in candidates {
        if !compliant.iter().any(|id| id == &candidate.id) {
            continue;
        }
        let need = need_ref(&candidate.id, PRICE_FACT);
        let price_sek = evidence.verified_u64(&need.id()).ok_or_else(|| {
            DemoError::new(format!(
                "compliant supplier {} has no verified price evidence",
                candidate.id
            ))
        })?;
        ranked.push(RankedSupplier {
            id: candidate.id.clone(),
            name: candidate.name.clone(),
            price_sek,
        });
    }
    ranked.sort_by(|left, right| {
        left.price_sek
            .cmp(&right.price_sek)
            .then_with(|| left.id.cmp(&right.id))
    });
    Ok(ranked)
}

/// Handler wrapper: serialises the typed [`OpOutcome`] for the kernel.
pub fn select_cheapest_handler(input: &Json) -> DemoResult<Json> {
    Ok(select_cheapest(input)?.into_handler_json())
}

/// Price operation: ALL_REQUIRED gate, then the deterministic comparison.
pub fn select_cheapest(input: &Json) -> DemoResult<OperationResult> {
    let candidates = Candidate::list_from_json(field(input, "candidates")?)?;
    let registry = OperationRegistry::from_json(field(input, "operations")?)?;
    let evidence = EvidenceSet::from_json(field(input, "evidence")?)?;
    let compliant = field_array(input, "compliant")?
        .iter()
        .map(|item| {
            item.as_str()
                .map(ToString::to_string)
                .ok_or_else(|| DemoError::new("compliant supplier ids must be strings"))
        })
        .collect::<DemoResult<Vec<_>>>()?;
    let contract = registry.contract(PRICE_OPERATION)?;
    if let Some(failure) = gate_inputs(PRICE_OPERATION, &contract.required, &evidence)? {
        return Ok(OperationResult::insufficient(failure));
    }
    let ranked = cheapest_compliant(&candidates, &compliant, &evidence)?;
    let Some(winner) = ranked.first() else {
        return Ok(OperationResult::answer(obj([
            ("answer", Json::Null),
            ("ranked", Json::Array(Vec::new())),
            (
                "reason",
                Json::from("no supplier meets all three requirements"),
            ),
        ])));
    };
    Ok(OperationResult::answer(obj([
        ("answer", winner.to_json()),
        (
            "ranked",
            Json::Array(ranked.iter().map(RankedSupplier::to_json).collect()),
        ),
        (
            "reason",
            Json::from(format!(
                "{} has the lowest price ({} SEK) among {} compliant supplier(s)",
                winner.name,
                winner.price_sek,
                ranked.len()
            )),
        ),
    ])))
}

/// Final report on the completed investigation.
pub fn build_report_handler(input: &Json) -> DemoResult<Json> {
    let screen = field(input, "screen")?;
    let price = field(input, "price")?;
    let evidence = EvidenceSet::from_json(field(input, "evidence")?)?;
    let answer = field(price, "answer")?;
    let reason = field_str(price, "reason")?;
    let evidence_status_counts = evidence.counts().to_json();
    let supplier_states = screen
        .get("supplier_states")
        .cloned()
        .unwrap_or(Json::Array(Vec::new()));

    if answer.is_missing_compatible_null() {
        // Screening ran and every required fact was verified, but no candidate
        // satisfies the requirements. That is a complete answer: abstain from
        // naming a supplier instead of dereferencing a missing one.
        return Ok(obj([
            ("status", Json::from("no_compliant_supplier")),
            ("conclusion", Json::from(NO_COMPLIANT_SUPPLIER)),
            ("conclusion_id", Json::Null),
            ("price_sek", Json::Null),
            ("reason", Json::from(reason)),
            ("supplier_states", supplier_states),
            ("evidence", evidence.to_json()),
            ("evidence_status_counts", evidence_status_counts),
        ]));
    }

    let conclusion = field_str(answer, "name")?;
    let conclusion_id = field_str(answer, "id")?;
    let price_sek = as_u64(field(answer, "price_sek")?)
        .ok_or_else(|| DemoError::new("answer.price_sek must be an unsigned integer"))?;
    Ok(obj([
        ("status", Json::from("ok")),
        ("conclusion", Json::from(conclusion)),
        ("conclusion_id", Json::from(conclusion_id)),
        ("price_sek", Json::from_u64(price_sek)),
        ("reason", Json::from(reason)),
        ("supplier_states", supplier_states),
        ("evidence", evidence.to_json()),
        ("evidence_status_counts", evidence_status_counts),
    ]))
}

/// Terminal abstention: incomplete run with an explicit reason.
pub fn build_abstain_handler(input: &Json) -> DemoResult<Json> {
    let reason = field_str(input, "reason")?;
    let evidence = EvidenceSet::from_json(field(input, "evidence")?)?;
    Ok(obj([
        ("status", Json::from("INSUFFICIENT_EVIDENCE")),
        ("conclusion", Json::from("INSUFFICIENT_EVIDENCE")),
        ("reason", Json::from(reason)),
        ("evidence", evidence.to_json()),
        ("evidence_status_counts", evidence.counts().to_json()),
    ]))
}
