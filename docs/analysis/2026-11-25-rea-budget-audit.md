# REA-rapport och budget-audit

**Status:** uppdaterad på isolerad branch `agent/rea-analysis-takeover`.
**Ursprung:** `experiment/rea-budget-audit` @ `63e7393` (2026-11-23), worktree
`/Users/erikfremred/swe-worktrees/rea-budget-audit` (borttagen efter arbetet;
inga ocommittade filer återfanns, se §1.2).
**Ursprunglig bas:** `origin/main` `eff1c7e` (2026-11-22).
**Aktuell bas:** `origin/main` `ee2132f` (2026-11-25,
"Merge pull request #20 … python-cancel-effect").
**Takeover-worktree:** `/Users/erikfremred/swe-worktrees/rea-analysis`.
**Arbetssätt:** ingen Ollama, inga betalda anrop, ingen riktig
budgetreservation. Alla körningar nedan är offline och använder syntetiska
handlers eller den in-memory-ledger som redan finns i testsviten.

**Syfte:** bevara den unika REA-analysen av sju publika repos, uppdatera
budget-auditen mot nuvarande `main`, och skilja på vad som redan är infört och
vad som fortfarande är en verklig lucka. Den ursprungliga
gränssnittskarakteriseringen (avsaknad av budget i Python-dispatch) är
**obsolet** och porteras inte som den är; se §6.

---

## 0. Sammanfattning

- Alla sju externa repos finns fortfarande på de pinade commits som lästes
  2026-11-23. Ingen extern kod har körts, varken då eller nu.
- `kajmund/swe` har fortfarande starkast determinism, replay, journal och
  verifieringsgrindar. De kvarvarande funktionella luckorna är heterogen
  routing, retry/backoff/rate-limit, semantisk/prefix-cache och inlärning.
- **Budgetfrågan är besvarad och delvis stängd.** På den ursprungliga basen
  `eff1c7e` saknades budget i Python-dispatch. Sedan dess har PR #11 gjort
  budget till en förstaklassresurs (`WorkflowResources.budget`,
  `spawn_invocations(budget=…)`) och exponerat den befintliga Rust-ledgern som
  `swe_budget.Ledger` via PyO3. PR #15 lade ett **experiment** med adaptiva
  tariffer under `experiments/microtask-swarm/`, inte en produktionspolicy.
  Det finns nu 29 insamlade livscykelfall (22 testfunktioner, en
  parametriserad) i `test_swe_budget_runtime.py` och
  `test_swe_budget_binding.py`, plus 3 offline-gränsfall i den nya
  `test_swe_budget_dispatch_boundary.py`.
- Det ursprungliga regressions-testet hade fyra fall som pinade **frånvaron**.
  Vid körning på `ee2132f` gav originalet `3 failed, 1 passed`: tre påståenden
  är strukturellt falska i dag (`WorkflowResources.budget` finns;
  `swe_budget` är importerbart; även dispatch har budget), och det fjärde
  passerar bara när budgeten utelämnas. Testet är därför ersatt av ett
  opt-in-test på samma dispatch-gräns (§6.4).
- Ready-map-kontraktet (PR #16) och agent-kontraktet (PR #17) finns redan i
  `main`. Det finns ingen unik, ocommittad kod kvar i t1bq:s gamla
  ready-dag-worktree, och PR #13:s agent-kontrakt är identiskt med det som
  PR #17 lade på `main` (§1.2–§1.3).
- Två cachefynd från den ursprungliga auditen är fortfarande giltiga:
  warm-cache-benchmarken mäter en enda het nyckel, och specen beskrev FIFO
  medan koden gör LRU. Specen är rättad i denna PR; en deterministisk
  working-set-karakterisering är tillagd.

---

## 1. Proveniens, räddning och samordning

### 1.1 Original och takeover

Det ursprungliga arbetet gjordes av den stoppade Ollama-sessionen `t1bq` i en
egen worktree på `experiment/rea-budget-audit`. Commit `63e7393` innehåller
endast två filer:

```text
docs/analysis/2026-11-23-rea-budget-audit.md      720 rader
python/tests/test_swe_budget_dispatch_boundary.py 123 rader
```

Branchen finns kvar på `origin/experiment/rea-budget-audit`. Den här takeoveren
utgår från `origin/main` `ee2132f` i en ny worktree
`/Users/erikfremred/swe-worktrees/rea-analysis`, så att den gamla worktreen och
andra agenters worktrees inte rörs.

### 1.2 Räddning av de gamla Ollama-worktreena

Före någon städning inventerades `git worktree list`, `git stash list`,
`git fsck --lost-found`, herdr:s terminalregister
(`~/.local/state/herdr-core/default/terminals.json`), Claude Code:s
`file-history-snapshot`-backuper och sessionshistoriken.

| Agent | Worktree | Status vid inventering | Commit/branch | Slutsats |
|---|---|---|---|---|
| `t1bp` Microtask | `/Users/erikfremred/swe-worktrees/microtask-swarm` | **Saknas på disk** | `36ccdf7`, `origin/experiment/microtask-swarm` | Sista `git status --short` var tom; alla `file-history`-backuper matchar commit. Ingen ocommittad förlust. |
| `t1bp` budget-policy | `/Users/erikfremred/swe-worktrees/runtime-budget-policy` | Finns, **ren** | `134b085`, mergad via PR #15 | Inga ocommittade ändringar. |
| `t1bq` REA/budget | `/Users/erikfremred/swe-worktrees/rea-budget-audit` | **Saknas på disk** | `63e7393`, `origin/experiment/rea-budget-audit` | Backuperna för båda filerna är byte-identiska med commit. Unikt material: dokumentet + det obsoleta testet. |
| `t1bq` ready-dag | `/Users/erikfremred/swe-worktrees/ready-dag-contract` | **Saknas på disk** | `1aaf9e0`, mergad via PR #16 | `file-history`-backuperna är tidigare mellanversioner; branchen innehåller senare, kompletterande kod. Ingen unik förlust. |
| `t1br` Laya G3 | `/private/tmp/research-laya-g3-data-lab` | Finns, **ren** | `7c04897`, upstream `origin/experiment/laya-g3-data-lab` | Inga ocommittade ändringar. Stashen i `research` tillhör en annan, äldre branch. |

Det fanns inga `git stash`-poster i `swe`-repot. `git fsck` visade endast
dangling commits som motsvarar redan kända branches (bland annat den
ursprungliga `5071a8b` ready-dag-seamen). En genomsökning av alla listade
dangling blobs fann endast mellanversioner av kända filer (`runtime.py`,
`cache.py`, microtask-swarm, changelog); ingen innehöll unik REA- eller
ready-map-kod. Ingen unik kod behövde därför återställas från förlorade blobs.

**Varför det inte fanns mer att rädda:** både `t1bp` och `t1bq` committade och
pushade sina sista kända tillstånd innan sessionerna stoppades. Det som såg ut
som "förlorat" i ready-dag-worktreen var i själva verket mellanversioner som
senare commits (`a490b31` … `1aaf9e0`) byggde vidare på. Den enda unika,
icke-mergade artefakten är REA-dokumentet och det obsoleta gränssnittstestet.

### 1.3 Vad som redan finns i `main`

- **PR #13** (`experiment/agent-microtask-contract`, stängd utan merge) bar
  127 filer och 20 574 rader, varav större delen var Hy4-experimentet. Dess
  `experiments/agent-contract`-träd är **identiskt** med det som PR #17 lade
  på `main` (`git diff d156145:experiments/agent-contract
  9186c5a:experiments/agent-contract` är tomt). Ingen unik agent-kontraktskod
  gick förlorad.
- **PR #16** (`experiment/ready-dag-contract`) är mergad. `main` har SWD 1.1,
  `python/swe/swd_v1_1.schema.json`, `docs/specs/swe-swd-1.1.md` och det
  godkända beslutet `docs/swe-standalone/18-ready-map-decision.md`.
- **PR #11** är mergad och stänger budget-luckan (§6). **PR #15** är ett
  experiment med adaptiva tariffer under `experiments/microtask-swarm/`, inte
  en produktionspolicy.
- Den tidigare ready-dag-worktreen saknas på disk, men dess innehåll finns i
  `main` via PR #16. Det finns därför inget att återimplementera.

### 1.4 AGENTS.md och arbetssätt

`AGENTS.md` finns i `kajmund/swe` på `ee2132f` (tillagd av PR #18 i `55c4af5`
och länkad i `430678d`). Den är repots delivery-gate, inte en allmän
kodstilsfil:

- **En ren commit:** den granskade träden är exakt en commit.
- **Receipt i PR-body före merge:** base/head-SHA, ren tree, runtime- och
  beroendeversioner, varje test-/lintkommando med exitkod,
  arkitekturgrindar, samt review-terminal, verdict och var rapporten finns.
- **Oberoende DeepSeek-harness-review av samma SHA** i en egen HerDr-terminal
  via `deepseek` på `PATH`; implementeraren och den lokala testkörningen ska
  vara separata från reviewen.
- **Lokal CI:** kör workflowens always-on-jobb på samma SHA; `make test` är
  inte en ersättning. En gate som selector-hoppar är ett avslutat utfall.
- **Branch protection:** läs de checks GitHub faktiskt kräver.

Repots `AGENTS.md` säger också att en icke-mergad diagnostikbranch inte
använder den mellan iterationer. Denna branch är en merge-kandidat, så reglerna
gäller. Den överordnade `/Users/erikfremred/swe-standalone/AGENTS.md`
(Socialism) bidrar med de allmänna kodreglerna: **reuse before new**, **no
fallbacks**, små uppenbara funktioner, validering vid gränser och inga
spekulativa feature flags.

Arbetet har därför inte byggt en ny scheduler, en andra ledger eller ett nytt
testramverk. Det återanvänder `RuntimeBudget`, `swe_budget.Ledger`,
`InMemoryDecisionCache` och befintliga testmönster.

---

## 2. REA-verktyget: status och avgränsning

- Skill installerad: `~/.claude/skills/reverse-engineer-anything/SKILL.md`
  (`metadata.version: "63"`, `tool_count: 263`).
- MCP-servern svarar: `mcp__rea__list_documents` returnerade
  `target_unavailable` med meddelandet "No app is open. Ask the user which app
  to investigate…".
- Ingen `rea`-binär finns på `PATH`; ingen hittades i `~/.local/bin`,
  `/opt/homebrew/bin` eller `~/.cargo/bin`.
- Skillens egen avgränsning: *"Skip REA for ordinary source-repository
  architecture analysis"* (`SKILL.md:3`) och *"For ordinary analysis of a
  complete source repository, use normal repository tools and do not run REA
  readiness or provider commands"* (`SKILL.md:12–15`).
- Slutsats: REA är installerat och MCP-servern lever, men för den här uppgiften
  är REA fel verktyg enligt sin egen dokumentation. Analysen är därför vanlig
  läs-only källkodsanalys. Det är inte ett blockerande fel.

**Utförande:** alla sju repos verifierades med `git ls-remote --symref` och
klonades därefter ytligt (`--depth 1 --single-branch`) till
`/private/tmp/rea-repo-analysis-1795467265/`. Ingen extern repo-kod kördes; inga
`pip install`, `npm install`, tester eller byggen i de externa reporna.
Endast fil-läsning och ofarliga `git log`/`git ls-files`. På 2026-11-25
verifierades att klonerna fortfarande ligger på samma commits (§3).

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
Commit-pins verifierades igen 2026-11-25 utan att någon extern kod kördes.

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
  "A10G 23GB, Qwen/Qwen2.5-7B-Instruct, vLLM 0.6.6.post1".
- **Påstående utan mätning:** kostnadsjämförelsen är blockerad;
  `real_cost_usd: null`, `status: "blocked"` i
  `tests/benchmarks/results/cost_comparison/results.json`.
- README är ärligt: KVFlow "not verified", TokenDance "mock only",
  distribuerad Redis "prototype" (`README.md:216–218,256–261`).
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
- **"Specialist routing" i README är inte implementerad:** inga filer matchar
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
  MMLU-CSV). Rubrikpåståendena "upp till 85 % billigare vid 95 % av GPT-4"
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
  (`store.js:83–116`), och `costEstimate` med "saved vs all-flagship"
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
  skiljer "kan aldrig qualify" från "tillfälligt slut".
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
- Licens: MIT i `LICENSE`, men copyright-raden säger "Tashfeen Ahmed" medan
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
  "Laya"-modell och vid låg konfidens konsulterar en "Jev"-tiebreaker
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
- **Ingen licens alls** – varken LICENSE-fil eller `license`-fält; "all rights
  reserved". README:s Apache 2.0 gäller den externa Laya-modellen, inte repot.
- 0 tester, ingen CI, ingen manifestfil; kräver ocommittad `config.json`
  (`settings.py:8`) och privat `laya`-paket.
- Alla kvantitativa README-siffror (1 606 beslut, 216 gate-checks, 2,25 USD)
  saknar committad data; `stats.jsonl` är untracked.
- Användbara idéer: prefix-hash-validerad återanvändning; append-only
  spend-replay med deterministisk degradering; skip-first circuit breaker.

---

## 5. `kajmund/swe` på `ee2132f`: arkitektur och jämförelse

### 5.1 Verifierad grundarkitektur

- Rust-kärnan `swe-native` äger deterministisk maskin, routing, join-barriärer,
  events och journal. Python-adaptern äger asyncio, deadlines, cancellation,
  handlers och providers (`docs/specs/swe-runtime.md:1–20`).
- Mikrotasks är en authoring-fasad: en statisk DAG kompileras till vanliga SWD
  `map`-noder, ett per topologiskt lager
  (`python/swe/microtasks.py:1–8,169–279`). Med `schedule="ready"` emitteras i
  stället en SWD 1.1 ready-map (`python/swe/microtasks.py:6,158,188–268`;
  `python/swe/swd_v1_1.schema.json`; `docs/specs/swe-swd-1.1.md`).
- Exakt beslutscache: identitetsnyckel över provider, prompt, input, choices
  och params; `auth_scope`, `policy_version` och `model` krävs i params och
  fail-closed (`python/swe/cache.py:57,126–159`).
- `ActionOffer` är en host-layer med persistent `APPROVING`-claim, lease,
  takeover via compare-and-set och valfri `OfferBudget`
  (`python/swe/offers.py:272–279,551–575,732–856,947,1002–1007`).
- Journalen skiljer exekvering, bekräftelse och debitering; delivery-klasser
  avgör recovery (`crates/swe-native/src/journal.rs:1–90`). PR #18/#20 lade
  till lokal delivery-receipt och replaysäker hantering av okända externa
  effekter (`python/swe/effect_journal.py`, `python/swe/runtime_dispatch.py`).
- Budget är nu en opt-in förstaklassresurs: `WorkflowResources.budget`
  (`python/swe/run_store.py:126–172`), `spawn_invocations(budget=…,
  budget_slots=…, run_id=…)` (`python/swe/runtime_dispatch.py:198–200`), och
  `RuntimeBudget` ovanpå den befintliga Rust-ledgern via
  `swe_budget.Ledger` (`python/swe/budget.py:159–780`,
  `crates/swe-budget-py/src/lib.rs`). Kontraktet är dokumenterat i
  `docs/specs/swe-budget-runtime.md` och `docs/swe-standalone/17-budget-runtime-decision.md`.

### 5.2 Jämförelsetabell

| Dimension | `kajmund/swe` (`ee2132f`) | Starkaste externa referens | Bedömning |
|---|---|---|---|
| Atomiska mikrotasks | Statisk DAG → topologiska lager eller opt-in `schedule="ready"` (SWD 1.1) (`microtasks.py:169–268`) | `batchagent`, `swarm_ai` | SWE starkast på determinism/replay; ready-map stänger det värsta barrier-gapet |
| Async parallellism | asyncio + `CapacityLimiter` + Rust `map.concurrency` (`runtime.py:223`, `runtime_dispatch.py:83`) | `async_llm`, `batchagent` | Två gränser och bounded fan-out; ingen batchning, prioritet eller adaptivitet |
| Heterogen routing | Ingen; en namngiven provider per beslut, ingen fallback (`docs/specs/swe-provider.md:32,64`) | `RouteLLM`, `Agent-LLM-Feeder`, `model-router`, `dsh` | Största kvarvarande funktionella gapet |
| Stigmergi/delat tillstånd | `OfferStore`-claim/lease/CAS; `RunStore`/journal (`offers.py:732–856`) | `swarm_ai`, `async_llm` | Durable claims, inte stigmergi/avdunstning |
| Cancellation | `cancel_event`, action-timeouts, `InvocationHandle`, bounded abandoned registry, effect-journal (`runtime.py:28–36`, `invocation_handle.py`, PR #20) | `async_llm` | Explicit och testat, men kooperativt |
| Budget ledger | Opt-in `RuntimeBudget` + `swe_budget.Ledger` i dispatch (`budget.py`, `runtime_dispatch.py:198–200,346–371,406–551`) | `model-router`, `Agent-LLM-Feeder` | Stark ledger och nu även Python-koppling; 29 livscykelfall + 3 gränsfall |
| Cache reuse | Exakt identitetscache, single-flight, bounded L1 (`cache.py:436–535,571–615`) | `async_llm`, `model-router` | Stark exact identity; ingen semantisk/prefix-cache |
| Oberoende verifiering | Contract freeze 38 fall, mutation 32 fall, license gate OK (körda 2026-11-25) | `Agent-LLM-Feeder`, `async_llm` | Starkast sammantaget; cachebenchmarkens working-set-mått är svagt |
| Lärande | Ingen | `RouteLLM`, `Agent-LLM-Feeder`, `dsh` | Saknas helt |

### 5.3 Egna fynd i SWE

**A. Warm-cache-testet mäter en enda nyckel.**
`benchmark_exact_cache` skapar nycklar med
`input_data={"ordinal": index if unique else 0}` (`cache.py:724`). I warm-fasen
anropas `run_wave(unique=False)` (`cache.py:751–754`), så alla 10 000 anrop får
samma nyckel. Testet mäter 10 000 träffar på en nyckel, inte återanvändning
över en arbetsmängd. Före denna PR sade specen "10,000 cold unique resolves
plus 10,000 warm hits" (`origin/main:docs/specs/swe-exact-cache.md:54–59`);
denna PR ändrar texten till "hot-key hits" och dokumenterar begränsningen.
Tekniskt sant, men
missvisande: med `max_entries=256` och 10 000 unika nycklar är en andra
sekventiell genomgång i praktiken en miss-serie. Fyndet kvarstår och är nu
explicit dokumenterat; `python/tests/test_swe_cache_working_set.py`
karakteriserar skillnaden (fit → full reuse; sekventiell working set > L1 → noll
warm-träffar).

**B. Specen sade FIFO, koden gör LRU.**
Specen sade "FIFO-by-`(inserted_at, key)` eviction"
(`docs/specs/swe-exact-cache.md:32` före denna PR). Koden väljer offer med
`min(..., key=lambda entry: (entry.last_access_ms, entry.inserted_at_ms,
entry.key))` (`cache.py:597`), och `_fresh` uppdaterar `last_access_ms` vid
träff (`cache.py:579`). Det är LRU, inte FIFO. Beteendet är rimligt och
`python/tests/test_swe_cache.py::test_hits_refresh_lru_eviction_order` pinar
det. Specen är rättad i denna PR till LRU med samma nyckelordning.

**C. Lagerbarriärer och utebliven resultatinjektion.**
Standardvägen är fortfarande ett `map` per topologiskt lager
(`microtasks.py:169–279`), så en långsam uppgift i lager N blockerar hela lager
N+1. PR #16 lade till opt-in `schedule="ready"`, som tar bort barriären för
statiska beroenden. Beroenderesultat injiceras fortfarande inte automatiskt i
nedströms handlers (`docs/specs/swe-microtasks.md:50–52`). Bra för determinism,
men agentpipelines måste fortfarande bära värden via hosten.

**D. Ingen retry/backoff/rate-limit/fallback.**
`retry` avvisas i preflight (`python/swe/runtime_preflight.py:87`;
`crates/swe-native/src/preflight.rs:52–55`). Provider-dokumentationen säger att
ingen provider-, prompt- eller modell-fallback finns
(`docs/specs/swe-provider.md:32,64`). Cancellation är kooperativ.

**E. Verifieringsgrindarna är starka men dokumentationen driftar.**
Körda 2026-11-25 på `ee2132f`:
- `python3 scripts/test_check_mutations.py` → **32 cases, 0 failed**.
- `python3 scripts/test_check_contract_freeze.py` → **38 cases, 0 failed**.
- `PATH="$HOME/.cargo/bin:$PATH" python3 scripts/check_licenses.py` →
  **license gate OK** (41 Rust- och 14 Python-beroenden).
Dokumenten säger fortfarande "30 cases, 0 failed"
(`docs/swe-standalone/14-mutation-gate.md:53`), men självtesten har 32. Fyndet
kvarstår; det är en dokumentationsdrift, inte en grind som saknas.

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
  (`benchmarks/offers-cache/independent-10000.json`). Den varma fasen är
  hot-key-throughput, inte working-set-reuse (fynd A).
- Båda använder syntetiska no-op-handlers (`asyncio.sleep(0)`); de mäter
  scheduler-/cache-overhead, inte LLM-latens eller kostnad.

---

## 6. Budget-auditen, omprövad

### 6.1 Frågan och svaret 2026-11-23

Frågan var: finns `swe-budget`-ledgern i den vanliga Python-vägen
`WorkflowRunner` → `runtime_dispatch.spawn_invocations` → handler/provider?

På basen `eff1c7e` var svaret **nej**:

- `WorkflowResources` hade inget budgetfält.
- `spawn_invocations` hade inget budgetargument.
- `swe-budget` var en Rust-only crate utan PyO3-yta.
- Endast Rust-referenshosten och `OfferBudget` använde ledgern.
- Det var dokumenterad host-policy, inte ett SWD 1.0-kontraktsbrott.

Det ursprungliga testet `test_swe_budget_dispatch_boundary.py` pinade denna
frånvaro i fyra fall.

### 6.2 Vad som ändrades i `main`

- **PR #11** (merge `252620b`): budget som förstaklassresurs.
  `WorkflowResources.budget: RuntimeBudget | None`;
  `spawn_invocations(..., budget=…, budget_slots=…, run_id=…)`;
  `swe.budget.RuntimeBudget` ovanpå `swe_budget.Ledger`
  (`crates/swe-budget-py`). Beslutet finns i
  `docs/swe-standalone/17-budget-runtime-decision.md`.
- **PR #15** (merge `85fe8a7`): ett **experiment** under
  `experiments/microtask-swarm/` som kopplar adaptiva tariffer genom
  `RuntimeBudget` och den befintliga Rust-ledgern. Det ändrar inte
  `python/swe/` och är ingen produktionspolicy; en produktionshost måste
  fortfarande leverera sin egen `BudgetPolicy`.
- Normativ dokumentation: `docs/specs/swe-budget-runtime.md` och den
  uppdaterade `docs/specs/swe-runtime.md:91–102`.

### 6.3 Nuvarande belägg

- Dispatch-ytan: `python/swe/runtime_dispatch.py:198–200` (argumenten),
  `:346–371` (settle/release/mark_pending), `:406–551` (reserve före handler
  och provider).
- Resursen: `python/swe/run_store.py:126–172`.
- Livscykeln: `python/swe/budget.py:159–780` (`RuntimeBudget`, `reserve`,
  `settle`, `release`, `mark_pending`, `reconcile`, `restore`) och
  `NativeBudgetLedger` (`:710–770`) som lazy-importerar `swe_budget`.
- PyO3-ytan: `crates/swe-budget-py/src/lib.rs` (`swe_budget.Ledger`).
- Tester: `python/tests/test_swe_budget_runtime.py` (22 testfunktioner,
  24 insamlade fall; en är parametriserad) och
  `python/tests/test_swe_budget_binding.py` (5 fall) täcker reserve-före-
  dispatch, tak som failar closed, cancel som håller reservationen pending,
  fel som settle:ar noll, crash/replay utan dubbeldebitering, provider/cache,
  overrun, stale snapshots och bounded terminal retention.
- Opt-in-garantin: utan `WorkflowResources.budget` sker ingen reservation och
  inga nya events (`docs/specs/swe-budget-runtime.md`, "When the resource is
  absent").

### 6.4 De fyra ursprungliga testfallen

| Ursprungligt fall | Påstod 2026-11-23 | Status på `ee2132f` |
|---|---|---|
| `test_dispatch_runs_paid_handler_without_consulting_host_budget` | Dispatch kan inte ta emot en ledger; en host-budget med tak 0 konsulteras aldrig | Passerar bara om man *utelämnar* budget; påståendet "det finns ingen parameter" är falskt |
| `test_workflow_resources_and_dispatch_expose_no_budget_hook` | Varken resursen eller dispatch har budget/ledger | **Falskt:** båda har `budget` |
| `test_core_ledger_is_not_exposed_as_a_python_module` | `swe_budget` går inte att importera | **Falskt:** `crates/swe-budget-py` bygger `swe_budget` |
| `test_offer_path_has_an_optional_budget_but_dispatch_does_not` | Endast `ActionOfferHost` har budget | **Falskt:** även dispatch har budget |

Testet porteras därför **inte**. I stället finns
`python/tests/test_swe_budget_dispatch_boundary.py` med samma filnamn och en
ny, korrekt karakterisering:

- utan budget får dispatch ingen ledger, `budget_slots`-listan för
  invocationen är tom (ingen reservation appendas) och handlern körs;
- med budget reserveras före handleranropet, handlern ser holden, och dispatch
  settle:ar exakt en gång;
- `WorkflowResources().budget` är `None` som standard och
  `spawn_invocations` har `budget`/`budget_slots`.

Testet använder en liten in-memory `BudgetLedger`-implementation och kräver
varken `swe_native` eller `swe_budget`. Det gör inga betalda anrop.

### 6.5 Återstående risk

Den verkliga risken är nu inte "runtime saknar budget" utan "hosten glömmer
att skicka `WorkflowResources.budget`". Det är dokumenterat opt-in-beteende,
inte en bugg: frånvaro ska bevara det gamla beteendet exakt. En host som vill
ha enforcement måste:

1. konstruera en `RuntimeBudget` med en policy och ett `swe_budget.Ledger`,
2. skicka den via `WorkflowResources.budget`, och
3. reconcilierea pending holds vid cancel/timeout.

Den gamla rekommendationen om en separat host-wrapper är därmed infriad: det
är exakt vad `RuntimeBudget` är. Ingen andra ledger behövs. Om enforcement ska
bli en kärngaranti i SWD krävs en separat kontraktsändring; det är en
produktpolicy, inte en bugfix.

---

## 7. Reproduktion och resultat

### 7.1 Nya tester i denna PR (offline)

```sh
cd python
PYTHONPATH=. uv run --no-project \
  --with pytest --with pytest-asyncio --with jsonschema --with pyyaml \
  python -m pytest \
  tests/test_swe_budget_dispatch_boundary.py \
  tests/test_swe_cache_working_set.py -q -o asyncio_mode=auto
```

Resultat 2026-11-25:

```text
.....                                                                    [100%]
5 passed
```

Hela Python-sviten på samma bas (med byggda native-wheels) gav
`390 passed`.

### 7.2 Befintlig budget-svit (kräver byggda native-wheels)

```sh
cd python
uv sync --locked --group dev
cd ..
uvx maturin build --manifest-path crates/swe-budget-py/Cargo.toml \
  --interpreter python/.venv/bin/python --out /tmp/swe-budget-wheel
uvx maturin build --manifest-path crates/swe-native/Cargo.toml \
  --interpreter python/.venv/bin/python --out /tmp/swe-native-wheel
cd python
uv pip install --reinstall /tmp/swe-budget-wheel/*.whl /tmp/swe-native-wheel/*.whl
uv run --no-sync pytest tests/test_swe_budget_runtime.py \
  tests/test_swe_budget_binding.py -q
```

Resultat 2026-11-25: `29 passed`. Den nya gränsfilen körs separat i §7.1.

Detta är samma väg som CI:s Python-jobb använder. Inga betalda anrop.

### 7.3 Grindar på `ee2132f` (körda 2026-11-25)

```text
python3 scripts/test_check_mutations.py        -> 32 cases, 0 failed
python3 scripts/test_check_contract_freeze.py  -> 38 cases, 0 failed
python3 scripts/check_licenses.py              -> license gate OK
```

### 7.4 Historiskt resultat på `eff1c7e`

Det ursprungliga testet kördes på den gamla basen och gav `4 passed`. Det
resultatet är korrekt för dåvarande kod men är inte längre ett mått på
nuvarande beteende; se §6.4.

---

## 8. Vad som återstår

Följande ska **inte** återimplementeras; det finns redan i `main`:

- budget som opt-in runtime-resurs (PR #11; PR #15 är ett experiment),
- SWD 1.1 ready-map (PR #16),
- agent-kontraktet (PR #17),
- delivery-receipt/effect-journal (PR #18/#20).

Kvarvarande arbete, i prioritetsordning, är:

1. **Realistisk cachebenchmark.** Den befintliga varma fasen mäter en het
   nyckel. En working-set-benchmark med Zipf-fördelning och flera storlekar
   (64/256/1024/10000) skulle visa var 256-gränsen biter. Denna PR lägger en
   deterministisk karakterisering, inte hela benchmarken.
2. **Heterogen routing.** SWE har fortfarande en namngiven provider per beslut
   och ingen fallback. En ren router med capability-gates (dsh/Agent-LLM-
   Feeder) är den största funktionella luckan.
3. **Retry/backoff/rate-limit.** `retry` avvisas uttryckligen. En framtida
   policy måste hantera idempotens, budget och journal tillsammans.
4. **Semantisk/prefix-cache.** Exact identity är stark, men det finns ingen
   återanvändning över liknande prompts.
5. **Inlärning/feedback.** Kräver både routing och loggade utfall; sist.

---

## 9. Licenser och återanvändning

| Repo | Licens | Får kod återanvändas? | Kommentar |
|---|---|---|---|
| `kajmund/swe` | MIT OR Apache-2.0 | Ja | 41 Rust- + 14 Python-beroenden permissiva enligt license gate |
| `batchagent` | MIT | Ja, med attribution | LICENSE + metadata tydliga |
| `swarm_ai` | MIT | Ja, med attribution | `THIRD_PARTY_LICENSES.md` är selektiv, inte transitiv |
| `RouteLLM` | Apache-2.0 | Ja, med NOTICE/attribution | Modellvikterna ligger externt på HF; deras villkor är **UNKNOWN** |
| `dsh-swarm-router` | MIT | Ja, med attribution | Inga tester; granska före kopiering |
| `Agent-LLM-Feeder` | MIT | Ja, men verifiera härstamning | LICENSE säger "Tashfeen Ahmed", inte repoägaren; bygger på FreeLLMAPI |
| `async_llm` | MIT | Ja, med attribution | LICENSE säger "sgl-project"; härstamning från Mini-SGLang |
| `model-router` | **Ingen** | **Nej** | "All rights reserved"; endast idéer, ingen kod |

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
- Det uppdaterade dispatch-testet använder en in-memory testledger; det bevisar
  dispatchgränsen, inte den riktiga `swe_budget`-ledgerns lagringsdurabilitet.
  Den riktiga bindningen täcks av `test_swe_budget_binding.py` i CI.
- `cargo test -p swe-reference` kördes inte om i denna takeover; den
  ursprungliga körningen (30 passed på `eff1c7e`) kvarstår som historiskt
  belägg.
- Inga betalda anrop gjordes och ingen riktig budgetreservation skapades.

---

## 11. Prioriterade experiment (per dollar och sekund)

Kostnad och tid är **uppskattningar**.

| Prio | Experiment | Hypotes | Metod | Framgångskriterium | Kostnad | Väg­tid |
|---:|---|---|---|---|---|---|
| 1 | Realistisk cachebenchmark | Nuvarande warm-test överskattar återanvändning | Arbetsmängder 64/256/1024/10000 med Zipf α=0,8/1,0; mät hit rate, p95, evictions | Hit-rate-kurva som visar var 256-gränsen biter | Låg | 1–2 h |
| 2 | Dedup/claims för mikrotasks | Identiska payloads kan återanvändas | Hasha `(handler, payload)`, claim via CAS, återanvänd | Mätbar compute-besparing, 0 dubbel sidoeffekt | Medel | 1–2 dagar |
| 3 | Offline routing-replay | Ren router kan minska kostnad vid given kvalitet | `provider_selector` över journal/decision-loggar; tröskel som kvantil | Kostnad vid fast kvalitetsgolv | Medel–hög | 2–3 dagar |
| 4 | Feedback-loop | ≥3 observationer per modell/kind ger bättre beslut | Logga utfall, boosta/demota enligt `dsh`-mönstret, offline-replay | Förbättrad träffsäkerhet utan oscillation | Hög | 1–2 veckor |

**Varför ordningen:** 1 är ren mätning och kräver ingen wire-ändring. 2 bygger
vidare på SWE:s starkaste befintliga mekanism (claims). 3 kräver märkta utfall.
4 är sist eftersom det förutsätter både routing och loggade utfall.
Budget-wrapper (tidigare prio 3) och ready-queue (tidigare prio 5) är redan
införda och står därför inte kvar.

---

## 12. Källindex (urval)

### SWE på `ee2132f`

- `python/swe/run_store.py:126–172` – `WorkflowResources`, opt-in `budget`.
- `python/swe/runtime_dispatch.py:198–200` – budgetargumenten.
- `python/swe/runtime_dispatch.py:346–371,406–551` – settle/release/
  mark_pending och reserve före handler/provider.
- `python/swe/budget.py:159–780` – `RuntimeBudget`-livscykeln.
- `python/swe/budget.py:710–770` – `NativeBudgetLedger` (lazy `swe_budget`).
- `crates/swe-budget-py/src/lib.rs` – PyO3-ytan `swe_budget.Ledger`.
- `crates/swe-budget/src/lib.rs` – ledgerinvarianter.
- `docs/specs/swe-budget-runtime.md` – normativt budgetkontrakt.
- `docs/specs/swe-runtime.md:91–102` – budget på Python-runtimevägen.
- `docs/swe-standalone/17-budget-runtime-decision.md` – beslutet.
- `docs/swe-standalone/18-ready-map-decision.md` – SWD 1.1-beslutet.
- `python/swe/microtasks.py:6,158,188–268` – `schedule="ready"`.
- `python/swe/swd_v1_1.schema.json`, `docs/specs/swe-swd-1.1.md` – SWD 1.1.
- `experiments/agent-contract/` – agent-kontraktet från PR #17.
- `python/swe/cache.py:57,126–159,436–535,571–615,697–753` – exact cache,
  single-flight, LRU-eviction och benchmark.
- `python/swe/cache.py:579,597` – LRU-ordningen.
- `docs/specs/swe-exact-cache.md:32,52–62` – LRU och hot-key-benchmarken.
- `python/swe/microtasks.py:169–279` – DAG → lager.
- `docs/specs/swe-microtasks.md:50–52` – resultat injiceras inte.
- `python/swe/runtime_preflight.py:87` – retry avvisas.
- `docs/specs/swe-provider.md:32,64` – ingen fallback.
- `python/swe/offers.py:272–279,551–575,732–856,947,1002–1007` – `OfferBudget`
  och claims.
- `crates/swe-native/src/journal.rs:46–60,117–125,280–355` – delivery-klasser
  och charge-events.
- `crates/swe-reference/Cargo.toml`, `src/host.rs` – Rust-referenshostens
  ledger-väg.
- `python/tests/test_swe_budget_runtime.py` (22 fall) och
  `python/tests/test_swe_budget_binding.py` (5 fall) – budgetlivscykeln.
- `python/tests/test_swe_budget_dispatch_boundary.py` – opt-in-gränsen.
- `python/tests/test_swe_cache_working_set.py` – working-set-karakterisering.
- `python/tests/test_swe_cache.py::test_hits_refresh_lru_eviction_order` –
  LRU-beviset.

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
Det är också det repo som bäst skiljer "vad som kan ha hänt", "vad vi vet har
hänt" och "vad som debiterats".

Budgetledgern är inte längre en oinkopplad Rust-crate på Python-vägen. Sedan
PR #11 är den en opt-in förstaklassresurs med samma Rust-ledger, en
dokumenterad livscykel och 29 livscykelfall plus 3 gränsfall. PR #15 är ett
experiment med adaptiva tariffer, inte en produktionspolicy. Den ursprungliga auditens
slutsats var korrekt på sin bas men är nu historisk; det som återstår är
hostens ansvar att faktiskt skicka resursen och att reconcilierea pending
holds.

De externa reporna bidrar inte med en bättre kärna, men med kompletterande
mekanismer: batchning och felisolering (`batchagent`, `async_llm`), claims och
stigmergi (`swarm_ai`), ren routing och feedback (`dsh`), samt
capability-gates, budget-guardrails och deterministisk degradering
(`RouteLLM`, `Agent-LLM-Feeder`, `model-router`). Den största kvarvarande
funktionella luckan är heterogen routing; den största mätluckan är en
realistisk cachebenchmark.
