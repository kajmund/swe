//! Deterministic mock provider: the only data source, fully offline.
//!
//! The provider is a pure function of `(scenario, supplier, fact)`. It emits a
//! raw observation; verification status is assigned later by the evidence
//! layer, never here. The SWD workflow reaches it through the engine's
//! `InvokeProvider` effect and `decision` provider routing, so the provider is
//! an engine-level call, not a hidden handler shortcut.

use swe_native::Json;

use crate::domain::{fetch_observation, Scenario};
use crate::{field_str, DemoResult};

pub const PROVIDER_NAME: &str = "mock.supplier_facts";
pub const PROMPT_KEY: &str = "reference.supplier_fact";

#[derive(Clone, Debug)]
pub struct MockSupplierFacts {
    scenario: Scenario,
}

impl MockSupplierFacts {
    pub fn new(scenario: Scenario) -> Self {
        Self { scenario }
    }

    pub fn scenario(&self) -> Scenario {
        self.scenario
    }

    pub fn prompt_key(&self) -> &'static str {
        PROMPT_KEY
    }

    /// Fetch one observation for one needed fact.
    pub fn fetch(&self, input: &Json) -> DemoResult<Json> {
        let supplier = field_str(input, "supplier")?;
        let fact = field_str(input, "fact")?;
        fetch_observation(self.scenario, supplier, fact)
    }
}
