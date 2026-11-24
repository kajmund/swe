# Offline integration preflight

PR 13, branch `experiment/agent-microtask-contract`. The run uses the checkout's
`WorkflowRunner` and a `swe_native` wheel built from `crates/swe-native` in the
same tree. `origin/main` is `85fe8a7`, which contains the ready-map merge
`f1ae2b8`. `python/` and `crates/` are unchanged since that merge. Each batch
uses `schedule="ready"`. Each run attaches
`RuntimeBudget` through `WorkflowResources` with an injected one-milli policy.
The production tariff stays with t1bp. The run makes no provider call.

## Reproduction

From this worktree, with Python 3.12 and `cargo` on `PATH`:

```bash
python3.12 -m venv /tmp/pr13-preflight/venv
/tmp/pr13-preflight/venv/bin/pip install maturin pydantic jsonschema pyyaml pytest
/tmp/pr13-preflight/venv/bin/maturin build \
  --manifest-path crates/swe-budget-py/Cargo.toml \
  --interpreter /tmp/pr13-preflight/venv/bin/python \
  --out /tmp/pr13-preflight/dist
/tmp/pr13-preflight/venv/bin/maturin build \
  --manifest-path crates/swe-native/Cargo.toml \
  --interpreter /tmp/pr13-preflight/venv/bin/python \
  --out /tmp/pr13-preflight/dist
/tmp/pr13-preflight/venv/bin/pip install /tmp/pr13-preflight/dist/*.whl
PYTHONPATH=experiments/agent-contract \
  /tmp/pr13-preflight/venv/bin/python -m agent_contract.preflight
PYTHONPATH=experiments/agent-contract \
  /tmp/pr13-preflight/venv/bin/python -m pytest experiments/agent-contract/tests/test_preflight.py
```

The wheels built for this run are
`swe_budget_native-0.1.0-cp312-cp312-macosx_11_0_arm64.whl` and
`swe_native-0.1.0-cp312-cp312-macosx_11_0_arm64.whl`. Leave the system
interpreter unchanged.

## Observed trace

`experiments/agent-contract/preflight-trace.json` is a scrubbed copy of
`python -m agent_contract.preflight`, not the raw stdout. The published file
drops `checkout.head`, `wheel.file`, and every `invocation_id`. `checkout.head`
would name the commit that contains this file. `wheel.file` is a machine path.
`invocation_id` is a new value on every run. `paid_calls` is 0.

The reproducible sources for that run are `origin/main`
`85fe8a79e7a6a9e9c9aca8665ac88bb45686b6f6`, which contains the ready-map merge
`f1ae2b8714fc6b88f66a00b47a1baa0469af718f`, and PR 12
`f17027bb173ad34510ba2596087d3700884443c6`. `python/` and `crates/` match
`origin/main`. Re-run the command and drop the same three fields to compare
the observation.

`require_swe_dispatch` raises `DispatchGap` while `swe_native` imports. The
package records `package_dispatcher` false and `fail_closed` true. It does not
execute the plan. `swe_ready` is the installed compiler probe: this
interpreter's `compile_microtasks(..., schedule="ready")` accepts that
schedule. `swe_ordered_default` stays `layer_map` for the ordered
compiler. The preflight script is the caller. It compiles each batch with
`schedule="ready"`
and runs that one SWD 1.1 map on `WorkflowRunner`. The map node is
`microtasks`. Every persisted invocation in this trace has `node_id`
`microtask`.

The ready witness uses `slow`, `fast`, and `after_fast`. `after_fast` depends
only on `fast`. The trace still lists the topological groups `["slow", "fast"]`
and `["after_fast"]`. Those groups are the DAG shape. With concurrency 2, the
starts recorded while `slow` was blocked were `slow`, `fast`, and `after_fast`.
`after_fast_started_before_slow_released` is true, and
`slow_still_blocked_when_after_fast_started` is true. Final order: `slow`,
`fast`, `after_fast`. The witness budget is attached on account
`agent-contract` with policy `injected-fixed-milli`: spent 3 milli, reserved
0, 7 events.

The agent plan's topological list still has eight groups. Alpha and beta share
definition `52dc0023e74135a02997107b4dc305a387d6b7d055ba18728a660da47cf451d8`
and the same task ids. `map_nodes` is `["microtasks"]` and `schedule` is
`ready`. Generation payloads use `alpha` / `alpha-model` or `beta` /
`beta-model`. Decision payloads stay `system-one` / `laya`. Journal nodes, and
only these, are the five model invocations: `relevance.generate`,
`extraction.generate`, `entailment.generate`, `atomicity.generate`,
`operation.generate`. All five validator rows have `model_called` false. The
contract budget spends 10 milli, reserved 0, 21 events.

`_CrashOnce` raises inside the same process after the first persisted
invocation. Resume calls `WorkflowRunner.resume` on that same `RunStore` and
the same `RuntimeBudget` object. `resume_scope` is `in-process` and
`fresh_ledger` is false. This run does not build a new
`NativeBudgetLedger` from saved events after a process exit. `schedule` is
`ready`. `executed_before_resume` is `a`. `executed_after_resume` is `a`, `b`.
`replayed_task_executed_again` is false, so `a` runs once. The replay budget
spends 2 milli, reserved 0, 5 events.

## Historical ordered baseline

Commit `fb97b6a` recorded the ordered compiler, before `schedule="ready"`.
That trace compiled the eight topological groups into eight map nodes,
`microtasks_0` through `microtasks_7`, definition
`dd86b2cc97d6d292af886d3bc0f929a0722b09768016dcbb8c0e6f29c054e591`.
`entailment.generate` and `atomicity.generate` shared `node_id` `microtask_4`,
and their validators shared `microtask_5`. The witness starts while `slow` was
blocked were `slow` and `fast`. `after_fast` started after `slow` was released.
`docs/specs/swe-microtasks.md` describes that ordered layer compiler. The
current trace above is the ready-map run.

## Gap

The ready map admits `after_fast` while `slow` is still blocked. The
value-channel limit remains, and this run reproduces it:

- The handler receives `task_id`, `payload`, and `depends_on`. Dependency
  results stay in the completed run output. Every contract row has
  `upstream_output_present` false, so a validator invocation cannot read the
  generation output through this dispatch. `item_depends_on` admits a task and
  carries no generation output.

Config selects the provider and model inside the static payload. Alpha and beta
share one compiled definition. The semantic check of a generation result needs
the separate host bind in `VALUE_CHANNEL.md`. That bind leaves SWD unchanged.
The contract run reserves and settles one milli-unit per handler invocation
through the real `WorkflowRunner`.
