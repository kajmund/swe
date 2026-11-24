# 18 — Kontraktsbeslut: statisk ready-map i SWD

**Status:** **Godkänt för implementation 2026-11-24.** SWD 1.0 förblir fryst.
Kontraktsändring: SWD 1.1 lägger till opt-in `schedule: "ready"` på befintlig `map`. Journal v1, budget och värdekanal är oförändrade. Dynamisk admission ingår inte.
Det följer `contracts/README.md` (“Changing a contract”) och
`scripts/check_contract_freeze.py`: en ändrad SWD-yta kräver versionsbump och
ett dokumenterat beslut — ingen tyst kontraktsändring.

**Datum:** 2026-11-24.
**Bas:** `origin/main` `252620be31a4bf0d2c1f451d3ce2137a2b158fc1`
(2026-11-24, PR #11 mergad av ägaren; jag har inte mergat den).
**Underlag:** PR #14 `experiment/barrier-free-scheduler` (offline
simulering + riktiga SWE-handlers), PR13-preflighten `a69bd50` och
PR13-seamen `5071a8b` (`experiments/agent-contract/SEAM.md`), separata
branches utan budget eller betalda anrop.

## 1. Fråga

Kan SWE:s befintliga `map`-nod få en minimal, förstaklassig **statisk
ready-DAG** utan att införa en parallell scheduler eller ett nytt DAG-kontrakt?
Om ja: vilken är den minsta SWD-versionerade ändringen, och vilka invarianter
måste gälla för item-ID:n, beroenden, invocation-identitet, journal/replay,
cancellation och concurrency?

## 2. Varför en wire-ändring krävs

SWD 1.0 är fryst (`docs/specs/swe-swd-1.0.md:1–3`), och `map` är en bounded
fan-out över en JSON-array (`docs/specs/swe-swd-1.0.md:28–35`). Items är
oberoende värden. I maskinen startas barn i indataordning och admission styrs
bara av `map.concurrency` (`crates/swe-native/src/machine/schedule.rs:396–455`);
`finish_map` väntar på `remaining == 0` (`complete.rs:255–256,310`). Det finns
ingen per-item-beroendegraf.

PR #14 visar att en host-workaround kan simulera en ready queue, men den har
tre brister som inte kan lösas i hosten utan att duplicera motorns semantik:

1. väntande handlers håller kvar map-slots, så effektiv concurrency sjunker;
2. dependency-grafen är host-state, inte machine-state; replay kräver att
   hosten pre-seedar events från `RunStore.replay_results`;
3. dynamiska tasks tappar per-task-journal (worker-pool ger en invocation per
   worker, inte per task).

En första klassens lösning måste ägas av maskinen och journalen. Eftersom
`map`-noden då får nya fält krävs en SWD-version. Det finns inget sätt att
uttrycka detta i oförändrad SWD 1.0.

## 3. Beslut

**Utöka den befintliga `map`-noden med en opt-in `schedule: "ready"`-mode i
SWD 1.1. Inför ingen ny nodtyp, ingen ny scheduler och inget nytt DAG-kontrakt.**

Detta följer den normativa principen “No new scheduler, DAG model …” i
`docs/swe-standalone/04-public-contracts.md:36–40`: samma `map`, samma
effect/completion-protokoll, samma `InvocationRecord`, samma journal.
Skillnaden är att `spawn_map_children` får en item-beroendegraf som styr
admission i stället för enbart indataordning.

### 3.1 Föreslagen schemaform (SWD 1.1)

```json
{
  "type": "map",
  "items": "$.input.tasks",
  "child": "work",
  "concurrency": 4,
  "schedule": "ready",
  "item_id": "$.item.task_id",
  "item_depends_on": "$.item.depends_on",
  "next": "done"
}
```

- `schedule`: `"ordered"` (default, exakt dagens map) eller `"ready"`.
- `item_id`: JSON-path inom varje item till ett stabilt, icke-tomt sträng-ID.
  Mönster: `[A-Za-z][A-Za-z0-9_.-]{0,127}`; unikt inom mapen.
- `item_depends_on`: JSON-path till en array av item-ID:n. Kända ID:n, inga
  självberoenden, acyklisk graf.
- Fälten är **förbjudna** när `schedule` är `ordered` (fail closed), och
  **obligatoriska** när `schedule` är `ready`.
- `items` måste vid körning vara en array av JSON-objekt; annars typat fel före
  första barn-invocationen.
- `join`/`next` behåller dagens map/join-semantik; `finish_map` sker när alla
  items är terminala.

`python/swe/swd_v1.schema.json:268–283` är den frysta 1.0-formen. 1.1 får en
egen schemafil; 1.0-filen ändras inte.

### 3.2 Semantik

1. **Validering före första spawn.** Vid `start_map` läses item-arrayen och
   bygger en item-DAG. Fail closed vid:
   - item som inte är objekt,
   - saknat/ogiltigt/duplicerat item-ID,
   - okänt beroende,
   - självberoende,
   - cykel.
   Inga child-invocationer och inga journalposter för barn får ske före
   valideringen är klar.
2. **Admission.** Initialt är items utan beroenden redo. När ett item blir
   klart markeras det done; dess dependents decrementeras och blir redo när alla
   beroenden är klara. Minst ett redo item startas om en slot finns, upp till
   `map.concurrency`. Stabil tie-break: itemets indataindex, därefter item-ID.
   Ingen starvation i den statiska DAG:en.
3. **Completion.** `remaining == 0` → `finish_map`; output ordnas i
   indataordning, exakt som `map` i dag. `join`/`next` oförändrat.
4. **Invocation-identitet.** Varje item är en vanlig child-invocation.
   `branch_path` utökas med item-ID:t (säkert mönster enligt ovan) utöver
   index, så identiteten är stabil över replay och inte bara positionsbaserad.
   Den publika formen `<run>:<node>:<branch_path>:<loop_epoch>:<serial>`
   behålls (`docs/swe-standalone/04-public-contracts.md`, avsnitt 1.3).
5. **Journal/replay.** Varje item får en `InvocationRecord` som i dag. Vid
   `resume` rekonstruerar maskinen item-DAG:en och markerar persistrade item
   som klara innan den fortsätter; dependents admitteras utan host-pre-seeding.
   Journal v1 behåller sina event-typer; item-ID/index får ligga i det redan
   otypade event-payloadet. Om en ny event-typ skulle behövas är det en separat
   journal-kontraktsändring och ingår inte här.
6. **Cancellation.** Vid `cancel_event`/deadline: sluta admittera nya items,
   avbryt pågående children enligt befintlig `CancelInvocation`-väg, terminal
   `cancelled`. Inga nya item startar efter cancel.
7. **Concurrency.** Samma `concurrency`-tak som `map`; både Rust-mappen och
   Python-`CapacityLimiter` behålls. Peak får aldrig överstiga taket.
8. **Budget.** Varje child är en vanlig invocation; efter PR #11 gäller
   `WorkflowResources.budget` oförändrat per child. Ready-mapen inför ingen
   ledger, ingen ny charge-typ och ingen budgetändring.
9. **Fel.** Ett child-fel följer befintlig map/join-semantik (fail closed,
   terminal status, inga nya admissioner). Ett valideringsfel före start är ett
   typat preflight-/limit-fel.

### 3.3 Backward compatibility och version

- `SWD_VERSION = "1.0"` (`python/swe/schema.py:11`) och
  `python/swe/swd_v1.schema.json` förblir frysta; alla 1.0-dokument fungerar
  exakt som i dag.
- Ny `SWD_VERSION_1_1 = "1.1"` och `python/swe/swd_v1_1.schema.json` med
  `version` const `"1.1"`. `load_schema`/`compile_definition` accepterar båda
  versionerna och väljer matchande schema; okänd version är ett typat fel
  (`python/swe/definition.py:93–98`).
- `swd_v1_1.schema.json` lägger till de tre fälten i `mapNode` med
  `if/then`-villkor; `ordered`-dokument är oförändrade.
- `contracts/` får en ny 1.1 SWD-fixture + `MANIFEST.sha256`; v1.0-fixtures
  ändras inte. `scripts/check_contract_freeze.py` får en version-mapping för
  den nya schemats `version`-const, i samma mönster som
  `("python/swe/swd_v1.schema.json", "/properties/version/const")` →
  `SWD_VERSION` (`check_contract_freeze.py:241`).
- Journal v1 ändras inte; om item-metadata kräver en ny event-typ ska det
  hanteras som ett separat journal-beslut.

### 3.4 PR13-seam och värdekanal

PR13 har en separat integrationsseam i `experiments/agent-contract/SEAM.md`
vid `5071a8b`. Den låser **samma tre fältnamn** som detta förslag:
`schedule`, `item_id`, `item_depends_on`. Seamen:

- har `ready_ids` som enbart predikatet “alla beroenden är klara”, i
  indataordning; den har ingen kö, ingen klocka och inget concurrency-tak,
- importerar eller kopierar inte scheduler-experimentet,
- bevarar vittnet: `after_fast` är eligible när `fast` är klar medan `slow`
  fortfarande är öppen, medan `layer_is_complete(("slow","fast"), {"fast"})`
  är false — det är just lagerbarriären,
- kopplar inte in `RuntimeBudget`; PR #11 är mergad på main som `252620b`, men
  PR13 håller budgeten okopplad.

**Värdekanalen är uttryckligen `absent`.** `item_depends_on` styr endast
admission. Den injicerar inte generationens output i validatorns handler-input;
den uppmätta handler-inputen är fortfarande `task_id`, `payload` och
`depends_on`. Detta förslag stänger alltså **inte** värdekanalen. Om en
validator behöver generationens output krävs ett separat value-channel-kontrakt
(exempelvis en explicit output-binding eller ett resultatflöde), med egen
kontraktsändring. Det får inte smygas in i `item_depends_on`.

Seamens `tests/test_seam.py` är ett acceptansvittne: en framtida scheduler ska
göra samma `ready_ids`-predikat sant för samma items och ge varje admitted task
sin egen invocation-id. Att ersätta predikatet med en lagerwait ska fälla
testet.

## 4. Implementation plan (efter godkännande)

### 4.1 Python

- `python/swe/swd_v1_1.schema.json`: mapNode-fälten + `if/then`-validering.
- `python/swe/schema.py`: `SWD_VERSION_1_1`, version→schema-map, `NODE_TYPES`
  oförändrad (`map` finns redan).
- `python/swe/definition.py`: acceptera 1.0/1.1 och välj schema; behåll
  fail-closed för okänd version.
- `python/swe/graph.py`/`joins.py`: oförändrad map/join-ägarskap; ready-mode
  påverkar inte join-strukturen.
- `python/swe/microtasks.py`: opt-in `schedule="ready"` som emitterar en enda
  ready-map i stället för en map per lager; `_topological_layers` behålls för
  stabil ordning. Standard förblir dagens layer-maps.
- Tester: 1.0 oförändrat; 1.1 ready straggler; okänt ID/cykel/självberoende;
  replay; cancellation; concurrency; budget per child (efter PR11).

### 4.2 Rust

- `crates/swe-native/src/definition.rs:76–83` (`Node::Map`): nya fält
  `schedule`, `item_id`, `item_depends_on`.
- `preflight.rs`: tillåt fälten endast för `schedule: "ready"`; avvisa i 1.0.
- `machine/state.rs`: map-scope får item-ID:n, dependencies och status per
  item.
- `machine/schedule.rs:372–455`: `start_map` validerar item-DAG och bygger
  ready-set; `spawn_map_children` admitterar redo items i stället för enbart
  nästa index. `complete.rs:219–256`: markera item klart, decrementera
  dependents och admittera; `finish_map` vid `remaining == 0`.
- `events.rs`/`effects.rs`: item-ID/index i `BranchSpawned`/completion-payload;
  `InvokeHandler` behåller samma fält. Stabil `branch_path`.
- `journal.rs`: per-item-identitet och replay; persistrade results ska
  rekonstruera ready-set utan host-events.
- Rust-tester: straggler, okänt/duplicerat ID, cykel, cancellation, replay,
  concurrency, 1.0-parity.

### 4.3 Kontrakt och gates

- Ny 1.1-fixture i `contracts/`, semantic test i
  `crates/swe-reference/tests/contracts.rs`, `MANIFEST.sha256`.
- `scripts/check_contract_freeze.py`: version-mapping för 1.1-schemat.
- `docs/specs/swe-swd-1.1.md` (eller uppdatering av 1.0-specen med tydlig
  versionssektion), schema-version och kontraktsbeslut.
- Ingen ändring av journal v1, `RunResult`, effect-typer eller budget.

## 5. Dynamisk admission

Utanför detta beslut. Statisk ready-DAG har alla items kända vid `start_map`.
Dynamisk tillkomst kräver ett separat admission-kontrakt (t.ex. en host-effekt
eller ett event som lägger till items i en aktiv map), med egna journalposter,
replay och fairness. Det får inte smygas in i 1.1.

## 6. Förkastade alternativ

| Alternativ | Varför inte |
| --- | --- |
| Ny `dag`-nod | Parallellt DAG-kontrakt/scheduler; bryter principen i `04-public-contracts.md:36–40`. |
| Host-ready-queue (PR #14) | Väntande handlers håller slots, dependency-state är host-ägd, replay kräver pre-seeding, dynamiska tasks tappar per-task-journal. |
| Tysta fält i SWD 1.0 | `check_contract_freeze.py` fäller formändring utan versionsbump; ingen tyst kontraktsändring. |
| Ny ledger/charge för ready-map | Ingen budgetändring behövs; child-invocations använder befintlig dispatch/budget. |
| Värdekanal via `item_depends_on` | `item_depends_on` är admission, inte dataflöde; att injicera generationens output kräver ett separat value-channel-kontrakt. |
| Dynamisk admission i samma steg | Kräver eget kontrakt, journal och fairness; hålls separat. |

## 7. Acceptanskriterier

- 1.0-dokument kompilerar och beter sig exakt som före ändringen; 1.0-schemat
  och dess fixture-hashar är oförändrade.
- 1.1 `schedule: "ready"` ger samma arbete/makespan-förbättring som PR #14 på
  straggler-DAG:en, men med machine-ägd dependency-state.
- Okänt ID, duplicerat ID, självberoende och cykel failar före första
  handleranrop; inga child-journalposter skrivs.
- Per-item `InvocationRecord` och resume fungerar utan host-pre-seeding.
- PR13-seamens `tests/test_seam.py` passerar oförändrat: `ready_ids` ger
  `after_fast` när `fast` är klar medan `slow` är öppen, medan
  `layer_is_complete` fortsätter vara false; varje admitted task har egen
  invocation-id.
- Värdekanalen förblir `absent`: `item_depends_on` styr endast admission och
  injicerar inte generationens output i validatorn.
- Cancellation stoppar admission och avbryter pågående children.
- Peak concurrency ≤ `map.concurrency`.
- Budget per child fungerar via befintlig PR11-väg utan ny ledger.
- Contract freeze, mutation, license och Python/Rust-parity är gröna.

## 8. Beslutspunkt

Ägaren godkände 2026-11-24:

1. SWD 1.1 som ny version (1.0 förblir fryst),
2. ready-mode på befintlig `map` (inte ny nod),
3. item-ID-mönstret och `branch_path`-formen,
4. att dynamisk admission är separat.

Implementationen följer avsnitt 4. Värdekanalen förblir `absent`.
