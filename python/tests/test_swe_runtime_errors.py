"""Typed terminal errors: handler, cancel, timeout, limits, completions."""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("swe_native")

from swe.runtime import WorkflowResources, run_workflow, require_native
from swe.runtime_clock import FrozenClock
from swe.runtime_errors import SwePreflightError
from swe.runtime_thaw import thaw_json
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
        limits={"max_steps": 8, "max_depth": 1, "timeout_seconds": 1},
        handlers=frozenset({"demo.work"}),
    )


async def test_unregistered_handler_is_preflight() -> None:
    compiled = _task_flow()
    with pytest.raises(SwePreflightError, match="unregistered handler"):
        await run_workflow(compiled, {}, {})


async def test_non_json_output_fails_the_run() -> None:
    async def bad(_payload: dict[str, object]) -> object:
        return object()

    result = await run_workflow(_task_flow(), {"demo.work": bad}, {})
    assert result.status == "failed"
    assert result.error is not None
    assert "JSON-serializable" in result.error["message"]


async def test_task_exception_fails_and_cancels_siblings() -> None:
    compiled = compile_runtime(
        {
            "split": {
                "type": "fork",
                "branches": {"left": "boom", "right": "slow"},
                "join": "together",
            },
            "boom": {"type": "task", "handler": "demo.boom"},
            "slow": {"type": "task", "handler": "demo.slow"},
            "together": {"type": "join", "mode": "all", "next": "done"},
            "done": {"type": "end", "status": "completed"},
        },
        name="failjoin",
        entrypoint="split",
        handlers=frozenset({"demo.boom", "demo.slow"}),
    )
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def boom(_payload: dict[str, object]) -> dict[str, str]:
        raise RuntimeError("need failed")

    async def slow(_payload: dict[str, object]) -> dict[str, str]:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return {"ok": "no"}

    result = await run_workflow(
        compiled,
        {"demo.boom": boom, "demo.slow": slow},
        {},
    )
    assert result.status == "failed"
    await asyncio.wait_for(cancelled.wait(), timeout=1)
    assert swe_tasks() == []


async def test_cancellation_does_not_reroute() -> None:
    started = asyncio.Event()
    cancel = asyncio.Event()

    async def work(_payload: dict[str, object]) -> dict[str, str]:
        started.set()
        await asyncio.Event().wait()
        return {"ok": "late"}

    async def trip() -> None:
        await started.wait()
        cancel.set()

    result, _ = await asyncio.gather(
        run_workflow(
            _task_flow(),
            {"demo.work": work},
            {},
            resources=WorkflowResources(cancel_event=cancel),
        ),
        trip(),
    )
    assert result.status == "cancelled"
    assert any(event["type"] == "RunCancelled" for event in result.events)
    assert swe_tasks() == []


async def test_timeout_is_terminal() -> None:
    compiled = _task_flow()
    clock = FrozenClock(0)

    async def work(_payload: dict[str, object]) -> dict[str, str]:
        clock.advance(1000)
        return {"ok": True}

    result = await run_workflow(compiled, {"demo.work": work}, {}, clock=clock)
    assert result.status == "failed"
    assert result.error is not None
    assert "timeout" in result.error["message"]
    assert swe_tasks() == []


async def test_hanging_handler_hits_deadline_and_is_cancelled() -> None:
    cancelled = asyncio.Event()

    async def hang(_payload: dict[str, object]) -> dict[str, str]:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return {"ok": "late"}

    result = await asyncio.wait_for(
        run_workflow(_task_flow(), {"demo.work": hang}, {}),
        timeout=3,
    )
    assert result.status == "failed"
    assert result.error is not None
    assert result.error["kind"] == "timeout"
    assert "timeout" in result.error["message"]
    await asyncio.wait_for(cancelled.wait(), timeout=1)
    assert swe_tasks() == []
    assert not any(event["type"] == "RunCompleted" for event in result.events)


async def test_success_leaves_no_swe_tasks() -> None:
    async def work(_payload: dict[str, object]) -> dict[str, bool]:
        return {"ok": True}

    result = await run_workflow(_task_flow(), {"demo.work": work}, {})
    assert result.status == "completed"
    assert swe_tasks() == []


async def test_ignore_cancel_late_result_does_not_reroute() -> None:
    async def stubborn(_payload: dict[str, object]) -> dict[str, str]:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            return {"ok": "late"}
        return {"ok": "no"}

    result = await asyncio.wait_for(
        run_workflow(_task_flow(), {"demo.work": stubborn}, {}),
        timeout=3,
    )
    assert result.status == "failed"
    assert result.error is not None
    assert "timeout" in result.error["message"]
    assert "work" not in result.output
    assert swe_tasks() == []


async def test_max_depth_is_terminal() -> None:
    compiled = compile_runtime(
        {
            "outer": {
                "type": "fork",
                "branches": {"left": "inner", "right": "leaf"},
                "join": "outer_join",
            },
            "inner": {
                "type": "fork",
                "branches": {"a": "leaf_a", "b": "leaf_b"},
                "join": "inner_join",
            },
            "leaf": {"type": "task", "handler": "demo.leaf"},
            "leaf_a": {"type": "task", "handler": "demo.leaf"},
            "leaf_b": {"type": "task", "handler": "demo.leaf"},
            "inner_join": {"type": "join", "mode": "all"},
            "outer_join": {"type": "join", "mode": "all", "next": "done"},
            "done": {"type": "end", "status": "completed"},
        },
        name="deep",
        entrypoint="outer",
        limits={"max_steps": 20, "max_depth": 1, "timeout_seconds": 5},
        handlers=frozenset({"demo.leaf"}),
    )

    async def leaf(_payload: dict[str, object]) -> dict[str, bool]:
        return {"ok": True}

    result = await run_workflow(compiled, {"demo.leaf": leaf}, {})
    assert result.status == "failed"
    assert result.error is not None
    assert "max_depth" in result.error["message"]


async def test_output_schema_is_enforced() -> None:
    compiled = compile_runtime(
        {
            "work": {
                "type": "task",
                "handler": "demo.work",
                "output_schema": {
                    "type": "object",
                    "required": ["n"],
                    "properties": {"n": {"type": "integer"}},
                    "additionalProperties": False,
                },
                "next": "done",
            },
            "done": {"type": "end", "status": "completed"},
        },
        name="schema",
        entrypoint="work",
        handlers=frozenset({"demo.work"}),
    )

    async def bad(_payload: dict[str, object]) -> dict[str, str]:
        return {"n": "nope"}

    failed = await run_workflow(compiled, {"demo.work": bad}, {})
    assert failed.status == "failed"
    assert failed.error is not None
    assert "output_schema" in failed.error["message"]

    async def good(_payload: dict[str, object]) -> dict[str, int]:
        return {"n": 1}

    ok = await run_workflow(compiled, {"demo.work": good}, {})
    assert ok.status == "completed"
    assert ok.output["work"]["result"] == {"n": 1}


async def test_max_steps_is_terminal() -> None:
    compiled = compile_runtime(
        {
            "loop": {
                "type": "decision",
                "evaluator": "expression",
                "condition": {"const": True},
                "routes": {"true": "loop", "false": "done"},
            },
            "done": {"type": "end", "status": "completed"},
        },
        name="spin",
        entrypoint="loop",
        limits={"max_steps": 5, "max_depth": 1, "timeout_seconds": 5},
    )
    result = await run_workflow(compiled, {}, {})
    assert result.status == "failed"
    assert result.error is not None
    assert "max_steps" in result.error["message"]


async def test_duplicate_and_unknown_completion() -> None:
    compiled = _task_flow()
    native = require_native()
    machine = native.Machine()
    outcome = machine.start(
        thaw_json(compiled.document),
        compiled.sha256,
        "dup",
        {},
        0,
    )
    invocation_id = outcome["effects"][0]["invocation_id"]
    first = machine.complete(invocation_id, {"n": 1}, 1)
    assert first["status"] == "completed"
    again = machine.complete(invocation_id, {"n": 1}, 2)
    assert again["status"] == "completed"
    machine.complete(invocation_id, {"n": 2}, 3)
    error = machine.error()
    assert error is not None
    assert "conflicting" in error["message"]
    fresh = native.Machine()
    fresh.start(thaw_json(compiled.document), compiled.sha256, "unk", {}, 0)
    fresh.complete("missing", {"n": 1}, 1)
    unknown = fresh.error()
    assert unknown is not None
    assert "unknown invocation" in unknown["message"]
