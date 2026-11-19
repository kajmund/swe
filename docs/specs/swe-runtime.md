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
bound steps, graph depth, map concurrency, and wall-clock runtime.
Host-owned `WorkflowResources.action_timeouts` bound individual task and
provider invocations by SWD node id without changing the SWD document.
Action expiry cancels that asyncio task and is terminal for the invocation
(`kind=timeout`, `path=nodes.{node_id}`); it does not call `Machine.tick`
(tick remains workflow-wide). Host `cancel_event` is terminal `cancelled`,
not timeout. A handler that swallows `CancelledError` or returns after the
deadline does not complete or route; the late result is discarded and does
not write a second journal row. Expiry uses the handler's completion time,
so a delayed `RunStore.record` cannot convert a timely success into a
timeout.

Each handler and provider call is bound to `InvocationHandle` via
`current_invocation()`. The handle's `deadline_ms` is
`min(action deadline, workflow deadline)`. On action timeout, workflow tick,
or host `cancel_event`, SWE sets `handle.cancel` and `reason` (`timeout` vs
`cancel`) and only then `task.cancel()`, so the Event is visible before
`CancelledError`. Handler signatures stay `async def handler(payload)`.

After cancel, SWE persists terminal status without waiting for physical I/O,
waits a short grace period, and detaches unfinished work into a bounded
abandoned-work registry (cap defaults to runner `concurrency`). The registry
is observable (`abandoned_count`, invocation ids, age). Exceeding the cap
fails closed on new dispatch (`kind=limit`); the success path never waits
for abandoned I/O. `WorkflowRunner.run` / `resume` `finally` may cancel
abandoned tasks but must not hang: a tiny timeout, then detach. A host
`cancel_event` during that grace still uses the bounded detach path.

SWE cannot guarantee that a provider received cancel, that a provider
stopped billing, or that an OS thread or socket actually closed if the host
ignores `InvocationHandle.cancel`. `task.cancel()` is cooperative asyncio
only; blocking I/O (`time.sleep`, blocking sockets, threads that ignore
cancel) is not aborted by SWE. Retry stays unsupported.

Budget settlement is not on the Python runtime path. `swe-budget` is a
separate ledger (reference host and ActionOffer). Timeout and cancel must
not call `complete` / `fail` twice on the machine; hosts that settle from
those completions therefore cannot double-settle. SWE does not invent a
second ledger. A timed-out or cancelled invocation that never completed is
not replayed as success; resume of a persisted timeout/cancel stays
failed/cancelled and does not re-dispatch.

## Non-goals

The runtime is not a broker, database, distributed scheduler, provider SDK, or
prompt catalog. It makes no exactly-once claim for unknown external side effects;
that guarantee requires an idempotent external system or fail-stop recovery.
Cancel and timeout are at-most-once for the machine and the journal, not for
unknown provider or OS effects.
