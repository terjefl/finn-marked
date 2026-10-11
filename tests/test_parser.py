#!/usr/bin/env python3
"""Enhetstester for kildeparseren (kjøres uten pytest: `python3 tests/test_parser.py`).

Fixturen under er et nedkortet utdrag av ekte FINN-HTML (mobilitet-søk). Den låser
de tre tingene som er lett å brekke: pris, «Solgt»-badge (annonse uten pris), og
sted/selger-splittingen.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.kilder.finn import parse_kort  # noqa: E402

KORT_VANLIG = '''
<article class="relative isolate sf-search-ad card">
<a aria-owns="search-ad-1" class="sf-search-ad-link" href="https://www.finn.no/mobility/item/478402697" id="478402697"></a>
<div class="mobility-search-ad-card-content min-w-0 m-16">
  <h2 class="h4">BMW i4</h2>
  <div class="text-caption mb-4 s-text-subtle truncate">Fully Charged</div>
  <span class="text-caption font-bold inline-block mb-8">2022 &middot; 47 926 km &middot; El &middot; 540 km rekkevidde</span>
  <span class="t3 font-bold">434 532</span> <span class="t4 font-bold">kr</span>
  <div class="flex items-end text-detail min-w-0"><div class="flex-1 s-text-subtle min-w-0">
    <span class="truncate block">Kl&oslash;fta &middot; Rebil</span>
    <span class="truncate block">Forhandler &middot; 6 mnd garanti</span></div></div>
</div></article>
'''

KORT_SOLGT = '''
<article class="relative isolate sf-search-ad card">
<a href="https://www.finn.no/mobility/item/476678416" id="476678416"></a>
<div class="mobility-search-ad-card-content min-w-0 m-16">
  <div class="mb-8 z-20 py-4 px-8 inline-flex bg-[--w-color-badge-warning-background] s-text absolute">Solgt</div>
  <h2 class="h4">BMW i4</h2>
  <div class="text-caption mb-4 s-text-subtle truncate">Laser - H&amp;K - LavKm</div>
  <span class="text-caption font-bold inline-block mb-8">2022 &middot; 32 000 km &middot; El &middot; 501 km rekkevidde</span>
  <div class="flex items-end text-detail min-w-0"><div class="flex-1 s-text-subtle min-w-0">
    <span class="truncate block">Drammen &middot; AUTO B AS</span>
    <span class="truncate block">Forhandler</span></div></div>
</div></article>
'''

KORT_PRIVAT = '''
<article class="relative isolate sf-search-ad card">
<a href="https://www.finn.no/mobility/item/436686385" id="436686385"></a>
<div class="mobility-search-ad-card-content min-w-0 m-16">
  <h2 class="h4">BMW i4</h2>
  <div class="text-caption mb-4 s-text-subtle truncate">Fully Charged</div>
  <span class="text-caption font-bold inline-block mb-8">2022 &middot; 62 900 km &middot; El &middot; Automat</span>
  <span class="t3 font-bold">436 532</span> <span class="t4 font-bold">kr</span>
  <div class="flex items-end text-detail min-w-0"><div class="flex-1 s-text-subtle min-w-0">
    <span class="truncate block">Malvik</span>
    <span class="truncate block">Smidig bilhandel &ndash; Privat &middot; 60 mnd garanti</span></div></div>
</div></article>
'''

feil = []


def sjekk(navn: str, faktisk, ventet) -> None:
    ok = faktisk == ventet
    print(f"{'OK  ' if ok else 'FEIL'} {navn}: {faktisk!r}" + ("" if ok else f" (ventet {ventet!r})"))
    if not ok:
        feil.append(navn)


def main() -> int:
    # Søke-parsingen: kilden skal utledes av URL-en, og taggen kan settes eksplisitt.
    from app.sok import parse_sok
    sok = parse_sok("alle | https://www.finn.no/mobility/search/car?q=x\n"
                     "auksjon | https://www.auksjonen.no/api/category-search/search?category1=1000&q=i4\n"
                     "auksjonen:auksjon2 | https://www.auksjonen.no/x")
    sjekk("finn-søk kilde", sok[0]["kilde"], "finn")
    sjekk("finn-søk tag", sok[0]["tag"], "alle")
    sjekk("auksjonen utledet av URL", sok[1]["kilde"], "auksjonen")
    sjekk("auksjonen tag", sok[1]["tag"], "auksjon")
    sjekk("eksplisitt kilde:tag", (sok[2]["kilde"], sok[2]["tag"]), ("auksjonen", "auksjon2"))

    a = parse_kort(KORT_VANLIG)
    sjekk("pris", a["pris"], 434532)
    sjekk("km", a["km"], 47926)
    sjekk("år", a["aar"], 2022)
    sjekk("rekkevidde", a["rekkevidde"], 540)
    sjekk("sted", a["sted"], "Kløfta")
    sjekk("selger", a["selger"], "Rebil")
    sjekk("selger_type", a["selger_type"], "Forhandler")
    sjekk("kilde_id", a["kilde_id"], "478402697")
    sjekk("pris_type", a["pris_type"], "utropspris")

    b = parse_kort(KORT_SOLGT)
    sjekk("solgt-flagg", b["solgt"], True)
    sjekk("solgt uten pris", b["pris"], None)
    sjekk("solgt pris_type", b["pris_type"], "solgt")
    sjekk("solgt sted", b["sted"], "Drammen")
    sjekk("solgt selger", b["selger"], "AUTO B AS")

    c = parse_kort(KORT_PRIVAT)
    sjekk("privat type", c["selger_type"], "Privat")
    sjekk("privat sted", c["sted"], "Malvik")
    sjekk("privat selger", c["selger"], "Smidig bilhandel")

    # Kategorier utledes av vaktens egne nøkler/visningsnavn (rett 12), ikke av en
    # hardkodet liste per merke.
    from app.kilder.finn import kategoriser
    kat = {"fully": "Fully Charged", "m50": "M50", "edrive40": "eDrive40"}
    a = {"tittel": "BMW i4 M50 xDrive", "undertittel": "Fully Charged"}
    sjekk("kategori: nøkkel og navn", kategoriser(a, kat, []), "Fully Charged,M50")
    sjekk("kategori: nøkkel som eget ord", kategoriser({"tittel": "BMW i4 eDrive40"}, kat, []), "eDrive40")
    sjekk("kategori: ikke del av annet ord",
          kategoriser({"tittel": "Superduper bil"}, {"super": "Supercharged"}, []), "")
    sjekk("kategori: ingen kategorier gir ingen tagger", kategoriser(a, {}, []), "")
    sjekk("kategori: tag fra søket", kategoriser({"tittel": "BMW i4"}, kat, ["m50"]), "M50")

    print()
    if feil:
        print(f"{len(feil)} test(er) feilet: {', '.join(feil)}")
        return 1
    print("alle tester OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
