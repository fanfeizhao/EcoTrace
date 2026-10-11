#!/usr/bin/env python3
"""
Merge official Santa Clara County building footprints/heights into EcoTrace's canopy model.

Inputs in current folder:
  - ecotrace_building_footprints.geojson
  - canopy_model.json OR canopy_model_arcgis.json

Output:
  - canopy_model_county_buildings.json

What it does:
  1) Reads each filtered County building footprint.
  2) Converts building_h from feet to meters.
  3) Converts MultiPolygon/Polygon geometry into EcoTrace building polygons.
  4) Replaces the old building list in the canopy model.
  5) Preserves the existing trees and metadata.
"""

from __future__ import annotations

import json
import math
from decimal import Decimal
from pathlib import Path

import ijson

BUILDINGS_FILE = Path("ecotrace_building_footprints.geojson")

MODEL_CANDIDATES = [
    Path("canopy_model_arcgis.json"),
    Path("canopy_model.json"),
]

OUTPUT = Path("canopy_model_county_buildings.json")

FEET_TO_METERS = 0.3048


def choose_model():
    for path in MODEL_CANDIDATES:
        if path.exists():
            return path
    raise SystemExit(
        "No canopy model found. Expected canopy_model_arcgis.json or canopy_model.json "
        "in the current EcoTrace folder."
    )


def normalize_number(value):
    if isinstance(value, Decimal):
        return float(value)
    return value


def valid_height_m(raw_height):
    try:
        feet = float(raw_height)
    except (TypeError, ValueError):
        return None

    if not math.isfinite(feet) or feet <= 0:
        return None

    meters = feet * FEET_TO_METERS

    # Conservative sanity bounds for building-shadow modeling.
    # Very tiny/huge values are retained only after clipping.
    return max(1.5, min(150.0, meters))


def exterior_rings(geometry):
    """
    Yield exterior rings as [[lon,lat], ...] for Polygon/MultiPolygon.
    """
    if not isinstance(geometry, dict):
        return

    gtype = geometry.get("type")
    coords = geometry.get("coordinates") or []

    if gtype == "Polygon":
        if coords and coords[0]:
            yield coords[0]

    elif gtype == "MultiPolygon":
        for polygon in coords:
            if polygon and polygon[0]:
                yield polygon[0]


if not BUILDINGS_FILE.exists():
    raise SystemExit(f"Missing building file: {BUILDINGS_FILE.resolve()}")

model_path = choose_model()
print(f"Loading existing canopy model: {model_path}")

with model_path.open("r", encoding="utf-8") as f:
    model = json.load(f)

trees = model.get("trees") or []
old_buildings = model.get("buildings") or []

print(f"Existing trees: {len(trees):,}")
print(f"Existing buildings being replaced: {len(old_buildings):,}")
print("Reading official County buildings...")

new_buildings = []
features_scanned = 0
features_used = 0
polygons_added = 0
missing_height = 0

with BUILDINGS_FILE.open("rb") as f:
    for feature in ijson.items(f, "features.item"):
        features_scanned += 1

        props = feature.get("properties") or {}
        height_m = valid_height_m(props.get("building_h"))

        if height_m is None:
            missing_height += 1
            continue

        geometry = feature.get("geometry") or {}
        building_id = props.get("building_id")

        used_this_feature = False

        for ring in exterior_rings(geometry):
            clean_ring = []
            for point in ring:
                if not isinstance(point, (list, tuple)) or len(point) < 2:
                    continue
                try:
                    lon = float(normalize_number(point[0]))
                    lat = float(normalize_number(point[1]))
                except (TypeError, ValueError):
                    continue
                if math.isfinite(lon) and math.isfinite(lat):
                    clean_ring.append([lon, lat])

            if len(clean_ring) < 4:
                continue

            # Ensure the polygon is closed.
            if clean_ring[0] != clean_ring[-1]:
                clean_ring.append(clean_ring[0])

            new_buildings.append({
                "building_id": building_id,
                "coordinates": clean_ring,
                "height_m": round(height_m, 2),
                "height_source": "Santa Clara County Buildings Footprints 2D building_h",
                "height_original_ft": props.get("building_h"),
            })
            polygons_added += 1
            used_this_feature = True

        if used_this_feature:
            features_used += 1

        if features_scanned % 50000 == 0:
            print(
                f"Scanned {features_scanned:,}; "
                f"used {features_used:,}; polygons {polygons_added:,}"
            )

model["buildings"] = new_buildings

metadata = model.get("metadata") or {}
metadata.update({
    "building_source": "Santa Clara County Buildings Footprints 2D",
    "building_height_field": "building_h",
    "building_height_units_original": "feet",
    "building_height_units_model": "meters",
    "building_height_conversion": "feet * 0.3048",
    "county_building_features_scanned": features_scanned,
    "county_building_features_used": features_used,
    "county_building_polygons_added": polygons_added,
})
model["metadata"] = metadata

print()
print("Writing merged canopy model...")
with OUTPUT.open("w", encoding="utf-8") as f:
    json.dump(model, f, separators=(",", ":"))

print()
print("Done.")
print(f"Output: {OUTPUT.resolve()}")
print(f"Trees preserved: {len(trees):,}")
print(f"County building features scanned: {features_scanned:,}")
print(f"County building features used: {features_used:,}")
print(f"Building polygons added: {polygons_added:,}")
print(f"Missing/invalid heights skipped: {missing_height:,}")

# quick sanity stats
if new_buildings:
    heights = sorted(b["height_m"] for b in new_buildings)
    median = heights[len(heights)//2]
    p95 = heights[min(len(heights)-1, int(len(heights)*0.95))]
    print(f"Median building height in model: {median:.2f} m")
    print(f"95th percentile building height in model: {p95:.2f} m")
