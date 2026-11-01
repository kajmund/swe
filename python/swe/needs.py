"""Projection between domain objects and the engine's generic core.

This module is deliberately thin. It converts domain data into the engine's
versioned wire shape, and reads back states the engine computed. It contains no
decisions: it never decides that a need is resolved, that evidence is ambiguous
or that a conclusion is insufficient. Those transitions belong to
`crates/swe-native` (see ``docs/swe-standalone/09-contract-decision.md`` §2).

Each payload carries ``schema_version``. An unknown version is a typed error,
never a silent interpretation.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

NEED_SPEC_VERSION = 1
EVIDENCE_ASSESSMENT_VERSION = 1
NEED_STATE_VERSION = 1
OPERATION_OUTCOME_VERSION = 1

PRIORITIES = frozenset({"REQUIRED", "SUPPORTING", "EXPLORATORY"})
CARDINALITIES = frozenset({"SINGLE", "MULTIPLE", "UNKNOWN"})
ADMISSIBILITIES = frozenset({"Admitted", "NotAdmitted", "Uncertain"})
NEED_STATUSES = frozenset({"UNRESOLVED", "RESOLVED", "AMBIGUOUS"})
CONCLUSIONS = frozenset({"answered", "INSUFFICIENT_EVIDENCE"})
GATE_CONCLUSIONS = frozenset({"ready", "INSUFFICIENT_EVIDENCE"})
EVIDENCE_STATUSES = frozenset({"VERIFIED", "AMBIGUOUS", "REJECTED"})


class ProjectionError(ValueError):
    """A domain value cannot be projected, or the engine returned an unknown shape."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ProjectionError(message)


def _text(value: object, field: str) -> str:
    _require(isinstance(value, str) and value != "", f"{field} must be a non-empty string")
    return str(value)


def _check_version(payload: Mapping[str, Any], expected: int, kind: str) -> None:
    version = payload.get("schema_version")
    _require(
        version == expected,
        f"unsupported {kind} schema_version {version!r}; this adapter speaks {expected}",
    )


def project_need(
    *,
    fact: str,
    subject: str,
    priority: str = "REQUIRED",
    cardinality: str = "SINGLE",
    reason: str = "",
) -> dict[str, Any]:
    """Project one domain need into the engine's wire shape.

    The domain chooses priority and cardinality; the engine applies them. This
    function only validates that the declared values are ones the engine knows.
    """
    _require(priority in PRIORITIES, f"unknown need priority {priority!r}")
    _require(cardinality in CARDINALITIES, f"unknown need cardinality {cardinality!r}")
    return {
        "schema_version": NEED_SPEC_VERSION,
        "fact": _text(fact, "fact"),
        "subject": _text(subject, "subject"),
        "priority": priority,
        "cardinality": cardinality,
        "reason": reason,
    }


def project_provenance(
    *,
    source_id: str,
    locator: str | None = None,
    content_hash: str | None = None,
    retrieved_at_ms: int | None = None,
    provider: str | None = None,
) -> dict[str, Any]:
    """Project where a fact came from. Never invented: the caller provides it."""
    provenance: dict[str, Any] = {"source_id": _text(source_id, "source_id")}
    if locator is not None:
        provenance["locator"] = locator
    if content_hash is not None:
        provenance["content_hash"] = content_hash
    if retrieved_at_ms is not None:
        _require(
            isinstance(retrieved_at_ms, int) and retrieved_at_ms >= 0,
            "retrieved_at_ms must be a non-negative integer",
        )
        provenance["retrieved_at_ms"] = retrieved_at_ms
    if provider is not None:
        provenance["provider"] = provider
    return provenance


@dataclass(frozen=True)
class EvidenceJudgement:
    """The domain's judgement about one candidate, before projection.

    Kept as a value object so the projection has one parameter per concern
    instead of a long argument list.
    """

    fact: str
    subject: str
    admissibility: str
    provenance: Mapping[str, Any] | None = None
    value: str | None = None
    equivalence_key: str | None = None
    conflict_scope: str = ""
    operational_role: str | None = None
    reason_code: str = ""
    policy_version: str = ""


def project_evidence_assessment(judgement: EvidenceJudgement) -> dict[str, Any]:
    """Project the domain's judgement about one candidate.

    The domain decides admissibility, equivalence and scope. This function
    checks the vocabulary and never derives a status from it.
    """
    _require(
        judgement.admissibility in ADMISSIBILITIES,
        f"unknown evidence admissibility {judgement.admissibility!r}",
    )
    return {
        "schema_version": EVIDENCE_ASSESSMENT_VERSION,
        "fact": _text(judgement.fact, "fact"),
        "subject": _text(judgement.subject, "subject"),
        "value": judgement.value,
        "admissibility": judgement.admissibility,
        "equivalence_key": judgement.equivalence_key,
        "conflict_scope": judgement.conflict_scope,
        "operational_role": judgement.operational_role,
        "reason_code": judgement.reason_code,
        "policy_version": judgement.policy_version,
        "provenance": (
            dict(judgement.provenance) if judgement.provenance is not None else None
        ),
    }


def read_need_state(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Read a state the engine computed. Never recomputed here."""
    _check_version(payload, NEED_STATE_VERSION, "need state")
    status = payload.get("status")
    _require(status in NEED_STATUSES, f"unknown need status {status!r}")
    return {
        "fact": _text(payload.get("fact"), "fact"),
        "subject": _text(payload.get("subject"), "subject"),
        "status": status,
        "reason": payload.get("reason", ""),
    }


def read_evidence(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Read one evidence verdict the engine computed."""
    status = payload.get("status")
    _require(status in EVIDENCE_STATUSES, f"unknown evidence status {status!r}")
    return {
        "fact": payload.get("fact"),
        "subject": payload.get("subject"),
        "value": payload.get("value"),
        "status": status,
        "source_id": payload.get("source_id"),
        "reason_code": payload.get("reason_code", ""),
    }


def read_operation_outcome(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Read the engine's operation outcome.

    ``INSUFFICIENT_EVIDENCE`` is the engine's verdict. This function reports
    it; it must never turn it into an answer or retry the decision.
    """
    _check_version(payload, OPERATION_OUTCOME_VERSION, "operation outcome")
    conclusion = payload.get("conclusion")
    _require(
        conclusion in CONCLUSIONS,
        f"unknown operation conclusion {conclusion!r}",
    )
    return {
        "conclusion": conclusion,
        "answer": payload.get("answer"),
        "detail": payload.get("detail", ""),
        "missing": list(payload.get("missing") or ()),
    }


def read_operation_gate(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Read the gate verdict the engine computed.

    ``INSUFFICIENT_EVIDENCE`` is the engine's decision. This reports it; it must
    never turn a refusal into a ready operation.
    """
    gated_version = payload.get("schema_version")
    _require(
        gated_version == OPERATION_OUTCOME_VERSION,
        f"unsupported operation gate schema_version {gated_version!r}",
    )
    conclusion = payload.get("conclusion")
    _require(
        conclusion in GATE_CONCLUSIONS,
        f"unknown gate conclusion {conclusion!r}",
    )
    return {
        "satisfied": bool(payload.get("satisfied")),
        "conclusion": conclusion,
        "detail": payload.get("detail", ""),
        "missing": list(payload.get("missing") or ()),
    }


def read_derived_needs(payload: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Read the needs the engine derived from a declaration."""
    needs: list[dict[str, Any]] = []
    for item in payload:
        needs.append(
            {
                "fact": _text(item.get("fact"), "fact"),
                "subject": _text(item.get("subject"), "subject"),
                "id": _text(item.get("id"), "id"),
                "priority": item.get("priority"),
                "cardinality": item.get("cardinality"),
            }
        )
    return needs


def project_need_states(
    states: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Read back a batch of engine-computed need states."""
    return [read_need_state(state) for state in states]


StatusLiteral = Literal["UNRESOLVED", "RESOLVED", "AMBIGUOUS"]
