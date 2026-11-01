//! The frozen contract gate: structural hashes **and** executed semantics.
//!
//! See `contracts/README.md`. A hash only proves the bytes did not change; these
//! tests execute the fixtures so a behavioural change is caught even when the
//! bytes were regenerated.

use std::collections::BTreeMap;
use std::path::PathBuf;

use swe_native::{
    canonical_json_bytes, parse_json_str, sha256_hex, ChargeState, CompiledDefinition, Delivery,
    GateError, Journal, JournalEvent, JournalHeader, NeedDiscovery, NeedLoopError, NeedRef,
    NeedStatus, OperationSpec, Recovery, JOURNAL_SCHEMA_VERSION,
};
use swe_reference::{journal_events_from_json, journal_events_to_json};

fn contracts_dir() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../contracts")
}

fn read(name: &str) -> Vec<u8> {
    std::fs::read(contracts_dir().join(name)).unwrap_or_else(|error| panic!("read {name}: {error}"))
}

fn fixture(name: &str) -> swe_native::Json {
    let text = String::from_utf8(read(name)).expect("utf-8 fixture");
    parse_json_str(&text).unwrap_or_else(|error| panic!("parse {name}: {error}"))
}

fn fixture_digest(name: &str) -> String {
    sha256_hex(&fixture(name)).expect("digest")
}

/// The SHA-256 of the file's **bytes**, which is what the manifest pins.
/// Hashing the parsed, canonicalised JSON instead would let whitespace and key
/// order change while the manifest stayed green.
fn file_digest(name: &str) -> String {
    use sha2::{Digest, Sha256};
    let mut hasher = Sha256::new();
    hasher.update(read(name));
    format!("{:x}", hasher.finalize())
}

/// The definition hash the journal header fixture pins. The SWD fixture must
/// compile to exactly this hash, which is what makes the two fixtures one
/// contract rather than two files.
fn header_field(name: &str) -> swe_native::Json {
    let header = fixture("journal-v1-header.json");
    header
        .get(name)
        .cloned()
        .unwrap_or_else(|| panic!("journal-v1-header.json has no {name:?}"))
}

fn header_definition_sha256() -> String {
    header_field("definition_sha256")
        .as_str()
        .expect("definition_sha256 is a string")
        .to_string()
}

fn header_contract_version() -> u32 {
    u32::try_from(
        header_field("contract_version")
            .as_number()
            .and_then(|number| number.as_u64())
            .expect("contract_version is an integer"),
    )
    .expect("contract_version fits u32")
}

fn dependency_versions(header: &swe_native::Json) -> BTreeMap<String, u32> {
    let dependencies = header
        .get("dependency_versions")
        .expect("journal header has dependency_versions");
    let mut versions = BTreeMap::new();
    for (name, version) in dependencies
        .as_object()
        .expect("dependency_versions is an object")
    {
        versions.insert(
            name.clone(),
            u32::try_from(
                version
                    .as_number()
                    .and_then(|n| n.as_u64())
                    .expect("version"),
            )
            .expect("fits u32"),
        );
    }
    versions
}

fn header_dependencies() -> BTreeMap<String, u32> {
    dependency_versions(&fixture("journal-v1-header.json"))
}

fn emitted_contract_version(header: &swe_native::Json) -> u32 {
    u32::try_from(
        header
            .get("contract_version")
            .and_then(|value| value.as_number())
            .and_then(|number| number.as_u64())
            .expect("emitted header has an integer contract_version"),
    )
    .expect("contract_version fits u32")
}

// ---------------------------------------------------------------- structural

#[test]
fn every_frozen_contract_matches_its_manifest() {
    let manifest = String::from_utf8(read("MANIFEST.sha256")).expect("manifest utf-8");
    let mut listed = Vec::new();
    for line in manifest.lines().filter(|line| !line.trim().is_empty()) {
        let mut parts = line.split_whitespace();
        let expected = parts.next().expect("hash");
        let name = parts.next().expect("name");
        assert!(
            parts.next().is_none(),
            "unexpected manifest field in {line:?}"
        );
        let actual = file_digest(name);
        assert_eq!(
            actual, expected,
            "{name} changed: if this is intended, bump the contract version and \
             regenerate MANIFEST.sha256 in the same commit (contracts/README.md)"
        );
        listed.push(name.to_string());
    }
    listed.sort();

    let mut present: Vec<String> = std::fs::read_dir(contracts_dir())
        .expect("contracts dir")
        .filter_map(|entry| entry.ok())
        .map(|entry| entry.file_name().to_string_lossy().to_string())
        .filter(|name| name.ends_with(".json"))
        .collect();
    present.sort();
    assert_eq!(
        present, listed,
        "every contract fixture must be listed in MANIFEST.sha256"
    );
}

// ----------------------------------------------------------------- semantic

#[test]
fn the_swd_fixture_compiles_to_the_definition_hash_the_journal_pins() {
    // One contract, not two files: the document and the header must agree.
    let document = fixture("swd-v1-workflow.json");
    let pinned = header_definition_sha256();
    assert_eq!(
        fixture_digest("swd-v1-workflow.json"),
        pinned,
        "the fixture's canonical hash must be the header's definition hash"
    );
    let compiled = CompiledDefinition::load(document.clone(), &pinned).expect("compiles");
    // Every node the workflow declares is addressable through the frozen shape.
    for node in ["plan", "screen", "fetch_fact", "price", "finalize", "done"] {
        assert!(compiled.node(node).is_ok(), "node {node} must resolve");
    }
    // And the canonical form is stable: re-encoding the parsed fixture is a
    // no-op, so the hash above is about content, not formatting.
    let re_encoded = canonical_json_bytes(&document).expect("canonical");
    assert_eq!(read("swd-v1-workflow.json"), re_encoded);
}

/// The fixtures are the emitter's output, not a second hand-maintained copy.
/// Whatever the constants say, the shipped files must say the same; otherwise
/// the "frozen contract" would document a world the code does not build.
#[test]
fn the_frozen_fixtures_are_exactly_what_the_emitter_produces() {
    assert_eq!(
        fixture("swd-v1-workflow.json"),
        swe_reference::workflow_document(),
        "swd-v1-workflow.json is stale: regenerate the fixtures with \
         `cargo run -p swe-reference --example emit_contracts`"
    );
    assert_eq!(
        fixture("journal-v1-header.json"),
        swe_reference::contract_journal_header(),
        "journal-v1-header.json is stale: regenerate the fixtures with \
         `cargo run -p swe-reference --example emit_contracts`"
    );
    assert_eq!(
        fixture("journal-v1-events.json"),
        swe_reference::contract_events(),
        "journal-v1-events.json is stale: regenerate the fixtures with \
         `cargo run -p swe-reference --example emit_contracts`"
    );
}

#[test]
fn the_journal_header_fixture_pins_every_version() {
    // The expected side comes from the emitter, which builds the header from
    // the real constants (CONTRACT_VERSION, current_dependency_versions() and
    // JOURNAL_SCHEMA_VERSION). Reading a value from the fixture and feeding the
    // same value back into verify() would compare a value with itself.
    let emitted = swe_reference::contract_journal_header();
    assert_eq!(
        header_field("schema_version")
            .as_number()
            .and_then(|n| n.as_u64()),
        Some(u64::from(JOURNAL_SCHEMA_VERSION)),
        "a new schema version is a contract change, not a fixture edit"
    );
    assert_eq!(
        header_field("contract_version")
            .as_number()
            .and_then(|n| n.as_u64()),
        Some(u64::from(emitted_contract_version(&emitted))),
        "the frozen contract version must be the code's CONTRACT_VERSION"
    );
    assert_eq!(
        header_dependencies(),
        dependency_versions(&emitted),
        "the frozen dependency versions must be the code's current versions"
    );
    assert_eq!(
        header_field("run_id").as_str(),
        Some("run-contract-v1"),
        "the fixture must stay a fixed example"
    );

    let header = JournalHeader {
        schema_version: JOURNAL_SCHEMA_VERSION,
        run_id: "run-contract-v1".to_string(),
        definition_sha256: header_definition_sha256(),
        contract_version: header_contract_version(),
        dependency_versions: header_dependencies(),
    };
    header
        .verify(
            &header_definition_sha256(),
            emitted_contract_version(&emitted),
            &dependency_versions(&emitted),
        )
        .expect("the pinned header verifies against the world the code builds");
}

#[test]
fn a_wrong_schema_version_is_refused_and_names_it() {
    let mut header = JournalHeader::new("run", &header_definition_sha256(), 1);
    header.schema_version = JOURNAL_SCHEMA_VERSION + 99;
    let error = header
        .verify(&header_definition_sha256(), 1, &BTreeMap::new())
        .expect_err("a foreign schema version cannot be resumed");
    assert!(
        error
            .to_string()
            .contains(&(JOURNAL_SCHEMA_VERSION + 99).to_string()),
        "the error must name the version it refused: {error}"
    );
}

#[test]
fn a_wrong_contract_version_is_refused_and_names_it() {
    let contract_version = header_contract_version();
    let header = JournalHeader::new("run", &header_definition_sha256(), contract_version);
    let error = header
        .verify(
            &header_definition_sha256(),
            contract_version + 7,
            &BTreeMap::new(),
        )
        .expect_err("a different contract version cannot be resumed");
    assert!(
        error.to_string().contains(&contract_version.to_string()),
        "the error must name the refused contract version: {error}"
    );
}

#[test]
fn a_changed_definition_or_dependency_is_refused() {
    let header = JournalHeader {
        schema_version: JOURNAL_SCHEMA_VERSION,
        run_id: "run".to_string(),
        definition_sha256: header_definition_sha256(),
        contract_version: header_contract_version(),
        dependency_versions: header_dependencies(),
    };
    let error = header
        .verify(
            "0".repeat(64).as_str(),
            header_contract_version(),
            &header_dependencies(),
        )
        .expect_err("a different definition cannot be resumed");
    assert!(
        error
            .to_string()
            .contains("does not match the resumed definition"),
        "the error must name the changed definition: {error}"
    );

    let error = header
        .verify(
            &header_definition_sha256(),
            header_contract_version(),
            &BTreeMap::new(),
        )
        .expect_err("a dependency recorded in the journal cannot vanish");
    assert!(
        error.to_string().contains("mock.supplier_facts")
            && error.to_string().contains("no longer available"),
        "the error must name the missing dependency and its version: {error}"
    );

    let mut changed = header_dependencies();
    for version in changed.values_mut() {
        *version += 1;
    }
    let error = header
        .verify(
            &header_definition_sha256(),
            header_contract_version(),
            &changed,
        )
        .expect_err("a changed dependency version cannot be resumed");
    assert!(
        error
            .to_string()
            .contains("dependency mock.supplier_facts changed from version"),
        "the error must name the dependency and both versions: {error}"
    );
}

#[test]
fn every_journal_event_kind_decodes_and_re_encodes_identically() {
    let encoded = fixture("journal-v1-events.json");
    let events = journal_events_from_json(&encoded).expect("the frozen log decodes");

    // Every variant the v1 format defines must appear, or a wire change to the
    // missing one would pass unnoticed. Count distinct kinds, not array entries.
    let kinds: std::collections::BTreeSet<String> = encoded
        .as_array()
        .expect("array")
        .iter()
        .map(|event| {
            event
                .get("kind")
                .and_then(swe_native::Json::as_str)
                .expect("every event names its kind")
                .to_string()
        })
        .collect();
    assert_eq!(
        kinds,
        std::collections::BTreeSet::from([
            "AttemptStarted".to_string(),
            "ChargeReserved".to_string(),
            "ResultConfirmed".to_string(),
            "ChargeSettled".to_string(),
            "ChargePendingReconciliation".to_string(),
            "AttemptAbandoned".to_string(),
            "ChargeReleased".to_string(),
        ]),
        "the frozen log must cover every journal event variant"
    );
    let re_encoded = journal_events_to_json(&events);
    assert_eq!(
        re_encoded, encoded,
        "decoding and re-encoding must be lossless, or the format is not a format"
    );
    // An unknown kind is a version problem, not a silently skipped event.
    let mut broken = encoded.as_array().expect("array").to_vec();
    let mut unknown = swe_native::Json::Object(Default::default());
    if let swe_native::Json::Object(object) = &mut unknown {
        object.insert("kind".to_string(), "NotAnEvent".into());
    }
    broken.push(unknown);
    assert!(journal_events_from_json(&swe_native::Json::Array(broken)).is_err());
}

#[test]
fn a_journal_event_missing_a_required_number_is_refused() {
    // A missing amount or timestamp must not become zero: a zero settlement
    // under-reports what a run cost.
    let encoded = fixture("journal-v1-events.json");
    for field in ["amount_milli", "settled_at_ms"] {
        let mut broken = encoded.as_array().expect("array").to_vec();
        let mut settled = swe_native::Json::Object(Default::default());
        if let swe_native::Json::Object(object) = &mut settled {
            object.insert("kind".to_string(), "ChargeSettled".into());
            object.insert("charge_id".to_string(), "ch-x".into());
            if field != "amount_milli" {
                object.insert("amount_milli".to_string(), swe_native::Json::from_u64(1));
            }
            if field != "settled_at_ms" {
                object.insert("settled_at_ms".to_string(), swe_native::Json::from_u64(2));
            }
        }
        broken.push(settled);
        assert!(
            journal_events_from_json(&swe_native::Json::Array(broken)).is_err(),
            "a ChargeSettled without {field} must be refused, not defaulted"
        );
    }
}

#[test]
fn a_need_more_repeat_ends_the_loop() {
    let mut discovery = NeedDiscovery::new();
    let need = NeedRef::new("c_exception", "alpha");
    discovery.record(&need).expect("first discovery is fine");
    match discovery.record(&need) {
        Err(NeedLoopError::RepeatedNeed(repeated)) => assert_eq!(repeated, need),
        other => panic!("a repeated need must end the loop, got {other:?}"),
    }
}

#[test]
fn duplicate_need_statuses_and_ambiguous_evidence_are_typed_refusals() {
    let spec = OperationSpec::new("screen", vec![NeedRef::new("req_a", "alpha")]);

    // AMBIGUOUS is not usable, and it is the engine that says so.
    let ambiguous = vec![(NeedRef::new("req_a", "alpha"), NeedStatus::Ambiguous)];
    let failure = spec
        .gate(&ambiguous)
        .expect_err("AMBIGUOUS must block")
        .unsatisfied()
        .expect("a real verdict")
        .clone();
    assert_eq!(failure.missing.len(), 1);
    assert_eq!(failure.missing[0].status, NeedStatus::Ambiguous);

    // A duplicated status is a malformed call, not a verdict.
    let duplicated = vec![
        (NeedRef::new("req_a", "alpha"), NeedStatus::Resolved),
        (NeedRef::new("req_a", "alpha"), NeedStatus::Ambiguous),
    ];
    match spec.gate(&duplicated) {
        Err(GateError::DuplicateState { need }) => assert_eq!(need.id(), "alpha.req_a"),
        other => panic!("duplicate statuses must be refused, got {other:?}"),
    }
}

#[test]
fn journal_recovery_is_decided_per_delivery_class() {
    let mut journal = Journal::new();
    for (key, delivery) in [
        ("pure", Delivery::Pure),
        ("replayable", Delivery::Replayable),
        ("idempotent", Delivery::ExternalIdempotent),
        ("irreversible", Delivery::ExternalNonIdempotent),
    ] {
        let attempt = format!("{key}:1");
        journal
            .append(JournalEvent::AttemptStarted {
                attempt_id: attempt.clone(),
                delivery_key: key.to_string(),
                invocation_id: format!("inv-{key}"),
                action: format!("demo.{key}"),
                delivery,
                started_at_ms: 1,
            })
            .expect("append");
    }
    let plan = swe_native::plan_recovery(&journal);
    assert_eq!(plan.len(), 4);
    let mut kinds: Vec<String> = plan
        .iter()
        .map(|recovery| match recovery {
            Recovery::Recompute { attempt_id } => format!("recompute:{attempt_id}"),
            Recovery::ReplayRequiresResult { attempt_id } => format!("replay:{attempt_id}"),
            Recovery::IdempotentRetry {
                attempt_id,
                delivery_key,
            } => format!("idempotent:{attempt_id}:{delivery_key}"),
            Recovery::FailStop { attempt_id } => format!("failstop:{attempt_id}"),
        })
        .collect();
    kinds.sort();
    assert_eq!(
        kinds,
        vec![
            "failstop:irreversible:1",
            "idempotent:idempotent:1:idempotent",
            "recompute:pure:1",
            "replay:replayable:1",
        ],
        "the irreversible action must never be retried"
    );
}

#[test]
fn an_unconfirmed_external_charge_is_never_reported_as_zero() {
    let mut journal = Journal::new();
    journal
        .append(JournalEvent::AttemptStarted {
            attempt_id: "x:1".to_string(),
            delivery_key: "x".to_string(),
            invocation_id: "inv-x".to_string(),
            action: "demo.external".to_string(),
            delivery: Delivery::ExternalNonIdempotent,
            started_at_ms: 1,
        })
        .expect("append");
    journal
        .append(JournalEvent::ChargeReserved {
            charge_id: "ch-x".to_string(),
            attempt_id: "x:1".to_string(),
            delivery_key: "x".to_string(),
            amount_milli: 2_000,
            tariff_milli: 2_000,
            reserved_at_ms: 2,
        })
        .expect("append");
    journal
        .append(JournalEvent::ChargePendingReconciliation {
            charge_id: "ch-x".to_string(),
            reason: "external side effect unknown after crash".to_string(),
            at_ms: 3,
        })
        .expect("append");

    assert_eq!(
        journal.charge_state("ch-x"),
        Some(ChargeState::PendingReconciliation)
    );
    assert_eq!(
        journal.settled_total(),
        0,
        "nothing may be reported as settled"
    );
    let unresolved = swe_native::unresolved_charges(&journal);
    assert_eq!(unresolved.len(), 1, "the operator must see it");
    assert_eq!(unresolved[0].0, "ch-x");
    // And it cannot be written off as zero by a release.
    assert!(journal
        .append(JournalEvent::ChargeReleased {
            charge_id: "ch-x".to_string(),
            reason: "assume zero".to_string(),
            released_at_ms: 4,
        })
        .is_err());
}
