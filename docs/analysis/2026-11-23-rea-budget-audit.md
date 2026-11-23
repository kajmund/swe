# REA-rapport och budget-audit

**Status:** dokumenterad på isolerad branch `experiment/rea-budget-audit`.
**Datum:** 2026-11-23.
**Bas:** `origin/main` `eff1c7e8943dd64827fb4916719ced5515bf3db5`
(2026-11-22, "Merge pull request #5 from kajmund/cursor/expired-offer-effect-reconcile-257d").
Lokal `main` stod på `baa76a2` och var 20 commits bakom `origin/main` vid start.
Arbetet skedde i worktreen `/Users/erikfremred/swe-worktrees/rea-budget-audit`.
Ingen merge, ingen ändring av produktionsruntime, ingen ändring av `main`.

**Syfte:** dokumentera den tidigare REA-analysen av sju publika repos med exakta
källreferenser, commit-pins, licenser och verifieringsbegränsningar, samt
verifiera om budgetledgern saknas i ordinarie Python-dispatch och om någon
alternativ native/host-väg redan täcker det.

---

## 0. Sammanfattning

- Alla sju repos existerar och kunde klonas på angivna commits. Ingen repo
  behövde hoppas över. `model-router` saknar licens; övriga är MIT eller
  Apache-2.0.
- `kajmund/swe` har starkast determinism, replay och verifieringsgrindar.
  Svagheterna är: ingen heterogen routing, ingen budget i ordinarie Python-
  dispatch, lagerbarriärer i mikrotasks, ingen retry/backoff/rate-limit och
  ingen inlärning.
- **Budgetfrågan:** `swe-budget` finns som en stark Rust-ledger, men den är
  **inte** inkopplad i `WorkflowRunner`/`runtime_dispatch`. Det är **avsiktlig,
  dokumenterad host-policy**, inte ett kontraktsbrott: SWD 1.0-schemat har
  inget budgetfält, och `docs/specs/swe-runtime.md:91` säger uttryckligen att
  budget settlement inte ligger på Python-runtimevägen.
- **Alternativa vägar finns:** Rust-referenshosten `crates/swe-reference`
  använder `swe-budget` och pausar vid budgettak; 30 referenstester passerar.
  Native-journalen har `ChargeReserved`/`ChargeSettled`, men endast
  referenshosten emitterar dem. `ActionOffer` har en valfri `OfferBudget`-
  adapter, men ingen färdig Python-bindning till `swe-budget`.
- **Regressionsstestet** visar att en host-budget med tak 0 inte konsulteras
  och att en “betald” handler ändå körs. Det är en karaktärisering av
  gränssnittet, inte ett påstående att SWD 1.0 lovar enforcement.
- **Minsta fix:** ingen runtimeändring ännu. Lägg först en dokumenterad
  host-wrapper (reserve/settle/release) eller exponera `swe-budget` för
  Python; därefter kan en opt-in budget-hook i `WorkflowResources` beslutas
  som en separat kontraktsändring.

---

## 1. Uppdrag, avgränsning och samordning

### 1.1 Kontroller före arbetet

- `AGENTS.md` finns **inte** i SWE-repot: varken i roten eller inom
  `maxdepth 3` (verifierat med `find`). Inte heller `CLAUDE.md` finns.
  Mikrotask-swarm-sessionen `t1bp` noterade samma sak.
- `git status` på `main` var ren men visade `## main...origin/main [behind 20]`.
  Ingen lokal ändring fanns. Worktreen skapades därför från `origin/main`
  (`eff1c7e`), inte från den lokala, äldre `main`.
- Inga Hy4- eller microtask-swarm-processer startades, stoppades eller
  ändrades. Endast läsning av deras status via `hc get`/`hc read`.

### 1.2 Samordning med befintliga audits

| Session | Vad den gör | Vår relation |
|---|---|---|
| `t19c` `swe-graph-ledger-audit` | Cursor-agent, read-only audit av SWE/Research OverGraph och Sweycoin. Kör i `/Users/erikfremred/swe`, `--mode ask`. | Vi duplicerar inte OverGraph/Research-granskningen. `t19c` noterar själv att `WorkflowRunner` inte reserverar Sweycoin och att `OfferBudget` är separat. Vi verifierar SWE:s Python-dispatchgräns och lägger ett regressionstest. |
| `t1bp` `swe-microtask-swarm` | Claude Code, offline-pilot på `experiment/microtask-swarm` (`41769ba`, baserad på `eff1c7e`), i `/Users/erikfremred/swe-worktrees/microtask-swarm`. | Vi rör inte den worktreen. Piloten har en egen host-meter `SweycoinMeter` eftersom `swe-budget` saknar Python-bindning; dess tester täcker meterinvarianter, inte runtime-wiring. Vårt test täcker dispatchgränsen. |

Inga meddelanden, tangenttryck eller avbrott skickades till `t19c`/`t1bp`.

---

## 2. REA-verktyget: status och avgränsning

- Skill installerad: `~/.claude/skills/reverse-engineer-anything/SKILL.md`
  (`metadata.version: "63"`, `tool_count: 263`).
- MCP-servern svarar: `mcp__rea__list_documents` returnerade
  `target_unavailable` med meddelandet “No app is open. Ask the user which app
  to investigate…”.
- Ingen `rea`-binär finns på `PATH`; ingen hittades i `~/.local/bin`,
  `/opt/homebrew/bin` eller `~/.cargo/bin`.
- Skillens egen avgränsning: *“Skip REA for ordinary source-repository
  architecture analysis”* (`SKILL.md:3`) och *“For ordinary analysis of a
  complete source repository, use normal repository tools and do not run REA
  readiness or provider commands”* (`SKILL.md:12–15`).
- Slutsats: REA är installerat och MCP-servern lever, men för den här uppgiften
  är REA fel verktyg enligt sin egen dokumentation. Analysen är därför vanlig
  läs-only källkodsanalys. Det är inte ett blockerande fel.

**Utförande:** alla sju repos verifierades med `git ls-remote --symref` och
klonades därefter ytligt (`--depth 1 --single-branch`) till
`/tmp/rea-repo-analysis-1795467265/`. Ingen extern repo-kod kördes; inga
`pip install`, `npm install`, tester eller byggen i de externa reporna.
Endast fil-läsning och ofarliga `git log`/`git ls-files`.

---

## 3. Repo-inventering med commit-pins

| Repo | HEAD | Datum | Licens | Relevans | Storlek | Tester/CI |
|---|---|---|---|---|---|---|
| `alityb/batchagent` | `c937f54b151ad15b3158fe852de9356194dfd670` | 2026-05-17 | MIT | Ja: batchning av verktygsanvändande LLM-agenter | 37 685 rader, 185 filer | 39 testfiler, 164 testfunktioner; ingen CI |
| `ellmos-ai/swarm_ai` | `4394f61763f6068304e00fd4c7b9f8c483508f10` | 2026-11-22 | MIT | Ja: stigmergi, claims, consensus | 22 402 rader, 93 filer | 12 testfiler, 195 testfunktioner; CI-matris |
| `lm-sys/RouteLLM` | `0b64fdafe049e596a3f5657c219329f24af24198` | 2024-08-10 | Apache-2.0 | Ja: strong/weak-routing | 121 909 rader, 108 filer | Inga automatiska tester; release-CI |
| `r600a-code/dsh-swarm-router` | `fc051ea2ad05fc27ad7271630b7e0bd7ebb24bb9` | 2026-08-16 | MIT | Ja: modellrouter + feedback | 2 808 rader, 24 filer | 0 tester; ingen CI |
| `adamreading/Agent-LLM-Feeder` | `20f8580f282ed44112cb6a275199fab2ec1d1c6b` | 2026-09-18 | MIT | Ja: OpenAI-kompatibel router/gateway | 128 151 rader, 286 filer | 50 Vitest-filer; CI med Postgres |
| `dvmazur/async_llm` | `c03eed4edbcd44b4b55b8b8353f593d2e095eec0` | 2026-10-26 | MIT | Ja: asynkron batchning, delad KV-cache | 78 479 rader, 232 filer | 57 testfiler; ingen CI |
| `as3k/model-router` | `5d18047418739dbe38c31f5a913fec5b227ecf44` | 2026-10-04 | **Saknas** | Ja: tier-routing, budget, failover | 1 890 rader, 15 filer | 0 tester; ingen CI |

Alla kloner är `--depth 1`; långsiktig historik och underhållsstatus är
**UNKNOWN** utöver HEAD. Det gäller särskilt om ett repo är övergivet.

---

## 4. Per-repo analys

### 4.1 `batchagent` – batchkörning med delad prefix

- Kör en lista av `AgentJob` mot en gemensam backend; en modell per plan, inte
  per uppgift (`batch_agent/spec.py:55–56`, `batch_agent/compiler.py:58–65`).
- Asyncio-baserad `WaveScheduler`, `PrioritySemaphore` och adaptiv
  samtidighetskontroll (`batch_agent/scheduler.py:42–47,50`,
  `batch_agent/priority_semaphore.py:16–26`).
- `ToolPool` har TTL-cache och single-flight för identiska verktygsanrop
  (`batch_agent/tools/pool.py:38–57`).
- Ingen budgetledger eller USD-kostnadsstyrning i runtime-paketet;
  `metrics.py` mäter varv/jobb/fel/latens, inte pengar.
- **Mätt:** prefix-cache-benchmarken finns som artefakt
  (`tests/benchmarks/results/prefix_cache_benchmark/results.json`, 15 504 byte)
  och matchar README-tabellen, t.ex. `n=5`, `ttft_p50=0.1838`, hårdvara
  “A10G 23GB, Qwen/Qwen2.5-7B-Instruct, vLLM 0.6.6.post1”.
- **Påstående utan mätning:** kostnadsjämförelsen är blockerad;
  `real_cost_usd: null`, `status: "blocked"` i
  `tests/benchmarks/results/cost_comparison/results.json`.
- README är ärligt: KVFlow “not verified”, TokenDance “mock only”,
  distribuerad Redis “prototype” (`README.md:216–218,256–261`).
- Användbara idéer: single-flight-koalescering, prioritet efter återstående
  varv, släpp inferensslot under verktygsväntan.

### 4.2 `swarm_ai` – SQLite-claims och stigmergi

- Delar upp text i chunkar som claimas atomiskt i SQLite innan API-anrop
  (`tools/summarize_chunks.py:273–300`, `tools/translate_swarm.py:218–259`).
- Stigmergi-tabell `swarm_pheromones` med `strength` 0..1 och explicit
  `evaporate()` som avaktiverar svagaste fraktionen
  (`tools/stigmergy_api.py:34–60,233–250`).
- `ThreadPoolExecutor`, inte asyncio (`tools/consensus_swarm.py:34,378–380`).
- Ingen avbokning av redan körande trådar; endast subprocess-timeout för Claude
  CLI (`tools/runner.py:124,137–145`).
- Kostnad: förhandsberäknad övre gräns och `max_budget_usd`-validering före
  live-körning (`tools/consensus_swarm.py:330–340`). Det är en förkontroll,
  inte en löpande ledger.
- **“Specialist routing” i README är inte implementerad:** inga filer matchar
  `specialist|hierarch|router`, och `TODO.md:24` säger att det är oavgjort om
  `hierarchy.py`/`specialist.py` ska portas.
- **Mätt:** `results/benchmark_20260306.json` är en riktig körning;
  `results/benchmark_20260813.json` är `"dry_run": true`; en pilotartefakt har
  `total_cost_usd: 0.0` och `completed: 0`.
- Användbara idéer: claim-rader som idempotent partitionsprimitive,
  pheromone-store med avdunstning, förhandsberäknad kostnadsgräns.

### 4.3 `RouteLLM` – stark/weak-routing med tröskel

- `Controller` routar varje request till `strong_model` eller `weak_model` via
  ett tröskelvärde (`routellm/controller.py:139–170`,
  `routellm/routers/routers.py:41–45`).
- Fem routrar: matrix factorization, BERT, Llama-3-klassificerare,
  similarity-weighted Elo och random (`routers.py:51–253`). Viktiga vikter
  ligger externt på Hugging Face (`config.example.yaml:9–13`), inte i repot.
- Ingen failover, retry eller budget. Ingen cache av routingbeslut; endast
  offline-cache av win-rates till `.npy`.
- Inlärning är offline; ingen online-feedback.
- **Allvarligt:** eval-vägen är trasig vid HEAD. `evaluate.py:270` skickar
  `controller.model_pair.strong` (en sträng) till `generate_results`, som vid
  `evaluate.py:36` gör `routed_pair.weak`. `calibrate_threshold.py:31–49`
  skickar `routed_pair=None` och utelämnar `strong_model`/`weak_model`, medan
  `Controller.__init__` kräver dem (`controller.py:41–51`).
- **Mätt:** committade eval-data finns (GSM8K-CSV, MT-Bench-judgements,
  MMLU-CSV). Rubrikpåståendena “upp till 85 % billigare vid 95 % av GPT-4”
  (`README.md:14–15`) backas bara av PNG:er och extern paper, inte av en
  committad resultattabell. Tröskeln `0.11593` går inte att återskapa utan
  externt HF-dataset.
- Inga automatiska tester: `routellm/tests/` innehåller manuella
  argparse-skript, noll `def test_`/pytest.
- Senaste commit 2024-08-10; beroenden grovt opinnade; CI publicerar bara till
  PyPI.
- Användbar idé: tröskel som kvantil av en kalibrerad score-fördelning, med
  dataset-hash dokumenterad.

### 4.4 `dsh-swarm-router` – ren, förklarabar router

- `routeTask` poängsätter alla kandidater, filtrerar på capability och
  returnerar vald modell plus topp-5 och en läsbar motivering
  (`router.js:60–88`).
- `Promise.all` över alla uppgifter, **obegränsat** (`index.js:332`); verktyget
  deklareras samtidighetssäkert (`index.js:315,324`).
- Tokenräkning per anrop, uppdelad per provider/modell/kind
  (`store.js:83–116`), och `costEstimate` med “saved vs all-flagship”
  (`store.js:136–168`). Ingen hård gräns eller förhandsreservation.
- Feedback: `rankings.json` med `{correct, quality}`; boost `+1.5` vid
  success ≥0.9 och `−3.0` vid ≤0.4, men först efter ≥3 försök
  (`ranking.js:44–54`).
- Delat tillstånd: JSON-filer med read-modify-write utan lås
  (`store.js:23–45`). Det är den största korrekthetsrisken under samtidig
  fan-out.
- **Mätt:** `benchmark/benchmark_RESULT.json`,
  `benchmark_subagent_RESULT.json`, `benchmark_direct_RESULT.json` finns med
  exakta token-siffror, t.ex. GLM-5.2 `{82,114,196}` och DeepSeek V3.2
  `{61,448,509}`.
- 0 tester, ingen CI, opinnade peer-dependencies (`package.json:42–47`).
- Användbara idéer: ren router med topp-5 och motivering; feedback-boost med
  minimitröskel; tokenattribuering per barn via `sessionId == run.id`.

### 4.5 `Agent-LLM-Feeder` – router/gateway, inte dokumentfeeder

- Öppen OpenAI-kompatibel endpoint framför många gratisnivåleverantörer.
  Capability-gates accepterar bara `measured`/`observed`, aldrig `declared`
  (`server/src/services/router.ts:796–812`).
- Typade slutfel: `NO_ELIGIBLE_MODEL` (422) vs `ALL_RATE_LIMITED` (429). Det
  skiljer “kan aldrig qualify” från “tillfälligt slut”.
- Rate limiting per `platform:model:key`, cooldowns, failover upp till 20
  försök, en retry för nätverksfel (`proxy.ts:345,908,1209–1220`;
  `ratelimit.ts:8–11,26–79`).
- Budget: `swarmBudget.ts:10–27` har uttalade invarianter – opt-in,
  **set-once/lower-only**, fail-open, bounded overshoot, och räknaren seedas
  från request-loggen vid omstart.
- Cache: sökcache med TTL och bounded eviction (`searchCache.ts:3–24,28–51`);
  sticky session per `session_id`. Ingen exakt beslutscache för routing.
- Inlärning: tumme upp/ner justerar score, och live-utfall skriver
  `source='observed'`-capabilities. Det är bounded online-adaptation, inte
  modellträning.
- **Namnet är missvisande:** den indexerar/chunkar/embeddar inte filer.
  `/api/agent/chat` läser upp till 8 filer per request och stoppar in
  innehållet i prompten (`server/src/routes/agent.ts:388–405`). Ingen
  persistent chunk- eller embeddingsstore.
- **Mätt:** 50 Vitest-filer under `server/src/__tests__/`; CI med Postgres 16.
  README-siffrorna (1 606 beslut, −0,27-korrelation, 46 % egress-fel) har inga
  committade råloggar; `stats.jsonl` är untracked.
- Klient-avbrott: ingen `req.on('close')`/`res.on('close')` hittades i
  `proxy.ts`; om uppströmsanropet avbryts vid klient-disconnect är **UNKNOWN**.
- Licens: MIT i `LICENSE`, men copyright-raden säger “Tashfeen Ahmed” medan
  repot ägs av `adamreading`. Härstamning/attribution bör verifieras före
  kopiering.
- Användbara idéer: evidence-gated eligibility med typade fel;
  set-once/lower-only run-budget med replay; bounded exact cache med
  failure-aware cooldown.

### 4.6 `async_llm` – asynkron inferens med delad KV-cache

- Forskning/ingenjörs-fork av Mini-SGLang: flera agentkoroutiner delar
  `CacheBlock` och batchas i samma forward-pass (`README.md:9–21`,
  `python/minisgl/llm/async_llm.py:22–27`).
- Prefill-gruppering tar längsta FIFO-prefix som kan dela en forward och
  stoppar vid KV-kollision; resten behåller sin plats
  (`python/minisgl/scheduler/async_engine.py:254–280`). Det är ett bra mönster
  för deterministisk batchning.
- Avbokning: att bryta `async_generate` stoppar strömmen rent, och en senare
  konsument återupptar vid nästa token (`async_llm.py:310–330`; test
  `tests/core/test_async_llm.py:155–176`). HTTP-disconnect avbryter requesten
  (`python/minisgl/server/api_server.py:192–207`).
- Cache: radix-prefix-cache och chunked prefill
  (`kvcache/radix_cache.py:101–168`, `scheduler/cache.py:17–49`).
- Ingen kostnadsledger, ingen provider-routing, en modell per engine. Ingen
  per-request-deadline i `AsyncLLM`.
- **Mätt:** 57 testfiler; CPU-stubbar och GPU-E2E är separerade. Inga
  committade tokens/s- eller latensresultat; README/paper är påståenden.
  Ingen CI (`.github` saknas), version 0.1.0 alpha.
- Användbara idéer: FIFO-batchning med explicit konfliktdeferral; felisolering
  per request som håller schedulern vid liv; återupptagbara strömmar.

### 4.7 `model-router` – personlig router med spend-guardrails

- Liten FastAPI-proxy som klassificerar varje request med en extern
  “Laya”-modell och vid låg konfidens konsulterar en “Jev”-tiebreaker
  (`router.py:1–14,127–324`; `modules/classify.py:153–184`).
- Fyra tiers (`k2`/`deepseek`/`luna`/`terra`) och sex backends;
  session-pinning per systemprompt-hash (`classify.py:93–103`).
- Failover: ordnade kedjor, skip-first circuit breaker med 120 s cooldown,
  högst en kvalitetseskalering per request
  (`modules/dispatch.py:18–43,99–141,182–193`).
- Budget: daglig spend-ledger med soft/hard USD-gränser, seedad genom replay
  av dagens `stats.jsonl`; hard limit ger deterministiskt degraded mode
  (`modules/spend.py:21–100`; `router.py:231–267`).
- Cache: sessionspinning och kompaktionssammanfattning validerad med rullande
  prefix-hash (`modules/compact.py:36–43,107–141`). Ingen exakt beslutscache.
- Inlärning: `/v1/feedback` och `/v1/outcomes` lagrar signaler, men inget matas
  automatiskt tillbaka till routing (`modules/stats_store.py:163–165`).
- **Ingen licens alls** – varken LICENSE-fil eller `license`-fält; “all rights
  reserved”. README:s Apache 2.0 gäller den externa Laya-modellen, inte repot.
- 0 tester, ingen CI, ingen manifestfil; kräver ocommittad `config.json`
  (`settings.py:8`) och privat `laya`-paket.
- Alla kvantitativa README-siffror (1 606 beslut, 216 gate-checks, 2,25 USD)
  saknar committad data; `stats.jsonl` är untracked.
- Användbara idéer: prefix-hash-validerad återanvändning; append-only
  spend-replay med deterministisk degradering; skip-first circuit breaker.

---

## 5. `kajmund/swe` på `eff1c7e`: arkitektur och jämförelse

### 5.1 Verifierad grundarkitektur

- Rust-kärnan `swe-native` äger deterministisk maskin, routing, join-barriärer,
  events och journal. Python-adaptern äger asyncio, deadlines, cancellation,
  handlers och providers (`docs/specs/swe-runtime.md:1–20`).
- Mikrotasks är en authoring-fasad: en statisk DAG kompileras till vanliga SWD
  `map`-noder, ett per topologiskt lager
  (`python/swe/microtasks.py:1–8,169–279,282–310`).
- Exakt beslutscache: identitetsnyckel över provider, prompt, input, choices
  och params; `auth_scope`, `policy_version` och `model` krävs i params och
  fail-closed (`python/swe/cache.py:57,126–159`).
- `ActionOffer` är en host-layer med persistent `APPROVING`-claim, lease,
  takeover via compare-and-set och valfri `OfferBudget`
  (`python/swe/offers.py:272–279,551–575,732–856,947,1002–1007`).
- Journalen skiljer exekvering, bekräftelse och debitering; delivery-klasser
  avgör recovery (`crates/swe-native/src/journal.rs:1–90`).

### 5.2 Jämförelsetabell

| Dimension | `kajmund/swe` (`eff1c7e`) | Starkaste externa referens | Bedömning |
|---|---|---|---|
| Atomiska mikrotasks | Statisk DAG → topologiska lager → SWD `map` (`microtasks.py:169–279,282–310`) | `batchagent`, `swarm_ai` | SWE starkast på determinism/replay; svagast på dynamisk beredskap |
| Async parallellism | asyncio + `CapacityLimiter` + Rust `map.concurrency` (`runtime.py:190`, `runtime_dispatch.py:72–130`) | `async_llm`, `batchagent` | Två gränser och bounded fan-out; ingen batchning, prioritet eller adaptivitet |
| Heterogen routing | Ingen; en namngiven provider per beslut, ingen fallback (`docs/specs/swe-provider.md:32,61`) | `RouteLLM`, `Agent-LLM-Feeder`, `model-router`, `dsh` | Största funktionella gapet |
| Stigmergi/delat tillstånd | `OfferStore`-claim/lease/CAS; `RunStore`/journal (`offers.py:732–856`) | `swarm_ai`, `async_llm` | Durable claims, inte stigmergi/avdunstning |
| Cancellation | `cancel_event`, action-timeouts, `InvocationHandle`, bounded abandoned registry (`runtime.py:8–33`, `invocation_handle.py`) | `async_llm` | Explicit och testat, men kooperativt |
| Budget ledger | Stark crate (`crates/swe-budget/src/lib.rs:1–20`), **inte** i Python-dispatch; endast referenshost/OfferBudget | `crates/swe-reference`, `model-router`, `Agent-LLM-Feeder` | Bäst ledger-primitiv, sämst Python-inkoppling |
| Cache reuse | Exakt identitetscache, single-flight, bounded L1 (`cache.py:436–535,571–615`) | `async_llm`, `model-router` | Stark exact identity; ingen semantisk/prefix-cache |
| Oberoende verifiering | Contract freeze 38 fall, mutation 32 fall, license gate OK, 255 Python-/140 Rust-testfunktioner (grep-räknade på `eff1c7e`) | `Agent-LLM-Feeder`, `async_llm` | Starkast sammantaget; cachebenchmarken är svag |
| Lärande | Ingen | `RouteLLM`, `Agent-LLM-Feeder`, `dsh` | Saknas helt |

### 5.3 Egna fynd i SWE

**A. Warm-cache-testet mäter en enda nyckel.**
`benchmark_exact_cache` skapar nycklar med
`input_data={"ordinal": index if unique else 0}` (`cache.py:723`). I warm-fasen
anropas `run_wave(unique=False)` (`cache.py:751,753`), så alla 10 000 anrop får
samma nyckel. Testet mäter 10 000 träffar på en nyckel, inte återanvändning
över en arbetsmängd. Dokumentationen säger “10,000 cold unique resolves plus
10,000 warm hits” (`docs/specs/swe-exact-cache.md:49–55`). Tekniskt sant, men
missvisande: med `max_entries=256` och 10 000 unika nycklar är en andra
sekventiell genomgång i praktiken en miss-serie.

**B. Specen säger FIFO, koden gör LRU.**
Specen: “FIFO-by-`(inserted_at, key)` eviction”
(`docs/specs/swe-exact-cache.md:32`). Koden väljer offer med
`min(..., key=lambda entry: (entry.last_access_ms, entry.inserted_at_ms, entry.key))`
(`cache.py:594`), och `_fresh` uppdaterar `last_access_ms` vid träff. Det är
LRU, inte FIFO. Beteendet är rimligt, men dokumentation och tester bör alignas.

**C. Lagerbarriärer och utebliven resultatinjektion.**
Ett `map` per topologiskt lager (`microtasks.py:230`) och
`_topological_layers` (`microtasks.py:282–310`) gör att en långsam uppgift i
lager N blockerar hela lager N+1. Beroenderesultat injiceras inte automatiskt i
nedströms handlers (`docs/specs/swe-microtasks.md:50–52`). Bra för determinism,
sämre för sneda DAG:ar och agentpipelines.

**D. Ingen retry/backoff/rate-limit/fallback.**
`retry` avvisas i preflight (`python/swe/runtime_preflight.py:87`;
`crates/swe-native/src/preflight.rs:25–28`). Provider-dokumentationen säger att
ingen provider-, prompt- eller modell-fallback finns
(`docs/specs/swe-provider.md:32,61`). Cancellation är kooperativ.

**E. Verifieringsgrindarna är starka men dokumentationen driftar.**
På `eff1c7e` passerade vid min körning:
- `python3 scripts/test_check_mutations.py` → **32 cases, 0 failed**.
- `python3 scripts/test_check_contract_freeze.py` → **38 cases, 0 failed**.
- `PATH="$HOME/.cargo/bin:$PATH" python3 scripts/check_licenses.py` →
  **license gate OK** (41 Rust- och 14 Python-beroenden).
- `cargo test -p swe-budget --locked` → **9 passed**.
Dokumenten säger “30 cases, 0 failed”
(`docs/swe-standalone/14-mutation-gate.md:53`), men självtesten har 32.

**F. Erbjudandeclaims är seriösa.**
`ActionOfferHost` tar en `APPROVING`-claim via `store.transition`, kontrollerar
lease-ägande före dispatch och gör takeover som compare-and-set på utgången
`claimed_until_ms` (`offers.py:732–856`). Det är en riktig exklusiv claim.

**G. Mätningar (committade, inte omkörda här).**
- Mikrotasks: 10 000 uppgifter, concurrency 64, 2 466,028 ms, 4 055,103
  tasks/s, p50 0,034166 ms, p95 0,049083 ms, 10 000 unika resultat, peak 64.
  Miljö: macOS 15.7.4 arm64, 10 CPU, Python 3.13.6
  (`benchmarks/microtasks/independent-10000.json`).
- Offers/cache: 10 000 kalla + 10 000 varma resolves, 22 882,812 cache ops/s,
  3 818,218 offer ops/s, peak in-flight 64, peak entries 256, p95 för offers
  17,627 ms. Miljö: Linux 6.12 x86_64, 4 CPU, Python 3.12.3
  (`benchmarks/offers-cache/independent-10000.json`).
- Båda använder syntetiska no-op-handlers (`asyncio.sleep(0)`); de mäter
  scheduler-/cache-overhead, inte LLM-latens eller kostnad.

---

## 6. Budget-audit: saknas ledgern i ordinarie Python-dispatch?

### 6.1 Frågan

Finns `swe-budget`-ledgern i den vanliga Python-vägen
`WorkflowRunner` → `runtime_dispatch.spawn_invocations` → handler/provider?
Täcker någon alternativ native- eller host-väg redan behovet? Är frånvaron en
avsiktlig host-policy eller ett verkligt kontraktsbrott?

### 6.2 Svar

**Nej, ledgern finns inte i ordinarie Python-dispatch.**
**Ja, det är avsiktlig och dokumenterad host-policy.**
**Nej, det är inte ett kontraktsbrott enligt SWD 1.0.**
**Ja, en alternativ Rust-referenshost täcker budget, men den är inte Python-
vägen.**

### 6.3 Belägg

**a) Ingen budget i Python-ytan.**
- `WorkflowResources` har fälten `providers`, `prompts`, `prompt_keys`,
  `cache`, `cancel_event`, `action_timeouts` – inget `budget`/`ledger`
  (`python/swe/run_store.py:112–140`).
- `spawn_invocations` tar `handlers`, `nodes`, `pending`, `semaphore`,
  `provider_dispatch`, `now_ms`, `handles` – inget budgetargument
  (`python/swe/runtime_dispatch.py:177–187`).
- `python/swe/__init__.py` exporterar kompilering, schema, uttryck, hash och
  `InvocationHandle`; ingen ledger.
- `swe-native` har ingen `swe-budget`-dependency (`crates/swe-native/Cargo.toml`).
- den PyO3-modul som byggs exporterar `Machine`, `sha256_hex`,
  `canonical_json_bytes`, `evaluate_expression`, `resolve_evidence`,
  `derive_needs` och `operation_gate` – ingen ledger
  (`crates/swe-native/src/python.rs:21–29,59`).
- `runtime.py` har en intern `_settle_invocation` (`runtime.py:810`), men den
  städar invocationer/abandoned work; den debiterar inget.

**b) Policyn är dokumenterad.**
- `docs/specs/swe-runtime.md:91–95`: “Budget settlement is not on the Python
  runtime path. `swe-budget` is a separate ledger (reference host and
  ActionOffer).”
- `python/swe/runtime.py:33`: “`swe-budget` is not on this path: timeout/cancel
  must not double-complete or double-fail the machine.”
- SWD 1.0-schemats `limits` har endast `max_steps`, `max_depth` och
  `timeout_seconds` (`python/swe/swd_v1.schema.json:55–65`). Det finns inget
  budgetfält i det frysta dokumentet.
- Slutsats: en host som vill ha budget måste äga den policyn. Runtime kan inte
  läsa ett tak ur SWD-dokumentet, eftersom något sådant fält inte finns.

**c) Alternativa vägar som faktiskt täcker budget.**
- `crates/swe-reference` är en Rust-referenshost som använder
  `swe_budget::Ledger` (`crates/swe-reference/Cargo.toml:22`,
  `src/host.rs:18,329–330`), reserverar före handlingen
  (`src/host.rs:639`), settle:ar efter (`src/host.rs:669`) och pausar vid
  `pause_at_spend_milli` (`src/host.rs:132–135`).
- Referenshostens test `pause_and_resume_charge_and_execute_exactly_once`
  visar att ett tight budget pausar och återupptas utan dubbeldebitering
  (`tests/reference.rs:560`); `metering_is_per_action_and_sums_to_the_ledger`
  finns vid `tests/reference.rs:1061`.
- Jag körde `cargo test -p swe-reference --locked` på `eff1c7e`:
  **30 passed, 0 failed**.
- Native-journalen har `ChargeReserved` och `ChargeSettled`
  (`crates/swe-native/src/journal.rs:117–125,280–355`), men endast
  referenshosten appenderar dem (`crates/swe-reference/src/host.rs:646,675`).
  Python-runtime appenderar aldrig dessa events.
- `ActionOfferHost` har en valfri `OfferBudget` (`offers.py:272–279,551–575`)
  och anropar `reserve`/`settle` (`offers.py:947,1002–1007`). Men
  `MemoryOfferBudget` är en testdubbel (`offers.py:404–445`); det finns ingen
  färdig Python-adapter till `swe-budget`.
- `swe-budget` är en ren Rust-crate utan `pyproject.toml` och utan PyO3-yta;
  `python/tests/test_swe_budget_dispatch_boundary.py` verifierar att
  `swe_budget` inte går att importera som Python-modul på denna commit.

**d) Mikrotask-swarm-pilotens vittnesmål.**
Sessionen `t1bp` noterade att `swe-budget` saknar Python-bindning och att
piloten därför använder en egen host-meter `SweycoinMeter` med samma
integer/reserve/settle-semantik (`experiments/microtask-swarm/microtask_swarm/
budget.py:1–12`). Det är ett oberoende belägg för samma gap. Pilotens
`tests/test_budget.py` täcker meterinvarianter, inte runtime-wiring.

### 6.4 Vad regressionstestet demonstrerar

Testet `python/tests/test_swe_budget_dispatch_boundary.py` har fyra fall:

1. `test_dispatch_runs_paid_handler_without_consulting_host_budget` – en
   host-budget med tak 0 konsulteras aldrig; en handler som “kostar” 1 000
   milli körs ändå och `reserve_calls == 0`.
2. `test_workflow_resources_and_dispatch_expose_no_budget_hook` – frånvaron är
   strukturell: varken `WorkflowResources` eller `spawn_invocations` har ett
   budget-/ledger-fält.
3. `test_core_ledger_is_not_exposed_as_a_python_module` – `swe_budget` finns
   inte som importerbar Python-modul.
4. `test_offer_path_has_an_optional_budget_but_dispatch_does_not` –
   `ActionOfferHost` har `budget`, men dispatch har det inte.

Testet är en **karaktärisering**, inte ett påstående att SWD 1.0 lovar
enforcement. Om någon lägger till en budget-hook ska testet faila och uppdateras
tillsammans med policydokumentet. Det använder inga betalda anrop och ingen
native-modul.

### 6.5 Avsiktlig host-policy vs verkligt kontraktsbrott

| Fråga | Svar | Belägg |
|---|---|---|
| Saknas budget i Python-dispatch? | Ja | `run_store.py:112–140`, `runtime_dispatch.py:177–187` |
| Är det dokumenterat? | Ja | `docs/specs/swe-runtime.md:91–95`, `runtime.py:33` |
| Finns ett SWD-budgetfält som runtime ignorerar? | Nej | `swd_v1.schema.json:55–65` |
| Är frånvaron ett kontraktsbrott? | Nej | Ingen normativ löfte om enforcement; host äger policyn |
| Finns en alternativ väg? | Ja, Rust-referenshost | `crates/swe-reference/src/host.rs`, 30 tester |
| Finns en färdig Python-väg? | Nej | Ingen `swe_budget`-bindning; `OfferBudget` är opt-in/testdubbel |
| Vad är den verkliga risken? | En host som antar att runtime stoppar vid tak kan överspendera | Regressionstestet demonstrerar att `reserve` aldrig anropas |

---

## 7. Reproduktion och resultat

### 7.1 Python-regressionstestet (offline, inga betalda anrop)

```sh
cd /Users/erikfremred/swe-worktrees/rea-budget-audit/python
PYTHONPATH=. uv run --no-project \
  --with jsonschema --with pyyaml --with pytest --with pytest-asyncio \
  python -m pytest tests/test_swe_budget_dispatch_boundary.py -q \
  -o asyncio_mode=auto
```

Resultat på `eff1c7e`:

```text
....                                                                     [100%]
4 passed in 0.06s
```

### 7.2 Rust-referenshosten (offline)

```sh
cd /Users/erikfremred/swe-worktrees/rea-budget-audit
PATH="$HOME/.cargo/bin:$PATH" cargo test -p swe-reference --locked
```

Resultat:

```text
test result: ok. 30 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out
```

### 7.3 Övriga grindar på `eff1c7e`

```text
python3 scripts/test_check_mutations.py        -> 32 cases, 0 failed
python3 scripts/test_check_contract_freeze.py  -> 38 cases, 0 failed
PATH="$HOME/.cargo/bin:$PATH" python3 scripts/check_licenses.py
                                               -> license gate OK
PATH="$HOME/.cargo/bin:$PATH" cargo test -p swe-budget --locked
                                               -> 9 passed
```

---

## 8. Minsta föreslagna fix

**Ingen produktionsruntime ändras i detta steg.** Den minsta fixen är en
host-adapter, inte en ny scheduler eller ett nytt SWD-fält.

### Steg 1 – dokumenterad host-wrapper (minst ändring)

Lägg en liten, frivillig Python-adapter (t.ex. `swe.budget` eller i hostens
egen kod) som:

1. tar en `BudgetLedger`-protocol med `reserve`/`settle`/`release`,
2. använder `current_invocation()` eller `InvocationHandle.invocation_id` som
   stabil idempotensnyckel,
3. reserverar före handleranropet, settle:ar på framgång och release:ar på
   fel/avbrott.

Detta kräver ingen ändring av `WorkflowRunner`. Det gör budgeten till en
uttalad host-policy och ger ett enda ställe att testa. Regressionstestet kan
utökas med en wrapper som faktiskt anropar `reserve`/`settle`.

### Steg 2 – Python-bindning eller FFI

Exponera `swe-budget` för Python, antingen via en liten PyO3-modul i
`swe-native` (separat klass, t.ex. `Ledger`) eller via en host-implementation
av protokollet. Detta är den egentliga luckan: `swe-budget` är idag en
Rust-only-crate. Alternativt kan en host använda en egen ledger, som
microtask-swarm-piloten gör, men då är den inte samma crate.

### Steg 3 – endast om enforcement ska bli en kärngaranti

Om SWE ska lova budget enforcement i runtime krävs en explicit
kontraktsändring:

- ett opt-in-fält i `WorkflowResources` (t.ex. `budget: BudgetLedger | None`),
- en dokumenterad semantik för reserve/settle/release vid timeout, cancel och
  replay,
- en ny contract decision, eftersom SWD/journal-ytan påverkas.

Det ska **inte** göras förrän steg 1–2 är utvärderade. Nuvarande dokumentation
säger uttryckligen att budget inte ligger på runtime-vägen; att ändra det är en
produktpolicy, inte en bugfix.

---

## 9. Licenser och återanvändning

| Repo | Licens | Får kod återanvändas? | Kommentar |
|---|---|---|---|
| `kajmund/swe` | MIT OR Apache-2.0 | Ja | 41 Rust- + 14 Python-beroenden permissiva enligt license gate |
| `batchagent` | MIT | Ja, med attribution | LICENSE + metadata tydliga |
| `swarm_ai` | MIT | Ja, med attribution | `THIRD_PARTY_LICENSES.md` är selektiv, inte transitiv |
| `RouteLLM` | Apache-2.0 | Ja, med NOTICE/attribution | Modellvikterna ligger externt på HF; deras villkor är **UNKNOWN** |
| `dsh-swarm-router` | MIT | Ja, med attribution | Inga tester; granska före kopiering |
| `Agent-LLM-Feeder` | MIT | Ja, men verifiera härstamning | LICENSE säger “Tashfeen Ahmed”, inte repoägaren; bygger på FreeLLMAPI |
| `async_llm` | MIT | Ja, med attribution | LICENSE säger “sgl-project”; härstamning från Mini-SGLang |
| `model-router` | **Ingen** | **Nej** | “All rights reserved”; endast idéer, ingen kod |

Praktisk regel: MIT/Apache-2.0-kod får lånas med bevarade licens- och
copyrighttexter. `model-router` får bara inspirera arkitektur. `RouteLLM`-
vikter och `laya`-modellen är separata artefakter med oklar licens.

---

## 10. Verifieringsbegränsningar

- Externa repors fullständiga historik och underhållsstatus: alla kloner är
  `--depth 1`; endast HEAD är känd.
- Externa repo-körningar: ingen kod exekverades, inga tester kördes, inga
  beroenden installerades. Påståenden om körtidsbeteende är kodläsning, inte
  mätning.
- `RouteLLM`s rubriksiffror (85 %/95 %) och tröskel `0.11593`: kräver externa
  HF-vikter/dataset och API-nycklar; ej återskapade.
- `Agent-LLM-Feeder`s README-siffror: råloggarna är untracked.
- `model-router`s faktiska beteende: kräver ocommittad `config.json` och privat
  `laya`-paket.
- SWE:s prestandabenchmarkar kunde inte köras om här (saknad venv/native);
  de committade JSON-resultaten används som underlag.
- Agentrapporternas alla fil:rad-angivelser är stickprovskontrollerade, inte
  uttömmande verifierade rad för rad.
- Python-regressionstestet kräver `jsonschema` och `pyyaml` men **inte**
  `swe_native`; det testar dispatchgränsen, inte en full `WorkflowRunner`-körning.
- `cargo test -p swe-reference` byggde endast Rust-craten; det bevisar
  referenshostens budgetväg, inte Python-hostens.
- Inga betalda anrop gjordes i något steg.

---

## 11. Prioriterade experiment (per dollar och sekund)

Kostnad och tid är **uppskattningar**.

| Prio | Experiment | Hypotes | Metod | Framgångskriterium | Kostnad | Väg­tid |
|---:|---|---|---|---|---|---|
| 1 | Realistisk cachebenchmark | Nuvarande warm-test överskattar återanvändning | Arbetsmängder 64/256/1024/10000 med Zipf α=0,8/1,0; mät hit rate, p95, evictions | Hit-rate-kurva som visar var 256-gränsen biter | Låg | 1–2 h |
| 2 | Cancellation-orphan-test | En handler som sväljer `CancelledError` kan lämna sena resultat | Adversariell handler + `cancel_event`; mät taskar efter terminal status | 0 sena routes, 0 dubbletter, alla reaped | Låg | 2–4 h |
| 3 | Budget-wrapper-overhead | Wrapper-overhead är försumbar och idempotensen håller | Wrappa handler med reserve/settle på `invocation_id`; 10k syntetiska tasks | ≤5 % tapp, 0 dubbeldebiteringar | Medel | 0,5–1 dag |
| 4 | Dedup/claims för mikrotasks | Identiska payloads kan återanvändas | Hasha `(handler, payload)`, claim via CAS, återanvänd | Mätbar compute-besparing, 0 dubbel sidoeffekt | Medel | 1–2 dagar |
| 5 | Layer-barrier vs ready-queue | Lagerbarriärer kostar makespan på sneda DAG:ar | 1 lång + 100 korta; jämför lager mot per-task-beredskap | Makespan-förbättring >1,5× | Medel | 1–2 dagar |
| 6 | Offline routing-replay | Ren router kan minska kostnad vid given kvalitet | `provider_selector` över journal/decision-loggar; tröskel som kvantil | Kostnad vid fast kvalitetsgolv | Medel–hög | 2–3 dagar |
| 7 | Feedback-loop | ≥3 observationer per modell/kind ger bättre beslut | Logga utfall, boosta/demota enligt `dsh`-mönstret, offline-replay | Förbättrad träffsäkerhet utan oscillation | Hög | 1–2 veckor |

**Varför ordningen:** 1–3 kräver ingen wire-ändring. 1 och 2 är rena mät-/
säkerhetstester med hög informationsdensitet. 3 stänger budgetgapet på
host-sidan. 4 bygger vidare på SWE:s starkaste befintliga mekanism (claims).
5 kräver prototyp och kan påverka determinismen. 6 kräver märkta utfall.
7 är sist eftersom det förutsätter både routing (6) och loggade utfall.

---

## 12. Källindex (urval)

### SWE på `eff1c7e`

- `python/swe/runtime.py:33` – `swe-budget` inte på runtimevägen.
- `python/swe/runtime.py:190` – `CapacityLimiter(concurrency)`.
- `python/swe/runtime.py:810` – `_settle_invocation` (invocation cleanup, inte pengar).
- `python/swe/runtime_dispatch.py:72–130` – `CapacityLimiter`.
- `python/swe/runtime_dispatch.py:177–187` – `spawn_invocations`-signaturen.
- `python/swe/run_store.py:112–140` – `WorkflowResources` utan budgetfält.
- `python/swe/swd_v1.schema.json:55–65` – `limits` utan budget.
- `docs/specs/swe-runtime.md:91–95` – budget settlement utanför Python-runtime.
- `python/swe/offers.py:272–279` – `OfferBudget`-protokollet.
- `python/swe/offers.py:404–445` – `MemoryOfferBudget` (testdubbel).
- `python/swe/offers.py:551–575` – `ActionOfferHost(budget=...)`.
- `python/swe/offers.py:947,1002–1007` – reserve/settle.
- `crates/swe-budget/src/lib.rs:1–20,156,193,250,325,495` – ledgerinvarianter.
- `crates/swe-native/Cargo.toml` – ingen `swe-budget`-dependency.
- `crates/swe-native/src/python.rs:21–29,59` – PyO3 exporterar `Machine`, inte ledger.
- `crates/swe-native/src/journal.rs:51–59,117–125,280–355` – delivery-klasser och charge-events.
- `crates/swe-native/src/journal.rs:32` – `delivery_key`.
- `crates/swe-reference/Cargo.toml:22` – `swe-budget`-dependency.
- `crates/swe-reference/src/host.rs:18,132–135,329–330,639,646,669,675` – ledger, pause, charge-events.
- `crates/swe-reference/tests/reference.rs:560,1061` – pause/metering-tester.
- `python/swe/cache.py:57,126–159,436–535,571–615,696–753` – exact cache, single-flight, eviction, benchmark.
- `python/swe/microtasks.py:169–279,282–310,406` – DAG → lager → maps, benchmark.
- `docs/specs/swe-microtasks.md:50–52` – resultat injiceras inte.
- `python/swe/runtime_preflight.py:87` – retry avvisas.
- `docs/specs/swe-provider.md:32,61` – ingen fallback.
- `python/tests/test_swe_budget_dispatch_boundary.py` – regressionstestet.

### Externa repos

- `batchagent`: `batch_agent/spec.py:55–56`, `batch_agent/scheduler.py:42–47,50`, `batch_agent/priority_semaphore.py:16–26`, `batch_agent/tools/pool.py:38–57`, `tests/benchmarks/results/prefix_cache_benchmark/results.json`, `tests/benchmarks/results/cost_comparison/results.json`, `README.md:216–218,256–261`.
- `swarm_ai`: `tools/summarize_chunks.py:273–300`, `tools/translate_swarm.py:218–259`, `tools/stigmergy_api.py:34–60,233–250`, `tools/consensus_swarm.py:330–340`, `TODO.md:24`, `results/benchmark_20260306.json`, `results/benchmark_20260813.json`.
- `RouteLLM`: `routellm/controller.py:41–51,139–170`, `routellm/routers/routers.py:41–45,51–253`, `config.example.yaml:9–13`, `routellm/evals/evaluate.py:36,270`, `routellm/calibrate_threshold.py:31–49`, `README.md:14–15`.
- `dsh-swarm-router`: `router.js:60–88`, `index.js:315,324,332`, `ranking.js:44–54`, `store.js:23–45,83–116,136–168`, `benchmark/benchmark_RESULT.json`, `benchmark/benchmark_subagent_RESULT.json`.
- `Agent-LLM-Feeder`: `server/src/services/router.ts:796–812`, `server/src/routes/proxy.ts:345,908,1209–1220`, `server/src/services/ratelimit.ts:8–11,26–79`, `server/src/services/swarmBudget.ts:10–27`, `server/src/routes/agent.ts:388–405`, `server/src/services/searchCache.ts:3–24,28–51`, `server/src/__tests__/`.
- `async_llm`: `python/minisgl/scheduler/async_engine.py:254–280,282–347`, `python/minisgl/llm/async_llm.py:310–330`, `python/minisgl/server/api_server.py:192–207`, `python/minisgl/kvcache/radix_cache.py:101–168`, `tests/core/test_async_llm.py:155–176`.
- `model-router`: `router.py:1–14,127–324`, `modules/classify.py:153–184`, `modules/dispatch.py:18–43,99–141,182–193`, `modules/spend.py:21–100`, `modules/compact.py:36–43,107–141`, `modules/stats_store.py:163–165`, `settings.py:8`.

---

## 13. Slutsats

`kajmund/swe` är arkitektoniskt starkast där determinism och återhämtning
krävs: explicit maskin, journal, replay, exact cache och fail-closed-validering.
Det är också det repo som bäst skiljer “vad som kan ha hänt”, “vad vi vet har
hänt” och “vad som debiterats”.

Budgetledgern är **inte** en oupptäckt bugg i Python-dispatch. Den är en
medvetet separat Rust-crate, dokumenterad som host-policy, med en fungerande
referenshost och en valfri `OfferBudget`-väg för offers. Den verkliga luckan är
att det inte finns någon färdig Python-bindning eller host-wrapper, vilket gör
att en Python-host kan tro att `WorkflowRunner` stoppar vid ett tak när den
inte gör det. Regressionstestet och den föreslagna host-wrappern gör den
gränsen explicit utan att ändra produktionsruntime.

De externa reporna bidrar inte med en bättre kärna, men med kompletterande
mekanismer: batchning och felisolering (`batchagent`, `async_llm`), claims och
stigmergi (`swarm_ai`), ren routing och feedback (`dsh`), samt
capability-gates, budget-guardrails och deterministisk degradering
(`RouteLLM`, `Agent-LLM-Feeder`, `model-router`).
