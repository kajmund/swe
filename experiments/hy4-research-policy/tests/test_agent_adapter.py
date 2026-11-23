import ast
import importlib.util
import inspect
import json
from decimal import Decimal
from pathlib import Path

import pytest

from hy4_research_policy.agent_adapter import (
    CATALOG_VERSION,
    DECISION_PROFILE,
    NATIVE,
    PROFILE_FLAGS,
    AgentConfig,
    AgentHost,
    Completion,
    DecisionRequest,
    GenerationRequest,
    Usage,
    decision_status,
)
from hy4_research_policy.semantic_contract import INVOCATION_FIELDS

EXPERIMENT = Path(__file__).resolve().parents[1]
ADAPTER = EXPERIMENT / "hy4_research_policy" / "agent_adapter.py"


class Budget:
    def __init__(self, remaining: Decimal) -> None:
        self.remaining = remaining
        self.keys: list[str] = []

    def reserve(self, key: str, amount: Decimal) -> bool:
        if amount > self.remaining:
            return False
        self.remaining -= amount
        self.keys.append(key)
        return True


class Journal:
    def __init__(self) -> None:
        self.records: list[dict] = []

    def append(self, record: dict) -> None:
        self.records.append(record)


class Generation:
    def __init__(self, name: str, profile: dict[str, bool], usage: Usage, raises: int = 0) -> None:
        self.name = name
        self._profile = profile
        self.usage = usage
        self.raises = raises
        self.calls = 0

    def profile(self) -> dict[str, bool]:
        return self._profile

    def complete(self, instruction: str, payload: dict[str, str]) -> Completion:
        self.calls += 1
        if self.calls <= self.raises:
            raise RuntimeError("down")
        return Completion(output={"text": instruction, "provider": self.name}, usage=self.usage)


class Decision:
    def __init__(self, name: str) -> None:
        self.name = name
        self.calls = 0

    def profile(self) -> dict[str, bool]:
        return dict(DECISION_PROFILE)

    def decide(self, question: str, options: list[str], state: dict[str, str]) -> Completion:
        self.calls += 1
        return Completion(output={"choice": options[0], "question": question}, usage=Usage())


def _request() -> GenerationRequest:
    return GenerationRequest(
        invocation_id="gen-1",
        node_id="generate",
        instruction="say hello",
        output_mode="text",
    )


def test_two_generation_providers_share_one_contract() -> None:
    alpha = Generation("alpha", {"supports_text_output": True, "supports_tools": True, "supports_json_schema_output": True}, Usage(input_tokens=3, output_tokens=1, requests=1))
    beta = Generation("beta", {"supports_text_output": True, "supports_tools": False, "supports_json_schema_output": False}, Usage())
    request = _request()
    for provider in (alpha, beta):
        journal = Journal()
        result = AgentHost(budget=Budget(Decimal("1")), journal=journal, retries=0).generate(
            AgentConfig(kind="generation", provider=provider.name, model="configured-model"),
            request,
            provider,
            amount=Decimal("0.001"),
        )
        assert result.ok is True
        assert result.output is not None
        assert result.output["provider"] == provider.name
        assert provider.calls == 1
        assert set(journal.records[0]) == set(INVOCATION_FIELDS)
        if provider.name == "alpha":
            assert result.usage.input_tokens == 3
            assert result.usage.output_tokens == 1
            assert result.usage.requests == 1
        else:
            assert result.usage.input_tokens is None
            assert result.usage.output_tokens is None
            assert result.usage.requests is None


def test_capability_gate_skips_the_provider() -> None:
    beta = Generation("beta", {"supports_text_output": True, "supports_tools": False, "supports_json_schema_output": False}, Usage())
    host = AgentHost(budget=Budget(Decimal("1")), journal=Journal())
    request = _request()
    request = request.model_copy(update={"output_mode": "tool", "tool_name": "lookup"})
    result = host.generate(
        AgentConfig(kind="generation", provider="beta", model="configured-model"),
        request,
        beta,
        amount=Decimal("0.001"),
    )
    assert result.error is not None
    assert result.error.kind == "capability"
    assert beta.calls == 0


def test_decision_models_are_separate_and_clef_is_absent() -> None:
    assert decision_status("system-one", "laya") == "mockable"
    assert decision_status("system-one", "clm-latest") == "mockable"
    assert decision_status("typesafe", "jev-1.13.0") == "mockable"
    assert decision_status("clef", "clef") == "absent"
    laya = Decision("system-one")
    journal = Journal()
    result = AgentHost(budget=Budget(Decimal("1")), journal=journal).decide(
        AgentConfig(kind="decision", provider="system-one", model="laya"),
        DecisionRequest(invocation_id="d1", node_id="decide", question="Is it supported?", options=["yes", "no"]),
        laya,
        amount=Decimal("0.001"),
    )
    assert result.ok is True
    assert laya.calls == 1
    clef = Decision("clef")
    refused = AgentHost(budget=Budget(Decimal("1")), journal=Journal()).decide(
        AgentConfig(kind="decision", provider="clef", model="clef"),
        DecisionRequest(invocation_id="d2", node_id="decide", question="Is it supported?", options=["yes"]),
        clef,
        amount=Decimal("0.001"),
    )
    assert refused.error is not None
    assert refused.error.kind == "provider"
    assert clef.calls == 0
    native = Decision("typesafe")
    blocked = AgentHost(budget=Budget(Decimal("1")), journal=Journal()).decide(
        AgentConfig(kind="decision", provider="typesafe", model="jev-1.13.0"),
        DecisionRequest(invocation_id="d3", node_id="decide", question="Is it supported?", options=["yes"]),
        native,
        amount=Decimal("0.001"),
        native=True,
    )
    assert blocked.error is not None
    assert "typesafe-sdk" in blocked.error.message
    assert native.calls == 0


def test_retry_is_explicit_and_journaled() -> None:
    provider = Generation("alpha", {"supports_text_output": True}, Usage(), raises=1)
    journal = Journal()
    result = AgentHost(budget=Budget(Decimal("1")), journal=journal, retries=1).generate(
        AgentConfig(kind="generation", provider="alpha", model="configured-model"),
        _request(),
        provider,
        amount=Decimal("0.001"),
    )
    assert result.ok is True
    assert provider.calls == 2
    assert [row["ok"] for row in journal.records] == [False, True]


def test_tool_call_is_a_separate_invocation() -> None:
    provider = Generation("alpha", {"supports_tools": True, "supports_text_output": True}, Usage())
    journal = Journal()
    host = AgentHost(budget=Budget(Decimal("1")), journal=journal)
    result = host.call_tool(
        invocation_id="tool-1",
        node_id="tool",
        name="lookup",
        payload={"key": "a"},
        tools={"lookup": lambda payload: {"value": payload["key"]}},
        amount=Decimal("0.001"),
    )
    assert result.output == {"value": "a"}
    assert provider.calls == 0
    assert journal.records[0]["node_id"] == "tool"
    missing = host.call_tool(
        invocation_id="tool-2",
        node_id="tool",
        name="missing",
        payload={},
        tools={},
        amount=Decimal("0.001"),
    )
    assert missing.error is not None
    assert missing.error.kind == "tool"


def test_budget_refusal_does_not_call_the_provider() -> None:
    provider = Generation("alpha", {"supports_text_output": True}, Usage())
    result = AgentHost(budget=Budget(Decimal("0")), journal=Journal()).generate(
        AgentConfig(kind="generation", provider="alpha", model="configured-model"),
        _request(),
        provider,
        amount=Decimal("0.001"),
    )
    assert result.error is not None
    assert result.error.kind == "budget"
    assert provider.calls == 0


def test_adapter_has_no_agent_loop_or_engine() -> None:
    tree = ast.parse(ADAPTER.read_text(encoding="utf-8"))
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)
    assert "pydantic_ai" not in imported
    assert "pydantic_graph" not in imported
    assert "swe" not in imported
    assert NATIVE["ollama"]["prefix"] == "ollama:"
    assert NATIVE["typesafe"]["prefix"] == "typesafe:"
    assert NATIVE["system-one"]["prefix"] == "system-one:"
    protocol = json.loads((EXPERIMENT / "config" / "protocol.json").read_text(encoding="utf-8"))
    assert protocol["budget"]["usd_cap"] == "2"


def test_installed_model_directory() -> None:
    import pydantic_ai

    version = pydantic_ai.__version__
    if version == "1.63.0":
        for name in ("pydantic_ai.models.ollama", "pydantic_ai.models.typesafe", "pydantic_ai.models.system_one"):
            assert importlib.util.find_spec(name) is None
        return
    if version != "2.54.0":
        pytest.fail(f"unexpected pydantic-ai {version}")
    assert CATALOG_VERSION == version
    from pydantic_ai.models.ollama import OllamaModel
    from pydantic_ai.models.system_one import SystemOneModel
    from pydantic_ai.profiles import ModelProfile
    from pydantic_ai.profiles.decision import decision_model_profile

    assert OllamaModel.__name__ == "OllamaModel"
    assert SystemOneModel.__name__ == "SystemOneModel"
    assert set(PROFILE_FLAGS) <= set(ModelProfile.__annotations__)
    profile = decision_model_profile("laya")
    for flag, expected in DECISION_PROFILE.items():
        assert profile[flag] is expected
    module_text = Path(inspect.getfile(SystemOneModel)).read_text(encoding="utf-8")
    assert "Laya" in module_text
    assert "clef" not in module_text.lower()
    with pytest.raises(ImportError, match="typesafe-sdk"):
        import pydantic_ai.models.typesafe  # noqa: F401
