# Hy4 research-policy benchmark

Offline comparison harness for three policy models on the frozen Research Gym.
The questions, corpus, tool menu, Sweycoin allocation, and USD cap are the
existing Gym values. SWE core is not modified.

Status: `OFFLINE_PREPARED_NOT_LIVE`. This directory has no provider client.
`refuse_live_dispatch` is the only live entry point, and it always refuses.

## Frozen inputs

| Input | Pin |
| --- | --- |
| Questions | Research `benchmarks/gym/questions.json` at `4a58d5a`, sha256 `142d8d8c…` |
| Corpus | Research `benchmarks/corpus`, Gym directory sha256 `ff85a396…` |
| Tools | `retrieve` (1 000 milli), `read_passage` (500 milli). No web search |
| Evaluator | Live pin `jev-1.13.0`. Offline method `exact-quote-v1` |
| Sweycoins | 50 000 milli per question, 250 000 milli per arm. Lexical baseline spent 29 250 milli |
| USD cap | 25.0, the existing Research experiment cap |

The same budget object applies to `hy4`, `hy4-flash`, and `deepseek-flash`.

## What a trace is scored on

- Decomposition: expected Gym quotes named by the first `decompose` step.
- Next action: `select_next` must use a frozen tool and, while an expected quote is still unread, name that open gap.
- Stopping: the `stop` conclusion must match the Gym conclusion, and `answer_complete` must not fire while expected evidence is unread or the conclusion is not `ANSWERED`.
- Verified evidence: a `read_passage` quote that occurs verbatim in the cited corpus document. Coverage and precision use the Gym `(source_id, quote)` pairs.
- Unsupported claims: decompose targets or stop claims that are not verbatim corpus text, and stop claims that were not read.
- Cost, latency, and tool calls: Sweycoin tariffs from the protocol, summed step latency, and frozen-tool calls.

Recorded fixtures are scorer tests. They are not model results.

## Official model pins, checked 2026-11-23

| Arm | API model | Approval price per 1M tokens |
| --- | --- | --- |
| Hy4 | `hy4` | input $0.834, output $2.501, cache $0.042 |
| Hy4 Flash | `hy4-flash` | input $0.070, output $0.281, cache $0.003 |
| DeepSeek Flash | `deepseek-flash` (DeepSeek-V4.1-Flash-1017) | peak cache-miss input $0.30, output $1.20 |

Hy4 prices are Tencent TokenHub list prices
(`https://www.tencentcloud.com/document/product/1300/78937`). A temporary 50%
discount runs through 31 December 2026 for Hy4 and through 30 November 2026
12:00 Beijing time for Hy4 Flash. The approval bound ignores that discount.
DeepSeek prices are the official peak cache-miss rates
(`https://api-docs.deepseek.com/quick_start/pricing/`). JEV stays
`jev-1.13.0` at $0.042 per 1M input tokens and $0 output
(`https://docs.typesafe.ai/models`).

`GET https://api.deepseek.com/models` returned `deepseek-flash` and
`deepseek-v4-pro`. `GET https://api.typesafe.ai/v1/models` returned the aliases
`jev-latest` and `jev-preview`; no System One request was sent. TokenHub
`GET /v1/models` without a key returned 401. No `TOKENHUB_API_KEY` is part of
this protocol, so Hy4 account availability is not confirmed.

## Cost estimate, not a measurement

Assumptions for one matrix of 5 questions and 3 arms: 8 policy calls per
question, 8 000 input tokens, cache miss, peak DeepSeek rates, list Hy rates,
and 4 shared JEV reads of 32 000 input tokens per question. Low output is
4 000 tokens. The approval output bound is 16 000 tokens.

| Bound | USD |
| --- | ---: |
| Low output | 1.04928 |
| Approval (high output, list/peak) | 2.96064 |
| High output with the temporary Hy discount | 1.92576 |
| JEV portion | 0.02688 |

Sweycoin worst case inside those assumptions is 11 750 milli per question and
58 750 milli per arm, below the 50 000 milli question allocation and the
250 000 milli arm allocation. The existing USD cap is 25. The estimate fitting
under that cap is not approval to dispatch.

## Run the offline tests

```sh
python3 -m pytest experiments/hy4-research-policy/tests
```

## Blockers before any paid call

1. Explicit approval of the approval bound, or a revised bound if reasoning
   tokens will exceed 16 000 output tokens per call.
2. A TokenHub credential for `hy4` and `hy4-flash`. DeepSeek and the configured
   JEV key can be present in the environment; this package does not read them.
3. A live runner still has to be written. It must keep domain policy out of
   `python/swe` and `crates/`, use the frozen tool menu, and keep the fixed
   `jev-1.13.0` evaluator independent of the policy arm.
