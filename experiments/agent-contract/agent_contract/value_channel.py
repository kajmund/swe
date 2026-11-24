"""Proposed value channel. It is not an SWD change.

``item_depends_on`` admits a task. It does not carry the generation result.
A host that wants a validator to see that result builds ``ValidatorInput``
explicitly from the generation invocation. Nothing in ``python/swe`` reads
this module.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

CHANNEL = "explicit-generation-output"


class GenerationOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    task_id: str = Field(min_length=1)
    kind: str = Field(min_length=1)
    output: dict[str, Any]


class ValidatorInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    task_id: str = Field(min_length=1)
    source_task_id: str = Field(min_length=1)
    generation: GenerationOutput


def bind_validator(generation: GenerationOutput) -> ValidatorInput:
    """One explicit bind. The map handler input is left unchanged."""

    return ValidatorInput(
        task_id=f"{generation.kind}.validate",
        source_task_id=generation.task_id,
        generation=generation,
    )
