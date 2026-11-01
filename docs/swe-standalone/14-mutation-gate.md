# Mutationsgrinden: klassificerad, inte räknad

En grön mutationssiffra kan ljuga. B2:s differentialtest passerade varje mutation
eftersom det var en tautologi; B3:s tabell sade "7 av 7" när 7 missades. Därför
är grinden inte "kör `cargo-mutants` och kräv att alla mutationer dör". Den är:

> **Varje kritisk mutation är klassificerad som `detected`, `equivalent` eller
> `excepted` — med motivering — och en mutation vars utfall inte stämmer med
> klassificeringen fäller CI.**

## Vad klasserna betyder

| Klass | Krav | Varför |
| --- | --- | --- |
| `detected` | Mutationen **måste** göra `expected_red`-testerna röda: körningen ska ge ett icke-noll-utfall och **varje** namngivet test ska finnas bland de parsade `test ... FAILED`-raderna | Om den slutar fällas har testet som skulle fånga den ruttnat; om något annat test blir rött bevisades fel sak |
| `equivalent` | Mutationen **måste** lämna sviten grön: **körningen ska avslutas med exit 0**, och motiveringen säger varför den inte kan ändra beteende | En äkta ekvivalent mutation ska inte kräva ett konstlat test |
| `excepted` | Samma grön-krav som `equivalent`, med motivering | Vissa saker är uttryckligen inte kontrakt (t.ex. mänsklig feltext) |

`detected` kräver alltså alltid ett explicit `expected_red`. Ett tomt
`expected_red` är inte "inga krav" — det är gate-fel: annars kunde en mutation
klassas som `detected` utan att någon vet vilket test som ska fånga den.

## Grön betyder exit 0 — inte "inga parsade röda tester"

Det räcker inte att titta på exitkoden, och det räcker inte att titta på om någon
`test ... FAILED`-rad finns. En kompileringsmisslyckad mutation, en svit där inget
testharnesk körde och en testprocess som kraschar/abortar ger alla icke-noll-utfall
utan att nödvändigtvis lämna en `test ... FAILED`-rad. En abort dödar processen
innan libtest hinner rapportera testet, och en annan, grön testbinär kan
fortfarande skriva sin `test result: ok`-rad — då ser sviten "grön" ut om man bara
räknar parsade röda rader.

Därför läser `run_tests` hela utdata och skiljer på **tolkbara** och
**otolkbara** körningar:

- tolkbar grön: exit 0 och inga rapporterade röda tester;
- tolkbar röd: icke-noll-utfall där **varje** rapporterad röd test också är en
  namngiven `test ... FAILED`-rad (antalet stämmer);
- allt annat är ett `run_error` som fäller grinden **för varje klass**:
  kompileringsfel, ingen testharnesk-utdata, krasch/abort eller
  test-launch-fel, och motsägelsefulla utdata (t.ex. exit 0 men röda tester).

Beslutet ligger i den rena funktionen `judge(mutation, code, failed, run_error)` i
`scripts/check_mutations.py`, så att det kan prövas utan cargo.

## Grindens egna negativa tester

```
$ python3 scripts/test_check_mutations.py
PASS  expected 'red' got 'red'  detected + clean green: fails (the catching test has rotted)
PASS  expected 'red' got 'red'  equivalent + crash/abort (code 101, no parsed failure): fails
...
30 cases, 0 failed
```

Matrisen importerar den levererade grinden (inga mockar, ingen cargo) och prövar
både `judge` och utdataparsern mot riktiga cargo-utdataformer, plus
manifestvalideringen. Minsta uppsättning negativa fall: `detected` som är grön,
`detected` med fel test rött, `detected` utan `expected_red`, `detected` med
kompileringsfel; `equivalent`/`excepted` med ren testrött respektive
krasch/abort; icke-noll utan namngivet test för alla klasser; och manifest med
okänd klass, saknad motivering, saknade nycklar, tomt manifest eller fel version.
En grind vars negativa fall inte körs är ett påstående, inte en mekanism.

## Vad som körs

`scripts/check_mutations.py`:

1. skapar en **isolerad, detached `git worktree` av committed HEAD** i en
   temporär katalog med egen `CARGO_TARGET_DIR`. Den checkout som ska committas
   muteras aldrig — en låsfil räcker inte, för den hindrar inte en oberoende
   `git add` från att se en muterad fil. **Ocommittade ändringar i
   arbetskatalogen omfattas inte**: grinden mäter HEAD, inte det du håller på att
   ändra. Committa först, eller låt CI (som kör på PR:ens/merge-commitens HEAD)
   vara avgörande. En lokal grön körning bevisar alltså inte en smutsig
   arbetskatalog;
2. kör **`cargo test --workspace --no-fail-fast`** — hela workspace-sviten, alltså
   både unit- och integrationstester (inklusive doctests), inte bara ett pakets
   enhetstester — och kräver ett tolkbart grönt utgångsläge (annars kan grinden
   inte döma något);
3. applicerar varje mutation i worktreen, verifierar att textersättningen
   träffade, kör testerna och jämför utfallet med klassificeringen via `judge`;
4. återställer filen och verifierar med **sha256 att byten är identiska** med
   originalet. Eftersom mutationen bara lever i en isolerad worktree som
   hashas tillbaka behövs ingen ny grön körning efter varje återställning: byten
   är verifierade, inte antagna. (Det motsvarar krav 2:3 i
   `13-verification-standard.md`.)
5. rensar worktree och scratch-katalog i `try/finally`, så att även Ctrl-C eller
   ett undantag städar. Checkouten är aldrig muterad oavsett var körningen
   avbryts.

Tabellen skrivs till stdout; alla fel skrivs till stderr och ger exit 1.

## Offline och determinism

Grinden kräver ingen extern tjänst och inget nätverk **när beroendecachen är
varm**; samma `Cargo.lock` och samma verktygskedja gör körningen deterministisk.

- **CI:** `cargo fetch --locked` körs först (online, en gång per runner) och
  därefter körs grinden med `--offline`. Den ordningen är nödvändig: en ren
  runner har ingen registercache, så `--offline` före `fetch` skulle fallera på
  att lösa de låsta beroendena.
- **Lokalt:** standard är online, så en kall maskin kan hämta de låsta
  beroendena. När cachen är varm, kör med `--offline` som CI.

Det finns ingen `--package`-flagga: grinden kör alltid hela workspace-sviten.

## Utöka grinden

Lägg till en post i `mutations/baseline.json`:

```json
{
  "id": "kort-unik-beskrivning",
  "file": "crates/swe-native/src/operations.rs",
  "find": "exakt text som finns i filen",
  "replace": "ersättningen",
  "classification": "detected",
  "expected_red": ["operations::tests::namnet_pa_testet"]
}
```

`detected` kräver `expected_red` (minst ett testnamn som den muterade koden ska
göra rött); `equivalent` och `excepted` kräver `"reason"` i stället. Testnamnen
skrivs precis som `cargo test` skriver dem i `test ... FAILED`-raden:
unit-tester med modulväg (`operations::tests::...`), integrationstester som bara
testnamnet.

Arbetsordning: lägg till testet och mutationen i samma ändring, **committa** (den
isolerade worktreen är committed HEAD), kör sedan
`python3 scripts/check_mutations.py --offline` och lägg in resultatet i
loggen/commiten. En `equivalent` som börjar fälla sviten är ett gate-fel — då är
baslinjen inaktuell och påståendet om att mutationen är ofarlig falskt.

## Vad grinden inte skyddar

- **Grinden skyddar inte sig själv.** En PR kan ändra `scripts/check_mutations.py`,
  `mutations/baseline.json` eller CI-workflowen, och grinden körs då i den
  versionen. Det fångas av repository-granskning (CODEOWNERS/ruleset och CI:ns
  selector-härdning), inte av grinden själv. CI:s närvaro-guard kontrollerar att
  skriptet inte försvinner, inte att det är oförändrat.
- **Den bevisar inte att sviten är fullständig.** Att alla klassificerade
  mutationer beter sig som klassificerade säger inget om tester som ingen mutation
  pekar på; en tautologisk test fångas bara om krav 2 i
  `13-verification-standard.md` följs när testet läggs till.
- **Den mäter committed HEAD**, inte arbetskatalogen och inte något annat än den
  mutation som står i manifestet.

## Vad grinden redan hittat

När den infördes fällde den två av sina egna första mutationer:

- kärnan rapporterade `UNRESOLVED` för varje saknat behov i stället för den
  faktiska statusen — inget kärntest pinnade `MissingInput::status` (det gjorde
  bara referensdemon). Nytt kärntest: `the_gate_reports_why_each_input_is_missing`.
- en mutation som skulle förbjuda release av en avstämningspost träffade fel
  `match`-arm och var därför ekvivalent. Det syntes bara för att grinden kräver
  att en `detected`-mutation faktiskt fäller något.

Granskningsvarvet som införde den strikta gröna regeln hittade en tredje
felform: en mutation som fick en testprocess att aborta räknades som "grön" för
`equivalent`, eftersom ingen `test ... FAILED`-rad hann skrivas. Den fångas nu av
att `equivalent`/`excepted` kräver exit 0 och av matrisens kraschfall.

Det är samma lärdom som `13-verification-standard.md` bygger på: verifieringen är
svagare än självförtroendet tills någon mäter den.
