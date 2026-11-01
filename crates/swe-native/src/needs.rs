//! Generic information needs: what a conclusion requires, and how that changes.
//!
//! This is the mechanism, not the domain. A need says *which fact about which
//! subject* a conclusion depends on; the domain decides what a fact means. The
//! rules encoded here are the ones that must not be negotiable:
//!
//! - needs are derived from what a conclusion declares it requires, never from
//!   what happened to get resolved (`derive_needs`);
//! - a need that stays unresolved never silently drops out;
//! - an operation that discovers a need it cannot satisfy reports it instead of
//!   guessing (`NeedMore`), and asking for the identical need twice ends the
//!   investigation rather than looping (`NeedDiscovery::record`).
//!
//! Status is a function of evidence, never an assignment: [`NeedStatus`] is
//! derived from [`crate::evidence`] and cannot be set directly.

use std::fmt;

/// Which fact, about what, a conclusion depends on.
///
/// `fact` and `subject` are opaque strings. The engine never interprets them,
/// so any domain can use this type without leaking its vocabulary into it.
#[derive(Clone, Debug, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub struct NeedRef {
    pub fact: String,
    pub subject: String,
}

impl NeedRef {
    pub fn new(fact: impl Into<String>, subject: impl Into<String>) -> Self {
        Self {
            fact: fact.into(),
            subject: subject.into(),
        }
    }

    /// Stable identity used in journals, events and cache keys.
    pub fn id(&self) -> String {
        format!("{}.{}", self.subject, self.fact)
    }
}

impl fmt::Display for NeedRef {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}.{}", self.subject, self.fact)
    }
}

/// How important a need is to the conclusion.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, PartialOrd, Ord)]
pub enum NeedPriority {
    /// The conclusion may not be drawn without it.
    #[default]
    Required,
    Supporting,
    Exploratory,
}

impl NeedPriority {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Required => "REQUIRED",
            Self::Supporting => "SUPPORTING",
            Self::Exploratory => "EXPLORATORY",
        }
    }

    /// Parse the stable wire spelling; unknown values are an error, never a
    /// silent default.
    pub fn parse(value: &str) -> Option<Self> {
        match value {
            "REQUIRED" => Some(Self::Required),
            "SUPPORTING" => Some(Self::Supporting),
            "EXPLORATORY" => Some(Self::Exploratory),
            _ => None,
        }
    }
}

/// How many values a need may resolve to.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub enum NeedCardinality {
    /// Exactly one value satisfies the need.
    #[default]
    Single,
    /// Many values may satisfy it together.
    Multiple,
    /// The investigation does not know yet.
    Unknown,
}

impl NeedCardinality {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Single => "SINGLE",
            Self::Multiple => "MULTIPLE",
            Self::Unknown => "UNKNOWN",
        }
    }

    pub fn parse(value: &str) -> Option<Self> {
        match value {
            "SINGLE" => Some(Self::Single),
            "MULTIPLE" => Some(Self::Multiple),
            "UNKNOWN" => Some(Self::Unknown),
            _ => None,
        }
    }
}

/// One declared need.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Need {
    pub reference: NeedRef,
    pub priority: NeedPriority,
    pub cardinality: NeedCardinality,
    /// Why the need exists, for audit. Opaque to the engine.
    pub reason: String,
}

impl Need {
    pub fn required(reference: NeedRef) -> Self {
        Self {
            reference,
            priority: NeedPriority::Required,
            cardinality: NeedCardinality::Single,
            reason: String::new(),
        }
    }

    pub fn with_priority(mut self, priority: NeedPriority) -> Self {
        self.priority = priority;
        self
    }

    pub fn with_cardinality(mut self, cardinality: NeedCardinality) -> Self {
        self.cardinality = cardinality;
        self
    }

    pub fn with_reason(mut self, reason: impl Into<String>) -> Self {
        self.reason = reason.into();
        self
    }
}

/// The state of a need. Derived from evidence; never set directly.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum NeedStatus {
    /// No verified value yet.
    Unresolved,
    /// One verified, unambiguous value.
    Resolved,
    /// Several values compete for the same fact.
    Ambiguous,
}

impl NeedStatus {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Unresolved => "UNRESOLVED",
            Self::Resolved => "RESOLVED",
            Self::Ambiguous => "AMBIGUOUS",
        }
    }

    pub fn is_resolved(self) -> bool {
        self == Self::Resolved
    }
}

/// Derive the needs a conclusion requires from what it declares.
///
/// Needs come from the contract, not from resolved data: a fact that never got
/// resolved is still a need, and a fact the contract does not declare is not
/// one. Keeping this a pure function on declared inputs is what stops a partial
/// investigation from looking complete.
pub fn derive_needs(required: &[NeedRef]) -> Vec<Need> {
    required.iter().cloned().map(Need::required).collect()
}

/// Why need discovery stopped.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum NeedLoopError {
    /// The operation asked for a need that was already requested. Repeating an
    /// identical request can never make progress, so the investigation ends.
    RepeatedNeed(NeedRef),
}

impl fmt::Display for NeedLoopError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::RepeatedNeed(need) => {
                write!(f, "operation requested the identical need {need} twice")
            }
        }
    }
}

impl std::error::Error for NeedLoopError {}

/// Tracks needs discovered during an investigation.
///
/// The only thing it does is refuse to discover the same need twice: that is
/// the loop guard, and it lives in the engine so no caller can forget it.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct NeedDiscovery {
    seen: Vec<NeedRef>,
}

impl NeedDiscovery {
    pub fn new() -> Self {
        Self::default()
    }

    /// Record a discovered need, or refuse an identical repeat.
    pub fn record(&mut self, need: &NeedRef) -> Result<(), NeedLoopError> {
        if self.seen.contains(need) {
            return Err(NeedLoopError::RepeatedNeed(need.clone()));
        }
        self.seen.push(need.clone());
        Ok(())
    }

    pub fn seen(&self) -> &[NeedRef] {
        &self.seen
    }

    pub fn is_empty(&self) -> bool {
        self.seen.is_empty()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn needs_come_from_the_declaration_not_from_resolution() {
        // The classic regression: one of three facts happened to resolve. The
        // contract still declares three needs.
        let declared = vec![
            NeedRef::new("fact_a", "doc-a"),
            NeedRef::new("fact_a", "doc-b"),
            NeedRef::new("fact_a", "doc-c"),
        ];
        let needs = derive_needs(&declared);
        assert_eq!(needs.len(), 3);
        assert!(needs
            .iter()
            .all(|need| need.priority == NeedPriority::Required));
        assert_eq!(needs[0].reference.id(), "doc-a.fact_a");
    }

    #[test]
    fn an_empty_declaration_yields_no_needs() {
        assert!(derive_needs(&[]).is_empty());
    }

    #[test]
    fn identical_need_more_twice_ends_the_investigation() {
        let need = NeedRef::new("condition", "doc-x");
        let mut discovery = NeedDiscovery::new();
        assert_eq!(discovery.record(&need), Ok(()));
        assert_eq!(
            discovery.record(&need),
            Err(NeedLoopError::RepeatedNeed(need.clone()))
        );
        assert_eq!(discovery.seen(), &[need]);
    }

    #[test]
    fn different_needs_can_all_be_discovered() {
        let mut discovery = NeedDiscovery::new();
        discovery
            .record(&NeedRef::new("condition", "doc-x"))
            .unwrap();
        discovery
            .record(&NeedRef::new("condition", "doc-y"))
            .unwrap();
        discovery.record(&NeedRef::new("annex", "doc-x")).unwrap();
        assert_eq!(discovery.seen().len(), 3);
    }

    #[test]
    fn priority_and_cardinality_round_trip_through_the_wire_spelling() {
        for priority in [
            NeedPriority::Required,
            NeedPriority::Supporting,
            NeedPriority::Exploratory,
        ] {
            assert_eq!(NeedPriority::parse(priority.as_str()), Some(priority));
        }
        for cardinality in [
            NeedCardinality::Single,
            NeedCardinality::Multiple,
            NeedCardinality::Unknown,
        ] {
            assert_eq!(
                NeedCardinality::parse(cardinality.as_str()),
                Some(cardinality)
            );
        }
        assert_eq!(NeedPriority::parse("bogus"), None);
        assert_eq!(NeedCardinality::parse(""), None);
    }

    #[test]
    fn status_spelling_is_stable() {
        assert_eq!(NeedStatus::Unresolved.as_str(), "UNRESOLVED");
        assert_eq!(NeedStatus::Resolved.as_str(), "RESOLVED");
        assert_eq!(NeedStatus::Ambiguous.as_str(), "AMBIGUOUS");
        assert!(NeedStatus::Resolved.is_resolved());
        assert!(!NeedStatus::Ambiguous.is_resolved());
    }
}
