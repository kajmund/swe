"""Command line entry point for the microtask-swarm experiment."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from decimal import Decimal
from pathlib import Path
from typing import Sequence

from microtask_swarm.estimate import estimate_matrix, estimate_sweycoins
from microtask_swarm.async_benchmark import (
    render_async_markdown,
    run_async_benchmark,
    write_async_report,
)
from microtask_swarm.live import (
    LiveDispatchRefused,
    OllamaCloudClient,
    check_live_gate,
    credential_status,
    preflight,
)
from microtask_swarm.runner import (
    available_models,
    compact_report,
    load_config,
    load_suite,
    run_matrix,
    run_offline,
    write_report,
)

_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_CONFIG = _ROOT / "config" / "experiment.json"
_DEFAULT_REPORT = _ROOT / "reports" / "offline-pilot.json"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="SWE microtask-swarm pilot: offline pipeline and gated live matrix"
    )
    parser.add_argument(
        "--config", type=Path, default=_DEFAULT_CONFIG, help="experiment config path"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    offline = sub.add_parser("offline", help="run the offline heuristic matrix")
    offline.add_argument("--report", type=Path, default=_DEFAULT_REPORT)
    offline.add_argument("--include-rows", action="store_true")
    offline.add_argument("--full", action="store_true", help="write full per-run diagnostics")
    offline.add_argument("--modes", nargs="*")
    offline.add_argument("--models", nargs="*")
    offline.add_argument("--concurrency", nargs="*", type=int)

    estimate = sub.add_parser("estimate", help="print the pre-dispatch cost estimate")
    estimate.add_argument("--pretty", action="store_true")

    async_bench = sub.add_parser(
        "async", help="run the offline A/B/C/D scheduling benchmark"
    )
    async_bench.add_argument(
        "--async-config",
        type=Path,
        default=_ROOT / "config" / "async-benchmark.json",
    )
    async_bench.add_argument(
        "--report", type=Path, default=_ROOT / "reports" / "async-benchmark.json"
    )
    async_bench.add_argument(
        "--markdown", type=Path, default=_ROOT / "reports" / "async-benchmark.md"
    )
    async_bench.add_argument(
        "--scale", type=float, default=None, help="scale deterministic trace latencies"
    )

    verify = sub.add_parser("verify-fixtures", help="validate the frozen gold fixtures")
    verify.add_argument("--pretty", action="store_true")

    live = sub.add_parser(
        "live",
        help="run the paid live matrix; refused without an approved cap",
    )
    live.add_argument("--approve-usd-cap", required=False)
    live.add_argument("--use-local-daemon", action="store_true")
    live.add_argument("--report", type=Path, default=_ROOT / "reports" / "live.json")
    live.add_argument("--include-rows", action="store_true")
    live.add_argument("--full", action="store_true", help="write full per-run diagnostics")
    live.add_argument("--modes", nargs="*")
    live.add_argument("--models", nargs="*")
    live.add_argument("--concurrency", nargs="*", type=int)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "offline":
            report = asyncio.run(
                run_offline(
                    args.config,
                    include_rows=args.include_rows,
                    modes=args.modes,
                    model_ids=args.models,
                    concurrency_levels=args.concurrency,
                )
            )
            write_report(
                report if args.full else compact_report(report), args.report
            )
            _print_summary(report, args.report)
            return 0
        if args.command == "estimate":
            config = load_config(args.config)
            payload = {
                "usd": estimate_matrix(config),
                "sweycoins": estimate_sweycoins(config),
                "credentials": credential_status(),
            }
            _print(payload, pretty=args.pretty)
            return 0
        if args.command == "async":
            report = asyncio.run(
                run_async_benchmark(
                    args.config, args.async_config, scale=args.scale
                )
            )
            write_async_report(report, args.report)
            markdown = render_async_markdown(report)
            args.markdown.parent.mkdir(parents=True, exist_ok=True)
            args.markdown.write_text(markdown, encoding="utf-8")
            _print_async_summary(report, args.report, args.markdown)
            return 0
        if args.command == "verify-fixtures":
            config = load_config(args.config)
            suite = load_suite(config, _ROOT)
            payload = {
                "ok": True,
                "sha256": suite.sha256,
                "contract_version": suite.contract_version,
                "items": suite.item_count,
                "microtasks": suite.microtask_count,
                "domains": list(suite.domains),
            }
            _print(payload, pretty=args.pretty)
            return 0
        if args.command == "live":
            return _live(args)
    except LiveDispatchRefused as exc:
        print(f"live dispatch refused: {exc}", file=sys.stderr)
        return 2
    except (ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    parser.error("unknown command")
    return 1


def _live(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    gate = check_live_gate(
        config,
        approved_cap_usd=args.approve_usd_cap,
        use_local_daemon=args.use_local_daemon,
    )
    model_ids = list(args.models) if args.models else available_models(config)
    metadata = preflight(config, gate, model_ids)
    estimate = estimate_matrix(config)
    planned = Decimal(estimate["all_in_estimated_usd"])
    worst_case = Decimal(estimate["baseline_all_calls_estimated_usd"])
    if planned > gate.approved_cap_usd:
        raise LiveDispatchRefused(
            f"planned matrix estimate {planned} USD exceeds approved cap "
            f"{gate.approved_cap_usd} USD; lower the matrix or approve a larger cap"
        )
    if worst_case > gate.approved_cap_usd:
        print(
            f"warning: all-calls-at-baseline extreme is {worst_case} USD, above the "
            f"approved cap {gate.approved_cap_usd} USD; the planned mixed matrix is "
            f"{planned} USD",
            file=sys.stderr,
        )
    suite = load_suite(config, _ROOT)
    models_by_id = {str(model["id"]): model for model in config["models"]}
    client = OllamaCloudClient(gate=gate, models=models_by_id)
    concurrency = (
        list(args.concurrency)
        if args.concurrency
        else list(config["concurrency_levels"])
    )
    report = asyncio.run(
        run_matrix(
            config,
            suite,
            provider=client,
            model_ids=model_ids,
            modes=args.modes,
            concurrency_levels=concurrency,
            evidence_kind="live_ollama_cloud",
            include_rows=args.include_rows,
            effective_concurrency_limit=int(
                config["provider"]["assumed_concurrency_limit"]
            ),
        )
    )
    report["live_gate"] = gate.to_dict()
    report["preflight"] = metadata
    report["approval_estimate"] = estimate
    write_report(report if args.full else compact_report(report), args.report)
    _print_summary(report, args.report)
    return 0


def _print_summary(report: dict, path: Path) -> None:
    print(f"report: {path}")
    print(f"evidence: {report['evidence_kind']}  runs: {report['summary']['run_count']}")
    for mode, row in sorted(report["summary"]["best_by_mode"].items()):
        print(
            f"  {mode}: accuracy={row['accuracy']} "
            f"coverage={row['extraction_coverage']} "
            f"false_support={row['false_support_rate']} "
            f"p95={row['p95_call_latency_ms']}ms "
            f"calculated_usd={row['calculated_usd']} "
            f"calculated_complete={row['calculated_usd_complete_calls']} "
            f"actual_usd={row['actual_usd']} "
            f"sweycoins_milli={row['sweycoins_spent_milli']}"
        )


def _print_async_summary(report: dict, json_path: Path, md_path: Path) -> None:
    print(f"report: {json_path}")
    print(f"markdown: {md_path}")
    print(f"evidence: {report['evidence_kind']}  runs: {report['summary']['run_count']}")
    for strategy, row in sorted(report["summary"]["best_by_strategy"].items()):
        print(
            f"  {strategy}: wall={row['best_wall_ms']}ms "
            f"first_verified={row['first_verified_ms']}ms "
            f"p95={row['p95_ms']}ms "
            f"worker_util={row['worker_utilization']} "
            f"provider_util={row['provider_utilization']} "
            f"accuracy={row['accuracy']} "
            f"coverage={row['coverage']} "
            f"false_support={row['false_support_rate']} "
            f"settled_milli={row['settled_milli']} "
            f"pending_before={row['pending_before_milli']}"
        )


def _print(payload: dict, *, pretty: bool) -> None:
    if pretty:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(json.dumps(payload, sort_keys=True))


if __name__ == "__main__":
    raise SystemExit(main())
