"""Bakgrunnsjobb: henter markedet én gang i døgnet per aktiv vakt.

Kjører i en egen tråd i web-prosessen (tjenesten har ingen ekstern cron i
containeren). Hver vakt har sin egen `hentetime`; siste hentedato lagres i basen,
så en omstart av containeren ikke gir dobbel henting samme dag.
"""

from __future__ import annotations

import logging
import threading
import time
import traceback
from datetime import datetime

from . import db, innsamling, varsling

log = logging.getLogger("finn.scheduler")

INTERVALL = 300  # sekunder mellom hver sjekk
_state: dict[str, object] = {"siste_sjekk": None, "siste_feil": None, "siste_resultat": None}


def status() -> dict:
    return dict(_state)


def start() -> None:
    # Uten dette havner scheduler-loggene ingen steder: docker logs viser bare det
    # uvicorn selv skriver, og en vakt som feiler ville vært usynlig.
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    threading.Thread(target=_lokke, daemon=True, name="finn-scheduler").start()


def _lokke() -> None:
    time.sleep(20)  # la web-tjeneren komme opp først
    while True:
        try:
            _sjekk()
            _state["siste_feil"] = None
        except Exception:  # noqa: BLE001 – tråden skal aldri dø av en enkelt feil
            _state["siste_feil"] = traceback.format_exc(limit=4)
        _state["siste_sjekk"] = datetime.now().isoformat(timespec="seconds")
        time.sleep(INTERVALL)


def _sjekk() -> None:
    con = db.base()
    try:
        naa = datetime.now()
        for v in db.vakter(con, kun_aktive=True):
            if naa.hour < int(v.get("hentetime") or 7):
                continue
            sist = (v.get("sist_hentet") or "")[:10]
            if sist == naa.date().isoformat():
                continue
            # Én vakt som feiler skal ikke stoppe de andre. Feilen havner i vaktens
            # egen status, og sporet går til loggen (docker logs). Hentelåsen er felles
            # med «Oppdater nå»-knappen, så planlagt og manuell henting ikke kolliderer.
            with innsamling.hentelaas(v["slug"]) as fikk:
                if not fikk:
                    log.info("%s: hopper over — en henting kjører alt", v["slug"])
                    continue
                try:
                    fra_id = varsling.siste_hendelse_id(con, v["slug"])
                    res = innsamling.hent_vakt(con, v)
                    try:
                        vs = varsling.etter_henting(con, v, fra_id)
                        if vs:
                            logging.getLogger("finn-marked").info("%s: %s", v["slug"], vs)
                    except Exception:  # noqa: BLE001 – varsling skal ikke stoppe de andre vaktene
                        logging.getLogger("finn-marked").exception("varsling feilet for %s", v["slug"])
                    _state["siste_resultat"] = res
                    log.info("%s: %s", v["slug"], res.get("status"))
                    innsamling.berik_detaljer(con, v, grense=15)
                except Exception as e:  # noqa: BLE001 – skal ikke velte de andre vaktene
                    log.exception("vakt %s feilet", v.get("slug"))
                    try:
                        con.rollback()          # ikke lagre halvferdige data fra feilen
                        con.execute("UPDATE vakter SET sist_status=? WHERE slug=?",
                                    (f"FEIL: {e}", v["slug"]))
                        con.commit()
                    except Exception:  # noqa: BLE001
                        log.exception("kunne ikke skrive feilstatus for %s", v.get("slug"))
    finally:
        con.close()
