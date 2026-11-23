"""Layer-barrier baseline: one bounded map per topological layer.

This mirrors ``swe.microtasks.compile_microtasks``: the Rust ``map`` node is
work-conserving *inside* one layer (``spawn_map_children`` starts the next child
as soon as one completes), but the next layer is a separate node and starts only
when the previous map's ``remaining`` reaches zero.
"""

from __future__ import annotations

import heapq
from collections import deque
from typing import Sequence

from .model import (
    Event,
    ScheduleResult,
    Task,
    TaskRun,
    normalize_tasks,
    schedule_result,
    topological_layers,
)


def schedule_layers(
    tasks: Sequence[Task],
    concurrency: int,
    *,
    policy: str = "layer-barrier",
) -> ScheduleResult:
    if isinstance(concurrency, bool) or not isinstance(concurrency, int) or concurrency < 1:
        raise ValueError("concurrency must be a positive integer")
    normalized = normalize_tasks(tasks)
    by_id = {task.task_id: task for task in normalized}
    order = {task.task_id: index for index, task in enumerate(normalized)}
    layers = topological_layers(normalized)

    now = 0
    runs: dict[str, TaskRun] = {}
    events: list[Event] = []
    peak = 0

    for layer in layers:
        queue = deque(layer)
        running: list[tuple[int, int, str]] = []
        while queue or running:
            while queue and len(running) < concurrency:
                task_id = queue.popleft()
                task = by_id[task_id]
                finish = now + task.duration_ms
                heapq.heappush(running, (finish, order[task_id], task_id))
                runs[task_id] = TaskRun(task_id, now, finish)
                events.append(Event(now, "start", task_id))
                peak = max(peak, len(running))
            if not running:
                break
            finish, _, task_id = heapq.heappop(running)
            now = finish
            events.append(Event(now, "finish", task_id))
        # The next layer starts only after this layer's map has fully finished.

    return schedule_result(
        policy=policy,
        concurrency=concurrency,
        tasks=normalized,
        runs=runs,
        events=events,
        makespan_ms=now,
        peak_concurrency=peak,
    )


__all__ = ["schedule_layers"]
