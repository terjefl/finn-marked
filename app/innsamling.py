"""Innsamling: henter alle søk for én vakt gjennom kildelaget, lagrer og logger hendelser.

Lageret er felles for alle vakter (se db.py); alt nøkles på (slug, kilde, kilde_id).
"""

from __future__ import annotations

import json
import statistics
import time
from datetime import date

from . import db, kilder


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

    nye = prisendret = solgte = borte = 0
    i_dag = date.today().isoformat()
    sett = set(alle)
    for (kilde, kilde_id), a in alle.items():
        a["tagger"] = kilder_finn_kategoriser(a, v, sorted(x for x in a.pop("sok_tagger") if x))
        rad = con.execute("SELECT * FROM annonser WHERE slug=? AND kilde=? AND kilde_id=?",
                          (slug, kilde, kilde_id)).fetchone()
        if rad is None:
            nye += 1
            con.execute(
                """INSERT INTO annonser (slug, kilde, kilde_id, tittel, undertittel, aar, km, drivstoff,
                       rekkevidde, pris, pris_type, sted, selger, selger_type, url, tagger, sok_tagger,
                       ekstra, forst_sett, sist_sett, borte_dato, aktiv, merke, solgt, solgt_dato)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,?,?,?,?)""",
                (slug, kilde, kilde_id, a["tittel"], a["undertittel"], a["aar"], a["km"], a["drivstoff"],
                 a["rekkevidde"], a["pris"], a["pris_type"], a["sted"], a["selger"], a["selger_type"],
                 a["url"], a["tagger"], json.dumps(a.get("sok_tagger_liste") or [], ensure_ascii=False),
                 json.dumps(a.get("ekstra") or {}, ensure_ascii=False), i_dag, i_dag,
                 1 if not a["solgt"] else 0, a["merke"], 1 if a["solgt"] else 0,
                 i_dag if a["solgt"] else None))
            if a["pris"]:
                con.execute("""INSERT INTO pris_historikk (slug, kilde, kilde_id, dato, pris, pris_type)
                               VALUES (?,?,?,?,?,?)""",
                            (slug, kilde, kilde_id, i_dag, a["pris"], a["pris_type"]))
            db.hendelse(con, slug, "solgt" if a["solgt"] else "ny", a["url"],
                        f"{'Solgt: ' if a['solgt'] else 'Ny annonse: '}{_tittel(a)}".strip(),
                        {"pris": a["pris"], "km": a["km"], "sted": a["sted"], "url": a["url"],
                         "kilde": kilde, "kilde_id": kilde_id, "pris_type": a["pris_type"]})
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
                       solgt_dato=COALESCE(?, solgt_dato)
                   WHERE slug=? AND kilde=? AND kilde_id=?""",
                (a["tittel"], a["undertittel"], a["aar"], a["km"], a["drivstoff"], a["rekkevidde"],
                 a["pris"], a["pris_type"], a["sted"], a["selger"], a["selger_type"], a["tagger"],
                 json.dumps(a.get("ekstra") or {}, ensure_ascii=False), i_dag,
                 0 if a["solgt"] else 1, a["merke"], 1 if a["solgt"] else rad["solgt"] or 0,
                 i_dag if (a["solgt"] and not rad["solgt"]) else None, slug, kilde, kilde_id))

    for rad in con.execute("SELECT * FROM annonser WHERE slug=? AND aktiv=1", (slug,)).fetchall():
        if (rad["kilde"], rad["kilde_id"]) not in sett:
            borte += 1
            con.execute("UPDATE annonser SET aktiv=0, borte_dato=? WHERE slug=? AND kilde=? AND kilde_id=?",
                        (i_dag, slug, rad["kilde"], rad["kilde_id"]))
            db.hendelse(con, slug, "borte", rad["url"],
                        f"Forsvant fra søket: {rad['tittel']} {rad['undertittel'] or ''} "
                        f"({rad['km']} km, {_pris(rad['pris'], rad['pris_type'])})",
                        {"pris": rad["pris"], "km": rad["km"], "url": rad["url"], "kilde": rad["kilde"]})

    priser = sorted(a["pris"] for a in alle.values() if a["pris"] and not a["solgt"])

    def pct(p: float):
        if not priser:
            return None
        return priser[min(len(priser) - 1, int(round(p / 100 * (len(priser) - 1))))]

    varighet = round(time.monotonic() - start, 1)
    treff_totalt = sum(treff.values()) if len(treff) > 1 else (max(treff.values()) if treff else 0)
    con.execute("""INSERT INTO kjøringer (slug, dato, annonser_hentet, treff, aktive, median, p25, p75,
                                          min, max, varighet)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (slug, db.naa(), len(alle), treff_totalt, len(priser),
                 statistics.median(priser) if priser else None, pct(25), pct(75),
                 priser[0] if priser else None, priser[-1] if priser else None, varighet))
    kilder_txt = ", ".join(f"{k}: {n}" for k, n in sorted(treff.items()))
    status = (f"OK: {len(alle)} annonser lest ({kilder_txt}), {len(priser)} til salgs med pris, "
              f"{nye} nye, {prisendret} prisendringer, {solgte} solgt, {borte} borte ({varighet}s)")
    if advarsler:
        status += " | ADVARSEL: " + "; ".join(advarsler)
    con.execute("UPDATE vakter SET sist_hentet=?, sist_status=? WHERE slug=?",
                (db.naa(), status, slug))
    con.commit()
    db.slett_gammelt(con)
    con.commit()
    logg(status)
    return {"slug": slug, "annonser": len(alle), "treff": treff_totalt, "aktive": len(priser),
            "nye": nye, "prisendringer": prisendret, "solgt": solgte, "borte": borte,
            "varighet": varighet, "advarsler": advarsler, "status": status}


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
