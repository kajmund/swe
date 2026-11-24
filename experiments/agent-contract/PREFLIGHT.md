# Offline integration preflight

PR 13, branch `experiment/agent-microtask-contract`. The run uses the checkout's
`WorkflowRunner` and a `swe_native` wheel built from `crates/swe-native` in the
same tree. `python/` and `crates/` match `origin/main` after PR 11
(`252620be31a4bf0d2c1f451d3ce2137a2b158fc1`). Each run attaches
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

The invocation rows from that run are in
`experiments/agent-contract/preflight-trace.json`. Local wheel paths are omitted.

`require_swe_dispatch` raises `DispatchGap` while `swe_native` imports. The
package does not execute the plan.

The layer witness compiles `slow` and `fast` into one map layer and
`after_fast`, which depends only on `fast`, into the next layer. With
concurrency 2, the starts recorded while `slow` was blocked were `slow` and
`fast`. `after_fast` started after `slow` was released. Final order: `slow`,
`fast`, `after_fast`.

The agent plan compiles to eight map layers and definition
`dd86b2cc97d6d292af886d3bc0f929a0722b09768016dcbb8c0e6f29c054e591`. Alpha and
beta share that definition and the same task ids. Generation payloads use
`alpha` / `alpha-model` or `beta` / `beta-model`. Decision payloads stay
`system-one` / `laya`. Journal nodes, and only these, are the five model
invocations: `relevance.generate`, `extraction.generate`, `entailment.generate`,
`atomicity.generate`, `operation.generate`. All five validator rows have
`model_called` false.

Each `InvocationRecord` is one handler call. `entailment.generate` and
`atomicity.generate` share `node_id` `microtask_4` because they are children of
one layer map. The same pairing is `microtask_5` for their validators.

A crash after the persisted invocation `a` resumes through `WorkflowRunner` to
`completed`. The resumed execution is `a`, `b`, and `a` runs once.

## Gap

`docs/specs/swe-microtasks.md` states both limits, and this run reproduces them:

- A later layer waits until every task in the preceding layer has finished.
  `after_fast` waited for `slow` even though its own dependency was `fast`.
- The handler receives `task_id`, `payload`, and `depends_on`. Dependency
  results are in the completed run output and are not injected into the next
  handler. Every contract row has `upstream_output_present` false, so a
  validator invocation cannot read the generation output through this dispatch.

Config selects the provider and model inside the static payload. It does not
change the compiled definition. The semantic check of a generation result needs
a value channel the map child input does not have. `VALUE_CHANNEL.md`
proposes that bind as a host call. It does not change SWD. The contract run
reserves and settles one milli-unit per handler invocation through the real
`WorkflowRunner`.
