# finn-marked

Web-tjeneste som følger med på bruktbilmarkedet over tid: henter *hele*
søkeresultatet fra kildene, lagrer hver annonse med pris-historikk, og viser
endringer, salgstakt og verdikurve i et webgrensesnitt.

Bygget for å kunne følge flere markeder samtidig (flere biler, ulike modeller) —
hver **vakt** er ett marked med ett eller flere søk, og hvert søk navngir sin
**kilde**. Nye plattformer legges til som en adapter i `app/kilder/`.

## Hva den gjør

* **Henter hele resultatsettet**, ikke bare første side. Antall unike annonser
  kontrolleres mot treff-tallet kilden selv oppgir. Er avviket stort (under 10 %
  av oppgitt treff) avbrytes hentingen uten å skrive noe — ellers ville hele
  markedet blitt markert som «borte» om siden endret seg.
* **Registrerer salg.** FINN markerer solgte annonser i søket («Solgt», uten pris).
  Det flagget fanges, så du får reell salgstakt — ikke bare tilbudssiden.
* **Lagrer historikk**: første/siste observasjon, prisendringer og liggetid per
  annonse, samt én rad per henting med median/percentiler.
* **Verdikurve** per marked: regresjon på utropspriser mot kilometerstand, med
  spredning. Sammenlignbare innen ±10 000 km vises ved siden av, fordi det tallet
  er lettere å forholde seg til enn en linje.
* **Endringsstrøm** på JSON-API (`/api/vakt/<slug>/endringer?since=…`) som en
  ekstern varsler kan lese. `since` tåler både `2026-10-11 13:52:11` og ISO med
  `T`.

Utropspriser er ikke oppnådde salgspriser. Kilden viser ikke hva bilene faktisk
gikk for — liggetid og *hva* som ble solgt er det nærmeste vi kommer. Bud fra
auksjoner holdes utenfor median, kurve og km-bøtter, og vises bare i annonselisten.

## Kilder

| Kilde | Status | Merknad |
|---|---|---|
| `finn` | i bruk | FINN mobilitet (`/mobility/search/car`), SSR-HTML |
| `auksjonen` | i bruk | Auksjonen.no (JSON-API). Objektene ligger også på FINN, men bud/minstepris hentes her |

Kategorier er ikke hardkodet per merke: mønstrene utledes av vaktens egne
`kategorier`-nøkler (se «Nye vakter»).

## API

```
GET /api/vakter                              alle vakter + nøkkeltall
GET /api/vakt/<slug>                         full markedsdata for én vakt
GET /api/vakt/<slug>/annonser.json           annonser (valgfritt ?kategori=)
GET /api/vakt/<slug>/endringer?since=ISO     hendelser siden tidspunkt
GET /helse                                   helsesjekk (Docker healthcheck) + scheduler-tilstand
```

## Drift

* **Bygg:** stacken i Portainer (`DOCKERSRV1/finn-marked/compose.yml`) bygger imaget
  direkte fra dette repoet — `build.context: https://github.com/terjefl/finn-marked.git#main`.
  Ingen registry-innlogging, ingen lokal arbeidskopi: «Pull and redeploy» i Portainer
  henter fersk kode og bygger på nytt (~15 s). Det finnes **ingen** deploy-script for
  dette lenger; tidligere lå tarball-varianten i `scripts/oppdater-vert.sh`.
* **Hvorfor ikke `image:` fra ghcr:** Portainer kjører `docker compose pull` før hver
  deploy. ghcr-pakken er privat og svarer 401 anonymt, og da avbrytes hele deployen.
  Uten `image:`-linje svarer compose «Skipped - No image to be pulled». GitHub Actions
  bygger og pusher likevel imaget ved push, så det kan tas i bruk ved å bytte til
  `image: ghcr.io/terjefl/finn-marked:latest` + `pull_policy: missing`.
* **CI:** `.github/workflows/bygg.yml` kjører `ruff` og testene før image-bygget.
  Feiler sjekkene, blir det ikke noe image.
* **Data:** SQLite i `/data` — bind-mount på verten. Ingen database å drifte.
  Skjemaendringer håndteres med nummererte migreringer (`PRAGMA user_version`, se
  `app/db.py`), så nye kolonner kommer med ved oppstart.
* **Henting:** én gang i døgnet per vakt, styrt av `hentetime` i vakt-konfigurasjonen.
  Kjøres også manuelt fra UI-et. En henting av gangen per vakt: både scheduleren og
  «Oppdater nå»-knappen tar samme lås, så de ikke skriver over hverandre.
* **Tilgang:** `https://bilmarked.flagan.net` bak NPM + Cloudflare Access.

## Lokal kjøring

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
FINN_DATA=./data .venv/bin/uvicorn app.web:app --port 8099

# tester (ingen tredjepartsavhengigheter) og lint
python3 tests/test_parser.py
python3 tests/test_innsamling.py
python3 tests/test_auksjonen.py
.venv/bin/ruff check .        # eller: pip install ruff
```

## Nye vakter: søk og kategorier

I skjemaet skrives ett søk per linje, på formen `[kilde:][tag] | url`:

```
alle | https://www.finn.no/mobility/search/car?variant=1.749.8308&year_from=2022
auksjon | https://www.auksjonen.no/api/category-search/search?category1=1000&q=i4
```

Kilden utledes av URL-en (`auksjonen.no` → `auksjonen`, ellers `finn`); skriv
`kilde:tag` foran bare for å overstyre. Taggen merkes på annonsene fra søket.

Kategorier skrives som `nokkel = visningsnavn`, ett par per linje:

```
fully = Fully Charged
m50 = M50
```

Hver kategori merkes når **nøkkelen eller visningsnavnet** står som eget ord i
tittel/undertittel — `fully` fanger «Fully Charged», `m50` fanger «M50». Ingen
kodeendring trengs for nye merker: legg bare inn kategoriene i vaktens skjema.

## Importere historikk fra Hermes-scriptet

```bash
python3 scripts/importer.py --kilde ~/.hermes/data/finn_bil_marked/bmw-i4-2022.sqlite \
    --slug bmw-i4-2022 --ny-slug bmw-i4-2022 [--db ./data]
```

`--db` peker målbasen (mappe eller fil); uten den brukes `FINN_DATA`/`/data`.
Bare annonser og prispunkter for `--slug` importeres.

## Legge til en ny kilde

1. Lag `app/kilder/<navn>.py` med en klasse som har `navn` og
   `hent(sok, logg) -> (annonser, treff)`.
2. Normaliser annonsene til felles-formatet i `app/kilder/__init__.py`.
3. Registrer klassen med `registrer(...)` nederst i modulen.
4. Søket skrives som `[kilde:][tag] | url` i skjemaet — kilden utledes av URL-en
   hvis den ikke står eksplisitt (se «Nye vakter»).
