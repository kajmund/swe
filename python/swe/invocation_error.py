"""Normalize invocation errors to a generic mapping.

Hosts may persist a string (legacy) or a mapping ``{kind, message, path?,
payload?}``. This module does not interpret domain kinds: unknown ``kind``
values stay on the mapping and map to SWE kernel ``handler`` unless they
already match a kernel kind.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from swe.runtime_errors import SweRuntimeError

_REQUIRED_KIND = "kind"
_REQUIRED_MESSAGE = "message"
_OPTIONAL_PATH = "path"
_OPTIONAL_PAYLOAD = "payload"
_DEFAULT_KIND = "handler"


def normalize_invocation_error(error: object) -> dict[str, Any]:
    """Return a JSON-object invocation error.

    ``None`` becomes a replay fallback. A string becomes ``handler`` with that
    message. A mapping must include ``kind`` and ``message`` as strings.
    """

    if error is None:
        return {
            _REQUIRED_KIND: _DEFAULT_KIND,
            _REQUIRED_MESSAGE: "replayed failure",
            _OPTIONAL_PATH: "",
        }
    if isinstance(error, str):
        return {
            _REQUIRED_KIND: _DEFAULT_KIND,
            _REQUIRED_MESSAGE: error,
            _OPTIONAL_PATH: "",
        }
    if isinstance(error, Mapping):
        kind = error.get(_REQUIRED_KIND)
        message = error.get(_REQUIRED_MESSAGE)
        if not isinstance(kind, str) or not kind.strip():
            raise TypeError("invocation error mapping requires a string kind")
        if not isinstance(message, str) or not message:
            raise TypeError("invocation error mapping requires a string message")
        normalized: dict[str, Any] = {
            _REQUIRED_KIND: kind.strip(),
            _REQUIRED_MESSAGE: message,
            _OPTIONAL_PATH: "",
        }
        path = error.get(_OPTIONAL_PATH, "")
        if path is None:
            path = ""
        if not isinstance(path, str):
            raise TypeError("invocation error path must be a string")
        normalized[_OPTIONAL_PATH] = path
        if _OPTIONAL_PAYLOAD in error:
            normalized[_OPTIONAL_PAYLOAD] = error[_OPTIONAL_PAYLOAD]
        return normalized
    raise TypeError("invocation error must be a string, mapping, or None")


def invocation_error_from_exception(error: BaseException) -> dict[str, Any]:
    """Build an invocation-error mapping from a handler exception."""

    if isinstance(error, SweRuntimeError):
        payload: dict[str, Any] = {
            _REQUIRED_KIND: str(error.kind),
            _REQUIRED_MESSAGE: str(error),
            _OPTIONAL_PATH: str(error.path),
        }
        extra = getattr(error, "payload", None)
        if extra is not None:
            payload[_OPTIONAL_PAYLOAD] = extra
        return payload
    return {
        _REQUIRED_KIND: _DEFAULT_KIND,
        _REQUIRED_MESSAGE: f"{type(error).__name__}: {error}",
        _OPTIONAL_PATH: "",
    }
