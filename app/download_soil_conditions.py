#!/usr/bin/env python3
import json
import requests

BASE = "https://services2.arcgis.com/tcv2cMrq63AgvbHF/ArcGIS/rest/services/Soils2/FeatureServer/1/query"
OUT = "soil_conditions.geojson"

all_features = []
offset = 0
page_size = 2000

while True:
    params = {
        "where": "1=1",
        "outFields": "*",
        "returnGeometry": "true",
        "f": "geojson",
        "outSR": "4326",
        "resultOffset": offset,
        "resultRecordCount": page_size,
    }
    r = requests.get(BASE, params=params, timeout=60)
    r.raise_for_status()
    data = r.json()

    features = data.get("features", [])
    all_features.extend(features)
    print(f"Downloaded {len(all_features):,} features...")

    if not data.get("properties", {}).get("exceededTransferLimit") and len(features) < page_size:
        break
    if not features:
        break

    offset += len(features)

with open(OUT, "w", encoding="utf-8") as f:
    json.dump({
        "type": "FeatureCollection",
        "crs": {"type": "name", "properties": {"name": "EPSG:4326"}},
        "features": all_features,
    }, f)

print(f"Saved {len(all_features):,} features to {OUT}")
