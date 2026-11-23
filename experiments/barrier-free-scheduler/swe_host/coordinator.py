"""Host-level coordinators used by the real-handler SWE experiment.

These are experiment adapters, not a production scheduler. They run *inside*
ordinary SWE handlers so the existing ``WorkflowRunner``/journal path is used
without adding a second engine.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Mapping


@dataclass(frozen=True, slots=True)
class TaskSpec:
    task_id: str
    duration_s: float
    depends_on: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.task_id:
            raise ValueError("task_id must be non-empty")
        if self.duration_s < 0:
            raise ValueError("duration_s must be non-negative")


class ActiveMeter:
    """Measure peak in-flight handlers without a second scheduler."""

    def __init__(self, handler) -> None:
        self.handler = handler
        self.active = 0
        self.peak = 0
        self.calls: list[str] = []

    async def __call__(self, payload: Mapping[str, Any]) -> Any:
        self.calls.append(str(payload.get("task_id", "")))
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            return await self.handler(payload)
        finally:
            self.active -= 1


class ReadyQueueCoordinator:
    """Static DAG executed inside one SWE ``map``.

    Every map child waits for its dependencies through host events. The map's
    own concurrency still bounds admission, so a waiting child occupies a slot;
    this is the limitation that motivates a machine-owned ready queue.
    """

    def __init__(self, specs: tuple[TaskSpec, ...]) -> None:
        self.specs = {spec.task_id: spec for spec in specs}
        self.events = {spec.task_id: asyncio.Event() for spec in specs}
        self.started: list[str] = []
        self.finished: list[str] = []
        self.cancelled: list[str] = []
        self.calls: list[str] = []

    def seed_from_records(self, records: Mapping[str, Any]) -> int:
        """Reconstruct completed dependency events from persisted invocations."""

        seeded = 0
        for record in records.values():
            if not getattr(record, "ok", False):
                continue
            value = getattr(record, "value", None)
            if not isinstance(value, Mapping):
                continue
            task_id = value.get("task_id")
            if task_id in self.events and task_id not in self.finished:
                self.events[str(task_id)].set()
                self.finished.append(str(task_id))
                seeded += 1
        return seeded

    async def handler(self, payload: Mapping[str, Any]) -> Mapping[str, str]:
        task_id = str(payload["task_id"])
        spec = self.specs[task_id]
        self.calls.append(task_id)
        if task_id not in self.started:
            self.started.append(task_id)
        try:
            for dependency in spec.depends_on:
                await self.events[dependency].wait()
            await asyncio.sleep(spec.duration_s)
            self.events[task_id].set()
            self.finished.append(task_id)
            return {"task_id": task_id}
        except asyncio.CancelledError:
            self.cancelled.append(task_id)
            raise


class DynamicCoordinator:
    """Worker-pool coordinator for dynamically submitted tasks.

    SWE's map starts a fixed number of workers. Each worker pulls ready tasks
    from this coordinator, so new tasks can arrive while the run is active.
    The journal sees worker invocations, not individual task invocations.
    """

    def __init__(self, workers: int) -> None:
        self.workers = workers
        self.condition = asyncio.Condition()
        self.pending: dict[str, TaskSpec] = {}
        self.running: set[str] = set()
        self.finished: set[str] = set()
        self.cancelled: set[str] = set()
        self.active = 0
        self.peak = 0
        self.closed = False
        self.aborted = False

    async def submit(self, spec: TaskSpec) -> None:
        async with self.condition:
            if self.closed:
                raise RuntimeError("coordinator is closed")
            if spec.task_id in self.pending or spec.task_id in self.finished:
                raise RuntimeError(f"duplicate dynamic task {spec.task_id!r}")
            self.pending[spec.task_id] = spec
            self.condition.notify_all()

    async def close(self) -> None:
        async with self.condition:
            self.closed = True
            self.condition.notify_all()

    async def next_task(self) -> TaskSpec | None:
        async with self.condition:
            while True:
                if self.aborted:
                    return None
                ready = [
                    spec
                    for spec in self.pending.values()
                    if all(dep in self.finished for dep in spec.depends_on)
                ]
                if ready:
                    spec = ready[0]
                    del self.pending[spec.task_id]
                    self.running.add(spec.task_id)
                    self.active += 1
                    self.peak = max(self.peak, self.active)
                    return spec
                if self.closed and not self.pending:
                    return None
                await self.condition.wait()

    async def finish(self, task_id: str) -> None:
        async with self.condition:
            self.running.discard(task_id)
            self.finished.add(task_id)
            self.active = max(0, self.active - 1)
            self.condition.notify_all()

    async def cancel_task(self, task_id: str) -> None:
        async with self.condition:
            self.running.discard(task_id)
            self.active = max(0, self.active - 1)
            self.cancelled.add(task_id)
            self.aborted = True
            self.pending.clear()
            self.condition.notify_all()

    async def worker(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        worker_id = payload.get("worker")
        while True:
            spec = await self.next_task()
            if spec is None:
                return {"worker": worker_id}
            try:
                await asyncio.sleep(spec.duration_s)
                await self.finish(spec.task_id)
            except asyncio.CancelledError:
                await self.cancel_task(spec.task_id)
                raise


def layer_handler_factory() -> Any:
    async def layer_handler(payload: Mapping[str, Any]) -> Mapping[str, Any]:
        task_id = str(payload["task_id"])
        body = payload.get("payload", {})
        duration = float(body.get("duration_s", 0.0))
        await asyncio.sleep(duration)
        return {"task_id": task_id}

    return layer_handler


__all__ = [
    "ActiveMeter",
    "DynamicCoordinator",
    "ReadyQueueCoordinator",
    "TaskSpec",
    "layer_handler_factory",
]
