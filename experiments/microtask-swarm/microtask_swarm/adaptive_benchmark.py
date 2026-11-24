"""Provider-aware scheduling analysis on top of the frozen async benchmark.

This module does not modify the frozen C/D baseline. It imports the frozen
runner, types, trace, and fixtures, then adds:

- provider-aware adaptive concurrency (queue-wait AIMD);
- critical-path priority in the event-ready queue;
- a quality-gated early stop based on measured accuracy, extraction coverage,
  and false-support rate, not only a verified count.

The frozen baseline files and reports are hashed into the new report so a
baseline drift is visible. The comparison runs C, D, and provider-aware P on
the same traces, provider limits, fixtures, verifier, model, and local budget.
"""

from __future__ import annotations

import asyncio
import hashlib
import heapq
import json
import math
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

from microtask_swarm.async_benchmark import (
    AsyncBenchmarkRunner,
    Attempt,
    Dag,
    LatencyTrace,
    Strategy,
    TraceProvider,
    build_dag,
    choose_straggler_items,
)
from microtask_swarm.contracts import Kind
from microtask_swarm.fixtures import (
    GoldSuite,
    TaskSpec,
    all_microtask_specs,
    build_task_specs,
    load_fixtures,
)
from microtask_swarm.offline import OfflineHeuristicProvider
from microtask_swarm.scoring import score_suite


FROZEN_FILES = (
    "microtask_swarm/async_benchmark.py",
    "config/async-benchmark.json",
    "reports/async-benchmark.json",
    "reports/async-benchmark.md",
    "tests/test_async_benchmark.py",
)


@dataclass(frozen=True, slots=True)
class QualityGate:
    min_items: int
    min_accuracy: float
    min_coverage: float
    max_false_support: float

    def evaluate(
        self,
        specs: Sequence[TaskSpec],
        results: Mapping[str, Attempt],
        *,
        specs_by_item: Mapping[str, Sequence[TaskSpec]],
    ) -> dict[str, Any]:
        completed_items: list[str] = []
        for item_id, item_specs in specs_by_item.items():
            if not all(
                spec.task_id in results
                and not results[spec.task_id].cancelled
                for spec in item_specs
            ):
                continue
            completed_items.append(item_id)
        completed_ids = set(completed_items)
        subset_specs = [spec for spec in specs if spec.item_id in completed_ids]
        subset_results = {
            spec.task_id: results[spec.task_id].to_task_result()
            for spec in subset_specs
            if spec.task_id in results
        }
        quality = score_suite(subset_specs, subset_results)
        extraction = quality["extraction"]
        met = (
            len(completed_items) >= self.min_items
            and quality["accuracy"] >= self.min_accuracy
            and extraction["coverage"] >= self.min_coverage
            and quality["false_support"]["false_support_rate"]
            <= self.max_false_support
        )
        return {
            "met": met,
            "min_items": self.min_items,
            "completed_items": len(completed_items),
            "completed_item_ids": sorted(completed_items),
            "accuracy": quality["accuracy"],
            "coverage": extraction["coverage"],
            "false_support_rate": quality["false_support"]["false_support_rate"],
            "min_accuracy": self.min_accuracy,
            "min_coverage": self.min_coverage,
            "max_false_support": self.max_false_support,
            "scored_subtasks": quality["scored_subtasks"],
        }


class AdaptiveConcurrency:
    """Queue-wait AIMD. Starts at 1 and adapts up to the requested maximum."""

    def __init__(
        self,
        *,
        max_limit: int,
        min_limit: int = 1,
        target_queue_ms: float = 400.0,
        increase_after: int = 2,
        initial_limit: int | None = None,
    ) -> None:
        if max_limit < 1:
            raise ValueError("max_limit must be at least 1")
        self.max_limit = max_limit
        self.min_limit = max(1, min_limit)
        self.target_queue_ms = target_queue_ms
        self.increase_after = max(1, increase_after)
        start = self.min_limit if initial_limit is None else initial_limit
        self.limit = min(self.max_limit, max(self.min_limit, start))
        self._good_streak = 0
        self.increases = 0
        self.decreases = 0
        self.history: list[dict[str, float]] = [
            {"elapsed_ms": 0.0, "limit": float(self.limit), "reason": "start"}
        ]

    def observe(self, *, queue_wait_ms: float, elapsed_ms: float) -> None:
        if queue_wait_ms > self.target_queue_ms * 4:
            new_limit = max(self.min_limit, self.limit // 2)
            if new_limit < self.limit:
                self.limit = new_limit
                self.decreases += 1
                self._good_streak = 0
                self.history.append(
                    {
                        "elapsed_ms": round(elapsed_ms, 3),
                        "limit": float(self.limit),
                        "reason": "queue_decrease",
                    }
                )
            return
        if queue_wait_ms <= self.target_queue_ms:
            self._good_streak += 1
            if self._good_streak >= self.increase_after and self.limit < self.max_limit:
                self.limit += 1
                self.increases += 1
                self._good_streak = 0
                self.history.append(
                    {
                        "elapsed_ms": round(elapsed_ms, 3),
                        "limit": float(self.limit),
                        "reason": "queue_increase",
                    }
                )
        else:
            self._good_streak = 0


def remaining_paths(dag: Dag, durations: Mapping[str, float]) -> dict[str, float]:
    memo: dict[str, float] = {}

    def remaining(task_id: str) -> float:
        if task_id in memo:
            return memo[task_id]
        base = float(durations.get(task_id, 0.0))
        children = dag.dependents.get(task_id, ())
        value = base + (max(remaining(child) for child in children) if children else 0.0)
        memo[task_id] = value
        return value

    return {task_id: remaining(task_id) for task_id in dag.nodes}


class ProviderAwareRunner(AsyncBenchmarkRunner):
    """Frozen runner with adaptive concurrency, critical-path priority, quality stop."""

    def __init__(
        self,
        *,
        adaptive: bool,
        priority: bool,
        quality_gate: QualityGate | None,
        target_queue_ms: float = 400.0,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.adaptive_enabled = adaptive
        self.priority_enabled = priority
        self.quality_gate = quality_gate
        self.target_queue_ms = target_queue_ms
        self._adaptive: AdaptiveConcurrency | None = None
        self._quality_result: dict[str, Any] | None = None
        self._quality_last_items = -1
        self._remaining: dict[str, float] = {}
        self._specs_by_item: dict[str, list[TaskSpec]] = {}

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
        provider_cap = min(concurrency, self.provider_limit)
        initial = provider_cap if provider_cap <= 3 else max(1, provider_cap // 2)
        self._adaptive = AdaptiveConcurrency(
            max_limit=provider_cap,
            initial_limit=initial,
            target_queue_ms=self.target_queue_ms,
            increase_after=1,
        )
        self._quality_result = None
        self._quality_last_items = -1
        self._specs_by_item = defaultdict(list)
        for spec in specs:
            self._specs_by_item[spec.item_id].append(spec)
        if dag is not None:
            durations = {
                spec.task_id: self.trace.duration_ms(
                    task_id=spec.task_id,
                    item_id=spec.item_id,
                    model_id=model_id,
                    kind=str(spec.kind),
                )
                for spec in specs
            }
            self._remaining = remaining_paths(dag, durations)
        else:
            self._remaining = {}
        result = await super().run(
            strategy,
            specs,
            model_id=model_id,
            concurrency=concurrency,
            dag=dag,
            early_stop_after=early_stop_after,
            cancel_after_ms=cancel_after_ms,
            run_id=run_id,
        )
        result["provider_aware"] = self.adaptive_enabled or self.priority_enabled
        result["adaptive_enabled"] = self.adaptive_enabled
        result["priority_enabled"] = self.priority_enabled
        result["variant"] = (
            "P_provider_aware"
            if self.adaptive_enabled or self.priority_enabled
            else "C_baseline"
            if strategy is Strategy.C_LAYERED
            else "D_baseline"
        )
        result["adaptive_limit_final"] = (
            self._adaptive.limit if self.adaptive_enabled and self._adaptive else None
        )
        result["adaptive_limit_history"] = (
            list(self._adaptive.history)
            if self.adaptive_enabled and self._adaptive
            else []
        )
        result["adaptive_increases"] = (
            self._adaptive.increases if self.adaptive_enabled and self._adaptive else 0
        )
        result["adaptive_decreases"] = (
            self._adaptive.decreases if self.adaptive_enabled and self._adaptive else 0
        )
        result["quality_gate"] = self._quality_result
        return result

    async def _run_ready_queue(
        self, specs: Sequence[TaskSpec], dag: Dag, concurrency: int
    ) -> list[Attempt]:
        assert dag is not None
        by_id = {spec.task_id: spec for spec in specs}
        indegree = {
            task_id: len(node.depends_on) for task_id, node in dag.nodes.items()
        }
        dependents = dag.dependents
        ready: deque[str] | list[tuple[float, str]]
        if self.priority_enabled:
            ready = [
                (-self._remaining.get(task_id, 0.0), task_id)
                for task_id in dag.initial_ready()
            ]
            heapq.heapify(ready)
        else:
            ready = deque(dag.initial_ready())
        ready_at: dict[str, int] = {
            task_id: self._start_ns for task_id in dag.initial_ready()
        }
        running: set[asyncio.Task[Attempt]] = set()
        task_specs: dict[asyncio.Task[Attempt], tuple[TaskSpec, int]] = {}
        results: list[Attempt] = []
        start_ns = self._start_ns

        def push_ready(task_id: str) -> None:
            if self.priority_enabled:
                heapq.heappush(
                    ready, (-self._remaining.get(task_id, 0.0), task_id)
                )
            else:
                ready.append(task_id)  # type: ignore[union-attr]

        def pop_ready() -> str | None:
            if self.priority_enabled:
                return heapq.heappop(ready)[1] if ready else None  # type: ignore[arg-type]
            return ready.popleft() if ready else None  # type: ignore[union-attr]

        def ready_len() -> int:
            return len(ready)

        while (ready_len() or running) and not self._controller.is_stopped():
            limit = (
                self._adaptive.limit
                if self.adaptive_enabled and self._adaptive is not None
                else concurrency
            )
            while (
                ready_len()
                and len(running) < limit
                and not self._controller.is_stopped()
            ):
                task_id = pop_ready()
                if task_id is None:
                    break
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
                if self.adaptive_enabled and self._adaptive is not None:
                    self._adaptive.observe(
                        queue_wait_ms=outcome.queue_wait_ms,
                        elapsed_ms=(completed_at - start_ns) / 1_000_000.0,
                    )
                if not outcome.cancelled:
                    for dependent in dependents.get(outcome.task_id, ()):
                        indegree[dependent] -= 1
                        if indegree[dependent] == 0:
                            ready_at[dependent] = completed_at
                            push_ready(dependent)
                self._maybe_quality_stop(results, specs)
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
            for (spec, _), outcome in zip(entries, outcomes):
                results.append(
                    self._coerce_outcome(outcome, spec, ready_ns)
                )
            self._maybe_quality_stop(results, specs)
            if self._controller.is_stopped():
                break
        return results

    def _maybe_quality_stop(
        self, results: Sequence[Attempt], specs: Sequence[TaskSpec]
    ) -> None:
        if self.quality_gate is None or self._controller.is_stopped():
            return
        by_id = {attempt.task_id: attempt for attempt in results}
        evaluation = self.quality_gate.evaluate(
            specs,
            by_id,
            specs_by_item=self._specs_by_item,
        )
        if evaluation["completed_items"] == self._quality_last_items:
            return
        self._quality_last_items = evaluation["completed_items"]
        self._quality_result = evaluation
        if evaluation["met"]:
            self._controller.stop("quality_gate")


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _frozen_hashes(root: Path) -> dict[str, str]:
    return {
        name: hashlib.sha256((root / name).read_bytes()).hexdigest()
        for name in FROZEN_FILES
    }


def _build_trace(
    experiment: Mapping[str, Any],
    async_config: Mapping[str, Any],
    *,
    scale: float,
    straggler_extra_ms: float | None = None,
) -> LatencyTrace:
    trace_cfg = async_config["trace"]
    seed = int(trace_cfg["seed"])
    suite = load_fixtures(
        Path(__file__).resolve().parents[1] / experiment["fixtures"]["gold"],
        expected_sha256=experiment["fixtures"]["sha256"],
    )
    straggler_items = choose_straggler_items(
        [item.item_id for item in suite.items],
        count=int(trace_cfg.get("straggler_count", 0)),
        seed=seed,
    )
    return LatencyTrace(
        base_ms={str(k): float(v) for k, v in trace_cfg["base_ms"].items()},
        model_multiplier={
            str(k): float(v) for k, v in trace_cfg["model_multiplier"].items()
        },
        jitter_fraction=float(trace_cfg.get("jitter_fraction", 0.0)),
        straggler_items=straggler_items,
        straggler_extra_ms=float(
            trace_cfg.get("straggler_extra_ms", 0.0)
            if straggler_extra_ms is None
            else straggler_extra_ms
        ),
        seed=seed,
        scale=scale,
        extra_ms_by_task={
            str(k): float(v)
            for k, v in trace_cfg.get("extra_ms_by_task", {}).items()
        },
    )


async def run_adaptive_benchmark(
    experiment_config_path: Path,
    async_config_path: Path,
    adaptive_config_path: Path,
    *,
    scale: float | None = None,
) -> dict[str, Any]:
    experiment = _load_json(experiment_config_path)
    async_config = _load_json(async_config_path)
    adaptive_config = _load_json(adaptive_config_path)
    root = experiment_config_path.resolve().parent.parent
    suite = load_fixtures(
        root / experiment["fixtures"]["gold"],
        expected_sha256=experiment["fixtures"]["sha256"],
    )
    models = {str(model["id"]): model for model in experiment["models"]}
    contracts = experiment["contracts"]
    model_a = str(async_config["models"]["A"])
    model_bcd = str(async_config["models"]["BCD"])
    provider_limit_default = int(async_config.get("provider_rate_limit", 3))
    allocated = int(
        async_config.get(
            "run_allocated_milli", experiment["sweycoins"]["run_allocated_milli"]
        )
    )
    late_fraction = float(async_config.get("late_billing_fraction", 0.5))
    specs_a = build_task_specs(suite, Kind.COMBINED)
    specs_bcd = all_microtask_specs(suite)
    dag = build_dag(specs_bcd)
    quality_cfg = adaptive_config.get("quality_gate", {})
    quality_gate = QualityGate(
        min_items=int(quality_cfg.get("min_items", 5)),
        min_accuracy=float(quality_cfg.get("min_accuracy", 0.8)),
        min_coverage=float(quality_cfg.get("min_coverage", 0.8)),
        max_false_support=float(quality_cfg.get("max_false_support", 0.2)),
    )
    trace_scale = float(scale if scale is not None else adaptive_config.get("scale", 1.0))
    trace = _build_trace(experiment, async_config, scale=trace_scale)
    inner = OfflineHeuristicProvider(
        model_ids=[model_a, model_bcd], simulated_latency_ms=0.0
    )
    provider = TraceProvider(inner, trace)
    target_queue_ms = float(adaptive_config.get("target_queue_ms", 400.0))
    levels = [int(value) for value in adaptive_config["concurrency_levels"]]
    limits = [int(value) for value in adaptive_config["provider_rate_limits"]]
    runs: list[dict[str, Any]] = []

    def baseline(provider_limit: int):
        return AsyncBenchmarkRunner(
            provider=provider,
            models=models,
            contracts=contracts,
            trace=trace,
            provider_limit=provider_limit,
            allocated_milli=allocated,
            late_billing_fraction=late_fraction,
        )

    def aware(
        provider_limit: int,
        *,
        adaptive: bool,
        priority: bool,
        gate: QualityGate | None = None,
    ):
        return ProviderAwareRunner(
            provider=provider,
            models=models,
            contracts=contracts,
            trace=trace,
            provider_limit=provider_limit,
            allocated_milli=allocated,
            late_billing_fraction=late_fraction,
            adaptive=adaptive,
            priority=priority,
            quality_gate=gate,
            target_queue_ms=target_queue_ms,
        )

    for limit in limits:
        for concurrency in levels:
            runs.append(
                await baseline(limit).run(
                    Strategy.C_LAYERED,
                    specs_bcd,
                    model_id=model_bcd,
                    concurrency=concurrency,
                    dag=dag,
                    run_id=f"adaptive:C:{limit}:{concurrency}",
                )
            )
            runs[-1]["provider_aware"] = False
            runs[-1]["adaptive_enabled"] = False
            runs[-1]["priority_enabled"] = False
            runs[-1]["scenario"] = "main"
            runs[-1]["variant"] = "C_baseline"
            runs.append(
                await baseline(limit).run(
                    Strategy.D_EVENT_READY,
                    specs_bcd,
                    model_id=model_bcd,
                    concurrency=concurrency,
                    dag=dag,
                    run_id=f"adaptive:D:{limit}:{concurrency}",
                )
            )
            runs[-1]["provider_aware"] = False
            runs[-1]["adaptive_enabled"] = False
            runs[-1]["priority_enabled"] = False
            runs[-1]["scenario"] = "main"
            runs[-1]["variant"] = "D_baseline"
            p_run = await aware(
                limit, adaptive=True, priority=True
            ).run(
                Strategy.D_EVENT_READY,
                specs_bcd,
                model_id=model_bcd,
                concurrency=concurrency,
                dag=dag,
                run_id=f"adaptive:P:{limit}:{concurrency}",
            )
            p_run["scenario"] = "main"
            runs.append(p_run)

    scenario_cfg = adaptive_config.get("scenarios", {})
    heavy = scenario_cfg.get("heavy_straggler")
    if heavy:
        heavy_trace = _build_trace(
            experiment,
            async_config,
            scale=trace_scale,
            straggler_extra_ms=float(heavy["straggler_extra_ms"]),
        )
        heavy_provider = TraceProvider(inner, heavy_trace)
        h_limit = int(heavy.get("provider_limit", provider_limit_default))
        h_conc = int(heavy.get("concurrency", 8))
        for name, strategy, adaptive, priority, gate in (
            ("heavy_C", Strategy.C_LAYERED, False, False, None),
            ("heavy_D", Strategy.D_EVENT_READY, False, False, None),
            ("heavy_P", Strategy.D_EVENT_READY, True, True, None),
        ):
            runner_cls = AsyncBenchmarkRunner if name != "heavy_P" else ProviderAwareRunner
            kwargs: dict[str, Any] = {}
            if name == "heavy_P":
                kwargs = {
                    "adaptive": adaptive,
                    "priority": priority,
                    "quality_gate": gate,
                    "target_queue_ms": target_queue_ms,
                }
            runner = runner_cls(
                provider=heavy_provider,
                models=models,
                contracts=contracts,
                trace=heavy_trace,
                provider_limit=h_limit,
                allocated_milli=allocated,
                late_billing_fraction=late_fraction,
                **kwargs,
            )
            result = await runner.run(
                strategy,
                specs_bcd,
                model_id=model_bcd,
                concurrency=h_conc,
                dag=dag,
                run_id=f"adaptive:{name}",
            )
            result["scenario"] = name
            result["variant"] = "P_provider_aware" if name == "heavy_P" else ("C_baseline" if name == "heavy_C" else "D_baseline")
            result["provider_aware"] = name == "heavy_P"
            result["adaptive_enabled"] = name == "heavy_P"
            result["priority_enabled"] = name == "heavy_P"
            runs.append(result)

    quality_cfg = scenario_cfg.get("quality_stop")
    if quality_cfg:
        q_limit = int(quality_cfg.get("provider_limit", provider_limit_default))
        q_conc = int(quality_cfg.get("concurrency", 8))
        for name, strategy, adaptive, priority in (
            ("quality_C", Strategy.C_LAYERED, False, False),
            ("quality_D", Strategy.D_EVENT_READY, False, False),
            ("quality_P", Strategy.D_EVENT_READY, True, True),
        ):
            runner = aware(
                q_limit,
                adaptive=adaptive,
                priority=priority,
                gate=quality_gate,
            )
            result = await runner.run(
                strategy,
                specs_bcd,
                model_id=model_bcd,
                concurrency=q_conc,
                dag=dag,
                run_id=f"adaptive:{name}",
            )
            result["scenario"] = name
            runs.append(result)

    count_cfg = scenario_cfg.get("count_stop")
    if count_cfg:
        c_limit = int(count_cfg.get("provider_limit", provider_limit_default))
        c_conc = int(count_cfg.get("concurrency", 8))
        for name, strategy, adaptive, priority in (
            ("count_C", Strategy.C_LAYERED, False, False),
            ("count_D", Strategy.D_EVENT_READY, False, False),
            ("count_P", Strategy.D_EVENT_READY, True, True),
        ):
            runner = aware(c_limit, adaptive=adaptive, priority=priority)
            result = await runner.run(
                strategy,
                specs_bcd,
                model_id=model_bcd,
                concurrency=c_conc,
                dag=dag,
                early_stop_after=int(count_cfg.get("after_verified", 25)),
                run_id=f"adaptive:{name}",
            )
            result["scenario"] = name
            runs.append(result)

    for run in runs:
        run.setdefault("scenario", "main")
        run.setdefault("provider_aware", False)
        run.setdefault("adaptive_enabled", False)
        run.setdefault("priority_enabled", False)
        run.setdefault("adaptive_limit_final", None)
        run.setdefault("adaptive_limit_history", [])
        run.setdefault("adaptive_increases", 0)
        run.setdefault("adaptive_decreases", 0)
        run.setdefault("quality_gate", None)
    return {
        "schema_version": 1,
        "experiment_id": "swe-microtask-swarm-adaptive",
        "evidence_kind": "offline_adaptive_trace",
        "generated_at": datetime.now(UTC).isoformat(),
        "fixture_sha256": suite.sha256,
        "models": {"C/D": model_bcd, "P": model_bcd, "A_reference": model_a},
        "provider_rate_limits": limits,
        "concurrency_levels": levels,
        "target_queue_ms": target_queue_ms,
        "quality_gate": {
            "min_items": quality_gate.min_items,
            "min_accuracy": quality_gate.min_accuracy,
            "min_coverage": quality_gate.min_coverage,
            "max_false_support": quality_gate.max_false_support,
        },
        "trace": {
            "seed": trace.seed,
            "base_ms": trace.base_ms,
            "model_multiplier": trace.model_multiplier,
            "jitter_fraction": trace.jitter_fraction,
            "straggler_items": sorted(trace.straggler_items),
            "straggler_extra_ms": trace.straggler_extra_ms,
            "scale": trace.scale,
        },
        "dag_layers": [list(layer) for layer in dag.layers],
        "frozen_baseline_sha256": _frozen_hashes(root),
        "runs": runs,
        "summary": _summarize(runs),
        "totals": _totals(runs),
        "notes": [
            "Offline deterministic trace; no model or network call.",
            "C and D are the frozen baseline; P is provider-aware adaptive + critical-path priority.",
            "Quality-gated stop uses accuracy, extraction coverage, and false-support rate on completed items.",
            "The runtime budget is still the local pilot meter; SWE PR #11 remains unattached.",
        ],
    }


def _summarize(runs: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    by_key: dict[str, Any] = {}
    for run in runs:
        if run.get("scenario") != "main":
            continue
        strategy = str(run["strategy"])
        label = (
            "P_provider_aware"
            if run.get("provider_aware") and strategy == "D_event_ready"
            else "C_baseline"
            if strategy == "C_layered_barrier"
            else "D_baseline"
        )
        key = f"{label}:p{run['provider_rate_limit']}"
        current = by_key.get(key)
        candidate = (
            float(run["wall_ms"]),
            -float(run["quality"]["accuracy"]),
        )
        if current is None or candidate < current["_key"]:
            by_key[key] = {
                "_key": candidate,
                "label": label,
                "strategy": strategy,
                "provider_limit": run["provider_rate_limit"],
                "concurrency": run["concurrency"],
                "wall_ms": run["wall_ms"],
                "first_verified_ms": run["first_verified_ms"],
                "p95_ms": run["task_latency_p95_ms"],
                "critical_path_ms": run["critical_path_ms"],
                "worker_utilization": run["worker_utilization"],
                "provider_utilization": run["provider_utilization"],
                "adaptive_limit_final": run.get("adaptive_limit_final"),
                "accuracy": run["quality"]["accuracy"],
                "coverage": run["quality"]["extraction"]["coverage"],
                "false_support_rate": run["quality"]["false_support"][
                    "false_support_rate"
                ],
                "calls": run["call_count"],
                "settled_milli": run["sweycoins"]["settled_milli"],
            }
    for value in by_key.values():
        value.pop("_key", None)
    return {"run_count": len(runs), "best_by_strategy_and_limit": by_key}


def _totals(runs: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    calls = sum(int(run["call_count"]) for run in runs)
    settled = sum(int(run["sweycoins"]["settled_milli"]) for run in runs)
    pending = sum(
        int(run["sweycoins"]["pending_before_reconcile_milli"]) for run in runs
    )
    late = sum(int(run["sweycoins"]["late_billed_milli"]) for run in runs)
    released = sum(int(run["sweycoins"]["released_milli"]) for run in runs)
    calculated = 0.0
    for run in runs:
        value = run["cost"]["all_in"]["calculated_usd"]
        if value != "N/A":
            calculated += float(value)
        else:
            complete = run["cost"]["all_in_complete_calls_calculated_usd"]
            if complete != "N/A":
                calculated += float(complete)
    return {
        "calls": calls,
        "settled_milli": settled,
        "pending_before_reconcile_milli": pending,
        "late_billed_milli": late,
        "released_milli": released,
        "calculated_usd": round(calculated, 9),
        "actual_usd": "N/A",
        "note": "Local pilot budget; SWE PR #11 runtime budget is not attached.",
    }


def write_adaptive_report(report: Mapping[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def render_adaptive_markdown(report: Mapping[str, Any]) -> str:
    lines = [
        "# Provider-aware adaptive/critical-path analysis (offline)",
        "",
        f"Generated: {report['generated_at']}",
        f"Fixture SHA-256: `{report['fixture_sha256']}`",
        "C/D = frozen baseline; P = adaptive concurrency + critical-path priority.",
        "No model or network call was made.",
        "",
        "| Scenario | Variant | Strategy | Provider limit | Conc. | Wall ms | First verified ms | p50 ms | p95 ms | Critical path ms | Worker util | Provider util | Adaptive final | Accuracy | Coverage | False support | Calls | Settled milli | Pending before | Late milli | Released milli | Calc USD |",
        "| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for run in report["runs"]:
        budget = run["sweycoins"]
        lines.append(
            "| {scenario} | {variant} | {strategy} | {limit} | {conc} | {wall} | {first} | {p50} | {p95} | {cp} | {wu} | {pu} | {af} | {acc} | {cov} | {fs} | {calls} | {settled} | {pending} | {late} | {released} | {usd} |".format(
                scenario=run.get("scenario") or "main",
                variant=run.get("variant") or "unknown",
                strategy=run["strategy"],
                limit=run["provider_rate_limit"],
                conc=run["concurrency"],
                wall=run["wall_ms"],
                first=run["first_verified_ms"],
                p50=run["task_latency_p50_ms"],
                p95=run["task_latency_p95_ms"],
                cp=run["critical_path_ms"],
                wu=run["worker_utilization"],
                pu=run["provider_utilization"],
                af=run.get("adaptive_limit_final"),
                acc=run["quality"]["accuracy"],
                cov=run["quality"]["extraction"]["coverage"],
                fs=run["quality"]["false_support"]["false_support_rate"],
                calls=run["call_count"],
                settled=budget["settled_milli"],
                pending=budget["pending_before_reconcile_milli"],
                late=budget["late_billed_milli"],
                released=budget["released_milli"],
                usd=run["cost"]["all_in"]["calculated_usd"],
            )
        )
    totals = report["totals"]
    lines.extend(
        [
            "",
            "Totals: calls={calls}, settled={settled} milli, pending={pending} milli, "
            "late={late} milli, released={released} milli, calculated USD={usd}. "
            "actual_usd is N/A.".format(
                calls=totals["calls"],
                settled=totals["settled_milli"],
                pending=totals["pending_before_reconcile_milli"],
                late=totals["late_billed_milli"],
                released=totals["released_milli"],
                usd=totals["calculated_usd"],
            ),
            "",
            "Frozen baseline hashes are included in the JSON report.",
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
    "AdaptiveConcurrency",
    "FROZEN_FILES",
    "ProviderAwareRunner",
    "QualityGate",
    "remaining_paths",
    "render_adaptive_markdown",
    "run_adaptive_benchmark",
    "write_adaptive_report",
]
