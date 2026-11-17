"""Host-owned per-action timeouts: cancel one invocation without Machine.tick."""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("swe_native")

from swe.runtime import WorkflowResources, WorkflowRunner, run_workflow
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


def _resources(timeout: int = 1) -> WorkflowResources:
    return WorkflowResources(action_timeouts={"work": timeout})


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
    assert result.status == "failed"
    assert result.error is not None
    assert result.error["kind"] == "timeout"
    assert result.error["path"] == "nodes.work"
    assert result.error["message"] == "action timeout_seconds 1 exceeded"
    await asyncio.wait_for(cancelled.wait(), timeout=1)
    assert swe_tasks() == []
    assert not any(event["type"] == "RunCompleted" for event in result.events)


async def test_handler_that_catches_cancel_completes() -> None:
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
    assert result.status == "completed"
    assert result.output["work"]["result"] == {"status": "timeout"}
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
