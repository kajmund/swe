from __future__ import annotations

import json
import unittest
from unittest import mock

from microtask_swarm.async_benchmark import (
    AsyncBenchmarkRunner,
    LatencyTrace,
    Strategy,
    TraceProvider,
    build_dag,
    choose_straggler_items,
)
from microtask_swarm.contracts import Kind
from microtask_swarm.fixtures import (
    all_microtask_specs,
    build_task_specs,
    load_fixtures,
)
from microtask_swarm.offline import OfflineHeuristicProvider
from microtask_swarm.runner import load_config

from tests.support import CONFIG_PATH, ROOT

MODEL_A = "deepseek-v4.1-flash"
MODEL_BCD = "nemotron-3-super"


def _config():
    return load_config(CONFIG_PATH)


def _suite(config=None):
    config = config or _config()
    return load_fixtures(
        ROOT / config["fixtures"]["gold"],
        expected_sha256=config["fixtures"]["sha256"],
    )


def _specs(kind: Kind, items: int = 2):
    suite = _suite()
    item_ids = {item.item_id for item in suite.items[:items]}
    return tuple(
        spec for spec in build_task_specs(suite, kind) if spec.item_id in item_ids
    )


def _trace(
    *,
    base_ms: dict[str, float] | None = None,
    extra_ms_by_task: dict[str, float] | None = None,
    straggler_items: frozenset[str] | None = None,
    straggler_extra_ms: float = 0.0,
    scale: float = 1.0,
    jitter: float = 0.0,
) -> LatencyTrace:
    return LatencyTrace(
        base_ms=base_ms
        or {
            "relevance": 1.0,
            "entailment": 1.0,
            "atomicity": 1.0,
            "extraction": 1.0,
            "operation_selection": 1.0,
            "combined": 2.0,
        },
        model_multiplier={MODEL_A: 1.0, MODEL_BCD: 1.0},
        jitter_fraction=jitter,
        straggler_items=straggler_items or frozenset(),
        straggler_extra_ms=straggler_extra_ms,
        seed=7,
        scale=scale,
        extra_ms_by_task=extra_ms_by_task or {},
    )


def _runner(
    trace: LatencyTrace,
    *,
    provider_limit: int = 2,
    late_billing_fraction: float = 1.0,
) -> AsyncBenchmarkRunner:
    config = _config()
    models = {model["id"]: model for model in config["models"]}
    inner = OfflineHeuristicProvider(
        model_ids=[MODEL_A, MODEL_BCD], simulated_latency_ms=0.0
    )
    return AsyncBenchmarkRunner(
        provider=TraceProvider(inner, trace),
        models=models,
        contracts=config["contracts"],
        trace=trace,
        provider_limit=provider_limit,
        allocated_milli=250000,
        late_billing_fraction=late_billing_fraction,
    )


class TraceAndDagTests(unittest.TestCase):
    def test_trace_is_deterministic(self) -> None:
        trace = _trace()
        first = trace.duration_ms(
            task_id="bio-01:relevance",
            item_id="bio-01",
            model_id=MODEL_BCD,
            kind="relevance",
        )
        second = trace.duration_ms(
            task_id="bio-01:relevance",
            item_id="bio-01",
            model_id=MODEL_BCD,
            kind="relevance",
        )
        self.assertEqual(first, second)

    def test_straggler_selection_is_stable(self) -> None:
        first = choose_straggler_items(
            ["a", "b", "c", "d"], count=2, seed=1
        )
        second = choose_straggler_items(
            ["d", "c", "b", "a"], count=2, seed=1
        )
        self.assertEqual(first, second)
        self.assertEqual(len(first), 2)

    def test_dag_layers_and_critical_path(self) -> None:
        specs = _specs(Kind.RELEVANCE, items=2) + tuple(
            spec
            for spec in all_microtask_specs(_suite())
            if spec.item_id in {"bio-01", "bio-02"}
            and spec.kind is not Kind.RELEVANCE
        )
        dag = build_dag(specs)
        self.assertEqual(len(dag.layers), 4)
        self.assertEqual(len(dag.layers[0]), 2)  # relevance
        self.assertEqual(len(dag.layers[1]), 2)  # extraction
        self.assertEqual(len(dag.layers[2]), 4)  # entailment + atomicity
        self.assertEqual(len(dag.layers[3]), 2)  # operation selection
        durations = {task_id: 1.0 for task_id in dag.nodes}
        self.assertEqual(dag.longest_path_ms(durations), 4.0)


class SchedulingTests(unittest.IsolatedAsyncioTestCase):
    async def test_A_uses_one_combined_call_per_item(self) -> None:
        specs = _specs(Kind.COMBINED, items=2)
        runner = _runner(_trace())
        run = await runner.run(
            Strategy.A_SINGLE_LARGE,
            specs,
            model_id=MODEL_A,
            concurrency=2,
        )
        self.assertEqual(run["call_count"], 2)
        self.assertEqual(run["scored_subtasks"], 10)
        self.assertEqual(run["sweycoins"]["settled_milli"], 2500)
        self.assertEqual(run["cancelled_tasks"], 0)

    async def test_B_is_sequential(self) -> None:
        specs = _specs(Kind.RELEVANCE, items=2)
        specs = tuple(
            spec
            for spec in all_microtask_specs(_suite())
            if spec.item_id in {"bio-01", "bio-02"}
        )
        dag = build_dag(specs)
        runner = _runner(_trace())
        run = await runner.run(
            Strategy.B_SEQUENTIAL,
            specs,
            model_id=MODEL_BCD,
            concurrency=1,
            dag=dag,
        )
        self.assertEqual(run["call_count"], 10)
        self.assertEqual(run["peak_worker"], 1)
        self.assertEqual(run["cancelled_tasks"], 0)

    async def test_C_and_D_respect_worker_and_provider_limits(self) -> None:
        specs = tuple(
            spec
            for spec in all_microtask_specs(_suite())
            if spec.item_id in {"bio-01", "bio-02"}
        )
        dag = build_dag(specs)
        for strategy in (Strategy.C_LAYERED, Strategy.D_EVENT_READY):
            runner = _runner(_trace(), provider_limit=2)
            run = await runner.run(
                strategy,
                specs,
                model_id=MODEL_BCD,
                concurrency=4,
                dag=dag,
            )
            self.assertLessEqual(run["peak_worker"], 4)
            self.assertLessEqual(run["peak_provider"], 2)
            self.assertEqual(run["call_count"], 10)
            self.assertEqual(run["quality"]["scored_subtasks"], 10)

    async def test_D_beats_C_when_a_straggler_blocks_a_barrier(self) -> None:
        # Item 1 has a slow relevance task; item 2 has a slow extraction task.
        # C waits for item 1's relevance before starting any extraction.
        # D starts item 2's extraction as soon as its own relevance is done.
        extra = {
            "bio-01:relevance": 30.0,
            "bio-02:extraction": 90.0,
        }
        specs = tuple(
            spec
            for spec in all_microtask_specs(_suite())
            if spec.item_id in {"bio-01", "bio-02"}
        )
        dag = build_dag(specs)
        trace = _trace(extra_ms_by_task=extra)
        c_run = await _runner(trace, provider_limit=4).run(
            Strategy.C_LAYERED,
            specs,
            model_id=MODEL_BCD,
            concurrency=4,
            dag=dag,
        )
        d_run = await _runner(trace, provider_limit=4).run(
            Strategy.D_EVENT_READY,
            specs,
            model_id=MODEL_BCD,
            concurrency=4,
            dag=dag,
        )
        self.assertLess(d_run["wall_ms"], c_run["wall_ms"])
        self.assertLess(
            d_run["item_end_to_end_p95_ms"], c_run["item_end_to_end_p95_ms"]
        )

    async def test_early_stop_and_pending_late_billing(self) -> None:
        specs = tuple(
            spec
            for spec in all_microtask_specs(_suite())
            if spec.item_id in {"bio-01", "bio-02"}
        )
        dag = build_dag(specs)
        trace = _trace(
            base_ms={
                "relevance": 10.0,
                "entailment": 10.0,
                "atomicity": 10.0,
                "extraction": 10.0,
                "operation_selection": 10.0,
                "combined": 10.0,
            }
        )
        run = await _runner(trace, provider_limit=2, late_billing_fraction=1.0).run(
            Strategy.D_EVENT_READY,
            specs,
            model_id=MODEL_BCD,
            concurrency=3,
            dag=dag,
            early_stop_after=3,
        )
        budget = run["sweycoins"]
        self.assertTrue(run["stopped_early"])
        self.assertEqual(run["stop_reason"], "early_stop")
        self.assertGreaterEqual(run["verified_count"], 3)
        self.assertGreater(run["unscheduled_tasks"] + run["cancelled_tasks"], 0)
        self.assertGreaterEqual(budget["pending_before_reconcile_milli"], 0)
        self.assertEqual(
            budget["late_billed_milli"] + budget["released_milli"],
            budget["pending_before_reconcile_milli"],
        )
        self.assertEqual(budget["pending_after_reconcile_milli"], 0)
        self.assertEqual(
            budget["total_exposure_milli"], budget["settled_milli"]
        )

    async def test_cancellation_accounts_for_pending(self) -> None:
        specs = tuple(
            spec
            for spec in all_microtask_specs(_suite())
            if spec.item_id in {"bio-01", "bio-02"}
        )
        dag = build_dag(specs)
        trace = _trace(
            base_ms={
                "relevance": 20.0,
                "entailment": 20.0,
                "atomicity": 20.0,
                "extraction": 20.0,
                "operation_selection": 20.0,
                "combined": 20.0,
            }
        )
        run = await _runner(trace, provider_limit=2, late_billing_fraction=0.5).run(
            Strategy.D_EVENT_READY,
            specs,
            model_id=MODEL_BCD,
            concurrency=3,
            dag=dag,
            cancel_after_ms=5.0,
        )
        budget = run["sweycoins"]
        self.assertEqual(run["stop_reason"], "cancelled")
        self.assertGreater(run["cancelled_tasks"], 0)
        self.assertGreater(budget["pending_before_reconcile_milli"], 0)
        self.assertEqual(
            budget["late_billed_milli"] + budget["released_milli"],
            budget["pending_before_reconcile_milli"],
        )
        self.assertEqual(budget["pending_after_reconcile_milli"], 0)

    async def test_reproducible_quality_without_network(self) -> None:
        specs = tuple(
            spec
            for spec in all_microtask_specs(_suite())
            if spec.item_id in {"bio-01", "bio-02"}
        )
        dag = build_dag(specs)
        trace = _trace()
        with mock.patch(
            "urllib.request.urlopen", side_effect=AssertionError("network")
        ):
            first = await _runner(trace).run(
                Strategy.D_EVENT_READY,
                specs,
                model_id=MODEL_BCD,
                concurrency=3,
                dag=dag,
            )
            second = await _runner(trace).run(
                Strategy.D_EVENT_READY,
                specs,
                model_id=MODEL_BCD,
                concurrency=3,
                dag=dag,
            )
        self.assertEqual(first["call_count"], second["call_count"])
        self.assertEqual(
            first["quality"]["accuracy"], second["quality"]["accuracy"]
        )
        self.assertEqual(
            first["quality"]["extraction"]["coverage"],
            second["quality"]["extraction"]["coverage"],
        )


class AsyncMatrixTests(unittest.IsolatedAsyncioTestCase):
    async def test_matrix_is_reproducible_and_marks_scenarios(self) -> None:
        from microtask_swarm.async_benchmark import run_async_benchmark

        report = await run_async_benchmark(
            CONFIG_PATH,
            ROOT / "config" / "async-benchmark.json",
            scale=0.002,
        )
        self.assertEqual(report["evidence_kind"], "offline_deterministic_trace")
        self.assertEqual(report["fixture_microtask_count"], 50)
        self.assertEqual(len(report["dag_layers"]), 4)
        runs = report["runs"]
        self.assertTrue(any(run.get("scenario") for run in runs))
        main = [run for run in runs if run.get("scenario") is None]
        self.assertTrue(main)
        for run in main:
            self.assertEqual(run["quality"]["scored_subtasks"], 50)
            self.assertLessEqual(run["peak_worker"], run["concurrency"])
            self.assertLessEqual(
                run["peak_provider"], report["provider_rate_limit"]
            )
        self.assertIn("A_single_large", report["summary"]["best_by_strategy"])
        self.assertIn("D_event_ready", report["summary"]["best_by_strategy"])


if __name__ == "__main__":
    unittest.main()
