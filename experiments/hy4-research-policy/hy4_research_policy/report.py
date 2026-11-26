"""Rebuild the diagnosis report from saved files. It does not call a provider."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

from hy4_research_policy.agent_adapter import NATIVE, decision_status
from hy4_research_policy.plan import DIAGNOSIS_USD_CAP, JEV_INPUT_USD_PER_MILLION

RUN_ID = "20261123T213131Z"


def _text(value: Any) -> str:
    if value in (None, "N/A"):
        return "N/A"
    return format(Decimal(str(value)).normalize(), "f")


def _sum(values: list[Any]) -> str:
    if any(item in (None, "N/A") for item in values):
        return "N/A"
    total = sum((Decimal(str(item)) for item in values), Decimal(0))
    return format(total.normalize(), "f")


def build_summary(run_dir: Path) -> dict[str, Any]:
    saved = json.loads((run_dir / "report.json").read_text(encoding="utf-8"))
    probes = [
        {
            "name": row["name"],
            "structure_error": row["structure_error"],
            "finish_reason": row["finish_reason"],
            "truncated": row["truncated"],
            "thinking_present": row["thinking_present"],
            "output_usd": _text(row["output_usd"]),
            "actual_usd": _text(row["actual_usd"]),
        }
        for row in saved["diagnosis"]
    ]
    tasks = []
    for row in saved["tasks"]:
        classification = row["classification"]
        exact = row["exact_quote"]
        tasks.append(
            {
                "arm_id": row["arm_id"],
                "task": row["task"],
                "question_id": row["question_id"],
                "structure_error": classification["structure_error"],
                "truncated": classification["truncated"],
                "finish_reason": classification["finish_reason"],
                "exact_quote": None if exact is None else exact["exact_quote"],
                "output_usd": _text(row["output_usd"]),
                "actual_usd": _text(row["actual_usd"]),
                "hidden_retry": row["hidden_retry"],
            }
        )
    choices: dict[str, int] = {}
    input_tokens = 0
    output_tokens = 0
    for row in saved["jev"]:
        choice = row["jev"]["choice"]
        choices[choice] = choices.get(choice, 0) + 1
        input_tokens += row["jev"]["input_tokens"]
        output_tokens += row["jev"]["output_tokens"]
    calculated = Decimal(input_tokens) * JEV_INPUT_USD_PER_MILLION / Decimal(1_000_000)
    return {
        "run_id": run_dir.name,
        "usd_cap": format(DIAGNOSIS_USD_CAP, "f"),
        "reserved_worst_usd": _text(saved["reserved_worst_usd"]),
        "stopped": saved["stopped"],
        "validated_policy_mode": saved["validated_policy_mode"],
        "task_mode": saved["task_mode"],
        "paid_calls_for_this_report": 0,
        "diagnosis": {
            "count": len(probes),
            "probes": probes,
            "output_usd": _sum([row["output_usd"] for row in probes]),
            "actual_usd": _sum([row["actual_usd"] for row in probes]),
        },
        "tasks": {
            "count": len(tasks),
            "schema_valid": sum(row["structure_error"] is None for row in tasks),
            "rows": tasks,
            "output_usd": _sum([row["output_usd"] for row in tasks]),
            "actual_usd": _sum([row["actual_usd"] for row in tasks]),
            "hidden_retry": any(row["hidden_retry"] for row in tasks),
        },
        "jev": {
            "count": len(saved["jev"]),
            "http_status": sorted({row["http_status"] for row in saved["jev"]}),
            "choices": choices,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "input_usd_per_million": format(JEV_INPUT_USD_PER_MILLION, "f"),
            "calculated_input_usd": format(calculated.normalize(), "f"),
            "output_priced": False,
            "actual_usd": _sum([row["actual_usd"] for row in saved["jev"]]),
        },
        "adapter": {
            "catalog_version": "2.54.0",
            "native_importable": {name: item["importable"] for name, item in NATIVE.items()},
            "prefixes": {name: item["prefix"] for name, item in NATIVE.items()},
            "laya": decision_status("system-one", "laya"),
            "clm": decision_status("system-one", "clm-latest"),
            "jev": decision_status("typesafe", "jev-1.13.0"),
            "clef": decision_status("clef", "clef"),
            "native_constructed": False,
        },
    }


def write_summary(run_dir: Path) -> Path:
    target = run_dir / "summary.json"
    summary = build_summary(run_dir)
    target.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return target
