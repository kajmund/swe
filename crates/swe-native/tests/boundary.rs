//! The framework boundary: the engine owns the mechanism, never the domain.
//!
//! `swe-native` must stay describable, testable and usable without any
//! application's vocabulary. This test fails if a generic module starts naming
//! a domain's facts, vendors, models or document concepts — including inside
//! test fixtures, because fixtures are what future code copies from.
//!
//! See `docs/swe-standalone/09-contract-decision.md` §5 and
//! `11-pr2-b2-core-contracts.md` C2.2.

use std::fs;
use std::path::{Path, PathBuf};

/// Words that only make sense inside one application.
const DOMAIN_TERMS: &[&str] = &[
    "avtal",
    "uppsäg",
    "notice_period",
    "legal",
    "juridik",
    "las",
    "gliner",
    "jev",
    "chunk",
    "research",
    "socialism",
    "overgraph",
    "supplier",
    "leverantör",
    "price_sek",
    "kunskap",
];

fn source_files() -> Vec<PathBuf> {
    // Scan fixtures too: integration tests are what future code copies from,
    // and a domain word there is exactly the leak this gate exists to stop.
    let manifest = Path::new(env!("CARGO_MANIFEST_DIR"));
    let mut stack = vec![manifest.join("src"), manifest.join("tests")];
    let mut files = Vec::new();
    while let Some(dir) = stack.pop() {
        let Ok(entries) = fs::read_dir(&dir) else {
            continue;
        };
        for entry in entries.flatten() {
            let path = entry.path();
            if path.is_dir() {
                stack.push(path);
            } else if path.extension().is_some_and(|ext| ext == "rs") {
                // This file is the allowlist; it must name the terms.
                if path == manifest.join("tests").join("boundary.rs") {
                    continue;
                }
                files.push(path);
            }
        }
    }
    files
}

/// Match a term as a whole word so `classify` does not match `las`.
fn contains_word(line: &str, term: &str) -> bool {
    let bytes = line.as_bytes();
    let needle = term.as_bytes();
    let mut start = 0;
    while let Some(offset) = line[start..].find(term) {
        let at = start + offset;
        let before_ok = at == 0 || !is_word_byte(bytes[at - 1]);
        let end = at + needle.len();
        let after_ok = end >= bytes.len() || !is_word_byte(bytes[end]);
        if before_ok && after_ok {
            return true;
        }
        start = at + 1;
    }
    false
}

fn is_word_byte(byte: u8) -> bool {
    byte.is_ascii_alphanumeric() || byte == b'_'
}

#[test]
fn generic_modules_contain_no_application_vocabulary() {
    let mut offenders = Vec::new();
    for path in source_files() {
        let text = fs::read_to_string(&path).unwrap_or_default();
        let lowered = text.to_lowercase();
        for term in DOMAIN_TERMS {
            // A comment that explains the boundary may name a domain term
            // followed by a colon-free explanatory clause; code and fixtures
            // may not.
            for (index, line) in lowered.lines().enumerate() {
                let lowered_line = line.to_lowercase();
                let trimmed = line.trim_start();
                // Doc comments are documentation that ships with the API, so
                // they are scanned. Only a plain `//` explanation is exempt.
                let is_plain_comment = trimmed.starts_with("//") && !trimmed.starts_with("///");
                if is_plain_comment {
                    continue;
                }
                if contains_word(&lowered_line, term) {
                    offenders.push(format!(
                        "{}:{}: {:?} (domain term {:?})",
                        path.display(),
                        index + 1,
                        trimmed,
                        term
                    ));
                }
            }
        }
    }
    assert!(
        offenders.is_empty(),
        "the engine must not name application concepts; inject them instead:\n{}",
        offenders.join("\n")
    );
}

#[test]
fn public_api_is_mechanism_only() {
    // The exported surface is named after mechanisms, not domains.
    let lib = fs::read_to_string(Path::new(env!("CARGO_MANIFEST_DIR")).join("src/lib.rs"))
        .expect("lib.rs");
    for forbidden in ["Avtal", "Supplier", "Contract", "Legal", "Chunk"] {
        assert!(
            !lib.contains(forbidden),
            "public API must not expose domain type {forbidden:?}"
        );
    }
}
