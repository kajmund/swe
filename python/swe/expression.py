"""Typed JSON-AST expressions. No eval, no Python operators on the document."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, TypeGuard

from swe.errors import SwdExpressionError
from swe.paths import MISSING, resolve_path

ExprOp = Callable[[object, Mapping[str, Any]], object]


def evaluate_expression(expr: object, context: Mapping[str, Any]) -> object:
    kind, payload = split_expr(expr)
    if kind == "const":
        return payload
    if kind == "var":
        return _eval_var(payload, context)
    handler = OPS.get(kind)
    if handler is None:
        raise SwdExpressionError(f"unknown operator {kind!r}")
    return handler(payload, context)


def split_expr(expr: object) -> tuple[str, object]:
    if not isinstance(expr, Mapping) or not expr:
        raise SwdExpressionError("expression must be a non-empty object")
    if "var" in expr:
        extra = set(expr) - {"var", "default"}
        if extra:
            raise SwdExpressionError(f"unknown var fields {sorted(extra)}")
        return "var", expr
    if len(expr) != 1:
        raise SwdExpressionError("operator expressions must have exactly one key")
    kind, payload = next(iter(expr.items()))
    return kind, payload


def _eval_var(payload: object, context: Mapping[str, Any]) -> object:
    if not isinstance(payload, Mapping) or not isinstance(payload.get("var"), str):
        raise SwdExpressionError("var requires a string path")
    value = resolve_path(context, payload["var"])
    if value is not MISSING:
        return value
    if "default" in payload:
        return payload["default"]
    raise SwdExpressionError(f"missing value at {payload['var']!r}")


def _operands(payload: object, context: Mapping[str, Any], *, count: int) -> list[object]:
    if not _is_array(payload) or len(payload) != count:
        raise SwdExpressionError(f"operator requires exactly {count} operands")
    return [evaluate_expression(item, context) for item in payload]


def _is_array(value: object) -> TypeGuard[list[object] | tuple[object, ...]]:
    return isinstance(value, (list, tuple))


def _json_kind(value: object) -> str | None:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    return _json_container_kind(value)


def _json_container_kind(value: object) -> str | None:
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, Mapping):
        return "object"
    if _is_array(value):
        return "array"
    return None


def _same_json_type(left: object, right: object) -> bool:
    kind = _json_kind(left)
    return kind is not None and kind == _json_kind(right)


def _require_bool(value: object, *, op: str) -> bool:
    if not isinstance(value, bool):
        raise SwdExpressionError(f"{op} requires boolean operands")
    return value


def _require_number(value: object, *, op: str) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SwdExpressionError(f"{op} requires numbers")
    return value


def _eval_eq(payload: object, context: Mapping[str, Any]) -> bool:
    left, right = _operands(payload, context, count=2)
    if not _same_json_type(left, right):
        raise SwdExpressionError("eq/ne require matching JSON types")
    return _json_equal(left, right)


def _eval_ne(payload: object, context: Mapping[str, Any]) -> bool:
    return not _eval_eq(payload, context)


def _eval_ordered(payload: object, context: Mapping[str, Any], *, op: str) -> tuple[int | float, int | float]:
    left, right = _operands(payload, context, count=2)
    return _require_number(left, op=op), _require_number(right, op=op)


def _eval_lt(payload: object, context: Mapping[str, Any]) -> bool:
    left, right = _eval_ordered(payload, context, op="lt")
    return left < right


def _eval_lte(payload: object, context: Mapping[str, Any]) -> bool:
    left, right = _eval_ordered(payload, context, op="lte")
    return left <= right


def _eval_gt(payload: object, context: Mapping[str, Any]) -> bool:
    left, right = _eval_ordered(payload, context, op="gt")
    return left > right


def _eval_gte(payload: object, context: Mapping[str, Any]) -> bool:
    left, right = _eval_ordered(payload, context, op="gte")
    return left >= right


def _eval_and(payload: object, context: Mapping[str, Any]) -> bool:
    values = _operands(payload, context, count=2)
    return all(_require_bool(value, op="and") for value in values)


def _eval_or(payload: object, context: Mapping[str, Any]) -> bool:
    values = _operands(payload, context, count=2)
    return any(_require_bool(value, op="or") for value in values)


def _eval_not(payload: object, context: Mapping[str, Any]) -> bool:
    return not _require_bool(evaluate_expression(payload, context), op="not")


def _json_equal(left: object, right: object) -> bool:
    kind = _json_kind(left)
    if kind is None or kind != _json_kind(right):
        return False
    if kind == "array" and _is_array(left) and _is_array(right):
        return len(left) == len(right) and all(
            _json_equal(item, other) for item, other in zip(left, right, strict=True)
        )
    if kind == "object" and isinstance(left, Mapping) and isinstance(right, Mapping):
        if set(left) != set(right):
            return False
        return all(_json_equal(left[key], right[key]) for key in left)
    return left == right


def _eval_in(payload: object, context: Mapping[str, Any]) -> bool:
    item, collection = _operands(payload, context, count=2)
    if not _is_array(collection):
        raise SwdExpressionError("in requires an array collection")
    return any(_json_equal(item, element) for element in collection)


def _eval_add(payload: object, context: Mapping[str, Any]) -> int | float:
    left, right = _eval_ordered(payload, context, op="add")
    return left + right


def _eval_sub(payload: object, context: Mapping[str, Any]) -> int | float:
    left, right = _eval_ordered(payload, context, op="sub")
    return left - right


def _eval_mul(payload: object, context: Mapping[str, Any]) -> int | float:
    left, right = _eval_ordered(payload, context, op="mul")
    return left * right


def _eval_div(payload: object, context: Mapping[str, Any]) -> float:
    left, right = _eval_ordered(payload, context, op="div")
    if right == 0:
        raise SwdExpressionError("division by zero")
    return float(left) / float(right)


def _eval_exists(payload: object, context: Mapping[str, Any]) -> bool:
    if isinstance(payload, Mapping) and set(payload) <= {"var", "default"}:
        path = payload.get("var")
    elif isinstance(payload, str):
        path = payload
    else:
        raise SwdExpressionError("exists requires a var path")
    if not isinstance(path, str):
        raise SwdExpressionError("exists requires a var path")
    return resolve_path(context, path) is not MISSING


OPS: dict[str, ExprOp] = {
    "eq": _eval_eq,
    "ne": _eval_ne,
    "lt": _eval_lt,
    "lte": _eval_lte,
    "gt": _eval_gt,
    "gte": _eval_gte,
    "and": _eval_and,
    "or": _eval_or,
    "not": _eval_not,
    "in": _eval_in,
    "add": _eval_add,
    "sub": _eval_sub,
    "mul": _eval_mul,
    "div": _eval_div,
    "exists": _eval_exists,
}
