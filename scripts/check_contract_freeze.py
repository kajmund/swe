#!/usr/bin/env python3
"""A changed contract needs a version bump for its *type*, not for being touched.

Two failure modes this gate must survive:

1. **Silent drift** — a journal field is renamed, the fixtures and manifest are
   regenerated, everything stays green, and every existing journal file is now
   misinterpreted.
2. **False alarms** — bumping `CONTRACT_VERSION` necessarily edits the file that
   also holds the journal codec, and regenerating a fixture changes example
   values. Neither may demand an unrelated journal schema bump.

So the gate works on **contract types** and compares a **per-file fingerprint**
of the wire form and public API, not paths:

    SWD form                      -> SWD_VERSION
    journal wire form             -> JOURNAL_SCHEMA_VERSION
    needs/evidence/operations API -> CONTRACT_VERSION

A type is "affected" when its fingerprint changed. Only an affected type needs
its version bumped. What counts as a change is deliberately narrow and
fail-closed:

* Every JSON value is compared. Renaming a key, changing a required list, an
  enum, a const, a pattern, a limit or any unknown scalar is a contract change.
* Example values are ignored only at **exact `(file, JSON pointer)` pairs**
  listed in ``IGNORED_EXAMPLES``. There are no global key names and no
  wildcards: an unnoticed scalar anywhere else still fails the gate.
* Values that embed a version constant are owned by that constant: they are
  listed in ``EMBEDDED``, checked against the owning constant's value at the
  same revision, and normalised to ``version:<CONSTANT>`` only when they match.
  A mismatch keeps the raw value (fail-closed). This is what lets a
  ``CONTRACT_VERSION`` bump regenerate the journal header without demanding a
  ``JOURNAL_SCHEMA_VERSION`` bump.
* A version constant's value is read from its **top-level declaration**, never
  from raw text: Python uses its AST and Rust requires a crate-scope
  ``pub const``. A nested assignment/const, comment or string literal that
  merely spells ``JOURNAL_SCHEMA_VERSION = 2`` is not a bump. Missing,
  duplicated or non-literal declarations read as "no version" and fail closed.
* The SWD schema keeps every semantic value (required, enum, const, pattern,
  bounds, refs); only its exact version location is normalised.
* Journal fixtures keep every ``kind`` value; example ids, timestamps,
  amounts/tariffs, actions, reasons, run ids and definition hashes are ignored
  at the frozen 11 entries' exact pointers. The SWD flow fixture keeps all
  values.
* Rust declarations are compared as whole declarations — including multi-line
  parameter lists with balanced ``()``, ``[]``, ``<>`` and ``where`` clauses,
  and struct/enum/trait bodies — with comments and string/char literals
  handled correctly. A signature that only changed on a later line is still a
  change.

Renames are diffed with ``--no-renames``: a file moved away from a frozen path
appears as a deletion plus an addition, and the deletion is a contract change
that needs the owning type's version bump (and a documented decision).

Every registered head version is validated before the "no contract path
changed" early return, independent of what the fingerprints say: if a
contract's files exist at the head, its version declaration must exist at the
head. Bootstrap may only excuse a *missing base value* (the first freeze, or a
package move in the same PR); it may never excuse a missing head declaration.

Bootstrap: while `contracts/` does not exist at the base commit, the initial
freeze may start at version 1 with a documented decision. The exception is
self-closing — once contracts exist at the base it can never apply again.

Editing `contracts/README.md` is documentation, not a contract change: the
structural test still pins every `*.json` fixture in `MANIFEST.sha256`.

Usage:
    python3 scripts/check_contract_freeze.py --base <sha> --head <sha>
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import subprocess
import sys
from pathlib import Path

# Overridable so the mutation matrix can point the gate at a throwaway repo.
ROOT = Path(os.environ.get("CONTRACT_FREEZE_ROOT") or Path(__file__).resolve().parent.parent)

DECISION_MARKERS = ("Kontraktsändring:", "Contract change:")
DECISION_DIR = "docs/swe-standalone/"
GUARD_SCRIPT = "scripts/check_contract_freeze.py"

SWD = "swd"
JOURNAL = "journal"
API = "contracts"

VERSIONS = {
    SWD: ("python/swe/schema.py", "SWD_VERSION"),
    JOURNAL: ("crates/swe-native/src/journal.rs", "JOURNAL_SCHEMA_VERSION"),
    API: ("crates/swe-reference/src/journal.rs", "CONTRACT_VERSION"),
}

VERSION_CONSTANTS = frozenset(name for _, name in VERSIONS.values())

FIXTURES = {
    SWD: ("contracts/swd-v1-workflow.json",),
    JOURNAL: ("contracts/journal-v1-events.json", "contracts/journal-v1-header.json"),
}

SOURCES = {
    SWD: ("crates/swe-native/src/definition.rs",),
    JOURNAL: ("crates/swe-native/src/journal.rs", "crates/swe-reference/src/journal.rs"),
    API: (
        "crates/swe-native/src/needs.rs",
        "crates/swe-native/src/evidence.rs",
        "crates/swe-native/src/operations.rs",
    ),
}

# Quoted wire keys live as string literals in the reference codec, not as
# declarations, so they are collected separately.
WIRE_KEY_FILES = {"crates/swe-reference/src/journal.rs"}

# Version declaration files must be watched even when their own contract shape is
# unchanged: deleting `SWD_VERSION` from schema.py changes nothing else, and
# without this the deletion would not even count as a contract path change.
VERSION_FILES = tuple(dict.fromkeys(path for path, _ in VERSIONS.values()))

EXTRA_CONTRACT_PATHS = ("python/swe/swd_v1.schema.json",)

# Documentation that lives next to the fixtures but is not part of the frozen
# surface. Editing it must not demand a contract decision; every *.json fixture
# is still pinned by the structural manifest test.
CONTRACT_DOCS = frozenset({"contracts/README.md"})

CONTRACT_PATHS = (
    "contracts/",
    GUARD_SCRIPT,
    *EXTRA_CONTRACT_PATHS,
    *VERSION_FILES,
    *[path for paths in FIXTURES.values() for path in paths],
    *[path for paths in SOURCES.values() for path in paths],
    *WIRE_KEY_FILES,
)


def contract_path(path: str) -> bool:
    """True when a changed path belongs to the frozen contract surface."""
    return path not in CONTRACT_DOCS and path.startswith(CONTRACT_PATHS)

# ---------------------------------------------------------------------------
# Exact example values the frozen fixtures may change without a contract change.
#
# These are (file, JSON pointer) pairs, never key names and never wildcards.
# The journal fixture has exactly 11 frozen entries; the pointers below cover
# the ids, timestamps, amounts/tariffs, actions, reasons, run id and definition
# hash of those entries. Everything else — above all `kind` — is contract form.
# ---------------------------------------------------------------------------
IGNORED_EXAMPLES: dict[str, frozenset[str]] = {
    "contracts/journal-v1-events.json": frozenset(
        {
            # 0 AttemptStarted
            "/0/action",
            "/0/attempt_id",
            "/0/delivery_key",
            "/0/invocation_id",
            "/0/started_at_ms",
            # 1 ChargeReserved
            "/1/amount_milli",
            "/1/attempt_id",
            "/1/charge_id",
            "/1/delivery_key",
            "/1/reserved_at_ms",
            "/1/tariff_milli",
            # 2 ResultConfirmed
            "/2/attempt_id",
            "/2/confirmed_at_ms",
            # 3 ChargeSettled
            "/3/amount_milli",
            "/3/charge_id",
            "/3/settled_at_ms",
            # 4 AttemptStarted
            "/4/action",
            "/4/attempt_id",
            "/4/delivery_key",
            "/4/invocation_id",
            "/4/started_at_ms",
            # 5 ChargeReserved
            "/5/amount_milli",
            "/5/attempt_id",
            "/5/charge_id",
            "/5/delivery_key",
            "/5/reserved_at_ms",
            "/5/tariff_milli",
            # 6 ChargePendingReconciliation
            "/6/at_ms",
            "/6/charge_id",
            "/6/reason",
            # 7 AttemptAbandoned
            "/7/abandoned_at_ms",
            "/7/attempt_id",
            "/7/reason",
            # 8 AttemptStarted
            "/8/action",
            "/8/attempt_id",
            "/8/delivery_key",
            "/8/invocation_id",
            "/8/started_at_ms",
            # 9 ChargeReserved
            "/9/amount_milli",
            "/9/attempt_id",
            "/9/charge_id",
            "/9/delivery_key",
            "/9/reserved_at_ms",
            "/9/tariff_milli",
            # 10 ChargeReleased
            "/10/charge_id",
            "/10/reason",
            "/10/released_at_ms",
        }
    ),
    "contracts/journal-v1-header.json": frozenset({"/run_id", "/definition_sha256"}),
}

# ---------------------------------------------------------------------------
# Exact (file, JSON pointer) locations that embed a *version constant's value*.
# The value is owned by the named constant: if it matches the constant's value
# at this revision it is normalised to `version:<CONSTANT>`; otherwise the raw
# value is kept, i.e. it becomes a normal contract change (fail-closed).
#
# The journal header emitter writes CONTRACT_VERSION into both `contract_version`
# and the recorded `mock.supplier_facts` dependency version, so both pointers are
# owned by CONTRACT_VERSION. The SWD schema's `version` const is owned by
# SWD_VERSION; no other schema location is ignored.
# ---------------------------------------------------------------------------
EMBEDDED: dict[tuple[str, str], str] = {
    ("contracts/journal-v1-header.json", "/schema_version"): "JOURNAL_SCHEMA_VERSION",
    ("contracts/journal-v1-header.json", "/contract_version"): "CONTRACT_VERSION",
    (
        "contracts/journal-v1-header.json",
        "/dependency_versions/mock.supplier_facts",
    ): "CONTRACT_VERSION",
    ("python/swe/swd_v1.schema.json", "/properties/version/const"): "SWD_VERSION",
}


def git(*args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=ROOT, capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        raise SystemExit(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


_PATHS_AT: dict[str, frozenset[str]] = {}


def paths_at(rev: str) -> frozenset[str]:
    """File paths in a revision, read once per checker process."""
    cached = _PATHS_AT.get(rev)
    if cached is None:
        cached = frozenset(git("ls-tree", "-r", "--name-only", rev).splitlines())
        _PATHS_AT[rev] = cached
    return cached


def path_exists_at(rev: str, path: str) -> bool:
    if path.endswith("/"):
        return any(candidate.startswith(path) for candidate in paths_at(rev))
    return path in paths_at(rev)


_READ_AT: dict[tuple[str, str], str | None] = {}


def read_at(rev: str, path: str) -> str | None:
    """One file's content at a revision, read once per checker process."""
    key = (rev, path)
    if key in _READ_AT:
        return _READ_AT[key]
    result = subprocess.run(
        ["git", "show", f"{rev}:{path}"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    blob = result.stdout if result.returncode == 0 else None
    _READ_AT[key] = blob
    return blob


_CHANGED_PATHS: dict[tuple[str, str], tuple[str, ...]] = {}


def changed_paths(base: str, head: str) -> list[str]:
    """Changed paths with rename detection disabled.

    A rename must be visible as a deletion of the old path plus an addition of
    the new one; otherwise moving a frozen fixture out of `contracts/` would
    look like an unrelated new file and quietly drop the contract.
    """
    key = (base, head)
    cached = _CHANGED_PATHS.get(key)
    if cached is None:
        merge_base = git("merge-base", base, head).strip()
        cached = tuple(
            line
            for line in git(
                "diff", "--no-renames", "--name-only", merge_base, head
            ).splitlines()
            if line
        )
        _CHANGED_PATHS[key] = cached
    return list(cached)


def _declared_literal(tokens: list[_Token], index: int) -> str | None:
    """The literal value of the declaration whose name token is at ``index``.

    A declaration without a literal value reads as None, so the caller fails
    closed instead of guessing.
    """
    cursor = index + 1
    while cursor < len(tokens) and tokens[cursor].text not in ("=", ";"):
        if tokens[cursor].line_start and tokens[cursor].text in DECLARATION_KEYWORDS:
            return None
        cursor += 1
    if cursor >= len(tokens) or tokens[cursor].text != "=":
        return None
    cursor += 1
    if cursor >= len(tokens):
        return None
    literal = tokens[cursor].text
    if len(literal) >= 2 and literal[0] == '"' and literal[-1] == '"':
        return literal[1:-1]
    if re.fullmatch(r"\d+(?:\.\d+)*", literal):
        return literal
    return None


def _python_version(source: str, name: str) -> str | None:
    """Read exactly one literal assignment at Python module scope."""
    try:
        module = ast.parse(source)
    except SyntaxError:
        return None
    declarations = 0
    values: list[str] = []
    for statement in module.body:
        value_node: ast.expr | None = None
        if (
            isinstance(statement, ast.Assign)
            and len(statement.targets) == 1
            and isinstance(statement.targets[0], ast.Name)
            and statement.targets[0].id == name
        ):
            value_node = statement.value
        elif (
            isinstance(statement, ast.AnnAssign)
            and isinstance(statement.target, ast.Name)
            and statement.target.id == name
            and statement.value is not None
        ):
            value_node = statement.value
        else:
            continue
        declarations += 1
        if value_node is None or not isinstance(value_node, ast.Constant):
            continue
        value = value_node.value
        if isinstance(value, bool) or not isinstance(value, (str, int, float)):
            continue
        values.append(str(value))
    return values[0] if declarations == 1 and len(values) == 1 else None


def _rust_version(source: str, name: str) -> str | None:
    """Read exactly one literal ``pub const`` declaration at crate scope."""
    tokens = _tokenize(source)
    declarations = 0
    values: list[str] = []
    brace_depth = 0
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token.text == "}":
            brace_depth = max(0, brace_depth - 1)
        if brace_depth == 0 and token.text == "pub":
            cursor = index + 1
            if cursor < len(tokens) and tokens[cursor].text == "(":
                cursor = _matching(tokens, cursor, "(", ")") + 1
            if (
                cursor + 1 < len(tokens)
                and tokens[cursor].text == "const"
                and tokens[cursor + 1].text == name
            ):
                declarations += 1
                value = _declared_literal(tokens, cursor + 1)
                if value is not None:
                    values.append(value)
        if token.text == "{":
            brace_depth += 1
        index += 1
    return values[0] if declarations == 1 and len(values) == 1 else None


def read_version(rev: str, path: str, name: str) -> str | None:
    """A registered version constant's value, read from its declaration.

    ``SWD_VERSION = "1.0"`` must be a top-level Python assignment and
    ``pub const CONTRACT_VERSION: u32 = 1;`` must be a crate-scope Rust
    declaration. A nested assignment/const, comment or string literal cannot
    stand in for a version bump. Missing, duplicated or malformed declarations
    read as None (fail closed).
    """
    blob = read_at(rev, path)
    if blob is None:
        return None
    if path.endswith(".py"):
        return _python_version(blob, name)
    return _rust_version(blob, name)


def version_of(contract: str, rev: str) -> str | None:
    path, name = VERSIONS[contract]
    return read_version(rev, path, name)


def constant_value(rev: str, constant: str) -> str | None:
    """Read a registered version constant's value by its constant name."""
    for path, name in VERSIONS.values():
        if name == constant:
            return read_version(rev, path, name)
    raise KeyError(f"unknown version constant {constant}")


def _pointer_escape(key: str) -> str:
    return key.replace("~", "~0").replace("/", "~1")


def _json_type(value: object) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    return type(value).__name__


def _scalar_text(value: object) -> str | None:
    """The text form a version constant would be compared against."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, (int, str)):
        return str(value)
    return None


def fingerprint_value(path: str, value: object, pointer: str, rev: str) -> object:
    """Fingerprint one JSON node: structure + every value, plus the two rules.

    ``IGNORED_EXAMPLES`` replaces an exact example value with a type-tagged
    marker (the value may change; the type is still form). ``EMBEDDED`` replaces
    a version-owner value with `version:<CONSTANT>` only when it matches the
    constant at this revision, and keeps the raw value otherwise.
    """
    if isinstance(value, dict):
        return {
            key: fingerprint_value(
                path, item, pointer + "/" + _pointer_escape(str(key)), rev
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [
            fingerprint_value(path, item, f"{pointer}/{index}", rev)
            for index, item in enumerate(value)
        ]

    embedded = EMBEDDED.get((path, pointer))
    if embedded is not None:
        owned = constant_value(rev, embedded)
        text = _scalar_text(value)
        if owned is not None and text is not None and text == owned:
            return f"version:{embedded}"
        return value

    if pointer in IGNORED_EXAMPLES.get(path, frozenset()):
        return f"<example:{_json_type(value)}>"
    return value


def file_fingerprint(rev: str, path: str) -> object | None:
    """Per-file fingerprint of a JSON contract fixture."""
    blob = read_at(rev, path)
    if blob is None:
        return None
    try:
        value = json.loads(blob)
    except json.JSONDecodeError:
        return None
    return fingerprint_value(path, value, "", rev)


def wire_keys(rev: str, path: str) -> object | None:
    blob = read_at(rev, path)
    if blob is None:
        return None
    return sorted(set(re.findall(r'"([a-z][a-z0-9_]{2,})"', blob)))


# ---------------------------------------------------------------------------
# Rust declaration fingerprinting
# ---------------------------------------------------------------------------

DECLARATION_KEYWORDS = frozenset({"const", "static", "fn", "struct", "enum", "trait", "type"})
STRUCT_LIKE = frozenset({"struct", "enum", "trait"})


class _Token:
    __slots__ = ("text", "line_start")

    def __init__(self, text: str, line_start: bool) -> None:
        self.text = text
        self.line_start = line_start


def _skip_block_comment(source: str, index: int) -> int:
    depth = 1
    index += 2
    length = len(source)
    while index < length and depth:
        if source.startswith("/*", index):
            depth += 1
            index += 2
        elif source.startswith("*/", index):
            depth -= 1
            index += 2
        else:
            index += 1
    return index


def _literal_end(source: str, index: int) -> int | None:
    """End index of a string or char literal starting at ``index`` (else None)."""
    length = len(source)
    if index >= length:
        return None

    raw = re.match(r"(?:b|c)?r(#*)\"", source[index : index + 24])
    if raw is not None:
        close = '"' + raw.group(1)
        found = source.find(close, index + raw.end())
        return length if found < 0 else found + len(close)

    char = source[index]
    if char == '"' or (char in "bc" and index + 1 < length and source[index + 1] == '"'):
        cursor = index + 1 if char == '"' else index + 2
        while cursor < length:
            if source[cursor] == "\\":
                cursor += 2
                continue
            if source[cursor] == '"':
                return cursor + 1
            cursor += 1
        return length

    if char == "'":
        # Char literal vs lifetime: `'x'`, `'\n'`, `'\u{..}'` are literals;
        # `'static` is a lifetime and stays punctuation.
        if index + 1 < length and source[index + 1] == "\\":
            cursor = index + 2
            if cursor < length and source[cursor] == "u" and source[cursor + 1 : cursor + 2] == "{":
                close = source.find("}", cursor)
                if close < 0:
                    return None
                cursor = close + 1
            if cursor < length and source[cursor] == "'":
                return cursor + 1
            return None
        if index + 2 < length and source[index + 2] == "'":
            return index + 3
        return None

    return None


def _skip_attribute(source: str, index: int) -> int:
    """Skip ``#[...]`` / ``#![...]`` starting at ``#``."""
    cursor = index + 1
    if cursor < len(source) and source[cursor] == "!":
        cursor += 1
    if cursor >= len(source) or source[cursor] != "[":
        return index + 1
    depth = 0
    while cursor < len(source):
        literal = _literal_end(source, cursor)
        if literal is not None:
            cursor = literal
            continue
        if source.startswith("//", cursor):
            newline = source.find("\n", cursor)
            cursor = len(source) if newline < 0 else newline
            continue
        if source.startswith("/*", cursor):
            cursor = _skip_block_comment(source, cursor)
            continue
        if source[cursor] == "[":
            depth += 1
        elif source[cursor] == "]":
            depth -= 1
            if depth == 0:
                return cursor + 1
        cursor += 1
    return len(source)


def _tokenize(source: str) -> list[_Token]:
    """Tokens with comments/attributes dropped and line-start tracked.

    String and char literals are single opaque tokens, so braces, semicolons
    and commas inside them cannot unbalance a declaration.
    """
    tokens: list[_Token] = []
    index = 0
    length = len(source)
    line_start = True
    while index < length:
        char = source[index]
        if char in " \t\r":
            index += 1
            continue
        if char == "\n":
            line_start = True
            index += 1
            continue
        if source.startswith("//", index):
            newline = source.find("\n", index)
            index = length if newline < 0 else newline
            continue
        if source.startswith("/*", index):
            index = _skip_block_comment(source, index)
            continue
        if char == "#":
            index = _skip_attribute(source, index)
            continue

        literal = _literal_end(source, index)
        if literal is not None:
            tokens.append(_Token(source[index:literal], line_start))
            line_start = False
            index = literal
            continue

        if char.isalpha() or char == "_":
            end = index + 1
            while end < length and (source[end].isalnum() or source[end] == "_"):
                end += 1
            tokens.append(_Token(source[index:end], line_start))
            line_start = False
            index = end
            continue

        if char.isdigit():
            end = index + 1
            while end < length and (source[end].isalnum() or source[end] in "._"):
                end += 1
            tokens.append(_Token(source[index:end], line_start))
            line_start = False
            index = end
            continue

        if source.startswith(("::", "->", "=>"), index):
            tokens.append(_Token(source[index : index + 2], line_start))
            line_start = False
            index += 2
            continue

        tokens.append(_Token(char, line_start))
        line_start = False
        index += 1
    return tokens


def _matching(tokens: list[_Token], index: int, opener: str, closer: str) -> int:
    """Index of the closer matching the opener at ``index`` (or len-1)."""
    depth = 0
    for cursor in range(index, len(tokens)):
        text = tokens[cursor].text
        if text == opener:
            depth += 1
        elif text == closer:
            depth -= 1
            if depth == 0:
                return cursor
    return len(tokens) - 1


def _declaration_name(tokens: list[_Token], keyword_index: int) -> str | None:
    cursor = keyword_index + 1
    while cursor < len(tokens):
        text = tokens[cursor].text
        if text in {"fn", "unsafe", "async", "extern", "const", "pub"}:
            cursor += 1
            continue
        if re.match(r"^[A-Za-z_]", text):
            return text
        return None
    return None


def _declaration_end(tokens: list[_Token], keyword_index: int, keyword: str) -> tuple[int, int]:
    """Return ``(slice_end, next_index)`` for the declaration at keyword_index.

    ``slice_end`` is the end of the text the fingerprint keeps: for functions,
    consts, statics, type aliases and trait methods it stops before the body
    (the whole signature, including multi-line parameters, where clauses and
    balanced generics); for structs, enums and traits it includes the body.
    """
    paren = bracket = angle = 0
    cursor = keyword_index + 1
    while cursor < len(tokens):
        text = tokens[cursor].text
        if text == "(":
            paren += 1
        elif text == ")":
            paren = max(0, paren - 1)
        elif text == "[":
            bracket += 1
        elif text == "]":
            bracket = max(0, bracket - 1)
        elif text == "<":
            angle += 1
        elif text == ">":
            angle = max(0, angle - 1)
        elif text == "{" and paren == 0 and bracket == 0 and angle == 0:
            body_end = _matching(tokens, cursor, "{", "}")
            slice_end = body_end + 1 if keyword in STRUCT_LIKE else cursor
            return slice_end, body_end + 1
        elif text == ";" and paren == 0 and bracket == 0 and angle == 0:
            return cursor + 1, cursor + 1
        cursor += 1
    return len(tokens), len(tokens)


def _declarations(tokens: list[_Token]) -> set[str]:
    declared: set[str] = set()
    index = 0
    total = len(tokens)
    while index < total:
        token = tokens[index]
        start = None
        keyword_index = None
        if token.line_start:
            if token.text == "pub":
                cursor = index + 1
                if cursor < total and tokens[cursor].text == "(":
                    cursor = _matching(tokens, cursor, "(", ")") + 1
                if cursor < total and tokens[cursor].text in DECLARATION_KEYWORDS:
                    start, keyword_index = index, cursor
            elif token.text in DECLARATION_KEYWORDS:
                start, keyword_index = index, index
        if start is not None and keyword_index is not None:
            keyword = tokens[keyword_index].text
            name = _declaration_name(tokens, keyword_index)
            slice_end, next_index = _declaration_end(tokens, keyword_index, keyword)
            if name not in VERSION_CONSTANTS:
                declared.add(" ".join(item.text for item in tokens[start:slice_end]))
            index = max(next_index, index + 1)
            continue
        index += 1
    return declared


def source_shape(rev: str, path: str) -> object | None:
    """The declared API/wire surface of a Rust file, ignoring bodies and comments."""
    blob = read_at(rev, path)
    if blob is None:
        return None
    return sorted(_declarations(_tokenize(blob)))


def contract_shape(contract: str, rev: str) -> dict[str, object]:
    parts: dict[str, object] = {}
    for path in FIXTURES.get(contract, ()):
        parts[path] = file_fingerprint(rev, path)
    for path in EXTRA_CONTRACT_PATHS:
        if contract is SWD:
            parts[path] = file_fingerprint(rev, path)
    for path in SOURCES.get(contract, ()):
        parts[path] = source_shape(rev, path)
    if contract is JOURNAL:
        for path in WIRE_KEY_FILES:
            parts[path + "#keys"] = wire_keys(rev, path)
    return parts


def affected_contracts(base: str, head: str) -> list[str]:
    return [
        contract
        for contract in VERSIONS
        if contract_shape(contract, base) != contract_shape(contract, head)
    ]


def contract_files(contract: str) -> tuple[str, ...]:
    """Every file that belongs to a contract type, including its version file."""
    files: list[str] = list(FIXTURES.get(contract, ()))
    if contract is SWD:
        files.extend(EXTRA_CONTRACT_PATHS)
    files.extend(SOURCES.get(contract, ()))
    if contract is JOURNAL:
        files.extend(WIRE_KEY_FILES)
    files.append(VERSIONS[contract][0])
    return tuple(dict.fromkeys(files))


def missing_head_versions(head: str) -> list[str]:
    """Every registered version must exist at head whenever its files exist.

    This runs for all contracts, before and independent of the "no contract path
    changed" early return: deleting a version constant alone is still a broken
    contract, even though nothing else changed.
    """
    failures: list[str] = []
    for contract, (path, name) in VERSIONS.items():
        if not any(path_exists_at(head, candidate) for candidate in contract_files(contract)):
            continue
        if read_version(head, path, name) is None:
            failures.append(
                f"{contract}: {name} is missing at the head ({path}); a registered "
                "version declaration must exist whenever the contract's files exist"
            )
    return failures


def documented_decision(base: str, head: str) -> bool:
    merge_base = git("merge-base", base, head).strip()
    for path in changed_paths(base, head):
        if not path.startswith(DECISION_DIR) or not path.endswith(".md"):
            continue
        diff = git("diff", "-U0", merge_base, head, "--", path)
        for line in diff.splitlines():
            if line.startswith("+") and not line.startswith("+++"):
                if any(marker in line for marker in DECISION_MARKERS):
                    return True
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="")
    parser.add_argument("--head", default="HEAD")
    args = parser.parse_args()

    if not args.base:
        print("contract freeze: no base commit, skipping (not a pull request)")
        return 0

    # The gate itself may not be deleted: if it exists at the base it must still
    # exist at the head, whatever else changed.
    if path_exists_at(args.base, GUARD_SCRIPT) and not path_exists_at(args.head, GUARD_SCRIPT):
        print(
            "\ncontract freeze FAILED: the contract gate script was deleted. "
            f"Restore {GUARD_SCRIPT}; a gate that can be removed is not a gate.",
            file=sys.stderr,
        )
        return 1

    # A registered version declaration is contract state, not a comment: it must
    # survive even in a PR that touches no contract path. Validate this before
    # the early return and independent of the fingerprint comparison.
    missing_versions = missing_head_versions(args.head)
    if missing_versions:
        print("\ncontract freeze FAILED:", file=sys.stderr)
        for failure in missing_versions:
            print(f"  - {failure}", file=sys.stderr)
        return 1

    changed = changed_paths(args.base, args.head)
    contract_touched = [path for path in changed if contract_path(path)]
    if not contract_touched:
        print("contract freeze: no contract path changed")
        return 0

    documented = documented_decision(args.base, args.head)
    affected = affected_contracts(args.base, args.head)
    bootstrap = not path_exists_at(args.base, "contracts/")

    print("contract freeze:")
    for path in contract_touched:
        print(f"  touched: {path}")
    print(f"  affected types: {affected or 'none (fingerprint unchanged)'}")
    print(f"  bootstrap (no contracts/ at base): {bootstrap}")
    print(f"  documented decision: {documented}")

    if not documented:
        print(
            "\ncontract freeze FAILED: a contract path changed without a documented "
            f"decision (a line containing one of {DECISION_MARKERS} in {DECISION_DIR}).",
            file=sys.stderr,
        )
        return 1

    failures: list[str] = []
    for contract in affected:
        before = version_of(contract, args.base)
        after = version_of(contract, args.head)
        if before is None or after is None:
            # During the initial freeze the files may have moved in the same PR
            # (the Python package went from backend/app/swe to python/swe), so
            # there is no prior version to read. That is bootstrap, not drift.
            if bootstrap and before is None and after is not None:
                print(
                    f"  {contract}: no version at the base (new or moved in this "
                    "PR) — allowed by the bootstrap exception"
                )
                continue
            if bootstrap and after is None:
                path, name = VERSIONS[contract]
                failures.append(
                    f"{contract}: no {name} at the head either ({path}); bootstrap "
                    "does not excuse a missing version"
                )
                continue
            path, name = VERSIONS[contract]
            failures.append(f"{contract}: could not read {name} from {path}")
            continue
        if before == after:
            if bootstrap:
                print(
                    f"  {contract}: form changed but version stays {before} — allowed by "
                    "the bootstrap exception (contracts/ does not exist at the base)"
                )
            else:
                path, name = VERSIONS[contract]
                failures.append(
                    f"{contract}: wire form changed but its version stayed {before}; "
                    f"bump {name} in {path}"
                )
        else:
            print(f"  {contract}: version {before} -> {after}")

    if failures:
        print("\ncontract freeze FAILED:", file=sys.stderr)
        for failure in failures:
            print(f"  - {failure}", file=sys.stderr)
        return 1
    print("contract freeze OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
