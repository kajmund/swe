"""Fork/map branch completion: every branch ends at its owning join."""

from __future__ import annotations

from collections import deque
from typing import Any

from swe.errors import SwdValidationError

NodeMap = dict[str, dict[str, Any]]
EdgeMap = dict[str, set[str]]


def validate_join_structure(nodes: NodeMap, edges: EdgeMap) -> EdgeMap:
    """Require join entry only from owning branches; add completion edges."""
    owners = _join_owners(nodes)
    completed = {node_id: set(targets) for node_id, targets in edges.items()}
    for join_id, owner_id in owners.items():
        owner = nodes[owner_id]
        starts = _branch_starts(owner_id, owner)
        members = _branch_members(
            owner_id, starts, join_id, edges=edges, nodes=nodes
        )
        _add_completion_edges(
            members, join_id, completed=completed, explicit=edges
        )
        _assert_join_not_escaped(owner_id, join_id, members, completed)
        _assert_join_entry(join_id, owner_id, members, completed)
        _assert_branch_starts_entry(owner_id, starts, edges)
    _complete_joinless_maps(nodes, edges, completed)
    return completed


def _complete_joinless_maps(nodes: NodeMap, explicit: EdgeMap, completed: EdgeMap) -> None:
    for node_id, node in nodes.items():
        if node["type"] != "map" or isinstance(node.get("join"), str):
            continue
        nxt = node.get("next")
        if not isinstance(nxt, str):
            continue
        members = _collect_branch(
            node["child"], nxt, explicit, nodes, owner_id=node_id
        )
        _add_completion_edges(
            members, nxt, completed=completed, explicit=explicit
        )
        _assert_join_not_escaped(node_id, nxt, members, completed)


def _join_owners(nodes: NodeMap) -> dict[str, str]:
    owners: dict[str, str] = {}
    for node_id, node in nodes.items():
        if node["type"] not in {"fork", "map"}:
            continue
        join_id = node.get("join")
        if not isinstance(join_id, str):
            continue
        join_node = nodes.get(join_id)
        if join_node is None or join_node["type"] != "join":
            raise SwdValidationError(
                "join reference must target a join node",
                path=f"nodes.{node_id}.join",
            )
        previous = owners.get(join_id)
        if previous is not None:
            raise SwdValidationError(
                f"join {join_id!r} is owned by both {previous!r} and {node_id!r}",
                path=f"nodes.{node_id}.join",
            )
        owners[join_id] = node_id
    for node_id, node in nodes.items():
        if node["type"] == "join" and node_id not in owners:
            raise SwdValidationError(
                "join must be referenced by exactly one fork or map",
                path=f"nodes.{node_id}",
            )
    return owners


def _branch_starts(owner_id: str, owner: dict[str, Any]) -> list[str]:
    if owner["type"] == "fork":
        starts = list(owner["branches"].values())
    else:
        starts = [owner["child"]]
    if len(starts) != len(set(starts)):
        raise SwdValidationError(
            "branch starts must be unique",
            path=f"nodes.{owner_id}",
        )
    return starts


def _branch_members(
    owner_id: str,
    starts: list[str],
    join_id: str,
    *,
    edges: EdgeMap,
    nodes: NodeMap,
) -> set[str]:
    members: set[str] = set()
    for start in starts:
        branch = _collect_branch(start, join_id, edges, nodes, owner_id=owner_id)
        overlap = members & branch
        if overlap:
            raise SwdValidationError(
                f"fork/map branches share nodes {', '.join(sorted(overlap))}",
                path=f"nodes.{owner_id}",
            )
        members.update(branch)
    return members


def _collect_branch(
    start: str,
    join_id: str,
    edges: EdgeMap,
    nodes: NodeMap,
    *,
    owner_id: str,
) -> set[str]:
    if start == join_id:
        raise SwdValidationError(
            "branch cannot start at its join",
            path=f"nodes.{owner_id}",
        )
    seen: set[str] = set()
    queue = deque([start])
    reached_join = False
    while queue:
        node_id = queue.popleft()
        if node_id == join_id:
            reached_join = True
            continue
        if node_id in seen:
            continue
        seen.add(node_id)
        reached_join = _walk_branch_node(
            node_id,
            join_id,
            owner_id=owner_id,
            edges=edges,
            nodes=nodes,
            queue=queue,
        ) or reached_join
    if not reached_join:
        raise SwdValidationError(
            f"branch {start!r} never completes at join {join_id!r}",
            path=f"nodes.{owner_id}",
        )
    return seen


def _walk_branch_node(
    node_id: str,
    join_id: str,
    *,
    owner_id: str,
    edges: EdgeMap,
    nodes: NodeMap,
    queue: deque[str],
) -> bool:
    node = nodes[node_id]
    if node["type"] == "end":
        raise SwdValidationError(
            f"branch must complete at join {join_id!r}, not terminate at {node_id!r}",
            path=f"nodes.{node_id}",
        )
    nested_exit = None if node_id == owner_id else _nested_continuation(node)
    if nested_exit is not None:
        if nested_exit == join_id:
            return True
        queue.append(nested_exit)
        return False
    outgoing = edges.get(node_id, set())
    if not outgoing:
        return True
    reached_join = False
    for target in outgoing:
        if target == join_id:
            reached_join = True
            continue
        queue.append(target)
    return reached_join


def _nested_continuation(node: dict[str, Any]) -> str | None:
    if node["type"] not in {"fork", "map"}:
        return None
    join_id = node.get("join")
    if isinstance(join_id, str):
        return join_id
    nxt = node.get("next")
    return nxt if isinstance(nxt, str) else None


def _add_completion_edges(
    members: set[str],
    join_id: str,
    *,
    completed: EdgeMap,
    explicit: EdgeMap,
) -> None:
    for node_id in members:
        if explicit.get(node_id):
            continue
        completed.setdefault(node_id, set()).add(join_id)


def _assert_join_not_escaped(
    owner_id: str,
    join_id: str,
    members: set[str],
    edges: EdgeMap,
) -> None:
    after_join = _walk_until(edges, join_id, stop_at=owner_id)
    escaped = sorted(members & after_join - {join_id})
    if escaped:
        raise SwdValidationError(
            f"branch escapes join {join_id!r} through {', '.join(escaped)}",
            path=f"nodes.{owner_id}",
        )


def _assert_join_entry(
    join_id: str,
    owner_id: str,
    members: set[str],
    edges: EdgeMap,
) -> None:
    incoming = sorted(
        source for source, targets in edges.items() if join_id in targets
    )
    illegal = [source for source in incoming if source not in members]
    if illegal:
        raise SwdValidationError(
            f"join {join_id!r} may only be entered from owning fork/map "
            f"{owner_id!r} branches",
            path=f"nodes.{join_id}",
        )
    if not incoming:
        raise SwdValidationError(
            f"join {join_id!r} is never completed by a branch",
            path=f"nodes.{join_id}",
        )


def _assert_branch_starts_entry(
    owner_id: str,
    starts: list[str],
    edges: EdgeMap,
) -> None:
    allowed = {owner_id, *starts}
    for start in starts:
        incoming = [source for source, targets in edges.items() if start in targets]
        extra = [source for source in incoming if source not in allowed]
        if extra:
            raise SwdValidationError(
                f"branch start {start!r} may only be entered from {owner_id!r}",
                path=f"nodes.{start}",
            )


def _walk_until(edges: EdgeMap, start: str, *, stop_at: str) -> set[str]:
    seen: set[str] = set()
    queue = deque([start])
    while queue:
        node_id = queue.popleft()
        if node_id == stop_at or node_id in seen:
            continue
        seen.add(node_id)
        queue.extend(edges.get(node_id, ()))
    return seen
