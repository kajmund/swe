import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE_NAME = "hy4_" + "research_policy"


def test_agent_contract_does_not_import_the_hy4_package() -> None:
    offenders = []
    for path in ROOT.rglob("*"):
        if path.suffix not in {".py", ".md"} or not path.is_file():
            continue
        if PACKAGE_NAME in path.read_text(encoding="utf-8"):
            offenders.append(path.relative_to(ROOT).as_posix())
    assert offenders == []


def test_offline_adapter_imports_no_experiment_package() -> None:
    tree = ast.parse((ROOT / "agent_contract" / "offline_adapter.py").read_text(encoding="utf-8"))
    modules = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.append(node.module)
    assert modules == ["__future__", "collections.abc", "decimal", "typing", "pydantic"]
