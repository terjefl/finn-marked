"""Søke-oppsettet fra skjemaet: `[kilde:][tag] | url` og `nokkel = visningsnavn`.

Flyttet ut av `web.py` fordi det er ren tekstbehandling: da kan det testes uten
FastAPI og Jinja2 (se `tests/test_parser.py`).
"""

from __future__ import annotations

import urllib.parse


def kilde_fra_url(url: str) -> str:
    """Kilden utledes av vertsnavnet — da slipper brukeren å huske kildenavnet,
    og en limt inn søke-URL fra auksjonen.no treffer riktig adapter."""
    vert = urllib.parse.urlparse(url).netloc.lower()
    if "auksjonen.no" in vert:
        return "auksjonen"
    return "finn"


def parse_sok(tekst: str) -> list[dict]:
    """Ett søk per linje, på formen `[kilde:][tag] | <url>` (tag kan utelates).

    Kilden utledes av URL-en når den ikke står eksplisitt; skriv `auksjonen:auksjon`
    bare hvis du vil overstyre.
    """
    ut = []
    for linje in (tekst or "").splitlines():
        linje = linje.strip()
        if not linje or linje.startswith("#"):
            continue
        merke, url = ([d.strip() for d in linje.split("|", 1)] if "|" in linje else ("", linje))
        if not url.startswith("http"):
            continue
        kilde, tag = None, merke or None
        if merke and ":" in merke:
            kilde, tag = [d.strip() for d in merke.split(":", 1)]
        kilde = kilde or kilde_fra_url(url)
        ut.append({"kilde": kilde, "tag": tag or ("alle" if kilde == "finn" else kilde), "url": url})
    return ut


def parse_kategorier(tekst: str) -> dict:
    """Ett par per linje: `nokkel = Visningsnavn` (nøkkelen matcher q=-tag og fritekst-nøkkel)."""
    ut = {}
    for linje in (tekst or "").splitlines():
        if "=" in linje:
            k, navn = [d.strip() for d in linje.split("=", 1)]
            if k:
                ut[k] = navn or k
    return ut
