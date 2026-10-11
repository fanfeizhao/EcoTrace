#!/usr/bin/env python3
"""
Filter Santa Clara County 2D building footprints to the EcoTrace study area.

Input:
  santa_clara_building_footprints.geojson

Output:
  ecotrace_building_footprints.geojson

The script streams the 469 MB FeatureCollection with ijson so it does not need
to load the entire file into RAM.
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

try:
    import ijson
except ImportError:
    print("Missing package: ijson")
    print("Install it with: python3 -m pip install ijson")
    raise SystemExit(1)

INPUT = Path("santa_clara_building_footprints.geojson")
OUTPUT = Path("ecotrace_building_footprints.geojson")

# Same EcoTrace study rectangle previously used for canopy data.
MIN_LON, MIN_LAT = -122.12, 37.27
MAX_LON, MAX_LAT = -121.82, 37.47


def iter_coords(coords):
    if isinstance(coords, (list, tuple)):
        if len(coords) >= 2 and isinstance(coords[0], (int, float)) and isinstance(coords[1], (int, float)):
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
        max_lon < MIN_LON or min_lon > MAX_LON or
        max_lat < MIN_LAT or min_lat > MAX_LAT
    )


def first_property(props, names):
    for name in names:
        if name in props and props[name] not in (None, ""):
            return props[name]
    return None


if not INPUT.exists():
    print(f"Input not found: {INPUT.resolve()}")
    raise SystemExit(1)

kept = 0
scanned = 0
height_count = 0
height_values = []

with INPUT.open("rb") as source, OUTPUT.open("w", encoding="utf-8") as out:
    out.write('{"type":"FeatureCollection","features":[\n')
    first = True

    for feature in ijson.items(source, "features.item"):
        scanned += 1
        geometry = feature.get("geometry")
        if not intersects_bounds(geometry):
            continue

        props = feature.get("properties") or {}

        building_height = first_property(
            props,
            [
                "Building_H", "BUILDING_H", "building_h",
                "Building_Height", "BUILDING_HEIGHT", "Height", "HEIGHT"
            ],
        )
        base_height = first_property(
            props,
            ["Base_Height", "BASE_HEIGHT", "base_height"],
        )
        building_id = first_property(
            props,
            ["OBJECTID", "ObjectID", "objectid", "GlobalID", "BUILDING_ID", "Building_ID"],
        )

        clean_props = {
            "building_id": building_id,
            "building_height": building_height,
            "base_height": base_height,
        }

        # Keep the original values too if they exist, so nothing important is lost.
        if "Building_H" in props:
            clean_props["Building_H"] = props["Building_H"]
        if "Base_Height" in props:
            clean_props["Base_Height"] = props["Base_Height"]

        try:
            h = float(building_height)
            if math.isfinite(h) and h > 0:
                height_count += 1
                if len(height_values) < 100000:
                    height_values.append(h)
        except (TypeError, ValueError):
            pass

        cleaned = {
            "type": "Feature",
            "geometry": geometry,
            "properties": clean_props,
        }

        if not first:
            out.write(",\n")
        json.dump(cleaned, out, separators=(",", ":"))
        first = False
        kept += 1

        if scanned % 50000 == 0:
            print(f"Scanned {scanned:,} buildings; kept {kept:,}...")

    out.write("\n]}\n")

print()
print(f"Done.")
print(f"Scanned: {scanned:,}")
print(f"Kept in EcoTrace bounds: {kept:,}")
print(f"Buildings with usable height: {height_count:,}")
print(f"Output: {OUTPUT.resolve()}")

if height_values:
    height_values.sort()
    mid = height_values[len(height_values)//2]
    p95 = height_values[min(len(height_values)-1, int(len(height_values)*0.95))]
    print(f"Sample median height value: {mid:.2f}")
    print(f"Sample 95th percentile height value: {p95:.2f}")
    print("If these values look like ~10–100, they may be feet; we will verify units before using them.")
