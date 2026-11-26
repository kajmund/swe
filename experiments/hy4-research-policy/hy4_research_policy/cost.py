"""Cost ledger for one policy arm against DeepSeek Flash.

A missing token, charge, or quota field stays ``N/A``. Cache is priced on its
own row. A subscription allowance is never turned into a per-call cost.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

NA = "N/A"
_MILLION = Decimal(1_000_000)


def _text(value: Decimal | None) -> str:
    if value is None:
        return NA
    return format(value.normalize(), "f")


def _optional_int(mapping: dict[str, Any], *keys: str) -> int | None:
    for key in keys:
        if key not in mapping or mapping[key] is None:
            continue
        value = mapping[key]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"token count {key} must be a non-negative integer")
        return value
    return None


def _usage_mapping(call: dict[str, Any]) -> dict[str, Any]:
    usage = call.get("usage")
    if isinstance(usage, dict):
        return usage
    return call


def _tokens(call: dict[str, Any]) -> dict[str, int | None]:
    explicit = call.get("tokens")
    source = explicit if isinstance(explicit, dict) else _usage_mapping(call)
    details = source.get("completion_tokens_details")
    prompt_details = source.get("prompt_tokens_details")
    reasoning = _optional_int(source, "reasoning", "reasoning_tokens")
    if reasoning is None and isinstance(details, dict):
        reasoning = _optional_int(details, "reasoning_tokens")
    cache_read = _optional_int(
        source,
        "cache_read",
        "prompt_cache_hit_tokens",
        "cache_hit_tokens",
    )
    if cache_read is None and isinstance(prompt_details, dict):
        cache_read = _optional_int(prompt_details, "cached_tokens")
    return {
        "input": _optional_int(source, "input", "prompt_tokens", "input_tokens", "prompt_eval_count"),
        "output": _optional_int(
            source, "output", "completion_tokens", "output_tokens", "eval_count"
        ),
        "reasoning": reasoning,
        "cache_read": cache_read,
        "cache_write": _optional_int(source, "cache_write"),
    }


def _money(raw: object) -> Decimal | None:
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, str):
        raise ValueError("money values must be decimal strings or null")
    amount = Decimal(raw)
    if amount < 0:
        raise ValueError("money values must be non-negative")
    return amount


def _rates(arm: dict[str, Any], period: str | None) -> tuple[str, str, str] | None:
    prices = arm["prices_usd_per_million"]
    if "input_off_peak" in prices:
        if period == "off_peak":
            return (
                prices["input_off_peak"],
                prices["output_off_peak"],
                prices["cache_input_off_peak"],
            )
        if period == "peak":
            return (prices["input"], prices["output"], prices["cache_input"])
        return None
    return (prices["input"], prices["output"], prices["cache_input"])


def _component(tokens: int | None, per_million: str | None) -> Decimal | None:
    if tokens is None or per_million is None:
        return None
    return Decimal(tokens) * Decimal(per_million) / _MILLION


def _price_call(tokens: dict[str, int | None], rates: tuple[str, str, str] | None) -> dict[str, Decimal | None]:
    if rates is None:
        return {"input": None, "output": None, "cache_read": None}
    input_tokens = tokens["input"]
    cache_tokens = tokens["cache_read"]
    uncached: int | None
    if input_tokens is None or cache_tokens is None:
        uncached = None
    else:
        if cache_tokens > input_tokens:
            raise ValueError("cache tokens exceed input tokens")
        uncached = input_tokens - cache_tokens
    return {
        "input": _component(uncached, rates[0]),
        "output": _component(tokens["output"], rates[1]),
        "cache_read": _component(cache_tokens, rates[2]),
    }


def _sum_money(parts: list[Decimal | None]) -> Decimal | None:
    if not parts or any(part is None for part in parts):
        return None
    return sum(parts, Decimal(0))


def _sum_count(values: list[int | None]) -> int | None:
    if not values or any(value is None for value in values):
        return None
    return sum(values)


def _arm(protocol: dict[str, Any], arm_id: str) -> dict[str, Any]:
    for arm in protocol["arms"]:
        if arm["id"] == arm_id:
            return arm
    raise ValueError(f"unknown arm {arm_id!r}")


def _normalize_call(call: dict[str, Any], arm: dict[str, Any]) -> dict[str, Any]:
    tokens = _tokens(call)
    period = call.get("price_period")
    if period is not None and period not in {"peak", "off_peak"}:
        raise ValueError("price_period must be peak, off_peak, or null")
    priced = _price_call(tokens, _rates(arm, period if isinstance(period, str) else None))
    attempt = call.get("attempt")
    if attempt is not None and (isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1):
        raise ValueError("attempt must be a positive integer")
    latency = call.get("latency_ms")
    if latency is not None and (
        isinstance(latency, bool) or not isinstance(latency, int | float) or latency < 0
    ):
        raise ValueError("latency_ms must be non-negative")
    billed = _money(call.get("billed_usd")) if "billed_usd" in call else None
    billed_known = "billed_usd" in call and call.get("billed_usd") is not None
    return {
        "attempt": attempt,
        "retry": attempt is not None and attempt > 1,
        "latency_ms": None if latency is None else float(latency),
        "price_period": period if isinstance(period, str) else None,
        "tokens": tokens,
        "calculated_input_usd": priced["input"],
        "calculated_output_usd": priced["output"],
        "calculated_cache_usd": priced["cache_read"],
        "billed_usd": billed,
        "billed_known": billed_known,
    }


def _per_unit(actual: Decimal | None, count: int) -> str:
    if actual is None or count <= 0:
        return NA
    return _text(actual / Decimal(count))


def meets_quality(score: dict[str, Any], protocol: dict[str, Any]) -> bool:
    threshold = protocol["quality_threshold"]
    recall = Decimal(str(score["evidence_recall"]))
    precision = Decimal(str(score["evidence_precision"]))
    return (
        recall >= Decimal(threshold["min_evidence_recall"])
        and precision >= Decimal(threshold["min_evidence_precision"])
        and int(score["unsupported_claims"]) <= int(threshold["max_unsupported_claims"])
        and (bool(score["stop_correct"]) or not threshold["require_stop_correct"])
        and (not bool(score["false_completeness_claim"]) or threshold["allow_false_completeness"])
    )


def account_question(
    trace: dict[str, Any],
    score: dict[str, Any],
    protocol: dict[str, Any],
) -> dict[str, Any]:
    """Build the per-question cost record. Calculated USD never replaces a charge."""

    arm = _arm(protocol, str(trace["arm_id"]))
    raw_calls = trace.get("calls") or []
    if not isinstance(raw_calls, list):
        raise ValueError("calls must be a list")
    calls = [_normalize_call(call, arm) for call in raw_calls]
    tokens = {
        name: _sum_count([call["tokens"][name] for call in calls])
        for name in ("input", "output", "reasoning", "cache_read", "cache_write")
    }
    input_usd = _sum_money([call["calculated_input_usd"] for call in calls])
    output_usd = _sum_money([call["calculated_output_usd"] for call in calls])
    cache_usd = _sum_money([call["calculated_cache_usd"] for call in calls])
    calculated = _sum_money([input_usd, output_usd, cache_usd]) if calls else None
    if calls and all(call["billed_known"] for call in calls):
        actual = sum((call["billed_usd"] for call in calls), Decimal(0))
    else:
        actual = None
    attempts = [call["attempt"] for call in calls]
    retries = None if any(attempt is None for attempt in attempts) else sum(attempt > 1 for attempt in attempts)
    if not calls:
        retries = 0
    latencies = [call["latency_ms"] for call in calls]
    latency = None if not calls or any(value is None for value in latencies) else sum(latencies)
    quota = trace.get("quota") if isinstance(trace.get("quota"), dict) else {}
    credits = quota.get("credits_consumed_usd") if "credits_consumed_usd" in quota else None
    verified = int(score["verified_evidence"])
    return {
        "arm_id": arm["id"],
        "question_id": trace.get("question_id"),
        "calls": len(calls),
        "retries": retries if retries is not None else NA,
        "latency_ms": None if latency is None else float(latency),
        "tokens": {name: (NA if value is None else value) for name, value in tokens.items()},
        "calculated_input_usd": _text(input_usd),
        "calculated_output_usd": _text(output_usd),
        "calculated_cache_usd": _text(cache_usd),
        "calculated_reasoning_usd": NA,
        "calculated_usd": _text(calculated),
        "actual_usd": _text(actual),
        "quota": {
            "requests": quota.get("requests", NA) if "requests" in quota else NA,
            "credits_consumed_usd": NA if credits is None else str(credits),
            "plan": quota.get("plan", NA) if quota.get("plan") else NA,
        },
        "usd_per_question": _text(actual),
        "usd_per_verified_evidence": _per_unit(actual, verified),
        "verified_evidence": verified,
        "meets_quality": meets_quality(score, protocol),
        "budget": {
            "per_question_allocated_milli": protocol["budget"]["per_question_allocated_milli"],
            "usd_cap": protocol["budget"]["usd_cap"],
        },
    }


def compare_with_deepseek(
    scores: list[dict[str, Any]],
    protocol: dict[str, Any],
) -> dict[str, Any]:
    """Pair each question with the DeepSeek Flash row. Deltas stay N/A without both charges."""

    baseline_id = protocol["cost_comparison"]["baseline_arm"]
    if baseline_id != "deepseek-flash":
        raise ValueError("the cost baseline is DeepSeek Flash")
    grouped: dict[str, dict[str, dict[str, Any]]] = {}
    for score in scores:
        cost = score["cost"]
        grouped.setdefault(str(score["question_id"]), {})[str(score["arm_id"])] = cost
    questions = []
    for question_id, arms in grouped.items():
        baseline = arms.get(baseline_id)
        if baseline is None:
            raise ValueError(f"{question_id} has no DeepSeek Flash cost row")
        pairs = []
        for arm_id, cost in arms.items():
            if arm_id == baseline_id:
                continue
            pairs.append(
                {
                    "arm_id": arm_id,
                    "actual_usd_delta": _delta(cost["actual_usd"], baseline["actual_usd"]),
                    "calculated_usd_delta": _delta(cost["calculated_usd"], baseline["calculated_usd"]),
                    "cache_usd_delta": _delta(
                        cost["calculated_cache_usd"], baseline["calculated_cache_usd"]
                    ),
                    "usd_per_question": cost["usd_per_question"],
                    "baseline_usd_per_question": baseline["usd_per_question"],
                    "usd_per_verified_evidence": cost["usd_per_verified_evidence"],
                    "baseline_usd_per_verified_evidence": baseline["usd_per_verified_evidence"],
                    "meets_quality": cost["meets_quality"],
                    "baseline_meets_quality": baseline["meets_quality"],
                }
            )
        questions.append({"question_id": question_id, "arms": arms, "versus_deepseek_flash": pairs})
    return {
        "baseline_arm": baseline_id,
        "budget": protocol["budget"],
        "quality_threshold": protocol["quality_threshold"],
        "questions": questions,
    }


def _delta(left: str, right: str) -> str:
    if left == NA or right == NA:
        return NA
    return _text(Decimal(left) - Decimal(right))
