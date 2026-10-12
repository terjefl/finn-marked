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

        # 7b) Prisindeks i kjøringer: fem annonser med km og pris gir regresjon og indeks ved REF_KM
        from app import analyse
        _FEED["annonser"] = [dict(annonse(str(100 + i), pris=300000 - i * 20000), km=20000 + i * 20000) for i in range(5)]
        _FEED["treff"] = 5
        innsamling.hent_vakt(con, v)
        rad = con.execute("SELECT indeks, ref_km, r2, nye, solgte FROM kjøringer ORDER BY rowid DESC LIMIT 1").fetchone()
        sjekk("indeks ved REF_KM (lineært: 300000 - (60000-20000)*1 kr/km)", (rad["indeks"], rad["ref_km"]), (260000, analyse.REF_KM))
        sjekk("r2 og strøm", (round(rad["r2"], 2), rad["nye"], rad["solgte"]), (1.0, 5, 0))
        sjekk("historikk tar alle rader", len(analyse.historikk(con, "test")) >= 7, True)
        mnd = analyse.maanedsoversikt(con, "test")
        sjekk("månedsoversikt: har rad for denne måneden", any(r["indeks"] == 260000 for r in mnd), True)
        sjekk("svg_historikk tegner indeks", "indeks" in analyse.svg_historikk(analyse.historikk(con, "test")), True)

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

        # 10) Varsling: plukk hendelser etter kriterier og send via stubbede kanaler
        import os as _os

        from app import varsling
        _os.environ["FINN_SMTP_HOST"] = "smtp.test"          # kanalene regnes som satt opp (senderne stubbes)
        _os.environ["FINN_PUSHOVER_TOKEN"] = "app-token"
        sendt: list = []
        varsling.send_epost = lambda til, emne, tekst, html=None: sendt.append(("epost", til, emne))
        varsling.send_pushover = lambda nokkel, tittel, tekst, lenke=None: sendt.append(("pushover", nokkel, tittel))
        con.execute("INSERT INTO brukere (handle, navn, epost, rolle, aktiv, opprettet, pushover, varsel_epost) "
                    "VALUES ('h','Terje','t@x.no','admin',1,?, 'u123', 1)", (db.naa(),))
        bid = con.execute("SELECT id FROM brukere WHERE navn='Terje'").fetchone()["id"]
        v_k = dict(v, slug="kjop", eier=bid, formaal="kjoper", kriterier={"maks_pris": 250000},
                   varsling={"aktiv": True, "typer": ["kandidat", "pris"]})
        db.lagre_vakt(con, v_k)
        _FEED["annonser"] = [dict(annonse(str(300 + i), pris=200000 + i * 30000), km=30000 + i * 15000) for i in range(6)]
        _FEED["treff"] = 6
        fra = varsling.siste_hendelse_id(con, "kjop")
        innsamling.hent_vakt(con, v_k)
        plukk = varsling.plukk_hendelser(con, db.vakt(con, "kjop"), fra)
        sjekk("varsling: bare kandidater under maks pris", sorted(h["d"]["kilde_id"] for h in plukk), ["300", "301"])
        vs = varsling.etter_henting(con, db.vakt(con, "kjop"), fra)
        sjekk("varsling: sendt på begge kanaler", [k for k, *_ in sendt], ["epost", "pushover"])
        sjekk("varsling: emne", sendt[0][2], "Test: 2 nye hendelser")
        sjekk("varsling: status", vs, "varslet 2 hendelser via e-post, pushover")
        sjekk("varsling: sist_varslet satt", bool(db.vakt(con, "kjop")["sist_varslet"]), True)
        sendt.clear()
        fra = varsling.siste_hendelse_id(con, "kjop")
        innsamling.hent_vakt(con, v_k)
        sjekk("varsling: ingenting nytt → ingen melding", (varsling.etter_henting(con, db.vakt(con, "kjop"), fra), sendt), (None, []))
        sjekk("varsling: av → ingen plukk", varsling.plukk_hendelser(con, dict(v_k, varsling={"typer": []}), 0), [])
        sjekk("varsling: aldri lagret → forslag, men av", varsling.varsling_for({"formaal": "eier"}),
              {"aktiv": False, "typer": ["egen", "solgt"], "kanaler": {"epost": True, "pushover": True}})
        sjekk("varsling: lagret med typer → på", varsling.varsling_for({"varsling": {"typer": ["ny"]}})["aktiv"], True)
        # Kanaler: av per bil, og av globalt på kontoen
        sendt.clear()
        v_k2 = dict(v_k, varsling={"typer": ["kandidat", "pris"], "kanaler": {"epost": False, "pushover": True}})
        db.lagre_vakt(con, v_k2)
        _FEED["annonser"] = _FEED["annonser"] + [dict(annonse("399", pris=150000), km=10000)]
        _FEED["treff"] = 7
        fra = varsling.siste_hendelse_id(con, "kjop")
        innsamling.hent_vakt(con, v_k2)
        varsling.etter_henting(con, db.vakt(con, "kjop"), fra)
        sjekk("kanal av per bil: bare pushover", [k for k, *_ in sendt], ["pushover"])
        sendt.clear()
        con.execute("UPDATE brukere SET varsel_pushover=0 WHERE id=?", (bid,))
        _FEED["annonser"] = _FEED["annonser"] + [dict(annonse("398", pris=140000), km=12000)]
        _FEED["treff"] = 8
        fra = varsling.siste_hendelse_id(con, "kjop")
        innsamling.hent_vakt(con, v_k2)
        sjekk("globalt av på kontoen: ingenting sendes", (varsling.etter_henting(con, db.vakt(con, "kjop"), fra), sendt), (None, []))

        # 11) Traktor: aksen følger FINN-URL-en (ikke kilden), timer lagres, indeks ved 3 000 t
        traktor_url = "https://www.finn.no/mobility/search/b2b/agriculturetractor?make=0.3154"
        v_t = dict(v, slug="traktor", sok=[{"kilde": "falsk", "tag": "alle", "url": traktor_url}], egen_timer=2500)
        db.lagre_vakt(con, v_t)
        sjekk("traktor: egen_timer lagret og formål eier", (db.vakt(con, "traktor")["egen_timer"], db.vakt(con, "traktor")["formaal"]),
              (2500, "eier"))
        traktorer = [dict(annonse(str(500 + i), pris=600000 - i * 50000), km=None, timer=1000 + i * 1000, tittel=f"Traktor {i}")
                     for i in range(5)]
        _FEED["annonser"] = traktorer
        _FEED["treff"] = 5
        innsamling.hent_vakt(con, v_t)
        sjekk("traktor: timer lagret, km NULL",
              tuple(con.execute("SELECT timer, km FROM annonser WHERE slug='traktor' AND kilde_id='502'").fetchone()), (3000, None))
        rad = con.execute("SELECT indeks, ref_km, ref_timer FROM kjøringer WHERE slug='traktor' ORDER BY rowid DESC LIMIT 1").fetchone()
        sjekk("traktor: indeks ved 3 000 t, ref_km NULL", (rad["indeks"], rad["ref_km"], rad["ref_timer"]),
              (500000, None, analyse.AKSER["timer"]["ref"]))
        _FEED["annonser"] = [dict(traktorer[0], solgt=True, pris_type="solgt", merke="Solgt")] + traktorer[1:4]
        _FEED["treff"] = 4
        innsamling.hent_vakt(con, v_t)
        th = [t for t in hendelser(con) if t[0] in ("solgt", "borte") and "Traktor" in t[1]]
        sjekk("traktor: hendelsestekster i timer", [h[1].split("(")[1].split(",")[0] for h in th], ["1000 t", "5000 t"])
        sjekk("bil-vakten har fortsatt ref_km",
              con.execute("SELECT ref_km, ref_timer FROM kjøringer WHERE slug='test' AND indeks IS NOT NULL LIMIT 1").fetchone()[0], analyse.REF_KM)
        # Analysefunksjonene med timer-aksen; bil-kallene uten akse er uendret
        t_akse = analyse.AKSER["timer"]
        ann = [dict(a, kilde_id=a["kilde_id"]) for a in traktorer]
        sjekk("oppsummer timer", (analyse.oppsummer(ann, t_akse)["timer_median"], "km_median" in analyse.oppsummer(ann, t_akse)), (3000, False))
        k = analyse.verdikurve(ann, 3000, t_akse)
        sjekk("verdikurve timer: estimat og per 100 t", (round(k["estimat"]), round(k["per_enhet"]), k["egen"]), (500000, 5000, 3000))
        sjekk("km_botter timer: 500-bøtter", [(b["fra"], b["til"]) for b in analyse.km_botter(ann, akse=t_akse)][:2], [(1000, 1500), (2000, 2500)])
        sjekk("nabolag timer ±500", analyse.nabolag(ann, 3200, akse=t_akse)["antall"], 1)
        sjekk("kjoperliste maks_timer", sorted(r["kilde_id"] for r in analyse.kjoperliste(ann, k, {"maks_timer": 2000}, akse=t_akse)), ["500", "501"])
        svg = analyse.svg_kurve(ann, 3000, akse=t_akse)
        sjekk("svg_kurve timer: aksetekst", ("Arbeidstimer" in svg, "Kilometerstand" in svg), (True, False))
        sjekk("svg_historikk timer: pris ved 3 000 t", "pris ved 3 000 t" in analyse.svg_historikk(
            analyse.historikk(con, "traktor") * 2, akse=t_akse), True)
        biler = [dict(annonse(str(i), pris=300000 - i * 20000), km=20000 + i * 20000) for i in range(5)]
        sjekk("bil uten akse: uendret", (analyse.oppsummer(biler)["km_median"], analyse.verdikurve(biler, 60000)["per_1000km"]),
              (60000, 1000.0))
        sjekk("urimelige: hjulsett til 20 000 holdes utenfor", analyse.urimelige(ann + [dict(annonse("599", pris=20000), timer=None)], t_akse), {"599"})
        sjekk("urimelige: traktor til 60 000 (12 %) er med", analyse.urimelige(ann + [dict(annonse("598", pris=60000), timer=7000)], t_akse), set())
        sjekk("urimelige: bil-faktor 10 %", analyse.urimelige(biler + [dict(annonse("99", pris=20000))]), {"99"})

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
