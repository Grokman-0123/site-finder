"""Tyre shops, garages and car washes from SCB's free company register (Företagsregistret).

The register lists every active workplace in Sweden with its industry code (SNI 2025),
name, address and coordinates, so it catches the many businesses OpenStreetMap lacks.
API docs: https://apiafr.scb.se/scalar/  (free key: https://registreraafr.scb.se/)

The key is read from the SCB_API_KEY environment variable. In a Claude cloud session the
network proxy adds it instead, so any placeholder works there.

Workplace lists filtered by industry code carry no coordinates, so each workplace's full
record is fetched once and its position kept in a cache file; later runs only look up
workplaces they have not seen before.
"""
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

from pyproj import Transformer

API = os.environ.get("SCB_API_BASE", "https://apiafr.scb.se/v1/")
TYRE = "95313"       # Däckservice
REPAIR = "95311"     # Allmän service och reparation av motorfordon (incl. washing)
WASH_WORDS = ("tvätt", "tvatt", "bilvård", "bilvard", "carwash", "car wash", "wash", "rekond")
TYRE_WORDS = ("däck", "dack", "tyre", "tire", "vianor", "euromaster", "hjul")
MIN_SIZE = 2         # employee size class: 1 = no employees, 2 = 1-4, ... (0 = unknown)
GAP = 0.21           # SCB allows 5 requests per second per key

_last = 0.0


def get(path, **params):
    global _last
    url = API + path + ("?" + urllib.parse.urlencode(params) if params else "")
    key = os.environ.get("SCB_API_KEY", "set-by-proxy")
    for attempt in range(6):
        wait = _last + GAP - time.time()
        if wait > 0:
            time.sleep(wait)
        _last = time.time()
        try:
            req = urllib.request.Request(url, headers={"X-API-Key": key, "Accept": "application/json",
                                                       "User-Agent": "site-finder-data/1.0"})
            with urllib.request.urlopen(req, timeout=120) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                raise SystemExit(f"SCB refused the API key ({e.code}). Check the SCB_API_KEY secret.")
            if e.code == 404:
                return None
            print(f"SCB {e.code} on {path}; retrying", file=sys.stderr)
        except Exception as e:
            print(f"SCB {e} on {path}; retrying", file=sys.stderr)
        time.sleep(5 * (attempt + 1))
    raise SystemExit(f"SCB API kept failing on {path}")


def codes():
    """Industry codes to fetch: tyre service, car repair, and any code that mentions tyres."""
    table = get("kodtabeller/naringsgrenkoder") or []
    found = {TYRE: "t", REPAIR: "r"}
    for row in table:
        vals = list(row.values())
        code = next((str(v) for v in vals if isinstance(v, str) and v.isdigit() and len(v) == 5), None)
        text = " ".join(str(v) for v in vals if isinstance(v, str) and not v.isdigit()).lower()
        if code and "däck" in text and code not in found:
            found[code] = "t"
        if code and ("motorfordon" in text or "däck" in text):
            print(f"  SNI {code}: {text}", file=sys.stderr)
    return found


def listing(code):
    out, cursor = [], None
    while True:
        params = {"limit": 5000}
        if cursor is not None:
            params["cursorId"] = cursor
        d = get(f"arbetsstallen/naringsgren/{code}", **params) or {}
        out += d.get("arbetsstallen") or []
        page = d.get("pagination") or {}
        if not page.get("hasMore"):
            return out
        cursor = page.get("nextCursorId")


def kind_for(base, name):
    n = name.lower()
    if base == "r" and any(w in n for w in WASH_WORDS):
        return "w"
    if base == "r" and any(w in n for w in TYRE_WORDS):
        return "tr"
    return base


def as_int(x, default=0):
    try:
        return int(x)
    except (TypeError, ValueError):
        return default


def fetch(cache_path):
    """Rows [lat, lon, kinds, name, address] for active workplaces."""
    try:
        with open(cache_path, encoding="utf-8") as f:
            cache = json.load(f)
    except (OSError, ValueError):
        cache = {}
    to_wgs = Transformer.from_crs("EPSG:3006", "EPSG:4326", always_xy=True).transform
    picked = {}
    stats = {}
    for code, base in codes().items():
        rows = listing(code)
        stats[code] = [len(rows), 0]
        for ae in rows:
            if as_int(ae.get("aeStat")) != 1:
                continue
            size = as_int(ae.get("anstKl"))
            if size < MIN_SIZE:
                continue
            cfar = str(ae["cfarNr"])
            name = (ae.get("ben") or "").strip()
            k = kind_for(base, name)
            prev = picked.get(cfar)
            if prev:
                prev["k"] = "".join(sorted(set(prev["k"] + k), key="trw".index))
                continue
            b = ae.get("belagenhetsadress") or {}
            addr = ", ".join(x for x in ((b.get("bGatuAdress") or "").strip().title(),
                                          (b.get("bPostOrt") or "").strip().title()) if x)
            picked[cfar] = {"k": k, "name": name, "addr": addr}
            stats[code][1] += 1
    new = [c for c in picked if c not in cache]
    print(f"SCB: {len(picked)} active workplaces with staff; looking up {len(new)} new positions", file=sys.stderr)
    for i, cfar in enumerate(new):
        full = get(f"arbetsstallen/{cfar}/full") or {}
        g = full.get("geografiskInformation") or {}
        n = as_int(g.get("nordKoordinatAdressSW")) or as_int(g.get("nordKoordinatSW"))
        e = as_int(g.get("ostKoordinatAdressSW")) or as_int(g.get("ostKoordinatSW"))
        if n and e:
            lon, lat = to_wgs(e, n)
            cache[cfar] = [round(lat, 5), round(lon, 5)]
        else:
            cache[cfar] = None
        if i % 500 == 499:
            print(f"  {i + 1}/{len(new)}", file=sys.stderr)
            save(cache_path, cache)
    # forget workplaces that are no longer listed, so the cache does not grow forever
    cache = {c: v for c, v in cache.items() if c in picked}
    save(cache_path, cache)
    out = []
    for cfar, p in picked.items():
        pos = cache.get(cfar)
        if pos:
            out.append([pos[0], pos[1], p["k"], p["name"], p["addr"]])
    for code, (listed, kept) in stats.items():
        print(f"  SNI {code}: {listed} workplaces listed, {kept} active with staff", file=sys.stderr)
    return out


def save(path, cache):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cache, f, separators=(",", ":"))
