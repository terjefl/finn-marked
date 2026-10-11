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

    test_finn_koder()

    # Det gamle tekstformatet skal fortsatt parses (brukes av importeren og eldre klienter)
    sjekk("parse_sok", sok.parse_sok("fully | https://www.finn.no/mobility/search/car?q=fully")[0]["tag"], "fully")

    print()
    if feil:
        print(f"{len(feil)} test(er) feilet: {', '.join(feil)}")
        return 1
    print("alle tester OK")
    return 0



# ----------------------------------------------------------------- FINN merke/modell

def test_finn_koder() -> None:
    from app.kilder import finn
    tre = [
        {"navn": "BMW", "kode": "0.749", "modeller": [
            {"navn": "3-Serie", "kode": "1.749.2132"}, {"navn": "i4", "kode": "1.749.8308"},
            {"navn": "iX-Serie", "kode": "1.749.9"}, {"navn": "i3-serie", "kode": "1.749.10"}]},
        {"navn": "Volkswagen", "kode": "0.817", "modeller": [
            {"navn": "ID.4", "kode": "1.817.8392"}, {"navn": "ID.4 GTX", "kode": "1.817.2000553"},
            {"navn": "Golf-Serie", "kode": "1.817.1433"}]},
        {"navn": "Mercedes-Benz", "kode": "0.785", "modeller": [{"navn": "EQC-Klasse", "kode": "1.785.1"}]},
    ]
    f = lambda m, mo: finn.finn_merke_og_modell(m, mo, tre)  # noqa: E731
    sjekk("merke eksakt, modell eksakt (I4→i4)", f("BMW", "I4")["modell"]["kode"], "1.749.8308")
    sjekk("ID.4 velger ikke GTX", f("VOLKSWAGEN", "ID.4")["modell"]["navn"], "ID.4")
    sjekk("ID.4 GTX velger GTX", f("VOLKSWAGEN", "ID.4 GTX")["modell"]["navn"], "ID.4 GTX")
    sjekk("serie-stamme 320D→3-Serie", f("BMW", "320D")["modell"]["navn"], "3-Serie")
    sjekk("serie-stamme IX3→iX-Serie", f("BMW", "IX3")["modell"]["navn"], "iX-Serie")
    sjekk("klasse-stamme EQC", f("MERCEDES-BENZ", "EQC")["modell"]["navn"], "EQC-Klasse")
    sjekk("ukjent modell gir merke + liste", (f("BMW", "Z9")["modell"], len(f("BMW", "Z9")["modeller"])), (None, 4))
    sjekk("ukjent merke", f("Lada", "Niva"), {"merke": None, "modell": None, "modeller": []})
    sjekk("sok_url", finn.sok_url("1.749.8308", 2022, True),
          "https://www.finn.no/mobility/search/car?variant=1.749.8308&year_from=2022&year_to=2022&fuel=4")
    sjekk("sok_url uten år", finn.sok_url("0.749"), "https://www.finn.no/mobility/search/car?variant=0.749")


if __name__ == "__main__":
    sys.exit(hoved())
