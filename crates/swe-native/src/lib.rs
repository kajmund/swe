//! SWE in-memory execution kernel. No OverGraph, Jev, or research knowledge.

mod canonicalize;
mod convert;
mod definition;
mod effects;
mod error;
mod events;
mod expression;
mod json;
mod machine;
mod path;
mod preflight;

pub use canonicalize::{canonical_json_bytes, canonicalize, sha256_hex, CANONICAL_SCHEMA_VERSION};
pub use convert::{json_from_serde, json_to_serde, parse_json_str};
pub use definition::CompiledDefinition;
pub use effects::{Effect, RunStatus, StepOutcome};
pub use error::{ErrorKind, Result, RuntimeError};
pub use events::{EventKind, EventSink, Lineage, MemoryEventSink, RuntimeEvent};
pub use expression::evaluate_expression;
pub use json::Json;
pub use machine::{start_run, WorkflowMachine};

#[cfg(feature = "python")]
mod python;
