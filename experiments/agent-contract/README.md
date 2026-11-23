# Agent contract join

Owner: t1bm (`experiment/agent-microtask-contract`, PR 13).

t1bp owns `experiment/microtask-swarm` and does not edit this directory.

## Dispatch gap

SWE does not have event-ready scheduling. `compile_microtasks` turns a DAG
into SWD map layers, and a later layer waits until every task in the previous
layer has finished (`docs/specs/swe-microtasks.md`). The dispatch boundary is
`WorkflowRunner`, which needs `swe_native`. This package does not run the plan
and does not contain another scheduler. `require_swe_dispatch` still raises
when the wheel is installed.

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

## Budget

PR #11 is open. `RuntimeBudget` is not attached. t1bq owns that wait.
