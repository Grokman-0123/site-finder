"""Fetch every tyre shop, car repair shop and car wash in Sweden from OpenStreetMap
and write a compact JSON file for the Site Finder page.

Output: {"updated": "YYYY-MM-DD", "c": [[lat, lon, kinds, name, address], ...]}
kinds is a string of letters: t = tyres, r = car repair, w = car wash.
"""
import datetime
import json
import sys
import time
import urllib.parse
import urllib.request

SERVERS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
]
QUERY = """[out:json][timeout:300];
area["ISO3166-1"="SE"][admin_level=2]->.se;
(
  nwr["shop"="tyres"](area.se);
  nwr["service:vehicle:tyres"="yes"](area.se);
  nwr["shop"="car_repair"](area.se);
  nwr["craft"="car_repair"](area.se);
  nwr["amenity"="car_wash"](area.se);
);
out center tags;"""


def fetch():
    body = urllib.parse.urlencode({"data": QUERY}).encode()
    last = None
    for attempt in range(3):
        for url in SERVERS:
            try:
                req = urllib.request.Request(url, data=body, headers={"User-Agent": "site-finder-data/1.0"})
                with urllib.request.urlopen(req, timeout=360) as r:
                    data = json.load(r)
                if data.get("elements"):
                    return data["elements"]
            except Exception as e:  # try next server
                last = e
                print(f"{url}: {e}", file=sys.stderr)
        time.sleep(30)
    raise SystemExit(f"All servers failed: {last}")


def kinds(t):
    k = ""
    if t.get("shop") == "tyres" or t.get("service:vehicle:tyres") == "yes":
        k += "t"
    if t.get("shop") == "car_repair" or t.get("craft") == "car_repair":
        k += "r"
    if t.get("amenity") == "car_wash":
        k += "w"
    return k


def main(out):
    rows, seen = [], set()
    for e in fetch():
        key = (e["type"], e["id"])
        if key in seen:
            continue
        seen.add(key)
        t = e.get("tags", {})
        lat = e.get("lat", e.get("center", {}).get("lat"))
        lon = e.get("lon", e.get("center", {}).get("lon"))
        k = kinds(t)
        if lat is None or not k:
            continue
        street = " ".join(x for x in (t.get("addr:street"), t.get("addr:housenumber")) if x)
        addr = ", ".join(x for x in (street, t.get("addr:city")) if x)
        rows.append([round(lat, 5), round(lon, 5), k, t.get("name", ""), addr])
    rows.sort()
    if len(rows) < 1000:
        raise SystemExit(f"Only {len(rows)} places found; refusing to overwrite the data file")
    with open(out, "w", encoding="utf-8") as f:
        json.dump({"updated": datetime.date.today().isoformat(), "c": rows}, f, ensure_ascii=False, separators=(",", ":"))
    print(f"Wrote {len(rows)} places to {out}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "competitors.json")
