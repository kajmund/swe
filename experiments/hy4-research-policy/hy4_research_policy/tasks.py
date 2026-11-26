"""Isolated policy tasks. Semantic scores run only after a schema validates."""

from __future__ import annotations

import json
from typing import Any

from hy4_research_policy.schemas import Decomposition, EvidenceGap, NextAction, StopDecision
from hy4_research_policy.score import norm

TASK_SCHEMAS = {
    "decomposition": Decomposition,
    "next_action": NextAction,
    "evidence_gap": EvidenceGap,
    "stop": StopDecision,
}


def task_prompt(task: str, question: dict[str, Any], corpus: dict[str, str]) -> str:
    documents = [
        {"source_id": source_id, "text": text} for source_id, text in sorted(corpus.items())
    ]
    payload = {
        "task": task,
        "question_id": question["question_id"],
        "question": question["text"],
        "corpus": documents,
        "instructions": _INSTRUCTIONS[task],
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


_INSTRUCTIONS = {
    "decomposition": "Name every exact corpus quote the question needs. Copy quotes verbatim.",
    "next_action": "Choose one next tool. Use stop and a null gap when the corpus does not contain the answer.",
    "evidence_gap": "Set corpus_contains_answer from the corpus alone. unresolved lists exact quotes still required.",
    "stop": "Stop with ANSWERED, CONTRADICTED, or INSUFFICIENT_EVIDENCE. Claims must be exact quotes.",
}


def _pair(source_id: str, quote: str) -> tuple[str, str]:
    return (source_id, norm(quote))


def _expected(question: dict[str, Any]) -> set[tuple[str, str]]:
    return {_pair(item["source_id"], item["quote"]) for item in question.get("expected_evidence", [])}


def _in_document(corpus: dict[str, str], source_id: str, quote: str) -> bool:
    text = corpus.get(source_id)
    return text is not None and quote in text


def _coverage(quotes: list[dict[str, Any]], question: dict[str, Any], corpus: dict[str, str]) -> dict[str, Any]:
    expected = _expected(question)
    verified: set[tuple[str, str]] = set()
    unsupported = 0
    for item in quotes:
        source_id = str(item.get("source_id", ""))
        quote = str(item.get("quote", ""))
        if _in_document(corpus, source_id, quote):
            verified.add(_pair(source_id, quote))
        else:
            unsupported += 1
    matched = expected & verified
    recall = 1.0 if not expected else len(matched) / len(expected)
    precision = 1.0 if not verified else len(matched) / len(verified)
    return {
        "recall": recall,
        "precision": precision,
        "unsupported": unsupported,
        "verified": len(matched),
        "expected": len(expected),
    }


def score_exact(task: str, parsed: dict[str, Any], question: dict[str, Any], corpus: dict[str, str]) -> dict[str, Any]:
    if task == "decomposition":
        coverage = _coverage(parsed["target_quotes"], question, corpus)
        return {"exact_quote": coverage}
    if task == "next_action":
        expected = _expected(question)
        gap = parsed.get("gap")
        gap_ok = False
        unsupported = 0
        if not expected:
            gap_ok = parsed["tool"] == "stop" and gap is None
        elif isinstance(gap, dict) and parsed["tool"] in {"retrieve", "read_passage"}:
            gap_ok = _in_document(corpus, gap["source_id"], gap["quote"]) and _pair(
                gap["source_id"], gap["quote"]
            ) in expected
            if not _in_document(corpus, gap["source_id"], gap["quote"]):
                unsupported = 1
        return {"exact_quote": {"correct": gap_ok, "unsupported": unsupported, "tool": parsed["tool"]}}
    if task == "evidence_gap":
        should_contain = question["expected_conclusion"] != "INSUFFICIENT_EVIDENCE"
        coverage = _coverage(parsed["unresolved"], question, corpus)
        coverage["contains_answer_correct"] = parsed["corpus_contains_answer"] is should_contain
        return {"exact_quote": coverage}
    if task == "stop":
        coverage = _coverage(parsed["claims"], question, corpus)
        conclusion_ok = parsed["conclusion"] == question["expected_conclusion"]
        false_completeness = bool(parsed["answer_complete"]) and (
            parsed["conclusion"] != "ANSWERED" or not conclusion_ok or coverage["recall"] < 1
        )
        coverage["conclusion_correct"] = conclusion_ok
        coverage["false_completeness"] = false_completeness
        coverage["stop_correct"] = conclusion_ok and not false_completeness
        return {"exact_quote": coverage}
    raise ValueError(f"unknown task {task}")
