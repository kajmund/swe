import json
from decimal import Decimal

from hy4_research_policy.cost import NA, compare_with_deepseek
from hy4_research_policy.freeze import load_protocol
from test_offline import _score, _trace


def _with_calls(name: str, arm_id: str, calls: list[dict], quota: dict | None = None) -> dict:
    trace = json.loads(json.dumps(_trace(name)))
    trace["arm_id"] = arm_id
    trace["calls"] = calls
    if quota is not None:
        trace["quota"] = quota
    protocol = load_protocol()
    from hy4_research_policy.freeze import load_corpus, load_questions, question_by_id
    from hy4_research_policy.score import score_trace

    question = question_by_id(load_questions(protocol))[trace["question_id"]]
    return score_trace(trace, question, load_corpus(protocol), protocol)


def test_absent_usage_stays_na_and_does_not_become_zero_cost() -> None:
    scored = _score("complete_direct.json")
    cost = scored["cost"]
    assert cost["calls"] == 0
    assert cost["retries"] == 0
    assert cost["tokens"] == {
        "input": NA,
        "output": NA,
        "reasoning": NA,
        "cache_read": NA,
        "cache_write": NA,
    }
    assert cost["calculated_usd"] == NA
    assert cost["calculated_cache_usd"] == NA
    assert cost["actual_usd"] == NA
    assert cost["usd_per_question"] == NA
    assert cost["usd_per_verified_evidence"] == NA
    assert cost["quota"]["credits_consumed_usd"] == NA
    assert cost["budget"]["per_question_allocated_milli"] == 50_000
    assert cost["budget"]["usd_cap"] == "25.0"


def test_cache_is_priced_apart_from_input_and_reasoning_is_not_priced() -> None:
    call = {
        "attempt": 1,
        "latency_ms": 12,
        "tokens": {
            "input": 1000,
            "output": 100,
            "reasoning": 40,
            "cache_read": 200,
        },
        "billed_usd": "0.001",
    }
    scored = _with_calls("complete_direct.json", "hy4", [call])
    cost = scored["cost"]
    assert cost["tokens"]["cache_read"] == 200
    assert cost["tokens"]["reasoning"] == 40
    assert cost["calculated_input_usd"] == "0.0006672"
    assert cost["calculated_cache_usd"] == "0.0000084"
    assert cost["calculated_output_usd"] == "0.0002501"
    assert cost["calculated_reasoning_usd"] == NA
    assert cost["calculated_usd"] == "0.0009257"
    assert cost["actual_usd"] == "0.001"
    assert cost["usd_per_question"] == "0.001"
    assert cost["usd_per_verified_evidence"] == "0.001"
    assert Decimal(cost["calculated_input_usd"]) != Decimal(1000 * 834) / Decimal(1_000_000_000)


def test_deepseek_without_a_price_period_is_not_given_a_peak_price() -> None:
    scored = _with_calls(
        "complete_direct.json",
        "deepseek-flash",
        [
            {
                "attempt": 1,
                "latency_ms": 9,
                "prompt_tokens": 1000,
                "completion_tokens": 100,
                "prompt_cache_hit_tokens": 200,
                "completion_tokens_details": {"reasoning_tokens": 15},
            }
        ],
    )
    cost = scored["cost"]
    assert cost["tokens"]["input"] == 1000
    assert cost["tokens"]["cache_read"] == 200
    assert cost["tokens"]["reasoning"] == 15
    assert cost["calculated_usd"] == NA
    assert cost["calculated_cache_usd"] == NA
    assert cost["actual_usd"] == NA
    assert cost["usd_per_question"] == NA


def test_ollama_quota_is_not_converted_into_a_marginal_cost() -> None:
    scored = _with_calls(
        "complete_direct.json",
        "hy4",
        [
            {
                "attempt": 1,
                "latency_ms": 4,
                "prompt_eval_count": 20,
                "eval_count": 5,
            }
        ],
        quota={"plan": "pro", "requests": 1},
    )
    cost = scored["cost"]
    assert cost["tokens"]["input"] == 20
    assert cost["tokens"]["cache_read"] == NA
    assert cost["calculated_usd"] == NA
    assert cost["actual_usd"] == NA
    assert cost["usd_per_question"] == NA
    assert cost["quota"]["plan"] == "pro"
    assert cost["quota"]["requests"] == 1
    assert cost["quota"]["credits_consumed_usd"] == NA
    assert "60" not in json.dumps(cost["actual_usd"])


def test_retry_without_a_charge_keeps_the_question_cost_unknown() -> None:
    scored = _with_calls(
        "complete_direct.json",
        "hy4",
        [
            {
                "attempt": 1,
                "latency_ms": 10,
                "billed_usd": "0.001",
                "tokens": {"input": 10, "output": 2, "cache_read": 0},
            },
            {"attempt": 2, "latency_ms": 15, "prompt_eval_count": 10, "eval_count": 2},
        ],
    )
    cost = scored["cost"]
    assert cost["calls"] == 2
    assert cost["retries"] == 1
    assert cost["latency_ms"] == 25
    assert cost["actual_usd"] == NA
    assert cost["usd_per_verified_evidence"] == NA


def test_no_verified_evidence_has_no_unit_cost_even_when_a_charge_exists() -> None:
    scored = _with_calls(
        "unsupported_stop.json",
        "hy4",
        [
            {
                "attempt": 1,
                "latency_ms": 3,
                "billed_usd": "0.004",
                "tokens": {"input": 8, "output": 2, "cache_read": 0},
            }
        ],
    )
    assert scored["verified_evidence"] == 0
    assert scored["cost"]["meets_quality"] is False
    assert scored["cost"]["actual_usd"] == "0.004"
    assert scored["cost"]["usd_per_question"] == "0.004"
    assert scored["cost"]["usd_per_verified_evidence"] == NA


def test_comparison_uses_one_budget_and_leaves_unknown_deltas_na() -> None:
    protocol = load_protocol()
    hy4 = _with_calls(
        "complete_direct.json",
        "hy4",
        [
            {
                "attempt": 1,
                "latency_ms": 12,
                "billed_usd": "0.001",
                "tokens": {"input": 1000, "output": 100, "reasoning": 40, "cache_read": 200},
            }
        ],
    )
    deepseek = _with_calls(
        "complete_direct.json",
        "deepseek-flash",
        [
            {
                "attempt": 1,
                "latency_ms": 8,
                "price_period": "peak",
                "tokens": {"input": 1000, "output": 100, "reasoning": 15, "cache_read": 200},
            }
        ],
    )
    report = compare_with_deepseek([hy4, deepseek], protocol)
    assert report["budget"]["per_question_allocated_milli"] == 50_000
    assert report["budget"]["usd_cap"] == "25.0"
    assert report["quality_threshold"]["same_for_every_arm"] is True
    pair = report["questions"][0]["versus_deepseek_flash"][0]
    assert pair["arm_id"] == "hy4"
    assert pair["meets_quality"] is True
    assert pair["baseline_meets_quality"] is True
    assert pair["actual_usd_delta"] == NA
    assert pair["baseline_usd_per_question"] == NA
    assert pair["usd_per_question"] == "0.001"
    assert pair["calculated_usd_delta"] == "0.0005645"
    assert pair["cache_usd_delta"] == "0.0000072"
    assert hy4["cost"]["budget"] == deepseek["cost"]["budget"]
