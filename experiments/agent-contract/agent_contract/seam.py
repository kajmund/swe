"""Seam for the merged ready map. This module does not run it.

``origin/main`` ``f1ae2b8`` compiles ``schedule="ready"`` as one SWD 1.1 map.
The preflight calls that compiler. This module does not import a scheduler
and does not admit, order, or run tasks. ``ready_ids`` only names the
eligibility rule: a task is eligible when every dependency id is complete.
The ordered layer map still waits for the whole layer.

``item_depends_on`` admits a task. It does not put the dependency result in
the handler input. That value channel stays a separate contract.
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
