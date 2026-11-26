import json
from pathlib import Path

from hy4_research_policy.schemas import PolicyDecision
from hy4_research_policy.structure import classify_output

RUN = Path(__file__).resolve().parents[1] / "runs" / "20261123T205650Z"
FAILED = (
    "multi_document_orbit",
    "dependent_northstar_release",
    "conflicting_orbit_budget",
    "missing_helios_retention",
)


def _saved_observation(question_id: str) -> dict:
    trace = json.loads((RUN / f"deepseek-flash-{question_id}.json").read_text(encoding="utf-8"))
    call = trace["calls"][0]
    return {
        "model": trace["model"],
        "content_present": False,
        "thinking_present": False,
        "finish_reason": call["done_reason"],
        "usage": {"input": call["prompt_eval_count"], "output": call["eval_count"]},
    }


def test_prior_deepseek_traces_dropped_the_response_text() -> None:
    for question_id in FAILED:
        result = classify_output(
            _saved_observation(question_id),
            PolicyDecision,
            requested_model="deepseek-v4.1-flash:cloud",
            max_output=1024,
            channel="content",
        )
        assert result["structure_error"] == "content_not_retained"
        assert result["parsed"] is None
        assert result["content"] is None


def test_wrong_object_is_a_schema_error_and_keeps_the_text() -> None:
    content = json.dumps({"answer": "yes"})
    result = classify_output(
        {
            "model": "deepseek-v4.1-flash:cloud",
            "content": content,
            "content_present": True,
            "thinking_present": False,
            "finish_reason": "stop",
            "usage": {"output": 12},
        },
        PolicyDecision,
        requested_model="deepseek-v4.1-flash:cloud",
        max_output=1024,
        channel="content",
    )
    assert result["structure_error"] == "schema_validation"
    assert result["content"] == content
    assert result["json_keys"] == ["answer"]
    assert result["parsed"] is None


def test_strict_schema_rejects_extra_and_coercion() -> None:
    extra = {
        "needs": [],
        "actions": [],
        "stop": {
            "conclusion": "ANSWERED",
            "answer_complete": "true",
            "claims": [],
            "note": "extra",
        },
    }
    result = classify_output(
        {
            "model": "deepseek-v4.1-flash:cloud",
            "content": json.dumps(extra),
            "content_present": True,
            "thinking_present": False,
            "finish_reason": "stop",
            "usage": {"output": 20},
        },
        PolicyDecision,
        requested_model="deepseek-v4.1-flash:cloud",
        max_output=1024,
        channel="content",
    )
    assert result["structure_error"] == "schema_validation"
