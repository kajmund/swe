# SWE runtime

**Status:** Rust kernel and Python host adapter implemented for SWD 1.0.

## Ownership

The Rust crate `swe-native` owns deterministic machine semantics: scheduling,
branch paths, invocation ids, routing, barriers, expression evaluation, events,
information-need contracts, operation gates, and journal recovery decisions.
It emits effects but performs no I/O.

The Python package `swe-engine` owns authoring and host integration: JSON Schema
validation, graph checks, YAML loading, async task/provider dispatch, deadlines,
cancellation, injected prompts, cache/run-store protocols, and projection to the
native module. The host owns the concrete handlers and adapters.

## Execution

1. `compile_definition` validates registries and the SWD graph and returns an
   immutable canonical document plus its SHA-256 hash.
2. `start_run` creates a machine with a stable run id and input.
3. The host repeatedly asks the kernel for effects, dispatches task/provider
   invocations, and returns typed completions.
4. The kernel records events and routes until an `end` node or a typed failure.

Fork/map completion order cannot change the owning join's result. Invocation ids
include run, node, branch path, loop epoch, and serial components so replay can
match persisted results exactly.

## Persistence and recovery

`swe.run_store.RunStore` is the Python durable-run protocol; applications supply
its implementation. The Rust journal is append-only and records attempts,
reservations, confirmed results, settlement, release, abandonment, and pending
reconciliation. Recovery is selected by delivery class:

- pure work may be recomputed;
- an idempotent external effect may be retried with its delivery key;
- an unknown non-idempotent external effect fails closed;
- a confirmed result is replayed without re-execution.

The frozen journal header/events are in `contracts/`. `swe-budget` separately
replays its ledger and deduplicates settlement by charge id.

## Failures and limits

Unknown handlers/providers/choices, malformed completions, missing prompt data,
invalid bindings, deadlines, and host exceptions become typed failures. Limits
bound steps, graph depth, map concurrency, and wall-clock runtime. Cancellation
emits cancellation effects for outstanding host work.

## Non-goals

The runtime is not a broker, database, distributed scheduler, provider SDK, or
prompt catalog. It makes no exactly-once claim for unknown external side effects;
that guarantee requires an idempotent external system or fail-stop recovery.
