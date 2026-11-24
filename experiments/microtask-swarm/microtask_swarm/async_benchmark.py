"""Async scheduling benchmark A/B/C/D for the microtask swarm (offline).

A: one combined large-model call per item.
B: sequential microtasks in dependency order.
C: bounded parallel microtasks with a global barrier per topological layer.
D: event-driven ready queue; a task starts when its own dependencies finish,
   without waiting for unrelated tasks in the same layer.

All strategies use the same frozen fixtures, verifier, B/C/D model, and budget.
Latency is a deterministic trace, not a network measurement. Cancellation and
early stop keep reservations pending; the run reconciles them with a
deterministic late-billing policy.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Mapping, Sequence

from microtask_swarm.budget import BudgetError, SweycoinMeter
from microtask_swarm.contracts import (
    ContractViolation,
    Kind,
    output_schema,
    parse_json_output,
    render_prompt,
    verify_output,
)
from microtask_swarm.cost import account_calls
from microtask_swarm.dag import ScheduledTask, compile_layers
from microtask_swarm.executor import ChatRequest, ProviderResponse
from microtask_swarm.fixtures import (
    GoldSuite,
    TaskSpec,
    all_microtask_specs,
    build_task_specs,
    load_fixtures,
)
from microtask_swarm.offline import OfflineHeuristicProvider
from microtask_swarm.results import CallRecord, TaskResult
from microtask_swarm.scoring import score_suite


class Strategy(StrEnum):
    A_SINGLE_LARGE = "A_single_large"
    B_SEQUENTIAL = "B_sequential"
    C_LAYERED = "C_layered_barrier"
    D_EVENT_READY = "D_event_ready"


_DAG_DEPENDENCIES: dict[str, tuple[str, ...]] = {
    "relevance": (),
    "extraction": ("relevance",),
    "entailment": ("extraction",),
    "atomicity": ("extraction",),
    "operation_selection": ("entailment", "atomicity"),
}


@dataclass(frozen=True, slots=True)
class DagNode:
    task_id: str
    item_id: str
    kind: str
    depends_on: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Dag:
    nodes: Mapping[str, DagNode]
    layers: tuple[tuple[str, ...], ...]
    dependents: Mapping[str, tuple[str, ...]]

    def initial_ready(self) -> tuple[str, ...]:
        return tuple(
            task_id for task_id, node in self.nodes.items() if not node.depends_on
        )

    def longest_path_ms(self, durations: Mapping[str, float]) -> float:
        memo: dict[str, float] = {}

        def longest(task_id: str) -> float:
            if task_id in memo:
                return memo[task_id]
            node = self.nodes[task_id]
            base = float(durations.get(task_id, 0.0))
            value = base + (
                max(longest(dep) for dep in node.depends_on)
                if node.depends_on
                else 0.0
            )
            memo[task_id] = value
            return value

        return max((longest(task_id) for task_id in self.nodes), default=0.0)


def build_dag(specs: Sequence[TaskSpec]) -> Dag:
    by_item: dict[str, dict[str, TaskSpec]] = defaultdict(dict)
    for spec in specs:
        by_item[spec.item_id][str(spec.kind)] = spec
    nodes: dict[str, DagNode] = {}
    for item_id, tasks in sorted(by_item.items()):
        for kind, deps in _DAG_DEPENDENCIES.items():
            if kind not in tasks:
                continue
            task_id = tasks[kind].task_id
            nodes[task_id] = DagNode(
                task_id=task_id,
                item_id=item_id,
                kind=kind,
                depends_on=tuple(tasks[dep].task_id for dep in deps),
            )
    scheduled = [
        ScheduledTask(
            task_id=node.task_id,
            payload={"kind": node.kind},
            depends_on=node.depends_on,
        )
        for node in nodes.values()
    ]
    plan = compile_layers(scheduled, concurrency=1)
    dependents: dict[str, list[str]] = defaultdict(list)
    for node in nodes.values():
        for dep in node.depends_on:
            dependents[dep].append(node.task_id)
    return Dag(
        nodes=nodes,
        layers=plan.layers,
        dependents={
            task_id: tuple(sorted(items)) for task_id, items in dependents.items()
        },
    )


def choose_straggler_items(
    item_ids: Sequence[str], *, count: int, seed: int
) -> frozenset[str]:
    ordered = sorted(
        set(item_ids),
        key=lambda item: hashlib.sha256(
            f"{seed}:straggler:{item}".encode("utf-8")
        ).hexdigest(),
    )
    return frozenset(ordered[: max(0, count)])


@dataclass(frozen=True, slots=True)
class LatencyTrace:
    base_ms: Mapping[str, float]
    model_multiplier: Mapping[str, float]
    jitter_fraction: float
    straggler_items: frozenset[str]
    straggler_extra_ms: float
    seed: int
    scale: float = 1.0
    extra_ms_by_task: Mapping[str, float] = field(default_factory=dict)

    def duration_ms(
        self,
        *,
        task_id: str,
        item_id: str,
        model_id: str,
        kind: str,
    ) -> float:
        base = float(self.base_ms[str(kind)])
        multiplier = float(self.model_multiplier.get(model_id, 1.0))
        digest = hashlib.sha256(
            f"{self.seed}:{task_id}:{model_id}:{kind}".encode("utf-8")
        ).hexdigest()
        jitter = 1.0 + self.jitter_fraction * (
            (int(digest[:8], 16) % 2001) / 1000.0 - 1.0
        )
        extra = float(self.extra_ms_by_task.get(task_id, 0.0))
        if item_id in self.straggler_items:
            extra += self.straggler_extra_ms
        return max(0.05, (base * multiplier * jitter + extra) * self.scale)


class TraceProvider:
    """Deterministic latency around the offline heuristic answer."""

    def __init__(self, inner: OfflineHeuristicProvider, trace: LatencyTrace) -> None:
        self.inner = inner
        self.trace = trace

    async def chat(self, request: ChatRequest) -> ProviderResponse:
        item_id = request.task_id.split(":", 1)[0]
        duration = self.trace.duration_ms(
            task_id=request.task_id,
            item_id=item_id,
            model_id=request.model_id,
            kind=str(request.kind),
        )
        await asyncio.sleep(duration / 1000.0)
        return self.inner.respond(request)


class StopController:
    """Set a stop reason and cancel registered provider tasks."""

    def __init__(self) -> None:
        self.event = asyncio.Event()
        self.reason: str | None = None
        self._tasks: set[asyncio.Task[Any]] = set()

    def register(self, task: asyncio.Task[Any]) -> None:
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def is_stopped(self) -> bool:
        return self.event.is_set()

    def stop(self, reason: str) -> None:
        if self.event.is_set():
            return
        self.reason = reason
        self.event.set()
        current = asyncio.current_task()
        for task in list(self._tasks):
            if task is not current and not task.done():
                task.cancel()


@dataclass(slots=True)
class Attempt:
    task_id: str
    item_id: str
    kind: Kind
    model_id: str
    reservation_id: int | None
    tariff_milli: int
    ready_ns: int
    start_ns: int | None = None
    end_ns: int | None = None
    provider_start_ns: int | None = None
    provider_end_ns: int | None = None
    output: Any = None
    verification: Any = None
    error: str | None = None
    cancelled: bool = False
    provider_started: bool = False
    tokens: dict[str, Any] = field(default_factory=dict)
    billed_usd: str | None = None
    price_period: str | None = None
    late_billed: bool = False

    @property
    def worker_ms(self) -> float:
        if self.start_ns is None or self.end_ns is None:
            return 0.0
        return (self.end_ns - self.start_ns) / 1_000_000.0

    @property
    def provider_ms(self) -> float:
        if self.provider_start_ns is None or self.provider_end_ns is None:
            return 0.0
        return (self.provider_end_ns - self.provider_start_ns) / 1_000_000.0

    @property
    def queue_wait_ms(self) -> float:
        if self.start_ns is None or self.provider_start_ns is None:
            return 0.0
        return (self.provider_start_ns - self.start_ns) / 1_000_000.0

    @property
    def task_latency_ms(self) -> float:
        if self.start_ns is None or self.end_ns is None:
            return 0.0
        return (self.end_ns - self.start_ns) / 1_000_000.0

    @property
    def ready_latency_ms(self) -> float:
        if self.end_ns is None:
            return 0.0
        return (self.end_ns - self.ready_ns) / 1_000_000.0

    def to_call(self) -> dict[str, Any]:
        return {
            "model_id": self.model_id,
            "role": "candidate",
            "attempt": 1,
            "latency_ms": self.provider_ms if self.provider_started else 0.0,
            "tokens": dict(self.tokens)
            if self.tokens
            else {
                "input": None,
                "output": None,
                "reasoning": None,
                "cache_read": None,
                "cache_write": None,
                "source": "provider",
                "cache_read_assumed_miss": False,
            },
            "billed_usd": self.billed_usd,
            "price_period": self.price_period,
        }

    def to_task_result(self) -> TaskResult:
        verification = self.verification
        error = self.error
        if self.cancelled:
            error = "cancelled"
        return TaskResult(
            task_id=self.task_id,
            kind=str(self.kind),
            output=self.output,
            verification=verification,
            error=error,
            selected_models=(self.model_id,) if not self.cancelled else (),
            calls=(
                CallRecord(
                    model_id=self.model_id,
                    role="candidate",
                    attempt=1,
                    latency_ms=self.provider_ms if self.provider_started else 0.0,
                    tokens=dict(self.tokens)
                    if self.tokens
                    else {
                        "input": None,
                        "output": None,
                        "reasoning": None,
                        "cache_read": None,
                        "cache_write": None,
                        "source": "provider",
                        "cache_read_assumed_miss": False,
                    },
                    billed_usd=self.billed_usd,
                    price_period=self.price_period,
                    error=error,
                    cancelled=self.cancelled,
                    output=self.output,
                ),
            ),
        )


class AsyncBenchmarkRunner:
    def __init__(
        self,
        *,
        provider: TraceProvider,
        models: Mapping[str, Mapping[str, Any]],
        contracts: Mapping[str, Mapping[str, Any]],
        trace: LatencyTrace,
        provider_limit: int,
        allocated_milli: int,
        late_billing_fraction: float = 0.5,
    ) -> None:
        if provider_limit < 1:
            raise ValueError("provider_limit must be at least 1")
        if not 0.0 <= late_billing_fraction <= 1.0:
            raise ValueError("late_billing_fraction must be between 0 and 1")
        self.provider = provider
        self.models = models
        self.contracts = contracts
        self.trace = trace
        self.provider_limit = provider_limit
        self.allocated_milli = allocated_milli
        self.late_billing_fraction = late_billing_fraction
        self.meter = SweycoinMeter(allocated_milli)
        self._provider_sem = asyncio.Semaphore(provider_limit)
        self._worker_sem: asyncio.Semaphore | None = None
        self._controller = StopController()
        self._active_worker = 0
        self._peak_worker = 0
        self._active_provider = 0
        self._peak_provider = 0
        self._verified_count = 0
        self._first_verified_ns: int | None = None
        self._early_stop_after: int | None = None
        self._pending: list[Attempt] = []
        self._run_id = "run"
        self._start_ns = 0
        self._current_model = ""

    async def run(
        self,
        strategy: Strategy,
        specs: Sequence[TaskSpec],
        *,
        model_id: str,
        concurrency: int,
        dag: Dag | None = None,
        early_stop_after: int | None = None,
        cancel_after_ms: float | None = None,
        run_id: str | None = None,
    ) -> dict[str, Any]:
        try:
            strategy = Strategy(strategy)
        except ValueError as exc:
            raise ValueError(f"unknown strategy {strategy!r}") from exc
        if model_id not in self.models:
            raise ValueError(f"unknown model {model_id!r}")
        if not self.models[model_id].get("available", False):
            raise ValueError(f"model {model_id!r} is unavailable")
        if strategy in {Strategy.C_LAYERED, Strategy.D_EVENT_READY} and dag is None:
            raise ValueError(f"{strategy} requires a dependency DAG")
        if concurrency < 1:
            raise ValueError("concurrency must be at least 1")
        self._run_id = run_id or f"async:{strategy}:{concurrency}"
        self._current_model = model_id
        self._early_stop_after = early_stop_after
        self._controller = StopController()
        self._worker_sem = asyncio.Semaphore(concurrency)
        self._active_worker = 0
        self._peak_worker = 0
        self._active_provider = 0
        self._peak_provider = 0
        self._verified_count = 0
        self._first_verified_ns = None
        self._pending = []
        self.meter = SweycoinMeter(self.allocated_milli)
        self._start_ns = time.perf_counter_ns()
        timer: asyncio.Task[Any] | None = None
        if cancel_after_ms is not None and cancel_after_ms > 0:
            timer = asyncio.create_task(self._cancel_timer(cancel_after_ms))
        try:
            if strategy is Strategy.A_SINGLE_LARGE:
                attempts = await self._run_bounded(specs, concurrency)
            elif strategy is Strategy.B_SEQUENTIAL:
                attempts = await self._run_sequential(specs, dag)
            elif strategy is Strategy.C_LAYERED:
                attempts = await self._run_layered(specs, dag, concurrency)
            else:
                attempts = await self._run_ready_queue(specs, dag, concurrency)
        finally:
            if timer is not None:
                timer.cancel()
                await asyncio.gather(timer, return_exceptions=True)
        end_ns = time.perf_counter_ns()
        results_by_id = {attempt.task_id: attempt for attempt in attempts}
        unscheduled = tuple(
            spec.task_id for spec in specs if spec.task_id not in results_by_id
        )
        quality = score_suite(
            specs, {task_id: attempt.to_task_result() for task_id, attempt in results_by_id.items()}
        )
        verified_evidence = int(quality["extraction"]["matched_quotes"])
        cost = account_calls(
            [attempt.to_call() for attempt in attempts],
            models=self.models,
            verified_evidence=verified_evidence,
        )
        budget = self._reconcile_budget(attempts)
        wall_ms = (end_ns - self._start_ns) / 1_000_000.0
        task_latencies = [
            attempt.task_latency_ms
            for attempt in attempts
            if attempt.start_ns is not None and attempt.end_ns is not None
        ]
        ready_latencies = [
            attempt.ready_latency_ms
            for attempt in attempts
            if attempt.end_ns is not None
        ]
        queue_waits = [
            attempt.queue_wait_ms
            for attempt in attempts
            if attempt.provider_started
        ]
        item_completions = self._item_completions(attempts)
        item_latencies = [
            (finish - self._start_ns) / 1_000_000.0
            for finish in item_completions.values()
        ]
        durations = {
            spec.task_id: self.trace.duration_ms(
                task_id=spec.task_id,
                item_id=spec.item_id,
                model_id=model_id,
                kind=str(spec.kind),
            )
            for spec in specs
        }
        if strategy is Strategy.A_SINGLE_LARGE:
            critical_path_ms = max(durations.values(), default=0.0)
        else:
            assert dag is not None
            critical_path_ms = dag.longest_path_ms(durations)
        worker_busy_ms = sum(attempt.worker_ms for attempt in attempts)
        provider_busy_ms = sum(attempt.provider_ms for attempt in attempts)
        return {
            "strategy": str(strategy),
            "run_id": self._run_id,
            "model_ids": [model_id],
            "concurrency": concurrency,
            "provider_rate_limit": self.provider_limit,
            "task_count": len(specs),
            "scored_subtasks": quality["scored_subtasks"],
            "wall_ms": round(wall_ms, 3),
            "first_verified_ms": None
            if self._first_verified_ns is None
            else round((self._first_verified_ns - self._start_ns) / 1_000_000.0, 3),
            "task_latency_p50_ms": round(_percentile(task_latencies, 0.50), 3),
            "task_latency_p95_ms": round(_percentile(task_latencies, 0.95), 3),
            "ready_latency_p50_ms": round(_percentile(ready_latencies, 0.50), 3),
            "ready_latency_p95_ms": round(_percentile(ready_latencies, 0.95), 3),
            "queue_wait_p50_ms": round(_percentile(queue_waits, 0.50), 3),
            "queue_wait_p95_ms": round(_percentile(queue_waits, 0.95), 3),
            "queue_wait_total_ms": round(sum(queue_waits), 3),
            "item_end_to_end_p50_ms": round(_percentile(item_latencies, 0.50), 3),
            "item_end_to_end_p95_ms": round(_percentile(item_latencies, 0.95), 3),
            "completed_items": len(item_completions),
            "item_count": len({spec.item_id for spec in specs}),
            "critical_path_ms": round(critical_path_ms, 3),
            "worker_busy_ms": round(worker_busy_ms, 3),
            "provider_busy_ms": round(provider_busy_ms, 3),
            "worker_utilization": round(
                min(1.0, worker_busy_ms / max(wall_ms * concurrency, 1e-9)), 6
            ),
            "provider_utilization": round(
                min(1.0, provider_busy_ms / max(wall_ms * self.provider_limit, 1e-9)),
                6,
            ),
            "peak_worker": self._peak_worker,
            "peak_provider": self._peak_provider,
            "call_count": len(attempts),
            "provider_calls": sum(1 for attempt in attempts if attempt.provider_started),
            "retry_count": 0,
            "cancelled_tasks": sum(1 for attempt in attempts if attempt.cancelled),
            "unscheduled_tasks": len(unscheduled),
            "unscheduled_task_ids": list(unscheduled),
            "stopped_early": self._controller.is_stopped(),
            "stop_reason": self._controller.reason,
            "early_stop_after_verified": self._early_stop_after,
            "verified_count": self._verified_count,
            "quality": {key: value for key, value in quality.items() if key != "rows"},
            "cost": cost,
            "sweycoins": budget,
            "straggler_items": sorted(self.trace.straggler_items),
            "straggler_extra_ms": self.trace.straggler_extra_ms,
        }

    async def _cancel_timer(self, delay_ms: float) -> None:
        await asyncio.sleep(delay_ms / 1000.0)
        self._controller.stop("cancelled")

    async def _run_bounded(
        self, specs: Sequence[TaskSpec], concurrency: int
    ) -> list[Attempt]:
        entries: list[tuple[TaskSpec, int, asyncio.Task[Attempt]]] = []
        for spec in specs:
            ready_ns = time.perf_counter_ns()
            task = asyncio.create_task(self._task_entry(spec, ready_ns))
            self._controller.register(task)
            entries.append((spec, ready_ns, task))
        outcomes = await asyncio.gather(
            *(task for _, _, task in entries), return_exceptions=True
        )
        return [
            self._coerce_outcome(outcome, spec, ready_ns)
            for (spec, ready_ns, _), outcome in zip(entries, outcomes)
        ]

    async def _run_sequential(
        self, specs: Sequence[TaskSpec], dag: Dag | None
    ) -> list[Attempt]:
        order = self._topological_order(specs, dag)
        results: list[Attempt] = []
        for task_id in order:
            if self._controller.is_stopped():
                break
            spec = next(spec for spec in specs if spec.task_id == task_id)
            outcome = await self._task_entry(spec, time.perf_counter_ns())
            results.append(outcome)
        return results

    async def _run_layered(
        self, specs: Sequence[TaskSpec], dag: Dag, concurrency: int
    ) -> list[Attempt]:
        assert dag is not None
        by_id = {spec.task_id: spec for spec in specs}
        results: list[Attempt] = []
        for layer in dag.layers:
            if self._controller.is_stopped():
                break
            ready_ns = time.perf_counter_ns()
            entries: list[tuple[TaskSpec, asyncio.Task[Attempt]]] = []
            for task_id in layer:
                if task_id not in by_id:
                    continue
                task = asyncio.create_task(
                    self._task_entry(by_id[task_id], ready_ns)
                )
                self._controller.register(task)
                entries.append((by_id[task_id], task))
            outcomes = await asyncio.gather(
                *(task for _, task in entries), return_exceptions=True
            )
            results.extend(
                self._coerce_outcome(outcome, spec, ready_ns)
                for (spec, _), outcome in zip(entries, outcomes)
            )
            if self._controller.is_stopped():
                break
        return results

    async def _run_ready_queue(
        self, specs: Sequence[TaskSpec], dag: Dag, concurrency: int
    ) -> list[Attempt]:
        assert dag is not None
        by_id = {spec.task_id: spec for spec in specs}
        indegree = {
            task_id: len(node.depends_on) for task_id, node in dag.nodes.items()
        }
        dependents = dag.dependents
        ready: deque[str] = deque(dag.initial_ready())
        ready_at: dict[str, int] = {
            task_id: self._start_ns for task_id in dag.initial_ready()
        }
        running: set[asyncio.Task[Attempt]] = set()
        task_specs: dict[asyncio.Task[Attempt], tuple[TaskSpec, int]] = {}
        results: list[Attempt] = []
        while (ready or running) and not self._controller.is_stopped():
            while (
                ready
                and len(running) < concurrency
                and not self._controller.is_stopped()
            ):
                task_id = ready.popleft()
                ready_ns = ready_at.pop(task_id, time.perf_counter_ns())
                task = asyncio.create_task(
                    self._task_entry(by_id[task_id], ready_ns)
                )
                self._controller.register(task)
                running.add(task)
                task_specs[task] = (by_id[task_id], ready_ns)
            if not running:
                break
            done, _ = await asyncio.wait(
                running, return_when=asyncio.FIRST_COMPLETED
            )
            completed_at = time.perf_counter_ns()
            for task in done:
                running.discard(task)
                spec, ready_ns = task_specs.pop(task)
                outcome = self._coerce_outcome(
                    self._task_result(task), spec, ready_ns
                )
                results.append(outcome)
                if outcome.cancelled:
                    continue
                for dependent in dependents.get(outcome.task_id, ()):
                    indegree[dependent] -= 1
                    if indegree[dependent] == 0:
                        ready_at[dependent] = completed_at
                        ready.append(dependent)
        if self._controller.is_stopped() and running:
            for task in list(running):
                task.cancel()
            done, _ = await asyncio.wait(running)
            for task in done:
                spec, ready_ns = task_specs.pop(task, (None, 0))
                if spec is None:
                    continue
                results.append(
                    self._coerce_outcome(
                        self._task_result(task), spec, ready_ns
                    )
                )
            running.clear()
        return results

    def _task_result(self, task: asyncio.Task[Attempt]) -> Any:
        try:
            return task.result()
        except BaseException as exc:  # noqa: BLE001 - cancelled before start
            return exc

    def _coerce_outcome(
        self, outcome: Any, spec: TaskSpec, ready_ns: int
    ) -> Attempt:
        if isinstance(outcome, Attempt):
            return outcome
        return Attempt(
            task_id=spec.task_id,
            item_id=spec.item_id,
            kind=spec.kind,
            model_id=self._current_model,
            reservation_id=None,
            tariff_milli=0,
            ready_ns=ready_ns,
            end_ns=time.perf_counter_ns(),
            cancelled=True,
            error="cancelled before dispatch",
        )

    async def _task_entry(self, spec: TaskSpec, ready_ns: int) -> Attempt:
        try:
            return await self._execute(spec, ready_ns)
        except asyncio.CancelledError:
            return Attempt(
                task_id=spec.task_id,
                item_id=spec.item_id,
                kind=spec.kind,
                model_id=self._current_model,
                reservation_id=None,
                tariff_milli=0,
                ready_ns=ready_ns,
                end_ns=time.perf_counter_ns(),
                cancelled=True,
                error="cancelled before dispatch",
            )

    async def _execute(self, spec: TaskSpec, ready_ns: int) -> Attempt:
        model_id = self._model_for(spec)
        tariff = self._tariff(spec.kind)
        key = f"{self._run_id}:{spec.task_id}:{model_id}:attempt-1"
        try:
            reservation_id = self.meter.reserve(key, tariff)
        except BudgetError as exc:
            return Attempt(
                task_id=spec.task_id,
                item_id=spec.item_id,
                kind=spec.kind,
                model_id=model_id,
                reservation_id=None,
                tariff_milli=tariff,
                ready_ns=ready_ns,
                start_ns=time.perf_counter_ns(),
                end_ns=time.perf_counter_ns(),
                error=f"budget: {exc}",
            )
        attempt = Attempt(
            task_id=spec.task_id,
            item_id=spec.item_id,
            kind=spec.kind,
            model_id=model_id,
            reservation_id=reservation_id,
            tariff_milli=tariff,
            ready_ns=ready_ns,
        )
        attempt.start_ns = time.perf_counter_ns()
        try:
            async with self._worker_sem:  # type: ignore[arg-type]
                self._active_worker += 1
                self._peak_worker = max(self._peak_worker, self._active_worker)
                try:
                    async with self._provider_sem:
                        attempt.provider_start_ns = time.perf_counter_ns()
                        attempt.provider_started = True
                        self._active_provider += 1
                        self._peak_provider = max(
                            self._peak_provider, self._active_provider
                        )
                        try:
                            response = await self.provider.chat(self._request(spec, model_id))
                        finally:
                            self._active_provider -= 1
                        attempt.provider_end_ns = time.perf_counter_ns()
                finally:
                    self._active_worker -= 1
            attempt.end_ns = time.perf_counter_ns()
            attempt.tokens = dict(response.tokens)
            attempt.billed_usd = response.billed_usd
            attempt.price_period = response.price_period
            try:
                output = parse_json_output(response.content)
                verification = verify_output(
                    spec.kind,
                    output,
                    passage=spec.passage,
                    max_quotes=int(self.contracts.get("extraction", {}).get("max_quotes", 3)),
                    min_quote_chars=int(
                        self.contracts.get("extraction", {}).get("min_quote_chars", 10)
                    ),
                )
                attempt.output = output
                attempt.verification = verification
                if not verification.ok:
                    attempt.error = "verification: " + "; ".join(verification.errors)
                else:
                    self._record_verified(attempt)
            except ContractViolation as exc:
                attempt.error = f"contract: {exc}"
            self.meter.settle(reservation_id, tariff)
        except asyncio.CancelledError:
            attempt.end_ns = time.perf_counter_ns()
            attempt.cancelled = True
            attempt.error = "cancelled"
            if attempt.provider_started:
                self._pending.append(attempt)
            else:
                self.meter.release(reservation_id)
        except Exception as exc:  # noqa: BLE001 - benchmark boundary
            attempt.end_ns = time.perf_counter_ns()
            attempt.error = f"{type(exc).__name__}: {exc}"
            self.meter.settle(reservation_id, tariff)
        if attempt.end_ns is None:
            attempt.end_ns = time.perf_counter_ns()
        return attempt

    def _record_verified(self, attempt: Attempt) -> None:
        self._verified_count += 1
        now = attempt.end_ns or time.perf_counter_ns()
        if self._first_verified_ns is None or now < self._first_verified_ns:
            self._first_verified_ns = now
        if (
            self._early_stop_after is not None
            and self._verified_count >= self._early_stop_after
        ):
            self._controller.stop("early_stop")

    def _model_for(self, spec: TaskSpec) -> str:
        # A passes its large model; B/C/D pass the shared microtask model.
        return self._current_model

    def _request(self, spec: TaskSpec, model_id: str) -> ChatRequest:
        system, user = render_prompt(spec.kind, spec.input_data)
        return ChatRequest(
            request_id=f"{self._run_id}:{spec.task_id}:{model_id}",
            task_id=spec.task_id,
            kind=spec.kind,
            model_id=model_id,
            system=system,
            user=user,
            schema=output_schema(spec.kind),
            max_output_tokens=self._max_tokens(spec.kind, model_id),
            passage=spec.passage,
            input_data=spec.input_data,
            attempt=1,
            timeout_seconds=60.0,
        )

    def _tariff(self, kind: Kind) -> int:
        return int(self.contracts[str(kind)]["sweycoins_milli"])

    def _max_tokens(self, kind: Kind, model_id: str) -> int:
        base = int(self.contracts[str(kind)]["max_output_tokens"])
        headroom = int(self.models.get(model_id, {}).get("output_token_headroom", 0))
        return base + max(0, headroom)

    def _topological_order(
        self, specs: Sequence[TaskSpec], dag: Dag | None
    ) -> tuple[str, ...]:
        if dag is None:
            return tuple(spec.task_id for spec in specs)
        return tuple(task_id for layer in dag.layers for task_id in layer)

    def _item_completions(self, attempts: Sequence[Attempt]) -> dict[str, int]:
        by_item: dict[str, list[Attempt]] = defaultdict(list)
        for attempt in attempts:
            by_item[attempt.item_id].append(attempt)
        completions: dict[str, int] = {}
        for item_id, rows in by_item.items():
            if any(row.cancelled or row.error for row in rows):
                continue
            completions[item_id] = max(
                (row.end_ns for row in rows if row.end_ns is not None), default=0
            )
        return completions

    def _reconcile_budget(self, attempts: Sequence[Attempt]) -> dict[str, Any]:
        settled_before = self.meter.spent_milli
        pending_before = self.meter.reserved_milli
        late_billed = 0
        released = 0
        for attempt in self._pending:
            if attempt.reservation_id is None:
                continue
            digest = hashlib.sha256(
                f"late:{attempt.task_id}:{attempt.model_id}".encode("utf-8")
            ).hexdigest()
            fraction = int(digest[:8], 16) / 0xFFFFFFFF
            if fraction < self.late_billing_fraction:
                self.meter.settle(attempt.reservation_id, attempt.tariff_milli)
                attempt.late_billed = True
                late_billed += attempt.tariff_milli
            else:
                self.meter.release(attempt.reservation_id)
                released += attempt.tariff_milli
        settled_after = self.meter.spent_milli
        pending_after = self.meter.reserved_milli
        return {
            "allocated_milli": self.meter.allocated_milli,
            "settled_before_reconcile_milli": settled_before,
            "pending_before_reconcile_milli": pending_before,
            "late_billed_milli": late_billed,
            "released_milli": released,
            "settled_milli": settled_after,
            "pending_after_reconcile_milli": pending_after,
            "available_milli": self.meter.available_milli,
            "total_exposure_milli": settled_after + pending_after,
            "pending_tasks": len(self._pending),
            "late_billed_tasks": sum(1 for row in self._pending if row.late_billed),
            "released_tasks": sum(1 for row in self._pending if not row.late_billed),
        }


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


async def run_async_benchmark(
    experiment_config_path: Path,
    async_config_path: Path,
    *,
    scale: float | None = None,
) -> dict[str, Any]:
    experiment = load_json(experiment_config_path)
    async_config = load_json(async_config_path)
    fixtures = experiment["fixtures"]
    suite = load_fixtures(
        experiment_config_path.resolve().parent.parent / str(fixtures["gold"]),
        expected_sha256=str(fixtures["sha256"]),
    )
    models = {str(model["id"]): model for model in experiment["models"]}
    contracts = experiment["contracts"]
    trace_cfg = async_config["trace"]
    seed = int(trace_cfg["seed"])
    straggler_items = choose_straggler_items(
        [item.item_id for item in suite.items],
        count=int(trace_cfg.get("straggler_count", 0)),
        seed=seed,
    )
    trace = LatencyTrace(
        base_ms={str(k): float(v) for k, v in trace_cfg["base_ms"].items()},
        model_multiplier={
            str(k): float(v) for k, v in trace_cfg["model_multiplier"].items()
        },
        jitter_fraction=float(trace_cfg.get("jitter_fraction", 0.0)),
        straggler_items=straggler_items,
        straggler_extra_ms=float(trace_cfg.get("straggler_extra_ms", 0.0)),
        seed=seed,
        scale=float(scale if scale is not None else async_config.get("scale", 1.0)),
        extra_ms_by_task={
            str(k): float(v)
            for k, v in trace_cfg.get("extra_ms_by_task", {}).items()
        },
    )
    model_a = str(async_config["models"]["A"])
    model_bcd = str(async_config["models"]["BCD"])
    inner = OfflineHeuristicProvider(
        model_ids=[model_a, model_bcd], simulated_latency_ms=0.0
    )
    provider = TraceProvider(inner, trace)
    runner = AsyncBenchmarkRunner(
        provider=provider,
        models=models,
        contracts=contracts,
        trace=trace,
        provider_limit=int(async_config.get("provider_rate_limit", 3)),
        allocated_milli=int(
            async_config.get(
                "run_allocated_milli", experiment["sweycoins"]["run_allocated_milli"]
            )
        ),
        late_billing_fraction=float(async_config.get("late_billing_fraction", 0.5)),
    )
    specs_a = build_task_specs(suite, Kind.COMBINED)
    specs_bcd = all_microtask_specs(suite)
    dag = build_dag(specs_bcd)
    levels = [int(value) for value in async_config["concurrency_levels"]]
    runs: list[dict[str, Any]] = []
    for concurrency in levels:
        runs.append(
            await runner.run(
                Strategy.A_SINGLE_LARGE,
                specs_a,
                model_id=model_a,
                concurrency=concurrency,
                run_id=f"async:A:{concurrency}",
            )
        )
    runs.append(
        await runner.run(
            Strategy.B_SEQUENTIAL,
            specs_bcd,
            model_id=model_bcd,
            concurrency=1,
            dag=dag,
            run_id="async:B:1",
        )
    )
    for concurrency in levels:
        runs.append(
            await runner.run(
                Strategy.C_LAYERED,
                specs_bcd,
                model_id=model_bcd,
                concurrency=concurrency,
                dag=dag,
                run_id=f"async:C:{concurrency}",
            )
        )
        runs.append(
            await runner.run(
                Strategy.D_EVENT_READY,
                specs_bcd,
                model_id=model_bcd,
                concurrency=concurrency,
                dag=dag,
                run_id=f"async:D:{concurrency}",
            )
        )
    scenarios = async_config.get("scenarios", {})
    for name, scenario in scenarios.items():
        strategy = Strategy(str(scenario["strategy"]))
        cancel_after = scenario.get("cancel_after_ms")
        if cancel_after is not None:
            cancel_after = float(cancel_after) * trace.scale
        scenario_run = await runner.run(
            strategy,
            specs_bcd,
            model_id=model_bcd,
            concurrency=int(scenario.get("concurrency", 8)),
            dag=dag,
            early_stop_after=scenario.get("after_verified"),
            cancel_after_ms=cancel_after,
            run_id=f"async:{name}",
        )
        scenario_run["scenario"] = name
        runs.append(scenario_run)
    for run in runs:
        run.setdefault("scenario", None)
    return {
        "schema_version": 1,
        "experiment_id": "swe-microtask-swarm-async",
        "evidence_kind": "offline_deterministic_trace",
        "generated_at": datetime.now(UTC).isoformat(),
        "fixture_sha256": suite.sha256,
        "fixture_item_count": suite.item_count,
        "fixture_microtask_count": suite.microtask_count,
        "models": {"A": model_a, "BCD": model_bcd},
        "provider_rate_limit": int(async_config.get("provider_rate_limit", 3)),
        "concurrency_levels": levels,
        "trace": {
            "seed": seed,
            "base_ms": trace.base_ms,
            "model_multiplier": trace.model_multiplier,
            "jitter_fraction": trace.jitter_fraction,
            "straggler_items": sorted(trace.straggler_items),
            "straggler_extra_ms": trace.straggler_extra_ms,
            "scale": trace.scale,
        },
        "dag_layers": [list(layer) for layer in dag.layers],
        "runs": runs,
        "summary": _summarize_async(runs),
        "totals": _totals_async(runs),
        "notes": [
            "Offline deterministic latency trace; no model or network call.",
            "A is one combined call per item; B/C/D use the same microtask model, verifier, fixtures, and budget.",
            "B is sequential and therefore only run at concurrency 1.",
            "Cancellation and early stop keep reservations pending; the run reconciles them with a deterministic late-billing policy.",
        ],
    }


def _totals_async(runs: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    calculated = 0.0
    calculated_known = 0.0
    calls = 0
    provider_calls = 0
    cancelled = 0
    settled = 0
    pending_before = 0
    late = 0
    released = 0
    for run in runs:
        budget = run["sweycoins"]
        calls += int(run["call_count"])
        provider_calls += int(run["provider_calls"])
        cancelled += int(run["cancelled_tasks"])
        settled += int(budget["settled_milli"])
        pending_before += int(budget["pending_before_reconcile_milli"])
        late += int(budget["late_billed_milli"])
        released += int(budget["released_milli"])
        all_in = run["cost"]["all_in"]["calculated_usd"]
        complete = run["cost"]["all_in_complete_calls_calculated_usd"]
        if all_in != "N/A":
            calculated += float(all_in)
        elif complete != "N/A":
            calculated += float(complete)
        if complete != "N/A":
            calculated_known += float(complete)
    return {
        "calls": calls,
        "provider_calls": provider_calls,
        "cancelled_tasks": cancelled,
        "settled_milli": settled,
        "pending_before_reconcile_milli": pending_before,
        "late_billed_milli": late,
        "released_milli": released,
        "total_exposure_milli": settled,
        "calculated_usd": round(calculated, 9),
        "calculated_usd_complete_calls": round(calculated_known, 9),
        "actual_usd": "N/A",
        "note": "Calculated USD sums known complete-call subsets when an all-in value is N/A.",
    }


def _summarize_async(runs: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    by_strategy: dict[str, Any] = {}
    for run in runs:
        if run.get("scenario") is not None:
            continue
        strategy = str(run["strategy"])
        current = by_strategy.get(strategy)
        if current is None:
            by_strategy[strategy] = {
                "best_wall_ms": run["wall_ms"],
                "run_id": run["run_id"],
                "concurrency": run["concurrency"],
                "accuracy": run["quality"]["accuracy"],
                "coverage": run["quality"]["extraction"]["coverage"],
                "false_support_rate": run["quality"]["false_support"][
                    "false_support_rate"
                ],
                "first_verified_ms": run["first_verified_ms"],
                "p95_ms": run["task_latency_p95_ms"],
                "worker_utilization": run["worker_utilization"],
                "provider_utilization": run["provider_utilization"],
                "settled_milli": run["sweycoins"]["settled_milli"],
                "pending_before_milli": run["sweycoins"][
                    "pending_before_reconcile_milli"
                ],
            }
            continue
        if float(run["wall_ms"]) < float(current["best_wall_ms"]):
            current.update(
                {
                    "best_wall_ms": run["wall_ms"],
                    "run_id": run["run_id"],
                    "concurrency": run["concurrency"],
                    "accuracy": run["quality"]["accuracy"],
                    "coverage": run["quality"]["extraction"]["coverage"],
                    "false_support_rate": run["quality"]["false_support"][
                        "false_support_rate"
                    ],
                    "first_verified_ms": run["first_verified_ms"],
                    "p95_ms": run["task_latency_p95_ms"],
                    "worker_utilization": run["worker_utilization"],
                    "provider_utilization": run["provider_utilization"],
                    "settled_milli": run["sweycoins"]["settled_milli"],
                    "pending_before_milli": run["sweycoins"][
                        "pending_before_reconcile_milli"
                    ],
                }
            )
    return {"run_count": len(runs), "best_by_strategy": by_strategy}


def write_async_report(report: Mapping[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def render_async_markdown(report: Mapping[str, Any]) -> str:
    lines = [
        "# Async microtask scheduling benchmark (offline)",
        "",
        f"Generated: {report['generated_at']}",
        f"Fixture SHA-256: `{report['fixture_sha256']}`",
        f"Models: A=`{report['models']['A']}`, B/C/D=`{report['models']['BCD']}`",
        f"Provider rate limit: {report['provider_rate_limit']}",
        f"Trace scale: {report['trace']['scale']}",
        "",
        "Deterministic latency traces. No model or network call was made.",
        "Quality values are offline heuristic personas, not live model results.",
        "",
        "| Strategy | Scenario | Conc. | Wall ms | First verified ms | p50 ms | p95 ms | Critical path ms | Worker util | Provider util | Accuracy | Coverage | False support | Calls | Settled milli | Pending before | Late milli | Released milli | Calc USD |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for run in report["runs"]:
        budget = run["sweycoins"]
        cost = run["cost"]["all_in"]["calculated_usd"]
        lines.append(
            "| {strategy} | {scenario} | {conc} | {wall} | {first} | {p50} | {p95} | {cp} | {wu} | {pu} | {acc} | {cov} | {fs} | {calls} | {settled} | {pending} | {late} | {released} | {usd} |".format(
                strategy=run["strategy"],
                scenario=run.get("scenario") or "main",
                conc=run["concurrency"],
                wall=run["wall_ms"],
                first=run["first_verified_ms"],
                p50=run["task_latency_p50_ms"],
                p95=run["task_latency_p95_ms"],
                cp=run["critical_path_ms"],
                wu=run["worker_utilization"],
                pu=run["provider_utilization"],
                acc=run["quality"]["accuracy"],
                cov=run["quality"]["extraction"]["coverage"],
                fs=run["quality"]["false_support"]["false_support_rate"],
                calls=run["call_count"],
                settled=budget["settled_milli"],
                pending=budget["pending_before_reconcile_milli"],
                late=budget["late_billed_milli"],
                released=budget["released_milli"],
                usd=cost,
            )
        )
    lines.extend(
        [
            "",
            "Totals across all runs: calls={calls}, provider calls={provider_calls}, "
            "cancelled={cancelled}, settled={settled} milli, "
            "pending before reconciliation={pending} milli, "
            "late-billed={late} milli, released={released} milli, "
            "calculated USD={usd} (complete-call subset {usd_known}).".format(
                calls=report["totals"]["calls"],
                provider_calls=report["totals"]["provider_calls"],
                cancelled=report["totals"]["cancelled_tasks"],
                settled=report["totals"]["settled_milli"],
                pending=report["totals"]["pending_before_reconcile_milli"],
                late=report["totals"]["late_billed_milli"],
                released=report["totals"]["released_milli"],
                usd=report["totals"]["calculated_usd"],
                usd_known=report["totals"]["calculated_usd_complete_calls"],
            ),
            "",
            "`actual_usd` is N/A; calculated USD is a price-list equivalent. "
            "Pending/late milli are Sweycoin reservations, not provider dollars.",
            "",
            "Worker utilization = task-held worker time / (wall × requested "
            "workers); it includes waiting for a provider permit. Provider "
            "utilization = provider-call time / (wall × provider rate limit).",
            "",
            "A = one combined call per item. B = sequential microtasks. "
            "C = bounded parallel with a global barrier per layer. "
            "D = event-driven ready queue without unrelated barriers.",
            "",
        ]
    )
    return "\n".join(lines)


def _percentile(values: Sequence[float], quantile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, math.ceil(len(ordered) * quantile) - 1))
    return ordered[index]


__all__ = [
    "AsyncBenchmarkRunner",
    "Dag",
    "DagNode",
    "LatencyTrace",
    "StopController",
    "Strategy",
    "TraceProvider",
    "build_dag",
    "choose_straggler_items",
    "load_json",
    "render_async_markdown",
    "run_async_benchmark",
    "write_async_report",
]
