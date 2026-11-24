"""All-in cost accounting for candidate and verifier calls.

Two numbers are kept separate and are never substituted for each other:

* ``calculated_usd`` is the published price-list equivalent from token counts.
* ``actual_usd`` is a provider-reported charge. It is ``N/A`` unless every call
  reports a non-null billed amount.

Cache is priced on its own line. Reasoning tokens are recorded but not given a
price because no verified price row prices them independently.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Sequence

NA = "N/A"
_MILLION = Decimal(1_000_000)
_TOKEN_KEYS = ("input", "output", "reasoning", "cache_read", "cache_write")


class CostError(ValueError):
    """A cost record is malformed or internally inconsistent."""


@dataclass(frozen=True, slots=True)
class TokenUsage:
    input: int | None = None
    output: int | None = None
    reasoning: int | None = None
    cache_read: int | None = None
    cache_write: int | None = None
    source: str = "provider"
    cache_read_assumed_miss: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "input": _count(self.input),
            "output": _count(self.output),
            "reasoning": _count(self.reasoning),
            "cache_read": _count(self.cache_read),
            "cache_write": _count(self.cache_write),
            "source": self.source,
            "cache_read_assumed_miss": self.cache_read_assumed_miss,
        }


def _count(value: int | None) -> int | str:
    return NA if value is None else value


def _optional_count(mapping: Mapping[str, Any], key: str) -> int | None:
    if key not in mapping or mapping[key] is None:
        return None
    value = mapping[key]
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise CostError(f"token count {key!r} must be a non-negative integer")
    return value


def _decimal(value: Any, *, name: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        raise CostError(f"{name} must be a decimal string")
    try:
        amount = Decimal(str(value))
    except InvalidOperation as exc:
        raise CostError(f"{name} is not a decimal") from exc
    if amount < 0:
        raise CostError(f"{name} must be non-negative")
    return amount


def _text(value: Decimal | None) -> str:
    if value is None:
        return NA
    return format(value.normalize(), "f")


def _rates(model: Mapping[str, Any], period: str | None) -> tuple[Decimal, Decimal, Decimal] | None:
    prices = model.get("prices_usd_per_million")
    if not isinstance(prices, Mapping):
        raise CostError(f"model {model.get('id')!r} has no price table")
    if "input_off_peak" in prices:
        if period == "off_peak":
            return (
                _decimal(prices["input_off_peak"], name="input_off_peak"),
                _decimal(prices["output_off_peak"], name="output_off_peak"),
                _decimal(prices["cache_input_off_peak"], name="cache_input_off_peak"),
            )
        if period == "peak":
            return (
                _decimal(prices["input"], name="input"),
                _decimal(prices["output"], name="output"),
                _decimal(prices["cache_input"], name="cache_input"),
            )
        return None
    return (
        _decimal(prices["input"], name="input"),
        _decimal(prices["output"], name="output"),
        _decimal(prices["cache_input"], name="cache_input"),
    )


def _component(tokens: int | None, rate: Decimal | None) -> Decimal | None:
    if tokens is None or rate is None:
        return None
    return Decimal(tokens) * rate / _MILLION


def _sum(values: Sequence[Decimal | None]) -> Decimal | None:
    if not values or any(value is None for value in values):
        return None
    return sum(value for value in values if value is not None)


def _sum_int(values: Sequence[int | None]) -> int | None:
    if not values:
        return 0
    if any(value is None for value in values):
        return None
    return sum(value for value in values if value is not None)


def price_call(
    model: Mapping[str, Any],
    tokens: TokenUsage,
    *,
    period: str | None,
) -> dict[str, Decimal | None]:
    rates = _rates(model, period)
    if rates is None:
        return {"input": None, "output": None, "cache_read": None}
    input_tokens = tokens.input
    cache_tokens = tokens.cache_read
    if input_tokens is not None and cache_tokens is None:
        # A missing cache count cannot be silently treated as a cache miss.
        uncached = None
    elif input_tokens is None or cache_tokens is None:
        uncached = None
    else:
        if cache_tokens > input_tokens:
            raise CostError("cache_read exceeds input tokens")
        uncached = input_tokens - cache_tokens
    return {
        "input": _component(uncached, rates[0]),
        "output": _component(tokens.output, rates[1]),
        "cache_read": _component(cache_tokens, rates[2]),
    }


@dataclass(frozen=True, slots=True)
class CallCost:
    model_id: str
    role: str
    attempt: int | None
    price_period: str | None
    tokens: TokenUsage
    latency_ms: float | None
    billed_usd: Decimal | None
    billed_known: bool
    calculated_input_usd: Decimal | None
    calculated_output_usd: Decimal | None
    calculated_cache_usd: Decimal | None

    @property
    def calculated_usd(self) -> Decimal | None:
        return _sum(
            (
                self.calculated_input_usd,
                self.calculated_output_usd,
                self.calculated_cache_usd,
            )
        )

    @property
    def retry(self) -> bool:
        return self.attempt is not None and self.attempt > 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "model_id": self.model_id,
            "role": self.role,
            "attempt": self.attempt if self.attempt is not None else NA,
            "retry": self.retry,
            "price_period": self.price_period or NA,
            "latency_ms": None if self.latency_ms is None else round(self.latency_ms, 3),
            "tokens": self.tokens.to_dict(),
            "calculated_input_usd": _text(self.calculated_input_usd),
            "calculated_output_usd": _text(self.calculated_output_usd),
            "calculated_cache_usd": _text(self.calculated_cache_usd),
            "calculated_reasoning_usd": NA,
            "calculated_usd": _text(self.calculated_usd),
            "actual_usd": _text(self.billed_usd) if self.billed_known else NA,
        }


def normalize_call(raw: Mapping[str, Any], model: Mapping[str, Any]) -> CallCost:
    role = str(raw.get("role", "candidate"))
    if role not in {"candidate", "verifier"}:
        raise CostError("role must be candidate or verifier")
    attempt = raw.get("attempt")
    if attempt is not None and (
        isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1
    ):
        raise CostError("attempt must be a positive integer")
    period = raw.get("price_period")
    if period is not None and period not in {"peak", "off_peak"}:
        raise CostError("price_period must be peak, off_peak, or null")
    latency = raw.get("latency_ms")
    if latency is not None and (
        isinstance(latency, bool)
        or not isinstance(latency, (int, float))
        or latency < 0
    ):
        raise CostError("latency_ms must be non-negative")
    raw_tokens = raw.get("tokens")
    if not isinstance(raw_tokens, Mapping):
        raise CostError("tokens must be an object")
    tokens = TokenUsage(
        input=_optional_count(raw_tokens, "input"),
        output=_optional_count(raw_tokens, "output"),
        reasoning=_optional_count(raw_tokens, "reasoning"),
        cache_read=_optional_count(raw_tokens, "cache_read"),
        cache_write=_optional_count(raw_tokens, "cache_write"),
        source=str(raw_tokens.get("source", "provider")),
        cache_read_assumed_miss=bool(
            raw_tokens.get("cache_read_assumed_miss", False)
        ),
    )
    priced = price_call(model, tokens, period=period if isinstance(period, str) else None)
    billed_known = "billed_usd" in raw and raw.get("billed_usd") is not None
    billed = _decimal(raw["billed_usd"], name="billed_usd") if billed_known else None
    return CallCost(
        model_id=str(model.get("id", raw.get("model_id", "unknown"))),
        role=role,
        attempt=attempt,
        price_period=period if isinstance(period, str) else None,
        tokens=tokens,
        latency_ms=None if latency is None else float(latency),
        billed_usd=billed,
        billed_known=billed_known,
        calculated_input_usd=priced["input"],
        calculated_output_usd=priced["output"],
        calculated_cache_usd=priced["cache_read"],
    )


def account_calls(
    raw_calls: Sequence[Mapping[str, Any]],
    *,
    models: Mapping[str, Mapping[str, Any]],
    verified_evidence: int = 0,
) -> dict[str, Any]:
    """Account candidate and verifier calls, including retries and cache."""

    calls: list[CallCost] = []
    for raw in raw_calls:
        model_id = str(raw.get("model_id", ""))
        if model_id not in models:
            raise CostError(f"unknown model {model_id!r}")
        calls.append(normalize_call(raw, models[model_id]))

    def subset(role: str | None) -> list[CallCost]:
        return [call for call in calls if role is None or call.role == role]

    def totals(items: Sequence[CallCost]) -> dict[str, Any]:
        tokens = {
            key: _sum_int([getattr(item.tokens, key) for item in items])
            for key in _TOKEN_KEYS
        }
        input_usd = _sum([item.calculated_input_usd for item in items])
        output_usd = _sum([item.calculated_output_usd for item in items])
        cache_usd = _sum([item.calculated_cache_usd for item in items])
        calculated = _sum([input_usd, output_usd, cache_usd]) if items else None
        complete = [item for item in items if item.calculated_usd is not None]
        complete_tokens = {
            key: _sum_int([getattr(item.tokens, key) for item in complete])
            for key in _TOKEN_KEYS
        }
        calculated_complete = (
            sum((item.calculated_usd for item in complete if item.calculated_usd is not None), Decimal(0))
            if complete
            else None
        )
        if items and all(item.billed_known for item in items):
            actual: Decimal | None = sum(
                (item.billed_usd for item in items if item.billed_usd is not None),
                Decimal(0),
            )
        else:
            actual = None
        attempts = [item.attempt for item in items]
        retries = (
            0
            if not items
            else (None if any(value is None for value in attempts) else sum(value > 1 for value in attempts))
        )
        latencies = [item.latency_ms for item in items]
        latency = (
            None
            if not items or any(value is None for value in latencies)
            else sum(value for value in latencies if value is not None)
        )
        return {
            "calls": len(items),
            "calls_with_complete_usage": len(complete),
            "calls_with_unknown_usage": len(items) - len(complete),
            "cache_read_assumed_miss_calls": sum(
                1 for item in items if item.tokens.cache_read_assumed_miss
            ),
            "retries": retries if retries is not None else NA,
            "latency_ms": None if latency is None else round(latency, 3),
            "tokens": {key: _count(value) for key, value in tokens.items()},
            "tokens_complete_calls": {
                key: _count(value) for key, value in complete_tokens.items()
            },
            "estimated_tokens": any(item.tokens.source != "provider" for item in items),
            "calculated_input_usd": _text(input_usd),
            "calculated_output_usd": _text(output_usd),
            "calculated_cache_usd": _text(cache_usd),
            "calculated_reasoning_usd": NA,
            "calculated_usd": _text(calculated),
            "calculated_usd_complete_calls": _text(calculated_complete),
            "actual_usd": _text(actual),
        }

    candidate = totals(subset("candidate"))
    verifier = totals(subset("verifier"))
    all_in = totals(calls)
    actual_all_in = all_in["actual_usd"]
    calculated_all_in = all_in["calculated_usd"]
    verified = int(verified_evidence)
    if verified < 0:
        raise CostError("verified_evidence must be non-negative")
    actual_value = None if actual_all_in == NA else Decimal(actual_all_in)
    return {
        "candidate": candidate,
        "verifier": verifier,
        "all_in": all_in,
        "all_in_complete_calls_calculated_usd": all_in[
            "calculated_usd_complete_calls"
        ],
        "usd_per_verified_evidence": (
            NA
            if actual_value is None or verified <= 0
            else _text(actual_value / Decimal(verified))
        ),
        "calculated_usd_per_verified_evidence": (
            NA
            if calculated_all_in == NA or verified <= 0
            else _text(Decimal(calculated_all_in) / Decimal(verified))
        ),
        "verified_evidence": verified,
    }


__all__ = [
    "CallCost",
    "CostError",
    "NA",
    "TokenUsage",
    "account_calls",
    "normalize_call",
    "price_call",
]
