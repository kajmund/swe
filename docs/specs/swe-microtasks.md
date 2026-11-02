# SWE P2 microtask runtime

**Status:** implemented as a Python authoring facade over SWD 1.0.

## Inventory before design

SWE already owns every execution primitive needed for a microtask batch:

| Concern | Existing mechanism | P2 decision |
| --- | --- | --- |
| Parallel branches | `fork` plus the owning `join` | Keep as-is; microtask batches do not need named branches |
| Fan-out | `map` over a JSON array | Lower each topological dependency layer to one `map` |
| Concurrency | Rust `start_map` / `spawn_map_children` limits running children to `map.concurrency` | Reuse directly; do not create a second scheduler |
| Global host pressure | `WorkflowRunner.concurrency` and `asyncio.Semaphore` | Set both bounds to the requested microtask concurrency |
| Dependencies | SWD graph edges are stage-level dependencies | Compile a static task DAG into deterministic topological layers |
| Interruptions | Existing `cancel_event`, deadline handling, and `CancelInvocation` effects | Reuse unchanged |
| Budget | `swe-budget::Ledger` owns allocation, reserve, settle, release, and replay | Keep the existing ledger as the only currency implementation |
| Recovery | `RunStore`, persisted `InvocationRecord`s, and deterministic replay | Persist and replay each microtask invocation exactly once |
| Observability | Rust events plus `InvocationRecord` | Expose host-level latency, concurrency, and throughput measurements |

The frozen SWD 1.0 and journal contracts are unchanged. There is no new node
kind, scheduler, dependency format in the workspace document, or execution
journal.

## API

`swe.microtasks.compile_microtasks` accepts `Microtask` objects with stable
`task_id`, JSON `payload`, and static `depends_on` ids. The compiler:

1. validates unique ids, known dependencies, and absence of cycles;
2. groups ready tasks into deterministic topological layers;
3. emits one ordinary `map` node per layer, chained in dependency order;
4. emits one task child per map layer using the caller's handler;
5. creates run input containing the per-layer task arrays.

The handler receives:

```json
{
  "task_id": "validate",
  "payload": {"document": "doc-1"},
  "depends_on": ["fetch"]
}
```

Tasks in one layer may run concurrently. A later layer cannot start until every
task in the preceding layer completes. Failure and cancellation use the normal
SWE terminal semantics.

The minimum P2 scope is static execution ordering. Dependency results are
available in the completed workflow output in deterministic layer/input order;
they are not injected automatically into downstream handlers. Dynamic graph
mutation, retries, and cross-run scheduling remain outside P2.

## Backpressure and interruption

The Rust map scheduler starts at most `map.concurrency` child scopes. It starts
the next child only after an active child completes, so 10,000 tasks do not
become 10,000 simultaneously live handler tasks. The Python dispatcher applies
the same concurrency cap as a second boundary.

`WorkflowResources(cancel_event=...)` stops the active window and cancels
outstanding handler tasks through the existing runtime path. Deadlines and
handler failures remain terminal and do not allow late results to reroute.

## Budget and recovery

The microtask facade does not mint or reinterpret Sweycoins. A host that needs
per-task cost accounting continues to use `swe-budget::Ledger` with stable
invocation or host-operation keys. The batch result and event sequence remain
the source for linking execution to those cost records.

With a `RunStore`, each handler outcome is persisted as an `InvocationRecord`.
Resume starts a fresh deterministic machine, replays those outcomes, and
continues from the same effect boundary. A task whose outcome was persisted is
not called again; tasks without a confirmed record remain eligible under the
normal at-most-once and fail-closed recovery rules.

## Benchmark and quality gates

Run the offline synthetic gate:

```sh
uv run swe-microtasks-benchmark \
  --tasks 10000 \
  --concurrency 64 \
  --min-throughput 100 \
  --output /tmp/swe-microtasks.json
```

The benchmark performs no model or network calls. It checks:

- exactly 10,000 unique results;
- completed terminal status;
- peak in-flight handlers no greater than the configured concurrency;
- a conservative throughput floor of 100 tasks/s;
- p50 and p95 handler latency plus end-to-end throughput reporting.

The committed baseline is generated with the same command and records the host
environment alongside its measurements. Throughput is diagnostic across hosts;
correctness and bounded concurrency are the hard gates.

The initial baseline is
[`benchmarks/microtasks/independent-10000.json`](../../benchmarks/microtasks/independent-10000.json):
10,000/10,000 unique results, peak concurrency 64, 4,055 tasks/s, p50 0.034 ms
and p95 0.049 ms on the recorded macOS host.
