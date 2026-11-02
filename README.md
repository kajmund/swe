# SWE

SWE is a deterministic workflow engine for SWD 1.0 documents. The repository
contains a Rust execution kernel, an installable Python host package, a generic
budget ledger, frozen wire contracts, and an offline reference application.

The engine owns workflow semantics: canonical definitions, task/provider
effects, expression decisions, fork/join/map scheduling, information needs,
evidence status, operation gates, and append-only recovery journals. Storage,
providers, prompts, networking, and domain policy belong to the embedding host.

## Repository layout

| Path | Purpose |
| --- | --- |
| `crates/swe-native` | Rust kernel and optional `swe_native` PyO3 module |
| `crates/swe-budget` | Reserve/settle/release/replay budget ledger |
| `crates/swe-reference` | Fully offline supplier-selection reference app |
| `python/swe` | Python compiler, host runtime, and adapter protocols |
| `contracts` | Frozen SWD and journal v1 fixtures |
| `scripts` | Contract, mutation, and licensing gates |

## Development

Prerequisites are Rust 1.83, Python 3.12+, and
[`uv`](https://docs.astral.sh/uv/). The lockfiles are committed.

```sh
make test
make contract-gate
make mutation-gate
make package
```

`make test` runs Rust formatting, Clippy, the complete workspace, the reference
application, and the Python suite against a locally built native wheel. The
mutation gate uses an isolated Git worktree and never mutates the checkout.

## Contracts and compatibility

SWD, journal, and public Rust contract changes are fail-closed. A change to a
frozen surface requires its owning version to change and a documented contract
decision. See [`contracts/README.md`](contracts/README.md) and
[`docs/swe-standalone/09-contract-decision.md`](docs/swe-standalone/09-contract-decision.md).

The SWD v1 schema retains its original `$id` URI for wire compatibility; the
URI is an identifier, not a network location or a runtime dependency.

## Microtask batches

`swe.microtasks` compiles a static dependency DAG into ordinary SWD 1.0 `map`
stages. It reuses the existing bounded scheduler, cancellation, events, and
`RunStore` replay path; it does not introduce another workflow format or wire
contract.

```python
from swe.microtasks import Microtask, compile_microtasks

batch = compile_microtasks(
    [
        Microtask("fetch"),
        Microtask("validate", depends_on=("fetch",)),
    ],
    handler="job.execute",
    concurrency=16,
)
result = await batch.run(handler)
```

Run the 10,000-operation synthetic gate with:

```sh
uv run swe-microtasks-benchmark --tasks 10000 --concurrency 64
```

## Releases

The workspace version is `0.1.0`. `swe-native` and `swe-budget` are prepared
for crates.io, and the Python distribution is named `swe-engine` while its
import package remains `swe`. No package or tag is published automatically by
this repository setup.

## License and provenance

Copyright (c) 2026 Erik Fremred.

Licensed under either Apache-2.0 or MIT, at your option. Third-party licenses
and attributions are in [`THIRD-PARTY-NOTICES`](THIRD-PARTY-NOTICES).
The extraction and AI-agent contribution provenance is recorded in
[`docs/swe-standalone/06-extraction-plan.md`](docs/swe-standalone/06-extraction-plan.md).
