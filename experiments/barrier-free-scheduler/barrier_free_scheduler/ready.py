"""Event-driven ready-queue scheduler.

A task is admitted only when every dependency has finished. Ready tasks are
served in stable input order (FIFO; no starvation), up to the configured
concurrency. The model is virtual-time, so results are deterministic and
replayable without sleeping.
"""

from __future__ import annotations

import heapq
from typing import Sequence

from .model import (
    Event,
    ScheduleResult,
    Task,
    TaskRun,
    normalize_tasks,
    schedule_result,
)


def schedule_ready(
    tasks: Sequence[Task],
    concurrency: int,
    *,
    cancel_at_ms: int | None = None,
    policy: str = "ready-queue",
) -> ScheduleResult:
    if isinstance(concurrency, bool) or not isinstance(concurrency, int) or concurrency < 1:
        raise ValueError("concurrency must be a positive integer")
    if cancel_at_ms is not None and (
        isinstance(cancel_at_ms, bool)
        or not isinstance(cancel_at_ms, int)
        or cancel_at_ms < 0
    ):
        raise ValueError("cancel_at_ms must be a non-negative integer or None")

    normalized = normalize_tasks(tasks)
    by_id = {task.task_id: task for task in normalized}
    order = {task.task_id: index for index, task in enumerate(normalized)}
    dependents: dict[str, list[str]] = {task.task_id: [] for task in normalized}
    indegree: dict[str, int] = {}
    for task in normalized:
        indegree[task.task_id] = len(task.depends_on)
        for dependency in task.depends_on:
            dependents[dependency].append(task.task_id)

    ready = sorted(
        (task.task_id for task in normalized if indegree[task.task_id] == 0),
        key=order.__getitem__,
    )
    running: list[tuple[int, int, str]] = []
    runs: dict[str, TaskRun] = {}
    events: list[Event] = []
    now = 0
    peak = 0

    def start_ready() -> None:
        nonlocal peak
        while (
            ready
            and len(running) < concurrency
            and (cancel_at_ms is None or now < cancel_at_ms)
        ):
            task_id = ready.pop(0)
            task = by_id[task_id]
            finish = now + task.duration_ms
            heapq.heappush(running, (finish, order[task_id], task_id))
            runs[task_id] = TaskRun(task_id, now, finish)
            events.append(Event(now, "start", task_id))
            peak = max(peak, len(running))

    def cancel_all() -> None:
        nonlocal now
        assert cancel_at_ms is not None
        now = cancel_at_ms
        for _, _, task_id in sorted(running, key=lambda item: item[1]):
            run = runs[task_id]
            runs[task_id] = TaskRun(
                task_id, run.start_ms, cancel_at_ms, cancelled=True
            )
            events.append(Event(cancel_at_ms, "cancel", task_id))
        running.clear()
        for task in normalized:
            if task.task_id not in runs:
                runs[task.task_id] = TaskRun(
                    task.task_id, -1, cancel_at_ms, cancelled=True
                )
                events.append(Event(cancel_at_ms, "cancel", task.task_id))

    start_ready()
    while running or ready:
        if cancel_at_ms is not None and now >= cancel_at_ms:
            cancel_all()
            break
        if cancel_at_ms is not None and running:
            if cancel_at_ms <= running[0][0]:
                cancel_all()
                break
        if not running:
            if not ready:
                break
            if cancel_at_ms is not None and now >= cancel_at_ms:
                cancel_all()
                break
            start_ready()
            continue
        finish, _, task_id = heapq.heappop(running)
        now = finish
        events.append(Event(now, "finish", task_id))
        for dependent in dependents[task_id]:
            indegree[dependent] -= 1
            if indegree[dependent] == 0:
                ready.append(dependent)
        ready.sort(key=order.__getitem__)
        start_ready()

    return schedule_result(
        policy=policy,
        concurrency=concurrency,
        tasks=normalized,
        runs=runs,
        events=events,
        makespan_ms=now,
        peak_concurrency=peak,
    )


__all__ = ["schedule_ready"]
