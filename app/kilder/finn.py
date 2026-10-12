"""Henting og parsing av FINN mobilitet-søk.

Bygger på samme mønster som ~/.hermes/scripts/finn_bil_marked.py (se skillen
`finn-bil-marked` for fallgruvene): SSR-HTML med inntil 50 kort per side,
paginering virker, «Solgt»-badge i kortet, pris skjules på solgte annonser.
"""

from __future__ import annotations

import html as htmllib
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date
from functools import lru_cache

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/126.0 Safari/537.36")
SLEEP = 1.0
#: FINNs JSON-API bak søkesiden: samme parametre som søkesiden, strukturerte annonser,
#: og filteret «variant» (bil) / «make» (de andre) = merke- og modelltreet.
API_SOK_BASE = "https://www.finn.no/mobility/search/api/search/"
API_SOK = API_SOK_BASE + "SEARCH_ID_CAR_USED"

#: FINN har én søkevertikal per kjøretøytype, med egen søkenøkkel, egen side og eget
#: merketre. Bil har merke → modell (variant=1.<merke>.<modell>); de andre bare merke (make=).
#: Sidene for bobil/campingvogn/MC rendres i nettleseren, så der finnes bare API-et.
#: Traktor ligger under «Nyttekjøretøy og maskiner» (b2b) og oppgir arbeidstimer, ikke
#: kilometer — `akse` sier hvilken måleenhet analysen skal regne verdifall mot (se analyse.AKSER).
VERTIKALER: dict[str, dict] = {
    "car": {"key": "SEARCH_ID_CAR_USED", "sti": "/mobility/search/car", "tre": "variant", "navn": "bil",
            "akse": "km"},
    "mobilehome": {"key": "SEARCH_ID_CAR_MOBILE_HOME", "sti": "/mobility/search/caravan/mobilehome",
                   "tre": "make", "navn": "bobil", "akse": "km"},
    "caravan": {"key": "SEARCH_ID_CAR_CARAVAN", "sti": "/mobility/search/caravan", "tre": "make",
                "navn": "campingvogn", "akse": "km"},
    "mc": {"key": "SEARCH_ID_MC_USED", "sti": "/mobility/search/mc", "tre": "make", "navn": "MC", "akse": "km"},
    "tractor": {"key": "SEARCH_ID_AGRICULTURE_TRACTOR", "sti": "/mobility/search/b2b/agriculturetractor",
                "tre": "make", "navn": "traktor", "akse": "timer"},
}


def vertikal_av_url(url: str) -> str:
    sti = urllib.parse.urlsplit(url or "").path.rstrip("/")
    if sti.endswith("/mobilehome"):
        return "mobilehome"
    if sti.endswith("/caravan"):
        return "caravan"
    if sti.endswith("/mc"):
        return "mc"
    if sti.endswith("/agriculturetractor"):
        return "tractor"
    return "car"


def vakt_vertikal(v: dict) -> str:
    """Vertikalen til vaktens første FINN-søk; «car» når vakten ikke har noe FINN-søk.
    Vertikalen lagres ikke — den følger søke-URL-en, så den kan ikke komme i utakt."""
    for s in v.get("sok") or []:
        url = (s.get("url") or "") if isinstance(s, dict) else ""
        if "finn.no" in url:
            return vertikal_av_url(url)
    return "car"


def vakt_akse(v: dict) -> str:
    """«km» eller «timer»: hva annonsene i vakten måler slitasje i."""
    return VERTIKALER[vakt_vertikal(v)]["akse"]


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
    if kilde_modus() == "html":
        ann, treff, sider = les_side(hent(url))
    else:
        try:
            ann, treff, sider = les_api_side(hent(api_url(url, 1)))
        except (RuntimeError, ValueError):
            ann, treff, sider = les_side(hent(url))
    kort = list(ann.values())
    return {"treff": treff, "sider": sider, "kort_pa_siden": len(kort),
            "eksempler": [{k: a.get(k) for k in ("tittel", "undertittel", "pris", "km", "timer", "aar", "url")}
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
    return parse_detaljer(hent(url))


def parse_detaljer(h: str) -> dict:
    """Detaljsidens HTML → {felter, utstyr, beskrivelse}. Bil-sider har «Om bilen» og
    «Kilometerstand»; traktor-sider «Beskrivelse», «Arbeidstimer», «Effekt» og «Vekt».
    Etiketter som mangler gir None, så samme parser dekker begge."""
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
    # «Beskrivelse» sist: bil-sider har gjerne både «Om bilen» og en «Beskrivelse»-knapp.
    for anker in ("Beskrivelse av varen", "Om bilen", "Beskrivelse"):
        if anker in linjer:
            i = linjer.index(anker)
            beskrivelse = re.sub(r"\s+", " ", " ".join(linjer[i + 1:i + 300]))[:3000]
            break
    felter = {k: etter(k) for k in ("Modellår", "Kilometerstand", "Batterikapasitet", "Rekkevidde (WLTP)",
                                    "1. gang registrert", "Eiere", "Garanti", "Salgsform", "Farge",
                                    "Arbeidstimer", "Effekt", "Vekt")}
    return {"felter": felter, "utstyr": utstyr, "beskrivelse": beskrivelse}


def i_dag() -> str:
    return date.today().isoformat()


# ------------------------------------------------------------------ adapter

# ------------------------------------------------------------------ JSON-API (primær kilde)

def api_url(sokeurl: str, side: int = 1) -> str:
    """Søkesidens URL → API-URL med samme parametre. Søkesiden kaller selv dette
    API-et, så filtrene (variant, year_from, q, fuel …) er de samme. `page` byttes,
    og søkenøkkelen følger vertikalen i stien (bil, bobil, campingvogn, MC, traktor)."""
    deler = urllib.parse.urlsplit(sokeurl)
    q = [(k, v) for k, v in urllib.parse.parse_qsl(deler.query, keep_blank_values=False) if k != "page"]
    if side > 1:
        q.append(("page", str(side)))
    key = VERTIKALER[vertikal_av_url(sokeurl)]["key"]
    return API_SOK_BASE + key + ("?" + urllib.parse.urlencode(q) if q else "")


def _dato_av_ms(ms) -> str | None:
    try:
        return date.fromtimestamp(int(ms) / 1000).isoformat() if ms else None
    except (TypeError, ValueError, OverflowError):
        return None


def fra_api_doc(d: dict) -> dict | None:
    """Ett JSON-dokument fra søke-API-et → felles annonseformat (se kilder/__init__)."""
    finn_id = str(d.get("id") or d.get("ad_id") or "").strip()
    if not finn_id or d.get("type") not in (None, "motor"):
        return None
    flagg = [str(f) for f in (d.get("flags") or [])]
    merker = [str(x.get("id") if isinstance(x, dict) else x) for x in (d.get("labels") or [])]
    solgt = "sold" in flagg or "sold" in merker
    pris = (d.get("price") or {}).get("amount")
    pris = int(pris) if isinstance(pris, int | float) and pris > 0 else None
    km = d.get("mileage")
    km = int(km) if isinstance(km, int | float) and km >= 0 else None
    timer = d.get("hours_used")                      # traktor: arbeidstimer i stedet for km
    timer = int(timer) if isinstance(timer, int | float) and timer >= 0 else None
    aar = d.get("year")                              # traktor-annonser bruker 0/1900 for «ukjent»
    aar = int(aar) if isinstance(aar, int | float) and aar > 1900 else None
    reg = d.get("registration_class")
    reg = reg.get("value") if isinstance(reg, dict) else reg
    return {
        "kilde": "finn",
        "kilde_id": finn_id,
        "url": d.get("canonical_url") or f"https://www.finn.no/mobility/item/{finn_id}",
        "tittel": (d.get("heading") or "").strip() or None,
        "undertittel": (d.get("model_specification") or "").strip() or None,
        "aar": aar,
        "km": km,
        "timer": timer,
        "drivstoff": d.get("fuel") or None,
        "rekkevidde": int(d["driving_range"]) if isinstance(d.get("driving_range"), int | float) else None,
        "pris": pris,
        "pris_type": "solgt" if solgt else ("utropspris" if pris else "ukjent"),
        "sted": d.get("location") or None,
        "selger": (d.get("organisation_name") or "")[:80] or None,
        "selger_type": d.get("dealer_segment") or None,
        "merke": "Solgt" if solgt else None,
        "solgt": solgt,
        "ekstra": {
            "regnr": (d.get("regno") or "").replace(" ", "").upper() or None,
            "vin": (d.get("chassis_number") or "").strip().upper() or None,
            "lagt_ut": _dato_av_ms(d.get("timestamp")),
            "merke": d.get("make"), "modell": d.get("model"),
            "girkasse": d.get("transmission"), "garanti_mnd": d.get("warranty_duration"),
            "salgsform": d.get("sales_form"), "klasse": reg, "flagg": flagg,
        },
    }


def les_api_side(tekst: str) -> tuple[dict[str, dict], int, int]:
    """Parser ett API-svar: (annonser per id, treff, antall sider)."""
    data = json.loads(tekst)
    if not isinstance(data, dict) or "docs" not in data:
        raise RuntimeError("uventet svar fra FINN-API-et (ingen docs)")
    meta = data.get("metadata") or {}
    treff = int(((meta.get("result_size") or {}).get("match_count")) or 0)
    sider = int(((meta.get("paging") or {}).get("last")) or 1)
    ann: dict[str, dict] = {}
    for d in data.get("docs") or []:
        a = fra_api_doc(d)
        if a:
            ann[a["kilde_id"]] = a
    return ann, treff, sider


def hent_sok_api(url: str, logg=lambda m: None) -> tuple[list[dict], int, int]:
    """Som hent_sok, men mot JSON-API-et. Returnerer (annonser, treff, antall_sider)."""
    ann, treff, sider = les_api_side(hent(api_url(url, 1)))
    logg(f"side 1/{sider}: {len(ann)} annonser (API-et oppgir {treff} treff)")
    for p in range(2, sider + 1):
        time.sleep(SLEEP)
        flere, _, _ = les_api_side(hent(api_url(url, p)))
        nye = 0
        for k, a in flere.items():
            if k not in ann:
                ann[k] = a
                nye += 1
        logg(f"side {p}/{sider}: +{nye} nye (totalt {len(ann)})")
    return list(ann.values()), treff, sider


def kilde_modus() -> str:
    """«api» (standard) eller «html» (FINN_KILDE=html) — reserven hvis API-et endres."""
    return (os.environ.get("FINN_KILDE") or "api").strip().lower()


def hent_sok_valgt(url: str, logg=lambda m: None) -> tuple[list[dict], int, int]:
    """API først; faller tilbake til HTML-henteren hvis API-et svarer uventet."""
    if kilde_modus() == "html" and vertikal_av_url(url) == "car":
        return hent_sok(url, logg)
    try:
        return hent_sok_api(url, logg)
    except (RuntimeError, ValueError) as e:
        if vertikal_av_url(url) != "car":
            raise                                  # ingen HTML-reserve for bobil/campingvogn/MC/traktor
        logg(f"FINN-API-et feilet ({e}); prøver HTML-siden")
        return hent_sok(url, logg)


class FinnKilde:
    """FINN mobilitet. Søket er {"url": "<søke-URL>", "tag": "<kortnavn>"}."""

    navn = "finn"

    def hent(self, sok: dict, logg=lambda m: None) -> tuple[list[dict], int]:
        annonser, treff, _sider = hent_sok_valgt(sok["url"], logg)
        for a in annonser:
            a.setdefault("ekstra", {})
            a["ekstra"]["sok_tag"] = sok.get("tag")
        return annonser, treff


from . import registrer  # noqa: E402  (sirkulær import er trygg her: pakken er lastet)

registrer(FinnKilde())


# ------------------------------------------------------------------ merke/modell-koder

#: FINNs JSON-API bak søkesiden. Gir blant annet filteret «variant» = hele treet av
#: merker (0.<kode>) og modeller (1.<merke>.<kode>) med visningsnavn.
_merketre: dict[str, dict] = {}
MERKETRE_LEVETID = 24 * 3600


def _normaliser_navn(s: str | None) -> str:
    """Små bokstaver uten aksenter og skilletegn: «Bürstner» og «BURSTNER» → «burstner»."""
    import unicodedata
    t = unicodedata.normalize("NFKD", (s or "").lower())
    t = "".join(c for c in t if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9æøå]", "", t)


def hent_merketre(tving: bool = False, vertikal: str = "car") -> list[dict]:
    """[{navn, kode, param, modeller: [{navn, kode, param}]}] fra FINN, mellomlagret et døgn
    per vertikal. Bil har modeller under merkene (variant=…), de andre bare merker (make=…)."""
    import json as _json
    v = VERTIKALER[vertikal]
    cache = _merketre.get(vertikal)
    if not tving and cache and time.monotonic() - cache["hentet"] < MERKETRE_LEVETID:
        return cache["merker"]
    data = _json.loads(hent(API_SOK_BASE + v["key"] + "?q="))
    filtre = {f.get("name"): f for f in data.get("filters") or []}
    merker = []
    for m in (filtre.get(v["tre"]) or {}).get("filter_items") or []:
        merker.append({"navn": m.get("display_name"), "kode": m.get("value"), "param": v["tre"],
                       "modeller": [{"navn": x.get("display_name"), "kode": x.get("value"), "param": v["tre"]}
                                    for x in (m.get("filter_items") or [])]})
    if not merker:
        raise RuntimeError(f"FINN svarte uten merkefilter for {v['navn']} — API-et kan ha endret seg")
    _merketre[vertikal] = {"hentet": time.monotonic(), "merker": merker}
    return merker


_undermodeller: dict[str, dict] = {}


def hent_undermodeller(modellkode: str, vertikal: str = "car") -> list[dict]:
    """Tredje nivå i FINNs tre: «X-Serie» → X1, X3, X5 … (variant=2.<merke>.<serie>.<kode>).
    Finnes bare i svaret når serien er valgt, så det er ett ekstra kall, mellomlagret et døgn."""
    import json as _json
    cache = _undermodeller.get(modellkode)
    if cache and time.monotonic() - cache["hentet"] < MERKETRE_LEVETID:
        return cache["liste"]
    v = VERTIKALER[vertikal]
    data = _json.loads(hent(API_SOK_BASE + v["key"] + "?" + urllib.parse.urlencode({v["tre"]: modellkode})))
    liste: list[dict] = []
    for m in ({f.get("name"): f for f in data.get("filters") or []}.get(v["tre"]) or {}).get("filter_items") or []:
        for mo in m.get("filter_items") or []:
            if mo.get("value") == modellkode:
                liste = [{"navn": x.get("display_name"), "kode": x.get("value"), "param": v["tre"]}
                         for x in (mo.get("filter_items") or [])]
    _undermodeller[modellkode] = {"hentet": time.monotonic(), "liste": liste}
    return liste


def _match_navn(nmo: str, modeller: list[dict]) -> dict | None:
    """Beste treff for et normalisert navn i en liste: eksakt → FINN-navn som prefiks →
    -Serie/-Klasse-stamme → korteste utvidelse."""
    eksakt = [x for x in modeller if _normaliser_navn(x["navn"]) == nmo]
    if eksakt:
        return eksakt[0]
    kand = [x for x in modeller if _normaliser_navn(x["navn"]) and nmo.startswith(_normaliser_navn(x["navn"]))]
    if kand:
        return max(kand, key=lambda x: len(x["navn"]))
    stammer = []
    for x in modeller:
        m = re.match(r"^(.+?)[\s-]*(serie|klasse)$", x["navn"], re.I)
        if m:
            stammer.append((_normaliser_navn(m.group(1)), x))
    kand2 = [(st, x) for st, x in stammer if st and nmo.startswith(st)]
    if kand2:
        return max(kand2, key=lambda p: len(p[0]))[1]
    kand = [x for x in modeller if _normaliser_navn(x["navn"]).startswith(nmo)]
    return min(kand, key=lambda x: len(x["navn"])) if kand else None


def finn_merke_og_modell(merke: str | None, modell: str | None, tre: list[dict] | None = None,
                         vertikal: str = "car", undermodeller=None) -> dict:
    """Mapper Vegvesenets merke/modell («BMW», «I4») til FINNs koder.

    Returnerer {merke: {navn,kode}|None, modell: {navn,kode}|None, modeller: [...]} —
    `modeller` er alle FINN-modellene for merket, så skjemaet kan la brukeren velge
    når treffet er usikkert. Modell-match: eksakt normalisert navn, ellers det
    lengste FINN-navnet som er inneholdt i Vegvesenets (eller omvendt).
    """
    eget_tre = tre is not None
    tre = tre if tre is not None else hent_merketre(vertikal=vertikal)
    nm = _normaliser_navn(merke)
    m_treff = None
    if nm:
        for m in tre:
            if _normaliser_navn(m["navn"]) == nm:
                m_treff = m
                break
        if not m_treff:
            kand = [m for m in tre if _normaliser_navn(m["navn"]) and
                    (nm.startswith(_normaliser_navn(m["navn"])) or _normaliser_navn(m["navn"]).startswith(nm))]
            m_treff = max(kand, key=lambda m: len(m["navn"])) if kand else None
    if not m_treff:
        return {"merke": None, "modell": None, "modeller": []}
    modeller = m_treff["modeller"]
    nmo = _normaliser_navn(modell)
    mo_treff = _match_navn(nmo, modeller) if nmo else None
    param = m_treff.get("param") or "variant"
    # Traff vi en serie («X-Serie», «3-Serie», «EQC-Klasse») uten at navnet var eksakt, har FINN
    # ofte et tredje nivå med de enkelte modellene (X5, 320d …). Prøv det, og la skjemaet velge
    # blant undermodellene i stedet for toppnivået.
    # (Med et eget tre og uten undermodell-oppslag, som i testene, hoppes dette over.)
    if mo_treff and nmo and _normaliser_navn(mo_treff["navn"]) != nmo \
            and re.search(r"[\s-]*(serie|klasse)$", mo_treff["navn"], re.I) and (undermodeller or not eget_tre):
        try:
            under = undermodeller(mo_treff["kode"]) if undermodeller else hent_undermodeller(mo_treff["kode"], vertikal)
        except (RuntimeError, ValueError):
            under = []
        if under:
            u_treff = _match_navn(nmo, under)
            modeller = [{"navn": f"Hele {mo_treff['navn']}", "kode": mo_treff["kode"], "param": param}] + under
            if u_treff:
                mo_treff = u_treff
    return {"merke": {"navn": m_treff["navn"], "kode": m_treff["kode"], "param": param},
            "modell": ({"navn": mo_treff["navn"], "kode": mo_treff["kode"], "param": param} if mo_treff else None),
            "modeller": modeller}


def sok_url(kode: str, aar: int | None = None, elektrisk: bool = False, aar_til: int | None = None,
            vertikal: str = "car", param: str | None = None) -> str:
    """FINN-søk for en merke-/modellkode i en vertikal, valgfritt låst til årsmodell
    (eller et spenn) og, for bil, til el."""
    v = VERTIKALER[vertikal]
    url = f"https://www.finn.no{v['sti']}?{param or v['tre']}={kode}"
    if aar:
        url += f"&year_from={aar}&year_to={aar_til or aar}"
    if elektrisk and vertikal == "car":
        url += "&fuel=4"
    return url
