"""Offline preflight of PR 13 against the real SWE machine.

The wheel and ``WorkflowRunner`` come from this checkout. Each batch is
compiled with ``schedule="ready"`` on the merged SWD 1.1 map. Each run attaches
``RuntimeBudget`` through ``WorkflowResources``. The tariff is the injected
one-milli policy, not the microtask-swarm policy. Dependency admission does
not copy generation output into the handler. The script does not call a model
provider.
"""

from __future__ import annotations

import asyncio
import inspect
import subprocess
import sys
from collections import defaultdict
from collections.abc import Mapping, Sequence
from decimal import Decimal
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "python"))
sys.path.insert(0, str(ROOT / "experiments" / "hy4-research-policy"))
sys.path.insert(0, str(ROOT / "experiments" / "agent-contract"))

from agent_contract.join import (  # noqa: E402
    DispatchGap,
    dispatch_gap,
    plan_invocations,
    require_swe_dispatch,
    runtime_budget_status,
)
from hy4_research_policy.agent_adapter import (  # noqa: E402
    DECISION_PROFILE,
    AgentConfig,
    AgentHost,
    Completion,
    DecisionRequest,
    GenerationRequest,
    Usage,
)
from agent_contract.budget_run import budget_trace, open_budget, resources_for  # noqa: E402

SCHEDULE = "ready"
from swe.microtasks import Microtask, compile_microtasks  # noqa: E402
from swe.run_store import InvocationRecord, MemoryRunStore, RunRecord, RunStore  # noqa: E402
from swe.runtime import WorkflowRunner  # noqa: E402


class _Journal:
    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []

    def append(self, record: dict[str, Any]) -> None:
        self.records.append(record)


class _Allow:
    def reserve(self, key: str, amount: object) -> bool:
        return True


class _Generation:
    def __init__(self, name: str) -> None:
        self.name = name
        self.calls: list[str] = []

    def profile(self) -> dict[str, bool]:
        return {
            "supports_text_output": True,
            "supports_tools": False,
            "supports_json_schema_output": False,
        }

    def complete(self, instruction: str, payload: dict[str, str]) -> Completion:
        self.calls.append(instruction)
        if instruction == "relevance":
            output: dict[str, Any] = {"relevant": True}
        elif instruction == "extraction":
            output = {"quotes": ["quoted text."]}
        else:
            output = {"operation": "check_relevance"}
        return Completion(output=output, usage=Usage())


class _Decision:
    def __init__(self) -> None:
        self.name = "system-one"
        self.calls: list[str] = []

    def profile(self) -> dict[str, bool]:
        return dict(DECISION_PROFILE)

    def decide(self, question: str, options: list[str], state: dict[str, str]) -> Completion:
        self.calls.append(question)
        output = {"atomic": True} if question == "atomicity" else {"choice": "entailment"}
        return Completion(output=output, usage=Usage())


class _CrashOnce(RunStore):
    def __init__(self, *, crash_after: int) -> None:
        self.crash_after = crash_after
        self.crashed = False
        self.runs: dict[str, RunRecord] = {}
        self.invocations: dict[str, dict[str, InvocationRecord]] = defaultdict(dict)
        self.event_log: dict[str, list[dict[str, Any]]] = defaultdict(list)

    async def record(
        self,
        run: RunRecord,
        *,
        events: Sequence[Mapping[str, Any]] = (),
        invocation: InvocationRecord | None = None,
    ) -> None:
        self.runs[run.run_id] = run
        self.event_log[run.run_id].extend(dict(event) for event in events)
        if invocation is not None:
            self.invocations[run.run_id][invocation.invocation_id] = invocation
            if not self.crashed and len(self.invocations[run.run_id]) >= self.crash_after:
                self.crashed = True
                raise RuntimeError("simulated crash")

    async def load_run(self, run_id: str) -> RunRecord | None:
        return self.runs.get(run_id)

    async def replay_results(self, run_id: str) -> Mapping[str, InvocationRecord]:
        return dict(self.invocations[run_id])

    async def events(self, run_id: str) -> list[dict[str, Any]]:
        return list(self.event_log[run_id])


def _git(*args: str) -> str:
    return subprocess.check_output(("git", "-C", str(ROOT), *args), text=True).strip()


def _configs(provider: str) -> tuple[AgentConfig, AgentConfig]:
    return (
        AgentConfig(kind="generation", provider=provider, model=f"{provider}-model"),
        AgentConfig(kind="decision", provider="system-one", model="laya"),
    )


def _tasks(provider: str) -> list[Microtask]:
    return [
        Microtask(
            row["task_id"],
            {
                "role": row["role"],
                "kind": row["kind"],
                "provider": row["provider"],
                "model": row["model"],
            },
            depends_on=tuple(row["depends_on"]),
        )
        for row in plan_invocations(*_configs(provider))
    ]


async def _layer_barrier() -> dict[str, Any]:
    batch = compile_microtasks(
        [
            Microtask("slow", {"role": "block"}),
            Microtask("fast", {"role": "release"}),
            Microtask("after_fast", {"role": "dependent"}, depends_on=("fast",)),
        ],
        handler="microtask.preflight",
        concurrency=2,
        name="ready_map_witness",
        schedule=SCHEDULE,
    )
    started: list[str] = []
    release_slow = asyncio.Event()
    store = MemoryRunStore()
    budget = open_budget("layer-barrier", batch.task_count)

    async def work(payload: Mapping[str, Any]) -> dict[str, Any]:
        task_id = str(payload["task_id"])
        started.append(task_id)
        if task_id == "slow":
            await release_slow.wait()
        return {"task_id": task_id, "input_keys": sorted(payload)}

    async def watch() -> list[str]:
        while "fast" not in started:
            await asyncio.sleep(0)
        for _ in range(50):
            if "after_fast" in started:
                break
            await asyncio.sleep(0)
        window = list(started)
        release_slow.set()
        return window

    run, window = await asyncio.wait_for(
        asyncio.gather(
            batch.run(
                work,
                resources=resources_for(budget),
                run_store=store,
                run_id="layer-barrier",
            ),
            watch(),
        ),
        timeout=5,
    )
    trace = []
    for record in store.invocations["layer-barrier"].values():
        value = record.value if isinstance(record.value, Mapping) else {}
        trace.append(
            {
                "invocation_id": record.invocation_id,
                "node_id": record.node_id,
                "ok": record.ok,
                "task_id": value.get("task_id"),
            }
        )
    trace.sort(key=lambda row: str(row["task_id"]))
    return {
        "status": run.status,
        "schedule": batch.schedule,
        "map_nodes": list(batch.map_nodes),
        "layers": [list(layer) for layer in batch.layers],
        "starts_while_slow_blocked": window,
        "after_fast_started_before_slow_released": "after_fast" in window,
        "slow_still_blocked_when_after_fast_started": "slow" in window and "after_fast" in window,
        "final_start_order": started,
        "handler_input_keys": ["depends_on", "payload", "task_id"],
        "trace": trace,
        "budget": budget_trace(budget),
    }


async def _contract(provider_name: str) -> dict[str, Any]:
    batch = compile_microtasks(
        _tasks(provider_name),
        handler="agent.preflight",
        concurrency=2,
        name="agent_contract_preflight",
        schedule=SCHEDULE,
    )
    generation = _Generation(provider_name)
    decision = _Decision()
    journal = _Journal()
    host = AgentHost(budget=_Allow(), journal=journal, retries=0)
    store = MemoryRunStore()
    run_id = f"contract-{provider_name}"
    budget = open_budget(run_id, batch.task_count)
    seen_inputs: list[list[str]] = []

    async def work(payload: Mapping[str, Any]) -> dict[str, Any]:
        body = payload["payload"]
        assert isinstance(body, Mapping)
        seen_inputs.append(sorted(payload))
        role = str(body["role"])
        kind = str(body["kind"])
        provider = str(body["provider"])
        model = str(body["model"])
        model_called = False
        semantic_check = "upstream output is not in the handler input"
        if role == "generation":
            result = host.generate(
                AgentConfig(kind="generation", provider=provider, model=model),
                GenerationRequest(
                    invocation_id=str(payload["task_id"]),
                    node_id=str(payload["task_id"]),
                    instruction=kind,
                    output_mode="text",
                ),
                generation,
                amount=Decimal("1"),
            )
            model_called = result.ok
            semantic_check = "generation invocation"
        elif role == "decision":
            result = host.decide(
                AgentConfig(kind="decision", provider=provider, model=model),
                DecisionRequest(
                    invocation_id=str(payload["task_id"]),
                    node_id=str(payload["task_id"]),
                    question=kind,
                    options=["entailment", "contradiction", "neutral"],
                ),
                decision,
                amount=Decimal("1"),
            )
            model_called = result.ok
            semantic_check = "decision invocation"
        return {
            "task_id": payload["task_id"],
            "role": role,
            "kind": kind,
            "provider": provider,
            "model": model,
            "model_called": model_called,
            "semantic_check": semantic_check,
            "upstream_output_present": "output" in payload,
        }

    run = await batch.run(
        work,
        resources=resources_for(budget),
        run_store=store,
        run_id=run_id,
    )
    rows = []
    for record in store.invocations[run_id].values():
        value = record.value if isinstance(record.value, Mapping) else {}
        rows.append(
            {
                "invocation_id": record.invocation_id,
                "node_id": record.node_id,
                "ok": record.ok,
                "task_id": value.get("task_id"),
                "role": value.get("role"),
                "provider": value.get("provider"),
                "model": value.get("model"),
                "model_called": value.get("model_called"),
                "semantic_check": value.get("semantic_check"),
                "upstream_output_present": value.get("upstream_output_present"),
            }
        )
    rows.sort(key=lambda row: str(row["task_id"]))
    return {
        "status": run.status,
        "schedule": batch.schedule,
        "definition_sha256": batch.compiled.sha256,
        "layers": [list(layer) for layer in batch.layers],
        "map_nodes": list(batch.map_nodes),
        "trace": rows,
        "journal_roles": [record["node_id"] for record in journal.records],
        "generation_calls": list(generation.calls),
        "decision_calls": list(decision.calls),
        "handler_input_keys": sorted({key for keys in seen_inputs for key in keys}),
        "budget": budget_trace(budget),
    }


async def _replay() -> dict[str, Any]:
    batch = compile_microtasks(
        [
            Microtask("a", {"ordinal": 0}),
            Microtask("b", {"ordinal": 1}, depends_on=("a",)),
        ],
        handler="microtask.replay",
        concurrency=1,
        name="replay_witness",
        schedule=SCHEDULE,
    )
    executed: list[str] = []

    async def work(payload: Mapping[str, Any]) -> dict[str, str]:
        task_id = str(payload["task_id"])
        executed.append(task_id)
        return {"task_id": task_id}

    store = _CrashOnce(crash_after=1)
    budget = open_budget("replay-run", batch.task_count)
    resources = resources_for(budget)
    try:
        await batch.run(work, resources=resources, run_store=store, run_id="replay-run")
    except RuntimeError as exc:
        crashed = str(exc) == "simulated crash"
    else:
        crashed = False
    before = list(executed)
    resumed = await batch.resume(work, "replay-run", resources=resources, run_store=store)
    return {
        "schedule": batch.schedule,
        "crashed_after_first_persisted_invocation": crashed,
        "executed_before_resume": before,
        "executed_after_resume": list(executed),
        "resume_status": resumed.status,
        "replayed_task_executed_again": executed.count("a") != 1,
        "budget": budget_trace(budget),
    }


async def run_preflight() -> dict[str, Any]:
    import swe_native

    package_budget = runtime_budget_status()
    gap = dispatch_gap()
    refused = False
    try:
        require_swe_dispatch()
    except DispatchGap:
        refused = True
    barrier = await _layer_barrier()
    alpha = await _contract("alpha")
    beta = await _contract("beta")
    replay = await _replay()
    alpha_ids = [row["task_id"] for row in alpha["trace"]]
    beta_ids = [row["task_id"] for row in beta["trace"]]
    return {
        "paid_calls": 0,
        "checkout": {
            "head": _git("rev-parse", "HEAD"),
            "branch": _git("branch", "--show-current"),
            "merge_base": _git("merge-base", "HEAD", "origin/main"),
            "python_and_crates_diff": _git("diff", "--stat", "origin/main", "--", "python", "crates"),
        },
        "wheel": {"file": swe_native.__file__},
        "runner_parameters": [
            name
            for name in inspect.signature(WorkflowRunner.__init__).parameters
            if name != "self"
        ],
        "runtime_budget": alpha["budget"],
        "package_runtime_budget": package_budget,
        "package_dispatch_gap": {
            "package_dispatcher": gap["package_dispatcher"],
            "fail_closed": gap["fail_closed"],
            "swe_ready": gap["swe_ready"],
            "swe_ordered_default": gap["swe_ordered_default"],
            "swe_native": gap["swe_native"],
            "require_swe_dispatch_raises": refused,
        },
        "layer_barrier": barrier,
        "contract": {
            "schedule": alpha["schedule"],
            "same_definition": alpha["definition_sha256"] == beta["definition_sha256"],
            "definition_sha256": alpha["definition_sha256"],
            "same_task_ids": alpha_ids == beta_ids,
            "layers": alpha["layers"],
            "map_nodes": alpha["map_nodes"],
            "handler_input_keys": alpha["handler_input_keys"],
            "upstream_output_present": any(
                row["upstream_output_present"] for row in alpha["trace"]
            ),
            "alpha_trace": alpha["trace"],
            "beta_generation_providers": sorted(
                {row["provider"] for row in beta["trace"] if row["role"] == "generation"}
            ),
            "alpha_generation_providers": sorted(
                {row["provider"] for row in alpha["trace"] if row["role"] == "generation"}
            ),
            "decision_providers": sorted(
                {row["provider"] for row in alpha["trace"] if row["role"] == "decision"}
            ),
            "decision_models": sorted(
                {row["model"] for row in alpha["trace"] if row["role"] == "decision"}
            ),
            "validator_model_calls": sum(
                1 for row in alpha["trace"] if row["role"] == "validator" and row["model_called"]
            ),
            "generation_calls": alpha["generation_calls"],
            "decision_calls": alpha["decision_calls"],
            "journal_nodes": alpha["journal_roles"],
            "status": alpha["status"],
        },
        "replay": replay,
    }


def main() -> None:
    import json

    report = asyncio.run(run_preflight())
    json.dump(report, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
