"""Static microtask DAGs compile onto SWD maps and reuse runtime recovery."""

from __future__ import annotations

import asyncio
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from typing import Any

import pytest

pytest.importorskip("swe_native")

from swe.microtasks import (
    Microtask,
    MicrotaskError,
    MicrotaskMeter,
    benchmark_microtasks,
    compile_microtasks,
)
from swe.run_store import InvocationRecord, RunRecord, RunStore
from swe.runtime import WorkflowResources

pytestmark = pytest.mark.swe_runtime


async def test_dependencies_execute_in_topological_layers() -> None:
    batch = compile_microtasks(
        [
            Microtask("publish", depends_on=("lint", "test")),
            Microtask("lint", {"kind": "lint"}),
            Microtask("test", {"kind": "test"}, depends_on=("cache",)),
            Microtask("cache", {"kind": "cache"}),
        ],
        handler="microtask.work",
        concurrency=4,
        name="microtask_dependencies",
    )
    completed: set[str] = set()

    async def work(payload: Mapping[str, Any]) -> dict[str, str]:
        task_id = str(payload["task_id"])
        dependencies = payload["depends_on"]
        assert isinstance(dependencies, list)
        assert set(dependencies) <= completed
        await asyncio.sleep(0)
        completed.add(task_id)
        return {"task_id": task_id}

    run = await batch.run(work)
    assert run.status == "completed"
    assert batch.layers == (("lint", "cache"), ("test",), ("publish",))
    rows = batch.collect_results(run)
    assert [row["task_id"] for row in rows] == ["lint", "cache", "test", "publish"]


async def test_map_backpressure_never_exceeds_requested_concurrency() -> None:
    batch = compile_microtasks(
        (Microtask(f"task-{index}", index) for index in range(200)),
        handler="microtask.work",
        concurrency=7,
        name="microtask_backpressure",
    )

    async def work(payload: Mapping[str, Any]) -> dict[str, Any]:
        await asyncio.sleep(0.001)
        return {"task_id": payload["task_id"], "value": payload["payload"]}

    meter = MicrotaskMeter(work)
    run = await batch.run(meter)
    assert run.status == "completed"
    metrics = meter.snapshot()
    assert metrics["started"] == 200
    assert metrics["succeeded"] == 200
    assert 1 < metrics["peak_concurrency"] <= 7
    assert len(batch.collect_results(run)) == 200


async def test_cancel_stops_the_bounded_active_window() -> None:
    batch = compile_microtasks(
        (Microtask(f"task-{index}") for index in range(100)),
        handler="microtask.wait",
        concurrency=5,
        name="microtask_cancel",
    )
    cancel = asyncio.Event()
    started = asyncio.Event()
    started_count = 0
    cancelled_count = 0

    async def wait(_payload: Mapping[str, Any]) -> dict[str, bool]:
        nonlocal started_count, cancelled_count
        started_count += 1
        if started_count >= 5:
            started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled_count += 1
            raise
        return {"ok": False}

    async def trip() -> None:
        await started.wait()
        cancel.set()

    run, _ = await asyncio.gather(
        batch.run(wait, resources=WorkflowResources(cancel_event=cancel)),
        trip(),
    )
    assert run.status == "cancelled"
    assert started_count <= 5
    assert cancelled_count == started_count


async def test_resume_replays_persisted_microtask_results() -> None:
    batch = compile_microtasks(
        (Microtask(f"task-{index}") for index in range(60)),
        handler="microtask.work",
        concurrency=4,
        name="microtask_resume",
    )
    store = _CrashOnceStore(crash_after=7)
    executions: list[str] = []

    async def work(payload: Mapping[str, Any]) -> dict[str, str]:
        task_id = str(payload["task_id"])
        executions.append(task_id)
        await asyncio.sleep(0)
        return {"task_id": task_id}

    with pytest.raises(RuntimeError, match="simulated crash"):
        await batch.run(work, run_store=store, run_id="resume-run")
    persisted = {
        str(record.value["task_id"])
        for record in store.invocations["resume-run"].values()
        if record.ok and isinstance(record.value, Mapping)
    }
    assert len(persisted) == 7

    resumed = await batch.resume(
        work,
        "resume-run",
        run_store=store,
    )
    assert resumed.status == "completed"
    rows = batch.collect_results(resumed)
    assert {str(row["task_id"]) for row in rows} == {
        f"task-{index}" for index in range(60)
    }
    counts = Counter(executions)
    assert all(counts[task_id] == 1 for task_id in persisted)


async def test_cycle_is_rejected_before_compilation() -> None:
    with pytest.raises(MicrotaskError, match="cycle"):
        compile_microtasks(
            [
                Microtask("a", depends_on=("b",)),
                Microtask("b", depends_on=("a",)),
            ],
            handler="microtask.work",
        )


async def test_unknown_dependency_is_rejected() -> None:
    with pytest.raises(MicrotaskError, match="unknown tasks"):
        compile_microtasks(
            [Microtask("a", depends_on=("missing",))],
            handler="microtask.work",
        )


async def test_empty_batch_uses_the_same_runtime() -> None:
    batch = compile_microtasks([], handler="microtask.work", concurrency=3)

    async def work(_payload: Mapping[str, Any]) -> dict[str, bool]:
        raise AssertionError("no task should run for an empty batch")

    run = await batch.run(work)
    assert run.status == "completed"
    assert batch.collect_results(run) == ()


async def test_ten_thousand_synthetic_microtasks_quality_gate() -> None:
    report = await benchmark_microtasks(
        tasks=10_000,
        concurrency=64,
        timeout_seconds=300,
        min_throughput=100.0,
    )
    assert report.result_count == 10_000
    assert report.unique_result_count == 10_000
    assert 1 < report.peak_concurrency <= 64
    assert report.throughput_tasks_per_second >= 100.0


class _CrashOnceStore(RunStore):
    """Persist a transition, then fail once to emulate a process crash."""

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
            if (
                not self.crashed
                and len(self.invocations[run.run_id]) >= self.crash_after
            ):
                self.crashed = True
                raise RuntimeError("simulated crash")

    async def load_run(self, run_id: str) -> RunRecord | None:
        return self.runs.get(run_id)

    async def replay_results(self, run_id: str) -> Mapping[str, InvocationRecord]:
        return dict(self.invocations[run_id])

    async def events(self, run_id: str) -> list[dict[str, Any]]:
        return list(self.event_log[run_id])
