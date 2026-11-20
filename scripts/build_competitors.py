"""Build the Sweden-wide competitor file for the Site Finder page.

Downloads the OpenStreetMap extract for Sweden from Geofabrik, keeps every tyre
shop, car repair shop and car wash (with osmium-tool), and writes a compact JSON
file. Geofabrik serves a ready-made file, so this does not depend on the busy
public Overpass servers that kept timing out on a whole-country query.

Requires the `osmium` command (apt-get install osmium-tool).

Usage: build_competitors.py OUT.json [SOURCE.osm.pbf]
  If SOURCE is given it is used instead of downloading (handy for testing).

Output: {"updated": "YYYY-MM-DD", "c": [[lat, lon, kinds, name, address, source], ...]}
kinds is a string of letters: t = tyres, r = car repair, w = car wash.
source: o = OpenStreetMap, s = SCB company register, os = both.
With SCB_API_KEY set, workplaces from SCB's company register are merged in (scb_workplaces.py).
"""
import datetime
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request

EXTRACT = "https://download.geofabrik.de/europe/sweden-latest.osm.pbf"
# Broad pre-filter; kinds() below decides what each object counts as.
FILTERS = [
    "nwr/shop=tyres,car_repair,car,car_parts",
    "nwr/craft=car_repair",
    "nwr/amenity=car_wash,fuel",
    "nwr/service:vehicle:tyres",
    "nwr/service:vehicle:car_repair",
    "nwr/car_wash",
]
TYRE_WORDS = ("däck", "dack", "tyre", "tire", "vianor", "euromaster", "däckia", "dackia", "däckteam", "hjulmagasinet")
REPAIR_BRANDS = ("mekonomen", "meca", "autoexperten", "bosch car service", "bilxtra", "verkstad", "motor", "bilservice")


def download(dest):
    last = None
    for attempt in range(4):
        try:
            req = urllib.request.Request(EXTRACT, headers={"User-Agent": "site-finder-data/2.0"})
            with urllib.request.urlopen(req, timeout=120) as r, open(dest, "wb") as f:
                while chunk := r.read(1 << 20):
                    f.write(chunk)
            print(f"Downloaded {os.path.getsize(dest) // (1 << 20)} MB", file=sys.stderr)
            return
        except Exception as e:
            last = e
            print(f"Download failed ({e}); retrying", file=sys.stderr)
            time.sleep(30 * (attempt + 1))
    raise SystemExit(f"Could not download {EXTRACT}: {last}")


def kinds(t):
    """t = tyres, r = car repair, w = car wash. Also returns why, for the log."""
    shop, craft, amenity = t.get("shop"), t.get("craft"), t.get("amenity")
    label = " ".join(t.get(x, "") for x in ("name", "brand", "operator")).lower()
    motoring = shop in ("car_repair", "car", "car_parts", "tyres") or craft == "car_repair"
    k, why = "", []
    if shop == "tyres":
        k += "t"; why.append("shop=tyres")
    elif t.get("service:vehicle:tyres") in ("yes", "only"):
        k += "t"; why.append("service:vehicle:tyres")
    elif motoring and any(w in label for w in TYRE_WORDS):
        k += "t"; why.append("tyre name")
    if shop == "car_repair" or craft == "car_repair":
        k += "r"; why.append("car_repair")
    elif shop in ("car", "car_parts") and (t.get("service:vehicle:car_repair") in ("yes", "only")
                                         or any(w in label for w in REPAIR_BRANDS)):
        k += "r"; why.append("dealer/parts with workshop")
    if amenity == "car_wash":
        k += "w"; why.append("amenity=car_wash")
    elif t.get("car_wash") in ("yes", "self_service", "automated"):
        k += "w"; why.append("fuel/other with car_wash")
    return k, why


def points(geom):
    """Every [lon, lat] pair in a GeoJSON geometry."""
    def walk(c):
        if c and isinstance(c[0], (int, float)):
            yield c
        else:
            for x in c:
                yield from walk(x)
    if geom["type"] == "Polygon":  # outer ring only
        return list(walk(geom["coordinates"][0]))
    if geom["type"] == "MultiPolygon":
        return [p for poly in geom["coordinates"] for p in walk(poly[0])]
    return list(walk(geom["coordinates"]))


def extract(pbf, workdir):
    filtered = os.path.join(workdir, "filtered.osm.pbf")
    geo = os.path.join(workdir, "places.geojsonseq")
    subprocess.run(["osmium", "tags-filter", "--overwrite", "-o", filtered, pbf, *FILTERS], check=True)
    subprocess.run(["osmium", "export", "--overwrite", "-f", "geojsonseq", "-x", "print_record_separator=false",
                    "-a", "type,id", "-o", geo, filtered], check=True)
    with open(geo, encoding="utf-8") as f:
        for line in f:
            line = line.strip().lstrip("\x1e")
            if line:
                yield json.loads(line)


def merge(rows, extra, radius_m=120):
    """Add register workplaces; one already on OpenStreetMap within ~120 m with a shared kind is merged."""
    cell = 0.002  # about 220 m north-south
    grid = {}
    for r in rows:
        grid.setdefault((int(r[0] / cell), int(r[1] / cell)), []).append(r)
    added = merged = 0
    for e in extra:
        gy, gx = int(e[0] / cell), int(e[1] / cell)
        best, bestd = None, radius_m
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                for r in grid.get((gy + dy, gx + dx), ()):
                    if not set(r[2]) & set(e[2]):
                        continue
                    d = ((r[0] - e[0]) * 111_000) ** 2 + ((r[1] - e[1]) * 111_000 * 0.5) ** 2
                    if d ** 0.5 < bestd:
                        best, bestd = r, d ** 0.5
        if best:
            best[2] = "".join(x for x in "trw" if x in best[2] + e[2])
            best[3] = best[3] or e[3]
            best[4] = best[4] or e[4]
            if "s" not in best[5]:
                best[5] += "s"
            merged += 1
        else:
            row = [e[0], e[1], e[2], e[3], e[4], "s"]
            rows.append(row)
            grid.setdefault((gy, gx), []).append(row)
            added += 1
    print(f"  SCB register: {added} new places, {merged} matched OpenStreetMap")


def main(out, source=None):
    with tempfile.TemporaryDirectory() as work:
        pbf = source
        if not pbf:
            pbf = os.path.join(work, "sweden.osm.pbf")
            download(pbf)
        rows, seen, reasons = [], set(), {}
        for feat in extract(pbf, work):
            t = feat.get("properties") or {}
            key = (t.get("@type"), t.get("@id"))
            k, why = kinds(t)
            if not k or key in seen:
                continue
            pts = points(feat["geometry"])
            if len(pts) > 1 and pts[0] == pts[-1]:  # closed ring repeats its first point
                pts = pts[:-1]
            if not pts:
                continue
            seen.add(key)
            for w in why:
                reasons[w] = reasons.get(w, 0) + 1
            lon = sum(p[0] for p in pts) / len(pts)
            lat = sum(p[1] for p in pts) / len(pts)
            street = " ".join(x for x in (t.get("addr:street"), t.get("addr:housenumber")) if x)
            addr = ", ".join(x for x in (street, t.get("addr:city")) if x)
            rows.append([round(lat, 5), round(lon, 5), k, t.get("name", ""), addr])
    for w, n in sorted(reasons.items(), key=lambda x: -x[1]):
        print(f"  {n:6d}  {w}")
    print("  OpenStreetMap by kind: " + ", ".join(f"{x}={sum(x in r[2] for r in rows)}" for x in "trw"))
    for r in rows:
        r.append("o")
    if os.environ.get("SCB_API_KEY") or os.environ.get("SCB_VIA_PROXY"):
        import scb_workplaces
        cache = os.path.join(os.path.dirname(os.path.abspath(out)), "scb_positions.json")
        merge(rows, scb_workplaces.fetch(cache))
    else:
        print("  SCB_API_KEY not set: OpenStreetMap only")
    rows.sort()
    print("  total by kind: " + ", ".join(f"{x}={sum(x in r[2] for r in rows)}" for x in "trw"))
    minimum = int(os.environ.get("MIN_PLACES", "1000"))
    if len(rows) < minimum:
        raise SystemExit(f"Only {len(rows)} places found; refusing to overwrite the data file")
    with open(out, "w", encoding="utf-8") as f:
        json.dump({"updated": datetime.date.today().isoformat(), "c": rows}, f, ensure_ascii=False, separators=(",", ":"))
    print(f"Wrote {len(rows)} places to {out}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "competitors.json", sys.argv[2] if len(sys.argv) > 2 else None)
