//! Domain-neutral reference experiment for the SWE workflow core.
//!
//! The demo drives one supplier-selection question through the `swe-native`
//! SWD 1.0 kernel and meters every action with the `swe-budget` Sweycoin ledger.
//! It contains no product configuration, no network access and no model calls:
//! the only data source is a deterministic in-memory provider, and the
//! expected conclusion (Beta) is never given to the workflow.
//!
//! Engine pieces reused as-is:
//!
//! - `swe_native`: SWD document loading, the host-paced [`WorkflowMachine`],
//!   the [`swe_native::Effect`] commands and the canonical JSON / SHA-256
//!   definition hash.
//! - `swe_budget`: the append-only Sweycoin ledger (`allocate` /
//!   `reserve` / `settle` / `replay`) used for the budget and its journal.
//!
//! Information needs, their status, evidence classification and the
//! ALL_REQUIRED gate are **engine** primitives (`swe_native::needs`,
//! `swe_native::evidence`, `swe_native::operations`). This crate owns only its
//! domain vocabulary (a supplier register, its facts and its screening rule)
//! and the mapping onto those primitives; it decides nothing the engine decides.
//! See the README section "What is reused vs. what is application layer".

mod domain;
mod evidence;
mod host;
mod journal;
mod mock_provider;
mod operations;
mod workflow;

use std::fmt;

pub use domain::{Candidate, Scenario};
pub use evidence::{Evidence, EvidenceSet, EvidenceStatus, StatusCounts};
pub use host::{
    resume, run, ActionCharge, ActionKind, InvestigationOutcome, InvocationLog, PausedReport,
    ResumeConfig, RunConfig, RunReport, SegmentReport, Tariffs, ACCOUNT,
};
pub use journal::{
    read_journal, tariff_sha256, write_journal, Journal, JournalCompletedInvocation,
};
pub use mock_provider::MockSupplierFacts;
pub use operations::{
    cheapest_compliant, gate_failure_json, gate_inputs, need_ref, screen_compliance,
    select_cheapest, Need, NeedRef, OperationContract, OperationRegistry, OperationResult,
    RankedSupplier, NO_COMPLIANT_SUPPLIER,
};
pub use swe_native::{Json, RunStatus};
pub use workflow::{
    candidates, question, workflow_document, workflow_document_sha256, workflow_input,
};

/// Error type for the application layer of the demo.
///
/// Engine and ledger errors are wrapped verbatim; nothing is swallowed and
/// there are no fallback paths.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct DemoError {
    message: String,
}

impl DemoError {
    pub fn new(message: impl Into<String>) -> Self {
        Self {
            message: message.into(),
        }
    }

    pub fn message(&self) -> &str {
        &self.message
    }
}

impl fmt::Display for DemoError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.message)
    }
}

impl std::error::Error for DemoError {}

impl From<swe_native::RuntimeError> for DemoError {
    fn from(error: swe_native::RuntimeError) -> Self {
        Self::new(format!("kernel: {error}"))
    }
}

impl From<swe_budget::LedgerError> for DemoError {
    fn from(error: swe_budget::LedgerError) -> Self {
        Self::new(format!("ledger: {error}"))
    }
}

impl From<std::io::Error> for DemoError {
    fn from(error: std::io::Error) -> Self {
        Self::new(format!("journal io: {error}"))
    }
}

pub type DemoResult<T> = std::result::Result<T, DemoError>;

/// Compact builder for JSON objects with static keys.
pub(crate) fn obj(pairs: impl IntoIterator<Item = (&'static str, Json)>) -> Json {
    Json::Object(
        pairs
            .into_iter()
            .map(|(key, value)| (key.to_string(), value))
            .collect(),
    )
}

pub(crate) fn field<'a>(value: &'a Json, key: &str) -> DemoResult<&'a Json> {
    value
        .get(key)
        .ok_or_else(|| DemoError::new(format!("missing field {key:?}")))
}

pub(crate) fn field_str<'a>(value: &'a Json, key: &str) -> DemoResult<&'a str> {
    field(value, key)?
        .as_str()
        .ok_or_else(|| DemoError::new(format!("field {key:?} must be a string")))
}

pub(crate) fn field_array<'a>(value: &'a Json, key: &str) -> DemoResult<&'a [Json]> {
    field(value, key)?
        .as_array()
        .ok_or_else(|| DemoError::new(format!("field {key:?} must be an array")))
}

pub(crate) fn as_u64(value: &Json) -> Option<u64> {
    value.as_number().and_then(|number| number.as_u64())
}

pub(crate) fn field_u64(value: &Json, key: &str) -> DemoResult<u64> {
    as_u64(field(value, key)?)
        .ok_or_else(|| DemoError::new(format!("field {key:?} must be an unsigned integer")))
}
