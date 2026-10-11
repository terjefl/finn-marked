#!/usr/bin/env python3
"""Tester for auksjonen-adapteren: `_aar_km` (år/km ut av tittelen) og `_normaliser`.

Kjør uten pytest: `python3 tests/test_auksjonen.py`.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.kilder.auksjonen import _aar_km, _normaliser  # noqa: E402

feil: list[str] = []


def sjekk(navn: str, faktisk, ventet) -> None:
    ok = faktisk == ventet
    print(f"{'OK  ' if ok else 'FEIL'} {navn}: {faktisk!r}" + ("" if ok else f" (ventet {ventet!r})"))
    if not ok:
        feil.append(navn)


def hoved() -> int:
    # --- _aar_km
    sjekk("tittel med år og km", _aar_km("2022 Iveco DAILY 35S18 varebil, kapellpåbygg, 93311km"),
          (2022, 93311))
    sjekk("år og km med mellomrom", _aar_km("2019 BMW i4 45 000 km"), (2019, 45000))
    sjekk("ingen km", _aar_km("2021 BMW i4"), (2021, None))
    sjekk("ingen år", _aar_km("BMW i4, 88 000 km"), (None, 88000))
    sjekk("tom tittel", _aar_km(""), (None, None))
    sjekk("km over grensen forkastes", _aar_km("BMW i4 1 200 000 km"), (None, None))
    sjekk("eldre årstall", _aar_km("1989 Volvo 240, 120000 km"), (1989, 120000))

    # --- _normaliser: prissemantikken
    fastpris = _normaliser({"objectId": 123, "title": "2022 BMW i4, 45000 km",
                            "type": "BUYNOW", "buyNowPrice": 300000})
    sjekk("objektid blir streng", fastpris["kilde_id"], "123")
    sjekk("url bygges fra objektid", fastpris["url"].endswith("/bruktbil/123"), True)
    sjekk("fastpris", (fastpris["pris"], fastpris["pris_type"]), (300000, "fastpris"))
    sjekk("år og km fra tittel", (fastpris["aar"], fastpris["km"]), (2022, 45000))
    sjekk("kilde", fastpris["kilde"], "auksjonen")
    sjekk("ikke solgt", fastpris["solgt"], False)
    sjekk("km merket som usikker", fastpris["ekstra"]["km_fra_tittel"], True)

    bud = _normaliser({"objectId": 124, "title": "BMW i4",
                       "currentBidAmount": 200000, "startPrice": 150000})
    sjekk("bud slår utropspris", (bud["pris"], bud["pris_type"]), (200000, "bud"))

    bud_og_fastpris = _normaliser({"objectId": 125, "title": "BMW i4", "type": "BUYNOW",
                                   "buyNowPrice": 300000, "currentBidAmount": 210000})
    sjekk("bud går foran fastpris når budet finnes", (bud_og_fastpris["pris"], bud_og_fastpris["pris_type"]),
          (210000, "bud"))

    utrop = _normaliser({"objectId": 126, "title": "BMW i4", "startPrice": 100000})
    sjekk("utropspris på auksjon føres som bud", (utrop["pris"], utrop["pris_type"]), (100000, "bud"))

    uten = _normaliser({"objectId": 127, "title": "BMW i4"})
    sjekk("ingen priser", (uten["pris"], uten["pris_type"]), (None, "ukjent"))

    print()
    if feil:
        print(f"{len(feil)} test(er) feilet: {', '.join(feil)}")
        return 1
    print("alle tester OK")
    return 0


if __name__ == "__main__":
    sys.exit(hoved())
