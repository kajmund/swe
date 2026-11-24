"""Strict, typed microtask contracts for the swarm pilot.

Every exchange is a JSON object with an exact key set. There is no free-form
agent-to-agent text: a task input is a typed payload, a task output is a typed
payload, and the verifier sees only the contract, the passage, and the output.
Gold labels never enter a prompt.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Mapping, Sequence

ENTAILMENT_LABELS: tuple[str, ...] = ("entailment", "contradiction", "neutral")
OPERATION_CHOICES: tuple[str, ...] = (
    "check_relevance",
    "classify_entailment",
    "check_atomicity",
    "extract_quote",
    "answer_question",
)


class ContractViolation(ValueError):
    """A typed microtask payload does not satisfy its contract."""

    def __init__(self, message: str, *, path: str = "") -> None:
        super().__init__(message)
        self.path = path


class Kind(StrEnum):
    RELEVANCE = "relevance"
    ENTAILMENT = "entailment"
    ATOMICITY = "atomicity"
    EXTRACTION = "extraction"
    OPERATION = "operation_selection"
    COMBINED = "combined"


TASK_KINDS: tuple[Kind, ...] = (
    Kind.RELEVANCE,
    Kind.ENTAILMENT,
    Kind.ATOMICITY,
    Kind.EXTRACTION,
    Kind.OPERATION,
)

_SCHEMAS: dict[Kind, dict[str, Any]] = {
    Kind.RELEVANCE: {
        "type": "object",
        "additionalProperties": False,
        "required": ["relevant"],
        "properties": {"relevant": {"type": "boolean"}},
    },
    Kind.ENTAILMENT: {
        "type": "object",
        "additionalProperties": False,
        "required": ["label"],
        "properties": {"label": {"type": "string", "enum": list(ENTAILMENT_LABELS)}},
    },
    Kind.ATOMICITY: {
        "type": "object",
        "additionalProperties": False,
        "required": ["atomic"],
        "properties": {"atomic": {"type": "boolean"}},
    },
    Kind.EXTRACTION: {
        "type": "object",
        "additionalProperties": False,
        "required": ["quotes"],
        "properties": {
            "quotes": {
                "type": "array",
                "maxItems": 3,
                "items": {"type": "string", "minLength": 10, "maxLength": 400},
            }
        },
    },
    Kind.OPERATION: {
        "type": "object",
        "additionalProperties": False,
        "required": ["operation"],
        "properties": {
            "operation": {"type": "string", "enum": list(OPERATION_CHOICES)}
        },
    },
}

# Fill the combined schema from the single-task schemas after definition.
_SCHEMAS[Kind.COMBINED] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["relevance", "entailment", "atomicity", "extraction", "operation"],
    "properties": {
        "relevance": _SCHEMAS[Kind.RELEVANCE],
        "entailment": _SCHEMAS[Kind.ENTAILMENT],
        "atomicity": _SCHEMAS[Kind.ATOMICITY],
        "extraction": {
            "type": "object",
            "additionalProperties": False,
            "required": ["quotes"],
            "properties": {
                "quotes": {
                    "type": "array",
                    "maxItems": 3,
                    "items": {"type": "string", "minLength": 10, "maxLength": 400},
                }
            },
        },
        "operation": _SCHEMAS[Kind.OPERATION],
    },
}

_FORBIDDEN_INPUT_KEYS = frozenset(
    {"gold", "item_id", "domain", "label", "answer", "expected", "expected_quotes"}
)


def output_schema(kind: Kind) -> dict[str, Any]:
    """Return a fresh JSON Schema suitable for Ollama's ``format`` field."""

    return json.loads(json.dumps(_SCHEMAS[kind]))


def _require_object(value: Any, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ContractViolation("must be a JSON object", path=path)
    return value


def _exact_keys(
    value: Mapping[str, Any], required: Sequence[str], path: str
) -> None:
    keys = set(value)
    expected = set(required)
    if keys != expected:
        missing = sorted(expected - keys)
        extra = sorted(keys - expected)
        parts = []
        if missing:
            parts.append(f"missing keys {missing}")
        if extra:
            parts.append(f"extra keys {extra}")
        raise ContractViolation("; ".join(parts), path=path)


def _require_bool(value: Any, path: str) -> bool:
    if isinstance(value, bool):
        return value
    raise ContractViolation("must be a boolean", path=path)


def _require_enum(value: Any, choices: Sequence[str], path: str) -> str:
    if isinstance(value, str) and value in choices:
        return value
    raise ContractViolation(f"must be one of {list(choices)}", path=path)


def validate_output(
    kind: Kind,
    value: Any,
    *,
    max_quotes: int = 3,
    min_quote_chars: int = 10,
) -> Mapping[str, Any]:
    """Validate one typed output. Extra keys and prose are rejected."""

    if kind is Kind.RELEVANCE:
        obj = _require_object(value, "output")
        _exact_keys(obj, ("relevant",), "output")
        _require_bool(obj["relevant"], "output.relevant")
        return obj
    if kind is Kind.ENTAILMENT:
        obj = _require_object(value, "output")
        _exact_keys(obj, ("label",), "output")
        _require_enum(obj["label"], ENTAILMENT_LABELS, "output.label")
        return obj
    if kind is Kind.ATOMICITY:
        obj = _require_object(value, "output")
        _exact_keys(obj, ("atomic",), "output")
        _require_bool(obj["atomic"], "output.atomic")
        return obj
    if kind is Kind.OPERATION:
        obj = _require_object(value, "output")
        _exact_keys(obj, ("operation",), "output")
        _require_enum(obj["operation"], OPERATION_CHOICES, "output.operation")
        return obj
    if kind is Kind.EXTRACTION:
        obj = _require_object(value, "output")
        _exact_keys(obj, ("quotes",), "output")
        quotes = obj["quotes"]
        if not isinstance(quotes, list):
            raise ContractViolation("must be an array", path="output.quotes")
        if len(quotes) > max_quotes:
            raise ContractViolation(
                f"has {len(quotes)} quotes, maximum is {max_quotes}",
                path="output.quotes",
            )
        seen: set[str] = set()
        for index, quote in enumerate(quotes):
            path = f"output.quotes[{index}]"
            if not isinstance(quote, str):
                raise ContractViolation("must be a string", path=path)
            if len(quote) < min_quote_chars:
                raise ContractViolation(
                    f"is shorter than {min_quote_chars} characters", path=path
                )
            if len(quote) > 400:
                raise ContractViolation("is longer than 400 characters", path=path)
            if quote in seen:
                raise ContractViolation("duplicates an earlier quote", path=path)
            seen.add(quote)
        return obj
    if kind is Kind.COMBINED:
        obj = _require_object(value, "output")
        _exact_keys(
            obj,
            ("relevance", "entailment", "atomicity", "extraction", "operation"),
            "output",
        )
        for key, subkind in (
            ("relevance", Kind.RELEVANCE),
            ("entailment", Kind.ENTAILMENT),
            ("atomicity", Kind.ATOMICITY),
            ("extraction", Kind.EXTRACTION),
            ("operation", Kind.OPERATION),
        ):
            validate_output(
                subkind,
                obj[key],
                max_quotes=max_quotes,
                min_quote_chars=min_quote_chars,
            )
        return obj
    raise ContractViolation(f"unknown contract kind {kind!r}", path="kind")


@dataclass(frozen=True, slots=True)
class Verification:
    """Deterministic, generator-independent contract check."""

    ok: bool
    errors: tuple[str, ...]
    verified_quotes: tuple[str, ...] = ()
    unverified_quotes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "errors": list(self.errors),
            "verified_quotes": list(self.verified_quotes),
            "unverified_quotes": list(self.unverified_quotes),
        }


def verify_output(
    kind: Kind,
    value: Any,
    *,
    passage: str,
    max_quotes: int = 3,
    min_quote_chars: int = 10,
) -> Verification:
    """Validate shape and, for extraction, exact substring support.

    Exact means byte-for-byte ``quote in passage`` after JSON decoding. No
    whitespace normalisation, case folding, or fuzzy matching is applied.
    """

    try:
        obj = validate_output(
            kind,
            value,
            max_quotes=max_quotes,
            min_quote_chars=min_quote_chars,
        )
    except ContractViolation as exc:
        return Verification(False, (f"{exc.path or 'output'}: {exc}",))

    if kind is Kind.EXTRACTION:
        quotes = tuple(obj["quotes"])
    elif kind is Kind.COMBINED:
        quotes = tuple(obj["extraction"]["quotes"])
    else:
        quotes = ()

    unverified = tuple(quote for quote in quotes if quote not in passage)
    if unverified:
        return Verification(
            False,
            ("unverified exact quote",),
            verified_quotes=tuple(q for q in quotes if q in passage),
            unverified_quotes=unverified,
        )
    return Verification(True, (), verified_quotes=quotes)


def assert_input_is_typed(input_data: Mapping[str, Any]) -> None:
    """Reject gold-bearing keys before any prompt is rendered."""

    def walk(value: Any, path: str) -> None:
        if isinstance(value, Mapping):
            for key, child in value.items():
                if not isinstance(key, str):
                    raise ContractViolation("input keys must be strings", path=path)
                if key in _FORBIDDEN_INPUT_KEYS:
                    raise ContractViolation(
                        f"gold-bearing key {key!r} is not allowed in input",
                        path=f"{path}.{key}" if path else key,
                    )
                walk(child, f"{path}.{key}" if path else key)
        elif isinstance(value, list):
            for index, child in enumerate(value):
                walk(child, f"{path}[{index}]")

    walk(input_data, "")


_SYSTEM_PROMPT = (
    "You are a typed microtask worker. Return exactly one JSON object that "
    "matches the supplied output schema. Do not return prose, markdown, code "
    "fences, explanations, or any keys outside the schema. If the input does "
    "not support an answer, return the schema's empty or negative value; never "
    "invent a quote."
)


def render_prompt(kind: Kind, input_data: Mapping[str, Any]) -> tuple[str, str]:
    """Render a system/user pair. The user payload is JSON, not prose."""

    assert_input_is_typed(input_data)
    payload = {
        "contract": str(kind),
        "input": dict(input_data),
        "output_schema": output_schema(kind),
    }
    user = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return _SYSTEM_PROMPT, user


def parse_json_output(text: str) -> Any:
    """Parse a strict JSON response, rejecting markdown and surrounding prose."""

    stripped = text.strip()
    if not stripped:
        raise ContractViolation("empty response")
    if stripped.startswith("```") or stripped.endswith("```"):
        raise ContractViolation("markdown code fences are not allowed")
    try:
        return json.loads(stripped)
    except json.JSONDecodeError as exc:
        raise ContractViolation(f"response is not valid JSON: {exc.msg}") from exc


__all__ = [
    "ENTAILMENT_LABELS",
    "OPERATION_CHOICES",
    "ContractViolation",
    "Kind",
    "TASK_KINDS",
    "Verification",
    "assert_input_is_typed",
    "output_schema",
    "parse_json_output",
    "render_prompt",
    "validate_output",
    "verify_output",
]
