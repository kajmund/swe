import ast
from pathlib import Path

from agent_contract.join import validate
from agent_contract.seam import (
    FIRST_CLASS_READY_FIELDS,
    HANDLER_INPUT_KEYS,
    READY_MAP_PROPOSAL,
    VALUE_CHANNEL,
    layer_is_complete,
    migration_items,
    ready_ids,
)
from hy4_research_policy.agent_adapter import AgentConfig

SEAM = Path(__file__).resolve().parents[1] / "agent_contract" / "seam.py"
JOIN = Path(__file__).resolve().parents[1] / "agent_contract" / "join.py"
WITNESS_LAYERS = (["slow", "fast"], ["after_fast"])


def _items(provider: str = "alpha") -> list[dict]:
    return migration_items(
        AgentConfig(kind="generation", provider=provider, model=f"{provider}-model"),
        AgentConfig(kind="decision", provider="system-one", model="laya"),
    )


def test_plan_is_a_static_dag_for_a_future_ready_map() -> None:
    alpha = _items("alpha")
    beta = _items("beta")
    assert [item["task_id"] for item in alpha] == [item["task_id"] for item in beta]
    assert [item["depends_on"] for item in alpha] == [item["depends_on"] for item in beta]
    assert alpha[0]["provider"] == "alpha"
    assert beta[0]["provider"] == "beta"
    assert alpha[4]["provider"] == "system-one"
    ids = [item["task_id"] for item in alpha]
    assert len(ids) == len(set(ids)) == 10
    for item in alpha:
        assert set(item["depends_on"]) <= set(ids)
        assert "output" not in item
    assert READY_MAP_PROPOSAL["schedule"] == "ready"
    assert FIRST_CLASS_READY_FIELDS == ("schedule", "item_id", "item_depends_on")
    assert HANDLER_INPUT_KEYS == ("task_id", "payload", "depends_on")
    assert VALUE_CHANNEL == "absent"


def test_ready_predicate_disagrees_with_the_layer_barrier() -> None:
    items = [
        {"task_id": "slow", "depends_on": []},
        {"task_id": "fast", "depends_on": []},
        {"task_id": "after_fast", "depends_on": ["fast"]},
    ]
    completed = {"fast"}
    assert "after_fast" in ready_ids(items, completed)
    assert layer_is_complete(WITNESS_LAYERS[0], completed) is False


def test_unhashable_entailment_choice_is_a_semantic_error() -> None:
    assert validate("entailment", {"choice": []}) == "choice is outside the entailment labels"
    assert validate("entailment", {"choice": {}}) == "choice is outside the entailment labels"
    assert validate("entailment", {"choice": "entailment"}) is None


def test_seam_does_not_import_a_scheduler_or_budget() -> None:
    text = SEAM.read_text(encoding="utf-8")
    tree = ast.parse(text)
    modules = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.append(node.module)
    assert modules == [
        "__future__",
        "collections.abc",
        "typing",
        "hy4_research_policy.agent_adapter",
        "agent_contract.join",
    ]
    names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    assert "RuntimeBudget" not in names
    assert "schedule_ready" not in names
    assert "asyncio" not in text
    assert "barrier_free_scheduler" not in text
    join = JOIN.read_text(encoding="utf-8")
    assert "ReadyQueueCoordinator" not in join
    assert "item_depends_on" not in join
