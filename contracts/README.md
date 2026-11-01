# Frozen contracts, v1

These files are the **behavioural surface** the extraction promises not to change
without a version bump. They are not documentation of the implementation; they
are inputs the Rust contract tests execute.

| File | Contract | What it pins |
| --- | --- | --- |
| `swd-v1-workflow.json` | SWD 1.0 | A complete workflow document: task, expression and provider decisions, map, fork/join, end statuses |
| `journal-v1-header.json` | Journal header v1 | `schema_version`, run identity, definition hash, contract version, dependency versions |
| `journal-v1-events.json` | Journal events v1 | Every event kind, including an unconfirmed external charge |
| `MANIFEST.sha256` | Structural | SHA-256 of each `*.json` file above |

The frozen fixture set is exactly the `*.json` files above. This README and
`MANIFEST.sha256` are not hashed by it (a file cannot hash itself), and editing
this README is documentation, not a contract change.

## Two gates, not one

A hash proves the **bytes** did not change. It does not prove the **behaviour**
did not change, and it cannot tell an intended change from an accidental one. The
contract gate therefore has two halves, both in `crates/swe-reference/tests/contracts.rs`:

1. **Structural** — every `*.json` fixture matches `MANIFEST.sha256`, and the
   directory contains no unlisted `*.json` file. Non-JSON documentation in the
   directory is not part of the frozen set.
2. **Semantic** — the fixtures are *executed* and each one must equal what the
   emitter produces (`the_frozen_fixtures_are_exactly_what_the_emitter_produces`):
   the SWD document is compiled and must hash to the definition hash in the
   journal header; every journal event decodes and re-encodes identically;
   `NeedMore` repeats end the loop; duplicated need statuses and `AMBIGUOUS`
   evidence are typed refusals; journal recovery is decided per delivery class;
   the header's `contract_version`, dependency versions and `schema_version` are
   pinned to the code's constants. Wrong schema and contract version numbers are
   refused and the number is named (a contract-version mismatch currently reuses
   the `schema_version` message wording, naming only the refused number); a
   changed dependency version is refused and the error names the dependency and
   both the recorded and current version.

The version decision itself is enforced by `scripts/check_contract_freeze.py`:
it fingerprints the fixture form and the public API and requires the owning
version declaration to change. It reads the declaration from the token stream,
so a comment or string literal that merely spells a version assignment is not a
bump.

## Changing a contract

Both halves must change together, deliberately:

1. Change the contract and add a **migration test or a documented breaking
   decision** (a short note in `docs/swe-standalone/` saying what breaks and why).
2. Bump the relevant version: `JOURNAL_SCHEMA_VERSION`, `CONTRACT_VERSION`, or
   the SWD contract's own version.
3. Regenerate the fixtures:
   `cargo run -p swe-reference --example emit_contracts`
   The semantic test compares each fixture with the emitter output, so a stale
   or hand-edited fixture fails CI.
4. Update `MANIFEST.sha256` in the **same commit**:
   `cd contracts && shasum -a 256 *.json > MANIFEST.sha256`

A PR that changes a fixture's *shape* without its contract version bump fails
CI, and so does a changed manifest; a PR that bumps the version without a
migration test or a documented breaking decision is rejected in review. That is
the whole mechanism — no silent drift, and no ceremony when no frozen file
changes.

## What the semantic tests do *not* cover

They cover what the code can express in Rust. Python-side wire behaviour
(adapter readers, differential tests) is covered by the standalone harness in
`python/tests/`, which does not read these fixtures; only the Rust tests execute
them. Neither replaces the other.
