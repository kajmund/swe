"""Shared test helpers for the microtask-swarm pilot."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Mapping

from microtask_swarm.contracts import Kind
from microtask_swarm.executor import ChatRequest, ProviderResponse
from microtask_swarm.fixtures import TaskSpec

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config" / "experiment.json"


class ScriptedProvider:
    """Deterministic provider keyed by (task_id, model_id, attempt)."""

    def __init__(
        self,
        responses: Mapping[tuple[str, str, int], Any],
        *,
        default: Any = None,
        delay_ms: float = 0.0,
        delay_by_model: Mapping[str, float] | None = None,
    ) -> None:
        self.responses = dict(responses)
        self.default = default
        self.delay_ms = delay_ms
        self.delay_by_model = dict(delay_by_model or {})
        self.calls: list[ChatRequest] = []

    async def chat(self, request: ChatRequest) -> ProviderResponse:
        self.calls.append(request)
        delay = self.delay_by_model.get(request.model_id, self.delay_ms)
        if delay > 0:
            await asyncio.sleep(delay / 1000.0)
        value = self.responses.get(
            (request.task_id, request.model_id, request.attempt),
            self.default,
        )
        if callable(value):
            value = value(request)
        if isinstance(value, BaseException):
            raise value
        if isinstance(value, ProviderResponse):
            return value
        if value is None:
            value = {}
        if isinstance(value, str):
            content = value
        else:
            content = json.dumps(value, sort_keys=True)
        return ProviderResponse(
            content=content,
            tokens={
                "input": 100,
                "output": 50,
                "reasoning": 0,
                "cache_read": 50,
                "cache_write": 0,
                "source": "provider",
            },
            billed_usd=None,
            price_period="peak",
        )


def spec(
    kind: Kind,
    *,
    input_data: Mapping[str, Any],
    gold: Mapping[str, Any],
    passage: str,
    task_id: str = "task-1",
) -> TaskSpec:
    return TaskSpec(
        task_id=task_id,
        item_id="item-1",
        domain="test",
        kind=kind,
        input_data=dict(input_data),
        gold=dict(gold),
        passage=passage,
        question="question",
    )


def response(**values: Any) -> ProviderResponse:
    return ProviderResponse(
        content=json.dumps(values, sort_keys=True),
        tokens={
            "input": 100,
            "output": 50,
            "reasoning": 0,
            "cache_read": 50,
            "cache_write": 0,
            "source": "provider",
        },
        billed_usd=None,
        price_period="peak",
    )


def load_test_config() -> dict[str, Any]:
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


__all__ = [
    "CONFIG_PATH",
    "ROOT",
    "ScriptedProvider",
    "load_test_config",
    "response",
    "spec",
]
