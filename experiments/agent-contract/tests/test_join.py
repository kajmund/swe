import ast
import asyncio
from pathlib import Path

import pytest

from agent_contract.join import LAYERS, BudgetPending, run_join, runtime_budget_status, validate
from hy4_research_policy.agent_adapter import (
    DECISION_PROFILE,
    AgentConfig,
    Completion,
    Usage,
)

PACKAGE = Path(__file__).resolve().parents[1] / "agent_contract" / "join.py"


class Ledger:
    def __init__(self, remaining: int = 10) -> None:
        self.remaining = remaining
        self.next_id = 1
        self.settled: list[tuple[int, int, str]] = []

    def reserve(self, *, run_id: str, account: str, amount_milli: int, key: str) -> int:
        if amount_milli > self.remaining:
            raise RuntimeError("refused")
        self.remaining -= amount_milli
        reservation_id = self.next_id
        self.next_id += 1
        return reservation_id

    def settle(self, *, reservation_id: int, spent_milli: int, key: str) -> None:
        self.settled.append((reservation_id, spent_milli, key))

    def release(self, *, reservation_id: int, key: str) -> None:
        raise AssertionError("release is for PR 11 reconciliation, not this run")


class Journal:
    def __init__(self) -> None:
        self.records: list[dict] = []

    def append(self, record: dict) -> None:
        self.records.append(record)


class Generation:
    def __init__(self, name: str) -> None:
        self.name = name
        self.calls: list[str] = []

    def profile(self) -> dict[str, bool]:
        return {
            "supports_text_output": True,
            "supports_tools": False,
            "supports_json_schema_output": False,
            "supports_json_object_output": False,
        }

    def complete(self, instruction: str, payload: dict[str, str]) -> Completion:
        self.calls.append(instruction)
        if instruction == "relevance":
            output = {"relevant": True}
        elif instruction == "extraction":
            output = {"quotes": ["quoted text."]}
        else:
            output = {"operation": "check_relevance"}
        return Completion(output=output, usage=Usage())


class Decision:
    def __init__(self) -> None:
        self.name = "system-one"
        self.calls: list[str] = []

    def profile(self) -> dict[str, bool]:
        return dict(DECISION_PROFILE)

    def decide(self, question: str, options: list[str], state: dict[str, str]) -> Completion:
        self.calls.append(question)
        if question == "atomicity":
            output = {"atomic": True}
        else:
            output = {"choice": "entailment"}
        return Completion(output=output, usage=Usage())


def _configs(provider: str = "alpha") -> tuple[AgentConfig, AgentConfig]:
    return (
        AgentConfig(kind="generation", provider=provider, model="configured-model"),
        AgentConfig(kind="decision", provider="system-one", model="laya"),
    )


def test_pr11_budget_is_not_attached() -> None:
    status = runtime_budget_status()
    assert status["attached"] is False
    generation, decision = _configs()
    with pytest.raises(BudgetPending):
        asyncio.run(
            run_join(
                run_id="run-1",
                generation=generation,
                decision=decision,
                generation_provider=Generation("alpha"),
                decision_provider=Decision(),
                ledger=None,
                journal=Journal(),
            )
        )


def test_config_swaps_the_generation_provider_without_a_retry() -> None:
    for name in ("alpha", "beta"):
        provider = Generation(name)
        decision = Decision()
        generation, decision_config = _configs(name)
        journal = Journal()
        report = asyncio.run(
            run_join(
                run_id="run-1",
                generation=generation,
                decision=decision_config,
                generation_provider=provider,
                decision_provider=decision,
                ledger=Ledger(),
                journal=journal,
            )
        )
        assert report["retries"] == 0
        assert [row["ok"] for row in journal.records].count(True) == 5
        assert provider.calls == ["relevance", "extraction", "operation"]
        assert decision.calls == ["entailment", "atomicity"]
        assert report["results"]["operation"]["ok"] is True


def test_semantic_rejection_does_not_call_the_provider_again() -> None:
    class BadGeneration(Generation):
        def complete(self, instruction: str, payload: dict[str, str]) -> Completion:
            self.calls.append(instruction)
            return Completion(output={"relevant": "yes"}, usage=Usage())

    provider = BadGeneration("alpha")
    generation, decision_config = _configs()
    report = asyncio.run(
        run_join(
            run_id="run-1",
            generation=generation,
            decision=decision_config,
            generation_provider=provider,
            decision_provider=Decision(),
            ledger=Ledger(),
            journal=Journal(),
        )
    )
    assert provider.calls == ["relevance"]
    assert report["results"]["relevance"]["error"] == "semantic"
    assert report["results"]["operation"]["called"] is False
    assert validate("relevance", {"relevant": True}) is None


def test_middle_layer_runs_concurrently() -> None:
    entered: list[str] = []
    release = asyncio.Event()

    async def before_call(kind: str) -> None:
        if kind not in {"entailment", "atomicity"}:
            return
        entered.append(kind)
        if len(entered) == 2:
            release.set()
        await release.wait()

    generation, decision_config = _configs()
    asyncio.run(
        run_join(
            run_id="run-1",
            generation=generation,
            decision=decision_config,
            generation_provider=Generation("alpha"),
            decision_provider=Decision(),
            ledger=Ledger(),
            journal=Journal(),
            before_call=before_call,
        )
    )
    assert entered == ["entailment", "atomicity"] or set(entered) == {"entailment", "atomicity"}
    assert LAYERS[2] == ("entailment", "atomicity")


def test_join_has_no_hidden_retry_or_second_ledger() -> None:
    tree = ast.parse(PACKAGE.read_text(encoding="utf-8"))
    names = [node.id for node in ast.walk(tree) if isinstance(node, ast.Name)]
    assert "ModelRetry" not in names
    assert "SweycoinMeter" not in names
    assert "retries=0" in PACKAGE.read_text(encoding="utf-8")
