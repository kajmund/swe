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
//! are normalised). The gate is the executable boundary: an executor runs only
//! after [`OperationSpec::gate`] has accepted its inputs.

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
/// The single implementation of that rule: [`OperationSpec::gate`] calls it, so
/// there is one place where a duplicate is defined and one place to mutate when
/// testing it.
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
    fn the_gate_reports_why_each_input_is_missing() {
        // The status is the reason: reporting every refusal as UNRESOLVED would
        // hide whether the evidence was absent or ambiguous.
        let spec = spec(&["doc-a", "doc-b"]);
        let states = vec![(need("fact_a", "doc-a"), NeedStatus::Ambiguous)];
        let failure = spec
            .gate(&states)
            .expect_err("both inputs are unusable")
            .unsatisfied()
            .expect("a real verdict")
            .clone();
        assert_eq!(failure.missing.len(), 2);
        assert_eq!(failure.missing[0].status, NeedStatus::Ambiguous);
        assert_eq!(failure.missing[1].status, NeedStatus::Unresolved);

        // A usable input is not reported at all.
        let states = vec![
            (need("fact_a", "doc-a"), NeedStatus::Resolved),
            (need("fact_a", "doc-b"), NeedStatus::Ambiguous),
        ];
        let failure = spec
            .gate(&states)
            .expect_err("doc-b is ambiguous")
            .unsatisfied()
            .expect("a real verdict")
            .clone();
        assert_eq!(failure.missing.len(), 1);
        assert_eq!(failure.missing[0].need.id(), "doc-b.fact_a");
        assert_eq!(failure.missing[0].status, NeedStatus::Ambiguous);
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
}
