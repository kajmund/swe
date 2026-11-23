# Barriärfri scheduler: karakterisering och minimalt straggler-experiment

**Status:** offline experiment på isolerad branch
`experiment/barrier-free-scheduler`, baserad på `origin/main`
`eff1c7e8943dd64827fb4916719ced5515bf3db5` (2026-11-22).
**Datum:** 2026-11-24.
**Omfattning:** ingen produktionsändring, ingen SWD-wireändring, ingen
`RuntimeBudget`, inga betalda anrop. PR11 är orörd och fortfarande öppen.

## 1. Fråga

Dagens SWE-mikrotasks kompileras till en `map`-nod per topologiskt lager. Rust-
maskinen är work-conserving **inom** en map, men nästa lager är en ny nod och
startar först när föregående map är helt klar. Hur mycket kostar den
lagerbarriären på en DAG med en straggler, och vilken minskad makespan ger en
event-driven ready queue som behåller dependency-semantik, bounded concurrency,
determinism/replay och cancellation?

## 2. Karakterisering av faktiska kodvägar

### 2.1 Mikrotask-kompileringen

`compile_microtasks` grupperar redo uppgifter i stabila topologiska lager
(`python/swe/microtasks.py:169–279`; `_topological_layers` vid `:282–310`).
Varje lager blir en `map`-nod; `next` pekar på nästa lagers map
(`microtasks.py:228–234`). Beroenderesultat injiceras inte automatiskt i
nedströms handlers (`docs/specs/swe-microtasks.md:50–52`).

### 2.2 Rust-maskinens map/join

- `start_map` läser `map.concurrency` och sätter kapaciteten
  (`crates/swe-native/src/machine/schedule.rs:372–393`).
- `spawn_map_children` startar barn i indataordning och startar nästa barn när
  ett tidigare blir klart; den respekterar `inflight >= map.concurrency`
  (`schedule.rs:396–455`). Det är alltså work-conserving **inom** mappen.
- `complete_map_child` markerar barnet klart, anropar `spawn_map_children` igen
  och avslutar mappen först när `remaining == 0`
  (`crates/swe-native/src/machine/complete.rs:219–256`; `finish_map` vid
  `:310`).
- Barriären är därför **mellan** två map-noder, inte mellan barnen i samma
  map. Det är exakt den yta som ett ready-queue-experiment ska mäta.
- `fork`/`join` har samma mönster: join väntar tills alla owning branches är
  klara (`complete.rs:213`), och `joins.py:14–…` validerar att varje branch
  slutar i rätt join. Join-output är deterministisk per branch-namn.

### 2.3 Bounded concurrency

Två gränser samverkar: Rust-mappens `map.concurrency` och Python-adapterns
`CapacityLimiter` (`python/swe/runtime.py`, `runtime_dispatch.py`). En ready
queue måste behålla båda eller en gemensam gräns; annars kan den överskrida
hostens kapacitet.

### 2.4 Determinism och replay

Invocation-id:n bär run, nod, branch-path, loop-epoch och serial; map-barn
ligger i indexordning. `RunStore` persisterar `InvocationRecord` per invocation,
och `WorkflowRunner.resume` replayar dem utan att anropa handlern igen
(`python/swe/runtime.py:253–323`; `_apply_replay` vid `:499`). En ready queue
måste därför:

- använda stabila tie-breaks (indataordning/index) för start och completion,
- journalföra varje invocation med samma identitet,
- kunna replaya ett resultat från loggen utan att köra om sidoeffekter.

### 2.5 Cancellation

`cancel_event`, deadlines och `InvocationHandle` stoppar ny dispatch och
signalerar pågående arbete; blockering I/O kan inte tvingas stoppa
(`docs/specs/swe-runtime.md`). En ready queue måste sluta admitted nya tasks vid
cancel och markera pågående/aldrig startade tasks entydigt.

### 2.6 Budget

Utanför experimentet. Inget `swe.budget`/`RuntimeBudget` importeras; PR11 är
öppen och kopplas inte in.

## 3. Experiment

### 3.1 Modell

Två rena virtuella-tids-simuleringar över samma statiska DAG:

- **`layer-barrier`** (`barrier_free_scheduler/layer.py`): ett bounded map per
  topologiskt lager, med barriär till nästa lager. Inom lagret startas nästa
  task när en slot blir fri, som Rust-mappen.
- **`ready-queue`** (`barrier_free_scheduler/ready.py`): en task admitted när
  alla dess dependencies är klara, FIFO i indataordning, bounded av samma
  concurrency. Event-loggen innehåller start/finish/cancel.

Båda använder `model.py` för stabila topologiska lager, metrics och
`ScheduleResult`. `replay.py` validerar en eventlogg och rekonstruerar runs;
dependency-ordning och concurrency-gräns kontrolleras.

### 3.2 Straggler-DAG

- `slow`: 120 ms.
- `fast_0..fast_9`: 10 ms, inga beroenden.
- `mid_i`: 10 ms, beror på `fast_i`.
- `final_i`: 10 ms, beror på `mid_i`.
- `final_slow`: 10 ms, beror på `slow`.
- Concurrency 4.

### 3.3 Resultat

`PYTHONPATH=. python3 -m barrier_free_scheduler`:

| Policy | Makespan | Peak | Utilization | Kumulativ startfördröjning |
| --- | ---: | ---: | ---: | ---: |
| `layer-barrier` | 180 ms | 4 | 0.597 | 1400 ms |
| `ready-queue` | 130 ms | 4 | 0.827 | 580 ms |

Ready queue startar `mid_0` vid 30 ms, medan stragglern `slow` fortfarande
kör; layer-baselinen startar den först vid 120 ms. Makespan minskar 180 → 130 ms
(≈28 %), och den kumulativa startfördröjningen 1400 → 580 ms (≈59 %).

Båda resultaten:

- respekterar att en task inte startar före sina dependencies,
- håller `peak_concurrency <= 4`,
- ger ett deterministiskt eventlogg som `replay_result` accepterar,
- ger samma resultat vid upprepad körning.

Cancellationstestet (`ready-queue`, cancel vid 35 ms, fyra 100 ms-tasks,
concurrency 2) visar att inga nya tasks startar efter cancel, att pågående och
aldrig startade tasks markeras cancelled, och att eventloggen kan replayas.

## 4. Vad experimentet inte visar

- Det är en virtuell-tids-simulering, inte den riktiga Rust-maskinen och inte
  riktiga handlare. Resultaten är en övre/lägre uppskattning av
  schemaläggningseffekten, inte en produktionsmätning.
- Ingen faktisk `RunStore`/journal används; replay är en eventloggsvalidering.
  En produktionsvariant måste koppla varje task till samma
  invocation-identitet och journal.
- Ingen budget, ingen provider, inga betalda anrop.
- Ingen dynamisk **insättning** av nya tasks efter start. DAG:en är statisk;
  det som blir dynamiskt är när beroende tasks blir redo. Verklig dynamisk
  tillkomst kräver en host-policy för admission och fairness.
- Ingen multi-tenant-fairness; FIFO i indataordning räcker för den statiska
  DAG:en men kan behöva aging/priority vid dynamisk tillkomst.

## 5. Slutsats och rekommendation

På en DAG med en tydlig straggler och korta beroendekedjor kostar
lagerbarriären mätbart: ready queue ger samma arbete med cirka 28 % kortare
makespan och högre utilizationsgrad, utan att bryta dependency-semantiken eller
concurrency-taket.

En produktionsändring bör **inte** göras direkt i SWD 1.0. Nästa steg är ett
host-level prototypförsök med riktiga handlare, `RunStore`/journal, cancellation
och (efter PR11) budget, jämfört med dagens `compile_microtasks` på samma DAG.
Först därefter är en eventuell ny SWD-nod eller scheduler ett kontraktsbeslut.

## 6. Reproduktion

```sh
cd experiments/barrier-free-scheduler
PYTHONPATH=. python3 -m pytest tests -q
PYTHONPATH=. python3 -m barrier_free_scheduler
```

## 7. Samordning

- `t1bm` (Hy4/PR13): informerad om att experimentet är separat, utan
  PR11/PR13/budget-koppling och utan betalda anrop; deras worktree rördes inte.
- `t1bp` (microtask-swarm): informerad om samma avgränsning; deras
  `experiment/microtask-swarm`-worktree rördes inte.
- PR11 lämnades orörd; inga kosmetiska commits där under Codex-granskning.

## 8. Riktiga offline-handlers via SWE host/runtime

`swe_host/` kör experimentet genom den befintliga `WorkflowRunner`-vägen med
riktiga async-handlers och `MemoryRunStore`; ingen separat produktionsmotor och
ingen budget. Native `swe_native` byggdes från denna branchs `python/`.

Uppmätt på straggler-DAG:en med riktiga `asyncio.sleep`-handlers:

```text
real handlers: layer≈195–198ms ready≈146–147ms layer_peak=4 ready_peak=4
5 passed
```

Den statiska ready queue-varianten är **en enda SWE `map`** med en handler per
task som väntar på host-events för sina dependencies. Det ger per-task
`InvocationRecord` och bounded concurrency, men:

- väntande handlers håller kvar map-slots (effektiv concurrency kan sjunka),
- dependency-grafen är host-state, inte machine-state,
- replay kräver att hosten pre-seedar dependency-events från
  `RunStore.replay_results` innan `resume`; annars skulle en replayed task
  aldrig sätta sin event. Testet med en store som kraschar efter fem records
  visar att resume kan slutföra med pre-seeding, men det är en host-workaround,
- dynamisk tillkomst kan inte lägga till nya map-items. Worker-pool-varianten
  (två fasta map-items som loopar över en host-kö) kör en sent submittad task,
  men journalen ser två worker-invocations, inte fyra task-invocations. Per-task
  journal/replay går därmed förlorad.

Cancellation testades med `cancel_event`: runen blir `cancelled`, pågående
handlers avbryts och inga nya tasks startar. Bounded concurrency testades med
`ActiveMeter`: peak ≤ 4 i båda varianterna.

## 9. Exakt Rust/SWD-förändring som krävs

Den befintliga maskinen kan **inte** uttrycka en ready queue som en
förstaklassmedborgare. `map`-items är oberoende värden; `spawn_map_children`
startar barn i indataordning och känner bara `map.concurrency`
(`crates/swe-native/src/machine/schedule.rs:396–455`). Det finns ingen
per-item-beroendegraf; `finish_map` väntar på `remaining == 0`
(`complete.rs:255–256,310`). En host-workaround kan simulera ready queue, men
inte ge maskinägda beroenden, per-task replay eller dynamisk admission.

Minsta tänkbara förstaklassvariant är att utöka `map`-noden med en ready-mode:

```json
{
  "type": "map",
  "items": "$.input.tasks",
  "child": "work",
  "concurrency": 4,
  "schedule": "ready",
  "item_id": "$.item.task_id",
  "item_depends_on": "$.item.depends_on"
}
```

Alternativet är en ny `dag`-nod med samma semantik. Båda är en SWD-wireändring,
inte en tyst v1-utökning.

Förändringen kräver minst:

1. **Schema och kompilering**
   - `python/swe/swd_v1.schema.json:267–283` (`mapNode`) och
     `python/swe/schema.py:12–14` (`NODE_TYPES`).
   - `python/swe/definition.py`, `graph.py`, `joins.py`: validera unika
     item-id:n, kända beroenden, acyklisk graf och stabil tie-break.
   - `crates/swe-native/src/definition.rs:76–83` (`Node::Map`) och
     `preflight.rs:12`: nya fält/flaggor.
2. **Maskinen**
   - `machine/schedule.rs:372–455`: bygg item-DAG, ready-set och starta endast
     redo items upp till `concurrency`.
   - `machine/complete.rs:219–256,310`: när ett item blir klart, decrementera
     dependents och admission; `finish_map` först när alla items är terminala.
   - `events.rs:13,31,47` och `effects.rs`: item-id/index i events och
     invocation-identitet; `BranchSpawned` för redo items.
   - `journal.rs`: per-item invocation/replay; vid resume ska persistrade
     item-resultat rekonstruera ready-set och admittera dependents, utan host-
     pre-seeding.
3. **Cancellation, budget, fel**
   - Cancel: sluta admittera, avbryta pågående children, terminal status.
   - Budget: varje child-invocation går genom befintlig dispatch/budget (PR11)
     utan ny ledger.
   - Fel i ett item: fail-closed enligt befintlig map/join-semantik.
4. **Kompilator och kontrakt**
   - `python/swe/microtasks.py:169–310` kan emittera en ready-`map` i stället
     för en map per lager; `_topological_layers` behålls för stabil ordning.
   - `contracts/`, `docs/specs/swe-swd-1.0.md`, schema-version och
     kontraktsbeslut måste uppdateras; frysta v1-dokument ska fortsätta fungera.
5. **Dynamisk tillkomst**
   - Kräver ett separat admission-kontrakt (t.ex. en host-effekt/ett event som
     lägger till items i en aktiv DAG) med journalposter och replay. Det ingår
     inte i den minimala statiska ready-mode-förändringen; worker-pool är bara
     ett host-experiment.

Ingen sådan ändring är gjord här. Rekommendationen är att först köra en
host-level prototyp med riktiga handlare, journal/replay, cancellation och
budget på samma DAG, och därefter fatta ett SWD-kontraktsbeslut.

## 10. PR13-preflight

PR13:s offline-preflight är pushad separat som `a69bd50` på
`experiment/agent-microtask-contract`, med `PREFLIGHT.md` och
`preflight-trace.json`. Den rapporterar att lagerbarriären håller och att
handler-input bara innehåller `task_id`, `payload` och `depends_on`; ingen
`RuntimeBudget` och inga betalda anrop. Den branchen och denna branch är
oberoende; ingen ändring gjordes i PR13 eller dess worktree.
