"""Auksjonen.no som kilde.

Kartlagt 11.10.2026 (se skillen `finn-bil-marked`): auksjonen.no er en
Angular-SSR-app med et eget, åpent JSON-API under /api/ på samme host. Ingen
cookies eller innlogging for lesing, ingen rate-limit-headere observert — men
hold deg til ~1 kall/sekund.

Søket i en vakt ser slik ut:
    {"kilde": "auksjonen", "tag": "auksjon", "category1": "1000", "q": "BMW i4"}

`category1=1000` er Bil (MC=1001, Båt=1002, Caravan=1003, Lastebil/henger=1004,
Bygg og anlegg=1006, Landbruk=1007). `q` er fritekst.

Merknader som styrer normaliseringen:
  * `category-search/search` returnerer KUN aktive auksjoner, og `orderBy` MÅ
    være med — uten den svarer API-et stille {"size":0,"items":[]}.
  * `from` er 1-basert og `to` er inklusiv.
  * Prissemantikken er en annen enn FINN: `currentBidAmount` er høyeste bud,
    `startPrice` er utrops-/minstepris (0 = uten minstepris) og `buyNowPrice` er
    fastpris. Derfor settes `pris_type` til «bud» eller «fastpris» slik at
    analyse og UI kan holde bud unna verdikurven (bud er ikke en utropspris).
  * Kilometerstand og årsmodell er IKKE strukturerte i listesvaret — de står som
    regel i tittelen. Vi henter dem ut derfra med et forsøk, og markerer dem som
    usikre (`ekstra.km_fra_tittel`). Detalj-endepunktet `/api/auction/<id>` har
    dem strukturert i `details`-lista, men koster ett kall per objekt.
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/126.0 Safari/537.36")
BASE = "https://www.auksjonen.no"
SLEEP = 1.0
SIDE = 50          # objekter per side
MAKS_SIDER = 20    # sikkerhetsgrense (1000 objekter)


def _json(url: str, timeout: int = 40, forsok: int = 3) -> dict | list:
    siste: Exception | None = None
    for n in range(forsok):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": UA,
                "Accept": "application/json",
                "Accept-Language": "nb-NO,nb;q=0.9",
            })
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as e:
            siste = e
            time.sleep(2 * (n + 1))
    raise RuntimeError(f"henting feilet for {url}: {siste}")


def _aar_km(tittel: str) -> tuple[int | None, int | None]:
    """År og kilometerstand ligger som tekst i tittelen, f.eks.
    «2022 Iveco DAILY 35S18 varebil, kapellpåbygg …, 93311km». Beste forsøk."""
    aar = None
    m = re.search(r"\b(19[89]\d|20[0-4]\d)\b", tittel or "")
    if m:
        aar = int(m.group(1))
    km = None
    # Lookbehind: tallet må starte et eget ord. Uten den ble «BMW i4 45 000 km» til
    # 445 000 km, fordi «4» fra «i4» ble limt sammen med kilometerstanden.
    m = re.search(r"(?<![\w.,])(\d[\d\s\xa0.]{2,9})\s*km\b", (tittel or "").lower())
    if m:
        d = re.sub(r"[^\d]", "", m.group(1))
        if d and int(d) < 1_000_000:      # «1500 km» og «1 200 000» er støy
            km = int(d)
    return aar, km


def _normaliser(o: dict) -> dict:
    tittel = (o.get("title") or "").strip()
    aar, km = _aar_km(tittel)
    bud = o.get("currentBidAmount") or 0
    fastpris = o.get("buyNowPrice") or 0
    utrop = o.get("startPrice") or 0
    if o.get("type") == "BUYNOW" and fastpris and not bud:
        pris, pris_type = fastpris, "fastpris"
    elif bud:
        pris, pris_type = bud, "bud"
    elif utrop:
        pris, pris_type = utrop, "bud"       # utropspris på auksjon = åpningsbud
    else:
        pris, pris_type = None, "ukjent"
    objekt = o.get("objectId")
    return {
        "kilde": "auksjonen",
        "kilde_id": str(objekt),
        "url": f"{BASE}/auksjon/bruktbil/{objekt}",
        "tittel": tittel[:120] or None,
        "undertittel": o.get("auctionTypeLabel") or o.get("type"),
        "aar": aar,
        "km": km,
        "drivstoff": None,
        "rekkevidde": None,
        "pris": int(pris) if pris else None,
        "pris_type": pris_type,
        "sted": o.get("city"),
        "selger": None,
        "selger_type": None,
        "merke": None,
        "solgt": False,
        "ekstra": {
            "bud": bud or None,
            "startpris": utrop or None,
            "fastpris": fastpris or None,
            "antall_bud": o.get("bidCount"),
            "antall_bydere": o.get("bidderCount"),
            "slutter": o.get("endTime"),
            "auksjonstype": o.get("auctionTypeLabel") or o.get("type"),
            "reservert_pris_nadd": o.get("reservePriceReached"),
            "uten_minstepris": o.get("noReservePrice"),
            "kategori2": o.get("category2"),
            "km_fra_tittel": km is not None,
        },
    }


class AuksjonenKilde:
    """Auksjonen.no: aktive auksjoner i en kategori, valgfritt fritekstfiltrert."""

    navn = "auksjonen"

    def hent(self, sok: dict, logg=lambda m: None) -> tuple[list[dict], int]:
        # Søket kan oppgis som en ferdig auksjonen.no-URL (samme mønster som FINN-søket
        # i UI-et) — da leser vi parametrene ut av query-strengen. Eksplisitte felt i
        # søkeobjektet går foran.
        parametre: dict[str, str] = {}
        if sok.get("url"):
            qs = urllib.parse.urlparse(sok["url"]).query
            parametre = {k: v[0] for k, v in urllib.parse.parse_qs(qs).items()}

        def verdi(navn: str, standard: str | None = None):
            return sok.get(navn) or parametre.get(navn) or standard

        kategori = str(verdi("category1", "1000"))
        q = verdi("q")
        sortering = verdi("orderBy", "endTime")
        retning = "false" if str(verdi("asc", "true")).lower() == "false" else "true"
        maks = int(verdi("maks", "200") or 200)
        annonser: dict[str, dict] = {}
        totalt = 0
        frm = 1
        for _ in range(MAKS_SIDER):
            url = (f"{BASE}/api/category-search/search?category1={kategori}"
                   f"&orderBy={sortering}&asc={retning}&from={frm}&to={frm + SIDE - 1}")
            if q:
                url += "&q=" + urllib.request.quote(q)
            data = _json(url)
            if not isinstance(data, dict):
                raise RuntimeError(f"uventet svar fra auksjonen.no: {str(data)[:200]}")
            totalt = data.get("size") or 0
            items = data.get("items") or []
            if not items:
                break
            for o in items:
                a = _normaliser(o)
                if a["kilde_id"] not in annonser:
                    annonser[a["kilde_id"]] = a
            logg(f"auksjonen: {len(annonser)}/{totalt} objekter (from={frm})")
            if len(annonser) >= min(maks, totalt):
                break
            frm += SIDE
            time.sleep(SLEEP)
        return list(annonser.values()), totalt


from . import registrer  # noqa: E402

registrer(AuksjonenKilde())
