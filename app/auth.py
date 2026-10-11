"""Brukere, passkeys (WebAuthn) og sesjoner.

Modellen er bevisst liten:
  * én bruker har én rolle («admin» eller «bruker») og én eller flere passkeys,
  * en sesjon er en tilfeldig id i en HttpOnly-cookie, lagret i basen med utløpstid,
  * en invitasjon er et engangstoken som lar en ny bruker registrere sin første passkey.

Innlogging er brukernavnløs: nettleseren velger passkey selv (discoverable
credentials), så innloggingssiden er bare én knapp. Selve WebAuthn-kryptografien
gjør py_webauthn; her ligger bare flyten og lagringen rundt.

Utfordringene (challenges) holdes i minnet med kort levetid. Tjenesten kjører som én
prosess (scheduleren forutsetter det samme), så det er trygt.
"""

from __future__ import annotations

import os
import secrets
import sqlite3
import threading
import time
from datetime import datetime, timedelta

import webauthn
from fastapi import HTTPException, Request
from webauthn.helpers import base64url_to_bytes, bytes_to_base64url
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from . import db

SESJON_COOKIE = "fm_sesjon"
UTFORDRING_COOKIE = "fm_utfordring"
SESJON_DAGER = 30
INVITASJON_DAGER = 7
UTFORDRING_SEKUNDER = 300

#: Stier som er åpne uten innlogging (prefiks-match).
AAPNE_STIER = ("/helse", "/static/", "/logginn", "/loggut", "/oppsett", "/invitasjon/", "/api/auth/")


# ------------------------------------------------------------------ RP / origin

def rp_id(request: Request) -> str:
    """Relying party-id: vertsnavnet. `FINN_RP_ID` overstyrer (sett i drift)."""
    fast = os.environ.get("FINN_RP_ID")
    if fast:
        return fast
    vert = request.headers.get("host") or request.url.hostname or "localhost"
    return vert.split(":")[0]


def origin(request: Request) -> str:
    fast = os.environ.get("FINN_ORIGIN")
    if fast:
        return fast
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme or "http"
    vert = request.headers.get("host") or request.url.netloc
    return f"{proto}://{vert}"


def sikker_cookie(request: Request) -> bool:
    return origin(request).startswith("https://")


# ------------------------------------------------------------------ utfordringer

_utfordringer: dict[str, tuple[bytes, float, dict]] = {}
_utf_laas = threading.Lock()


def ny_utfordring(challenge: bytes, ekstra: dict) -> str:
    uid = secrets.token_urlsafe(24)
    with _utf_laas:
        naa = time.monotonic()
        for k in [k for k, (_, t, _) in _utfordringer.items() if t < naa]:
            del _utfordringer[k]
        _utfordringer[uid] = (challenge, naa + UTFORDRING_SEKUNDER, ekstra)
    return uid


def ta_utfordring(uid: str | None) -> tuple[bytes, dict]:
    """Henter og fjerner utfordringen — den kan bare brukes én gang."""
    with _utf_laas:
        rad = _utfordringer.pop(uid or "", None)
    if not rad or rad[1] < time.monotonic():
        raise HTTPException(400, "utfordringen er utløpt — prøv igjen")
    return rad[0], rad[2]


# ------------------------------------------------------------------ brukere

def antall_brukere(con: sqlite3.Connection) -> int:
    return con.execute("SELECT COUNT(*) c FROM brukere").fetchone()["c"]


def brukere(con: sqlite3.Connection) -> list[dict]:
    return db.ok(con, """SELECT b.*, (SELECT COUNT(*) FROM passkeys p WHERE p.bruker_id=b.id) passkeys,
                                (SELECT COUNT(*) FROM vakter v WHERE v.eier=b.id) vakter
                         FROM brukere b ORDER BY rolle, navn""")


def bruker(con: sqlite3.Connection, bruker_id: int | None) -> dict | None:
    if bruker_id is None:
        return None
    r = con.execute("SELECT * FROM brukere WHERE id=?", (bruker_id,)).fetchone()
    return dict(r) if r else None


def ny_bruker(con: sqlite3.Connection, navn: str, epost: str | None = None, rolle: str = "bruker") -> int:
    navn = (navn or "").strip()
    if not navn:
        raise HTTPException(400, "navn mangler")
    cur = con.execute("INSERT INTO brukere (handle, navn, epost, rolle, aktiv, opprettet) VALUES (?,?,?,?,1,?)",
                      (secrets.token_urlsafe(16), navn, (epost or "").strip() or None, rolle, db.naa()))
    return int(cur.lastrowid)


def er_admin(b: dict | None) -> bool:
    return bool(b and b.get("rolle") == "admin")


# ------------------------------------------------------------------ sesjoner

def _tid(dager: int) -> str:
    return (datetime.now() + timedelta(days=dager)).strftime(db.TIDSFORMAT)


def ny_sesjon(con: sqlite3.Connection, bruker_id: int, brukeragent: str | None) -> str:
    sid = secrets.token_urlsafe(32)
    con.execute("INSERT INTO sesjoner (id, bruker_id, opprettet, utloper, brukeragent) VALUES (?,?,?,?,?)",
                (sid, bruker_id, db.naa(), _tid(SESJON_DAGER), (brukeragent or "")[:200]))
    con.execute("UPDATE brukere SET sist_innlogget=? WHERE id=?", (db.naa(), bruker_id))
    con.execute("DELETE FROM sesjoner WHERE utloper < ?", (db.naa(),))
    return sid


def bruker_av_sesjon(con: sqlite3.Connection, sid: str | None) -> dict | None:
    if not sid:
        return None
    r = con.execute("""SELECT b.*, s.utloper FROM sesjoner s JOIN brukere b ON b.id=s.bruker_id
                       WHERE s.id=? AND s.utloper > ? AND b.aktiv=1""", (sid, db.naa())).fetchone()
    if not r:
        return None
    b = dict(r)
    # Glidende levetid: forleng når det er under halve perioden igjen.
    if b.pop("utloper") < _tid(SESJON_DAGER // 2):
        con.execute("UPDATE sesjoner SET utloper=? WHERE id=?", (_tid(SESJON_DAGER), sid))
        con.commit()
    return b


def slett_sesjon(con: sqlite3.Connection, sid: str | None) -> None:
    if sid:
        con.execute("DELETE FROM sesjoner WHERE id=?", (sid,))


def sett_sesjon_cookie(response, request: Request, sid: str) -> None:
    response.set_cookie(SESJON_COOKIE, sid, max_age=SESJON_DAGER * 86400, httponly=True,
                        samesite="lax", secure=sikker_cookie(request), path="/")


# ------------------------------------------------------------------ invitasjoner

def ny_invitasjon(con: sqlite3.Connection, bruker_id: int) -> str:
    token = secrets.token_urlsafe(24)
    con.execute("INSERT INTO invitasjoner (token, bruker_id, opprettet, utloper) VALUES (?,?,?,?)",
                (token, bruker_id, db.naa(), _tid(INVITASJON_DAGER)))
    return token


def invitasjon(con: sqlite3.Connection, token: str | None) -> dict | None:
    """Gyldig (ubrukt, ikke utløpt) invitasjon med brukerens navn, ellers None."""
    if not token:
        return None
    r = con.execute("""SELECT i.*, b.navn, b.rolle FROM invitasjoner i JOIN brukere b ON b.id=i.bruker_id
                       WHERE i.token=? AND i.brukt IS NULL AND i.utloper > ? AND b.aktiv=1""",
                    (token, db.naa())).fetchone()
    return dict(r) if r else None


# ------------------------------------------------------------------ passkeys

def passkeys(con: sqlite3.Connection, bruker_id: int) -> list[dict]:
    return db.ok(con, "SELECT id, navn, transports, opprettet, sist_brukt FROM passkeys WHERE bruker_id=? "
                      "ORDER BY opprettet", (bruker_id,))


def _oppsett_nokkel_ok(nokkel: str | None) -> bool:
    fasit = os.environ.get("FINN_OPPSETT_NOKKEL")
    return bool(fasit and nokkel and secrets.compare_digest(fasit, nokkel))


def registrering_start(request: Request, con: sqlite3.Connection, modus: str, *, bruker_id: int | None = None,
                       navn: str | None = None, token: str | None = None, nokkel: str | None = None) -> tuple[str, str]:
    """Første halvdel av registreringen. Returnerer (options som JSON, utfordrings-id).

    modus:  «oppsett»    – første admin (basen er tom) eller gjenoppretting med FINN_OPPSETT_NOKKEL
            «invitasjon» – ny bruker via token
            «konto»      – innlogget bruker legger til en passkey til
    """
    if modus == "oppsett":
        if antall_brukere(con) == 0:
            subjekt = {"ny": True, "navn": navn, "rolle": "admin"}
            visningsnavn = navn or "admin"
        elif _oppsett_nokkel_ok(nokkel) and bruker_id:
            b = bruker(con, bruker_id)
            if not b or not er_admin(b):
                raise HTTPException(400, "ukjent admin")
            subjekt = {"bruker_id": b["id"]}
            visningsnavn = b["navn"]
        else:
            raise HTTPException(404, "oppsettet er alt gjort")
    elif modus == "invitasjon":
        inv = invitasjon(con, token)
        if not inv:
            raise HTTPException(400, "invitasjonen er ugyldig eller utløpt")
        subjekt = {"bruker_id": inv["bruker_id"], "token": token}
        visningsnavn = inv["navn"]
    elif modus == "konto":
        if not bruker_id:
            raise HTTPException(401, "ikke innlogget")
        subjekt = {"bruker_id": bruker_id}
        visningsnavn = bruker(con, bruker_id)["navn"]
    else:
        raise HTTPException(400, "ukjent modus")

    if not (visningsnavn or "").strip():
        raise HTTPException(400, "navn mangler")
    eksisterende = []
    if subjekt.get("bruker_id"):
        eksisterende = [PublicKeyCredentialDescriptor(id=base64url_to_bytes(p["id"]))
                        for p in passkeys(con, subjekt["bruker_id"])]
        handle = bruker(con, subjekt["bruker_id"])["handle"]
    else:
        handle = secrets.token_urlsafe(16)
        subjekt["handle"] = handle
    options = webauthn.generate_registration_options(
        rp_id=rp_id(request), rp_name="FINN-marked",
        user_id=handle.encode(), user_name=visningsnavn, user_display_name=visningsnavn,
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.REQUIRED,
            user_verification=UserVerificationRequirement.PREFERRED),
        exclude_credentials=eksisterende,
    )
    uid = ny_utfordring(options.challenge, subjekt)
    return webauthn.options_to_json(options), uid


def registrering_fullfor(request: Request, con: sqlite3.Connection, uid: str | None, credential: dict,
                         passkey_navn: str | None) -> int:
    """Andre halvdel: verifiserer svaret, lagrer passkeyen (og brukeren, om ny).
    Returnerer bruker-id. Ruta lager sesjonen."""
    challenge, subjekt = ta_utfordring(uid)
    try:
        v = webauthn.verify_registration_response(
            credential=credential, expected_challenge=challenge,
            expected_rp_id=rp_id(request), expected_origin=origin(request))
    except Exception as e:  # noqa: BLE001 – py_webauthn kaster egne feiltyper; alle betyr «avvist»
        raise HTTPException(400, f"passkeyen ble avvist: {e}") from e

    if subjekt.get("ny"):
        bruker_id = ny_bruker(con, subjekt["navn"], rolle=subjekt["rolle"])
        con.execute("UPDATE brukere SET handle=? WHERE id=?", (subjekt["handle"], bruker_id))
        if subjekt["rolle"] == "admin":
            # Vakter fra før flerbrukerstøtten tilhører den første adminen.
            con.execute("UPDATE vakter SET eier=? WHERE eier IS NULL", (bruker_id,))
    else:
        bruker_id = int(subjekt["bruker_id"])
    transports = ",".join(credential.get("response", {}).get("transports") or [])
    con.execute("""INSERT INTO passkeys (id, bruker_id, public_key, sign_count, transports, navn, opprettet)
                   VALUES (?,?,?,?,?,?,?)""",
                (bytes_to_base64url(v.credential_id), bruker_id, v.credential_public_key, v.sign_count,
                 transports, (passkey_navn or "").strip()[:60] or _gjett_navn(request), db.naa()))
    if subjekt.get("token"):
        con.execute("UPDATE invitasjoner SET brukt=? WHERE token=?", (db.naa(), subjekt["token"]))
    return bruker_id


def _gjett_navn(request: Request) -> str:
    ua = (request.headers.get("user-agent") or "").lower()
    for nokkel, navn in (("iphone", "iPhone"), ("ipad", "iPad"), ("android", "Android"),
                         ("mac os", "Mac"), ("windows", "Windows"), ("linux", "Linux")):
        if nokkel in ua:
            return navn
    return "Passkey"


def logginn_start(request: Request) -> tuple[str, str]:
    options = webauthn.generate_authentication_options(
        rp_id=rp_id(request), user_verification=UserVerificationRequirement.PREFERRED)
    uid = ny_utfordring(options.challenge, {})
    return webauthn.options_to_json(options), uid


def logginn_fullfor(request: Request, con: sqlite3.Connection, uid: str | None, credential: dict) -> dict:
    """Verifiserer innloggingen og returnerer brukeren. Ruta lager sesjonen."""
    challenge, _ = ta_utfordring(uid)
    cid = credential.get("id") or ""
    p = con.execute("SELECT * FROM passkeys WHERE id=?", (cid,)).fetchone()
    if not p:
        raise HTTPException(400, "ukjent passkey")
    try:
        v = webauthn.verify_authentication_response(
            credential=credential, expected_challenge=challenge,
            expected_rp_id=rp_id(request), expected_origin=origin(request),
            credential_public_key=p["public_key"], credential_current_sign_count=p["sign_count"])
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"innloggingen ble avvist: {e}") from e
    con.execute("UPDATE passkeys SET sign_count=?, sist_brukt=? WHERE id=?", (v.new_sign_count, db.naa(), cid))
    b = bruker(con, p["bruker_id"])
    if not b or not b["aktiv"]:
        raise HTTPException(403, "brukeren er deaktivert")
    return b
