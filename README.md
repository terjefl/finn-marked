# finn-marked

Web-tjeneste som følger med på bruktbilmarkedet over tid: henter *hele*
søkeresultatet fra kildene, lagrer hver annonse med pris-historikk, og viser
endringer, salgstakt og verdikurve i et webgrensesnitt.

Bygget for å kunne følge flere markeder samtidig (flere biler, ulike modeller) —
hver **vakt** er ett marked med ett eller flere søk, og hvert søk navngir sin
**kilde**. Nye plattformer legges til som en adapter i `app/kilder/`.

## Hva den gjør

* **Henter hele resultatsettet**, ikke bare første side. Antall unike annonser
  kontrolleres mot treff-tallet kilden selv oppgir, og avvik logges som advarsel.
* **Registrerer salg.** FINN markerer solgte annonser i søket («Solgt», uten pris).
  Det flagget fanges, så du får reell salgstakt — ikke bare tilbudssiden.
* **Lagrer historikk**: første/siste observasjon, prisendringer og liggetid per
  annonse, samt én rad per henting med median/percentiler.
* **Verdikurve** per marked: regresjon på utropspriser mot kilometerstand, med
  spredning. Sammenlignbare innen ±10 000 km vises ved siden av, fordi det tallet
  er lettere å forholde seg til enn en linje.
* **Endringsstrøm** på JSON-API (`/api/vakt/<slug>/endringer?since=…`) som en
  ekstern varsler kan lese.

Utropspriser er ikke oppnådde salgspriser. Kilden viser ikke hva bilene faktisk
gikk for — liggetid og *hva* som ble solgt er det nærmeste vi kommer.

## Kilder

| Kilde | Status | Merknad |
|---|---|---|
| `finn` | i bruk | FINN mobilitet (`/mobility/search/car`), SSR-HTML |
| `auksjonen` | ikke bygget | Auksjonen.no legger objektene sine ut på FINN som vanlige annonser, men bud/minstepris-logikken ligger på auksjonen.no |

## API

```
GET /api/vakter                              alle vakter + nøkkeltall
GET /api/vakt/<slug>                         full markedsdata for én vakt
GET /api/vakt/<slug>/annonser.json           annonser (valgfritt ?kategori=)
GET /api/vakt/<slug>/endringer?since=ISO     hendelser siden tidspunkt
GET /helse                                   helsesjekk (Docker healthcheck)
```

## Drift

* Image: `ghcr.io/terjefl/finn-marked:latest` (bygges av GitHub Actions på push til `main`).
* Data: SQLite i `/data` — bind-mount på verten. Ingen database å drifte.
* Henting: én gang i døgnet per vakt, styrt av `hentetime` i vakt-konfigurasjonen.
  Kjøres også manuelt fra UI-et.
* Tjenesten er tenkt bak Nginx Proxy Manager + Cloudflare Access.

## Lokal kjøring

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
FINN_DATA=./data .venv/bin/uvicorn app.web:app --port 8099
python3 tests/test_parser.py        # parseren (krever ingen avhengigheter)
```

## Importere historikk fra Hermes-scriptet

```bash
python3 scripts/importer.py --kilde ~/.hermes/data/finn_bil_marked/bmw-i4-2022.sqlite \
    --slug bmw-i4-2022 --ny-slug bmw-i4-2022
```

## Legge til en ny kilde

1. Lag `app/kilder/<navn>.py` med en klasse som har `navn` og
   `hent(sok, logg) -> (annonser, treff)`.
2. Normaliser annonsene til felles-formatet i `app/kilder/__init__.py`.
3. Registrer klassen med `registrer(...)` nederst i modulen.
4. I UI-et skrives søket som `<navn> | <url eller parametre>`.
