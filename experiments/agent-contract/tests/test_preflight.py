import asyncio

import pytest

pytest.importorskip("swe_native")

from agent_contract.preflight import run_preflight


def test_offline_machine_preflight() -> None:
    report = asyncio.run(run_preflight())
    assert report["paid_calls"] == 0
    assert report["runtime_budget"]["attached"] is False
    assert report["package_runtime_budget"]["attached"] is False
    assert "budget" not in report["runner_parameters"]
    assert report["package_dispatch_gap"]["event_ready"] is False
    assert report["package_dispatch_gap"]["require_swe_dispatch_raises"] is True
    barrier = report["layer_barrier"]
    assert barrier["layers"] == [["slow", "fast"], ["after_fast"]]
    assert barrier["after_fast_started_before_slow_released"] is False
    assert set(barrier["starts_while_slow_blocked"]) == {"slow", "fast"}
    contract = report["contract"]
    assert contract["same_definition"] is True
    assert contract["same_task_ids"] is True
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
    assert report["replay"]["replayed_task_executed_again"] is False
    assert report["replay"]["executed_before_resume"] == ["a"]
    assert report["replay"]["executed_after_resume"] == ["a", "b"]
    assert report["replay"]["resume_status"] == "completed"
