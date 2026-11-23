"""Worst-case reservation for the schema diagnosis and the isolated tasks.

The cap is a new $2 pool. It is not the earlier pilot's remaining balance.
Retries are not included: a failed validation is logged and not sent again.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from hy4_research_policy.pilot import worst_call_usd

DIAGNOSIS_USD_CAP = Decimal("2")
DIAGNOSIS_OUTPUT_TOKENS = 1024
TASK_OUTPUT_TOKENS = 512
DIAGNOSIS_REQUESTS = 9
TASKS = ("decomposition", "next_action", "evidence_gap", "stop")
JEV_INPUT_USD_PER_MILLION = Decimal("0.042")
JEV_CALLS_UPPER = 80
JEV_INPUT_BOUND = 2000
_MILLION = Decimal(1_000_000)


def _arm(protocol: dict[str, Any], arm_id: str) -> dict[str, Any]:
    for arm in protocol["arms"]:
        if arm["id"] == arm_id:
            return arm
    raise KeyError(arm_id)


def _money(value: Decimal) -> str:
    return format(value.normalize(), "f")


def diagnosis_plan(
    protocol: dict[str, Any],
    *,
    replay_input_tokens: int,
    task_input_tokens: int,
) -> dict[str, Any]:
    deepseek = _arm(protocol, "deepseek-flash")
    hy4 = _arm(protocol, "hy4")
    questions = len(protocol["frozen"]["question_ids"])
    task_calls = len(TASKS) * questions
    diagnosis = worst_call_usd(
        deepseek, input_tokens=replay_input_tokens, max_output=DIAGNOSIS_OUTPUT_TOKENS
    ) * DIAGNOSIS_REQUESTS
    deepseek_tasks = worst_call_usd(
        deepseek, input_tokens=task_input_tokens, max_output=TASK_OUTPUT_TOKENS
    ) * task_calls
    hy4_tasks = worst_call_usd(
        hy4, input_tokens=task_input_tokens, max_output=TASK_OUTPUT_TOKENS
    ) * task_calls
    jev = Decimal(JEV_CALLS_UPPER) * Decimal(JEV_INPUT_BOUND) * JEV_INPUT_USD_PER_MILLION / _MILLION
    total = diagnosis + deepseek_tasks + hy4_tasks + jev
    return {
        "usd_cap": _money(DIAGNOSIS_USD_CAP),
        "retries": 0,
        "diagnosis_requests": DIAGNOSIS_REQUESTS,
        "diagnosis_output_tokens": DIAGNOSIS_OUTPUT_TOKENS,
        "task_calls_per_arm": task_calls,
        "task_output_tokens": TASK_OUTPUT_TOKENS,
        "diagnosis_usd": _money(diagnosis),
        "deepseek_tasks_usd": _money(deepseek_tasks),
        "hy4_tasks_usd": _money(hy4_tasks),
        "jev_usd": _money(jev),
        "worst_usd": _money(total),
        "fits": total <= DIAGNOSIS_USD_CAP,
        "price_basis": "cache-miss peak for policy calls; JEV input only",
    }
