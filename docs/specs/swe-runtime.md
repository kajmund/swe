# SWE runtime (through phase 4)

**Status:** In-memory Rust/PyO3 execution kernel, durable OverGraph run store
(phase 3) and provider decisions with an exact Jev cache (phase 4)  
**Not yet:** research cutover (phase 5)

## Public API

Python compiles with the existing `compile_definition(...)` / `compile_swd_text(...)`.
The adapter `app.swe.runtime.run_workflow` sends the frozen document and its
SHA-256 to `swe_native.Machine` and runs registered `async` handlers and decision
providers.

```python
run_workflow(compiled, handlers, input_data, *, resources=None, run_id=None, clock=None, concurrency=32)
WorkflowRunner(compiled, handlers, *, resources=None, run_store=None, clock=None, concurrency=32)
resume_run(compiled, handlers, *, resources, run_store, run_id, clock=None, concurrency=32)
WorkflowResources(providers={}, prompts=None, cache=None, cancel_event=None)
```

```text
compile_definition → Machine.start(document, sha256, run_id, input, now_ms)
                 ← InvokeHandler / InvokeProvider effects
Python asyncio tasks (outside the Rust lock / GIL)
                 → Machine.complete / fail / cancel / tick
                 ← more effects or a terminal status
```

`run_workflow` is the unpersisted path. `WorkflowRunner(..., run_store=...)`
persists every transition atomically and `resume` continues a crashed run
(see [swe-overgraph.md](swe-overgraph.md)). `WorkflowResources` carries the
provider registry, the active prompt map, the decision cache and the cancel
event; a missing provider, prompt or cache fails preflight instead of falling
back.

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
| `decision` (`provider`) | Render the active prompt, invoke the named provider, route on the chosen value. Choices are declared in the node; an undeclared choice fails the run |
| `fork` | Spawn named branches; results stay per branch |
| `join` (`mode=all`, `on_failure=fail`) | Wait for every branch; `outputs` use `$.branches.<name>.result` |
| `map` | Fan-out over an array; wait for all children even without a join |
| `end` | Terminal `completed` / `incomplete` / `failed` / `cancelled` |

A branch without `next` completes at its owning join. Nested forks finish their
own join before the outer branch is done. `map` with `next` and no join is a
barrier, not fire-and-forget.

## Reserved (preflight reject)

`wait`, `subworkflow`, and any `retry` block. `cache_policy: exact` is supported
on provider decisions only (phase 4); on task nodes it stays rejected. An
accepted definition never silently ignores declared features.

`output_schema` is validated in Python with jsonschema before `complete`.

Provider decisions and the L1/L2 exact cache are documented in
[swe-provider.md](swe-provider.md).

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
`BranchSpawned`, `JoinSatisfied`, `DecisionTaken`, `ProviderInvoked`,
`ProviderCompleted`, `RunCompleted` / `RunFailed` / `RunCancelled`.

Each event has `run_id`, a run-local monotonic `seq`, definition SHA, optional
node instance id, and lineage (`branch_path`, fork/map instance, `loop_epoch`).
The in-memory sink is the source for the durable OverGraph event append in
`swe-overgraph.md`; the sink itself makes no durability claim.

## Isolation

The machine is per run. Two concurrent runs of the same definition do not share
branch ids or outputs. The crate and `app.swe.runtime` stay free of research,
Jev, OverGraph, and expert routing imports: the run store, providers and cache
are injected as adapters (`app.services.overgraph.workflows`,
`app.swe.providers`, `app.services.overgraph.jev_cache`).

## Research wave loop

The research cutover runs the wave loop of `execute_attempt_research` on SWE
through `backend/app/services/research/swe_bridge.py`, wired from
`execution._run_research_loop`. The compiled definition is:

```
run_wave (task) -> needs (map, child execute_need) -> finish_wave (task) -> route (decision)
route true  -> done (end, completed)
route false -> run_wave (next wave)
```

The handlers call the research domain's public wave primitives
(`pending_need_pairs`, `runtime_plan`, `execute_one_need`,
`complete_timed_out_need`, `quality_descriptors`, `finish_research_wave`); the
engine itself never imports research.

- **Concurrency:** the map schedules one child per pending need (no wave-level
  cap, mirroring the phase-2 loop's one worker per pending need).
  `research_need_concurrency` bounds provider retrieval inside the child handler
  through the same semaphore the old loop used; `finish_wave` is the barrier.
  The wider-fanout measurement variant only raises that retrieval bound.
- **Lease/cancel:** the research lease-lost event is the runner
  `cancel_event` (`WorkflowResources`). A lost lease cancels open handlers and a
  terminal `cancelled` run re-raises `asyncio.CancelledError`, which the existing
  research write fence/worker path already expects. Committed transitions are
  unchanged.
- **Deadlines:** `limits.timeout_seconds` is a run backstop only. The per-need
  deadline remains `research_need_timeout_seconds`, enforced in the
  `research.execute_need` handler exactly as before.
- **Run store:** the OverGraph run store is not production-wired in the cutover;
  the bridge accepts one for tests and measurement only (`swe-overgraph.md`).

The old in-process `loop_control.run_research_loop` and its fan-out shell were
deleted in the cutover's delete-PR. `execution._run_research_loop` is the only
loop seam and it always calls the bridge; there is no fallback path.

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
