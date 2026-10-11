"""SQLite-lager for finn-marked. Én base, flere vakter (søk).

Hver vakt er ett marked (f.eks. «BMW i4 2022») med ett eller flere søk mot FINN.
Alt merkes med `slug` slik at flere vakter kan dele samme base.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import datetime
from pathlib import Path

DATA = Path(os.environ.get("FINN_DATA", "/data"))

#: Ett tidsformat for hele basen. SQLite sin `datetime('now','localtime')` gir samme
#: form; poenget er at Python-siden ikke skal skrive ISO med «T» ved siden av, for da
#: sorterer tekstsammenligningen (`tid >= ?`) feil og hendelser forsvinner.
TIDSFORMAT = "%Y-%m-%d %H:%M:%S"

SKJEMA = """
CREATE TABLE IF NOT EXISTS vakter (
  slug TEXT PRIMARY KEY,
  navn TEXT NOT NULL,
  sok TEXT NOT NULL,              -- JSON: [{"tag": "alle", "url": "https://..."}]
  kategorier TEXT,                -- JSON: {"fully": "Fully Charged", ...}
  egen_bil TEXT,
  egen_km INTEGER,
  hentetime INTEGER DEFAULT 7,    -- lokal time for daglig henting
  aktiv INTEGER DEFAULT 1,
  opprettet TEXT,
  sist_hentet TEXT,
  sist_status TEXT
);

CREATE TABLE IF NOT EXISTS annonser (
  slug TEXT, kilde TEXT DEFAULT 'finn', kilde_id TEXT,
  tittel TEXT, undertittel TEXT, aar INTEGER, km INTEGER, drivstoff TEXT,
  rekkevidde INTEGER, pris INTEGER, pris_type TEXT, sted TEXT, selger TEXT,
  selger_type TEXT, url TEXT, tagger TEXT, sok_tagger TEXT, ekstra TEXT,
  forst_sett TEXT, sist_sett TEXT, borte_dato TEXT,
  aktiv INTEGER, merke TEXT, solgt INTEGER DEFAULT 0, solgt_dato TEXT,
  PRIMARY KEY (slug, kilde, kilde_id)
);

CREATE TABLE IF NOT EXISTS pris_historikk (
  slug TEXT, kilde TEXT, kilde_id TEXT, dato TEXT, pris INTEGER, pris_type TEXT
);

-- Detaljsiden (utstyr, eiere, garantier), hentes sjeldnere enn søket.
CREATE TABLE IF NOT EXISTS detaljer (
  slug TEXT, kilde TEXT, kilde_id TEXT, hentet TEXT, felter TEXT, utstyr TEXT, beskrivelse TEXT,
  PRIMARY KEY (slug, kilde, kilde_id)
);

CREATE TABLE IF NOT EXISTS kjøringer (
  slug TEXT, dato TEXT, annonser_hentet INTEGER, treff INTEGER, aktive INTEGER,
  median INTEGER, p25 INTEGER, p75 INTEGER, min INTEGER, max INTEGER, varighet REAL
);

-- Hendelseslogg: det UI-et viser som «hva har skjedd» og API-et serverer som
-- endringsstrøm (Hermes-varsling leser denne via /api/vakt/<slug>/endringer).
CREATE TABLE IF NOT EXISTS hendelser (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  slug TEXT, tid TEXT, type TEXT, lenke TEXT, tekst TEXT, detaljer TEXT
);

CREATE INDEX IF NOT EXISTS idx_annonser_slug ON annonser(slug, aktiv, solgt);
CREATE INDEX IF NOT EXISTS idx_hendelser_slug ON hendelser(slug, tid);
"""

_lock = threading.Lock()


def naa() -> str:
    """Nå, i basens format (lokal tid)."""
    return datetime.now().strftime(TIDSFORMAT)


def normaliser_tid(verdi: str | None) -> str | None:
    """Gjør et tidsstempel om til basens format.

    Tar ISO med «T» (`2026-10-11T13:52:11`, med eller uten sone) i tillegg til basens
    eget format. Uten dette ble `?since=2026-10-11T13:52:11` sammenlignet som tekst mot
    `2026-10-11 13:52:11` og ga null treff på hendelser som fantes.
    """
    if not verdi:
        return verdi
    v = verdi.strip()
    if "T" not in v:
        return v
    try:
        tid = datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        return v
    if tid.tzinfo is not None:                 # sone gitt: legg om til lokal tid
        tid = tid.astimezone().replace(tzinfo=None)
    return tid.strftime(TIDSFORMAT)


def data() -> Path:
    """Basens mappe, lest på nytt hver gang.

    `FINN_DATA` må kunne settes *etter* import — scripts/importer.py satte den først
    inne i funksjonen, etter at `DATA` alt var beregnet, og skrev dermed til feil base.
    """
    return Path(os.environ.get("FINN_DATA", "/data"))


def sett_data(mappe: str | Path) -> None:
    """Peker basen et annet sted (brukes av importeren og av testene)."""
    os.environ["FINN_DATA"] = str(mappe)


def base() -> sqlite3.Connection:
    mappe = data()
    mappe.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(mappe / "finn-marked.sqlite", timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    return con


def init() -> None:
    with _lock, base() as con:
        con.executescript(SKJEMA)


def slett_gammelt(con: sqlite3.Connection, dager: int = 365) -> None:
    """Beskjærer hendelsesloggen.

    Pris-historikken beholdes med vilje: den er poenget med basen (prisutvikling over
    tid), og den vokser med noen få rader per annonse — ikke som hendelsesloggen.
    """
    con.execute("DELETE FROM hendelser WHERE tid < datetime('now','localtime', ?)",
               (f"-{dager} days",))


def vakter(con: sqlite3.Connection, kun_aktive: bool = False) -> list[dict]:
    """Alle vakter med sok/kategorier ferdig parset (samme form som vakt())."""
    sql = "SELECT * FROM vakter"
    if kun_aktive:
        sql += " WHERE aktiv=1"
    ut = []
    for r in con.execute(sql + " ORDER BY navn"):
        v = dict(r)
        v["sok"] = json.loads(v.get("sok") or "[]")
        v["kategorier"] = json.loads(v.get("kategorier") or "{}")
        ut.append(v)
    return ut


def vakt(con: sqlite3.Connection, slug: str) -> dict | None:
    r = con.execute("SELECT * FROM vakter WHERE slug=?", (slug,)).fetchone()
    if not r:
        return None
    v = dict(r)
    v["sok"] = json.loads(v["sok"] or "[]")
    v["kategorier"] = json.loads(v["kategorier"] or "{}")
    return v


def lagre_vakt(con: sqlite3.Connection, v: dict) -> None:
    con.execute(
        """INSERT INTO vakter (slug, navn, sok, kategorier, egen_bil, egen_km, hentetime, aktiv,
                               opprettet, sist_hentet, sist_status)
           VALUES (:slug, :navn, :sok, :kategorier, :egen_bil, :egen_km, :hentetime, :aktiv,
                   COALESCE(:opprettet, :naa), :sist_hentet, :sist_status)
           ON CONFLICT(slug) DO UPDATE SET
             navn=excluded.navn, sok=excluded.sok, kategorier=excluded.kategorier,
             egen_bil=excluded.egen_bil, egen_km=excluded.egen_km,
             hentetime=excluded.hentetime, aktiv=excluded.aktiv""",
        {
            "slug": v["slug"], "navn": v["navn"],
            "sok": json.dumps(v.get("sok") or [], ensure_ascii=False),
            "kategorier": json.dumps(v.get("kategorier") or {}, ensure_ascii=False),
            "egen_bil": v.get("egen_bil"), "egen_km": v.get("egen_km"),
            "hentetime": int(v.get("hentetime") or 7), "aktiv": int(v.get("aktiv", 1)),
            "opprettet": v.get("opprettet"), "sist_hentet": v.get("sist_hentet"),
            "sist_status": v.get("sist_status"), "naa": naa(),
        },
    )


def hendelse(con: sqlite3.Connection, slug: str, type_: str, lenke: str | None,
             tekst: str, detaljer: dict | None = None) -> None:
    con.execute("INSERT INTO hendelser (slug, tid, type, lenke, tekst, detaljer) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (slug, naa(), type_, lenke, tekst, json.dumps(detaljer or {}, ensure_ascii=False)))


def ok(con: sqlite3.Connection, sql: str, args: tuple = ()) -> list[dict]:
    return [dict(r) for r in con.execute(sql, args)]
