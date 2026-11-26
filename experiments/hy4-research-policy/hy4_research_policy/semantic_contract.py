"""Strict claim, source check, then one explicit JEV judgement.

Pydantic AI 2.54 documents ``TypeSafeModel`` for Jev and ``SystemOneModel`` for
the ``/v1/systemone`` API. The class is not importable here without
``typesafe-sdk``, and 1.63 has neither module. This contract does not construct
those models, register an output validator, or raise ``ModelRetry``. The host
reserves budget, writes the journal, and calls JEV once.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError

# Fields of swe.run_store.InvocationRecord. SWE persists this shape; the
# experiment does not import the runtime.
INVOCATION_FIELDS = ("invocation_id", "node_id", "ok", "value", "error")
MICROTASK_FIELDS = ("task_id", "payload", "depends_on")
CONFIDENCE_FLOOR = Decimal("0.6")


class Claim(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    source_id: str = Field(min_length=1)
    quote: str = Field(min_length=1)
    proposition: str = Field(min_length=1)


class Judgement(BaseModel):
    """One atomic JEV answer. The client, not a validator, returns this."""

    model_config = ConfigDict(extra="forbid", strict=True)
    relation: Literal["SUPPORTS", "CONTRADICTS", "INSUFFICIENT"]
    confidence: str


class Verdict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    status: Literal["entailment", "contradiction", "abstain", "error"]
    relation: Literal["SUPPORTS", "CONTRADICTS", "INSUFFICIENT"] | None = None
    atomic: bool
    confidence: str | None = None
    error: Literal["structure", "locator", "budget", "jev"] | None = None
    detail: str


class JevClient(Protocol):
    def judge(self, *, proposition: str, evidence: str, source_id: str) -> Judgement: ...


class Budget(Protocol):
    def reserve(self, key: str, amount: Decimal) -> bool: ...


class Journal(Protocol):
    def append(self, record: dict[str, Any]) -> None: ...


def contract_microtasks(claim: dict[str, Any]) -> list[dict[str, Any]]:
    """Three microtasks in the SWE shape: structure, locator, then JEV."""

    tasks = [
        {
            "task_id": "structure",
            "payload": {"stage": "structure", "claim": claim},
            "depends_on": [],
        },
        {
            "task_id": "locator",
            "payload": {"stage": "locator"},
            "depends_on": ["structure"],
        },
        {
            "task_id": "jev",
            "payload": {"stage": "jev"},
            "depends_on": ["locator"],
        },
    ]
    for task in tasks:
        unknown = set(task) - set(MICROTASK_FIELDS)
        if unknown:
            raise ValueError(f"unknown microtask fields: {sorted(unknown)}")
    return tasks


def locate(claim: Claim, corpus: dict[str, str]) -> bool:
    text = corpus.get(claim.source_id)
    return text is not None and claim.quote in text


def _error(kind: Literal["structure", "locator", "budget", "jev"], detail: str, *, atomic: bool) -> Verdict:
    return Verdict(status="error", atomic=atomic, error=kind, detail=detail)


def _from_judgement(judgement: Judgement) -> Verdict:
    confidence = Decimal(judgement.confidence)
    if judgement.relation == "INSUFFICIENT" or confidence < CONFIDENCE_FLOOR:
        status = "abstain"
    elif judgement.relation == "SUPPORTS":
        status = "entailment"
    else:
        status = "contradiction"
    return Verdict(
        status=status,
        relation=judgement.relation,
        atomic=True,
        confidence=judgement.confidence,
        detail=judgement.relation,
    )


class _DecisionBinding:
    """One configured decision provider. The host performs the invocation."""

    name = "typesafe"

    def __init__(self, client: JevClient) -> None:
        self.client = client

    def profile(self) -> dict[str, bool]:
        from hy4_research_policy.agent_adapter import DECISION_PROFILE

        return dict(DECISION_PROFILE)

    def decide(self, question: str, options: list[str], state: dict[str, str]) -> Any:
        from hy4_research_policy.agent_adapter import Completion

        judgement = self.client.judge(
            proposition=question,
            evidence=state["evidence"],
            source_id=state["source_id"],
        )
        if judgement.relation not in options:
            raise ValueError("relation is outside the requested options")
        return Completion(output=judgement.model_dump())


def _invoke_jev(
    claim: Claim,
    *,
    client: JevClient,
    budget: Budget,
    journal: Journal,
    amount: Decimal,
) -> Verdict:
    from hy4_research_policy.agent_adapter import AgentConfig, AgentHost, DecisionRequest

    result = AgentHost(budget=budget, journal=journal, retries=0).decide(
        AgentConfig(kind="decision", provider="typesafe", model="jev-1.13.0"),
        DecisionRequest(
            invocation_id="jev",
            node_id="jev",
            question=claim.proposition,
            options=["SUPPORTS", "CONTRADICTS", "INSUFFICIENT"],
            state={"evidence": claim.quote, "source_id": claim.source_id},
        ),
        _DecisionBinding(client),
        amount=amount,
        native=False,
    )
    if result.error is not None and result.error.kind == "budget":
        return _error("budget", result.error.message, atomic=True)
    if not result.ok or result.output is None:
        detail = result.error.message if result.error is not None else "jev"
        return _error("jev", detail, atomic=True)
    return _from_judgement(Judgement.model_validate(result.output))


def evaluate(
    raw: dict[str, Any],
    corpus: dict[str, str],
    *,
    client: JevClient,
    budget: Budget,
    journal: Journal,
    amount: Decimal = Decimal("0.001"),
) -> Verdict:
    """Run structure, then the source check, then at most one JEV call."""

    tasks = contract_microtasks(raw if isinstance(raw, dict) else {})
    claim: Claim | None = None
    verdict = _error("structure", "not run", atomic=False)
    for task in tasks:
        stage = task["payload"]["stage"]
        if stage == "structure":
            if not isinstance(raw, dict):
                verdict = _error("structure", "claim must be an object", atomic=False)
                continue
            try:
                claim = Claim.model_validate(raw)
            except ValidationError as exc:
                verdict = _error("structure", exc.errors()[0]["type"], atomic=False)
                continue
            verdict = Verdict(status="abstain", atomic=True, detail="structure_ok")
        elif stage == "locator":
            if verdict.error is not None or claim is None:
                continue
            if not locate(claim, corpus):
                verdict = _error("locator", "quote is not in the cited source", atomic=True)
        elif stage == "jev":
            if verdict.error is not None or claim is None:
                continue
            verdict = _invoke_jev(claim, client=client, budget=budget, journal=journal, amount=amount)
        else:
            raise ValueError(f"unknown stage {stage}")
    return verdict
