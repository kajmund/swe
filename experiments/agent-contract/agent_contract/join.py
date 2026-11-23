"""One host run: configured model, async microtask layers, explicit checks.

The runner retries nothing. A validator rejection is a semantic error and does
not call the provider again. The ledger methods match PR #11
(``reserve`` / ``settle`` / ``release``). This module does not construct that
ledger and does not keep a second one.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Any, Awaitable, Callable, Protocol

from hy4_research_policy.agent_adapter import (
    AgentConfig,
    AgentHost,
    AgentResult,
    DecisionRequest,
    GenerationRequest,
)

LAYERS: tuple[tuple[str, ...], ...] = (
    ("relevance",),
    ("extraction",),
    ("entailment", "atomicity"),
    ("operation",),
)
DECISION_TASKS = frozenset({"entailment", "atomicity"})
MILLI = Decimal(1)


class BudgetPending(RuntimeError):
    """PR #11 is not merged, so no ledger is attached."""


class Ledger(Protocol):
    def reserve(self, *, run_id: str, account: str, amount_milli: int, key: str) -> int: ...

    def settle(self, *, reservation_id: int, spent_milli: int, key: str) -> None: ...

    def release(self, *, reservation_id: int, key: str) -> None: ...


class Journal(Protocol):
    def append(self, record: dict[str, Any]) -> None: ...


def runtime_budget_status() -> dict[str, Any]:
    try:
        import swe.budget as budget
    except ImportError:
        return {"attached": False, "reason": "PR 11 is open and not merged"}
    if not hasattr(budget, "RuntimeBudget"):
        return {"attached": False, "reason": "RuntimeBudget is absent"}
    return {"attached": True, "reason": "swe.budget.RuntimeBudget"}


class _HostBudget:
    def __init__(self, ledger: Ledger, run_id: str) -> None:
        self.ledger = ledger
        self.run_id = run_id
        self.ids: dict[str, int] = {}

    def reserve(self, key: str, amount: Decimal) -> bool:
        try:
            reservation_id = self.ledger.reserve(
                run_id=self.run_id,
                account="experiment",
                amount_milli=int(amount),
                key=key,
            )
        except Exception:
            return False
        self.ids[key] = reservation_id
        return True


def validate(kind: str, output: dict[str, Any] | None) -> str | None:
    """Return a semantic error message, or None when the output is acceptable."""

    if not isinstance(output, dict):
        return "output is not an object"
    if kind == "relevance" and not isinstance(output.get("relevant"), bool):
        return "relevant must be a boolean"
    if kind == "extraction" and not isinstance(output.get("quotes"), list):
        return "quotes must be a list"
    if kind == "entailment" and output.get("choice") not in {"entailment", "contradiction", "neutral"}:
        return "choice is outside the entailment labels"
    if kind == "atomicity" and not isinstance(output.get("atomic"), bool):
        return "atomic must be a boolean"
    if kind == "operation" and not isinstance(output.get("operation"), str):
        return "operation must be a string"
    return None


async def run_join(
    *,
    run_id: str,
    generation: AgentConfig,
    decision: AgentConfig,
    generation_provider: Any,
    decision_provider: Any,
    ledger: Ledger | None,
    journal: Journal,
    before_call: Callable[[str], Awaitable[None]] | None = None,
) -> dict[str, Any]:
    if ledger is None:
        raise BudgetPending(runtime_budget_status()["reason"])
    host_budget = _HostBudget(ledger, run_id)
    host = AgentHost(budget=host_budget, journal=journal, retries=0)
    failed: set[str] = set()
    results: dict[str, dict[str, Any]] = {}
    for layer in LAYERS:
        await asyncio.gather(
            *[
                _task(
                    kind,
                    host=host,
                    host_budget=host_budget,
                    ledger=ledger,
                    generation=generation,
                    decision=decision,
                    generation_provider=generation_provider,
                    decision_provider=decision_provider,
                    failed=failed,
                    results=results,
                    before_call=before_call,
                )
                for kind in layer
            ]
        )
    return {"results": results, "retries": 0}


async def _task(
    kind: str,
    *,
    host: AgentHost,
    host_budget: _HostBudget,
    ledger: Ledger,
    generation: AgentConfig,
    decision: AgentConfig,
    generation_provider: Any,
    decision_provider: Any,
    failed: set[str],
    results: dict[str, dict[str, Any]],
    before_call: Callable[[str], Awaitable[None]] | None,
) -> None:
    dependencies = {
        "relevance": (),
        "extraction": ("relevance",),
        "entailment": ("extraction",),
        "atomicity": ("extraction",),
        "operation": ("entailment", "atomicity"),
    }[kind]
    if any(name in failed for name in dependencies):
        results[kind] = {"ok": False, "error": "dependency", "called": False}
        failed.add(kind)
        return
    if before_call is not None:
        await before_call(kind)
    if kind in DECISION_TASKS:
        result = host.decide(
            decision,
            DecisionRequest(
                invocation_id=kind,
                node_id=kind,
                question=kind,
                options=["entailment", "contradiction", "neutral"] if kind == "entailment" else ["yes", "no"],
                state={},
            ),
            decision_provider,
            amount=MILLI,
        )
    else:
        result = host.generate(
            generation,
            GenerationRequest(
                invocation_id=kind,
                node_id=kind,
                instruction=kind,
                output_mode="text",
            ),
            generation_provider,
            amount=MILLI,
        )
    _finish(kind, result, host_budget, ledger, failed, results)


def _finish(
    kind: str,
    result: AgentResult,
    host_budget: _HostBudget,
    ledger: Ledger,
    failed: set[str],
    results: dict[str, dict[str, Any]],
) -> None:
    reservation_id = host_budget.ids.get(kind)
    if reservation_id is None:
        error = result.error.kind if result.error is not None else "budget"
        results[kind] = {"ok": False, "error": error, "called": False}
        failed.add(kind)
        return
    if not result.ok:
        ledger.settle(reservation_id=reservation_id, spent_milli=0, key=kind)
        results[kind] = {"ok": False, "error": result.error.kind if result.error else "provider", "called": True}
        failed.add(kind)
        return
    message = validate(kind, result.output)
    ledger.settle(reservation_id=reservation_id, spent_milli=1 if message is None else 0, key=kind)
    if message is not None:
        results[kind] = {"ok": False, "error": "semantic", "detail": message, "called": True}
        failed.add(kind)
        return
    results[kind] = {"ok": True, "output": result.output, "called": True}
