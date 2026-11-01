//! The SWD 1.0 definition the demo executes, plus its run input.
//!
//! The document is embedded as JSON so the definition SHA-256 can be computed
//! exactly like a real host would before starting a run. It uses every node
//! type the kernel supports for this shape: `task` for application handlers,
//! `decision` (expression and provider), `fork`/`join` for the two independent
//! evidence waves, `map` for the fan-out over needs, and `end` for the two
//! terminal outcomes.

use swe_native::{parse_json_str, sha256_hex, Json};

use crate::domain::Candidate;
use crate::{obj, DemoResult};

/// The question the investigation answers, Swedish to match the source scenario.
pub const WORKFLOW_QUESTION: &str =
    "Vilken leverantör erbjuder lägst pris och uppfyller samtliga tre krav?";

const CANDIDATE_REGISTRY: [(&str, &str); 3] =
    [("alpha", "Alpha"), ("beta", "Beta"), ("gamma", "Gamma")];

const WORKFLOW_DOCUMENT: &str = r#"{
  "version": "1.0",
  "name": "supplier_selection_reference",
  "entrypoint": "plan",
  "limits": {"max_steps": 2000, "max_depth": 4, "timeout_seconds": 60},
  "nodes": {
    "plan": {
      "type": "task",
      "handler": "reference.plan_needs",
      "input": {"candidates": "$.input.candidates"},
      "next": "split"
    },
    "split": {
      "type": "fork",
      "branches": {"compliance": "fetch_compliance", "prices": "fetch_prices"},
      "join": "gather"
    },
    "fetch_compliance": {
      "type": "map",
      "items": "$.plan.result.needs_compliance",
      "child": "fetch_fact",
      "concurrency": 4
    },
    "fetch_prices": {
      "type": "map",
      "items": "$.plan.result.needs_prices",
      "child": "fetch_fact",
      "concurrency": 4
    },
    "fetch_fact": {
      "type": "decision",
      "evaluator": "provider",
      "provider": "mock.supplier_facts",
      "prompt_key": "reference.supplier_fact",
      "input": {
        "need_id": "$.item.id",
        "supplier": "$.item.supplier",
        "fact": "$.item.fact"
      },
      "choices": ["found", "missing"],
      "routes": {"found": "accept_fact", "missing": "record_missing"}
    },
    "accept_fact": {
      "type": "task",
      "handler": "reference.accept_evidence",
      "input": {"need": "$.item", "observation": "$.fetch_fact"}
    },
    "record_missing": {
      "type": "task",
      "handler": "reference.record_missing",
      "input": {"need": "$.item", "observation": "$.fetch_fact"}
    },
    "gather": {
      "type": "join",
      "mode": "all",
      "outputs": {
        "compliance": "$.branches.compliance.result",
        "prices": "$.branches.prices.result"
      },
      "next": "evidence"
    },
    "evidence": {
      "type": "task",
      "handler": "reference.merge_evidence",
      "input": {
        "previous": "$.evidence.result",
        "compliance": "$.gather.compliance",
        "prices": "$.gather.prices",
        "extra": "$.extra_evidence"
      },
      "next": "screen"
    },
    "screen": {
      "type": "task",
      "handler": "reference.screen_compliance",
      "input": {
        "candidates": "$.input.candidates",
        "operations": "$.plan.result.operations",
        "evidence": "$.evidence.result"
      },
      "next": "screen_gate"
    },
    "screen_gate": {
      "type": "decision",
      "evaluator": "expression",
      "condition": {"eq": [{"var": "$.screen.result.status"}, {"const": "need_more"}]},
      "routes": {"true": "extra_evidence", "false": "screen_done_gate"}
    },
    "screen_done_gate": {
      "type": "decision",
      "evaluator": "expression",
      "condition": {"eq": [{"var": "$.screen.result.status"}, {"const": "insufficient"}]},
      "routes": {"true": "abstain_screen", "false": "price"}
    },
    "extra_evidence": {
      "type": "map",
      "items": "$.screen.result.needs",
      "child": "fetch_fact",
      "concurrency": 4,
      "next": "evidence"
    },
    "price": {
      "type": "task",
      "handler": "reference.select_cheapest",
      "input": {
        "candidates": "$.input.candidates",
        "operations": "$.plan.result.operations",
        "evidence": "$.evidence.result",
        "compliant": "$.screen.result.compliant"
      },
      "next": "price_gate"
    },
    "price_gate": {
      "type": "decision",
      "evaluator": "expression",
      "condition": {"eq": [{"var": "$.price.result.status"}, {"const": "insufficient"}]},
      "routes": {"true": "abstain_price", "false": "finalize"}
    },
    "finalize": {
      "type": "task",
      "handler": "reference.build_report",
      "input": {
        "candidates": "$.input.candidates",
        "screen": "$.screen.result",
        "price": "$.price.result",
        "evidence": "$.evidence.result"
      },
      "next": "done"
    },
    "abstain_screen": {
      "type": "task",
      "handler": "reference.build_abstain",
      "input": {"reason": "$.screen.result.reason", "evidence": "$.evidence.result"},
      "next": "insufficient_end"
    },
    "abstain_price": {
      "type": "task",
      "handler": "reference.build_abstain",
      "input": {"reason": "$.price.result.reason", "evidence": "$.evidence.result"},
      "next": "insufficient_end"
    },
    "done": {"type": "end", "status": "completed"},
    "insufficient_end": {"type": "end", "status": "incomplete"}
  }
}"#;

pub fn question() -> &'static str {
    WORKFLOW_QUESTION
}

pub fn candidates() -> Vec<Candidate> {
    CANDIDATE_REGISTRY
        .iter()
        .map(|(id, name)| Candidate {
            id: (*id).to_string(),
            name: (*name).to_string(),
        })
        .collect()
}

/// Parse the embedded SWD document. The constant is covered by a test.
pub fn workflow_document() -> Json {
    parse_json_str(WORKFLOW_DOCUMENT).expect("embedded SWD document is valid JSON")
}

pub fn workflow_document_sha256() -> DemoResult<String> {
    Ok(sha256_hex(&workflow_document())?)
}

/// Run input: the question, candidate identities, and two empty application
/// registers seeded so every task binding resolves on the first pass.
///
/// Nothing here reveals an answer: `evidence` starts as an empty evidence batch
/// and `extra_evidence` as an empty list. They are overwritten by the workflow.
pub fn workflow_input(question: &str, candidates: &[Candidate]) -> Json {
    obj([
        ("question", Json::from(question)),
        (
            "candidates",
            Json::Array(candidates.iter().map(Candidate::to_json).collect()),
        ),
        (
            "evidence",
            obj([("result", obj([("evidence", Json::Array(Vec::new()))]))]),
        ),
        ("extra_evidence", Json::Array(Vec::new())),
    ])
}
