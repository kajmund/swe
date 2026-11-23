# Barrier-free scheduler experiment

Status: offline simulation, not production code.

This experiment compares the current SWE microtask shape with an event-driven,
work-conserving ready queue on the same static DAG. It is deliberately isolated
from `swe-native`, the frozen contracts, and PR11's `RuntimeBudget`: no budget
code is imported and no paid model/network call is made.

## What is modeled

SWE microtasks compile a static DAG into **one bounded `map` per topological
layer** (`python/swe/microtasks.py`). The Rust machine is work-conserving inside
one map: `spawn_map_children` starts the next child as soon as one completes.
The barrier is between maps: the next layer starts only when the previous map's
`remaining` reaches zero.

The baseline here models that layer barrier with the same bounded concurrency.
The ready-queue model admits a task as soon as all of its dependencies have
finished, still bounded by the same concurrency. Both are virtual-time
simulations, so results and event logs are deterministic.

Not modeled: real handler latency, provider calls, journal/RunStore persistence,
budget, dynamic insertion of new tasks after start, or a SWD wire change. A
production ready queue would need a host scheduler or a new SWD construct and
would have to preserve invocation IDs, journal replay, cancellation, and budget.

## Run

```sh
cd experiments/barrier-free-scheduler
PYTHONPATH=. python3 -m pytest tests -q
PYTHONPATH=. python3 -m barrier_free_scheduler
```

No third-party Python package is imported by the experiment itself. The test
runner only needs `pytest`.

## Straggler DAG

- `slow`: 120 ms.
- `fast_0..fast_9`: 10 ms each, no dependencies.
- `mid_i`: 10 ms, depends on `fast_i`.
- `final_i`: 10 ms, depends on `mid_i`.
- `final_slow`: 10 ms, depends on `slow`.
- Concurrency: 4.

Measured result (`python -m barrier_free_scheduler`):

| Policy | Makespan | Peak | Utilization | Cumulative start delay |
| --- | ---: | ---: | ---: | ---: |
| `layer-barrier` | 180 ms | 4 | 0.597 | 1400 ms |
| `ready-queue` | 130 ms | 4 | 0.827 | 580 ms |

The ready queue starts `mid_0` at 30 ms, before the 120 ms straggler finishes.
The layer baseline starts it at 120 ms, after the whole first layer is done.

## Files

- `barrier_free_scheduler/model.py` — task/DAG model, stable topological layers,
  metrics, event log.
- `barrier_free_scheduler/layer.py` — bounded layer-barrier baseline.
- `barrier_free_scheduler/ready.py` — event-driven ready queue with bounded
  concurrency and cancellation.
- `barrier_free_scheduler/replay.py` — validates/replays an event log.
- `barrier_free_scheduler/fixtures.py` — the straggler DAG.
- `tests/test_scheduler.py` — dependency, concurrency, determinism, replay,
  cancellation, and no-budget assertions.

## Scope

This is a feasibility measurement, not a proposal to change SWD 1.0. PR11 is
open; this branch is based on `origin/main` (`eff1c7e`) and does not modify it.
