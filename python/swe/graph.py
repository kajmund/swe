"""Semantic graph checks that JSON Schema cannot express."""

from __future__ import annotations

from collections import defaultdict, deque
from typing import Any

from swe.errors import SwdValidationError
from swe.joins import validate_join_structure
from swe.schema import NODE_TYPES

NodeMap = dict[str, dict[str, Any]]
EdgeMap = dict[str, set[str]]


def assert_known_node_types(document: dict[str, Any]) -> None:
    _assert_node_objects(document.get("nodes"))


def validate_graph(document: dict[str, Any]) -> None:
    nodes = document["nodes"]
    assert_known_node_types(document)
    entrypoint = document["entrypoint"]
    _require_node(nodes, entrypoint, path="entrypoint")
    edges = _explicit_edges(nodes)
    _check_decision_routes(nodes)
    edges = validate_join_structure(nodes, edges)
    _check_reachability(nodes, edges, entrypoint)
    _check_end_reachable(nodes, edges, entrypoint)


def _assert_node_objects(nodes: object) -> None:
    if not isinstance(nodes, dict) or not nodes:
        raise SwdValidationError("nodes must be a non-empty object", path="nodes")
    for node_id, node in nodes.items():
        if not isinstance(node, dict) or "type" not in node:
            raise SwdValidationError("node must be an object with type", path=f"nodes.{node_id}")
        node_type = node["type"]
        if node_type not in NODE_TYPES:
            raise SwdValidationError(
                f"unknown node type {node_type!r}",
                path=f"nodes.{node_id}.type",
            )


def _require_node(nodes: NodeMap, node_id: str, *, path: str) -> dict[str, Any]:
    if node_id not in nodes:
        raise SwdValidationError(f"unknown node {node_id!r}", path=path)
    return nodes[node_id]


def _explicit_edges(nodes: NodeMap) -> EdgeMap:
    edges: EdgeMap = defaultdict(set)
    for node_id, node in nodes.items():
        for target, path in _successors(node_id, node):
            _require_node(nodes, target, path=path)
            edges[node_id].add(target)
    return edges


def _successors(node_id: str, node: dict[str, Any]) -> list[tuple[str, str]]:
    node_type = node["type"]
    if node_type == "end":
        if "next" in node:
            raise SwdValidationError("end nodes cannot have next", path=f"nodes.{node_id}.next")
        return []
    if node_type == "decision":
        return _route_targets(node_id, node)
    if node_type == "fork":
        return _fork_targets(node_id, node)
    if node_type == "map":
        return _map_targets(node_id, node)
    return _optional_next(node_id, node)


def _optional_next(node_id: str, node: dict[str, Any]) -> list[tuple[str, str]]:
    nxt = node.get("next")
    if nxt is None:
        return []
    if not isinstance(nxt, str):
        raise SwdValidationError("next must be a node id", path=f"nodes.{node_id}.next")
    return [(nxt, f"nodes.{node_id}.next")]


def _route_targets(node_id: str, node: dict[str, Any]) -> list[tuple[str, str]]:
    routes = node.get("routes")
    if not isinstance(routes, dict) or not routes:
        raise SwdValidationError("decision requires routes", path=f"nodes.{node_id}.routes")
    return [
        (target, f"nodes.{node_id}.routes.{choice}")
        for choice, target in routes.items()
        if _route_target(choice, target, node_id)
    ]


def _route_target(choice: object, target: object, node_id: str) -> str:
    if not isinstance(choice, str) or not isinstance(target, str):
        raise SwdValidationError(
            "routes must map strings to node ids",
            path=f"nodes.{node_id}.routes",
        )
    return target


def _fork_targets(node_id: str, node: dict[str, Any]) -> list[tuple[str, str]]:
    branches = node.get("branches")
    join_id = node.get("join")
    if not isinstance(branches, dict) or not branches:
        raise SwdValidationError("fork requires named branches", path=f"nodes.{node_id}.branches")
    if not isinstance(join_id, str):
        raise SwdValidationError("fork requires join", path=f"nodes.{node_id}.join")
    if join_id == node_id:
        raise SwdValidationError("fork cannot join to itself", path=f"nodes.{node_id}.join")
    return [
        (target, f"nodes.{node_id}.branches.{name}")
        for name, target in branches.items()
        if _branch_target(name, target, node_id)
    ]


def _branch_target(name: object, target: object, node_id: str) -> str:
    if not isinstance(name, str) or not isinstance(target, str):
        raise SwdValidationError(
            "fork branches must map names to node ids",
            path=f"nodes.{node_id}.branches",
        )
    return target


def _map_targets(node_id: str, node: dict[str, Any]) -> list[tuple[str, str]]:
    child = node.get("child")
    nxt = node.get("next")
    join_id = node.get("join")
    if not isinstance(child, str):
        raise SwdValidationError("map requires child", path=f"nodes.{node_id}.child")
    if isinstance(join_id, str) and isinstance(nxt, str):
        raise SwdValidationError(
            "map with join continues from the join, not next",
            path=f"nodes.{node_id}",
        )
    if not isinstance(join_id, str) and not isinstance(nxt, str):
        raise SwdValidationError("map requires join or next", path=f"nodes.{node_id}")
    return [(child, f"nodes.{node_id}.child")]


def _check_decision_routes(nodes: NodeMap) -> None:
    for node_id, node in nodes.items():
        if node["type"] != "decision":
            continue
        routes = node.get("routes")
        if not isinstance(routes, dict):
            continue
        evaluator = node.get("evaluator")
        if evaluator == "expression" and set(routes) != {"true", "false"}:
            raise SwdValidationError(
                "expression decisions require exactly true and false routes",
                path=f"nodes.{node_id}.routes",
            )
        if evaluator == "provider":
            _check_provider_routes(node_id, node, routes)


def _check_provider_routes(node_id: str, node: dict[str, Any], routes: dict[str, Any]) -> None:
    choices = node.get("choices")
    if not isinstance(choices, list) or not choices:
        raise SwdValidationError("provider decision requires choices", path=f"nodes.{node_id}.choices")
    missing = [choice for choice in choices if choice not in routes]
    extra = [choice for choice in routes if choice not in choices]
    if missing or extra:
        raise SwdValidationError(
            "provider routes must match choices exactly",
            path=f"nodes.{node_id}.routes",
        )


def _check_reachability(nodes: NodeMap, edges: EdgeMap, entrypoint: str) -> None:
    reachable = _walk(edges, entrypoint)
    missing = sorted(set(nodes) - reachable)
    if missing:
        raise SwdValidationError(
            f"unreachable nodes: {', '.join(missing)}",
            path="nodes",
        )


def _check_end_reachable(nodes: NodeMap, edges: EdgeMap, entrypoint: str) -> None:
    ends = {node_id for node_id, node in nodes.items() if node["type"] == "end"}
    if not ends:
        raise SwdValidationError("definition requires at least one end node", path="nodes")
    reverse = _reverse_edges(nodes, edges)
    can_end: set[str] = set()
    for end_id in ends:
        can_end.update(_walk(reverse, end_id))
    stuck = sorted(set(_walk(edges, entrypoint)) - can_end)
    if stuck:
        raise SwdValidationError(
            f"non-terminal nodes never reach end: {', '.join(stuck)}",
            path="nodes",
        )


def _reverse_edges(nodes: NodeMap, edges: EdgeMap) -> EdgeMap:
    reverse: EdgeMap = defaultdict(set)
    for node_id in nodes:
        reverse.setdefault(node_id, set())
    for source, targets in edges.items():
        for target in targets:
            reverse[target].add(source)
    return reverse


def _walk(edges: EdgeMap, start: str) -> set[str]:
    seen: set[str] = set()
    queue = deque([start])
    while queue:
        node_id = queue.popleft()
        if node_id in seen:
            continue
        seen.add(node_id)
        queue.extend(edges.get(node_id, set()))
    return seen
