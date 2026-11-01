# Licensbeslut (2026-11-01)

## Beslut

| Fråga | Beslut |
| --- | --- |
| Licens | **MIT OR Apache-2.0** (dubbel) |
| Rättighetsinnehavare | `Copyright (c) 2026 Erik Fremred` |
| AI-agentförfattarskap | dokumenteras i `06-extraction-plan.md` |
| Yta | **endast extraktionsmålet**: `crates/swe-native`, `crates/swe-budget`, `crates/swe-reference`, `python/**`, `contracts/**` |
| Socialism-repot i övrigt | förblir `UNLICENSED` (Research, frontend, word-addin m.m.) |
| Synlighet | **publikt** repository |
| Repo | **`kajmund/swe`** |
| crates.io | `swe-native`, `swe-budget` (båda lediga vid kontroll 2026-11-01) |
| PyPI-distribution | **`swe-engine`** — `swe` är upptaget; importnamnet är fortfarande `swe` |
| Publicering | Manifesten förbereds med `publish = true` i PR 4; faktisk registry-publicering och taggning sker först efter målrepo-CI och konsument-PR |

## Vad som låg i källträdet före PR 4

- `LICENSE-MIT` och `LICENSE-APACHE` i `crates/swe-native`, `crates/swe-budget`,
  `crates/swe-reference` och `python/`.
- `license = "MIT OR Apache-2.0"` i de tre `Cargo.toml`.
- `license = { text = "MIT OR Apache-2.0" }` i `python/pyproject.toml`.
- `license = { text = "MIT OR Apache-2.0" }` i `crates/swe-native/pyproject.toml`
  (maturin-wheelens metadata).
- Licenstexterna är hämtade från den kanoniska Apache-2.0- och MIT-texten som
  levereras i cargo-registret, med vår egen copyright-rad i MIT-filen —
  inte avskrivna från minnet.

Filerna låg i varje paketkatalog, inte i repo-roten, eftersom källrepots rot
fortfarande innehåller kod som **inte** licensieras. I PR 4 blir
`LICENSE-MIT`/`LICENSE-APACHE` även rotfiler i det nya repot.

Synligheten är en förutsättning för licensen, inte en detalj: ett publikt repo
**distribuerar** koden, och då gäller MIT/Apache-2.0:s krav på att licens- och
upphovsrättstexter följer med. Därför blir `THIRD-PARTY-NOTICES` och licensgrinden
inte valfria dekorationer utan en del av publiceringen. Ett publikt repo innebär
också att historiken syns — därav att AI-agentförfattarskapet är dokumenterat i
`06` och inte bara går att hitta i `git log`.

## Namn — kontrollerat mot registren

- **crates.io:** `swe-native` och `swe-budget` finns inte (HTTP 404 vid kontroll
  2026-11-01). Namnen kan behållas.
- **PyPI:** distributionen `swe` är **upptagen** av ett annat projekt (svensk-engelsk
  ordbok, `bjesus/swe`). Därför heter distributionen `swe-engine` (också ledigt vid
  kontrollen), medan **importpaketet fortfarande heter `swe`** — det påverkar inte
  någon kod, bara paketets namn i `python/pyproject.toml`, backendens beroende och
  lockfilerna.
- **Repo:** [`kajmund/swe`](https://github.com/kajmund/swe) skapades i PR 4 och
  verifierades publikt (`isPrivate=false`) 2026-11-01 efter att både PR-körningen
  och push-körningen på mergade `main` var gröna.

En kollision som den här är exakt varför registry-publicering väntar till efter
flytten: namnen är verifierade lediga, licensen är beslutad och äganderätten
bekräftad, men faktisk publicering är ett separat steg efter PR 4.

## Genomfört i PR 4

- `THIRD-PARTY-NOTICES`: MIT och Apache-2.0 kräver att licens- och
  upphovsrättstexter följer med vid distribution av binärer/wheels. Alla 41
  Rust-beroenden (`Cargo.lock`: 41 paket utanför workspace) och alla 14 externa
  Python-paket (`python/uv.lock`: 15 poster inklusive projektet `swe-engine`
  självt; 6 i runtime-kedjan) är permissiva; `unicode-ident` är
  `(MIT OR Apache-2.0) AND Unicode-3.0` och kräver sin Unicode-text.
  genereras med `cargo-about`; native-wheelen inkluderar notiserna och fulla
  licenstexter.
- `scripts/check_licenses.py` jämför manifest, 41 Rust- och 14 Python-beroenden,
  rot-/paketlicenser och notices fail-closed i CI.
- PEP 639 `license-files` och SPDX-uttryck finns i båda `pyproject.toml`.
- `repository`/`homepage` pekar på `https://github.com/kajmund/swe` i Cargo- och
  Python-metadata.
- `publish = true` gäller `swe-native` och `swe-budget`; ingen registry har
  publicerats av PR 4.

## Grund för beslutet

Fakta från licensutredningen (`08 §7.14`):

- **Ingen LICENSE-fil har någonsin funnits** i den granskade historiken (mätt
  vid basen `9d17b096`: **1 415 commits**; HEAD efter denna PR: 1 422); alla
  crates angav `UNLICENSED` och `publish = false`; inga SPDX-identifierare eller
  copyright-huvuden fanns. MIT/Apache är därför ett positivt beslut av
  rättighetsinnehavaren, inte ett klargörande av något som redan gällde.
- **Ingen kopierad tredjepartskod** hittades i extraktionsmålet. Enda
  härledningen är intern (`swe-budget` är en 0-rads flytt av `swe-gym`-ledgern,
  `af15a396`).
- **Alla 41 Rust-beroenden och alla 14 externa Python-paket är permissiva**;
  ingen copyleft, inga NOTICE-filer, inga git-beroenden. `unicode-ident` är det
  enda `AND`-fallet.
- **9 commits på extraktionsmålens vägar är författade under `Cursor Agent`**
  (mätt på `9d17b096` med filtret `crates/swe-native`, `backend/app/swe`,
  `backend/tests/swe`, `backend/tests/test_swe_*.py`,
  `backend/scripts/benchmark_swe_runtime.py`, `docs/specs/swe-swd-1.0.md`,
  `docs/specs/swe-runtime.md`, `docs/specs/swe-provider.md`), inklusive kärnan
  (`b78055ba`) och SWD-schemat/parsern (`832a7f29`) — dokumenterat i `06`,
  eftersom en extern läsare annars hittar det i `git log`.
- **Rättighetsinnehavaren har bekräftats av ägaren** som personligt ägande av
  koden; detta är den enda punkten i B4 som inte kunde verifieras tekniskt.
