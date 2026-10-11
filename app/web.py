"""Web-front (FastAPI) for finn-marked.

Sider:  /                          – alle vakter med nøkkeltall
        /vakt/<slug>               – dashbord for ett marked
        /vakt/ny, /vakt/<slug>/rediger – skjema for søk (flere vakter samtidig)
API:    /api/vakter, /api/vakt/<slug>, /api/vakt/<slug>/endringer?since=ISO
        /api/vakt/<slug>/annonser.json – annonselisten som JSON
        /helse                     – for Docker healthcheck (og scheduler-tilstand)

Søke-oppsettet fra skjemaet parses i `app/sok.py`, ikke her.
"""

from __future__ import annotations

import json
import threading
from datetime import date
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import analyse, db, innsamling
from .sok import parse_kategorier, parse_sok

BASE = Path(__file__).parent
templates = Jinja2Templates(directory=str(BASE / "templates"))
app = FastAPI(title="FINN-marked")
app.mount("/static", StaticFiles(directory=str(BASE / "static")), name="static")

# Hentinger som kjører nå (slug -> siste statuslinje), så UI-et kan vise fremdrift
_kjorer: dict[str, str] = {}
_kjorer_aktive: set[str] = set()
_kjorer_laas = threading.Lock()


def _logg_til(slug: str):
    def logg(melding: str) -> None:
        with _kjorer_laas:
            _kjorer[slug] = melding
    return logg


def _hent_bakgrunn(slug: str, detaljer: bool = False) -> None:
    """Kjører hentingen i en tråd slik at UI-et svarer umiddelbart (ingen blokkering)."""

    def jobb():
        con = db.base()
        try:
            v = db.vakt(con, slug)
            if not v:
                _logg_til(slug)("FEIL: vakt ikke funnet")
                return
            res = innsamling.hent_vakt(con, v, _logg_til(slug))
            if detaljer:
                innsamling.berik_detaljer(con, v, logg=_logg_til(slug))
            _logg_til(slug)(res["status"])
        except Exception as e:  # noqa: BLE001 – feilen skal vises i UI-et, ikke drepe tråden
            _logg_til(slug)(f"FEIL: {e}")
            try:
                # Ikke committ halvferdige annonser/hendelser fra den avbrutte hentingen:
                # rull tilbake til forrige commit før statusen skrives.
                con.rollback()
                con.execute("UPDATE vakter SET sist_status=? WHERE slug=?", (f"FEIL: {e}", slug))
                con.commit()
            except Exception:  # noqa: BLE001
                pass
        finally:
            con.close()
            with _kjorer_laas:
                _kjorer_aktive.discard(slug)

    threading.Thread(target=jobb, daemon=True, name=f"hent-{slug}").start()


def _vakt_kontekst(con, slug: str) -> dict:
    v = db.vakt(con, slug)
    if not v:
        raise HTTPException(404, f"ukjent vakt: {slug}")
    annonser = db.ok(con, "SELECT * FROM annonser WHERE slug=? AND aktiv=1 AND solgt=0 ORDER BY km", (slug,))
    solgte = db.ok(con, "SELECT * FROM annonser WHERE slug=? AND solgt=1 ORDER BY solgt_dato DESC", (slug,))
    return {
        "v": v,
        "annonser": annonser,
        "solgte": solgte,
        "oppsummering": analyse.oppsummer(annonser),
        "kurve": analyse.verdikurve(annonser, v.get("egen_km")),
        "botter": analyse.km_botter(annonser),
        "nabolag": analyse.nabolag(annonser, v["egen_km"]) if v.get("egen_km") else None,
        "kjoringer": analyse.historikk(con, slug, dager=180),
        "hendelser": db.ok(con, "SELECT * FROM hendelser WHERE slug=? ORDER BY id DESC LIMIT 60", (slug,)),
    }


@app.on_event("startup")
def oppstart() -> None:
    db.init()
    from . import scheduler
    scheduler.start()


@app.get("/helse")
def helse():
    con = db.base()
    try:
        antall = con.execute("SELECT COUNT(*) c FROM vakter").fetchone()["c"]
    finally:
        con.close()
    return {"status": "ok", "vakter": antall}


@app.get("/", response_class=HTMLResponse)
def forside(request: Request):
    con = db.base()
    try:
        oversikt = []
        for v in db.vakter(con):
            annonser = db.ok(con, "SELECT * FROM annonser WHERE slug=? AND aktiv=1 AND solgt=0", (v["slug"],))
            solgte = con.execute("SELECT COUNT(*) c FROM annonser WHERE slug=? AND solgt=1",
                                 (v["slug"],)).fetchone()["c"]
            nye = con.execute("SELECT COUNT(*) c FROM hendelser WHERE slug=? AND type='ny' "
                              "AND tid >= datetime('now','localtime','-7 days')",
                              (v["slug"],)).fetchone()["c"]
            oversikt.append({"v": v, "oppsummering": analyse.oppsummer(annonser),
                             "solgte": solgte, "nye_uke": nye, "status": _kjorer.get(v["slug"])})
        return templates.TemplateResponse(request, "forside.html", {"oversikt": oversikt})
    finally:
        con.close()


@app.get("/vakt/ny", response_class=HTMLResponse)
def ny_vakt(request: Request):
    return templates.TemplateResponse(request, "rediger.html", _skjema_kontekst(None))


# ⚠️ Rekkefølge: /vakt/ny MÅ stå før /vakt/{slug}, ellers fanger {slug} ordet «ny».
@app.get("/vakt/{slug}", response_class=HTMLResponse)
def vakt_side(request: Request, slug: str, kategori: str | None = None, q: str | None = None):
    con = db.base()
    try:
        k = _vakt_kontekst(con, slug)
        annonser = k["annonser"]
        if kategori:
            annonser = [a for a in annonser if kategori.lower() in (a["tagger"] or "").lower()]
        if q:
            ql = q.lower()
            annonser = [a for a in annonser if ql in f"{a['tittel']} {a['undertittel'] or ''}".lower()]
        valgt = kategori or "alle"
        if valgt == "alle":
            alle_kategorier = sorted({x for a in k["annonser"] for x in (a["tagger"] or "").split(",") if x})
        else:
            alle_kategorier = sorted({x for a in annonser for x in (a["tagger"] or "").split(",") if x})
        return templates.TemplateResponse(request, "vakt.html", {
            **k,
            "annonser": annonser,
            "filtrert_oppsummering": analyse.oppsummer(annonser),
            "kurve": analyse.verdikurve(annonser, k["v"].get("egen_km")),
            "botter": analyse.km_botter(annonser),
            "svg": analyse.svg_kurve(annonser, k["v"].get("egen_km")),
            "kategori": valgt,
            "kategorier": alle_kategorier,
            "q": q or "",
            "kjorer": _kjorer.get(slug),
            "kjorer_aktiv": slug in _kjorer_aktive,
            "i_dag": date.today().isoformat(),
        })
    finally:
        con.close()


@app.post("/vakt/{slug}/hent")
def hent(slug: str, detaljer: str | None = Form(None)):
    con = db.base()
    try:
        if not db.vakt(con, slug):
            raise HTTPException(404, "ukjent vakt")
    finally:
        con.close()
    with _kjorer_laas:
        if slug in _kjorer_aktive:
            return RedirectResponse(f"/vakt/{slug}", status_code=303)
        _kjorer_aktive.add(slug)
        _kjorer[slug] = "Henter …"
    threading.Thread(target=_hent_bakgrunn, args=(slug, bool(detaljer)), daemon=True,
                     name=f"hent-{slug}").start()
    return RedirectResponse(f"/vakt/{slug}", status_code=303)


def _skjema_kontekst(v: dict | None) -> dict:
    if not v:  # ny vakt: tomme felt, men et forslag til kategorier
        return {"v": None, "sok_tekst": "",
                "kat_tekst": "fully = Fully Charged\nsuper = Supercharged\nm50 = M50"}
    sok_tekst = "\n".join(f"{s.get('tag', 'alle')} | {s['url']}" for s in v["sok"])
    kat_tekst = "\n".join(f"{k} = {navn}" for k, navn in (v.get("kategorier") or {}).items())
    return {"v": v, "sok_tekst": sok_tekst, "kat_tekst": kat_tekst}


@app.get("/vakt/{slug}/rediger", response_class=HTMLResponse)
def rediger(request: Request, slug: str):
    con = db.base()
    try:
        v = db.vakt(con, slug)
        if not v:
            raise HTTPException(404, "ukjent vakt")
        return templates.TemplateResponse(request, "rediger.html", _skjema_kontekst(v))
    finally:
        con.close()




@app.post("/vakt/lagre")
def lagre(slug: str = Form(...), navn: str = Form(...), sok: str = Form(...),
          kategorier: str = Form(""), egen_bil: str = Form(""), egen_km: str = Form(""),
          hentetime: int = Form(7), aktiv: str | None = Form(None)):
    ren = "".join(c if (c.isalnum() or c in "-_") else "-" for c in slug.lower()).strip("-")
    if not ren:
        raise HTTPException(400, "slug mangler")
    con = db.base()
    try:
        eksisterende = db.vakt(con, ren)
        db.lagre_vakt(con, {
            "slug": ren, "navn": navn, "sok": parse_sok(sok), "kategorier": parse_kategorier(kategorier),
            "egen_bil": egen_bil or None, "egen_km": int(egen_km) if egen_km.strip().isdigit() else None,
            "hentetime": hentetime, "aktiv": 1 if aktiv else 0,
            "opprettet": eksisterende.get("opprettet") if eksisterende else None,
            "sist_hentet": eksisterende.get("sist_hentet") if eksisterende else None,
            "sist_status": eksisterende.get("sist_status") if eksisterende else None,
        })
        con.commit()
    finally:
        con.close()
    return RedirectResponse(f"/vakt/{ren}", status_code=303)


@app.post("/vakt/{slug}/slett")
def slett(slug: str, data: str | None = Form(None)):
    con = db.base()
    try:
        if data:  # «slett også data» – ellers beholdes historikken for arkivet
            for t in ("annonser", "pris_historikk", "kjøringer", "hendelser", "detaljer"):
                con.execute(f"DELETE FROM {t} WHERE slug=?", (slug,))
        con.execute("DELETE FROM vakter WHERE slug=?", (slug,))
        con.commit()
    finally:
        con.close()
    return RedirectResponse("/", status_code=303)


# ------------------------------------------------------------------ JSON-API

@app.get("/api/vakter")
def api_vakter():
    con = db.base()
    try:
        ut = []
        for v in db.vakter(con):
            annonser = db.ok(con, "SELECT * FROM annonser WHERE slug=? AND aktiv=1 AND solgt=0", (v["slug"],))
            ut.append({"vakt": v, "oppsummering": analyse.oppsummer(annonser),
                       "solgte": con.execute("SELECT COUNT(*) c FROM annonser WHERE slug=? AND solgt=1",
                                             (v["slug"],)).fetchone()["c"]})
        return {"vakter": ut}
    finally:
        con.close()


@app.get("/api/vakt/{slug}")
def api_vakt(slug: str):
    con = db.base()
    try:
        k = _vakt_kontekst(con, slug)
        return JSONResponse({
            "vakt": k["v"], "oppsummering": k["oppsummering"],
            "verdikurve": {x: k["kurve"][x] for x in ("n", "a", "b", "r2", "sd", "estimat", "egen_km")}
            if k["kurve"] else None,
            "km_botter": k["botter"], "nabolag": k["nabolag"],
            "annonser": k["annonser"], "solgte": k["solgte"],
        })
    finally:
        con.close()


@app.get("/api/vakt/{slug}/endringer")
def api_endringer(slug: str, since: str | None = None, limit: int = 100):
    """Endringsstrøm for varsling (Hermes-vakten leser denne).

    `since` tåler både basens format (`2026-10-11 13:52:11`) og ISO med «T»
    (`2026-10-11T13:52:11`); det normaliseres før sammenligningen — den er tekstlig.
    """
    con = db.base()
    try:
        siden = db.normaliser_tid(since)
        if siden:
            rader = db.ok(con, "SELECT * FROM hendelser WHERE slug=? AND tid >= ? ORDER BY id LIMIT ?",
                          (slug, siden, limit))
        else:
            rader = db.ok(con, "SELECT * FROM hendelser WHERE slug=? ORDER BY id DESC LIMIT ?", (slug, limit))
        for r in rader:
            r["detaljer"] = json.loads(r["detaljer"] or "{}")
        return {"slug": slug, "since": siden, "antall": len(rader), "hendelser": rader}
    finally:
        con.close()


@app.get("/api/vakt/{slug}/annonser.json")
def api_annonser(slug: str, kategori: str | None = None):
    con = db.base()
    try:
        annonser = db.ok(con, "SELECT * FROM annonser WHERE slug=? AND aktiv=1 AND solgt=0 ORDER BY pris", (slug,))
        if kategori:
            annonser = [a for a in annonser if kategori.lower() in (a["tagger"] or "").lower()]
        return {"slug": slug, "antall": len(annonser), "annonser": annonser}
    finally:
        con.close()
