import ast
from pathlib import Path

import pytest

from agent_contract.join import (
    DispatchGap,
    dispatch_gap,
    plan_invocations,
    require_swe_dispatch,
    runtime_budget_status,
    validate,
)
from hy4_research_policy.agent_adapter import (
    DECISION_PROFILE,
    AgentConfig,
    AgentHost,
    Completion,
    DecisionRequest,
    GenerationRequest,
    Usage,
)

PACKAGE = Path(__file__).resolve().parents[1] / "agent_contract" / "join.py"
SPEC = Path(__file__).resolve().parents[3] / "docs" / "specs" / "swe-microtasks.md"
READY = Path(__file__).resolve().parents[3] / "docs" / "swe-standalone" / "18-ready-map-decision.md"


class Journal:
    def __init__(self) -> None:
        self.records: list[dict] = []

    def append(self, record: dict) -> None:
        self.records.append(record)


class Allow:
    def reserve(self, key: str, amount: object) -> bool:
        return True


class Generation:
    def __init__(self, name: str, relevant: object = True) -> None:
        self.name = name
        self.relevant = relevant
        self.calls: list[str] = []

    def profile(self) -> dict[str, bool]:
        return {"supports_text_output": True, "supports_tools": False, "supports_json_schema_output": False}

    def complete(self, instruction: str, payload: dict[str, str]) -> Completion:
        self.calls.append(instruction)
        if instruction == "relevance":
            output = {"relevant": self.relevant}
        elif instruction == "extraction":
            output = {"quotes": ["quoted text."]}
        else:
            output = {"operation": "check_relevance"}
        return Completion(output=output, usage=Usage())


class Decision:
    name = "system-one"
    calls: list[str]

    def __init__(self) -> None:
        self.calls = []

    def profile(self) -> dict[str, bool]:
        return dict(DECISION_PROFILE)

    def decide(self, question: str, options: list[str], state: dict[str, str]) -> Completion:
        self.calls.append(question)
        output = {"atomic": True} if question == "atomicity" else {"choice": "entailment"}
        return Completion(output=output, usage=Usage())


def _configs(provider: str = "alpha") -> tuple[AgentConfig, AgentConfig]:
    return (
        AgentConfig(kind="generation", provider=provider, model="configured-model"),
        AgentConfig(kind="decision", provider="system-one", model="laya"),
    )


def _invoke(
    item: dict,
    generation_provider: Generation,
    decision_provider: Decision,
    journal: Journal,
    store: dict[str, dict],
) -> dict:
    """One explicit call. The test lists the calls; this is not a scheduler."""

    if item["role"] == "validator":
        source = store[f"{item['kind']}.generate"]["output"]
        return {"task_id": item["task_id"], "role": "validator", "error": validate(item["kind"], source)}
    host = AgentHost(budget=Allow(), journal=journal, retries=0)
    if item["role"] == "decision":
        result = host.decide(
            AgentConfig(kind="decision", provider=item["provider"], model=item["model"]),
            DecisionRequest(
                invocation_id=item["task_id"],
                node_id=item["task_id"],
                question=item["kind"],
                options=["entailment", "contradiction", "neutral"],
            ),
            decision_provider,
            amount=1,
        )
    else:
        result = host.generate(
            AgentConfig(kind="generation", provider=item["provider"], model=item["model"]),
            GenerationRequest(
                invocation_id=item["task_id"],
                node_id=item["task_id"],
                instruction=item["kind"],
                output_mode="text",
            ),
            generation_provider,
            amount=1,
        )
    return {"task_id": item["task_id"], "ok": result.ok, "output": result.output}


def test_package_stays_closed_and_swe_has_ready() -> None:
    gap = dispatch_gap()
    assert gap["package_dispatcher"] is False
    assert gap["fail_closed"] is True
    assert gap["swe_ready"] is True
    assert gap["swe_ordered_default"] == "layer_map"
    assert "event_ready" not in gap
    try:
        import swe_native  # noqa: F401
    except ImportError:
        assert gap["swe_native"] is False
    else:
        assert gap["swe_native"] is True
    assert gap["runtime_budget"]["attached"] is False
    spec = SPEC.read_text(encoding="utf-8")
    assert "A later layer cannot start until every" in spec
    ready = READY.read_text(encoding="utf-8")
    section = ready.split("## 7. Acceptanskriterier", 1)[1].split("## 8.", 1)[0]
    assert 'schedule: "ready"' in section
    text = PACKAGE.read_text(encoding="utf-8")
    assert "There is no event-ready" not in text
    with pytest.raises(DispatchGap):
        require_swe_dispatch()
    assert runtime_budget_status()["attached"] is False


def test_generation_and_validators_are_separate_invocations() -> None:
    generation, decision = _configs()
    plan = plan_invocations(generation, decision)
    assert [row["task_id"] for row in plan] == [
        "relevance.generate",
        "relevance.validate",
        "extraction.generate",
        "extraction.validate",
        "entailment.generate",
        "entailment.validate",
        "atomicity.generate",
        "atomicity.validate",
        "operation.generate",
        "operation.validate",
    ]
    assert plan[5]["depends_on"] == ["entailment.generate"]
    assert plan[8]["depends_on"] == ["entailment.validate", "atomicity.validate"]
    roles = {row["role"] for row in plan}
    assert roles == {"generation", "decision", "validator"}


def test_config_changes_the_provider_and_not_the_graph() -> None:
    alpha = plan_invocations(*_configs("alpha"))
    beta = plan_invocations(*_configs("beta"))
    assert [row["task_id"] for row in alpha] == [row["task_id"] for row in beta]
    assert alpha[0]["provider"] == "alpha"
    assert beta[0]["provider"] == "beta"
    assert alpha[4]["provider"] == "system-one"


def test_offline_invocations_cancel_and_replay() -> None:
    generation_provider = Generation("alpha")
    decision_provider = Decision()
    journal = Journal()
    plan = plan_invocations(*_configs())
    store: dict[str, dict] = {}
    cancelled = False
    for item in plan:
        if cancelled:
            break
        if item["task_id"] in store:
            continue
        outcome = _invoke(item, generation_provider, decision_provider, journal, store)
        if item["role"] == "validator":
            assert outcome["error"] is None
        store[item["task_id"]] = outcome
        if item["task_id"] == "relevance.validate":
            cancelled = True
    assert "extraction.generate" not in store
    assert generation_provider.calls == ["relevance"]

    replayed = Generation("alpha")
    again = Journal()
    store["relevance.generate"] = store["relevance.generate"]
    for item in plan[:1]:
        if item["task_id"] in store:
            continue
        _invoke(item, replayed, decision_provider, again, store)
    assert replayed.calls == []
    assert again.records == []


def test_semantic_failure_does_not_add_another_model_call() -> None:
    provider = Generation("alpha", relevant="yes")
    plan = plan_invocations(*_configs())
    outcome = _invoke(plan[0], provider, Decision(), Journal(), {})
    assert validate("relevance", outcome["output"]) == "relevant must be a boolean"
    assert provider.calls == ["relevance"]
    assert validate("relevance", {"relevant": True}) is None


def test_module_does_not_schedule_or_attach_budget() -> None:
    text = PACKAGE.read_text(encoding="utf-8")
    tree = ast.parse(text)
    names = [node.id for node in ast.walk(tree) if isinstance(node, ast.Name)]
    assert "gather" not in names
    assert "ModelRetry" not in names
    assert "SweycoinMeter" not in names
    assert "RuntimeBudget" not in names
