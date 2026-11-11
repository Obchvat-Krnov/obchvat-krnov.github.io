#!/usr/bin/env python3
"""Aktualizace dat o nehodách na obchvatu Krnova (silnice I/57).

Stáhne nehody z aplikace Dopravní nehody v ČR (nehody.cdv.cz, data Policie ČR),
vybere ty, které leží na trase obchvatu, a uloží je do data.json, ze kterého čte web.
Spouští se automaticky přes GitHub Actions (.github/workflows/update.yml).
Používá jen standardní knihovnu Pythonu.
"""
import html
import json
import math
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

BASE = "https://nehody.cdv.cz"
AREA_CODE = "597520"            # Krnov - obec
AREA_NAME = "Krnov - obec"
DATE_FROM = "2021-09-01"        # od zkušebního provozu obchvatu
MAX_DIST_M = 60                 # nehoda max. 60 m od osy obchvatu
ROADS = {"57", "45"}            # silnice I/57 a křižovatka s I/45
UA = "obchvat-krnov.github.io data updater (monthly; contact via GitHub)"

COLS = ["id", "lat", "lng", "silnice", "datum", "druh", "pricina", "usmrceno", "tezce", "lehce",
        "skoda", "vozidel", "alkohol", "krizovatka", "smer", "noc", "mokro", "zver", "prekazka", "vozidla"]
DICT_KEYS = ["druh", "pricina", "smer", "zver", "prekazka"]


def http(url, data=None, tries=4):
    body = urllib.parse.urlencode(data).encode() if data is not None else None
    headers = {"User-Agent": UA, "X-Requested-With": "XMLHttpRequest"}
    if body is not None:
        headers["Content-Type"] = "application/x-www-form-urlencoded; charset=UTF-8"
    for i in range(tries):
        try:
            req = urllib.request.Request(url, data=body, headers=headers)
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.read().decode("utf-8", "replace")
        except Exception as e:  # noqa: BLE001
            if i == tries - 1:
                raise
            print(f"  opakuji ({e})")
            time.sleep(5 * (i + 1))


def data_asof():
    """Datum poslední aktualizace dat na nehody.cdv.cz, např. '31. 10. 2026'."""
    page = http(BASE + "/")
    m = re.search(r"Poslední aktualizace[^0-9]*(\d{1,2})\.\s*(\d{1,2})\.\s*(\d{4})", page)
    if not m:
        raise RuntimeError("Na nehody.cdv.cz nebylo nalezeno datum poslední aktualizace.")
    d, mth, y = (int(x) for x in m.groups())
    return f"{d}. {mth}. {y}", f"{y:04d}-{mth:02d}-{d:02d}"


def load_points(date_to):
    """Všechny nehody v Krnově s GPS (id, lat, lng)."""
    form = [
        ("span", "day"), ("dateFrom", DATE_FROM), ("dateTo", date_to), ("types[]", "nehody"),
        ("area[code]", AREA_CODE), ("area[name]", AREA_NAME),
        ("extent[northeast][lat]", "50.14"), ("extent[northeast][lng]", "17.76"),
        ("extent[southwest][lat]", "50.07"), ("extent[southwest][lng]", "17.62"),
        ("zoom", "15"), ("layers[accidentType]", "accidents-all"),
        ("layers[accidentDetail][]", "accidents-injury-death"),
        ("layers[accidentDetail][]", "accidents-injury-heavy"),
        ("layers[accidentDetail][]", "accidents-injury-light"),
        ("layers[accidentDetail][]", "accidents-injury-no"),
    ]
    res = json.loads(http(BASE + "/handlers/loadMap.php", form))
    if res.get("uspech") != 1 or not isinstance(res.get("data"), list):
        raise RuntimeError(f"Neočekávaná odpověď loadMap: {str(res)[:300]}")
    return [(int(d["p1"]), float(d["lat"]), float(d["lng"])) for d in res["data"]]


def dist_to_route(lat, lng, route):
    k = math.cos(math.radians(50.1))
    px, py = lng * k, lat
    best = 1e9
    for line in route:
        for (a_lat, a_lng), (b_lat, b_lng) in zip(line, line[1:]):
            ax, ay, bx, by = a_lng * k, a_lat, b_lng * k, b_lat
            dx, dy = bx - ax, by - ay
            t = ((px - ax) * dx + (py - ay) * dy) / ((dx * dx + dy * dy) or 1)
            t = max(0.0, min(1.0, t))
            best = min(best, math.hypot(px - (ax + t * dx), py - (ay + t * dy)) * 111320)
    return best


def clean(s):
    s = re.sub(r"<[^>]+>", " ", s)
    return re.sub(r"\s+", " ", html.unescape(s)).strip()


def detail(p1):
    page = http(f"{BASE}/detail.php?p1={p1}")
    pairs = re.findall(r'<td class="caption">(.*?)</td>\s*<td[^>]*>(.*?)</td>', page, re.S)
    f, vehicles = {}, []
    for k, v in pairs:
        k, v = clean(k), clean(v)
        if k == "Druh vozidla":
            vehicles.append(v)
        elif k not in f:
            f[k] = v
    if "Datum" not in f:
        raise RuntimeError(f"Detail nehody {p1} nemá očekávaný formát.")
    return f, vehicles


def to_row(p1, lat, lng, f, vehicles, dicts):
    def ix(key, val):
        lst = dicts[key]
        if val not in lst:
            lst.append(val)
        return lst.index(val)

    def num(k):
        m = re.search(r"-?\d+", f.get(k, "0").replace(" ", ""))
        return int(m.group()) if m else 0

    m = re.search(r"(\d+)\.(\d+)\.(\d+).*?,\s*([\d?]+):([\d?]+)", f["Datum"])
    datum = f"{m.group(3)}-{int(m.group(2)):02d}-{int(m.group(1)):02d} {m.group(4).zfill(2)}:{m.group(5)}"
    zver = f.get("Druh zvěře/zvířete", "")
    return [
        p1, lat, lng, f.get("Číslo komunikace", ""), datum,
        ix("druh", f.get("Druh nehody", "")), ix("pricina", f.get("Hlavní příčina nehody", "")),
        num("Usmrceno osob"), num("Těžce zraněno osob"), num("Lehce zraněno osob"),
        num("Celková hmotná škoda (Kč)"), num("Počet zúčastněných vozidel"),
        1 if f.get("Přítomnost alkoholu u viníka nehody", "").startswith("ano") else 0,
        0 if f.get("Místo dopravní nehody", "").startswith("mimo") else 1,
        ix("smer", f.get("Směrové poměry", "")),
        1 if f.get("Viditelnost", "").startswith("v noci") else 0,
        0 if f.get("Stav povrchu vozovky v době nehody", "").startswith("povrch suchý") else 1,
        ix("zver", zver), ix("prekazka", f.get("Druh pevné překážky", "")),
        "; ".join(v.split(" (")[0] for v in vehicles),
    ]


# ---------- Zprávy v médiích (Google News RSS) ----------
NEWS_PLACE = re.compile(r"krnov|obchvat", re.I)
NEWS_ACC = re.compile(r"nehod|srážk|střet|havár|zemřel|nepřežil|motork|naboural|srazil|tragéd|tragick|vrtulník|zraněn", re.I)
NEWS_RECHECK_DAYS = 21   # u čerstvých nehod hledat znovu (články vycházejí se zpožděním)


def news_for(day):
    """Články o nehodě v Krnově vydané v den nehody nebo do 2 dnů po ní."""
    d0 = datetime.strptime(day, "%Y-%m-%d").date()
    after = (d0 - timedelta(days=1)).isoformat()
    before = (d0 + timedelta(days=4)).isoformat()
    last = (d0 + timedelta(days=2)).isoformat()
    q = f"Krnov (nehoda OR havárie OR srážka OR nehodě OR motorkář OR řidič) after:{after} before:{before}"
    url = "https://news.google.com/rss/search?" + urllib.parse.urlencode({"q": q, "hl": "cs", "gl": "CZ", "ceid": "CZ:cs"})
    xml = http(url)
    out = []
    for item in re.findall(r"<item>(.*?)</item>", xml, re.S):
        def g(tag):
            m = re.search(rf"<{tag}[^>]*>(.*?)</{tag}>", item, re.S)
            return html.unescape(m.group(1).strip()) if m else ""
        full, src, link = g("title"), g("source"), g("link")
        title = full[: -(len(src) + 3)] if src and full.endswith(" - " + src) else full
        try:
            pub = parsedate_to_datetime(g("pubDate")).date().isoformat()
        except Exception:  # noqa: BLE001
            continue
        if day <= pub <= last and NEWS_PLACE.search(title) and NEWS_ACC.search(title):
            out.append({"t": title, "s": src, "u": link, "d": pub})
    return out[:6]


def update_news(rows, old):
    articles = dict(old.get("articles", {}))
    checked = dict(old.get("news_checked", {}))
    today = datetime.now(timezone.utc).date()
    for r in rows:
        rid, day = str(r[0]), r[4][:10]
        injured = r[7] + r[8] + r[9] > 0
        if not injured:
            continue
        age = (today - datetime.strptime(day, "%Y-%m-%d").date()).days
        if rid in checked and age > NEWS_RECHECK_DAYS:
            continue
        try:
            found = news_for(day)
        except Exception as e:  # noqa: BLE001
            print(f"  zprávy k {rid} se nepodařilo načíst: {e}")
            continue
        if found:
            articles[rid] = found
        checked[rid] = today.isoformat()
        time.sleep(1)
    ids = {str(r[0]) for r in rows}
    return ({k: v for k, v in articles.items() if k in ids}, {k: v for k, v in checked.items() if k in ids})


def main():
    with open("bypass.json", encoding="utf-8") as fh:
        route = json.load(fh)
    try:
        with open("data.json", encoding="utf-8") as fh:
            old = json.load(fh)
    except FileNotFoundError:
        old = {}
    dicts = {k: list(old.get("dict", {}).get(k, [])) for k in DICT_KEYS}
    known = {r[0]: r for r in old.get("rows", [])}
    excluded = set(old.get("excluded", []))

    asof, asof_iso = data_asof()
    print(f"Data CDV k: {asof}")
    pts = load_points(asof_iso)
    print(f"Nehod v Krnově s GPS: {len(pts)}")
    near = [(p1, lat, lng) for p1, lat, lng in pts if dist_to_route(lat, lng, route) <= MAX_DIST_M]
    print(f"Nehod u trasy obchvatu: {len(near)}")

    rows, new = [], 0
    for p1, lat, lng in near:
        if p1 in known:
            rows.append(known[p1]); continue
        if p1 in excluded:
            continue
        f, vehicles = detail(p1)
        time.sleep(0.5)  # šetrně k serveru CDV
        if f.get("Číslo komunikace", "") not in ROADS:
            excluded.add(p1); continue
        rows.append(to_row(p1, lat, lng, f, vehicles, dicts)); new += 1

    rows.sort(key=lambda r: r[4])
    articles, checked = update_news(rows, old)
    print(f"Nehod s nalezenými články: {len(articles)}")
    out = {
        "asof": asof, "asof_iso": asof_iso,
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": "Policie ČR / CDV, nehody.cdv.cz",
        "dict": dicts, "cols": COLS, "rows": rows, "excluded": sorted(excluded),
        "articles": articles, "news_checked": checked,
    }
    same = lambda d: {k: v for k, v in d.items() if k not in ("generated", "news_checked")}
    if old and same(old) == same(out):
        print("Žádná nová data, beze změny.")
        return
    with open("data.json", "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, separators=(",", ":"))
    print(f"Uloženo: {len(rows)} nehod (nových {new}).")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # noqa: BLE001
        print(f"CHYBA: {e}", file=sys.stderr)
        sys.exit(1)
