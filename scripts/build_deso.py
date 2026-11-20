"""Build neighbourhood (DeSO) market data for the Site Finder "Market heat" layer.

DeSO are Statistics Sweden's ~6,160 small statistical areas (700-2,700 residents
each), much finer than the 290 municipalities. For each one this writes:
  c    DeSO code          k    municipality code
  pop  residents (latest year)
  cars passenger cars in traffic owned by residents (latest year)
  inc  mean net income per person, thousand SEK (latest year)
  km2  land area in km²
  lat/lon  a point inside the area
  cars3/pop3  cars and residents within 3 km (areas whose centre is that close)
plus the simplified boundary in WGS84.

Usage: build_deso.py OUT.json
Needs: shapely, pyproj (pip install shapely pyproj)
"""
import json
import sys
import time
import urllib.request

import shapely

from pyproj import Transformer
from shapely.geometry import mapping, shape
from shapely.ops import transform  # noqa: shapely 2 still supports this

WFS = ("https://geodata.scb.se/geoserver/stat/wfs?service=WFS&version=2.0.0&request=GetFeature"
       "&typeNames=stat:DeSO_2025&outputFormat=application/json&srsName=EPSG:3006")
API = "https://api.scb.se/OV0104/v1/doris/sv/ssd/"
SUFFIX = "_DeSO2025"
CATCHMENT_M = 3000
SIMPLIFY_M = 250  # metres; keeps outlines recognisable while keeping the file small


def get(url, body=None):
    for attempt in range(5):
        try:
            data = json.dumps(body).encode() if body is not None else None
            req = urllib.request.Request(url, data=data, headers={"User-Agent": "site-finder-data/1.0",
                                                                  "Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=300) as r:
                return json.loads(r.read().decode("utf-8-sig"))
        except Exception as e:
            print(f"{url[:80]}: {e}; retrying", file=sys.stderr)
            time.sleep(10 * (attempt + 1))  # SCB allows ~30 requests per 10 s
    raise SystemExit(f"Giving up on {url}")


def table(path, fixed):
    """Latest year of a DeSO table, as {deso_code: value}."""
    meta = get(API + path)
    var = {v["code"]: v["values"] for v in meta["variables"]}
    regions = [r for r in var["Region"] if r.endswith(SUFFIX)]
    year = var["Tid"][-1]
    query = [{"code": "Region", "selection": {"filter": "item", "values": regions}},
             {"code": "Tid", "selection": {"filter": "item", "values": [year]}}]
    query += [{"code": k, "selection": {"filter": "item", "values": [v]}} for k, v in fixed.items()]
    d = get(API + path, {"query": query, "response": {"format": "json"}})
    out = {}
    for row in d["data"]:
        val = row["values"][0]
        if val not in ("..", ".", "-", ""):
            out[row["key"][0].removesuffix(SUFFIX)] = float(val)
    print(f"{path}: {len(out)} areas, {year}", file=sys.stderr)
    return out, year


def catchment(feats, radius=CATCHMENT_M):
    """cars3/pop3: cars and residents in every area whose centre is within 3 km."""
    grid = {}
    for f in feats:
        x, y = f["properties"]["_xy"]
        grid.setdefault((int(x // radius), int(y // radius)), []).append(f["properties"])
    for f in feats:
        p = f["properties"]
        x, y = p["_xy"]
        gx, gy = int(x // radius), int(y // radius)
        cars = pop = 0
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for q in grid.get((gx + dx, gy + dy), ()):
                    qx, qy = q["_xy"]
                    if (qx - x) ** 2 + (qy - y) ** 2 <= radius ** 2:
                        cars += q["cars"]
                        pop += q["pop"]
        p["cars3"], p["pop3"] = cars, pop
    for f in feats:
        del f["properties"]["_xy"]


def main(out):
    pop, ypop = table("BE/BE0101/BE0101Y/FolkmDesoAldKon", {"Alder": "totalt", "Kon": "1+2", "ContentsCode": "000007Y7"})
    cars, ycars = table("TK/TK1001/TK1001Z/PersBilarDesoN", {"Bestand": "ITRAF", "ContentsCode": "000007ZL"})
    inc, yinc = table("HE/HE0110/HE0110I/Tab2InkDesoRegso", {"Inkomstkomponenter": "240", "Kon": "1+2", "ContentsCode": "000008A4"})

    geo = get(WFS)
    to_wgs = Transformer.from_crs("EPSG:3006", "EPSG:4326", always_xy=True).transform
    feats = []
    shapes = [shape(f["geometry"]) for f in geo["features"]]
    # Simplify all areas together so neighbours keep identical shared borders (no slivers)
    simple = shapely.coverage_simplify(shapes, SIMPLIFY_M)
    for f, g, gs in zip(geo["features"], shapes, simple):
        p = f["properties"]
        c = p["desokod"]
        km2 = g.area / 1e6
        pt = g.representative_point()
        lon, lat = to_wgs(pt.x, pt.y)
        g = transform(to_wgs, gs if not gs.is_empty else g.simplify(SIMPLIFY_M))
        gj = mapping(g)
        if gj["type"] == "Polygon":
            gj = {"type": "MultiPolygon", "coordinates": [gj["coordinates"]]}
        gj = {"type": "MultiPolygon",
              "coordinates": [[[[round(x, 4), round(y, 4)] for x, y in ring] for ring in poly] for poly in gj["coordinates"]]}
        feats.append({"type": "Feature", "geometry": gj, "properties": {
            "c": c, "k": p["kommunkod"], "pop": int(pop.get(c, 0)), "cars": int(cars.get(c, 0)),
            "inc": inc.get(c), "km2": round(km2, 3), "lat": round(lat, 4), "lon": round(lon, 4),
            "_xy": (pt.x, pt.y)}})
    catchment(feats)
    if len(feats) < 5000:
        raise SystemExit(f"Only {len(feats)} areas; refusing to write")
    with open(out, "w", encoding="utf-8") as fh:
        json.dump({"type": "FeatureCollection", "years": {"pop": ypop, "cars": ycars, "inc": yinc}, "features": feats},
                  fh, separators=(",", ":"))
    print(f"Wrote {len(feats)} areas to {out}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "deso.json")
