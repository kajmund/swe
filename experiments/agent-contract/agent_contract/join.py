"""Explicit invocations for the agent contract. Not a scheduler.

This module has no dispatcher. ``require_swe_dispatch`` stays closed.
SWE's ordered default compiles a microtask DAG into one SWD map per
topological layer (``docs/specs/swe-microtasks.md``). ``schedule="ready"``
emits one SWD 1.1 map and admits a task when its own dependencies finish
(``docs/swe-standalone/18-ready-map-decision.md`` section 7).
``WorkflowRunner`` is that dispatch boundary and it requires ``swe_native``.
This module does not replace that machine and does not add another scheduler.

PR #11 is merged. A run attaches ``RuntimeBudget`` through
``WorkflowResources`` at that boundary. This module does not construct one.
"""

from __future__ import annotations

from typing import Any

from agent_contract.offline_adapter import AgentConfig

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
    """This package has no dispatcher. The call stays closed."""


def probe_swe_ready() -> bool:
    """Whether this interpreter's compiler accepts ``schedule="ready"``.

    A missing compiler or a rejected schedule is false. This is not a static
    claim about a checkout.
    """

    try:
        from swe.microtasks import Microtask, MicrotaskError, compile_microtasks
    except ImportError:
        return False
    try:
        batch = compile_microtasks(
            [Microtask("probe")],
            handler="probe.ready",
            schedule="ready",
        )
    except (MicrotaskError, TypeError):
        return False
    return batch.schedule == "ready" and batch.map_nodes == ("microtasks",)


def runtime_budget_status() -> dict[str, Any]:
    try:
        from swe import budget
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
        "package_dispatcher": False,
        "fail_closed": True,
        "swe_ready": probe_swe_ready(),
        "swe_ordered_default": "layer_map",
        "swe_native": native,
        "ordered_spec": "docs/specs/swe-microtasks.md",
        "ready_spec": "docs/swe-standalone/18-ready-map-decision.md",
        "reason": (
            "This package has no dispatcher, so require_swe_dispatch stays closed. "
            'SWE schedule="ready" emits one SWD 1.1 map and admits a task when its own dependencies finish. '
            "The ordered default emits one map node per topological layer. "
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
    if kind not in KINDS:
        return "kind is outside the planned kinds"
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
