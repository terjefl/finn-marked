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


def _dato(verdi) -> str | None:
    """Vegvesenet blander «2023-08-30» og «2023-08-30T16:17:03.444+02:00»; vi viser bare datoen."""
    if not verdi:
        return None
    return str(verdi)[:10]


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
    # Bruktimporterte biler har to datoer: første registrering i utlandet
    # (forstegangsGodkjenning.forstegangRegistrertDato) og første i Norge. Bilens alder
    # er den eldste; den norske vises ved siden av.
    norge = _g(k, "forstegangsregistrering", "registrertForstegangNorgeDato")
    opprinnelig = _g(k, "godkjenning", "forstegangsGodkjenning", "forstegangRegistrertDato")
    datoer = [d for d in (norge, opprinnelig) if d]
    forste = min(datoer) if datoer else None
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
        "forste_registrert_norge": norge if (norge and ((opprinnelig and norge != opprinnelig)
                                             or _g(k, "godkjenning", "forstegangsGodkjenning", "bruktimport"))) else None,
        "registrert_paa_eier": _dato(_g(k, "registrering", "registrertForstegangPaEierskap")),
        "status": _g(k, "registrering", "registreringsstatus", "kodeNavn"),
        "avregistrert": _dato(_g(k, "registrering", "avregistrertSidenDato")),
        "drivstoff": ", ".join(dict.fromkeys(drivstoff)) or None,
        "elektrisk": bool(_g(td, "motorOgDrivverk", "utelukkendeElektriskDrift", default=False)),
        "hybrid": bool(_g(td, "motorOgDrivverk", "hybridElektriskKjoretoy", default=False)),
        "effekt_kw": max(effekter) if effekter else None,
        "rekkevidde_km": max(rekkevidder) if rekkevidder else None,
        "farge": ", ".join(f for f in farger if f) or None,
        "egenvekt": _g(td, "vekter", "egenvekt"),
        "tillatt_totalvekt": _g(td, "vekter", "tillattTotalvekt"),
        "tilhengervekt": _g(td, "vekter", "tillattTilhengervektMedBrems"),
        "klasse": _g(gen, "tekniskUnderkode", "kodeNavn") or _g(gen, "tekniskKode", "kodeNavn"),
        "klassekode": _g(gen, "tekniskKode", "kodeVerdi"),
        "drivhjul": _g(td, "akslinger", "forbindelseMellomDrivaksler", "kodeNavn"),
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
    ut["modellaar"] = modellaar_fra_vin(ut["understellsnummer"], ut["aar"])
    ut["beskrivelse"] = beskrivelse(ut)
    return ut


_VIN_AAR = "ABCDEFGHJKLMNPRSTVWXY123456789"   # posisjon 10: A=2010 … Y=2030, 1=2031 …


def modellaar_fra_vin(vin: str | None, reg_aar: int | None = None) -> int | None:
    """Modellår fra posisjon 10 i understellsnummeret, slik Volkswagen, BMW, Tesla og de
    fleste andre koder det. Ikke alle produsenter gjør det (EU krever det ikke), så verdien
    godtas bare når den ligger innenfor ett år fra registreringsåret."""
    v = (vin or "").strip().upper()
    if len(v) < 10 or v[9] not in _VIN_AAR:
        return None
    i = _VIN_AAR.index(v[9])
    aar = 2010 + i if i < 21 else 2031 + (i - 21)
    if reg_aar and abs(aar - reg_aar) > 1:
        return None
    return aar


def finn_vertikal(kj: dict) -> str:
    """Hvilken FINN-vertikal kjøretøyet hører hjemme i: bobil, campingvogn, MC eller bil.
    Vegvesenets underkode sier «Campingbil»; tekniske koder L* er MC/moped, O* tilhenger."""
    klasse = (kj.get("klasse") or "").lower()
    kode = (kj.get("klassekode") or "").upper()
    if "campingbil" in klasse or "bobil" in klasse:
        return "mobilehome"
    if kode.startswith("O") or "campingvogn" in klasse or "campingtilhenger" in klasse:
        return "caravan"
    if kode.startswith("L"):
        return "mc"
    return "car"


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
        # Ord for ord: «MODEL 3» → «Model 3», «CADDY» → «Caddy», men «ID.4», «EV6», «I4» og «XC40»
        # beholdes (ord med tall eller på tre tegn eller færre).
        ord_ = [o.title() if (o.isupper() and o.isalpha() and len(o) > 3) else o for o in modell.split()]
        deler.append(" ".join(ord_))
    # Typebetegnelsen er ofte en intern typekode («2KN», «F3K», «5G») — den sier ingen noe.
    # Ta den bare med når den ser ut som et navn («M50 xDrive», «Long Range»).
    er_kode = bool(re.fullmatch(r"[0-9A-Z]{2,5}", typ)) and any(c.isdigit() for c in typ)
    if typ and not er_kode and typ.lower() not in modell.lower() and len(typ) <= 30:
        deler.append(typ)
    # Modellåret (fra VIN) er det folk kaller bilen; registreringsåret er reserven.
    aar = kj.get("modellaar") or kj.get("aar")
    if aar:
        deler.append(str(aar))
    return " ".join(d for d in deler if d).strip()


def hk(kw) -> int | None:
    return round(kw * 1.36) if kw else None
