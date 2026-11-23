"""Print the straggler-DAG comparison.

Run from the experiment directory:

    python3 -m barrier_free_scheduler
"""

from __future__ import annotations

from .fixtures import straggler_dag
from .layer import schedule_layers
from .model import start_delay_total_ms, utilization
from .ready import schedule_ready
from .replay import replay_result


def main() -> int:
    tasks = straggler_dag()
    concurrency = 4
    layer = schedule_layers(tasks, concurrency)
    ready = schedule_ready(tasks, concurrency)

    replay_result(tasks, layer)
    replay_result(tasks, ready)

    print(f"{'policy':<16}{'makespan_ms':>12}{'peak':>7}{'utilization':>13}{'delay_ms':>11}")
    for result in (layer, ready):
        print(
            f"{result.policy:<16}"
            f"{result.makespan_ms:>12}"
            f"{result.peak_concurrency:>7}"
            f"{utilization(tasks, result):>13.3f}"
            f"{start_delay_total_ms(tasks, result):>11}"
        )

    slow_finish = ready.run_for("slow").finish_ms
    mid_0 = ready.run_for("mid_0")
    print(
        "ready queue starts mid_0 at "
        f"{mid_0.start_ms} ms, before slow finishes at {slow_finish} ms"
    )
    print(
        "layer barrier starts mid_0 at "
        f"{layer.run_for('mid_0').start_ms} ms (after the layer-0 map)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
