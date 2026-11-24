"""Model-independent adapter. SWE selects the provider and owns each call.

Recorded against the model directory at
https://pydantic.dev/docs/ai/models/overview/ on 2026-11-23 and Pydantic AI
2.54.0. Ollama is a generation prefix. TypeSafe (Jev) and System One are
decision prefixes. System One's documented examples are CLM and Laya. Clef is
not in that directory or in the installed package.

The host reserves budget, writes one journal row per attempt, runs a tool
only when asked, and retries only up to the count it was given. It does not
import Pydantic AI, start an agent loop, or use Pydantic Graph.
"""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

CATALOG_VERSION = "2.54.0"
PROFILE_FLAGS = (
    "supports_tools",
    "supports_text_output",
    "supports_json_schema_output",
    "supports_json_object_output",
)
# Same four flags decision_model_profile sets in pydantic_ai 2.54.0.
DECISION_PROFILE = {
    "supports_tools": True,
    "supports_text_output": False,
    "supports_json_schema_output": False,
    "supports_json_object_output": False,
}
NATIVE = {
    "ollama": {
        "kind": "generation",
        "prefix": "ollama:",
        "importable": True,
        "reason": "OllamaModel imports in 2.54.0. This adapter does not construct it.",
    },
    "typesafe": {
        "kind": "decision",
        "prefix": "typesafe:",
        "importable": False,
        "reason": "TypeSafeModel requires the typesafe-sdk extra, which is not installed.",
    },
    "system-one": {
        "kind": "decision",
        "prefix": "system-one:",
        "importable": True,
        "models": ("clm-latest", "laya"),
        "reason": "SystemOneModel imports in 2.54.0. This adapter does not open HTTP.",
    },
}


class Usage(BaseModel):
    """Token counts the provider actually reported. Absent counts stay null."""

    model_config = ConfigDict(extra="forbid", strict=True)
    input_tokens: int | None = None
    output_tokens: int | None = None
    requests: int | None = None


class AgentConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    kind: Literal["generation", "decision"]
    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)


class GenerationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    invocation_id: str = Field(min_length=1)
    node_id: str = Field(min_length=1)
    instruction: str = Field(min_length=1)
    output_mode: Literal["text", "json_schema", "tool"]
    tool_name: str | None = None
    payload: dict[str, str] = Field(default_factory=dict)


class DecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    invocation_id: str = Field(min_length=1)
    node_id: str = Field(min_length=1)
    question: str = Field(min_length=1)
    options: list[str] = Field(min_length=1)
    state: dict[str, str] = Field(default_factory=dict)


class StructuredError(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    kind: Literal["capability", "budget", "provider", "schema", "tool"]
    message: str
    path: str


class AgentResult(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    ok: bool
    output: dict[str, Any] | None = None
    usage: Usage
    error: StructuredError | None = None


class Completion(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    output: dict[str, Any]
    usage: Usage = Field(default_factory=Usage)


class GenerationProvider(Protocol):
    name: str

    def profile(self) -> dict[str, bool]: ...

    def complete(self, instruction: str, payload: dict[str, str]) -> Completion: ...


class DecisionProvider(Protocol):
    name: str

    def profile(self) -> dict[str, bool]: ...

    def decide(self, question: str, options: list[str], state: dict[str, str]) -> Completion: ...


class Budget(Protocol):
    def reserve(self, key: str, amount: Decimal) -> bool: ...


class Journal(Protocol):
    def append(self, record: dict[str, Any]) -> None: ...


def decision_status(provider: str, model: str) -> Literal["mockable", "absent"]:
    """Whether the overview documents this decision pair.

    ``mockable`` does not mean the native client is called. Clef is absent.
    """

    if provider == "clef" or model == "clef":
        return "absent"
    if provider == "typesafe" and model.startswith("jev"):
        return "mockable"
    if provider == "system-one" and model in {"clm-latest", "laya"}:
        return "mockable"
    return "absent"


def _record(invocation_id: str, node_id: str, result: AgentResult) -> dict[str, Any]:
    error = None if result.error is None else result.error.model_dump()
    value = None if result.output is None else {"output": result.output, "usage": result.usage.model_dump()}
    return {
        "invocation_id": invocation_id,
        "node_id": node_id,
        "ok": result.ok,
        "value": value,
        "error": error,
    }


class AgentHost:
    """One explicit invocation. The caller supplies the provider object."""

    def __init__(self, *, budget: Budget, journal: Journal, retries: int = 0) -> None:
        if retries < 0:
            raise ValueError("retries must be zero or greater")
        self.budget = budget
        self.journal = journal
        self.retries = retries

    def generate(
        self,
        config: AgentConfig,
        request: GenerationRequest,
        provider: GenerationProvider,
        *,
        amount: Decimal,
        native: bool = False,
    ) -> AgentResult:
        if config.kind != "generation":
            return self._fail(request, "provider", "configuration is not a generation provider", "kind")
        if provider.name != config.provider:
            return self._fail(request, "provider", "provider object does not match configuration", "provider")
        if native:
            reason = NATIVE.get(config.provider, {}).get("reason", "no native client is constructed")
            return self._fail(request, "provider", str(reason), "native")
        required = {
            "text": "supports_text_output",
            "json_schema": "supports_json_schema_output",
            "tool": "supports_tools",
        }[request.output_mode]
        if not provider.profile().get(required, False):
            return self._fail(request, "capability", f"profile lacks {required}", required)
        if request.output_mode == "tool" and not request.tool_name:
            return self._fail(request, "schema", "tool mode needs a tool name", "tool_name")
        return self._attempts(
            request,
            amount,
            lambda: provider.complete(request.instruction, request.payload),
        )

    def decide(
        self,
        config: AgentConfig,
        request: DecisionRequest,
        provider: DecisionProvider,
        *,
        amount: Decimal,
        native: bool = False,
    ) -> AgentResult:
        if config.kind != "decision":
            return self._fail(request, "provider", "configuration is not a decision provider", "kind")
        if decision_status(config.provider, config.model) != "mockable":
            return self._fail(
                request,
                "provider",
                f"{config.provider}:{config.model} is not a documented decision model",
                "model",
            )
        if native:
            return self._fail(request, "provider", str(NATIVE[config.provider]["reason"]), "native")
        if provider.name != config.provider:
            return self._fail(request, "provider", "provider object does not match configuration", "provider")
        profile = provider.profile()
        if profile.get("supports_text_output", True):
            return self._fail(request, "capability", "a decision profile does not generate text", "supports_text_output")
        if not profile.get("supports_tools", False):
            return self._fail(request, "capability", "a decision profile needs tool-mode questions", "supports_tools")
        return self._attempts(
            request,
            amount,
            lambda: provider.decide(request.question, request.options, request.state),
        )

    def call_tool(
        self,
        *,
        invocation_id: str,
        node_id: str,
        name: str,
        payload: dict[str, str],
        tools: dict[str, Callable[[dict[str, str]], dict[str, Any]]],
        amount: Decimal,
    ) -> AgentResult:
        request = GenerationRequest(
            invocation_id=invocation_id,
            node_id=node_id,
            instruction=name,
            output_mode="tool",
            tool_name=name,
            payload=payload,
        )
        if name not in tools:
            return self._fail(request, "tool", f"unknown tool {name}", "tool")
        if not self.budget.reserve(node_id, amount):
            return self._fail(request, "budget", "reservation refused", "budget")
        try:
            output = tools[name](payload)
        except Exception as exc:  # noqa: BLE001
            return self._fail(request, "tool", type(exc).__name__, "tool")
        result = AgentResult(ok=True, output=output, usage=Usage())
        self.journal.append(_record(invocation_id, node_id, result))
        return result

    def _attempts(self, request: GenerationRequest | DecisionRequest, amount: Decimal, call: Callable[[], Completion]) -> AgentResult:
        attempts = self.retries + 1
        result: AgentResult | None = None
        for _ in range(attempts):
            if not self.budget.reserve(request.node_id, amount):
                return self._fail(request, "budget", "reservation refused", "budget")
            try:
                completion = call()
            except Exception as exc:  # noqa: BLE001
                result = self._fail(request, "provider", type(exc).__name__, "provider")
                continue
            result = AgentResult(ok=True, output=completion.output, usage=completion.usage)
            self.journal.append(_record(request.invocation_id, request.node_id, result))
            return result
        if result is None:
            raise RuntimeError("invocation was not attempted")
        return result

    def _fail(
        self,
        request: GenerationRequest | DecisionRequest,
        kind: Literal["capability", "budget", "provider", "schema", "tool"],
        message: str,
        path: str,
    ) -> AgentResult:
        result = AgentResult(
            ok=False,
            usage=Usage(),
            error=StructuredError(kind=kind, message=message, path=path),
        )
        self.journal.append(_record(request.invocation_id, request.node_id, result))
        return result
