"""Typed SWE runtime errors at the Python adapter boundary."""

from __future__ import annotations


class SweRuntimeError(ValueError):
    """Base error for the in-memory SWE runtime adapter."""

    def __init__(self, message: str, *, kind: str = "runtime", path: str = "") -> None:
        super().__init__(message)
        self.kind = kind
        self.path = path


class SwePreflightError(SweRuntimeError):
    def __init__(self, message: str, *, path: str = "") -> None:
        super().__init__(message, kind="preflight", path=path)


class SweHandlerError(SweRuntimeError):
    def __init__(self, message: str, *, path: str = "") -> None:
        super().__init__(message, kind="handler", path=path)


class SweUnsupportedError(SwePreflightError):
    def __init__(self, message: str, *, path: str = "") -> None:
        super().__init__(message, path=path)
        self.kind = "unsupported"
