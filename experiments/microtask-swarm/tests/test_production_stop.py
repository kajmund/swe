from __future__ import annotations

import dataclasses
import unittest

from microtask_swarm.async_benchmark import LatencyTrace, Strategy, TraceProvider, build_dag
from microtask_swarm.contracts import Kind, Verification, verify_output
from microtask_swarm.fixtures import all_microtask_specs, load_fixtures
from microtask_swarm.offline import OfflineHeuristicProvider
from microtask_swarm.production_stop import (
    FROZEN_EXPECTED,
    FROZEN_FILES,
    ProductionStopPolicy,
    ProductionStopRunner,
    StopSignals,
    _frozen_hashes,
    compute_signals,
    run_production_stop_benchmark,
    verifier_confidence,
)
from microtask_swarm.runner import load_config

from tests.support import CONFIG_PATH, ROOT

MODEL_BCD = "nemotron-3-super"


def _config():
    return load_config(CONFIG_PATH)


def _suite():
    config = _config()
    return load_fixtures(
        ROOT / config["fixtures"]["gold"],
        expected_sha256=config["fixtures"]["sha256"],
    )


def _specs(items: int = 2):
    suite = _suite()
    item_ids = {item.item_id for item in suite.items[:items]}
    return tuple(
        spec for spec in all_microtask_specs(suite) if spec.item_id in item_ids
    )


def _trace() -> LatencyTrace:
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
        seed=21,
        scale=1.0,
    )


def _answer(spec):
    if spec.kind is Kind.RELEVANCE:
        return {"relevant": True}
    if spec.kind is Kind.ENTAILMENT:
        return {"label": "entailment"}
    if spec.kind is Kind.ATOMICITY:
        return {"atomic": True}
    if spec.kind is Kind.OPERATION:
        return {"operation": "extract_quote"}
    quote = spec.passage.split(".")[0].strip() + "."
    return {"quotes": [quote]}


def _attempt(spec, output=None):
    from microtask_swarm.async_benchmark import Attempt

    output = _answer(spec) if output is None else output
    verification = verify_output(spec.kind, output, passage=spec.passage)
    return Attempt(
        task_id=spec.task_id,
        item_id=spec.item_id,
        kind=spec.kind,
        model_id=MODEL_BCD,
        reservation_id=1,
        tariff_milli=100,
        ready_ns=0,
        start_ns=1,
        end_ns=2,
        output=output,
        verification=verification,
    )


def _runner(*, stop_policy=None, adaptive=False, priority=False):
    config = _config()
    trace = _trace()
    inner = OfflineHeuristicProvider(
        model_ids=[MODEL_BCD], simulated_latency_ms=0.0
    )
    return ProductionStopRunner(
        provider=TraceProvider(inner, trace),
        models={model["id"]: model for model in config["models"]},
        contracts=config["contracts"],
        trace=trace,
        provider_limit=2,
        allocated_milli=250000,
        late_billing_fraction=1.0,
        adaptive=adaptive,
        priority=priority,
        quality_gate=None,
        target_queue_ms=0.5,
        stop_policy=stop_policy,
    )


class SignalTests(unittest.TestCase):
    def test_verifier_confidence_is_gold_free_and_contract_based(self) -> None:
        extraction = next(
            spec for spec in _specs(items=1) if spec.kind is Kind.EXTRACTION
        )
        high = _attempt(extraction)
        self.assertGreaterEqual(verifier_confidence(extraction, high), 0.5)
        empty = _attempt(extraction, {"quotes": []})
        self.assertEqual(verifier_confidence(extraction, empty), 0.0)
        unsupported = _attempt(extraction, {"quotes": ["not in the passage"]})
        self.assertEqual(verifier_confidence(extraction, unsupported), 0.0)
        # Replacing gold must not change the signal.
        stripped = dataclasses.replace(extraction, gold={})
        self.assertEqual(
            verifier_confidence(stripped, high),
            verifier_confidence(extraction, high),
        )

    def test_compute_signals_covers_needs_and_abstention(self) -> None:
        specs = _specs(items=2)
        by_item: dict[str, list] = {}
        for spec in specs:
            by_item.setdefault(spec.item_id, []).append(spec)
        first = specs[0].item_id
        attempts = [_attempt(spec) for spec in specs if spec.item_id == first]
        signals = compute_signals(specs, attempts, specs_by_item=by_item)
        self.assertEqual(signals.completed_items, 1)
        self.assertEqual(signals.covered_needs, 5)
        self.assertEqual(signals.need_coverage, 0.5)
        self.assertEqual(signals.evidence_total, 2)
        self.assertEqual(signals.evidence_verified, 1)
        self.assertEqual(signals.abstention_rate, 0.0)

        abstained = [
            _attempt(spec, {"quotes": []})
            if spec.kind is Kind.EXTRACTION
            else _attempt(spec)
            for spec in specs
            if spec.item_id == first
        ]
        signals = compute_signals(specs, abstained, specs_by_item=by_item)
        self.assertGreater(signals.abstention_rate, 0.0)
        self.assertEqual(signals.evidence_verified, 0)
        self.assertLess(signals.need_coverage, 0.5)

    def test_gold_does_not_affect_signals(self) -> None:
        specs = _specs(items=1)
        stripped = tuple(dataclasses.replace(spec, gold={}) for spec in specs)
        by_item = {spec.item_id: [spec] for spec in specs}
        attempts = [_attempt(spec) for spec in specs]
        first = compute_signals(specs, attempts, specs_by_item=by_item)
        second = compute_signals(stripped, attempts, specs_by_item=by_item)
        self.assertEqual(first.to_dict(), second.to_dict())

    def test_policy_evaluates_observed_signals(self) -> None:
        policy = ProductionStopPolicy(
            name="test",
            min_items=1,
            min_need_coverage=0.5,
            min_evidence_coverage=1.0,
            max_abstention=0.2,
            min_confidence=0.5,
        )
        signals = StopSignals(
            attempted=5,
            total_needs=10,
            covered_needs=5,
            need_coverage=0.5,
            evidence_verified=1,
            evidence_total=1,
            evidence_coverage=1.0,
            abstentions=0,
            invalid_outputs=0,
            abstention_rate=0.0,
            average_confidence=0.8,
            verified_count=5,
            completed_items=1,
            completed_item_ids=("bio-01",),
        )
        self.assertTrue(policy.evaluate(signals))


class RunnerTests(unittest.IsolatedAsyncioTestCase):
    async def test_production_policy_stops_with_posthoc_quality(self) -> None:
        specs = _specs(items=2)
        dag = build_dag(specs)
        policy = ProductionStopPolicy(
            name="early",
            min_items=1,
            min_need_coverage=0.0,
            min_evidence_coverage=0.0,
            max_abstention=1.0,
            min_confidence=0.0,
        )
        run = await _runner(stop_policy=policy).run(
            Strategy.D_EVENT_READY,
            specs,
            model_id=MODEL_BCD,
            concurrency=3,
            dag=dag,
        )
        self.assertEqual(run["stop_reason"], "production:early")
        self.assertIsNotNone(run["production_signals_at_stop"])
        self.assertIsNotNone(run["posthoc_stop_quality"])
        self.assertGreaterEqual(
            run["production_signals_at_stop"]["completed_items"], 1
        )

    async def test_count_stop_has_posthoc_quality_without_gold_in_signal(self) -> None:
        specs = _specs(items=2)
        dag = build_dag(specs)
        run = await _runner(stop_policy=None).run(
            Strategy.D_EVENT_READY,
            specs,
            model_id=MODEL_BCD,
            concurrency=3,
            dag=dag,
            early_stop_after=5,
        )
        self.assertEqual(run["stop_reason"], "early_stop")
        self.assertIsNotNone(run["production_signals_at_stop"])


class FrozenAndMatrixTests(unittest.IsolatedAsyncioTestCase):
    def test_frozen_baseline_hashes_are_unchanged(self) -> None:
        hashes = _frozen_hashes(ROOT)
        for name in FROZEN_FILES:
            self.assertEqual(hashes[name], FROZEN_EXPECTED[name], name)

    async def test_production_stop_matrix_smoke(self) -> None:
        report = await run_production_stop_benchmark(
            CONFIG_PATH,
            ROOT / "config" / "async-benchmark.json",
            ROOT / "config" / "production-stop.json",
            scale=0.002,
        )
        self.assertEqual(report["evidence_kind"], "offline_production_early_stop")
        self.assertEqual(report["frozen_baseline_sha256"], FROZEN_EXPECTED)
        self.assertEqual(len(report["runs"]), 10)
        conditions = {run["condition"] for run in report["runs"]}
        self.assertIn("full", conditions)
        self.assertIn("count_25", conditions)
        for name in ("coverage", "confidence", "hybrid"):
            self.assertIn(name, conditions)
        for run in report["runs"]:
            self.assertIn("production_signals_at_stop", run)
            self.assertIn("posthoc_stop_quality", run)
            if run["production_signals_at_stop"]["completed_items"] > 0:
                self.assertIsNotNone(run["posthoc_stop_quality"])


if __name__ == "__main__":
    unittest.main()
