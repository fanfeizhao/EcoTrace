#!/usr/bin/env python3
"""
Reproject and filter Santa Clara County 2D building footprints for EcoTrace.

Input:
  santa_clara_building_footprints.geojson

Output:
  ecotrace_building_footprints_wgs84.geojson

The source County dataset uses California State Plane Zone 3, NAD83(2011),
US survey feet. This script converts coordinates to WGS84 latitude/longitude
and keeps only buildings within the EcoTrace study area.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import ijson
from pyproj import Transformer

INPUT = Path("santa_clara_building_footprints.geojson")
OUTPUT = Path("ecotrace_building_footprints_wgs84.geojson")

# EcoTrace coverage rectangle
MIN_LON, MIN_LAT = -122.12, 37.27
MAX_LON, MAX_LAT = -121.82, 37.47

# NAD83(2011) / California zone 3 (ftUS) -> WGS84
transformer = Transformer.from_crs("EPSG:6420", "EPSG:4326", always_xy=True)


def transform_coords(coords):
    if isinstance(coords, (list, tuple)):
        if (
            len(coords) >= 2
            and isinstance(coords[0], (int, float))
            and isinstance(coords[1], (int, float))
        ):
            lon, lat = transformer.transform(float(coords[0]), float(coords[1]))
            return [lon, lat]
        return [transform_coords(item) for item in coords]
    return coords


def iter_coords(coords):
    if isinstance(coords, (list, tuple)):
        if (
            len(coords) >= 2
            and isinstance(coords[0], (int, float))
            and isinstance(coords[1], (int, float))
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
        max_lon < MIN_LON or min_lon > MAX_LON
        or max_lat < MIN_LAT or min_lat > MAX_LAT
    )


def first_property(props, names):
    for name in names:
        if name in props and props[name] not in (None, ""):
            return props[name]
    return None


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
        if not geometry:
            continue

        transformed_geometry = {
            "type": geometry.get("type"),
            "coordinates": transform_coords(geometry.get("coordinates")),
        }

        if not intersects_bounds(transformed_geometry):
            if scanned % 50000 == 0:
                print(f"Scanned {scanned:,}; kept {kept:,}...")
            continue

        props = feature.get("properties") or {}
        building_height = first_property(
            props,
            [
                "Building_H", "BUILDING_H", "building_h",
                "Building_Height", "BUILDING_HEIGHT", "Height", "HEIGHT",
            ],
        )
        base_height = first_property(
            props,
            ["Base_Height", "BASE_HEIGHT", "base_height"],
        )
        building_id = first_property(
            props,
            [
                "OBJECTID", "ObjectID", "objectid",
                "GlobalID", "BUILDING_ID", "Building_ID",
            ],
        )

        clean_props = {
            "building_id": building_id,
            "Building_H": building_height,
            "Base_Height": base_height,
        }

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
            "geometry": transformed_geometry,
            "properties": clean_props,
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
    print(f"Sample median Building_H: {median:.2f}")
    print(f"Sample 95th percentile Building_H: {p95:.2f}")
    print("Send me these two values so we can confirm whether Building_H is feet or meters.")
