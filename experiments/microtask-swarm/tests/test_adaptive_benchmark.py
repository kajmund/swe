from __future__ import annotations

import unittest

from microtask_swarm.adaptive_benchmark import (
    AdaptiveConcurrency,
    FROZEN_FILES,
    ProviderAwareRunner,
    QualityGate,
    _frozen_hashes,
    remaining_paths,
    run_adaptive_benchmark,
)
from microtask_swarm.async_benchmark import (
    LatencyTrace,
    Strategy,
    TraceProvider,
    build_dag,
)
from microtask_swarm.contracts import Kind
from microtask_swarm.fixtures import all_microtask_specs, load_fixtures
from microtask_swarm.offline import OfflineHeuristicProvider
from microtask_swarm.runner import load_config

from tests.support import CONFIG_PATH, ROOT

MODEL_BCD = "nemotron-3-super"

FROZEN_EXPECTED = {
    "microtask_swarm/async_benchmark.py": "30f47bf864791962f10da8583a46327cdf7850c7c03d3ec2b71fd86d7c0a710c",
    "config/async-benchmark.json": "dec26493555465b285cee546a7be7fec69161e4cc5ee4b6c3486df8f8b20582f",
    "reports/async-benchmark.json": "1b3da307bc6db34c5e418e12b9fae50abc149a31f412d5abf988822006d4b448",
    "reports/async-benchmark.md": "9a920a037c5d623bed8893eb4b5fe15a8fe0926fd569e7cd497cdaa0e68a474c",
    "tests/test_async_benchmark.py": "ea78b24cd99383cfd24a9044661c031b5a275b58e8e8576054a08c853eb439a3",
}


def _config():
    return load_config(CONFIG_PATH)


def _specs(items: int = 2):
    suite = load_fixtures(
        ROOT / _config()["fixtures"]["gold"],
        expected_sha256=_config()["fixtures"]["sha256"],
    )
    item_ids = {item.item_id for item in suite.items[:items]}
    return tuple(
        spec for spec in all_microtask_specs(suite) if spec.item_id in item_ids
    )


def _trace(
    *,
    extra_ms_by_task: dict[str, float] | None = None,
    scale: float = 1.0,
) -> LatencyTrace:
    return LatencyTrace(
        base_ms={
            "relevance": 1.0,
            "entailment": 1.0,
            "atomicity": 1.0,
            "extraction": 1.0,
            "operation_selection": 1.0,
            "combined": 1.0,
        },
        model_multiplier={MODEL_BCD: 1.0},
        jitter_fraction=0.0,
        straggler_items=frozenset(),
        straggler_extra_ms=0.0,
        seed=11,
        scale=scale,
        extra_ms_by_task=extra_ms_by_task or {},
    )


class RecordingProvider:
    def __init__(self, inner: TraceProvider) -> None:
        self.inner = inner
        self.calls: list[str] = []

    async def chat(self, request):
        self.calls.append(request.task_id)
        return await self.inner.chat(request)


def _runner(
    *,
    provider=None,
    trace: LatencyTrace | None = None,
    provider_limit: int = 2,
    adaptive: bool = True,
    priority: bool = True,
    quality_gate: QualityGate | None = None,
):
    config = _config()
    trace = trace or _trace()
    models = {model["id"]: model for model in config["models"]}
    if provider is None:
        inner = OfflineHeuristicProvider(
            model_ids=[MODEL_BCD], simulated_latency_ms=0.0
        )
        provider = TraceProvider(inner, trace)
    return ProviderAwareRunner(
        provider=provider,
        models=models,
        contracts=config["contracts"],
        trace=trace,
        provider_limit=provider_limit,
        allocated_milli=250000,
        late_billing_fraction=1.0,
        adaptive=adaptive,
        priority=priority,
        quality_gate=quality_gate,
        target_queue_ms=0.5,
    )


class AdaptiveConcurrencyTests(unittest.TestCase):
    def test_aimd_increases_and_decreases(self) -> None:
        controller = AdaptiveConcurrency(
            max_limit=8, target_queue_ms=100.0, increase_after=2
        )
        controller.observe(queue_wait_ms=10.0, elapsed_ms=1.0)
        controller.observe(queue_wait_ms=10.0, elapsed_ms=2.0)
        self.assertEqual(controller.limit, 2)
        controller.observe(queue_wait_ms=1000.0, elapsed_ms=3.0)
        self.assertEqual(controller.limit, 1)
        self.assertGreaterEqual(controller.increases, 1)
        self.assertGreaterEqual(controller.decreases, 1)
        self.assertGreaterEqual(len(controller.history), 3)

    def test_remaining_paths_include_downstream(self) -> None:
        specs = _specs(items=2)
        dag = build_dag(specs)
        durations = {spec.task_id: 1.0 for spec in specs}
        remaining = remaining_paths(dag, durations)
        self.assertGreater(
            remaining["bio-01:relevance"], remaining["bio-01:operation_selection"]
        )
        self.assertEqual(remaining["bio-01:operation_selection"], 1.0)


class ProviderAwareTests(unittest.IsolatedAsyncioTestCase):
    async def test_priority_starts_longest_remaining_chain(self) -> None:
        # bio-01 has a long extraction; bio-02 has a short one.
        trace = _trace(extra_ms_by_task={"bio-01:extraction": 50.0})
        inner = OfflineHeuristicProvider(
            model_ids=[MODEL_BCD], simulated_latency_ms=0.0
        )
        recording = RecordingProvider(TraceProvider(inner, trace))
        runner = _runner(provider=recording, trace=trace, priority=True)
        specs = _specs(items=2)
        dag = build_dag(specs)
        await runner.run(
            Strategy.D_EVENT_READY,
            specs,
            model_id=MODEL_BCD,
            concurrency=1,
            dag=dag,
        )
        self.assertEqual(recording.calls[0], "bio-01:relevance")

    async def test_provider_aware_respects_limits_and_records_adaptation(self) -> None:
        specs = _specs(items=2)
        dag = build_dag(specs)
        trace = _trace()
        run = await _runner(
            trace=trace, provider_limit=2, adaptive=True, priority=True
        ).run(
            Strategy.D_EVENT_READY,
            specs,
            model_id=MODEL_BCD,
            concurrency=16,
            dag=dag,
        )
        self.assertLessEqual(run["peak_provider"], 2)
        self.assertLessEqual(run["peak_worker"], 16)
        self.assertLessEqual(run["adaptive_limit_final"], 2)
        self.assertTrue(run["adaptive_limit_history"])
        self.assertTrue(run["adaptive_enabled"])
        self.assertTrue(run["priority_enabled"])

    async def test_quality_gate_stops_on_measured_quality(self) -> None:
        specs = _specs(items=2)
        dag = build_dag(specs)
        gate = QualityGate(
            min_items=1,
            min_accuracy=0.0,
            min_coverage=0.0,
            max_false_support=1.0,
        )
        run = await _runner(
            trace=_trace(), quality_gate=gate
        ).run(
            Strategy.D_EVENT_READY,
            specs,
            model_id=MODEL_BCD,
            concurrency=4,
            dag=dag,
        )
        self.assertEqual(run["stop_reason"], "quality_gate")
        self.assertIsNotNone(run["quality_gate"])
        self.assertTrue(run["quality_gate"]["met"])
        self.assertGreaterEqual(run["quality_gate"]["completed_items"], 1)

    async def test_count_stop_is_still_available_for_comparison(self) -> None:
        specs = _specs(items=2)
        dag = build_dag(specs)
        run = await _runner(
            trace=_trace(), adaptive=False, priority=False
        ).run(
            Strategy.D_EVENT_READY,
            specs,
            model_id=MODEL_BCD,
            concurrency=4,
            dag=dag,
            early_stop_after=5,
        )
        self.assertEqual(run["stop_reason"], "early_stop")
        self.assertGreaterEqual(run["verified_count"], 5)


class FrozenBaselineTests(unittest.TestCase):
    def test_frozen_baseline_hashes_are_unchanged(self) -> None:
        hashes = _frozen_hashes(ROOT)
        for name in FROZEN_FILES:
            self.assertEqual(hashes[name], FROZEN_EXPECTED[name], name)


class AdaptiveMatrixTests(unittest.IsolatedAsyncioTestCase):
    async def test_matrix_smoke(self) -> None:
        report = await run_adaptive_benchmark(
            CONFIG_PATH,
            ROOT / "config" / "async-benchmark.json",
            ROOT / "config" / "adaptive-benchmark.json",
            scale=0.002,
        )
        self.assertEqual(report["evidence_kind"], "offline_adaptive_trace")
        self.assertEqual(
            report["frozen_baseline_sha256"],
            FROZEN_EXPECTED,
        )
        scenarios = {run["scenario"] for run in report["runs"]}
        self.assertIn("main", scenarios)
        self.assertIn("heavy_P", scenarios)
        self.assertIn("quality_P", scenarios)
        self.assertIn("count_P", scenarios)
        main_p = [
            run
            for run in report["runs"]
            if run["scenario"] == "main" and run["strategy"] == "D_event_ready" and run["provider_aware"]
        ]
        self.assertTrue(main_p)
        self.assertTrue(all(run["adaptive_limit_history"] for run in main_p))
        quality_p = [
            run
            for run in report["runs"]
            if run["scenario"] == "quality_P"
        ][0]
        self.assertEqual(quality_p["stop_reason"], "quality_gate")
        self.assertIsNotNone(quality_p["quality_gate"])


if __name__ == "__main__":
    unittest.main()
