//! Writes the frozen v1 contract fixtures in canonical form.
//!
//! Run only when a contract version is deliberately changed:
//!   cargo run -p swe-reference --example emit_contracts
//! and update `contracts/MANIFEST.sha256` in the same commit.

use std::path::PathBuf;

fn write(dir: &std::path::Path, name: &str, value: &swe_native::Json) {
    let bytes = swe_native::canonical_json_bytes(value).expect("canonical JSON");
    let path = dir.join(name);
    std::fs::write(&path, &bytes).expect("write fixture");
    println!("{}", path.display());
}

fn main() {
    let root = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../..");
    let contracts = root.join("contracts");
    std::fs::create_dir_all(&contracts).expect("contracts dir");

    let document = swe_reference::workflow_document();
    write(&contracts, "swd-v1-workflow.json", &document);
    write(
        &contracts,
        "journal-v1-header.json",
        &swe_reference::contract_journal_header(),
    );
    write(
        &contracts,
        "journal-v1-events.json",
        &swe_reference::contract_events(),
    );
}
