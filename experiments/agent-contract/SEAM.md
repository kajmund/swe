# PR 13 integration seam

Owner: t1bm, branch `experiment/agent-microtask-contract`.

`origin/main` `f1ae2b8` merged the ready map. The preflight compiles
`schedule="ready"` and runs that one SWD 1.1 map on `WorkflowRunner`. This
package does not import `experiment/barrier-free-scheduler` and does not copy
its queue.

## What is stable now

`plan_invocations` emits ten static items, `kind.generate` then
`kind.validate`. A validator depends on its own generate. A generate depends
on the validate items of the kinds it follows. Provider and model are payload
fields. Alpha and beta keep the same ids and dependencies.

`compile_microtasks(..., schedule="ready")` turns that DAG into one SWD 1.1
map. The measured handler input is `task_id`, `payload`, and `depends_on`.
Generation output is not in the next handler input.

`require_swe_dispatch` raises `DispatchGap` when the wheel is present. This
package has no second scheduler.

## Ready map

The merged map, recorded in `docs/swe-standalone/18-ready-map-decision.md`,
uses these fields:

```json
{
  "type": "map",
  "items": "$.input.tasks",
  "schedule": "ready",
  "item_id": "$.item.task_id",
  "item_depends_on": "$.item.depends_on"
}
```

`agent_contract.seam` locks those field names. `ready_ids` is the migration
predicate: a task is eligible once every dependency id is complete, in input
order. It does not keep a queue, a clock, or a concurrency limit.

The ordered layer check stays false while the ready predicate is true:

- `after_fast` depends only on `fast`.
- With `fast` complete and `slow` still open, `ready_ids` lists `after_fast`.
- `layer_is_complete(("slow", "fast"), {"fast"})` stays false.

The preflight witness is the machine, not the predicate. `after_fast` starts
while `slow` is still blocked, and each start has its own invocation id.

## What the ready map does not close

`item_depends_on` decides admission. The handler input measured here still has
no dependency result, so a validator cannot read the generation output through
that field. `VALUE_CHANNEL.md` is the separate proposal: the host binds a
typed `GenerationOutput` into `ValidatorInput`. That bind is not an SWD change.

PR #11 is merged. Budget attaches per run through `WorkflowResources` and the
real `WorkflowRunner`. This package does not own the production tariff.
