"""Offline and live matrix runner for the microtask swarm experiment."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

from microtask_swarm.budget import SweycoinMeter
from microtask_swarm.contracts import Kind
from microtask_swarm.cost import account_calls
from microtask_swarm.estimate import estimate_matrix, estimate_sweycoins
from microtask_swarm.executor import Execution, SwarmExecutor
from microtask_swarm.fixtures import (
    GoldSuite,
    TaskSpec,
    all_microtask_specs,
    build_task_specs,
    load_fixtures,
)
from microtask_swarm.offline import OfflineHeuristicProvider
from microtask_swarm.scoring import score_suite

_MODE_SINGLE = "single_call"
_MODE_DISTRIBUTED = "distributed_independent"
_MODE_COMPETITION = "heterogeneous_competition"


def load_config(path: Path) -> dict[str, Any]:
    config = json.loads(path.read_text(encoding="utf-8"))
    if config.get("schema_version") != 1:
        raise ValueError("config schema_version must be 1")
    return config


def load_suite(config: Mapping[str, Any], root: Path) -> GoldSuite:
    fixtures = config["fixtures"]
    return load_fixtures(
        root / str(fixtures["gold"]),
        expected_sha256=str(fixtures["sha256"]),
    )


def available_models(config: Mapping[str, Any]) -> list[str]:
    return [
        str(model["id"])
        for model in config["models"]
        if model.get("available", False)
    ]


def mode_model_ids(
    config: Mapping[str, Any], mode: str, model_ids: Sequence[str] | None
) -> list[list[str]]:
    if model_ids is not None:
        requested = list(model_ids)
    else:
        requested = available_models(config)
    if mode == _MODE_COMPETITION:
        return [requested]
    return [[model] for model in requested]


def _specs_for_mode(suite: GoldSuite, mode: str) -> tuple[TaskSpec, ...]:
    if mode == _MODE_SINGLE:
        return build_task_specs(suite, Kind.COMBINED)
    if mode in {_MODE_DISTRIBUTED, _MODE_COMPETITION}:
        return all_microtask_specs(suite)
    raise ValueError(f"unknown mode {mode!r}")


def _raw_calls(execution: Execution) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for result in execution.results.values():
        for call in result.calls:
            rows.append(
                {
                    "model_id": call.model_id,
                    "role": call.role,
                    "attempt": call.attempt,
                    "latency_ms": call.latency_ms,
                    "tokens": call.tokens,
                    "billed_usd": call.billed_usd,
                    "price_period": call.price_period,
                }
            )
    return rows


def _percentile(values: Sequence[float], quantile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, math.ceil(len(ordered) * quantile) - 1))
    return ordered[index]


async def run_matrix(
    config: Mapping[str, Any],
    suite: GoldSuite,
    *,
    provider: Any,
    model_ids: Sequence[str] | None = None,
    modes: Sequence[str] | None = None,
    concurrency_levels: Sequence[int] | None = None,
    evidence_kind: str,
    include_rows: bool = False,
    effective_concurrency_limit: int | None = None,
) -> dict[str, Any]:
    selected_modes = list(modes) if modes is not None else [
        _MODE_SINGLE,
        _MODE_DISTRIBUTED,
        _MODE_COMPETITION,
    ]
    levels = list(concurrency_levels) if concurrency_levels is not None else list(
        config["concurrency_levels"]
    )
    models_by_id = {str(model["id"]): model for model in config["models"]}
    contracts = config["contracts"]
    provider_limit = int(
        config["provider"].get("assumed_concurrency_limit", 0) or 0
    )
    runs: list[dict[str, Any]] = []
    for mode in selected_modes:
        mode_config = config["modes"][mode]
        max_attempts = int(mode_config.get("max_attempts", 1))
        specs = _specs_for_mode(suite, mode)
        for assignment in mode_model_ids(config, mode, model_ids):
            for requested in levels:
                effective = (
                    min(requested, effective_concurrency_limit)
                    if effective_concurrency_limit is not None
                    else requested
                )
                meter = SweycoinMeter(int(config["sweycoins"]["run_allocated_milli"]))
                executor = SwarmExecutor(
                    provider=provider,
                    models=models_by_id,
                    contracts=contracts,
                    concurrency=effective,
                    max_attempts=max_attempts,
                    timeout_seconds=float(config["measurement"]["timeout_seconds"]),
                    meter=meter,
                    retry_backoff_ms=float(
                        config["measurement"].get("retry_backoff_ms", 0.0)
                    ),
                    default_price_period="peak",
                )
                execution = await executor.run(
                    specs, mode=mode, model_ids=assignment
                )
                quality = score_suite(specs, execution.results)
                verified_evidence = int(quality["extraction"]["matched_quotes"])
                cost = account_calls(
                    _raw_calls(execution),
                    models=models_by_id,
                    verified_evidence=verified_evidence,
                )
                latencies = [
                    call.latency_ms
                    for result in execution.results.values()
                    for call in result.calls
                ]
                wall_seconds = max(execution.wall_ms / 1000.0, 1e-9)
                run = {
                    "mode": mode,
                    "model_ids": list(assignment),
                    "requested_concurrency": requested,
                    "effective_concurrency": effective,
                    "provider_concurrency_limit": provider_limit,
                    "limit_exceeded": requested > provider_limit,
                    "wall_ms": round(execution.wall_ms, 3),
                    "p50_call_latency_ms": round(_percentile(latencies, 0.50), 3),
                    "p95_call_latency_ms": round(_percentile(latencies, 0.95), 3),
                    "task_throughput_per_second": round(
                        quality["scored_subtasks"] / wall_seconds, 3
                    ),
                    "call_throughput_per_second": round(
                        execution.call_count / wall_seconds, 3
                    ),
                    "task_count": len(specs),
                    "scored_subtasks": quality["scored_subtasks"],
                    "call_count": execution.call_count,
                    "retry_count": execution.retry_count,
                    "cancelled_count": execution.cancelled_count,
                    "early_cancelled_tasks": execution.early_cancelled_tasks,
                    "peak_in_flight": execution.peak_in_flight,
                    "execution": execution.to_dict(),
                    "quality": quality,
                    "cost": cost,
                    "sweycoins": meter.to_dict(),
                }
                if include_rows:
                    run["quality_rows"] = quality["rows"]
                else:
                    run["quality"].pop("rows", None)
                runs.append(run)

    return {
        "schema_version": 1,
        "experiment_id": config["experiment_id"],
        "evidence_kind": evidence_kind,
        "status": config["status"] if evidence_kind.startswith("offline") else "LIVE_RUN",
        "generated_at": datetime.now(UTC).isoformat(),
        "base_commit": config["base_commit"],
        "fixture_sha256": suite.sha256,
        "fixture_item_count": suite.item_count,
        "fixture_microtask_count": suite.microtask_count,
        "domains": list(suite.domains),
        "modes": selected_modes,
        "concurrency_levels": levels,
        "models": [
            {
                "id": model["id"],
                "api_model": model.get("api_model"),
                "available": model.get("available", False),
                "reason": model.get("reason"),
                "thinking": model.get("thinking"),
                "prices_usd_per_million": model.get("prices_usd_per_million"),
            }
            for model in config["models"]
        ],
        "estimate": {
            "usd": estimate_matrix(config),
            "sweycoins": estimate_sweycoins(config),
        },
        "runs": runs,
        "summary": _summary(runs),
        "notes": [
            "No agent-to-agent prose is exchanged; every request and response is typed JSON.",
            "Single-call quality is expanded into the same five contract rows as distributed mode.",
            "Calculated USD is a price-list equivalent; actual USD is N/A unless every call reports a charge.",
            "Offline heuristic numbers are pipeline evidence, not model results.",
        ],
    }


def _summary(runs: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    by_mode: dict[str, Any] = {}
    by_model_mode: dict[str, dict[str, Any]] = {}
    for run in runs:
        mode = str(run["mode"])
        key = (
            -float(run["quality"]["accuracy"]),
            float(run["cost"]["all_in"]["calculated_usd"])
            if run["cost"]["all_in"]["calculated_usd"] != "N/A"
            else float(run["cost"]["all_in_complete_calls_calculated_usd"])
            if run["cost"]["all_in_complete_calls_calculated_usd"] != "N/A"
            else 1e9,
            float(run["p95_call_latency_ms"]),
        )
        row = {
            "_key": key,
            "model_ids": run["model_ids"],
            "mode": mode,
            "concurrency": run["requested_concurrency"],
            "accuracy": run["quality"]["accuracy"],
            "answered_rate": run["quality"]["answered_rate"],
            "verified_rate": run["quality"]["verified_rate"],
            "extraction_coverage": run["quality"]["extraction"]["coverage"],
            "false_support_rate": run["quality"]["false_support"][
                "false_support_rate"
            ],
            "p50_call_latency_ms": run["p50_call_latency_ms"],
            "p95_call_latency_ms": run["p95_call_latency_ms"],
            "calculated_usd": run["cost"]["all_in"]["calculated_usd"],
            "calculated_usd_complete_calls": run["cost"][
                "all_in_complete_calls_calculated_usd"
            ],
            "calls_with_unknown_usage": run["cost"]["all_in"][
                "calls_with_unknown_usage"
            ],
            "tokens_complete_calls": run["cost"]["all_in"][
                "tokens_complete_calls"
            ],
            "actual_usd": run["cost"]["all_in"]["actual_usd"],
            "sweycoins_spent_milli": run["sweycoins"]["spent_milli"],
            "early_cancelled_tasks": run["early_cancelled_tasks"],
        }
        current = by_mode.get(mode)
        if current is None or key < current["_key"]:
            by_mode[mode] = dict(row)
        if mode in {"single_call", "distributed_independent"}:
            model_id = str(run["model_ids"][0])
            model_mode = by_model_mode.setdefault(model_id, {})
            existing = model_mode.get(mode)
            if existing is None or key < existing["_key"]:
                model_mode[mode] = dict(row)
    for value in by_mode.values():
        value.pop("_key", None)
    for modes in by_model_mode.values():
        for value in modes.values():
            value.pop("_key", None)
    return {
        "run_count": len(runs),
        "best_by_mode": by_mode,
        "by_model_and_mode": by_model_mode,
        "offline_disclaimer": (
            "Offline runs use deterministic lexical personas; they are not evidence "
            "about any Ollama model."
        ),
    }


def _config_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


async def run_offline(
    config_path: Path,
    *,
    root: Path | None = None,
    include_rows: bool = False,
    modes: Sequence[str] | None = None,
    model_ids: Sequence[str] | None = None,
    concurrency_levels: Sequence[int] | None = None,
) -> dict[str, Any]:
    root = root or config_path.resolve().parent.parent
    config = load_config(config_path)
    suite = load_suite(config, root)
    chosen_models = list(model_ids) if model_ids is not None else available_models(config)
    provider = OfflineHeuristicProvider(
        model_ids=chosen_models,
        simulated_latency_ms=float(config["measurement"]["simulated_latency_ms"]),
    )
    report = await run_matrix(
        config,
        suite,
        provider=provider,
        model_ids=chosen_models,
        modes=modes,
        concurrency_levels=concurrency_levels,
        evidence_kind="offline_heuristic",
        include_rows=include_rows,
    )
    report["config_sha256"] = _config_hash(config_path)
    return report


def write_report(report: Mapping[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def compact_report(report: Mapping[str, Any]) -> dict[str, Any]:
    """Keep the comparison fields and drop bulky per-task diagnostics."""

    compact_runs: list[dict[str, Any]] = []
    for run in report.get("runs", []):
        quality = run.get("quality", {})
        compact_runs.append(
            {
                "mode": run.get("mode"),
                "model_ids": run.get("model_ids"),
                "requested_concurrency": run.get("requested_concurrency"),
                "effective_concurrency": run.get("effective_concurrency"),
                "provider_concurrency_limit": run.get("provider_concurrency_limit"),
                "limit_exceeded": run.get("limit_exceeded"),
                "wall_ms": run.get("wall_ms"),
                "p50_call_latency_ms": run.get("p50_call_latency_ms"),
                "p95_call_latency_ms": run.get("p95_call_latency_ms"),
                "task_throughput_per_second": run.get(
                    "task_throughput_per_second"
                ),
                "call_throughput_per_second": run.get(
                    "call_throughput_per_second"
                ),
                "task_count": run.get("task_count"),
                "scored_subtasks": run.get("scored_subtasks"),
                "call_count": run.get("call_count"),
                "retry_count": run.get("retry_count"),
                "cancelled_count": run.get("cancelled_count"),
                "early_cancelled_tasks": run.get("early_cancelled_tasks"),
                "peak_in_flight": run.get("peak_in_flight"),
                "layer_source": run.get("execution", {}).get("layer_source"),
                "layer_fallback_reason": run.get("execution", {}).get(
                    "layer_fallback_reason"
                ),
                "quality": {
                    key: quality.get(key)
                    for key in (
                        "scored_subtasks",
                        "correct",
                        "accuracy",
                        "answered",
                        "answered_rate",
                        "verified",
                        "verified_rate",
                        "accuracy_on_verified",
                        "by_contract",
                        "extraction",
                        "false_support",
                    )
                },
                "cost": {
                    "all_in": run.get("cost", {}).get("all_in"),
                    "all_in_complete_calls_calculated_usd": run.get("cost", {}).get(
                        "all_in_complete_calls_calculated_usd"
                    ),
                },
                "sweycoins": run.get("sweycoins"),
            }
        )
    compact = {
        key: report.get(key)
        for key in (
            "schema_version",
            "experiment_id",
            "evidence_kind",
            "status",
            "generated_at",
            "base_commit",
            "fixture_sha256",
            "fixture_item_count",
            "fixture_microtask_count",
            "domains",
            "modes",
            "concurrency_levels",
            "config_sha256",
            "live_gate",
            "preflight",
            "approval_estimate",
        )
        if key in report
    }
    compact["models"] = [
        {
            "id": model.get("id"),
            "api_model": model.get("api_model"),
            "available": model.get("available"),
            "reason": model.get("reason"),
        }
        for model in report.get("models", [])
    ]
    compact["estimate"] = report.get("estimate")
    compact["summary"] = report.get("summary")
    compact["notes"] = report.get("notes")
    compact["runs"] = compact_runs
    compact["report_scope"] = "compact; use --full for per-task diagnostics"
    return compact


__all__ = [
    "available_models",
    "compact_report",
    "load_config",
    "load_suite",
    "run_matrix",
    "run_offline",
    "write_report",
]
