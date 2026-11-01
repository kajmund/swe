//! SWE in-memory execution kernel. No OverGraph, Jev, or research knowledge.

mod canonicalize;
mod convert;
mod definition;
mod effects;
mod error;
mod events;
mod evidence;
mod expression;
mod journal;
mod json;
mod machine;
mod needs;
mod operations;
mod path;
mod preflight;

pub use canonicalize::{canonical_json_bytes, canonicalize, sha256_hex, CANONICAL_SCHEMA_VERSION};
pub use convert::{json_from_serde, json_to_serde, parse_json_str};
pub use definition::CompiledDefinition;
pub use effects::{Effect, RunStatus, StepOutcome};
pub use error::{ErrorKind, Result, RuntimeError};
pub use events::{EventKind, EventSink, Lineage, MemoryEventSink, RuntimeEvent};
pub use evidence::{
    classify, resolve_need, Admissibility, Evidence, EvidenceAssessment, EvidencePolicy,
    EvidenceStatus, Provenance,
};
pub use expression::evaluate_expression;
pub use journal::{
    attempt_id, delivery_key, plan_recovery, unresolved_charges, ChargeState, CompletionOutcome,
    Delivery, Journal, JournalError, JournalEvent, JournalHeader, OpenAttempt, Recovery,
    JOURNAL_SCHEMA_VERSION,
};
pub use json::Json;
pub use machine::{start_run, WorkflowMachine};
pub use needs::{
    derive_needs, Need, NeedCardinality, NeedDiscovery, NeedLoopError, NeedPriority, NeedRef,
    NeedStatus,
};
pub use operations::{
    execute, ExecutionPolicy, GateError, GateFailure, Investigation, MissingInput, MissingOutcome,
    OpOutcome, OperationSpec, StopReason,
};

#[cfg(feature = "python")]
mod python;
