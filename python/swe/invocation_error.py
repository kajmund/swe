"""Normalize invocation errors to a generic mapping.

Hosts may persist a string (legacy) or a mapping ``{kind, message, path?,
payload?}``. This module does not interpret domain kinds: unknown ``kind``
values stay on the mapping and map to SWE kernel ``handler`` unless they
already match a kernel kind.

Rules shared with the native ``Machine.fail`` binding:

- any ``collections.abc.Mapping`` is accepted;
- a missing, ``None``, empty or whitespace ``kind`` is ``handler``; other kinds
  are stripped of surrounding whitespace; a non-string ``kind`` is a TypeError;
- a missing, ``None``, empty or whitespace ``message`` is a ValueError and a
  non-string ``message`` is a TypeError;
- ``path`` is a string or ``None`` (read as ``""``).

The runtime never hands an unchecked value to the kernel:
``live_invocation_error`` substitutes a default message and degrades malformed
values to ``handler``, so a failing handler always yields a clean failed result.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from swe.runtime_errors import SweRuntimeError
from swe.runtime_thaw import thaw_json
from swe.runtime_validate import is_json_value

_REQUIRED_KIND = "kind"
_REQUIRED_MESSAGE = "message"
_OPTIONAL_PATH = "path"
_OPTIONAL_PAYLOAD = "payload"
_DEFAULT_KIND = "handler"
_REPLAY_FALLBACK = "replayed failure"
_EMPTY_MESSAGE_FALLBACK = "handler failed without a message"
_INVALID_PAYLOAD_SUFFIX = " (invocation error payload is not a JSON value; dropped)"
_INVALID_ERROR_MESSAGE = "handler reported a malformed invocation error"


def normalize_invocation_error(error: object) -> dict[str, Any]:
    """Return a JSON-object invocation error.

    ``None`` and an empty or whitespace legacy string become the
    ``"replayed failure"`` fallback. Any other string becomes ``handler`` with
    that message. A mapping follows the rules in the module docstring. An
    invalid ``payload`` (not a JSON value, cyclic, or too deep) degrades the
    error to ``handler`` and drops the payload. The result is a fixed point:
    normalizing it again returns an equal mapping.
    """

    if error is None:
        return _mapping(_DEFAULT_KIND, _REPLAY_FALLBACK, "")
    if isinstance(error, str):
        return _mapping(_DEFAULT_KIND, error if error.strip() else _REPLAY_FALLBACK, "")
    if not isinstance(error, Mapping):
        raise TypeError("invocation error must be a string, mapping, or None")
    kind = error.get(_REQUIRED_KIND)
    if kind is None:
        kind = _DEFAULT_KIND
    if not isinstance(kind, str):
        raise TypeError("invocation error kind must be a string")
    message = error.get(_REQUIRED_MESSAGE)
    if message is None:
        raise ValueError("invocation error mapping requires a non-empty message")
    if not isinstance(message, str):
        raise TypeError("invocation error message must be a string")
    if not message.strip():
        raise ValueError("invocation error mapping requires a non-empty message")
    path = error.get(_OPTIONAL_PATH)
    if path is None:
        path = ""
    if not isinstance(path, str):
        raise TypeError("invocation error path must be a string")
    normalized = _mapping(kind.strip() or _DEFAULT_KIND, message, path)
    if _OPTIONAL_PAYLOAD in error:
        payload = _json_payload(error[_OPTIONAL_PAYLOAD])
        if payload is _INVALID:
            return _mapping(_DEFAULT_KIND, message + _INVALID_PAYLOAD_SUFFIX, path)
        normalized[_OPTIONAL_PAYLOAD] = payload
    return normalized


def live_invocation_error(error: object) -> dict[str, Any]:
    """Normalize a live handler failure without ever raising.

    ``error`` is a handler exception or the failure value of a handler outcome.
    The result is what the runtime passes to ``Machine.fail`` and stores on
    ``InvocationRecord.error``, so replay sees exactly the same mapping.
    """

    raw = invocation_error_from_exception(error) if isinstance(error, BaseException) else error
    if isinstance(raw, Mapping):
        message = raw.get(_REQUIRED_MESSAGE)
        if message is None or (isinstance(message, str) and not message.strip()):
            raw = {**raw, _REQUIRED_MESSAGE: _EMPTY_MESSAGE_FALLBACK}
    elif raw is None or (isinstance(raw, str) and not raw.strip()):
        raw = _EMPTY_MESSAGE_FALLBACK
    try:
        return normalize_invocation_error(raw)
    except (TypeError, ValueError):
        path = raw.get(_OPTIONAL_PATH) if isinstance(raw, Mapping) else ""
        return _mapping(
            _DEFAULT_KIND, _INVALID_ERROR_MESSAGE, path if isinstance(path, str) else ""
        )


def invocation_error_from_exception(error: BaseException) -> dict[str, Any]:
    """Build a raw invocation-error mapping from a handler exception.

    The result is not normalized; the runtime passes it through
    ``live_invocation_error`` before the kernel sees it.
    """

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


_INVALID = object()


def _json_payload(value: object) -> object:
    """Validate ``value`` the way handler outputs are validated."""

    try:
        if not is_json_value(value):
            return _INVALID
        return thaw_json(value)
    except RecursionError:
        return _INVALID


def _mapping(kind: str, message: str, path: str) -> dict[str, Any]:
    return {_REQUIRED_KIND: kind, _REQUIRED_MESSAGE: message, _OPTIONAL_PATH: path}
