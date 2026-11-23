"""Offline tests for the layer-barrier vs ready-queue experiment."""

from __future__ import annotations

from pathlib import Path

import pytest

from barrier_free_scheduler.fixtures import straggler_dag
from barrier_free_scheduler.layer import schedule_layers
from barrier_free_scheduler.model import (
    DagError,
    Task,
    start_delay_total_ms,
    utilization,
    validate_dependencies,
)
from barrier_free_scheduler.ready import schedule_ready
from barrier_free_scheduler.replay import replay_result

CONCURRENCY = 4


def _assert_common(tasks, result) -> None:
    validate_dependencies(tasks, result)
    assert result.peak_concurrency <= result.concurrency
    assert len(result.completion_order()) == len(tasks)
    assert all(
        run.finish_ms >= run.start_ms
        for run in result.runs
        if run.start_ms >= 0
    )


def test_straggler_ready_queue_beats_layer_barrier() -> None:
    tasks = straggler_dag()
    layer = schedule_layers(tasks, CONCURRENCY)
    ready = schedule_ready(tasks, CONCURRENCY)

    _assert_common(tasks, layer)
    _assert_common(tasks, ready)

    assert layer.makespan_ms == 180
    assert ready.makespan_ms == 130
    assert ready.makespan_ms < layer.makespan_ms
    assert start_delay_total_ms(tasks, ready) < start_delay_total_ms(tasks, layer)
    assert utilization(tasks, ready) > utilization(tasks, layer)


def test_ready_queue_starts_downstream_before_straggler_finishes() -> None:
    tasks = straggler_dag()
    layer = schedule_layers(tasks, CONCURRENCY)
    ready = schedule_ready(tasks, CONCURRENCY)

    slow_finish = ready.run_for("slow").finish_ms
    assert ready.run_for("mid_0").start_ms < slow_finish
    assert layer.run_for("mid_0").start_ms >= slow_finish


def test_layer_baseline_is_a_barrier_between_layers() -> None:
    tasks = straggler_dag()
    layer = schedule_layers(tasks, CONCURRENCY)
    layer_zero_finish = max(
        layer.run_for(task.task_id).finish_ms
        for task in tasks
        if task.task_id == "slow" or task.task_id.startswith("fast_")
    )
    assert layer.run_for("mid_0").start_ms == layer_zero_finish
    assert layer_zero_finish == 120


def test_schedule_is_deterministic_and_replayable() -> None:
    tasks = straggler_dag()
    first = schedule_ready(tasks, CONCURRENCY)
    second = schedule_ready(tasks, CONCURRENCY)
    assert first == second
    assert replay_result(tasks, first) == first

    layer_first = schedule_layers(tasks, CONCURRENCY)
    layer_second = schedule_layers(tasks, CONCURRENCY)
    assert layer_first == layer_second
    assert replay_result(tasks, layer_first) == layer_first


def test_ready_queue_cancellation_stops_new_starts() -> None:
    tasks = tuple(Task(f"task-{index}", 100) for index in range(4))
    result = schedule_ready(tasks, 2, cancel_at_ms=35)

    assert result.makespan_ms == 35
    assert result.peak_concurrency <= 2
    assert result.completion_order() == ()
    assert len(result.cancelled) == 4
    assert all(run.cancelled for run in result.runs)
    assert all(
        event.at_ms < 35
        for event in result.events
        if event.kind == "start"
    )
    replay_result(tasks, result)


def test_cancellation_after_all_tasks_is_a_noop() -> None:
    tasks = (Task("a", 10), Task("b", 10))
    result = schedule_ready(tasks, 2, cancel_at_ms=100)
    assert result.cancelled == ()
    assert result.makespan_ms == 10
    assert len(result.completion_order()) == 2


def test_dependency_errors_are_rejected() -> None:
    with pytest.raises(DagError):
        schedule_ready((Task("a", 1, ("missing",)), Task("b", 1)), 1)
    with pytest.raises(DagError):
        schedule_ready((Task("a", 1, ("b",)), Task("b", 1, ("a",))), 1)
    with pytest.raises(DagError):
        schedule_ready((Task("a", 1), Task("a", 1)), 1)
    with pytest.raises(DagError):
        Task("a", 0)


def test_experiment_has_no_budget_or_swe_import() -> None:
    root = Path(__file__).resolve().parents[1] / "barrier_free_scheduler"
    text = "\n".join(path.read_text() for path in sorted(root.glob("*.py")))
    assert "swe.budget" not in text
    assert "RuntimeBudget" not in text
    assert "swe_native" not in text
