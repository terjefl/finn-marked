"""Varsling etter henting: e-post til alle, Pushover som tillegg for dem som har nøkkel.

Kanalene er per bruker (Konto): e-postadresse og valgfri Pushover-brukernøkkel.
Hva man varsles om er per bil man følger (skjemaet): «kandidat» (nye annonser som
oppfyller kjøpskriteriene), «pris» (prisfall på kandidater / i markedet), «egen»
(din bil ligger ute), «solgt», «ny» (alle nye annonser). Ett varsel per henting,
med alt som skjedde i den hentingen.

Oppsett (miljø): FINN_SMTP_HOST, FINN_SMTP_PORT (587 = STARTTLS, 465 = SSL),
FINN_SMTP_BRUKER, FINN_SMTP_PASSORD, FINN_SMTP_FRA, FINN_BASE_URL (til lenker),
og FINN_PUSHOVER_TOKEN (app-token; brukernøkkelen ligger på kontoen).
"""

from __future__ import annotations

import json
import logging
import os
import smtplib
import urllib.parse
import urllib.request
from email.message import EmailMessage

from . import analyse, db

logg = logging.getLogger("finn-marked.varsling")

TYPER = {
    "kandidat": "Nye annonser som oppfyller kjøpskriteriene",
    "pris": "Prisendringer",
    "egen": "Din bil dukker opp i markedet",
    "solgt": "Biler som blir solgt",
    "ny": "Alle nye annonser",
}
FORKLARING = {
    "kandidat": "En ny annonse (eller en som er lagt ut på nytt) oppfyller kjøpskriteriene dine.",
    "pris": "Prisen på en annonse er endret. Kjøper: bare kandidater. Eier: hele markedet.",
    "egen": "En annonse med ditt registreringsnummer eller understellsnummer dukker opp.",
    "solgt": "En annonse er markert solgt hos FINN.",
    "ny": "Alle nye annonser i søket, uansett kriterier.",
}
STANDARD = {"kjoper": ["kandidat", "pris"], "eier": ["egen", "solgt"]}


def smtp_oppsett() -> dict | None:
    vert = os.environ.get("FINN_SMTP_HOST")
    if not vert:
        return None
    return {"vert": vert, "port": int(os.environ.get("FINN_SMTP_PORT") or 587),
            "bruker": os.environ.get("FINN_SMTP_BRUKER") or "", "passord": os.environ.get("FINN_SMTP_PASSORD") or "",
            "fra": os.environ.get("FINN_SMTP_FRA") or os.environ.get("FINN_SMTP_BRUKER") or "finn-marked@localhost"}


def pushover_token() -> str | None:
    return (os.environ.get("FINN_PUSHOVER_TOKEN") or "").strip() or None


def base_url() -> str:
    return (os.environ.get("FINN_BASE_URL") or os.environ.get("FINN_ORIGIN") or "").rstrip("/")


# ------------------------------------------------------------------ kanaler

def send_epost(til: str, emne: str, tekst: str, html: str | None = None) -> None:
    o = smtp_oppsett()
    if not o:
        raise RuntimeError("e-post er ikke satt opp (FINN_SMTP_HOST mangler)")
    m = EmailMessage()
    m["From"], m["To"], m["Subject"] = o["fra"], til, emne
    m.set_content(tekst)
    if html:
        m.add_alternative(html, subtype="html")
    if o["port"] == 465:
        with smtplib.SMTP_SSL(o["vert"], o["port"], timeout=20) as s:
            if o["bruker"]:
                s.login(o["bruker"], o["passord"])
            s.send_message(m)
    else:
        with smtplib.SMTP(o["vert"], o["port"], timeout=20) as s:
            s.ehlo()
            s.starttls()
            if o["bruker"]:
                s.login(o["bruker"], o["passord"])
            s.send_message(m)


def send_pushover(brukernokkel: str, tittel: str, tekst: str, lenke: str | None = None) -> None:
    token = pushover_token()
    if not token:
        raise RuntimeError("Pushover er ikke satt opp (FINN_PUSHOVER_TOKEN mangler)")
    data = {"token": token, "user": brukernokkel, "title": tittel[:250], "message": tekst[:1024]}
    if lenke:
        data["url"] = lenke
    req = urllib.request.Request("https://api.pushover.net/1/messages.json",
                                 data=urllib.parse.urlencode(data).encode(), method="POST")
    with urllib.request.urlopen(req, timeout=20) as r:
        svar = json.loads(r.read().decode("utf-8", "replace"))
    if svar.get("status") != 1:
        raise RuntimeError(f"Pushover avviste meldingen: {svar.get('errors') or svar}")


# ------------------------------------------------------------------ utvalg og melding

def varsling_for(v: dict) -> dict:
    """{aktiv: bool, typer: [...]} for vakten, med standardvalg etter formål."""
    o = v.get("varsling") or {}
    if isinstance(o, str):
        try:
            o = json.loads(o or "{}")
        except ValueError:
            o = {}
    formaal = v.get("formaal") or "eier"
    if "typer" in o:                                   # lagret eksplisitt: det brukeren krysset av
        typer = list(o.get("typer") or [])
        return {"aktiv": bool(typer), "typer": typer}
    return {"aktiv": False, "typer": list(STANDARD.get(formaal, []))}   # aldri lagret: forslag, men av


def plukk_hendelser(con, v: dict, fra_id: int) -> list[dict]:
    """Hendelsene fra denne hentingen som brukeren vil høre om. «kandidat» er en
    ny/igjen-hendelse hvis annonse oppfyller kjøpskriteriene."""
    valg = varsling_for(v)
    if not valg["aktiv"] or not valg["typer"]:
        return []
    rader = db.ok(con, "SELECT * FROM hendelser WHERE slug=? AND id>? ORDER BY id", (v["slug"], fra_id))
    typer = set(valg["typer"])
    kandidat_ider: set[str] = set()
    if "kandidat" in typer:
        annonser = db.ok(con, "SELECT * FROM annonser WHERE slug=? AND aktiv=1 AND solgt=0", (v["slug"],))
        kurve = analyse.verdikurve(annonser)
        kandidat_ider = {a["kilde_id"] for a in analyse.kjoperliste(annonser, kurve, v.get("kriterier"))}
    ut = []
    for h in rader:
        try:
            d = json.loads(h.get("detaljer") or "{}")
        except ValueError:
            d = {}
        h["d"] = d
        t = h["type"]
        if t in ("ny", "igjen") and d.get("kilde_id") in kandidat_ider and "kandidat" in typer:
            h["merkelapp"] = "Kandidat"
            ut.append(h)
        elif t in ("ny", "igjen") and "ny" in typer:
            h["merkelapp"] = "Ny annonse"
            ut.append(h)
        elif t == "pris" and "pris" in typer:
            if "kandidat" in typer and kandidat_ider and d.get("kilde_id") and d["kilde_id"] not in kandidat_ider:
                continue
            h["merkelapp"] = "Prisendring"
            ut.append(h)
        elif t == "egen" and "egen" in typer:
            h["merkelapp"] = "Din bil"
            ut.append(h)
        elif t == "solgt" and "solgt" in typer:
            h["merkelapp"] = "Solgt"
            ut.append(h)
    return ut


def bygg_melding(v: dict, hendelser: list[dict]) -> tuple[str, str, str]:
    """(emne, ren tekst, html) for ett varsel."""
    navn = v["navn"]
    lenke = f"{base_url()}/vakt/{v['slug']}" if base_url() else ""
    antall = len(hendelser)
    emne = f"{navn}: {antall} {'ny hendelse' if antall == 1 else 'nye hendelser'}"
    linjer, html = [], []
    for h in hendelser:
        tekst = h["tekst"]
        linjer.append(f"• [{h['merkelapp']}] {tekst}" + (f"\n  {h['lenke']}" if h.get("lenke") else ""))
        html.append(f"<li><strong>{h['merkelapp']}</strong>: "
                    + (f'<a href="{h["lenke"]}">{tekst}</a>' if h.get("lenke") else tekst) + "</li>")
    ren = f"{navn}\n\n" + "\n".join(linjer) + (f"\n\nSe hele markedet: {lenke}" if lenke else "") + "\n"
    htm = (f"<p><strong>{navn}</strong></p><ul>{''.join(html)}</ul>"
           + (f'<p><a href="{lenke}">Se hele markedet</a></p>' if lenke else ""))
    return emne, ren, htm


def etter_henting(con, v: dict, fra_id: int) -> str | None:
    """Kalles etter en vellykket henting. Sender ett varsel per kanal eieren har.
    Returnerer en kort statusstreng (til loggen) eller None når det ikke var noe å si."""
    hendelser = plukk_hendelser(con, v, fra_id)
    if not hendelser:
        return None
    eier = con.execute("SELECT * FROM brukere WHERE id=?", (v.get("eier"),)).fetchone()
    if not eier:
        return None
    emne, ren, htm = bygg_melding(v, hendelser)
    lenke = f"{base_url()}/vakt/{v['slug']}" if base_url() else None
    status = []
    if eier["epost"] and (eier["varsel_epost"] if "varsel_epost" in eier.keys() else 1):
        try:
            send_epost(eier["epost"], emne, ren, htm)
            status.append("e-post")
        except Exception as e:  # noqa: BLE001 – varsling skal aldri velte hentingen
            logg.warning("e-post til %s feilet: %s", eier["epost"], e)
            status.append(f"e-post feilet ({e})")
    if eier["pushover"] if "pushover" in eier.keys() else None:
        try:
            send_pushover(eier["pushover"], emne, "\n".join(h["tekst"] for h in hendelser[:8]), lenke)
            status.append("pushover")
        except Exception as e:  # noqa: BLE001
            logg.warning("pushover til bruker %s feilet: %s", eier["id"], e)
            status.append(f"pushover feilet ({e})")
    if status:
        con.execute("UPDATE vakter SET sist_varslet=? WHERE slug=?", (db.naa(), v["slug"]))
        con.commit()
    return f"varslet {len(hendelser)} hendelser via {', '.join(status)}" if status else None


def siste_hendelse_id(con, slug: str) -> int:
    r = con.execute("SELECT COALESCE(MAX(id), 0) m FROM hendelser WHERE slug=?", (slug,)).fetchone()
    return int(r["m"])
