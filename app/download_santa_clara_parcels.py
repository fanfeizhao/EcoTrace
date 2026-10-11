#!/usr/bin/env python3
from __future__ import annotations

import json
import time
from pathlib import Path

import requests

ENDPOINT = "https://data.sccgov.org/resource/ubcd-cewv.geojson"
OUTPUT = Path("property_parcels.geojson")
PAGE_SIZE = 50000
TIMEOUT = 120

session = requests.Session()
session.headers.update({"User-Agent": "EcoTrace parcel downloader/1.0"})

offset = 0
total = 0
first_feature = True

with OUTPUT.open("w", encoding="utf-8") as out:
    out.write('{"type":"FeatureCollection","features":[\n')

    while True:
        params = {
            "$limit": PAGE_SIZE,
            "$offset": offset,
            "$order": ":id",
        }

        print(f"Requesting rows {offset:,}–{offset + PAGE_SIZE - 1:,}...")
        response = session.get(ENDPOINT, params=params, timeout=TIMEOUT)
        response.raise_for_status()
        page = response.json()

        features = page.get("features", [])
        if not features:
            break

        for feature in features:
            if not first_feature:
                out.write(",\n")
            json.dump(feature, out, separators=(",", ":"))
            first_feature = False

        total += len(features)
        print(f"Saved {total:,} parcels so far.")

        if len(features) < PAGE_SIZE:
            break

        offset += len(features)
        time.sleep(0.2)

    out.write("\n]}\n")

print()
print("Done.")
print(f"Saved {total:,} parcel features.")
print(f"Output: {OUTPUT.resolve()}")
