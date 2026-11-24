"""Typed records shared by the dispatcher, scorer, and reporter."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from microtask_swarm.contracts import Verification


@dataclass(frozen=True, slots=True)
class CallRecord:
    model_id: str
    role: str
    attempt: int
    latency_ms: float
    tokens: dict[str, Any]
    billed_usd: str | None = None
    price_period: str | None = None
    error: str | None = None
    cancelled: bool = False
    output: Any = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "model_id": self.model_id,
            "role": self.role,
            "attempt": self.attempt,
            "latency_ms": round(self.latency_ms, 3),
            "tokens": dict(self.tokens),
            "billed_usd": self.billed_usd,
            "price_period": self.price_period,
            "error": self.error,
            "cancelled": self.cancelled,
        }


@dataclass(frozen=True, slots=True)
class TaskResult:
    task_id: str
    kind: str
    output: Any = None
    verification: Verification | None = None
    error: str | None = None
    selected_models: tuple[str, ...] = ()
    calls: tuple[CallRecord, ...] = field(default_factory=tuple)
    cancelled_candidates: int = 0
    early_cancelled: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "kind": self.kind,
            "output": self.output,
            "verification": None
            if self.verification is None
            else self.verification.to_dict(),
            "error": self.error,
            "selected_models": list(self.selected_models),
            "cancelled_candidates": self.cancelled_candidates,
            "early_cancelled": self.early_cancelled,
            "calls": [call.to_dict() for call in self.calls],
        }


__all__ = ["CallRecord", "TaskResult"]
