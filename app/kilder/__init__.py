"""Kildelaget: én adapter per plattform (FINN, Auksjonen, …).

Alle adaptere normaliserer til samme annonse-dict, slik at lagring, analyse og
UI-et er helt uavhengige av hvor annonsen kommer fra:

    {
      "kilde": "finn",              # adapterens navn
      "kilde_id": "478402697",      # unik id hos kilden
      "url": "https://…",
      "tittel": "BMW i4", "undertittel": "Fully Charged",
      "aar": 2022, "km": 47926, "drivstoff": "El", "rekkevidde": 540,
      "timer": None,                # arbeidstimer (traktor) — der er km None
      "pris": 434532,
      "pris_type": "utropspris",    # utropspris | fastpris | bud | solgt | ukjent
      "sted": "Moss", "selger": "…", "selger_type": "Forhandler",
      "solgt": False,
      "ekstra": {}                  # kildespesifikt (bud, antall bud, sluttdato …)
    }

En vakt (ett marked) har en liste søk; hvert søk navngir sin adapter med `kilde`
(standard «finn» når feltet mangler).
"""

from __future__ import annotations

import importlib
import pkgutil
from pathlib import Path
from typing import Callable, Protocol


class Kilde(Protocol):
    navn: str

    def hent(self, sok: dict, logg: Callable[[str], None]) -> tuple[list[dict], int]:
        """Returnerer (annonser, antall treff kilden selv oppgir)."""
        ...


#: Adaptere registrert ved import. Nøkkel = verdien i søkets `kilde`-felt.
KILDER: dict[str, Kilde] = {}


def registrer(kilde: Kilde) -> Kilde:
    KILDER[kilde.navn] = kilde
    return kilde


def last_alle() -> dict[str, Kilde]:
    """Importerer alle moduler i denne pakken, slik at de registrerer seg selv."""
    pakke = Path(__file__).parent
    for mod in pkgutil.iter_modules([str(pakke)]):
        if mod.name in ("base",):
            continue
        importlib.import_module(f"{__name__}.{mod.name}")
    return KILDER


def hent(sok: dict, logg: Callable[[str], None] = lambda m: None) -> tuple[list[dict], int]:
    """Henter ett søk via riktig adapter."""
    navn = sok.get("kilde") or "finn"
    if not KILDER:
        last_alle()
    if navn not in KILDER:
        raise ValueError(f"ukjent kilde «{navn}» (har: {', '.join(sorted(KILDER))})")
    annonser, treff = KILDER[navn].hent(sok, logg)
    for a in annonser:
        a.setdefault("kilde", navn)
        a.setdefault("pris_type", "utropspris")
        a.setdefault("ekstra", {})
    return annonser, treff
