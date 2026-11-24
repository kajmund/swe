import ast
from pathlib import Path

import pytest
from pydantic import ValidationError

from agent_contract.join import KINDS
from agent_contract.value_channel import CHANNEL, GenerationOutput, ValidatorInput, bind_validator

ROOT = Path(__file__).resolve().parents[3]
SCHEMA = ROOT / "python" / "swe" / "swd_v1.schema.json"
MODULE = Path(__file__).resolve().parents[1] / "agent_contract" / "value_channel.py"


def test_bind_carries_the_typed_generation_output() -> None:
    bound = bind_validator(
        GenerationOutput(
            task_id="relevance.generate",
            kind="relevance",
            output={"relevant": True},
        )
    )
    assert bound.task_id == "relevance.validate"
    assert bound.source_task_id == "relevance.generate"
    assert bound.generation.output == {"relevant": True}
    assert CHANNEL == "explicit-generation-output"


def test_mistyped_generation_output_is_rejected() -> None:
    with pytest.raises(ValidationError):
        GenerationOutput(task_id="relevance.generate", kind="relevance", output=["no"])


@pytest.mark.parametrize("kind", KINDS)
def test_each_planned_kind_binds_to_its_own_validator(kind: str) -> None:
    bound = bind_validator(
        GenerationOutput(task_id=f"{kind}.generate", kind=kind, output={"kept": True})
    )
    assert bound.task_id == f"{kind}.validate"
    assert bound.source_task_id == f"{kind}.generate"
    assert bound.generation.kind == kind


def test_crossed_kind_and_task_id_is_rejected() -> None:
    with pytest.raises(ValidationError):
        bind_validator(
            GenerationOutput(
                task_id="atomicity.generate",
                kind="relevance",
                output={"atomic": True},
            )
        )


def test_unplanned_kind_is_rejected() -> None:
    with pytest.raises(ValidationError):
        bind_validator(GenerationOutput(task_id="unrelated", kind="invented", output={}))


def test_direct_validator_input_rejects_a_crossed_generation() -> None:
    generation = GenerationOutput(
        task_id="relevance.generate",
        kind="relevance",
        output={"relevant": True},
    )
    with pytest.raises(ValidationError):
        ValidatorInput(
            task_id="atomicity.validate",
            source_task_id="unrelated.generate",
            generation=generation,
        )


def test_proposal_does_not_change_swd() -> None:
    schema = SCHEMA.read_text(encoding="utf-8")
    assert "value_channel" not in schema
    assert "GenerationOutput" not in schema
    text = MODULE.read_text(encoding="utf-8")
    tree = ast.parse(text)
    modules = [
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    ]
    assert "swe" not in modules
    assert "swe.microtasks" not in modules
