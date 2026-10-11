"""Henting og parsing av FINN mobilitet-søk.

Bygger på samme mønster som ~/.hermes/scripts/finn_bil_marked.py (se skillen
`finn-bil-marked` for fallgruvene): SSR-HTML med inntil 50 kort per side,
paginering virker, «Solgt»-badge i kortet, pris skjules på solgte annonser.
"""

from __future__ import annotations

import html as htmllib
import re
import time
import urllib.error
import urllib.request
from datetime import date
from functools import lru_cache

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/126.0 Safari/537.36")
SLEEP = 1.0


def hent(url: str, timeout: int = 40, forsok: int = 3) -> str:
    """Henter en side og prøver på nytt ved nettverksfeil, 5xx og 429.

    403/404 prøves ikke på nytt: en permanent avvisning blir ikke bedre av å gjentas,
    og de gamle forsøkene gjorde bare at hver skannedag brukte tre ganger så lang tid
    på sider som uansett ikke finnes.
    """
    siste: Exception | None = None
    for n in range(forsok):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": UA,
                "Accept-Language": "nb-NO,nb;q=0.9",
                "Accept": "text/html,application/xhtml+xml",
            })
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            siste = e
            if e.code == 429:                     # «for mange forespørsler» – vent lenger
                time.sleep(10 * (n + 1))
            elif e.code >= 500:
                time.sleep(2 * (n + 1))
            else:
                raise RuntimeError(f"henting avvist for {url}: HTTP {e.code} {e.reason}") from e
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            siste = e
            time.sleep(2 * (n + 1))
    raise RuntimeError(f"henting feilet for {url}: {siste}")


def _tekst(s: str | None) -> str | None:
    if not s:
        return None
    t = htmllib.unescape(re.sub(r"<[^>]+>", " ", s))
    t = re.sub(r"\s+", " ", t).strip()
    return t or None


def _tall(s: str | None) -> int | None:
    if not s:
        return None
    d = re.sub(r"[^\d]", "", s.replace("\xa0", " "))
    return int(d) if d else None


def parse_kort(art: str) -> dict | None:
    m = re.search(r'href="https://www\.finn\.no/mobility/item/(\d+)"', art)
    if not m:
        return None
    finn_id = m.group(1)
    h2 = re.search(r"<h2[^>]*>(.*?)</h2>", art, re.S)
    sub = re.search(r'<div class="text-caption mb-4 s-text-subtle[^"]*">(.*?)</div>', art, re.S)
    meta = re.search(r'<span class="text-caption font-bold inline-block mb-8">(.*?)</span>', art, re.S)
    meta_t = _tekst(meta.group(1)) if meta else ""
    a = {
        "kilde": "finn",
        "kilde_id": finn_id,
        "url": f"https://www.finn.no/mobility/item/{finn_id}",
        "tittel": _tekst(h2.group(1)) if h2 else None,
        "undertittel": _tekst(sub.group(1)) if sub else None,
    }
    a["aar"] = _tall(re.search(r"\b(19|20)\d{2}\b", meta_t).group(0)) if re.search(r"\b(19|20)\d{2}\b", meta_t) else None
    km = re.search(r"([\d\s\xa0]+)\s*km", meta_t)
    a["km"] = _tall(km.group(1)) if km else None
    rek = re.search(r"([\d\s\xa0]+)\s*km\s*rekkevidde", meta_t)
    a["rekkevidde"] = _tall(rek.group(1)) if rek else None
    a["drivstoff"] = "El" if re.search(r"\bEl\b", meta_t) else None
    pris = re.search(r'<span class="t3 font-bold">([\d\s\xa0]+)</span>', art)
    a["pris"] = _tall(pris.group(1)) if pris else None
    # FINN markerer solgte annonser med badge og skjuler prisen deres
    badge = re.search(r'class="[^"]*badge-warning-background[^"]*"[^>]*>([^<]+)<', art)
    a["merke"] = _tekst(badge.group(1)) if badge else None
    a["solgt"] = bool(a["merke"] and re.search(r"solgt|reservert", a["merke"], re.I))
    # Tåler at HTML-en er pent formatert med linjeskift/mellomrom mellom taggene —
    # FINN leverer den kompakt i dag, men det skal ikke være nok til å brekke parsingen.
    blk = re.search(r'<div class="flex items-end text-detail min-w-0">\s*'
                    r'<div class="flex-1 s-text-subtle min-w-0">\s*'
                    r'<span class="truncate block">(.*?)</span>\s*'
                    r'<span class="truncate block">(.*?)</span>', art, re.S)
    a["sted"] = a["selger"] = a["selger_type"] = None
    if blk:
        seg1 = _tekst(blk.group(1)) or ""
        seg2 = _tekst(blk.group(2)) or ""
        # Kulen FINN bruker er U+2219 (∙), men varianter forekommer — del på alle.
        deler = [d.strip(" –-") for d in re.split(r"[∙·•]", seg1) if d.strip(" –-")]
        a["sted"] = deler[0] if deler else None
        navn = deler[1:]
        if not navn and seg2:
            hode = re.split(r"\s[–-]\s", seg2.split("∙")[0])[0].strip(" –-")
            if hode and not re.match(r"^(privat|forhandler|merkeforhandler)$", hode, re.I):
                navn = [hode]
        a["selger"] = ", ".join(navn)[:80] if navn else None
        all_txt = f"{seg1} {seg2}"
        a["selger_type"] = ("Merkeforhandler" if re.search(r"\bMerkeforhandler\b", all_txt, re.I)
                            else "Forhandler" if re.search(r"\bForhandler\b", all_txt, re.I)
                            else "Privat" if re.search(r"\bPrivat\b", all_txt, re.I) else None)
    a["pris_type"] = "solgt" if a["solgt"] else ("utropspris" if a["pris"] else "ukjent")
    a["ekstra"] = {"finn_merke": a["merke"]}
    return a


@lru_cache(maxsize=None)
def _ordmonster(tekst: str) -> re.Pattern[str]:
    """Kategorimønster: nøkkelen/visningsnavnet som egne ord.

    «Fully Charged» → /fully[\\s._-]*charged/ (skilletegn mellom ordene tillatt),
    «m50» → /m50/ med ordgrense. Bygges fra vaktens egne kategorier, så nye merker
    og modeller kan følges uten kodeendring.
    """
    deler = [re.escape(del_) for del_ in re.split(r"[^\w]+", (tekst or "").lower()) if del_]
    if not deler:
        return re.compile(r"(?!)")               # matcher ingenting
    return re.compile(r"(?<!\w)" + r"[\s._-]*".join(deler) + r"(?!\w)")


def kategoriser(ann: dict, kategorier: dict, sok_tagger: list[str]) -> str:
    """Fritekst-treff i tittel/undertittel + treff fra q=-søket.

    Kategoriene kommer fra vaktens eget skjema (`nokkel = visningsnavn`): en kategori
    merkes når nøkkelen eller visningsnavnet står som eget ord i annonsekortet.
    q=-søket treffer også beskrivelsen, så en annonse funnet der er en bekreftet
    treff selv om ordet ikke står i kortet.
    """
    tekst = f"{ann.get('tittel') or ''} {ann.get('undertittel') or ''}".lower()
    treff: list[str] = []
    for nokkel, navn in (kategorier or {}).items():
        navn = navn or nokkel
        if navn in treff:
            continue
        if _ordmonster(nokkel).search(tekst) or _ordmonster(navn).search(tekst):
            treff.append(navn)
    for tag in sok_tagger:
        if tag in kategorier and kategorier[tag] not in treff:
            treff.append(kategorier[tag])
    return ",".join(treff)


def les_side(html: str) -> tuple[dict[str, dict], int, int]:
    """Parser én resultatside: (kort per id, treff siden oppgir, antall sider)."""
    tekst = re.sub(r"<[^>]+>", " ", html)
    m = re.search(r"([\d][\d\s\xa0]{0,7})\s*treff", tekst)
    treff = (_tall(m.group(1)) if m else 0) or 0
    sider = int((re.findall(r'pages="(\d+)"', html) or ["1"])[0])
    ann: dict[str, dict] = {}
    for art in re.findall(r"<article.*?</article>", html, re.S):
        k = parse_kort(art)
        if k:
            ann[k["kilde_id"]] = k
    return ann, treff, sider


def forhandsvis(url: str, antall: int = 3) -> dict:
    """Bare første side — til «Ny vakt»-skjemaet, så brukeren ser at URL-en virker
    før noe lagres. Returnerer treff, sider, noen eksempler og alle titlene (til navneforslag)."""
    ann, treff, sider = les_side(hent(url))
    kort = list(ann.values())
    return {"treff": treff, "sider": sider, "kort_pa_siden": len(kort),
            "eksempler": [{k: a.get(k) for k in ("tittel", "undertittel", "pris", "km", "aar", "url")}
                          for a in kort[:antall]],
            "titler": [a.get("tittel") for a in kort]}


def hent_sok(url: str, logg=lambda m: None) -> tuple[list[dict], int, int]:
    """Henter alle sider. Returnerer (annonser, treff_fra_siden, antall_sider)."""
    ann, treff, sider = les_side(hent(url))
    logg(f"side 1/{sider}: {len(ann)} kort (siden oppgir {treff} treff)")
    for p in range(2, sider + 1):
        time.sleep(SLEEP)
        sep = "&" if "?" in url else "?"
        h = hent(re.sub(r"([?&])page=\d+", r"\1", url) + f"{sep}page={p}")
        nye = 0
        for art in re.findall(r"<article.*?</article>", h, re.S):
            k = parse_kort(art)
            if k and k["kilde_id"] not in ann:
                ann[k["kilde_id"]] = k
                nye += 1
        logg(f"side {p}/{sider}: +{nye} nye (totalt {len(ann)})")
    return list(ann.values()), treff, sider


def hent_detaljer(url: str) -> dict:
    """Leser detaljsiden: spesifikasjoner, utstyrsliste og beskrivelse."""
    h = hent(url)
    tekst = htmllib.unescape(re.sub(r"<[^>]+>", "\n", h))
    linjer = [linje.strip() for linje in re.sub(r"\n{2,}", "\n", tekst).splitlines() if linje.strip()]

    def etter(etikett: str, vindu: int = 6):
        for i, linje in enumerate(linjer):
            if linje.lower() == etikett.lower():
                for j in range(i + 1, min(i + vindu, len(linjer))):
                    if linjer[j] not in (":", ""):
                        return linjer[j]
        return None

    utstyr = ""
    if "Utstyr" in linjer:
        i = linjer.index("Utstyr")
        slutt = next((j for j in range(i + 1, len(linjer))
                      if linjer[j] in ("Vis hele beskrivelsen", "Beskrivelse", "Om bilen")), i + 120)
        utstyr = ", ".join(linjer[i + 1:slutt])[:4000]
    beskrivelse = ""
    for anker in ("Beskrivelse av varen", "Om bilen"):
        if anker in linjer:
            i = linjer.index(anker)
            beskrivelse = re.sub(r"\s+", " ", " ".join(linjer[i + 1:i + 300]))[:3000]
            break
    felter = {k: etter(k) for k in ("Modellår", "Kilometerstand", "Batterikapasitet", "Rekkevidde (WLTP)",
                                    "1. gang registrert", "Eiere", "Garanti", "Salgsform", "Farge")}
    return {"felter": felter, "utstyr": utstyr, "beskrivelse": beskrivelse}


def i_dag() -> str:
    return date.today().isoformat()


# ------------------------------------------------------------------ adapter

class FinnKilde:
    """FINN mobilitet. Søket er {"url": "<søke-URL>", "tag": "<kortnavn>"}."""

    navn = "finn"

    def hent(self, sok: dict, logg=lambda m: None) -> tuple[list[dict], int]:
        annonser, treff, _sider = hent_sok(sok["url"], logg)
        for a in annonser:
            a.setdefault("ekstra", {})
            a["ekstra"]["sok_tag"] = sok.get("tag")
        return annonser, treff


from . import registrer  # noqa: E402  (sirkulær import er trygg her: pakken er lastet)

registrer(FinnKilde())
