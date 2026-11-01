"""Fail when the engine package grows a dependency on the application.

`python/swe` is the engine: it may import only its own modules, the standard
library and declared engine dependencies. Anything from `app.config`,
`app.jev` or `app.services` is a domain import and breaks the boundary the
extraction depends on (see ``docs/swe-standalone/10-pr1-b1-python-boundary.md``).

Application code may keep importing ``swe``; that direction is correct.
Ownership of the host concepts (``FEATURE_IDS``, ``WorkflowHostContext``,
``workflow_catalog_dir``) is asserted by the application's own test
``backend/tests/test_swe_host_context.py`` because it needs the host package.

The scan parses each module with ``ast`` instead of matching text, so
``from app import services``, indented imports inside functions, parenthesised
multi-line imports and dynamic import calls are all covered. A regex misses
most of those.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest

#: The installed engine package, next to this test directory.
PROJECT_DIR = Path(__file__).resolve().parents[1]
ENGINE_DIR = PROJECT_DIR / "swe"

#: Top-level application packages the engine must never reach into.
DOMAIN_ROOTS = frozenset({"app", "config", "jev", "services"})

#: Names that belong to a domain and must never be exported by the engine.
FORBIDDEN_EXPORTS = frozenset(
    {"FEATURE_IDS", "WorkflowHostContext", "workflow_catalog_dir"}
)

#: Dynamic import helpers that could hide a domain import from a static scan.
DYNAMIC_IMPORT_NAMES = frozenset({"__import__", "import_module"})
BANNED_BUILTINS = frozenset({"exec", "eval"})


def _modules() -> list[Path]:
    """Every engine module, including any future subpackage."""
    return sorted(ENGINE_DIR.rglob("*.py"))


def _tree(path: Path) -> ast.AST:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _imported_modules(tree: ast.AST) -> set[str]:
    """Every module the file imports, however the import is spelled."""
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
            # `from app import services` imports the `app.services` module.
            if node.module == "app":
                imported.update(f"app.{alias.name}" for alias in node.names)
    return imported


def _domain_violations(path: Path) -> list[str]:
    return [
        f"{path.name}: imports {module}"
        for module in sorted(_imported_modules(_tree(path)))
        if module.split(".")[0] in DOMAIN_ROOTS
    ]


def test_engine_package_has_no_domain_imports() -> None:
    offenders = [
        violation for path in _modules() for violation in _domain_violations(path)
    ]
    assert offenders == [], (
        "python/swe must not import the application; move the caller instead: "
        f"{offenders}"
    )


def test_engine_package_has_no_dynamic_import_or_exec() -> None:
    """No importlib/__import__/exec/eval escape hatch around the boundary."""
    offenders: list[str] = []
    for path in _modules():
        tree = _tree(path)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import | ast.ImportFrom):
                names = [alias.name for alias in node.names]
                if isinstance(node, ast.ImportFrom) and node.module:
                    names.append(node.module)
                if any(name.split(".")[0] == "importlib" for name in names):
                    offenders.append(f"{path.name}: imports importlib")
            elif isinstance(node, ast.Call):
                func = node.func
                called = (
                    func.id
                    if isinstance(func, ast.Name)
                    else func.attr if isinstance(func, ast.Attribute) else ""
                )
                if called in DYNAMIC_IMPORT_NAMES or called in BANNED_BUILTINS:
                    offenders.append(f"{path.name}: {called}() at line {node.lineno}")
    assert offenders == [], f"dynamic execution in the engine: {offenders}"


def test_engine_package_has_no_prompt_catalog_dependency() -> None:
    offenders = [
        path.name
        for path in _modules()
        if "prompt_catalog" in path.read_text(encoding="utf-8")
    ]
    assert offenders == [], (
        "the prompt catalog is host configuration and is injected, not imported: "
        f"{offenders}"
    )


def test_engine_public_api_excludes_host_concepts() -> None:
    import swe

    leaked = sorted(FORBIDDEN_EXPORTS.intersection(swe.__all__))
    assert leaked == [], f"host concepts exported by the engine: {leaked}"


def test_engine_imports_without_application_state(tmp_path: Path) -> None:
    """Importing the engine must not require config, JEV or a database.

    The subprocess runs outside the repository with the installed distribution,
    so a stray `backend/` checkout cannot satisfy an application import.
    """
    roots = ",".join(sorted(f"'{root}'" for root in DOMAIN_ROOTS))
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys, swe, swe.runtime_dispatch; "
            "roots = {" + roots + "}; "
            "print(','.join(sorted(m for m in sys.modules if m.split('.')[0] in roots)))",
        ],
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
    )
    assert result.returncode == 0, result.stderr
    loaded = [module for module in result.stdout.strip().split(",") if module]
    assert loaded == [], f"importing the engine pulled in the application: {loaded}"


@pytest.mark.parametrize("forbidden", sorted(FORBIDDEN_EXPORTS))
def test_engine_does_not_define_host_concepts(forbidden: str) -> None:
    import swe.schema

    assert not hasattr(swe, forbidden)
    assert not hasattr(swe.schema, forbidden)
