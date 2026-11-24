"""Proposed host bind. It is not an SWD change and not a live integration.

``item_depends_on`` admits a task. It does not carry the generation result.
A host that wants a validator to see that result builds ``ValidatorInput``
explicitly from the generation invocation. The bind accepts only a planned
kind, and the generation task, validator task, and source task must name that
same kind. Nothing in ``python/swe`` reads this module.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agent_contract.join import KINDS

CHANNEL = "explicit-generation-output"
_PLANNED_KINDS = frozenset(KINDS)


def _generation_task_id(kind: str) -> str:
    return f"{kind}.generate"


def _validator_task_id(kind: str) -> str:
    return f"{kind}.validate"


class GenerationOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    task_id: str = Field(min_length=1)
    kind: str = Field(min_length=1)
    output: dict[str, Any]

    @model_validator(mode="after")
    def planned_identity(self) -> GenerationOutput:
        if self.kind not in _PLANNED_KINDS or self.task_id != _generation_task_id(self.kind):
            raise ValueError("generation task_id must be {kind}.generate for a planned kind")
        return self


class ValidatorInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    task_id: str = Field(min_length=1)
    source_task_id: str = Field(min_length=1)
    generation: GenerationOutput

    @model_validator(mode="after")
    def planned_identity(self) -> ValidatorInput:
        kind = self.generation.kind
        if self.task_id != _validator_task_id(kind) or self.source_task_id != self.generation.task_id:
            raise ValueError("validator task_id and source_task_id must match the generation kind")
        return self


def bind_validator(generation: GenerationOutput) -> ValidatorInput:
    """One explicit bind. The map handler input is left unchanged."""

    return ValidatorInput(
        task_id=_validator_task_id(generation.kind),
        source_task_id=generation.task_id,
        generation=generation,
    )
