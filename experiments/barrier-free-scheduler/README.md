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

## Real-handler SWE host experiment

`swe_host/` runs the same idea through the actual SWE host/runtime with real
async handlers, `WorkflowRunner`, and `MemoryRunStore`. It uses the native
`swe_native` wheel; no model or network call is made.

```sh
# one-time native build from the repository root
cd python
uv sync --locked --group dev
uvx maturin build --manifest-path ../crates/swe-native/Cargo.toml \
  --interpreter .venv/bin/python --out dist
uv pip install --reinstall dist/*.whl

# from experiments/barrier-free-scheduler
PYTHONPATH=. ../../python/.venv/bin/python -m pytest swe_host/tests -q -s
```

Measured with real `asyncio.sleep` handlers on the same straggler DAG:

```text
real handlers: layer≈195–198ms ready≈146–147ms layer_peak=4 ready_peak=4
5 passed
```

The experiment also verifies:

- a static ready queue inside one SWE `map`, with host `asyncio.Event`s for
  dependencies and per-task `InvocationRecord`s;
- crash/resume: a store that crashes after five records, then `resume` with
  dependency events pre-seeded from `RunStore.replay_results`; the run completes
  and replayed tasks do not call the handler again;
- cancellation: `cancel_event` cancels the ready-queue handlers and the run
  becomes `cancelled`;
- dynamic arrival: a fixed worker-pool `map` executes a task submitted after
  the run started, but the journal records two worker invocations, not the four
  task invocations.

The static ready queue is a host workaround, not a production scheduler:
waiting handlers hold map slots, and dependency state is not owned by the
machine. Dynamic tasks lose per-task journal/replay with the worker-pool shape.
A first-class Rust/SWD `map` mode or `dag` node is required for the real
semantics; see `docs/analysis/2026-11-24-barrier-free-scheduler.md`.

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
- `swe_host/coordinator.py` — static host ready queue and dynamic worker pool.
- `swe_host/flows.py` — SWD map definitions for the real-handler experiment.
- `swe_host/tests/test_swe_host_ready_queue.py` — real handlers, journal/replay,
  cancellation, dynamic arrival, and bounded concurrency.

## Scope

This is a feasibility measurement, not a proposal to change SWD 1.0. PR11 is
open; this branch is based on `origin/main` (`eff1c7e`) and does not modify it.
PR13's offline preflight (`a69bd50`, `experiment/agent-microtask-contract`) is a
separate branch and was not touched. No `RuntimeBudget` is imported and no paid
model/network call is made.
