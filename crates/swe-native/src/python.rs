//! PyO3 surface. Convert, then drop the GIL before mutating run state.

use pyo3::exceptions::{PyRuntimeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyBool, PyDict, PyList, PyMapping, PyNone, PyTuple};
use serde_json::Number;
use std::sync::Mutex;

use crate::canonicalize::{canonical_json_bytes, sha256_hex};
use crate::effects::{Effect, StepOutcome};
use crate::error::RuntimeError;
use crate::events::RuntimeEvent;
use crate::expression::evaluate_expression;
use crate::json::Json;
use crate::machine::{start_run, WorkflowMachine};

#[pymodule]
fn swe_native(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<PyMachine>()?;
    m.add_function(wrap_pyfunction!(py_sha256_hex, m)?)?;
    m.add_function(wrap_pyfunction!(py_canonical_json_bytes, m)?)?;
    m.add_function(wrap_pyfunction!(py_evaluate_expression, m)?)?;
    Ok(())
}

#[pyfunction]
#[pyo3(name = "sha256_hex")]
fn py_sha256_hex(value: Bound<'_, PyAny>) -> PyResult<String> {
    let json = json_from_py(&value)?;
    sha256_hex(&json).map_err(runtime_to_py)
}

#[pyfunction]
#[pyo3(name = "canonical_json_bytes")]
fn py_canonical_json_bytes(value: Bound<'_, PyAny>) -> PyResult<Vec<u8>> {
    let json = json_from_py(&value)?;
    canonical_json_bytes(&json).map_err(runtime_to_py)
}

#[pyfunction]
#[pyo3(name = "evaluate_expression")]
fn py_evaluate_expression(
    expr: Bound<'_, PyAny>,
    context: Bound<'_, PyAny>,
) -> PyResult<Py<PyAny>> {
    let expr = json_from_py(&expr)?;
    let context = json_from_py(&context)?;
    let value = evaluate_expression(&expr, &context).map_err(runtime_to_py)?;
    Python::with_gil(|py| json_to_py(py, &value))
}

#[pyclass(name = "Machine")]
struct PyMachine {
    inner: Mutex<Option<WorkflowMachine>>,
}

#[pymethods]
impl PyMachine {
    #[new]
    fn new() -> Self {
        Self {
            inner: Mutex::new(None),
        }
    }

    #[pyo3(signature = (document, sha256, run_id, input, now_ms))]
    fn start(
        &self,
        py: Python<'_>,
        document: Bound<'_, PyAny>,
        sha256: &str,
        run_id: &str,
        input: Bound<'_, PyAny>,
        now_ms: u64,
    ) -> PyResult<Py<PyAny>> {
        let document = json_from_py(&document)?;
        let input = json_from_py(&input)?;
        let run_id = run_id.to_string();
        let sha = sha256.to_string();
        let (machine, outcome) = py
            .allow_threads(|| start_run(document, &sha, run_id, input, now_ms))
            .map_err(runtime_to_py)?;
        *self.lock()? = Some(machine);
        outcome_to_py(py, &outcome)
    }

    #[pyo3(signature = (invocation_id, output, now_ms))]
    fn complete(
        &self,
        py: Python<'_>,
        invocation_id: &str,
        output: Bound<'_, PyAny>,
        now_ms: u64,
    ) -> PyResult<Py<PyAny>> {
        let output = json_from_py(&output)?;
        let invocation_id = invocation_id.to_string();
        let outcome = self.with_machine(py, |machine| {
            machine.complete(&invocation_id, output, now_ms)
        })?;
        outcome_to_py(py, &outcome)
    }

    #[pyo3(signature = (invocation_id, message, now_ms))]
    fn fail(
        &self,
        py: Python<'_>,
        invocation_id: &str,
        message: &str,
        now_ms: u64,
    ) -> PyResult<Py<PyAny>> {
        let invocation_id = invocation_id.to_string();
        let error = RuntimeError::handler(message.to_string());
        let outcome =
            self.with_machine(py, |machine| machine.fail(&invocation_id, error, now_ms))?;
        outcome_to_py(py, &outcome)
    }

    fn cancel(&self, py: Python<'_>, now_ms: u64) -> PyResult<Py<PyAny>> {
        let outcome = self.with_machine(py, |machine| machine.cancel(now_ms))?;
        outcome_to_py(py, &outcome)
    }

    fn status(&self) -> PyResult<String> {
        Ok(self
            .lock()?
            .as_ref()
            .map(|machine| machine.status().as_str().to_string())
            .unwrap_or_else(|| "idle".to_string()))
    }

    fn events(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        let events = self
            .lock()?
            .as_ref()
            .map(|machine| machine.events().to_vec())
            .unwrap_or_default();
        let list = PyList::empty(py);
        for event in events {
            list.append(event_to_py(py, &event)?)?;
        }
        Ok(list.into_any().unbind())
    }

    fn output(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        let output = self
            .lock()?
            .as_ref()
            .map(WorkflowMachine::output)
            .unwrap_or_else(Json::empty_object);
        json_to_py(py, &output)
    }

    fn error(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        match self.lock()?.as_ref().and_then(WorkflowMachine::error) {
            Some(error) => error_to_py(py, error),
            None => Ok(py.None()),
        }
    }
}

impl PyMachine {
    fn lock(&self) -> PyResult<std::sync::MutexGuard<'_, Option<WorkflowMachine>>> {
        self.inner
            .lock()
            .map_err(|_| PyRuntimeError::new_err("swe machine lock poisoned"))
    }

    fn with_machine(
        &self,
        py: Python<'_>,
        op: impl FnOnce(&mut WorkflowMachine) -> StepOutcome + Send,
    ) -> PyResult<StepOutcome> {
        let mut machine = self
            .lock()?
            .take()
            .ok_or_else(|| PyRuntimeError::new_err("machine has not been started"))?;
        let outcome = py.allow_threads(|| op(&mut machine));
        *self.lock()? = Some(machine);
        Ok(outcome)
    }
}

fn json_from_py(value: &Bound<'_, PyAny>) -> PyResult<Json> {
    if value.is_none() {
        return Ok(Json::Null);
    }
    if value.is_instance_of::<PyBool>() {
        return Ok(Json::Bool(value.extract::<bool>()?));
    }
    if let Ok(number) = value.extract::<i128>() {
        return int_from_i128(number);
    }
    if let Ok(number) = value.extract::<f64>() {
        if !number.is_finite() {
            return Err(PyValueError::new_err(
                "NaN and Infinity are not allowed in canonical JSON",
            ));
        }
        return Number::from_f64(number)
            .map(Json::Number)
            .ok_or_else(|| PyValueError::new_err("NaN and Infinity are not allowed"));
    }
    if let Ok(text) = value.extract::<String>() {
        return Ok(Json::String(text));
    }
    if let Ok(list) = value.downcast::<PyList>() {
        return list_from_py(list.iter());
    }
    if let Ok(tuple) = value.downcast::<PyTuple>() {
        return list_from_py(tuple.iter());
    }
    if let Ok(mapping) = value.downcast::<PyMapping>() {
        return object_from_mapping(mapping.clone());
    }
    if let Ok(dict) = value.downcast::<PyDict>() {
        return object_from_mapping(dict.as_mapping().clone());
    }
    Err(PyValueError::new_err(format!(
        "unsupported JSON type {}",
        value.get_type().name()?
    )))
}

fn int_from_i128(number: i128) -> PyResult<Json> {
    if let Ok(value) = i64::try_from(number) {
        return Ok(Json::from(value));
    }
    if let Ok(value) = u64::try_from(number) {
        return Ok(Json::from_u64(value));
    }
    Err(PyValueError::new_err(
        "integer is outside the JSON number range supported by the Rust runtime",
    ))
}

fn list_from_py<'py>(items: impl Iterator<Item = Bound<'py, PyAny>>) -> PyResult<Json> {
    let mut out = Vec::new();
    for item in items {
        out.push(json_from_py(&item)?);
    }
    Ok(Json::Array(out))
}

fn object_from_mapping(mapping: Bound<'_, PyMapping>) -> PyResult<Json> {
    let mut out = indexmap::IndexMap::new();
    for item in mapping.items()?.iter() {
        let pair = item.downcast::<PyTuple>()?;
        let key = pair.get_item(0)?.extract::<String>()?;
        let value = json_from_py(&pair.get_item(1)?)?;
        out.insert(key, value);
    }
    Ok(Json::Object(out))
}

fn json_to_py(py: Python<'_>, value: &Json) -> PyResult<Py<PyAny>> {
    match value {
        Json::Null => Ok(PyNone::get(py).to_owned().into_any().unbind()),
        Json::Bool(value) => Ok(PyBool::new(py, *value).to_owned().into_any().unbind()),
        Json::Number(number) => {
            if let Some(value) = number.as_i64() {
                return Ok(value.into_pyobject(py)?.into_any().unbind());
            }
            if let Some(value) = number.as_u64() {
                return Ok(value.into_pyobject(py)?.into_any().unbind());
            }
            let value = number
                .as_f64()
                .ok_or_else(|| PyValueError::new_err("unsupported JSON number"))?;
            Ok(value.into_pyobject(py)?.into_any().unbind())
        }
        Json::String(value) => Ok(value.into_pyobject(py)?.into_any().unbind()),
        Json::Array(items) => {
            let list = PyList::empty(py);
            for item in items {
                list.append(json_to_py(py, item)?)?;
            }
            Ok(list.into_any().unbind())
        }
        Json::Object(map) => {
            let dict = PyDict::new(py);
            for (key, item) in map {
                dict.set_item(key, json_to_py(py, item)?)?;
            }
            Ok(dict.into_any().unbind())
        }
    }
}

fn outcome_to_py(py: Python<'_>, outcome: &StepOutcome) -> PyResult<Py<PyAny>> {
    let dict = PyDict::new(py);
    dict.set_item("status", outcome.status.as_str())?;
    dict.set_item("ignored", outcome.ignored)?;
    let effects = PyList::empty(py);
    for effect in &outcome.effects {
        effects.append(effect_to_py(py, effect)?)?;
    }
    dict.set_item("effects", effects)?;
    Ok(dict.into_any().unbind())
}

fn effect_to_py(py: Python<'_>, effect: &Effect) -> PyResult<Py<PyAny>> {
    let dict = PyDict::new(py);
    dict.set_item("type", effect.kind())?;
    match effect {
        Effect::InvokeHandler {
            run_id,
            invocation_id,
            node_id,
            branch_path,
            handler,
            input,
        } => {
            dict.set_item("run_id", run_id)?;
            dict.set_item("invocation_id", invocation_id)?;
            dict.set_item("node_id", node_id)?;
            dict.set_item("branch_path", branch_path)?;
            dict.set_item("handler", handler)?;
            dict.set_item("input", json_to_py(py, input)?)?;
        }
        Effect::CancelInvocation { invocation_id } => {
            dict.set_item("invocation_id", invocation_id)?;
        }
    }
    Ok(dict.into_any().unbind())
}

fn event_to_py(py: Python<'_>, event: &RuntimeEvent) -> PyResult<Py<PyAny>> {
    let dict = PyDict::new(py);
    dict.set_item("type", event.kind.as_str())?;
    dict.set_item("run_id", &event.run_id)?;
    dict.set_item("seq", event.seq)?;
    dict.set_item("definition_sha256", &event.definition_sha256)?;
    dict.set_item(
        "node_instance_id",
        opt_str(event.node_instance_id.as_deref()),
    )?;
    dict.set_item("node_id", opt_str(event.node_id.as_deref()))?;
    dict.set_item("result_ref", opt_str(event.result_ref.as_deref()))?;
    dict.set_item("payload", json_to_py(py, &event.payload)?)?;
    if let Some(lineage) = &event.lineage {
        let item = PyDict::new(py);
        item.set_item("branch_path", &lineage.branch_path)?;
        item.set_item(
            "fork_instance_id",
            opt_str(lineage.fork_instance_id.as_deref()),
        )?;
        item.set_item(
            "map_instance_id",
            opt_str(lineage.map_instance_id.as_deref()),
        )?;
        item.set_item("loop_epoch", lineage.loop_epoch)?;
        dict.set_item("lineage", item)?;
    } else {
        dict.set_item("lineage", py.None())?;
    }
    Ok(dict.into_any().unbind())
}

fn error_to_py(py: Python<'_>, error: &RuntimeError) -> PyResult<Py<PyAny>> {
    let dict = PyDict::new(py);
    dict.set_item("kind", error.kind.to_string())?;
    dict.set_item("message", &error.message)?;
    dict.set_item("path", &error.path)?;
    Ok(dict.into_any().unbind())
}

fn opt_str(value: Option<&str>) -> Option<&str> {
    value
}

fn runtime_to_py(error: RuntimeError) -> PyErr {
    PyValueError::new_err(format!("{}: {}", error.kind, error.message))
}
