"""Offline, gold-free early-stop comparison.

The stop decision uses only signals a production host can observe:

- verified evidence requirements (exact-quote extraction per item);
- coverage of the 50 typed needs;
- abstention rate;
- a deterministic verifier confidence.

Gold labels are read only after the run, to score what the stop decision
produced. The frozen async and adaptive baselines are imported, never edited;
their hashes are embedded in the report and checked by tests.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

from microtask_swarm.adaptive_benchmark import ProviderAwareRunner
from microtask_swarm.async_benchmark import (
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
from microtask_swarm.executor import ProviderResponse
from microtask_swarm.scoring import score_suite


FROZEN_FILES = (
    "microtask_swarm/async_benchmark.py",
    "config/async-benchmark.json",
    "reports/async-benchmark.json",
    "reports/async-benchmark.md",
    "tests/test_async_benchmark.py",
    "microtask_swarm/adaptive_benchmark.py",
    "config/adaptive-benchmark.json",
    "reports/adaptive-benchmark.json",
    "reports/adaptive-benchmark.md",
    "tests/test_adaptive_benchmark.py",
)

FROZEN_EXPECTED = {
    "microtask_swarm/async_benchmark.py": "30f47bf864791962f10da8583a46327cdf7850c7c03d3ec2b71fd86d7c0a710c",
    "config/async-benchmark.json": "dec26493555465b285cee546a7be7fec69161e4cc5ee4b6c3486df8f8b20582f",
    "reports/async-benchmark.json": "1b3da307bc6db34c5e418e12b9fae50abc149a31f412d5abf988822006d4b448",
    "reports/async-benchmark.md": "9a920a037c5d623bed8893eb4b5fe15a8fe0926fd569e7cd497cdaa0e68a474c",
    "tests/test_async_benchmark.py": "ea78b24cd99383cfd24a9044661c031b5a275b58e8e8576054a08c853eb439a3",
    "microtask_swarm/adaptive_benchmark.py": "be80c2dc1eaad1a955cdc4a186729455c1cc06e60a0899f1bc84add70f6a9402",
    "config/adaptive-benchmark.json": "5189ed84e889a78808a03ea8388aa2299519d8ae574bd9bbf2be93d17d6283aa",
    "reports/adaptive-benchmark.json": "7f2f446f9bfc745cb724bab1f1d9a825f7d745aa801218a2761567481851fa18",
    "reports/adaptive-benchmark.md": "62776e4b36e354adf0b90ea8e6fa50946694467febe90a723320a3f306911168",
    "tests/test_adaptive_benchmark.py": "5fdd810fd6bb2d238e6d7944808a0a1bcaddaa687e134dd243ba6178f2b0eca4",
}

_WORD = re.compile(r"[a-z0-9']+")


@dataclass(frozen=True, slots=True)
class StopSignals:
    attempted: int
    total_needs: int
    covered_needs: int
    need_coverage: float
    evidence_verified: int
    evidence_total: int
    evidence_coverage: float
    abstentions: int
    invalid_outputs: int
    abstention_rate: float
    average_confidence: float
    verified_count: int
    completed_items: int
    completed_item_ids: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "attempted": self.attempted,
            "total_needs": self.total_needs,
            "covered_needs": self.covered_needs,
            "need_coverage": self.need_coverage,
            "evidence_verified": self.evidence_verified,
            "evidence_total": self.evidence_total,
            "evidence_coverage": self.evidence_coverage,
            "abstentions": self.abstentions,
            "invalid_outputs": self.invalid_outputs,
            "abstention_rate": self.abstention_rate,
            "average_confidence": self.average_confidence,
            "verified_count": self.verified_count,
            "completed_items": self.completed_items,
            "completed_item_ids": list(self.completed_item_ids),
        }


def _tokens(text: str) -> set[str]:
    return set(_WORD.findall(text.lower()))


def _overlap(left: str, right: str) -> float:
    left_tokens = _tokens(left)
    if not left_tokens:
        return 0.0
    return len(left_tokens & _tokens(right)) / len(left_tokens)


def verifier_confidence(spec: TaskSpec, attempt: Attempt) -> float:
    """Gold-free confidence from verification and answer support.

    This is a deterministic host heuristic, not a calibrated probability.
    It never reads ``spec.gold``.
    """

    if attempt.cancelled or attempt.output is None:
        return 0.0
    verification = attempt.verification
    if verification is None or not verification.ok:
        return 0.0
    if spec.kind is Kind.EXTRACTION:
        quotes = attempt.output.get("quotes", [])
        if not quotes:
            return 0.0
        support = max(_overlap(spec.question, str(quote)) for quote in quotes)
        return round(0.5 + 0.5 * min(1.0, support), 6)
    if spec.kind is Kind.ENTAILMENT:
        label = attempt.output.get("label")
        return 0.6 if label == "neutral" else 1.0
    if spec.kind in {Kind.RELEVANCE, Kind.ATOMICITY, Kind.OPERATION}:
        return 1.0
    return 0.0


def compute_signals(
    specs: Sequence[TaskSpec],
    attempts: Sequence[Attempt],
    *,
    specs_by_item: Mapping[str, Sequence[TaskSpec]],
) -> StopSignals:
    by_id = {attempt.task_id: attempt for attempt in attempts}
    attempted: list[tuple[TaskSpec, Attempt]] = []
    abstentions = 0
    invalid = 0
    covered = 0
    verified_count = 0
    evidence_total = sum(1 for spec in specs if spec.kind is Kind.EXTRACTION)
    evidence_verified = 0
    confidences: list[float] = []
    for spec in specs:
        attempt = by_id.get(spec.task_id)
        if attempt is None or attempt.cancelled:
            continue
        attempted.append((spec, attempt))
        if attempt.verification is not None and attempt.verification.ok:
            verified_count += 1
        if attempt.verification is None or not attempt.verification.ok:
            invalid += 1
        abstained = attempt.output is None or (
            spec.kind is Kind.EXTRACTION
            and not attempt.output.get("quotes", [])
        )
        if abstained:
            abstentions += 1
        elif attempt.verification is not None and attempt.verification.ok:
            covered += 1
            if spec.kind is Kind.EXTRACTION:
                evidence_verified += 1
        confidences.append(verifier_confidence(spec, attempt))
    completed_items: list[str] = []
    for item_id, item_specs in specs_by_item.items():
        if all(
            spec.task_id in by_id and not by_id[spec.task_id].cancelled
            for spec in item_specs
        ):
            completed_items.append(item_id)
    attempted_count = len(attempted)
    return StopSignals(
        attempted=attempted_count,
        total_needs=len(specs),
        covered_needs=covered,
        need_coverage=round(covered / len(specs), 6) if specs else 0.0,
        evidence_verified=evidence_verified,
        evidence_total=evidence_total,
        evidence_coverage=round(
            evidence_verified / evidence_total, 6
        )
        if evidence_total
        else 0.0,
        abstentions=abstentions,
        invalid_outputs=invalid,
        abstention_rate=round(abstentions / attempted_count, 6)
        if attempted_count
        else 0.0,
        average_confidence=round(
            sum(confidences) / len(confidences), 6
        )
        if confidences
        else 0.0,
        verified_count=verified_count,
        completed_items=len(completed_items),
        completed_item_ids=tuple(sorted(completed_items)),
    )


@dataclass(frozen=True, slots=True)
class ProductionStopPolicy:
    name: str
    min_items: int
    min_need_coverage: float
    min_evidence_coverage: float
    max_abstention: float
    min_confidence: float
    max_invalid_outputs: int = 1_000_000

    def evaluate(self, signals: StopSignals) -> bool:
        return (
            signals.completed_items >= self.min_items
            and signals.need_coverage >= self.min_need_coverage
            and signals.evidence_coverage >= self.min_evidence_coverage
            and signals.abstention_rate <= self.max_abstention
            and signals.average_confidence >= self.min_confidence
            and signals.invalid_outputs <= self.max_invalid_outputs
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "min_items": self.min_items,
            "min_need_coverage": self.min_need_coverage,
            "min_evidence_coverage": self.min_evidence_coverage,
            "max_abstention": self.max_abstention,
            "min_confidence": self.min_confidence,
            "max_invalid_outputs": self.max_invalid_outputs,
        }


class ProductionStopRunner(ProviderAwareRunner):
    """Provider-aware runner whose stop decision is gold-free."""

    def __init__(self, *, stop_policy: ProductionStopPolicy | None, **kwargs: Any) -> None:
        kwargs["quality_gate"] = None
        super().__init__(**kwargs)
        self.stop_policy = stop_policy
        self._production_signals: StopSignals | None = None
        self._last_attempts: list[Attempt] = []

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
        self._production_signals = None
        self._last_attempts = []
        try:
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
        finally:
            if self._production_signals is None:
                self._production_signals = compute_signals(
                    specs, self._last_attempts, specs_by_item=self._specs_by_item
                )
        signals = self._production_signals
        posthoc = self._posthoc_stop_quality(specs, signals)
        result["stop_policy"] = (
            None if self.stop_policy is None else self.stop_policy.to_dict()
        )
        result["production_signals_at_stop"] = signals.to_dict()
        result["posthoc_stop_quality"] = posthoc
        return result

    async def _run_ready_queue(self, specs, dag, concurrency):
        results = await super()._run_ready_queue(specs, dag, concurrency)
        self._last_attempts = list(results)
        return results

    async def _run_layered(self, specs, dag, concurrency):
        results = await super()._run_layered(specs, dag, concurrency)
        self._last_attempts = list(results)
        return results

    def _maybe_quality_stop(
        self, results: Sequence[Attempt], specs: Sequence[TaskSpec]
    ) -> None:
        if self.stop_policy is None or self._controller.is_stopped():
            return
        signals = compute_signals(
            specs, results, specs_by_item=self._specs_by_item
        )
        self._production_signals = signals
        if self.stop_policy.evaluate(signals):
            self._controller.stop(f"production:{self.stop_policy.name}")

    def _posthoc_stop_quality(
        self, specs: Sequence[TaskSpec], signals: StopSignals
    ) -> dict[str, Any] | None:
        completed = set(signals.completed_item_ids)
        if not completed:
            return None
        subset_specs = [spec for spec in specs if spec.item_id in completed]
        by_id = {attempt.task_id: attempt for attempt in self._last_attempts}
        subset_results = {
            spec.task_id: by_id[spec.task_id].to_task_result()
            for spec in subset_specs
            if spec.task_id in by_id
        }
        quality = score_suite(subset_specs, subset_results)
        extraction = quality["extraction"]
        spec_by_id = {spec.task_id: spec for spec in subset_specs}
        samples: list[dict[str, Any]] = []
        false_support_rows: list[dict[str, Any]] = []
        for row in quality["rows"]:
            spec = spec_by_id[row["task_id"]]
            attempt = by_id[row["task_id"]]
            extraction_row = row["extraction"]
            false_support = (
                extraction_row["unsupported_quotes"] + extraction_row["unverified_quotes"]
            ) > 0
            sample = {
                "task_id": row["task_id"],
                "item_id": row["item_id"],
                "contract": row["contract"],
                "confidence": verifier_confidence(spec, attempt),
                "correct": row["correct"],
                "false_support": false_support,
                "emitted_quotes": extraction_row["emitted_quotes"],
                "matched_quotes": extraction_row["matched_quotes"],
                "unsupported_quotes": extraction_row["unsupported_quotes"],
                "unverified_quotes": extraction_row["unverified_quotes"],
            }
            samples.append(sample)
            if false_support:
                false_support_rows.append(sample)
        return {
            "completed_items": len(completed),
            "scored_subtasks": quality["scored_subtasks"],
            "accuracy": quality["accuracy"],
            "coverage": extraction["coverage"],
            "false_support_rate": quality["false_support"]["false_support_rate"],
            "false_support_count": len(false_support_rows),
            "verified_rate": quality["verified_rate"],
            "samples": samples,
            "false_support_rows": false_support_rows,
        }


class StressTraceProvider:
    """Deterministic gold-free stress case: abstain or answer neutral.

    Extraction tasks selected by a stable hash return an empty quote list;
    selected entailment tasks return ``neutral``. The wrapped provider still
    supplies latency and token counts, so scheduling, budget, and verification
    stay on the normal path.
    """

    def __init__(
        self,
        inner: TraceProvider,
        *,
        abstain_fraction: float = 0.25,
        neutral_fraction: float = 0.25,
        seed: int = 99,
    ) -> None:
        self.inner = inner
        self.abstain_fraction = abstain_fraction
        self.neutral_fraction = neutral_fraction
        self.seed = seed

    async def chat(self, request):
        response = await self.inner.chat(request)
        digest = hashlib.sha256(
            f"{self.seed}:{request.task_id}:{request.model_id}".encode("utf-8")
        ).hexdigest()
        fraction = int(digest[:8], 16) / 0xFFFFFFFF
        if request.kind is Kind.EXTRACTION and fraction < self.abstain_fraction:
            return ProviderResponse(
                content='{"quotes":[]}',
                tokens=response.tokens,
                billed_usd=response.billed_usd,
                price_period=response.price_period,
            )
        if request.kind is Kind.ENTAILMENT and fraction < self.neutral_fraction:
            return ProviderResponse(
                content='{"label":"neutral"}',
                tokens=response.tokens,
                billed_usd=response.billed_usd,
                price_period=response.price_period,
            )
        return response


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
        straggler_extra_ms=float(trace_cfg.get("straggler_extra_ms", 0.0)),
        seed=seed,
        scale=scale,
        extra_ms_by_task={
            str(k): float(v)
            for k, v in trace_cfg.get("extra_ms_by_task", {}).items()
        },
    )


async def run_production_stop_benchmark(
    experiment_config_path: Path,
    async_config_path: Path,
    production_config_path: Path,
    *,
    scale: float | None = None,
) -> dict[str, Any]:
    experiment = _load_json(experiment_config_path)
    async_config = _load_json(async_config_path)
    production = _load_json(production_config_path)
    root = experiment_config_path.resolve().parent.parent
    suite = load_fixtures(
        root / experiment["fixtures"]["gold"],
        expected_sha256=experiment["fixtures"]["sha256"],
    )
    models = {str(model["id"]): model for model in experiment["models"]}
    contracts = experiment["contracts"]
    model_a = str(async_config["models"]["A"])
    model_bcd = str(async_config["models"]["BCD"])
    provider_limit = int(production.get("provider_limit", 3))
    concurrency = int(production.get("concurrency", 8))
    allocated = int(
        async_config.get(
            "run_allocated_milli", experiment["sweycoins"]["run_allocated_milli"]
        )
    )
    late_fraction = float(async_config.get("late_billing_fraction", 0.5))
    trace_scale = float(scale if scale is not None else production.get("scale", 1.0))
    trace = _build_trace(experiment, async_config, scale=trace_scale)
    inner = OfflineHeuristicProvider(
        model_ids=[model_a, model_bcd], simulated_latency_ms=0.0
    )
    provider = TraceProvider(inner, trace)
    specs = all_microtask_specs(suite)
    dag = build_dag(specs)
    policies = {
        name: ProductionStopPolicy(
            name=name,
            min_items=int(cfg["min_items"]),
            min_need_coverage=float(cfg["min_need_coverage"]),
            min_evidence_coverage=float(cfg["min_evidence_coverage"]),
            max_abstention=float(cfg["max_abstention"]),
            min_confidence=float(cfg["min_confidence"]),
            max_invalid_outputs=int(
                cfg.get("max_invalid_outputs", 1_000_000)
            ),
        )
        for name, cfg in production.get("policies", {}).items()
    }
    count_after = int(production.get("count_stop_after_verified", 25))
    schedulers = production.get(
        "schedulers",
        [
            {"id": "D", "adaptive": False, "priority": False},
            {"id": "P", "adaptive": True, "priority": True},
        ],
    )
    runs: list[dict[str, Any]] = []
    for scheduler in schedulers:
        scheduler_id = str(scheduler["id"])
        adaptive = bool(scheduler.get("adaptive", False))
        priority = bool(scheduler.get("priority", False))
        conditions: list[tuple[str, ProductionStopPolicy | None, int | None]] = [
            ("full", None, None),
            (f"count_{count_after}", None, count_after),
        ]
        conditions.extend((name, policy, None) for name, policy in policies.items())
        for condition, policy, early_after in conditions:
            runner = ProductionStopRunner(
                provider=provider,
                models=models,
                contracts=contracts,
                trace=trace,
                provider_limit=provider_limit,
                allocated_milli=allocated,
                late_billing_fraction=late_fraction,
                adaptive=adaptive,
                priority=priority,
                quality_gate=None,
                target_queue_ms=float(production.get("target_queue_ms", 400.0)),
                stop_policy=policy,
            )
            result = await runner.run(
                Strategy.D_EVENT_READY,
                specs,
                model_id=model_bcd,
                concurrency=concurrency,
                dag=dag,
                early_stop_after=early_after,
                run_id=f"production:{scheduler_id}:{condition}",
            )
            result["scheduler"] = scheduler_id
            result["condition"] = condition
            result["stop_policy_name"] = None if policy is None else policy.name
            result["stress"] = False
            runs.append(result)
    stress_cfg = production.get("stress", {})
    if stress_cfg.get("enabled", False):
        stress_provider = StressTraceProvider(
            provider,
            abstain_fraction=float(stress_cfg.get("abstain_fraction", 0.25)),
            neutral_fraction=float(stress_cfg.get("neutral_fraction", 0.25)),
            seed=int(stress_cfg.get("seed", 99)),
        )
        stress_scheduler_id = str(stress_cfg.get("scheduler", "P"))
        stress_scheduler = next(
            (
                scheduler
                for scheduler in schedulers
                if str(scheduler.get("id")) == stress_scheduler_id
            ),
            {"id": stress_scheduler_id, "adaptive": True, "priority": True},
        )
        stress_adaptive = bool(stress_scheduler.get("adaptive", True))
        stress_priority = bool(stress_scheduler.get("priority", True))
        stress_limit = int(stress_cfg.get("provider_limit", provider_limit))
        stress_concurrency = int(stress_cfg.get("concurrency", concurrency))
        stress_conditions: list[
            tuple[str, ProductionStopPolicy | None, int | None]
        ] = [
            ("full", None, None),
            (f"count_{count_after}", None, count_after),
        ]
        stress_conditions.extend(
            (name, policy, None) for name, policy in policies.items()
        )
        for condition, policy, early_after in stress_conditions:
            runner = ProductionStopRunner(
                provider=stress_provider,
                models=models,
                contracts=contracts,
                trace=trace,
                provider_limit=stress_limit,
                allocated_milli=allocated,
                late_billing_fraction=late_fraction,
                adaptive=stress_adaptive,
                priority=stress_priority,
                quality_gate=None,
                target_queue_ms=float(production.get("target_queue_ms", 400.0)),
                stop_policy=policy,
            )
            result = await runner.run(
                Strategy.D_EVENT_READY,
                specs,
                model_id=model_bcd,
                concurrency=stress_concurrency,
                dag=dag,
                early_stop_after=early_after,
                run_id=f"production:stress:{condition}",
            )
            result["scheduler"] = f"{stress_scheduler_id}_stress"
            result["condition"] = f"stress_{condition}"
            result["stop_policy_name"] = None if policy is None else policy.name
            result["stress"] = True
            runs.append(result)
    return {
        "schema_version": 1,
        "experiment_id": "swe-microtask-swarm-production-stop",
        "evidence_kind": "offline_production_early_stop",
        "generated_at": datetime.now(UTC).isoformat(),
        "fixture_sha256": suite.sha256,
        "models": {"BCD": model_bcd, "A_reference": model_a},
        "provider_rate_limit": provider_limit,
        "concurrency": concurrency,
        "count_stop_after_verified": count_after,
        "policies": {name: policy.to_dict() for name, policy in policies.items()},
        "stress": stress_cfg,
        "trace": {
            "seed": trace.seed,
            "base_ms": trace.base_ms,
            "model_multiplier": trace.model_multiplier,
            "jitter_fraction": trace.jitter_fraction,
            "straggler_items": sorted(trace.straggler_items),
            "straggler_extra_ms": trace.straggler_extra_ms,
            "scale": trace.scale,
        },
        "frozen_baseline_sha256": _frozen_hashes(root),
        "runs": runs,
        "summary": _summarize(runs),
        "analysis": _analysis(runs),
        "totals": _totals(runs),
        "notes": [
            "Gold is used only after the run for posthoc_stop_quality and run quality.",
            "Stop signals use verified evidence, need coverage, abstention, and verifier confidence only.",
            "Offline deterministic trace; no model or network call.",
            "SWE PR #11 runtime budget is still unattached; t1bm/PR 13 is untouched.",
        ],
    }


def _analysis(runs: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    full_runs = [
        run
        for run in runs
        if run["condition"] in {"full", "stress_full"}
    ]
    samples: list[Mapping[str, Any]] = []
    false_support_runs: list[dict[str, Any]] = []
    for run in full_runs:
        posthoc = run["posthoc_stop_quality"] or {}
        run_samples = list(posthoc.get("samples", []))
        samples.extend(run_samples)
        extraction = [
            sample
            for sample in run_samples
            if sample["contract"] == "extraction"
        ]
        emitted = sum(int(sample["emitted_quotes"]) for sample in extraction)
        matched = sum(int(sample["matched_quotes"]) for sample in extraction)
        unsupported = sum(
            int(sample["unsupported_quotes"]) for sample in extraction
        )
        unverified = sum(
            int(sample["unverified_quotes"]) for sample in extraction
        )
        examples = [
            {
                "item_id": sample["item_id"],
                "task_id": sample["task_id"],
                "confidence": sample["confidence"],
                "unsupported_quotes": sample["unsupported_quotes"],
                "unverified_quotes": sample["unverified_quotes"],
            }
            for sample in extraction
            if sample["false_support"]
        ][:5]
        false_support_runs.append(
            {
                "scheduler": run["scheduler"],
                "condition": run["condition"],
                "stress": run["stress"],
                "completed_items": (run["posthoc_stop_quality"] or {}).get(
                    "completed_items"
                ),
                "emitted_quotes": emitted,
                "matched_quotes": matched,
                "unsupported_quotes": unsupported,
                "unverified_quotes": unverified,
                "false_support_count": unsupported + unverified,
                "false_support_rate": round(
                    (unsupported + unverified) / emitted, 6
                )
                if emitted
                else 0.0,
                "examples": examples,
            }
        )
    edges = ((0.0, 0.5), (0.5, 0.8), (0.8, 0.95), (0.95, 1.000001))
    calibration: list[dict[str, Any]] = []
    for low, high in edges:
        bucket = [
            sample
            for sample in samples
            if low <= float(sample["confidence"]) < high
        ]
        correct = sum(1 for sample in bucket if sample["correct"])
        false_support = sum(
            1 for sample in bucket if sample["false_support"]
        )
        calibration.append(
            {
                "confidence_low": low,
                "confidence_high": high,
                "count": len(bucket),
                "accuracy": round(correct / len(bucket), 6)
                if bucket
                else 0.0,
                "false_support_count": false_support,
                "false_support_rate": round(
                    false_support / len(bucket), 6
                )
                if bucket
                else 0.0,
            }
        )
    return {
        "confidence_calibration": calibration,
        "false_support_full_runs": false_support_runs,
        "gate_proposal": {
            "name": "verifier_gate_v1",
            "min_complete_items": 6,
            "min_need_coverage": 0.8,
            "min_evidence_coverage": 0.8,
            "max_abstention": 0.05,
            "min_confidence": 0.8,
            "max_invalid_outputs": 0,
            "hard_rules": [
                "every emitted extraction quote must be an exact passage substring",
                "at least one verified extract per complete item",
                "no unverified extraction may remain in the completed set",
            ],
            "limitation": (
                "Runtime cannot see gold. A verified quote that is not the gold "
                "span is false support and is only measurable post-hoc."
            ),
        },
        "calibration_note": (
            "Verifier confidence is a deterministic host heuristic, not a "
            "calibrated probability. Bins are descriptive."
        ),
    }


def _summarize(runs: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for run in runs:
        signals = run["production_signals_at_stop"]
        posthoc = run["posthoc_stop_quality"] or {}
        rows.append(
            {
                "scheduler": run["scheduler"],
                "condition": run["condition"],
                "stop_reason": run["stop_reason"],
                "wall_ms": run["wall_ms"],
                "calls": run["call_count"],
                "completed_items": signals["completed_items"],
                "need_coverage": signals["need_coverage"],
                "evidence_coverage": signals["evidence_coverage"],
                "abstention_rate": signals["abstention_rate"],
                "average_confidence": signals["average_confidence"],
                "posthoc_accuracy": posthoc.get("accuracy"),
                "posthoc_coverage": posthoc.get("coverage"),
                "posthoc_false_support": posthoc.get("false_support_rate"),
                "pending_milli": run["sweycoins"]["pending_before_reconcile_milli"],
                "late_milli": run["sweycoins"]["late_billed_milli"],
            }
        )
    return {"run_count": len(runs), "rows": rows}


def _totals(runs: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
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
        "calls": sum(int(run["call_count"]) for run in runs),
        "settled_milli": sum(
            int(run["sweycoins"]["settled_milli"]) for run in runs
        ),
        "pending_before_reconcile_milli": sum(
            int(run["sweycoins"]["pending_before_reconcile_milli"])
            for run in runs
        ),
        "late_billed_milli": sum(
            int(run["sweycoins"]["late_billed_milli"]) for run in runs
        ),
        "released_milli": sum(
            int(run["sweycoins"]["released_milli"]) for run in runs
        ),
        "calculated_usd": round(calculated, 9),
        "actual_usd": "N/A",
        "note": "Local pilot budget; SWE PR #11 runtime budget is not attached.",
    }


def write_production_report(report: Mapping[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def render_production_markdown(report: Mapping[str, Any]) -> str:
    lines = [
        "# Gold-free production early-stop comparison (offline)",
        "",
        f"Generated: {report['generated_at']}",
        f"Fixture SHA-256: `{report['fixture_sha256']}`",
        "Gold is used only for post-hoc scoring; the stop decision never reads it.",
        "No model or network call was made.",
        "",
        "| Scheduler | Condition | Stop reason | Wall ms | Calls | Completed items | Need cov. | Evidence cov. | Abstention | Avg conf. | Post-hoc acc. | Post-hoc cov. | Post-hoc false support | Pending milli | Late milli | Calc USD |",
        "| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for run in report["runs"]:
        signals = run["production_signals_at_stop"]
        posthoc = run["posthoc_stop_quality"] or {}
        budget = run["sweycoins"]
        lines.append(
            "| {scheduler} | {condition} | {stop} | {wall} | {calls} | {items} | {need} | {evidence} | {abst} | {conf} | {acc} | {cov} | {fs} | {pending} | {late} | {usd} |".format(
                scheduler=run["scheduler"],
                condition=run["condition"],
                stop=run["stop_reason"],
                wall=run["wall_ms"],
                calls=run["call_count"],
                items=signals["completed_items"],
                need=signals["need_coverage"],
                evidence=signals["evidence_coverage"],
                abst=signals["abstention_rate"],
                conf=signals["average_confidence"],
                acc=posthoc.get("accuracy"),
                cov=posthoc.get("coverage"),
                fs=posthoc.get("false_support_rate"),
                pending=budget["pending_before_reconcile_milli"],
                late=budget["late_billed_milli"],
                usd=run["cost"]["all_in"]["calculated_usd"],
            )
        )
    totals = report["totals"]
    analysis = report["analysis"]
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
            "## False support (full runs)",
            "",
            "| Scheduler | Condition | Items | Emitted | Matched | Unsupported | Unverified | False-support rate | Examples |",
            "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
        ]
    )
    for row in analysis["false_support_full_runs"]:
        examples = ", ".join(
            f"{item['item_id']}:{item['task_id'].split(':')[-1]}"
            for item in row["examples"]
        )
        lines.append(
            "| {scheduler} | {condition} | {items} | {emitted} | {matched} | "
            "{unsupported} | {unverified} | {rate} | {examples} |".format(
                scheduler=row["scheduler"],
                condition=row["condition"],
                items=row["completed_items"],
                emitted=row["emitted_quotes"],
                matched=row["matched_quotes"],
                unsupported=row["unsupported_quotes"],
                unverified=row["unverified_quotes"],
                rate=row["false_support_rate"],
                examples=examples or "—",
            )
        )
    lines.extend(
        [
            "",
            "## Verifier confidence calibration (full runs)",
            "",
            "| Confidence bin | Count | Accuracy | False-support count | False-support rate |",
            "| --- | ---: | ---: | ---: | ---: |",
        ]
    )
    for row in analysis["confidence_calibration"]:
        lines.append(
            "| [{low}, {high}) | {count} | {acc} | {fs} | {rate} |".format(
                low=row["confidence_low"],
                high=row["confidence_high"],
                count=row["count"],
                acc=row["accuracy"],
                fs=row["false_support_count"],
                rate=row["false_support_rate"],
            )
        )
    gate = analysis["gate_proposal"]
    lines.extend(
        [
            "",
            "## Proposed verifier-based gate",
            "",
            "`{name}` requires at least {items} complete items, need coverage "
            "≥ {need}, evidence coverage ≥ {evidence}, abstention ≤ {abstain}, "
            "confidence ≥ {conf}, and at most {invalid} invalid outputs. "
            "Hard rules: {rules}.".format(
                name=gate["name"],
                items=gate["min_complete_items"],
                need=gate["min_need_coverage"],
                evidence=gate["min_evidence_coverage"],
                abstain=gate["max_abstention"],
                conf=gate["min_confidence"],
                invalid=gate["max_invalid_outputs"],
                rules="; ".join(gate["hard_rules"]),
            ),
            "",
            gate["limitation"],
            "",
            analysis["calibration_note"],
            "",
            "Frozen async and adaptive baseline hashes are included in the JSON.",
            "",
        ]
    )
    return "\n".join(lines)


__all__ = [
    "FROZEN_EXPECTED",
    "FROZEN_FILES",
    "ProductionStopPolicy",
    "ProductionStopRunner",
    "StopSignals",
    "compute_signals",
    "render_production_markdown",
    "run_production_stop_benchmark",
    "verifier_confidence",
    "write_production_report",
]
