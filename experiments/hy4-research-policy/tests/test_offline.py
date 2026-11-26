import ast
import json
from decimal import Decimal
from pathlib import Path

import pytest

from hy4_research_policy.estimate import estimate_matrix
from hy4_research_policy.freeze import load_corpus, load_protocol, load_questions, question_by_id
from hy4_research_policy.live import LiveDispatchRefused, credential_status, refuse_live_dispatch
from hy4_research_policy.score import score_trace

EXPERIMENT = Path(__file__).resolve().parents[1]
REPO = EXPERIMENT.parents[1]


def _trace(name: str) -> dict:
    return json.loads((EXPERIMENT / "fixtures" / name).read_text(encoding="utf-8"))


def _score(name: str) -> dict:
    protocol = load_protocol()
    questions = question_by_id(load_questions(protocol))
    trace = _trace(name)
    return score_trace(trace, questions[trace["question_id"]], load_corpus(protocol), protocol)


def test_frozen_questions_and_corpus_match_the_gym_manifests() -> None:
    protocol = load_protocol()
    questions = load_questions(protocol)
    corpus = load_corpus(protocol)
    assert [item["question_id"] for item in questions] == protocol["frozen"]["question_ids"]
    assert set(corpus) == {
        "policy-p17",
        "orbit-approval",
        "orbit-audit",
        "northstar-security",
        "release-handbook",
        "helios-overview",
    }


def test_arms_share_one_budget_and_the_frozen_tool_menu() -> None:
    protocol = load_protocol()
    raw = (EXPERIMENT / "config" / "protocol.json").read_text(encoding="utf-8").lower()
    assert "tencent" not in raw
    assert "tokenhub" not in raw
    assert [arm["id"] for arm in protocol["arms"]] == ["hy4", "hy4-flash", "deepseek-flash"]
    hy4 = protocol["arms"][0]
    assert hy4["provider"] == "ollama-cloud"
    assert hy4["api_model"] == "hy4"
    assert hy4["cli_model"] == "hy4:cloud"
    assert hy4["available"] is True
    assert protocol["arms"][1]["available"] is False
    assert protocol["arms"][1]["model"] is None
    assert protocol["budget"]["same_for_every_arm"] is True
    assert protocol["budget"]["per_question_allocated_milli"] == 50_000
    assert protocol["budget"]["suite_allocated_milli"] == 250_000
    assert protocol["budget"]["usd_cap"] == "2"
    assert protocol["budget"]["lexical_baseline_spent_milli"] == 29_250
    assert [tool["id"] for tool in protocol["tools"]] == ["retrieve", "read_passage"]
    assert protocol["evaluator"]["live_model"] == "jev-1.13.0"
    assert protocol["evaluator"]["offline_method"] == "exact-quote-v1"
    assert "budget" not in protocol["arms"][0]


def test_estimate_is_deterministic_and_below_the_existing_usd_cap() -> None:
    estimate = estimate_matrix(load_protocol())
    assert estimate["billable_arms"] == ["hy4", "deepseek-flash"]
    assert estimate["unavailable_arms"] == ["hy4-flash"]
    assert estimate["approval_usd"] == "2.7584"
    assert estimate["low_usd"] == "0.98192"
    assert estimate["jev_usd"] == "0.02688"
    assert estimate["sweycoins_milli_per_question"] == 11_750
    assert estimate["sweycoins_milli_per_arm"] == 58_750
    assert estimate["within_existing_usd_cap"] is False
    assert estimate["approval_required"] is True
    assert Decimal(estimate["approval_usd"]) > Decimal(estimate["usd_cap"])


def test_complete_trace_covers_the_expected_quote() -> None:
    scored = _score("complete_direct.json")
    assert scored["evidence_recall"] == 1
    assert scored["evidence_precision"] == 1
    assert scored["unsupported_claims"] == 0
    assert scored["decomposition_recall"] == 1
    assert scored["next_action_errors"] == 0
    assert scored["stop_correct"] is True
    assert scored["false_completeness_claim"] is False
    assert scored["tool_calls"] == 2
    assert scored["sweycoins_milli"] == 4_250
    assert scored["over_sweycoin_budget"] is False
    assert scored["latency_ms"] == 7


def test_fabricated_retention_claim_is_unsupported() -> None:
    scored = _score("unsupported_stop.json")
    assert scored["unsupported_claims"] == 2
    assert scored["decomposition_recall"] == 0
    assert scored["stop_correct"] is False
    assert scored["false_completeness_claim"] is True
    assert scored["evidence_precision"] == 1
    assert scored["verified_evidence"] == 0


def test_contradicted_budget_stops_without_a_single_answer() -> None:
    scored = _score("contradicted_stop.json")
    assert scored["evidence_recall"] == 1
    assert scored["evidence_precision"] == 1
    assert scored["decomposition_recall"] == 1
    assert scored["next_action_errors"] == 0
    assert scored["unsupported_claims"] == 0
    assert scored["stop_correct"] is True
    assert scored["false_completeness_claim"] is False
    assert scored["tool_calls"] == 3


def test_web_search_is_outside_the_frozen_tool_menu() -> None:
    scored = _score("unknown_tool.json")
    assert scored["unknown_tools"] == 1
    assert scored["next_action_errors"] == 1
    assert scored["tool_calls"] == 0


def test_next_action_must_name_an_open_expected_gap() -> None:
    scored = _score("ignored_gap.json")
    assert scored["next_action_errors"] == 1
    assert scored["decomposition_recall"] == 1
    assert scored["stop_correct"] is False


def test_repeated_retrieval_crosses_the_shared_sweycoin_allocation() -> None:
    protocol = load_protocol()
    question = question_by_id(load_questions(protocol))["direct_effective_date"]
    steps: list[dict] = [
        {
            "type": "decompose",
            "needs": [
                {
                    "need_id": "date",
                    "text": "effective date",
                    "target_quotes": [
                        {
                            "source_id": "policy-p17",
                            "quote": "The effective date of Policy P-17 is 2026-03-01.",
                        }
                    ],
                }
            ],
        }
    ]
    gap = {
        "source_id": "policy-p17",
        "quote": "The effective date of Policy P-17 is 2026-03-01.",
    }
    for _ in range(60):
        steps.append({"type": "select_next", "tool": "retrieve", "gap": gap})
        steps.append({"type": "tool", "tool": "retrieve"})
    trace = {"arm_id": "hy4", "question_id": question["question_id"], "steps": steps}
    scored = score_trace(trace, question, load_corpus(protocol), protocol)
    assert scored["sweycoins_milli"] == 60_500
    assert scored["over_sweycoin_budget"] is True


def test_live_dispatch_and_secret_values_stay_closed() -> None:
    with pytest.raises(LiveDispatchRefused):
        refuse_live_dispatch()
    status = credential_status(
        load_protocol(),
        {"DEEPSEEK_API_KEY": "secret-value", "OLLAMA_API_KEY": "  ", "TYPESAFE_API_KEY": "x"},
    )
    assert status == {
        "DEEPSEEK_API_KEY": "present",
        "OLLAMA_API_KEY": "absent",
        "TYPESAFE_API_KEY": "present",
    }
    assert "secret-value" not in json.dumps(status)


def test_package_has_no_network_or_engine_imports() -> None:
    banned = {"urllib", "http", "socket", "requests", "httpx", "openai", "swe", "research_engine", "app"}
    package = EXPERIMENT / "hy4_research_policy"
    offenders: list[str] = []
    for path in sorted(package.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name.split(".")[0] for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module.split(".")[0]]
            else:
                continue
            for name in names:
                if name not in banned:
                    continue
                if path.name in {"pilot.py", "native_chat.py", "jev_quote.py"} and name == "urllib":
                    continue
                offenders.append(f"{path.name}: {name}")
    assert offenders == []


def test_swe_core_does_not_contain_this_experiment() -> None:
    needles = ("hy4", "deepseek-flash", "hy4_research_policy", "Policy P-17")
    roots = [REPO / "python" / "swe", REPO / "crates"]
    hits: list[str] = []
    for root in roots:
        for path in root.rglob("*"):
            if not path.is_file() or path.suffix not in {".py", ".rs", ".toml", ".md"}:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            if any(needle in text for needle in needles):
                hits.append(str(path.relative_to(REPO)))
    assert hits == []
