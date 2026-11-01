#!/usr/bin/env python3
"""The mutation gate: every critical mutation is classified, and drift blocks CI.

Why not "run cargo-mutants and require every mutant to die": a green mutation
number can lie (a tautological test passes every mutant), and some mutants are
genuinely equivalent or outside the contract. The owner's rule is therefore:

    every critical mutation is classified as detected, equivalent or excepted
    with a justification; new surviving mutations block CI.

This script executes a **verified baseline** of mutations instead of trusting a
number. Mutations are applied to a detached git worktree of committed HEAD with
its own CARGO_TARGET_DIR, so the checkout being committed is never modified.
Uncommitted working-tree changes are not covered: CI, where HEAD is the code
under review, is authoritative.

The classifications are decided by ``judge``, a pure function with a negative
test matrix in ``scripts/test_check_mutations.py`` and ``run_tests`` never treats
an exit code alone as evidence (a compiler error or a crashing test process also
exits non-zero):

* ``detected``   — the mutation must make the tests in ``expected_red`` fail: the
                   run exits non-zero and every expected test is among the parsed
                   ``test ... FAILED`` lines. If it stops failing, the test that
                   was supposed to catch it has rotted.
* ``equivalent`` — the mutation must keep the suite green: the run must exit 0
                   because the mutation cannot change behaviour, and the reason
                   says why.
* ``excepted``   — the mutation keeps the suite green and we accept that, with a
                   recorded justification for why the behaviour is not part of
                   the contract.

A run that cannot be read as green-or-red-by-named-failing-tests is a gate error
for *every* class: compilation errors, runs where no test harness produced
output, and runs where a test process crashed or failed to launch are not "the
suite stayed green". That is the strict-green rule: "no parsed failing test" is
not the same thing as "green".

The manifest lives in ``mutations/baseline.json`` next to this script's root.
Run locally with ``python3 scripts/check_mutations.py``: online by default, so a
cold machine can resolve the locked dependency set; add ``--offline`` once the
cache is warm. (CI fetches the locked set first and runs the gate with
``--offline``.) The gate's own negative cases run with
``python3 scripts/test_check_mutations.py``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MANIFEST = ROOT / "mutations" / "baseline.json"
CARGO = "cargo"
# Empty by default: a clean runner has no registry cache, so the first resolve
# needs the network. CI fetches the locked set explicitly and then runs the gate
# with --offline; a warm development machine can do the same.
CARGO_OFFLINE: list[str] = []
# Mutations are applied to an isolated worktree with its own target directory, so
# the checkout being committed is never modified. A lock file would not help:
# it cannot stop an unrelated `git add` from seeing a mutated file.
ISOLATED = {"worktree": "", "target": ""}
BUILD_ERROR = "the workspace did not compile"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_manifest(path: Path) -> list[dict]:
    data = json.loads(path.read_text())
    if data.get("version") != 1:
        raise SystemExit(f"unsupported mutation baseline version {data.get('version')!r}")
    mutations = data.get("mutations") or []
    if not mutations:
        raise SystemExit("the mutation baseline is empty; that would let everything pass")
    for index, mutation in enumerate(mutations):
        missing = {"id", "file", "find", "replace", "classification"} - set(mutation)
        if missing:
            raise SystemExit(f"mutation #{index} is missing {sorted(missing)}")
        if mutation["classification"] not in {"detected", "equivalent", "excepted"}:
            raise SystemExit(
                f"mutation {mutation['id']}: unknown classification "
                f"{mutation['classification']!r}"
            )
        if mutation["classification"] != "detected" and not mutation.get("reason"):
            raise SystemExit(
                f"mutation {mutation['id']}: {mutation['classification']} needs a reason"
            )
        file_value = mutation["file"]
        if not isinstance(file_value, str) or not file_value:
            raise SystemExit(f"mutation {mutation['id']}: file must be a non-empty path")
        manifest_path = PurePosixPath(file_value)
        if manifest_path.is_absolute() or ".." in manifest_path.parts:
            raise SystemExit(
                f"mutation {mutation['id']}: file must stay inside the isolated worktree"
            )
    return mutations


def read_test_output(output: str, returncode: int) -> tuple[list[str], str | None]:
    """Read one ``cargo test`` output as evidence, or say why it is not evidence.

    Pure parser shared with the negative-case matrix. Returns ``(parsed failing
    test names, run_error)``. ``run_error`` is ``None`` only when the output can
    be judged: the run exited 0, or it exited non-zero and every reported
    failure is a parsed ``test ... FAILED`` line. Compilation errors, runs where
    no test harness produced output, test processes that crash/abort (a green
    sibling binary can still print a ``test result:`` line), and launch failures
    all return a run_error: a non-zero exit is not evidence on its own.
    """
    failed = sorted(
        {
            line.split(" ... FAILED")[0].removeprefix("test ").strip()
            for line in output.splitlines()
            if line.startswith("test ") and line.rstrip().endswith("FAILED")
        }
    )
    reported_failed = sum(
        int(match.group(1))
        for match in re.finditer(
            r"^test result: \w+\. \d+ passed; (\d+) failed;", output, re.MULTILINE
        )
    )
    if "could not compile" in output:
        return failed, BUILD_ERROR
    for marker in (
        "process didn't exit successfully",
        "could not execute process",
        "test failed to run to completion",
    ):
        if marker in output:
            return failed, (
                "a test process crashed or failed to launch; that is not a "
                "judgeable test failure"
            )
    if "test result:" not in output:
        return failed, "no test harness produced output"
    if returncode == 0:
        if failed or reported_failed:
            return failed, "cargo exited 0 while reporting failing tests; refusing to trust the run"
        return failed, None
    # A non-zero exit is only judgeable when every reported failure is a named
    # `test ... FAILED` line. A crash/abort kills the process before libtest can
    # report the test, so the counts stop matching.
    if not failed or reported_failed != len(failed):
        return failed, (
            f"the run reported {reported_failed} failing test(s) but named "
            f"{len(failed)}; a test process probably crashed or failed to launch"
        )
    return failed, None


def run_tests() -> tuple[int, list[str], str | None]:
    """Run the workspace tests (unit and integration) and classify the output.

    Returns ``(exit code, parsed failing test names, run_error)``; see
    ``read_test_output`` for what makes a run judgeable at all.
    """
    result = subprocess.run(
        [CARGO, "test", *CARGO_OFFLINE, "--workspace", "--no-fail-fast"],
        cwd=ISOLATED["worktree"] or ROOT,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "CARGO_TARGET_DIR": ISOLATED["target"]
            or os.environ.get("CARGO_TARGET_DIR", str(ROOT / "target")),
        },
    )
    failed, run_error = read_test_output(result.stdout + result.stderr, result.returncode)
    return result.returncode, failed, run_error


def judge(
    mutation: dict,
    code: int,
    failed: list[str],
    run_error: str | None,
) -> tuple[bool, str, list[str]]:
    """Decide one mutation from one test run. Pure: no I/O, no globals.

    Returns ``(ok, outcome, problems)``. ``run_error`` comes from ``run_tests``;
    when it is set the run is not evidence and the gate fails for every
    classification. ``equivalent`` and ``excepted`` are green only on
    ``code == 0``: a suite can be red without leaving a parsed ``test ... FAILED``
    line behind (a test process that aborts is the common case).
    """
    mutation_id = mutation["id"]
    classification = mutation["classification"]
    if run_error is not None:
        outcome = "BUILD ERROR" if run_error == BUILD_ERROR else "RUN ERROR"
        return (
            False,
            outcome,
            [f"{mutation_id}: the test run is not evidence ({run_error})"],
        )
    if classification == "detected":
        expected = mutation.get("expected_red") or []
        if not expected:
            return (
                False,
                "NO EXPECTATION",
                [
                    f"{mutation_id}: a detected mutation must name the tests it is "
                    "expected to redden (expected_red)"
                ],
            )
        if code == 0:
            return (
                False,
                "MISSED",
                [
                    f"{mutation_id}: expected {expected} to fail, the suite stayed "
                    "green — the test that catches this has rotted"
                ],
            )
        missing = sorted(set(expected) - set(failed))
        if missing:
            return (
                False,
                "MISSED",
                [
                    f"{mutation_id}: expected {missing} to fail, but the red tests "
                    f"were {failed or 'none'} — wrong test was proven"
                ],
            )
        return True, "detected", []
    if code != 0:
        return (
            False,
            "DETECTED BUT MARKED " + classification.upper(),
            [
                f"{mutation_id}: classified {classification} but reddened the suite "
                f"({', '.join(sorted(failed)) or 'no named test'}); the baseline is stale"
            ],
        )
    return True, classification, []


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument(
        "--offline",
        action="store_true",
        help="add --offline to cargo; only valid once the dependency cache is warm",
    )
    args = parser.parse_args()
    if args.offline:
        CARGO_OFFLINE.append("--offline")

    mutations = load_manifest(args.manifest)

    failures: list[str] = []
    rows: list[tuple[str, str, str, str]] = []
    scratch = Path(tempfile.mkdtemp(prefix="swe-mutants-"))
    ISOLATED["worktree"] = str(scratch / "tree")
    ISOLATED["target"] = str(scratch / "target")
    try:
        subprocess.run(
            ["git", "worktree", "add", "--detach", ISOLATED["worktree"], "HEAD"],
            cwd=ROOT,
            check=True,
            capture_output=True,
        )
        baseline_code, baseline_failed, baseline_error = run_tests()
        if baseline_code != 0:
            print(
                "the suite is not green before mutating, so the gate cannot judge "
                f"anything (run error: {baseline_error}, failing: {baseline_failed})",
                file=sys.stderr,
            )
            return 1

        worktree_root = Path(ISOLATED["worktree"]).resolve()
        for mutation in mutations:
            path = (worktree_root / mutation["file"]).resolve()
            if not path.is_relative_to(worktree_root):
                failures.append(
                    f"{mutation['id']}: {mutation['file']} resolves outside the "
                    "isolated worktree"
                )
                rows.append((mutation["id"], mutation["classification"], "INVALID PATH", ""))
                continue
            original = path.read_text()
            digest = sha256(path)
            if mutation["find"] not in original:
                failures.append(
                    f"{mutation['id']}: the mutation does not apply "
                    f"(no match in {mutation['file']}); refusing to call that a pass"
                )
                rows.append((mutation["id"], mutation["classification"], "NOT APPLIED", ""))
                continue
            mutated = original.replace(mutation["find"], mutation["replace"], 1)
            if mutated == original:
                failures.append(f"{mutation['id']}: replacement produced no change")
                rows.append((mutation["id"], mutation["classification"], "NO CHANGE", ""))
                continue
            try:
                path.write_text(mutated)
                code, failed, run_error = run_tests()
            finally:
                path.write_text(original)
                if sha256(path) != digest:
                    raise SystemExit(f"{mutation['id']}: FAILED TO RESTORE {path}")

            ok, outcome, problems = judge(mutation, code, failed, run_error)
            failures.extend(problems)
            rows.append(
                (
                    mutation["id"],
                    mutation["classification"],
                    outcome if ok else "✗ " + outcome,
                    ", ".join(sorted(failed)[:3]),
                )
            )
    finally:
        # Exception-safe cleanup: Ctrl-C or a failed run must not leave a worktree
        # or a scratch target directory behind (the checkout is never modified).
        subprocess.run(
            ["git", "worktree", "remove", "--force", ISOLATED["worktree"]],
            cwd=ROOT,
            check=False,
            capture_output=True,
        )
        shutil.rmtree(scratch, ignore_errors=True)

    width = max(len(row[0]) for row in rows) if rows else 10
    print(f"{'mutation'.ljust(width)}  {'class':10}  {'outcome':10}  first failing tests")
    print("-" * (width + 60))
    for name, classification, outcome, tests in rows:
        print(f"{name.ljust(width)}  {classification:10}  {outcome:10}  {tests}")

    if failures:
        print("\nmutation gate FAILED:", file=sys.stderr)
        for failure in failures:
            print(f"  - {failure}", file=sys.stderr)
        return 1
    print(f"\nmutation gate OK: {len(rows)} mutations, every one as classified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
