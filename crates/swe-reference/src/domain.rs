//! Supplier register: the only data source in the experiment.
//!
//! The workflow input carries the question and the candidate identities, never
//! prices or compliance answers. Those facts live here and are only reachable
//! through the deterministic mock provider, so the workflow has to retrieve and
//! verify them like any other application would.

use swe_native::Json;

use crate::{obj, DemoError, DemoResult};

/// Which version of the supplier register the mock provider serves.
///
/// The scenario is provider configuration, not workflow input: the same SWD
/// definition and the same run input are used for all scenarios.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Scenario {
    /// Every fact the workflow needs is present and unambiguous.
    Baseline,
    /// One fact is absent and one fact has conflicting sources.
    MissingEvidence,
    /// Every fact is present and verified, but no supplier meets all three
    /// requirements: screening succeeds and the comparison has no candidate.
    NoCompliantSupplier,
    /// Two suppliers fail requirement C, so a single round discovers two
    /// separate missing facts. Exercises one need per round.
    TwoExceptions,
    /// The discovered exception fact exists but its source is ambiguous, so the
    /// screening operation cannot use it. Exercises the path where a discovered
    /// need is refused by the engine's gate rather than by a demo-side rule.
    AmbiguousException,
}

impl Scenario {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Baseline => "baseline",
            Self::MissingEvidence => "missing_evidence",
            Self::NoCompliantSupplier => "no_compliant_supplier",
            Self::TwoExceptions => "two_exceptions",
            Self::AmbiguousException => "ambiguous_exception",
        }
    }

    pub fn parse(value: &str) -> DemoResult<Self> {
        match value {
            "baseline" => Ok(Self::Baseline),
            "missing_evidence" => Ok(Self::MissingEvidence),
            "no_compliant_supplier" => Ok(Self::NoCompliantSupplier),
            "two_exceptions" => Ok(Self::TwoExceptions),
            "ambiguous_exception" => Ok(Self::AmbiguousException),
            other => Err(DemoError::new(format!("unknown scenario {other:?}"))),
        }
    }
}

/// A candidate supplier as seen by the workflow: identity only.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Candidate {
    pub id: String,
    pub name: String,
}

impl Candidate {
    pub fn from_json(value: &Json) -> DemoResult<Self> {
        Ok(Self {
            id: crate::field_str(value, "id")?.to_string(),
            name: crate::field_str(value, "name")?.to_string(),
        })
    }

    pub fn to_json(&self) -> Json {
        obj([
            ("id", Json::from(self.id.as_str())),
            ("name", Json::from(self.name.as_str())),
        ])
    }

    pub fn list_from_json(value: &Json) -> DemoResult<Vec<Self>> {
        let items = value
            .as_array()
            .ok_or_else(|| DemoError::new("candidates must be an array"))?;
        items.iter().map(Self::from_json).collect()
    }
}

struct SupplierRecord {
    id: &'static str,
    price_sek: u64,
    requirements: [bool; 3],
    exception: &'static str,
}

const REGISTER: [SupplierRecord; 3] = [
    SupplierRecord {
        id: "alpha",
        price_sek: 800,
        requirements: [true, true, false],
        exception: "none",
    },
    SupplierRecord {
        id: "beta",
        price_sek: 950,
        requirements: [true, true, true],
        exception: "none",
    },
    SupplierRecord {
        id: "gamma",
        price_sek: 900,
        requirements: [true, false, true],
        exception: "none",
    },
];

const SOURCE_PREFIX: &str = "mock://supplier-register/v1";

/// One deterministic observation for `(scenario, supplier, fact)`.
///
/// The returned JSON is the provider's raw observation, not evidence: it has no
/// verification status yet. `choice` is what the SWD provider decision routes
/// on; `value`, `source_ref`, `confidence_bp` and `conflicting_sources` are what
/// the evidence layer verifies.
pub(crate) fn fetch_observation(
    scenario: Scenario,
    supplier: &str,
    fact: &str,
) -> DemoResult<Json> {
    let source_ref = format!("{SOURCE_PREFIX}/{supplier}/{fact}");
    let found = |value: Json| {
        obj([
            ("choice", Json::from("found")),
            ("value", value),
            ("source_ref", Json::from(source_ref.as_str())),
            ("confidence_bp", Json::from_u64(9_500)),
            ("conflicting_sources", Json::Bool(false)),
        ])
    };

    if scenario == Scenario::TwoExceptions && fact == "req_c" {
        // Alpha and Beta both fail requirement C, so screening discovers two
        // separate missing facts in one round.
        return Ok(found(Json::Bool(false)));
    }

    if scenario == Scenario::NoCompliantSupplier && fact == "req_b" {
        // Every supplier keeps its price and requirement A, but nobody meets
        // requirement B: screening succeeds and lists nobody as compliant.
        return Ok(found(Json::Bool(false)));
    }

    if scenario == Scenario::MissingEvidence {
        if supplier == "beta" && fact == "req_c" {
            return Ok(missing(
                "supplier register has no record for beta/req_c",
                &source_ref,
            ));
        }
        if supplier == "gamma" && fact == "price" {
            return Ok(obj([
                ("choice", Json::from("found")),
                ("value", Json::from_u64(900)),
                ("source_ref", Json::from(source_ref.as_str())),
                ("confidence_bp", Json::from_u64(4_500)),
                ("conflicting_sources", Json::Bool(true)),
            ]));
        }
    }

    let Some(record) = REGISTER.iter().find(|record| record.id == supplier) else {
        return Ok(missing(
            format!("unknown supplier {supplier:?}"),
            &source_ref,
        ));
    };
    // One discovered fact has two equally weighted sources: it exists, but it is
    // not usable. The engine's gate must refuse it.
    if scenario == Scenario::AmbiguousException && fact == "c_exception" {
        return Ok(obj([
            ("choice", Json::from("found")),
            ("value", Json::from(record.exception)),
            ("source_ref", Json::from(source_ref.as_str())),
            ("confidence_bp", Json::from_u64(4_000)),
            ("conflicting_sources", Json::Bool(true)),
        ]));
    }

    let observation = match fact {
        "price" => found(Json::from_u64(record.price_sek)),
        "req_a" => found(Json::Bool(record.requirements[0])),
        "req_b" => found(Json::Bool(record.requirements[1])),
        "req_c" => found(Json::Bool(record.requirements[2])),
        "c_exception" => found(Json::from(record.exception)),
        other => missing(format!("unknown fact {other:?}"), &source_ref),
    };
    Ok(observation)
}

fn missing(reason: impl Into<String>, source_ref: &str) -> Json {
    obj([
        ("choice", Json::from("missing")),
        ("source_ref", Json::from(source_ref)),
        ("reason", Json::from(reason.into())),
    ])
}
