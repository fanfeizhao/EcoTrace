#!/usr/bin/env python3
from __future__ import annotations

import json
import math
import shutil
from pathlib import Path
from typing import Any

PATH = Path("experimental_tree_sites_fusion_v2.json")
BACKUP = Path("experimental_tree_sites_fusion_v2.before_levi_patch.json")

BAD_POINTS = [
    (37.3289927, -121.9182570, 30.0),
    (37.4049051, -121.9695283, 35.0),
    (37.4018217, -121.9684404, 35.0),
]

STADIUM_CORE = {
    "min_lat": 37.40265,
    "max_lat": 37.40595,
    "min_lon": -121.97225,
    "max_lon": -121.96805,
}

REPLACEMENTS = [
    (37.405126, -121.968895),
    (37.399909, -121.969326),
    (37.399759, -121.970143),
    (37.399495, -121.970481),
    (37.399038, -121.970120),
    (37.396759, -121.968728),
]


def haversine_m(lat1, lon1, lat2, lon2):
    r = 6_371_008.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2-lat1)
    dl = math.radians(lon2-lon1)
    a = math.sin(dp/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
    return 2*r*math.atan2(math.sqrt(a), math.sqrt(max(0,1-a)))


def coords(record: dict[str, Any]):
    lat = record.get("latitude", record.get("lat"))
    lon = record.get("longitude", record.get("lon", record.get("lng")))
    if lat is None or lon is None:
        geom = record.get("geometry")
        if isinstance(geom, dict) and geom.get("type") == "Point":
            c = geom.get("coordinates") or []
            if len(c) >= 2:
                lon, lat = c[0], c[1]
    try:
        return float(lat), float(lon)
    except (TypeError, ValueError):
        return None


def get_records(payload):
    if isinstance(payload, list):
        return payload, None
    if isinstance(payload, dict):
        for key in ("sites", "records", "features", "candidates"):
            if isinstance(payload.get(key), list):
                return payload[key], key
    raise SystemExit("Unsupported planting-site JSON structure.")


if not PATH.exists():
    raise SystemExit(f"Missing {PATH.resolve()}")

if not BACKUP.exists():
    shutil.copy2(PATH, BACKUP)
    print(f"Backup created: {BACKUP}")

payload = json.loads(PATH.read_text(encoding="utf-8"))
records, key = get_records(payload)

kept = []
removed = []

for record in records:
    c = coords(record)
    if c is None:
        kept.append(record)
        continue
    lat, lon = c

    remove = any(haversine_m(lat, lon, a, b) <= radius for a,b,radius in BAD_POINTS)

    if (
        STADIUM_CORE["min_lat"] <= lat <= STADIUM_CORE["max_lat"]
        and STADIUM_CORE["min_lon"] <= lon <= STADIUM_CORE["max_lon"]
    ):
        remove = True

    text = " ".join(
        str(record.get(k) or "")
        for k in ("area_name", "road_name", "where_location", "site_type", "source_type")
    ).casefold()
    if "levi" in text and haversine_m(lat, lon, 37.4046, -121.97011) < 450:
        remove = True

    if remove:
        removed.append(record)
    else:
        kept.append(record)

for i, (lat, lon) in enumerate(REPLACEMENTS, start=1):
    if any(
        (c := coords(record)) is not None and haversine_m(lat, lon, c[0], c[1]) < 8
        for record in kept
    ):
        continue

    kept.append({
        "site_id": f"MANUAL-LEVI-{i:03d}",
        "latitude": lat,
        "longitude": lon,
        "site_type": "street-edge planting opportunity",
        "area_name": "Levi's Stadium / Great America corridor",
        "source_type": "manual_qa_replacement",
        "experimental_fusion_score": 1.0,
        "evidence_confidence": "manual-reviewed",
        "screening_status": "Manual QA replacement — field/city verification required",
        "manual_review_status": "USER_REVIEWED_COORDINATE",
        "why_chosen": "Manually reviewed replacement coordinate supplied during EcoTrace QA after removing invalid Stadium-area candidates."
    })

if key is None:
    output = kept
else:
    payload[key] = kept
    meta = payload.get("metadata")
    if isinstance(meta, dict):
        meta["manual_levi_qa_removed"] = len(removed)
        meta["manual_levi_qa_replacements"] = len(REPLACEMENTS)
    output = payload

PATH.write_text(json.dumps(output, indent=2), encoding="utf-8")

print(f"Removed invalid Stadium/explicit records: {len(removed)}")
print(f"Added/ensured reviewed replacements: {len(REPLACEMENTS)}")
print(f"Final records: {len(kept)}")
print(f"Updated: {PATH.resolve()}")
