//! PyO3 binding for the existing journal. Schema v1 is unchanged.

use std::sync::Mutex;

use pyo3::exceptions::{PyRuntimeError, PyValueError};
use pyo3::prelude::*;

use crate::journal::{
    attempt_id, delivery_key, plan_recovery, CompletionOutcome, Delivery, Journal, JournalError,
    JournalEvent, Recovery,
};
use crate::json::Json;

use super::{json_from_py, json_to_py};

#[pyclass(name = "Journal")]
pub(crate) struct PyJournal {
    inner: Mutex<Journal>,
}

impl PyJournal {
    fn lock(&self) -> PyResult<std::sync::MutexGuard<'_, Journal>> {
        self.inner
            .lock()
            .map_err(|_| PyRuntimeError::new_err("swe journal lock poisoned"))
    }
}

#[pymethods]
impl PyJournal {
    #[new]
    fn new() -> Self {
        Self {
            inner: Mutex::new(Journal::new()),
        }
    }

    /// Replay a v1 event list into a new journal. Append order is the log order.
    #[staticmethod]
    fn load(events: &Bound<'_, PyAny>) -> PyResult<Self> {
        let value = json_from_py(events)?;
        let items = value
            .as_array()
            .ok_or_else(|| PyValueError::new_err("journal events must be an array"))?;
        let mut journal = Journal::new();
        for item in items {
            journal
                .append(event_from_json(item)?)
                .map_err(journal_err)?;
        }
        Ok(Self {
            inner: Mutex::new(journal),
        })
    }

    fn append(&self, event: &Bound<'_, PyAny>) -> PyResult<()> {
        let value = json_from_py(event)?;
        self.lock()?
            .append(event_from_json(&value)?)
            .map_err(journal_err)
    }

    fn events(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        let encoded = {
            let journal = self.lock()?;
            events_to_json(journal.events())
        };
        json_to_py(py, &encoded)
    }

    fn plan_recovery(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        let plan = {
            let journal = self.lock()?;
            plan_recovery(&journal)
        };
        json_to_py(py, &recovery_to_json(&plan))
    }
}

#[pyfunction]
#[pyo3(name = "delivery_key")]
pub(crate) fn py_delivery_key(run_id: &str, invocation_id: &str, action: &str) -> String {
    delivery_key(run_id, invocation_id, action)
}

#[pyfunction]
#[pyo3(name = "attempt_id")]
pub(crate) fn py_attempt_id(delivery: &str, attempt_no: u32) -> String {
    attempt_id(delivery, attempt_no)
}

fn journal_err(err: JournalError) -> PyErr {
    PyValueError::new_err(err.to_string())
}

fn events_to_json(events: &[JournalEvent]) -> Json {
    Json::Array(events.iter().map(event_to_json).collect())
}

fn event_to_json(event: &JournalEvent) -> Json {
    match event {
        JournalEvent::AttemptStarted {
            attempt_id,
            delivery_key,
            invocation_id,
            action,
            delivery,
            started_at_ms,
        } => obj([
            ("kind", Json::from("AttemptStarted")),
            ("attempt_id", Json::from(attempt_id.as_str())),
            ("delivery_key", Json::from(delivery_key.as_str())),
            ("invocation_id", Json::from(invocation_id.as_str())),
            ("action", Json::from(action.as_str())),
            ("delivery", Json::from(delivery.as_str())),
            ("started_at_ms", Json::from_u64(*started_at_ms)),
        ]),
        JournalEvent::ResultConfirmed {
            attempt_id,
            outcome,
            confirmed_at_ms,
        } => obj([
            ("kind", Json::from("ResultConfirmed")),
            ("attempt_id", Json::from(attempt_id.as_str())),
            ("outcome", outcome_to_json(outcome)),
            ("confirmed_at_ms", Json::from_u64(*confirmed_at_ms)),
        ]),
        JournalEvent::AttemptAbandoned {
            attempt_id,
            reason,
            abandoned_at_ms,
        } => obj([
            ("kind", Json::from("AttemptAbandoned")),
            ("attempt_id", Json::from(attempt_id.as_str())),
            ("reason", Json::from(reason.as_str())),
            ("abandoned_at_ms", Json::from_u64(*abandoned_at_ms)),
        ]),
        JournalEvent::ChargeReserved {
            charge_id,
            attempt_id,
            delivery_key,
            amount_milli,
            tariff_milli,
            reserved_at_ms,
        } => obj([
            ("kind", Json::from("ChargeReserved")),
            ("charge_id", Json::from(charge_id.as_str())),
            ("attempt_id", Json::from(attempt_id.as_str())),
            ("delivery_key", Json::from(delivery_key.as_str())),
            ("amount_milli", Json::from_u64(*amount_milli)),
            ("tariff_milli", Json::from_u64(*tariff_milli)),
            ("reserved_at_ms", Json::from_u64(*reserved_at_ms)),
        ]),
        JournalEvent::ChargeSettled {
            charge_id,
            amount_milli,
            settled_at_ms,
        } => obj([
            ("kind", Json::from("ChargeSettled")),
            ("charge_id", Json::from(charge_id.as_str())),
            ("amount_milli", Json::from_u64(*amount_milli)),
            ("settled_at_ms", Json::from_u64(*settled_at_ms)),
        ]),
        JournalEvent::ChargeReleased {
            charge_id,
            reason,
            released_at_ms,
        } => obj([
            ("kind", Json::from("ChargeReleased")),
            ("charge_id", Json::from(charge_id.as_str())),
            ("reason", Json::from(reason.as_str())),
            ("released_at_ms", Json::from_u64(*released_at_ms)),
        ]),
        JournalEvent::ChargePendingReconciliation {
            charge_id,
            reason,
            at_ms,
        } => obj([
            ("kind", Json::from("ChargePendingReconciliation")),
            ("charge_id", Json::from(charge_id.as_str())),
            ("reason", Json::from(reason.as_str())),
            ("at_ms", Json::from_u64(*at_ms)),
        ]),
    }
}

fn outcome_to_json(outcome: &CompletionOutcome) -> Json {
    match outcome {
        CompletionOutcome::Ok(value) => obj([("kind", Json::from("ok")), ("value", value.clone())]),
        CompletionOutcome::Err { kind, message } => obj([
            ("kind", Json::from("error")),
            ("error_kind", Json::from(kind.as_str())),
            ("message", Json::from(message.as_str())),
        ]),
        CompletionOutcome::Cancelled => obj([("kind", Json::from("cancelled"))]),
    }
}

fn recovery_to_json(plan: &[Recovery]) -> Json {
    Json::Array(
        plan.iter()
            .map(|item| match item {
                Recovery::Recompute { attempt_id } => obj([
                    ("kind", Json::from("recompute")),
                    ("attempt_id", Json::from(attempt_id.as_str())),
                ]),
                Recovery::ReplayRequiresResult { attempt_id } => obj([
                    ("kind", Json::from("replay_requires_result")),
                    ("attempt_id", Json::from(attempt_id.as_str())),
                ]),
                Recovery::IdempotentRetry {
                    attempt_id,
                    delivery_key,
                } => obj([
                    ("kind", Json::from("idempotent_retry")),
                    ("attempt_id", Json::from(attempt_id.as_str())),
                    ("delivery_key", Json::from(delivery_key.as_str())),
                ]),
                Recovery::FailStop { attempt_id } => obj([
                    ("kind", Json::from("fail_stop")),
                    ("attempt_id", Json::from(attempt_id.as_str())),
                ]),
            })
            .collect(),
    )
}

fn event_from_json(value: &Json) -> PyResult<JournalEvent> {
    let obj = require_object(value)?;
    match field_str(obj, "kind")? {
        "AttemptStarted" => Ok(JournalEvent::AttemptStarted {
            attempt_id: field_string(obj, "attempt_id")?,
            delivery_key: field_string(obj, "delivery_key")?,
            invocation_id: field_string(obj, "invocation_id")?,
            action: field_string(obj, "action")?,
            delivery: Delivery::parse(field_str(obj, "delivery")?)
                .ok_or_else(|| PyValueError::new_err("unknown delivery in journal"))?,
            started_at_ms: field_u64(obj, "started_at_ms")?,
        }),
        "ResultConfirmed" => Ok(JournalEvent::ResultConfirmed {
            attempt_id: field_string(obj, "attempt_id")?,
            outcome: outcome_from_json(
                obj.get("outcome")
                    .ok_or_else(|| PyValueError::new_err("journal field outcome is required"))?,
            )?,
            confirmed_at_ms: field_u64(obj, "confirmed_at_ms")?,
        }),
        "AttemptAbandoned" => Ok(JournalEvent::AttemptAbandoned {
            attempt_id: field_string(obj, "attempt_id")?,
            reason: field_string(obj, "reason")?,
            abandoned_at_ms: field_u64(obj, "abandoned_at_ms")?,
        }),
        "ChargeReserved" => Ok(JournalEvent::ChargeReserved {
            charge_id: field_string(obj, "charge_id")?,
            attempt_id: field_string(obj, "attempt_id")?,
            delivery_key: field_string(obj, "delivery_key")?,
            amount_milli: field_u64(obj, "amount_milli")?,
            tariff_milli: field_u64(obj, "tariff_milli")?,
            reserved_at_ms: field_u64(obj, "reserved_at_ms")?,
        }),
        "ChargeSettled" => Ok(JournalEvent::ChargeSettled {
            charge_id: field_string(obj, "charge_id")?,
            amount_milli: field_u64(obj, "amount_milli")?,
            settled_at_ms: field_u64(obj, "settled_at_ms")?,
        }),
        "ChargeReleased" => Ok(JournalEvent::ChargeReleased {
            charge_id: field_string(obj, "charge_id")?,
            reason: field_string(obj, "reason")?,
            released_at_ms: field_u64(obj, "released_at_ms")?,
        }),
        "ChargePendingReconciliation" => Ok(JournalEvent::ChargePendingReconciliation {
            charge_id: field_string(obj, "charge_id")?,
            reason: field_string(obj, "reason")?,
            at_ms: field_u64(obj, "at_ms")?,
        }),
        other => Err(PyValueError::new_err(format!(
            "unknown journal event {other}"
        ))),
    }
}

fn outcome_from_json(value: &Json) -> PyResult<CompletionOutcome> {
    let obj = require_object(value)?;
    match field_str(obj, "kind")? {
        "ok" => Ok(CompletionOutcome::Ok(
            obj.get("value").cloned().unwrap_or(Json::Null),
        )),
        "error" => Ok(CompletionOutcome::Err {
            kind: field_string(obj, "error_kind")?,
            message: field_string(obj, "message")?,
        }),
        "cancelled" => Ok(CompletionOutcome::Cancelled),
        other => Err(PyValueError::new_err(format!(
            "unknown completion outcome {other}"
        ))),
    }
}

fn require_object(value: &Json) -> PyResult<&indexmap::IndexMap<String, Json>> {
    value
        .as_object()
        .ok_or_else(|| PyValueError::new_err("journal event must be an object"))
}

fn field_str<'a>(obj: &'a indexmap::IndexMap<String, Json>, name: &str) -> PyResult<&'a str> {
    obj.get(name)
        .and_then(Json::as_str)
        .ok_or_else(|| PyValueError::new_err(format!("journal field {name} must be a string")))
}

fn field_string(obj: &indexmap::IndexMap<String, Json>, name: &str) -> PyResult<String> {
    Ok(field_str(obj, name)?.to_string())
}

fn field_u64(obj: &indexmap::IndexMap<String, Json>, name: &str) -> PyResult<u64> {
    obj.get(name)
        .and_then(Json::as_number)
        .and_then(serde_json::Number::as_u64)
        .ok_or_else(|| {
            PyValueError::new_err(format!("journal field {name} must be an unsigned integer"))
        })
}

fn obj<const N: usize>(pairs: [(&str, Json); N]) -> Json {
    Json::object(
        pairs
            .into_iter()
            .map(|(key, value)| (key.to_string(), value)),
    )
}
