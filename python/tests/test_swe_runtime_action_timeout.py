"""Host-owned per-action timeouts: cancel one invocation without Machine.tick."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from typing import Any

import pytest

pytest.importorskip("swe_native")

from swe.runtime import WorkflowResources, WorkflowRunner, run_workflow
from swe.runtime_clock import FrozenClock
from swe.runtime_errors import SwePreflightError
from swe.run_store import InvocationRecord, MemoryRunStore, RunRecord
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


async def test_cancel_during_action_grace_does_not_hang_runner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import swe.runtime as runtime_mod

    monkeypatch.setattr(runtime_mod, "_ACTION_CANCEL_GRACE_SECONDS", 2.0)
    cancel = asyncio.Event()
    in_grace = asyncio.Event()
    release = asyncio.Event()

    async def never(_payload: dict[str, object]) -> dict[str, str]:
        while not release.is_set():
            try:
                await release.wait()
            except asyncio.CancelledError:
                in_grace.set()
        return {"ok": "late"}

    runner = WorkflowRunner(
        _task_flow(),
        {"demo.work": never},
        resources=WorkflowResources(
            action_timeouts={"work": 1},
            cancel_event=cancel,
        ),
        concurrency=1,
    )

    async def trip() -> None:
        await in_grace.wait()
        cancel.set()

    trip_task = asyncio.create_task(trip())
    try:
        result = await asyncio.wait_for(runner.run({}), timeout=5)
        assert result.status in {"cancelled", "failed"}
        assert runner.abandoned_count == 1
    finally:
        release.set()
        trip_task.cancel()
        await asyncio.gather(trip_task, return_exceptions=True)
        await _cancel_swe_tasks()


async def test_delayed_persist_does_not_timeout_timely_success() -> None:
    class _SlowStore(MemoryRunStore):
        def __init__(self) -> None:
            super().__init__()
            self._settled = 0

        async def record(
            self,
            run: RunRecord,
            *,
            events: Sequence[Mapping[str, Any]] = (),
            invocation: InvocationRecord | None = None,
        ) -> None:
            if invocation is not None:
                self._settled += 1
                if self._settled == 1:
                    await asyncio.sleep(1.2)
            await super().record(run, events=events, invocation=invocation)

    async def left(_payload: dict[str, object]) -> dict[str, str]:
        return {"who": "left"}

    async def right(_payload: dict[str, object]) -> dict[str, str]:
        await asyncio.sleep(0.05)
        return {"who": "right"}

    result = await asyncio.wait_for(
        WorkflowRunner(
            _fork_flow(),
            {"demo.slow": left, "demo.fast": right},
            resources=WorkflowResources(action_timeouts={"slow": 1, "fast": 1}),
            run_store=_SlowStore(),
        ).run({}),
        timeout=5,
    )
    assert result.status == "completed"
    assert result.output["together"]["left"] == {"who": "left"}
    assert result.output["together"]["right"] == {"who": "right"}
    await _cancel_swe_tasks()


async def test_abandoned_work_is_bounded_across_runs() -> None:
    async def never(_payload: dict[str, object]) -> dict[str, str]:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await asyncio.Event().wait()
        return {"ok": "late"}

    runner = WorkflowRunner(
        _task_flow(),
        {"demo.work": never},
        resources=_resources(1),
        concurrency=2,
    )
    first = await asyncio.wait_for(runner.run({}, run_id="abandon-1"), timeout=5)
    _assert_action_timeout(first, node_id="work", timeout=1)
    assert runner.abandoned_count == 1

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


async def test_cooperative_fork_sibling_gets_grace_before_abandon() -> None:
    async def boom(_payload: dict[str, object]) -> dict[str, str]:
        raise RuntimeError("need failed")

    async def hang(_payload: dict[str, object]) -> dict[str, str]:
        await asyncio.Event().wait()
        return {"ok": "no"}

    runner = WorkflowRunner(
        _fork_flow(),
        {"demo.slow": boom, "demo.fast": hang},
        concurrency=1,
    )
    first = await asyncio.wait_for(runner.run({}, run_id="coop-sib-1"), timeout=5)
    assert first.status == "failed"
    assert first.error is not None
    assert first.error["kind"] != "limit"
    assert runner.abandoned_count == 0
    second = await asyncio.wait_for(runner.run({}, run_id="coop-sib-2"), timeout=5)
    assert second.status == "failed"
    assert second.error is not None
    assert second.error["kind"] != "limit"
    assert runner.abandoned_count == 0
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
