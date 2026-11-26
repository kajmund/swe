import ast
import importlib.util
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import BaseModel

from hy4_research_policy.semantic_contract import (
    INVOCATION_FIELDS,
    MICROTASK_FIELDS,
    Claim,
    Judgement,
    contract_microtasks,
    evaluate,
)

REPO = Path(__file__).resolve().parents[3]
PACKAGE = Path(__file__).resolve().parents[1] / "hy4_research_policy" / "semantic_contract.py"


class ScriptedJev:
    def __init__(self, script: list[Judgement | Exception]) -> None:
        self.script = list(script)
        self.calls: list[dict[str, str]] = []

    def judge(self, *, proposition: str, evidence: str, source_id: str) -> Judgement:
        self.calls.append(
            {"proposition": proposition, "evidence": evidence, "source_id": source_id}
        )
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class MemoryBudget:
    def __init__(self, remaining: Decimal) -> None:
        self.remaining = remaining
        self.keys: list[str] = []

    def reserve(self, key: str, amount: Decimal) -> bool:
        if amount > self.remaining:
            return False
        self.remaining -= amount
        self.keys.append(key)
        return True


class MemoryJournal:
    def __init__(self) -> None:
        self.records: list[dict] = []

    def append(self, record: dict) -> None:
        self.records.append(record)


CORPUS = {"policy-p17": "The effective date of Policy P-17 is 2026-03-01."}
CLAIM = {
    "source_id": "policy-p17",
    "quote": "The effective date of Policy P-17 is 2026-03-01.",
    "proposition": "Policy P-17 takes effect on 2026-03-01.",
}


def test_structure_failure_does_not_call_jev() -> None:
    client = ScriptedJev([Judgement(relation="SUPPORTS", confidence="0.9")])
    journal = MemoryJournal()
    verdict = evaluate(
        {**CLAIM, "note": "extra"},
        CORPUS,
        client=client,
        budget=MemoryBudget(Decimal("1")),
        journal=journal,
    )
    assert verdict.status == "error"
    assert verdict.error == "structure"
    assert client.calls == []
    assert journal.records == []


def test_locator_failure_does_not_call_jev() -> None:
    client = ScriptedJev([Judgement(relation="SUPPORTS", confidence="0.9")])
    verdict = evaluate(
        {**CLAIM, "quote": "not in the document"},
        CORPUS,
        client=client,
        budget=MemoryBudget(Decimal("1")),
        journal=MemoryJournal(),
    )
    assert verdict.error == "locator"
    assert client.calls == []


def test_support_is_entailment_after_one_explicit_call() -> None:
    client = ScriptedJev([Judgement(relation="SUPPORTS", confidence="0.91")])
    journal = MemoryJournal()
    budget = MemoryBudget(Decimal("1"))
    verdict = evaluate(CLAIM, CORPUS, client=client, budget=budget, journal=journal)
    assert verdict.status == "entailment"
    assert verdict.relation == "SUPPORTS"
    assert verdict.atomic is True
    assert verdict.confidence == "0.91"
    assert len(client.calls) == 1
    assert budget.keys == ["jev"]
    assert set(journal.records[0]) == set(INVOCATION_FIELDS)
    assert journal.records[0]["ok"] is True
    assert journal.records[0]["node_id"] == "jev"


def test_low_confidence_abstains_without_a_second_call() -> None:
    client = ScriptedJev([Judgement(relation="SUPPORTS", confidence="0.4")])
    verdict = evaluate(
        CLAIM,
        CORPUS,
        client=client,
        budget=MemoryBudget(Decimal("1")),
        journal=MemoryJournal(),
    )
    assert verdict.status == "abstain"
    assert verdict.relation == "SUPPORTS"
    assert len(client.calls) == 1


def test_jev_error_is_journaled_once() -> None:
    client = ScriptedJev([RuntimeError("down")])
    journal = MemoryJournal()
    verdict = evaluate(
        CLAIM,
        CORPUS,
        client=client,
        budget=MemoryBudget(Decimal("1")),
        journal=journal,
    )
    assert verdict.error == "jev"
    assert len(client.calls) == 1
    assert journal.records[0]["ok"] is False
    assert journal.records[0]["error"]["kind"] == "provider"


def test_budget_refusal_skips_jev() -> None:
    client = ScriptedJev([Judgement(relation="CONTRADICTS", confidence="0.8")])
    verdict = evaluate(
        CLAIM,
        CORPUS,
        client=client,
        budget=MemoryBudget(Decimal("0")),
        journal=MemoryJournal(),
    )
    assert verdict.error == "budget"
    assert client.calls == []


def test_contract_has_no_hidden_retry_or_validator() -> None:
    text = PACKAGE.read_text(encoding="utf-8")
    tree = ast.parse(text)
    names = [
        node.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
    ]
    assert "ModelRetry" not in names
    assert "output_validator" not in names
    assert "TypeSafeModel" not in names
    imported = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module.split(".")[0])
    assert "swe" not in imported
    assert issubclass(Claim, BaseModel)


def test_microtasks_use_the_swe_contract_fields() -> None:
    tasks = contract_microtasks(CLAIM)
    assert [task["task_id"] for task in tasks] == ["structure", "locator", "jev"]
    assert tasks[2]["depends_on"] == ["locator"]
    source = (REPO / "python" / "swe" / "microtasks.py").read_text(encoding="utf-8")
    assert set(MICROTASK_FIELDS) == {"task_id", "payload", "depends_on"}
    assert '{"task_id", "payload", "depends_on"}' in source


def test_contradiction_is_a_typed_verdict() -> None:
    client = ScriptedJev([Judgement(relation="CONTRADICTS", confidence="0.8")])
    verdict = evaluate(
        CLAIM,
        CORPUS,
        client=client,
        budget=MemoryBudget(Decimal("1")),
        journal=MemoryJournal(),
    )
    assert verdict.status == "contradiction"
    assert len(client.calls) == 1


def test_invocation_fields_match_the_swe_record() -> None:
    source = (REPO / "python" / "swe" / "run_store.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    fields = []
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "InvocationRecord":
            for child in node.body:
                if isinstance(child, ast.AnnAssign) and isinstance(child.target, ast.Name):
                    fields.append(child.target.id)
    assert tuple(fields) == INVOCATION_FIELDS


def test_installed_pydantic_ai_jev_integration() -> None:
    import pydantic_ai

    version = pydantic_ai.__version__
    typesafe = importlib.util.find_spec("pydantic_ai.models.typesafe")
    system_one = importlib.util.find_spec("pydantic_ai.models.system_one")
    if version == "1.63.0":
        assert typesafe is None
        assert system_one is None
        return
    if version == "2.54.0":
        assert typesafe is not None
        assert system_one is not None
        with pytest.raises(ImportError, match="typesafe-sdk"):
            import pydantic_ai.models.typesafe  # noqa: F401
        return
    pytest.fail(f"unexpected pydantic-ai {version}")
