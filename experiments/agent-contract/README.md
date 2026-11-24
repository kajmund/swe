# Agent contract join

Owner: t1bm (`experiment/agent-microtask-contract`, PR 13).

t1bp owns `experiment/microtask-swarm` and does not edit this directory.

## Dispatch gap

`compile_microtasks(..., schedule="ready")` emits one SWD 1.1 map. The
ordered default still waits for a whole layer (`docs/specs/swe-microtasks.md`).
The dispatch boundary is `WorkflowRunner`, which needs `swe_native`. This
package does not contain another scheduler. `dispatch_gap` records
`package_dispatcher` false and `fail_closed` true, and `require_swe_dispatch`
still raises when the wheel is installed. `swe_ready` probes the installed
`compile_microtasks` for `schedule="ready"`. The preflight is the caller that
runs the map.

`experiments/agent-contract/PREFLIGHT.md` builds that wheel from this checkout
and records the machine trace. Dependency results stay in the completed run
output; the handler input is `task_id`, `payload`, and `depends_on`.

Cancellation and replay belong to that same machine: `WorkflowResources.cancel_event`
and `RunStore` replay of `InvocationRecord`. The offline test drives one
invocation at a time only to show those rules. It is not a production runner.

## Invocations

Each semantic kind is two invocations, `kind.generate` and `kind.validate`.
The provider and model come from the generation and decision configurations.
A validator does not call the model again.

## Ready-DAG seam

`SEAM.md` records the boundary with the merged ready map. `ready_ids` names
eligibility and does not start work. The fields `schedule`, `item_id`, and
`item_depends_on` admit a task and do not deliver the dependency result.

## Budget

PR #11 is merged. A run attaches `RuntimeBudget` through `WorkflowResources`
on `WorkflowRunner`. The fixed one-milli policy in `budget_run.py` is an
injected stand-in. t1bp owns the production tariff in `experiment/microtask-swarm`.

## Value channel

`VALUE_CHANNEL.md` proposes a typed `GenerationOutput` bound into
`ValidatorInput` by an explicit host call. The SWD schema is unchanged, and
the map handler still does not receive that output.
