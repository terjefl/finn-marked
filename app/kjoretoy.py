"""Kjøretøyoppslag hos Statens vegvesen (Autosys enkeltoppslag).

Åpent API for privatpersoner og virksomheter: GET /enkeltoppslag/kjoretoydata med
`kjennemerke=` og nøkkelen i headeren `SVV-Authorization: Apikey <nøkkel>`. Grense
50 000 kall per nøkkel per døgn; 422 betyr at kvoten er brukt opp (prøv etter midnatt).

Svaret er stort (hele den tekniske godkjenningen). Vi plukker ut det en bileier
faktisk vil se i «Egen bil»: merke, modell, årsmodell, første registrering, drivstoff,
effekt, rekkevidde, EU-kontroll — og lagrer det som en liten, flat dict på vakten.
Registreringsnummer er en personopplysning hos Vegvesenet; vi slår bare opp det
brukeren selv taster inn på sin egen vakt.

Nøkkelen leses fra `FINN_SVV_NOKKEL`.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request

BASE = "https://akfell-datautlevering.atlas.vegvesen.no"
UA = "finn-marked (kjoretoyoppslag; terjefl/finn-marked)"


class Oppslagsfeil(RuntimeError):
    """Feil brukeren skal se i skjemaet (ugyldig regnr, ingen nøkkel, kvote brukt opp …)."""


def nokkel() -> str | None:
    return (os.environ.get("FINN_SVV_NOKKEL") or "").strip() or None


def rens_regnr(regnr: str | None) -> str:
    """«eb 12345» → «EB12345». Godtar 2–7 tegn (vanlige, veteran- og personlige skilt)."""
    r = re.sub(r"[\s\-]", "", (regnr or "").upper())
    if not re.fullmatch(r"[A-ZÆØÅ0-9]{2,7}", r):
        raise Oppslagsfeil("Registreringsnummeret ser feil ut (f.eks. EB12345).")
    return r


def _hent(url: str, timeout: int = 20) -> dict:
    req = urllib.request.Request(url, headers={
        "SVV-Authorization": f"Apikey {nokkel()}", "Accept": "application/json", "User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise Oppslagsfeil("Vegvesenet avviste API-nøkkelen (FINN_SVV_NOKKEL).") from e
        if e.code == 422:
            raise Oppslagsfeil("Dagskvoten hos Vegvesenet er brukt opp — prøv igjen etter midnatt.") from e
        raise Oppslagsfeil(f"Vegvesenet svarte {e.code}.") from e
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as e:
        raise Oppslagsfeil(f"Fikk ikke kontakt med Vegvesenet: {e}") from e


def slaa_opp(regnr: str) -> dict:
    """Slår opp kjennemerket og returnerer den flate kjøretøy-dicten (se `normaliser`)."""
    if not nokkel():
        raise Oppslagsfeil("Ingen API-nøkkel til Vegvesenet er satt (FINN_SVV_NOKKEL).")
    r = rens_regnr(regnr)
    data = _hent(f"{BASE}/enkeltoppslag/kjoretoydata?kjennemerke={urllib.parse.quote(r)}")
    return normaliser(data, r)


def _g(d, *sti, default=None):
    """Trygg sti-oppslag: _g(d, "a", 0, "b") → d["a"][0]["b"] eller default."""
    for s in sti:
        try:
            d = d[s]
        except (KeyError, IndexError, TypeError):
            return default
    return default if d is None else d


def normaliser(data: dict, regnr: str | None = None) -> dict:
    if not isinstance(data, dict):
        raise Oppslagsfeil("Uventet svar fra Vegvesenet.")
    if data.get("feilmelding") and not data.get("kjoretoydataListe"):
        raise Oppslagsfeil(f"Vegvesenet: {data['feilmelding']}")
    k = _g(data, "kjoretoydataListe", 0)
    if not k:
        raise Oppslagsfeil("Fant ikke noe kjøretøy med det registreringsnummeret.")
    td = _g(k, "godkjenning", "tekniskGodkjenning", "tekniskeData", default={})
    gen = td.get("generelt") or {}
    forste = _g(k, "forstegangsregistrering", "registrertForstegangNorgeDato") \
        or _g(k, "godkjenning", "forstegangsGodkjenning", "forstegangRegistrertDato")
    grupper = _g(td, "miljodata", "miljoOgdrivstoffGruppe", default=[]) or []
    drivstoff = [_g(gr, "drivstoffKodeMiljodata", "kodeNavn") for gr in grupper]
    drivstoff = [d for d in drivstoff if d]
    rekkevidder = [_g(f, "rekkeviddeKm") for gr in grupper for f in (gr.get("forbrukOgUtslipp") or [])]
    rekkevidder = [x for x in rekkevidder if isinstance(x, int | float) and x > 0]
    motorer = _g(td, "motorOgDrivverk", "motor", default=[]) or []
    effekter = [_g(dr, "maksNettoEffekt") for m in motorer for dr in (m.get("drivstoff") or [])]
    effekter = [e for e in effekter if isinstance(e, int | float) and e > 0]
    farger = [_g(f, "kodeNavn") for f in (_g(td, "karosseriOgLasteplan", "rFarge", default=[]) or [])]
    ut = {
        "regnr": _g(k, "kjoretoyId", "kjennemerke") or regnr,
        "understellsnummer": _g(k, "kjoretoyId", "understellsnummer"),
        "merke": _g(gen, "merke", 0, "merke"),
        "modell": _g(gen, "handelsbetegnelse", 0),
        "typebetegnelse": gen.get("typebetegnelse"),
        "aar": int(forste[:4]) if forste and forste[:4].isdigit() else None,
        "forste_registrert": forste,
        "registrert_paa_eier": _g(k, "registrering", "registrertForstegangPaEierskap"),
        "status": _g(k, "registrering", "registreringsstatus", "kodeNavn"),
        "avregistrert": _g(k, "registrering", "avregistrertSidenDato"),
        "drivstoff": ", ".join(dict.fromkeys(drivstoff)) or None,
        "elektrisk": bool(_g(td, "motorOgDrivverk", "utelukkendeElektriskDrift", default=False)),
        "hybrid": bool(_g(td, "motorOgDrivverk", "hybridElektriskKjoretoy", default=False)),
        "effekt_kw": max(effekter) if effekter else None,
        "rekkevidde_km": max(rekkevidder) if rekkevidder else None,
        "farge": ", ".join(f for f in farger if f) or None,
        "egenvekt": _g(td, "vekter", "egenvekt"),
        "tillatt_totalvekt": _g(td, "vekter", "tillattTotalvekt"),
        "tilhengervekt": _g(td, "vekter", "tillattTilhengervektMedBrems"),
        "seter": _g(td, "persontall", "sitteplasserTotalt"),
        "girkasse": _g(td, "motorOgDrivverk", "girkassetype", "kodeNavn"),
        "karosseri": _g(td, "karosseriOgLasteplan", "karosseritype", "kodeNavn"),
        "euroklasse": _g(td, "miljodata", "euroKlasse", "kodeNavn"),
        "slagvolum": _g(td, "motorOgDrivverk", "motor", 0, "slagvolum"),
        "lengde": _g(td, "dimensjoner", "lengde"),
        "bredde": _g(td, "dimensjoner", "bredde"),
        "dekk": _g(td, "dekkOgFelg", "akselDekkOgFelgKombinasjon", 0, "akselDekkOgFelg", 0, "dekkdimensjon"),
        "eu_frist": _g(k, "periodiskKjoretoyKontroll", "kontrollfrist"),
        "eu_sist": _g(k, "periodiskKjoretoyKontroll", "sistGodkjent"),
        "importert_km": _g(k, "godkjenning", "forstegangsGodkjenning", "bruktimport", "kilometerstand"),
        "importland": _g(k, "godkjenning", "forstegangsGodkjenning", "bruktimport", "importland", "landNavn"),
    }
    ut["beskrivelse"] = beskrivelse(ut)
    return ut


def beskrivelse(kj: dict) -> str:
    """«BMW i4 M50 xDrive 2022» — det som foreslås i feltet «Egen bil».

    Vegvesenets modellnavn er ofte bare «I4», så typebetegnelsen tas med når den
    sier noe mer. Effekt i hk (kW × 1,36) er det folk kjenner igjen."""
    merke = (kj.get("merke") or "").strip()
    modell = (kj.get("modell") or "").strip()
    typ = (kj.get("typebetegnelse") or "").strip()
    # Vegvesenet skriver alt med store bokstaver: VOLKSWAGEN → Volkswagen, men BMW/KIA/MG beholdes.
    deler = [merke.title() if (merke.isupper() and len(merke) > 4) else merke]
    if modell:
        # «MODEL Y» → «Model Y», men «ID.4», «EV6» og «I4» beholdes som de er.
        pen = modell.title() if (modell.isupper() and len(modell) > 3 and not any(c.isdigit() for c in modell)) else modell
        deler.append(pen)
    if typ and typ.lower() not in modell.lower() and len(typ) <= 30:
        deler.append(typ)
    if kj.get("aar"):
        deler.append(str(kj["aar"]))
    return " ".join(d for d in deler if d).strip()


def hk(kw) -> int | None:
    return round(kw * 1.36) if kw else None
