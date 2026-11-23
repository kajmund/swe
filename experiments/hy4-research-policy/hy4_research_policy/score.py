"""Score one recorded policy trace against the frozen Gym facit.

Verified evidence is a quote returned by ``read_passage`` that occurs verbatim
in the cited corpus document. The facit decides coverage and the expected
conclusion. A claim the trace did not read, or that is not in the cited
document, is unsupported.
"""

from __future__ import annotations

from typing import Any

from hy4_research_policy.cost import account_question

Pair = tuple[str, str]


def norm(text: str) -> str:
    return " ".join(text.split()).casefold()


def _pair(item: dict[str, Any]) -> Pair:
    return (str(item["source_id"]), norm(str(item["quote"])))


def _quote_in_document(corpus: dict[str, str], item: dict[str, Any]) -> bool:
    content = corpus.get(str(item["source_id"]))
    return content is not None and str(item["quote"]) in content


def _tariffs(protocol: dict[str, Any]) -> tuple[dict[str, int], dict[str, int]]:
    tools = {item["id"]: int(item["tariff_milli"]) for item in protocol["tools"]}
    decisions = {item["kind"]: int(item["tariff_milli"]) for item in protocol["decisions"]}
    return tools, decisions


def score_trace(
    trace: dict[str, Any],
    question: dict[str, Any],
    corpus: dict[str, str],
    protocol: dict[str, Any],
) -> dict[str, Any]:
    if trace.get("question_id") != question["question_id"]:
        raise ValueError("trace question_id does not match the facit question")
    arm_ids = {arm["id"] for arm in protocol["arms"]}
    if trace.get("arm_id") not in arm_ids:
        raise ValueError(f"unknown arm {trace.get('arm_id')!r}")
    steps = trace.get("steps")
    if not isinstance(steps, list) or not steps:
        raise ValueError("trace steps must be a non-empty list")

    tools, decisions = _tariffs(protocol)
    expected = {_pair(item) for item in question.get("expected_evidence", [])}
    verified: set[Pair] = set()
    named_targets: set[Pair] = set()
    unsupported = 0
    bad_targets = 0
    next_errors = 0
    unknown_tools = 0
    tool_calls = 0
    spent = 0
    latency = 0.0
    saw_decompose = False
    stop: dict[str, Any] | None = None

    for index, step in enumerate(steps):
        if not isinstance(step, dict) or "type" not in step:
            raise ValueError(f"step {index} is missing type")
        kind = step["type"]
        latency += float(step.get("latency_ms") or 0)
        if stop is not None:
            next_errors += 1
            continue
        if kind == "decompose":
            if index != 0 or saw_decompose:
                next_errors += 1
            saw_decompose = True
            spent += decisions["decompose"]
            for need in step.get("needs") or []:
                for target in need.get("target_quotes") or []:
                    if _quote_in_document(corpus, target):
                        named_targets.add(_pair(target))
                    else:
                        bad_targets += 1
                        unsupported += 1
        elif kind == "select_next":
            tool = str(step.get("tool") or "")
            if tool not in tools:
                unknown_tools += 1
                next_errors += 1
                continue
            open_gaps = expected - verified
            gap = step.get("gap")
            if open_gaps and (not isinstance(gap, dict) or _pair(gap) not in open_gaps):
                next_errors += 1
        elif kind == "tool":
            tool = str(step.get("tool") or "")
            tool_calls += 1
            if tool not in tools:
                unknown_tools += 1
                continue
            spent += tools[tool]
            quote = step.get("quote")
            if tool == "read_passage":
                if not isinstance(quote, dict):
                    raise ValueError("read_passage requires a quote")
                if _quote_in_document(corpus, quote):
                    verified.add(_pair(quote))
                else:
                    unsupported += 1
        elif kind == "stop":
            stop = step
            spent += decisions["stop"]
            for claim in step.get("claims") or []:
                claim_pair = _pair(claim)
                if not _quote_in_document(corpus, claim) or claim_pair not in verified:
                    unsupported += 1
        else:
            raise ValueError(f"unknown step type {kind!r}")

    matched = expected & verified
    recall = len(matched) / len(expected) if expected else 1.0
    precision = len(matched) / len(verified) if verified else 1.0
    if expected:
        decomposition_recall = len(named_targets & expected) / len(expected)
    else:
        decomposition_recall = 0.0 if bad_targets else 1.0
    unresolved = len(expected - verified)
    conclusion = str(stop.get("conclusion")) if stop else ""
    answer_complete = bool(stop and stop.get("answer_complete"))
    expected_conclusion = str(question["expected_conclusion"])
    false_completeness = bool(stop) and (
        (answer_complete and unresolved > 0)
        or (conclusion == "ANSWERED" and expected_conclusion != "ANSWERED")
        or (answer_complete and conclusion != "ANSWERED")
    )
    allocated = int(protocol["budget"]["per_question_allocated_milli"])
    scored = {
        "question_id": question["question_id"],
        "arm_id": trace["arm_id"],
        "evidence_recall": recall,
        "evidence_precision": precision,
        "verified_evidence": len(matched),
        "expected_evidence": len(expected),
        "unsupported_claims": unsupported,
        "decomposition_present": saw_decompose and steps[0]["type"] == "decompose",
        "decomposition_recall": decomposition_recall,
        "next_action_errors": next_errors,
        "stop_correct": bool(stop) and conclusion == expected_conclusion and not false_completeness,
        "false_completeness_claim": false_completeness,
        "tool_calls": tool_calls,
        "unknown_tools": unknown_tools,
        "sweycoins_milli": spent,
        "over_sweycoin_budget": spent > allocated,
        "latency_ms": latency,
    }
    scored["cost"] = account_question(trace, scored, protocol)
    return scored
