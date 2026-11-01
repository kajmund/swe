"""Canonical JSON bytes and SHA-256 for SWD definitions and decision-cache keys.

PR 1 uses this Python encoder as the test oracle. A later Rust crate must
match these bytes; Rust then becomes the normative encoder.
"""

from __future__ import annotations

import hashlib
import json
import math
import unicodedata
from collections.abc import Mapping
from typing import Any

from swe.errors import SwdParseError

CANONICAL_SCHEMA_VERSION = "swd-canonical-json-v1"


def canonicalize(value: object) -> Any:
    """NFC keys/strings, LF newlines, ints for integer floats, collision-checked keys."""
    return _normalize(value)


def canonical_json_bytes(value: object) -> bytes:
    text = json.dumps(
        canonicalize(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return text.encode("utf-8")


def sha256_hex(value: object) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _normalize(value: object) -> Any:
    if isinstance(value, Mapping):
        return _normalize_object(value)
    if isinstance(value, (list, tuple)):
        return [_normalize(item) for item in value]
    return _normalize_leaf(value)


def _normalize_object(value: Mapping[object, object]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, item in value.items():
        normalized_key = _normalize_key(key)
        if normalized_key in out:
            raise SwdParseError(f"canonical key collision {normalized_key!r}")
        out[normalized_key] = _normalize(item)
    return out


def _normalize_key(key: object) -> str:
    if not isinstance(key, str):
        raise SwdParseError("object keys must be strings")
    return _normalize_text(key)


def _normalize_leaf(value: object) -> Any:
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return _normalize_float(value)
    if isinstance(value, str):
        return _normalize_text(value)
    raise SwdParseError(f"unsupported JSON type {type(value).__name__}")


def _normalize_text(value: str) -> str:
    return unicodedata.normalize("NFC", value.replace("\r\n", "\n").replace("\r", "\n"))


def _normalize_float(value: float) -> int | float:
    if not math.isfinite(value):
        raise SwdParseError("NaN and Infinity are not allowed in canonical JSON")
    if value.is_integer():
        return int(value)
    return value
