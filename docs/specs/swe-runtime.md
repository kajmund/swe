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
                 → Machine.complete / fail / cancel
                 ← more effects or a terminal status
```

Rust never calls Python. Python never waits on a handler while the machine lock
is held. `py.allow_threads` wraps every Rust mutation.

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
  monotonic `now_ms` on every mutation.

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
