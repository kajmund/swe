# Hy4 research-policy benchmark

Offline comparison harness for three policy models on the frozen Research Gym.
The questions, corpus, tool menu, Sweycoin allocation, and USD cap are the
existing Gym values. SWE core is not modified.

Status: schema diagnosis `runs/20261123T213131Z` is a separate $2 pool.
`refuse_live_dispatch` still refuses an uncapped dispatch. Pydantic AI is used
only inside this experiment and does not schedule SWE work. The semantic
contract in `semantic_contract.py` is an offline mock: strict output, a
source-text check, then one explicit decision call through `agent_adapter.py`.

## Agent adapter

`agent_adapter.py` is the model-independent host. It does not import Pydantic
AI, the Rust runtime, or an agent loop. SWE supplies the provider, the budget,
the journal, the tool, and the retry count. Each `generate`, `decide`, and
`call_tool` writes one journal row per attempt.

The model directory at https://pydantic.dev/docs/ai/models/overview/, read
2026-11-23, lists `ollama:`, `typesafe:` (Jev), and `system-one:`. System One's
installed 2.54.0 text names CLM and Laya. Clef is not listed and is refused.
`ModelProfile` flags used for gating are `supports_tools`,
`supports_text_output`, `supports_json_schema_output`, and
`supports_json_object_output`. In 2.54.0, `OllamaModel` and `SystemOneModel`
import. `TypeSafeModel` raises `ImportError` until `typesafe-sdk` is installed.
Installed 1.63.0 has none of those model modules. The adapter never constructs
the native classes. Two configured generation mocks share one request type,
and the decision method is separate. Missing usage counts stay null. The USD
cap in `config/protocol.json` stays 2.

## Frozen inputs

| Input | Pin |
| --- | --- |
| Questions | Research `benchmarks/gym/questions.json` at `4a58d5a`, sha256 `142d8d8c…` |
| Corpus | Research `benchmarks/corpus`, Gym directory sha256 `ff85a396…` |
| Tools | `retrieve` (1 000 milli), `read_passage` (500 milli). No web search |
| Evaluator | Live pin `jev-1.13.0`. Offline method `exact-quote-v1` |
| Sweycoins | 50 000 milli per question, 250 000 milli per arm. Lexical baseline spent 29 250 milli |
| USD cap | 2, approved for the Hy4 vs DeepSeek Flash Ollama Cloud pilot |

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
`jev-1.13.0` at $0.042 per 1M input tokens and $0 output. The pilot did not
call JEV; verification was the local exact-quote check.

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

That high estimate is above the approved $2 cap, so it is not dispatched. The
live pilot is a separate plan: one completion per frozen question per available
arm, one reserved retry, `num_predict` as the output ceiling, peak cache-miss
prices, and local exact-quote verification at $0.

Sweycoin worst case inside those assumptions is 11 750 milli per question and
58 750 milli per arm, below the 50 000 milli question allocation.

## Cost comparison against DeepSeek Flash

Every scored trace carries a cost record. Input, output, reasoning, cache-read
and cache-write tokens are logged only when the provider field is present.
Cache is a separate calculated line: cached input is removed from the uncached
input line before either price is applied. Reasoning tokens are logged and are
not given a price, because neither verified price list has a reasoning row.

`calculated_usd` is the price-list equivalent. It is `N/A` when a token count
needed for that total is missing, and for DeepSeek Flash also when the call
does not say `peak` or `off_peak`. `actual_usd` is the provider charge. It is
`N/A` unless every call in the question reports `billed_usd`. Ollama plan name
and request quota stay in `quota` and are not divided into a dollar cost.
`usd_per_question` and `usd_per_verified_evidence` use `actual_usd` only. The
second is `N/A` when no facit quote was verified. Both arms use the same
Sweycoin allocation, the same USD cap, and the quality threshold in
`quality_threshold`. A delta against DeepSeek Flash is `N/A` when either side
lacks the corresponding amount.

## Run the offline tests

```sh
python3 -m pytest experiments/hy4-research-policy/tests
```

## Pilot

`runs/20261123T205650Z` is the approved Hy4 vs DeepSeek Flash run. Preflight
worst case was $0.05034956 at 1 098 input tokens, 1 024 output tokens, one
retry, and peak cache-miss prices. Ten completions were sent. None exceeded
`num_predict`. No weights were downloaded. Hy4 Flash stayed out.

The high 16 000-output matrix remains above the $2 cap and is not dispatched.
JEV was not called in that run. Ollama did not return cache tokens or a dollar
charge, so `calculated_usd` and `actual_usd` stay `N/A`.

## Schema diagnosis

`runs/20261123T213131Z` has its own $2 cap, zero retries, and saved request
bodies. With `think` false, DeepSeek's four empty decisions were the prompt
JSON echoed back (`conclusions`, `corpus`, `question`, …), `done_reason` stop,
and no thinking field. The server reports the model as `deepseek-v4.1-flash`
for a `:cloud` request. Native `format` accepted the call and did not enforce
the schema. Native tools returned an object. `think` true filled all 1 024
tokens with reasoning and an empty answer. Pydantic AI 2.54 treats
`:cloud` on the local daemon as native-schema capable; this adapter refuses
that mode before HTTP. Its prompted call returned 2 264 tokens after
`max_tokens` 1 024 and still included thinking. Isolated tasks then used native
tools, where `num_predict` held. Exact-quote scores and the 26 JEV relation
calls are stored separately. No provider charge field was returned.
