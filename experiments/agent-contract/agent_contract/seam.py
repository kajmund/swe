"""Seam between this plan and a future first-class ready DAG.

``experiment/barrier-free-scheduler`` owns the scheduler experiment. This
module does not import it and does not admit, order, or run tasks. The
predicate below is the migration check: a task is eligible when every
dependency id is complete. Today's SWE map still waits for the whole layer.

The field names match the proposed SWD map mode in
``docs/analysis/2026-11-24-barrier-free-scheduler.md`` on that branch.
``item_depends_on`` admits a task. It does not put the dependency result in
the handler input. That value channel is a separate contract. ``RuntimeBudget``
stays unattached until PR 11 is merged.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from agent_contract.join import plan_invocations
from hy4_research_policy.agent_adapter import AgentConfig

FIRST_CLASS_READY_FIELDS = ("schedule", "item_id", "item_depends_on")
READY_MAP_PROPOSAL = {
    "type": "map",
    "items": "$.input.tasks",
    "schedule": "ready",
    "item_id": "$.item.task_id",
    "item_depends_on": "$.item.depends_on",
}
HANDLER_INPUT_KEYS = ("task_id", "payload", "depends_on")
VALUE_CHANNEL = "absent"


def migration_items(generation: AgentConfig, decision: AgentConfig) -> list[dict[str, Any]]:
    """Static items a future ready map would receive. No scheduler state."""

    items = []
    for row in plan_invocations(generation, decision):
        items.append(
            {
                "task_id": row["task_id"],
                "depends_on": list(row["depends_on"]),
                "role": row["role"],
                "kind": row["kind"],
                "provider": row["provider"],
                "model": row["model"],
            }
        )
    return items


def ready_ids(items: Sequence[Mapping[str, Any]], completed: set[str]) -> list[str]:
    """Eligible task ids in input order. This does not start work."""

    eligible = []
    for item in items:
        task_id = str(item["task_id"])
        if task_id in completed:
            continue
        dependencies = item["depends_on"]
        if all(dependency in completed for dependency in dependencies):
            eligible.append(task_id)
    return eligible


def layer_is_complete(layer: Sequence[str], completed: set[str]) -> bool:
    """The current map barrier: the next layer waits for every id in this one."""

    return all(task_id in completed for task_id in layer)
