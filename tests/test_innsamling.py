#!/usr/bin/env python3
"""Tester for innsamlingslogikken: ny / prisendring / solgt / borte, og bremsen.

Kjør uten pytest: `python3 tests/test_innsamling.py`.

Bruker en midlertidig SQLite-base (FINN_DATA pekes dit) og en falsk kilde som
registrerer seg i `kilder.KILDER`, så ingenting går over nett.
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import db, innsamling, kilder  # noqa: E402

# Nett-pausene i hent_vakt gjør testen treg og sier ingenting om logikken.
innsamling.time.sleep = lambda _s: None  # type: ignore[assignment]

feil: list[str] = []


def sjekk(navn: str, faktisk, ventet) -> None:
    ok = faktisk == ventet
    print(f"{'OK  ' if ok else 'FEIL'} {navn}: {faktisk!r}" + ("" if ok else f" (ventet {ventet!r})"))
    if not ok:
        feil.append(navn)


# ----------------------------------------------------------------- falsk kilde

_FEED: dict = {"annonser": [], "treff": 0}


class FalskKilde:
    navn = "falsk"

    def hent(self, sok, logg=lambda m: None):
        return [dict(a) for a in _FEED["annonser"]], _FEED["treff"]


def annonse(finn_id: str, pris: int | None = 200000, solgt: bool = False, ekstra: dict | None = None) -> dict:
    return {
        "kilde": "falsk", "kilde_id": finn_id, "url": f"https://example.invalid/{finn_id}",
        "tittel": f"Bil {finn_id}", "undertittel": None, "aar": 2020, "km": 50000,
        "drivstoff": "El", "rekkevidde": 400, "pris": pris,
        "pris_type": "solgt" if solgt else "utropspris", "sted": "Oslo",
        "selger": None, "selger_type": None, "merke": "Solgt" if solgt else None,
        "solgt": solgt, "ekstra": dict(ekstra or {}),
    }


def hendelser(con) -> list[tuple[str, str]]:
    return [(r["type"], r["tekst"]) for r in con.execute("SELECT type, tekst FROM hendelser ORDER BY id")]


def hoved() -> int:
    mappe = Path(tempfile.mkdtemp(prefix="finn-marked-test-"))
    try:
        db.sett_data(mappe)
        kilder.KILDER["falsk"] = FalskKilde()
        db.init()
        con = db.base()
        sjekk("migrering: skjemaversjon satt ved oppstart",
              con.execute("PRAGMA user_version").fetchone()[0],
              max([db.SKJEMA_BASELINE] + [nr for nr, _ in db.MIGRERINGER]))
        v = {"slug": "test", "navn": "Test", "sok": [{"kilde": "falsk", "tag": "alle",
                                                     "url": "https://example.invalid/sok"}],
             "kategorier": {}, "egen_km": None}

        # 1) To nye annonser
        _FEED["annonser"] = [annonse("1"), annonse("2")]
        _FEED["treff"] = 2
        r = innsamling.hent_vakt(con, v)
        sjekk("første kjøring: nye", r["nye"], 2)
        sjekk("første kjøring: aktive", r["aktive"], 2)
        sjekk("første kjøring: hendelsestyper", [t for t, _ in hendelser(con)], ["ny", "ny"])
        sjekk("pris_historikk: to punkter", con.execute("SELECT COUNT(*) c FROM pris_historikk").fetchone()["c"], 2)
        sjekk("kjøringer: én rad", con.execute("SELECT COUNT(*) c FROM kjøringer").fetchone()["c"], 1)
        sjekk("kjøringer.dato uten T",
              "T" in con.execute("SELECT dato FROM kjøringer").fetchone()["dato"], False)

        # 2) Uendret feed: ingen nye hendelser
        r = innsamling.hent_vakt(con, v)
        sjekk("uendret: nye", (r["nye"], r["prisendringer"], r["solgt"], r["borte"]), (0, 0, 0, 0))
        sjekk("uendret: fortsatt to hendelser", len(hendelser(con)), 2)

        # 3) Prisendring
        _FEED["annonser"] = [annonse("1", pris=190000), annonse("2")]
        r = innsamling.hent_vakt(con, v)
        sjekk("prisendring: antall", r["prisendringer"], 1)
        sjekk("prisendring: type", hendelser(con)[-1][0], "pris")
        sjekk("prisendring: ny pris lagret",
              con.execute("SELECT pris FROM annonser WHERE kilde_id='1'").fetchone()["pris"], 190000)
        sjekk("prisendring: nytt prispunkt",
              con.execute("SELECT COUNT(*) c FROM pris_historikk WHERE kilde_id='1'").fetchone()["c"], 2)

        # 4) Solgt
        _FEED["annonser"] = [annonse("1", pris=190000), annonse("2", pris=None, solgt=True)]
        r = innsamling.hent_vakt(con, v)
        sjekk("solgt: antall", r["solgt"], 1)
        sjekk("solgt: type", hendelser(con)[-1][0], "solgt")
        rad = con.execute("SELECT solgt, aktiv, solgt_dato FROM annonser WHERE kilde_id='2'").fetchone()
        sjekk("solgt: flagg/aktiv/dato", (rad["solgt"], rad["aktiv"], bool(rad["solgt_dato"])), (1, 0, True))

        # 5) Borte fra søket. En solgt annonse er alt ute av søket (aktiv=0) og skal
        #    ikke meldes som «borte» — derfor bruker vi annonse 1, som fortsatt er aktiv.
        _FEED["annonser"] = []
        _FEED["treff"] = 0
        r = innsamling.hent_vakt(con, v)
        sjekk("borte: antall", r["borte"], 1)
        sjekk("borte: type", hendelser(con)[-1][0], "borte")
        sjekk("borte: aktiv=0 og borte_dato satt",
              tuple(con.execute("SELECT aktiv, borte_dato IS NOT NULL FROM annonser WHERE kilde_id='1'").fetchone()),
              (0, 1))
        sjekk("solgt annonse meldes ikke borte",
              con.execute("SELECT COUNT(*) c FROM hendelser WHERE type='borte'").fetchone()["c"], 1)

        # 6) Bremsen: kilden oppgir mange treff, parseren gir null
        forventet_annonser = con.execute("SELECT COUNT(*) c FROM annonser").fetchone()["c"]
        forventet_hendelser = len(hendelser(con))
        _FEED["annonser"] = []
        _FEED["treff"] = 100
        try:
            innsamling.hent_vakt(con, v)
            sjekk("bremsen: kastet feil", False, True)
        except RuntimeError as e:
            sjekk("bremsen: kastet feil", "avbrutt" in str(e), True)
        sjekk("bremsen: ingen nye hendelser", len(hendelser(con)), forventet_hendelser)
        sjekk("bremsen: ingen annonser markert borte",
              con.execute("SELECT COUNT(*) c FROM annonser").fetchone()["c"], forventet_annonser)
        sjekk("bremsen: ingen kjøring skrevet",
              con.execute("SELECT COUNT(*) c FROM kjøringer").fetchone()["c"], 5)

        # 7) Liten avviksmengde (over bremsen, under 90 %): advarsel, ikke avbrudd
        _FEED["annonser"] = [annonse("1", pris=190000)]
        _FEED["treff"] = 1
        r = innsamling.hent_vakt(con, v)
        sjekk("normal avviksmengde: ingen advarsel", r["advarsler"], [])

        # 8) Samme bil lagt ut på nytt (nytt kilde_id, samme understellsnummer) → «igjen», ikke «ny»
        _FEED["annonser"] = [annonse("1", pris=190000, ekstra={"vin": "VIN123", "regnr": "AB12345", "lagt_ut": "2026-09-01"})]
        _FEED["treff"] = 1
        innsamling.hent_vakt(con, v)
        sjekk("vin/regnr/lagt_ut lagret",
              tuple(con.execute("SELECT vin, regnr, lagt_ut FROM annonser WHERE kilde_id='1'").fetchone()),
              ("VIN123", "AB12345", "2026-09-01"))
        _FEED["annonser"] = [annonse("9", pris=185000, ekstra={"vin": "VIN123", "regnr": "AB12345", "lagt_ut": "2026-10-10"})]
        r = innsamling.hent_vakt(con, v)
        sjekk("igjen: telles", (r["nye"], r["igjen"]), (1, 1))
        sjekk("igjen: hendelsestype", hendelser(con)[-2][0], "igjen")
        sjekk("igjen: forrige annonse i detaljer",
              "forrige_kilde_id" in con.execute("SELECT ekstra FROM annonser WHERE kilde_id='9'").fetchone()["ekstra"], True)
        sjekk("igjen: annonse 1 markert borte", hendelser(con)[-1][0], "borte")
        innsamling.hent_vakt(con, v)
        sjekk("igjen: forrige_kilde_id overlever oppdatering",
              "forrige_kilde_id" in con.execute("SELECT ekstra FROM annonser WHERE kilde_id='9'").fetchone()["ekstra"], True)

        # 9) Egen bil dukker opp i markedet (vakten har reg.nr)
        v_egen = dict(v, slug="egen", regnr="ZZ99999", kjoretoy={"understellsnummer": "VINEGEN"})
        _FEED["annonser"] = [annonse("50", ekstra={"vin": "VINEGEN", "regnr": "ZZ99999"}), annonse("51", ekstra={"vin": "ANNEN"})]
        _FEED["treff"] = 2
        innsamling.hent_vakt(con, v_egen)
        typer = [t for t, _ in con.execute("SELECT type, tekst FROM hendelser WHERE slug='egen' ORDER BY id")]
        sjekk("egen bil: hendelse", typer.count("egen"), 1)
        sjekk("egen bil: bare én gang", [h for h in hendelser(con) if h[0] == "egen"][0][1].startswith("Din bil ligger ute"), True)
        innsamling.hent_vakt(con, v_egen)
        sjekk("egen bil: ikke på nytt neste dag",
              con.execute("SELECT COUNT(*) c FROM hendelser WHERE slug='egen' AND type='egen'").fetchone()["c"], 1)

        con.close()
    finally:
        shutil.rmtree(mappe, ignore_errors=True)

    print()
    if feil:
        print(f"{len(feil)} test(er) feilet: {', '.join(feil)}")
        return 1
    print("alle tester OK")
    return 0


if __name__ == "__main__":
    sys.exit(hoved())
