# 17 — Budget som förstaklassmedborgare i SWE-runtime

**Status:** **Godkänt av projektägaren 2026-11-23.** Beslutet gäller den
opt-in-budget som implementeras på `experiment/runtime-budget`; det ändrar
inte SWD 1.0, journal v1 eller någon fryst wire-yta.

## 1. Beslut

SWE:s Python-runtime ska kunna bära budget som en förstaklassresurs:

- `WorkflowResources` får ett valfritt `budget`-fält av typen
  `swe.budget.RuntimeBudget`.
- `RuntimeBudget` komponerar en host-policy med den **befintliga** Rust-ledgern
  `swe-budget`, exponerad via PyO3 som `swe_budget.Ledger`.
- Runtime reserverar atomärt före handler-/provideranropet, settle:ar en gång
  och håller reservationen pending vid cancel/timeout/abandonment; hosten kan
  release:a en pending hold vid reconciliation.
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
3. En reservation avslutas exakt en gång: settle från dispatch eller release
   från host-reconciliation. Dispatch release:ar aldrig. Dubbel
   settle/release avvisas av `RuntimeBudget` och dedupliceras av ledgern.
4. Settlement får aldrig överstiga reservationen. Overrun failar closed och
   lämnar reservationen `pending`, aldrig som en osynlig `reserved`-hold.
5. Cancel, timeout och **misslyckad settlement** (policyfel, ogiltigt belopp,
   overrun eller ledgerfel) markerar reservationen `pending`; medel frigörs
   inte tidigt. Hosten hittar den via `pending()`/`unresolved()` och avslutar
   den med `reconcile_invocation(invocation_id, ...)` utan att behöva
   reservationsobjektet. En pågående `reserved`-hold visas i `unresolved()`
   men får inte reconcileras förrän den är pending eller har settlement:ats
   normalt.
6. Replay anropar inte budget för redan persistrade invocations. Hosten
   persisterar och replayar ledgerhändelserna tillsammans med `RunStore`.
   `resume` kallar `restore(run_id=...)` för både terminala och running runs,
   före early-return, dispatch och cancel, så öppna holds blir synliga och
   terminala holds kan vägra re-dispatch.
7. Ingen generell retry införs. En host-retry återanvänder samma
   `run_id`/`invocation_id`; en `PENDING`-hold får en färsk `RESERVED`-objekt
   med samma ledger-id, medan en terminal delivery vägras före handlern.
   Ledgerns `lookup` är auktoritativ även när den in-process terminala
   retentionen har evictat posten. En hold som tillhör ett annat ledger-konto
   än `RuntimeBudget.account` avvisas före admission och kan aldrig debitera
   fel konto. När en retry adopterar en `PENDING`-hold ersätts objektet; en
   äldre `pending()`-snapshot är stale och avvisas av settle/release/reconcile.
   Använd `reservation_for` eller `reconcile_invocation` för aktuellt objekt.
8. Budgeten är domänoberoende: heltal i milli-enheter, ingen valuta, inga
   modellnamn och ingen prissättning i SWE.

## 5. Kompatibilitet och kontraktsändring

- `WorkflowResources.budget` är additivt och valfritt. Utan fältet är
  beteendet oförändrat.
- SWD 1.0-schemat, journal v1, events, `RunResult` och Rust-maskinen ändras
  inte. Ingen wire-version behöver bumpas.
- Den nya PyO3-ytan ligger i en separat, icke-publicerad crate
  `crates/swe-budget-py` (`publish = false`, Python-modul `swe_budget`,
  wheel `swe-budget-native`). `swe-native` har därför **ingen** dependency på
  `swe-budget` och kan fortfarande `cargo package`-verifieras mot crates.io.
- `cargo package -p swe-budget` och `cargo package -p swe-native` är fortsatt
  de publicerade crate-grindarna. `swe-budget-py` byggs som Python-wheel/sdist
  men publiceras inte som crate; CI bygger båda native-wheelen och installerar
  dem tillsammans. Ingen extern publicering krävs för att package-jobbet ska
  bli grönt.
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
