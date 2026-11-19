"""Generic InvocationHandle cancel/timeout contract on top of action timeouts."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from typing import Any

import pytest

pytest.importorskip("swe_native")

from swe import InvocationHandle, current_invocation
from swe.runtime import WorkflowResources, WorkflowRunner, run_workflow
from swe.runtime_clock import FrozenClock
from swe.run_store import MemoryRunStore, RunRecord
from swe_runtime_helpers import compile_runtime, swe_tasks

pytestmark = pytest.mark.swe_runtime


def _task_flow(*, timeout_seconds: int = 30) -> object:
    return compile_runtime(
        {
            "work": {"type": "task", "handler": "demo.work", "next": "done"},
            "done": {"type": "end", "status": "completed"},
        },
        name="once",
        entrypoint="work",
        limits={"max_steps": 80, "max_depth": 4, "timeout_seconds": timeout_seconds},
        handlers=frozenset({"demo.work"}),
    )


def _fork_flow() -> object:
    return compile_runtime(
        {
            "split": {
                "type": "fork",
                "branches": {"left": "slow", "right": "fast"},
                "join": "together",
            },
            "slow": {"type": "task", "handler": "demo.slow"},
            "fast": {"type": "task", "handler": "demo.fast"},
            "together": {"type": "join", "mode": "all", "next": "done"},
            "done": {"type": "end", "status": "completed"},
        },
        name="fanout",
        entrypoint="split",
        handlers=frozenset({"demo.slow", "demo.fast"}),
    )


def _resources(timeout: int = 1) -> WorkflowResources:
    return WorkflowResources(action_timeouts={"work": timeout})


def _assert_action_timeout(result: object, *, node_id: str, timeout: int) -> None:
    assert result.status == "failed"
    assert result.error is not None
    assert result.error["kind"] == "timeout"
    assert result.error["path"] == f"nodes.{node_id}"
    assert result.error["message"] == f"action timeout_seconds {timeout} exceeded"
    assert not any(event["type"] == "RunCompleted" for event in result.events)


def _event_types(result: object) -> list[str]:
    return [event["type"] for event in result.events]


async def _cancel_swe_tasks() -> None:
    leftover = swe_tasks()
    for task in leftover:
        task.cancel()
    if leftover:
        await asyncio.gather(*leftover, return_exceptions=True)


async def _never(_payload: Mapping[str, object]) -> dict[str, str]:
    try:
        await asyncio.Event().wait()
    except asyncio.CancelledError:
        await asyncio.Event().wait()
    return {"ok": "late"}


class _CountingStore(MemoryRunStore):
    """Count invocation settles; budget is not on the Python runtime path."""

    def __init__(self) -> None:
        super().__init__()
        self.invocation_writes = 0

    async def record(
        self,
        run: RunRecord,
        *,
        events: Sequence[Mapping[str, Any]] = (),
        invocation: object = None,
    ) -> None:
        if invocation is not None:
            self.invocation_writes += 1
        await super().record(run, events=events, invocation=invocation)


async def test_stalled_cooperative_await_binds_handle_and_sets_cancel() -> None:
    seen: dict[str, object] = {}

    async def hang(_payload: dict[str, object]) -> dict[str, str]:
        handle = current_invocation()
        seen["handle"] = handle
        assert handle is not None
        seen["cancel_before"] = handle.cancel.is_set()
        seen["reason_before"] = handle.reason()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            seen["cancel_after"] = handle.cancel.is_set()
            seen["reason_after"] = handle.reason()
            raise
        return {"ok": "late"}

    result = await asyncio.wait_for(
        run_workflow(_task_flow(), {"demo.work": hang}, {}, resources=_resources()),
        timeout=5,
    )
    _assert_action_timeout(result, node_id="work", timeout=1)
    handle = seen["handle"]
    assert isinstance(handle, InvocationHandle)
    assert handle.node_id == "work"
    assert seen["cancel_before"] is False
    assert seen["reason_before"] is None
    assert seen["cancel_after"] is True
    assert seen["reason_after"] == "timeout"
    assert handle.cancel.is_set()
    assert handle.reason() == "timeout"
    assert swe_tasks() == []


async def test_swallowed_cancel_is_timeout_not_completed() -> None:
    async def stubborn(_payload: dict[str, object]) -> dict[str, str]:
        handle = current_invocation()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            assert handle is not None
            assert handle.cancel.is_set()
            assert handle.reason() == "timeout"
            return {"status": "timeout"}
        return {"ok": "no"}

    result = await asyncio.wait_for(
        run_workflow(_task_flow(), {"demo.work": stubborn}, {}, resources=_resources()),
        timeout=5,
    )
    _assert_action_timeout(result, node_id="work", timeout=1)
    assert "work" not in result.output
    assert _event_types(result).count("NodeCompleted") == 0
    assert swe_tasks() == []


async def test_fork_timeout_does_not_join_success_branch() -> None:
    seen: dict[str, object] = {}

    async def slow(_payload: dict[str, object]) -> dict[str, str]:
        seen["slow"] = current_invocation()
        await asyncio.Event().wait()
        return {"who": "slow"}

    async def fast(_payload: dict[str, object]) -> dict[str, str]:
        return {"who": "fast"}

    result = await asyncio.wait_for(
        run_workflow(
            _fork_flow(),
            {"demo.slow": slow, "demo.fast": fast},
            {},
            resources=WorkflowResources(action_timeouts={"slow": 1, "fast": 5}),
        ),
        timeout=5,
    )
    _assert_action_timeout(result, node_id="slow", timeout=1)
    assert result.status != "completed"
    handle = seen["slow"]
    assert isinstance(handle, InvocationHandle)
    assert handle.cancel.is_set()
    assert handle.reason() == "timeout"
    assert "together" not in result.output
    assert swe_tasks() == []


async def test_cancel_before_dispatch_does_not_run_handler() -> None:
    cancel = asyncio.Event()
    cancel.set()
    calls: list[str] = []

    async def work(_payload: dict[str, object]) -> dict[str, str]:
        calls.append("ran")
        return {"ok": True}

    result = await run_workflow(
        _task_flow(),
        {"demo.work": work},
        {},
        resources=WorkflowResources(cancel_event=cancel, action_timeouts={"work": 5}),
    )
    assert calls == []
    assert result.status == "cancelled"
    assert any(event["type"] == "RunCancelled" for event in result.events)
    assert not any(event["type"] == "RunCompleted" for event in result.events)
    assert swe_tasks() == []


async def test_cancel_after_dispatch_is_cancelled_not_timeout() -> None:
    started = asyncio.Event()
    cancel = asyncio.Event()
    released = asyncio.Event()
    finished = asyncio.Event()
    seen: dict[str, object] = {}

    async def work(_payload: dict[str, object]) -> dict[str, str]:
        handle = current_invocation()
        seen["handle"] = handle
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            assert handle is not None
            seen["reason"] = handle.reason()
            seen["cancel"] = handle.cancel.is_set()
            await released.wait()
            finished.set()
            return {"ok": "late"}
        return {"ok": "no"}

    async def trip() -> None:
        await started.wait()
        cancel.set()

    runner = WorkflowRunner(
        _task_flow(),
        {"demo.work": work},
        resources=WorkflowResources(cancel_event=cancel),
    )
    result, _ = await asyncio.gather(
        asyncio.wait_for(runner.run({}), timeout=5),
        trip(),
    )
    assert result.status == "cancelled"
    assert result.error is None or result.error.get("kind") != "timeout"
    assert seen["reason"] == "cancel"
    assert seen["cancel"] is True
    assert "work" not in result.output
    assert not any(event["type"] == "RunCompleted" for event in result.events)
    assert _event_types(result).count("NodeCompleted") == 0
    released.set()
    await asyncio.wait_for(finished.wait(), timeout=1)
    assert not any(event["type"] == "RunCompleted" for event in result.events)
    await _cancel_swe_tasks()


async def test_late_completion_after_abandon_does_not_route() -> None:
    store = _CountingStore()
    released = asyncio.Event()
    finished = asyncio.Event()

    async def stubborn(_payload: dict[str, object]) -> dict[str, str]:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await released.wait()
            finished.set()
            return {"ok": "late"}
        return {"ok": "no"}

    result = await asyncio.wait_for(
        WorkflowRunner(
            _task_flow(),
            {"demo.work": stubborn},
            resources=_resources(),
            run_store=store,
        ).run({}, run_id="late-abandon"),
        timeout=5,
    )
    _assert_action_timeout(result, node_id="work", timeout=1)
    assert "work" not in result.output
    assert store.invocation_writes == 1
    writes = store.invocation_writes
    released.set()
    await asyncio.wait_for(finished.wait(), timeout=1)
    assert store.invocation_writes == writes
    recorded = await store.replay_results("late-abandon")
    assert len(recorded) == 1
    assert next(iter(recorded.values())).ok is False
    assert not any(event["type"] == "RunCompleted" for event in result.events)
    await _cancel_swe_tasks()
    assert swe_tasks() == []


async def test_never_finishing_handler_returns_and_registry_caps() -> None:
    runner = WorkflowRunner(
        _task_flow(),
        {"demo.work": _never},
        resources=_resources(1),
        concurrency=2,
    )
    first = await asyncio.wait_for(runner.run({}, run_id="abandon-1"), timeout=5)
    _assert_action_timeout(first, node_id="work", timeout=1)
    assert runner.abandoned_count == 1
    assert len(runner.abandoned.invocation_ids()) == 1
    snap = runner.abandoned.snapshot()
    assert snap[0].age_seconds() >= 0
    assert snap[0].node_id == "work"

    second = await asyncio.wait_for(runner.run({}, run_id="abandon-2"), timeout=5)
    _assert_action_timeout(second, node_id="work", timeout=1)
    assert runner.abandoned_count == 2
    assert runner.abandoned.at_capacity()

    third = await asyncio.wait_for(runner.run({}, run_id="abandon-3"), timeout=5)
    assert third.status == "failed"
    assert third.error is not None
    assert third.error["kind"] == "limit"
    assert "abandoned work cap" in third.error["message"]
    assert third.error["path"] == "abandoned"
    assert runner.abandoned_count == 2
    await _cancel_swe_tasks()


async def test_timeout_journal_resume_does_not_reinvoke() -> None:
    store = MemoryRunStore()
    calls: list[str] = []

    async def hang(_payload: dict[str, object]) -> dict[str, str]:
        calls.append("run")
        await asyncio.Event().wait()
        return {"ok": "late"}

    compiled = _task_flow()
    first = await asyncio.wait_for(
        WorkflowRunner(
            compiled,
            {"demo.work": hang},
            resources=_resources(1),
            run_store=store,
        ).run({}, run_id="timeout-resume"),
        timeout=5,
    )
    _assert_action_timeout(first, node_id="work", timeout=1)
    assert calls == ["run"]
    assert _event_types(first).count("NodeFailed") == 1
    assert _event_types(first).count("NodeCompleted") == 0

    async def must_not_run(_payload: dict[str, object]) -> dict[str, str]:
        calls.append("resume")
        raise AssertionError("resume must not invoke the handler")

    resumed = await WorkflowRunner(
        compiled,
        {"demo.work": must_not_run},
        resources=_resources(1),
        run_store=store,
    ).resume("timeout-resume")
    assert resumed.status == "failed"
    assert resumed.error is not None
    assert resumed.error["kind"] == "timeout"
    assert calls == ["run"]
    assert _event_types(resumed).count("NodeFailed") == 1
    assert _event_types(resumed).count("NodeCompleted") == 0
    await _cancel_swe_tasks()


async def test_cancel_journal_resume_does_not_reinvoke() -> None:
    store = MemoryRunStore()
    calls: list[str] = []
    started = asyncio.Event()
    cancel = asyncio.Event()

    async def work(_payload: dict[str, object]) -> dict[str, str]:
        calls.append("run")
        started.set()
        await asyncio.Event().wait()
        return {"ok": "late"}

    async def trip() -> None:
        await started.wait()
        cancel.set()

    compiled = _task_flow()
    first, _ = await asyncio.gather(
        asyncio.wait_for(
            WorkflowRunner(
                compiled,
                {"demo.work": work},
                resources=WorkflowResources(cancel_event=cancel),
                run_store=store,
            ).run({}, run_id="cancel-resume"),
            timeout=5,
        ),
        trip(),
    )
    assert first.status == "cancelled"
    assert calls == ["run"]

    async def must_not_run(_payload: dict[str, object]) -> dict[str, str]:
        calls.append("resume")
        raise AssertionError("resume must not invoke the handler")

    resumed = await WorkflowRunner(
        compiled,
        {"demo.work": must_not_run},
        resources=WorkflowResources(cancel_event=asyncio.Event()),
        run_store=store,
    ).resume("cancel-resume")
    assert resumed.status == "cancelled"
    assert calls == ["run"]
    await _cancel_swe_tasks()


async def test_timeout_does_not_double_settle_machine_or_journal() -> None:
    """Budget is not on the Python runtime path; prove no second complete/fail.

    ``swe-budget`` is a separate ledger. Hosts settle from machine completions,
    so a single NodeFailed and a single invocation record is the SWE guarantee.
    """

    store = _CountingStore()
    released = asyncio.Event()
    finished = asyncio.Event()

    async def stubborn(_payload: dict[str, object]) -> dict[str, str]:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await released.wait()
            finished.set()
            return {"ok": "late"}
        return {"ok": "no"}

    result = await asyncio.wait_for(
        WorkflowRunner(
            _task_flow(),
            {"demo.work": stubborn},
            resources=_resources(1),
            run_store=store,
        ).run({}, run_id="settle-once"),
        timeout=5,
    )
    _assert_action_timeout(result, node_id="work", timeout=1)
    assert _event_types(result).count("NodeFailed") == 1
    assert _event_types(result).count("NodeCompleted") == 0
    assert _event_types(result).count("RunFailed") == 1
    assert store.invocation_writes == 1
    released.set()
    await asyncio.wait_for(finished.wait(), timeout=1)
    assert store.invocation_writes == 1
    recorded = await store.replay_results("settle-once")
    assert len(recorded) == 1
    stored = await store.load_run("settle-once")
    assert stored is not None
    assert stored.status == "failed"
    await _cancel_swe_tasks()


async def test_handle_deadline_ms_is_min_of_action_and_workflow() -> None:
    seen: dict[str, float | int] = {}

    async def work(_payload: dict[str, object]) -> dict[str, bool]:
        handle = current_invocation()
        assert handle is not None
        seen["deadline_ms"] = handle.deadline_ms
        seen["remaining"] = handle.remaining_seconds(0)
        return {"ok": True}

    action_tighter = await run_workflow(
        _task_flow(timeout_seconds=30),
        {"demo.work": work},
        {},
        resources=WorkflowResources(action_timeouts={"work": 5}),
        clock=FrozenClock(0),
    )
    assert action_tighter.status == "completed"
    assert seen["deadline_ms"] == 5_000
    assert seen["remaining"] == 5.0

    workflow_tighter = await run_workflow(
        _task_flow(timeout_seconds=2),
        {"demo.work": work},
        {},
        resources=WorkflowResources(action_timeouts={"work": 10}),
        clock=FrozenClock(0),
    )
    assert workflow_tighter.status == "completed"
    assert seen["deadline_ms"] == 2_000
    assert seen["remaining"] == 2.0
    assert swe_tasks() == []
