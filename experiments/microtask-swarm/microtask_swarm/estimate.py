"""Conservative pre-dispatch estimate for the live matrix.

The estimate is not a measurement and is not approval. It uses published peak
price rows, the configured retry rate, and the full concurrency sweep because
the sweep repeats the suite at each level.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Mapping, Sequence

_MILLION = Decimal(1_000_000)


class EstimateError(ValueError):
    """The protocol cannot produce a bounded estimate."""


def _decimal(value: Any, name: str) -> Decimal:
    try:
        amount = Decimal(str(value))
    except Exception as exc:  # noqa: BLE001 - surfaced as a typed config error
        raise EstimateError(f"{name} must be a decimal") from exc
    if amount < 0:
        raise EstimateError(f"{name} must be non-negative")
    return amount


def _text(value: Decimal) -> str:
    return format(value.normalize(), "f")


def _rates(model: Mapping[str, Any], period: str = "peak") -> tuple[Decimal, Decimal, Decimal]:
    prices = model.get("prices_usd_per_million")
    if not isinstance(prices, Mapping):
        raise EstimateError(f"model {model.get('id')!r} has no price table")
    if "input_off_peak" in prices and period == "off_peak":
        return (
            _decimal(prices["input_off_peak"], "input_off_peak"),
            _decimal(prices["output_off_peak"], "output_off_peak"),
            _decimal(prices["cache_input_off_peak"], "cache_input_off_peak"),
        )
    return (
        _decimal(prices["input"], "input"),
        _decimal(prices["output"], "output"),
        _decimal(prices["cache_input"], "cache_input"),
    )


def _call_usd(
    model: Mapping[str, Any],
    *,
    input_tokens: int,
    output_tokens: int,
    cache_fraction: Decimal,
    period: str,
) -> Decimal:
    input_rate, output_rate, cache_rate = _rates(model, period)
    cache_tokens = Decimal(input_tokens) * cache_fraction
    uncached_tokens = Decimal(input_tokens) - cache_tokens
    return (
        uncached_tokens * input_rate / _MILLION
        + cache_tokens * cache_rate / _MILLION
        + Decimal(output_tokens) * output_rate / _MILLION
    )


def _available_models(
    config: Mapping[str, Any], model_ids: Sequence[str] | None
) -> list[Mapping[str, Any]]:
    models = [m for m in config["models"] if m.get("available", False)]
    if model_ids is None:
        return models
    wanted = {str(model_id) for model_id in model_ids}
    return [model for model in models if str(model["id"]) in wanted]


def _call_plan(
    config: Mapping[str, Any],
    *,
    model_ids: Sequence[str] | None = None,
    modes: Sequence[str] | None = None,
    concurrency_levels: Sequence[int] | None = None,
) -> dict[str, int]:
    assumptions = config["cost_control"]["estimate"]
    models = _available_models(config, model_ids)
    if concurrency_levels is None:
        sweep = int(assumptions["concurrency_levels"])
    else:
        sweep = len(list(concurrency_levels))
    items = int(assumptions["items"])
    microtasks = items * int(assumptions["microtasks_per_item"])
    selected = (
        {
            "single_call",
            "distributed_independent",
            "heterogeneous_competition",
        }
        if modes is None
        else {str(mode) for mode in modes}
    )
    per_sweep = 0
    if "single_call" in selected:
        per_sweep += int(assumptions["single_call_calls_per_model_per_sweep"])
    if "distributed_independent" in selected:
        per_sweep += int(assumptions["distributed_calls_per_model_per_sweep"])
    if "heterogeneous_competition" in selected:
        # One competition candidate per microtask, counted on each model.
        per_sweep += microtasks
    per_model = per_sweep * sweep
    return {
        "available_models": len(models),
        "sweep": sweep,
        "items": items,
        "microtasks": microtasks,
        "calls_per_model": per_model,
        "candidate_calls": per_model * len(models),
        "verifier_calls": int(assumptions["deterministic_verifier_calls_per_sweep"]) * sweep,
    }


def estimate_matrix(
    config: Mapping[str, Any],
    *,
    model_ids: Sequence[str] | None = None,
    modes: Sequence[str] | None = None,
    concurrency_levels: Sequence[int] | None = None,
) -> dict[str, Any]:
    assumptions = config["cost_control"]["estimate"]
    plan = _call_plan(
        config,
        model_ids=model_ids,
        modes=modes,
        concurrency_levels=concurrency_levels,
    )
    retry_rate = _decimal(assumptions["retry_rate"], "retry_rate")
    input_tokens = int(assumptions["input_tokens_per_call"])
    output_tokens = int(assumptions["output_tokens_per_call"])
    cache_fraction = _decimal(assumptions["cache_read_fraction"], "cache_read_fraction")
    if cache_fraction < 0 or cache_fraction > 1:
        raise EstimateError("cache_read_fraction must be between 0 and 1")
    period = str(assumptions.get("price_period", "peak"))
    if period not in {"peak", "off_peak"}:
        raise EstimateError("price_period must be peak or off_peak")

    models = _available_models(config, model_ids)
    attempts_per_model = Decimal(plan["calls_per_model"]) * (Decimal(1) + retry_rate)
    per_model: list[dict[str, Any]] = []
    total = Decimal(0)
    for model in models:
        per_call = _call_usd(
            model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_fraction=cache_fraction,
            period=period,
        )
        amount = per_call * attempts_per_model
        total += amount
        per_model.append(
            {
                "model_id": model["id"],
                "api_model": model["api_model"],
                "calls": plan["calls_per_model"],
                "attempts_with_retries": _text(attempts_per_model),
                "input_tokens": plan["calls_per_model"] * input_tokens,
                "output_tokens": plan["calls_per_model"] * output_tokens,
                "usd": _text(amount),
            }
        )

    baseline = next(
        (m for m in models if m["id"] == config.get("baseline_model")), None
    )
    worst_case = Decimal(0)
    if baseline is not None:
        per_call = _call_usd(
            baseline,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_fraction=cache_fraction,
            period=period,
        )
        worst_case = per_call * Decimal(plan["candidate_calls"]) * (
            Decimal(1) + retry_rate
        )

    return {
        "currency": "USD",
        "price_basis": f"published peak price rows; price_period={period}",
        "assumptions": {
            "input_tokens_per_call": input_tokens,
            "output_tokens_per_call_including_generated_reasoning": output_tokens,
            "cache_read_fraction": _text(cache_fraction),
            "retry_rate": _text(retry_rate),
            "concurrency_levels": plan["sweep"],
            "deterministic_verifier_calls": plan["verifier_calls"],
            "reasoning_tokens_priced_independently": False,
        },
        "calls": plan,
        "per_model": per_model,
        "all_in_estimated_usd": _text(total),
        "baseline_all_calls_estimated_usd": _text(worst_case),
        "recommended_cap_usd": config["cost_control"].get("recommended_cap_usd", "3.00"),
        "note": "Estimate only. Actual billing is N/A unless the provider reports a charge.",
    }


def estimate_sweycoins(config: Mapping[str, Any]) -> dict[str, Any]:
    assumptions = config["cost_control"]["estimate"]
    contracts = config["contracts"]
    models = [m for m in config["models"] if m.get("available", False)]
    sweep = int(assumptions["concurrency_levels"])
    items = int(assumptions["items"])
    microtasks = items * int(assumptions["microtasks_per_item"])
    distributed_per_suite = sum(
        items * int(contracts[key]["sweycoins_milli"])
        for key in ("relevance", "entailment", "atomicity", "extraction", "operation_selection")
    )
    single_per_suite = items * int(contracts["combined"]["sweycoins_milli"])
    competition_per_suite = distributed_per_suite * len(models)
    verifier_per_suite = microtasks * int(contracts["verifier"]["sweycoins_milli"])
    retry_rate = _decimal(assumptions["retry_rate"], "retry_rate")
    multiplier = Decimal(sweep) * (Decimal(1) + retry_rate)
    per_model_modes = single_per_suite + distributed_per_suite
    total_milli = (
        Decimal(per_model_modes * len(models)) * multiplier
        + Decimal(competition_per_suite) * multiplier
        + Decimal(verifier_per_suite) * multiplier
    )
    return {
        "currency": "milli-Sweycoin",
        "single_call_per_model_suite": single_per_suite,
        "distributed_per_model_suite": distributed_per_suite,
        "competition_per_suite": competition_per_suite,
        "deterministic_verifier_per_suite": verifier_per_suite,
        "matrix_estimated_milli": int(total_milli),
        "matrix_estimated_sweycoins": _text(total_milli / Decimal(1000)),
        "run_allocated_milli": int(config["sweycoins"]["run_allocated_milli"]),
        "retry_rate": _text(retry_rate),
        "note": "Host meter estimate; retries and verifier calls are charged separately.",
    }


__all__ = ["EstimateError", "estimate_matrix", "estimate_sweycoins"]
