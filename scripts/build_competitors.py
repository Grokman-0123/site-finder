"""Build the Sweden-wide competitor file for the Site Finder page.

Downloads the OpenStreetMap extract for Sweden from Geofabrik, keeps every tyre
shop, car repair shop and car wash (with osmium-tool), and writes a compact JSON
file. Geofabrik serves a ready-made file, so this does not depend on the busy
public Overpass servers that kept timing out on a whole-country query.

Requires the `osmium` command (apt-get install osmium-tool).

Usage: build_competitors.py OUT.json [SOURCE.osm.pbf]
  If SOURCE is given it is used instead of downloading (handy for testing).

Output: {"updated": "YYYY-MM-DD", "c": [[lat, lon, kinds, name, address], ...]}
kinds is a string of letters: t = tyres, r = car repair, w = car wash.
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
FILTERS = [
    "nwr/shop=tyres,car_repair",
    "nwr/craft=car_repair",
    "nwr/amenity=car_wash",
    "nwr/service:vehicle:tyres=yes",
]


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
    k = ""
    if t.get("shop") == "tyres" or t.get("service:vehicle:tyres") == "yes":
        k += "t"
    if t.get("shop") == "car_repair" or t.get("craft") == "car_repair":
        k += "r"
    if t.get("amenity") == "car_wash":
        k += "w"
    return k


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


def main(out, source=None):
    with tempfile.TemporaryDirectory() as work:
        pbf = source
        if not pbf:
            pbf = os.path.join(work, "sweden.osm.pbf")
            download(pbf)
        rows, seen = [], set()
        for feat in extract(pbf, work):
            t = feat.get("properties") or {}
            key = (t.get("@type"), t.get("@id"))
            k = kinds(t)
            if not k or key in seen:
                continue
            pts = points(feat["geometry"])
            if len(pts) > 1 and pts[0] == pts[-1]:  # closed ring repeats its first point
                pts = pts[:-1]
            if not pts:
                continue
            seen.add(key)
            lon = sum(p[0] for p in pts) / len(pts)
            lat = sum(p[1] for p in pts) / len(pts)
            street = " ".join(x for x in (t.get("addr:street"), t.get("addr:housenumber")) if x)
            addr = ", ".join(x for x in (street, t.get("addr:city")) if x)
            rows.append([round(lat, 5), round(lon, 5), k, t.get("name", ""), addr])
    rows.sort()
    minimum = int(os.environ.get("MIN_PLACES", "1000"))
    if len(rows) < minimum:
        raise SystemExit(f"Only {len(rows)} places found; refusing to overwrite the data file")
    with open(out, "w", encoding="utf-8") as f:
        json.dump({"updated": datetime.date.today().isoformat(), "c": rows}, f, ensure_ascii=False, separators=(",", ":"))
    print(f"Wrote {len(rows)} places to {out}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "competitors.json", sys.argv[2] if len(sys.argv) > 2 else None)
