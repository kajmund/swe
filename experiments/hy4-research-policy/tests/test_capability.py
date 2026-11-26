import json

import pytest

from hy4_research_policy.adapter import NativeOutputRefused, run_structured
from hy4_research_policy.capability import OPENAI_BASE_URL, output_modes, routes_to_cloud
from hy4_research_policy.freeze import load_corpus, load_protocol, load_questions
from hy4_research_policy.pilot import build_prompt, input_token_bound
from hy4_research_policy.plan import diagnosis_plan
from hy4_research_policy.schemas import Decomposition
from hy4_research_policy.tasks import TASK_SCHEMAS, task_prompt


def test_colon_cloud_on_the_local_daemon_refuses_native_schema() -> None:
    route = routes_to_cloud("deepseek-v4.1-flash:cloud", OPENAI_BASE_URL)
    assert route["cloud"] is True
    assert "colon-cloud" in route["signals"]
    modes = output_modes("deepseek-v4.1-flash:cloud", OPENAI_BASE_URL)
    assert modes["native_structured"] == "refused_before_http"
    assert modes["tool"] == "supported"
    assert modes["prompt_json"] == "supported"
    with pytest.raises(NativeOutputRefused):
        run_structured(
            model_name="deepseek-v4.1-flash:cloud",
            prompt="{}",
            schema=Decomposition,
            mode="native",
            max_output=32,
            think=False,
        )


def test_hyphen_cloud_and_ollama_host_are_cloud_too() -> None:
    assert routes_to_cloud("gpt-oss-cloud", "http://127.0.0.1:11434/v1")["signals"] == ["hyphen-cloud"]
    assert "ollama-host" in routes_to_cloud("hy4", "https://ollama.com/v1")["signals"]


def test_diagnosis_plan_fits_under_its_own_two_dollar_cap() -> None:
    protocol = load_protocol()
    corpus = load_corpus(protocol)
    questions = load_questions(protocol)
    replay = max(input_token_bound(build_prompt(question, corpus)) for question in questions) + 2000
    task_bound = 0
    for task, schema in TASK_SCHEMAS.items():
        suffix = json.dumps(schema.model_json_schema())
        task_bound = max(
            task_bound,
            max(input_token_bound(task_prompt(task, question, corpus) + suffix) + 2000 for question in questions),
        )
    plan = diagnosis_plan(protocol, replay_input_tokens=replay, task_input_tokens=task_bound)
    assert plan["retries"] == 0
    assert plan["fits"] is True
    assert plan["usd_cap"] == "2"
