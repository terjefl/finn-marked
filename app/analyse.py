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


REF_KM = 60_000          # kilometerstanden prisindeksen regnes ved
PRIS_GULV = 10_000       # kr: under dette er det aldri en bilpris
PRIS_FAKTOR = 0.1        # … og under 10 % av medianen er det en månedspris, en feiltasting e.l.


def urimelige(annonser: list[dict]) -> set:
    """Kilde-id-er for annonser med urimelig lav pris: under 10 000 kr, eller under
    10 % av medianen av utropsprisene. Leasingannonser med månedspris (1 857 kr) og
    feiltastinger trekker ellers median, kurve og min ned."""
    priser = sorted(a["pris"] for a in annonser if a.get("pris") and er_utropspris(a))
    grense = PRIS_GULV
    if len(priser) >= 5:
        grense = max(grense, statistics.median(priser) * PRIS_FAKTOR)
    return {a.get("kilde_id") for a in annonser if a.get("pris") and er_utropspris(a) and a["pris"] < grense}


def brukbare(annonser: list[dict]) -> list[dict]:
    """Annonsene uten de urimelige — det statistikken og kurvene regner på."""
    ut = urimelige(annonser)
    return [a for a in annonser if a.get("kilde_id") not in ut] if ut else annonser


def oppsummer(annonser: list[dict]) -> dict:
    ute = urimelige(annonser)
    priser = sorted(a["pris"] for a in annonser if a.get("pris") and er_utropspris(a) and a.get("kilde_id") not in ute)
    kms = sorted(a["km"] for a in annonser if a.get("km"))
    ut = {"antall": len(annonser), "antall_pris": len(priser), "antall_urimelig": len(ute),
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
    annonser = brukbare(annonser)
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
    for a in brukbare(annonser):
        if a.get("pris") and a.get("km") is not None and er_utropspris(a):
            nokkel = a["km"] // steg * steg
            botter.setdefault(nokkel, []).append(a["pris"])
    return [{"fra": k, "til": k + steg, "antall": len(v), "median": statistics.median(v)}
            for k, v in sorted(botter.items())]


def nabolag(annonser: list[dict], km: int, vindu: int = 10000) -> dict | None:
    """De sammenlignbare annonsene rundt en gitt kilometerstand — tallet brukeren
    faktisk forholder seg til, i motsetning til regresjonslinja."""
    n = [a for a in brukbare(annonser) if a.get("km") is not None and a.get("pris")
         and er_utropspris(a) and abs(a["km"] - km) <= vindu]
    if not n:
        return None
    priser = sorted(a["pris"] for a in n)
    return {"vindu": vindu, "antall": len(n), "median": statistics.median(priser),
            "min": priser[0], "max": priser[-1],
            "annonser": sorted(n, key=lambda a: a["pris"])}


def historikk(con, slug: str, dager: int | None = None) -> list[dict]:
    """Én rad per henting; hele historikken når `dager` er None (det er poenget med basen)."""
    sql = ("SELECT dato, aktive, median, p25, p75, min, max, treff, indeks, ref_km, r2, nye, solgte "
           "FROM kjøringer WHERE slug=?")
    args: tuple = (slug,)
    if dager:
        sql += " AND dato >= datetime('now','localtime', ?)"
        args = (slug, f"-{dager} days")
    return [dict(r) for r in con.execute(sql + " ORDER BY dato", args)]


def maanedsoversikt(con, slug: str) -> list[dict]:
    """Per måned: nye, solgte, borte, snitt til salgs, median og indeks ved månedsslutt.
    Salgstakt mot tilbud er det som sier om det er kjøpers eller selgers marked."""
    rader: dict[str, dict] = {}

    def rad(m: str) -> dict:
        return rader.setdefault(m, {"maaned": m, "nye": 0, "solgte": 0, "borte": 0, "tilbud": None,
                                    "median": None, "indeks": None})
    for r in con.execute("SELECT substr(COALESCE(lagt_ut, forst_sett),1,7) m, COUNT(*) c FROM annonser "
                         "WHERE slug=? GROUP BY m", (slug,)):
        if r["m"]:
            rad(r["m"])["nye"] = r["c"]
    for r in con.execute("SELECT substr(solgt_dato,1,7) m, COUNT(*) c FROM annonser WHERE slug=? AND solgt=1 "
                         "AND solgt_dato IS NOT NULL GROUP BY m", (slug,)):
        rad(r["m"])["solgte"] = r["c"]
    for r in con.execute("SELECT substr(borte_dato,1,7) m, COUNT(*) c FROM annonser WHERE slug=? AND solgt=0 "
                         "AND borte_dato IS NOT NULL GROUP BY m", (slug,)):
        rad(r["m"])["borte"] = r["c"]
    for r in con.execute("SELECT substr(dato,1,7) m, AVG(aktive) a, AVG(median) md FROM kjøringer WHERE slug=? "
                         "GROUP BY m", (slug,)):
        x = rad(r["m"])
        x["tilbud"] = round(r["a"]) if r["a"] is not None else None
        x["median"] = round(r["md"]) if r["md"] is not None else None
    for r in con.execute("SELECT substr(dato,1,7) m, indeks FROM kjøringer WHERE slug=? AND indeks IS NOT NULL "
                         "ORDER BY dato", (slug,)):
        rad(r["m"])["indeks"] = r["indeks"]           # siste i måneden vinner
    ut = [rader[m] for m in sorted(rader)]
    for x in ut:
        x["omsetning"] = round(x["solgte"] / x["tilbud"] * 100) if x["tilbud"] else None   # % av tilbudet solgt
    return ut


def svg_kurve(annonser: list[dict], egen_km: int | None = None,
              bredde: int = 720, hoyde: int = 380, m: int = 58) -> str:
    """Pris mot kilometerstand som inline-SVG. Fargelegger kategori hvis satt."""
    par = [(a["km"], a["pris"], a) for a in brukbare(annonser)
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
                 f'stroke="var(--kant)" stroke-width="1"/>'
                 f'<text x="{m-8}" y="{sy(yv)+4:.0f}" text-anchor="end" '
                 f'fill="var(--dempet)" font-size="11">{kr(int(yv))}</text>')
    for i in range(5):
        xv = x0 + (x1 - x0) * i / 4
        grid += (f'<text x="{sx(xv):.0f}" y="{hoyde-m+18}" text-anchor="middle" '
                 f'fill="var(--dempet)" font-size="11">{xv/1000:.0f}k km</text>')
    linje = ""
    if v:
        linje = (f'<line x1="{sx(x0):.0f}" y1="{sy(v["a"]+v["b"]*x0):.0f}" x2="{sx(x1):.0f}" '
                 f'y2="{sy(v["a"]+v["b"]*x1):.0f}" stroke="var(--dempet)" stroke-width="1.5" '
                 f'stroke-dasharray="6 4"/>')
    punkter = "".join(
        f'<a href="{_esc(str(a.get("url") or ""), quote=True)}" target="_blank">'
        f'<circle cx="{sx(a["km"]):.0f}" cy="{sy(a["pris"]):.0f}" r="5" fill="var(--aksent)" '
        f'opacity=".75"><title>{_esc(_t(a))} — {kr(a["pris"])}, {km_str(a["km"])}</title>'
        f'</circle></a>' for _, _, a in par)
    return (f'<svg viewBox="0 0 {bredde} {hoyde}" style="width:100%;height:auto">{grid}{linje}{punkter}'
            f'<text x="{bredde/2}" y="{hoyde-6}" text-anchor="middle" fill="var(--dempet)" '
            f'font-size="12">Kilometerstand</text>'
            f'<text x="12" y="18" fill="var(--dempet)" font-size="12">Utropspris</text></svg>')


def svg_historikk(kjoringer: list[dict], bredde: int = 720, hoyde: int = 300, m: int = 58) -> str:
    """Prisutvikling over hele historikken: indeks (pris ved REF_KM), median, kvartilbånd,
    og antall til salgs som flate nederst. Tidsaksen er ekte datoer, ikke kjøringsnummer."""
    from datetime import date as _date
    pts = []
    for k in kjoringer:
        try:
            d = _date.fromisoformat(str(k["dato"])[:10])
        except (TypeError, ValueError):
            continue
        pts.append((d, k))
    if len(pts) < 2:
        return '<p class="dempet">For få hentinger ennå — grafen tegnes fra andre kjøring.</p>'
    d0, d1 = pts[0][0], pts[-1][0]
    span = max((d1 - d0).days, 1)
    verdier = [v for _, k in pts for v in (k.get("median"), k.get("p25"), k.get("p75"), k.get("indeks")) if v]
    if not verdier:
        return '<p class="dempet">Ingen priser i historikken ennå.</p>'
    y0, y1 = min(verdier) * 0.96, max(verdier) * 1.04
    topp, bunn = 28, 70                     # plass til tittel øverst og tilbudsflate nederst
    sx = lambda d: m + (d - d0).days / span * (bredde - 2 * m)                       # noqa: E731
    sy = lambda y: hoyde - bunn - (y - y0) / (y1 - y0) * (hoyde - bunn - topp)       # noqa: E731
    grid = ""
    for i in range(4):
        yv = y0 + (y1 - y0) * i / 3
        grid += (f'<line x1="{m}" y1="{sy(yv):.0f}" x2="{bredde-m}" y2="{sy(yv):.0f}" stroke="var(--kant)"/>'
                 f'<text x="{m-8}" y="{sy(yv)+4:.0f}" text-anchor="end" fill="var(--dempet)" font-size="11">'
                 f'{_esc(kr(int(yv)))}</text>')
    # månedsmerker
    mnd = ""
    d = _date(d0.year, d0.month, 1)
    while d <= d1:
        if d >= d0:
            x = sx(d)
            mnd += (f'<line x1="{x:.0f}" y1="{topp}" x2="{x:.0f}" y2="{hoyde-bunn}" stroke="var(--kant)" '
                    f'stroke-dasharray="2 4"/><text x="{x+3:.0f}" y="{hoyde-bunn+14}" fill="var(--dempet)" '
                    f'font-size="10">{d.strftime("%b %y")}</text>')
        d = _date(d.year + (d.month == 12), d.month % 12 + 1, 1)
    # kvartilbånd
    baand = [(dd, k["p25"], k["p75"]) for dd, k in pts if k.get("p25") and k.get("p75")]
    band = ""
    if len(baand) >= 2:
        opp = " ".join(f"{sx(dd):.0f},{sy(p75):.0f}" for dd, _, p75 in baand)
        ned = " ".join(f"{sx(dd):.0f},{sy(p25):.0f}" for dd, p25, _ in reversed(baand))
        band = f'<polygon points="{opp} {ned}" fill="var(--aksent)" opacity=".10"/>'
    # tilbud (antall til salgs) som flate nederst, egen skala
    tilb = [(dd, k["aktive"]) for dd, k in pts if k.get("aktive") is not None]
    tilbud = ""
    if len(tilb) >= 2:
        mx = max(a for _, a in tilb) or 1
        sa = lambda a: hoyde - 24 - a / mx * (bunn - 40)                                 # noqa: E731
        flate = " ".join(f"{sx(dd):.0f},{sa(a):.0f}" for dd, a in tilb)
        tilbud = (f'<polygon points="{sx(tilb[0][0]):.0f},{hoyde-24} {flate} {sx(tilb[-1][0]):.0f},{hoyde-24}" '
                  f'fill="var(--dempet)" opacity=".18"/>'
                  f'<text x="{bredde-m}" y="{hoyde-28}" text-anchor="end" fill="var(--dempet)" font-size="10">'
                  f'til salgs: {tilb[-1][1]} (maks {mx})</text>')

    def linje(felt: str, farge: str, bredde_px: str, dash: str = "") -> str:
        ser = [(dd, k[felt]) for dd, k in pts if k.get(felt)]
        if len(ser) < 2:
            return ""
        pt = " ".join(f"{sx(dd):.0f},{sy(v):.0f}" for dd, v in ser)
        prikker = "".join(f'<circle cx="{sx(dd):.0f}" cy="{sy(v):.0f}" r="2.5" fill="{farge}">'
                          f'<title>{_esc(str(dd))} — {felt}: {_esc(kr(v))}</title></circle>' for dd, v in ser)
        return (f'<polyline points="{pt}" fill="none" stroke="{farge}" stroke-width="{bredde_px}"'
                f'{f" stroke-dasharray={chr(34)}{dash}{chr(34)}" if dash else ""}/>' + prikker)
    median = linje("median", "var(--dempet)", "1.5", "5 3")
    indeks = linje("indeks", "var(--aksent)", "2.5")
    ref = next((k.get("ref_km") for _, k in reversed(pts) if k.get("ref_km")), None)
    tittel = (f'<text x="12" y="18" fill="var(--tekst)" font-size="12" font-weight="600">Prisutvikling</text>'
              f'<text x="118" y="18" fill="var(--aksent)" font-size="11">— indeks{f" (pris ved {kr(ref)} km)" if ref else ""}</text>'
              f'<text x="{300 if ref else 190}" y="18" fill="var(--dempet)" font-size="11">- - median · bånd = 25–75 %</text>')
    return (f'<svg viewBox="0 0 {bredde} {hoyde}" style="width:100%;height:auto">{grid}{mnd}{band}{tilbud}'
            f'{median}{indeks}{tittel}</svg>')


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


def liggetid(solgte: list[dict]) -> dict | None:
    """Dager fra annonsen ble lagt ut (FINN sin dato; ellers første gang vi så den) til
    den ble registrert solgt. Median og antall, eller None uten data."""
    from datetime import date as _date
    dager = []
    for a in solgte:
        fra, til = a.get("lagt_ut") or a.get("forst_sett"), a.get("solgt_dato")
        try:
            d = (_date.fromisoformat(til[:10]) - _date.fromisoformat(fra[:10])).days
        except (TypeError, ValueError):
            continue
        if d >= 0:
            dager.append(d)
    if not dager:
        return None
    dager.sort()
    return {"antall": len(dager), "median": statistics.median(dager), "min": dager[0], "max": dager[-1],
            "fra_finn": sum(1 for a in solgte if a.get("lagt_ut"))}


def dager_mellom(fra: str | None, til: str | None) -> int | None:
    from datetime import date as _date
    try:
        return (_date.fromisoformat(til[:10]) - _date.fromisoformat(fra[:10])).days
    except (TypeError, ValueError):
        return None


def kjoperliste(annonser: list[dict], kurve: dict | None, kriterier: dict | None,
                i_dag: str | None = None) -> list[dict]:
    """Kjøperens liste: annonser som oppfyller kriteriene, sortert etter hvor mye de ligger
    under regresjonslinja (negativt avvik = billigere enn forventet for kilometerstanden).

    kriterier: {maks_pris, maks_km, aar_fra, aar_til, kategorier: [visningsnavn …]}.
    Hver rad får avvik (kr), avvik_pct og ligget (dager siden utlagt / først sett)."""
    from datetime import date as _date
    k = kriterier or {}
    i_dag = i_dag or _date.today().isoformat()
    ut = []
    for a in brukbare(annonser):
        if not a.get("pris") or not er_utropspris(a) or a.get("solgt"):
            continue
        if k.get("maks_pris") and a["pris"] > k["maks_pris"]:
            continue
        if k.get("maks_km") and (a.get("km") is None or a["km"] > k["maks_km"]):
            continue
        if k.get("aar_fra") and (not a.get("aar") or a["aar"] < k["aar_fra"]):
            continue
        if k.get("aar_til") and (not a.get("aar") or a["aar"] > k["aar_til"]):
            continue
        tagger = {t for t in (a.get("tagger") or "").split(",") if t}
        if k.get("kategorier") and not set(k["kategorier"]).issubset(tagger):
            continue
        r = dict(a)
        if kurve and a.get("km") is not None:
            forventet = kurve["a"] + kurve["b"] * a["km"]
            r["forventet"] = round(forventet)
            r["avvik"] = round(a["pris"] - forventet)
            r["avvik_pct"] = round((a["pris"] - forventet) / forventet * 100, 1) if forventet else None
        else:
            r["forventet"] = r["avvik"] = r["avvik_pct"] = None
        r["ligget"] = dager_mellom(a.get("lagt_ut") or a.get("forst_sett"), i_dag)
        ut.append(r)
    ut.sort(key=lambda r: (r["avvik"] if r["avvik"] is not None else 10**9, r["pris"]))
    return ut


def indeks_trend(kjoringer: list[dict], dager: int = 30) -> dict | None:
    """Indeksen nå mot for `dager` siden (nærmeste kjøring før): {naa, foer, diff, pct, dager}."""
    from datetime import date as _date
    from datetime import timedelta
    med = [k for k in kjoringer if k.get("indeks")]
    if len(med) < 2:
        return None
    naa = med[-1]
    try:
        grense = _date.fromisoformat(str(naa["dato"])[:10]) - timedelta(days=dager)
    except ValueError:
        return None
    foer = next((k for k in reversed(med[:-1]) if _date.fromisoformat(str(k["dato"])[:10]) <= grense), None)
    if not foer:
        foer = med[0]
    try:
        faktiske_dager = (_date.fromisoformat(str(naa["dato"])[:10]) - _date.fromisoformat(str(foer["dato"])[:10])).days
    except ValueError:
        faktiske_dager = dager
    if faktiske_dager <= 0:
        return None
    diff = naa["indeks"] - foer["indeks"]
    return {"naa": naa["indeks"], "foer": foer["indeks"], "diff": diff,
            "pct": round(diff / foer["indeks"] * 100, 1) if foer["indeks"] else None, "dager": faktiske_dager}
