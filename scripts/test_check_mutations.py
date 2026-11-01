#!/usr/bin/env python3
"""The negative-case matrix for the mutation gate's judge and manifest loader.

A gate is only worth what its negative tests prove. This matrix imports the
shipped gate module (no mocks, no cargo, no workspace checkout) and runs
``judge`` and ``read_test_output`` against the outcomes a real ``cargo test`` can
produce: green runs, clean test failures, compilation errors, and a test process
that aborts while a green sibling binary still prints ``test result:``. A green
run that could not have been red is the failure mode this whole programme keeps
finding, so every "this counts as green" rule has a case that must say "red".

Manifest validation is covered too: the baseline is the only way in, so an
unknown classification, a non-detected mutation without a reason, escaping file
paths, missing keys, an empty baseline and an unsupported version must all be
``SystemExit``.

Run with ``python3 scripts/test_check_mutations.py``; exits non-zero on any FAIL.
"""

from __future__ import annotations

import importlib.util
import json
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GATE_PATH = ROOT / "scripts" / "check_mutations.py"


def load_gate():
    spec = importlib.util.spec_from_file_location("check_mutations", GATE_PATH)
    if spec is None or spec.loader is None:
        raise SystemExit(f"cannot import {GATE_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


GATE = load_gate()
GREEN = 0
CLEAN_RED = 101

# Real cargo test output shapes (captured from this workspace and from the
# abort experiment the review ran against the shipped gate).
CLEAN_RED_OUTPUT = """\
running 2 tests
test tests::unit_ok ... FAILED

failures:

failures:
    tests::unit_ok

test result: FAILED. 0 passed; 1 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.00s

error: test failed, to rerun pass `--lib`
"""

CRASH_OUTPUT = """\
running 2 tests
test tests::crashes_when_mutated ... error: test failed, to rerun pass `--lib`
  process didn't exit successfully: `/tmp/swe-mutants-xxx/tree/target/debug/deps/a-abc123` (signal: 6, SIGABRT: process abort signal)
test smoke_ok ... ok
test result: ok. 1 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.00s
test result: ok. 0 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.00s
error: 1 target failed:
"""

BUILD_OUTPUT = """\
error[E0308]: mismatched types
 --> crates/swe-native/src/lib.rs:3:5
  |
3 |     0
  |     ^ expected `u8`, found `&str`

error: could not compile `swe-native` (lib test) due to 1 previous error
"""

NO_HARNESS_OUTPUT = """\
error: no test targets were found, yet testing was requested
"""

GREEN_OUTPUT = """\
running 1 test
test tests::unit_ok ... ok

test result: ok. 1 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.00s
"""

# A crash marker next to a clean failure whose counts *do* match: only the
# marker shows that another test process died, so it must still be a run error.
MIXED_CRASH_OUTPUT = """\
running 1 test
test tests::unit_ok ... FAILED

test result: FAILED. 0 passed; 1 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.00s

running 1 test
  process didn't exit successfully: `/tmp/swe-mutants-xxx/tree/target/debug/deps/smoke-abc123` (signal: 6, SIGABRT: process abort signal)

error: test failed, to rerun pass `--test smoke`
"""


def mutation(classification: str, expected_red=None, reason=None) -> dict:
    data = {
        "id": "case",
        "file": "crates/swe-native/src/lib.rs",
        "find": "old",
        "replace": "new",
        "classification": classification,
    }
    if expected_red is not None:
        data["expected_red"] = expected_red
    if reason is not None:
        data["reason"] = reason
    return data


def judge_case(
    classification: str,
    code: int,
    failed: list[str],
    run_error: str | None = None,
    expected_red=None,
    expect_outcome: str | None = None,
):
    """Run the pure judge and check the verdict is internally consistent."""

    def run() -> str:
        ok, outcome, problems = GATE.judge(
            mutation(classification, expected_red=expected_red),
            code,
            failed,
            run_error,
        )
        assert ok == (not problems), f"verdict {ok} with problems {problems}"
        assert isinstance(outcome, str) and outcome, f"empty outcome for {classification}"
        if expect_outcome is not None:
            assert outcome == expect_outcome, f"expected outcome {expect_outcome!r} got {outcome!r}"
        return "green" if ok else "red"

    return run


def output_case(output: str, code: int, expect_failed: list[str], expect: str, detail: str = ""):
    """Run the pure cargo-output classifier."""

    def run() -> str:
        failed, run_error = GATE.read_test_output(output, code)
        assert failed == expect_failed, f"expected failed {expect_failed} got {failed}"
        if expect == "build error":
            assert run_error == GATE.BUILD_ERROR, f"expected BUILD_ERROR got {run_error!r}"
        elif expect == "run error":
            assert run_error is not None, "expected a run_error, got a judgeable run"
            if detail:
                assert detail in run_error, f"expected {detail!r} in {run_error!r}"
        else:
            assert run_error is None, f"expected a judgeable run, got {run_error!r}"
        return expect

    return run


def manifest_case(payload: dict):
    """Run the shipped manifest loader against a throwaway baseline file."""

    def run() -> str:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "baseline.json"
            path.write_text(json.dumps(payload))
            GATE.load_manifest(path)
        return "accepted"

    return run


def valid_manifest() -> dict:
    return {
        "version": 1,
        "mutations": [
            {
                "id": "m",
                "file": "crates/swe-native/src/lib.rs",
                "find": "old",
                "replace": "new",
                "classification": "detected",
                "expected_red": ["tests::unit_ok"],
            }
        ],
    }


def cases():
    return [
        # ---- detected: red must be the named tests, not just any failure ----
        (
            "detected + clean green: fails (the catching test has rotted)",
            "red",
            judge_case(
                "detected",
                GREEN,
                [],
                expected_red=["tests::unit_ok"],
                expect_outcome="MISSED",
            ),
        ),
        (
            "detected + expected_red red: green (ok)",
            "green",
            judge_case(
                "detected",
                CLEAN_RED,
                ["tests::unit_ok"],
                expected_red=["tests::unit_ok"],
                expect_outcome="detected",
            ),
        ),
        (
            "detected + expected_red plus extra reds: green (superset is fine)",
            "green",
            judge_case(
                "detected",
                CLEAN_RED,
                ["tests::other", "tests::unit_ok"],
                expected_red=["tests::unit_ok"],
            ),
        ),
        (
            "detected + wrong/other test red: fails",
            "red",
            judge_case(
                "detected",
                CLEAN_RED,
                ["tests::other"],
                expected_red=["tests::unit_ok"],
                expect_outcome="MISSED",
            ),
        ),
        (
            "detected + build error: fails",
            "red",
            judge_case(
                "detected",
                101,
                [],
                run_error=GATE.BUILD_ERROR,
                expected_red=["tests::unit_ok"],
                expect_outcome="BUILD ERROR",
            ),
        ),
        (
            "detected without expected_red: fails",
            "red",
            judge_case(
                "detected",
                CLEAN_RED,
                ["tests::unit_ok"],
                expect_outcome="NO EXPECTATION",
            ),
        ),
        # ---- equivalent/excepted: green means exit 0, not "no parsed failure" ----
        (
            "equivalent + exit 0: green (ok)",
            "green",
            judge_case("equivalent", GREEN, []),
        ),
        (
            "equivalent + clean test failure: fails",
            "red",
            judge_case(
                "equivalent",
                CLEAN_RED,
                ["tests::unit_ok"],
                expect_outcome="DETECTED BUT MARKED EQUIVALENT",
            ),
        ),
        (
            "equivalent + crash/abort (code 101, no parsed failure): fails",
            "red",
            judge_case("equivalent", CLEAN_RED, []),
        ),
        (
            "excepted + crash/abort (code 101, no parsed failure): fails",
            "red",
            judge_case("excepted", CLEAN_RED, []),
        ),
        (
            "excepted + exit 0: green (ok)",
            "green",
            judge_case("excepted", GREEN, []),
        ),
        # ---- a non-zero exit with no named failure is a gate error, every class ----
        (
            "detected + non-zero with no named failure: fails",
            "red",
            judge_case("detected", CLEAN_RED, [], expected_red=["tests::unit_ok"]),
        ),
        (
            "equivalent + non-zero with no named failure: fails",
            "red",
            judge_case("equivalent", CLEAN_RED, []),
        ),
        (
            "excepted + non-zero with no named failure: fails",
            "red",
            judge_case("excepted", CLEAN_RED, []),
        ),
        (
            "detected + no test harness ran: fails",
            "red",
            judge_case(
                "detected",
                101,
                [],
                run_error="no test harness produced output",
                expected_red=["tests::unit_ok"],
                expect_outcome="RUN ERROR",
            ),
        ),
        (
            "equivalent + test process crashed, sibling binary green: fails",
            "red",
            judge_case(
                "equivalent",
                101,
                [],
                run_error=(
                    "the run reported 0 failing test(s) but named 0; "
                    "a test process probably crashed or failed to launch"
                ),
                expect_outcome="RUN ERROR",
            ),
        ),
        # ---- cargo output parser (the real failure shapes) ----
        (
            "output: clean test failure is judgeable and names the test",
            "explained",
            output_case(
                CLEAN_RED_OUTPUT,
                CLEAN_RED,
                ["tests::unit_ok"],
                "explained",
            ),
        ),
        (
            "output: green run is judgeable",
            "explained",
            output_case(GREEN_OUTPUT, GREEN, [], "explained"),
        ),
        (
            "output: abort with green sibling binary is a run error, not green",
            "run error",
            output_case(CRASH_OUTPUT, CLEAN_RED, [], "run error", "crashed or failed to launch"),
        ),
        (
            "output: crash marker is a run error even when failure counts match",
            "run error",
            output_case(
                MIXED_CRASH_OUTPUT,
                CLEAN_RED,
                ["tests::unit_ok"],
                "run error",
                "crashed or failed to launch",
            ),
        ),
        (
            "output: compilation error is a build error, not a detection",
            "build error",
            output_case(BUILD_OUTPUT, CLEAN_RED, [], "build error"),
        ),
        (
            "output: no test harness ran is a run error",
            "run error",
            output_case(NO_HARNESS_OUTPUT, CLEAN_RED, [], "run error", "no test harness"),
        ),
        # ---- manifest validation: the only way in ----
        (
            "manifest: valid detected entry is accepted",
            "accepted",
            manifest_case(valid_manifest()),
        ),
        (
            "manifest: detected without expected_red loads (judge rejects it)",
            "accepted",
            manifest_case(
                {
                    "version": 1,
                    "mutations": [
                        {
                            "id": "m",
                            "file": "x.rs",
                            "find": "a",
                            "replace": "b",
                            "classification": "detected",
                        }
                    ],
                }
            ),
        ),
        (
            "manifest: unknown classification is SystemExit",
            "SystemExit",
            manifest_case(
                {
                    "version": 1,
                    "mutations": [
                        {
                            "id": "m",
                            "file": "x.rs",
                            "find": "a",
                            "replace": "b",
                            "classification": "suspicious",
                        }
                    ],
                }
            ),
        ),
        (
            "manifest: equivalent without reason is SystemExit",
            "SystemExit",
            manifest_case(
                {
                    "version": 1,
                    "mutations": [
                        {
                            "id": "m",
                            "file": "x.rs",
                            "find": "a",
                            "replace": "b",
                            "classification": "equivalent",
                        }
                    ],
                }
            ),
        ),
        (
            "manifest: excepted without reason is SystemExit",
            "SystemExit",
            manifest_case(
                {
                    "version": 1,
                    "mutations": [
                        {
                            "id": "m",
                            "file": "x.rs",
                            "find": "a",
                            "replace": "b",
                            "classification": "excepted",
                        }
                    ],
                }
            ),
        ),
        (
            "manifest: missing a required key is SystemExit",
            "SystemExit",
            manifest_case(
                {
                    "version": 1,
                    "mutations": [
                        {
                            "id": "m",
                            "file": "x.rs",
                            "find": "a",
                            "classification": "detected",
                            "expected_red": ["t"],
                        }
                    ],
                }
            ),
        ),
        (
            "manifest: absolute mutation path is SystemExit",
            "SystemExit",
            manifest_case(
                {
                    **valid_manifest(),
                    "mutations": [
                        {**valid_manifest()["mutations"][0], "file": "/tmp/outside.rs"}
                    ],
                }
            ),
        ),
        (
            "manifest: parent traversal mutation path is SystemExit",
            "SystemExit",
            manifest_case(
                {
                    **valid_manifest(),
                    "mutations": [
                        {**valid_manifest()["mutations"][0], "file": "../outside.rs"}
                    ],
                }
            ),
        ),
        (
            "manifest: empty baseline is SystemExit",
            "SystemExit",
            manifest_case({"version": 1, "mutations": []}),
        ),
        (
            "manifest: unsupported version is SystemExit",
            "SystemExit",
            manifest_case({**valid_manifest(), "version": 2}),
        ),
    ]


def main() -> int:
    failures = 0
    all_cases = cases()
    for name, expected, runner in all_cases:
        try:
            got = runner()
        except SystemExit:
            got = "SystemExit"
        except AssertionError as exc:
            got = f"assertion error: {exc}"
        ok = got == expected
        failures += 0 if ok else 1
        print(f"{'PASS' if ok else 'FAIL'}  expected {expected!r} got {got!r}  {name}")
    print(f"\n{len(all_cases)} cases, {failures} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
