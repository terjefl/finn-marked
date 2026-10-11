"""Innsamling: henter alle søk for én vakt gjennom kildelaget, lagrer og logger hendelser.

Lageret er felles for alle vakter (se db.py); alt nøkles på (slug, kilde, kilde_id).
"""

from __future__ import annotations

import contextlib
import json
import statistics
import threading
import time
from datetime import date

from . import analyse, db, kilder

#: Vakter som hentes nå. Delt mellom web-ruta («Oppdater nå») og scheduleren, som
#: ellers kunne hente samme vakt samtidig og skrive over hverandres annonser.
_aktive: set[str] = set()
_aktive_laas = threading.Lock()


def er_opptatt(slug: str) -> bool:
    """Kjører det en henting for denne vakten nå?"""
    with _aktive_laas:
        return slug in _aktive


@contextlib.contextmanager
def hentelaas(slug: str):
    """Tar hentelåsen for én vakt. Gir False (uten å låse) hvis den alt er tatt."""
    with _aktive_laas:
        if slug in _aktive:
            yield False
            return
        _aktive.add(slug)
    try:
        yield True
    finally:
        with _aktive_laas:
            _aktive.discard(slug)


def hent_vakt(con, v: dict, logg=lambda m: None) -> dict:
    """Henter alle søk for vakten, oppdaterer basen og returnerer en oppsummering."""
    start = time.monotonic()
    slug = v["slug"]
    alle: dict[tuple[str, str], dict] = {}
    treff: dict[str, int] = {}
    advarsler: list[str] = []

    avbrutt: list[str] = []
    for sok in v["sok"]:
        navn = sok.get("kilde") or "finn"
        ann, t = kilder.hent(sok, logg)
        treff[navn] = max(treff.get(navn, 0), t)
        if t and len(ann) < t * 0.1:
            # Parseren har åpenbart feilet (eller siden er endret): da skal ingenting
            # skrives. Uten denne bremsen ble hele markedet markert som «borte» og
            # hendelsesloggen fylt opp med falske hendelser.
            avbrutt.append(f"«{sok.get('tag')}» ({navn}): fant {len(ann)} unike, kilden oppgir {t}")
        elif t and len(ann) < t * 0.9:
            advarsler.append(f"«{sok.get('tag')}» ({navn}): fant {len(ann)} unike, kilden oppgir {t}")
        for a in ann:
            nokkel = (a["kilde"], a["kilde_id"])
            tag = sok.get("tag") or ""
            if nokkel in alle:
                alle[nokkel]["sok_tagger"].update({tag} if tag else set())
            else:
                a["sok_tagger"] = {tag} if tag else set()
                alle[nokkel] = a
        time.sleep(1.0)

    if avbrutt:
        raise RuntimeError("avbrutt, ingenting skrevet — parsingen ga nesten ingen treff: "
                           + "; ".join(avbrutt))

    nye = prisendret = solgte = borte = igjen = 0
    i_dag = date.today().isoformat()
    sett = set(alle)
    egen = _egen_bil(v)
    for (kilde, kilde_id), a in alle.items():
        a["tagger"] = kilder_finn_kategoriser(a, v, sorted(x for x in a.pop("sok_tagger") if x))
        ekstra = a.get("ekstra") or {}
        regnr, vin, lagt_ut = ekstra.get("regnr"), ekstra.get("vin"), ekstra.get("lagt_ut")
        rad = con.execute("SELECT * FROM annonser WHERE slug=? AND kilde=? AND kilde_id=?",
                          (slug, kilde, kilde_id)).fetchone()
        if rad is None:
            nye += 1
            forrige = _samme_bil(con, slug, kilde, kilde_id, vin, regnr)
            if forrige:
                igjen += 1
                ekstra["forrige_kilde_id"] = forrige["kilde_id"]
                a["ekstra"] = ekstra
            con.execute(
                """INSERT INTO annonser (slug, kilde, kilde_id, tittel, undertittel, aar, km, drivstoff,
                       rekkevidde, pris, pris_type, sted, selger, selger_type, url, tagger, sok_tagger,
                       ekstra, forst_sett, sist_sett, borte_dato, aktiv, merke, solgt, solgt_dato,
                       regnr, vin, lagt_ut)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,?,?,?,?,?,?,?)""",
                (slug, kilde, kilde_id, a["tittel"], a["undertittel"], a["aar"], a["km"], a["drivstoff"],
                 a["rekkevidde"], a["pris"], a["pris_type"], a["sted"], a["selger"], a["selger_type"],
                 a["url"], a["tagger"], json.dumps(a.get("sok_tagger_liste") or [], ensure_ascii=False),
                 json.dumps(a.get("ekstra") or {}, ensure_ascii=False), i_dag, i_dag,
                 1 if not a["solgt"] else 0, a["merke"], 1 if a["solgt"] else 0,
                 i_dag if a["solgt"] else None, regnr, vin, lagt_ut))
            if a["pris"]:
                con.execute("""INSERT INTO pris_historikk (slug, kilde, kilde_id, dato, pris, pris_type)
                               VALUES (?,?,?,?,?,?)""",
                            (slug, kilde, kilde_id, i_dag, a["pris"], a["pris_type"]))
            detaljer = {"pris": a["pris"], "km": a["km"], "sted": a["sted"], "url": a["url"],
                        "kilde": kilde, "kilde_id": kilde_id, "pris_type": a["pris_type"]}
            if forrige and not a["solgt"]:
                detaljer["forrige"] = {"kilde_id": forrige["kilde_id"], "pris": forrige["pris"],
                                       "sist_sett": forrige["sist_sett"], "solgt": forrige["solgt"]}
                db.hendelse(con, slug, "igjen", a["url"],
                            f"Lagt ut på nytt: {_tittel(a)} {_pris(a['pris'], a['pris_type'])} "
                            f"(forrige annonse {_pris(forrige['pris'], forrige['pris_type'])}, "
                            f"sist sett {forrige['sist_sett']}{', solgt' if forrige['solgt'] else ''})",
                            detaljer)
            else:
                db.hendelse(con, slug, "solgt" if a["solgt"] else "ny", a["url"],
                            f"{'Solgt: ' if a['solgt'] else 'Ny annonse: '}{_tittel(a)}".strip(), detaljer)
            if egen and _er_egen(egen, regnr, vin):
                db.hendelse(con, slug, "egen", a["url"],
                            f"Din bil ligger ute: {_tittel(a)} {_pris(a['pris'], a['pris_type'])}",
                            {"pris": a["pris"], "km": a["km"], "url": a["url"], "kilde": kilde,
                             "kilde_id": kilde_id, "regnr": regnr})
        else:
            if not rad["solgt"] and a["solgt"]:
                solgte += 1
                db.hendelse(con, slug, "solgt", a["url"],
                            f"Solgt: {_tittel(a)} ({rad['km'] or a['km']} km, "
                            f"sist {_pris(rad['pris'], rad['pris_type'])})",
                            {"pris": rad["pris"], "km": rad["km"], "url": a["url"], "kilde": kilde})
            elif rad["pris"] != a["pris"] and a["pris"] and not a["solgt"]:
                prisendret += 1
                db.hendelse(con, slug, "pris", a["url"],
                            f"Prisendring: {_tittel(a)} {_pris(rad['pris'], rad['pris_type'])} → "
                            f"{_pris(a['pris'], a['pris_type'])}",
                            {"fra": rad["pris"], "til": a["pris"], "url": a["url"], "kilde": kilde,
                             "pris_type": a["pris_type"]})
                con.execute("""INSERT INTO pris_historikk (slug, kilde, kilde_id, dato, pris, pris_type)
                               VALUES (?,?,?,?,?,?)""",
                            (slug, kilde, kilde_id, i_dag, a["pris"], a["pris_type"]))
            con.execute(
                """UPDATE annonser SET tittel=?, undertittel=?, aar=?, km=?, drivstoff=?, rekkevidde=?,
                       pris=COALESCE(?, pris), pris_type=?, sted=?, selger=?, selger_type=?, tagger=?,
                       ekstra=?, sist_sett=?, aktiv=?, borte_dato=NULL, merke=?, solgt=?,
                       solgt_dato=COALESCE(?, solgt_dato),
                       regnr=COALESCE(?, regnr), vin=COALESCE(?, vin), lagt_ut=COALESCE(?, lagt_ut)
                   WHERE slug=? AND kilde=? AND kilde_id=?""",
                (a["tittel"], a["undertittel"], a["aar"], a["km"], a["drivstoff"], a["rekkevidde"],
                 a["pris"], a["pris_type"], a["sted"], a["selger"], a["selger_type"], a["tagger"],
                 json.dumps(_behold_ekstra(rad, a), ensure_ascii=False), i_dag,
                 0 if a["solgt"] else 1, a["merke"], 1 if a["solgt"] else rad["solgt"] or 0,
                 i_dag if (a["solgt"] and not rad["solgt"]) else None,
                 regnr, vin, lagt_ut, slug, kilde, kilde_id))

    for rad in con.execute("SELECT * FROM annonser WHERE slug=? AND aktiv=1", (slug,)).fetchall():
        if (rad["kilde"], rad["kilde_id"]) not in sett:
            borte += 1
            con.execute("UPDATE annonser SET aktiv=0, borte_dato=? WHERE slug=? AND kilde=? AND kilde_id=?",
                        (i_dag, slug, rad["kilde"], rad["kilde_id"]))
            db.hendelse(con, slug, "borte", rad["url"],
                        f"Forsvant fra søket: {rad['tittel']} {rad['undertittel'] or ''} "
                        f"({rad['km']} km, {_pris(rad['pris'], rad['pris_type'])})",
                        {"pris": rad["pris"], "km": rad["km"], "url": rad["url"], "kilde": rad["kilde"]})

    ute = analyse.urimelige([dict(a, kilde_id=k[1]) for k, a in alle.items()])
    priser = sorted(a["pris"] for (_, kid), a in alle.items() if a["pris"] and not a["solgt"] and kid not in ute)

    def pct(p: float):
        if not priser:
            return None
        return priser[min(len(priser) - 1, int(round(p / 100 * (len(priser) - 1))))]

    varighet = round(time.monotonic() - start, 1)
    treff_totalt = sum(treff.values()) if len(treff) > 1 else (max(treff.values()) if treff else 0)
    # Prisindeks: hva markedet ber for en bil med REF_KM på telleren, uavhengig av hvilke
    # biler som tilfeldigvis ligger ute. Medianen faller av seg selv når bilene eldes.
    kurve = analyse.verdikurve([dict(a, kilde_id=k[1]) for k, a in alle.items() if not a["solgt"]], analyse.REF_KM)
    indeks = int(round(kurve["estimat"])) if kurve and kurve.get("estimat") else None
    con.execute("""INSERT INTO kjøringer (slug, dato, annonser_hentet, treff, aktive, median, p25, p75,
                                          min, max, varighet, indeks, ref_km, r2, nye, solgte)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (slug, db.naa(), len(alle), treff_totalt, len(priser),
                 statistics.median(priser) if priser else None, pct(25), pct(75),
                 priser[0] if priser else None, priser[-1] if priser else None, varighet,
                 indeks, analyse.REF_KM if indeks else None, round(kurve["r2"], 3) if kurve else None,
                 nye, solgte))
    kilder_txt = ", ".join(f"{k}: {n}" for k, n in sorted(treff.items()))
    status = (f"OK: {len(alle)} annonser lest ({kilder_txt}), {len(priser)} til salgs med pris, "
              f"{nye} nye{f' ({igjen} lagt ut på nytt)' if igjen else ''}, {prisendret} prisendringer, "
              f"{solgte} solgt, {borte} borte ({varighet}s)")
    if advarsler:
        status += " | ADVARSEL: " + "; ".join(advarsler)
    con.execute("UPDATE vakter SET sist_hentet=?, sist_status=? WHERE slug=?",
                (db.naa(), status, slug))
    con.commit()
    db.slett_gammelt(con)
    con.commit()
    logg(status)
    return {"slug": slug, "annonser": len(alle), "treff": treff_totalt, "aktive": len(priser),
            "nye": nye, "igjen": igjen, "prisendringer": prisendret, "solgt": solgte, "borte": borte,
            "varighet": varighet, "advarsler": advarsler, "status": status}


def _samme_bil(con, slug: str, kilde: str, kilde_id: str, vin: str | None, regnr: str | None):
    """Forrige annonse for samme bil (understellsnummer, ellers reg.nr) i dette markedet.
    Det er slik en bil som legges ut på nytt med ny annonse-id gjenkjennes."""
    if not vin and not regnr:
        return None
    if vin:
        r = con.execute("""SELECT kilde_id, pris, pris_type, sist_sett, solgt FROM annonser
                           WHERE slug=? AND kilde=? AND kilde_id!=? AND vin=? ORDER BY sist_sett DESC LIMIT 1""",
                        (slug, kilde, kilde_id, vin)).fetchone()
        if r:
            return r
    if regnr:
        return con.execute("""SELECT kilde_id, pris, pris_type, sist_sett, solgt FROM annonser
                              WHERE slug=? AND kilde=? AND kilde_id!=? AND regnr=? ORDER BY sist_sett DESC LIMIT 1""",
                           (slug, kilde, kilde_id, regnr)).fetchone()
    return None


def _egen_bil(v: dict) -> dict | None:
    """Reg.nr og understellsnummer for eierens egen bil, hvis vakten har det."""
    kj = v.get("kjoretoy") or {}
    regnr = (v.get("regnr") or kj.get("regnr") or "").replace(" ", "").upper() or None
    vin = (kj.get("understellsnummer") or "").strip().upper() or None
    return {"regnr": regnr, "vin": vin} if (regnr or vin) else None


def _er_egen(egen: dict, regnr: str | None, vin: str | None) -> bool:
    return bool((vin and egen.get("vin") and vin == egen["vin"]) or (regnr and egen.get("regnr") and regnr == egen["regnr"]))


def _behold_ekstra(rad, a: dict) -> dict:
    """Ny ekstra-dict fra kilden, men «forrige_kilde_id» (satt ved første observasjon) skal overleve."""
    ny = dict(a.get("ekstra") or {})
    try:
        gammel = json.loads(rad["ekstra"] or "{}")
    except (TypeError, ValueError):
        gammel = {}
    if gammel.get("forrige_kilde_id") and "forrige_kilde_id" not in ny:
        ny["forrige_kilde_id"] = gammel["forrige_kilde_id"]
    return ny


def _tittel(a: dict) -> str:
    return f"{a.get('tittel') or ''} {a.get('undertittel') or ''}".strip()


def _pris(pris, pris_type) -> str:
    if not pris:
        return "ukjent pris"
    enhet = {"bud": "kr i bud", "fastpris": "kr fastpris"}.get(pris_type or "", "kr")
    return f"{pris} {enhet}"


def kilder_finn_kategoriser(a: dict, v: dict, sok_tagger: list[str]) -> str:
    """Kategorisering er kildespesifikk (fritekst-mønstre), men felles for vakten."""
    from .kilder import finn as finn_kilde
    a["sok_tagger_liste"] = sok_tagger
    return finn_kilde.kategoriser(a, v.get("kategorier") or {}, sok_tagger)


def berik_detaljer(con, v: dict, maks_alder_dager: int = 7, grense: int = 40,
                   logg=lambda m: None) -> int:
    """Henter detaljsiden for aktive annonser uten fersk detaljdata (utstyr, eiere, garantier).

    Bare FINN-annonser støttes foreløpig — andre kilder hoppes over.
    """
    from .kilder import finn as finn_kilde
    rader = db.ok(con, """SELECT a.kilde, a.kilde_id, a.url FROM annonser a
                          WHERE a.slug=? AND a.kilde='finn' AND a.aktiv=1 AND NOT a.solgt
                            AND NOT EXISTS (SELECT 1 FROM detaljer d
                                            WHERE d.slug=a.slug AND d.kilde=a.kilde
                                              AND d.kilde_id=a.kilde_id
                                              AND d.hentet >= datetime('now','localtime', ?))
                          ORDER BY a.forst_sett DESC LIMIT ?""",
                  (v["slug"], f"-{maks_alder_dager} days", grense))
    n = 0
    for r in rader:
        try:
            d = finn_kilde.hent_detaljer(r["url"])
        except RuntimeError as e:
            logg(f"detalj feilet for {r['kilde_id']}: {e}")
            continue
        con.execute("""INSERT INTO detaljer (slug, kilde, kilde_id, hentet, felter, utstyr, beskrivelse)
                       VALUES (?,?,?,datetime('now','localtime'),?,?,?)
                       ON CONFLICT(slug, kilde, kilde_id) DO UPDATE SET
                         hentet=excluded.hentet, felter=excluded.felter,
                         utstyr=excluded.utstyr, beskrivelse=excluded.beskrivelse""",
                    (v["slug"], r["kilde"], r["kilde_id"], json.dumps(d["felter"], ensure_ascii=False),
                     d["utstyr"], d["beskrivelse"]))
        n += 1
        time.sleep(finn_kilde.SLEEP)
    con.commit()
    if n:
        logg(f"detaljer hentet: {n}")
    return n
