"""Synthetic DAGs for the offline scheduler experiment."""

from __future__ import annotations

from .model import Task


def straggler_dag() -> tuple[Task, ...]:
    """One 120 ms straggler, ten short chains, and a slow-only tail."""

    tasks: list[Task] = [Task("slow", 120)]
    tasks.extend(Task(f"fast_{index}", 10) for index in range(10))
    tasks.extend(
        Task(f"mid_{index}", 10, (f"fast_{index}",)) for index in range(10)
    )
    tasks.extend(
        Task(f"final_{index}", 10, (f"mid_{index}",)) for index in range(10)
    )
    tasks.append(Task("final_slow", 10, ("slow",)))
    return tuple(tasks)


__all__ = ["straggler_dag"]
