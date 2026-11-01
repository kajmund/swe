"""Canonical JSON / SHA-256 byte contract for SWD."""

from __future__ import annotations

import json

import pytest

from swe.canonicalize import canonical_json_bytes, canonicalize, sha256_hex
from swe.errors import SwdParseError


def test_object_keys_are_sorted() -> None:
    left = canonical_json_bytes({"b": 1, "a": 1})
    right = canonical_json_bytes({"a": 1, "b": 1})
    assert left == b'{"a":1,"b":1}'
    assert left == right


def test_unicode_is_nfc_and_unescaped() -> None:
    composed = canonical_json_bytes({"name": "é"})
    decomposed = canonical_json_bytes({"name": "e\u0301"})
    assert composed == decomposed
    assert composed == '{"name":"é"}'.encode("utf-8")


def test_crlf_normalizes_to_lf() -> None:
    assert canonical_json_bytes("a\r\nb") == canonical_json_bytes("a\nb")


def test_integer_floats_become_ints() -> None:
    assert canonical_json_bytes(1.0) == b"1"
    assert sha256_hex(1) == sha256_hex(1.0)


def test_nan_is_rejected() -> None:
    with pytest.raises(SwdParseError, match="NaN"):
        canonical_json_bytes(float("nan"))


def test_bool_is_not_an_int() -> None:
    assert canonical_json_bytes(True) == b"true"
    assert json.loads(canonical_json_bytes(True)) is True


def test_object_keys_are_nfc_normalized() -> None:
    composed = canonical_json_bytes({"é": 1})
    decomposed = canonical_json_bytes({"e\u0301": 1})
    assert composed == decomposed
    assert composed == '{"é":1}'.encode("utf-8")


def test_nfc_key_collision_is_rejected() -> None:
    with pytest.raises(SwdParseError, match="canonical key collision"):
        canonical_json_bytes({"é": 1, "e\u0301": 2})


def test_canonicalize_is_idempotent() -> None:
    value = {"e\u0301": 1.0, "child": ["a\r\nb"]}
    once = canonicalize(value)
    assert once == {"é": 1, "child": ["a\nb"]}
    assert canonicalize(once) == once
