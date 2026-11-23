"""Separate a provider payload's shape from any later semantic score."""

from __future__ import annotations

import json
import re
from typing import Any

from pydantic import BaseModel, ValidationError

_SECRET_KEY = re.compile(
    r"(?i)^(authorization|api[_-]?key|x-api-key|secret|access_token|password)$"
)
_BEARER = re.compile(r"(?i)bearer\s+\S+")
_KEY_PREFIX = re.compile(r"(?i)\b(?:sk|tvly)-[A-Za-z0-9_-]+")


def sanitize(value: Any) -> Any:
    if isinstance(value, dict):
        cleaned = {}
        for key, item in value.items():
            if _SECRET_KEY.match(str(key)):
                cleaned[str(key)] = "<REDACTED>"
            else:
                cleaned[str(key)] = sanitize(item)
        return cleaned
    if isinstance(value, list):
        return [sanitize(item) for item in value]
    if isinstance(value, str):
        text = _BEARER.sub("Bearer <REDACTED>", value)
        return _KEY_PREFIX.sub("<REDACTED>", text)
    return value


def _object_from_text(text: str) -> tuple[dict[str, Any] | None, str | None]:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:].strip()
    candidates = [cleaned]
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start >= 0 and end > start:
        candidates.append(cleaned[start : end + 1])
    last_error = "invalid_json"
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed, None
        last_error = "json_not_object"
    return None, last_error


def _validation_error(exc: ValidationError) -> list[dict[str, str]]:
    rows = []
    for item in exc.errors():
        rows.append(
            {
                "loc": ".".join(str(part) for part in item.get("loc", ())),
                "type": str(item.get("type", "")),
                "msg": str(item.get("msg", ""))[:500],
            }
        )
    return rows


def _tool_object(tool_calls: list[Any] | None) -> tuple[dict[str, Any] | None, str | None]:
    if not tool_calls:
        return None, "tool_calls_absent"
    first = tool_calls[0]
    if not isinstance(first, dict):
        return None, "tool_call_not_object"
    function = first.get("function") if isinstance(first.get("function"), dict) else first
    arguments = function.get("arguments")
    if isinstance(arguments, dict):
        return arguments, None
    if isinstance(arguments, str):
        return _object_from_text(arguments)
    return None, "tool_arguments_absent"


def classify_output(
    observation: dict[str, Any],
    schema: type[BaseModel],
    *,
    requested_model: str,
    max_output: int,
    channel: str,
) -> dict[str, Any]:
    """Validate one channel. A schema miss does not produce a semantic score."""

    content_present = bool(observation.get("content_present"))
    content = observation.get("content") if isinstance(observation.get("content"), str) else None
    thinking_present = bool(observation.get("thinking_present"))
    thinking = observation.get("thinking") if isinstance(observation.get("thinking"), str) else None
    tool_calls = observation.get("tool_calls") if isinstance(observation.get("tool_calls"), list) else None
    finish = observation.get("finish_reason")
    usage = observation.get("usage") if isinstance(observation.get("usage"), dict) else {}
    output_tokens = usage.get("output")
    truncated = finish in {"length", "max_tokens"} or (
        isinstance(output_tokens, int) and not isinstance(output_tokens, bool) and output_tokens > max_output
    )
    returned_model = observation.get("model")
    model_tag_match: bool | None
    if not isinstance(returned_model, str) or not returned_model:
        model_tag_match = None
    else:
        model_tag_match = (
            returned_model == requested_model
            or returned_model in requested_model
            or requested_model in returned_model
        )
    thinking_object, _thinking_error = _object_from_text(thinking or "")
    if channel == "tool":
        parsed, error = _tool_object(tool_calls)
    elif not content_present:
        parsed, error = None, "content_not_retained"
    elif content is None or content.strip() == "":
        parsed, error = None, "empty_content"
    else:
        parsed, error = _object_from_text(content)
    validation: list[dict[str, str]] | None = None
    json_keys = sorted(parsed) if parsed is not None else None
    if error is None and parsed is not None:
        try:
            schema.model_validate(parsed)
        except ValidationError as exc:
            error = "schema_validation"
            validation = _validation_error(exc)
    return sanitize(
        {
            "channel": channel,
            "structure_error": error,
            "validation_errors": validation,
            "json_keys": json_keys,
            "truncated": truncated,
            "model_returned": returned_model,
            "model_tag_match": model_tag_match,
            "content_present": content_present,
            "content": content,
            "thinking_present": thinking_present,
            "thinking": thinking,
            "thinking_is_object": thinking_object is not None,
            "tool_calls": tool_calls,
            "finish_reason": finish,
            "usage": usage,
            "parsed": parsed if error is None else None,
        }
    )
