"""Strict decision objects. Extra fields and coerced scalars are rejected."""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict


class Quote(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    source_id: str
    quote: str


class PolicyDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    needs: list[Quote]
    actions: list["Action"]
    stop: "StopDecision"


class Action(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    tool: Literal["retrieve", "read_passage"]
    gap: Quote


class Decomposition(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    target_quotes: list[Quote]


class NextAction(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    tool: Literal["retrieve", "read_passage", "stop"]
    gap: Optional[Quote] = None


class EvidenceGap(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    corpus_contains_answer: bool
    unresolved: list[Quote]


class StopDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    conclusion: Literal["ANSWERED", "CONTRADICTED", "INSUFFICIENT_EVIDENCE"]
    answer_complete: bool
    claims: list[Quote]


PolicyDecision.model_rebuild()
