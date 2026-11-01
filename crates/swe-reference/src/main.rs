//! Walkthrough binary: runs the supplier-selection investigation end to end and
//! prints one section per required demonstration.
//!
//! `cargo run --offline` is fully offline: no network, no model calls, no
//! product configuration. It exits non-zero if any expected conclusion or
//! invariants fail, so a green exit means the demo actually held.

use swe_reference::{
    candidates, question, read_journal, resume, run, workflow_document, workflow_document_sha256,
    workflow_input, write_journal, DemoResult, InvocationLog, Json, ResumeConfig, RunConfig,
    RunReport, RunStatus, Scenario,
};

const RUN_ID: &str = "reference-investigation";
const BUDGET_MILLI: u64 = 50_000;
const CHECKPOINT_MILLI: u64 = 20_000;

fn main() -> DemoResult<()> {
    let document = workflow_document();
    let definition_sha256 = workflow_document_sha256()?;
    let candidate_list = candidates();
    let input = workflow_input(question(), &candidate_list);

    println!("SWE reference experiment — supplier selection, domain-neutral");
    println!("Question: {}", question());
    println!(
        "Candidates: {}",
        candidate_list
            .iter()
            .map(|candidate| candidate.name.as_str())
            .collect::<Vec<_>>()
            .join(", ")
    );
    println!("SWD definition sha256: {definition_sha256}");
    println!("Run input: question + candidate identities only. No prices, no answers.");
    println!(
        "Seeded empty registers (so first-pass bindings resolve): evidence=[], extra_evidence=[]"
    );
    println!();

    let baseline = run(
        &document,
        &input,
        Scenario::Baseline,
        &RunConfig::new(RUN_ID, BUDGET_MILLI),
    )?
    .into_finished()?;

    println!("[1] Information needs derived from operation contracts, before any data was read");
    let plan = invocation(&baseline, "plan").expect("the plan handler ran");
    let needs_compliance = json_array(&plan.output, "needs_compliance");
    let needs_prices = json_array(&plan.output, "needs_prices");
    for contract in json_array(&plan.output, "operations") {
        let required = contract
            .get("required")
            .and_then(Json::as_array)
            .map_or(0, <[Json]>::len);
        println!(
            "    contract {}: {} declared required inputs",
            json_str(contract, "id"),
            required
        );
    }
    println!(
        "    plan produced {} needs ({} compliance + {} price):",
        needs_compliance.len() + needs_prices.len(),
        needs_compliance.len(),
        needs_prices.len()
    );
    for need in needs_compliance.iter().chain(needs_prices.iter()) {
        println!("      - {}", json_str(need, "id"));
    }

    println!();
    println!("[2] Retrieval through a deterministic, offline mock provider");
    let provider_calls = baseline.charge_count("mock.supplier_facts");
    println!(
        "    provider effects dispatched: {provider_calls}; source = in-memory supplier register, no network, pure function"
    );
    for evidence in baseline.evidence.iter().take(3) {
        println!(
            "      {} = {} from {}",
            evidence.need_id,
            render(&evidence.value),
            evidence.source_ref.as_deref().unwrap_or("-")
        );
    }

    let missing = run(
        &document,
        &input,
        Scenario::MissingEvidence,
        &RunConfig::new(RUN_ID, BUDGET_MILLI),
    )?
    .into_finished()?;

    println!();
    println!(
        "[3] Missing data is detected and recorded, never guessed (scenario: missing_evidence)"
    );
    for evidence in missing.evidence.iter().filter(|entry| !entry.is_verified()) {
        println!(
            "    {} -> {} ({})",
            evidence.need_id,
            evidence.status.as_str(),
            evidence.reason
        );
    }

    println!();
    println!("[4] Evidence verification: generic VERIFIED / AMBIGUOUS / REJECTED + provenance");
    let counts = baseline.evidence_status_counts();
    println!(
        "    baseline: VERIFIED={} AMBIGUOUS={} REJECTED={}",
        counts.verified, counts.ambiguous, counts.rejected
    );
    let missing_counts = missing.evidence_status_counts();
    println!(
        "    missing_evidence: VERIFIED={} AMBIGUOUS={} REJECTED={}",
        missing_counts.verified, missing_counts.ambiguous, missing_counts.rejected
    );
    println!("    provenance per fact (baseline):");
    for evidence in &baseline.evidence {
        println!(
            "      {:<18} {:<9} {}",
            evidence.need_id,
            evidence.status.as_str(),
            evidence.source_ref.as_deref().unwrap_or("-")
        );
    }

    println!();
    println!("[5] Operation required_inputs checked with the ALL_REQUIRED gate");
    println!("    baseline: screen_compliance and select_cheapest gates are satisfied");
    let screen_missing = invocation(&missing, "screen").expect("the screen operation ran");
    println!(
        "    missing_evidence: screen_compliance -> {}",
        screen_missing.status().unwrap_or("-")
    );
    println!(
        "    gate reason: {}",
        json_str(&screen_missing.output, "reason")
    );
    for input in json_array(&screen_missing.output, "missing") {
        let need = input.get("need").unwrap_or(&Json::Null);
        println!(
            "      missing {} ({})",
            need.get("id").and_then(Json::as_str).unwrap_or("-"),
            json_str(input, "reason")
        );
    }

    println!();
    println!("[6] Deterministic comparison: lowest price among compliant suppliers");
    let price = invocation(&baseline, "price").expect("the price operation ran");
    for ranked in json_array(&price.output, "ranked") {
        println!(
            "    {} — {} SEK",
            json_str(ranked, "name"),
            json_u64(ranked, "price_sek")
        );
    }
    println!(
        "    rule: minimum price, tie-break by supplier id, so map ordering cannot change the result"
    );

    println!();
    println!("[7] Abstention is a terminal incomplete run with an explicit reason");
    println!("    terminal status: {:?}", missing.status);
    println!(
        "    conclusion: {}",
        missing.conclusion_str("conclusion").unwrap_or("-")
    );
    println!(
        "    reason: {}",
        missing.conclusion_str("reason").unwrap_or("-")
    );
    assert_eq!(missing.status, RunStatus::Incomplete);
    assert_eq!(
        missing.conclusion_str("conclusion"),
        Some("INSUFFICIENT_EVIDENCE")
    );

    println!();
    println!("[8] Sweycoin consumption from the reused swe_budget ledger (per action + total)");
    println!(
        "    {:<32} {:>5} {:>10} {:>14}",
        "action", "calls", "unit milli", "total milli"
    );
    for (action, total) in baseline.action_totals() {
        let calls = baseline.charge_count(&action);
        let unit = total / calls.max(1) as u64;
        println!("    {action:<32} {calls:>5} {unit:>10} {total:>14}");
    }
    let sum: u64 = baseline
        .action_totals()
        .iter()
        .map(|(_, total)| *total)
        .sum();
    assert_eq!(sum, baseline.spent_milli);
    println!(
        "    total: {} milli Sweycoin spent of {} allocated ({} remaining); {} ledger events; sum(per action) == ledger spent",
        baseline.spent_milli,
        baseline.allocated_milli,
        baseline.available_milli,
        baseline.ledger_events
    );

    println!();
    println!("[9] Pause and resume an ongoing investigation with a journal");
    let pause_config = RunConfig::new(RUN_ID, BUDGET_MILLI).with_pause_at_spend(CHECKPOINT_MILLI);
    let paused_outcome = run(&document, &input, Scenario::Baseline, &pause_config)?;
    let paused = paused_outcome
        .paused()
        .expect("the tight checkpoint pauses the run");
    println!(
        "    segment 1 paused at {} milli spent (checkpoint {} milli); {} actions executed; {} completed invocation results journaled",
        paused.segment.spent_milli,
        CHECKPOINT_MILLI,
        paused.segment.executed_invocations,
        paused.journal.completed.len()
    );
    let journal_path = std::env::temp_dir().join("swe-reference-journal.json");
    write_journal(&journal_path, &paused.journal)?;
    println!("    journal written to {}", journal_path.display());
    let loaded = read_journal(&journal_path)?;
    assert_eq!(loaded.definition_sha256, definition_sha256);
    let resumed = resume(&loaded, &ResumeConfig::new())?.into_finished()?;
    println!(
        "    segment 2 replayed {} completed invocations without re-running handlers or re-charging, then executed {} new actions",
        resumed.replayed_invocations,
        resumed.executed_invocations - loaded.completed.len()
    );
    println!(
        "    resumed conclusion: {}; total spent {} milli (uninterrupted run spent {} milli)",
        resumed.conclusion_str("conclusion").unwrap_or("-"),
        resumed.spent_milli,
        baseline.spent_milli
    );
    println!(
        "    unique invocations executed: {} (uninterrupted: {}) — nothing ran twice",
        resumed.executed_invocations, baseline.executed_invocations
    );
    assert_eq!(resumed.spent_milli, baseline.spent_milli);
    assert_eq!(resumed.available_milli, baseline.available_milli);
    assert_eq!(resumed.executed_invocations, baseline.executed_invocations);
    assert_eq!(resumed.ledger.events(), baseline.ledger.events());
    assert_eq!(resumed.charges, baseline.charges);

    // The engine journal, not a demo-private bookkeeping, tells the story:
    // one attempt, one confirmation and one settlement per action, carried
    // across the pause.
    let settlements = resumed
        .journal_events
        .iter()
        .filter(|kind| **kind == "ChargeSettled")
        .count();
    println!(
        "    engine journal: {} events — {} attempts, {} confirmations, {} reservations, {} settlements; settled {} milli",
        resumed.journal_events.len(),
        resumed.journal_events.iter().filter(|k| **k == "AttemptStarted").count(),
        resumed.journal_events
            .iter()
            .filter(|k| **k == "ResultConfirmed")
            .count(),
        resumed
            .journal_events
            .iter()
            .filter(|k| **k == "ChargeReserved")
            .count(),
        settlements,
        resumed.journal_settled_milli
    );
    assert_eq!(
        settled_actions(&resumed),
        resumed.executed_invocations,
        "exactly one settlement per executed action"
    );
    assert_eq!(resumed.journal_settled_milli, resumed.spent_milli);
    assert_eq!(resumed.journal_open_attempts, 0);
    assert_eq!(resumed.journal_pending_charges, 0);

    println!();
    println!("[10] NeedMore: an operation discovers a new need, resolves it, then re-runs");
    let screens = invocations(&baseline, "screen");
    for (index, log) in screens.iter().enumerate() {
        match log.status() {
            Some("need_more") => {
                println!("    screen_compliance pass {}: NeedMore", index + 1);
                for need in json_array(&log.output, "needs") {
                    println!(
                        "      NeedRef {} — {}",
                        json_str(need, "id"),
                        json_str(need, "reason")
                    );
                }
            }
            Some(status) => println!(
                "    screen_compliance pass {}: {} (compliant: {})",
                index + 1,
                status,
                json_array(&log.output, "compliant")
                    .iter()
                    .map(|id| id.as_str().unwrap_or("-"))
                    .collect::<Vec<_>>()
                    .join(", ")
            ),
            None => println!("    screen_compliance pass {}: no status", index + 1),
        }
    }
    let exception_fact = baseline
        .evidence
        .iter()
        .find(|evidence| evidence.need_id == "alpha.c_exception")
        .expect("the discovered exception need was resolved");
    println!(
        "    investigation resolved it: {} = {} from {}",
        exception_fact.need_id,
        render(&exception_fact.value),
        exception_fact.source_ref.as_deref().unwrap_or("-")
    );
    assert_eq!(baseline.charge_count("reference.screen_compliance"), 2);

    println!();
    println!(
        "Conclusion: {} ({} SEK) — {}",
        baseline.conclusion_str("conclusion").unwrap_or("-"),
        json_u64(
            baseline.conclusion.as_ref().unwrap_or(&Json::Null),
            "price_sek"
        ),
        baseline.conclusion_str("reason").unwrap_or("-")
    );
    assert_eq!(baseline.status, RunStatus::Completed);
    assert_eq!(baseline.conclusion_str("conclusion"), Some("Beta"));
    println!();
    println!("All ten demonstrations completed. Every action above was metered, every fact was retrieved through the engine.");
    Ok(())
}

fn invocation<'a>(report: &'a RunReport, node_id: &str) -> Option<&'a InvocationLog> {
    report.invocations.iter().find(|log| log.node_id == node_id)
}

fn invocations<'a>(report: &'a RunReport, node_id: &str) -> Vec<&'a InvocationLog> {
    report
        .invocations
        .iter()
        .filter(|log| log.node_id == node_id)
        .collect()
}

fn json_str<'a>(value: &'a Json, key: &str) -> &'a str {
    value.get(key).and_then(Json::as_str).unwrap_or("-")
}

fn json_array<'a>(value: &'a Json, key: &str) -> &'a [Json] {
    value.get(key).and_then(Json::as_array).unwrap_or(&[])
}

fn json_u64(value: &Json, key: &str) -> u64 {
    value
        .get(key)
        .and_then(Json::as_number)
        .and_then(|number| number.as_u64())
        .unwrap_or(0)
}

fn render(value: &Json) -> String {
    match value {
        Json::Null => "null".to_string(),
        Json::Bool(value) => value.to_string(),
        Json::Number(value) => value.to_string(),
        Json::String(value) => value.clone(),
        Json::Array(_) => "array".to_string(),
        Json::Object(_) => "object".to_string(),
    }
}

/// One settlement per executed action: the property the journal must preserve
/// across a pause and a resume.
fn settled_actions(report: &swe_reference::RunReport) -> usize {
    report
        .journal_events
        .iter()
        .filter(|kind| **kind == "ChargeSettled")
        .count()
}
