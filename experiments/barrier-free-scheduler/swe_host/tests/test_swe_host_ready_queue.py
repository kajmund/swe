"""Real offline handlers through WorkflowRunner on the straggler DAG."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("swe_native")

from barrier_free_scheduler.fixtures import straggler_dag
from swe.run_store import MemoryRunStore, WorkflowResources
from swe.runtime import WorkflowRunner, run_workflow

from swe_host.coordinator import (
    ActiveMeter,
    DynamicCoordinator,
    ReadyQueueCoordinator,
    TaskSpec,
    layer_handler_factory,
)
from swe_host.flows import layer_batch, ready_flow, worker_pool_flow

CONCURRENCY = 4


def _specs() -> tuple[TaskSpec, ...]:
    return tuple(
        TaskSpec(
            task.task_id,
            task.duration_ms / 1000.0,
            task.depends_on,
        )
        for task in straggler_dag()
    )


def test_real_layer_barrier_vs_host_ready_queue() -> None:
    specs = _specs()

    async def case() -> None:
        batch = layer_batch(specs, CONCURRENCY)
        layer_meter = ActiveMeter(layer_handler_factory())
        started = time.perf_counter()
        layer_run = await batch.run(layer_meter)
        layer_wall = time.perf_counter() - started
        assert layer_run.status == "completed"
        assert layer_meter.peak <= CONCURRENCY

        compiled, payload = ready_flow(specs, CONCURRENCY)
        coordinator = ReadyQueueCoordinator(specs)
        meter = ActiveMeter(coordinator.handler)
        started = time.perf_counter()
        ready_run = await run_workflow(
            compiled,
            {"swe.task": meter},
            payload,
            concurrency=CONCURRENCY,
            run_id="ready-real",
        )
        ready_wall = time.perf_counter() - started

        assert ready_run.status == "completed"
        assert set(coordinator.finished) == {spec.task_id for spec in specs}
        assert meter.peak <= CONCURRENCY
        print(
            f"real handlers: layer={layer_wall * 1000:.1f}ms "
            f"ready={ready_wall * 1000:.1f}ms "
            f"layer_peak={layer_meter.peak} ready_peak={meter.peak}"
        )
        assert ready_wall < layer_wall

    asyncio.run(case())


class _CrashOnceStore(MemoryRunStore):
    def __init__(self, crash_after: int) -> None:
        super().__init__()
        self.crash_after = crash_after
        self.crashed = False

    async def record(
        self,
        run,
        *,
        events: Sequence[Mapping[str, Any]] = (),
        invocation=None,
    ) -> None:
        await super().record(run, events=events, invocation=invocation)
        if invocation is not None:
            count = len(self.invocations[run.run_id])
            if not self.crashed and count >= self.crash_after:
                self.crashed = True
                raise RuntimeError("simulated crash")


def test_host_ready_queue_replays_from_run_store() -> None:
    specs = _specs()
    compiled, payload = ready_flow(specs, CONCURRENCY)
    store = _CrashOnceStore(crash_after=5)

    async def case() -> None:
        first = ReadyQueueCoordinator(specs)
        runner = WorkflowRunner(
            compiled,
            {"swe.task": first.handler},
            run_store=store,
            concurrency=CONCURRENCY,
        )
        with pytest.raises(RuntimeError, match="simulated crash"):
            await runner.run(payload, run_id="ready-replay")
        await asyncio.sleep(0)

        records = await store.replay_results("ready-replay")
        assert len(records) >= 5
        second = ReadyQueueCoordinator(specs)
        seeded = second.seed_from_records(records)
        assert seeded >= 1

        resumed = await WorkflowRunner(
            compiled,
            {"swe.task": second.handler},
            run_store=store,
            concurrency=CONCURRENCY,
        ).resume("ready-replay")

        assert resumed.status == "completed"
        assert set(second.finished) == {spec.task_id for spec in specs}
        assert len(second.calls) < len(specs)

    asyncio.run(case())


def test_host_ready_queue_cancellation() -> None:
    specs = tuple(TaskSpec(f"task-{index}", 0.2) for index in range(4))
    compiled, payload = ready_flow(specs, 2)

    async def case() -> None:
        coordinator = ReadyQueueCoordinator(specs)
        cancel = asyncio.Event()
        runner = WorkflowRunner(
            compiled,
            {"swe.task": coordinator.handler},
            resources=WorkflowResources(cancel_event=cancel),
            concurrency=2,
        )
        task = asyncio.create_task(runner.run(payload, run_id="ready-cancel"))
        await asyncio.sleep(0.05)
        cancel.set()
        result = await asyncio.wait_for(task, timeout=5)
        assert result.status == "cancelled"
        assert coordinator.cancelled
        assert coordinator.finished == []

    asyncio.run(case())


def test_dynamic_tasks_use_worker_pool_and_lose_per_task_journal() -> None:
    compiled, payload = worker_pool_flow(2)
    coordinator = DynamicCoordinator(2)
    store = MemoryRunStore()

    async def producer() -> None:
        await coordinator.submit(TaskSpec("slow", 0.08))
        await coordinator.submit(TaskSpec("fast", 0.01))
        await coordinator.submit(
            TaskSpec("after_fast", 0.01, ("fast",))
        )
        await asyncio.sleep(0.02)
        await coordinator.submit(TaskSpec("late", 0.01, ("fast",)))
        await coordinator.close()

    async def case() -> None:
        runner = WorkflowRunner(
            compiled,
            {"swe.worker": coordinator.worker},
            run_store=store,
            concurrency=2,
        )
        result, _ = await asyncio.gather(
            runner.run(payload, run_id="dynamic"), producer()
        )
        assert result.status == "completed"
        assert coordinator.finished == {"slow", "fast", "after_fast", "late"}
        assert coordinator.peak <= 2
        # The map journals two worker invocations, not four task invocations.
        assert len(store.invocations["dynamic"]) == 2

    asyncio.run(case())


def test_swe_host_experiment_has_no_budget_import() -> None:
    root = Path(__file__).resolve().parents[1] / "swe_host"
    text = "\n".join(path.read_text() for path in sorted(root.glob("*.py")))
    assert "swe.budget" not in text
    assert "RuntimeBudget" not in text
