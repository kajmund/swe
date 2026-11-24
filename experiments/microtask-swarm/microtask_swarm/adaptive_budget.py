"""Offline adaptive budget/routing policy experiment.

This is a policy simulator, not a production scheduler and not a ledger. It
reuses the frozen fixtures, offline heuristic, deterministic latency trace,
verifier confidence, and the local pilot Sweycoin meter.

Training items are used to estimate per-contract/model quality, false support,
abstention, confidence, latency, and tariff. Held-out items are evaluated with
a gold-free policy: the policy sees only task input, training statistics, and
the verifier/abstention/confidence view of each outcome. Held-out gold is used
only after the run for scoring.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence

from microtask_swarm.async_benchmark import Attempt, LatencyTrace, choose_straggler_items
from microtask_swarm.budget import BudgetError, SweycoinMeter
from microtask_swarm.contracts import (
    ContractViolation,
    Kind,
    output_schema,
    parse_json_output,
    render_prompt,
    verify_output,
)
from microtask_swarm.cost import account_calls
from microtask_swarm.executor import ChatRequest
from microtask_swarm.fixtures import GoldSuite, TaskSpec, all_microtask_specs, load_fixtures
from microtask_swarm.offline import OfflineHeuristicProvider
from microtask_swarm.production_stop import (
    ProductionStopPolicy,
    StopSignals,
    compute_signals,
    verifier_confidence,
)
from microtask_swarm.results import CallRecord, TaskResult
from microtask_swarm.scoring import score_suite


@dataclass(frozen=True, slots=True)
class ModelPlan:
    model_id: str
    tariff_milli: int
    role: str


@dataclass(frozen=True, slots=True)
class RoutingDecision:
    plans: tuple[ModelPlan, ...]
    speculative: bool
    reason: str


@dataclass(slots=True)
class PolicyState:
    cap_milli: int
    remaining_milli: int
    settled_milli: int
    pending_milli: int
    completed_tasks: int
    total_tasks: int
    signals: StopSignals | None = None


@dataclass(frozen=True, slots=True)
class OutcomeView:
    """Gold-free view passed to the policy after one task."""

    task_id: str
    item_id: str
    contract: str
    model_id: str
    verified: bool
    abstained: bool
    confidence: float
    latency_ms: float
    tariff_milli: int


@dataclass(frozen=True, slots=True)
class ModelStats:
    model_id: str
    contract: str
    count: int
    accuracy: float
    false_support_rate: float
    abstention_rate: float
    invalid_rate: float
    average_confidence: float
    average_latency_ms: float
    tariff_milli: int

    @property
    def utility(self) -> float:
        return self.accuracy - 0.5 * self.false_support_rate

    def to_dict(self) -> dict[str, Any]:
        return {
            "model_id": self.model_id,
            "contract": self.contract,
            "count": self.count,
            "accuracy": self.accuracy,
            "false_support_rate": self.false_support_rate,
            "abstention_rate": self.abstention_rate,
            "invalid_rate": self.invalid_rate,
            "average_confidence": self.average_confidence,
            "average_latency_ms": self.average_latency_ms,
            "tariff_milli": self.tariff_milli,
            "utility": self.utility,
        }


@dataclass(frozen=True, slots=True)
class ModelOutcome:
    task_id: str
    item_id: str
    contract: str
    model_id: str
    output: Any
    verification: Any
    error: str | None
    confidence: float
    latency_ms: float
    tokens: Mapping[str, Any]
    billed_usd: str | None
    price_period: str | None
    tariff_milli: int
    correct: bool
    false_support: bool
    abstained: bool

    def view(self) -> OutcomeView:
        return OutcomeView(
            task_id=self.task_id,
            item_id=self.item_id,
            contract=self.contract,
            model_id=self.model_id,
            verified=bool(self.verification and self.verification.ok),
            abstained=self.abstained,
            confidence=self.confidence,
            latency_ms=self.latency_ms,
            tariff_milli=self.tariff_milli,
        )


class BudgetRoutingPolicy(Protocol):
    name: str

    def begin(
        self,
        tasks: Sequence[TaskSpec],
        *,
        cap_milli: int,
        training: Mapping[tuple[str, str], ModelStats],
        models: Sequence[str],
    ) -> None: ...

    def choose(self, task: TaskSpec, state: PolicyState) -> RoutingDecision: ...

    def observe(
        self,
        task: TaskSpec,
        decision: RoutingDecision,
        outcome: OutcomeView,
        state: PolicyState,
    ) -> None: ...


class EqualSplitPolicy:
    name = "equal_split"

    def __init__(self) -> None:
        self.per_task = 0
        self._models: Sequence[str] = ()
        self._training: Mapping[tuple[str, str], ModelStats] = {}

    def begin(self, tasks, *, cap_milli, training, models) -> None:
        self.per_task = cap_milli // max(1, len(tasks))
        self.set_training(training, models)

    def choose(self, task, state) -> RoutingDecision:
        budget = min(self.per_task, state.remaining_milli)
        candidates = [
            (self._stats(task, model), model)
            for model in self._models
            if self._stats(task, model) and self._stats(task, model).tariff_milli <= budget
        ]
        if not candidates:
            return RoutingDecision((), False, "no_model_within_equal_share")
        stats, model = max(
            candidates,
            key=lambda item: (item[0].accuracy, -item[0].tariff_milli),
        )
        return RoutingDecision(
            (ModelPlan(model, stats.tariff_milli, "primary"),),
            False,
            "equal_share_best_accuracy",
        )

    def observe(self, task, decision, outcome, state) -> None:
        return None

    def _stats(self, task: TaskSpec, model: str) -> ModelStats | None:
        return self._training.get((str(task.kind), model))

    def set_training(self, training, models) -> None:
        self._training = training
        self._models = models


class GreedyPolicy(EqualSplitPolicy):
    name = "greedy"

    def begin(self, tasks, *, cap_milli, training, models) -> None:
        self.per_task = 10**9
        self.set_training(training, models)

    def choose(self, task, state) -> RoutingDecision:
        candidates = [
            (self._stats(task, model), model)
            for model in self._models
            if self._stats(task, model)
            and self._stats(task, model).tariff_milli <= state.remaining_milli
        ]
        if not candidates:
            return RoutingDecision((), False, "no_affordable_model")
        stats, model = max(
            candidates,
            key=lambda item: (item[0].accuracy, -item[0].tariff_milli),
        )
        return RoutingDecision(
            (ModelPlan(model, stats.tariff_milli, "primary"),),
            False,
            "greedy_highest_accuracy",
        )


class FixedRoutingPolicy(EqualSplitPolicy):
    name = "fixed_routing"

    def __init__(self, fixed_routes: Mapping[str, str] | None = None) -> None:
        super().__init__()
        self.fixed: dict[str, str] = dict(fixed_routes or {})

    def begin(self, tasks, *, cap_milli, training, models) -> None:
        self.set_training(training, models)
        if self.fixed:
            return
        by_contract: dict[str, list[ModelStats]] = defaultdict(list)
        for (contract, _model), stats in training.items():
            by_contract[contract].append(stats)
        for contract, rows in by_contract.items():
            best = max(rows, key=lambda row: (row.accuracy, -row.tariff_milli))
            self.fixed[contract] = best.model_id

    def choose(self, task, state) -> RoutingDecision:
        model = self.fixed.get(str(task.kind))
        if model is None:
            return RoutingDecision((), False, "no_fixed_route")
        stats = self._stats(task, model)
        if stats is None or stats.tariff_milli > state.remaining_milli:
            return RoutingDecision((), False, "fixed_route_unaffordable")
        return RoutingDecision(
            (ModelPlan(model, stats.tariff_milli, "primary"),),
            False,
            "fixed_contract_route",
        )


class AdaptivePolicy(EqualSplitPolicy):
    name = "adaptive"

    def __init__(
        self,
        *,
        name: str = "adaptive",
        false_support_penalty: float = 0.5,
        speculative: bool = True,
        max_speculative_extractions: int = 5,
        min_secondary_utility_gain: float = 0.02,
    ) -> None:
        super().__init__()
        self.name = name
        self.false_support_penalty = false_support_penalty
        self.speculative = speculative
        self.max_speculative_extractions = max_speculative_extractions
        self.min_secondary_utility_gain = min_secondary_utility_gain
        self.remaining_extractions = 0
        self.speculative_used = 0

    def begin(self, tasks, *, cap_milli, training, models) -> None:
        self.set_training(training, models)
        self.remaining_extractions = sum(
            1 for task in tasks if task.kind is Kind.EXTRACTION
        )
        self.speculative_used = 0

    def _utility(self, stats: ModelStats) -> float:
        return stats.accuracy - self.false_support_penalty * stats.false_support_rate

    def _cheapest_extraction_tariff(self) -> int:
        tariffs = [
            stats.tariff_milli
            for (contract, _model), stats in self._training.items()
            if contract == str(Kind.EXTRACTION)
        ]
        return min(tariffs) if tariffs else 0

    def choose(self, task, state) -> RoutingDecision:
        is_extraction = task.kind is Kind.EXTRACTION
        if is_extraction and self.remaining_extractions > 0:
            self.remaining_extractions -= 1
        reserve = 0
        if not is_extraction and self.remaining_extractions > 0:
            reserve = self.remaining_extractions * self._cheapest_extraction_tariff()
        budget = max(0, state.remaining_milli - reserve)
        rows = [
            (self._stats(task, model), model)
            for model in self._models
            if self._stats(task, model)
            and self._stats(task, model).tariff_milli <= budget
        ]
        if not rows:
            return RoutingDecision((), False, "no_affordable_route_after_reserve")
        utilities = [self._utility(stats) for stats, _model in rows]
        floor = min(utilities)
        ranked = sorted(
            rows,
            key=lambda item: (
                -(max(0.0, self._utility(item[0]) - floor) / item[0].tariff_milli),
                -self._utility(item[0]),
                item[0].tariff_milli,
            ),
        )
        primary_stats, primary_model = ranked[0]
        primary = ModelPlan(primary_model, primary_stats.tariff_milli, "primary")
        if (
            self.speculative
            and is_extraction
            and self.speculative_used < self.max_speculative_extractions
            and len(ranked) > 1
        ):
            secondary_stats, secondary_model = max(
                ranked[1:],
                key=lambda item: (self._utility(item[0]), -item[0].tariff_milli),
            )
            if (
                self._utility(secondary_stats)
                >= self._utility(primary_stats) + self.min_secondary_utility_gain
                and primary.tariff_milli + secondary_stats.tariff_milli
                <= state.remaining_milli
            ):
                self.speculative_used += 1
                return RoutingDecision(
                    (
                        primary,
                        ModelPlan(
                            secondary_model,
                            secondary_stats.tariff_milli,
                            "speculative",
                        ),
                    ),
                    True,
                    "extraction_escalation",
                )
        return RoutingDecision((primary,), False, "adaptive_emu_per_sweycoin")

    def observe(self, task, decision, outcome, state) -> None:
        return None


class BudgetPolicySimulator:
    """Offline policy simulator over held-out tasks; gold only post-hoc."""

    def __init__(
        self,
        *,
        suite: GoldSuite,
        async_config: Mapping[str, Any],
        experiment_config: Mapping[str, Any],
        budget_config: Mapping[str, Any],
    ) -> None:
        self.suite = suite
        self.models = {
            str(model["id"]): model
            for model in experiment_config["models"]
            if model.get("available", False)
        }
        self.contracts = experiment_config["contracts"]
        self.model_ids = [
            str(model) for model in budget_config["models"] if str(model) in self.models
        ]
        self.cap_milli = int(budget_config["budget_cap_milli"])
        self.late_fraction = float(budget_config.get("late_billing_fraction", 0.5))
        self.train_item_count = int(budget_config.get("train_item_count", 5))
        self.tariff_multiplier = {
            str(k): float(v)
            for k, v in budget_config["model_tariff_multiplier"].items()
        }
        self.latency_multiplier = {
            str(k): float(v)
            for k, v in budget_config.get("model_latency_multiplier", {}).items()
        }
        self.false_support_penalty = float(
            budget_config.get("false_support_penalty", 0.5)
        )
        stop_cfg = budget_config.get("stop", {})
        self.stop_policy = ProductionStopPolicy(
            name="gold_free_stop",
            min_items=int(stop_cfg.get("min_items", 3)),
            min_need_coverage=float(stop_cfg.get("min_need_coverage", 0.6)),
            min_evidence_coverage=float(stop_cfg.get("min_evidence_coverage", 0.6)),
            max_abstention=float(stop_cfg.get("max_abstention", 0.1)),
            min_confidence=float(stop_cfg.get("min_confidence", 0.8)),
            max_invalid_outputs=int(stop_cfg.get("max_invalid_outputs", 1_000_000)),
        )
        self.trace = self._build_trace(async_config)
        self.inner = OfflineHeuristicProvider(
            model_ids=self.model_ids, simulated_latency_ms=0.0
        )
        items = list(suite.items)
        self.training_items = items[: self.train_item_count]
        self.heldout_items = items[self.train_item_count :]
        train_ids = {item.item_id for item in self.training_items}
        heldout_ids = {item.item_id for item in self.heldout_items}
        all_specs = all_microtask_specs(suite)
        self.training_tasks = tuple(
            spec for spec in all_specs if spec.item_id in train_ids
        )
        self.heldout_tasks = tuple(
            spec for spec in all_specs if spec.item_id in heldout_ids
        )
        self.specs_by_item: dict[str, list[TaskSpec]] = defaultdict(list)
        for spec in self.heldout_tasks:
            self.specs_by_item[spec.item_id].append(spec)
        self.training_outcomes = self._build_outcomes(self.training_tasks, compute_truth=True)
        self.heldout_outcomes = self._build_outcomes(self.heldout_tasks, compute_truth=False)
        self.training_stats = self._training_stats()

    def _build_trace(self, async_config: Mapping[str, Any]) -> LatencyTrace:
        trace_cfg = async_config["trace"]
        seed = int(trace_cfg["seed"])
        straggler_items = choose_straggler_items(
            [item.item_id for item in self.suite.items],
            count=int(trace_cfg.get("straggler_count", 0)),
            seed=seed,
        )
        multipliers = {
            str(k): float(v)
            for k, v in trace_cfg.get("model_multiplier", {}).items()
        }
        multipliers.update(self.latency_multiplier)
        return LatencyTrace(
            base_ms={str(k): float(v) for k, v in trace_cfg["base_ms"].items()},
            model_multiplier=multipliers,
            jitter_fraction=float(trace_cfg.get("jitter_fraction", 0.0)),
            straggler_items=straggler_items,
            straggler_extra_ms=float(trace_cfg.get("straggler_extra_ms", 0.0)),
            seed=seed,
            scale=1.0,
            extra_ms_by_task={
                str(k): float(v)
                for k, v in trace_cfg.get("extra_ms_by_task", {}).items()
            },
        )

    def _tariff(self, model_id: str, kind: Kind) -> int:
        base = int(self.contracts[str(kind)]["sweycoins_milli"])
        return max(1, int(round(base * self.tariff_multiplier.get(model_id, 1.0))))

    def _request(self, spec: TaskSpec, model_id: str) -> ChatRequest:
        system, user = render_prompt(spec.kind, spec.input_data)
        base = int(self.contracts[str(spec.kind)]["max_output_tokens"])
        headroom = int(self.models[model_id].get("output_token_headroom", 0))
        return ChatRequest(
            request_id=f"budget:{spec.task_id}:{model_id}",
            task_id=spec.task_id,
            kind=spec.kind,
            model_id=model_id,
            system=system,
            user=user,
            schema=output_schema(spec.kind),
            max_output_tokens=base + max(0, headroom),
            passage=spec.passage,
            input_data=spec.input_data,
            attempt=1,
            timeout_seconds=60.0,
        )

    def _build_outcomes(
        self, specs: Sequence[TaskSpec], *, compute_truth: bool
    ) -> dict[str, dict[str, ModelOutcome]]:
        outcomes: dict[str, dict[str, ModelOutcome]] = defaultdict(dict)
        for spec in specs:
            for model_id in self.model_ids:
                response = self.inner.respond(self._request(spec, model_id))
                output = None
                verification = None
                error = None
                try:
                    output = parse_json_output(response.content)
                    verification = verify_output(
                        spec.kind,
                        output,
                        passage=spec.passage,
                        max_quotes=int(
                            self.contracts.get("extraction", {}).get("max_quotes", 3)
                        ),
                        min_quote_chars=int(
                            self.contracts.get("extraction", {}).get(
                                "min_quote_chars", 10
                            )
                        ),
                    )
                except ContractViolation as exc:
                    error = str(exc)
                attempt = Attempt(
                    task_id=spec.task_id,
                    item_id=spec.item_id,
                    kind=spec.kind,
                    model_id=model_id,
                    reservation_id=None,
                    tariff_milli=self._tariff(model_id, spec.kind),
                    ready_ns=0,
                    start_ns=1,
                    end_ns=2,
                    output=output,
                    verification=verification,
                    error=error,
                )
                confidence = verifier_confidence(spec, attempt)
                abstained = output is None or (
                    spec.kind is Kind.EXTRACTION
                    and not (output or {}).get("quotes", [])
                )
                correct = False
                false_support = False
                if compute_truth:
                    result = TaskResult(
                        task_id=spec.task_id,
                        kind=str(spec.kind),
                        output=output,
                        verification=verification,
                        error=error,
                        selected_models=(model_id,),
                    )
                    scored = score_suite((spec,), {spec.task_id: result})
                    extraction = scored["extraction"]
                    false_support = (
                        extraction["unsupported_quotes"]
                        + extraction["unverified_quotes"]
                    ) > 0
                    correct = bool(scored["correct"])
                outcomes[spec.task_id][model_id] = ModelOutcome(
                    task_id=spec.task_id,
                    item_id=spec.item_id,
                    contract=str(spec.kind),
                    model_id=model_id,
                    output=output,
                    verification=verification,
                    error=error,
                    confidence=confidence,
                    latency_ms=self.trace.duration_ms(
                        task_id=spec.task_id,
                        item_id=spec.item_id,
                        model_id=model_id,
                        kind=str(spec.kind),
                    ),
                    tokens=dict(response.tokens),
                    billed_usd=response.billed_usd,
                    price_period=response.price_period,
                    tariff_milli=self._tariff(model_id, spec.kind),
                    correct=correct,
                    false_support=false_support,
                    abstained=abstained,
                )
        return outcomes

    def _training_stats(self) -> dict[tuple[str, str], ModelStats]:
        grouped: dict[tuple[str, str], list[ModelOutcome]] = defaultdict(list)
        for task_id, per_model in self.training_outcomes.items():
            for model_id, outcome in per_model.items():
                grouped[(outcome.contract, model_id)].append(outcome)
        stats: dict[tuple[str, str], ModelStats] = {}
        for (contract, model_id), rows in grouped.items():
            count = len(rows)
            stats[(contract, model_id)] = ModelStats(
                model_id=model_id,
                contract=contract,
                count=count,
                accuracy=round(sum(1 for row in rows if row.correct) / count, 6),
                false_support_rate=round(
                    sum(1 for row in rows if row.false_support) / count, 6
                ),
                abstention_rate=round(
                    sum(1 for row in rows if row.abstained) / count, 6
                ),
                invalid_rate=round(
                    sum(
                        1
                        for row in rows
                        if row.verification is None or not row.verification.ok
                    )
                    / count,
                    6,
                ),
                average_confidence=round(
                    sum(row.confidence for row in rows) / count, 6
                ),
                average_latency_ms=round(
                    sum(row.latency_ms for row in rows) / count, 3
                ),
                tariff_milli=rows[0].tariff_milli,
            )
        return stats

    def run(self, policy: BudgetRoutingPolicy) -> dict[str, Any]:
        policy.begin(
            self.heldout_tasks,
            cap_milli=self.cap_milli,
            training=self.training_stats,
            models=self.model_ids,
        )
        meter = SweycoinMeter(self.cap_milli)
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
        for spec in self.heldout_tasks:
            if stop_signals is not None and self.stop_policy.evaluate(stop_signals):
                stopped = True
                break
            decision = policy.choose(spec, state)
            if not decision.plans:
                budget_denied += 1
                continue
            reservations: list[tuple[ModelPlan, int | None]] = []
            failed = False
            for plan in decision.plans:
                key = f"{policy.name}:{spec.task_id}:{plan.model_id}:{plan.role}"
                try:
                    reservation = meter.reserve(key, plan.tariff_milli)
                except BudgetError:
                    reservation = None
                reservations.append((plan, reservation))
                if reservation is None:
                    failed = True
                    break
            if failed:
                for plan, reservation in reservations:
                    if reservation is not None:
                        meter.release(reservation)
                budget_denied += 1
                continue
            outcomes = [
                (plan, reservation, self.heldout_outcomes[spec.task_id][plan.model_id])
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
                assert reservation is not None
                if plan is winner_plan:
                    meter.settle(reservation, plan.tariff_milli)
                elif outcome.latency_ms <= winner.latency_ms:
                    meter.settle(reservation, plan.tariff_milli)
                else:
                    pending_before += plan.tariff_milli
                    digest = hashlib.sha256(
                        f"late:{policy.name}:{spec.task_id}:{plan.model_id}".encode(
                            "utf-8"
                        )
                    ).hexdigest()
                    fraction = int(digest[:8], 16) / 0xFFFFFFFF
                    if fraction < self.late_fraction:
                        meter.settle(reservation, plan.tariff_milli)
                        late_billed += plan.tariff_milli
                    else:
                        meter.release(reservation)
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
                    reservation_id=winner_reservation,
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
            state.settled_milli = meter.spent_milli
            state.pending_milli = meter.reserved_milli
            state.remaining_milli = meter.available_milli
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
                "settled_milli": meter.spent_milli,
                "pending_before_reconcile_milli": pending_before,
                "late_billed_milli": late_billed,
                "released_milli": released,
                "pending_after_reconcile_milli": meter.reserved_milli,
                "available_milli": meter.available_milli,
                "denied_tasks": budget_denied,
                "overspent_milli": 0,
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
        }


def _percentile(values: Sequence[float], quantile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, math.ceil(len(ordered) * quantile) - 1))
    return ordered[index]


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _build_trace_for_report(async_config: Mapping[str, Any]) -> LatencyTrace:
    trace_cfg = async_config["trace"]
    seed = int(trace_cfg["seed"])
    return LatencyTrace(
        base_ms={str(k): float(v) for k, v in trace_cfg["base_ms"].items()},
        model_multiplier={
            str(k): float(v)
            for k, v in trace_cfg.get("model_multiplier", {}).items()
        },
        jitter_fraction=float(trace_cfg.get("jitter_fraction", 0.0)),
        straggler_items=frozenset(),
        straggler_extra_ms=float(trace_cfg.get("straggler_extra_ms", 0.0)),
        seed=seed,
        scale=1.0,
    )


async def run_adaptive_budget_benchmark(
    experiment_config_path: Path,
    async_config_path: Path,
    budget_config_path: Path,
) -> dict[str, Any]:
    experiment = _load_json(experiment_config_path)
    async_config = _load_json(async_config_path)
    budget_config = _load_json(budget_config_path)
    root = experiment_config_path.resolve().parent.parent
    suite = load_fixtures(
        root / experiment["fixtures"]["gold"],
        expected_sha256=experiment["fixtures"]["sha256"],
    )
    simulator = BudgetPolicySimulator(
        suite=suite,
        async_config=async_config,
        experiment_config=experiment,
        budget_config=budget_config,
    )
    policies: list[BudgetRoutingPolicy] = [
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
        "experiment_id": "swe-microtask-swarm-adaptive-budget",
        "evidence_kind": "offline_adaptive_budget_policy",
        "generated_at": datetime.now(UTC).isoformat(),
        "fixture_sha256": suite.sha256,
        "budget_cap_milli": simulator.cap_milli,
        "train_item_ids": [item.item_id for item in simulator.training_items],
        "heldout_item_ids": [item.item_id for item in simulator.heldout_items],
        "training_task_count": len(simulator.training_tasks),
        "heldout_task_count": len(simulator.heldout_tasks),
        "models": simulator.model_ids,
        "tariff_multiplier": simulator.tariff_multiplier,
        "latency_multiplier": simulator.latency_multiplier,
        "training_stats": [
            stats.to_dict() for stats in simulator.training_stats.values()
        ],
        "runs": runs,
        "summary": _summary(runs),
        "totals": _totals(runs),
        "notes": [
            "Training items fit model/contract statistics; held-out gold is used only after the run.",
            "Policy sees task input, training statistics, and gold-free outcome views.",
            "Offline simulator; no production scheduler, no new ledger, no model/network call.",
            "Runtime budget is the local pilot meter; SWE PR #11 is unattached.",
        ],
    }


def _summary(runs: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    rows = []
    for run in runs:
        rows.append(
            {
                "policy": run["policy"],
                "stopped": run["stopped"],
                "completed_tasks": run["completed_tasks"],
                "accuracy": run["quality"]["accuracy"],
                "coverage": run["quality"]["extraction"]["coverage"],
                "false_support": run["quality"]["false_support"]["false_support_rate"],
                "need_coverage": run["need_coverage"],
                "abstention_rate": run["abstention_rate"],
                "average_confidence": run["average_confidence"],
                "latency_ms": run["latency_ms"],
                "latency_p95_ms": run["latency_p95_ms"],
                "calls": run["calls"],
                "speculative_tasks": run["speculative_tasks"],
                "settled_milli": run["budget"]["settled_milli"],
                "pending_before_milli": run["budget"][
                    "pending_before_reconcile_milli"
                ],
                "late_milli": run["budget"]["late_billed_milli"],
                "released_milli": run["budget"]["released_milli"],
                "denied_tasks": run["budget"]["denied_tasks"],
                "overspent_milli": run["budget"]["overspent_milli"],
                "calculated_usd": run["cost"]["all_in"]["calculated_usd"],
                "calculated_usd_complete_calls": run["cost"][
                    "all_in_complete_calls_calculated_usd"
                ],
            }
        )
    return {"run_count": len(runs), "rows": rows}


def _totals(runs: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    calculated = 0.0
    calculated_complete = 0.0
    for run in runs:
        value = run["cost"]["all_in"]["calculated_usd"]
        if value != "N/A":
            calculated += float(value)
        complete = run["cost"]["all_in_complete_calls_calculated_usd"]
        if complete != "N/A":
            calculated_complete += float(complete)
    return {
        "calls": sum(int(run["calls"]) for run in runs),
        "settled_milli": sum(int(run["budget"]["settled_milli"]) for run in runs),
        "pending_before_reconcile_milli": sum(
            int(run["budget"]["pending_before_reconcile_milli"]) for run in runs
        ),
        "late_billed_milli": sum(
            int(run["budget"]["late_billed_milli"]) for run in runs
        ),
        "released_milli": sum(int(run["budget"]["released_milli"]) for run in runs),
        "denied_tasks": sum(int(run["budget"]["denied_tasks"]) for run in runs),
        "overspent_milli": sum(
            int(run["budget"]["overspent_milli"]) for run in runs
        ),
        "calculated_usd": round(calculated, 9),
        "calculated_usd_complete_calls": round(calculated_complete, 9),
        "actual_usd": "N/A",
    }


def write_adaptive_budget_report(report: Mapping[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def render_adaptive_budget_markdown(report: Mapping[str, Any]) -> str:
    lines = [
        "# Adaptive budget/routing policy comparison (offline)",
        "",
        f"Generated: {report['generated_at']}",
        f"Fixture SHA-256: `{report['fixture_sha256']}`",
        f"Budget cap: {report['budget_cap_milli']} milli-Sweycoin per policy",
        f"Training items: {', '.join(report['train_item_ids'])}",
        f"Held-out items: {', '.join(report['heldout_item_ids'])}",
        "Gold is used for training statistics and post-hoc scoring; the held-out policy never reads it.",
        "No model or network call was made.",
        "",
        "| Policy | Stop | Tasks | Accuracy | Coverage | False support | Need cov. | Abstain | Conf. | Latency | p95 | Calls | Spec. | Settled | Pending | Late | Released | Denied | Overspend | Calc USD |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in report["summary"]["rows"]:
        lines.append(
            "| {policy} | {stop} | {tasks} | {acc} | {cov} | {fs} | {need} | {abst} | {conf} | {lat} | {p95} | {calls} | {spec} | {settled} | {pending} | {late} | {released} | {denied} | {over} | {usd} |".format(
                policy=row["policy"],
                stop=row["stopped"],
                tasks=row["completed_tasks"],
                acc=row["accuracy"],
                cov=row["coverage"],
                fs=row["false_support"],
                need=row["need_coverage"],
                abst=row["abstention_rate"],
                conf=row["average_confidence"],
                lat=row["latency_ms"],
                p95=row["latency_p95_ms"],
                calls=row["calls"],
                spec=row["speculative_tasks"],
                settled=row["settled_milli"],
                pending=row["pending_before_milli"],
                late=row["late_milli"],
                released=row["released_milli"],
                denied=row["denied_tasks"],
                over=row["overspent_milli"],
                usd=row["calculated_usd"],
            )
        )
    totals = report.get("totals")
    lines.extend(
        [
            "",
            "`actual_usd` is N/A; calculated USD is a price-list equivalent.",
            "Equal-split uses a fixed per-task share; greedy takes the highest-accuracy "
            "affordable model; fixed routing uses the configured per-contract route; "
            "adaptive uses expected marginal utility per Sweycoin and may speculatively "
            "race a cheap primary against a stronger extraction model.",
        ]
    )
    if totals is not None:
        lines.append(
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
            )
        )
    lines.append("")
    return "\n".join(lines)


__all__ = [
    "AdaptivePolicy",
    "BudgetPolicySimulator",
    "BudgetRoutingPolicy",
    "EqualSplitPolicy",
    "FixedRoutingPolicy",
    "GreedyPolicy",
    "ModelPlan",
    "ModelStats",
    "OutcomeView",
    "PolicyState",
    "RoutingDecision",
    "render_adaptive_budget_markdown",
    "run_adaptive_budget_benchmark",
    "write_adaptive_budget_report",
]
