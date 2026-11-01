"""SWD parse, validation, and expression errors."""

from __future__ import annotations


class SwdError(ValueError):
    """Base error for SWD workflow definition handling."""


class SwdParseError(SwdError):
    """Document is not JSON/YAML that can be reduced to a JSON object."""


class SwdValidationError(SwdError):
    """Definition fails schema, graph, or registry checks."""

    def __init__(self, message: str, *, path: str = "") -> None:
        super().__init__(message)
        self.path = path


class SwdExpressionError(SwdError):
    """Deterministic expression is malformed or not well-typed."""
