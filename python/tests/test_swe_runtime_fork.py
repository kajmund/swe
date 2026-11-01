"""Parallel fork/join with a barrier, not a sleep."""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("swe_native")

from swe.definition import compile_swd_text
from swe.runtime import run_workflow
from swe_helpers import FIXTURES
from swe_runtime_helpers import compile_runtime

pytestmark = pytest.mark.swe_runtime


def _fork_definition() -> object:
    return compile_runtime(
        {
            "split": {
                "type": "fork",
                "branches": {"left": "task_a", "right": "task_b"},
                "join": "together",
            },
            "task_a": {"type": "task", "handler": "demo.left"},
            "task_b": {"type": "task", "handler": "demo.right"},
            "together": {
                "type": "join",
                "mode": "all",
                "outputs": {
                    "left": "$.branches.left.result",
                    "right": "$.branches.right.result",
                },
                "next": "after",
            },
            "after": {
                "type": "task",
                "handler": "demo.after",
                "input": {
                    "left": "$.together.left",
                    "right": "$.together.right",
                },
                "next": "done",
            },
            "done": {"type": "end", "status": "completed"},
        },
        name="fanout",
        entrypoint="split",
        handlers=frozenset({"demo.left", "demo.right", "demo.after"}),
    )


async def test_fork_join_overlaps_and_maps_results() -> None:
    entered: list[str] = []
    both = asyncio.Event()
    release = asyncio.Event()

    async def left(_payload: dict[str, object]) -> dict[str, str]:
        entered.append("left")
        if len(entered) == 2:
            both.set()
        await release.wait()
        return {"who": "left"}

    async def right(_payload: dict[str, object]) -> dict[str, str]:
        entered.append("right")
        if len(entered) == 2:
            both.set()
        await release.wait()
        return {"who": "right"}

    async def after(payload: dict[str, object]) -> dict[str, object]:
        return payload

    async def unlock() -> None:
        await both.wait()
        release.set()

    result = await asyncio.wait_for(
        asyncio.gather(
            run_workflow(
                _fork_definition(),
                {"demo.left": left, "demo.right": right, "demo.after": after},
                {},
            ),
            unlock(),
        ),
        timeout=2,
    )
    run = result[0]
    assert run.status == "completed"
    assert set(entered) == {"left", "right"}
    assert run.output["together"]["left"] == {"who": "left"}
    assert run.output["together"]["right"] == {"who": "right"}
    assert run.output["after"]["result"] == {
        "left": {"who": "left"},
        "right": {"who": "right"},
    }


async def test_two_runs_are_isolated() -> None:
    compiled = _fork_definition()

    async def left(_payload: dict[str, object]) -> dict[str, str]:
        return {"run": "a"}

    async def right(_payload: dict[str, object]) -> dict[str, str]:
        return {"run": "b"}

    async def after(payload: dict[str, object]) -> dict[str, object]:
        return payload

    first, second = await asyncio.gather(
        run_workflow(
            compiled,
            {"demo.left": left, "demo.right": left, "demo.after": after},
            {},
            run_id="run-a",
        ),
        run_workflow(
            compiled,
            {"demo.left": right, "demo.right": right, "demo.after": after},
            {},
            run_id="run-b",
        ),
    )
    assert first.run_id == "run-a"
    assert second.run_id == "run-b"
    assert first.output["together"]["left"]["run"] == "a"
    assert second.output["together"]["left"]["run"] == "b"
    assert {event["run_id"] for event in first.events} == {"run-a"}
    assert {event["run_id"] for event in second.events} == {"run-b"}


async def test_deterministic_parallel_fixture() -> None:
    compiled = compile_swd_text(
        (FIXTURES / "runtime_parallel.yaml").read_text(encoding="utf-8"),
        handlers=frozenset({"demo.fetch_catalog", "demo.fetch_pricing"}),
    )

    async def catalog(_payload: dict[str, object]) -> dict[str, str]:
        return {"hits": "catalog"}

    async def pricing(_payload: dict[str, object]) -> dict[str, str]:
        return {"hits": "pricing"}

    result = await run_workflow(
        compiled,
        {"demo.fetch_catalog": catalog, "demo.fetch_pricing": pricing},
        {},
    )
    assert result.status == "completed"
    assert result.output["combine"]["catalog"] == {"hits": "catalog"}
    assert result.output["combine"]["pricing"] == {"hits": "pricing"}


async def test_nested_fork_and_loop_back_to_same_fork() -> None:
    compiled = compile_runtime(
        {
            "outer": {
                "type": "fork",
                "branches": {"left": "inner", "right": "task_c"},
                "join": "outer_join",
            },
            "inner": {
                "type": "fork",
                "branches": {"a": "task_a", "b": "task_b"},
                "join": "inner_join",
            },
            "task_a": {"type": "task", "handler": "demo.a"},
            "task_b": {"type": "task", "handler": "demo.b"},
            "task_c": {"type": "task", "handler": "demo.c"},
            "inner_join": {"type": "join", "mode": "all"},
            "outer_join": {"type": "join", "mode": "all", "next": "again"},
            "again": {
                "type": "decision",
                "evaluator": "expression",
                "condition": {"lt": [{"var": "$.run.steps_used"}, {"const": 12}]},
                "routes": {"true": "outer", "false": "done"},
            },
            "done": {"type": "end", "status": "completed"},
        },
        name="nested_loop",
        entrypoint="outer",
        limits={"max_steps": 40, "max_depth": 4, "timeout_seconds": 30},
        handlers=frozenset({"demo.a", "demo.b", "demo.c"}),
    )

    async def stamp(payload: dict[str, object]) -> dict[str, object]:
        return {"ok": True, "payload": dict(payload)}

    result = await run_workflow(
        compiled,
        {"demo.a": stamp, "demo.b": stamp, "demo.c": stamp},
        {},
    )
    assert result.status == "completed"
    spawned = [event for event in result.events if event["type"] == "BranchSpawned"]
    assert len(spawned) >= 6
    epochs = {
        event["lineage"]["loop_epoch"]
        for event in result.events
        if event.get("lineage") is not None
    }
    assert 0 in epochs
    assert 1 in epochs
