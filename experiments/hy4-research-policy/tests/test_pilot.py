from decimal import Decimal

from hy4_research_policy.freeze import load_corpus, load_protocol, load_questions
from hy4_research_policy.pilot import (
    MAX_OUTPUT_TOKENS,
    build_prompt,
    input_token_bound,
    pilot_plan,
)


def test_measured_pilot_fits_under_the_two_dollar_cap() -> None:
    protocol = load_protocol()
    corpus = load_corpus(protocol)
    prompts = [build_prompt(question, corpus) for question in load_questions(protocol)]
    bound = max(input_token_bound(prompt) for prompt in prompts)
    plan = pilot_plan(protocol, input_tokens=bound, questions=len(prompts))
    assert plan["fits"] is True
    assert Decimal(plan["worst_usd"]) <= Decimal("2")
    assert plan["max_output_tokens"] == MAX_OUTPUT_TOKENS
    assert plan["verification_usd"] == "0"


def test_model_max_output_with_retries_does_not_fit() -> None:
    protocol = load_protocol()
    plan = pilot_plan(
        protocol,
        input_tokens=4000,
        questions=5,
        max_output=65536,
    )
    assert plan["fits"] is False
    assert Decimal(plan["worst_usd"]) > Decimal(plan["usd_cap"])
