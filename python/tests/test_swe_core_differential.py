"""Differential tests: Rust is the only implementation of the semantics.

B2's acceptance criteria require that the same input produces identical
outcomes whether it is evaluated by the engine directly or by the Python
projection. Each case runs through `swe_native.resolve_evidence` (the engine) and
through the adapter's reader.

Following `docs/swe-standalone/13-verification-standard.md` requirement 1, every
case carries an **explicitly stated expected verdict** (`CASES`) and both sides
are checked against that literal. Comparing the adapter's reading back to the
engine payload it was read from would be a tautology: it could never detect a
divergence, which is precisely the regression these tests exist for — one need
becoming RESOLVED in Python and INSUFFICIENT_EVIDENCE in Rust.
"""

from __future__ import annotations

from typing import Any

import pytest

swe_native = pytest.importorskip("swe_native")

NEED = {"fact": "fact_a", "subject": "subject_a"}
POLICY = {"uncertain_blocks_scope": True, "require_provenance": True}


def engine_resolve(
    assessments: list[dict[str, Any]], *, policy: dict[str, bool] = POLICY
) -> dict[str, Any]:
    """The normative path: whatever the engine says is the answer."""
    return swe_native.resolve_evidence(
        NEED,
        assessments,
        policy["uncertain_blocks_scope"],
        policy["require_provenance"],
    )


def adapter_view(result: dict[str, Any]) -> dict[str, Any]:
    """What the Python adapter reports after reading the engine's result."""
    from swe.needs import read_evidence, read_need_state

    state = read_need_state(result)
    evidence = [read_evidence(entry) for entry in result["evidence"]]
    return {"status": state["status"], "evidence": evidence}


def adjudicated(
    value: str,
    *,
    admissibility: str = "Admitted",
    key: str | None = None,
    scope: str = "scope_a",
    role: str | None = None,
    source: str | None = "source_a",
) -> dict[str, Any]:
    from swe.needs import EvidenceJudgement, project_evidence_assessment
    from swe.needs import project_provenance

    return project_evidence_assessment(
        EvidenceJudgement(
            fact=NEED["fact"],
            subject=NEED["subject"],
            admissibility=admissibility,
            value=value,
            equivalence_key=key if key is not None else value,
            conflict_scope=scope,
            operational_role=role,
            provenance=project_provenance(source_id=source) if source else None,
        )
    )


def assert_projection_matches_spec(
    assessments: list[dict[str, Any]], expected_status: str
) -> dict[str, Any]:
    """The core differential assertion.

    The expected status is stated here, separately from both implementations.
    Comparing the adapter's reading back to the engine payload it was read from
    would be a tautology (13-verification-standard.md, requirement 1): it can
    never detect a divergence. Each side is therefore checked against this
    literal, and the literals are the specification.
    """
    engine = engine_resolve(assessments)
    projected = adapter_view(engine)
    assert engine["status"] == expected_status, "the engine changed its verdict"
    assert projected["status"] == expected_status, "the adapter reports another verdict"
    assert projected["evidence"] == [
        {
            "fact": entry["fact"],
            "subject": entry["subject"],
            "value": entry["value"],
            "status": entry["status"],
            "source_id": entry["source_id"],
            "reason_code": entry["reason_code"],
        }
        for entry in engine["evidence"]
    ]
    # And the engine's verdict must be one the adapter knows how to report.
    assert projected["status"] in {"UNRESOLVED", "RESOLVED", "AMBIGUOUS"}
    return engine


# Each case states the verdict the specification requires. These literals are
# the expected behaviour; neither implementation is consulted to produce them.
CASES: dict[str, tuple[list[dict[str, Any]], str]] = {
    # One admitted, provenanced value settles the need.
    "single_verified": ([adjudicated("value_a")], "RESOLVED"),
    # A rejected candidate carries its own distinct key: it must not block the
    # accepted value, and it must not look like a "same key" non-conflict.
    "rejected_plus_verified": (
        [
            adjudicated("value_a"),
            adjudicated("value_b", admissibility="NotAdmitted", key="rejected_key"),
        ],
        "RESOLVED",
    ),
    # Two different raw values that the DOMAIN normalises to the same key.
    "equivalent_values_normalised": (
        [
            adjudicated("value_a", key="normalised_a"),
            adjudicated("VALUE A", key="normalised_a"),
        ],
        "RESOLVED",
    ),
    # Two admitted values for the same need and scope compete: ambiguous, never
    # a silent pick.
    "competing_values": (
        [adjudicated("value_a"), adjudicated("value_b")],
        "AMBIGUOUS",
    ),
    # Different operational roles are separate conclusions, not a conflict.
    "different_roles": (
        [
            adjudicated("value_a", role="role_a"),
            adjudicated("value_b", role="role_b"),
        ],
        "RESOLVED",
    ),
    # An unknown role cannot be treated as distinct, so it competes.
    "unknown_role_competes": (
        [adjudicated("value_a"), adjudicated("value_b", role="role_b")],
        "AMBIGUOUS",
    ),
    # Uncertainty blocks under this policy.
    "uncertain_alone": ([adjudicated("value_a", admissibility="Uncertain")], "AMBIGUOUS"),
    # Provenance is required by this policy, so an unprovenanced value leaves
    # the need unresolved.
    "no_provenance": ([adjudicated("value_a", source=None)], "UNRESOLVED"),
    # Values in different scopes do not compete.
    "different_scopes": (
        [
            adjudicated("value_a", scope="scope_a"),
            adjudicated("value_b", scope="scope_b"),
        ],
        "RESOLVED",
    ),
}

@pytest.mark.parametrize("case", sorted(CASES))
def test_engine_and_projection_both_match_the_specification(case: str) -> None:
    assessments, expected_status = CASES[case]
    assert_projection_matches_spec(assessments, expected_status)


def test_competing_values_never_resolve_in_either_layer() -> None:
    """The exact failure mode the owner named, on both sides of the boundary."""
    assessments, expected = CASES["competing_values"]
    engine = engine_resolve(assessments)
    assert engine["status"] == "AMBIGUOUS"
    assert all(entry["status"] == "AMBIGUOUS" for entry in engine["evidence"])
    assert adapter_view(engine)["status"] == expected


def test_rejected_does_not_block_an_accepted_value() -> None:
    assessments, expected = CASES["rejected_plus_verified"]
    engine = engine_resolve(assessments)
    assert engine["status"] == expected
    statuses = sorted(entry["status"] for entry in engine["evidence"])
    assert statuses == ["REJECTED", "VERIFIED"]
    assert adapter_view(engine)["status"] == expected


def test_unknown_role_counts_as_competing_in_both_layers() -> None:
    assessments, expected = CASES["unknown_role_competes"]
    engine = engine_resolve(assessments)
    assert engine["status"] == "AMBIGUOUS"
    assert adapter_view(engine)["status"] == expected


def test_competing_values_are_ambiguous_from_rust_to_python() -> None:
    """The exact divergence the owner named, pinned end to end.

    If the engine ever stopped reporting AMBIGUOUS here, this fails; if the
    adapter ever stopped echoing the engine, this fails too. The mutation that
    makes it fail is forcing `resolve_need` to return Resolved for competing
    values (verified manually while writing this test).
    """
    engine = engine_resolve(CASES["competing_values"][0])
    assert engine["status"] == "AMBIGUOUS"
    assert adapter_view(engine)["status"] == "AMBIGUOUS"
    assert all(entry["status"] == "AMBIGUOUS" for entry in engine["evidence"])


def test_adapter_reports_the_engine_status_it_is_given() -> None:
    """The adapter has no opinion: whatever the engine says is what is read."""
    from swe.needs import read_need_state

    engine = engine_resolve(CASES["single_verified"][0])
    assert engine["status"] == "RESOLVED"
    assert read_need_state(engine)["status"] == "RESOLVED"
    with pytest.raises(ValueError, match="unknown need status"):
        read_need_state({**engine, "status": "NOT_A_STATUS"})


def test_uncertain_policy_flag_is_honoured_across_the_boundary() -> None:
    """The domain's policy choice reaches the engine and back."""
    blocking = engine_resolve(
        CASES["uncertain_alone"][0],
        policy={"uncertain_blocks_scope": True, "require_provenance": True},
    )
    permissive = engine_resolve(
        CASES["uncertain_alone"][0],
        policy={"uncertain_blocks_scope": False, "require_provenance": True},
    )
    assert blocking["status"] == "AMBIGUOUS"
    assert permissive["status"] == "UNRESOLVED"
    assert adapter_view(blocking)["status"] == "AMBIGUOUS"
    assert adapter_view(permissive)["status"] == "UNRESOLVED"


def test_need_projection_emits_the_wire_shape() -> None:
    from swe.needs import project_need

    projected = project_need(fact="fact_a", subject="subject_a", priority="SUPPORTING")
    assert projected["priority"] == "SUPPORTING"
    assert projected["schema_version"] == 1


def test_derived_needs_match_between_engine_and_adapter() -> None:
    from swe.needs import read_derived_needs

    declared = [
        {"fact": "fact_a", "subject": "subject_a"},
        {"fact": "fact_b", "subject": "subject_a"},
        {"fact": "fact_a", "subject": "subject_b"},
    ]
    engine = swe_native.derive_needs(declared)
    read = read_derived_needs(engine)
    assert len(read) == len(declared)
    assert [need["id"] for need in read] == [
        "subject_a.fact_a",
        "subject_a.fact_b",
        "subject_b.fact_a",
    ]
    # Pin the payload fields themselves: a binding that swapped fact/subject, or
    # hard-coded the enums, would still produce the right ids above.
    assert [need["fact"] for need in read] == ["fact_a", "fact_b", "fact_a"]
    assert [need["subject"] for need in read] == [
        "subject_a",
        "subject_a",
        "subject_b",
    ]
    assert all(need["priority"] == "REQUIRED" for need in read)
    assert all(need["cardinality"] == "SINGLE" for need in read)


GATE_CASES = {
    "all_resolved": (
        [{"fact": "f", "subject": "s"}],
        [{"fact": "f", "subject": "s", "status": "RESOLVED"}],
    ),
    "one_missing": ([{"fact": "f", "subject": "s"}], []),
    "ambiguous_blocks": (
        [{"fact": "f", "subject": "s"}],
        [{"fact": "f", "subject": "s", "status": "AMBIGUOUS"}],
    ),
    "partial_of_three": (
        [
            {"fact": "f", "subject": "s1"},
            {"fact": "f", "subject": "s2"},
            {"fact": "f", "subject": "s3"},
        ],
        [{"fact": "f", "subject": "s1", "status": "RESOLVED"}],
    ),
}


@pytest.mark.parametrize("case", sorted(GATE_CASES))
def test_operation_gate_matches_between_engine_and_adapter(case: str) -> None:
    from swe.needs import read_operation_gate

    required, states = GATE_CASES[case]
    engine = swe_native.operation_gate("OP", required, states)
    read = read_operation_gate(engine)
    # The adapter must report the engine's verdict unchanged. Comparing that to
    # the engine payload again would be a tautology, so the expected values are
    # stated independently here.
    expected_ready = case == "all_resolved"
    assert read["satisfied"] is expected_ready
    assert read["conclusion"] == ("ready" if expected_ready else "INSUFFICIENT_EVIDENCE")
    assert (engine["conclusion"] == "ready") is expected_ready
    if not expected_ready:
        assert read["missing"], "a refusal must name the missing inputs"
        assert "OP" in read["detail"]
    for entry in read["missing"]:
        assert set(entry) == {"fact", "subject", "status"}


def test_the_gate_reports_the_status_that_blocked() -> None:
    """A refusal must say WHY each input is missing, not just that it is."""
    from swe.needs import read_operation_gate

    required = [{"fact": "f", "subject": "s"}]
    engine = swe_native.operation_gate(
        "OP", required, [{"fact": "f", "subject": "s", "status": "AMBIGUOUS"}]
    )
    read = read_operation_gate(engine)
    assert read["conclusion"] == "INSUFFICIENT_EVIDENCE"
    assert read["missing"][0]["status"] == "AMBIGUOUS", (
        "an ambiguous input must be reported as ambiguous, not as merely unresolved"
    )
    assert "AMBIGUOUS" in read["detail"]


def test_the_gate_can_never_report_ready_with_a_missing_input() -> None:
    """The failure mode that matters: a partial investigation must abstain."""
    from swe.needs import read_operation_gate

    required, states = GATE_CASES["partial_of_three"]
    engine = swe_native.operation_gate("OP", required, states)
    read = read_operation_gate(engine)
    assert read["conclusion"] == "INSUFFICIENT_EVIDENCE"
    assert read["satisfied"] is False
    assert len(read["missing"]) == 2


def test_the_gate_cannot_be_talked_into_ready_by_an_unknown_status() -> None:
    from swe.needs import read_operation_gate

    with pytest.raises(Exception, match="unknown need status"):
        swe_native.operation_gate(
            "OP",
            [{"fact": "f", "subject": "s"}],
            [{"fact": "f", "subject": "s", "status": "PROBABLY"}],
        )
    # And an unknown conclusion from the wire is refused, not reinterpreted.
    with pytest.raises(ValueError, match="unknown gate conclusion"):
        read_operation_gate({"schema_version": 1, "satisfied": True, "conclusion": "maybe"})


DUPLICATE_ORDERS = {
    "resolved_then_ambiguous": ["RESOLVED", "AMBIGUOUS"],
    "ambiguous_then_resolved": ["AMBIGUOUS", "RESOLVED"],
    "both_resolved": ["RESOLVED", "RESOLVED"],
    "both_ambiguous": ["AMBIGUOUS", "AMBIGUOUS"],
}


@pytest.mark.parametrize("case", sorted(DUPLICATE_ORDERS))
def test_a_duplicated_status_entry_is_refused_in_every_order(case: str) -> None:
    """Two statuses for one need is a malformed call, not a verdict.

    The gate must refuse it regardless of order, and refuse it *even when the two
    statuses agree*, because accepting them would mean the gate silently tolerates
    a state list it cannot interpret. The expected behaviour is stated here
    independently of the engine payload.
    """
    states = [
        {"fact": "notice_period", "subject": "contract", "status": status}
        for status in DUPLICATE_ORDERS[case]
    ]
    with pytest.raises(ValueError, match="duplicate need status"):
        swe_native.operation_gate(
            "OP", [{"fact": "notice_period", "subject": "contract"}], states
        )


def test_a_duplicated_entry_names_the_need_it_cannot_interpret() -> None:
    # A typed error must say which need is duplicated, not just that something
    # is wrong: the host has to be able to point at its own bug.
    with pytest.raises(ValueError, match="contract.notice_period"):
        swe_native.operation_gate(
            "OP",
            [{"fact": "notice_period", "subject": "contract"}],
            [
                {"fact": "notice_period", "subject": "contract", "status": "RESOLVED"},
                {"fact": "notice_period", "subject": "contract", "status": "UNRESOLVED"},
            ],
        )


def test_a_duplicate_is_not_reported_as_insufficient_evidence() -> None:
    """A host bug must never masquerade as an abstention."""
    from swe.needs import read_operation_gate

    with pytest.raises(ValueError) as raised:
        swe_native.operation_gate(
            "OP",
            [{"fact": "f", "subject": "s"}],
            [
                {"fact": "f", "subject": "s", "status": "RESOLVED"},
                {"fact": "f", "subject": "s", "status": "RESOLVED"},
            ],
        )
    # It raised, so no adapter reading was produced at all. If a future host
    # caught this and turned it into a payload, the adapter would report it.
    assert "INSUFFICIENT_EVIDENCE" not in str(raised.value)
    # And a payload that did claim insufficient evidence would still be read
    # faithfully rather than reinterpreted.
    read = read_operation_gate(
        {"schema_version": 1, "satisfied": False, "conclusion": "INSUFFICIENT_EVIDENCE", "missing": []}
    )
    assert read["conclusion"] == "INSUFFICIENT_EVIDENCE"


def test_a_distinct_entry_for_each_need_is_accepted() -> None:
    # The guard must reject duplicates without rejecting legitimate state lists.
    verdict = swe_native.operation_gate(
        "OP",
        [
            {"fact": "notice_period", "subject": "contract"},
            {"fact": "price", "subject": "supplier"},
        ],
        [
            {"fact": "notice_period", "subject": "contract", "status": "RESOLVED"},
            {"fact": "price", "subject": "supplier", "status": "RESOLVED"},
        ],
    )
    assert verdict["satisfied"] is True
    assert verdict["conclusion"] == "ready"
