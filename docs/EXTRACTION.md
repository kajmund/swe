# Repository extraction provenance

This repository was extracted from
[`kajmund/socialism`](https://github.com/kajmund/socialism) after pull request
459 was squash-merged.

- Source commit: `2f2f9ba2ee7e88f2c4aded61da15e8108d66aa46`
- Extraction date: 2026-11-01
- History tool: `git-filter-repo`
- Relevant commits retained by the path filter: 13

The filter retained the three SWE crates, Python package and tests, frozen
contracts, contract/mutation gates, generic specs, and the signed contract,
licensing, and provenance decisions. The standalone-repository commit then
added root packaging, CI, notices, and neutral fixtures.

The following did not move: application/backend code, product adapters,
frontends, document tooling, experiments, model/provider implementations,
customer configuration, legal-domain fixtures, and benchmark artifacts.

Before standalone edits, all 118 retained blobs were compared by Git object id
with the merged source tree: 118 matched and 0 differed. Untracked build caches
in the source checkout were outside that comparison.

The detailed boundary and AI-agent authorship record is preserved in
[`docs/swe-standalone/06-extraction-plan.md`](swe-standalone/06-extraction-plan.md),
the frozen contract sign-off in
[`docs/swe-standalone/09-contract-decision.md`](swe-standalone/09-contract-decision.md),
and the license decision in
[`docs/swe-standalone/15-licensing.md`](swe-standalone/15-licensing.md).
