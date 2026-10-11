#!/usr/bin/env python3
"""Tester for kjøretøyoppslaget (app/kjoretoy.py). Kjør: `python3 tests/test_kjoretoy.py`.

Fixturen følger Vegvesenets OpenAPI-skjema for /enkeltoppslag/kjoretoydata
(KjoretoydataResponse). Ingen nettkall."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import kjoretoy  # noqa: E402

feil: list[str] = []


def sjekk(navn: str, faktisk, ventet) -> None:
    ok = faktisk == ventet
    print(f"{'OK  ' if ok else 'FEIL'} {navn}: {faktisk!r}" + ("" if ok else f" (ventet {ventet!r})"))
    if not ok:
        feil.append(navn)


SVAR = {"kjoretoydataListe": [{
    "kjoretoyId": {"kjennemerke": "EB12345", "understellsnummer": "WBY31AW0X0FL12345"},
    "forstegangsregistrering": {"registrertForstegangNorgeDato": "2022-03-18"},
    "registrering": {"registrertForstegangPaEierskap": "2024-06-01",
                     "registreringsstatus": {"kodeNavn": "Registrert"}},
    "periodiskKjoretoyKontroll": {"kontrollfrist": "2026-03-18"},
    "godkjenning": {"tekniskGodkjenning": {"tekniskeData": {
        "generelt": {"merke": [{"merke": "BMW", "merkeKode": "0043"}], "handelsbetegnelse": ["I4"],
                     "typebetegnelse": "M50 xDrive"},
        "karosseriOgLasteplan": {"rFarge": [{"kodeNavn": "Grå"}]},
        "miljodata": {"miljoOgdrivstoffGruppe": [{"drivstoffKodeMiljodata": {"kodeNavn": "Elektrisk"},
                                                  "forbrukOgUtslipp": [{"rekkeviddeKm": 510}, {"rekkeviddeKm": 0}]}]},
        "motorOgDrivverk": {"utelukkendeElektriskDrift": True, "hybridElektriskKjoretoy": False,
                            "motor": [{"drivstoff": [{"maksNettoEffekt": 400.0}]}]},
        "vekter": {"egenvekt": 2290, "tillattTotalvekt": 2605, "tillattTilhengervektMedBrems": 1600},
        "persontall": {"sitteplasserTotalt": 5},
        "dimensjoner": {"lengde": 4783, "bredde": 1852},
        "dekkOgFelg": {"akselDekkOgFelgKombinasjon": [{"akselDekkOgFelg": [{"dekkdimensjon": "245/40R19"}]}]},
    }}},
}]}


def hoved() -> int:
    sjekk("rens_regnr", kjoretoy.rens_regnr(" eb 12-345 "), "EB12345")
    for ugyldig in ("", "E", "EB1234567890", "EB 12*45"):
        try:
            kjoretoy.rens_regnr(ugyldig)
            sjekk(f"rens_regnr avviser {ugyldig!r}", False, True)
        except kjoretoy.Oppslagsfeil:
            sjekk(f"rens_regnr avviser {ugyldig!r}", True, True)

    k = kjoretoy.normaliser(SVAR, "EB12345")
    sjekk("merke/modell", (k["merke"], k["modell"], k["typebetegnelse"]), ("BMW", "I4", "M50 xDrive"))
    sjekk("beskrivelse", k["beskrivelse"], "BMW I4 M50 xDrive 2022")
    sjekk("årsmodell", k["aar"], 2022)
    sjekk("første registrering", k["forste_registrert"], "2022-03-18")
    sjekk("på eier siden", k["registrert_paa_eier"], "2024-06-01")
    sjekk("drivstoff", k["drivstoff"], "Elektrisk")
    sjekk("elektrisk", k["elektrisk"], True)
    sjekk("effekt", (k["effekt_kw"], kjoretoy.hk(k["effekt_kw"])), (400.0, 544))
    sjekk("rekkevidde (største, ikke 0)", k["rekkevidde_km"], 510)
    sjekk("farge", k["farge"], "Grå")
    sjekk("eu-frist", k["eu_frist"], "2026-03-18")
    sjekk("tilhengervekt", k["tilhengervekt"], 1600)
    sjekk("understellsnummer", k["understellsnummer"], "WBY31AW0X0FL12345")
    sjekk("seter/mål/dekk", (k["seter"], k["lengde"], k["dekk"]), (5, 4783, "245/40R19"))
    sjekk("modellår fra VIN (J=2018, registrert 2017)", kjoretoy.modellaar_fra_vin("WV1ZZZ2KZJX026311", 2017), 2018)
    sjekk("modellår fra VIN: P=2023", kjoretoy.modellaar_fra_vin("WBY31AW04PFN73190", 2022), 2023)
    sjekk("modellår fra VIN: ugyldig tegn", kjoretoy.modellaar_fra_vin("WBY31AW04OFN73190", 2022), None)
    sjekk("modellår fra VIN: for langt fra registreringsåret", kjoretoy.modellaar_fra_vin("WV1ZZZ2KZJX026311", 2010), None)
    sjekk("fixture: modellår F=2015 avvist mot 2022", k["modellaar"], None)
    sjekk("vertikal: bil", kjoretoy.finn_vertikal({"klasse": "Personbil", "klassekode": "M1"}), "car")
    sjekk("vertikal: bobil", kjoretoy.finn_vertikal({"klasse": "Campingbil", "klassekode": "M1"}), "mobilehome")
    sjekk("vertikal: campingvogn", kjoretoy.finn_vertikal({"klasse": "Campingtilhenger", "klassekode": "O2"}), "caravan")
    sjekk("vertikal: MC", kjoretoy.finn_vertikal({"klasse": "Motorsykkel", "klassekode": "L3e"}), "mc")
    sjekk("typekode holdes ute av beskrivelsen",
          kjoretoy.beskrivelse({"merke": "VOLKSWAGEN", "modell": "CADDY", "typebetegnelse": "2KN", "aar": 2017}),
          "Volkswagen Caddy 2017")
    sjekk("kort utstyrsnavn uten tall blir med",
          kjoretoy.beskrivelse({"merke": "VOLKSWAGEN", "modell": "ID.4", "typebetegnelse": "GTX", "aar": 2023}),
          "Volkswagen ID.4 GTX 2023")
    sjekk("navn-aktig typebetegnelse blir med",
          kjoretoy.beskrivelse({"merke": "TESLA", "modell": "MODEL 3", "typebetegnelse": "Long Range", "aar": 2021}),
          "Tesla Model 3 Long Range 2021")

    # Bruktimport: første registrering i utlandet er bilens alder
    imp = {"kjoretoydataListe": [{"kjoretoyId": {"kjennemerke": "BT11614"},
            "forstegangsregistrering": {"registrertForstegangNorgeDato": "2014-09-23"},
            "godkjenning": {"forstegangsGodkjenning": {"forstegangRegistrertDato": "2011-06-20",
                            "bruktimport": {"importland": {"landNavn": "TYSKLAND"}, "kilometerstand": 60185}},
                            "tekniskGodkjenning": {"tekniskeData": {"generelt": {"merke": [{"merke": "BMW"}], "handelsbetegnelse": ["X5 XDRIVE30D"]}}}}}]}
    i = kjoretoy.normaliser(imp)
    sjekk("import: år fra opprinnelig registrering", (i["aar"], i["forste_registrert"], i["forste_registrert_norge"]),
          (2011, "2011-06-20", "2014-09-23"))
    sjekk("import: land og km", (i["importland"], i["importert_km"]), ("TYSKLAND", 60185))
    sjekk("ikke import: ingen norsk dato for seg", k["forste_registrert_norge"], None)

    # Feilveier
    for navn, data in (("tomt svar", {"kjoretoydataListe": []}),
                       ("feilmelding", {"feilmelding": "Ugyldig kjennemerke"}),
                       ("ikke dict", [])):
        try:
            kjoretoy.normaliser(data, "X")
            sjekk(f"normaliser feiler på {navn}", False, True)
        except kjoretoy.Oppslagsfeil:
            sjekk(f"normaliser feiler på {navn}", True, True)

    # Minimalt svar skal ikke krasje
    m = kjoretoy.normaliser({"kjoretoydataListe": [{"kjoretoyId": {"kjennemerke": "AB1234"}}]})
    sjekk("minimalt svar", (m["regnr"], m["merke"], m["beskrivelse"]), ("AB1234", None, ""))

    # Uten nøkkel: tydelig feil, ingen nettkall
    kjoretoy.os.environ.pop("FINN_SVV_NOKKEL", None)
    try:
        kjoretoy.slaa_opp("EB12345")
        sjekk("uten nøkkel", False, True)
    except kjoretoy.Oppslagsfeil as e:
        sjekk("uten nøkkel", "FINN_SVV_NOKKEL" in str(e), True)

    print()
    if feil:
        print(f"{len(feil)} test(er) feilet: {', '.join(feil)}")
        return 1
    print("alle tester OK")
    return 0


if __name__ == "__main__":
    sys.exit(hoved())
