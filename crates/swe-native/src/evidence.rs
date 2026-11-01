//! Generic evidence reduction: what a set of domain assessments means.
//!
//! The split is deliberate. The **domain** decides what evidence is
//! admissible, which values are equivalent, and which facts compete; it emits
//! [`EvidenceAssessment`]. The **engine** validates that data and owns the
//! reduction to [`EvidenceStatus`] and [`crate::needs::NeedStatus`].
//!
//! That keeps the engine honest without teaching it any domain: it sees
//! opaque `equivalence_key` / `conflict_scope` / `operational_role` strings and
//! can be described, tested and used without knowing what a fact means.
//!
//! Reduction rules (see `docs/swe-standalone/11-pr2-b2-core-contracts.md` C2.2):
//!
//! 1. `NotAdmitted` becomes `Rejected`. Rejected evidence is kept for audit and
//!    does **not** block an accepted value.
//! 2. Two or more distinct `equivalence_key`s inside one `conflict_scope` make
//!    that scope `Ambiguous`.
//! 3. Semantically equal normalised values share an `equivalence_key`, so they
//!    are not a conflict.
//! 4. Candidates with different `operational_role` do not compete; a candidate
//!    whose role is unknown counts as competing (fail-safe).
//! 5. `Uncertain` blocks only inside a scope the domain declared operational.
//! 6. A need is `Resolved` only with at least one `Verified` and no competing
//!    ambiguity in its scope.

use crate::needs::{NeedRef, NeedStatus};

/// Where a fact came from. Provenance is part of the contract: evidence
/// without a source is rejected rather than trusted.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct Provenance {
    /// Stable id of the source, meaningful only to the host.
    pub source_id: String,
    /// Human-readable location inside the source (page, cell, span, line).
    pub locator: Option<String>,
    /// Hash of the source content at retrieval time.
    pub content_hash: Option<String>,
    pub retrieved_at_ms: Option<u64>,
    /// Which tool or model produced the observation.
    pub provider: Option<String>,
}

impl Provenance {
    pub fn new(source_id: impl Into<String>) -> Self {
        Self {
            source_id: source_id.into(),
            ..Self::default()
        }
    }
}

/// What the domain decided about one candidate.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Admissibility {
    /// Passed the domain's own rules.
    Admitted,
    /// Failed the domain's own rules (no, rejected, out of range).
    NotAdmitted,
    /// The domain could not decide. Never silently treated as yes.
    Uncertain,
}

/// The domain's typed judgement about one piece of evidence.
///
/// Every string field is opaque to the engine. `policy_version` records which
/// domain rule produced the judgement, so a later audit can tell whether the
/// assessment is comparable.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct EvidenceAssessment {
    pub need: NeedRef,
    pub value: Option<String>,
    pub admissibility: Admissibility,
    /// Candidates with the same key are the same fact value, not a conflict.
    pub equivalence_key: Option<String>,
    /// Facts only compete inside the same scope.
    pub conflict_scope: String,
    /// Optional role; distinct roles in one scope do not compete.
    pub operational_role: Option<String>,
    /// Why the domain decided this, for audit.
    pub reason_code: String,
    pub policy_version: String,
    pub provenance: Option<Provenance>,
}

impl EvidenceAssessment {
    /// A minimal admitted candidate; the domain fills in what it cares about.
    pub fn admitted(need: NeedRef, value: impl Into<String>) -> Self {
        let value = value.into();
        Self {
            equivalence_key: Some(value.clone()),
            need,
            value: Some(value),
            admissibility: Admissibility::Admitted,
            conflict_scope: String::new(),
            operational_role: None,
            reason_code: String::new(),
            policy_version: String::new(),
            provenance: None,
        }
    }

    pub fn with_scope(mut self, scope: impl Into<String>) -> Self {
        self.conflict_scope = scope.into();
        self
    }

    pub fn with_role(mut self, role: impl Into<String>) -> Self {
        self.operational_role = Some(role.into());
        self
    }

    pub fn with_key(mut self, key: impl Into<String>) -> Self {
        self.equivalence_key = Some(key.into());
        self
    }

    pub fn with_provenance(mut self, provenance: Provenance) -> Self {
        self.provenance = Some(provenance);
        self
    }
}

/// Domain-declared policy knobs the engine applies uniformly.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct EvidencePolicy {
    /// Whether an `Uncertain` candidate may block inside an operational scope.
    /// The *decision* stays with the domain; the engine only applies it.
    pub uncertain_blocks_scope: bool,
    /// Provenance is mandatory: evidence without a source is rejected.
    pub require_provenance: bool,
}

impl Default for EvidencePolicy {
    fn default() -> Self {
        Self {
            uncertain_blocks_scope: true,
            require_provenance: true,
        }
    }
}

/// The engine's verdict on one candidate.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum EvidenceStatus {
    Verified,
    Ambiguous,
    Rejected,
}

impl EvidenceStatus {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Verified => "VERIFIED",
            Self::Ambiguous => "AMBIGUOUS",
            Self::Rejected => "REJECTED",
        }
    }

    /// Whether this status may satisfy a need on its own.
    pub fn is_usable(self) -> bool {
        self == Self::Verified
    }
}

/// One candidate with the engine's verdict attached.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Evidence {
    pub need: NeedRef,
    pub value: Option<String>,
    pub status: EvidenceStatus,
    pub source_id: Option<String>,
    pub reason_code: String,
    pub policy_version: String,
}

/// Reduce one candidate. Pure; no hidden state.
pub fn classify(assessment: &EvidenceAssessment, policy: &EvidencePolicy) -> EvidenceStatus {
    if policy.require_provenance && assessment.provenance.is_none() {
        return EvidenceStatus::Rejected;
    }
    match assessment.admissibility {
        // Rejected evidence is recorded but never blocks an accepted value.
        Admissibility::NotAdmitted => EvidenceStatus::Rejected,
        Admissibility::Uncertain => EvidenceStatus::Ambiguous,
        Admissibility::Admitted => EvidenceStatus::Verified,
    }
}

/// Reduce a whole need's candidates and decide whether the need is resolved.
///
/// Ambiguity is computed per `conflict_scope` over *admitted* candidates that
/// share an operational role, and over `Uncertain` candidates only when the
/// domain declared the scope operational.
pub fn resolve_need(
    assessments: &[EvidenceAssessment],
    policy: &EvidencePolicy,
) -> (NeedStatus, Vec<Evidence>) {
    let evid: Vec<Evidence> = assessments
        .iter()
        .map(|assessment| Evidence {
            need: assessment.need.clone(),
            value: assessment.value.clone(),
            status: classify(assessment, policy),
            source_id: assessment
                .provenance
                .as_ref()
                .map(|provenance| provenance.source_id.clone()),
            reason_code: assessment.reason_code.clone(),
            policy_version: assessment.policy_version.clone(),
        })
        .collect();

    // Rule 4: two candidates compete when they are admitted, share a conflict
    // scope, have different equivalence keys, and their operational roles are
    // compatible. An undeterminable role is compatible with every role, so it
    // can never be used to prove that facts are about different things — that
    // fail-safe is what stops a need resolving on possibly unrelated values.
    let mut ambiguous_scope = false;
    let usable: Vec<&EvidenceAssessment> = assessments
        .iter()
        .zip(evid.iter())
        .filter(|(_, evidence)| evidence.status == EvidenceStatus::Verified)
        .map(|(assessment, _)| assessment)
        .collect();

    'pairs: for (index, left) in usable.iter().enumerate() {
        for right in usable.iter().skip(index + 1) {
            if left.conflict_scope != right.conflict_scope {
                continue;
            }
            if left.equivalence_key == right.equivalence_key {
                continue;
            }
            let same_role = match (&left.operational_role, &right.operational_role) {
                (Some(a), Some(b)) => a == b,
                // An unknown role on either side forbids separation.
                _ => true,
            };
            if same_role {
                ambiguous_scope = true;
                break 'pairs;
            }
        }
    }

    if ambiguous_scope {
        // Every verified candidate in the contested scopes cannot be relied on;
        // mark the whole need ambiguous. Rejected stays rejected.
        let evid = evid
            .into_iter()
            .map(|mut evidence| {
                if evidence.status == EvidenceStatus::Verified {
                    evidence.status = EvidenceStatus::Ambiguous;
                }
                evidence
            })
            .collect();
        return (NeedStatus::Ambiguous, evid);
    }

    // Rule 5: `Uncertain` blocks only when the domain declared it blocking.
    let has_uncertain = evid
        .iter()
        .any(|evidence| evidence.status == EvidenceStatus::Ambiguous);
    if policy.uncertain_blocks_scope && has_uncertain {
        return (NeedStatus::Ambiguous, evid);
    }

    let has_verified = evid.iter().any(|evidence| evidence.status.is_usable());
    if has_verified {
        (NeedStatus::Resolved, evid)
    } else {
        (NeedStatus::Unresolved, evid)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn need() -> NeedRef {
        NeedRef::new("fact_a", "subject-a")
    }

    fn admitted(value: &str, scope: &str) -> EvidenceAssessment {
        EvidenceAssessment::admitted(need(), value)
            .with_scope(scope)
            .with_provenance(Provenance::new("source"))
    }

    #[test]
    fn rejected_evidence_is_recorded_but_does_not_block_a_verified_value() {
        let assessments = vec![
            admitted("value_a", "s"),
            EvidenceAssessment {
                need: need(),
                value: None,
                admissibility: Admissibility::NotAdmitted,
                equivalence_key: None,
                conflict_scope: "s".into(),
                operational_role: None,
                reason_code: "NO".into(),
                policy_version: "v1".into(),
                provenance: Some(Provenance::new("source")),
            },
        ];
        let (status, evidence) = resolve_need(&assessments, &EvidencePolicy::default());
        assert_eq!(status, NeedStatus::Resolved);
        assert_eq!(evidence[0].status, EvidenceStatus::Verified);
        assert_eq!(evidence[1].status, EvidenceStatus::Rejected);
    }

    #[test]
    fn normalised_equal_values_are_not_a_conflict() {
        let assessments = vec![
            admitted("value_a", "s").with_key("value_a"),
            admitted("value_a ", "s").with_key("value_a"),
        ];
        let (status, _) = resolve_need(&assessments, &EvidencePolicy::default());
        assert_eq!(status, NeedStatus::Resolved);
    }

    #[test]
    fn two_competing_values_make_the_need_ambiguous() {
        let assessments = vec![admitted("800", "s"), admitted("value_a", "s")];
        let (status, evidence) = resolve_need(&assessments, &EvidencePolicy::default());
        assert_eq!(status, NeedStatus::Ambiguous);
        assert!(evidence
            .iter()
            .all(|entry| entry.status == EvidenceStatus::Ambiguous));
    }

    #[test]
    fn different_operational_roles_do_not_compete() {
        let assessments = vec![
            admitted("value_a", "s").with_role("role_a"),
            admitted("value_b", "s").with_role("role_b"),
        ];
        let (status, _) = resolve_need(&assessments, &EvidencePolicy::default());
        assert_eq!(status, NeedStatus::Resolved);
    }

    #[test]
    fn an_unknown_role_counts_as_competing() {
        let assessments = vec![
            admitted("value_a", "s"),
            admitted("value_b", "s").with_role("other"),
        ];
        let (status, _) = resolve_need(&assessments, &EvidencePolicy::default());
        assert_eq!(status, NeedStatus::Ambiguous);
    }

    #[test]
    fn uncertain_blocks_only_when_the_domain_declares_the_scope_operational() {
        let mut assessment = admitted("value_a", "s");
        assessment.admissibility = Admissibility::Uncertain;

        let blocking = EvidencePolicy {
            uncertain_blocks_scope: true,
            ..EvidencePolicy::default()
        };
        assert_eq!(
            resolve_need(&[assessment.clone()], &blocking).0,
            NeedStatus::Ambiguous
        );

        let permissive = EvidencePolicy {
            uncertain_blocks_scope: false,
            ..EvidencePolicy::default()
        };
        assert_eq!(
            resolve_need(&[assessment], &permissive).0,
            NeedStatus::Unresolved
        );
    }

    #[test]
    fn evidence_without_provenance_is_rejected() {
        let mut assessment = EvidenceAssessment::admitted(need(), "value_a").with_scope("s");
        assessment.provenance = None;
        let (status, evidence) = resolve_need(&[assessment], &EvidencePolicy::default());
        assert_eq!(status, NeedStatus::Unresolved);
        assert_eq!(evidence[0].status, EvidenceStatus::Rejected);
    }

    #[test]
    fn no_candidates_leaves_the_need_unresolved() {
        assert_eq!(
            resolve_need(&[], &EvidencePolicy::default()).0,
            NeedStatus::Unresolved
        );
    }
}
