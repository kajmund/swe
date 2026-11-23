# 17 — Budget som förstaklassmedborgare i SWE-runtime

**Status:** **Godkänt av projektägaren 2026-11-23.** Beslutet gäller den
opt-in-budget som implementeras på `experiment/runtime-budget`; det ändrar
inte SWD 1.0, journal v1 eller någon fryst wire-yta.

## 1. Beslut

SWE:s Python-runtime ska kunna bära budget som en förstaklassresurs:

- `WorkflowResources` får ett valfritt `budget`-fält av typen
  `swe.budget.RuntimeBudget`.
- `RuntimeBudget` komponerar en host-policy med den **befintliga** Rust-ledgern
  `swe-budget`, exponerad via PyO3 som `swe_native.Ledger`.
- Runtime reserverar atomärt före handler-/provideranropet, settle:ar eller
  release:ar exakt en gång och håller reservationen pending vid cancel/timeout.
- Ingen duplicerad ledger, ingen ny SWD-nod, inget nytt journalfält och ingen
  generell retry införs.

## 2. Kontext

Den tidigare read-only-auditen (`docs/analysis/2026-11-23-rea-budget-audit.md`)
visade att:

- `WorkflowRunner`/`runtime_dispatch` inte anropade `swe-budget`;
- `swe-budget` var en Rust-only-crate utan Python-bindning;
- `crates/swe-reference` redan använde ledgern och pausade vid budgettak;
- `ActionOffer` hade en valfri `OfferBudget`-adapter, men ingen allmän
  runtime-budget.

Slutsatsen var att frånvaron var dokumenterad host-policy, inte ett
kontraktsbrott, men att avsaknaden av en Python-väg gjorde att varje host
tvingades skriva egen meter. Beslutet stänger den luckan utan att införa en
andra ledger.

## 3. Kontrakt

Den fullständiga specifikationen ligger i
[`docs/specs/swe-budget-runtime.md`](../specs/swe-budget-runtime.md).

Kort:

- `BudgetPolicy.reserve(request) -> int` ger ett konservativt tak, eller `0`
  för ett uttryckligt gratis anrop. Okänd kostnad kastar
  `BudgetUnknownCost` och inget dispatchas.
- `BudgetPolicy.settle(request, outcome) -> int` ger faktisk kostnad, som inte
  får överstiga reservationen.
- `BudgetLedger` är den delmängd av `swe-budget` som runtime behöver:
  `reserve`, `settle`, `release`, `balance`, `events`.
- `RuntimeBudget` äger livscykeln och tillstånden `reserved`, `pending`,
  `settled`, `released`.
- Nycklarna är stabila över replay och host-retries:
  `{run_id}:{invocation_id}:reserve|settle|release`.

## 4. Invarianter

1. Reservation sker omedelbart före handler-/provideranropet. För handlers
   sker den efter att concurrency-tillståndet tagits; för providers före
   exact-cache-uppslagningen, så en cache-träff fortfarande reserverar
   konservativt och policyn kan settle:a `0`. En invocation som aldrig når
   anropet reserverar inget.
2. Reservera är den atomära check-and-hold-operationen. Otillräckliga medel
   ger `BudgetExceeded`; handlern anropas inte.
3. En reservation avslutas exakt en gång: settle eller release. Dubbel
   settle/release avvisas av `RuntimeBudget` och dedupliceras av ledgern.
4. Settlement får aldrig överstiga reservationen. Overrun failar closed och
   lämnar reservationen öppen för host-reconciliation.
5. Cancel och timeout markerar reservationen `pending`; medel frigörs inte
   tidigt. Sen debitering kan settle:as eller reconcileras av hosten.
6. Replay anropar inte budget för redan persistrade invocations. Hosten
   persisterar och replayar ledgerhändelserna tillsammans med `RunStore`.
7. Ingen generell retry införs. En host-retry måste återanvända samma
   `run_id`/`invocation_id`, vilket gör ledgerns idempotens till skyddet mot
   dubbeldebitering.
8. Budgeten är domänoberoende: heltal i milli-enheter, ingen valuta, inga
   modellnamn och ingen prissättning i SWE.

## 5. Kompatibilitet och kontraktsändring

- `WorkflowResources.budget` är additivt och valfritt. Utan fältet är
  beteendet oförändrat.
- SWD 1.0-schemat, journal v1, events, `RunResult` och Rust-maskinen ändras
  inte. Ingen wire-version behöver bumpas.
- Den enda nya Rust-beroendekedjan är att `swe-native` med `python`-feature
  beror på workspace-craten `swe-budget`.
- Publiceringsordning: `swe-budget` måste publiceras före `swe-native`, eftersom
  `swe-native`s `python`-feature har en versionssatt path-dependency. `cargo
  package -p swe-budget` följt av registry-publicering ska ske först; därefter
  kan `cargo package -p swe-native` verifiera mot crates.io. CI bygger
  native-wheelen från källträdet och påverkas inte av den ordningen.
- Kernelns frysta `ErrorKind` saknar `budget`. Budgetfel blir därför
  `kind="handler"` i `RunResult.error` med ett meddelande som börjar
  `budget ...`; `InvocationRecord.error` behåller host-kind `"budget"`.

## 6. Förkastade alternativ

| Alternativ | Varför inte |
| --- | --- |
| Ny Python-ledger | Bryter beslutet att `swe-budget` är enda valutaimplementationen; skulle duplicera invarianter och replay. |
| Nytt `budget`-fält i SWD | Kräver wire-version och kontraktsbeslut; prissättning är host-domän, inte workflow-semantik. |
| Endast dokumentera en wrapper | Löser inte att `swe-budget` behöver en Python-bindning och lämnar varje host att återimplementera ledgern. |
| Generell retry i runtime | Retry är uttryckligen utanför SWE v1; budget-idempotensen ska inte användas som ursäkt att införa det. |
| Frigör reservation vid cancel | Kan återanvända medel som providern redan debiterat; bryter mot konservativ reservation. |

## 7. Verifiering

- `python/tests/test_swe_budget_binding.py` – bindning, replay, idempotens,
  overrun och tillståndsövergångar.
- `python/tests/test_swe_budget_runtime.py` – 10 000 mikrotasks vid
  concurrency 64 med budgettak, fail-closed tak, cancel med pending
  reservation, felsettlement och crash/resume-replay.
- `cargo test -p swe-budget --locked` – ledgerns acceptanstester.
- `cargo test -p swe-reference --locked` – referenshostens budgetväg.
- Hela Python-sviten.

## 8. Öppna frågor

- Host-reconciliation av pending-reservationer är ett host-ansvar. En framtida
  host-adapter kan exponera en tidsgräns eller en återkommande kvittningskö,
  men det hör inte till detta beslut.
- Om kernelns `ErrorKind` senare får `budget` krävs en separat
  kontraktsändring och versionering; tills dess är mappningen till `handler`
  avsiktlig och dokumenterad.
