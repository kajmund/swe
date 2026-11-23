"""Conservative USD and Sweycoin estimate for one full three-arm matrix.

The figures use list or peak cache-miss prices. Temporary Hy discounts are
reported separately and are not the approval bound.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any


def _money(tokens: int, per_million: str) -> Decimal:
    return Decimal(tokens) * Decimal(per_million) / Decimal(1_000_000)


def _text(value: Decimal) -> str:
    return format(value.normalize(), "f")


def _arm_call_usd(arm: dict[str, Any], *, input_tokens: int, output_tokens: int) -> Decimal:
    prices = arm["prices_usd_per_million"]
    return _money(input_tokens, prices["input"]) + _money(output_tokens, prices["output"])


def estimate_matrix(protocol: dict[str, Any]) -> dict[str, Any]:
    assumptions = protocol["estimate"]
    budget = protocol["budget"]
    questions = int(budget["question_count"])
    calls_per_arm = int(assumptions["policy_calls_per_question"]) * questions
    input_tokens = int(assumptions["input_tokens"])
    low_output = int(assumptions["output_tokens_low"])
    high_output = int(assumptions["output_tokens_high"])
    if assumptions.get("use_temporary_discount_for_approval"):
        raise ValueError("approval estimate must use list or peak prices")

    per_arm: list[dict[str, str]] = []
    low = Decimal(0)
    high = Decimal(0)
    discounted_high = Decimal(0)
    for arm in protocol["arms"]:
        low_call = _arm_call_usd(arm, input_tokens=input_tokens, output_tokens=low_output)
        high_call = _arm_call_usd(arm, input_tokens=input_tokens, output_tokens=high_output)
        arm_low = low_call * calls_per_arm
        arm_high = high_call * calls_per_arm
        fraction = Decimal(arm["temporary_discount_fraction"])
        low += arm_low
        high += arm_high
        discounted_high += arm_high * fraction
        per_arm.append(
            {
                "arm_id": arm["id"],
                "model": arm["model"],
                "calls": str(calls_per_arm),
                "low_usd": _text(arm_low),
                "high_usd": _text(arm_high),
            }
        )

    jev_calls = int(assumptions["jev_calls_per_question"]) * questions
    if not assumptions.get("jev_once_for_all_arms"):
        jev_calls *= len(protocol["arms"])
    jev = _money(
        jev_calls * int(assumptions["jev_input_tokens"]),
        protocol["evaluator"]["usd_per_million_input"],
    )
    low += jev
    high += jev
    discounted_high += jev

    tool_tariff = {item["id"]: int(item["tariff_milli"]) for item in protocol["tools"]}
    decision_tariff = {item["kind"]: int(item["tariff_milli"]) for item in protocol["decisions"]}
    pairs = int(assumptions["tool_pairs_per_question"])
    swey_per_question = (
        decision_tariff["decompose"]
        + decision_tariff["stop"]
        + pairs * (tool_tariff["retrieve"] + tool_tariff["read_passage"])
    )
    allocated = int(budget["per_question_allocated_milli"])
    if swey_per_question > allocated:
        raise ValueError("estimated Sweycoin use exceeds the frozen per-question allocation")
    return {
        "calls_per_arm": calls_per_arm,
        "arms": per_arm,
        "jev_calls": jev_calls,
        "jev_usd": _text(jev),
        "low_usd": _text(low),
        "approval_usd": _text(high),
        "discounted_high_usd": _text(discounted_high),
        "usd_cap": budget["usd_cap"],
        "sweycoins_milli_per_question": swey_per_question,
        "sweycoins_milli_per_arm": swey_per_question * questions,
        "per_question_allocated_milli": allocated,
        "within_existing_usd_cap": high <= Decimal(budget["usd_cap"]),
        "approval_required": True,
    }
