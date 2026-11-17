"""Host-owned per-action timeouts: cancel one invocation without Machine.tick."""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("swe_native")

from swe.runtime import WorkflowResources, WorkflowRunner, run_workflow
from swe.runtime_clock import FrozenClock
from swe.runtime_errors import SwePreflightError
from swe.run_store import MemoryRunStore
from swe_runtime_helpers import compile_runtime, swe_tasks

pytestmark = pytest.mark.swe_runtime


def _task_flow() -> object:
    return compile_runtime(
        {
            "work": {"type": "task", "handler": "demo.work", "next": "done"},
            "done": {"type": "end", "status": "completed"},
        },
        name="once",
        entrypoint="work",
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


async def _cancel_swe_tasks() -> None:
    leftover = swe_tasks()
    for task in leftover:
        task.cancel()
    if leftover:
        await asyncio.gather(*leftover, return_exceptions=True)


async def test_hanging_handler_fails_with_action_timeout() -> None:
    cancelled = asyncio.Event()

    async def hang(_payload: dict[str, object]) -> dict[str, str]:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return {"ok": "late"}

    result = await asyncio.wait_for(
        run_workflow(_task_flow(), {"demo.work": hang}, {}, resources=_resources()),
        timeout=5,
    )
    _assert_action_timeout(result, node_id="work", timeout=1)
    await asyncio.wait_for(cancelled.wait(), timeout=1)
    assert swe_tasks() == []


async def test_swallowed_cancel_is_timeout_not_completed() -> None:
    async def stubborn(_payload: dict[str, object]) -> dict[str, str]:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            return {"status": "timeout"}
        return {"ok": "no"}

    result = await asyncio.wait_for(
        run_workflow(_task_flow(), {"demo.work": stubborn}, {}, resources=_resources()),
        timeout=5,
    )
    _assert_action_timeout(result, node_id="work", timeout=1)
    assert "work" not in result.output
    assert swe_tasks() == []


async def test_frozen_clock_late_return_is_timeout() -> None:
    clock = FrozenClock(0)

    async def work(_payload: dict[str, object]) -> dict[str, str]:
        clock.advance(1000)
        return {"ok": True}

    result = await run_workflow(
        _task_flow(),
        {"demo.work": work},
        {},
        resources=_resources(1),
        clock=clock,
    )
    _assert_action_timeout(result, node_id="work", timeout=1)
    assert "work" not in result.output
    assert swe_tasks() == []


async def test_late_result_after_abandon_does_not_route() -> None:
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
        run_workflow(_task_flow(), {"demo.work": stubborn}, {}, resources=_resources()),
        timeout=5,
    )
    _assert_action_timeout(result, node_id="work", timeout=1)
    assert "work" not in result.output
    released.set()
    await asyncio.wait_for(finished.wait(), timeout=1)
    assert not any(event["type"] == "RunCompleted" for event in result.events)
    await _cancel_swe_tasks()
    assert swe_tasks() == []


async def test_never_finishes_after_cancel_returns_within_timeout() -> None:
    async def never(_payload: dict[str, object]) -> dict[str, str]:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await asyncio.Event().wait()
        return {"ok": "late"}

    result = await asyncio.wait_for(
        run_workflow(_task_flow(), {"demo.work": never}, {}, resources=_resources()),
        timeout=5,
    )
    _assert_action_timeout(result, node_id="work", timeout=1)
    await _cancel_swe_tasks()


async def test_parallel_fork_slow_timeout_does_not_join() -> None:
    async def slow(_payload: dict[str, object]) -> dict[str, str]:
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
    assert swe_tasks() == []


async def test_fast_handler_still_succeeds() -> None:
    async def work(_payload: dict[str, object]) -> dict[str, bool]:
        return {"ok": True}

    result = await run_workflow(
        _task_flow(),
        {"demo.work": work},
        {},
        resources=_resources(5),
    )
    assert result.status == "completed"
    assert result.output["work"]["result"] == {"ok": True}
    assert swe_tasks() == []


async def test_journal_resume_does_not_rerun_handler() -> None:
    store = MemoryRunStore()
    calls: list[str] = []

    async def work(_payload: dict[str, object]) -> dict[str, bool]:
        calls.append("run")
        return {"ok": True}

    compiled = _task_flow()
    runner = WorkflowRunner(
        compiled,
        {"demo.work": work},
        resources=_resources(5),
        run_store=store,
    )
    first = await runner.run({}, run_id="action-timeout-resume")
    assert first.status == "completed"
    assert calls == ["run"]

    async def must_not_run(_payload: dict[str, object]) -> dict[str, str]:
        calls.append("resume")
        raise AssertionError("resume must not invoke the handler")

    resumed = await WorkflowRunner(
        compiled,
        {"demo.work": must_not_run},
        resources=_resources(5),
        run_store=store,
    ).resume("action-timeout-resume")
    assert resumed.status == "completed"
    assert calls == ["run"]
    assert swe_tasks() == []


async def test_timeout_journal_resume_stays_failed() -> None:
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
        ).run({}, run_id="action-timeout-fail"),
        timeout=5,
    )
    _assert_action_timeout(first, node_id="work", timeout=1)
    assert calls == ["run"]

    async def must_not_run(_payload: dict[str, object]) -> dict[str, str]:
        calls.append("resume")
        raise AssertionError("resume must not invoke the handler")

    resumed = await WorkflowRunner(
        compiled,
        {"demo.work": must_not_run},
        resources=_resources(1),
        run_store=store,
    ).resume("action-timeout-fail")
    assert resumed.status == "failed"
    assert resumed.error is not None
    assert resumed.error["kind"] == "timeout"
    assert resumed.error["path"] == "nodes.work"
    assert calls == ["run"]
    assert swe_tasks() == []


@pytest.mark.parametrize(
    ("timeouts", "match"),
    [
        ({"work": 0}, "between 1 and 86400"),
        ({"Work": 1}, "invalid action timeout node id"),
    ],
)
def test_invalid_action_timeouts_are_preflight(
    timeouts: dict[str, int],
    match: str,
) -> None:
    with pytest.raises(SwePreflightError, match=match):
        WorkflowRunner(
            _task_flow(),
            {"demo.work": _ok},
            resources=WorkflowResources(action_timeouts=timeouts),
        )


async def _ok(_payload: dict[str, object]) -> dict[str, bool]:
    return {"ok": True}
