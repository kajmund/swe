"""Compile static microtask DAGs into ordinary SWD 1.0 map stages.

The microtask layer is an authoring and measurement convenience. It does not
introduce a scheduler or a second workflow format: dependencies are divided
into deterministic topological layers and each layer becomes one existing SWD
``map`` node. The Rust machine therefore keeps ownership of bounded fan-out,
join barriers, cancellation, events, and recovery identity.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import platform
import sys
import time
from collections import defaultdict, deque
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from swe.definition import CompiledDefinition, compile_definition
from swe.run_store import RunStore
from swe.runtime import RunResult, WorkflowResources, WorkflowRunner
from swe.runtime_clock import FrozenClock, WorkflowClock
from swe.runtime_dispatch import Handler
from swe.runtime_errors import SweRuntimeError
from swe.runtime_validate import is_json_value

DEFAULT_CONCURRENCY = 32
DEFAULT_TIMEOUT_SECONDS = 300
MAX_SWD_STEPS = 100_000


class MicrotaskError(SweRuntimeError):
    """Invalid or incomplete microtask batch."""

    def __init__(self, message: str, *, path: str = "") -> None:
        super().__init__(message, kind="microtasks", path=path)


@dataclass(frozen=True, slots=True)
class Microtask:
    """One small operation in a static acyclic dependency graph."""

    task_id: str
    payload: Any = field(default_factory=dict)
    depends_on: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.task_id, str) or not self.task_id.strip():
            raise MicrotaskError("task_id must be a non-empty string", path="task_id")
        if "\x00" in self.task_id:
            raise MicrotaskError("task_id cannot contain NUL", path="task_id")
        if not is_json_value(self.payload):
            raise MicrotaskError("payload must be a finite JSON value", path="payload")
        if len(self.depends_on) != len(set(self.depends_on)):
            raise MicrotaskError(
                "depends_on contains duplicate task ids", path="depends_on"
            )
        if any(not isinstance(item, str) or not item for item in self.depends_on):
            raise MicrotaskError(
                "depends_on entries must be non-empty strings", path="depends_on"
            )
        if self.task_id in self.depends_on:
            raise MicrotaskError("a task cannot depend on itself", path="depends_on")

    @classmethod
    def from_value(cls, value: Microtask | Mapping[str, Any]) -> Microtask:
        if isinstance(value, cls):
            return value
        if not isinstance(value, Mapping):
            raise MicrotaskError("each microtask must be an object")
        unknown = set(value) - {"task_id", "payload", "depends_on"}
        if unknown:
            raise MicrotaskError(
                f"unknown microtask fields: {', '.join(sorted(unknown))}"
            )
        task_id = value.get("task_id")
        depends_on = value.get("depends_on", ())
        if isinstance(depends_on, str) or not isinstance(depends_on, Sequence):
            raise MicrotaskError(
                "depends_on must be an array of task ids", path="depends_on"
            )
        return cls(
            task_id=task_id,  # type: ignore[arg-type]
            payload=value.get("payload", {}),
            depends_on=tuple(depends_on),
        )


@dataclass(frozen=True, slots=True)
class MicrotaskBatch:
    """A compiled SWD 1.0 workflow plus its deterministic layer input."""

    compiled: CompiledDefinition
    input_data: Mapping[str, Any]
    layers: tuple[tuple[str, ...], ...]
    map_nodes: tuple[str, ...]
    task_count: int
    handler_name: str
    concurrency: int
    timeout_seconds: int

    async def run(
        self,
        handler: Handler,
        *,
        resources: WorkflowResources | None = None,
        run_store: RunStore | None = None,
        clock: WorkflowClock | FrozenClock | None = None,
        run_id: str | None = None,
    ) -> RunResult:
        runner = WorkflowRunner(
            self.compiled,
            {self.handler_name: handler},
            resources=resources,
            run_store=run_store,
            clock=clock,
            concurrency=self.concurrency,
        )
        return await runner.run(self.input_data, run_id=run_id)

    async def resume(
        self,
        handler: Handler,
        run_id: str,
        *,
        resources: WorkflowResources | None = None,
        run_store: RunStore,
        clock: WorkflowClock | FrozenClock | None = None,
    ) -> RunResult:
        runner = WorkflowRunner(
            self.compiled,
            {self.handler_name: handler},
            resources=resources,
            run_store=run_store,
            clock=clock,
            concurrency=self.concurrency,
        )
        return await runner.resume(run_id)

    def collect_results(self, run: RunResult) -> tuple[Any, ...]:
        """Return task results in topological-layer and input order."""
        if not isinstance(run.output, Mapping):
            raise MicrotaskError("completed run did not return an object output")
        rows: list[Any] = []
        for node_id, expected_ids in zip(self.map_nodes, self.layers, strict=True):
            output = run.output.get(node_id)
            if not isinstance(output, Mapping):
                raise MicrotaskError(
                    f"missing map output for {node_id!r}", path=node_id
                )
            layer_rows = output.get("result")
            if not isinstance(layer_rows, list) or len(layer_rows) != len(expected_ids):
                raise MicrotaskError(
                    f"map output for {node_id!r} has the wrong result count",
                    path=node_id,
                )
            rows.extend(layer_rows)
        return tuple(rows)


def compile_microtasks(
    tasks: Iterable[Microtask | Mapping[str, Any]],
    *,
    handler: str,
    concurrency: int = DEFAULT_CONCURRENCY,
    name: str = "microtask_batch",
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
) -> MicrotaskBatch:
    """Compile a static DAG to existing SWD map stages without a wire change."""
    if not isinstance(handler, str) or not handler:
        raise MicrotaskError("handler must be a non-empty string", path="handler")
    if isinstance(concurrency, bool) or not isinstance(concurrency, int):
        raise MicrotaskError("concurrency must be an integer", path="concurrency")
    if not 1 <= concurrency <= 10_000:
        raise MicrotaskError(
            "concurrency must be between 1 and 10000", path="concurrency"
        )
    if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, int):
        raise MicrotaskError(
            "timeout_seconds must be an integer", path="timeout_seconds"
        )
    if not 1 <= timeout_seconds <= 86_400:
        raise MicrotaskError(
            "timeout_seconds must be between 1 and 86400", path="timeout_seconds"
        )

    normalized = tuple(Microtask.from_value(value) for value in tasks)
    by_id: dict[str, Microtask] = {}
    for task in normalized:
        if task.task_id in by_id:
            raise MicrotaskError(
                f"duplicate task_id {task.task_id!r}", path=task.task_id
            )
        by_id[task.task_id] = task
    for task in normalized:
        missing = [
            dependency for dependency in task.depends_on if dependency not in by_id
        ]
        if missing:
            raise MicrotaskError(
                f"task {task.task_id!r} depends on unknown tasks: {', '.join(missing)}",
                path=task.task_id,
            )

    layers = _topological_layers(normalized)
    if not layers:
        layers = ((),)
    max_steps = len(normalized) + len(layers) + 1
    if max_steps > MAX_SWD_STEPS:
        raise MicrotaskError(
            f"batch needs {max_steps} SWD steps, above the frozen maximum {MAX_SWD_STEPS}"
        )

    nodes: dict[str, Any] = {}
    map_nodes: list[str] = []
    for index, _layer in enumerate(layers):
        map_node = f"microtasks_{index}"
        child_node = f"microtask_{index}"
        map_nodes.append(map_node)
        next_node = f"microtasks_{index + 1}" if index + 1 < len(layers) else "done"
        nodes[map_node] = {
            "type": "map",
            "items": f"$.input.layers.{index}",
            "child": child_node,
            "concurrency": concurrency,
            "next": next_node,
        }
        nodes[child_node] = {
            "type": "task",
            "handler": handler,
            "input": {
                "task_id": "$.item.task_id",
                "payload": "$.item.payload",
                "depends_on": "$.item.depends_on",
            },
        }
    nodes["done"] = {"type": "end", "status": "completed"}

    document = {
        "version": "1.0",
        "name": name,
        "entrypoint": map_nodes[0],
        "limits": {
            "max_steps": max_steps,
            "max_depth": 2,
            "timeout_seconds": timeout_seconds,
        },
        "nodes": nodes,
    }
    compiled = compile_definition(document, handlers=frozenset({handler}))
    layer_payloads = [
        [
            {
                "task_id": by_id[task_id].task_id,
                "payload": by_id[task_id].payload,
                "depends_on": list(by_id[task_id].depends_on),
            }
            for task_id in layer
        ]
        for layer in layers
    ]
    return MicrotaskBatch(
        compiled=compiled,
        input_data={"layers": layer_payloads},
        layers=layers,
        map_nodes=tuple(map_nodes),
        task_count=len(normalized),
        handler_name=handler,
        concurrency=concurrency,
        timeout_seconds=timeout_seconds,
    )


def _topological_layers(tasks: Sequence[Microtask]) -> tuple[tuple[str, ...], ...]:
    if not tasks:
        return ()
    order = {task.task_id: index for index, task in enumerate(tasks)}
    indegree = {task.task_id: len(task.depends_on) for task in tasks}
    dependents: dict[str, list[str]] = defaultdict(list)
    for task in tasks:
        for dependency in task.depends_on:
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
        cycle = sorted(task_id for task_id, degree in indegree.items() if degree > 0)
        raise MicrotaskError(f"dependency cycle detected among: {', '.join(cycle)}")
    return tuple(layers)


class MicrotaskMeter:
    """Wrap a handler and measure concurrency plus end-to-end task throughput."""

    def __init__(self, handler: Handler) -> None:
        self.handler = handler
        self.started = 0
        self.finished = 0
        self.succeeded = 0
        self.active = 0
        self.peak_concurrency = 0
        self._durations_ns: list[int] = []
        self._started_ns: int | None = None
        self._finished_ns: int | None = None

    async def __call__(self, payload: Mapping[str, Any]) -> Any:
        now = time.perf_counter_ns()
        if self._started_ns is None:
            self._started_ns = now
        self.started += 1
        self.active += 1
        self.peak_concurrency = max(self.peak_concurrency, self.active)
        try:
            result = await self.handler(payload)
            self.succeeded += 1
            return result
        finally:
            end = time.perf_counter_ns()
            self.active -= 1
            self.finished += 1
            self._durations_ns.append(end - now)
            self._finished_ns = end

    def snapshot(self) -> dict[str, Any]:
        if self.started == 0 or self._started_ns is None or self._finished_ns is None:
            return {
                "started": self.started,
                "finished": self.finished,
                "succeeded": self.succeeded,
                "active": self.active,
                "peak_concurrency": self.peak_concurrency,
                "p50_latency_ms": 0.0,
                "p95_latency_ms": 0.0,
            }
        wall_seconds = max((self._finished_ns - self._started_ns) / 1_000_000_000, 1e-9)
        return {
            "started": self.started,
            "finished": self.finished,
            "succeeded": self.succeeded,
            "active": self.active,
            "peak_concurrency": self.peak_concurrency,
            "tasks_per_second": self.finished / wall_seconds,
            "p50_latency_ms": _percentile_ms(self._durations_ns, 0.50),
            "p95_latency_ms": _percentile_ms(self._durations_ns, 0.95),
        }


@dataclass(frozen=True, slots=True)
class MicrotaskBenchmark:
    tasks: int
    concurrency: int
    wall_ms: float
    throughput_tasks_per_second: float
    peak_concurrency: int
    p50_latency_ms: float
    p95_latency_ms: float
    result_count: int
    unique_result_count: int
    python_version: str
    platform: str
    cpu_count: int | None
    generated_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "tasks": self.tasks,
            "concurrency": self.concurrency,
            "wall_ms": round(self.wall_ms, 3),
            "throughput_tasks_per_second": round(self.throughput_tasks_per_second, 3),
            "peak_concurrency": self.peak_concurrency,
            "p50_latency_ms": round(self.p50_latency_ms, 6),
            "p95_latency_ms": round(self.p95_latency_ms, 6),
            "result_count": self.result_count,
            "unique_result_count": self.unique_result_count,
            "environment": {
                "python": self.python_version,
                "platform": self.platform,
                "cpu_count": self.cpu_count,
            },
            "generated_at": self.generated_at,
        }


async def benchmark_microtasks(
    *,
    tasks: int = 10_000,
    concurrency: int = 64,
    timeout_seconds: int = 300,
    min_throughput: float = 0.0,
) -> MicrotaskBenchmark:
    """Run the synthetic 10k-operation gate without model or network calls."""
    if isinstance(tasks, bool) or not isinstance(tasks, int) or tasks < 1:
        raise MicrotaskError("tasks must be a positive integer", path="tasks")
    batch = compile_microtasks(
        (Microtask(f"micro-{index}", {"ordinal": index}) for index in range(tasks)),
        handler="microtask.synthetic",
        concurrency=concurrency,
        name="microtask_benchmark",
        timeout_seconds=timeout_seconds,
    )

    async def synthetic(payload: Mapping[str, Any]) -> dict[str, Any]:
        await asyncio.sleep(0)
        value = payload["payload"]
        if not isinstance(value, Mapping):
            raise MicrotaskError("synthetic payload must be an object")
        return {"task_id": payload["task_id"], "ordinal": value["ordinal"]}

    meter = MicrotaskMeter(synthetic)
    started = time.perf_counter_ns()
    run = await batch.run(meter)
    wall_ns = time.perf_counter_ns() - started
    if run.status != "completed":
        raise MicrotaskError(
            f"benchmark run ended with {run.status!r}: {run.error or 'no error'}"
        )
    rows = batch.collect_results(run)
    task_ids = {row.get("task_id") for row in rows if isinstance(row, Mapping)}
    if len(rows) != tasks or len(task_ids) != tasks:
        raise MicrotaskError(
            f"quality gate failed: expected {tasks} unique results, got "
            f"{len(rows)} rows and {len(task_ids)} unique task ids"
        )
    metrics = meter.snapshot()
    if metrics["peak_concurrency"] > concurrency:
        raise MicrotaskError(
            f"quality gate failed: peak concurrency {metrics['peak_concurrency']} "
            f"exceeded {concurrency}"
        )
    throughput = tasks / max(wall_ns / 1_000_000_000, 1e-9)
    if throughput < min_throughput:
        raise MicrotaskError(
            f"quality gate failed: {throughput:.3f} tasks/s is below the requested "
            f"{min_throughput:.3f} tasks/s"
        )
    return MicrotaskBenchmark(
        tasks=tasks,
        concurrency=concurrency,
        wall_ms=wall_ns / 1_000_000,
        throughput_tasks_per_second=throughput,
        peak_concurrency=int(metrics["peak_concurrency"]),
        p50_latency_ms=float(metrics["p50_latency_ms"]),
        p95_latency_ms=float(metrics["p95_latency_ms"]),
        result_count=len(rows),
        unique_result_count=len(task_ids),
        python_version=platform.python_version(),
        platform=platform.platform(),
        cpu_count=os.cpu_count(),
        generated_at=datetime.now(UTC).isoformat(),
    )


def benchmark_main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the SWE microtask quality gate")
    parser.add_argument("--tasks", type=int, default=10_000)
    parser.add_argument("--concurrency", type=int, default=64)
    parser.add_argument("--timeout-seconds", type=int, default=300)
    parser.add_argument("--min-throughput", type=float, default=0.0)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        report = asyncio.run(
            benchmark_microtasks(
                tasks=args.tasks,
                concurrency=args.concurrency,
                timeout_seconds=args.timeout_seconds,
                min_throughput=args.min_throughput,
            )
        )
    except (MicrotaskError, SweRuntimeError, RuntimeError) as error:
        print(f"microtask benchmark failed: {error}", file=sys.stderr)
        return 1
    payload = json.dumps(report.to_dict(), indent=2, sort_keys=True) + "\n"
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload, encoding="utf-8")
    print(payload, end="")
    return 0


def _percentile_ms(values: Sequence[int], quantile: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, math.ceil(len(ordered) * quantile) - 1)
    return ordered[index] / 1_000_000


__all__ = [
    "DEFAULT_CONCURRENCY",
    "Microtask",
    "MicrotaskBatch",
    "MicrotaskBenchmark",
    "MicrotaskError",
    "MicrotaskMeter",
    "benchmark_main",
    "benchmark_microtasks",
    "compile_microtasks",
]


if __name__ == "__main__":
    raise SystemExit(benchmark_main())
