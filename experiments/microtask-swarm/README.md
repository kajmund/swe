# SWE microtask-swarm pilot

Status: `OFFLINE_PREPARED_NOT_LIVE`. The experiment is on `main`. The frozen
baseline was imported in `0f7a42f` from `experiment/microtask-swarm` at
`36ccdf7`; the runtime-budget integration path was added on top and merged in
PR #15. It does not modify SWE core, the frozen contracts, or the Hy4 worktree.

No paid model call was made. The offline pilot uses deterministic lexical
personas; its quality numbers are pipeline evidence, not results about any
Ollama model.

## What this asks

Can the same small amount of work be done better as typed independent
microtasks than as one model call, and does heterogeneous competition with an
independent verifier help? The pilot compares three execution modes over the
same frozen gold tasks:

| Mode | Calls | What is measured |
| --- | --- | --- |
| `single_call` | One combined JSON call per item | End-to-end quality, latency, cost of a monolithic call |
| `distributed_independent` | One call per typed microtask, no shared prose or state | Isolation effect, per-contract quality, bounded fan-out |
| `heterogeneous_competition` | Every available model competes per microtask; a deterministic verifier that never sees model identity selects the first contract-valid answer and cancels the rest | Candidate diversity, early cancellation, verifier-inclusive cost |

There is no agent-to-agent prose. A worker receives a JSON payload plus a JSON
Schema and must return exactly one JSON object with the exact contract keys.
Extra keys, markdown fences, and prose are contract violations.

## Typed contracts

| Contract | Output | Verification |
| --- | --- | --- |
| `relevance` | `{"relevant": bool}` | Strict bool and exact key set |
| `entailment` | `{"label": "entailment" | "contradiction" | "neutral"}` | Closed enum and exact key set |
| `atomicity` | `{"atomic": bool}` | Strict bool and exact key set |
| `extraction` | `{"quotes": [string, ...]}` | Every quote must be a byte-for-byte passage substring; exact key set, max 3 quotes, min 10 chars, no duplicates |
| `operation_selection` | `{"operation": enum}` | Closed operation menu and exact key set |
| `combined` | All five children | Each child is validated independently |

Exact quote verification uses `quote in passage` after JSON decoding. There is
no case folding, whitespace normalisation, fuzzy match, or semantic shortcut.
A quote that is a valid substring but not one of the gold spans is counted as
unsupported; a quote that is not a substring is counted as unverified. Both are
false support.

## Frozen gold fixtures

`fixtures/gold.json` is pinned by SHA-256 in `config/experiment.json`:

- 10 items, 5 domains (`biology`, `engineering`, `finance`, `law`, `medicine`);
- 5 microtasks per item, 50 scored microtasks;
- every extraction gold quote is an exact passage substring;
- prompts receive only task input fields; item ids, domains, gold objects,
  gold labels, and other tasks' gold are never rendered;
- any fixture drift fails the load.

The fixture is small on purpose. It is a pilot gate, not a benchmark. It is
not copied from the Hy4 Research Gym and does not reuse Hy4 questions, corpus,
tool menu, or cost protocol.

## Verified Ollama Cloud pins

Verified on 2026-11-23 with free metadata reads only:
`GET https://ollama.com/api/tags` and `POST https://ollama.com/api/show`.
No chat completion was sent. Prices are the published peak rows on
`https://ollama.com/pricing`.

| Arm | Exact API tag | Digest | Capabilities | Context | Thinking | Input / cached / output USD per 1M |
| --- | --- | --- | --- | ---: | --- | --- |
| GPT-OSS 20B | `gpt-oss:20b` | `05afbac4bad6` | completion, tools, thinking | 131,072 | values `low/medium/high`, default `medium`; **no `false`**; +128 output-token headroom | 0.07 / 0.035 / 0.30 |
| Nemotron 3 Super | `nemotron-3-super` | `da11955bb451` | completion, thinking, tools | 262,144 | `false/true`, default `true`; `false` supported | 0.015 / 0.015 / 0.60 |
| MiMo V2.6 Flash | `mimo-v2.6-flash` | `8b2b3c0ae11a` | completion, thinking, tools, vision | 1,048,576 | `false/true`, default `true`; `false` supported | 0.14 / 0.0028 / 0.28 |
| DeepSeek V4.1 Flash (baseline) | `deepseek-v4.1-flash` | `97308b916fc3` | completion, thinking, tools, vision | 1,048,576 | `false/low/high/max`, default `high`; `false` supported | peak 0.30 / 0.006 / 1.20; off-peak 0.15 / 0.003 / 0.60 |
| Nemotron 3 Nano | `nemotron-3-nano:30b-cloud` | — | — | — | — | **unavailable** |

Nemotron 3 Nano is a blocker, not an arm: `POST /api/show` returned HTTP 410
("retired on November 9, 2026") and the tag is absent from `/api/tags`.

GPT-OSS 20B is a blocker for a pure "reasoning off" stratum: `/api/show` lists
`low`, `medium`, `high` and no `false`. The request builder uses `think: "low"`
for that arm and reports `reasoning_off: false`. The other three arms use
`think: false`.

Every request uses `stream: false`, a JSON Schema in `format`, and strict
`options: {"temperature": 0, "num_predict": <contract limit>, "num_ctx": 4096}`.
Output bounds are 32 tokens for classifications, 192 for extraction, and 640 for
the combined call. GPT-OSS 20B adds a 128-token headroom because `think: "low"`
is its minimum and consumed the entire 32-token classification budget in the
first live run, returning empty content; the same request returned valid JSON
at 128 tokens. The headroom is capability-driven, not a general relaxation.

## What is measured

Every run reports:

- quality per contract (exact label accuracy; extraction exact match);
- answered rate, verified-output rate, accuracy on verified outputs;
- extraction coverage, quote precision, unverified quotes, unsupported quotes,
  false-support count and rate;
- per-call p50/p95 wall latency and end-to-end wall time;
- scored-subtask and call throughput;
- tokens: input, output, reasoning, cache-read, cache-write;
- calculated USD (price-list equivalent) and actual USD (only when every call
  reports `billed_usd`; otherwise `N/A`), separately, never substituted;
- retries and verifier calls included in all-in cost;
- Sweycoin milli charges per attempt and per verifier call;
- requested vs effective concurrency, provider limit, peak in-flight, and
  whether the request exceeded the assumed plan limit;
- early-cancelled tasks and cancelled candidate calls.

`calculated_usd_complete_calls` is reported when some cancelled candidates have
unknown usage, so the all-in calculated total is `N/A` but the complete-call
subset remains visible.

For live Ollama calls, `prompt_eval_count` maps to input tokens,
`prompt_eval_cached_count` maps to cache-read tokens, and `eval_count` maps to
generated output tokens. Reasoning has no separate token field in the chat
response, so it stays `N/A` rather than being invented. If a provider omits the
cached-token count, the client records 0 cached tokens, sets
`cache_read_assumed_miss: true`, and the report labels the price-list result as
a cache-miss equivalent. Ollama's chat response does not report a per-call
charge, so `actual_usd` stays `N/A`; an account-level `/api/balance` delta would
not be a per-call charge.

## Run the offline pilot

```sh
cd experiments/microtask-swarm
PYTHONPATH=. python3 -m microtask_swarm verify-fixtures --pretty
PYTHONPATH=. python3 -m microtask_swarm offline --report reports/offline-pilot.json
PYTHONPATH=. python3 -m microtask_swarm offline --full --report /tmp/offline-full.json
PYTHONPATH=. python3 -m microtask_swarm estimate --pretty
PYTHONPATH=. python3 -m unittest discover -s tests -t . -v
```

The committed report is compact (comparison fields only). `--full` adds
per-run execution layers and every quality row; `--include-rows` adds per-task
quality rows to a full report.

The offline matrix runs 54 combinations: 6 concurrency levels for 4 models in
single-call and distributed modes, plus 6 competition runs with all 4 models.
It uses no network. Peak in-flight never exceeds the requested concurrency.

With `jsonschema` and `pyyaml` available, the DAG compiler is reused from
`swe.microtasks` rather than the local fallback:

```sh
PYTHONPATH=.:../../python uv run --with jsonschema --with pyyaml \
  python3 -m unittest tests.test_integration_microtasks -v
```

## Live Ollama Cloud results (2026-11-23)

Run under the approved **$3.00** cap against `https://ollama.com/api/chat`.
Live concurrency was 1/2/3 because the Ollama plan allows 3 concurrent
requests; requested 4/8/16/32 are covered by the offline sweep and reported as
provider-capped. The matrix produced 27 runs, 1,333 calls, 13 retries, and 449
early-cancelled candidates. The compact evidence is
[`reports/live-pilot.json`](reports/live-pilot.json).

### Best run per mode

| Mode | Model | Conc. | Accuracy | Coverage | False support | p95 |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| `single_call` | GPT-OSS 20B | 1 | 0.80 | 1.00 | 0.23 | 20.7 s |
| `distributed_independent` | Nemotron 3 Super | 2 | **0.92** | 0.90 | 0.10 | 14.9 s |
| `heterogeneous_competition` | all four | 3 | 0.90 | 0.90 | 0.10 | 53.4 s |

### Per-model comparison (best run per model/mode)

| Model | Single acc. | Distributed acc. | Single cov. | Distributed cov. | Single false support | Distributed false support |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| DeepSeek V4.1 Flash | 0.72 | 0.90 | 0.90 | 0.90 | 0.10 | 0.10 |
| GPT-OSS 20B | 0.80 | 0.86 | 1.00 | 0.90 | 0.23 | 0.10 |
| MiMo V2.6 Flash | 0.76 | **0.92** | 1.00 | 0.90 | 0.09 | 0.10 |
| Nemotron 3 Super | 0.68 | **0.92** | 0.70 | 0.90 | 0.30 | 0.10 |

Distributed typed microtasks beat the single combined call for every model.
The largest difference is `operation_selection`: DeepSeek moved from 0.10
single-call to 0.70 distributed, Nemotron from 0.10 to 0.90, MiMo from 0.40 to
0.80, and GPT-OSS from 0.40 to 0.60. Extraction was already strong in both
modes (0.8–0.9), and relevance was 1.00 for every best run.

Heterogeneous competition reached 0.90 at concurrency 3, slightly below the
best distributed arm (0.92), while using more calls and much more wall time
(p95 53.4 s versus 14.9 s). It selected one verified candidate per task and
cancelled the rest; on this small fixture it did not produce a quality win over
the best single-model distributed arm. It remains useful as a fail-closed
diversity experiment, not as a default.

Cost and accounting:

- calculated price-list equivalent across all live runs: **~$0.017**
  (complete-usage subset ~$0.022);
- `actual_usd`: **N/A**; Ollama's chat response does not report a per-call
  charge;
- Sweycoins across the matrix: **637,750 milli (637.75)** including retries,
  verifier logic, and pending/cancelled calls;
- the approved $3.00 cap was not approached.

The GPT-OSS output-headroom fix was measured directly: before it, distributed
GPT-OSS scored 0.18 with 40 retries; after it, a targeted distributed run
scored 0.86 with 0 retries. The full matrix then produced only 13 retries in
1,333 calls.

Caveats: 10 items / 50 microtasks, one run per configuration, so this is a
pilot rather than a statistically powered benchmark. Temperature is 0, but the
provider still showed run-to-run variation. GPT-OSS is low-reasoning, not
reasoning-off, so its arm is not strictly comparable on reasoning cost.
Reasoning tokens are not separately reported by Ollama, and live 4/8/16/32
were not dispatched because of the plan's 3-request limit.

A per-contract deep dive on Nemotron distributed vs single, including Wilson
intervals, false-support accounting, normalized latency, cost per correct
microtask, and confounders, is in
[`reports/nemotron-distributed-analysis.md`](reports/nemotron-distributed-analysis.md).

## Async scheduling benchmark (offline)

The A/B/C/D benchmark isolates scheduling from model quality with a
deterministic latency trace:

- **A** — one combined large-model call per item (`deepseek-v4.1-flash`);
- **B** — sequential microtasks in dependency order (`nemotron-3-super`);
- **C** — bounded parallel microtasks with a global barrier per topological
  layer;
- **D** — event-driven ready queue: a microtask starts when its own
  dependencies finish, without waiting for unrelated tasks in the same layer.

B/C/D use the same frozen fixtures, verifier, model, and budget. The
dependency DAG is relevance → extraction → {entailment, atomicity} →
operation selection. The trace includes deterministic jitter and two
straggler items, and the provider rate limit is 3. Cancellation and early stop
keep reservations pending; the run reconciles them with a deterministic
late-billing fraction. Quality values come from the offline heuristic personas,
not from live model calls; only the scheduling and accounting are being
measured here.

```sh
PYTHONPATH=. python3 -m microtask_swarm async \
  --report reports/async-benchmark.json \
  --markdown reports/async-benchmark.md
```

Evidence: [`reports/async-benchmark.json`](reports/async-benchmark.json) and
[`reports/async-benchmark.md`](reports/async-benchmark.md). Reproducible tests:
`tests/test_async_benchmark.py`.

### Main results (no early stop, no cancellation)

| Strategy | Best wall | First verified | p50 task | p95 task | Critical path | Provider util | Accuracy | Coverage | Calls | Settled milli | Calc USD |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| A single large | 5.26 s | 1.28 s | 2.86 s | 5.26 s | 2.14 s | 0.88 | 0.68 | 0.10 | 10 | 12,500 | $0.00137 |
| B sequential | 15.51 s | 0.11 s | 0.17 s | 0.91 s | 3.53 s | 0.33 | 0.90 | 0.80 | 50 | 20,000 | $0.00040 |
| C layered barrier | **5.48 s** | 0.10 s | 0.78 s | 1.97 s | 3.53 s | **0.94** | 0.90 | 0.80 | 50 | 20,000 | $0.00040 |
| D event ready | 5.76 s | 0.10 s | 0.25 s | 1.73 s | 3.53 s | 0.90 | 0.90 | 0.80 | 50 | 20,000 | $0.00040 |

Readings:

- **A** has the fewest calls and lowest Sweycoin charge, but its accuracy and
  extraction coverage are much lower and its first verified result arrives
  about 12× later. It also uses a different (large) model, so the A-vs-B/C/D
  comparison confounds model and decomposition.
- **B** is deterministic and simple but 2.8× slower than C/D. Its provider
  utilization is only 0.33 because one call runs at a time.
- **C and D** have identical quality (same fixtures, verifier, and model).
  Under the 3-request provider limit, C's larger batches reach slightly lower
  makespan (5.48 s vs 5.76 s) and higher provider utilization. D gives much
  lower per-task latency (p50 0.25 s vs 0.78 s) because tasks start as soon as
  their own dependencies finish, but its makespan is limited by provider
  capacity and its worker utilization is lower. Both are 1.55–1.63× the DAG
  critical path of 3.53 s.
- The isolated straggler test shows the intended D advantage: when a slow
  task blocks a barrier but is not on the longest chain, D finishes before C.
  The main trace's provider limit and task mix make C marginally faster.

### Early stop and cancellation

| Scenario | Wall | Calls | Verified | Cancelled | Unscheduled | Accuracy | Coverage | Pending milli | Late milli | Released milli |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| C early stop (25) | 2.90 s | 40 | 25 | 15 | 10 | 0.42 | 0.80 | 500 | 500 | 0 |
| D early stop (25) | 2.60 s | 32 | 25 | 7 | 18 | 0.44 | 0.80 | 1,250 | 1,250 | 0 |
| C cancel at 1.5 s | 1.50 s | 20 | 13 | 7 | 30 | 0.22 | 0.20 | 3,000 | 2,000 | 1,000 |
| D cancel at 1.5 s | 1.50 s | 24 | 16 | 8 | 26 | 0.28 | 0.50 | 3,000 | 2,000 | 1,000 |

Early stop reaches the 25-verified target sooner in D and with fewer calls,
but leaves more reservations pending because more tasks were in flight.
Cancellation at the same wall time let D complete more verified work and higher
coverage. Pending reservations are not silently counted as settled: the report
separates settled, pending before reconciliation, late-billed, and released
milli-Sweycoins. `actual_usd` is N/A throughout; calculated USD is a price-list
equivalent.

Across all 26 offline runs: **936 calls** (909 reaching the provider), 37
cancelled tasks, **434,000 milli settled**, 7,750 milli pending before
reconciliation, 5,750 milli late-billed, 2,000 milli released, and
**$0.01633** calculated USD (complete-call subset). No paid call was made.

## Provider-aware adaptive/critical-path analysis (offline)

This analysis lives in a new module and does **not** modify the frozen C/D
baseline:

- `microtask_swarm/adaptive_benchmark.py`
- `config/adaptive-benchmark.json`
- `tests/test_adaptive_benchmark.py`
- `reports/adaptive-benchmark.json` / `.md`

The frozen baseline files are hashed into the new report and a test fails if
they drift.

### SWE dispatch boundary

SWE `swe.microtasks` lowers each topological layer to one ordinary `map` node;
the next layer starts only after the whole previous layer completes
(`docs/specs/swe-microtasks.md`, `python/swe/microtasks.py`). SWE dispatch has
no per-task event-ready start. The D and P schedulers below are host-level
experiment code, not a change to SWE dispatch or to t1bm's PR 13.

### Strategies

- **C** — frozen layered barrier baseline.
- **D** — frozen event-ready queue baseline.
- **P** — provider-aware P: event-ready queue, critical-path priority, and
  adaptive worker concurrency capped at the provider rate limit. At provider
  limit 8 it starts at 4 and increases 4→8 on low queue wait; at limits 1/3 it
  starts at the cap.

### Main results (same trace, 50 microtasks)

| Provider limit | C wall | D wall | P wall | P provider util | P adaptive final | P first verified | Quality (all) |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| 1 | 15.52 s | 15.52 s | 15.53 s | 0.998 | 1 | 830 ms | 0.90 / 0.80 / 0.20 |
| 3 | 5.49 s | 5.77 s | **5.18 s** | 0.997 | 3 | 129 ms | 0.90 / 0.80 / 0.20 |
| 8 | 3.69 s | 3.65 s | **3.53 s** | 0.548 | 8 | 128 ms | 0.90 / 0.80 / 0.20 |

Quality is accuracy / extraction coverage / false-support rate; it is identical
across C/D/P because they use the same fixtures, verifier, model, and budget.
P wins makespan at provider limits 3 and 8. At limit 1 it ties, but
critical-path priority delays the first verified result (830 ms vs 109 ms).
The P advantage is therefore a makespan/throughput trade, not a free
first-result win.

### Stragglers and quality-gated stop

| Scenario | Wall | Calls | Cancelled | Unscheduled | Accuracy | Coverage | Pending milli | Late milli | Released milli |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| heavy straggler C | 11.57 s | 50 | 0 | 0 | 0.90 | 0.80 | 0 | 0 | 0 |
| heavy straggler D | 11.62 s | 50 | 0 | 0 | 0.90 | 0.80 | 0 | 0 | 0 |
| heavy straggler P | **10.82 s** | 50 | 0 | 0 | 0.90 | 0.80 | 0 | 0 | 0 |
| quality-gated C | 5.49 s | 50 | 0 | 0 | 0.90 | 0.80 | 0 | 0 | 0 |
| quality-gated D | 4.37 s | 49 | 5 | 1 | 0.78 | 0.80 | 750 | 750 | 0 |
| quality-gated P | 5.07 s | 47 | 2 | 3 | 0.80 | 0.80 | 500 | 250 | 250 |

The quality gate is real, not a verified-count proxy: stop requires at least
5 completed items plus measured accuracy ≥ 0.80, extraction coverage ≥ 0.80,
and false-support rate ≤ 0.20 on the completed subset. The gate values at stop
were C 0.90/0.80/0.20 with all 10 items (the layer barrier leaves no room to
stop earlier), D 0.88/0.80/0.20 after 5 items, and P 0.92/0.80/0.20 after 5
items. P's completed-item quality at stop is highest, but its wall time is
higher than D's because P caps workers at the provider limit while D queues
more tasks behind the provider semaphore.

Count-gated stop remains available as a control: P stopped 25 verified with 27
calls and only 2 cancellations, but wall 3.86 s versus D 2.61 s. Fewer wasted
calls does not automatically mean lower latency.

Totals over all 63 runs: **3,095 calls**, **1,240,750 milli settled**, 3,500
milli pending before reconciliation, 3,000 milli late-billed, 500 milli
released, **$0.02464** calculated USD. `actual_usd` is N/A. This frozen
analysis keeps the local pilot meter so its recorded report stays reproducible.
The separate `runtime-budget-policy` integration path uses the merged
`swe.budget.RuntimeBudget`; see “Runtime budget integration” below. No paid
call was made.

Run:

```sh
PYTHONPATH=. python3 -m microtask_swarm adaptive \
  --report reports/adaptive-benchmark.json \
  --markdown reports/adaptive-benchmark.md
PYTHONPATH=. python3 -m unittest tests.test_adaptive_benchmark -v
```

## Gold-free production early-stop comparison (offline)

The production-stop comparison is a separate module on top of the frozen
baselines:

- `microtask_swarm/production_stop.py`
- `config/production-stop.json`
- `tests/test_production_stop.py`
- `reports/production-stop.json` / `.md`

The stop decision uses only observable signals: verified exact-quote evidence,
coverage of the 50 typed needs, abstention rate, a deterministic verifier
confidence, invalid outputs, and completed-item count. Gold is read only after
the run for `posthoc_stop_quality` and the normal run score.

Conditions: `full`, `count_25`, `coverage`, `confidence`, `hybrid`, and the
stricter proposed `strict` gate. The stress case uses a deterministic wrapper
that abstains on selected extraction tasks and returns `neutral` for selected
entailment tasks; it still uses the normal D/P scheduler, verifier, trace, and
budget.

### Normal trace (provider limit 3, concurrency 8)

| Scheduler | Condition | Stop | Wall | Calls | Items | Need cov. | Evid. cov. | Abstain | Conf. | Post-hoc acc./cov./false | Pending/late |
| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- |
| D | full | — | 5.76 s | 50 | 10 | 1.00 | 1.00 | 0.00 | 0.957 | 0.90 / 0.80 / 0.20 | 0/0 |
| D | count_25 | early | 2.60 s | 32 | 0 | 0.50 | 0.90 | 0.00 | 0.921 | None | 1250/1250 |
| D | coverage | production | 4.35 s | 49 | 5 | 0.88 | 1.00 | 0.00 | 0.951 | 0.92 / 0.80 / 0.20 | 750/750 |
| D | hybrid | production | 4.35 s | 49 | 6 | 0.90 | 1.00 | 0.00 | 0.952 | 0.90 / 0.833 / 0.167 | 750/750 |
| D | strict | production | 4.42 s | 49 | 6 | 0.90 | 1.00 | 0.00 | 0.952 | 0.90 / 0.833 / 0.167 | 750/750 |
| P | full | — | 5.20 s | 50 | 10 | 1.00 | 1.00 | 0.00 | 0.957 | 0.90 / 0.80 / 0.20 | 0/0 |
| P | count_25 | early | 3.84 s | 27 | 1 | 0.50 | 1.00 | 0.00 | 0.914 | 0.80 / 0.00 / 1.00 | 500/250 |
| P | coverage | production | 5.06 s | 47 | 5 | 0.90 | 1.00 | 0.00 | 0.952 | 0.92 / 0.80 / 0.20 | 500/250 |
| P | hybrid | production | 5.14 s | 48 | 6 | 0.92 | 1.00 | 0.00 | 0.953 | 0.90 / 0.833 / 0.167 | 500/0 |
| P | strict | production | 5.08 s | 48 | 6 | 0.92 | 1.00 | 0.00 | 0.953 | 0.90 / 0.833 / 0.167 | 500/0 |

Coverage and confidence stop at the same point on this trace because the
confidence heuristic is high (~0.95); confidence adds no discrimination here.
The count control is the cautionary case: D stopped before any complete item
and P stopped on one complete item with zero extraction coverage.

### Stress case (P scheduler, deterministic abstention/neutral)

| Condition | Stop | Wall | Calls | Items | Need cov. | Evid. cov. | Abstain | Conf. | Post-hoc acc./cov./false | Pending/late |
| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- |
| stress_full | — | 5.17 s | 50 | 10 | 0.96 | 0.80 | 0.040 | 0.917 | 0.88 / 0.70 / 0.125 | 0/0 |
| stress_count_25 | early | 3.84 s | 27 | 1 | 0.46 | 0.80 | 0.080 | 0.850 | 0.80 / 0.00 / 0.00 | 500/250 |
| stress_coverage | production | 5.07 s | 47 | 5 | 0.86 | 0.80 | 0.044 | 0.908 | 0.88 / 0.60 / 0.00 | 500/250 |
| stress_confidence | production | 5.06 s | 47 | 5 | 0.86 | 0.80 | 0.044 | 0.908 | 0.88 / 0.60 / 0.00 | 500/250 |
| stress_hybrid | production | 5.08 s | 48 | 6 | 0.88 | 0.80 | 0.043 | 0.910 | 0.867 / 0.667 / 0.00 | 500/0 |
| stress_strict | production | 5.08 s | 48 | 6 | 0.88 | 0.80 | 0.043 | 0.910 | 0.867 / 0.667 / 0.00 | 500/0 |

The gold-free policies tolerate up to 5% abstention in the strict gate and
still stop at six complete items. The count control again stops before a
meaningful evidence set.

### False support and confidence calibration

Full-run extraction false support:

- normal D/P full: 10 emitted, 8 matched gold, **2 unsupported**, 0 unverified,
  false-support rate **0.20**. Examples: `bio-01:extraction` at confidence 0.70
  and `finance-01:extraction` at confidence 0.8125;
- stress full: 8 emitted, 7 matched, **1 unsupported**, 0 unverified, rate
  **0.125**.

These are exact passage substrings, so runtime verification accepts them; they
are still not the gold span. **False support cannot be detected without gold**;
the runtime gate can only enforce exactness and coverage, not correctness of
the chosen span.

Confidence calibration over the full runs (descriptive, not calibrated):

| Confidence bin | Count | Accuracy | False-support rate |
| --- | ---: | ---: | ---: |
| [0.0, 0.5) | 2 | 0.000 | 0.000 |
| [0.5, 0.8) | 18 | 0.778 | 0.167 |
| [0.8, 0.95) | 11 | 0.818 | 0.182 |
| [0.95, 1.0] | 119 | 0.933 | 0.000 |

The relationship is not monotonic in the middle bins, and most samples sit in
the top bin. Treat confidence as a weak secondary signal, not a calibrated
probability or a standalone stop criterion.

### Proposed verifier gate `verifier_gate_v1`

- at least 6 complete items;
- need coverage ≥ 0.80;
- verified evidence coverage ≥ 0.80;
- abstention ≤ 0.05;
- average verifier confidence ≥ 0.80;
- zero invalid outputs;
- every emitted extraction quote is an exact passage substring;
- at least one verified extract per complete item;
- no unverified extraction remains in the completed set.

The gate deliberately does **not** claim to detect false support. A verified
but non-gold quote is invisible at runtime and is measured only post-hoc.

The implemented `strict` policy enforces the numeric thresholds, including
`max_invalid_outputs=0`. The three per-item hard rules are the proposed
production contract; enforcing them directly needs a per-item evidence signal
rather than the aggregate coverage ratio.

Totals across all 18 runs: **812 calls**, **332,000 milli settled**, 9,250
milli pending before reconciliation, 5,750 milli late-billed, 3,500 milli
released, **$0.00630** calculated USD. `actual_usd` is N/A. This frozen
analysis keeps the local pilot meter so its recorded report stays reproducible.
The separate `runtime-budget-policy` integration path uses the merged
`swe.budget.RuntimeBudget`; see “Runtime budget integration” below.

Run:

```sh
PYTHONPATH=. python3 -m microtask_swarm production-stop \
  --report reports/production-stop.json \
  --markdown reports/production-stop.md
PYTHONPATH=. python3 -m unittest tests.test_production_stop -v
```

## Adaptive budget/routing policy comparison (offline)

This is a policy simulator, not a production scheduler and not a ledger. It
reuses the frozen fixtures, offline heuristic, deterministic latency trace,
verifier confidence, and the local pilot Sweycoin meter.

- `microtask_swarm/adaptive_budget.py`
- `config/adaptive-budget.json`
- `tests/test_adaptive_budget.py`
- `reports/adaptive-budget.json` / `.md`

Training items: `bio-01`, `bio-02`, `law-01`, `law-02`, `finance-01`.
Held-out items: `finance-02`, `medicine-01`, `medicine-02`, `engineering-01`,
`engineering-02`. Training gold fits per-contract/model statistics; held-out
gold is used only after the run. The policy receives task input, training
statistics, and a gold-free `OutcomeView` (verification, abstention,
confidence, latency, tariff) — never held-out correctness.

Budget cap: **15,000 milli-Sweycoin** for 25 held-out tasks. Models and tariffs:
GPT-OSS 1.2×, Nemotron 0.8×, MiMo 1.0×, DeepSeek 1.5× over the contract
tariffs.

| Policy | Stop | Tasks | Accuracy | Coverage | False support | Need cov. | Latency | Calls | Spec. | Settled | Pending | Late | Released | Denied | Overspend | Calc USD |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| equal_split | no | 20 | 0.72 | 0.00 | 0.00 | 0.80 | 5.77 s | 20 | 0 | 4,000 | 0 | 0 | 0 | 5 | 0 | $0.000117 |
| greedy | yes | 23 | 0.84 | 1.00 | 0.00 | 0.92 | 6.95 s | 23 | 0 | 7,600 | 0 | 0 | 0 | 0 | 0 | $0.000187 |
| fixed_routing | yes | 23 | 0.84 | 1.00 | 0.00 | 0.92 | 6.56 s | 23 | 0 | 8,600 | 0 | 0 | 0 | 0 | 0 | $0.000209 |
| adaptive | yes | 23 | 0.84 | 1.00 | 0.00 | 0.92 | 6.56 s | 28 | 5 | 9,400 | 4,000 | 800 | 3,200 | 0 | 0 | N/A |
| adaptive_no_spec | yes | 23 | 0.84 | 1.00 | 0.00 | 0.92 | 6.95 s | 23 | 0 | 7,600 | 0 | 0 | 0 | 0 | 0 | $0.000187 |

Readings:

- **Equal split** gives every task 600 milli. The cheapest extraction model
  costs 800, so all five extraction needs are denied: coverage 0.00. The
  per-task split cannot move unused classification budget to expensive
  extraction.
- **Greedy** and **adaptive without speculation** both choose Nemotron for the
  affordable routes: same quality, calls, and 7,600 settled.
- **Fixed routing** (Nemotron classifications, MiMo extraction) matches quality
  and coverage but is faster (6.56 s vs 6.95 s) and costs 1,000 milli more.
- **Adaptive with speculation** races a MiMo extraction secondary against the
  Nemotron primary. It matches fixed-routing quality and latency but uses 28
  calls, settles 9,400, leaves 4,000 milli pending before reconciliation, and
  books 800 late-billed / 3,200 released. It produces **no quality gain** on
  this training split: training extraction accuracy is at most 0.60 for every
  model (GPT-OSS 0.40, Nemotron 0.60, MiMo 0.60, DeepSeek 0.00 with 1.00
  abstention), so the expected marginal utility of escalation is not positive.
- The gold-free stop gate stopped greedy, fixed, adaptive, and adaptive_no_spec
  after 23 tasks (coverage 1.00, need coverage 0.92). Equal split did not reach
  the gate because extraction was denied.
- **No budget overrun occurred.** All policies fail closed on reservation; only
  equal split recorded denied tasks.

Totals across the five policies: **117 calls**, **37,200 milli settled**,
4,000 milli pending before reconciliation, 800 milli late-billed, 3,200 milli
released, 5 denied tasks, 0 overspend, **$0.000698** calculated USD (complete
subset $0.000907). `actual_usd` is N/A.

Policy interface for REA/t1bm:

```python
class BudgetRoutingPolicy(Protocol):
    name: str
    def begin(self, tasks, *, cap_milli, training, models) -> None: ...
    def choose(self, task, state) -> RoutingDecision: ...
    def observe(self, task, decision, outcome, state) -> None: ...
```

`RoutingDecision` contains one or two `ModelPlan`s, whether the plan is
speculative, and a reason. `OutcomeView` deliberately omits held-out
correctness. The simulator owns no production scheduler and no second ledger.

Run:

```sh
PYTHONPATH=. python3 -m microtask_swarm adaptive-budget \
  --report reports/adaptive-budget.json \
  --markdown reports/adaptive-budget.md
PYTHONPATH=. python3 -m unittest tests.test_adaptive_budget -v
```

## Reuse and intentional gaps

| Concern | Reused | Gap |
| --- | --- | --- |
| Microtask DAG validation and topological layers | `swe.microtasks.compile_microtasks` on the merged ready map; `tests/test_integration_microtasks.py` runs a real `MicrotaskBatch` through `WorkflowRunner` with a `RuntimeBudget` when both native wheels are built (`make python`) | The frozen pilot still uses its equivalent bounded asyncio dispatcher so its recorded reports stay reproducible; `experiments/agent-contract/tests/test_preflight.py` exercises the same boundary |
| Invocation/cancellation semantics | Contract and event shape follow the existing runtime/journal vocabulary | The experiment does not claim journal replay or exactly-once external effects |
| Sweycoin ledger | `crates/swe-budget` is exposed as `swe_budget.Ledger`; the `runtime-budget-policy` integration path composes the real `swe.budget.RuntimeBudget` with `NativeBudgetLedger` and `CarriedMilliPolicy` | The frozen pilot keeps `microtask_swarm/budget.py` for reproducibility; the integration path does not use it |
| Provider routing | Typed operation menu mirrors a closed decision choice set | The pilot does not add SWD nodes or a second workflow format |
| Exact quote evidence | Existing evidence discipline: exact, fail-closed | No fuzzy or semantic fallback |

No domain logic was added to `python/swe` or `crates/`.

### Runtime budget integration

Status: `INTEGRATED_ON_MAIN` (2026-11-25).

PR #11 merged the shared `swe.budget.RuntimeBudget` and its Rust
`swe_budget.Ledger` binding. PR #15 added the integration path in
`microtask_swarm/runtime_budget_policy.py`:

- `CarriedMilliPolicy` implements `swe.budget.BudgetPolicy` and returns the
  milli amount the routing policy already chose; it does not price tokens.
- `RuntimeBudgetBackend` composes `NativeBudgetLedger` with that policy and
  owns no ledger code. It reports the real `balance()` and `events()` and
  replays them through `NativeBudgetLedger.replay`.
- The integration path uses reserve → settle, `mark_pending` on cancel/timeout,
  and host reconciliation; a denied reservation fails closed.
- The frozen baseline (`budget.py`, `executor.py`, `async_benchmark.py`,
  `adaptive_budget.py`, `production_stop.py`) intentionally still uses the
  local pilot meter so the committed reports remain reproducible. The
  integration path does not use it; `tests/test_runtime_budget_policy.py`
  asserts that.
- The agent-contract preflight separately exercises `RuntimeBudget` through
  `WorkflowResources.budget` on the real `WorkflowRunner`.

The historical migration plan is
[`docs/budget-migration-plan.md`](docs/budget-migration-plan.md); it is marked
`INTEGRATED_ON_MAIN`. The `tests/test_budget_migration_contract.py` probe runs
when `swe.budget` is importable.

Build the native wheels once from the repo root (`make python`), then run the
integration path offline from the experiment directory. No provider call is
made; the first command writes the ignored JSON report and the committed
markdown summary.

```sh
cd experiments/microtask-swarm
make PYTHON=../../python/.venv/bin/python runtime-budget-policy
PYTHONPATH=.:../../python ../../python/.venv/bin/python -m unittest tests.test_runtime_budget_policy -v
```

## Live gate and cost cap

The live runner is implemented but hard-gated. It refuses when:

- `--approve-usd-cap` is missing, zero, negative, or non-finite;
- `OLLAMA_API_KEY` is absent and `--use-local-daemon` was not supplied;
- any selected model is unavailable or a pinned tag/digest/capability differs;
- the planned matrix estimate exceeds the approved cap.

Before dispatch it performs only free metadata reads (`/api/tags`, `/api/show`).
It then caps effective concurrency at the assumed plan limit (3 for pay-as-you-go
or Pro; 10 for Max/Team) and reports requested vs effective concurrency.

Conservative estimate for the full live matrix (4 available models, 10 items,
50 microtasks, 6 concurrency levels, 10% retries, 1,000 input and 500 generated
output tokens per call including reasoning, 50% cache-read, peak prices):

- planned mixed matrix: **$1.08**;
- extreme if every call used the DeepSeek baseline: **$2.19**;
- Sweycoins: **1,551,000 milli (1,551 Sweycoins)** with retries and the
  deterministic verifier included.

Recommended cap: **$3.00**. This is an estimate, not an approval. No paid call is
made until the user explicitly approves a cap.

The exact live command is:

```sh
OLLAMA_API_KEY=... PYTHONPATH=. python3 -m microtask_swarm live \
  --approve-usd-cap 3.00 \
  --report reports/live.json
```

Add `--use-local-daemon` only if the signed-in local daemon exposes every
selected exact tag; it currently does not expose `gpt-oss:20b`,
`nemotron-3-super`, or `mimo-v2.6-flash`.

## Exact blockers

1. **No approved USD cap.** The live runner refuses; nothing paid has run.
2. **No direct cloud credential.** `OLLAMA_API_KEY` is absent. The local daemon
   is reachable and has 12 models, but not the three non-baseline candidate tags.
3. **Nemotron 3 Nano is retired.** `nemotron-3-nano:30b-cloud` returns HTTP 410
   and is absent from `/api/tags`; the Nano arm cannot run.
4. **GPT-OSS 20B cannot disable reasoning.** Only `low/medium/high` are
   supported, so it is a low-reasoning arm, not reasoning-off. It needs a
   128-token output headroom; without it, `think: "low"` consumed the entire
   classification budget and returned empty content.
5. **Resolved: Python binding for `swe-budget`.** PR #11 exposes
   `swe_budget.Ledger`; the integration path uses it. The local meter remains
   only in the frozen baseline.
6. **Resolved: Rust toolchain / `swe_native`.** `make python` builds both native
   wheels, and `tests/test_integration_microtasks.py` runs a real
   `MicrotaskBatch` through `WorkflowRunner` with a `RuntimeBudget` when they
   are installed.
7. **DeepSeek `:1017` is a separate tag.** The baseline is pinned to
   `deepseek-v4.1-flash` digest `97308b916fc3`. `deepseek-v4.1-flash:1017` has
   digest `7f8e880bc207`; choosing it requires an explicit pin change.

## Files

| Path | Purpose |
| --- | --- |
| `config/experiment.json` | Pinned protocol, models, prices, capabilities, modes, limits, estimate |
| `fixtures/gold.json` | Frozen domain-diverse gold fixtures, 50 microtasks |
| `microtask_swarm/contracts.py` | Typed contracts, strict validation, exact quote verification, prompt rendering |
| `microtask_swarm/fixtures.py` | Fixture loading, leakage checks, task construction |
| `microtask_swarm/dag.py` | `swe.microtasks` reuse with identical fallback |
| `microtask_swarm/executor.py` | Bounded dispatcher, retries, competition, early cancellation |
| `microtask_swarm/offline.py` | Deterministic lexical personas for the offline pilot |
| `microtask_swarm/scoring.py` | Quality, coverage, false support |
| `microtask_swarm/cost.py` | Calculated vs actual USD, tokens, cache, retries, verifier |
| `microtask_swarm/budget.py` | Frozen-baseline Sweycoin meter; not used by the integration path |
| `microtask_swarm/runtime_budget_policy.py` | Integration path on `swe.budget.RuntimeBudget` and `swe_budget.Ledger` |
| `microtask_swarm/live.py` | Gated Ollama Cloud client and free preflight |
| `microtask_swarm/runner.py` | Mode/model/concurrency matrix and report |
| `microtask_swarm/cli.py` | `offline`, `async`, `adaptive`, `production-stop`, `adaptive-budget`, `runtime-budget-policy`, `estimate`, `verify-fixtures`, `live` |
| `reports/offline-pilot.json` | Generated offline evidence, labelled `offline_heuristic` |

## Hy4 boundary

The Hy4 worktree is read-only. This experiment does not modify it, import it,
or reuse its Research Gym fixtures. Hy4 compares policy models on a research
Gym; this experiment compares typed microtask execution modes on a separate
microtask gold set. The only shared ideas are generic: frozen fixtures, exact
quote evidence, separate calculated/actual USD, and a pre-dispatch cost gate.
