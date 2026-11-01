"""The Python adapter must not decide what the engine decides.

B2 turns needs, evidence and operations into engine semantics. The Python
projection may convert and validate, but it must never implement a competing
decision: a need becomes RESOLVED, evidence becomes AMBIGUOUS or a conclusion
becomes INSUFFICIENT_EVIDENCE only because the engine said so.

This test fails if the adapter starts assigning those states itself.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ENGINE_DIR = Path(__file__).resolve().parents[1] / "swe"

#: State names that only the engine may produce.
ENGINE_OWNED_STATES = frozenset(
    {"RESOLVED", "AMBIGUOUS", "INSUFFICIENT_EVIDENCE", "VERIFIED", "REJECTED"}
)

#: The adapter module that is allowed to *name* those states, because it reads
#: and validates what the engine returned. It may not assign them as a result.
PROJECTION_MODULE = "needs.py"


def _is_vocabulary_definition(node: ast.AST) -> bool:
    """A frozenset/Literal of allowed spellings is validation, not a decision."""
    if not isinstance(node, ast.Call | ast.Subscript):
        return False
    text = ast.unparse(node)
    return text.startswith("frozenset(") or "Literal[" in text


def _state_literals(path: Path) -> list[str]:
    """Every use of an engine-owned state name outside vocabulary definitions.

    Covers assignments, returns, conditional expressions, asserts, walrus
    operators and comprehensions — everything an adapter could use to smuggle a
    decision past a check that only looks at `Assign`/`Return`.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    allowed_lines: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign | ast.AnnAssign) and node.value is not None:
            if _is_vocabulary_definition(node.value):
                allowed_lines.update(range(node.lineno, (node.end_lineno or node.lineno) + 1))
    offenders: list[str] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Constant) and node.value in ENGINE_OWNED_STATES):
            continue
        line = node.lineno
        if line in allowed_lines:
            continue
        offenders.append(f"{path.name}:{line}: {node.value!r} used here")
    return offenders


def _mentions_state(node: ast.AST) -> bool:
    for child in ast.walk(node):
        if isinstance(child, ast.Constant) and child.value in ENGINE_OWNED_STATES:
            return True
    return False


def _render(node: ast.AST) -> str:
    return ast.unparse(node)


def test_adapter_does_not_assign_engine_owned_states() -> None:
    offenders: list[str] = []
    for path in sorted(ENGINE_DIR.glob("*.py")):
        if path.name == PROJECTION_MODULE:
            # Allowed to validate incoming states, but still may not compute one.
            offenders.extend(_state_literals(path))
            continue
        offenders.extend(_state_literals(path))
    assert offenders == [], (
        "only the engine may decide need/evidence/conclusion state; "
        f"the adapter must read it: {offenders}"
    )


def test_projection_module_has_no_branching_on_domain_values() -> None:
    """The projection validates vocabulary; it does not reason about evidence."""
    path = ENGINE_DIR / PROJECTION_MODULE
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.If):
            text = ast.unparse(node.test)
            for state in ENGINE_OWNED_STATES:
                assert state not in text, (
                    f"{path.name}:{node.lineno}: branching on engine-owned state {state}"
                )


def test_unknown_schema_version_is_rejected() -> None:
    from swe.needs import ProjectionError, read_need_state

    with pytest.raises(ProjectionError, match="schema_version"):
        read_need_state(
            {"schema_version": 99, "fact": "f", "subject": "s", "status": "RESOLVED"}
        )


def test_unknown_need_status_is_rejected() -> None:
    from swe.needs import ProjectionError, read_need_state

    with pytest.raises(ProjectionError, match="unknown need status"):
        read_need_state(
            {"schema_version": 1, "fact": "f", "subject": "s", "status": "PROBABLY"}
        )


def test_unknown_evidence_status_is_rejected() -> None:
    from swe.needs import ProjectionError, read_evidence

    with pytest.raises(ProjectionError, match="unknown evidence status"):
        read_evidence({"status": "MAYBE"})


def test_insufficient_evidence_is_reported_not_overridden() -> None:
    from swe.needs import read_operation_outcome

    outcome = read_operation_outcome(
        {
            "schema_version": 1,
            "conclusion": "INSUFFICIENT_EVIDENCE",
            "detail": "gate missing one input",
            "missing": [{"fact": "f", "subject": "s", "status": "UNRESOLVED"}],
        }
    )
    assert outcome["conclusion"] == "INSUFFICIENT_EVIDENCE"
    assert outcome["answer"] is None


def test_projection_rejects_unknown_priority_and_cardinality() -> None:
    from swe.needs import ProjectionError, project_need

    with pytest.raises(ProjectionError, match="priority"):
        project_need(fact="f", subject="s", priority="CRITICAL")
    with pytest.raises(ProjectionError, match="cardinality"):
        project_need(fact="f", subject="s", cardinality="MANY")


def test_projection_never_invents_provenance() -> None:
    from swe.needs import (
        EvidenceJudgement,
        project_evidence_assessment,
        project_provenance,
    )

    without = project_evidence_assessment(
        EvidenceJudgement(fact="f", subject="s", admissibility="Admitted")
    )
    assert without["provenance"] is None
    with_source = project_evidence_assessment(
        EvidenceJudgement(
            fact="f",
            subject="s",
            admissibility="Admitted",
            provenance=project_provenance(source_id="doc-1"),
        )
    )
    assert with_source["provenance"] == {"source_id": "doc-1"}
