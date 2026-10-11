from __future__ import annotations

import math
import re
import time
from functools import lru_cache
from typing import Any

import certifi
import requests
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

router = APIRouter(prefix="/api/vta-bike", tags=["vta-bike"])

ITEM_ID = "5225512662264bc6a4ee369e4b3823dc"
ITEM_URL = f"https://www.arcgis.com/sharing/rest/content/items/{ITEM_ID}"
USER_AGENT = "EcoTrace-CA17/6.13 student bike-routing project"

_CACHE: dict[str, tuple[float, Any]] = {}


class VtaRouteRequest(BaseModel):
    fastest_coordinates: list[list[float]] = Field(min_length=2)
    cooler_coordinates: list[list[float]] = Field(min_length=2)


def _distance_m(lat: float, lon: float, a: list[float], b: list[float]) -> float:
    mean_lat = math.radians((lat + float(a[1]) + float(b[1])) / 3.0)
    sx = 111_320.0 * max(math.cos(mean_lat), 0.2)
    sy = 111_320.0
    px, py = lon * sx, lat * sy
    ax, ay = float(a[0]) * sx, float(a[1]) * sy
    bx, by = float(b[0]) * sx, float(b[1]) * sy
    dx, dy = bx - ax, by - ay
    denom = dx * dx + dy * dy
    if denom <= 0:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / denom))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def _segment_length_m(a: list[float], b: list[float]) -> float:
    mean_lat = math.radians((float(a[1]) + float(b[1])) / 2.0)
    dx = (float(b[0]) - float(a[0])) * 111_320.0 * max(math.cos(mean_lat), 0.2)
    dy = (float(b[1]) - float(a[1])) * 111_320.0
    return math.hypot(dx, dy)


def _classify(props: dict[str, Any]) -> tuple[str, str, str]:
    chunks = []
    for key, value in props.items():
        if value in (None, ""):
            continue
        chunks.append(f"{key}={value}")
    text = " | ".join(chunks)
    lowered = text.lower()

    # Explicit class values first.
    if re.search(r"\bclass\s*(iv|4)\b", lowered) or "separated bikeway" in lowered or "cycle track" in lowered:
        return "protected", "Class IV", text[:600]
    if re.search(r"\bclass\s*(i|1)\b", lowered) or "bike path" in lowered or "multi-use path" in lowered or "shared-use path" in lowered:
        return "protected", "Class I", text[:600]
    if re.search(r"\bclass\s*(ii|2)\b", lowered) or "bike lane" in lowered:
        return "painted", "Class II", text[:600]
    if re.search(r"\bclass\s*(iii|3)\b", lowered) or "sharrow" in lowered or "bike route" in lowered or "bike boulevard" in lowered:
        return "shared_low_speed", "Class III", text[:600]

    # Try bare Roman/Arabic class fields.
    for key, value in props.items():
        k = str(key).lower()
        if "class" not in k and "type" not in k and "facility" not in k:
            continue
        v = str(value).strip().upper()
        if v in {"IV", "4", "CLASS IV"}:
            return "protected", "Class IV", f"{key}={value}"
        if v in {"I", "1", "CLASS I"}:
            return "protected", "Class I", f"{key}={value}"
        if v in {"II", "2", "CLASS II"}:
            return "painted", "Class II", f"{key}={value}"
        if v in {"III", "3", "CLASS III"}:
            return "shared_low_speed", "Class III", f"{key}={value}"

    return "unknown", "Unclassified VTA feature", text[:600]


@lru_cache(maxsize=1)
def _layer_url() -> str:
    r = requests.get(
        ITEM_URL,
        params={"f": "json"},
        timeout=(5, 15),
        verify=certifi.where(),
        headers={"User-Agent": USER_AGENT},
    )
    r.raise_for_status()
    meta = r.json()
    service = str(meta.get("url") or "").rstrip("/")
    if not service:
        raise RuntimeError("VTA ArcGIS item did not expose a feature-service URL.")

    if re.search(r"/FeatureServer/\d+$", service, re.I):
        return service

    if service.lower().endswith("/featureserver"):
        info = requests.get(
            service,
            params={"f": "json"},
            timeout=(5, 15),
            verify=certifi.where(),
            headers={"User-Agent": USER_AGENT},
        )
        info.raise_for_status()
        layers = info.json().get("layers") or []
        if not layers:
            raise RuntimeError("VTA FeatureServer exposed no layers.")
        return f"{service}/{layers[0]['id']}"

    return service


def _route_bbox(routes: list[list[list[float]]]) -> tuple[float, float, float, float]:
    pts = [p for route in routes for p in route if len(p) >= 2]
    min_lon = min(float(p[0]) for p in pts) - 0.003
    min_lat = min(float(p[1]) for p in pts) - 0.003
    max_lon = max(float(p[0]) for p in pts) + 0.003
    max_lat = max(float(p[1]) for p in pts) + 0.003
    return min_lon, min_lat, max_lon, max_lat


def _fetch_features(routes: list[list[list[float]]]) -> list[dict[str, Any]]:
    bbox = _route_bbox(routes)
    key = ",".join(f"{v:.4f}" for v in bbox)
    cached = _CACHE.get(key)
    if cached and time.time() - cached[0] < 900:
        return cached[1]

    layer = _layer_url()
    offset = 0
    page_size = 2000
    features: list[dict[str, Any]] = []
    geometry = ",".join(str(v) for v in bbox)

    while True:
        r = requests.get(
            f"{layer}/query",
            params={
                "where": "1=1",
                "geometry": geometry,
                "geometryType": "esriGeometryEnvelope",
                "inSR": "4326",
                "spatialRel": "esriSpatialRelIntersects",
                "outFields": "*",
                "returnGeometry": "true",
                "outSR": "4326",
                "f": "geojson",
                "resultOffset": offset,
                "resultRecordCount": page_size,
            },
            timeout=(6, 25),
            verify=certifi.where(),
            headers={"User-Agent": USER_AGENT},
        )
        r.raise_for_status()
        payload = r.json()
        if payload.get("error"):
            raise RuntimeError(str(payload["error"]))
        page = payload.get("features") or []
        features.extend(page)
        if len(page) < page_size:
            break
        offset += len(page)
        if offset > 20000:
            break

    _CACHE[key] = (time.time(), features)
    return features


def _line_parts(feature: dict[str, Any]) -> list[list[list[float]]]:
    geom = feature.get("geometry") or {}
    if geom.get("type") == "LineString":
        return [geom.get("coordinates") or []]
    if geom.get("type") == "MultiLineString":
        return geom.get("coordinates") or []
    return []


def _analyze_route(route: list[list[float]], features: list[dict[str, Any]]) -> dict[str, Any]:
    indexed = []
    for feature in features:
        props = feature.get("properties") or {}
        category, vta_class, raw = _classify(props)
        for line in _line_parts(feature):
            if len(line) >= 2:
                indexed.append((category, vta_class, raw, line))

    totals = {"protected": 0.0, "painted": 0.0, "shared_low_speed": 0.0, "shared_road": 0.0, "unknown": 0.0}
    segments = []
    total_len = 0.0

    for a, b in zip(route, route[1:]):
        if len(a) < 2 or len(b) < 2:
            continue
        length = _segment_length_m(a, b)
        total_len += length
        lat = (float(a[1]) + float(b[1])) / 2.0
        lon = (float(a[0]) + float(b[0])) / 2.0

        best = None
        for category, vta_class, raw, line in indexed:
            # cheap bbox rejection around midpoint
            xs = [float(p[0]) for p in line]
            ys = [float(p[1]) for p in line]
            if lon < min(xs)-0.0004 or lon > max(xs)+0.0004 or lat < min(ys)-0.0004 or lat > max(ys)+0.0004:
                continue
            for x, y in zip(line, line[1:]):
                d = _distance_m(lat, lon, x, y)
                if best is None or d < best[0]:
                    best = (d, category, vta_class, raw)

        if best is not None and best[0] <= 25.0:
            _, category, vta_class, raw = best
            explanation = f"VTA 2023 {vta_class} • {best[0]:.1f} m spatial match"
            if raw:
                explanation += f" • {raw[:180]}"
        else:
            category = "unknown"
            vta_class = "No VTA match"
            explanation = "No VTA 2023 bikeway feature matched this route segment within 25 m"

        totals[category] = totals.get(category, 0.0) + length
        segments.append({
            "coordinates": [a, b],
            "category": category,
            "vta_class": vta_class,
            "explanation": explanation,
        })

    def pct(key: str) -> float:
        return round(totals.get(key, 0.0) / total_len * 100.0, 1) if total_len else 0.0

    protected = pct("protected")
    painted = pct("painted")
    shared = pct("shared_low_speed")
    unknown = pct("unknown")

    return {
        "data_available": True,
        "source": "Official VTA Santa Clara County Bikeways 2023 GIS",
        "official_vta_2023": True,
        "protected_percent": protected,
        "painted_percent": painted,
        "shared_low_speed_percent": shared,
        "shared_road_percent": pct("shared_road"),
        "unknown_percent": unknown,
        "dedicated_infrastructure_percent": round(protected + painted, 1),
        "classified_percent": round(max(0.0, 100.0 - unknown), 1),
        "segments": segments,
    }


@router.post("/classify")
async def classify_routes(payload: VtaRouteRequest) -> dict[str, Any]:
    try:
        features = _fetch_features([
            payload.fastest_coordinates,
            payload.cooler_coordinates,
        ])
        return {
            "source": "VTA Santa Clara County Bikeways 2023",
            "official": True,
            "feature_count_near_routes": len(features),
            "fastest_infrastructure": _analyze_route(payload.fastest_coordinates, features),
            "cooler_infrastructure": _analyze_route(payload.cooler_coordinates, features),
        }
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail=f"VTA 2023 GIS classification unavailable: {exc}",
        ) from exc
