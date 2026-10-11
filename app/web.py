"""Web-front (FastAPI) for finn-marked.

Sider:  /                          – egne vakter (admin: alle) med nøkkeltall
        /vakt/<slug>               – dashbord for ett marked
        /vakt/ny, /vakt/<slug>/rediger – skjema for søk (flere vakter samtidig)
        /logginn, /oppsett, /invitasjon/<token>, /konto, /admin/brukere – brukere og passkeys
API:    /api/vakter, /api/vakt/<slug>, /api/vakt/<slug>/annonser.json (krever sesjon)
        /api/auth/*                – passkey-seremoniene (se app/auth.py og static/passkey.js)
        /helse                     – for Docker healthcheck (og scheduler-tilstand), åpen

Alt unntatt /helse, /static og innloggingssidene krever innlogget bruker (middleware
under). En vakt har én eier; admin ser og redigerer alle.
Søke-oppsettet fra skjemaet parses i `app/sok.py`, ikke her.
"""

from __future__ import annotations

import contextlib
import sqlite3
import threading
import urllib.parse
from collections.abc import Iterator
from datetime import date
from pathlib import Path

from fastapi import Body, Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import analyse, auth, db, innsamling, kjoretoy
from . import sok as sokmod
from .sok import parse_kategorier, parse_sok

BASE = Path(__file__).parent


@contextlib.asynccontextmanager
async def levetid(_app: FastAPI):
    """Oppstart: sikre skjemaet og start scheduleren.

    `lifespan` i stedet for `@app.on_event("startup")`, som FastAPI har avviklet.
    """
    db.init()
    from . import scheduler
    scheduler.start()
    yield


def kon() -> Iterator[sqlite3.Connection]:
    """FastAPI-dependency: én tilkobling per forespørsel, alltid lukket etterpå."""
    with db.aapen() as con:
        yield con


def _malkontekst(request: Request) -> dict:
    """Innlogget bruker er tilgjengelig som `bruker` i alle maler (nav-linja)."""
    return {"bruker": getattr(request.state, "bruker", None)}


app = FastAPI(title="FINN-marked", lifespan=levetid)
templates = Jinja2Templates(directory=str(BASE / "templates"), context_processors=[_malkontekst])
# Malene skrev «"{:,.0f}".format(x).replace(",", " ")» overalt. analyse.kr gjør
# nøyaktig det samme (og gir «–» for tomt), så den registreres som filter.
templates.env.filters["kr"] = analyse.kr
templates.env.filters["km"] = analyse.km_str
templates.env.filters["dager"] = analyse.dager_mellom
app.mount("/static", StaticFiles(directory=str(BASE / "static")), name="static")


@app.middleware("http")
async def krev_innlogging(request: Request, call_next):
    """Slipper gjennom de åpne stiene; ellers må sesjonscookien peke på en gyldig sesjon.

    Uinnloggede sendes til /logginn (HTML) eller får 401 (JSON under /api/).
    """
    sti = request.url.path
    request.state.bruker = None
    if not (sti == "/helse" or any(sti.startswith(p) for p in auth.AAPNE_STIER if p != "/helse")):
        with db.aapen() as con:
            request.state.bruker = auth.bruker_av_sesjon(con, request.cookies.get(auth.SESJON_COOKIE))
        if request.state.bruker is None:
            if sti.startswith("/api/"):
                return JSONResponse({"detail": "ikke innlogget"}, status_code=401)
            videre = sti + (f"?{request.url.query}" if request.url.query else "")
            return RedirectResponse(f"/logginn?videre={urllib.parse.quote(videre, safe='')}", status_code=303)
    elif sti.startswith(("/logginn", "/oppsett", "/konto", "/api/auth/")):
        # Nav-linja og «konto»-modus trenger brukeren også på de åpne sidene.
        with db.aapen() as con:
            request.state.bruker = auth.bruker_av_sesjon(con, request.cookies.get(auth.SESJON_COOKIE))
    return await call_next(request)


def bruker(request: Request) -> dict:
    """Dependency: innlogget bruker (middlewaren har alt avvist andre)."""
    b = getattr(request.state, "bruker", None)
    if not b:
        raise HTTPException(401, "ikke innlogget")
    return b


def admin(b: dict = Depends(bruker)) -> dict:
    if not auth.er_admin(b):
        raise HTTPException(404)
    return b


def _min_vakt(con, slug: str, b: dict) -> dict:
    """Vakten hvis den finnes og brukeren eier den (eller er admin); ellers 404,
    så en annen brukers kortnavn ikke kan gjettes."""
    v = db.vakt(con, slug)
    if not v or not (auth.er_admin(b) or v.get("eier") == b["id"]):
        raise HTTPException(404, f"ukjent vakt: {slug}")
    return v

# Hentinger som kjører nå (slug -> siste statuslinje), så UI-et kan vise fremdrift.
# Hvem som faktisk henter holdes i innsamling.hentelaas(), felles med scheduleren.
_kjorer: dict[str, str] = {}
_kjorer_laas = threading.Lock()


def _logg_til(slug: str):
    def logg(melding: str) -> None:
        with _kjorer_laas:
            _kjorer[slug] = melding
    return logg


def _hent_jobb(slug: str, detaljer: bool = False) -> None:
    """Selve hentingen. Kalles i én tråd fra ruta, så UI-et svarer umiddelbart."""
    with innsamling.hentelaas(slug) as fikk:
        if not fikk:
            _logg_til(slug)("En henting kjører alt for denne vakten")
            return
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


def _vakt_kontekst(con, slug: str, b: dict) -> dict:
    v = _min_vakt(con, slug, b)
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
        "liggetid": analyse.liggetid(solgte),
        "egen_ute": _egen_ute(con, v, annonser + solgte),
        "igjen": {r["kilde_id"] for r in con.execute(
            "SELECT kilde_id FROM annonser WHERE slug=? AND ekstra LIKE '%forrige_kilde_id%'", (slug,))},
    }


def _egen_ute(con, v: dict, annonser: list[dict]) -> list[dict]:
    """Annonser i markedet som er eierens egen bil (samme reg.nr eller understellsnummer)."""
    kj = v.get("kjoretoy") or {}
    regnr = (v.get("regnr") or kj.get("regnr") or "").replace(" ", "").upper()
    vin = (kj.get("understellsnummer") or "").strip().upper()
    if not regnr and not vin:
        return []
    return [a for a in annonser if (vin and a.get("vin") == vin) or (regnr and a.get("regnr") == regnr)]


@app.get("/helse")
def helse(con: sqlite3.Connection = Depends(kon)):
    """Helsesjekk for Docker — tar også med schedulerens tilstand, så en scheduler
    som har sluttet å kjøre (eller feiler hver runde) er synlig utenfra."""
    from . import scheduler

    antall = con.execute("SELECT COUNT(*) c FROM vakter").fetchone()["c"]
    return {"status": "ok", "vakter": antall, "scheduler": scheduler.status()}


@app.get("/", response_class=HTMLResponse)
def forside(request: Request, con: sqlite3.Connection = Depends(kon), b: dict = Depends(bruker)):
    oversikt = []
    eiere = {u["id"]: u["navn"] for u in auth.brukere(con)} if auth.er_admin(b) else {}
    for v in db.vakter(con, eier=None if auth.er_admin(b) else b["id"]):
        annonser = db.ok(con, "SELECT * FROM annonser WHERE slug=? AND aktiv=1 AND solgt=0", (v["slug"],))
        solgte = con.execute("SELECT COUNT(*) c FROM annonser WHERE slug=? AND solgt=1",
                             (v["slug"],)).fetchone()["c"]
        nye = con.execute("SELECT COUNT(*) c FROM hendelser WHERE slug=? AND type='ny' "
                          "AND tid >= datetime('now','localtime','-7 days')",
                          (v["slug"],)).fetchone()["c"]
        oversikt.append({"v": v, "oppsummering": analyse.oppsummer(annonser),
                         "solgte": solgte, "nye_uke": nye, "status": _kjorer.get(v["slug"]),
                         "eier": eiere.get(v.get("eier"))})
    return templates.TemplateResponse(request, "forside.html", {"oversikt": oversikt})


def _brukervalg(con, b: dict) -> list[dict]:
    """Eier-valget i skjemaene — bare admin får velge."""
    return [{"id": u["id"], "navn": u["navn"]} for u in auth.brukere(con) if u["aktiv"]] if auth.er_admin(b) else []


@app.get("/vakt/ny", response_class=HTMLResponse)
def ny_vakt(request: Request, url: str | None = None, navn: str = "", slug: str = "", eier: str = "",
            regnr: str = "", con: sqlite3.Connection = Depends(kon), b: dict = Depends(bruker)):
    """Steg 1 (uten url): lim inn én søke-URL, få forhåndsvisning og navneforslag.
    Steg 2 (med url): fullt skjema, forhåndsutfylt med søket og forslagene fra steg 1."""
    valgt_eier = int(eier) if (auth.er_admin(b) and eier.isdigit()) else b["id"]
    if not url:
        return templates.TemplateResponse(request, "ny.html",
                                          {"brukere": _brukervalg(con, b), "valgt_eier": valgt_eier,
                                           "regnr": regnr.strip().upper()})
    url = sokmod.rens_url(url)
    kilde = sokmod.kilde_fra_url(url)
    k = _skjema_kontekst(None)
    k["skjema"].update({"navn": navn, "slug": slug or sokmod.slugify(navn), "eier": valgt_eier,
                        "sok": [{"tag": "alle" if kilde == "finn" else kilde, "url": url, "kilde": kilde, "auto": True}]})
    if regnr.strip():
        # Fra «Ny vakt fra registreringsnummer»: bilen er alt slått opp i steg 1, vis den i skjemaet.
        k["skjema"]["regnr"] = regnr.strip().upper()
        try:
            kj = kjoretoy.slaa_opp(regnr)
            k["skjema"]["kjoretoy"] = kj
            k["skjema"]["egen_bil"] = kj["beskrivelse"]
        except kjoretoy.Oppslagsfeil:
            pass
    k["brukere"] = _brukervalg(con, b)
    return templates.TemplateResponse(request, "rediger.html", k)


@app.get("/oppslag", response_class=HTMLResponse)
def oppslag(request: Request, regnr: str = "", b: dict = Depends(bruker)):
    """Raskt oppslag på et registreringsnummer, uten å lage noe. Knappen videre
    tar nummeret med til «Ny vakt»."""
    kj, feil = None, None
    if regnr.strip():
        try:
            kj = kjoretoy.slaa_opp(regnr)
        except kjoretoy.Oppslagsfeil as e:
            feil = str(e)
    return templates.TemplateResponse(request, "oppslag.html",
                                      {"regnr": regnr.strip().upper(), "kj": kj, "feil": feil})


@app.get("/api/kjoretoy")
def api_kjoretoy(regnr: str, b: dict = Depends(bruker)):
    """Egen bil fra registreringsnummer (Statens vegvesen). Brukes av skjemaet."""
    try:
        return {"kjoretoy": kjoretoy.slaa_opp(regnr)}
    except kjoretoy.Oppslagsfeil as e:
        return {"feil": str(e)}


@app.get("/api/sokforslag")
def api_sokforslag(regnr: str, b: dict = Depends(bruker)):
    """«Ny vakt» fra registreringsnummer: Vegvesenet gir merke/modell/år, FINNs
    merketre gir koden, og ut kommer et forslag til søke-URL pluss modellisten
    for merket (til å velge selv når treffet er usikkert)."""
    from .kilder import finn as finn_kilde
    try:
        kj = kjoretoy.slaa_opp(regnr)
    except kjoretoy.Oppslagsfeil as e:
        return {"feil": str(e)}
    try:
        treff = finn_kilde.finn_merke_og_modell(kj.get("merke"), kj.get("modell"))
    except RuntimeError as e:
        return {"feil": f"Fikk ikke merke-/modellisten fra FINN: {e}", "kjoretoy": kj}
    if not treff["merke"]:
        return {"feil": f"Fant ikke merket «{kj.get('merke')}» hos FINN.", "kjoretoy": kj}
    valgt = treff["modell"] or treff["merke"]
    navn = " ".join(x for x in (treff["merke"]["navn"], treff["modell"]["navn"] if treff["modell"] else None,
                                str(kj["aar"]) if kj.get("aar") else None) if x)
    return {"kjoretoy": kj, "merke": treff["merke"], "modell": treff["modell"], "modeller": treff["modeller"],
            "aar": kj.get("aar"), "elektrisk": kj.get("elektrisk"),
            "url": finn_kilde.sok_url(valgt["kode"], kj.get("aar"), bool(kj.get("elektrisk"))),
            "navn": navn, "slug": sokmod.slugify(navn),
            "advarsel": None if treff["modell"] else
            f"Fant ikke modellen «{kj.get('modell')}» i FINNs liste for {treff['merke']['navn']} — velg under."}


@app.get("/api/forhandsvis")
def api_forhandsvis(url: str, b: dict = Depends(bruker)):
    """Første side av et søk — til «Ny vakt». Henter fra kilden med én gang (1–2 s)."""
    url = sokmod.rens_url(url)
    advarsel = sokmod.sjekk_url(url)
    if advarsel:
        return {"advarsel": advarsel}
    kilde = sokmod.kilde_fra_url(url)
    try:
        if kilde == "auksjonen":
            from .kilder import auksjonen as kildemod
        else:
            from .kilder import finn as kildemod
        f = kildemod.forhandsvis(url)
    except RuntimeError as e:
        raise HTTPException(502, str(e)) from e
    if kilde == "finn" and f["treff"] and not f["kort_pa_siden"]:
        return {"advarsel": f"Siden oppgir {f['treff']} treff, men ingen annonser ble gjenkjent — "
                            "FINN kan ha endret siden. Si fra."}
    navn, slug = sokmod.navneforslag(url, f["titler"])
    return {"advarsel": None, "kilde": kilde, "treff": f["treff"], "sider": f["sider"],
            "eksempler": f["eksempler"], "navn": navn, "slug": slug}


# ⚠️ Rekkefølge: /vakt/ny MÅ stå før /vakt/{slug}, ellers fanger {slug} ordet «ny».
@app.get("/vakt/{slug}", response_class=HTMLResponse)
def vakt_side(request: Request, slug: str, kategori: str | None = None, q: str | None = None,
              con: sqlite3.Connection = Depends(kon), b: dict = Depends(bruker)):
    k = _vakt_kontekst(con, slug, b)
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
        "historikk_svg": analyse.svg_historikk(k["kjoringer"]),
        "kategori": valgt,
        "kategorier": alle_kategorier,
        "q": q or "",
        "kjorer": _kjorer.get(slug),
        "kjorer_aktiv": innsamling.er_opptatt(slug),
        "brukervalg": [u for u in _brukervalg(con, b) if u["id"] != k["v"].get("eier")],
        "i_dag": date.today().isoformat(),
    })


@app.post("/vakt/{slug}/hent")
def hent(slug: str, detaljer: str | None = Form(None), con: sqlite3.Connection = Depends(kon),
         b: dict = Depends(bruker)):
    _min_vakt(con, slug, b)
    if innsamling.er_opptatt(slug):
        return RedirectResponse(f"/vakt/{slug}", status_code=303)
    _logg_til(slug)("Henter …")
    threading.Thread(target=_hent_jobb, args=(slug, bool(detaljer)), daemon=True,
                     name=f"hent-{slug}").start()
    return RedirectResponse(f"/vakt/{slug}", status_code=303)


def _skjema_kontekst(v: dict | None) -> dict:
    """Feltene rediger.html viser. `skjema` er flat, så ny/rediger/kopi deler mal."""
    if not v:
        return {"v": None, "kopi": None, "forslag": [], "brukere": [],
                "skjema": {"navn": "", "slug": "", "eier": None, "sok": [], "kategorier": {},
                           "egen_bil": "", "egen_km": "", "regnr": "", "kjoretoy": None,
                           "hentetime": 7, "aktiv": True}}
    sok = [{"tag": s.get("tag") or "", "url": s["url"], "kilde": s.get("kilde") or "finn",
            "auto": (s.get("kilde") or "finn") == sokmod.kilde_fra_url(s["url"])} for s in v["sok"]]
    return {"v": v, "kopi": None, "forslag": [], "brukere": [],
            "skjema": {"navn": v["navn"], "slug": v["slug"], "eier": v.get("eier"), "sok": sok,
                       "kategorier": v.get("kategorier") or {}, "egen_bil": v.get("egen_bil") or "",
                       "egen_km": v.get("egen_km") or "", "regnr": v.get("regnr") or "",
                       "kjoretoy": v.get("kjoretoy"), "hentetime": v.get("hentetime") or 7,
                       "aktiv": bool(v.get("aktiv"))}}


def _forslag(con, v: dict) -> list[tuple[str, int]]:
    annonser = db.ok(con, "SELECT undertittel FROM annonser WHERE slug=? AND aktiv=1", (v["slug"],))
    return analyse.kategoriforslag(annonser, v.get("kategorier"))


@app.get("/vakt/{slug}/rediger", response_class=HTMLResponse)
def rediger(request: Request, slug: str, con: sqlite3.Connection = Depends(kon), b: dict = Depends(bruker)):
    v = _min_vakt(con, slug, b)
    k = _skjema_kontekst(v)
    k["forslag"] = _forslag(con, v)
    k["brukere"] = _brukervalg(con, b)
    return templates.TemplateResponse(request, "rediger.html", k)


@app.get("/vakt/{slug}/kopier", response_class=HTMLResponse)
def kopier(request: Request, slug: str, til: str = "", con: sqlite3.Connection = Depends(kon),
           a: dict = Depends(admin)):
    """Ny vakt for en annen bruker, basert på denne: søk, kategorier og hentetid blir med,
    egen bil/km og kortnavn er nye. Admin fyller inn bilen før brukeren logger inn."""
    v = _min_vakt(con, slug, a)
    mottaker = auth.bruker(con, int(til)) if til.isdigit() else None
    k = _skjema_kontekst(v)
    k["v"], k["kopi"] = None, v
    k["forslag"] = _forslag(con, v)
    k["brukere"] = _brukervalg(con, a)
    k["skjema"].update({
        "navn": f"{v['navn']} ({mottaker['navn']})" if mottaker else f"{v['navn']} (kopi)",
        "slug": f"{v['slug']}-{sokmod.slugify(mottaker['navn'])}" if mottaker else f"{v['slug']}-kopi",
        "eier": mottaker["id"] if mottaker else a["id"], "egen_bil": "", "egen_km": "",
        "regnr": "", "kjoretoy": None,
    })
    return templates.TemplateResponse(request, "rediger.html", k)




@app.post("/vakt/lagre")
def lagre(slug: str = Form(...), navn: str = Form(...), sok: str = Form(""),
          kategorier: str = Form(""), egen_bil: str = Form(""), egen_km: str = Form(""),
          hentetime: int = Form(7), aktiv: str | None = Form(None), eier: str = Form(""),
          sok_tag: list[str] = Form([]), sok_url: list[str] = Form([]), sok_kilde: list[str] = Form([]),
          kat_navn: list[str] = Form([]), kat_nokkel: list[str] = Form([]), regnr: str = Form(""),
          con: sqlite3.Connection = Depends(kon), b: dict = Depends(bruker)):
    """Skjemaet sender søk og kategorier som rader (sok_*/kat_*). De gamle tekstfeltene
    `sok`/`kategorier` godtas fortsatt, så eldre klienter og tester virker."""
    ren = sokmod.slugify(slug)
    if not ren:
        raise HTTPException(400, "slug mangler")
    nye_sok = sokmod.bygg_sok(sok_tag, sok_url, sok_kilde) if sok_url else parse_sok(sok)
    nye_kat = sokmod.bygg_kategorier(kat_nokkel, kat_navn) if (kat_navn or kat_nokkel) else parse_kategorier(kategorier)
    eksisterende = db.vakt(con, ren)
    if eksisterende:
        _min_vakt(con, ren, b)                     # andres kortnavn kan ikke overskrives
    # Eier: admin kan velge hvem som helst (skjemafeltet), andre eier alltid sine egne.
    ny_eier = int(eier) if (auth.er_admin(b) and eier.strip().isdigit()) else b["id"]
    if eksisterende and not eier.strip():
        ny_eier = eksisterende.get("eier") or ny_eier
    # Egen bil fra regnr: slås opp på nytt bare når nummeret er nytt eller endret.
    regnr = regnr.strip().upper().replace(" ", "")
    kj = eksisterende.get("kjoretoy") if eksisterende else None
    if regnr and not (kj and kj.get("regnr") == regnr):
        try:
            kj = kjoretoy.slaa_opp(regnr)
        except kjoretoy.Oppslagsfeil:
            kj = None                      # feilen vises ved «Slå opp» i skjemaet; lagringen stoppes ikke
    elif not regnr:
        kj = None
    if kj and not egen_bil.strip():
        egen_bil = kj["beskrivelse"]
    db.lagre_vakt(con, {
        "slug": ren, "navn": navn.strip() or ren, "sok": nye_sok, "kategorier": nye_kat,
        "regnr": regnr or None, "kjoretoy": kj,
        "egen_bil": egen_bil or None, "egen_km": int(egen_km) if egen_km.strip().isdigit() else None,
        "hentetime": hentetime, "aktiv": 1 if aktiv else 0,
        "opprettet": eksisterende.get("opprettet") if eksisterende else None,
        "sist_hentet": eksisterende.get("sist_hentet") if eksisterende else None,
        "sist_status": eksisterende.get("sist_status") if eksisterende else None,
        "eier": ny_eier,
    })
    con.commit()
    if not eksisterende and nye_sok and not innsamling.er_opptatt(ren):
        # Ny vakt: hent med en gang, så siden har tall når den åpnes.
        _logg_til(ren)("Henter …")
        threading.Thread(target=_hent_jobb, args=(ren, False), daemon=True, name=f"hent-{ren}").start()
    return RedirectResponse(f"/vakt/{ren}", status_code=303)


@app.post("/vakt/{slug}/slett")
def slett(slug: str, data: str | None = Form(None), con: sqlite3.Connection = Depends(kon),
          b: dict = Depends(bruker)):
    _min_vakt(con, slug, b)
    if data:  # «slett også data» – ellers beholdes historikken for arkivet
        for t in ("annonser", "pris_historikk", "kjøringer", "hendelser", "detaljer"):
            con.execute(f"DELETE FROM {t} WHERE slug=?", (slug,))
    con.execute("DELETE FROM vakter WHERE slug=?", (slug,))
    con.commit()
    return RedirectResponse("/", status_code=303)


# ------------------------------------------------------------------ JSON-API

@app.get("/api/vakter")
def api_vakter(con: sqlite3.Connection = Depends(kon), b: dict = Depends(bruker)):
    ut = []
    for v in db.vakter(con, eier=None if auth.er_admin(b) else b["id"]):
        annonser = db.ok(con, "SELECT * FROM annonser WHERE slug=? AND aktiv=1 AND solgt=0", (v["slug"],))
        ut.append({"vakt": v, "oppsummering": analyse.oppsummer(annonser),
                   "solgte": con.execute("SELECT COUNT(*) c FROM annonser WHERE slug=? AND solgt=1",
                                         (v["slug"],)).fetchone()["c"]})
    return {"vakter": ut}


@app.get("/api/vakt/{slug}")
def api_vakt(slug: str, con: sqlite3.Connection = Depends(kon), b: dict = Depends(bruker)):
    k = _vakt_kontekst(con, slug, b)
    return JSONResponse({
        "vakt": k["v"], "oppsummering": k["oppsummering"],
        "verdikurve": {x: k["kurve"][x] for x in ("n", "a", "b", "r2", "sd", "estimat", "egen_km")}
        if k["kurve"] else None,
        "km_botter": k["botter"], "nabolag": k["nabolag"],
        "annonser": k["annonser"], "solgte": k["solgte"],
    })


@app.get("/api/vakt/{slug}/annonser.json")
def api_annonser(slug: str, kategori: str | None = None, con: sqlite3.Connection = Depends(kon),
                 b: dict = Depends(bruker)):
    _min_vakt(con, slug, b)
    annonser = db.ok(con, "SELECT * FROM annonser WHERE slug=? AND aktiv=1 AND solgt=0 ORDER BY pris", (slug,))
    if kategori:
        annonser = [a for a in annonser if kategori.lower() in (a["tagger"] or "").lower()]
    return {"slug": slug, "antall": len(annonser), "annonser": annonser}


# ------------------------------------------------------------------ brukere og passkeys

@app.get("/logginn", response_class=HTMLResponse)
def logginn(request: Request, videre: str = "/", con: sqlite3.Connection = Depends(kon)):
    if request.state.bruker:
        return RedirectResponse("/", status_code=303)
    if auth.antall_brukere(con) == 0:
        return RedirectResponse("/oppsett", status_code=303)
    if not videre.startswith("/") or videre.startswith("//"):
        videre = "/"
    return templates.TemplateResponse(request, "logginn.html", {"videre": videre})


@app.post("/loggut")
def loggut(request: Request, con: sqlite3.Connection = Depends(kon)):
    auth.slett_sesjon(con, request.cookies.get(auth.SESJON_COOKIE))
    con.commit()
    svar = RedirectResponse("/logginn", status_code=303)
    svar.delete_cookie(auth.SESJON_COOKIE, path="/")
    return svar


@app.get("/oppsett", response_class=HTMLResponse)
def oppsett(request: Request, nokkel: str | None = None, con: sqlite3.Connection = Depends(kon)):
    """Første admin når basen er tom. Senere bare med FINN_OPPSETT_NOKKEL (gjenoppretting)."""
    tom = auth.antall_brukere(con) == 0
    if not tom and not auth._oppsett_nokkel_ok(nokkel):
        raise HTTPException(404)
    adminer = [u for u in auth.brukere(con) if u["rolle"] == "admin"] if not tom else []
    return templates.TemplateResponse(request, "oppsett.html",
                                      {"tom": tom, "adminer": adminer, "nokkel": nokkel or ""})


@app.get("/invitasjon/{token}", response_class=HTMLResponse)
def invitasjon(request: Request, token: str, con: sqlite3.Connection = Depends(kon)):
    return templates.TemplateResponse(request, "invitasjon.html", {"inv": auth.invitasjon(con, token)})


@app.post("/api/auth/registrer/start")
def auth_registrer_start(request: Request, data: dict = Body(...), con: sqlite3.Connection = Depends(kon)):
    b = request.state.bruker
    bruker_id = b["id"] if (b and data.get("modus") == "konto") else None
    if data.get("modus") == "oppsett" and str(data.get("bruker_id") or "").isdigit():
        bruker_id = int(data["bruker_id"])
    options, uid = auth.registrering_start(
        request, con, data.get("modus") or "", bruker_id=bruker_id, navn=data.get("navn"),
        token=data.get("token"), nokkel=data.get("nokkel"))
    svar = JSONResponse({"options": options, "utfordring": uid})
    return svar


@app.post("/api/auth/registrer/fullfor")
def auth_registrer_fullfor(request: Request, data: dict = Body(...), con: sqlite3.Connection = Depends(kon)):
    bruker_id = auth.registrering_fullfor(request, con, data.get("utfordring"), data.get("credential") or {},
                                         data.get("passkey_navn"))
    if request.state.bruker and request.state.bruker["id"] == bruker_id:
        con.commit()
        return {"ok": True, "videre": "/konto?melding=Passkey+lagt+til"}
    sid = auth.ny_sesjon(con, bruker_id, request.headers.get("user-agent"))
    con.commit()
    svar = JSONResponse({"ok": True, "videre": "/"})
    auth.sett_sesjon_cookie(svar, request, sid)
    return svar


@app.post("/api/auth/logginn/start")
def auth_logginn_start(request: Request):
    options, uid = auth.logginn_start(request)
    return {"options": options, "utfordring": uid}


@app.post("/api/auth/logginn/fullfor")
def auth_logginn_fullfor(request: Request, data: dict = Body(...), con: sqlite3.Connection = Depends(kon)):
    b = auth.logginn_fullfor(request, con, data.get("utfordring"), data.get("credential") or {})
    sid = auth.ny_sesjon(con, b["id"], request.headers.get("user-agent"))
    con.commit()
    svar = JSONResponse({"ok": True, "videre": "/"})
    auth.sett_sesjon_cookie(svar, request, sid)
    return svar


@app.get("/konto", response_class=HTMLResponse)
def konto(request: Request, melding: str | None = None, con: sqlite3.Connection = Depends(kon),
          b: dict = Depends(bruker)):
    return templates.TemplateResponse(request, "konto.html",
                                      {"passkeys": auth.passkeys(con, b["id"]), "melding": melding})


@app.post("/konto/passkey/{pid}/navn")
def konto_passkey_navn(pid: str, navn: str = Form(""), con: sqlite3.Connection = Depends(kon),
                       b: dict = Depends(bruker)):
    con.execute("UPDATE passkeys SET navn=? WHERE id=? AND bruker_id=?", (navn.strip()[:60], pid, b["id"]))
    con.commit()
    return RedirectResponse("/konto", status_code=303)


@app.post("/konto/passkey/{pid}/slett")
def konto_passkey_slett(pid: str, con: sqlite3.Connection = Depends(kon), b: dict = Depends(bruker)):
    if len(auth.passkeys(con, b["id"])) <= 1:
        raise HTTPException(400, "kan ikke slette den eneste passkeyen")
    con.execute("DELETE FROM passkeys WHERE id=? AND bruker_id=?", (pid, b["id"]))
    con.commit()
    return RedirectResponse("/konto?melding=Passkey+slettet", status_code=303)


@app.get("/admin/brukere", response_class=HTMLResponse)
def admin_brukere(request: Request, lenke: str | None = None, til: str | None = None,
                  con: sqlite3.Connection = Depends(kon), _a: dict = Depends(admin)):
    return templates.TemplateResponse(request, "brukere.html",
                                      {"brukere": auth.brukere(con), "lenke": lenke, "lenke_til": til})


def _invitasjonslenke(request: Request, con, bruker_id: int, navn: str) -> RedirectResponse:
    token = auth.ny_invitasjon(con, bruker_id)
    con.commit()
    lenke = f"{auth.origin(request)}/invitasjon/{token}"
    return RedirectResponse(f"/admin/brukere?lenke={urllib.parse.quote(lenke, safe='')}"
                            f"&til={urllib.parse.quote(navn)}", status_code=303)


@app.post("/admin/brukere/ny")
def admin_ny_bruker(request: Request, navn: str = Form(...), epost: str = Form(""), rolle: str = Form("bruker"),
                    con: sqlite3.Connection = Depends(kon), _a: dict = Depends(admin)):
    bid = auth.ny_bruker(con, navn, epost, "admin" if rolle == "admin" else "bruker")
    return _invitasjonslenke(request, con, bid, navn.strip())


@app.post("/admin/brukere/{bid}/inviter")
def admin_inviter(request: Request, bid: int, con: sqlite3.Connection = Depends(kon), _a: dict = Depends(admin)):
    u = auth.bruker(con, bid)
    if not u:
        raise HTTPException(404)
    return _invitasjonslenke(request, con, bid, u["navn"])


@app.post("/admin/brukere/{bid}/aktiv")
def admin_aktiv(bid: int, aktiv: int = Form(...), con: sqlite3.Connection = Depends(kon), a: dict = Depends(admin)):
    if bid == a["id"]:
        raise HTTPException(400, "kan ikke deaktivere deg selv")
    con.execute("UPDATE brukere SET aktiv=? WHERE id=?", (1 if aktiv else 0, bid))
    if not aktiv:
        con.execute("DELETE FROM sesjoner WHERE bruker_id=?", (bid,))
    con.commit()
    return RedirectResponse("/admin/brukere", status_code=303)
