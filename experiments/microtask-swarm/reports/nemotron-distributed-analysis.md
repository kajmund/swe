# Nemotron 3 Super / distributed live analysis

**Source:** `reports/live-pilot.json` (compact live report, 2026-11-23).
**No new paid calls were made for this analysis.** No `--include-rows` / `--full`
live report was persisted, so item-level attribution is unavailable; the
finest available granularity is contract × 10 fixtures. This limitation is
carried through every conclusion below.

## Data provenance

- Model: `nemotron-3-super`, digest `da11955bb451`, capabilities
  `completion/thinking/tools`, context 262,144.
- Thinking: `false` (reasoning off), verified via `POST /api/show`.
- Provider: Ollama Cloud, `https://ollama.com/api/chat`.
- Prices used by `calculated_usd`: $0.015 input, $0.015 cached input, $0.60
  output per 1M tokens (published peak row).
- Fixture: the same 10 items × 5 contracts = 50 microtasks for every mode,
  SHA-256 `be95745f8bed6981536c8c1592304be8afd02dfc4d78776eb4495f3138f0ad06`.
- Live concurrency: 1, 2, 3 (Ollama plan limit is 3). One run per concurrency.
- `actual_usd` is `N/A`; Ollama does not report a per-call charge.

## Headline: best Nemotron distributed vs best Nemotron single

| Metric | Distributed (concurrency 2) | Single call (concurrency 3) |
| --- | ---: | ---: |
| Accuracy | **46/50 = 0.92** | 34/50 = 0.68 |
| Wilson 95% CI (naive) | 0.812–0.968 | 0.542–0.792 |
| Extraction coverage | **9/10 = 0.90** | 7/10 = 0.70 |
| Extraction false support | **1/10 = 0.10** | 3/10 = 0.30 |
| p50 call latency | 7.55 s | 1.70 s |
| p95 call latency | 14.93 s | 3.05 s |
| Wall time, 50 scored subtasks | 15.49 s | 3.05 s |
| Wall time per item | 1.55 s | 0.305 s |
| Throughput | 3.23 subtasks/s | 16.41 subtasks/s |
| Calls | 50 | 10 |
| Retries | 0 | 0 |
| Calculated USD | $0.00055791 | $0.00065292 |
| Sweycoins | 20,000 milli | 12,500 milli |

The distributed arm gains **+0.24 accuracy** and **+0.20 coverage**, and cuts
false support by 0.20. The naive unpaired 95% interval for the accuracy
difference is **[+0.09, +0.39]**. With a crude design-effect adjustment of 5
(5 microtasks per item), the interval becomes **[-0.09, +0.57]** and includes
zero. Without per-item rows, a paired McNemar test or item bootstrap cannot be
computed. Treat the gain as suggestive, not conclusive.

## Per-contract analysis (10 microtasks each)

Distributed = Nemotron concurrency 2. Single = Nemotron concurrency 3 (the
faster of the two 0.68 single runs).

| Contract | Distributed correct | Distributed 95% CI | Single correct | Single 95% CI | Dominant error pattern |
| --- | ---: | ---: | ---: | ---: | --- |
| Relevance | 10/10 | 0.722–1.000 | 10/10 | 0.722–1.000 | None |
| Entailment | 9/10 | 0.596–0.982 | 7/10 | 0.397–0.892 | 1 vs 3 wrong labels |
| Atomicity | 9/10 | 0.596–0.982 | 9/10 | 0.596–0.982 | 1 wrong in both |
| Extraction | 9/10 | 0.596–0.982 | 7/10 | 0.397–0.892 | 1 vs 3 unsupported gold matches |
| Operation selection | 9/10 | 0.596–0.982 | 1/10 | 0.018–0.404 | The dominant single-call failure |
| **Overall** | **46/50** | **0.812–0.968** | **34/50** | **0.542–0.792** | |

Errors in the distributed arm: one each in atomicity, entailment, extraction,
and operation selection; none in relevance. The single arm's 16 errors are
concentrated in operation selection (9), entailment (3), and extraction (3),
plus one atomicity error. Which fixture items failed cannot be recovered from
the compact report.

The decomposition effect is consistent across models: for the best distributed
runs, `operation_selection` accuracy is Nemotron 0.9, MiMo 0.8, DeepSeek 0.7,
GPT-OSS 0.6; the corresponding single-call values are 0.1, 0.4, 0.1, 0.4.

## Coverage and false support

Extraction is scored against the exact gold quote. A verified passage substring
that is not the gold sentence is counted as **unsupported**.

| Metric | Distributed | Single |
| --- | ---: | ---: |
| Emitted quotes | 10 | 10 |
| Exact substring (verified) | 10 | 10 |
| Matched gold quotes | 9 | 7 |
| Unsupported (verified, not gold) | 1 | 3 |
| Unverified (not a substring) | 0 | 0 |
| Coverage | 0.90 | 0.70 |
| Quote precision | 0.90 | 0.70 |
| False-support rate | 0.10 (95% CI 0.018–0.404) | 0.30 (95% CI 0.108–0.603) |

Neither mode emitted an unverified quote: both respected the exact-substring
contract. The difference is in selecting the right sentence. The strict
gold-sentence rule can overstate false support when a different passage
sentence is semantically supportive; this is a measurement confound, not a
model failure by itself.

## Latency

| Arm | Conc. | p50 call | p95 call | Wall | Throughput | Wall/item | Calls | Retries |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Distributed | 1 | 12.33 s | 26.72 s | 27.93 s | 1.79/s | 2.79 s | 50 | 0 |
| Distributed | 2 | 7.55 s | 14.93 s | 15.49 s | 3.23/s | 1.55 s | 50 | 0 |
| Distributed | 3 | 9.59 s | 30.07 s | 33.10 s | 1.51/s | 3.31 s | 54 | 4 |
| Single | 1 | 4.05 s | 8.41 s | 8.41 s | 5.95/s | 0.84 s | 10 | 0 |
| Single | 2 | 2.20 s | 4.42 s | 4.42 s | 11.30/s | 0.44 s | 10 | 0 |
| Single | 3 | 1.70 s | 3.05 s | 3.05 s | 16.41/s | 0.30 s | 10 | 0 |

Concurrency 3 is not better than 2 for distributed Nemotron: it produced 4
retries, p95 rose from 14.9 s to 30.1 s, and wall time doubled. The concurrency
3 arm also has unknown token usage on the four failed attempts, so its all-in
calculated USD is `N/A`; the complete-usage subset is $0.00055911. The provider
plan's 3-request limit and queueing are plausible causes, but one run per level
cannot separate concurrency from run noise.

Per-call latency is not directly comparable across modes: one single call
produces all five answers, while one distributed call produces one answer. The
wall-per-item column is the fairer end-to-end comparison, and it still favors
single call on speed (0.305 s/item vs 1.55 s/item at the best quality points).

## Cost

| Component | Distributed (conc. 2) | Single (conc. 3) |
| --- | ---: | ---: |
| Input tokens | 8,874 | 4,968 |
| Cached input tokens | 7,504 | 4,304 |
| Uncached input tokens | 1,370 | 664 |
| Output tokens | 708 | 964 |
| Calculated input USD | $0.00002055 | $0.00000996 |
| Calculated cache USD | $0.00011256 | $0.00006456 |
| Calculated output USD | $0.00042480 | $0.00057840 |
| **Calculated total** | **$0.00055791** | **$0.00065292** |
| Actual USD | N/A | N/A |
| Sweycoins | 20,000 milli | 12,500 milli |
| USD per correct microtask | $1.21 × 10⁻⁵ | $1.92 × 10⁻⁵ |
| USD per verified gold quote | $6.20 × 10⁻⁵ | $9.33 × 10⁻⁵ |

Distributed uses more input tokens (more calls) but fewer output tokens
(shorter, focused answers). At Nemotron's output-heavy price, that makes the
distributed arm about **14.6% cheaper in calculated USD** and **37% cheaper per
correct microtask**, while costing **60% more Sweycoins** (host tariff) and
roughly 5× the wall time per item. The output-token reduction is the main cost
driver; it is not a general result about all models.

`actual_usd` is `N/A` because Ollama does not return a per-call charge. The
calculated values are price-list equivalents, not bills. Cache is a real
confounder for cost: 7,504 of 8,874 distributed input tokens were cached, and
4,304 of 4,968 single tokens were cached.

## Same-fixture comparison with other distributed arms

Best distributed run per model on the same 50 microtasks:

| Model | Accuracy | Coverage | False support | p95 | Calculated USD | Calls | Retries |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Nemotron 3 Super | **0.92** | 0.90 | 0.10 | 14.93 s | $0.00055791 | 50 | 0 |
| MiMo V2.6 Flash | **0.92** | 0.90 | 0.10 | 22.89 s | $0.00058904 | 50 | 0 |
| DeepSeek V4.1 Flash | 0.90 | 0.90 | 0.10 | 36.53 s | $0.00134033 | 50 | 0 |
| GPT-OSS 20B | 0.86 | 0.90 | 0.10 | 46.09 s | $0.00139300 | 51 | 1 |

Nemotron and MiMo tie on accuracy, coverage, and false support. Nemotron has
the lower p95 and slightly lower calculated cost; its output price is higher
but it emitted fewer output tokens in this run. Because these are unpaired,
single observations on a small fixture, the ranking is not established.

## Uncertainty and confounders

1. **No per-item rows.** The live report is compact. The analysis is per
   contract × 10 fixtures, not per individual item. Item-level error attribution,
   paired tests, and cluster bootstrap are impossible without a new run that
   persists `--include-rows` or `--full`.
2. **Small N and clustering.** 50 microtasks come from 10 items; five tasks per
   item are correlated. The contract-level Wilson intervals (n=10) are already
   wide; the cluster-adjusted difference interval for distributed vs single
   includes zero.
3. **Single observation per configuration.** One run per concurrency. Provider
   nondeterminism is visible across earlier smoke runs even at temperature 0.
   Latency and cost have no run-to-run interval.
4. **Decomposition confound.** Distributed changes the prompt, schema, output
   length, and number of calls at once. The operation-selection gain may be due
   to a focused prompt rather than "swarm" parallelism. This experiment cannot
   separate those mechanisms.
5. **Concurrency/queueing confound.** The provider cap is 3. Concurrency 3
   showed more retries and worse latency than 2. Live 4/8/16/32 were not run;
   offline sweep numbers do not represent provider queueing.
6. **Cache confound.** Cache-read counts differ by mode and run; cache lowers
   latency and input cost but is not a quality variable. Missing cache counts
   would be recorded as an explicit assumed miss; Nemotron reported cache counts
   in these runs.
7. **Strict extraction scoring.** A verified but different passage sentence is
   counted as unsupported, which can overstate false support.
8. **Reasoning is off for Nemotron**, so the GPT-OSS low-reasoning headroom does
   not apply here. Nemotron's reasoning tokens are not separately reported;
   `think=false` is the verified control.
9. **Cost is not billing.** `actual_usd` is unavailable; calculated USD depends
   on published prices, cache accounting, and token fields. Sweycoins are host
   tariffs and are not provider dollars.
10. **Multiple comparisons.** Five contracts × two modes without correction;
    contract-level intervals are exploratory.

## Practical conclusion

On this pilot, **Nemotron distributed at concurrency 2** is the best
quality/latency/cost point measured: 0.92 accuracy, 0.90 coverage, 0.10 false
support, p95 14.9 s, and $0.00056 calculated. MiMo ties on quality but is
slower in this run. Single-call Nemotron is much faster per item but loses
0.24 accuracy, concentrated in operation selection.

The result is suggestive, not established: the cluster-adjusted uncertainty
includes no difference, and the live report lacks per-item rows. The next
approved live run should persist per-task rows and repeat each configuration
before treating the ranking as a benchmark result. No paid call is needed or
authorized for that next step yet.
