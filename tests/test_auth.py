#!/usr/bin/env python3
"""Tester for brukere, sesjoner, invitasjoner og eierskap.

Kjør: `.venv/bin/python tests/test_auth.py` (trenger fastapi, jinja2, webauthn og httpx).

Selve passkey-kryptografien testes ikke her — py_webauthn sin verifisering byttes ut
med en stubb, så flyten rundt (utfordring, bruker, sesjon, cookie, eier) kan kjøres
ende-til-ende uten nettleser.
"""

from __future__ import annotations

import logging
import shutil
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

feil: list[str] = []


def sjekk(navn: str, faktisk, ventet) -> None:
    ok = faktisk == ventet
    print(f"{'OK  ' if ok else 'FEIL'} {navn}: {faktisk!r}" + ("" if ok else f" (ventet {ventet!r})"))
    if not ok:
        feil.append(navn)


def hoved() -> int:
    mappe = Path(tempfile.mkdtemp(prefix="finn-auth-"))
    try:
        from app import auth, db
        db.sett_data(mappe)
        db.init()
        from fastapi.testclient import TestClient

        from app.web import app
        logging.getLogger("httpx").setLevel(logging.WARNING)

        # Stubber for py_webauthn: alt som når hit regnes som gyldig.
        auth.webauthn.verify_registration_response = lambda **k: SimpleNamespace(
            credential_id=b"cred-" + k["expected_challenge"][:4], credential_public_key=b"pk", sign_count=0)
        auth.webauthn.verify_authentication_response = lambda **k: SimpleNamespace(new_sign_count=1)

        with TestClient(app, base_url="http://testserver", follow_redirects=False) as c:
            # Åpent og lukket uten innlogging
            sjekk("helse åpen", c.get("/helse").status_code, 200)
            r = c.get("/vakt/ny")
            sjekk("uinnlogget sendes til logginn", (r.status_code, r.headers["location"]),
                  (303, "/logginn?videre=%2Fvakt%2Fny"))
            sjekk("api uten sesjon gir 401", c.get("/api/vakter").status_code, 401)
            sjekk("tom base: logginn → oppsett", c.get("/logginn").headers["location"], "/oppsett")
            sjekk("oppsett åpent når tom", c.get("/oppsett").status_code, 200)

            # Vakt fra før flerbruker (uten eier) skal havne hos første admin
            with db.aapen() as con:
                db.lagre_vakt(con, {"slug": "gammel", "navn": "Gammel", "sok": [], "kategorier": {}})
                con.commit()

            # Første admin registrerer seg (stubbet seremoni)
            r = c.post("/api/auth/registrer/start", json={"modus": "oppsett", "navn": "Terje"})
            sjekk("registrering start", r.status_code, 200)
            sjekk("rp-id fra Host", '"id":"testserver"' in r.json()["options"].replace(" ", ""), True)
            uid = r.json()["utfordring"]
            r = c.post("/api/auth/registrer/fullfor",
                       json={"utfordring": uid, "credential": {"id": "x", "response": {"transports": ["internal"]}},
                             "passkey_navn": "Mac"})
            sjekk("registrering fullført", r.status_code, 200)
            sjekk("sesjonscookie satt", auth.SESJON_COOKIE in c.cookies, True)
            sjekk("utfordring er engangs",
                  c.post("/api/auth/registrer/fullfor", json={"utfordring": uid, "credential": {}}).status_code, 400)
            with db.aapen() as con:
                admin = con.execute("SELECT * FROM brukere").fetchone()
                sjekk("første bruker er admin", admin["rolle"], "admin")
                sjekk("gammel vakt fikk eier", db.vakt(con, "gammel")["eier"], admin["id"])
                sjekk("passkey lagret med navn",
                      con.execute("SELECT navn, transports FROM passkeys").fetchone()[:], ("Mac", "internal"))

            sjekk("oppsett lukket etterpå", c.get("/oppsett").status_code, 404)
            sjekk("forside som admin", c.get("/").status_code, 200)
            sjekk("brukere-siden som admin", c.get("/admin/brukere").status_code, 200)
            sjekk("konto-siden", c.get("/konto").status_code, 200)

            # «Ny vakt»: steg 1, forhåndsvisning (stubbet kilde) og steg 2
            from app.kilder import finn as finn_kilde
            finn_kilde.forhandsvis = lambda url, antall=3: {
                "treff": 61, "sider": 2, "kort_pa_siden": 50, "titler": ["BMW i4"] * 40 + ["BMW iX"],
                "eksempler": [{"tittel": "BMW i4", "undertittel": "M50", "pris": 1, "km": 2, "aar": 2022, "url": "u"}]}
            sjekk("ny vakt steg 1", "Sjekk søket" in c.get("/vakt/ny").text, True)
            r = c.get("/api/forhandsvis", params={"url": "https://www.finn.no/mobility/search/car?year_from=2022&year_to=2022&page=2"})
            sjekk("forhåndsvis", (r.json()["treff"], r.json()["navn"], r.json()["slug"]), (61, "BMW i4 2022", "bmw-i4-2022"))
            sjekk("forhåndsvis advarer", c.get("/api/forhandsvis", params={"url": "https://vg.no/"}).json()["advarsel"][:12], "Ukjent kilde")
            r = c.get("/vakt/ny", params={"url": "https://www.finn.no/mobility/search/car?a=1&page=2", "navn": "BMW i4 2022", "slug": "bmw-i4-2022"})
            sjekk("ny vakt steg 2 forhåndsutfylt", 'value="https://www.finn.no/mobility/search/car?a=1"' in r.text and 'value="bmw-i4-2022"' in r.text, True)

            # «Ny vakt» fra registreringsnummer (Vegvesenet og FINN-treet stubbet)
            from app import kjoretoy as kjmod
            kjmod.slaa_opp = lambda regnr: {"regnr": "EB12345", "merke": "BMW", "modell": "I4", "aar": 2022,
                                            "elektrisk": True, "beskrivelse": "BMW I4 M50 xDrive 2022"}
            finn_kilde.hent_merketre = lambda tving=False: [
                {"navn": "BMW", "kode": "0.749", "modeller": [{"navn": "i4", "kode": "1.749.8308"}]}]
            r = c.get("/oppslag", params={"regnr": "eb12345"})
            sjekk("oppslag-siden", r.status_code == 200 and "BMW I4 M50 xDrive 2022" in r.text
                  and 'href="/vakt/ny?regnr=EB12345"' in r.text, True)
            sjekk("oppslag uten regnr", c.get("/oppslag").status_code, 200)
            sjekk("ny vakt med regnr forhåndsutfylt", 'value="EB12345"' in c.get("/vakt/ny", params={"regnr": "EB12345"}).text, True)
            r = c.get("/api/sokforslag", params={"regnr": "eb12345"}).json()
            sjekk("sokforslag url", r["url"],
                  "https://www.finn.no/mobility/search/car?variant=1.749.8308&year_from=2022&year_to=2022&fuel=4")
            sjekk("sokforslag navn", (r["navn"], r["slug"], r["advarsel"]), ("BMW i4 2022", "bmw-i4-2022", None))
            r = c.get("/vakt/ny", params={"url": r["url"], "navn": "BMW i4 2022", "slug": "x", "regnr": "EB12345"})
            sjekk("steg 2 har regnr og egen bil", 'value="EB12345"' in r.text and 'value="BMW I4 M50 xDrive 2022"' in r.text, True)

            # Lagring med rader (søk og kategorier), og automatisk første henting
            web_hent: list = []
            import app.web as webmod
            webmod._hent_jobb = lambda slug, detaljer=False: web_hent.append(slug)
            r = c.post("/vakt/lagre", data={
                "slug": "BMW i4 2022", "navn": "BMW i4 2022", "hentetime": "7", "aktiv": "1",
                "sok_tag": ["", "fully", "x"],
                "sok_url": ["https://www.finn.no/mobility/search/car?a=1", "https://www.finn.no/mobility/search/car?q=fully", ""],
                "sok_kilde": ["auto", "auto", "auto"],
                "kat_navn": ["Fully Charged", "M50"], "kat_nokkel": ["", "m50"]})
            sjekk("lagre med rader", (r.status_code, r.headers["location"]), (303, "/vakt/bmw-i4-2022"))
            with db.aapen() as con:
                v = db.vakt(con, "bmw-i4-2022")
                sjekk("søk fra rader", [(x["tag"], x["kilde"]) for x in v["sok"]], [("alle", "finn"), ("fully", "finn")])
                sjekk("kategorier fra rader", v["kategorier"], {"fully-charged": "Fully Charged", "m50": "M50"})
                sjekk("eier er den innloggede", v["eier"], admin["id"])
            import time as _t
            _t.sleep(0.2)
            sjekk("første henting startet automatisk", web_hent, ["bmw-i4-2022"])
            c.post("/vakt/lagre", data={"slug": "bmw-i4-2022", "navn": "BMW i4 2022 (endret)", "hentetime": "8",
                                        "sok_tag": "alle", "sok_url": "https://www.finn.no/mobility/search/car?a=1"})
            _t.sleep(0.2)
            sjekk("redigering henter ikke på nytt", web_hent, ["bmw-i4-2022"])
            with db.aapen() as con:
                sjekk("redigering beholder eier", db.vakt(con, "bmw-i4-2022")["eier"], admin["id"])

            # Inviter Kari og lag en vakt til henne på forhånd
            r = c.post("/admin/brukere/ny", data={"navn": "Kari", "epost": "", "rolle": "bruker"})
            sjekk("ny bruker → lenke", r.status_code == 303 and "lenke=" in r.headers["location"], True)
            with db.aapen() as con:
                kari = con.execute("SELECT * FROM brukere WHERE navn='Kari'").fetchone()
                token = con.execute("SELECT token FROM invitasjoner WHERE bruker_id=?", (kari["id"],)).fetchone()[0]
            r = c.post("/vakt/lagre", data={"slug": "kari-bil", "navn": "Karis bil", "sok": "",
                                            "kategorier": "", "egen_bil": "Tesla", "egen_km": "1000",
                                            "hentetime": "7", "aktiv": "1", "eier": str(kari["id"])})
            sjekk("admin lager vakt for Kari", r.status_code, 303)
            with db.aapen() as con:
                sjekk("vakten eies av Kari", db.vakt(con, "kari-bil")["eier"], kari["id"])
            sjekk("admin ser Karis vakt", c.get("/vakt/kari-bil").status_code, 200)
            r = c.get("/vakt/bmw-i4-2022/kopier", params={"til": kari["id"]})
            sjekk("kopi til Kari forhåndsutfylt",
                  'value="BMW i4 2022 (endret) (Kari)"' in r.text and 'value="bmw-i4-2022-kari"' in r.text
                  and f'<option value="{kari["id"]}" selected' in r.text, True)

            # Kari logger inn via invitasjonen i en annen klient
            with TestClient(app, base_url="http://testserver", follow_redirects=False) as k:
                sjekk("invitasjonsside", "Kari" in k.get(f"/invitasjon/{token}").text, True)
                r = k.post("/api/auth/registrer/start", json={"modus": "invitasjon", "token": token})
                sjekk("invitasjon start", r.status_code, 200)
                r = k.post("/api/auth/registrer/fullfor",
                           json={"utfordring": r.json()["utfordring"], "credential": {"id": "y", "response": {}}})
                sjekk("invitasjon fullført", r.status_code, 200)
                sjekk("invitasjon brukt opp", "gjelder ikke lenger" in k.get(f"/invitasjon/{token}").text, True)
                sjekk("Kari ser egen vakt", k.get("/vakt/kari-bil").status_code, 200)
                sjekk("Kari ser ikke admins vakt", k.get("/vakt/gammel").status_code, 404)
                sjekk("Kari kan ikke redigere admins vakt", k.get("/vakt/gammel/rediger").status_code, 404)
                sjekk("Kari får ikke brukere-siden", k.get("/admin/brukere").status_code, 404)
                sjekk("Kari får ikke kopiere", k.get("/vakt/kari-bil/kopier").status_code, 404)
                sjekk("Kari kan ikke overskrive admins kortnavn",
                      k.post("/vakt/lagre", data={"slug": "gammel", "navn": "x", "sok": "", "hentetime": "7"}).status_code, 404)
                r = k.post("/vakt/lagre", data={"slug": "kari-2", "navn": "Kari 2", "sok": "", "hentetime": "7",
                                                "eier": str(admin["id"])})
                with db.aapen() as con:
                    sjekk("vanlig bruker kan ikke velge eier", db.vakt(con, "kari-2")["eier"], kari["id"])
                sjekk("api viser bare egne", sorted(v["vakt"]["slug"] for v in k.get("/api/vakter").json()["vakter"]),
                      ["kari-2", "kari-bil"])
                r = k.post("/loggut")
                sjekk("loggut", (r.status_code, r.headers["location"]), (303, "/logginn"))
                sjekk("etter loggut: avvist", k.get("/konto").status_code, 303)

            # Passkey-innlogging (stubbet) med Karis credential
            with db.aapen() as con:
                cid = con.execute("SELECT id FROM passkeys WHERE bruker_id=?", (kari["id"],)).fetchone()[0]
            with TestClient(app, base_url="http://testserver", follow_redirects=False) as k:
                r = k.post("/api/auth/logginn/start")
                sjekk("logginn start", r.status_code, 200)
                r = k.post("/api/auth/logginn/fullfor", json={"utfordring": r.json()["utfordring"],
                                                              "credential": {"id": cid}})
                sjekk("logginn fullført", r.status_code, 200)
                sjekk("logget inn som Kari", "Kari" in k.get("/konto").text, True)
                sjekk("ukjent passkey avvist",
                      k.post("/api/auth/logginn/fullfor",
                             json={"utfordring": k.post("/api/auth/logginn/start").json()["utfordring"],
                                   "credential": {"id": "finnes-ikke"}}).status_code, 400)

            # Deaktivering dreper sesjonen
            c.post(f"/admin/brukere/{kari['id']}/aktiv", data={"aktiv": "0"})
            with db.aapen() as con:
                sjekk("deaktivert: ingen sesjoner igjen",
                      con.execute("SELECT COUNT(*) FROM sesjoner WHERE bruker_id=?", (kari["id"],)).fetchone()[0], 0)
            sjekk("admin kan ikke deaktivere seg selv",
                  c.post(f"/admin/brukere/{admin['id']}/aktiv", data={"aktiv": "0"}).status_code, 400)
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
