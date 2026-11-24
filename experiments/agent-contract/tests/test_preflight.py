import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "python"))
pytest.importorskip("swe_native")
pytest.importorskip("swe.budget")
pytest.importorskip("swe_budget")

from agent_contract.preflight import run_preflight


def test_offline_machine_preflight() -> None:
    report = asyncio.run(run_preflight())
    assert report["paid_calls"] == 0
    assert report["runtime_budget"]["attached"] is True
    assert report["runtime_budget"]["spent_milli"] == 10
    assert report["runtime_budget"]["reserved_milli"] == 0
    assert report["package_runtime_budget"]["attached"] is False
    assert "budget" not in report["runner_parameters"]
    gap = report["package_dispatch_gap"]
    assert gap["package_dispatcher"] is False
    assert gap["fail_closed"] is True
    assert gap["swe_ready"] is True
    assert gap["swe_ordered_default"] == "layer_map"
    assert gap["require_swe_dispatch_raises"] is True
    barrier = report["layer_barrier"]
    assert barrier["schedule"] == "ready"
    assert barrier["map_nodes"] == ["microtasks"]
    assert barrier["layers"] == [["slow", "fast"], ["after_fast"]]
    assert barrier["after_fast_started_before_slow_released"] is True
    assert barrier["slow_still_blocked_when_after_fast_started"] is True
    assert set(barrier["starts_while_slow_blocked"]) == {"slow", "fast", "after_fast"}
    assert len({row["invocation_id"] for row in barrier["trace"]}) == 3
    contract = report["contract"]
    assert contract["schedule"] == "ready"
    assert contract["same_definition"] is True
    assert contract["same_task_ids"] is True
    assert len({row["invocation_id"] for row in contract["alpha_trace"]}) == 10
    assert contract["handler_input_keys"] == ["depends_on", "payload", "task_id"]
    assert contract["upstream_output_present"] is False
    assert contract["validator_model_calls"] == 0
    assert all(
        row["semantic_check"] == "upstream output is not in the handler input"
        for row in contract["alpha_trace"]
        if row["role"] == "validator"
    )
    assert contract["generation_calls"] == ["relevance", "extraction", "operation"]
    assert sorted(contract["decision_calls"]) == ["atomicity", "entailment"]
    assert contract["alpha_generation_providers"] == ["alpha"]
    assert contract["beta_generation_providers"] == ["beta"]
    assert contract["decision_providers"] == ["system-one"]
    assert contract["decision_models"] == ["laya"]
    assert report["replay"]["schedule"] == "ready"
    assert report["replay"]["resume_scope"] == "in-process"
    assert report["replay"]["fresh_ledger"] is False
    assert report["replay"]["replayed_task_executed_again"] is False
    assert report["replay"]["executed_before_resume"] == ["a"]
    assert report["replay"]["executed_after_resume"] == ["a", "b"]
    assert report["replay"]["resume_status"] == "completed"
