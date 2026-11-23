"""Reuse the SWE microtask DAG compiler when it is importable.

The experiment deliberately does not ship a second workflow format. When the
Python package and its ``jsonschema`` dependency are available, a batch of
``TaskSpec`` objects is compiled through ``swe.microtasks.compile_microtasks``.
The returned topological layers are then executed by the local bounded
dispatcher. The fallback implements the same deterministic layering so the
pilot remains executable on a machine without the SWE Python package.
"""

from __future__ import annotations

import sys
from collections import defaultdict, deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

_REPO_PYTHON = Path(__file__).resolve().parents[3] / "python"


@dataclass(frozen=True, slots=True)
class ScheduledTask:
    task_id: str
    payload: Any = None
    depends_on: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class LayerPlan:
    layers: tuple[tuple[str, ...], ...]
    source: str
    fallback_reason: str | None = None


def ensure_repo_python_on_path() -> None:
    if _REPO_PYTHON.is_dir() and str(_REPO_PYTHON) not in sys.path:
        sys.path.insert(0, str(_REPO_PYTHON))


def compile_layers(
    tasks: Sequence[ScheduledTask],
    *,
    concurrency: int,
    handler: str = "swarm.task",
) -> LayerPlan:
    """Compile dependencies through ``swe.microtasks`` or an identical fallback."""

    normalized = tuple(tasks)
    ensure_repo_python_on_path()
    try:
        from swe.microtasks import Microtask, compile_microtasks  # type: ignore

        batch = compile_microtasks(
            [
                Microtask(
                    task_id=task.task_id,
                    payload=task.payload,
                    depends_on=task.depends_on,
                )
                for task in normalized
            ],
            handler=handler,
            concurrency=concurrency,
            name="microtask_swarm",
        )
        return LayerPlan(batch.layers, "swe.microtasks.compile_microtasks")
    except Exception as exc:  # import, dependency, or validation failure
        reason = f"{type(exc).__name__}: {exc}"
    return LayerPlan(
        _topological_layers(normalized),
        "microtask_swarm.dag fallback",
        fallback_reason=reason,
    )


def _topological_layers(
    tasks: Sequence[ScheduledTask],
) -> tuple[tuple[str, ...], ...]:
    if not tasks:
        return ((),)
    order = {task.task_id: index for index, task in enumerate(tasks)}
    if len(order) != len(tasks):
        raise ValueError("duplicate task ids")
    indegree = {task.task_id: len(task.depends_on) for task in tasks}
    dependents: dict[str, list[str]] = defaultdict(list)
    for task in tasks:
        for dependency in task.depends_on:
            if dependency not in order:
                raise ValueError(f"{task.task_id} depends on unknown {dependency}")
            dependents[dependency].append(task.task_id)
    ready = deque(task.task_id for task in tasks if indegree[task.task_id] == 0)
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
        raise ValueError("dependency cycle detected")
    return tuple(layers)


__all__ = [
    "LayerPlan",
    "ScheduledTask",
    "compile_layers",
    "ensure_repo_python_on_path",
]
