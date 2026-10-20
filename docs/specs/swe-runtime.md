# SWE runtime (phase 2)

**Status:** In-memory Rust/PyO3 execution kernel  
**Not in this PR:** OverGraph persistence, Jev cache, research cutover

## Public API

Python compiles with the existing `compile_definition(...)` / `compile_swd_text(...)`.
The adapter `app.swe.runtime.run_workflow` sends the frozen document and its
SHA-256 to `swe_native.Machine` and runs registered `async` handlers.

```text
compile_definition → Machine.start(document, sha256, run_id, input, now_ms)
                 ← InvokeHandler effects
Python asyncio tasks (outside the Rust lock / GIL)
                 → Machine.complete / fail / cancel / tick
                 ← more effects or a terminal status
```

Rust never calls Python. Python never waits on a handler while the machine lock
is held. `py.allow_threads` wraps every Rust mutation.

The driver waits with `asyncio.wait(..., timeout=remaining_deadline)` and the
optional cancel event. When the deadline elapses it calls `Machine.tick(now_ms)`,
which fails the run and emits `CancelInvocation` for open handlers. A
`try/finally` cancels owned tasks (handlers and the cancel watcher) and
`await`s `asyncio.gather(..., return_exceptions=True)`.

Handlers that swallow `CancelledError` are not force-stopped. Terminal status
stays protected; a late `complete`/`fail` is an idempotent no-op or typed
conflict and does not route again.

Low-level module: `swe_native` (`sha256_hex`, `canonical_json_bytes`,
`evaluate_expression`, `Machine`).

## Supported nodes

| Node | Behaviour |
| --- | --- |
| `task` | Bind `input`, invoke a Python handler, store `$.<node>.result` |
| `decision` (`expression`) | Evaluate in Rust. Routes are exactly `true` / `false` |
| `fork` | Spawn named branches; results stay per branch |
| `join` (`mode=all`, `on_failure=fail`) | Wait for every branch; `outputs` use `$.branches.<name>.result` |
| `map` | Fan-out over an array; wait for all children even without a join |
| `end` | Terminal `completed` / `incomplete` / `failed` / `cancelled` |

A branch without `next` completes at its owning join. Nested forks finish their
own join before the outer branch is done. `map` with `next` and no join is a
barrier, not fire-and-forget.

## Reserved (preflight reject)

`decision.evaluator=provider`, `wait`, `subworkflow`, `cache_policy: exact`,
and any `retry` block. An accepted definition never silently ignores declared
features.

`output_schema` is validated in Python with jsonschema before `complete`.

## Limits

- **step:** each scheduled node instance (task, decision, fork, join, map, end).
- **depth:** nested fork/map frames. Root is 0. A new frame is `parent + 1`.
- **timeout:** `started_ms + timeout_seconds * 1000`, compared to injected
  monotonic `now_ms` on every mutation **and** on `tick`. Python uses the
  remaining deadline as the `asyncio.wait` timeout so a hanging handler cannot
  outlive `timeout_seconds`.

## State and paths

Results live on the current scope, not a global overwrite field. Parallel map
children do not clobber each other. After a join, `$.<join_id>.<output>` is
visible to later bindings. Run metadata is `$.run.steps_used`,
`$.run.max_steps`, `$.run.depth`. Workflow input is both `$.input` and
top-level keys.

## Completions

`invocation_id` names a node instance (`run`, node, branch path, loop epoch,
serial). `loop_epoch` starts at 0 and increments on the current scope when a
node id is scheduled again in that scope (decision/fork re-entry). Same
terminal payload: idempotent no-op. Different payload: typed conflict. Unknown
id: typed error. After cancel/fail, late handler returns do not route again.

Task failure with `join mode=all / on_failure=fail` fails the run immediately
and emits `CancelInvocation` for siblings.

## Events

In-memory only, append-only `EventSink`:

`RunStarted`, `NodeScheduled`, `NodeStarted`, `NodeCompleted`, `NodeFailed`,
`BranchSpawned`, `JoinSatisfied`, `DecisionTaken`, `RunCompleted` /
`RunFailed` / `RunCancelled`.

Each event has `run_id`, a run-local monotonic `seq`, definition SHA, optional
node instance id, and lineage (`branch_path`, fork/map instance, `loop_epoch`).
This is the seam for OverGraph append in phase 3. No durability is claimed.

## Isolation

The machine is per run. Two concurrent runs of the same definition do not share
branch ids or outputs. Research, Jev, OverGraph, and expert routing stay
outside this crate.

## Map memory (phase 2)

`map.items` is stored in full, the work queue is a `Vec` (`remove(0)` is O(n)),
and `events()` materializes the whole in-memory sink. Measured on this branch
with `uv run python scripts/benchmark_swe_runtime.py --steps 10000 --fanout 10000`:

| Case | Time | Events | max RSS |
| --- | --- | --- | --- |
| 10 000 decision steps | 319 ms | 30 004 | (shared process) |
| 1 000 map items | 1.95 s | 4 005 | 131 MiB |
| 10 000 map items | 233 s | 40 005 | 220 MiB |

Time is superlinear. The work queue stays small under `concurrency`, so
`Vec.remove(0)` is not the dominant cost. The map scope (items + children) is
cloned while spawning, and every event is kept. That is accepted for phase 2;
do not grow this toward a webcrawler fan-out without a dedicated pass.
