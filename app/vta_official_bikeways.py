"""
Official VTA Santa Clara County Bikeways 2023 proximity/index helper.

Primary source:
  ArcGIS item 5225512662264bc6a4ee369e4b3823dc
  "Santa Clara County Bikeways 2023"

The module lazily downloads/caches the public VTA feature layer. If it cannot
reach ArcGIS, EcoTrace can continue using the existing OSM bike model.
"""
from __future__ import annotations

import json
import math
import re
from collections import defaultdict
from pathlib import Path
from typing import Any

import certifi
import requests


ITEM_ID = "5225512662264bc6a4ee369e4b3823dc"
ITEM_META_URL = f"https://www.arcgis.com/sharing/rest/content/items/{ITEM_ID}"
CACHE_PATH = Path("vta_bikeways_2023_cache.json")
USER_AGENT = "EcoTrace-CA17/6.11 student environmental routing project"


def _classify_properties(properties: dict[str, Any]) -> tuple[str, str]:
    values = " | ".join(
        str(value) for value in properties.values()
        if value not in (None, "")
    )
    lowered = values.lower()

    # Prefer explicit Class IV before Class I because "IV" contains "I".
    patterns = [
        ("protected", r"\bclass\s*(iv|4)\b|separated bikeway|cycle\s*track"),
        ("protected", r"\bclass\s*(i|1)\b|bike\s*path|multi[- ]?use\s*path|trail"),
        ("painted", r"\bclass\s*(ii|2)\b|bike\s*lane"),
        ("shared_low_speed", r"\bclass\s*(iii|3)\b|bike\s*route|sharrow|bike\s*boulevard"),
    ]
    for category, pattern in patterns:
        if re.search(pattern, lowered, flags=re.I):
            return category, values[:500]

    # Some ArcGIS layers store only I / II / III / IV in a class field.
    for key, value in properties.items():
        key_lower = str(key).lower()
        if "class" not in key_lower and "type" not in key_lower:
            continue
        normalized = str(value).strip().upper()
        if normalized in {"IV", "4", "CLASS IV"}:
            return "protected", f"{key}: {value}"
        if normalized in {"I", "1", "CLASS I"}:
            return "protected", f"{key}: {value}"
        if normalized in {"II", "2", "CLASS II"}:
            return "painted", f"{key}: {value}"
        if normalized in {"III", "3", "CLASS III"}:
            return "shared_low_speed", f"{key}: {value}"

    return "unknown", values[:500] or "VTA feature with unrecognized class"


class VTABikewayIndex:
    def __init__(self, cache_path: str | Path = CACHE_PATH) -> None:
        self.cache_path = Path(cache_path)
        self.available = False
        self.load_attempted = False
        self.source_note = "VTA Santa Clara County Bikeways 2023"
        self.cell_deg = 0.003
        self.buckets: dict[tuple[int, int], list[dict[str, Any]]] = defaultdict(list)
        self.features: list[dict[str, Any]] = []

        if self.cache_path.exists():
            try:
                self._load_cache()
            except Exception:
                pass

    def _key(self, lat: float, lon: float) -> tuple[int, int]:
        return math.floor(lat / self.cell_deg), math.floor(lon / self.cell_deg)

    @staticmethod
    def _distance_m(
        latitude: float,
        longitude: float,
        a: list[float],
        b: list[float],
    ) -> float:
        mean_lat = math.radians((latitude + a[1] + b[1]) / 3.0)
        sx = 111_320.0 * max(math.cos(mean_lat), 0.2)
        sy = 111_320.0
        px, py = longitude * sx, latitude * sy
        ax, ay = a[0] * sx, a[1] * sy
        bx, by = b[0] * sx, b[1] * sy
        dx, dy = bx - ax, by - ay
        denom = dx * dx + dy * dy
        if denom <= 0:
            return math.hypot(px - ax, py - ay)
        t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / denom))
        return math.hypot(px - (ax + t * dx), py - (ay + t * dy))

    def _index_line(
        self,
        coordinates: list[list[float]],
        properties: dict[str, Any],
    ) -> None:
        if len(coordinates) < 2:
            return
        category, explanation = _classify_properties(properties)
        feature = {
            "coordinates": coordinates,
            "category": category,
            "explanation": explanation,
            "properties": properties,
        }
        self.features.append(feature)

        min_lon = min(p[0] for p in coordinates)
        max_lon = max(p[0] for p in coordinates)
        min_lat = min(p[1] for p in coordinates)
        max_lat = max(p[1] for p in coordinates)
        lo = self._key(min_lat, min_lon)
        hi = self._key(max_lat, max_lon)
        for x in range(lo[0] - 1, hi[0] + 2):
            for y in range(lo[1] - 1, hi[1] + 2):
                self.buckets[(x, y)].append(feature)

    def _load_payload(self, payload: dict[str, Any]) -> None:
        self.features = []
        self.buckets.clear()
        for item in payload.get("features", []):
            properties = item.get("properties") or item.get("attributes") or {}
            geometry = item.get("geometry") or {}

            if geometry.get("type") == "LineString":
                self._index_line(geometry.get("coordinates") or [], properties)
            elif geometry.get("type") == "MultiLineString":
                for line in geometry.get("coordinates") or []:
                    self._index_line(line, properties)
            elif isinstance(geometry.get("paths"), list):
                for line in geometry.get("paths") or []:
                    self._index_line(line, properties)

        self.available = bool(self.features)
        self.load_attempted = True

    def _load_cache(self) -> None:
        payload = json.loads(self.cache_path.read_text(encoding="utf-8"))
        self._load_payload(payload)

    def _service_layer_url(self) -> str:
        response = requests.get(
            ITEM_META_URL,
            params={"f": "json"},
            headers={"User-Agent": USER_AGENT},
            timeout=(5, 15),
            verify=certifi.where(),
        )
        response.raise_for_status()
        metadata = response.json()
        service_url = str(metadata.get("url") or "").rstrip("/")
        if not service_url:
            raise RuntimeError("VTA ArcGIS item did not expose a feature service URL.")

        if re.search(r"/FeatureServer/\d+$", service_url, flags=re.I):
            return service_url

        if service_url.lower().endswith("/featureserver"):
            info = requests.get(
                service_url,
                params={"f": "json"},
                headers={"User-Agent": USER_AGENT},
                timeout=(5, 15),
                verify=certifi.where(),
            )
            info.raise_for_status()
            layers = info.json().get("layers") or []
            if not layers:
                raise RuntimeError("VTA FeatureServer contained no layers.")
            return f"{service_url}/{layers[0]['id']}"

        return service_url

    def refresh(self) -> None:
        layer_url = self._service_layer_url()
        all_features: list[dict[str, Any]] = []
        offset = 0
        page_size = 1500

        while True:
            response = requests.get(
                f"{layer_url}/query",
                params={
                    "where": "1=1",
                    "outFields": "*",
                    "returnGeometry": "true",
                    "outSR": "4326",
                    "f": "json",
                    "resultOffset": offset,
                    "resultRecordCount": page_size,
                },
                headers={"User-Agent": USER_AGENT},
                timeout=(8, 30),
                verify=certifi.where(),
            )
            response.raise_for_status()
            page = response.json()
            if page.get("error"):
                raise RuntimeError(str(page["error"]))
            features = page.get("features") or []
            all_features.extend(features)
            if len(features) < page_size:
                break
            offset += len(features)
            if offset > 50_000:
                break

        payload = {
            "source": self.source_note,
            "arcgis_item_id": ITEM_ID,
            "features": all_features,
        }
        self.cache_path.write_text(json.dumps(payload), encoding="utf-8")
        self._load_payload(payload)

    def ensure_loaded(self) -> bool:
        if self.available:
            return True
        if self.load_attempted:
            return False
        self.load_attempted = True
        try:
            self.refresh()
        except Exception:
            self.available = False
        return self.available

    def classify_point(
        self,
        latitude: float,
        longitude: float,
        tolerance_m: float = 20.0,
    ) -> tuple[str, str]:
        if not self.ensure_loaded():
            return "unknown", "Official VTA 2023 bikeway layer unavailable"

        candidates = self.buckets.get(self._key(latitude, longitude), [])
        best: tuple[float, dict[str, Any]] | None = None
        for feature in candidates:
            coords = feature["coordinates"]
            for a, b in zip(coords, coords[1:]):
                distance = self._distance_m(latitude, longitude, a, b)
                if best is None or distance < best[0]:
                    best = (distance, feature)

        if best is None or best[0] > tolerance_m:
            return "unknown", "No VTA 2023 bikeway feature matched within 20 m"

        feature = best[1]
        return feature["category"], (
            f"VTA 2023 GIS • {feature['explanation']} • "
            f"{best[0]:.1f} m spatial match"
        )

    def analyze_route(self, coordinates: list[list[float]]) -> dict[str, Any]:
        categories = {
            "protected": 0.0,
            "painted": 0.0,
            "shared_low_speed": 0.0,
            "shared_road": 0.0,
            "unknown": 0.0,
        }
        segments: list[dict[str, Any]] = []
        total = 0.0

        for a, b in zip(coordinates, coordinates[1:]):
            if len(a) < 2 or len(b) < 2:
                continue
            mid_lat = (float(a[1]) + float(b[1])) / 2
            mid_lon = (float(a[0]) + float(b[0])) / 2
            category, explanation = self.classify_point(mid_lat, mid_lon)

            mean_lat = math.radians((float(a[1]) + float(b[1])) / 2)
            dx = (float(b[0]) - float(a[0])) * 111_320 * max(math.cos(mean_lat), 0.2)
            dy = (float(b[1]) - float(a[1])) * 111_320
            length = math.hypot(dx, dy)
            total += length
            categories[category] = categories.get(category, 0.0) + length
            segments.append({
                "coordinates": [a, b],
                "category": category,
                "explanation": explanation,
            })

        def pct(category: str) -> float:
            return round(categories.get(category, 0.0) / total * 100, 1) if total else 0.0

        return {
            "data_available": self.available,
            "source": self.source_note if self.available else "VTA unavailable",
            "official_vta_2023": self.available,
            "protected_percent": pct("protected"),
            "painted_percent": pct("painted"),
            "shared_low_speed_percent": pct("shared_low_speed"),
            "shared_road_percent": pct("shared_road"),
            "unknown_percent": pct("unknown"),
            "dedicated_infrastructure_percent": round(
                pct("protected") + pct("painted"), 1
            ),
            "segments": segments,
        }
