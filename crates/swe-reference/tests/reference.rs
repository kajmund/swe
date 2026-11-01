//! Integration tests for the supplier-selection reference experiment.
//!
//! They exercise the crate only through its public API: the embedded SWD
//! definition, the host driver, the reused Sweycoin ledger and the journal.

use std::path::PathBuf;

use swe_reference::{
    candidates, cheapest_compliant, gate_inputs, question, read_journal, resume, run,
    workflow_document, workflow_document_sha256, workflow_input, write_journal, ActionKind,
    Evidence, EvidenceSet, EvidenceStatus, InvocationLog, Json, OperationResult, ResumeConfig,
    RunConfig, RunReport, RunStatus, Scenario, Tariffs, NO_COMPLIANT_SUPPLIER,
};

fn document() -> Json {
    workflow_document()
}

fn input() -> Json {
    workflow_input(question(), &candidates())
}

fn run_scenario(scenario: Scenario, run_id: &str) -> RunReport {
    run(
        &document(),
        &input(),
        scenario,
        &RunConfig::new(run_id, 100_000),
    )
    .expect("run starts")
    .into_finished()
    .expect("run finishes")
}

fn journal_path(name: &str) -> PathBuf {
    std::env::temp_dir().join(format!(
        "swe-reference-test-{name}-{}.json",
        std::process::id()
    ))
}

fn conclusion_u64(report: &RunReport, key: &str) -> u64 {
    report
        .conclusion
        .as_ref()
        .and_then(|conclusion| conclusion.get(key))
        .and_then(Json::as_number)
        .and_then(|number| number.as_u64())
        .unwrap_or(0)
}

fn verified(need_id: &str, supplier: &str, fact: &str, value: Json) -> Evidence {
    Evidence {
        need_id: need_id.to_string(),
        supplier: supplier.to_string(),
        fact: fact.to_string(),
        status: EvidenceStatus::Verified,
        value,
        source_ref: Some(format!("mock://supplier-register/v1/{supplier}/{fact}")),
        confidence_bp: 9_500,
        reason: "test evidence".to_string(),
    }
}

#[test]
fn conclusion_is_beta() {
    let report = run_scenario(Scenario::Baseline, "test-beta");
    assert_eq!(report.status, RunStatus::Completed);
    assert_eq!(report.conclusion_str("conclusion"), Some("Beta"));
    assert_eq!(report.conclusion_str("conclusion_id"), Some("beta"));
    assert_eq!(conclusion_u64(&report, "price_sek"), 950);
    assert_eq!(report.evidence.len(), 13);
    assert!(report.evidence.iter().all(Evidence::is_verified));
    assert!(report
        .evidence
        .iter()
        .all(|evidence| evidence.source_ref.is_some()));
}

#[test]
fn needs_are_derived_from_operation_contracts_not_data() {
    let checkpoint = RunConfig::new("test-plan", 100_000).with_pause_at_spend(1_200);
    let paused = run(&document(), &input(), Scenario::Baseline, &checkpoint)
        .expect("run starts")
        .paused()
        .expect("the checkpoint pauses right after the plan")
        .clone();
    assert_eq!(paused.journal.completed.len(), 1);
    assert_eq!(paused.journal.completed[0].node_id, "plan");
    assert!(paused
        .segment
        .charges
        .iter()
        .all(|charge| charge.action == "reference.plan_needs"));
    assert_eq!(paused.segment.spent_milli, 1_000);

    let plan = &paused.journal.completed[0].output;
    let needs_compliance = plan
        .get("needs_compliance")
        .and_then(Json::as_array)
        .expect("compliance needs");
    let needs_prices = plan
        .get("needs_prices")
        .and_then(Json::as_array)
        .expect("price needs");
    assert_eq!(needs_compliance.len(), 9);
    assert_eq!(needs_prices.len(), 3);
    for need in needs_compliance.iter().chain(needs_prices.iter()) {
        assert!(need.get("id").and_then(Json::as_str).is_some());
        assert_ne!(
            need.get("fact").and_then(Json::as_str),
            Some("c_exception"),
            "conditional facts are discovered later, not planned from data"
        );
    }
    let operations = plan
        .get("operations")
        .and_then(Json::as_array)
        .expect("operations");
    assert_eq!(operations.len(), 2);

    // The exact same need list is planned for the scenario whose register is
    // damaged: needs come from contracts, not from what the provider happens to
    // return.
    let damaged_pause = run(
        &document(),
        &input(),
        Scenario::MissingEvidence,
        &checkpoint,
    )
    .expect("run starts")
    .paused()
    .expect("the checkpoint pauses right after the plan")
    .clone();
    assert_eq!(
        damaged_pause.journal.completed[0].output,
        paused.journal.completed[0].output
    );
}

#[test]
fn missing_and_ambiguous_evidence_abstains() {
    let report = run_scenario(Scenario::MissingEvidence, "test-abstain");
    assert_eq!(report.status, RunStatus::Incomplete);
    assert_eq!(
        report.conclusion_str("conclusion"),
        Some("INSUFFICIENT_EVIDENCE")
    );
    let reason = report.conclusion_str("reason").expect("abstain reason");
    assert!(reason.contains("ALL_REQUIRED"));
    assert!(reason.contains("beta.req_c"));

    let rejected: Vec<&Evidence> = report
        .evidence
        .iter()
        .filter(|evidence| evidence.status == EvidenceStatus::Rejected)
        .collect();
    assert_eq!(rejected.len(), 1);
    assert_eq!(rejected[0].need_id, "beta.req_c");

    let ambiguous: Vec<&Evidence> = report
        .evidence
        .iter()
        .filter(|evidence| evidence.status == EvidenceStatus::Ambiguous)
        .collect();
    assert_eq!(ambiguous.len(), 1);
    assert_eq!(ambiguous[0].need_id, "gamma.price");

    // The price operation must never run when screening cannot be gated.
    assert!(report
        .invocations
        .iter()
        .all(|invocation| invocation.node_id != "price"));
}

#[test]
fn no_compliant_supplier_is_a_terminal_report_not_a_crash() {
    let report = run_scenario(Scenario::NoCompliantSupplier, "test-no-compliant");
    assert_eq!(report.status, RunStatus::Completed);
    assert_eq!(
        report.conclusion_str("conclusion"),
        Some(NO_COMPLIANT_SUPPLIER)
    );
    let conclusion = report.conclusion.as_ref().expect("terminal report");
    assert_eq!(conclusion.get("conclusion_id"), Some(&Json::Null));
    assert_eq!(conclusion.get("price_sek"), Some(&Json::Null));

    // Every fact was verified; screening completed and listed nobody.
    assert!(report.evidence.iter().all(Evidence::is_verified));
    let screens: Vec<&InvocationLog> = report
        .invocations
        .iter()
        .filter(|invocation| invocation.node_id == "screen")
        .collect();
    let compliant = screens
        .last()
        .and_then(|screen| screen.output.get("compliant"))
        .and_then(Json::as_array)
        .expect("compliant list");
    assert!(compliant.is_empty());

    // The comparison ran on an empty candidate set and the report still built.
    let price = report
        .invocations
        .iter()
        .find(|invocation| invocation.node_id == "price")
        .expect("price operation ran");
    assert_eq!(price.status(), Some("ok"));
    assert_eq!(price.output.get("answer"), Some(&Json::Null));
}

#[test]
fn several_discovered_needs_are_resolved_over_several_rounds() {
    // Two suppliers fail requirement C, so one screening round discovers two
    // separate missing facts. The engine's protocol surfaces one need per
    // round; the investigation resolves it, re-runs, and the second surfaces.
    // That is what keeps the loop bounded without widening NeedMore to a list.
    let report = run(
        &document(),
        &input(),
        Scenario::TwoExceptions,
        &RunConfig::new("test-two-needs", 100_000),
    )
    .expect("run starts")
    .into_finished()
    .expect("run finishes");

    let screens: Vec<&InvocationLog> = report
        .invocations
        .iter()
        .filter(|invocation| invocation.node_id == "screen")
        .collect();
    assert!(
        screens.len() >= 3,
        "one round per discovered need plus the final round, got {}",
        screens.len()
    );
    assert_eq!(screens[0].status(), Some("need_more"));
    assert_eq!(screens[1].status(), Some("need_more"));
    assert_eq!(screens[screens.len() - 1].status(), Some("ok"));

    let first = screens[0].output.get("need").and_then(Json::as_str);
    let second = screens[1].output.get("need").and_then(Json::as_str);
    assert!(first.is_some() && second.is_some());
    assert_ne!(first, second, "a repeated need would end the investigation");

    // Both discovered facts were resolved, and each extra round cost money.
    let resolved: Vec<&str> = report
        .evidence
        .iter()
        .filter(|evidence| evidence.is_verified())
        .map(|evidence| evidence.need_id.as_str())
        .collect();
    let first = first.expect("first need").to_string();
    let second = second.expect("second need").to_string();
    assert!(
        resolved.contains(&first.as_str()),
        "first need was not resolved"
    );
    assert!(
        resolved.contains(&second.as_str()),
        "second need was not resolved"
    );
    assert_eq!(
        report.charge_count("reference.screen_compliance"),
        screens.len()
    );
    assert_eq!(
        report.spent_milli + report.available_milli,
        report.allocated_milli
    );
}

#[test]
fn need_more_is_resolved_and_operation_reruns() {
    let report = run_scenario(Scenario::Baseline, "test-need-more");
    let screens: Vec<&InvocationLog> = report
        .invocations
        .iter()
        .filter(|invocation| invocation.node_id == "screen")
        .collect();
    assert_eq!(screens.len(), 2);
    assert_eq!(screens[0].status(), Some("need_more"));
    let needs = screens[0]
        .output
        .get("needs")
        .and_then(Json::as_array)
        .expect("NeedMore needs");
    assert!(needs
        .iter()
        .any(|need| { need.get("id").and_then(Json::as_str) == Some("alpha.c_exception") }));
    assert_eq!(screens[1].status(), Some("ok"));
    let compliant = screens[1]
        .output
        .get("compliant")
        .and_then(Json::as_array)
        .expect("compliant list");
    assert_eq!(compliant.len(), 1);
    assert_eq!(compliant[0].as_str(), Some("beta"));

    let exception = report
        .evidence
        .iter()
        .find(|evidence| evidence.need_id == "alpha.c_exception")
        .expect("the discovered need was resolved");
    assert!(exception.is_verified());
    assert!(exception
        .source_ref
        .as_deref()
        .expect("provenance")
        .contains("alpha/c_exception"));
    assert_eq!(report.charge_count("reference.screen_compliance"), 2);
}

#[test]
fn the_gate_is_the_engines_and_lists_every_missing_input() {
    // The demo has no gate of its own: this asks the engine and only projects
    // the answer. The engine decides; the demo reports.
    let required = vec![
        swe_reference::need_ref("alpha", "price"),
        swe_reference::need_ref("beta", "price"),
    ];
    let mut evidence = EvidenceSet::new();

    let failure = gate_inputs("OP", &required, &evidence)
        .expect("call")
        .expect("two inputs are missing");
    assert_eq!(failure.missing.len(), 2);
    assert_eq!(failure.operation, "OP");

    evidence.insert(verified(
        "alpha.price",
        "alpha",
        "price",
        Json::from_u64(800),
    ));
    let failure = gate_inputs("OP", &required, &evidence)
        .expect("call")
        .expect("one input is missing");
    assert_eq!(failure.missing.len(), 1);
    assert_eq!(failure.missing[0].need.id(), "beta.price");

    let mut ambiguous = verified("beta.price", "beta", "price", Json::from_u64(950));
    ambiguous.status = EvidenceStatus::Ambiguous;
    evidence.insert(ambiguous);
    let failure = gate_inputs("OP", &required, &evidence)
        .expect("call")
        .expect("ambiguous is not usable");
    assert_eq!(failure.missing.len(), 1);
    // The status comes from the engine, not from a demo-side rule.
    assert_eq!(failure.missing[0].status, swe_native::NeedStatus::Ambiguous);

    // A rejected input is unresolved as far as the engine is concerned.
    let mut rejected = verified("beta.price", "beta", "price", Json::from_u64(950));
    rejected.status = EvidenceStatus::Rejected;
    evidence.insert(rejected);
    let failure = gate_inputs("OP", &required, &evidence)
        .expect("call")
        .expect("rejected is not usable");
    assert_eq!(
        failure.missing[0].status,
        swe_native::NeedStatus::Unresolved
    );

    // And a satisfied contract yields no failure at all.
    evidence.insert(verified("beta.price", "beta", "price", Json::from_u64(950)));
    assert!(gate_inputs("OP", &required, &evidence)
        .expect("call")
        .is_none());
}

#[test]
fn the_refusal_payload_reports_every_missing_input_and_its_status() {
    // The handler protocol's `missing` array is a rendering of the engine's
    // failure; dropping any of it would hide what blocked the operation.
    let required = vec![
        swe_reference::need_ref("alpha", "price"),
        swe_reference::need_ref("beta", "price"),
    ];
    let mut evidence = EvidenceSet::new();
    let mut ambiguous = verified("alpha.price", "alpha", "price", Json::from_u64(800));
    ambiguous.status = EvidenceStatus::Ambiguous;
    evidence.insert(ambiguous);

    let failure = gate_inputs("OP", &required, &evidence)
        .expect("call")
        .expect("two inputs are missing");
    let payload = swe_reference::gate_failure_json(&failure);
    let missing = payload
        .get("missing")
        .and_then(Json::as_array)
        .expect("missing array");
    assert_eq!(missing.len(), 2, "both missing inputs must be reported");
    assert_eq!(
        missing[0].get("status").and_then(Json::as_str),
        Some("AMBIGUOUS")
    );
    assert_eq!(
        missing[1].get("status").and_then(Json::as_str),
        Some("UNRESOLVED")
    );
    assert_eq!(
        missing[0]
            .get("need")
            .and_then(|need| need.get("id"))
            .and_then(Json::as_str),
        Some("alpha.price")
    );
    let detail = payload
        .get("detail")
        .and_then(Json::as_str)
        .expect("detail");
    assert!(detail.contains("ALL_REQUIRED"), "detail was {detail:?}");
    assert!(detail.contains("alpha.price"));
}

#[test]
fn the_demo_cannot_accept_an_input_the_engine_refuses() {
    // Pins the direction of the dependency: whatever the demo's evidence says,
    // the verdict is the engine's. A VERIFIED entry is the only thing that
    // passes, because that is the mapping the engine's policy implies.
    let required = vec![swe_reference::need_ref("alpha", "req_a")];
    for (status, usable) in [
        (EvidenceStatus::Verified, true),
        (EvidenceStatus::Ambiguous, false),
        (EvidenceStatus::Rejected, false),
    ] {
        let mut evidence = EvidenceSet::new();
        let mut entry = verified("alpha.req_a", "alpha", "req_a", Json::Bool(true));
        entry.status = status;
        evidence.insert(entry);
        let refused = gate_inputs("OP", &required, &evidence).expect("call");
        assert_eq!(
            refused.is_none(),
            usable,
            "engine and demo disagree for {status:?}"
        );
    }
    // An empty evidence set is never usable.
    assert!(gate_inputs("OP", &required, &EvidenceSet::new())
        .expect("call")
        .is_some());
}

#[test]
fn a_discovered_fact_the_engine_refuses_stops_the_investigation() {
    // The screening rule discovers a conditional fact whose source is
    // ambiguous. Whether that fact is usable is the engine's decision: the demo
    // asks the gate and reports its verdict, status included.
    let report = run(
        &document(),
        &input(),
        Scenario::AmbiguousException,
        &RunConfig::new("test-ambiguous-exception", 100_000),
    )
    .expect("run starts")
    .into_finished()
    .expect("run finishes");

    assert_eq!(report.status, RunStatus::Incomplete);
    assert_eq!(
        report
            .conclusion
            .as_ref()
            .and_then(|value| value.get("conclusion")),
        Some(&Json::from("INSUFFICIENT_EVIDENCE"))
    );
    let screens: Vec<&InvocationLog> = report
        .invocations
        .iter()
        .filter(|invocation| invocation.node_id == "screen")
        .collect();
    let last = screens.last().expect("the screen operation ran");
    assert_eq!(last.status(), Some("insufficient"));

    // The status is the engine's assessment of the evidence, not a demo guess:
    // a locally built failure would report UNRESOLVED here.
    let missing = last
        .output
        .get("missing")
        .and_then(Json::as_array)
        .expect("the refusal lists the missing input");
    assert_eq!(missing.len(), 1);
    assert_eq!(
        missing[0].get("status").and_then(Json::as_str),
        Some("AMBIGUOUS")
    );
    let need = missing[0].get("need").expect("the need is named");
    assert_eq!(
        need.get("id").and_then(Json::as_str),
        Some("alpha.c_exception")
    );
    // And nothing decided the price.
    assert_eq!(report.charge_count("reference.select_cheapest"), 0);
}

#[test]
fn deterministic_comparison_prefers_cheapest_compliant() {
    let list = candidates();
    let mut evidence = EvidenceSet::new();
    evidence.insert(verified(
        "alpha.price",
        "alpha",
        "price",
        Json::from_u64(800),
    ));
    evidence.insert(verified("beta.price", "beta", "price", Json::from_u64(950)));
    evidence.insert(verified(
        "gamma.price",
        "gamma",
        "price",
        Json::from_u64(900),
    ));

    let ranked = cheapest_compliant(&list, &["beta".to_string(), "gamma".to_string()], &evidence)
        .expect("comparison runs");
    assert_eq!(
        ranked
            .iter()
            .map(|supplier| supplier.id.as_str())
            .collect::<Vec<_>>(),
        ["gamma", "beta"]
    );

    let ranked = cheapest_compliant(
        &list,
        &["alpha".to_string(), "beta".to_string(), "gamma".to_string()],
        &evidence,
    )
    .expect("comparison runs");
    assert_eq!(ranked[0].id, "alpha");
    assert_eq!(ranked[0].price_sek, 800);

    // Equal prices are tie-broken by id, so map completion order never decides.
    let mut tied = evidence.clone();
    tied.insert(verified(
        "gamma.price",
        "gamma",
        "price",
        Json::from_u64(800),
    ));
    let ranked = cheapest_compliant(&list, &["alpha".to_string(), "gamma".to_string()], &tied)
        .expect("comparison runs");
    assert_eq!(ranked[0].id, "alpha");
    assert_eq!(ranked[1].id, "gamma");
}

#[test]
fn pause_and_resume_charge_and_execute_exactly_once() {
    let full = run_scenario(Scenario::Baseline, "test-resume");
    assert_eq!(full.status, RunStatus::Completed);

    let paused_outcome = run(
        &document(),
        &input(),
        Scenario::Baseline,
        &RunConfig::new("test-resume", 100_000).with_pause_at_spend(20_000),
    )
    .expect("run starts");
    let paused = paused_outcome.paused().expect("tight budget pauses");
    assert!(paused.segment.executed_invocations > 0);
    assert!(paused.segment.executed_invocations < full.executed_invocations);
    assert!(paused.segment.spent_milli <= 20_000);
    assert_eq!(
        paused.journal.completed.len(),
        paused.segment.executed_invocations
    );

    let path = journal_path("pause-resume");
    write_journal(&path, &paused.journal).expect("journal is written");
    let loaded = read_journal(&path).expect("journal is read back");
    std::fs::remove_file(&path).expect("journal file removed");

    assert_eq!(
        loaded.definition_sha256,
        workflow_document_sha256().expect("definition hash")
    );
    assert_eq!(loaded.input, paused.journal.input);
    assert_eq!(loaded.completed, paused.journal.completed);
    assert_eq!(loaded.charges, paused.journal.charges);
    assert_eq!(loaded.ledger_events, paused.journal.ledger_events);

    let resumed = resume(&loaded, &ResumeConfig::new())
        .expect("resume starts")
        .into_finished()
        .expect("resume finishes");
    assert_eq!(resumed.status, RunStatus::Completed);
    assert_eq!(resumed.conclusion_str("conclusion"), Some("Beta"));

    // Exactly-once: same total spend, same unique executions, same charges and
    // the same ledger event log as the uninterrupted run.
    assert_eq!(resumed.spent_milli, full.spent_milli);
    assert_eq!(resumed.executed_invocations, full.executed_invocations);
    assert_eq!(resumed.handler_calls, full.handler_calls);
    assert_eq!(resumed.provider_calls, full.provider_calls);
    assert_eq!(resumed.charges, full.charges);
    assert_eq!(resumed.ledger.events(), full.ledger.events());
    assert_eq!(resumed.replayed_invocations, loaded.completed.len());
    assert_eq!(
        resumed.executed_invocations - resumed.replayed_invocations,
        full.executed_invocations - loaded.completed.len()
    );
}

#[test]
fn the_journal_round_trip_is_lossless() {
    // Journal v1 must serialise and deserialise without losing anything. The
    // easiest way to prove that is to compare the two forms of one run.
    let paused_outcome = run(
        &document(),
        &input(),
        Scenario::Baseline,
        &RunConfig::new("test-roundtrip", 100_000).with_pause_at_spend(20_000),
    )
    .expect("run starts");
    let paused = paused_outcome.paused().expect("pauses");
    let written = paused.journal.clone();
    let path = journal_path("roundtrip");
    write_journal(&path, &written).expect("journal written");
    let loaded = read_journal(&path).expect("journal read");
    std::fs::remove_file(&path).expect("removed");

    assert_eq!(loaded, written, "round trip must preserve every field");
    // And the fields that used to be silently defaulted are present.
    assert_eq!(loaded.contract_version, written.contract_version);
    assert_eq!(loaded.journal_events.len(), written.journal_events.len());
    assert!(!loaded.journal_events.is_empty(), "events must not vanish");
}

#[test]
fn a_journal_recording_another_dependency_version_is_refused() {
    // H1: the recorded dependency versions must be compared against what this
    // build actually runs, not against a constant handed to itself.
    let paused_outcome = run(
        &document(),
        &input(),
        Scenario::Baseline,
        &RunConfig::new("test-dependency", 100_000).with_pause_at_spend(20_000),
    )
    .expect("run starts");
    let paused = paused_outcome.paused().expect("pauses");
    let path = journal_path("dependency");
    write_journal(&path, &paused.journal).expect("journal written");
    let text = std::fs::read_to_string(&path).expect("read");
    std::fs::remove_file(&path).expect("removed");

    // The journal claims the mock provider was a different version.
    let bumped = text.replace("\"mock.supplier_facts\":1", "\"mock.supplier_facts\":999");
    assert_ne!(
        bumped, text,
        "the dependency map must be in the file to bump"
    );
    let path = journal_path("dependency");
    std::fs::write(&path, bumped).expect("write");
    let loaded = read_journal(&path).expect("journal read");
    std::fs::remove_file(&path).expect("removed");
    assert!(
        resume(&loaded, &ResumeConfig::new()).is_err(),
        "a journal recorded against another dependency version must not resume"
    );
}

#[test]
fn a_journal_missing_a_recorded_dependency_is_refused() {
    let paused_outcome = run(
        &document(),
        &input(),
        Scenario::Baseline,
        &RunConfig::new("test-dependency-missing", 100_000).with_pause_at_spend(20_000),
    )
    .expect("run starts");
    let paused = paused_outcome.paused().expect("pauses");
    let path = journal_path("dependency-missing");
    write_journal(&path, &paused.journal).expect("journal written");
    let text = std::fs::read_to_string(&path).expect("read");
    std::fs::remove_file(&path).expect("removed");
    let emptied = text.replace(
        "\"dependency_versions\":{\"mock.supplier_facts\":1}",
        "\"dependency_versions\":{}",
    );
    assert_ne!(emptied, text);
    let path = journal_path("dependency-missing");
    std::fs::write(&path, emptied).expect("write");
    let loaded = read_journal(&path).expect("journal read");
    std::fs::remove_file(&path).expect("removed");
    assert!(
        resume(&loaded, &ResumeConfig::new()).is_err(),
        "a journal that recorded no dependency must not resume silently"
    );
}

#[test]
fn a_journal_without_a_contract_version_is_refused() {
    let paused_outcome = run(
        &document(),
        &input(),
        Scenario::Baseline,
        &RunConfig::new("test-missing-version", 100_000).with_pause_at_spend(20_000),
    )
    .expect("run starts");
    let paused = paused_outcome.paused().expect("pauses");
    let path = journal_path("missing-version");
    write_journal(&path, &paused.journal).expect("journal written");
    let text = std::fs::read_to_string(&path).expect("read");
    std::fs::remove_file(&path).expect("removed");

    for field in [
        "contract_version",
        "journal_events",
        "tariff_sha256",
        "dependency_versions",
        "handler_cost_milli",
        "provider_cost_milli",
        "now_ms",
        "budget_milli",
        "handler_calls",
        "provider_calls",
    ] {
        let stripped = drop_top_level_field(&text, field);
        let path = journal_path(&format!("missing-{field}"));
        std::fs::write(&path, stripped).expect("write");
        let outcome = read_journal(&path);
        std::fs::remove_file(&path).expect("removed");
        assert!(
            outcome.is_err(),
            "a journal without {field} must be refused, never defaulted"
        );
    }
}

#[test]
fn a_journal_with_an_unreadable_event_log_is_refused() {
    let paused_outcome = run(
        &document(),
        &input(),
        Scenario::Baseline,
        &RunConfig::new("test-bad-events", 100_000).with_pause_at_spend(20_000),
    )
    .expect("run starts");
    let paused = paused_outcome.paused().expect("pauses");
    let path = journal_path("bad-events");
    write_journal(&path, &paused.journal).expect("journal written");
    let text = std::fs::read_to_string(&path).expect("read");
    std::fs::remove_file(&path).expect("removed");

    // A non-array event log must be a typed error, not an empty journal.
    let broken = text.replace("\"journal_events\":[", "\"journal_events\":{\"x\":[");
    let path = journal_path("bad-events");
    std::fs::write(&path, broken).expect("write");
    let outcome = read_journal(&path);
    std::fs::remove_file(&path).expect("removed");
    assert!(outcome.is_err(), "an unreadable event log must be refused");
}

#[test]
fn confirmed_outcomes_survive_the_round_trip() {
    // Ok, Error and Cancelled are three different facts; a boolean loses two.
    let paused_outcome = run(
        &document(),
        &input(),
        Scenario::Baseline,
        &RunConfig::new("test-outcomes", 100_000).with_pause_at_spend(20_000),
    )
    .expect("run starts");
    let paused = paused_outcome.paused().expect("pauses");
    let path = journal_path("outcomes");
    write_journal(&path, &paused.journal).expect("journal written");
    let loaded = read_journal(&path).expect("journal read");
    std::fs::remove_file(&path).expect("removed");

    let confirmed: Vec<_> = loaded
        .journal_events
        .iter()
        .filter(|event| matches!(event, swe_native::JournalEvent::ResultConfirmed { .. }))
        .collect();
    assert!(!confirmed.is_empty());
    for event in confirmed {
        if let swe_native::JournalEvent::ResultConfirmed { outcome, .. } = event {
            assert!(
                matches!(outcome, swe_native::CompletionOutcome::Ok(_)),
                "a successful demo action must round-trip as Ok, not as a flag"
            );
        }
    }
}

#[test]
fn a_journal_replayed_under_other_tariffs_is_refused() {
    // Tariffs are part of the run contract. The host now states its current
    // tariffs in the resume config, so this varies the HOST, not the file.
    let paused_outcome = run(
        &document(),
        &input(),
        Scenario::Baseline,
        &RunConfig::new("test-tariffs", 100_000).with_pause_at_spend(20_000),
    )
    .expect("run starts");
    let paused = paused_outcome.paused().expect("pauses");

    let same = ResumeConfig::new().with_tariffs(Tariffs::default());
    assert!(
        resume(&paused.journal, &same).is_ok(),
        "resuming at the same tariffs must work"
    );

    let repriced = ResumeConfig::new().with_tariffs(Tariffs {
        handler_milli: 2_000,
        provider_milli: 1_000,
    });
    let outcome = resume(&paused.journal, &repriced);
    assert!(
        outcome.is_err(),
        "a host billing at other tariffs must not resume this journal"
    );
}

#[test]
fn a_run_priced_with_custom_tariffs_round_trips_and_resumes() {
    // Pins the write path: the journal must record the tariffs it was actually
    // billed at, not the defaults.
    let custom = Tariffs {
        handler_milli: 700,
        provider_milli: 1_300,
    };
    let paused_outcome = run(
        &document(),
        &input(),
        Scenario::Baseline,
        &RunConfig::new("test-custom-tariffs", 100_000)
            .with_pause_at_spend(20_000)
            .with_tariffs(custom),
    )
    .expect("run starts");
    let paused = paused_outcome.paused().expect("pauses");
    assert_eq!(paused.journal.recorded_tariffs(), custom);
    assert_eq!(
        paused.journal.tariff_sha256,
        swe_reference::tariff_sha256(&custom).expect("hash")
    );

    // Resuming without stating tariffs uses the recorded ones and succeeds.
    let resumed = resume(&paused.journal, &ResumeConfig::new())
        .expect("resumes")
        .into_finished()
        .expect("finishes");
    assert_eq!(resumed.status, RunStatus::Completed);
    // Resuming at the defaults must be refused: this run was not priced that way.
    assert!(resume(
        &paused.journal,
        &ResumeConfig::new().with_tariffs(Tariffs::default())
    )
    .is_err());
}

#[test]
fn a_tampered_tariff_hash_is_refused() {
    let paused_outcome = run(
        &document(),
        &input(),
        Scenario::Baseline,
        &RunConfig::new("test-tariff-hash", 100_000).with_pause_at_spend(20_000),
    )
    .expect("run starts");
    let paused = paused_outcome.paused().expect("pauses");
    let path = journal_path("tariff-hash");
    write_journal(&path, &paused.journal).expect("journal written");
    let text = std::fs::read_to_string(&path).expect("read");
    std::fs::remove_file(&path).expect("removed");

    // Change the recorded prices but keep the recorded hash: the journal's own
    // numbers no longer agree with the hash it claims to have been priced by.
    let repriced = text.replace("\"handler_cost_milli\":1000", "\"handler_cost_milli\":2000");
    assert_ne!(repriced, text, "the tariff must be present to tamper with");
    let path = journal_path("tariff-hash");
    std::fs::write(&path, repriced).expect("write");
    let outcome = read_journal(&path);
    std::fs::remove_file(&path).expect("removed");
    assert!(
        outcome.is_err(),
        "an inconsistent tariff hash must be refused"
    );
}

#[test]
fn a_journal_from_another_contract_version_is_refused() {
    let paused_outcome = run(
        &document(),
        &input(),
        Scenario::Baseline,
        &RunConfig::new("test-version", 100_000).with_pause_at_spend(20_000),
    )
    .expect("run starts");
    let paused = paused_outcome.paused().expect("tight budget pauses");

    let mut wrong_contract = paused.journal.clone();
    wrong_contract.contract_version = 999;
    let path = journal_path("contract-version");
    write_journal(&path, &wrong_contract).expect("journal written");
    let loaded = read_journal(&path).expect("journal read");
    std::fs::remove_file(&path).expect("removed");
    let outcome = resume(&loaded, &ResumeConfig::new());
    assert!(
        outcome.is_err(),
        "a changed contract version must not resume"
    );
    assert!(
        format!("{}", outcome.unwrap_err()).contains("version"),
        "the refusal must name the version"
    );
}

#[test]
fn a_journal_whose_definition_changed_is_refused() {
    let paused_outcome = run(
        &document(),
        &input(),
        Scenario::Baseline,
        &RunConfig::new("test-def", 100_000).with_pause_at_spend(20_000),
    )
    .expect("run starts");
    let paused = paused_outcome.paused().expect("tight budget pauses");

    let mut tampered = paused.journal.clone();
    // A well-formed hash that is not this document's hash.
    tampered.definition_sha256 = "b".repeat(64);
    let path = journal_path("def");
    write_journal(&path, &tampered).expect("journal written");
    let loaded = read_journal(&path).expect("journal read");
    std::fs::remove_file(&path).expect("removed");
    assert!(resume(&loaded, &ResumeConfig::new()).is_err());
}

#[test]
fn every_critical_transition_survives_a_pause_and_resume() {
    // Pause after each metered action in turn: the resumed run must reach the
    // same conclusion and the same spend as an uninterrupted one.
    let full = run_scenario(Scenario::Baseline, "test-every-transition");
    assert_eq!(full.status, RunStatus::Completed);
    let mut checked = 0;
    for checkpoint in (1_000..=20_000).step_by(1_000) {
        let paused_outcome = run(
            &document(),
            &input(),
            Scenario::Baseline,
            &RunConfig::new("test-every-transition", 100_000).with_pause_at_spend(checkpoint),
        )
        .expect("run starts");
        let Some(paused) = paused_outcome.paused() else {
            continue;
        };
        // Only meaningful when the checkpoint actually stopped mid-investigation.
        assert!(!paused.journal.completed.is_empty());
        let resumed = resume(&paused.journal, &ResumeConfig::new())
            .expect("resume starts")
            .into_finished()
            .expect("resume finishes");
        assert_eq!(resumed.status, RunStatus::Completed, "at {checkpoint}");
        assert_eq!(
            resumed.conclusion_str("conclusion"),
            full.conclusion_str("conclusion"),
            "at {checkpoint}"
        );
        assert_eq!(resumed.spent_milli, full.spent_milli, "at {checkpoint}");
        assert_eq!(
            resumed.executed_invocations, full.executed_invocations,
            "at {checkpoint}"
        );
        checked += 1;
    }
    assert!(checked >= 5, "expected several pause points, got {checked}");
}

#[test]
fn a_tampered_journal_is_rejected() {
    let paused_outcome = run(
        &document(),
        &input(),
        Scenario::Baseline,
        &RunConfig::new("test-tamper", 100_000).with_pause_at_spend(20_000),
    )
    .expect("run starts");
    let paused = paused_outcome.paused().expect("tight budget pauses");
    let mut tampered = paused.journal.clone();
    tampered.definition_sha256 = "0".repeat(64);

    let path = journal_path("tamper");
    write_journal(&path, &tampered).expect("journal is written");
    let loaded = read_journal(&path).expect("journal is read back");
    std::fs::remove_file(&path).expect("journal file removed");
    assert!(resume(&loaded, &ResumeConfig::new()).is_err());
}

#[test]
fn every_action_is_journaled_as_attempt_result_and_charge() {
    // The demo uses the engine journal, not a private one: each metered action
    // leaves an AttemptStarted, a ResultConfirmed, a ChargeReserved and a
    // ChargeSettled event, in that order.
    let report = run_scenario(Scenario::Baseline, "test-journal-events");
    let kinds = &report.journal_events;
    assert_eq!(kinds.len() % 4, 0, "four events per action");
    assert_eq!(kinds.iter().filter(|k| **k == "AttemptStarted").count(), 33);
    assert_eq!(
        kinds.iter().filter(|k| **k == "ResultConfirmed").count(),
        33
    );
    assert_eq!(kinds.iter().filter(|k| **k == "ChargeReserved").count(), 33);
    assert_eq!(kinds.iter().filter(|k| **k == "ChargeSettled").count(), 33);
    // The engine journal and the reused ledger agree on the money.
    assert_eq!(report.journal_settled_milli, report.spent_milli);
    // Nothing is left open or awaiting reconciliation in a clean run.
    assert_eq!(report.journal_open_attempts, 0);
    assert_eq!(report.journal_pending_charges, 0);
}

#[test]
fn no_action_settles_twice_across_a_resume() {
    let full = run_scenario(Scenario::Baseline, "test-resume-journal");
    let paused_outcome = run(
        &document(),
        &input(),
        Scenario::Baseline,
        &RunConfig::new("test-resume-journal", 100_000).with_pause_at_spend(20_000),
    )
    .expect("run starts");
    let paused = paused_outcome.paused().expect("pauses");
    let resumed = resume(&paused.journal, &ResumeConfig::new())
        .expect("resumes")
        .into_finished()
        .expect("finishes");
    // Same money, and a settlement per unique action only.
    assert_eq!(resumed.journal_settled_milli, full.journal_settled_milli);
    assert_eq!(resumed.spent_milli, full.spent_milli);
    assert_eq!(
        resumed
            .journal_events
            .iter()
            .filter(|k| **k == "ChargeSettled")
            .count(),
        full.journal_events
            .iter()
            .filter(|k| **k == "ChargeSettled")
            .count()
    );
    assert_eq!(resumed.journal_open_attempts, 0);
}

#[test]
fn repeated_runs_are_deterministic() {
    let first = run_scenario(Scenario::Baseline, "test-deterministic");
    let second = run_scenario(Scenario::Baseline, "test-deterministic");
    assert_eq!(first.conclusion, second.conclusion);
    assert_eq!(first.evidence, second.evidence);
    assert_eq!(first.charges, second.charges);
    assert_eq!(first.ledger.events(), second.ledger.events());
    assert_eq!(first.spent_milli, second.spent_milli);
    assert_eq!(first.steps_used, second.steps_used);
    assert_eq!(first.invocations.len(), second.invocations.len());
}

#[test]
fn metering_is_per_action_and_sums_to_the_ledger() {
    let report = run_scenario(Scenario::Baseline, "test-metering");
    let per_action: u64 = report.action_totals().iter().map(|(_, total)| *total).sum();
    assert_eq!(per_action, report.spent_milli);
    assert_eq!(
        report.spent_milli + report.available_milli,
        report.allocated_milli
    );
    assert_eq!(report.ledger_events, report.ledger.events().len());
    assert_eq!(report.provider_calls, 13);
    assert_eq!(report.handler_calls, 20);
    assert_eq!(report.charge_count("mock.supplier_facts"), 13);
    assert_eq!(
        report
            .charges
            .iter()
            .filter(|charge| charge.kind == ActionKind::Handler)
            .count(),
        report.handler_calls
    );
    assert!(report.action_total("reference.screen_compliance") > 0);
}

#[test]
fn need_more_is_the_engines_singular_protocol() {
    // The engine's protocol is one need per round, not a list: that keeps the
    // loop bounded and lets the investigation choose what to resolve next. The
    // demo consumes that outcome rather than widening it to a list.
    let need = swe_reference::need_ref("alpha", "c_exception");
    let result = OperationResult::need_more(need.clone());
    assert_eq!(result.status(), "need_more");
    let json = result.into_handler_json();
    assert_eq!(json.get("status").and_then(Json::as_str), Some("need_more"));
    assert_eq!(
        json.get("need").and_then(Json::as_str),
        Some(need.id().as_str())
    );
}

/// Drop one top-level JSON field so a test can prove its absence is refused
/// instead of silently defaulted. Text-level surgery is enough: the journal is
/// written by the demo's own canonical serialiser.
fn drop_top_level_field(text: &str, field: &str) -> String {
    let needle = format!("\"{field}\":");
    // A missing needle would make this a no-op and the test would then prove
    // nothing. Fail loudly instead.
    let Some(start) = text.find(&needle) else {
        panic!("field {field:?} is not present in the journal, so it cannot be removed");
    };
    let after = start + needle.len();
    let rest = &text[after..];
    // find the value's end at depth 0
    let mut depth = 0i32;
    let mut in_string = false;
    let mut escaped = false;
    let mut value_end = rest.len();
    for (offset, ch) in rest.char_indices() {
        match ch {
            '\\' if in_string => escaped = !escaped,
            '"' if !escaped => in_string = !in_string,
            '{' | '[' if !in_string => depth += 1,
            '}' | ']' if !in_string => {
                if depth == 0 {
                    value_end = offset;
                    break;
                }
                depth -= 1;
            }
            ',' if !in_string && depth == 0 => {
                value_end = offset + 1;
                break;
            }
            _ => escaped = false,
        }
        if ch != '\\' {
            escaped = false;
        }
    }
    let mut out = String::with_capacity(text.len());
    out.push_str(&text[..start]);
    out.push_str(&text[after + value_end..]);
    out
}
