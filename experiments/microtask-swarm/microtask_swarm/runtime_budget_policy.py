"""Real RuntimeBudget integration behind the adaptive policy interface.

This module is the integration path. It uses the merged ``swe.budget``
``RuntimeBudget`` and the real Rust ``swe_budget.Ledger``; the local
``SweycoinMeter`` is not used here. The frozen microtask-swarm baseline remains
unchanged and is imported for fixtures, policies, training statistics, and
scoring.

t1bm PR 13 (``6dfc451fc26e3720ba55f63def7cee0917e5905f``, stacked on open
PR 12 ``27eacd1ec1581174bdb242da6d63f2ca5f121ed6``) injects a one-milli
``RuntimeBudget`` policy through ``WorkflowRunner``. It does not import this
tariff. The production tariff stays here, on main. That branch's
``experiments/agent-contract`` tree is not edited here, and this path makes
no provider call.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping

from microtask_swarm.adaptive_budget import (
    AdaptivePolicy,
    BudgetPolicySimulator,
    EqualSplitPolicy,
    FixedRoutingPolicy,
    GreedyPolicy,
    ModelPlan,
    PolicyState,
    _percentile,
    _summary,
    _totals,
)
from microtask_swarm.async_benchmark import Attempt
from microtask_swarm.cost import account_calls
from microtask_swarm.dag import ensure_repo_python_on_path
from microtask_swarm.fixtures import load_fixtures
from microtask_swarm.production_stop import StopSignals, compute_signals
from microtask_swarm.results import CallRecord, TaskResult
from microtask_swarm.scoring import score_suite

# Open t1bm stack. PR 13 is the one-milli stand-in; PR 12 is its base. Neither
# imports this tariff, and this branch stays on main.
T1BM_PR12_COMMIT = "27eacd1ec1581174bdb242da6d63f2ca5f121ed6"
T1BM_PR13_COMMIT = "6dfc451fc26e3720ba55f63def7cee0917e5905f"

ensure_repo_python_on_path()

try:  # pragma: no cover - the native extension is built separately
    from swe.budget import (
        BudgetError,
        BudgetOutcome,
        BudgetRequest,
        NativeBudgetLedger,
        RuntimeBudget,
    )

    RUNTIME_BUDGET_AVAILABLE = True
except Exception:  # pragma: no cover - exercised by skip tests
    RUNTIME_BUDGET_AVAILABLE = False
    BudgetError = Exception  # type: ignore[assignment]
    BudgetOutcome = Any  # type: ignore[assignment]
    BudgetRequest = Any  # type: ignore[assignment]
    NativeBudgetLedger = Any  # type: ignore[assignment]
    RuntimeBudget = Any  # type: ignore[assignment]


class RuntimeBudgetUnavailable(RuntimeError):
    """The merged RuntimeBudget or swe_budget extension is not installed."""


class CarriedMilliPolicy:
    """Runtime ``BudgetPolicy`` for a tariff chosen before the call.

    ``reserve`` and ``settle`` return milli integers, the same surface as
    t1bm PR 13's ``InjectedPolicy``. That stand-in returns one milli. This
    policy returns the amount the routing policy already placed on the
    request. It does not price tokens and it does not call a provider.
    """

    def reserve(self, request: Any) -> int:
        return int(request.input["amount_milli"])

    def settle(self, request: Any, outcome: Any) -> int:
        return int((outcome.value or {}).get("amount_milli", 0))


class RuntimeBudgetBackend:
    """Thin adapter over the real RuntimeBudget. It owns no ledger code."""

    def __init__(
        self,
        *,
        cap_milli: int,
        run_id: str,
        account: str = "adaptive",
    ) -> None:
        if not RUNTIME_BUDGET_AVAILABLE:
            raise RuntimeBudgetUnavailable(
                "swe.budget and swe_budget are unavailable; build the native "
                "swe-budget wheel after PR #11"
            )
        self.run_id = run_id
        self.account = account
        self.ledger = NativeBudgetLedger()
        self.ledger.allocate(
            run_id=run_id,
            account=account,
            amount_milli=cap_milli,
            key=f"{run_id}:alloc",
        )
        self.budget = RuntimeBudget(self.ledger, CarriedMilliPolicy(), account=account)
        self.min_available_milli = cap_milli
        self._track_available()

    def reserve(
        self,
        *,
        invocation_id: str,
        node_id: str,
        name: str,
        amount_milli: int,
        kind: str = "provider",
    ) -> Any | None:
        request = BudgetRequest(
            run_id=self.run_id,
            invocation_id=invocation_id,
            node_id=node_id,
            kind=kind,
            name=name,
            input={"amount_milli": amount_milli},
        )
        try:
            reservation = self.budget.reserve(request)
        except BudgetError:
            return None
        self._track_available()
        return reservation

    def settle(self, reservation: Any, amount_milli: int) -> None:
        self.budget.settle(
            reservation,
            BudgetOutcome(ok=True, value={"amount_milli": amount_milli}),
        )
        self._track_available()

    def mark_pending(self, reservation: Any) -> None:
        self.budget.mark_pending(reservation)
        self._track_available()

    def reconcile(
        self,
        reservation: Any,
        *,
        spent_milli: int | None = None,
        release: bool = False,
    ) -> None:
        self.budget.reconcile(
            reservation, spent_milli=spent_milli, release=release
        )
        self._track_available()

    def release(self, reservation: Any) -> None:
        self.budget.release(reservation)
        self._track_available()

    def balance(self) -> Mapping[str, int]:
        return dict(self.budget.balance())

    def events(self) -> list[Mapping[str, Any]]:
        return list(self.budget.events())

    def replay_balance(self) -> Mapping[str, int]:
        replayed = NativeBudgetLedger.replay(self.events())
        return dict(replayed.balance(account=self.account))

    def _track_available(self) -> None:
        self.min_available_milli = min(
            self.min_available_milli,
            int(self.balance()["available"]),
        )


class RuntimeBudgetPolicySimulator(BudgetPolicySimulator):
    """Same policies and held-out outcomes, real RuntimeBudget behind them."""

    def run(self, policy) -> dict[str, Any]:
        if not RUNTIME_BUDGET_AVAILABLE:
            raise RuntimeBudgetUnavailable(
                "RuntimeBudget is unavailable; build swe_budget after PR #11"
            )
        policy.begin(
            self.heldout_tasks,
            cap_milli=self.cap_milli,
            training=self.training_stats,
            models=self.model_ids,
        )
        backend = RuntimeBudgetBackend(
            cap_milli=self.cap_milli,
            run_id=f"runtime:{policy.name}",
        )
        state = PolicyState(
            cap_milli=self.cap_milli,
            remaining_milli=self.cap_milli,
            settled_milli=0,
            pending_milli=0,
            completed_tasks=0,
            total_tasks=len(self.heldout_tasks),
        )
        winner_attempts: list[Attempt] = []
        task_results: dict[str, TaskResult] = {}
        all_calls: list[dict[str, Any]] = []
        latencies: list[float] = []
        speculative_tasks = 0
        budget_denied = 0
        pending_before = 0
        late_billed = 0
        released = 0
        stopped = False
        stop_signals: StopSignals | None = None
        reserve_before_dispatch = True
        for spec in self.heldout_tasks:
            if stop_signals is not None and self.stop_policy.evaluate(stop_signals):
                stopped = True
                break
            decision = policy.choose(spec, state)
            if not decision.plans:
                budget_denied += 1
                continue
            reservations: list[tuple[ModelPlan, Any | None]] = []
            failed = False
            for plan in decision.plans:
                reservation = backend.reserve(
                    invocation_id=(
                        f"{spec.task_id}:{plan.model_id}:{plan.role}"
                    ),
                    node_id=spec.task_id,
                    name=plan.model_id,
                    amount_milli=plan.tariff_milli,
                )
                reservations.append((plan, reservation))
                if reservation is None:
                    failed = True
                    break
            if failed:
                for _plan, reservation in reservations:
                    if reservation is not None:
                        backend.release(reservation)
                budget_denied += 1
                continue
            outcomes = [
                (
                    plan,
                    reservation,
                    self.heldout_outcomes[spec.task_id][plan.model_id],
                )
                for plan, reservation in reservations
            ]
            ranked = sorted(
                outcomes,
                key=lambda item: (
                    -self.training_stats[
                        (item[2].contract, item[2].model_id)
                    ].utility,
                    -item[2].confidence,
                    item[0].tariff_milli,
                ),
            )
            winner_plan, winner_reservation, winner = ranked[0]
            if decision.speculative:
                secondary = next(
                    (
                        item
                        for item in outcomes
                        if item[0].role == "speculative"
                        and item[2].verification is not None
                        and item[2].verification.ok
                    ),
                    None,
                )
                if secondary is not None:
                    winner_plan, winner_reservation, winner = secondary
            if decision.speculative:
                speculative_tasks += 1
            for plan, reservation, outcome in outcomes:
                if reservation is None:
                    reserve_before_dispatch = False
                    continue
                if plan is winner_plan or outcome.latency_ms <= winner.latency_ms:
                    backend.settle(reservation, plan.tariff_milli)
                else:
                    pending_before += plan.tariff_milli
                    backend.mark_pending(reservation)
                    digest = hashlib.sha256(
                        f"late:{policy.name}:{spec.task_id}:{plan.model_id}".encode(
                            "utf-8"
                        )
                    ).hexdigest()
                    fraction = int(digest[:8], 16) / 0xFFFFFFFF
                    if fraction < self.late_fraction:
                        backend.reconcile(
                            reservation, spent_milli=plan.tariff_milli
                        )
                        late_billed += plan.tariff_milli
                    else:
                        backend.reconcile(reservation, release=True)
                        released += plan.tariff_milli
            calls: list[CallRecord] = []
            for plan, _reservation, outcome in outcomes:
                cancelled = (
                    plan is not winner_plan
                    and outcome.latency_ms > winner.latency_ms
                )
                tokens = (
                    {
                        "input": None,
                        "output": None,
                        "reasoning": None,
                        "cache_read": None,
                        "cache_write": None,
                        "source": "provider",
                        "cache_read_assumed_miss": False,
                    }
                    if cancelled
                    else dict(outcome.tokens)
                )
                calls.append(
                    CallRecord(
                        model_id=plan.model_id,
                        role="candidate",
                        attempt=1,
                        latency_ms=outcome.latency_ms,
                        tokens=tokens,
                        billed_usd=outcome.billed_usd,
                        price_period=outcome.price_period,
                        error=outcome.error,
                        cancelled=cancelled,
                        output=outcome.output,
                    )
                )
                all_calls.append(
                    {
                        "model_id": plan.model_id,
                        "role": "candidate",
                        "attempt": 1,
                        "latency_ms": outcome.latency_ms,
                        "tokens": tokens,
                        "billed_usd": outcome.billed_usd,
                        "price_period": outcome.price_period,
                    }
                )
            task_results[spec.task_id] = TaskResult(
                task_id=spec.task_id,
                kind=str(spec.kind),
                output=winner.output,
                verification=winner.verification,
                error=winner.error,
                selected_models=(winner.model_id,),
                calls=tuple(calls),
            )
            winner_attempts.append(
                Attempt(
                    task_id=spec.task_id,
                    item_id=spec.item_id,
                    kind=spec.kind,
                    model_id=winner.model_id,
                    reservation_id=getattr(winner_reservation, "reservation_id", None),
                    tariff_milli=winner_plan.tariff_milli,
                    ready_ns=0,
                    start_ns=1,
                    end_ns=2,
                    output=winner.output,
                    verification=winner.verification,
                    error=winner.error,
                )
            )
            latencies.append(winner.latency_ms)
            policy.observe(spec, decision, winner.view(), state)
            state.completed_tasks += 1
            balance = backend.balance()
            state.settled_milli = int(balance["spent"])
            state.pending_milli = int(balance["reserved"])
            state.remaining_milli = int(balance["available"])
            stop_signals = compute_signals(
                self.heldout_tasks,
                winner_attempts,
                specs_by_item=self.specs_by_item,
            )
            state.signals = stop_signals
        quality = score_suite(self.heldout_tasks, task_results)
        verified_evidence = int(quality["extraction"]["matched_quotes"])
        cost = account_calls(
            all_calls, models=self.models, verified_evidence=verified_evidence
        )
        balance = backend.balance()
        events = backend.events()
        replay_balance = backend.replay_balance()
        reserve_indices = {
            event.get("reservation_id"): index
            for index, event in enumerate(events)
            if event.get("kind") == "reserve"
        }
        terminal_safe = all(
            event.get("reservation_id") in reserve_indices
            and reserve_indices[event.get("reservation_id")] < index
            for index, event in enumerate(events)
            if event.get("kind") in {"spend", "release"}
        )
        return {
            "policy": policy.name,
            "budget_cap_milli": self.cap_milli,
            "train_item_ids": [item.item_id for item in self.training_items],
            "heldout_item_ids": [item.item_id for item in self.heldout_items],
            "task_count": len(self.heldout_tasks),
            "completed_tasks": state.completed_tasks,
            "stopped": stopped,
            "stop_reason": "gold_free_stop" if stopped else None,
            "signals": None if stop_signals is None else stop_signals.to_dict(),
            "quality": {key: value for key, value in quality.items() if key != "rows"},
            "cost": cost,
            "budget": {
                "cap_milli": self.cap_milli,
                "settled_milli": int(balance["spent"]),
                "pending_before_reconcile_milli": pending_before,
                "late_billed_milli": late_billed,
                "released_milli": released,
                "pending_after_reconcile_milli": int(balance["reserved"]),
                "available_milli": int(balance["available"]),
                "denied_tasks": budget_denied,
                "overspent_milli": max(
                    0, -backend.min_available_milli
                ),
            },
            "calls": len(all_calls),
            "speculative_tasks": speculative_tasks,
            "latency_ms": round(sum(latencies), 3),
            "latency_p50_ms": round(_percentile(latencies, 0.50), 3),
            "latency_p95_ms": round(_percentile(latencies, 0.95), 3),
            "abstention_rate": (
                0.0 if stop_signals is None else stop_signals.abstention_rate
            ),
            "need_coverage": (
                0.0 if stop_signals is None else stop_signals.need_coverage
            ),
            "evidence_coverage": (
                0.0 if stop_signals is None else stop_signals.evidence_coverage
            ),
            "average_confidence": (
                0.0 if stop_signals is None else stop_signals.average_confidence
            ),
            "runtime_budget": {
                "available": True,
                "backend": "swe.budget.RuntimeBudget",
                "ledger": "swe_budget.Ledger",
                "account": backend.account,
                "balance": dict(balance),
                "events": len(events),
                "replay_balance": dict(replay_balance),
                "replay_ok": dict(replay_balance) == dict(balance),
                "reservation_before_dispatch": reserve_before_dispatch,
                "reservation_before_terminal": terminal_safe,
            },
        }


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


async def run_runtime_budget_benchmark(
    experiment_config_path: Path,
    async_config_path: Path,
    budget_config_path: Path,
) -> dict[str, Any]:
    if not RUNTIME_BUDGET_AVAILABLE:
        raise RuntimeBudgetUnavailable(
            "RuntimeBudget unavailable; build swe_budget after PR #11"
        )
    experiment = _load_json(experiment_config_path)
    async_config = _load_json(async_config_path)
    budget_config = _load_json(budget_config_path)
    root = experiment_config_path.resolve().parent.parent
    suite = load_fixtures(
        root / experiment["fixtures"]["gold"],
        expected_sha256=experiment["fixtures"]["sha256"],
    )
    simulator = RuntimeBudgetPolicySimulator(
        suite=suite,
        async_config=async_config,
        experiment_config=experiment,
        budget_config=budget_config,
    )
    policies = [
        EqualSplitPolicy(),
        GreedyPolicy(),
        FixedRoutingPolicy(budget_config.get("fixed_routes")),
        AdaptivePolicy(
            name="adaptive",
            false_support_penalty=float(
                budget_config.get("false_support_penalty", 0.5)
            ),
            speculative=bool(budget_config.get("speculative", True)),
            max_speculative_extractions=int(
                budget_config.get("max_speculative_extractions", 5)
            ),
            min_secondary_utility_gain=float(
                budget_config.get("min_secondary_utility_gain", 0.02)
            ),
        ),
        AdaptivePolicy(
            name="adaptive_no_spec",
            false_support_penalty=float(
                budget_config.get("false_support_penalty", 0.5)
            ),
            speculative=False,
            max_speculative_extractions=0,
            min_secondary_utility_gain=float(
                budget_config.get("min_secondary_utility_gain", 0.02)
            ),
        ),
    ]
    runs = [simulator.run(policy) for policy in policies]
    return {
        "schema_version": 1,
        "experiment_id": "swe-microtask-swarm-runtime-budget",
        "evidence_kind": "offline_runtime_budget_policy",
        "generated_at": datetime.now(UTC).isoformat(),
        "fixture_sha256": suite.sha256,
        "budget_cap_milli": simulator.cap_milli,
        "train_item_ids": [item.item_id for item in simulator.training_items],
        "heldout_item_ids": [item.item_id for item in simulator.heldout_items],
        "training_task_count": len(simulator.training_tasks),
        "heldout_task_count": len(simulator.heldout_tasks),
        "models": simulator.model_ids,
        "tariff_multiplier": simulator.tariff_multiplier,
        "training_stats": [
            stats.to_dict() for stats in simulator.training_stats.values()
        ],
        "runtime_budget": {
            "available": True,
            "backend": "swe.budget.RuntimeBudget",
            "ledger": "swe_budget.Ledger",
            "local_meter_used_in_integration_path": False,
        },
        "runs": runs,
        "summary": _summary(runs),
        "totals": _totals(runs),
        "notes": [
            "Real RuntimeBudget and Rust swe_budget ledger behind the policy interface.",
            "Reservation occurs before the held-out outcome is dispatched.",
            "Pending reservations are reconciled into late-billed or released milli.",
            "Gold is used for training statistics and post-hoc scoring only.",
            "No paid call, no new scheduler, and no local meter in this integration path.",
            (
                "Coordinates with open t1bm PR 13 at "
                f"{T1BM_PR13_COMMIT}, stacked on open PR 12 at "
                f"{T1BM_PR12_COMMIT}. PR 13 injects a one-milli policy through "
                "WorkflowRunner and does not import this tariff. This module "
                "owns the production tariff on main and does not edit "
                "experiments/agent-contract."
            ),
        ],
    }


def write_runtime_budget_report(report: Mapping[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def render_runtime_budget_markdown(report: Mapping[str, Any]) -> str:
    lines = [
        "# RuntimeBudget-backed policy comparison (offline)",
        "",
        f"Generated: {report['generated_at']}",
        f"Fixture SHA-256: `{report['fixture_sha256']}`",
        f"Budget cap: {report['budget_cap_milli']} milli-Sweycoin per policy",
        f"Training items: {', '.join(report['train_item_ids'])}",
        f"Held-out items: {', '.join(report['heldout_item_ids'])}",
        "Backend: `swe.budget.RuntimeBudget` + `swe_budget.Ledger`; no local meter.",
        "Gold is used for training statistics and post-hoc scoring only.",
        "",
        "| Policy | Stop | Tasks | Accuracy | Coverage | False support | Need cov. | Latency | Calls | Spec. | Settled | Pending | Late | Released | Denied | Overspend | Replay | Reserve first | Calc USD |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- | ---: |",
    ]
    for run in report["runs"]:
        b = run["budget"]
        rb = run["runtime_budget"]
        lines.append(
            "| {policy} | {stop} | {tasks} | {acc} | {cov} | {fs} | {need} | {lat} | {calls} | {spec} | {settled} | {pending} | {late} | {released} | {denied} | {over} | {replay} | {first} | {usd} |".format(
                policy=run["policy"],
                stop=run["stopped"],
                tasks=run["completed_tasks"],
                acc=run["quality"]["accuracy"],
                cov=run["quality"]["extraction"]["coverage"],
                fs=run["quality"]["false_support"]["false_support_rate"],
                need=run["need_coverage"],
                lat=run["latency_ms"],
                calls=run["calls"],
                spec=run["speculative_tasks"],
                settled=b["settled_milli"],
                pending=b["pending_before_reconcile_milli"],
                late=b["late_billed_milli"],
                released=b["released_milli"],
                denied=b["denied_tasks"],
                over=b["overspent_milli"],
                replay=rb["replay_ok"],
                first=rb["reservation_before_dispatch"],
                usd=run["cost"]["all_in"]["calculated_usd"],
            )
        )
    totals = report["totals"]
    lines.extend(
        [
            "",
            "Totals: calls={calls}, settled={settled} milli, pending={pending} milli, "
            "late={late} milli, released={released} milli, denied={denied}, "
            "overspent={over} milli, calculated USD={usd} (complete subset {complete}).".format(
                calls=totals["calls"],
                settled=totals["settled_milli"],
                pending=totals["pending_before_reconcile_milli"],
                late=totals["late_billed_milli"],
                released=totals["released_milli"],
                denied=totals["denied_tasks"],
                over=totals["overspent_milli"],
                usd=totals["calculated_usd"],
                complete=totals["calculated_usd_complete_calls"],
            ),
            "",
            "Every run replays its ledger event log and compares the replayed balance.",
            "Reservation-before-dispatch and reserve-before-terminal checks are in the JSON.",
            "",
        ]
    )
    return "\n".join(lines)


__all__ = [
    "RUNTIME_BUDGET_AVAILABLE",
    "T1BM_PR12_COMMIT",
    "T1BM_PR13_COMMIT",
    "CarriedMilliPolicy",
    "RuntimeBudgetBackend",
    "RuntimeBudgetPolicySimulator",
    "RuntimeBudgetUnavailable",
    "render_runtime_budget_markdown",
    "run_runtime_budget_benchmark",
    "write_runtime_budget_report",
]
