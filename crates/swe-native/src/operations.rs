//! Generic operations: typed contracts, the execution gate, and the outcome.
//!
//! An operation declares the facts it requires. The engine decides, from that
//! declaration and the resolved need states, whether the operation may run.
//! This is the part that must not be negotiable: a policy may choose *which*
//! permitted action runs next, but it can never make an under-specified
//! operation executable, and an operation can never fall back to a partial
//! comparison.
//!
//! The domain supplies the executors (what an operation computes and how facts
//! are normalised). It supplies them through [`execute`], which keeps the gate
//! structurally in front of every executor.

use std::fmt;

use crate::needs::{NeedRef, NeedStatus};

/// How an operation decides whether it may run.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ExecutionPolicy {
    /// Every declared input must be present and usable.
    AllRequired,
}

/// What happens when a declared input is not usable.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum MissingOutcome {
    /// Do not run; the conclusion is INSUFFICIENT_EVIDENCE.
    InsufficientEvidence,
}

/// The contract of one operation.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct OperationSpec {
    pub name: String,
    pub required_inputs: Vec<NeedRef>,
    pub execution_policy: ExecutionPolicy,
    pub on_missing: MissingOutcome,
}

impl OperationSpec {
    pub fn new(name: impl Into<String>, required_inputs: Vec<NeedRef>) -> Self {
        Self {
            name: name.into(),
            required_inputs,
            execution_policy: ExecutionPolicy::AllRequired,
            on_missing: MissingOutcome::InsufficientEvidence,
        }
    }

    /// The gate: does the resolved evidence satisfy the contract?
    ///
    /// Returns every missing input, not just the first, so the abstention can
    /// say exactly what was missing.
    ///
    /// Refuses a state list that names the same need twice. Guessing which of
    /// two conflicting statuses wins would make the answer depend on input
    /// order, and the gate's whole purpose is that its verdict is not a matter
    /// of ordering.
    pub fn gate(&self, states: &[(NeedRef, NeedStatus)]) -> Result<(), GateError> {
        ensure_unique_state(states)?;
        match self.execution_policy {
            ExecutionPolicy::AllRequired => {
                let missing: Vec<MissingInput> = self
                    .required_inputs
                    .iter()
                    .filter_map(|required| {
                        let status = states
                            .iter()
                            .find(|(need, _)| need == required)
                            .map(|(_, status)| *status)
                            .unwrap_or(NeedStatus::Unresolved);
                        if status.is_resolved() {
                            None
                        } else {
                            Some(MissingInput {
                                need: required.clone(),
                                status,
                            })
                        }
                    })
                    .collect();
                if missing.is_empty() {
                    Ok(())
                } else {
                    Err(GateError::Unsatisfied(GateFailure {
                        operation: self.name.clone(),
                        outcome: self.on_missing,
                        missing,
                    }))
                }
            }
        }
    }
}

/// Refuse a state list that names the same need twice.
///
/// The single implementation of that rule: both [`OperationSpec::gate`] and
/// [`execute`] call it, so there is one place where a duplicate is defined and
/// one place to mutate when testing it.
fn ensure_unique_state(states: &[(NeedRef, NeedStatus)]) -> Result<(), GateError> {
    let mut seen = std::collections::BTreeSet::new();
    for (need, _) in states {
        if !seen.insert(need.clone()) {
            return Err(GateError::DuplicateState { need: need.clone() });
        }
    }
    Ok(())
}

/// Why an operation was not allowed to run.
///
/// The two cases are different facts and must not be conflated. A contract that
/// is not satisfied is a normal abstention. A state list that names the same
/// need twice is a malformed call: reporting it as "insufficient evidence"
/// would turn a host bug into a conclusion.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum GateError {
    /// Two statuses were supplied for the same need. `need` is the first one
    /// that appeared twice, in input order, so the error is deterministic.
    DuplicateState { need: NeedRef },
    /// The contract is not satisfied.
    Unsatisfied(GateFailure),
}

impl GateError {
    /// The contract was genuinely not satisfied.
    pub fn unsatisfied(&self) -> Option<&GateFailure> {
        match self {
            Self::Unsatisfied(failure) => Some(failure),
            Self::DuplicateState { .. } => None,
        }
    }
}

impl fmt::Display for GateError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::DuplicateState { need } => write!(
                f,
                "the gate refuses a state list that names {} twice: which status wins would depend on order",
                need.id()
            ),
            Self::Unsatisfied(failure) => failure.fmt(f),
        }
    }
}

impl std::error::Error for GateError {}

/// One declared input the gate could not accept.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct MissingInput {
    pub need: NeedRef,
    pub status: NeedStatus,
}

/// Why an operation was not allowed to run.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct GateFailure {
    pub operation: String,
    pub outcome: MissingOutcome,
    pub missing: Vec<MissingInput>,
}

impl fmt::Display for GateFailure {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        let names: Vec<String> = self
            .missing
            .iter()
            .map(|input| format!("{} ({})", input.need, input.status.as_str()))
            .collect();
        write!(
            f,
            "INSUFFICIENT_EVIDENCE: operation {} ALL_REQUIRED gate is missing {} required input(s): {}",
            self.operation,
            self.missing.len(),
            names.join(", ")
        )
    }
}

impl std::error::Error for GateFailure {}

/// What an operation produced.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum OpOutcome {
    /// The operation ran and produced a value.
    Answer(String),
    /// The operation discovered a need its contract does not cover.
    NeedMore(NeedRef),
    /// The gate refused to run it.
    InsufficientEvidence(GateFailure),
}

/// Why an investigation stopped without an answer.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum StopReason {
    /// The gate refused: at least one required input was missing.
    InsufficientEvidence(GateFailure),
    /// The operation asked for the identical need twice.
    RepeatedNeed(NeedRef),
    /// The round budget ran out before the operation could answer.
    RoundLimit(u32),
    /// The operation itself reported it could not decide.
    ExecutorGaveUp,
}

impl StopReason {
    /// The stable wire spelling a host reports to a caller.
    pub fn conclusion(&self) -> &'static str {
        match self {
            Self::InsufficientEvidence(_)
            | Self::RepeatedNeed(_)
            | Self::RoundLimit(_)
            | Self::ExecutorGaveUp => "INSUFFICIENT_EVIDENCE",
        }
    }

    pub fn detail(&self) -> String {
        match self {
            Self::InsufficientEvidence(failure) => failure.to_string(),
            Self::RepeatedNeed(need) => format!("INSUFFICIENT_EVIDENCE: repeated need {need}"),
            Self::RoundLimit(rounds) => {
                format!("INSUFFICIENT_EVIDENCE: no answer within {rounds} round(s)")
            }
            Self::ExecutorGaveUp => {
                "INSUFFICIENT_EVIDENCE: the operation could not decide from verified evidence"
                    .to_string()
            }
        }
    }
}

/// The result of investigating until the operation can answer.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Investigation {
    Answer(String),
    Stopped(StopReason),
}

impl Investigation {
    pub fn conclusion(&self) -> &'static str {
        match self {
            Self::Answer(_) => "answered",
            Self::Stopped(reason) => reason.conclusion(),
        }
    }
}

/// Run one operation under its gate, bounded by a round budget.
///
/// The gate runs **before** the executor on every round, including the first.
/// `resolve` attempts to satisfy a discovered need and returns whether it
/// succeeded.
///
/// Returns the typed [`GateError`] rather than a conclusion when the state list
/// is malformed: a duplicate status is a bug in the caller, and turning it into
/// `INSUFFICIENT_EVIDENCE` would report it as a fact about the world.
pub fn execute<X, R>(
    spec: &OperationSpec,
    states: &mut Vec<(NeedRef, NeedStatus)>,
    mut executor: X,
    mut resolve: R,
    max_rounds: u32,
) -> Result<Investigation, GateError>
where
    X: FnMut() -> OpOutcome,
    R: FnMut(&NeedRef) -> Option<NeedStatus>,
{
    // The same rule as the gate's, applied before the loop: with a round budget
    // of zero the loop body never runs, and a malformed list would then look
    // like a clean round-limit stop — a conclusion about the world.
    ensure_unique_state(states)?;
    let mut seen: Vec<NeedRef> = Vec::new();
    for _ in 0..max_rounds {
        match spec.gate(states) {
            Ok(()) => {}
            // A genuine refusal is a conclusion: the operation did not run.
            Err(GateError::Unsatisfied(failure)) => {
                return Ok(Investigation::Stopped(StopReason::InsufficientEvidence(
                    failure,
                )));
            }
            // A malformed state list is not a conclusion. Propagate it.
            Err(duplicate @ GateError::DuplicateState { .. }) => return Err(duplicate),
        }
        match executor() {
            OpOutcome::Answer(answer) => return Ok(Investigation::Answer(answer)),
            OpOutcome::InsufficientEvidence(failure) => {
                return Ok(Investigation::Stopped(StopReason::InsufficientEvidence(
                    failure,
                )));
            }
            OpOutcome::NeedMore(need) => {
                // The loop guard lives here, not in the caller: an operation
                // that keeps asking for the same fact can never progress.
                if seen.contains(&need) {
                    return Ok(Investigation::Stopped(StopReason::RepeatedNeed(need)));
                }
                seen.push(need.clone());
                match resolve(&need) {
                    Some(status) => {
                        // A resolver that answers for a need the state list
                        // already holds would create the duplicate the gate
                        // refuses. Catch it here, where it is created.
                        if states.iter().any(|(known, _)| known == &need) {
                            return Err(GateError::DuplicateState { need });
                        }
                        states.push((need, status));
                    }
                    None => {
                        return Ok(Investigation::Stopped(StopReason::InsufficientEvidence(
                            GateFailure {
                                operation: spec.name.clone(),
                                outcome: spec.on_missing,
                                missing: vec![MissingInput {
                                    need,
                                    status: NeedStatus::Unresolved,
                                }],
                            },
                        )));
                    }
                }
            }
        }
    }
    Ok(Investigation::Stopped(StopReason::RoundLimit(max_rounds)))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn need(fact: &str, subject: &str) -> NeedRef {
        NeedRef::new(fact, subject)
    }

    fn spec(subjects: &[&str]) -> OperationSpec {
        OperationSpec::new(
            "COMPARE",
            subjects
                .iter()
                .map(|subject| need("fact_a", subject))
                .collect(),
        )
    }

    fn all_resolved(spec: &OperationSpec) -> Vec<(NeedRef, NeedStatus)> {
        spec.required_inputs
            .iter()
            .map(|required| (required.clone(), NeedStatus::Resolved))
            .collect()
    }

    #[test]
    fn gate_blocks_partial_inputs() {
        let spec = spec(&["doc-a", "doc-b", "doc-c"]);
        let states = vec![(need("fact_a", "doc-a"), NeedStatus::Resolved)];
        let error = spec.gate(&states).unwrap_err();
        let failure = error.unsatisfied().expect("a real verdict");
        assert_eq!(failure.missing.len(), 2);
        assert_eq!(failure.outcome, MissingOutcome::InsufficientEvidence);
        assert!(error.to_string().contains("doc-b.fact_a"));
    }

    #[test]
    fn a_duplicated_status_is_refused_and_names_the_need() {
        let spec = spec(&["doc-a"]);
        let states = vec![
            (need("fact_a", "doc-a"), NeedStatus::Resolved),
            (need("fact_a", "doc-a"), NeedStatus::Ambiguous),
        ];
        let error = spec.gate(&states).expect_err("a duplicate must be refused");
        match error {
            GateError::DuplicateState { need: duplicated } => {
                assert_eq!(duplicated.id(), "doc-a.fact_a");
            }
            other => panic!("a duplicate must be its own error, got {other:?}"),
        }
    }

    #[test]
    fn a_duplicate_is_refused_even_when_both_statuses_agree() {
        // Two identical statuses are still one need described twice. Accepting
        // them would mean the gate silently tolerates a malformed state list.
        let spec = spec(&["doc-a"]);
        let states = vec![
            (need("fact_a", "doc-a"), NeedStatus::Resolved),
            (need("fact_a", "doc-a"), NeedStatus::Resolved),
        ];
        assert!(matches!(
            spec.gate(&states),
            Err(GateError::DuplicateState { .. })
        ));
    }

    #[test]
    fn the_duplicate_verdict_does_not_depend_on_input_order() {
        // The point of the rule: ordering must never decide whether an
        // operation may run, so both orderings produce the same error.
        let spec = spec(&["doc-a"]);
        let resolved = (need("fact_a", "doc-a"), NeedStatus::Resolved);
        let ambiguous = (need("fact_a", "doc-a"), NeedStatus::Ambiguous);
        let first = spec.gate(&[resolved.clone(), ambiguous.clone()]);
        let second = spec.gate(&[ambiguous, resolved]);
        assert_eq!(first, second);
        assert!(matches!(first, Err(GateError::DuplicateState { .. })));
    }

    #[test]
    fn the_named_duplicate_follows_input_order_not_identity_order() {
        // The documented rule is "first duplicated need in input order". To pin
        // it, the first duplicate must NOT also be the smallest by identity:
        // doc-b sorts after doc-a, so a lexicographic implementation would name
        // doc-a here while the rule requires doc-b.
        let spec = spec(&["doc-a", "doc-b"]);
        let states = vec![
            (need("fact_b", "doc-b"), NeedStatus::Resolved),
            (need("fact_b", "doc-b"), NeedStatus::Ambiguous),
            (need("fact_a", "doc-a"), NeedStatus::Resolved),
            (need("fact_a", "doc-a"), NeedStatus::Ambiguous),
        ];
        match spec.gate(&states) {
            Err(GateError::DuplicateState { need: duplicated }) => {
                assert_eq!(
                    duplicated.id(),
                    "doc-b.fact_b",
                    "the first need to repeat in input order must be named"
                );
            }
            other => panic!("expected a duplicate, got {other:?}"),
        }
        // The same input always names the same need.
        assert_eq!(spec.gate(&states), spec.gate(&states));
    }

    #[test]
    fn a_duplicate_is_not_reported_as_insufficient_evidence() {
        // A malformed call must never masquerade as an abstention.
        let spec = spec(&["doc-a"]);
        let states = vec![
            (need("fact_a", "doc-a"), NeedStatus::Unresolved),
            (need("fact_a", "doc-a"), NeedStatus::Resolved),
        ];
        let error = spec.gate(&states).unwrap_err();
        assert!(error.unsatisfied().is_none());
        assert!(!error.to_string().starts_with("INSUFFICIENT_EVIDENCE"));
        assert!(error.to_string().contains("doc-a.fact_a"));
    }

    #[test]
    fn gate_passes_when_every_input_is_resolved() {
        let spec = spec(&["doc-a", "doc-b"]);
        assert!(spec.gate(&all_resolved(&spec)).is_ok());
    }

    #[test]
    fn an_unresolved_need_cannot_vanish_from_the_gate() {
        // The regression this type exists for: one of three resolved must never
        // make the operation look runnable.
        let spec = spec(&["doc-a", "doc-b", "doc-c"]);
        let mut states = vec![(need("fact_a", "doc-a"), NeedStatus::Resolved)];
        states.retain(|(need, _)| need.fact == "fact_a");
        assert_eq!(spec.required_inputs.len(), 3);
        assert!(spec.gate(&states).is_err());
    }

    #[test]
    fn ambiguous_is_not_accepted_by_the_gate() {
        let spec = spec(&["doc-a"]);
        let states = vec![(need("fact_a", "doc-a"), NeedStatus::Ambiguous)];
        assert!(spec.gate(&states).is_err());
    }

    #[test]
    fn execute_refuses_a_caller_supplied_duplicate_instead_of_abstaining() {
        // A malformed state list must not be laundered into a conclusion. The
        // caller hears about its bug; the world hears nothing.
        let spec = spec(&["doc-a"]);
        let mut states = vec![
            (need("fact_a", "doc-a"), NeedStatus::Resolved),
            (need("fact_a", "doc-a"), NeedStatus::Ambiguous),
        ];
        let outcome = execute(
            &spec,
            &mut states,
            || OpOutcome::Answer("x".into()),
            |_| None,
            3,
        );
        match outcome {
            Err(GateError::DuplicateState { need }) => assert_eq!(need.id(), "doc-a.fact_a"),
            other => panic!("expected a typed duplicate error, got {other:?}"),
        }
    }

    #[test]
    fn a_zero_round_budget_cannot_hide_a_duplicate() {
        // The gate runs inside the loop, so a round budget of zero would let a
        // malformed state list slip through as a clean round-limit stop. The
        // list is validated before the loop for exactly this case.
        let spec = spec(&["doc-a"]);
        let mut states = vec![
            (need("fact_a", "doc-a"), NeedStatus::Resolved),
            (need("fact_a", "doc-a"), NeedStatus::Ambiguous),
        ];
        let outcome = execute(
            &spec,
            &mut states,
            || OpOutcome::Answer("x".into()),
            |_| None,
            0,
        );
        assert!(
            matches!(outcome, Err(GateError::DuplicateState { .. })),
            "a duplicate must not become a round-limit conclusion, got {outcome:?}"
        );
    }

    #[test]
    fn execute_refuses_a_duplicate_that_the_resolver_creates() {
        // The engine loop can create the duplicate itself: the resolver answers
        // for a need the state list already holds. That is caught where it is
        // created, not silently merged.
        let spec = spec(&["doc-a"]);
        let mut states = vec![(need("fact_a", "doc-a"), NeedStatus::Resolved)];
        let outcome = execute(
            &spec,
            &mut states,
            || OpOutcome::NeedMore(need("fact_a", "doc-a")),
            |_| Some(NeedStatus::Resolved),
            3,
        );
        match outcome {
            Err(GateError::DuplicateState { need }) => assert_eq!(need.id(), "doc-a.fact_a"),
            other => panic!("expected a typed duplicate error, got {other:?}"),
        }
        // When the need is already unresolved the gate refuses before the
        // executor runs, so the resolver is never consulted and no duplicate is
        // created. That is an abstention, not an error.
        let mut states = vec![(need("fact_a", "doc-a"), NeedStatus::Unresolved)];
        let mut resolved_calls = 0;
        let outcome = execute(
            &spec,
            &mut states,
            || OpOutcome::NeedMore(need("fact_a", "doc-a")),
            |_| {
                resolved_calls += 1;
                Some(NeedStatus::Resolved)
            },
            3,
        );
        assert_eq!(resolved_calls, 0, "the gate must run before the executor");
        assert!(matches!(
            outcome,
            Ok(Investigation::Stopped(StopReason::InsufficientEvidence(_)))
        ));
    }

    #[test]
    fn executor_never_runs_on_partial_inputs() {
        let spec = spec(&["doc-a", "doc-b"]);
        let mut states = vec![(need("fact_a", "doc-a"), NeedStatus::Resolved)];
        let mut calls = 0;
        let outcome = execute(
            &spec,
            &mut states,
            || {
                calls += 1;
                OpOutcome::Answer("should not run".into())
            },
            |_| None,
            4,
        )
        .expect("execute must not see a malformed state list");
        assert_eq!(calls, 0, "the gate must reject before the executor runs");
        assert!(matches!(
            outcome,
            Investigation::Stopped(StopReason::InsufficientEvidence(_))
        ));
    }

    #[test]
    fn need_more_is_resolved_and_the_operation_reruns() {
        let spec = spec(&["doc-a"]);
        let mut states = all_resolved(&spec);
        let mut rounds = 0;
        let outcome = execute(
            &spec,
            &mut states,
            || {
                rounds += 1;
                if rounds == 1 {
                    OpOutcome::NeedMore(need("condition", "doc-x"))
                } else {
                    OpOutcome::Answer("rule applies".into())
                }
            },
            |_| Some(NeedStatus::Resolved),
            3,
        )
        .expect("execute must not see a malformed state list");
        assert_eq!(outcome.conclusion(), "answered");
        assert_eq!(rounds, 2);
    }

    #[test]
    fn an_unresolvable_discovered_need_ends_in_insufficient_evidence() {
        let spec = spec(&["doc-a"]);
        let mut states = all_resolved(&spec);
        let outcome = execute(
            &spec,
            &mut states,
            || OpOutcome::NeedMore(need("condition", "doc-x")),
            |_| None,
            3,
        )
        .expect("execute must not see a malformed state list");
        assert_eq!(outcome.conclusion(), "INSUFFICIENT_EVIDENCE");
    }

    #[test]
    fn identical_need_more_twice_ends_the_investigation() {
        let spec = spec(&["doc-a"]);
        let mut states = all_resolved(&spec);
        let outcome = execute(
            &spec,
            &mut states,
            || OpOutcome::NeedMore(need("condition", "doc-x")),
            |_| Some(NeedStatus::Resolved),
            5,
        )
        .expect("execute must not see a malformed state list");
        assert!(matches!(
            outcome,
            Investigation::Stopped(StopReason::RepeatedNeed(_))
        ));
    }

    #[test]
    fn the_gate_runs_every_round() {
        let spec = spec(&["doc-a", "doc-b"]);
        let mut states = vec![(need("fact_a", "doc-a"), NeedStatus::Resolved)];
        let outcome = execute(
            &spec,
            &mut states,
            || OpOutcome::Answer("never".into()),
            |_| Some(NeedStatus::Resolved),
            4,
        )
        .expect("execute must not see a malformed state list");
        // doc-b was never resolved because the executor never ran to discover it.
        assert_eq!(outcome.conclusion(), "INSUFFICIENT_EVIDENCE");
    }

    #[test]
    fn round_limit_is_a_clean_stop() {
        let spec = spec(&[]);
        let mut states = vec![];
        let mut rounds = 0;
        let outcome = execute(
            &spec,
            &mut states,
            || {
                rounds += 1;
                OpOutcome::NeedMore(need("more", &format!("doc-{rounds}")))
            },
            |_| Some(NeedStatus::Resolved),
            3,
        )
        .expect("execute must not see a malformed state list");
        assert_eq!(outcome.conclusion(), "INSUFFICIENT_EVIDENCE");
        assert!(matches!(
            outcome,
            Investigation::Stopped(StopReason::RoundLimit(3))
        ));
    }

    #[test]
    fn every_stop_reason_reports_insufficient_evidence() {
        let spec = spec(&["doc-a"]);
        let error = spec.gate(&[]).unwrap_err();
        let failure = error.unsatisfied().expect("a real verdict").clone();
        for reason in [
            StopReason::InsufficientEvidence(failure.clone()),
            StopReason::RepeatedNeed(need("x", "doc-x")),
            StopReason::RoundLimit(1),
            StopReason::ExecutorGaveUp,
        ] {
            assert_eq!(reason.conclusion(), "INSUFFICIENT_EVIDENCE");
            assert!(reason.detail().starts_with("INSUFFICIENT_EVIDENCE"));
        }
        assert_eq!(Investigation::Answer("x".into()).conclusion(), "answered");
    }
}
