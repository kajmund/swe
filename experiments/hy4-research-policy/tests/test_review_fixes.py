import json
from decimal import Decimal
from types import SimpleNamespace

from hy4_research_policy.adapter import _observation
from hy4_research_policy.diagnose import Ledger, _record_native, _record_pydantic
from hy4_research_policy.freeze import load_protocol
from hy4_research_policy.pilot import _steps_from_decision, build_prompt
from hy4_research_policy.schemas import PolicyDecision


def _arm() -> dict:
    protocol = load_protocol()
    return next(arm for arm in protocol["arms"] if arm["id"] == "deepseek-flash")


def test_observation_keeps_the_model_response_only() -> None:
    request = SimpleNamespace(
        kind="request",
        parts=[SimpleNamespace(content='{"needs": [{"target_quotes": []}]}')],
    )
    response = SimpleNamespace(kind="response", parts=[SimpleNamespace(content='{"needs": []}')])
    observed = _observation([request, response], SimpleNamespace())
    assert observed["content"] == '{"needs": []}'


def test_prompt_example_matches_the_decision_schema() -> None:
    prompt = build_prompt({"question_id": "q", "text": "When?"}, {"policy-p17": "text"})
    example = json.loads(prompt)["response_schema"]
    parsed = PolicyDecision.model_validate(example)
    assert parsed.needs[0].source_id == "document id"
    flat = _steps_from_decision(
        {"needs": [{"source_id": "policy-p17", "quote": "text"}], "actions": [], "stop": {}}
    )
    nested = _steps_from_decision(
        {
            "needs": [{"target_quotes": [{"source_id": "policy-p17", "quote": "text"}]}],
            "actions": [],
            "stop": {},
        }
    )
    assert flat[0]["needs"] == nested[0]["needs"]


def _cached(tmp_path, name: str, output: int) -> None:
    payload = {
        "name": name,
        "hidden_retry": False,
        "classification": {"usage": {"requests": 1, "output": output}},
    }
    (tmp_path / f"{name}.json").write_text(json.dumps(payload), encoding="utf-8")


def test_cached_pydantic_over_cap_restores_the_stop(tmp_path) -> None:
    _cached(tmp_path, "pydantic-prompt", 2264)
    ledger = Ledger(Decimal("2"))
    record = _record_pydantic(
        name="pydantic-prompt",
        mode="prompt",
        model="deepseek-v4.1-flash:cloud",
        prompt="{}",
        schema=PolicyDecision,
        max_output=1024,
        think=False,
        period="off_peak",
        arm=_arm(),
        ledger=ledger,
        output_dir=tmp_path,
        question={"question_id": "q", "text": "When?"},
    )
    assert record["classification"]["usage"]["output"] == 2264
    assert ledger.reason == "pydantic sent more than one request or output exceeded max_tokens"


def test_cached_native_over_cap_restores_the_stop(tmp_path) -> None:
    _cached(tmp_path, "direct-prompt", 2048)
    ledger = Ledger(Decimal("2"))
    record = _record_native(
        name="direct-prompt",
        arm_id="deepseek-flash",
        model="deepseek-v4.1-flash:cloud",
        prompt="{}",
        body={},
        schema=PolicyDecision,
        channel="content",
        max_output=1024,
        period="off_peak",
        arm=_arm(),
        ledger=ledger,
        output_dir=tmp_path,
    )
    assert record["classification"]["usage"]["output"] == 2048
    assert ledger.reason == "provider output exceeded num_predict or a second request was observed"
