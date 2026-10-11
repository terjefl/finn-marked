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
* **Flere brukere** med passkeys: hver bruker har sine egne vakter, admin ser alle
  og kan sette opp en vakt for en bruker før hen logger inn første gang.

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

## Brukere og innlogging

Alt unntatt `/helse` krever innlogget bruker. Innlogging er **passkey** (WebAuthn:
Touch ID, Face ID, Windows Hello, sikkerhetsnøkkel) — ingen passord i det hele tatt.

* **Første gang:** basen har ingen brukere, så `/` sender deg til `/oppsett`. Skriv
  navnet ditt og lag en passkey; du blir administrator, og vaktene som alt finnes
  i basen blir dine.
* **Invitere:** «Brukere» i menyen (admin) → navn → «Opprett og lag invitasjonslenke».
  Lenken er gyldig i 7 dager og kan brukes én gang; brukeren åpner den og lager sin
  egen passkey. Tjenesten sender ikke e-post, du sender lenken selv.
* **Vakt klar før første innlogging:** «Lag vakt» på brukerens rad, eller «Kopier til»
  på en vakt du alt har (søk og kategorier blir med, du fyller inn bilen og km).
  Vakten henter med en gang, så tallene er på plass når brukeren logger inn.
* **Flere enheter:** «Konto» → «Lag passkey» på hver enhet du vil logge inn fra.
* **Mistet passkey:** admin lager en ny invitasjon til brukeren (samme konto, ny
  passkey). Mister *alle* admin-passkeys: sett `FINN_OPPSETT_NOKKEL` i miljøet og
  åpne `/oppsett?nokkel=<verdien>`.
* **Eierskap:** én eier per vakt. Vanlige brukere ser og redigerer bare sine egne;
  admin ser alle (eieren vises som en pill på forsiden).

Miljøvariabler: `FINN_RP_ID` (vertsnavnet brukerne åpner, f.eks.
`bilmarked.flagan.net`) og `FINN_ORIGIN` (`https://bilmarked.flagan.net`). Uten dem
utledes begge av forespørselen, som er greit lokalt (`localhost`), men sett dem i
drift. Cloudflare Access foran tjenesten er uavhengig av dette og kan stå.

## API

JSON-endepunktene krever samme sesjonscookie som sidene (brukes av «JSON»-knappen).
Den tidligere endringsstrømmen for ekstern varsling er fjernet.

```
GET /api/vakter                              egne vakter + nøkkeltall (admin: alle)
GET /api/vakt/<slug>                         full markedsdata for én vakt
GET /api/vakt/<slug>/annonser.json           annonser (valgfritt ?kategori=)
GET /api/forhandsvis?url=…                   første side av et søk (til «Ny vakt»)
GET /helse                                   helsesjekk (Docker healthcheck) + scheduler-tilstand, åpen
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
* **Tilgang:** `https://bilmarked.flagan.net` bak NPM + Cloudflare Access, og
  passkey-innlogging i appen (se «Brukere og innlogging»). Stacken må sette
  `FINN_RP_ID` og `FINN_ORIGIN`.
* **Én prosess:** scheduleren, hentelåsene og passkey-utfordringene lever i minnet,
  så tjenesten skal kjøre med én uvicorn-worker (standard).

## Lokal kjøring

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
FINN_DATA=./data .venv/bin/uvicorn app.web:app --port 8099
# første besøk på http://localhost:8099 → /oppsett (passkeys virker på localhost)

# tester uten tredjepartsavhengigheter
python3 tests/test_parser.py
python3 tests/test_innsamling.py
python3 tests/test_auksjonen.py
python3 tests/test_sok.py
# trenger fastapi/jinja2/webauthn/httpx (requirements-dev.txt)
.venv/bin/python tests/test_auth.py
.venv/bin/ruff check .
```

## Nye vakter: søk og kategorier

«Ny vakt» er to steg. Først limer du inn **én** søke-URL fra finn.no (eller
auksjonen.no); appen henter første side med en gang og viser antall treff, noen
eksempler og et forslag til navn og kortnavn («BMW i4 2022» fra vanligste tittel +
`year_from`/`year_to`). Så får du hele skjemaet, forhåndsutfylt. Ved lagring starter
første henting av seg selv.

**Søk** er rader med tag og URL. Kilden utledes av adressen (`auksjonen.no` →
`auksjonen`, ellers `finn`) og kan overstyres per rad. Taggen merkes på annonsene
søket finner, og `page=` i URL-en strippes (vi paginerer selv). Et FINN-søk med
`q=fully` og tag `fully` gir kategorien også når ordet bare står i beskrivelsen.

**Kategorier** er rader med visningsnavn og nøkkel (nøkkelen lages av navnet om du
lar den stå tom). Hver kategori merkes når nøkkelen eller visningsnavnet står som
eget ord i tittel/undertittel — `fully` fanger «Fully Charged», `m50` fanger «M50».
Etter første henting foreslår rediger-siden kategorier fra det som går igjen i
undertitlene (f.eks. «Supercharged 17», «Laser 11»); klikk for å legge til. Ingen
kodeendring trengs for nye merker.

Importeren og eldre klienter kan fortsatt sende søk som tekst (`[kilde:][tag] | url`,
se `app/sok.py`).

## Importere historikk fra det gamle scriptet

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
4. Legg vertsnavnet inn i `sok.kilde_fra_url` og `sok.sjekk_url`, og gi modulen en
   `forhandsvis(url)` (se `finn.py`) så «Ny vakt» kan vise første side. Kilden kan
   også velges eksplisitt per søkerad i skjemaet.
