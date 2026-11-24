"""SWD 1.1 static ready-map: admission, replay, cancel, concurrency, 1.0."""

from __future__ import annotations

import asyncio
from collections import Counter
from collections.abc import Mapping, Sequence
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from swe.definition import compile_definition
from swe.errors import SwdValidationError
from swe.invocation_handle import current_invocation
from swe.microtasks import Microtask, MicrotaskError, MicrotaskMeter, compile_microtasks
from swe.run_store import InvocationRecord, MemoryRunStore, RunRecord, RunStore
from swe.runtime import WorkflowResources, WorkflowRunner
from swe.schema import SWD_VERSION, SWD_VERSION_1_1, load_schema

pytestmark = pytest.mark.swe_runtime


def test_schemas_keep_1_0_frozen_and_accept_1_1() -> None:
    schema_10 = load_schema()
    schema_11 = load_schema(SWD_VERSION_1_1)
    Draft202012Validator.check_schema(schema_10)
    Draft202012Validator.check_schema(schema_11)
    assert schema_10["properties"]["version"]["const"] == SWD_VERSION
    assert schema_11["properties"]["version"]["const"] == SWD_VERSION_1_1
    assert "schedule" not in schema_10["$defs"]["mapNode"]["properties"]
    assert set(schema_11["$defs"]["mapNode"]["properties"]) >= {
        "schedule",
        "item_id",
        "item_depends_on",
    }


def test_default_microtasks_stay_on_ordered_swd_1_0() -> None:
    batch = compile_microtasks(
        [
            Microtask("second", depends_on=("first",)),
            Microtask("first"),
        ],
        handler="microtask.work",
    )
    assert batch.schedule == "ordered"
    assert batch.compiled.version == "1.0"
    assert batch.compiled.document["version"] == "1.0"
    assert batch.layers == (("first",), ("second",))
    assert len(batch.map_nodes) == 2
    fanout = batch.compiled.document["nodes"][batch.map_nodes[0]]
    assert fanout["type"] == "map"
    assert "schedule" not in fanout
    assert "item_id" not in fanout


def test_ready_schedule_is_one_swd_1_1_map_and_rejects_a_bad_id() -> None:
    batch = compile_microtasks(
        [
            Microtask("second", depends_on=("first",)),
            Microtask("first"),
        ],
        handler="microtask.work",
        schedule="ready",
    )
    assert batch.compiled.version == "1.1"
    assert batch.map_nodes == ("microtasks",)
    fanout = batch.compiled.document["nodes"]["microtasks"]
    assert fanout["schedule"] == "ready"
    assert fanout["item_id"] == "$.item.task_id"
    assert fanout["item_depends_on"] == "$.item.depends_on"
    assert [row["task_id"] for row in batch.input_data["tasks"]] == ["first", "second"]
    with pytest.raises(MicrotaskError, match="ready-map item id"):
        compile_microtasks(
            [Microtask("1bad")],
            handler="microtask.work",
            schedule="ready",
        )


def test_ready_fields_are_rejected_on_1_0_and_required_for_ready() -> None:
    document = _ready_document()
    document["version"] = "1.0"
    with pytest.raises(SwdValidationError):
        compile_definition(document, handlers=frozenset({"demo.work"}))
    ready = _ready_document()
    del ready["nodes"]["fanout"]["item_id"]
    with pytest.raises(SwdValidationError):
        compile_definition(ready, handlers=frozenset({"demo.work"}))
    ordered = _ready_document()
    ordered["nodes"]["fanout"]["schedule"] = "ordered"
    with pytest.raises(SwdValidationError):
        compile_definition(ordered, handlers=frozenset({"demo.work"}))


def _ready_document() -> dict[str, Any]:
    return {
        "version": "1.1",
        "name": "ready_map",
        "entrypoint": "fanout",
        "limits": {"max_steps": 20, "max_depth": 2, "timeout_seconds": 30},
        "nodes": {
            "fanout": {
                "type": "map",
                "items": "$.input.tasks",
                "child": "work",
                "concurrency": 2,
                "schedule": "ready",
                "item_id": "$.item.task_id",
                "item_depends_on": "$.item.depends_on",
                "next": "done",
            },
            "work": {
                "type": "task",
                "handler": "demo.work",
                "input": {
                    "task_id": "$.item.task_id",
                    "payload": "$.item.payload",
                    "depends_on": "$.item.depends_on",
                },
            },
            "done": {"type": "end", "status": "completed"},
        },
    }


def _straggler_tasks() -> list[Microtask]:
    return [
        Microtask("slow", {"kind": "slow"}),
        Microtask("fast", {"kind": "fast"}),
        Microtask("after_fast", {"kind": "check"}, depends_on=("fast",)),
    ]


async def test_ready_map_starts_a_dependent_while_a_straggler_is_open() -> None:
    batch = compile_microtasks(
        _straggler_tasks(),
        handler="demo.work",
        concurrency=2,
        schedule="ready",
        name="ready_straggler",
    )
    release_slow = asyncio.Event()
    overlapped = False

    async def work(payload: Mapping[str, Any]) -> dict[str, Any]:
        nonlocal overlapped
        assert set(payload) == {"task_id", "payload", "depends_on"}
        task_id = str(payload["task_id"])
        if task_id == "after_fast":
            assert payload["depends_on"] == ["fast"]
            assert payload["payload"] == {"kind": "check"}
            overlapped = True
            release_slow.set()
        if task_id == "slow":
            await release_slow.wait()
            assert overlapped
        return {"task_id": task_id}

    run = await asyncio.wait_for(batch.run(work), timeout=2)
    assert run.status == "completed"
    assert overlapped
    assert [row["task_id"] for row in batch.collect_results(run)] == [
        "slow",
        "fast",
        "after_fast",
    ]


async def test_ready_map_replay_does_not_rerun_persisted_items() -> None:
    batch = compile_microtasks(
        _straggler_tasks(),
        handler="demo.work",
        concurrency=2,
        schedule="ready",
        name="ready_replay",
    )
    release_slow = asyncio.Event()
    executions: list[str] = []
    store = _CrashOnceStore(crash_after=2)

    async def work(payload: Mapping[str, Any]) -> dict[str, str]:
        task_id = str(payload["task_id"])
        executions.append(task_id)
        if task_id == "slow":
            await release_slow.wait()
        return {"task_id": task_id}

    with pytest.raises(RuntimeError, match="simulated crash"):
        await asyncio.wait_for(
            batch.run(work, run_store=store, run_id="ready-replay"),
            timeout=2,
        )
    release_slow.set()
    resumed = await asyncio.wait_for(
        batch.resume(work, "ready-replay", run_store=store),
        timeout=2,
    )
    assert resumed.status == "completed"
    counts = Counter(executions)
    assert counts["fast"] == 1
    assert counts["after_fast"] == 1
    assert counts["slow"] == 2
    assert {row["task_id"] for row in batch.collect_results(resumed)} == {
        "slow",
        "fast",
        "after_fast",
    }


async def test_ready_map_cancel_stops_admission() -> None:
    batch = compile_microtasks(
        (Microtask(f"task{index}") for index in range(4)),
        handler="demo.wait",
        concurrency=2,
        schedule="ready",
        name="ready_cancel",
    )
    cancel = asyncio.Event()
    started = asyncio.Event()
    started_count = 0

    async def wait(_payload: Mapping[str, Any]) -> dict[str, bool]:
        nonlocal started_count
        started_count += 1
        if started_count >= 2:
            started.set()
        await asyncio.Event().wait()
        return {"ok": False}

    async def trip() -> None:
        await started.wait()
        cancel.set()

    run, _ = await asyncio.wait_for(
        asyncio.gather(
            batch.run(wait, resources=WorkflowResources(cancel_event=cancel)),
            trip(),
        ),
        timeout=2,
    )
    assert run.status == "cancelled"
    assert started_count == 2


async def test_ready_map_peak_stays_within_concurrency() -> None:
    concurrency = 3
    batch = compile_microtasks(
        (Microtask(f"task{index}") for index in range(18)),
        handler="demo.work",
        concurrency=concurrency,
        schedule="ready",
        name="ready_cap",
    )

    async def work(payload: Mapping[str, Any]) -> dict[str, Any]:
        await asyncio.sleep(0.001)
        return {"task_id": payload["task_id"]}

    meter = MicrotaskMeter(work)
    run = await batch.run(meter)
    assert run.status == "completed"
    metrics = meter.snapshot()
    assert metrics["started"] == 18
    assert 1 < metrics["peak_concurrency"] <= concurrency


async def test_unknown_runtime_dependency_fails_before_the_handler() -> None:
    compiled = compile_definition(_ready_document(), handlers=frozenset({"demo.work"}))
    called = False

    async def work(_payload: Mapping[str, Any]) -> dict[str, bool]:
        nonlocal called
        called = True
        return {"ok": True}

    from swe.runtime import run_workflow

    result = await run_workflow(
        compiled,
        {"demo.work": work},
        {
            "tasks": [
                {"task_id": "only", "payload": {}, "depends_on": ["missing"]},
            ]
        },
    )
    assert result.status == "failed"
    assert result.error is not None
    assert result.error["kind"] == "preflight"
    assert called is False


def _gate_document() -> dict[str, Any]:
    document = _ready_document()
    document["name"] = "ready_gate"
    document["limits"]["max_steps"] = 40
    document["limits"]["max_depth"] = 4
    fanout = document["nodes"]["fanout"]
    fanout["child"] = "gate"
    document["nodes"]["gate"] = {
        "type": "decision",
        "evaluator": "expression",
        "condition": {"eq": [{"var": "$.item.task_id"}, {"const": ""}]},
        "routes": {"true": "work", "false": "work"},
    }
    return document


def _gate_input() -> dict[str, Any]:
    return {
        "tasks": [
            {"task_id": "slow", "payload": {}, "depends_on": []},
            {"task_id": "fast", "payload": {}, "depends_on": []},
            {"task_id": "after_fast", "payload": {}, "depends_on": ["fast"]},
            {"task_id": "after_slow", "payload": {}, "depends_on": ["slow"]},
        ]
    }


async def _run_gate(*, prefer_fast: bool, run_id: str) -> dict[str, str]:
    release = asyncio.Event()
    seen: dict[str, str] = {}

    async def work(payload: Mapping[str, Any]) -> dict[str, str]:
        task_id = str(payload["task_id"])
        handle = current_invocation()
        assert handle is not None
        seen[task_id] = handle.invocation_id
        if prefer_fast and task_id == "slow":
            await release.wait()
        if not prefer_fast and task_id == "fast":
            await release.wait()
        if task_id in {"after_fast", "after_slow"}:
            release.set()
        return {"task_id": task_id}

    store = MemoryRunStore()
    compiled = compile_definition(_gate_document(), handlers=frozenset({"demo.work"}))
    runner = WorkflowRunner(compiled, {"demo.work": work}, run_store=store)
    result = await asyncio.wait_for(runner.run(_gate_input(), run_id=run_id), timeout=2)
    assert result.status == "completed"
    assert set(store.invocations[run_id]) == set(seen.values())
    return seen


async def test_ready_gate_ids_match_across_completion_order_and_resume() -> None:
    fast_first = await _run_gate(prefer_fast=True, run_id="ready-gate")
    slow_first = await _run_gate(prefer_fast=False, run_id="ready-gate")
    assert fast_first == slow_first
    assert fast_first["after_fast"] != fast_first["after_slow"]
    assert "2/after_fast" in fast_first["after_fast"]
    assert "3/after_slow" in fast_first["after_slow"]

    release_slow = asyncio.Event()
    executions: list[str] = []
    store = _CrashOnceStore(crash_after=2)

    async def work(payload: Mapping[str, Any]) -> dict[str, str]:
        task_id = str(payload["task_id"])
        executions.append(task_id)
        if task_id == "slow":
            await release_slow.wait()
        return {"task_id": task_id}

    compiled = compile_definition(_gate_document(), handlers=frozenset({"demo.work"}))
    runner = WorkflowRunner(compiled, {"demo.work": work}, run_store=store)
    with pytest.raises(RuntimeError, match="simulated crash"):
        await asyncio.wait_for(runner.run(_gate_input(), run_id="ready-gate"), timeout=2)
    release_slow.set()
    resumed = await asyncio.wait_for(
        WorkflowRunner(compiled, {"demo.work": work}, run_store=store).resume("ready-gate"),
        timeout=2,
    )
    assert resumed.status == "completed"
    counts = Counter(executions)
    assert counts["fast"] == 1
    assert counts["after_fast"] == 1
    assert counts["slow"] == 2
    assert counts["after_slow"] == 1
    assert set(store.invocations["ready-gate"]) == set(fast_first.values())


def _nested_document() -> dict[str, Any]:
    return {
        "version": "1.1",
        "name": "ready_nested_resume",
        "entrypoint": "fanout",
        "limits": {"max_steps": 500, "max_depth": 8, "timeout_seconds": 30},
        "nodes": {
            "fanout": {
                "type": "map",
                "items": "$.input.tasks",
                "child": "split",
                "concurrency": 2,
                "schedule": "ready",
                "item_id": "$.item.task_id",
                "item_depends_on": "$.item.depends_on",
                "next": "done",
            },
            "split": {
                "type": "fork",
                "branches": {"left": "lane_a", "right": "lane_b"},
                "join": "joined",
            },
            "lane_a": {"type": "task", "handler": "demo.work", "next": "left_ordered"},
            "left_ordered": {
                "type": "map",
                "items": "$.input.parts",
                "child": "ordered_part",
                "concurrency": 1,
                "next": "lane_a2",
            },
            "ordered_part": {"type": "task", "handler": "demo.work"},
            "lane_a2": {"type": "task", "handler": "demo.work"},
            "lane_b": {"type": "task", "handler": "demo.work", "next": "right_ready"},
            "right_ready": {
                "type": "map",
                "items": "$.input.more",
                "child": "ready_part",
                "concurrency": 2,
                "schedule": "ready",
                "item_id": "$.item.task_id",
                "item_depends_on": "$.item.depends_on",
                "next": "right_tail",
            },
            "ready_part": {"type": "task", "handler": "demo.work"},
            "right_tail": {"type": "task", "handler": "demo.work"},
            "joined": {"type": "join", "mode": "all", "next": "first_map"},
            "first_map": {
                "type": "map",
                "items": "$.input.parts",
                "child": "part_a",
                "concurrency": 1,
                "next": "second_map",
            },
            "second_map": {
                "type": "map",
                "items": "$.input.parts",
                "child": "part_b",
                "concurrency": 1,
                "next": "body",
            },
            "part_a": {"type": "task", "handler": "demo.work"},
            "part_b": {"type": "task", "handler": "demo.work"},
            "body": {"type": "task", "handler": "demo.work", "next": "again"},
            "again": {
                "type": "decision",
                "evaluator": "expression",
                "condition": {"eq": [{"var": "$.body.result.go"}, {"const": True}]},
                "routes": {"true": "body", "false": "tail"},
            },
            "tail": {"type": "task", "handler": "demo.work"},
            "done": {"type": "end", "status": "completed"},
        },
    }


def _nested_input() -> dict[str, Any]:
    return {
        "tasks": [{"task_id": "outer", "payload": {}, "depends_on": []}],
        "parts": ["p0", "p1"],
        "more": [
            {"task_id": "slow", "depends_on": []},
            {"task_id": "fast", "depends_on": []},
            {"task_id": "after_fast", "depends_on": ["fast"]},
        ],
    }


def _split_invocation(invocation_id: str) -> tuple[str, str, str, str]:
    run_id, node, path, epoch, serial = invocation_id.split(":")
    assert run_id
    assert serial
    return node, path, epoch, serial


def _trace(invocations: Mapping[str, InvocationRecord]) -> dict[tuple[str, str, str], list[str]]:
    """Group full ids by node, branch path and loop epoch, in persist order."""
    grouped: dict[tuple[str, str, str], list[str]] = {}
    for invocation_id in invocations:
        node, path, epoch, _serial = _split_invocation(invocation_id)
        grouped.setdefault((node, path, epoch), []).append(invocation_id)
    return grouped


def _assert_full_ids(trace: Mapping[tuple[str, str, str], Sequence[str]]) -> None:
    flat = [invocation_id for ids in trace.values() for invocation_id in ids]
    assert len(flat) == len(set(flat))
    # Two maps reuse index paths. The loop reuses the item path for one node.
    first = trace[("part_a", "0/outer/0", "0")][0]
    second = trace[("part_b", "0/outer/0", "0")][0]
    assert first != second
    assert trace[("part_a", "0/outer/1", "0")][0] != trace[("part_b", "0/outer/1", "0")][0]
    assert ("body", "0/outer", "0") in trace
    assert ("body", "0/outer", "1") in trace
    assert any(path.startswith("0/outer/left") for (_node, path, _epoch) in trace)
    assert any("slow" in path for (_node, path, _epoch) in trace)
    assert any("after_fast" in path for (_node, path, _epoch) in trace)


async def _run_nested(
    *,
    hold: str,
    delay_ready: str | None,
    run_id: str,
) -> MemoryRunStore:
    fork_release = asyncio.Event()
    ready_release = asyncio.Event()
    release_node = "lane_a2" if hold == "right" else "right_tail"

    async def work(payload: Mapping[str, Any]) -> dict[str, bool]:
        handle = current_invocation()
        assert handle is not None
        invocation_id = handle.invocation_id
        node, path, epoch, _serial = _split_invocation(invocation_id)
        if (hold == "right" and "/right" in path) or (hold == "left" and "/left" in path):
            await fork_release.wait()
        item = payload.get("item")
        task_id = item.get("task_id") if isinstance(item, dict) else None
        if delay_ready is not None and task_id == delay_ready:
            await ready_release.wait()
        if node == release_node:
            fork_release.set()
        if delay_ready == "slow" and task_id == "after_fast":
            ready_release.set()
        if delay_ready == "fast" and task_id == "slow":
            ready_release.set()
        return {"go": node == "body" and epoch == "0"}

    store = MemoryRunStore()
    compiled = compile_definition(_nested_document(), handlers=frozenset({"demo.work"}))
    runner = WorkflowRunner(compiled, {"demo.work": work}, run_store=store)
    result = await asyncio.wait_for(runner.run(_nested_input(), run_id=run_id), timeout=2)
    assert result.status == "completed"
    return store


async def test_nested_ready_fork_ids_match_when_completion_order_reverses() -> None:
    run_id = "nested-ready"
    left_first = await _run_nested(hold="right", delay_ready="slow", run_id=run_id)
    right_first = await _run_nested(hold="left", delay_ready="fast", run_id=run_id)
    left_trace = _trace(left_first.invocations[run_id])
    right_trace = _trace(right_first.invocations[run_id])
    assert left_trace == right_trace
    _assert_full_ids(left_trace)


async def test_nested_ready_fork_resume_skips_settled_handlers() -> None:
    run_id = "nested-resume"
    left_first = await _run_nested(hold="right", delay_ready=None, run_id=run_id)
    right_first = await _run_nested(hold="left", delay_ready=None, run_id=run_id)
    expected = _trace(left_first.invocations[run_id])
    assert _trace(right_first.invocations[run_id]) == expected
    cases = (
        ("right", 4, "lane_b"),
        ("left", 5, "lane_a"),
    )
    for hold, crash_after, waiting_node in cases:
        store = _CrashOnceStore(crash_after=crash_after)
        fork_release = asyncio.Event()
        executions: list[str] = []

        async def work(payload: Mapping[str, Any]) -> dict[str, bool]:
            del payload
            handle = current_invocation()
            assert handle is not None
            invocation_id = handle.invocation_id
            executions.append(invocation_id)
            node, path, epoch, _serial = _split_invocation(invocation_id)
            held = (hold == "right" and "/right" in path) or (
                hold == "left" and "/left" in path
            )
            if held:
                await fork_release.wait()
            return {"go": node == "body" and epoch == "0"}

        compiled = compile_definition(_nested_document(), handlers=frozenset({"demo.work"}))
        runner = WorkflowRunner(compiled, {"demo.work": work}, run_store=store)
        with pytest.raises(RuntimeError, match="simulated crash"):
            await asyncio.wait_for(runner.run(_nested_input(), run_id=run_id), timeout=2)
        assert store.runs[run_id].status == "running"
        settled = set(store.invocations[run_id])
        assert len(settled) == crash_after
        prefix = list(store.event_log[run_id])
        assert prefix
        fork_release.set()
        resumed = await asyncio.wait_for(
            WorkflowRunner(compiled, {"demo.work": work}, run_store=store).resume(run_id),
            timeout=2,
        )
        assert resumed.status == "completed"
        assert store.event_log[run_id][: len(prefix)] == prefix
        assert len(store.event_log[run_id]) > len(prefix)
        counts = Counter(executions)
        for invocation_id in settled:
            assert counts[invocation_id] == 1
        waiting = [invocation_id for invocation_id, count in counts.items() if count == 2]
        assert len(waiting) == 1
        assert _split_invocation(waiting[0])[0] == waiting_node
        assert waiting[0] not in settled
        final = set(store.invocations[run_id])
        for invocation_id in final:
            if invocation_id != waiting[0]:
                assert counts[invocation_id] == 1
        assert _trace(store.invocations[run_id]) == expected


class _CrashOnceStore(RunStore):
    def __init__(self, *, crash_after: int) -> None:
        self.crash_after = crash_after
        self.crashed = False
        self.runs: dict[str, RunRecord] = {}
        self.invocations: dict[str, dict[str, InvocationRecord]] = {}
        self.event_log: dict[str, list[dict[str, Any]]] = {}

    async def record(
        self,
        run: RunRecord,
        *,
        events: Sequence[Mapping[str, Any]] = (),
        invocation: InvocationRecord | None = None,
    ) -> None:
        self.runs[run.run_id] = run
        self.event_log.setdefault(run.run_id, []).extend(dict(event) for event in events)
        if invocation is not None:
            self.invocations.setdefault(run.run_id, {})[invocation.invocation_id] = invocation
            if (
                not self.crashed
                and len(self.invocations[run.run_id]) >= self.crash_after
            ):
                self.crashed = True
                raise RuntimeError("simulated crash")

    async def load_run(self, run_id: str) -> RunRecord | None:
        return self.runs.get(run_id)

    async def replay_results(self, run_id: str) -> Mapping[str, InvocationRecord]:
        return dict(self.invocations.get(run_id, {}))

    async def events(self, run_id: str) -> list[dict[str, Any]]:
        return list(self.event_log.get(run_id, []))


class _MilliPolicy:
    """One milli-unit for a successful child. Matches the PR11 fixed tariff."""

    def reserve(self, request: object) -> int:
        del request
        return 1

    def settle(self, request: object, outcome: object) -> int:
        del request
        return 1 if getattr(outcome, "ok", False) else 0


def _ready_budget(cap_milli: int) -> tuple[Any, Any]:
    pytest.importorskip("swe_budget")
    from swe.budget import NativeBudgetLedger, RuntimeBudget

    ledger = NativeBudgetLedger()
    ledger.allocate(run_id="run-1", account="a", amount_milli=cap_milli, key="a:alloc")
    return RuntimeBudget(ledger, _MilliPolicy(), account="a"), ledger


def _ready_tasks(*task_ids: str, depends: Mapping[str, Sequence[str]] | None = None) -> dict[str, Any]:
    dependencies = depends or {}
    return {
        "tasks": [
            {
                "task_id": task_id,
                "payload": {},
                "depends_on": list(dependencies.get(task_id, ())),
            }
            for task_id in task_ids
        ]
    }


def _assert_child_invocation(invocation_id: str, *, run_id: str, task_id: str) -> None:
    run, node, path, epoch, serial = invocation_id.split(":")
    assert run == run_id
    assert node == "work"
    assert path.endswith(f"/{task_id}")
    assert epoch == "0"
    int(serial)


def _spend_invocation_ids(ledger: Any, *, run_id: str) -> list[str]:
    found: list[str] = []
    for event in ledger.events():
        if event["kind"] != "spend":
            continue
        key = str(event["key"])
        assert key.endswith(":settle")
        body = key[: -len(":settle")]
        prefix = f"{run_id}:"
        assert body.startswith(prefix)
        found.append(body[len(prefix) :])
    return found


def _assert_budget_closed(budget: Any, ledger: Any, *, spent: int) -> None:
    balance = ledger.balance(account="a")
    assert balance["spent"] == spent
    assert balance["reserved"] == 0
    assert budget.open_count == 0
    assert budget.unresolved() == ()
    assert {event["kind"] for event in ledger.events()} <= {
        "allocate",
        "reserve",
        "spend",
        "release",
    }


async def test_ready_map_budget_refuses_the_child_past_the_cap() -> None:
    calls: list[str] = []

    async def work(payload: Mapping[str, Any]) -> dict[str, str]:
        task_id = str(payload["task_id"])
        calls.append(task_id)
        handle = current_invocation()
        assert handle is not None
        _assert_child_invocation(handle.invocation_id, run_id="ready-budget-cap", task_id=task_id)
        return {"task_id": task_id}

    budget, ledger = _ready_budget(1)
    compiled = compile_definition(_ready_document(), handlers=frozenset({"demo.work"}))
    result = await asyncio.wait_for(
        WorkflowRunner(
            compiled,
            {"demo.work": work},
            resources=WorkflowResources(budget=budget),
            run_store=MemoryRunStore(),
        ).run(_ready_tasks("only", "next", "last"), run_id="ready-budget-cap"),
        timeout=2,
    )
    assert result.status == "failed"
    assert result.error is not None
    assert "budget" in result.error["message"].lower()
    assert len(calls) == 1
    assert calls[0] in {"only", "next", "last"}
    spends = _spend_invocation_ids(ledger, run_id="ready-budget-cap")
    assert len(spends) == 1
    _assert_child_invocation(spends[0], run_id="ready-budget-cap", task_id=calls[0])
    _assert_budget_closed(budget, ledger, spent=1)


async def test_ready_map_budget_resume_does_not_recharge_a_journaled_child() -> None:
    pytest.importorskip("swe_budget")
    from swe.budget import NativeBudgetLedger, RuntimeBudget

    run_id = "ready-budget-resume"
    release = asyncio.Event()
    executions: list[tuple[str, str]] = []

    async def work(payload: Mapping[str, Any]) -> dict[str, str]:
        task_id = str(payload["task_id"])
        handle = current_invocation()
        assert handle is not None
        _assert_child_invocation(handle.invocation_id, run_id=run_id, task_id=task_id)
        executions.append((task_id, handle.invocation_id))
        if task_id == "slow":
            await release.wait()
        return {"task_id": task_id}

    budget, ledger = _ready_budget(3)
    store = _CrashOnceStore(crash_after=1)
    compiled = compile_definition(_ready_document(), handlers=frozenset({"demo.work"}))
    runner = WorkflowRunner(
        compiled,
        {"demo.work": work},
        resources=WorkflowResources(budget=budget),
        run_store=store,
    )
    with pytest.raises(RuntimeError, match="simulated crash"):
        await asyncio.wait_for(
            runner.run(
                _ready_tasks("fast", "slow", "later", depends={"later": ["fast"]}),
                run_id=run_id,
            ),
            timeout=2,
        )
    assert store.runs[run_id].status == "running"
    settled = list(store.invocations[run_id])
    assert len(settled) == 1
    journaled = settled[0]
    assert _spend_invocation_ids(ledger, run_id=run_id) == [journaled]
    release.set()
    replayed = NativeBudgetLedger.replay(ledger.events())
    resumed_budget = RuntimeBudget(replayed, _MilliPolicy(), account="a")
    resumed = await asyncio.wait_for(
        WorkflowRunner(
            compiled,
            {"demo.work": work},
            resources=WorkflowResources(budget=resumed_budget),
            run_store=store,
        ).resume(run_id),
        timeout=2,
    )
    assert resumed.status == "completed"
    counts = Counter(task_id for task_id, _invocation_id in executions)
    assert counts["fast"] == 1
    assert counts["slow"] == 2
    assert counts["later"] == 1
    assert [invocation_id for task_id, invocation_id in executions if task_id == "fast"] == [
        journaled
    ]
    spends = _spend_invocation_ids(replayed, run_id=run_id)
    assert spends.count(journaled) == 1
    assert len(spends) == len(set(spends)) == 3
    assert set(spends) == set(store.invocations[run_id])
    _assert_budget_closed(resumed_budget, replayed, spent=3)
