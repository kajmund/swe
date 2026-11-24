# SWE agent delivery

This file is the delivery gate. An unmerged diagnostic branch does not use it
between iterations.

## Merge candidate

Delivery is one clean commit. The reviewed tree is that commit and nothing
else. Record the receipt in the PR body before merge and leave it there. The
field list matches the Research delivery receipt:

- base SHA and head SHA
- whether the tree is clean
- runtime and dependency versions
- every test and lint command with its exit code
- architecture-gate results
- review terminal, verdict, and where the report text is stored

After a normal merge, add verification of the merged SHA to the same receipt.

## Local CI

Run the jobs in [`.github/workflows/ci.yml`](.github/workflows/ci.yml) on that
exact SHA. `make test` is the fast local suite and does not replace the
workflow.

Always run the workflow's always-on jobs: Rust format, Clippy, `cargo test
--workspace --locked`, the reference app, the Python wheel and
`pytest -m "not smoke"`, the out-of-checkout install, license check, and
package dry runs. Run contract freeze when the selector's contract surface
changed. Run the mutation gate when the selector's core surface changed. A
job the selector skips is a completed outcome.

A failed test, lint, or selected gate blocks delivery.

## Review

Delivery also requires an independent DeepSeek harness review of that same
SHA, including when GitHub CI is green. The implementer and the local test
run stay separate from the review. The review runs in its own HerDr terminal.
Invoke the harness as `deepseek` on `PATH`. An absolute install path is
operator configuration, not part of this contract. If Herdr visibility is
unavailable, the review does not start and delivery stops.

## GitHub

Read branch protection and the required checks GitHub actually returns. A
rule or check that GitHub enforces still blocks a normal merge. Only when
Actions finishes with no job steps because billing is blocked does the
receipt stand in for that CI result. The receipt does not authorize spend, a
force-push, or an admin merge.
