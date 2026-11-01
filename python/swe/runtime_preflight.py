"""Reject reserved SWD features and unregistered registries before a run starts."""

from __future__ import annotations

from collections.abc import Callable, Collection, Mapping
from typing import Any

from swe.runtime_errors import SwePreflightError, SweUnsupportedError

UNSUPPORTED_TYPES = frozenset({"wait", "subworkflow"})


def assert_handlers_registered(
    document: Mapping[str, Any],
    handlers: Mapping[str, Callable[..., Any]],
) -> None:
    nodes = document["nodes"]
    for node_id, node in nodes.items():
        if node["type"] != "task":
            continue
        name = node["handler"]
        if name not in handlers:
            raise SwePreflightError(
                f"unregistered handler {name!r}",
                path=f"nodes.{node_id}.handler",
            )


def assert_providers_registered(
    document: Mapping[str, Any],
    providers: Mapping[str, Callable[..., Any]],
    prompts: Mapping[str, str] | None,
    cache: Any | None,
    *,
    prompt_keys: Collection[str] | None = None,
) -> None:
    """Fail-closed runtime counterpart of the compile-time prompt allowlist.

    Compilation already rejects a provider decision without an allowlist. The
    driver must hold the same line: a compiled definition can be constructed
    through another path, so execution validates membership here too. A prompt
    that is declared but not permitted is never rendered.
    """
    nodes = document["nodes"]
    for node_id, node in nodes.items():
        if node["type"] != "decision" or node.get("evaluator") != "provider":
            continue
        name = node["provider"]
        if name not in providers:
            raise SwePreflightError(
                f"unregistered decision provider {name!r}",
                path=f"nodes.{node_id}.provider",
            )
        prompt_key = node["prompt_key"]
        if prompt_keys is None:
            raise SwePreflightError(
                f"provider decision {node_id!r} requires a prompt_keys allowlist",
                path=f"nodes.{node_id}.prompt_key",
            )
        if prompt_key not in prompt_keys:
            raise SwePreflightError(
                f"prompt {prompt_key!r} is not in the active prompt catalog",
                path=f"nodes.{node_id}.prompt_key",
            )
        if prompts is None or not str(prompts.get(prompt_key) or "").strip():
            raise SwePreflightError(
                f"active prompt {prompt_key!r} is missing",
                path=f"nodes.{node_id}.prompt_key",
            )
        if node.get("cache_policy") == "exact" and cache is None:
            raise SwePreflightError(
                "cache_policy exact requires a provider cache",
                path=f"nodes.{node_id}.cache_policy",
            )


def assert_phase2_supported(document: Mapping[str, Any]) -> None:
    for node_id, node in document["nodes"].items():
        node_type = node["type"]
        if node_type in UNSUPPORTED_TYPES:
            raise SweUnsupportedError(
                f"{node_type} nodes are not executable in SWE v1",
                path=f"nodes.{node_id}.type",
            )
        if node.get("retry") is not None:
            raise SweUnsupportedError(
                "retry is not executable in SWE v1",
                path=f"nodes.{node_id}.retry",
            )
        provider_decision = (
            node_type == "decision" and node.get("evaluator") == "provider"
        )
        if node.get("cache_policy") == "exact" and not provider_decision:
            raise SweUnsupportedError(
                "cache_policy exact is only executable on provider decisions",
                path=f"nodes.{node_id}.cache_policy",
            )
