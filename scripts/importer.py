#!/usr/bin/env python3
"""Importerer historikk fra Hermes-scriptets base inn i finn-marked-basen.

Brukes én gang (eller ved flytting av en vakt) slik at annonser som allerede er
observert ikke mister «først sett», pris-historikk og solgt-status.

  python3 scripts/importer.py --kilde ~/.hermes/data/finn_bil_marked/bmw-i4-2022.sqlite \
      --slug bmw-i4-2022 --db /data/finn-marked.sqlite [--tørr]

Kolonnenavnene i den gamle basen er finn_id (ny: kilde_id, kilde='finn').
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import db  # noqa: E402


def importer(gammel: Path, mål: Path, slug: str, ny_slug: str | None, tørr: bool) -> None:
    if not gammel.exists():
        sys.exit(f"finner ikke {gammel}")
    g = sqlite3.connect(gammel)
    g.row_factory = sqlite3.Row
    import os
    if mål:
        os.environ["FINN_DATA"] = str(mål.parent)
    db.init()
    con = db.base()

    rader = g.execute("SELECT * FROM annonser WHERE vakt=?", (slug,)).fetchall()
    print(f"leser {len(rader)} annonser fra {gammel}")
    n = 0
    for r in rader:
        d = dict(r)
        con.execute(
            """INSERT OR REPLACE INTO annonser (slug, kilde, kilde_id, tittel, undertittel, aar, km,
                   drivstoff, rekkevidde, pris, pris_type, sted, selger, selger_type, url, tagger,
                   sok_tagger, ekstra, forst_sett, sist_sett, borte_dato, aktiv, merke, solgt, solgt_dato)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            ((ny_slug or slug), "finn", d["finn_id"], d["tittel"], d["undertittel"], d["aar"], d["km"],
             d["drivstoff"], d["rekkevidde"], d["pris"], "solgt" if d.get("solgt") else "utropspris",
             d["sted"], d["selger"], d["selger_type"], d["url"], d["tagger"], "[]",
             json.dumps({"importert": True}, ensure_ascii=False), d["forst_sett"], d["sist_sett"],
             d["borte_dato"], d["aktiv"], d.get("merke"), d.get("solgt") or 0, d.get("solgt_dato")))
        n += 1

    priser = g.execute("SELECT * FROM pris_historikk").fetchall()
    for r in priser:
        d = dict(r)
        con.execute("INSERT INTO pris_historikk (slug, kilde, kilde_id, dato, pris, pris_type) "
                    "VALUES (?,?,?,?,?,?)", ((ny_slug or slug), "finn", d["finn_id"], d["dato"],
                                             d["pris"], "utropspris"))
    print(f"la inn {n} annonser og {len(priser)} prispunkter")

    # Vakt-oppføringen
    if not con.execute("SELECT 1 FROM vakter WHERE slug=?", (ny_slug or slug,)).fetchone():
        db.lagre_vakt(con, {"slug": (ny_slug or slug), "navn": slug, "sok": [], "kategorier": {},
                            "egen_km": None, "egen_bil": None, "hentetime": 7, "aktiv": 0})
        print(f"opprettet (inaktiv) vakt-oppføring «{ny_slug or slug}» — fyll inn søk i UI-et")
    if tørr:
        con.rollback()
        print("TØRRKJØRING: ingen endringer skrevet")
    else:
        con.commit()
        print(f"skrevet til {mål or db.DATA / 'finn-marked.sqlite'}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--kilde", required=True, type=Path)
    ap.add_argument("--db", type=Path, default=None, help="målbase (mappe eller fil)")
    ap.add_argument("--slug", required=True, help="slug i den gamle basen (kolonnen «vakt»)")
    ap.add_argument("--ny-slug", default=None)
    ap.add_argument("--tørr", action="store_true")
    a = ap.parse_args()
    importer(a.kilde.expanduser(), a.db.expanduser() if a.db else None, a.slug, a.ny_slug, a.tørr)


if __name__ == "__main__":
    main()
