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

    # JSON-API-et (primærkilden nå): ett dokument til salgs, ett solgt
    from app.kilder.finn import api_url, fra_api_doc, les_api_side
    doc = {"id": "477918904", "type": "motor", "heading": "BMW i4", "model_specification": "M50 Supercharged",
           "year": 2022, "mileage": 70725, "fuel": "El", "driving_range": 419, "location": "Elverum",
           "dealer_segment": "Privat", "organisation_name": None, "regno": "EE 64976",
           "chassis_number": "wby31aw04pfn73190", "timestamp": 1791718755000, "flags": [], "labels": [],
           "price": {"amount": 449000, "currency_code": "NOK"}, "registration_class": {"id": 1, "value": "Personbil"},
           "transmission": "Automat", "warranty_duration": 12, "canonical_url": "https://www.finn.no/mobility/item/477918904"}
    d = fra_api_doc(doc)
    sjekk("api: kilde_id/url", (d["kilde_id"], d["url"]), ("477918904", "https://www.finn.no/mobility/item/477918904"))
    sjekk("api: tittel/undertittel", (d["tittel"], d["undertittel"]), ("BMW i4", "M50 Supercharged"))
    sjekk("api: tall", (d["aar"], d["km"], d["pris"], d["rekkevidde"]), (2022, 70725, 449000, 419))
    sjekk("api: pris_type/solgt", (d["pris_type"], d["solgt"], d["merke"]), ("utropspris", False, None))
    sjekk("api: sted/selger", (d["sted"], d["selger_type"], d["selger"]), ("Elverum", "Privat", None))
    sjekk("api: regnr/vin normalisert", (d["ekstra"]["regnr"], d["ekstra"]["vin"]), ("EE64976", "WBY31AW04PFN73190"))
    sjekk("api: lagt_ut fra timestamp", d["ekstra"]["lagt_ut"], "2026-10-11")
    solgt = dict(doc, id="1", flags=["sold", "right_to_exchange"], labels=[{"id": "sold"}], price={"amount": 449900})
    e = fra_api_doc(solgt)
    sjekk("api: solgt beholder prisen", (e["solgt"], e["pris"], e["pris_type"], e["merke"]), (True, 449900, "solgt", "Solgt"))
    sjekk("api: uten pris", fra_api_doc(dict(doc, price=None))["pris_type"], "ukjent")
    sjekk("api: ikke-motor hoppes over", fra_api_doc(dict(doc, type="realestate")), None)
    sjekk("api_url beholder filtre, bytter page",
          api_url("https://www.finn.no/mobility/search/car?variant=1.749.8308&year_from=2022&page=3", 2),
          "https://www.finn.no/mobility/search/api/search/SEARCH_ID_CAR_USED?variant=1.749.8308&year_from=2022&page=2")
    import json as _json
    ann, treff, sider = les_api_side(_json.dumps({"docs": [doc, solgt], "metadata": {"result_size": {"match_count": 61}, "paging": {"last": 2}}}))
    sjekk("les_api_side", (len(ann), treff, sider), (2, 61, 2))
    try:
        les_api_side('{"feil": 1}')
        sjekk("les_api_side avviser svar uten docs", False, True)
    except RuntimeError:
        sjekk("les_api_side avviser svar uten docs", True, True)
    sjekk("api: bil har ikke timer", d["timer"], None)

    # Traktor: arbeidstimer i stedet for kilometer, ingen model_specification, år 0/1900 = ukjent
    from app.kilder.finn import parse_detaljer
    traktor = {"id": "478604228", "type": "motor", "heading": "Fiat 60-90", "year": 1987, "hours_used": 2758,
               "mileage_unit": "KILOMETER", "make": "Fiat", "model": "60-90", "location": "Norheimsund",
               "dealer_segment": "Privat", "timestamp": 1791833897000, "flags": [], "labels": [],
               "price": {"amount": 185000, "currency_code": "NOK"}, "sales_form": 1,
               "canonical_url": "https://www.finn.no/mobility/item/478604228"}
    t = fra_api_doc(traktor)
    sjekk("traktor: timer, ikke km", (t["timer"], t["km"]), (2758, None))
    sjekk("traktor: tittel uten undertittel", (t["tittel"], t["undertittel"]), ("Fiat 60-90", None))
    sjekk("traktor: år/pris/drivstoff", (t["aar"], t["pris"], t["drivstoff"], t["rekkevidde"]), (1987, 185000, None, None))
    sjekk("traktor: merke/modell i ekstra", (t["ekstra"]["merke"], t["ekstra"]["modell"]), ("Fiat", "60-90"))
    sjekk("traktor: år 1900 er ukjent", fra_api_doc(dict(traktor, year=1900))["aar"], None)
    sjekk("traktor: uten timer", fra_api_doc({k: v for k, v in traktor.items() if k != "hours_used"})["timer"], None)
    sjekk("api_url traktor", api_url("https://www.finn.no/mobility/search/b2b/agriculturetractor?make=0.3154&year_from=2015", 2),
          "https://www.finn.no/mobility/search/api/search/SEARCH_ID_AGRICULTURE_TRACTOR?make=0.3154&year_from=2015&page=2")
    tr_html = "<h1>Fiat 60-90</h1><dt>Modellår</dt><dd>1987</dd><dt>Vekt</dt><dd>Ikke oppgitt</dd><dt>Effekt</dt><dd>60 hk</dd>" \
              "<dt>Arbeidstimer</dt><dd>2&nbsp;758</dd><h2>Beskrivelse</h2><p>Fin Fiat med lavt timeantal.</p><p>Selges privat.</p>" \
              "<h2>Spesifikasjoner</h2><dt>Merke</dt><dd>Fiat</dd>"
    pd = parse_detaljer(tr_html)
    sjekk("parse_detaljer traktor: felter", (pd["felter"]["Arbeidstimer"], pd["felter"]["Effekt"], pd["felter"]["Vekt"],
                                            pd["felter"]["Kilometerstand"]), ("2\xa0758", "60 hk", "Ikke oppgitt", None))
    sjekk("parse_detaljer traktor: beskrivelse", pd["beskrivelse"].startswith("Fin Fiat med lavt timeantal. Selges privat."), True)
    bil_html = "<button>Beskrivelse</button><dt>Kilometerstand</dt><dd>70 725 km</dd><h2>Om bilen</h2><p>Pen bil.</p>"
    sjekk("parse_detaljer bil: «Om bilen» vinner over «Beskrivelse»", parse_detaljer(bil_html)["beskrivelse"].startswith("Pen bil."), True)

    print()
    if feil:
        print(f"{len(feil)} test(er) feilet: {', '.join(feil)}")
        return 1
    print("alle tester OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
