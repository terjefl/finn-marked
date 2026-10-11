"""Bakgrunnsjobb: henter markedet én gang i døgnet per aktiv vakt.

Kjører i en egen tråd i web-prosessen (tjenesten har ingen ekstern cron i
containeren). Hver vakt har sin egen `hentetime`; siste hentedato lagres i basen,
så en omstart av containeren ikke gir dobbel henting samme dag.
"""

from __future__ import annotations

import threading
import time
import traceback
from datetime import datetime

from . import db, innsamling

INTERVALL = 300  # sekunder mellom hver sjekk
_state = {"siste_sjekk": None, "siste_feil": None, "siste_resultat": None}


def status() -> dict:
    return dict(_state)


def start() -> None:
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
            res = innsamling.hent_vakt(con, v)
            _state["siste_resultat"] = res
            innsamling.berik_detaljer(con, v, grense=15)
    finally:
        con.close()
