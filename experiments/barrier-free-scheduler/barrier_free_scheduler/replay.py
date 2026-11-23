"""Validate and replay a schedule from its event log.

The event log is the deterministic record: start, finish, and cancel events at
virtual times. Replay reconstructs the runs and rejects a log that violates
dependency order or the concurrency bound.
"""

from __future__ import annotations

from typing import Sequence

from .model import (
    ScheduleResult,
    Task,
    TaskRun,
    normalize_tasks,
    schedule_result,
    validate_dependencies,
)


def replay_result(tasks: Sequence[Task], result: ScheduleResult) -> ScheduleResult:
    normalized = normalize_tasks(tasks)
    by_id = {task.task_id: task for task in normalized}
    status = {task.task_id: "not_started" for task in normalized}
    start: dict[str, int] = {}
    finish: dict[str, int] = {}
    running = 0
    peak = 0
    last_at = -1

    for event in result.events:
        if event.at_ms < last_at:
            raise AssertionError("event log time went backwards")
        last_at = event.at_ms
        task_id = event.task_id
        if task_id not in by_id:
            raise AssertionError(f"unknown task in event log: {task_id!r}")
        if event.kind == "start":
            if status[task_id] != "not_started":
                raise AssertionError(f"{task_id!r} started twice")
            for dep in by_id[task_id].depends_on:
                if status[dep] != "finished":
                    raise AssertionError(
                        f"{task_id!r} started before dependency {dep!r} finished"
                    )
                if finish[dep] > event.at_ms:
                    raise AssertionError(
                        f"{task_id!r} started before {dep!r} finish time"
                    )
            if running >= result.concurrency:
                raise AssertionError("event log exceeded the concurrency bound")
            status[task_id] = "running"
            start[task_id] = event.at_ms
            running += 1
            peak = max(peak, running)
        elif event.kind == "finish":
            if status[task_id] != "running":
                raise AssertionError(f"{task_id!r} finished without running")
            if event.at_ms < start[task_id]:
                raise AssertionError(f"{task_id!r} finished before it started")
            status[task_id] = "finished"
            finish[task_id] = event.at_ms
            running -= 1
        elif event.kind == "cancel":
            if status[task_id] == "finished":
                raise AssertionError(f"{task_id!r} cancelled after finishing")
            if status[task_id] == "cancelled":
                raise AssertionError(f"{task_id!r} cancelled twice")
            if status[task_id] == "running":
                running -= 1
                start[task_id] = start.get(task_id, -1)
            else:
                start[task_id] = -1
            status[task_id] = "cancelled"
            finish[task_id] = event.at_ms
        else:
            raise AssertionError(f"unknown event kind {event.kind!r}")

    runs: dict[str, TaskRun] = {}
    for task in normalized:
        task_id = task.task_id
        if status[task_id] == "finished":
            runs[task_id] = TaskRun(task_id, start[task_id], finish[task_id])
        elif status[task_id] == "cancelled":
            runs[task_id] = TaskRun(
                task_id, start.get(task_id, -1), finish[task_id], cancelled=True
            )
        else:
            raise AssertionError(f"task {task_id!r} has no terminal event")

    replayed = schedule_result(
        policy=result.policy,
        concurrency=result.concurrency,
        tasks=normalized,
        runs=runs,
        events=result.events,
        makespan_ms=max(finish.values(), default=0),
        peak_concurrency=peak,
    )
    validate_dependencies(normalized, replayed)
    if replayed.makespan_ms != result.makespan_ms:
        raise AssertionError("replayed makespan differs from the recorded result")
    if replayed.peak_concurrency != result.peak_concurrency:
        raise AssertionError("replayed peak concurrency differs")
    return replayed


__all__ = ["replay_result"]
