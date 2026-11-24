"""Quality, coverage, and false-support scoring against frozen gold.

Scoring never feeds gold back to a worker or verifier. A combined response is
expanded into five contract-level rows so single-call and distributed modes are
directly comparable.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from microtask_swarm.contracts import (
    ContractViolation,
    Kind,
    TASK_KINDS,
    validate_output,
    verify_output,
)
from microtask_swarm.fixtures import TaskSpec
from microtask_swarm.results import TaskResult

_MISSING = object()


@dataclass(frozen=True, slots=True)
class SubScore:
    task_id: str
    item_id: str
    domain: str
    kind: Kind
    correct: bool
    answered: bool
    verified: bool
    detail: str
    gold_quotes: tuple[str, ...] = ()
    emitted_quotes: tuple[str, ...] = ()
    matched_quotes: tuple[str, ...] = ()
    unverified_quotes: tuple[str, ...] = ()
    unsupported_quotes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "item_id": self.item_id,
            "domain": self.domain,
            "contract": str(self.kind),
            "correct": self.correct,
            "answered": self.answered,
            "verified": self.verified,
            "detail": self.detail,
            "extraction": {
                "gold_quotes": len(self.gold_quotes),
                "emitted_quotes": len(self.emitted_quotes),
                "matched_quotes": len(self.matched_quotes),
                "unverified_quotes": len(self.unverified_quotes),
                "unsupported_quotes": len(self.unsupported_quotes),
            },
        }


def score_suite(
    specs: Sequence[TaskSpec],
    results: Mapping[str, TaskResult],
) -> dict[str, Any]:
    sub_scores: list[SubScore] = []
    for spec in specs:
        result = results.get(spec.task_id)
        if spec.kind is Kind.COMBINED:
            sub_scores.extend(_score_combined(spec, result))
        else:
            sub_scores.append(_score_one(spec, result))
    return _aggregate(sub_scores)


def _score_combined(
    spec: TaskSpec, result: TaskResult | None
) -> list[SubScore]:
    output = result.output if result is not None else None
    scores: list[SubScore] = []
    for kind, key in (
        (Kind.RELEVANCE, "relevance"),
        (Kind.ENTAILMENT, "entailment"),
        (Kind.ATOMICITY, "atomicity"),
        (Kind.EXTRACTION, "extraction"),
        (Kind.OPERATION, "operation"),
    ):
        sub_output = output.get(key) if isinstance(output, Mapping) else None
        sub_spec = TaskSpec(
            task_id=f"{spec.task_id}:{key}",
            item_id=spec.item_id,
            domain=spec.domain,
            kind=kind,
            input_data=spec.input_data,
            gold=spec.gold[key] if isinstance(spec.gold, Mapping) else {},
            passage=spec.passage,
            question=spec.question,
        )
        scores.append(_score_one(sub_spec, result, output_override=sub_output))
    return scores


def _score_one(
    spec: TaskSpec,
    result: TaskResult | None,
    *,
    output_override: Any = _MISSING,
) -> SubScore:
    if result is None:
        return _unanswered(spec, "missing result")
    output = result.output if output_override is _MISSING else output_override
    if result.error is not None and output is None:
        return _unanswered(spec, f"call error: {result.error}")
    if output is None:
        return _unanswered(spec, "no output")

    if spec.kind is Kind.EXTRACTION:
        return _score_extraction(spec, output)
    try:
        validate_output(spec.kind, output)
    except ContractViolation as exc:
        return SubScore(
            task_id=spec.task_id,
            item_id=spec.item_id,
            domain=spec.domain,
            kind=spec.kind,
            correct=False,
            answered=False,
            verified=False,
            detail=f"contract violation: {exc.path or 'output'}: {exc}",
        )

    if spec.kind is Kind.RELEVANCE:
        expected = bool(spec.gold["relevant"])
        actual = bool(output["relevant"])
    elif spec.kind is Kind.ENTAILMENT:
        expected = str(spec.gold["label"])
        actual = str(output["label"])
    elif spec.kind is Kind.ATOMICITY:
        expected = bool(spec.gold["atomic"])
        actual = bool(output["atomic"])
    elif spec.kind is Kind.OPERATION:
        expected = str(spec.gold["operation"])
        actual = str(output["operation"])
    else:
        return _unanswered(spec, f"unsupported contract {spec.kind}")
    return SubScore(
        task_id=spec.task_id,
        item_id=spec.item_id,
        domain=spec.domain,
        kind=spec.kind,
        correct=actual == expected,
        answered=True,
        verified=True,
        detail=f"expected={expected!r} actual={actual!r}",
    )


def _score_extraction(spec: TaskSpec, output: Any) -> SubScore:
    gold_quotes = tuple(str(quote) for quote in spec.gold["quotes"])
    try:
        validate_output(Kind.EXTRACTION, output)
    except ContractViolation as exc:
        return SubScore(
            task_id=spec.task_id,
            item_id=spec.item_id,
            domain=spec.domain,
            kind=spec.kind,
            correct=False,
            answered=False,
            verified=False,
            detail=f"contract violation: {exc.path or 'output'}: {exc}",
            gold_quotes=gold_quotes,
        )
    emitted = tuple(str(quote) for quote in output["quotes"])
    verified_set = {quote for quote in emitted if quote in spec.passage}
    gold_set = set(gold_quotes)
    matched = tuple(quote for quote in emitted if quote in gold_set)
    unverified = tuple(quote for quote in emitted if quote not in spec.passage)
    unsupported = tuple(
        quote
        for quote in emitted
        if quote in spec.passage and quote not in gold_set
    )
    correct = set(emitted) == gold_set and not unverified
    return SubScore(
        task_id=spec.task_id,
        item_id=spec.item_id,
        domain=spec.domain,
        kind=spec.kind,
        correct=correct,
        answered=bool(emitted),
        verified=not unverified,
        detail=(
            f"gold={len(gold_quotes)} emitted={len(emitted)} "
            f"matched={len(matched)} unverified={len(unverified)} "
            f"unsupported={len(unsupported)}"
        ),
        gold_quotes=gold_quotes,
        emitted_quotes=emitted,
        matched_quotes=matched,
        unverified_quotes=unverified,
        unsupported_quotes=unsupported,
    )


def _unanswered(spec: TaskSpec, detail: str) -> SubScore:
    return SubScore(
        task_id=spec.task_id,
        item_id=spec.item_id,
        domain=spec.domain,
        kind=spec.kind,
        correct=False,
        answered=False,
        verified=False,
        detail=detail,
        gold_quotes=tuple(str(q) for q in spec.gold.get("quotes", ())),
    )


def _aggregate(scores: Sequence[SubScore]) -> dict[str, Any]:
    by_contract: dict[str, dict[str, Any]] = {}
    for kind in TASK_KINDS:
        rows = [row for row in scores if row.kind is kind]
        by_contract[str(kind)] = _summarize(rows)

    extraction_rows = [row for row in scores if row.kind is Kind.EXTRACTION]
    extraction = _extraction_summary(extraction_rows)
    total = len(scores)
    correct = sum(1 for row in scores if row.correct)
    answered = sum(1 for row in scores if row.answered)
    verified = sum(1 for row in scores if row.verified)
    return {
        "scored_subtasks": total,
        "correct": correct,
        "accuracy": _ratio(correct, total),
        "answered": answered,
        "answered_rate": _ratio(answered, total),
        "verified": verified,
        "verified_rate": _ratio(verified, total),
        "accuracy_on_verified": _ratio(
            sum(1 for row in scores if row.verified and row.correct),
            verified,
        ),
        "by_contract": by_contract,
        "extraction": extraction,
        "false_support": {
            "emitted_quotes": extraction["emitted_quotes"],
            "unverified_quotes": extraction["unverified_quotes"],
            "unsupported_quotes": extraction["unsupported_quotes"],
            "false_support_count": extraction["false_support_count"],
            "false_support_rate": extraction["false_support_rate"],
        },
        "rows": [row.to_dict() for row in scores],
    }


def _summarize(rows: Sequence[SubScore]) -> dict[str, Any]:
    total = len(rows)
    correct = sum(1 for row in rows if row.correct)
    answered = sum(1 for row in rows if row.answered)
    verified = sum(1 for row in rows if row.verified)
    summary = {
        "tasks": total,
        "correct": correct,
        "accuracy": _ratio(correct, total),
        "answered": answered,
        "answered_rate": _ratio(answered, total),
        "verified": verified,
        "verified_rate": _ratio(verified, total),
        "accuracy_on_verified": _ratio(
            sum(1 for row in rows if row.verified and row.correct),
            verified,
        ),
    }
    if rows and rows[0].kind is Kind.EXTRACTION:
        summary.update(_extraction_summary(rows))
    return summary


def _extraction_summary(rows: Sequence[SubScore]) -> dict[str, Any]:
    gold = sum(len(row.gold_quotes) for row in rows)
    emitted = sum(len(row.emitted_quotes) for row in rows)
    matched = sum(len(row.matched_quotes) for row in rows)
    unverified = sum(len(row.unverified_quotes) for row in rows)
    unsupported = sum(len(row.unsupported_quotes) for row in rows)
    verified = emitted - unverified
    exact_rows = sum(1 for row in rows if row.correct)
    return {
        "gold_quotes": gold,
        "emitted_quotes": emitted,
        "verified_quotes": verified,
        "matched_quotes": matched,
        "unverified_quotes": unverified,
        "unsupported_quotes": unsupported,
        "false_support_count": unverified + unsupported,
        "false_support_rate": _ratio(unverified + unsupported, emitted),
        "coverage": _ratio(matched, gold),
        "quote_precision": _ratio(matched, emitted),
        "exact_match_tasks": exact_rows,
        "exact_match_rate": _ratio(exact_rows, len(rows)),
    }


def _ratio(numerator: int, denominator: int) -> float:
    if denominator <= 0:
        return 0.0
    return round(numerator / denominator, 6)


__all__ = ["SubScore", "score_suite"]
