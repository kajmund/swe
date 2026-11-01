"""task -> expression decision -> end."""

from __future__ import annotations

import pytest

pytest.importorskip("swe_native")

from swe.runtime import run_workflow
from swe_runtime_helpers import compile_runtime

pytestmark = pytest.mark.swe_runtime


def _definition() -> object:
    return compile_runtime(
        {
            "start": {
                "type": "task",
                "handler": "demo.probe",
                "input": {"flag": "$.flag"},
                "next": "check",
            },
            "check": {
                "type": "decision",
                "evaluator": "expression",
                "condition": {"eq": [{"var": "$.start.result.flag"}, {"const": True}]},
                "routes": {"true": "ok", "false": "no"},
            },
            "ok": {"type": "end", "status": "completed"},
            "no": {"type": "end", "status": "incomplete"},
        },
        handlers=frozenset({"demo.probe"}),
    )


async def _echo(payload: dict[str, object]) -> dict[str, object]:
    return dict(payload)


async def test_decision_true_completes() -> None:
    result = await run_workflow(_definition(), {"demo.probe": _echo}, {"flag": True})
    assert result.status == "completed"
    kinds = [event["type"] for event in result.events]
    assert "DecisionTaken" in kinds
    assert "RunCompleted" in kinds


async def test_decision_false_is_incomplete() -> None:
    result = await run_workflow(_definition(), {"demo.probe": _echo}, {"flag": False})
    assert result.status == "incomplete"
