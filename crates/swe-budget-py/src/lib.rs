//! PyO3 surface for the workspace `swe-budget` ledger.
//!
//! This crate is a shell only: the ledger, its invariants and its event log
//! stay in `crates/swe-budget`. The Python module is `swe_budget`.

use pyo3::exceptions::{PyRuntimeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList};
use std::sync::Mutex;

use budget_core::{Event, EventKind, Ledger, LedgerError};

#[pymodule]
fn swe_budget(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<PyLedger>()?;
    Ok(())
}

#[pyclass(name = "Ledger")]
struct PyLedger {
    inner: Mutex<Ledger>,
}

#[pymethods]
impl PyLedger {
    #[new]
    fn new() -> Self {
        Self {
            inner: Mutex::new(Ledger::new()),
        }
    }

    fn allocate(&self, run_id: &str, account: &str, amount_milli: u64, key: &str) -> PyResult<()> {
        self.lock()?
            .allocate(run_id, account, amount_milli, key)
            .map_err(ledger_to_py)
    }

    fn reserve(&self, run_id: &str, account: &str, amount_milli: u64, key: &str) -> PyResult<u64> {
        self.lock()?
            .reserve(run_id, account, amount_milli, key)
            .map_err(ledger_to_py)
    }

    fn settle(&self, reservation_id: u64, spent_milli: u64, key: &str) -> PyResult<()> {
        self.lock()?
            .settle(reservation_id, spent_milli, key)
            .map_err(ledger_to_py)
    }

    fn release(&self, reservation_id: u64, key: &str) -> PyResult<()> {
        self.lock()?
            .release(reservation_id, key)
            .map_err(ledger_to_py)
    }

    fn balance(&self, py: Python<'_>, account: &str) -> PyResult<Py<PyAny>> {
        let balance = self.lock()?.balance(account);
        let dict = PyDict::new(py);
        dict.set_item("allocated", balance.allocated)?;
        dict.set_item("incoming", balance.incoming)?;
        dict.set_item("outgoing", balance.outgoing)?;
        dict.set_item("spent", balance.spent)?;
        dict.set_item("reserved", balance.reserved)?;
        dict.set_item("available", balance.available())?;
        Ok(dict.into_any().unbind())
    }

    fn events(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        let list = PyList::empty(py);
        for event in self.lock()?.events() {
            list.append(event_to_py(py, event)?)?;
        }
        Ok(list.into_any().unbind())
    }

    #[staticmethod]
    fn replay(events: Bound<'_, PyAny>) -> PyResult<Self> {
        let mut parsed = Vec::new();
        for item in events.try_iter()? {
            parsed.push(event_from_py(&item?)?);
        }
        let ledger = Ledger::replay(&parsed).map_err(ledger_to_py)?;
        Ok(Self {
            inner: Mutex::new(ledger),
        })
    }
}

impl PyLedger {
    fn lock(&self) -> PyResult<std::sync::MutexGuard<'_, Ledger>> {
        self.inner
            .lock()
            .map_err(|_| PyRuntimeError::new_err("ledger lock poisoned"))
    }
}

fn ledger_to_py(error: LedgerError) -> PyErr {
    PyValueError::new_err(error.to_string())
}

fn event_to_py(py: Python<'_>, event: &Event) -> PyResult<Py<PyAny>> {
    let dict = PyDict::new(py);
    dict.set_item("kind", event_kind_name(event.kind))?;
    dict.set_item("run_id", &event.run_id)?;
    dict.set_item("account", &event.account)?;
    match &event.counterpart {
        Some(value) => dict.set_item("counterpart", value)?,
        None => dict.set_item("counterpart", py.None())?,
    }
    dict.set_item("amount_milli", event.amount_milli)?;
    match event.reservation_id {
        Some(value) => dict.set_item("reservation_id", value)?,
        None => dict.set_item("reservation_id", py.None())?,
    }
    dict.set_item("key", &event.key)?;
    Ok(dict.into_any().unbind())
}

fn event_from_py(value: &Bound<'_, PyAny>) -> PyResult<Event> {
    let kind_name: String = value.get_item("kind")?.extract()?;
    let kind = match kind_name.as_str() {
        "allocate" => EventKind::Allocate,
        "reserve" => EventKind::Reserve,
        "spend" => EventKind::Spend,
        "release" => EventKind::Release,
        "transfer" => EventKind::Transfer,
        "reward" => EventKind::Reward,
        other => {
            return Err(PyValueError::new_err(format!(
                "unknown ledger event kind {other:?}"
            )))
        }
    };
    Ok(Event {
        kind,
        run_id: value.get_item("run_id")?.extract()?,
        account: value.get_item("account")?.extract()?,
        counterpart: optional_extract(value, "counterpart")?,
        amount_milli: value.get_item("amount_milli")?.extract()?,
        reservation_id: optional_extract(value, "reservation_id")?,
        key: value.get_item("key")?.extract()?,
    })
}

fn optional_extract<T>(value: &Bound<'_, PyAny>, key: &str) -> PyResult<Option<T>>
where
    T: for<'py> FromPyObject<'py>,
{
    match value.get_item(key) {
        Ok(item) => item.extract(),
        Err(_) => Ok(None),
    }
}

fn event_kind_name(kind: EventKind) -> &'static str {
    match kind {
        EventKind::Allocate => "allocate",
        EventKind::Reserve => "reserve",
        EventKind::Spend => "spend",
        EventKind::Release => "release",
        EventKind::Transfer => "transfer",
        EventKind::Reward => "reward",
    }
}
