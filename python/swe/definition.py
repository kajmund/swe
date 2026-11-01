"""Parse and validate a canonical SWD 1.0 document."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from swe.canonicalize import canonicalize, canonical_json_bytes, sha256_hex
from swe.errors import SwdValidationError
from swe.freeze import freeze_json
from swe.graph import assert_known_node_types, validate_graph
from swe.schema import (
    HANDLER_NAME_PATTERN,
    PROMPT_KEY_PATTERN,
    PROVIDER_NAME_PATTERN,
    SWD_VERSION,
    load_schema,
)
from swe.yaml_load import json_object, load_swd_text

HANDLER_NAME_RE = re.compile(HANDLER_NAME_PATTERN)
PROVIDER_NAME_RE = re.compile(PROVIDER_NAME_PATTERN)
PROMPT_KEY_RE = re.compile(PROMPT_KEY_PATTERN)
MAX_DOCUMENT_BYTES = 1_000_000


@dataclass(frozen=True)
class CompiledDefinition:
    document: Mapping[str, Any]
    sha256: str
    version: str
    name: str
    entrypoint: str


def compile_definition(
    document: object,
    *,
    handlers: frozenset[str] | None = None,
    providers: frozenset[str] | None = None,
    prompt_keys: frozenset[str] | None = None,
) -> CompiledDefinition:
    """Compile and validate an SWD 1.0 document.

    ``prompt_keys`` is the host's prompt-catalog allowlist. It is fail-closed:
    a document that declares a provider decision must be given one, because a
    provider decision renders a prompt that the host must be able to resolve.
    A document without provider decisions may omit it.
    """
    parsed = json_object(document)
    _assert_version(parsed)
    canonical = canonicalize(parsed)
    _assert_size(canonical)
    assert_known_node_types(canonical)
    _validate_schema(canonical)
    validate_graph(canonical)
    _validate_registry(
        canonical,
        handlers=handlers,
        providers=providers,
        prompt_keys=prompt_keys,
    )
    frozen = freeze_json(canonical)
    return CompiledDefinition(
        document=frozen,
        sha256=sha256_hex(frozen),
        version=str(frozen["version"]),
        name=str(frozen["name"]),
        entrypoint=str(frozen["entrypoint"]),
    )


def compile_swd_text(
    raw: str | bytes,
    *,
    handlers: frozenset[str] | None = None,
    providers: frozenset[str] | None = None,
    prompt_keys: frozenset[str] | None = None,
) -> CompiledDefinition:
    return compile_definition(
        load_swd_text(raw),
        handlers=handlers,
        providers=providers,
        prompt_keys=prompt_keys,
    )


def _assert_version(document: dict[str, Any]) -> None:
    if document.get("version") != SWD_VERSION:
        raise SwdValidationError(
            f"unsupported SWD version {document.get('version')!r}",
            path="version",
        )


def _assert_size(document: dict[str, Any]) -> None:
    if len(canonical_json_bytes(document)) > MAX_DOCUMENT_BYTES:
        raise SwdValidationError("definition exceeds 1MB canonical size", path="")


def _validate_schema(document: dict[str, Any]) -> None:
    validator = Draft202012Validator(load_schema())
    error = next(validator.iter_errors(document), None)
    if error is None:
        return
    raise _schema_error(error)


def _schema_error(error: ValidationError) -> SwdValidationError:
    path = ".".join(str(part) for part in error.absolute_path)
    return SwdValidationError(error.message, path=path)


def _validate_registry(
    document: dict[str, Any],
    *,
    handlers: frozenset[str] | None,
    providers: frozenset[str] | None,
    prompt_keys: frozenset[str] | None,
) -> None:
    nodes = document["nodes"]
    for node_id, node in nodes.items():
        if node["type"] == "task":
            _check_handler(node_id, node, handlers)
        elif node["type"] == "decision" and node.get("evaluator") == "provider":
            _check_provider(node_id, node, providers)
            _check_prompt_key(node_id, node, prompt_keys)


def _check_handler(
    node_id: str,
    node: dict[str, Any],
    handlers: frozenset[str] | None,
) -> None:
    handler = node.get("handler")
    if not isinstance(handler, str) or not HANDLER_NAME_RE.fullmatch(handler):
        raise SwdValidationError(
            f"invalid handler name {handler!r}",
            path=f"nodes.{node_id}.handler",
        )
    if handlers is not None and handler not in handlers:
        raise SwdValidationError(
            f"unknown handler {handler!r}",
            path=f"nodes.{node_id}.handler",
        )


def _check_provider(
    node_id: str,
    node: dict[str, Any],
    providers: frozenset[str] | None,
) -> None:
    provider = node.get("provider")
    if not isinstance(provider, str) or not PROVIDER_NAME_RE.fullmatch(provider):
        raise SwdValidationError(
            f"invalid provider name {provider!r}",
            path=f"nodes.{node_id}.provider",
        )
    if providers is not None and provider not in providers:
        raise SwdValidationError(
            f"unknown decision provider {provider!r}",
            path=f"nodes.{node_id}.provider",
        )


def _check_prompt_key(
    node_id: str,
    node: dict[str, Any],
    prompt_keys: frozenset[str] | None,
) -> None:
    prompt_key = node.get("prompt_key")
    if not isinstance(prompt_key, str) or not PROMPT_KEY_RE.fullmatch(prompt_key):
        raise SwdValidationError(
            f"invalid prompt_key {prompt_key!r}",
            path=f"nodes.{node_id}.prompt_key",
        )
    # Fail-closed: a provider decision must be validated against the host's
    # prompt-catalog allowlist. There is no permissive path.
    if prompt_keys is None:
        raise SwdValidationError(
            f"provider decision {node_id!r} requires a prompt_keys allowlist",
            path=f"nodes.{node_id}.prompt_key",
        )
    if prompt_key not in prompt_keys:
        raise SwdValidationError(
            f"unknown prompt_key {prompt_key!r}",
            path=f"nodes.{node_id}.prompt_key",
        )
