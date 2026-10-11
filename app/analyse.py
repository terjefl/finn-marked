"""Analyse: medianer, km-bøtter, verdikurve og SVG-diagram for web-sidene."""

from __future__ import annotations

import re
import statistics
from html import escape as _esc


def linjereg(x: list[float], y: list[float]) -> tuple[float, float, float]:
    """Minste kvadraters regresjon y = a + b·x. Returnerer (a, b, r²)."""
    n = len(x)
    if n < 5:
        return (0.0, 0.0, 0.0)
    mx, my = sum(x) / n, sum(y) / n
    sxx = sum((xi - mx) ** 2 for xi in x)
    sxy = sum((xi - mx) * (yi - my) for xi, yi in zip(x, y))
    if sxx == 0:
        return (0.0, 0.0, 0.0)
    b = sxy / sxx
    a = my - b * mx
    ss_tot = sum((yi - my) ** 2 for yi in y)
    ss_res = sum((yi - (a + b * xi)) ** 2 for xi, yi in zip(x, y))
    return (a, b, 1 - ss_res / ss_tot if ss_tot else 0.0)


def kr(n) -> str:
    """Tall i norsk stil med mellomrom som tusenskille, «–» når det ikke finnes.

    Brukes både direkte og som Jinja-filter (`| kr` i malene), derfor tåler den
    også Jinja sin «undefined».
    """
    if n is None or n == "":
        return "–"
    try:
        return f"{n:,.0f}".replace(",", " ")
    except (TypeError, ValueError):
        return "–"


def km_str(n) -> str:
    if n is None or n == "":
        return "–"
    try:
        return f"{n:,}".replace(",", " ") + " km"
    except (TypeError, ValueError):
        return "–"


def er_utropspris(a: dict) -> bool:
    """Utropspris/fastpris er «hva selger ber om». Bud på auksjon er ikke det —
    de holdes utenfor median, kurve og km-bøtter, men vises i annonselisten."""
    return (a.get("pris_type") or "utropspris") in ("utropspris", "fastpris")


def oppsummer(annonser: list[dict]) -> dict:
    priser = sorted(a["pris"] for a in annonser if a.get("pris") and er_utropspris(a))
    kms = sorted(a["km"] for a in annonser if a.get("km"))
    ut = {"antall": len(annonser), "antall_pris": len(priser),
          "antall_bud": sum(1 for a in annonser if (a.get("pris_type") == "bud"))}
    if priser:
        ut.update({
            "median": statistics.median(priser),
            "snitt": statistics.mean(priser),
            "min": priser[0], "max": priser[-1],
            "p25": priser[len(priser) // 4], "p75": priser[3 * len(priser) // 4],
        })
    if kms:
        ut["km_median"] = statistics.median(kms)
        ut["km_min"], ut["km_max"] = kms[0], kms[-1]
    return ut


def verdikurve(annonser: list[dict], egen_km: int | None = None) -> dict | None:
    """Kurve på utropspriser. Estimatet er utropspris-nivå, ikke oppnådd pris."""
    par = [(a["km"], a["pris"]) for a in annonser if a.get("km") and a.get("pris") and er_utropspris(a)]
    if len(par) < 5:
        return None
    x = [p[0] for p in par]
    y = [p[1] for p in par]
    a_, b_, r2 = linjereg(x, y)
    resid = [yi - (a_ + b_ * xi) for xi, yi in zip(x, y)]
    return {
        "n": len(par), "a": a_, "b": b_, "r2": r2, "sd": statistics.pstdev(resid),
        "per_1000km": abs(b_) * 1000,
        "estimat": (a_ + b_ * egen_km) if egen_km else None,
        "egen_km": egen_km,
        "serie": sorted(par),
    }


def km_botter(annonser: list[dict], steg: int = 10000) -> list[dict]:
    botter: dict[int, list[int]] = {}
    for a in annonser:
        if a.get("pris") and a.get("km") is not None and er_utropspris(a):
            nokkel = a["km"] // steg * steg
            botter.setdefault(nokkel, []).append(a["pris"])
    return [{"fra": k, "til": k + steg, "antall": len(v), "median": statistics.median(v)}
            for k, v in sorted(botter.items())]


def nabolag(annonser: list[dict], km: int, vindu: int = 10000) -> dict | None:
    """De sammenlignbare annonsene rundt en gitt kilometerstand — tallet brukeren
    faktisk forholder seg til, i motsetning til regresjonslinja."""
    n = [a for a in annonser if a.get("km") is not None and a.get("pris")
         and er_utropspris(a) and abs(a["km"] - km) <= vindu]
    if not n:
        return None
    priser = sorted(a["pris"] for a in n)
    return {"vindu": vindu, "antall": len(n), "median": statistics.median(priser),
            "min": priser[0], "max": priser[-1],
            "annonser": sorted(n, key=lambda a: a["pris"])}


def historikk(con, slug: str, dager: int = 120) -> list[dict]:
    return [dict(r) for r in con.execute(
        "SELECT dato, aktive, median, p25, p75, min, max, treff FROM kjøringer "
        "WHERE slug=? AND dato >= datetime('now','localtime', ?) ORDER BY dato",
        (slug, f"-{dager} days"))]


def svg_kurve(annonser: list[dict], egen_km: int | None = None,
              bredde: int = 720, hoyde: int = 380, m: int = 58) -> str:
    """Pris mot kilometerstand som inline-SVG. Fargelegger kategori hvis satt."""
    par = [(a["km"], a["pris"], a) for a in annonser
           if a.get("km") and a.get("pris") and er_utropspris(a)]
    if not par:
        return "<p>Ingen annonser med både pris og kilometerstand.</p>"
    xs = [p[0] for p in par]
    ys = [p[1] for p in par]
    x0, x1 = min(xs) - 5000, max(xs) + 5000
    y0, y1 = min(ys) - 20000, max(ys) + 20000
    sx = lambda x: m + (x - x0) / (x1 - x0) * (bredde - 2 * m)          # noqa: E731
    sy = lambda y: hoyde - m - (y - y0) / (y1 - y0) * (hoyde - 2 * m)   # noqa: E731
    v = verdikurve(annonser, egen_km)
    grid = ""
    for i in range(5):
        yv = y0 + (y1 - y0) * i / 4
        grid += (f'<line x1="{m}" y1="{sy(yv):.0f}" x2="{bredde-m}" y2="{sy(yv):.0f}" '
                 f'stroke="var(--border)" stroke-width="1"/>'
                 f'<text x="{m-8}" y="{sy(yv)+4:.0f}" text-anchor="end" '
                 f'fill="var(--muted-foreground)" font-size="11">{kr(int(yv))}</text>')
    for i in range(5):
        xv = x0 + (x1 - x0) * i / 4
        grid += (f'<text x="{sx(xv):.0f}" y="{hoyde-m+18}" text-anchor="middle" '
                 f'fill="var(--muted-foreground)" font-size="11">{xv/1000:.0f}k km</text>')
    linje = ""
    if v:
        linje = (f'<line x1="{sx(x0):.0f}" y1="{sy(v["a"]+v["b"]*x0):.0f}" x2="{sx(x1):.0f}" '
                 f'y2="{sy(v["a"]+v["b"]*x1):.0f}" stroke="var(--muted-foreground)" stroke-width="1.5" '
                 f'stroke-dasharray="6 4"/>')
    punkter = "".join(
        f'<a href="{_esc(str(a.get("url") or ""), quote=True)}" target="_blank">'
        f'<circle cx="{sx(a["km"]):.0f}" cy="{sy(a["pris"]):.0f}" r="5" fill="var(--accent)" '
        f'opacity=".75"><title>{_esc(_t(a))} — {kr(a["pris"])}, {km_str(a["km"])}</title>'
        f'</circle></a>' for _, _, a in par)
    return (f'<svg viewBox="0 0 {bredde} {hoyde}" style="width:100%;height:auto">{grid}{linje}{punkter}'
            f'<text x="{bredde/2}" y="{hoyde-6}" text-anchor="middle" fill="var(--muted-foreground)" '
            f'font-size="12">Kilometerstand</text>'
            f'<text x="12" y="18" fill="var(--muted-foreground)" font-size="12">Utropspris</text></svg>')


def svg_historikk(kjoringer: list[dict], bredde: int = 720, hoyde: int = 220, m: int = 58) -> str:
    """Median utropspris per henting som enkel SVG-linje (én prikk per kjøring).

    Tabellen under viser de samme tallene, men en linje gjør utviklingen over
    måneder lesbar uten å bla.
    """
    ser = [(k["dato"], k["median"]) for k in kjoringer if k.get("median")]
    if len(ser) < 2:
        return '<p class="dempet">For få hentinger med median ennå — linja tegnes fra andre kjøring.</p>'
    ys = [p[1] for p in ser]
    y0, y1 = min(ys) * 0.97, max(ys) * 1.03
    if y1 <= y0:
        y1 = y0 + 1
    sx = lambda i: m + i / (len(ser) - 1) * (bredde - 2 * m)          # noqa: E731
    sy = lambda y: hoyde - m - (y - y0) / (y1 - y0) * (hoyde - 2 * m)  # noqa: E731
    grid = ""
    for i in range(3):
        yv = y0 + (y1 - y0) * i / 2
        grid += (f'<line x1="{m}" y1="{sy(yv):.0f}" x2="{bredde-m}" y2="{sy(yv):.0f}" '
                 f'stroke="var(--border)" stroke-width="1"/>'
                 f'<text x="{m-8}" y="{sy(yv)+4:.0f}" text-anchor="end" '
                 f'fill="var(--muted-foreground)" font-size="11">{_esc(kr(int(yv)))}</text>')
    linje = " ".join(f"{sx(i):.0f},{sy(p[1]):.0f}" for i, p in enumerate(ser))
    punkter = "".join(
        f'<circle cx="{sx(i):.0f}" cy="{sy(p[1]):.0f}" r="3.5" fill="var(--accent)" opacity=".8">'
        f'<title>{_esc(str(p[0]))} — {_esc(kr(p[1]))}</title></circle>' for i, p in enumerate(ser))
    return (f'<svg viewBox="0 0 {bredde} {hoyde}" style="width:100%;height:auto">{grid}'
            f'<polyline points="{linje}" fill="none" stroke="var(--accent)" stroke-width="1.5"/>{punkter}'
            f'<text x="{m}" y="{hoyde-6}" fill="var(--muted-foreground)" font-size="11">'
            f'{_esc(str(ser[0][0])[:10])}</text>'
            f'<text x="{bredde-m}" y="{hoyde-6}" text-anchor="end" fill="var(--muted-foreground)" '
            f'font-size="11">{_esc(str(ser[-1][0])[:10])}</text>'
            f'<text x="12" y="18" fill="var(--muted-foreground)" font-size="12">'
            f'Median utropspris per henting</text></svg>')


def _t(a: dict) -> str:
    return f'{a.get("tittel") or ""} {a.get("undertittel") or ""}'.strip()


def kategoriforslag(annonser: list[dict], eksisterende: dict | None = None, maks: int = 12,
                    minst: int = 2) -> list[tuple[str, int]]:
    """Forslag til kategorier fra annonsene selv: fraser i undertitlene som går igjen.

    Undertittelen på FINN er selgerens egen oppsummering («Fully Charged - Laser - H&K»),
    så den splittes på skilletegn og frasene telles på tvers av annonsene. Rene tall,
    fraser som alt er kategorier, og fraser nesten alle annonsene har (modellnavnet)
    filtreres bort. Resultat: [(frase, antall)], flest først.
    """
    har = {str(v).lower() for v in (eksisterende or {}).values()} | {k.lower() for k in (eksisterende or {})}
    telling: dict[str, int] = {}
    visning: dict[str, str] = {}
    for a in annonser:
        sett: set[str] = set()
        for frase in re.split(r"\s*[-–|/,·•∙]\s*|\s{2,}", a.get("undertittel") or ""):
            f = re.sub(r"\s+", " ", frase).strip(" .!")
            n = f.lower()
            if len(n) < 2 or len(n) > 40 or n in har or re.fullmatch(r"[\d\s.,]+(km|kr)?", n):
                continue
            sett.add(n)
            visning.setdefault(n, f)
        for n in sett:
            telling[n] = telling.get(n, 0) + 1
    tak = max(1, int(len(annonser) * 0.9))
    ut = [(visning[n], c) for n, c in telling.items() if minst <= c <= tak]
    ut.sort(key=lambda p: (-p[1], p[0].lower()))
    return ut[:maks]
