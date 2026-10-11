#!/usr/bin/env python3
"""
Correct EcoTrace building-footprint filter.

Fix: ijson parses GeoJSON numeric coordinates as Decimal objects, not float.
The previous script checked only int/float, so it accidentally treated every
coordinate array as nested data and found no coordinates.

Input:
  santa_clara_building_footprints.geojson

Output:
  ecotrace_building_footprints.geojson
"""

from __future__ import annotations

import json
import math
from decimal import Decimal
from numbers import Number
from pathlib import Path

import ijson

INPUT = Path("santa_clara_building_footprints.geojson")
OUTPUT = Path("ecotrace_building_footprints.geojson")

MIN_LON, MIN_LAT = -122.12, 37.27
MAX_LON, MAX_LAT = -121.82, 37.47


def is_number(value):
    return isinstance(value, Number) or isinstance(value, Decimal)


def normalize_coords(value):
    """Convert Decimal values from ijson into normal floats for JSON output."""
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, list):
        return [normalize_coords(item) for item in value]
    if isinstance(value, tuple):
        return [normalize_coords(item) for item in value]
    return value


def iter_coords(coords):
    if isinstance(coords, (list, tuple)):
        if (
            len(coords) >= 2
            and is_number(coords[0])
            and is_number(coords[1])
        ):
            yield float(coords[0]), float(coords[1])
        else:
            for item in coords:
                yield from iter_coords(item)


def intersects_bounds(geometry):
    if not isinstance(geometry, dict):
        return False
    coords = geometry.get("coordinates")
    if coords is None:
        return False

    min_lon = math.inf
    min_lat = math.inf
    max_lon = -math.inf
    max_lat = -math.inf
    found = False

    for lon, lat in iter_coords(coords):
        found = True
        min_lon = min(min_lon, lon)
        min_lat = min(min_lat, lat)
        max_lon = max(max_lon, lon)
        max_lat = max(max_lat, lat)

    if not found:
        return False

    return not (
        max_lon < MIN_LON
        or min_lon > MAX_LON
        or max_lat < MIN_LAT
        or min_lat > MAX_LAT
    )


if not INPUT.exists():
    raise SystemExit(f"Input not found: {INPUT.resolve()}")

scanned = 0
kept = 0
height_count = 0
sample_heights = []

with INPUT.open("rb") as source, OUTPUT.open("w", encoding="utf-8") as out:
    out.write('{"type":"FeatureCollection","features":[\n')
    first = True

    for feature in ijson.items(source, "features.item"):
        scanned += 1

        geometry = feature.get("geometry")
        if not intersects_bounds(geometry):
            if scanned % 50000 == 0:
                print(f"Scanned {scanned:,}; kept {kept:,}...")
            continue

        props = feature.get("properties") or {}

        building_height = props.get("building_h")
        base_height = props.get("base_heigh")
        building_id = props.get("objectid") or props.get(":id")

        try:
            h = float(building_height)
            if math.isfinite(h) and h > 0:
                height_count += 1
                if len(sample_heights) < 100000:
                    sample_heights.append(h)
        except (TypeError, ValueError):
            pass

        cleaned = {
            "type": "Feature",
            "geometry": {
                "type": geometry.get("type"),
                "coordinates": normalize_coords(geometry.get("coordinates")),
            },
            "properties": {
                "building_id": building_id,
                "building_h": building_height,
                "base_heigh": base_height,
                "class": props.get("class"),
            },
        }

        if not first:
            out.write(",\n")
        json.dump(cleaned, out, separators=(",", ":"))
        first = False
        kept += 1

        if scanned % 50000 == 0:
            print(f"Scanned {scanned:,}; kept {kept:,}...")

    out.write("\n]}\n")

print()
print("Done.")
print(f"Scanned: {scanned:,}")
print(f"Kept in EcoTrace bounds: {kept:,}")
print(f"Buildings with usable height: {height_count:,}")
print(f"Output: {OUTPUT.resolve()}")

if sample_heights:
    sample_heights.sort()
    median = sample_heights[len(sample_heights)//2]
    p95 = sample_heights[min(len(sample_heights)-1, int(len(sample_heights)*0.95))]
    print(f"Sample median building_h: {median:.2f}")
    print(f"Sample 95th percentile building_h: {p95:.2f}")
