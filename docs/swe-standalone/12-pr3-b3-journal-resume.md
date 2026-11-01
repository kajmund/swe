# 12 — PR 3 (B3): journal och resume som motorgaranti

**Blockerare:** B3 (`07-go-no-go.md` §4).
**Förutsättning:** [`09-contract-decision.md`](09-contract-decision.md) godkänt,
PR 1 och PR 2 mergade.
**Storlek:** ~1–2 dagar.
**Central distinktion:** deduplicerad (at-most-once) **settlement** är en garant
under ett liveness-antagande. Exakt-engångs**exekvering av externa verktyg** är
inte en garant och får inte påstås. Ett obekräftat försök betyder **okänt**
utfall — inte att åtgärden inte kördes.

## 1. Vad som finns idag

| Komponent | Plats | Vad den bevisar |
| --- | --- | --- |
| Python `RunStore` + `RunRecord`/`InvocationRecord` | `backend/app/swe/run_store.py:15-56` | Transitioner kan persisteras och replayas |
| Replay på resume | `backend/app/swe/runtime.py:294` (`resume_run`), `:401` (`_apply_replay`) | Persisterade invocation-resultat kompletteras utan att handlern körs igen |
| OverGraph-implementation | `backend/app/services/overgraph/workflows.py` | Atomisk persistens per transition — **inte produktionskopplad** (`02` E10) |
| Demon-journal | `crates/swe-reference/src/journal.rs` | Definition-hash-grind, replay + `Ledger::replay`; deduplicerad settlement bevisad för demoåtgärderna |
| Ledger-idempotens | `crates/swe-gym/src/ledger.rs` | Reservera/settle med nyckel debiteras exakt en gång |

Saknas: ett **Rust-kontrakt** för journalen, och en modell som skiljer försök,
bekräftelse och debitering.

## 2. Mål

```rust
// swe-core::journal  (schema swe.journal.v1)
pub struct RunSnapshot {
    pub run_id: String,
    pub definition_sha256: String,
    pub schema_version: u32,
    pub input: Json,
    pub status: RunStatus,
    pub started_at_ms: u64,
    pub last_seq: u64,
}

/// Identiteten: en logisk leverans är stabil över försök.
pub fn delivery_key(run_id: &str, invocation_id: &str, action: &str) -> String;
pub fn attempt_id(delivery: &str, attempt_no: u32) -> String;

/// Append-only händelselogg — ingen post skrivs över.
pub enum JournalEvent {
    AttemptStarted { attempt_id: String, delivery_key: String, invocation_id: String,
                     action: String, delivery: Delivery, started_at_ms: u64 },
    ResultConfirmed { attempt_id: String, outcome: CompletionOutcome, confirmed_at_ms: u64 },
    AttemptAbandoned { attempt_id: String, reason: String, abandoned_at_ms: u64 },
    ChargeReserved { charge_id: String, attempt_id: String, delivery_key: String,
                     amount_milli: u64, tariff_milli: u64, reserved_at_ms: u64 },
    ChargeSettled { charge_id: String, amount_milli: u64, settled_at_ms: u64 },
    ChargeReleased { charge_id: String, reason: String, released_at_ms: u64 },
    ChargePendingReconciliation { charge_id: String, reason: String, at_ms: u64 },
}

pub enum Delivery { Pure, Replayable, ExternalIdempotent, ExternalNonIdempotent }

pub trait RunJournal {
    fn append(&mut self, event: &JournalEvent) -> Result<()>;
    fn load(&self, run_id: &str) -> Result<Option<JournalView>>;
}

/// Härledda vyer — inte egna sanningar.
impl JournalView {
    pub fn confirmed(&self, attempt_id: &str) -> Option<&CompletionOutcome>;
    pub fn open_attempts(&self) -> Vec<&JournalEvent>;
    pub fn charge_state(&self, charge_id: &str) -> ChargeState;
    pub fn settled_total(&self) -> u64;
}
```

`Delivery` deklareras per logisk leverans **innan** första `AttemptStarted` och
sparas i `AttemptStarted` (`09` §4.4). En run kan därför blanda `Pure`-beräkningar
och `ExternalNonIdempotent`-anrop i samma effektloop.

## 3. Resume-algoritm (kontraktet)

```text
1.  Läs journalen. Saknas den        -> typat fel (`resume`), ingen nystart.
2.  schema_version måste vara exakt   -> annars `unsupported`.
3.  definition_sha256 måste matcha    -> annars `conflict`.
4.  Ledger rekonstrueras via replay   och verifieras mot journalens
    ChargeSettled/ChargeReleased      -> differens = typat fel, ingen gissning.
5.  För varje öppet försök (AttemptStarted utan ResultConfirmed/Abandoned):
      delivery = Pure               -> `recompute_pure`: kör om, inga externa effekter,
                                       debitera aldrig.
      delivery = Replayable         -> kräv ResultConfirmed för att replaya;
                                       finns inget sparat resultat är det ett typat fel,
                                       inte en ny exekvering.
      delivery = ExternalIdempotent -> `idempotent_retry` med SAMMA delivery_key
                                       (at-least-once leverans; högst en extern effekt
                                       förutsatt att verktyget respekterar nyckeln).
      delivery = ExternalNonIdempotent -> `fail_stop` + `ChargePendingReconciliation`.
                                       Utfallet är OKÄNT; ingen retry, ingen avskrivning.
6.  Starta en fräsch maskin med samma run_id/input/klocka, replaya
    ResultConfirmed i effektordning (samma som idag, runtime.py:401).
7.  Fortsätt från effektgränsen; varje ny åtgärd är ett nytt AttemptStarted.
```

### 3.1 Garantier (orden är nu exakta)

| Garanti | Formulering som får användas |
| --- | --- |
| Leverans, rena och replaybara åtgärder | at-least-once leverans; idempotent eftersom åtgärden är ren/fryst |
| Leverans, externa idempotenta | at-least-once leverans; högst en extern effekt **endast** om verktyget respekterar `delivery_key` |
| Leverans, externa icke-idempotenta | fail-stop med **okänt** utfall; ingen at-least-once-garanti |
| Ekonomi | **deduplicerad/at-most-once settlement**: en `ChargeSettled` per `charge_id`; exakt en settlement garanteras endast för bekräftade, debiterbara logiska anrop under ett uttryckligt liveness-antagande (journalen når hållbar lagring) |

Formuleringen "exakt-engångsdebitering" får inte användas i denna PR.

### 3.2 Okänt är okänt

Ett `unconfirmed_attempt` betyder att **exekveringsutfallet är okänt**, inte att
åtgärden inte kördes. `ResultConfirmed` bevisar bara att resultatet
journalfördes — inte att sidoeffekten inträffade exakt en gång. Därför:

- vid `fail_stop` får reservationen **inte** släppas utan bevis att ingen
  kostnad uppstod; annars `ChargePendingReconciliation`;
- `pending_reconciliation` måste lösas innan en run får ett slutligt
  ekonomiskt facit;
- ingen kodväg får skriva `0` för en okänd kostnad.

## 4. Förändringar per komponent

| Komponent | Ändring |
| --- | --- |
| `backend/app/swe/run_store.py` | `RunRecord` får `schema_version`; ny `AttemptRecord` och `ChargeRecord`; protokollet utökas med `attempts()`/`charges()` |
| `backend/app/swe/runtime.py` | Skriver `InvocationAttempt` före dispatch och `ConfirmedResult` när svaret tas emot; `_apply_replay` (`:401`) läser bara bekräftade resultat |
| `backend/app/services/overgraph/workflows.py` | Nya nodtyper `WorkflowAttempt`/`WorkflowCharge` i samma atomiska transition; ingen ändring i `WorkflowRun`/`WorkflowInvocation` |
| `crates/swe-core/src/journal.rs` | Ny modul med typerna ovan + `JournalView` + replayhjälp |
| `crates/swe-reference` | Journalen använder `swe-core`-kontraktet; de nya testerna i §5 |
| `docs/specs/swe-overgraph.md` | Uppdateras: nya nodtyper och `schema_version`; Owner-regeln gäller fortsatt för katalogen |

## 5. Tester som pinnar garantierna

| Test | Bevisar |
| --- | --- |
| `pause_resume_charge_and_execute_exactly_once` (finns) | Oförändrad semantik för deterministiska åtgärder; påstår *inte* extern exakt-engångs |
| `delivery_key_is_stable_across_attempts` (ny) | `delivery_key` identisk för retry; `attempt_id` olika |
| `unconfirmed_pure_action_is_recomputed` (ny) | `Pure`-försök utan resultat → `recompute_pure`, aldrig `replay_result` |
| `unconfirmed_external_action_is_fail_stop` (ny) | `ExternalNonIdempotent` utan resultat → typat `fail_stop`, ingen retry |
| `unconfirmed_external_charge_is_not_released` (ny) | Reservationen → `ChargePendingReconciliation`, aldrig `ChargeReleased`/`ChargeSettled` |
| `idempotent_retry_reuses_the_delivery_key` (ny) | Samma `delivery_key` i nytt försök; högst en `settled` per `delivery_key` |
| `journal_is_append_only` (ny) | Ingen händelse skrivs över; tillstånd härleds ur loggen |
| `journal_version_and_hash_are_enforced` (ny) | Fel `schema_version`/`definition_sha256` → typat fel, ingen tyst resume |
| `settled_total_matches_ledger` (ny) | Journalens `settled_total()` == ledgerns saldo efter resume |
| Python: `test_swe_persistence.py` (11) | Utökas med attempt/confirm/charge-händelser och versionskontroll |

## 6. Acceptanskriterier

1. Rust-journal-kontraktet finns i `swe-core` och referensexperimentet använder
   det (ingen demospecifik journal kvar).
2. De nio nya testerna i §5 är gröna; de tolv befintliga demotesterna dito.
3. Journalen är en append-only logg av händelserna i §2; inga muterbara
   statusfält på försök eller avgifter.
4. `Delivery` deklareras per logisk leverans före första dispatch och sparas i
   journalen; en run kan blanda rena och externa åtgärder.
5. Okänd kostnad representeras som `pending_reconciliation`, aldrig som `0` och
   aldrig som `released` utan bevis.
6. Ingen text i docs eller PR påstår exakt-engångs*exekvering* av externa
   verktyg eller obetingad exakt-engångs*sättlement*.
7. Research-beteendet oförändrat: `runtime.py`-ändringarna är additiva
   (händelser skrivs, resultatlogiken är densamma).

## 7. Risker

| Risk | Mildring |
| --- | --- |
| Ökad skrivvolym per transition | Attempts/charges skrivs i **samma** `begin_write_txn`; mät antal noder per run i PR:en |
| OverGraph-katalogen växer | `WorkflowAttempt`-noder är små; retention/kompaktering är ett separat beslut, inte en semantikändring |
| Owner-regeln | Inget ändras i ägarskapet: implementationen stannar som Socialism-adapter (`09` §2.1, `02` §8.3) |
| Äldre journaler saknar försök | Före produktion finns inga kunder; ingen migration byggs (`09` §3.5) |
| `fail_stop` gör att en run fastnar i avstämning | Avsiktligt: ett okänt utfall måste synas. Åtgärden är en explicit avstämningsprocess, inte en tyst retry |
| `fail_stop` gör att en run fastnar i `pending_reconciliation` | Avsiktligt: ett okänt utfall måste synas. Åtgärden är en explicit avstämningsprocess, inte en tyst retry |
