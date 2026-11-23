"""Offline cancellation/orphan experiment.

This is a characterization suite, not a runtime fix. It pins the observable
boundary of the current cooperative cancellation contract:

- a handler that swallows ``CancelledError`` can still perform an external side
  effect after the run is terminal;
- SWE reaps the run without waiting for that handler and never routes or
  persists its late result;
- resume does not re-invoke a persisted run, but a new run is a new logical
  delivery and can repeat the side effect;
- redundant work can be stopped early only by host cooperation (a shared
  ``InvocationHandle.cancel`` signal or a winner event), not by a SWE
  per-branch cancel API.

No paid inference and no network calls are used.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping
from typing import Any

import pytest

pytest.importorskip("swe_native")

from swe import current_invocation
from swe.run_store import MemoryRunStore, WorkflowResources
from swe.runtime import WorkflowRunner
from swe_runtime_helpers import compile_runtime, swe_tasks

pytestmark = pytest.mark.swe_runtime


def _task_flow() -> object:
    return compile_runtime(
        {
            "work": {"type": "task", "handler": "demo.work", "next": "done"},
            "done": {"type": "end", "status": "completed"},
        },
        name="cancel_experiment",
        entrypoint="work",
        limits={"max_steps": 80, "max_depth": 4, "timeout_seconds": 30},
        handlers=frozenset({"demo.work"}),
    )


def _redundant_flow() -> object:
    return compile_runtime(
        {
            "split": {
                "type": "fork",
                "branches": {"winner": "winner_node", "redundant": "redundant_node"},
                "join": "together",
            },
            "winner_node": {"type": "task", "handler": "demo.winner"},
            "redundant_node": {"type": "task", "handler": "demo.redundant"},
            "together": {
                "type": "join",
                "mode": "all",
                "outputs": {
                    "winner": "$.branches.winner.result",
                    "redundant": "$.branches.redundant.result",
                },
                "next": "done",
            },
            "done": {"type": "end", "status": "completed"},
        },
        name="redundant_work",
        entrypoint="split",
        limits={"max_steps": 80, "max_depth": 4, "timeout_seconds": 30},
        handlers=frozenset({"demo.winner", "demo.redundant"}),
    )


async def _cancel_swe_tasks() -> None:
    leftover = swe_tasks()
    for task in leftover:
        task.cancel()
    if leftover:
        await asyncio.gather(*leftover, return_exceptions=True)


def _event_types(result: object) -> list[str]:
    return [event["type"] for event in result.events]


@pytest.mark.asyncio
async def test_cancel_resistant_effect_is_reaped_and_never_persisted() -> None:
    """The run returns while the handler still runs; its late effect is visible.

    SWE guarantees that the late result does not route or persist. It does not
    force-stop the handler, so a host-visible side effect can still happen
    after the run is terminal.
    """

    effects: list[str] = []
    started = asyncio.Event()
    cancel = asyncio.Event()
    release = asyncio.Event()
    finished = asyncio.Event()
    cancelled_at: list[float] = []

    async def stubborn(_payload: Mapping[str, object]) -> dict[str, str]:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await release.wait()
            effects.append("late-side-effect")
            finished.set()
            return {"ok": "late"}
        return {"ok": "no"}

    async def trip() -> None:
        await started.wait()
        cancel.set()
        cancelled_at.append(time.perf_counter())

    store = MemoryRunStore()
    runner = WorkflowRunner(
        _task_flow(),
        {"demo.work": stubborn},
        resources=WorkflowResources(cancel_event=cancel),
        run_store=store,
    )
    result, _ = await asyncio.gather(
        asyncio.wait_for(runner.run({}, run_id="orphan-late"), timeout=5),
        trip(),
    )
    elapsed = time.perf_counter() - cancelled_at[0]
    print(f"orphan reap: run_returned_after_cancel_ms={elapsed * 1000:.1f}")

    assert result.status == "cancelled"
    assert result.error is None or result.error.get("kind") != "timeout"
    assert "work" not in result.output
    assert _event_types(result).count("NodeCompleted") == 0
    assert _event_types(result).count("RunCompleted") == 0

    # The handler is still blocked on release; the runner returned anyway.
    assert effects == []
    assert elapsed < 0.5
    assert runner.abandoned_count == 1
    assert await store.replay_results("orphan-late") == {}

    release.set()
    await asyncio.wait_for(finished.wait(), timeout=2)
    await asyncio.sleep(0)

    # The side effect exists, but SWE never persisted or routed the late value.
    assert effects == ["late-side-effect"]
    assert runner.abandoned_count == 0
    assert await store.replay_results("orphan-late") == {}
    assert _event_types(result).count("NodeCompleted") == 0
    await _cancel_swe_tasks()
    assert swe_tasks() == []


@pytest.mark.asyncio
async def test_resume_is_idempotent_but_a_new_run_repeats_the_side_effect() -> None:
    """SWE's exactly-once is per run/invocation, not global.

    Resume of a persisted terminal run does not invoke the handler. A new run,
    even with the same input, is a new logical delivery: the host must supply
    its own delivery key to prevent a second external effect.
    """

    effects: list[int] = []
    calls: list[str] = []

    async def work(_payload: Mapping[str, object]) -> dict[str, bool]:
        calls.append("run")
        effects.append(len(effects) + 1)
        await asyncio.sleep(0)
        return {"ok": True}

    compiled = _task_flow()
    store = MemoryRunStore()

    first = await WorkflowRunner(
        compiled, {"demo.work": work}, run_store=store
    ).run({}, run_id="idem-1")
    assert first.status == "completed"
    assert calls == ["run"]
    assert effects == [1]

    resumed = await WorkflowRunner(
        compiled, {"demo.work": work}, run_store=store
    ).resume("idem-1")
    assert resumed.status == "completed"
    assert calls == ["run"]
    assert effects == [1]

    # New run id = new delivery. SWE does not deduplicate across runs.
    second = await WorkflowRunner(
        compiled, {"demo.work": work}, run_store=store
    ).run({}, run_id="idem-2")
    assert second.status == "completed"
    assert calls == ["run", "run"]
    assert effects == [1, 2]


async def _run_redundant(*, abort_loser: bool) -> tuple[object, list[str], float]:
    """Run a two-branch fan-out; optionally stop the redundant branch early.

    The winner signals the redundant invocation through its public
    ``InvocationHandle.cancel`` Event. The redundant handler observes the
    signal and returns before doing expensive work. This is host cooperation,
    not a SWE per-branch cancel: the runtime still sees two successful tasks.
    """

    work: list[str] = []
    winner = asyncio.Event()
    ready = asyncio.Event()
    gate = asyncio.Event()
    loser_handle: list[Any] = []

    async def win(_payload: Mapping[str, object]) -> dict[str, bool]:
        await ready.wait()
        if abort_loser:
            assert loser_handle
            loser_handle[0].cancel.set()
        winner.set()
        gate.set()
        work.append("winner")
        return {"winner": True}

    async def redundant(_payload: Mapping[str, object]) -> dict[str, bool]:
        handle = current_invocation()
        assert handle is not None
        loser_handle.append(handle)
        ready.set()
        await winner.wait()
        await gate.wait()
        if handle.cancel.is_set():
            assert handle.reason() is None  # host signal, not a runtime cancel
            return {"winner": False, "aborted": True}
        for _ in range(100):
            await asyncio.sleep(0.001)
        work.append("redundant")
        return {"winner": False, "aborted": False}

    started = time.perf_counter()
    result = await asyncio.wait_for(
        WorkflowRunner(
            _redundant_flow(),
            {"demo.winner": win, "demo.redundant": redundant},
            concurrency=2,
        ).run({}, run_id="redundant-abort" if abort_loser else "redundant-full"),
        timeout=10,
    )
    elapsed = time.perf_counter() - started
    return result, work, elapsed


@pytest.mark.asyncio
async def test_winner_signal_aborts_redundant_work_early() -> None:
    aborted, aborted_work, aborted_elapsed = await _run_redundant(abort_loser=True)
    full, full_work, full_elapsed = await _run_redundant(abort_loser=False)

    assert aborted.status == "completed"
    assert full.status == "completed"
    assert aborted.output["together"]["redundant"]["aborted"] is True
    assert full.output["together"]["redundant"]["aborted"] is False

    # Work is measured by which branch reached its expensive loop.
    assert aborted_work == ["winner"]
    assert full_work == ["winner", "redundant"]
    assert aborted_elapsed < full_elapsed
    print(
        "redundant work: "
        f"aborted_ms={aborted_elapsed * 1000:.1f} "
        f"full_ms={full_elapsed * 1000:.1f} "
        f"aborted_work={aborted_work} full_work={full_work}"
    )

    # The losing invocation still completed successfully from SWE's view.
    assert _event_types(aborted).count("NodeCompleted") >= 2
    assert _event_types(aborted).count("RunCompleted") == 1
    await _cancel_swe_tasks()
    assert swe_tasks() == []
