"""Deterministic virtual-time model for a static dependency DAG.

The experiment compares the current SWE microtask shape (one bounded ``map``
per topological layer, with a barrier between layers) against an event-driven,
work-conserving ready queue. It is a pure simulation: no handlers run, no
network or model calls are made, and no budget code is imported.
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Iterable, Mapping, Sequence


class DagError(ValueError):
    """Invalid task graph."""


@dataclass(frozen=True, slots=True)
class Task:
    task_id: str
    duration_ms: int
    depends_on: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.task_id, str) or not self.task_id:
            raise DagError("task_id must be a non-empty string")
        if (
            isinstance(self.duration_ms, bool)
            or not isinstance(self.duration_ms, int)
            or self.duration_ms < 1
        ):
            raise DagError("duration_ms must be a positive integer")
        if len(self.depends_on) != len(set(self.depends_on)):
            raise DagError(f"duplicate dependency in {self.task_id!r}")
        if self.task_id in self.depends_on:
            raise DagError(f"{self.task_id!r} depends on itself")
        if any(
            not isinstance(item, str) or not item for item in self.depends_on
        ):
            raise DagError("dependencies must be non-empty strings")


@dataclass(frozen=True, slots=True)
class Event:
    at_ms: int
    kind: str  # start | finish | cancel
    task_id: str


@dataclass(frozen=True, slots=True)
class TaskRun:
    task_id: str
    start_ms: int  # -1 when the task never started
    finish_ms: int  # set for completed and cancelled tasks
    cancelled: bool = False


@dataclass(frozen=True, slots=True)
class ScheduleResult:
    policy: str
    concurrency: int
    task_order: tuple[str, ...]
    runs: tuple[TaskRun, ...]
    events: tuple[Event, ...]
    makespan_ms: int
    peak_concurrency: int

    def run_for(self, task_id: str) -> TaskRun:
        for run in self.runs:
            if run.task_id == task_id:
                return run
        raise KeyError(task_id)

    @property
    def cancelled(self) -> tuple[str, ...]:
        return tuple(run.task_id for run in self.runs if run.cancelled)

    def completion_order(self) -> tuple[str, ...]:
        index = {task_id: i for i, task_id in enumerate(self.task_order)}
        completed = [run for run in self.runs if not run.cancelled]
        completed.sort(key=lambda run: (run.finish_ms, index[run.task_id]))
        return tuple(run.task_id for run in completed)


def normalize_tasks(tasks: Iterable[Task]) -> tuple[Task, ...]:
    normalized = tuple(tasks)
    by_id: dict[str, Task] = {}
    for task in normalized:
        if task.task_id in by_id:
            raise DagError(f"duplicate task_id {task.task_id!r}")
        by_id[task.task_id] = task
    for task in normalized:
        missing = [dep for dep in task.depends_on if dep not in by_id]
        if missing:
            raise DagError(
                f"{task.task_id!r} depends on unknown tasks: {', '.join(missing)}"
            )
    topological_layers(normalized)  # cycle check
    return normalized


def topological_layers(
    tasks: Sequence[Task],
) -> tuple[tuple[str, ...], ...]:
    """Stable layers, mirroring swe.microtasks._topological_layers."""

    if not tasks:
        return ()
    order = {task.task_id: index for index, task in enumerate(tasks)}
    indegree = {task.task_id: len(task.depends_on) for task in tasks}
    dependents: dict[str, list[str]] = defaultdict(list)
    for task in tasks:
        for dependency in task.depends_on:
            dependents[dependency].append(task.task_id)
    ready = deque(
        task.task_id for task in tasks if indegree[task.task_id] == 0
    )
    layers: list[tuple[str, ...]] = []
    emitted = 0
    while ready:
        layer = tuple(sorted(ready, key=order.__getitem__))
        ready.clear()
        layers.append(layer)
        emitted += len(layer)
        next_ready: list[str] = []
        for task_id in layer:
            for dependent in dependents[task_id]:
                indegree[dependent] -= 1
                if indegree[dependent] == 0:
                    next_ready.append(dependent)
        ready.extend(sorted(next_ready, key=order.__getitem__))
    if emitted != len(tasks):
        cycle = sorted(
            task_id for task_id, degree in indegree.items() if degree > 0
        )
        raise DagError(f"dependency cycle among: {', '.join(cycle)}")
    return tuple(layers)


def start_delay_total_ms(
    tasks: Sequence[Task], result: ScheduleResult
) -> int:
    """Sum of (start - latest dependency finish) over completed tasks."""

    runs = {run.task_id: run for run in result.runs}
    total = 0
    for task in tasks:
        run = runs[task.task_id]
        if run.cancelled or run.start_ms < 0:
            continue
        ready = max(
            (runs[dep].finish_ms for dep in task.depends_on),
            default=0,
        )
        total += max(0, run.start_ms - ready)
    return total


def utilization(tasks: Sequence[Task], result: ScheduleResult) -> float:
    work = sum(
        run.finish_ms - run.start_ms
        for run in result.runs
        if not run.cancelled and run.start_ms >= 0
    )
    window = result.makespan_ms * result.concurrency
    return work / window if window > 0 else 0.0


def validate_dependencies(
    tasks: Sequence[Task], result: ScheduleResult
) -> None:
    runs = {run.task_id: run for run in result.runs}
    for task in tasks:
        run = runs[task.task_id]
        if run.cancelled or run.start_ms < 0:
            continue
        for dep in task.depends_on:
            dep_run = runs[dep]
            if dep_run.cancelled:
                raise AssertionError(
                    f"{task.task_id!r} started after cancelled dependency {dep!r}"
                )
            if run.start_ms < dep_run.finish_ms:
                raise AssertionError(
                    f"{task.task_id!r} started at {run.start_ms} before "
                    f"{dep!r} finished at {dep_run.finish_ms}"
                )


def schedule_result(
    *,
    policy: str,
    concurrency: int,
    tasks: Sequence[Task],
    runs: Mapping[str, TaskRun],
    events: Sequence[Event],
    makespan_ms: int,
    peak_concurrency: int,
) -> ScheduleResult:
    order = tuple(task.task_id for task in tasks)
    return ScheduleResult(
        policy=policy,
        concurrency=concurrency,
        task_order=order,
        runs=tuple(runs[task_id] for task_id in order),
        events=tuple(events),
        makespan_ms=makespan_ms,
        peak_concurrency=peak_concurrency,
    )


def running_count(runs: Sequence[TaskRun], at_ms: int) -> int:
    return sum(
        1
        for run in runs
        if run.start_ms >= 0
        and run.start_ms <= at_ms
        and run.finish_ms > at_ms
        and not run.cancelled
    )


__all__ = [
    "DagError",
    "Event",
    "ScheduleResult",
    "Task",
    "TaskRun",
    "normalize_tasks",
    "running_count",
    "schedule_result",
    "start_delay_total_ms",
    "topological_layers",
    "utilization",
    "validate_dependencies",
]
