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

Hy4 is an Ollama Cloud model. The protocol does not use a Tencent endpoint or
Tencent token prices.

`GET https://ollama.com/api/tags` returned the direct API name `hy4`
(digest `1de429535565`, modified `2026-11-19T15:00:00-08:00`). The library tag
page lists one tag, `hy4:cloud`, which is the name for the Ollama CLI, app, and
local API. Direct `https://ollama.com/api/chat` calls use `hy4`. No model
weights were pulled.

| Arm | Tag | Approval price per 1M tokens |
| --- | --- | --- |
| Hy4 | API `hy4`, CLI `hy4:cloud` | input $0.834, cached input $0.042, output $2.501 |
| Hy4 Flash | not a cloud model | no Ollama price |
| DeepSeek Flash | `deepseek-flash` (DeepSeek-V4.1-Flash-1017) | peak cache-miss input $0.30, output $1.20 |

Ollama's pricing table has one Hy4 row and no Hy4 off-peak row. The published
off-peak window is outside 12:00–18:00 UTC on weekdays and all day on weekends,
and the table only gives a second row to models that list one. DeepSeek Flash
has that second row: input $0.15, cached $0.003, output $0.60. The same
DeepSeek rows appear on `https://ollama.com/pricing`. The local daemon already
has `deepseek-v4.1-flash:cloud` pointing at remote `deepseek-v4.1-flash:1017`
on `https://ollama.com`.

Hy4 Flash is absent from the 25-model cloud catalog.
`https://ollama.com/library/hy4-flash` returned 404. Ollama search shows
`dhiltgen/hy4-flash` as a local weight. That weight was not downloaded.

Ollama bills cloud use as credits at the model row, not as a fixed token quota.
Pay as you go has no included credits and 3 concurrent requests. Pro includes
$60 per month and 3 concurrent requests. Max includes $300 and 10. Team includes
$1,000 shared and 10. The signed-in local daemon's `GET /api/usage` returns
request counts only; it does not return the plan or a remaining credit balance.
`OLLAMA_API_KEY` was absent from the checked environment. The signed-in local
daemon is the existing cloud path and does not require a Hy4 download.

DeepSeek's own `GET /models` still lists `deepseek-flash`. JEV stays
`jev-1.13.0` at $0.042 per 1M input tokens and $0 output. No chat completion
and no System One request were sent.

## Cost estimate, not a measurement

Assumptions for one matrix of 5 questions on the two available arms: 8 policy
calls per question, 8 000 input tokens, cache miss, the single Ollama Hy4 row,
peak DeepSeek rates, and 4 shared JEV reads of 32 000 input tokens per
question. Hy4 Flash adds nothing because it has no cloud tag. Low output is
4 000 tokens. The approval output bound is 16 000 tokens.

| Bound | USD |
| --- | ---: |
| Low output | 0.98192 |
| Approval (high output) | 2.7584 |
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
2. Hy4 Flash has no Ollama Cloud tag, so that arm cannot be run through the
   existing cloud integration.
3. A live runner still has to be written. It must call Ollama as `hy4` on
   `https://ollama.com/api/chat` or `hy4:cloud` through the local daemon, keep
   domain policy out of `python/swe` and `crates/`, and keep `jev-1.13.0`
   independent of the policy arm.
