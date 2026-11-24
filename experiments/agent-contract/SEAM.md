# PR 13 integration seam

Owner: t1bm, branch `experiment/agent-microtask-contract`.

t1bq owns `experiment/barrier-free-scheduler` from `origin/main`
(`eff1c7e8943dd64827fb4916719ced5515bf3db5`). That branch is the ready-DAG
experiment. This package does not import it, does not copy its queue, and does
not attach `RuntimeBudget`. Budget integration waits until PR 11 is merged.

## What is stable now

`plan_invocations` emits ten static items, `kind.generate` then
`kind.validate`. A validator depends on its own generate. A generate depends
on the validate items of the kinds it follows. Provider and model are payload
fields. Alpha and beta keep the same ids and dependencies.

`compile_microtasks` turns that DAG into one SWD map per topological layer.
The measured handler input is `task_id`, `payload`, and `depends_on`
(`preflight-trace.json`). Generation output is not in the next handler input.

`require_swe_dispatch` raises `DispatchGap` when the wheel is present. This
package has no second scheduler.

## Future ready map

The proposal on t1bq's branch, recorded in
`docs/analysis/2026-11-24-barrier-free-scheduler.md` section 9, adds these
fields to one map:

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

The witness from the offline preflight is the disagreement to preserve:

- `after_fast` depends only on `fast`.
- With `fast` complete and `slow` still open, `ready_ids` lists `after_fast`.
- `layer_is_complete(("slow", "fast"), {"fast"})` stays false, which is the
  current map barrier.

A later scheduler satisfies this seam when that predicate holds for the same
items and each admitted task still has its own invocation id. Replacing the
predicate with a layer wait would fail `tests/test_seam.py`.

## What the ready map does not close

`item_depends_on` decides admission. The handler input measured here still has
no dependency result, so a validator cannot read the generation output through
that field. The value channel stays `absent` until a separate contract injects
it. Production budget stays on PR 11.
