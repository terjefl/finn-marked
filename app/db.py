"""SQLite-lager for finn-marked. Én base, flere vakter (søk).

Hver vakt er ett marked (f.eks. «BMW i4 2022») med ett eller flere søk mot FINN.
Alt merkes med `slug` slik at flere vakter kan dele samme base.
"""

from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import threading
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

DATA = Path(os.environ.get("FINN_DATA", "/data"))

#: Ett tidsformat for hele basen. SQLite sin `datetime('now','localtime')` gir samme
#: form; poenget er at Python-siden ikke skal skrive ISO med «T» ved siden av, for da
#: sorterer tekstsammenligningen (`tid >= ?`) feil og hendelser forsvinner.
TIDSFORMAT = "%Y-%m-%d %H:%M:%S"

#: Skjemaversjonen `SKJEMA` under tilsvarer. Nye endringer legges i MIGRERINGER med
#: et høyere nummer — aldri ved å endre SKJEMA alene.
SKJEMA_BASELINE = 1

#: Nummererte migreringer: (versjon, [SQL …]). Eksempel:
#:     (2, ["ALTER TABLE annonser ADD COLUMN farge TEXT"]),
MIGRERINGER: list[tuple[int, list[str]]] = [
    # Flerbrukerstøtte: brukere, passkeys, sesjoner, invitasjoner og eier på vakten.
    (2, [
        """CREATE TABLE IF NOT EXISTS brukere (
             id INTEGER PRIMARY KEY AUTOINCREMENT,
             handle TEXT NOT NULL,           -- WebAuthn user.id (tilfeldig, ikke løpenummeret)
             navn TEXT NOT NULL, epost TEXT, rolle TEXT NOT NULL DEFAULT 'bruker',
             aktiv INTEGER NOT NULL DEFAULT 1, opprettet TEXT, sist_innlogget TEXT)""",
        """CREATE TABLE IF NOT EXISTS passkeys (
             id TEXT PRIMARY KEY,            -- credential id, base64url
             bruker_id INTEGER NOT NULL, public_key BLOB NOT NULL, sign_count INTEGER NOT NULL DEFAULT 0,
             transports TEXT, navn TEXT, opprettet TEXT, sist_brukt TEXT)""",
        """CREATE TABLE IF NOT EXISTS sesjoner (
             id TEXT PRIMARY KEY, bruker_id INTEGER NOT NULL, opprettet TEXT, utloper TEXT, brukeragent TEXT)""",
        """CREATE TABLE IF NOT EXISTS invitasjoner (
             token TEXT PRIMARY KEY, bruker_id INTEGER NOT NULL, opprettet TEXT, utloper TEXT, brukt TEXT)""",
        "CREATE INDEX IF NOT EXISTS idx_passkeys_bruker ON passkeys(bruker_id)",
        "CREATE INDEX IF NOT EXISTS idx_sesjoner_bruker ON sesjoner(bruker_id)",
        "ALTER TABLE vakter ADD COLUMN eier INTEGER",   # NULL til første admin er opprettet
    ]),
    # Egen bil fra registreringsnummer (Statens vegvesen): regnr + flat JSON med det vi viser.
    (3, [
        "ALTER TABLE vakter ADD COLUMN regnr TEXT",
        "ALTER TABLE vakter ADD COLUMN kjoretoy TEXT",
    ]),
    # FINN-API-et gir reg.nr, understellsnummer og når annonsen ble lagt ut: egne kolonner,
    # så samme bil kan gjenkjennes på tvers av annonser og liggetid måles fra FINN sin dato.
    (4, [
        "ALTER TABLE annonser ADD COLUMN regnr TEXT",
        "ALTER TABLE annonser ADD COLUMN vin TEXT",
        "ALTER TABLE annonser ADD COLUMN lagt_ut TEXT",
        "CREATE INDEX IF NOT EXISTS idx_annonser_vin ON annonser(slug, vin)",
        "CREATE INDEX IF NOT EXISTS idx_annonser_regnr ON annonser(slug, regnr)",
    ]),
    # Prisindeks per henting: regresjonens estimat ved fast kilometerstand, så utviklingen
    # over tid ikke farges av at bilene i markedet blir eldre. Pluss strøm inn/ut per dag.
    (5, [
        "ALTER TABLE kjøringer ADD COLUMN indeks INTEGER",
        "ALTER TABLE kjøringer ADD COLUMN ref_km INTEGER",
        "ALTER TABLE kjøringer ADD COLUMN r2 REAL",
        "ALTER TABLE kjøringer ADD COLUMN nye INTEGER",
        "ALTER TABLE kjøringer ADD COLUMN solgte INTEGER",
    ]),
]

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


@contextlib.contextmanager
def aapen() -> Iterator[sqlite3.Connection]:
    """Åpner basen og lukker den alltid.

    Rutene bruker denne (via FastAPI-dependencyen `web.kon`) i stedet for
    `con = db.base()` / try / finally `con.close()` i hver enkelt rute.
    """
    con = base()
    try:
        yield con
    finally:
        con.close()


def init() -> None:
    with _lock, base() as con:
        con.executescript(SKJEMA)
        if not con.execute("PRAGMA user_version").fetchone()[0]:
            con.execute(f"PRAGMA user_version={SKJEMA_BASELINE}")
        migrer(con)


def migrer(con: sqlite3.Connection) -> None:
    """Kjører migreringer basen ikke har fått ennå (sporet i `PRAGMA user_version`).

    `CREATE TABLE IF NOT EXISTS` legger ikke til kolonner i en base som alt finnes,
    så skjemaendringer må skrives som en migrering her — ellers ville en kjørende
    base manglet kolonnen til evig tid.
    """
    versjon = con.execute("PRAGMA user_version").fetchone()[0]
    for nr, setninger in MIGRERINGER:
        if nr <= versjon:
            continue
        for sql in setninger:
            con.execute(sql)
        con.execute(f"PRAGMA user_version={int(nr)}")
        con.commit()


def slett_gammelt(con: sqlite3.Connection, dager: int = 365) -> None:
    """Beskjærer hendelsesloggen.

    Pris-historikken beholdes med vilje: den er poenget med basen (prisutvikling over
    tid), og den vokser med noen få rader per annonse — ikke som hendelsesloggen.
    """
    con.execute("DELETE FROM hendelser WHERE tid < datetime('now','localtime', ?)",
               (f"-{dager} days",))


def vakter(con: sqlite3.Connection, kun_aktive: bool = False, eier: int | None = None) -> list[dict]:
    """Alle vakter med sok/kategorier ferdig parset (samme form som vakt()).

    `eier` avgrenser til én brukers vakter; uten den kommer alle (admin, scheduler)."""
    vilkaar, args = [], []
    if kun_aktive:
        vilkaar.append("aktiv=1")
    if eier is not None:
        vilkaar.append("eier=?")
        args.append(eier)
    sql = "SELECT * FROM vakter" + (" WHERE " + " AND ".join(vilkaar) if vilkaar else "")
    ut = []
    for r in con.execute(sql + " ORDER BY navn", args):
        v = dict(r)
        v["sok"] = json.loads(v.get("sok") or "[]")
        v["kategorier"] = json.loads(v.get("kategorier") or "{}")
        v["kjoretoy"] = json.loads(v.get("kjoretoy") or "null")
        ut.append(v)
    return ut


def vakt(con: sqlite3.Connection, slug: str) -> dict | None:
    r = con.execute("SELECT * FROM vakter WHERE slug=?", (slug,)).fetchone()
    if not r:
        return None
    v = dict(r)
    v["sok"] = json.loads(v["sok"] or "[]")
    v["kategorier"] = json.loads(v["kategorier"] or "{}")
    v["kjoretoy"] = json.loads(v.get("kjoretoy") or "null")
    return v


def lagre_vakt(con: sqlite3.Connection, v: dict) -> None:
    con.execute(
        """INSERT INTO vakter (slug, navn, sok, kategorier, egen_bil, egen_km, hentetime, aktiv,
                               opprettet, sist_hentet, sist_status, eier, regnr, kjoretoy)
           VALUES (:slug, :navn, :sok, :kategorier, :egen_bil, :egen_km, :hentetime, :aktiv,
                   COALESCE(:opprettet, :naa), :sist_hentet, :sist_status, :eier, :regnr, :kjoretoy)
           ON CONFLICT(slug) DO UPDATE SET
             navn=excluded.navn, sok=excluded.sok, kategorier=excluded.kategorier,
             egen_bil=excluded.egen_bil, egen_km=excluded.egen_km,
             hentetime=excluded.hentetime, aktiv=excluded.aktiv,
             eier=COALESCE(excluded.eier, vakter.eier),
             regnr=excluded.regnr, kjoretoy=excluded.kjoretoy""",
        {
            "slug": v["slug"], "navn": v["navn"],
            "sok": json.dumps(v.get("sok") or [], ensure_ascii=False),
            "kategorier": json.dumps(v.get("kategorier") or {}, ensure_ascii=False),
            "egen_bil": v.get("egen_bil"), "egen_km": v.get("egen_km"),
            "hentetime": int(v.get("hentetime") or 7), "aktiv": int(v.get("aktiv", 1)),
            "opprettet": v.get("opprettet"), "sist_hentet": v.get("sist_hentet"),
            "sist_status": v.get("sist_status"), "naa": naa(), "eier": v.get("eier"),
            "regnr": v.get("regnr") or None,
            "kjoretoy": json.dumps(v["kjoretoy"], ensure_ascii=False) if v.get("kjoretoy") else None,
        },
    )


def hendelse(con: sqlite3.Connection, slug: str, type_: str, lenke: str | None,
             tekst: str, detaljer: dict | None = None) -> None:
    con.execute("INSERT INTO hendelser (slug, tid, type, lenke, tekst, detaljer) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (slug, naa(), type_, lenke, tekst, json.dumps(detaljer or {}, ensure_ascii=False)))


def ok(con: sqlite3.Connection, sql: str, args: tuple = ()) -> list[dict]:
    return [dict(r) for r in con.execute(sql, args)]
