"""SWD definitions for the real-handler experiment."""

from __future__ import annotations

from collections import defaultdict, deque
from typing import Iterable

from swe.definition import CompiledDefinition, compile_definition
from swe.microtasks import Microtask, compile_microtasks

from .coordinator import TaskSpec


def _ordered(specs: Iterable[TaskSpec]) -> tuple[TaskSpec, ...]:
    items = tuple(specs)
    order = {spec.task_id: index for index, spec in enumerate(items)}
    indegree = {spec.task_id: len(spec.depends_on) for spec in items}
    dependents: dict[str, list[str]] = defaultdict(list)
    for spec in items:
        for dependency in spec.depends_on:
            dependents[dependency].append(spec.task_id)
    ready = deque(
        spec.task_id for spec in items if indegree[spec.task_id] == 0
    )
    result: list[TaskSpec] = []
    by_id = {spec.task_id: spec for spec in items}
    while ready:
        task_id = ready.popleft()
        result.append(by_id[task_id])
        for dependent in dependents[task_id]:
            indegree[dependent] -= 1
            if indegree[dependent] == 0:
                ready.append(dependent)
    if len(result) != len(items):
        raise ValueError("dependency cycle")
    return tuple(result)


def ready_flow(
    specs: Iterable[TaskSpec],
    concurrency: int,
) -> tuple[CompiledDefinition, dict[str, object]]:
    ordered = _ordered(specs)
    items = [
        {
            "task_id": spec.task_id,
            "duration_s": spec.duration_s,
            "depends_on": list(spec.depends_on),
        }
        for spec in ordered
    ]
    document = {
        "version": "1.0",
        "name": "host_ready_queue",
        "entrypoint": "run",
        "limits": {
            "max_steps": len(items) + 3,
            "max_depth": 2,
            "timeout_seconds": 30,
        },
        "nodes": {
            "run": {
                "type": "map",
                "items": "$.input.tasks",
                "child": "work",
                "concurrency": concurrency,
                "next": "done",
            },
            "work": {
                "type": "task",
                "handler": "swe.task",
                "input": {
                    "task_id": "$.item.task_id",
                    "duration_s": "$.item.duration_s",
                    "depends_on": "$.item.depends_on",
                },
            },
            "done": {"type": "end", "status": "completed"},
        },
    }
    compiled = compile_definition(
        document, handlers=frozenset({"swe.task"})
    )
    return compiled, {"tasks": items}


def layer_batch(
    specs: Iterable[TaskSpec],
    concurrency: int,
) -> object:
    return compile_microtasks(
        (
            Microtask(
                spec.task_id,
                {"duration_s": spec.duration_s},
                depends_on=spec.depends_on,
            )
            for spec in specs
        ),
        handler="swe.layer",
        concurrency=concurrency,
        name="host_layer_barrier",
        timeout_seconds=30,
    )


def worker_pool_flow(
    workers: int,
) -> tuple[CompiledDefinition, dict[str, object]]:
    items = [{"worker": index} for index in range(workers)]
    document = {
        "version": "1.0",
        "name": "host_worker_pool",
        "entrypoint": "run",
        "limits": {
            "max_steps": workers + 3,
            "max_depth": 2,
            "timeout_seconds": 30,
        },
        "nodes": {
            "run": {
                "type": "map",
                "items": "$.input.workers",
                "child": "worker",
                "concurrency": workers,
                "next": "done",
            },
            "worker": {
                "type": "task",
                "handler": "swe.worker",
                "input": {"worker": "$.item.worker"},
            },
            "done": {"type": "end", "status": "completed"},
        },
    }
    compiled = compile_definition(
        document, handlers=frozenset({"swe.worker"})
    )
    return compiled, {"workers": items}


__all__ = [
    "layer_batch",
    "ready_flow",
    "worker_pool_flow",
]
