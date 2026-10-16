#!/usr/bin/env python3
"""Build a compact search index of maps georeferenced with Allmaps.

Reads Allmaps' daily open-data dump (one georeferenced map per line, CC0) and
writes one row per map: enough for the Terra Nota app to search by title and
to rank the maps covering a place by how much ground they cover. The app
fetches a map's full georeference from Allmaps when it's chosen.

Usage: build_map_index.py OUTPUT.json [DUMP.ndjson]
Without DUMP, the dump is streamed from files.allmaps.org.
"""

import datetime
import html
import json
import re
import sys
import urllib.request

DUMP_URL = "https://files.allmaps.org/maps.ndjson"
MAP_URL_PREFIX = "https://annotations.allmaps.org/maps/"
COLUMNS = ["id", "title", "provider", "service", "minLat", "minLon", "maxLat", "maxLon", "area"]
# Outside these (m²) the georeference is broken: smaller than a building
# plot (control points on top of each other) or larger than the Earth.
MIN_AREA, MAX_AREA = 1e3, 5.2e14


def text(label):
    """First string in a IIIF language map. Some labels in the dump are
    themselves JSON-encoded language maps, so unwrap those too."""
    if not isinstance(label, dict):
        return None
    values = label.get("en") or label.get("none") or next(iter(label.values()), None)
    value = values[0] if isinstance(values, list) and values else None
    if isinstance(value, str) and value.startswith("{"):
        try:
            return text(json.loads(value))
        except ValueError:
            pass
    return value


def clean(value):
    if not value:
        return None
    value = re.sub(r"<[^>]+>", " ", html.unescape(value))
    value = re.sub(r"\s+", " ", value).strip(" ·,")
    return value or None


def title(resource):
    # resource → canvas → manifest. The manifest names the work; the canvas is
    # only a page of it ("image 1").
    canvas = (resource.get("partOf") or [None])[0] or {}
    manifest = canvas if canvas.get("type") == "Manifest" else next(
        (p for p in canvas.get("partOf") or [] if p.get("type") == "Manifest"), {})
    return clean(text(manifest.get("label"))) or clean(text(canvas.get("label")))


def extent(m):
    """(minLat, minLon, maxLat, maxLon) of the map's outline, or its control points."""
    ring = ((m.get("geoMask") or {}).get("coordinates") or [[]])[0]
    points = [p for p in ring if len(p) == 2] or [g["geo"] for g in m.get("gcps", []) if len(g.get("geo", [])) == 2]
    if not points:
        return None
    lons, lats = [p[0] for p in points], [p[1] for p in points]
    if min(lons) < -180 or max(lons) > 180 or min(lats) < -90 or max(lats) > 90:
        return None
    return round(min(lats), 4), round(min(lons), 4), round(max(lats), 4), round(max(lons), 4)


def rows(lines):
    for line in lines:
        try:
            m = json.loads(line)
        except ValueError:
            continue
        area = (m.get("_allmaps") or {}).get("area")
        box = extent(m)
        resource = m.get("resource") or {}
        if not (m.get("id", "").startswith(MAP_URL_PREFIX) and resource.get("id") and box
                and area and MIN_AREA <= area < MAX_AREA and len(m.get("gcps", [])) >= 3):
            continue
        provider = clean(text(((resource.get("provider") or [{}])[0]).get("label")))
        yield m["id"][len(MAP_URL_PREFIX):], title(resource), provider, resource["id"], *box, round(area)


def main():
    output = sys.argv[1]
    if len(sys.argv) > 2:
        lines = open(sys.argv[2], encoding="utf-8")
    else:
        request = urllib.request.Request(DUMP_URL, headers={"User-Agent": "terranota-map-index"})
        lines = (line.decode("utf-8") for line in urllib.request.urlopen(request, timeout=300))

    providers, maps = {}, []
    for id_, title_, provider, service, *rest in rows(lines):
        index = providers.setdefault(provider, len(providers)) if provider else None
        maps.append([id_, title_, index, service, *rest])
    # Most local first, so a reader can stop early; ties by id for stable output.
    maps.sort(key=lambda row: (row[-1], row[0]))

    index = {
        "version": 1,
        "generated": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "source": DUMP_URL,
        "attribution": "Georeferencing by Allmaps contributors (CC0)",
        "mapURLPrefix": MAP_URL_PREFIX,
        "columns": COLUMNS,
        "providers": list(providers),
        "maps": maps,
    }
    with open(output, "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False, separators=(",", ":"))
    titled = sum(1 for row in maps if row[1])
    print(f"{len(maps)} maps ({titled} with titles) from {len(providers)} providers → {output}")


if __name__ == "__main__":
    main()
