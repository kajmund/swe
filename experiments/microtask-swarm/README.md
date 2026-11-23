# SWE microtask-swarm pilot

Status: `OFFLINE_PREPARED_NOT_LIVE`. This experiment is isolated on
`experiment/microtask-swarm` from `origin/main` (`eff1c7e`). It does not modify
SWE core, the frozen contracts, the Hy4 worktree, or `main`.

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

## Reuse and intentional gaps

| Concern | Reused | Gap |
| --- | --- | --- |
| Microtask DAG validation and topological layers | `swe.microtasks.compile_microtasks` when importable | `swe_native` is not built (no Rust toolchain here), so `MicrotaskBatch.run` was not executed; the pilot uses an equivalent bounded asyncio dispatcher and verifies `peak_in_flight <= concurrency` |
| Invocation/cancellation semantics | Contract and event shape follow the existing runtime/journal vocabulary | The experiment does not claim journal replay or exactly-once external effects |
| Sweycoin ledger | Integer milli, reserve/settle/release, idempotency keys, replay | `crates/swe-budget` is Rust-only and has no Python binding; the host meter is a pilot adapter, not a second core ledger |
| Provider routing | Typed operation menu mirrors a closed decision choice set | The pilot does not add SWD nodes or a second workflow format |
| Exact quote evidence | Existing evidence discipline: exact, fail-closed | No fuzzy or semantic fallback |

No domain logic was added to `python/swe` or `crates/`.

### Budget migration

The local `SweycoinMeter` is temporary. The migration to the shared Rust/PyO3
runtime budget in [PR #11](https://github.com/kajmund/swe/pull/11) is planned
read-only in [`docs/budget-migration-plan.md`](docs/budget-migration-plan.md).
It is **not integrated**: PR #11 is open, has no independent approval, and its
packaging check is currently red. The plan deletes the local meter rather than
wrapping it, and defines the retry/cancel/replay compatibility decisions.
`tests/test_budget_migration_contract.py` is a skipped contract probe until the
landed `swe.budget` module is importable.

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
5. **No Python binding for `swe-budget`.** True core-ledger reuse needs a PyO3
   binding or a host adapter owned by the core package; the pilot meter is
   explicitly a host accounting adapter.
6. **No Rust toolchain / `swe_native`.** The DAG compiler is reused, but the
   full `MicrotaskBatch.run` path and journal replay were not executed. CI with
   Rust must run that integration before live dispatch.
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
| `microtask_swarm/budget.py` | Host Sweycoin meter with core-ledger integer semantics |
| `microtask_swarm/live.py` | Gated Ollama Cloud client and free preflight |
| `microtask_swarm/runner.py` | Mode/model/concurrency matrix and report |
| `microtask_swarm/cli.py` | `offline`, `estimate`, `verify-fixtures`, `live` |
| `reports/offline-pilot.json` | Generated offline evidence, labelled `offline_heuristic` |

## Hy4 boundary

The Hy4 worktree is read-only. This experiment does not modify it, import it,
or reuse its Research Gym fixtures. Hy4 compares policy models on a research
Gym; this experiment compares typed microtask execution modes on a separate
microtask gold set. The only shared ideas are generic: frozen fixtures, exact
quote evidence, separate calculated/actual USD, and a pre-dispatch cost gate.
