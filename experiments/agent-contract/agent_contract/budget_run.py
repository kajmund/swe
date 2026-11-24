"""Attach the merged runtime budget to one WorkflowRunner call.

The production tariff belongs to the microtask-swarm policy. This module
injects a fixed one-milli policy so the agent adapter can show the dispatch
boundary. It does not price tokens and it does not call a provider.
"""

from __future__ import annotations

from swe.budget import BudgetOutcome, BudgetRequest, NativeBudgetLedger, RuntimeBudget
from swe.run_store import WorkflowResources

ACCOUNT = "agent-contract"
MILLI = 1


class InjectedPolicy:
    """One milli-unit per successful handler call. Not the swarm tariff."""

    def reserve(self, request: BudgetRequest) -> int:
        return MILLI

    def settle(self, request: BudgetRequest, outcome: BudgetOutcome) -> int:
        return MILLI if outcome.ok else 0


def open_budget(run_id: str, calls: int) -> RuntimeBudget:
    if calls < 1:
        raise ValueError("a budgeted run needs at least one call")
    ledger = NativeBudgetLedger()
    ledger.allocate(
        run_id=run_id,
        account=ACCOUNT,
        amount_milli=calls * MILLI,
        key=f"{run_id}:allocate",
    )
    return RuntimeBudget(ledger, InjectedPolicy(), account=ACCOUNT)


def resources_for(budget: RuntimeBudget) -> WorkflowResources:
    return WorkflowResources(budget=budget)


def budget_trace(budget: RuntimeBudget) -> dict[str, object]:
    balance = dict(budget.balance())
    return {
        "attached": True,
        "account": budget.account_name,
        "policy": "injected-fixed-milli",
        "spent_milli": balance.get("spent"),
        "reserved_milli": balance.get("reserved"),
        "available_milli": balance.get("available"),
        "events": len(budget.events()),
    }
