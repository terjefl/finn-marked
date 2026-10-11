#!/usr/bin/env python3
"""Tester for skjema-hjelperne i app/sok.py og kategoriforslagene i app/analyse.py.
Kjør: `python3 tests/test_sok.py` (ingen avhengigheter)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import analyse, sok  # noqa: E402

feil: list[str] = []


def sjekk(navn: str, faktisk, ventet) -> None:
    ok = faktisk == ventet
    print(f"{'OK  ' if ok else 'FEIL'} {navn}: {faktisk!r}" + ("" if ok else f" (ventet {ventet!r})"))
    if not ok:
        feil.append(navn)


def hoved() -> int:
    sjekk("slugify", sok.slugify("BMW i4 2022 (M50 xDrive) Ø"), "bmw-i4-2022-m50-xdrive-oe")
    sjekk("slugify tom", sok.slugify("  --  "), "")
    sjekk("rens_url fjerner page og tomme", sok.rens_url("https://x.no/s?a=1&page=3&q="), "https://x.no/s?a=1")
    sjekk("sjekk_url annonse", sok.sjekk_url("https://www.finn.no/mobility/item/1"), "Dette er én annonse, ikke et søk.")
    sjekk("sjekk_url ok", sok.sjekk_url("https://www.finn.no/mobility/search/car?x=1"), None)
    sjekk("sjekk_url auksjonen", sok.sjekk_url("https://www.auksjonen.no/x"), None)
    sjekk("sjekk_url ukjent", (sok.sjekk_url("https://vg.no/") or "").startswith("Ukjent kilde"), True)
    sjekk("sjekk_url ikke-url", sok.sjekk_url("bmw i4"), "Dette er ikke en nettadresse.")

    s = sok.bygg_sok(["", "fully", "x"], ["https://www.finn.no/mobility/search/car?a=1&page=2",
                                          "https://www.auksjonen.no/x?category1=1000", ""], ["auto", "auto", "auto"])
    sjekk("bygg_sok antall (tom rad hoppes over)", len(s), 2)
    sjekk("bygg_sok finn", s[0], {"kilde": "finn", "tag": "alle", "url": "https://www.finn.no/mobility/search/car?a=1"})
    sjekk("bygg_sok auksjonen utledet", (s[1]["kilde"], s[1]["tag"]), ("auksjonen", "fully"))
    sjekk("bygg_sok overstyrt kilde",
          sok.bygg_sok([""], ["https://www.finn.no/mobility/search/car"], ["auksjonen"])[0]["kilde"], "auksjonen")
    sjekk("bygg_sok uten kilder-liste", sok.bygg_sok(["a"], ["https://www.finn.no/mobility/search/car"])[0]["kilde"], "finn")

    sjekk("bygg_kategorier", sok.bygg_kategorier(["", "m50", ""], ["Fully Charged", "M50", ""]),
          {"fully-charged": "Fully Charged", "m50": "M50"})
    sjekk("bygg_kategorier bare nøkkel", sok.bygg_kategorier(["krok"], [""]), {"krok": "krok"})

    sjekk("navneforslag", sok.navneforslag("https://f.no/s?year_from=2022&year_to=2022", ["BMW i4", "BMW i4", "BMW iX"]),
          ("BMW i4 2022", "bmw-i4-2022"))
    sjekk("navneforslag årsspenn", sok.navneforslag("https://f.no/s?year_from=2021&year_to=2023", ["Tesla Model 3"]),
          ("Tesla Model 3 2021–2023", "tesla-model-3-2021-2023"))
    sjekk("navneforslag uten kort, q", sok.navneforslag("https://f.no/s?q=i4", []), ("i4", "i4"))
    sjekk("navneforslag tomt", sok.navneforslag("https://f.no/s", []), ("", ""))

    ann = [{"undertittel": "Fully Charged - Laser - H&K"}, {"undertittel": "Fully Charged, Soltak"},
           {"undertittel": "M50 · Laser · 2022 · 45 000 km"}, {"undertittel": None}, {"undertittel": "fully charged"}]
    sjekk("kategoriforslag", analyse.kategoriforslag(ann, {"m50": "M50"}), [("Fully Charged", 3), ("Laser", 2)])
    sjekk("kategoriforslag tomt", analyse.kategoriforslag([], {}), [])
    sjekk("kategoriforslag tak (alle har det)", analyse.kategoriforslag([{"undertittel": "X"}] * 10, {}), [])

    # Det gamle tekstformatet skal fortsatt parses (brukes av importeren og eldre klienter)
    sjekk("parse_sok", sok.parse_sok("fully | https://www.finn.no/mobility/search/car?q=fully")[0]["tag"], "fully")

    print()
    if feil:
        print(f"{len(feil)} test(er) feilet: {', '.join(feil)}")
        return 1
    print("alle tester OK")
    return 0


if __name__ == "__main__":
    sys.exit(hoved())
