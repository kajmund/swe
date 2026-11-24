"""Explicit invocations for the agent contract. Not a scheduler.

SWE compiles a microtask DAG into SWD map layers. A later layer starts only
after every task in the previous layer finishes. There is no event-ready
mode, where a task would start as soon as its own dependencies finish.
``WorkflowRunner`` is the dispatch boundary and it requires ``swe_native``.
This module does not replace that machine.

PR #11 is merged. A run attaches ``RuntimeBudget`` through
``WorkflowResources`` at that boundary. This module does not construct one.
"""

from __future__ import annotations

from typing import Any

from hy4_research_policy.agent_adapter import AgentConfig

KINDS: tuple[str, ...] = (
    "relevance",
    "extraction",
    "entailment",
    "atomicity",
    "operation",
)
DECISION_TASKS = frozenset({"entailment", "atomicity"})
_DEPENDENCIES = {
    "relevance": (),
    "extraction": ("relevance",),
    "entailment": ("extraction",),
    "atomicity": ("extraction",),
    "operation": ("entailment", "atomicity"),
}


class DispatchGap(RuntimeError):
    """SWE cannot run this plan on the invocation boundary from here."""


def runtime_budget_status() -> dict[str, Any]:
    try:
        import swe.budget as budget
    except ImportError:
        return {
            "available": False,
            "attached": False,
            "reason": "swe.budget is not importable",
        }
    if not hasattr(budget, "RuntimeBudget"):
        return {
            "available": False,
            "attached": False,
            "reason": "RuntimeBudget is absent",
        }
    return {
        "available": True,
        "attached": False,
        "reason": "RuntimeBudget attaches per run through WorkflowResources",
    }


def dispatch_gap() -> dict[str, Any]:
    native = True
    try:
        import swe_native  # noqa: F401
    except ImportError:
        native = False
    return {
        "event_ready": False,
        "swe_dispatch": "layer_map",
        "swe_native": native,
        "spec": "docs/specs/swe-microtasks.md",
        "reason": (
            "compile_microtasks emits one map node per topological layer. "
            "A task waits for the whole layer, not only for its own dependencies. "
            "WorkflowRunner is the dispatch boundary and needs swe_native."
        ),
        "runtime_budget": runtime_budget_status(),
    }


def plan_invocations(generation: AgentConfig, decision: AgentConfig) -> list[dict[str, Any]]:
    """One generation invocation and one validator invocation per semantic kind."""

    if generation.kind != "generation" or decision.kind != "decision":
        raise ValueError("model choice is the pair of configurations")
    rows: list[dict[str, Any]] = []
    for kind in KINDS:
        config = decision if kind in DECISION_TASKS else generation
        generate_id = f"{kind}.generate"
        validate_id = f"{kind}.validate"
        rows.append(
            {
                "task_id": generate_id,
                "role": "decision" if kind in DECISION_TASKS else "generation",
                "kind": kind,
                "provider": config.provider,
                "model": config.model,
                "depends_on": [f"{name}.validate" for name in _DEPENDENCIES[kind]],
            }
        )
        rows.append(
            {
                "task_id": validate_id,
                "role": "validator",
                "kind": kind,
                "provider": config.provider,
                "model": config.model,
                "depends_on": [generate_id],
            }
        )
    return rows


def require_swe_dispatch() -> None:
    """Refuse a local runner. SWE's machine is the only dispatcher."""

    raise DispatchGap(dispatch_gap()["reason"])


def validate(kind: str, output: dict[str, Any] | None) -> str | None:
    """Explicit semantic check. It does not call a model and does not retry."""

    if not isinstance(output, dict):
        return "output is not an object"
    if kind == "relevance" and not isinstance(output.get("relevant"), bool):
        return "relevant must be a boolean"
    if kind == "extraction" and not isinstance(output.get("quotes"), list):
        return "quotes must be a list"
    choice = output.get("choice")
    if kind == "entailment" and (
        not isinstance(choice, str) or choice not in {"entailment", "contradiction", "neutral"}
    ):
        return "choice is outside the entailment labels"
    if kind == "atomicity" and not isinstance(output.get("atomic"), bool):
        return "atomic must be a boolean"
    if kind == "operation" and not isinstance(output.get("operation"), str):
        return "operation must be a string"
    return None
