#!/usr/bin/env python3
"""Fail closed when package, dependency, or license metadata drifts."""

from __future__ import annotations

import json
import subprocess
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXPRESSION = "MIT OR Apache-2.0"
REPOSITORY = "https://github.com/kajmund/swe"
WORKSPACE_PACKAGES = {"swe-budget", "swe-native", "swe-reference"}
PUBLISH = {"swe-budget": True, "swe-native": True, "swe-reference": False}
ALLOWED_RUST_LICENSES = {
    "Apache-2.0 OR BSL-1.0",
    "Apache-2.0 OR MIT",
    "Apache-2.0 WITH LLVM-exception",
    "MIT",
    "MIT OR Apache-2.0",
    "MIT/Apache-2.0",
    "Unlicense OR MIT",
    "Zlib OR Apache-2.0 OR MIT",
    "(MIT OR Apache-2.0) AND Unicode-3.0",
}
LICENSE_COPIES = (
    "crates/swe-budget",
    "crates/swe-native",
    "crates/swe-reference",
    "python",
)


def load_toml(path: str) -> dict:
    return tomllib.loads((ROOT / path).read_text(encoding="utf-8"))


def cargo_metadata() -> dict:
    result = subprocess.run(
        ["cargo", "metadata", "--locked", "--all-features", "--format-version", "1"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def main() -> int:
    failures: list[str] = []
    notice = (ROOT / "THIRD-PARTY-NOTICES").read_text(encoding="utf-8")
    notice_lower = notice.lower()
    notice_lines = notice_lower.splitlines()

    for filename in ("LICENSE-MIT", "LICENSE-APACHE"):
        root_bytes = (ROOT / filename).read_bytes()
        for directory in LICENSE_COPIES:
            copy = ROOT / directory / filename
            if not copy.is_file() or copy.read_bytes() != root_bytes:
                failures.append(f"{copy.relative_to(ROOT)} is missing or differs from {filename}")

    workspace = load_toml("Cargo.toml")["workspace"]["package"]
    for key, expected in {
        "license": EXPRESSION,
        "repository": REPOSITORY,
        "homepage": REPOSITORY,
    }.items():
        if workspace.get(key) != expected:
            failures.append(f"workspace.package.{key} must be {expected!r}")

    for package, should_publish in PUBLISH.items():
        manifest = load_toml(f"crates/{package}/Cargo.toml")["package"]
        if manifest.get("publish") is not should_publish:
            failures.append(f"{package}: publish must be {should_publish}")
        for key in ("license", "repository", "homepage"):
            if manifest.get(key) != {"workspace": True}:
                failures.append(f"{package}: {key} must inherit from the workspace")

    for path in ("python/pyproject.toml", "crates/swe-native/pyproject.toml"):
        project = load_toml(path)["project"]
        if project.get("license") != EXPRESSION:
            failures.append(f"{path}: project.license must be {EXPRESSION!r}")
        expected_files = ["LICENSE-MIT", "LICENSE-APACHE"]
        if path == "crates/swe-native/pyproject.toml":
            expected_files.append("THIRD-PARTY-NOTICES")
        if project.get("license-files") != expected_files:
            failures.append(f"{path}: project.license-files must be {expected_files!r}")
        urls = project.get("urls", {})
        if urls.get("Repository") != REPOSITORY or urls.get("Homepage") != REPOSITORY:
            failures.append(f"{path}: repository/homepage metadata is stale")

    metadata = cargo_metadata()
    external = [package for package in metadata["packages"] if package.get("source")]
    if len(external) != 41:
        failures.append(f"expected 41 Rust dependencies, found {len(external)}")
    for package in external:
        expression = package.get("license")
        if expression not in ALLOWED_RUST_LICENSES:
            failures.append(
                f"Rust dependency {package['name']} {package['version']} has unreviewed "
                f"license {expression!r}"
            )
        marker = f"| [{package['name']}]".lower()
        version = f"| {package['version']} |".lower()
        if not any(line.startswith(marker) and version in line for line in notice_lines):
            failures.append(
                f"Rust dependency {package['name']} {package['version']} is absent from notices"
            )

    uv_packages = load_toml("python/uv.lock")["package"]
    python_external = [package for package in uv_packages if package["name"] != "swe-engine"]
    if len(python_external) != 14:
        failures.append(f"expected 14 Python dependencies, found {len(python_external)}")
    for package in python_external:
        marker = f"| {package['name']} | {package['version']} |".lower()
        if marker not in notice_lower:
            failures.append(
                f"Python dependency {package['name']} {package['version']} is absent from notices"
            )

    if "UNICODE LICENSE V3" not in notice:
        failures.append("THIRD-PARTY-NOTICES is missing unicode-ident's Unicode-3.0 text")
    native_notice = ROOT / "crates/swe-native/THIRD-PARTY-NOTICES"
    if not native_notice.is_file() or native_notice.read_text(encoding="utf-8") != notice:
        failures.append("crates/swe-native/THIRD-PARTY-NOTICES must match the root notice")
    if "swe-native](https://github.com/kajmund/swe)" in notice:
        failures.append("project crates must not be listed as third-party dependencies")

    if failures:
        print("license gate failed:")
        for failure in failures:
            print(f"- {failure}")
        return 1
    print(
        "license gate OK: project metadata, 41 Rust dependencies, "
        "14 Python dependencies, and notices agree"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
