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


def slugify(tekst: str) -> str:
    """Kortnavn av fritekst: «BMW i4 2022 (M50)» → «bmw-i4-2022-m50». Æ/Ø/Å blir ae/oe/aa."""
    t = (tekst or "").lower()
    for a, b in (("æ", "ae"), ("ø", "oe"), ("å", "aa"), ("é", "e"), ("ü", "u"), ("ö", "o"), ("ä", "a")):
        t = t.replace(a, b)
    t = "".join(c if (c.isalnum() and c.isascii()) or c in "-_" else "-" for c in t)
    while "--" in t:
        t = t.replace("--", "-")
    return t.strip("-")


def rens_url(url: str) -> str:
    """Fjerner `page=` (vi paginerer selv) og tomme parametre fra en limt søke-URL."""
    url = (url or "").strip()
    deler = urllib.parse.urlsplit(url)
    q = [(k, v) for k, v in urllib.parse.parse_qsl(deler.query, keep_blank_values=False) if k != "page"]
    return urllib.parse.urlunsplit((deler.scheme, deler.netloc, deler.path, urllib.parse.urlencode(q), ""))


def sjekk_url(url: str) -> str | None:
    """Advarsel hvis URL-en ikke ser ut som et søk vi kan hente, ellers None."""
    deler = urllib.parse.urlsplit((url or "").strip())
    if deler.scheme not in ("http", "https") or not deler.netloc:
        return "Dette er ikke en nettadresse."
    vert = deler.netloc.lower()
    if "finn.no" in vert:
        if "/mobility/item/" in deler.path:
            return "Dette er én annonse, ikke et søk."
        if "/mobility/search" not in deler.path:
            return ("FINN-adressen ser ikke ut som et søk (forventer /mobility/search/…). "
                    "Gjør søket på finn.no og kopier adressen fra adressefeltet.")
        if deler.path.rstrip("/").endswith(("/car", "/caravan", "/mobilehome", "/mc", "/agriculturetractor")):
            return None
        return "Bare bil, bobil, campingvogn, MC og traktor støttes hos FINN."
    if "auksjonen.no" in vert:
        return None
    return f"Ukjent kilde «{deler.netloc}» — bare finn.no og auksjonen.no støttes."


def bygg_sok(tags: list[str], urls: list[str], kilder: list[str] | None = None) -> list[dict]:
    """Søk fra skjemaets rader (tag, url, [kilde]). Tomme rader hoppes over,
    kilden utledes av URL-en når raden sier «auto» eller ingenting."""
    ut = []
    kilder = kilder or []
    for i, url in enumerate(urls):
        url = rens_url(url)
        if not url.startswith("http"):
            continue
        tag = (tags[i] if i < len(tags) else "").strip()
        kilde = (kilder[i] if i < len(kilder) else "").strip()
        if kilde in ("", "auto"):
            kilde = kilde_fra_url(url)
        ut.append({"kilde": kilde, "tag": tag or ("alle" if kilde == "finn" else kilde), "url": url})
    return ut


def bygg_kategorier(nokler: list[str], navn: list[str]) -> dict:
    """Kategorier fra skjemaets rader. Mangler nøkkelen, lages den av visningsnavnet."""
    ut = {}
    for i, n in enumerate(navn):
        n = (n or "").strip()
        k = (nokler[i] if i < len(nokler) else "").strip()
        if not n and not k:
            continue
        k = k or slugify(n)
        if k:
            ut[k] = n or k
    return ut


def navneforslag(url: str, titler: list[str]) -> tuple[str, str]:
    """Forslag til (navn, kortnavn) for en ny vakt: vanligste tittel blant kortene
    pluss årstall fra søkets year_from/year_to. «BMW i4» + 2022 → «BMW i4 2022»."""
    tittel = ""
    if titler:
        telling: dict[str, int] = {}
        for t in titler:
            t = (t or "").strip()
            if t:
                telling[t] = telling.get(t, 0) + 1
        if telling:
            tittel = max(telling, key=lambda t: (telling[t], -len(t)))
    q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url or "").query))
    fra, til = q.get("year_from", ""), q.get("year_to", "")
    aar = fra if fra == til else "–".join(x for x in (fra, til) if x)
    if not tittel and q.get("q"):
        tittel = q["q"]
    navn = " ".join(x for x in (tittel, aar) if x).strip()
    return navn, slugify(navn)
