"""Conservative USD and Sweycoin estimate for the available policy arms.

Unavailable arms, including Hy4 Flash on Ollama Cloud, add no USD.
Prices are the published cache-miss rates. Hy4 uses its single Ollama row.
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
    billable = [arm for arm in protocol["arms"] if arm.get("available", True)]
    if not billable:
        raise ValueError("no available policy arms")

    per_arm: list[dict[str, str]] = []
    low = Decimal(0)
    high = Decimal(0)
    for arm in billable:
        low_call = _arm_call_usd(arm, input_tokens=input_tokens, output_tokens=low_output)
        high_call = _arm_call_usd(arm, input_tokens=input_tokens, output_tokens=high_output)
        arm_low = low_call * calls_per_arm
        arm_high = high_call * calls_per_arm
        low += arm_low
        high += arm_high
        per_arm.append(
            {
                "arm_id": arm["id"],
                "model": str(arm["model"]),
                "calls": str(calls_per_arm),
                "low_usd": _text(arm_low),
                "high_usd": _text(arm_high),
            }
        )

    jev_calls = int(assumptions["jev_calls_per_question"]) * questions
    if not assumptions.get("jev_once_for_all_arms"):
        jev_calls *= len(billable)
    jev = _money(
        jev_calls * int(assumptions["jev_input_tokens"]),
        protocol["evaluator"]["usd_per_million_input"],
    )
    low += jev
    high += jev

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
        "billable_arms": [arm["id"] for arm in billable],
        "unavailable_arms": [
            arm["id"] for arm in protocol["arms"] if not arm.get("available", True)
        ],
        "arms": per_arm,
        "jev_calls": jev_calls,
        "jev_usd": _text(jev),
        "low_usd": _text(low),
        "approval_usd": _text(high),
        "usd_cap": budget["usd_cap"],
        "sweycoins_milli_per_question": swey_per_question,
        "sweycoins_milli_per_arm": swey_per_question * questions,
        "per_question_allocated_milli": allocated,
        "within_existing_usd_cap": high <= Decimal(budget["usd_cap"]),
        "approval_required": True,
    }
