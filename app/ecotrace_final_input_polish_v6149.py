from __future__ import annotations
from ecotrace_visual_filter_context import visual_filter_context
import sqlite3
import secrets
import html
import hmac
import hashlib
from pathlib import Path
from ecotrace_route_performance import (
    route_work as _eco_route_work, memoized_work as _eco_memo,
    ors_timeout as _eco_ors_timeout, route_weather as _eco_route_weather,
)

import heapq
import json
import math
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from functools import lru_cache

import certifi
import requests
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request, Form
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from pydantic import BaseModel, Field
# EcoTrace 3,000-site build.
# One authoritative planting-site database is used throughout the website.
_experimental_site_db = Path(__file__).resolve().parent / "ecotrace_reviewed_tree_sites.json"
os.environ["ECOTRACE_SITE_DB"] = str(_experimental_site_db)


from ecotrace_tree_details import router as proposed_sites_router, public_tree_details
# ECOTRACE_TREE_WEBSITE_V6
from canopy_engine import CanopyEngine
from ecotrace_environmental_layers import WRI_CANOPY, environmental_status
from bike_infrastructure_snap_fixed import BikeInfrastructureIndex
from community_hub_v6_preserve_posts import router as community_router
from upload_backend import router as upload_router, UPLOAD_ROOT
from tree_guide_backend_v2 import router as tree_guide_router
from ewaste_backend_v7 import router as ewaste_router
from gemini_ewaste_backend_age_forceestimate import router as gemini_ewaste_router
from gemini_assistant_backend_v3 import router as gemini_assistant_router
from community_context_backend import router as community_context_router
from vta_bikeways_runtime import router as vta_bike_router
from gemini_ewaste_advice_v1 import router as gemini_ewaste_advice_router

app = FastAPI(
    title="EcoTrace",
    description="Hyper-local heat, shade-routing, and tree-canopy planning prototype for CA-17.",
    version="6.14.9-final-input-polish",
)

app.mount("/tree-evidence", StaticFiles(directory=str(Path(__file__).resolve().parent / "tree_visual_evidence")), name="ecotrace-tree-evidence")

app.include_router(proposed_sites_router)
app.include_router(community_router)
app.include_router(upload_router)
app.include_router(ewaste_router)
app.include_router(gemini_ewaste_router)
app.include_router(gemini_assistant_router)
app.include_router(community_context_router)
app.include_router(vta_bike_router)
app.include_router(gemini_ewaste_advice_router)
app.include_router(tree_guide_router)
app.mount('/uploads', StaticFiles(directory=str(UPLOAD_ROOT)), name='uploads')

MAX_TREE_RECOMMENDATION_DISTANCE_M = 800

DISTRICT_BOUNDS = {
    "min_lat": 37.20,
    "max_lat": 37.55,
    "min_lon": -122.20,
    "max_lon": -121.75,
}

def _first_existing_path(*candidates: str) -> str:
    for candidate in candidates:
        path = Path(candidate)
        if path.exists():
            return str(path)
    return candidates[0]


def _default_canopy_model_path() -> str:
    # Prefer the new model that combines the ArcGIS-enhanced tree inventory
    # with official Santa Clara County building footprints/heights.
    return _first_existing_path(
        "canopy_model_county_buildings.json",
        "../canopy_model_county_buildings.json",
        "canopy_model_arcgis.json",
        "../canopy_model_arcgis.json",
        "canopy_model.json",
        "../canopy_model.json",
    )


CANOPY_MODEL_PATH = os.getenv(
    "ECOTRACE_CANOPY_MODEL",
    _default_canopy_model_path(),
)
CANOPY_ENGINE = CanopyEngine(CANOPY_MODEL_PATH)
BIKE_INFRASTRUCTURE_PATH = os.getenv(
    "ECOTRACE_BIKE_INFRASTRUCTURE",
    _first_existing_path(
        "bike_infrastructure.json",
        "../bike_infrastructure.json",
    ),
)
BIKE_INFRASTRUCTURE = BikeInfrastructureIndex(BIKE_INFRASTRUCTURE_PATH)


class PlantingSpot(BaseModel):
    latitude: float
    longitude: float
    suitability: str
    description: str
    reason: str
    distance_meters: int
    nearby_place: str
    within_preferred_radius: bool
    factors: list[str]


class ExposureSegment(BaseModel):
    shade_applicable: bool = True
    coordinates: list[list[float]]
    category: str
    shade_percent: float
    canopy_shadow_percent: float = 0.0
    building_shadow_percent: float = 0.0
    terrain_shadow_percent: float = 0.0
    heat_penalty_percent: float = 0.0
    solar_altitude_degrees: float = 0.0
    solar_azimuth_degrees: float = 0.0
    tree_count_considered: int = 0
    building_count_considered: int = 0
    wri_canopy_height_m: float | None = None
    wri_canopy_fraction: float | None = None
    reason: str
    data_quality: str = "fallback model"


class ZoneAnalysis(BaseModel):
    clicked_latitude: float
    clicked_longitude: float
    zone_name: str
    zone_type: str
    heat_index: int = Field(ge=0, le=100)
    estimated_temperature_f: float
    priority: str
    message: str
    planting_spots: list[PlantingSpot]
    nearest_outside_range: PlantingSpot | None = None


class RouteAnalysis(BaseModel):
    nighttime: bool = False
    shade_applicable: bool = True
    lighting_note: str = ""
    shortest_coordinates: list[list[float]]
    shadiest_coordinates: list[list[float]]
    shortest_minutes: float
    shadiest_minutes: float
    shortest_distance_km: float
    shadiest_distance_km: float
    shortest_distance_miles: float
    shadiest_distance_miles: float
    shortest_shade_percent: float
    shadiest_shade_percent: float
    shade_gain_percent: float
    extra_minutes: float
    detour_percent: float
    detour_cap_percent: float
    snapped_start: list[float]
    snapped_end: list[float]
    message: str
    personalized_pace_mph: float
    departure_time: str
    shortest_segments: list[ExposureSegment]
    shadiest_segments: list[ExposureSegment]
    time_of_day_used: bool
    pace_warning: str | None = None
    weather: WeatherSnapshot | None = None
    route_limit_mode: str
    route_limit_description: str
    shortest_route_why: str
    shadiest_route_why: str
    shortest_canopy_percent: float
    shadiest_canopy_percent: float
    shortest_building_shadow_percent: float
    shadiest_building_shadow_percent: float
    shortest_terrain_shadow_percent: float
    shadiest_terrain_shadow_percent: float
    canopy_model_available: bool
    canopy_model_summary: str
    user_profile: str
    profile_guidance: str
    meaningful_shadier_route: bool


class BikeRouteAnalysis(BaseModel):
    nighttime: bool = False
    shade_applicable: bool = True
    lighting_note: str = ""
    fastest_coordinates: list[list[float]]
    cooler_coordinates: list[list[float]]
    fastest_minutes: float
    cooler_minutes: float
    fastest_distance_miles: float
    cooler_distance_miles: float
    fastest_distance_km: float
    cooler_distance_km: float
    fastest_shade_score: float
    cooler_shade_score: float
    fastest_tree_shadow_percent: float = 0.0
    cooler_tree_shadow_percent: float = 0.0
    fastest_building_shadow_percent: float = 0.0
    cooler_building_shadow_percent: float = 0.0
    meaningful_cooler_route: bool = True
    shade_gain: float
    extra_minutes: float
    detour_percent: float
    bike_speed_mph: float
    bike_type: str
    fastest_infrastructure: dict[str, Any]
    cooler_infrastructure: dict[str, Any]
    fastest_why: str
    cooler_why: str
    route_limit_description: str
    departure_time: str
    time_of_day_used: bool
    warning: str | None = None
    weather: WeatherSnapshot | None = None


class RoutePlantingRequest(BaseModel):
    coordinates: list[list[float]]
    buffer_meters: float = Field(default=75.0, ge=15.0, le=200.0)
    max_sites: int = Field(default=30, ge=1, le=60)



class WeatherSnapshot(BaseModel):
    available: bool
    source: str
    temperature_f: float | None = None
    apparent_temperature_f: float | None = None
    wind_speed_mph: float | None = None
    wind_direction: str | None = None
    precipitation_probability: int | None = None
    short_forecast: str | None = None
    period_name: str | None = None
    observed_at: str | None = None
    observation_station: str | None = None
    impact_summary: str


class GeocodeResult(BaseModel):
    query: str
    latitude: float
    longitude: float
    display_name: str
    source: str


HEAT_ANCHORS: list[dict[str, Any]] = [
    {"name": "North San José", "lat": 37.405, "lon": -121.936, "heat": 94, "temp_f": 97.0},
    {"name": "Milpitas", "lat": 37.432, "lon": -121.900, "heat": 88, "temp_f": 94.0},
    {"name": "Santa Clara", "lat": 37.360, "lon": -121.972, "heat": 79, "temp_f": 90.0},
    {"name": "Sunnyvale", "lat": 37.385, "lon": -122.025, "heat": 72, "temp_f": 87.0},
    {"name": "Cupertino", "lat": 37.323, "lon": -122.052, "heat": 38, "temp_f": 80.0},
]

LOCAL_GEOCODER: dict[str, tuple[float, float, str]] = {
    "levi's stadium": (37.4033, -121.9694, "Levi's Stadium, Santa Clara, CA"),
    "levis stadium": (37.4033, -121.9694, "Levi's Stadium, Santa Clara, CA"),
    "santa clara university": (37.3496, -121.9390, "Santa Clara University, Santa Clara, CA"),
    "santa clara high school": (37.34455, -121.98255, "Santa Clara High School, 3000 Benton Street, Santa Clara, CA 95051"),
    "santa clara high": (37.34455, -121.98255, "Santa Clara High School, 3000 Benton Street, Santa Clara, CA 95051"),
    "schs": (37.34455, -121.98255, "Santa Clara High School, 3000 Benton Street, Santa Clara, CA 95051"),
    "wilcox": (37.37115, -121.99470, "Adrian C. Wilcox High School, Santa Clara, CA"),
    "wilcox high": (37.37115, -121.99470, "Adrian C. Wilcox High School, Santa Clara, CA"),
    "adrian wilcox high school": (37.37115, -121.99470, "Adrian C. Wilcox High School, Santa Clara, CA"),
    "muir": (37.30978, -122.01155, "John Muir Elementary School, Cupertino, CA"),
    "muir elementary": (37.30978, -122.01155, "John Muir Elementary School, Cupertino, CA"),
    "john muir elementary": (37.30978, -122.01155, "John Muir Elementary School, Cupertino, CA"),
    "apple park": (37.3349, -122.0090, "Apple Park, Cupertino, CA"),
    "cupertino library": (37.3188, -122.0286, "Cupertino Library, Cupertino, CA"),
    "milpitas bart": (37.4103, -121.8914, "Milpitas BART Station, Milpitas, CA"),
    "great mall": (37.4143, -121.8970, "Great Mall, Milpitas, CA"),
    "sunnyvale caltrain": (37.3787, -122.0308, "Sunnyvale Caltrain Station, Sunnyvale, CA"),
    "mission college": (37.3913, -121.9821, "Mission College, Santa Clara, CA"),
    "sunnyvale middle": (37.361944, -122.050833, "Sunnyvale Middle School, 1080 Mango Avenue, Sunnyvale, CA 94087"),
    "sunnyvale middle school": (37.361944, -122.050833, "Sunnyvale Middle School, 1080 Mango Avenue, Sunnyvale, CA 94087"),
    "bracher": (37.366759, -121.975759, "Bracher Elementary School, 2700 Chromite Drive, Santa Clara, CA 95051"),
    "bracher elementary": (37.366759, -121.975759, "Bracher Elementary School, 2700 Chromite Drive, Santa Clara, CA 95051"),
    "bracher elementary school": (37.366759, -121.975759, "Bracher Elementary School, 2700 Chromite Drive, Santa Clara, CA 95051"),
    "cabrillo middle": (37.3494, -121.9877, "Cabrillo Middle School, 2550 Cabrillo Avenue, Santa Clara, CA"),
    "juan cabrillo middle": (37.3494, -121.9877, "Cabrillo Middle School, 2550 Cabrillo Avenue, Santa Clara, CA"),
    "juan cabrillo middle school": (37.3494, -121.9877, "Cabrillo Middle School, 2550 Cabrillo Avenue, Santa Clara, CA"),
    "central park": (37.342640, -121.976980, "Central Park, 909 Kiely Boulevard, Santa Clara, CA 95051"),
    "santa clara central park": (37.342640, -121.976980, "Central Park, 909 Kiely Boulevard, Santa Clara, CA 95051"),
    "central park library": (37.3422, -121.9749, "Central Park Library, 2635 Homestead Road, Santa Clara, CA 95051"),
    "santa clara central park library": (37.3422, -121.9749, "Central Park Library, 2635 Homestead Road, Santa Clara, CA 95051"),
    "briarwood elementary": (37.3717, -121.9586, "Briarwood Elementary School, 1930 Townsend Avenue, Santa Clara, CA 95051"),
    "briarwood elementary school": (37.3717, -121.9586, "Briarwood Elementary School, 1930 Townsend Avenue, Santa Clara, CA 95051"),
    "great america": (37.3957, -121.9720, "California's Great America, 4701 Great America Parkway, Santa Clara, CA 95054"),
    "california's great america": (37.3957, -121.9720, "California's Great America, 4701 Great America Parkway, Santa Clara, CA 95054"),
    "rivermark plaza": (37.3955, -121.9475, "Rivermark Plaza, Santa Clara, CA"),
    "north san jose": (37.4050, -121.9360, "North San José, CA"),
    "santa clara": (37.3541, -121.9552, "Santa Clara, CA"),
    "sunnyvale": (37.3688, -122.0363, "Sunnyvale, CA"),
    "cupertino": (37.3230, -122.0322, "Cupertino, CA"),
    "milpitas": (37.4323, -121.8996, "Milpitas, CA"),
}

# Simplified physical structures used by the prototype validator.
BUILDING_FOOTPRINTS: list[list[list[float]]] = [
    [[-121.9460, 37.4015], [-121.9390, 37.4015], [-121.9390, 37.3960], [-121.9460, 37.3960], [-121.9460, 37.4015]],
    [[-121.9070, 37.4385], [-121.8980, 37.4385], [-121.8980, 37.4315], [-121.9070, 37.4315], [-121.9070, 37.4385]],
    [[-121.9805, 37.3685], [-121.9710, 37.3685], [-121.9710, 37.3610], [-121.9805, 37.3610], [-121.9805, 37.3685]],
    [[-122.0410, 37.3935], [-122.0310, 37.3935], [-122.0310, 37.3850], [-122.0410, 37.3850], [-122.0410, 37.3935]],
    [[-122.0595, 37.3300], [-122.0520, 37.3300], [-122.0520, 37.3235], [-122.0595, 37.3235], [-122.0595, 37.3300]],
]

# ---------------------------------------------------------------------------
# AUTHORITATIVE 3,000-SITE PLANTING DATABASE
# ---------------------------------------------------------------------------

def _read_site_records(path: Path) -> list[dict[str, Any]]:
    """
    Load EcoTrace planting-site records from JSON.

    Supported layouts:
      {"sites": [...]}
      {"candidates": [...]}
      {"features": [...]}   # GeoJSON-like records
      [...]
    """
    if not path.exists():
        raise RuntimeError(
            f"EcoTrace planting-site database not found: {path}. "
            "Put experimental_tree_sites_fusion_v2.json in the same folder as "
            "this app before starting EcoTrace."
        )

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(
            f"Could not read planting-site database {path}: {error}"
        ) from error

    if isinstance(payload, list):
        rows = payload
    elif isinstance(payload, dict):
        rows = (
            payload.get("sites")
            or payload.get("candidates")
            or payload.get("features")
            or []
        )
    else:
        rows = []

    if not isinstance(rows, list):
        raise RuntimeError(
            f"Planting-site database {path} does not contain a site list."
        )

    return [row for row in rows if isinstance(row, dict)]


def _site_lat_lon(record: dict[str, Any]) -> tuple[float, float] | None:
    """Extract latitude/longitude from normal JSON or GeoJSON-like records."""
    lat = record.get("latitude", record.get("lat"))
    lon = record.get("longitude", record.get("lon", record.get("lng")))

    if lat is None or lon is None:
        geometry = record.get("geometry")
        if isinstance(geometry, dict):
            coordinates = geometry.get("coordinates")
            if (
                geometry.get("type") == "Point"
                and isinstance(coordinates, (list, tuple))
                and len(coordinates) >= 2
            ):
                lon, lat = coordinates[0], coordinates[1]

    try:
        return float(lat), float(lon)
    except (TypeError, ValueError):
        return None


def _clean_area_name(value: Any) -> str:
    text_value = str(value or "").strip()
    lowered = text_value.casefold()

    bad_names = {
        "",
        "unnamed",
        "unnamed street",
        "unnamed mapped street",
        "mapped road",
        "unknown",
        "none",
        "n/a",
    }

    if lowered in bad_names:
        return ""

    if lowered.startswith("unnamed "):
        return ""

    return text_value


def _normalize_authoritative_site(
    record: dict[str, Any],
    index: int,
) -> dict[str, Any] | None:
    coords = _site_lat_lon(record)
    if coords is None:
        return None

    latitude, longitude = coords

    if not record.get("inside_ca17", False) and not (
        DISTRICT_BOUNDS["min_lat"] <= latitude <= DISTRICT_BOUNDS["max_lat"]
        and DISTRICT_BOUNDS["min_lon"] <= longitude <= DISTRICT_BOUNDS["max_lon"]
    ):
        return None

    site_type = str(
        record.get("site_type")
        or record.get("surface_context")
        or record.get("mapped_open_space")
        or "planting opportunity"
    ).strip()

    area_name = _clean_area_name(
        record.get("road_name")
        or record.get("area_name")
        or record.get("where_location")
        or record.get("location_group")
        or record.get("mapped_open_space")
    )

    score_raw = record.get(
        "experimental_fusion_score",
        record.get("screening_score", 0.0),
    )
    try:
        score = float(score_raw)
    except (TypeError, ValueError):
        score = 0.0

    confidence_text = str(record.get("evidence_confidence") or "").casefold()
    if "high" in confidence_text:
        confidence = 0.95
    elif "moderate" in confidence_text:
        confidence = 0.82
    elif "limited" in confidence_text:
        confidence = 0.68
    else:
        # Keep an explicit numerical value if the generator supplied one.
        raw_confidence = record.get(
            "modeled_public_surface_confidence",
            record.get("confidence", 0.80),
        )
        try:
            confidence = float(raw_confidence)
        except (TypeError, ValueError):
            confidence = 0.80
        if confidence > 1.0:
            confidence /= 100.0

    candidate_id = str(
        record.get("site_id")
        or record.get("candidate_id")
        or f"ECO-{index:04d}"
    )

    return {
        "lat": latitude,
        "lon": longitude,
        "type": site_type,
        "area_name": area_name,
        "orientation": str(
            record.get("offset_side")
            or record.get("orientation")
            or record.get("surface_context")
            or "available planting area"
        ).strip(),
        "modeled_public_surface_confidence": max(0.0, min(1.0, confidence)),
        "candidate_id": candidate_id,
        "experimental_fusion_score": score,
        "source_type": str(record.get("source_type") or "environmental_fusion"),
        "why_chosen": str(record.get("why_chosen") or "").strip(),
        "screening_status": str(record.get("screening_status") or "").strip(),
        "evidence_confidence": str(record.get("evidence_confidence") or "").strip(),
        "nearest_road_m": record.get("nearest_road_m"),
        "nearest_path_m": record.get("nearest_path_m"),
        "nearest_existing_tree_m": record.get("nearest_existing_tree_m"),
        "wri_canopy_fraction_10m": record.get("wri_canopy_fraction_10m"),
        "wri_max_canopy_height_m": record.get("wri_max_canopy_height_m"),
        "county_canopy_percent": record.get("county_canopy_percent"),
        "soil_context": record.get("soil_context"),
        "parcel_context": record.get("parcel_context"),
        "original_record": record,
    }


def load_authoritative_planting_sites() -> list[dict[str, Any]]:
    database_path = Path(
        os.getenv("ECOTRACE_SITE_DB", str(_experimental_site_db))
    )

    rows = _read_site_records(database_path)
    normalized: list[dict[str, Any]] = []

    for index, row in enumerate(rows, start=1):
        site = _normalize_authoritative_site(row, index)
        if site is not None:
            normalized.append(site)

    if not normalized:
        raise RuntimeError(
            f"No usable planting sites were loaded from {database_path}."
        )

    print(
        f"Loaded {len(normalized):,} authoritative EcoTrace planting sites "
        f"from {database_path}."
    )
    return normalized


PLANTING_SITES: list[dict[str, Any]] = load_authoritative_planting_sites()

def _tree_qa_haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6_371_008.8
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = (
        math.sin(dp / 2) ** 2
        + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    )
    return 2 * r * math.atan2(math.sqrt(a), math.sqrt(max(0.0, 1.0 - a)))


# Manual QA correction for known invalid planting candidates.
# These are runtime safeguards; patch_levi_tree_sites.py also updates the
# underlying JSON persistently when the user runs it.
LEVI_QA_BAD_POINTS = [
    (37.3289927, -121.9182570, 30.0),
    (37.4049051, -121.9695283, 35.0),
    (37.4018217, -121.9684404, 35.0),
]

# Conservative stadium-core box. It removes candidates on/in the stadium
# structure itself while leaving surrounding road/parkway opportunities alone.
LEVI_STADIUM_CORE = {
    "min_lat": 37.40265,
    "max_lat": 37.40595,
    "min_lon": -121.97225,
    "max_lon": -121.96805,
}

LEVI_QA_REPLACEMENTS = [
    (37.405126, -121.968895),
    (37.399909, -121.969326),
    (37.399759, -121.970143),
    (37.399495, -121.970481),
    (37.399038, -121.970120),
    (37.396759, -121.968728),
]

ROAD_QA_BAD_POINTS = [
    (37.3237312, -121.9059204, 18.0),
    (37.3237214, -121.9061800, 18.0),
    (37.3234294, -121.9059002, 18.0),
]

ROAD_QA_REPLACEMENTS = [
    (37.322936, -121.906175),
    (37.323586, -121.906176),
    (37.323758, -121.905468),
]


def _manual_tree_qa_replacement(index: int, lat: float, lon: float) -> dict[str, Any]:
    return {
        "lat": lat,
        "lon": lon,
        "type": "street-edge planting opportunity",
        "area_name": "Levi's Stadium / Great America corridor",
        "orientation": "manually reviewed roadside planting area",
        "modeled_public_surface_confidence": 0.90,
        "candidate_id": f"MANUAL-LEVI-{index:03d}",
        "experimental_fusion_score": 1.0,
        "source_type": "manual_qa_replacement",
        "why_chosen": (
            "Manually reviewed replacement coordinate added after removing "
            "invalid Levi's Stadium-area candidates."
        ),
        "screening_status": "Manual QA replacement — field/city verification required",
        "evidence_confidence": "manual-reviewed",
        "nearest_road_m": None,
        "nearest_path_m": None,
        "nearest_existing_tree_m": None,
        "wri_canopy_fraction_10m": None,
        "wri_max_canopy_height_m": None,
        "county_canopy_percent": None,
        "soil_context": None,
        "parcel_context": None,
        "original_record": {
            "manual_review_status": "USER_REVIEWED_COORDINATE",
            "source": "manual QA correction",
        },
    }



def _manual_road_qa_replacement(index: int, lat: float, lon: float) -> dict[str, Any]:
    return {
        "lat": lat,
        "lon": lon,
        "type": "street-edge planting opportunity",
        "area_name": "Manually reviewed roadside replacement",
        "orientation": "manually reviewed roadside planting area",
        "modeled_public_surface_confidence": 0.90,
        "candidate_id": f"MANUAL-ROAD-{index:03d}",
        "experimental_fusion_score": 1.0,
        "source_type": "manual_qa_replacement",
        "why_chosen": "Manually reviewed roadside replacement after removing an invalid road-center candidate.",
        "screening_status": "Manual QA replacement — field/city verification required",
        "evidence_confidence": "manual-reviewed",
        "nearest_road_m": None,
        "nearest_path_m": None,
        "nearest_existing_tree_m": None,
        "wri_canopy_fraction_10m": None,
        "wri_max_canopy_height_m": None,
        "county_canopy_percent": None,
        "soil_context": None,
        "parcel_context": None,
        "original_record": {"manual_review_status": "USER_REVIEWED_COORDINATE"},
    }


def _apply_manual_tree_site_qa(sites: list[dict[str, Any]]) -> list[dict[str, Any]]:
    kept: list[dict[str, Any]] = []
    removed = 0

    for site in sites:
        lat = float(site["lat"])
        lon = float(site["lon"])

        remove = any(
            _tree_qa_haversine_m(lat, lon, bad_lat, bad_lon) <= radius_m
            for bad_lat, bad_lon, radius_m in LEVI_QA_BAD_POINTS
        ) or any(
            _tree_qa_haversine_m(lat, lon, bad_lat, bad_lon) <= radius_m
            for bad_lat, bad_lon, radius_m in ROAD_QA_BAD_POINTS
        )

        if (
            LEVI_STADIUM_CORE["min_lat"] <= lat <= LEVI_STADIUM_CORE["max_lat"]
            and LEVI_STADIUM_CORE["min_lon"] <= lon <= LEVI_STADIUM_CORE["max_lon"]
        ):
            remove = True

        label = " ".join(
            str(site.get(key) or "")
            for key in ("area_name", "type", "source_type")
        ).casefold()
        if "levi" in label and _tree_qa_haversine_m(lat, lon, 37.4046, -121.97011) < 450:
            remove = True

        if remove:
            removed += 1
        else:
            kept.append(site)

    for index, (lat, lon) in enumerate(LEVI_QA_REPLACEMENTS, start=1):
        if any(
            _tree_qa_haversine_m(lat, lon, float(site["lat"]), float(site["lon"])) < 8.0
            for site in kept
        ):
            continue
        kept.append(_manual_tree_qa_replacement(index, lat, lon))

    for index, (lat, lon) in enumerate(ROAD_QA_REPLACEMENTS, start=1):
        if any(
            _tree_qa_haversine_m(lat, lon, float(site["lat"]), float(site["lon"])) < 8.0
            for site in kept
        ):
            continue
        kept.append(_manual_road_qa_replacement(index, lat, lon))

    print(
        f"Manual tree QA: removed {removed} invalid Stadium/explicit candidates; "
        f"{len(LEVI_QA_REPLACEMENTS)} reviewed replacement coordinates enabled."
    )
    return kept


# The reviewed dataset is authoritative; do not reinsert old replacement coordinates.



# Connected modeled pedestrian network. Horizontal and vertical links represent
# sidewalk/walkway corridors; road crossings are explicitly tagged crosswalk.
def create_modeled_pedestrian_network() -> tuple[dict[str, tuple[float, float]], list[tuple[str, str, str]]]:
    nodes: dict[str, tuple[float, float]] = {}
    edges: list[tuple[str, str, str]] = []
    rows, cols = 80, 100
    min_lat, max_lat = DISTRICT_BOUNDS["min_lat"], DISTRICT_BOUNDS["max_lat"]
    min_lon, max_lon = DISTRICT_BOUNDS["min_lon"], DISTRICT_BOUNDS["max_lon"]
    for r in range(rows):
        lat = min_lat + (max_lat-min_lat) * r/(rows-1)
        for c in range(cols):
            lon = min_lon + (max_lon-min_lon) * c/(cols-1)
            nodes[f"N{r}_{c}"] = (lat, lon)
    for r in range(rows):
        for c in range(cols):
            here=f"N{r}_{c}"
            if c+1<cols:
                edges.append((here, f"N{r}_{c+1}", "sidewalk"))
            if r+1<rows:
                kind="crosswalk" if (r+c)%3==0 else "walkway"
                edges.append((here, f"N{r+1}_{c}", kind))
    return nodes, edges

NETWORK_NODES, NETWORK_EDGES = create_modeled_pedestrian_network()


def _tree_qa_haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius = 6_371_000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return radius * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def point_in_polygon(longitude: float, latitude: float, polygon: list[list[float]]) -> bool:
    inside = False
    j = len(polygon) - 1
    for i in range(len(polygon)):
        xi, yi = polygon[i]
        xj, yj = polygon[j]
        if (yi > latitude) != (yj > latitude):
            boundary = (xj - xi) * (latitude - yi) / (yj - yi) + xi
            if longitude < boundary:
                inside = not inside
        j = i
    return inside


def is_inside_structure(latitude: float, longitude: float) -> bool:
    return any(point_in_polygon(longitude, latitude, polygon) for polygon in BUILDING_FOOTPRINTS)


def segment_intersects_structure(a: tuple[float, float], b: tuple[float, float]) -> bool:
    for step in range(61):
        t = step / 60
        lat = a[0] + (b[0] - a[0]) * t
        lon = a[1] + (b[1] - a[1]) * t
        if is_inside_structure(lat, lon):
            return True
    return False


def interpolate_heat(latitude: float, longitude: float) -> tuple[float, float, str]:
    weighted_heat = 0.0
    weighted_temp = 0.0
    total_weight = 0.0
    nearest_name = HEAT_ANCHORS[0]["name"]
    nearest_distance = float("inf")

    for anchor in HEAT_ANCHORS:
        distance = max(250.0, _tree_qa_haversine_m(latitude, longitude, anchor["lat"], anchor["lon"]))
        weight = 1.0 / (distance ** 1.65)
        weighted_heat += anchor["heat"] * weight
        weighted_temp += anchor["temp_f"] * weight
        total_weight += weight
        if distance < nearest_distance:
            nearest_distance = distance
            nearest_name = anchor["name"]

    heat = weighted_heat / total_weight
    temperature = weighted_temp / total_weight
    return max(0.0, min(100.0, heat)), temperature, nearest_name


def human_direction(from_lat: float, from_lon: float, to_lat: float, to_lon: float) -> str:
    north = to_lat - from_lat
    east = to_lon - from_lon
    angle = (math.degrees(math.atan2(east, north)) + 360) % 360
    labels = ["north", "northeast", "east", "southeast", "south", "southwest", "west", "northwest"]
    return labels[int((angle + 22.5) // 45) % 8]



@lru_cache(maxsize=1024)
def reverse_geocode_nearby(latitude_5dp: float, longitude_5dp: float) -> str:
    """
    Return a human-readable location for a planting site.

    EcoTrace never intentionally displays labels such as "Unnamed mapped street".
    We prefer:
      1) street address / named road from reverse geocoding,
      2) nearby named place + road,
      3) nearest named mapped way from the local bike/street index,
      4) city/neighborhood fallback.
    """
    fallback_city = "Santa Clara County"

    try:
        response = requests.get(
            "https://nominatim.openstreetmap.org/reverse",
            params={
                "lat": latitude_5dp,
                "lon": longitude_5dp,
                "format": "jsonv2",
                "zoom": 18,
                "addressdetails": 1,
            },
            headers={
                "User-Agent": "EcoTrace-CA17/6.9.9 student planning prototype",
                "Accept-Language": "en",
            },
            timeout=(5, 12),
            verify=certifi.where(),
        )
        response.raise_for_status()
        payload = response.json()
        address = payload.get("address") or {}

        road = (
            address.get("road")
            or address.get("pedestrian")
            or address.get("residential")
            or address.get("cycleway")
            or address.get("footway")
            or address.get("path")
        )
        house_number = str(address.get("house_number") or "").strip()
        city = (
            address.get("city")
            or address.get("town")
            or address.get("village")
            or address.get("municipality")
            or fallback_city
        )
        neighborhood = (
            address.get("neighbourhood")
            or address.get("suburb")
            or address.get("quarter")
        )

        named_place = (
            address.get("park")
            or address.get("amenity")
            or address.get("school")
            or address.get("college")
            or address.get("university")
            or address.get("commercial")
        )

        pieces: list[str] = []

        if road:
            street = f"{house_number} {road}".strip()
            pieces.append(street)
        elif named_place:
            pieces.append(str(named_place))

        if neighborhood and str(neighborhood).casefold() not in {
            piece.casefold() for piece in pieces
        }:
            pieces.append(str(neighborhood))

        if city and str(city).casefold() not in {
            piece.casefold() for piece in pieces
        }:
            pieces.append(str(city))

        if pieces:
            return ", ".join(pieces[:3])

    except (requests.RequestException, ValueError, TypeError):
        pass

    # Offline/local fallback: find a REAL NAMED mapped street.
    try:
        nearest = BIKE_INFRASTRUCTURE.nearest_named_way(
            latitude_5dp,
            longitude_5dp,
            search_radius_m=900.0,
        )
        if nearest:
            road_name = _clean_area_name(nearest.get("name"))
            if road_name:
                distance = float(nearest.get("distance_meters", 0.0))
                if distance <= 50:
                    return f"near {road_name}, {fallback_city}"
                return f"near {road_name}, {fallback_city} ({distance:.0f} m away)"
    except Exception:
        pass

    return fallback_city


def planting_site_location(site: dict[str, Any]) -> str:
    return public_tree_details(site)['where_location']

def make_planting_spot(
    site: dict[str, Any],
    origin_latitude: float,
    origin_longitude: float,
    *,
    within_preferred_radius: bool,
) -> PlantingSpot:
    distance = round(
        _tree_qa_haversine_m(
            origin_latitude,
            origin_longitude,
            site["lat"],
            site["lon"],
        )
    )
    direction = human_direction(
        origin_latitude,
        origin_longitude,
        site["lat"],
        site["lon"],
    )
    miles = distance / 1609.344

    location = planting_site_location(site)

    confidence = int(
        round(site.get("modeled_public_surface_confidence", 0.8) * 100)
    )

    site_type = str(site.get("type") or "planting opportunity")
    candidate_id = str(site.get("candidate_id") or "unlabeled")

    factors: list[str] = [
        f"EcoTrace candidate {candidate_id}",
        f"Mapped category: {site_type}",
        'Evidence: AI review of dated imagery; field conditions and permission unverified',
        f"{distance} meters from the selected point",
    ]

    nearest_road = site.get("nearest_road_m")
    if nearest_road is not None:
        try:
            factors.append(f"{float(nearest_road):.1f} m from mapped road geometry")
        except (TypeError, ValueError):
            pass

    nearest_path = site.get("nearest_path_m")
    if nearest_path is not None:
        try:
            factors.append(f"{float(nearest_path):.1f} m from mapped pedestrian path")
        except (TypeError, ValueError):
            pass

    nearest_tree = site.get("nearest_existing_tree_m")
    if nearest_tree is not None:
        try:
            factors.append(f"{float(nearest_tree):.1f} m from nearest mapped existing tree")
        except (TypeError, ValueError):
            pass

    canopy_fraction = site.get("wri_canopy_fraction_10m")
    if canopy_fraction is not None:
        try:
            canopy_pct = float(canopy_fraction) * 100.0
            factors.append(f"WRI/Meta local canopy: {canopy_pct:.0f}%")
        except (TypeError, ValueError):
            pass

    evidence_confidence = str(site.get("evidence_confidence") or "").strip()
    if evidence_confidence:
        factors.append(evidence_confidence)

    screening_status = str(site.get("screening_status") or "").strip()
    if screening_status:
        factors.append(screening_status)

    range_text = (
        "within the 800-meter search radius"
        if within_preferred_radius
        else "the nearest available candidate outside the 800-meter search radius"
    )

    why_chosen = str(site.get("why_chosen") or "").strip()
    if not why_chosen:
        why_chosen = (
            "This location survived EcoTrace's mapped planting-site screening."
        )

    return PlantingSpot(
        latitude=round(site["lat"], 6),
        longitude=round(site["lon"], 6),
        suitability=site_type,
        description=(
            f"{location}. Approximately {distance} meters ({miles:.2f} miles) "
            f"{direction} of the selected point."
        ),
        reason=(
            f"This is {range_text}. {why_chosen} "
            "It is a planning candidate, not a city-approved planting location."
        ),
        distance_meters=distance,
        nearby_place=location,
        within_preferred_radius=within_preferred_radius,
        factors=factors,
    )

def valid_planting_site(
    site: dict[str, Any],
    existing: list[PlantingSpot],
) -> bool:
    """
    The 3,000-site database is already screened by the generation pipeline.

    The website therefore does NOT run those points through the old synthetic
    building/road model again. It only keeps the few displayed recommendations
    from stacking on top of each other.
    """
    return not any(
        _tree_qa_haversine_m(
            site["lat"],
            site["lon"],
            point.latitude,
            point.longitude,
        ) < 12.0
        for point in existing
    )

def generate_planting_spots(
    latitude: float,
    longitude: float,
) -> tuple[list[PlantingSpot], PlantingSpot | None]:
    """Return preferred sites plus one nearest fallback when none are within 800 m."""
    def candidate_rank(site: dict[str, Any]) -> float:
        distance = _tree_qa_haversine_m(
            latitude,
            longitude,
            site["lat"],
            site["lon"],
        )
        # Keep this first cleanup neutral and explainable:
        # nearest screened planting sites are shown first.
        return distance

    ranked = sorted(PLANTING_SITES, key=candidate_rank)
    preferred: list[PlantingSpot] = []
    nearest_outside: PlantingSpot | None = None

    for site in ranked:
        if not valid_planting_site(site, preferred):
            continue

        distance = _tree_qa_haversine_m(
            latitude,
            longitude,
            site["lat"],
            site["lon"],
        )
        if distance <= MAX_TREE_RECOMMENDATION_DISTANCE_M:
            preferred.append(
                make_planting_spot(
                    site,
                    latitude,
                    longitude,
                    within_preferred_radius=True,
                )
            )
            if len(preferred) == 3:
                break
        elif nearest_outside is None:
            nearest_outside = make_planting_spot(
                site,
                latitude,
                longitude,
                within_preferred_radius=False,
            )

    return preferred, nearest_outside if not preferred else None


def node_shade(node: tuple[float, float]) -> float:
    heat, _, _ = interpolate_heat(node[0], node[1])
    base = 1.0 - heat / 115.0
    texture = 0.10 * math.sin(node[0] * 800.0) + 0.08 * math.cos(node[1] * 730.0)
    return max(0.08, min(0.92, base + texture))


def build_network_graph() -> dict[str, list[dict[str, Any]]]:
    graph: dict[str, list[dict[str, Any]]] = {node: [] for node in NETWORK_NODES}
    for u, v, kind in NETWORK_EDGES:
        a, b = NETWORK_NODES[u], NETWORK_NODES[v]
        if kind not in {"sidewalk", "walkway", "park_walkway", "crosswalk"}:
            continue
        if segment_intersects_structure(a, b):
            continue
        distance = _tree_qa_haversine_m(a[0], a[1], b[0], b[1])
        shade = (node_shade(a) + node_shade(b)) / 2
        edge = {"to": v, "seconds": distance / 1.35, "shade": shade, "kind": kind}
        reverse = {"to": u, "seconds": distance / 1.35, "shade": shade, "kind": kind}
        graph[u].append(edge)
        graph[v].append(reverse)
    return graph


def nearest_network_node(point: tuple[float, float], excluded: set[str] | None = None) -> str:
    excluded = excluded or set()
    candidates = (node for node in NETWORK_NODES if node not in excluded)
    return min(
        candidates,
        key=lambda node: _tree_qa_haversine_m(
            point[0], point[1], NETWORK_NODES[node][0], NETWORK_NODES[node][1]
        ),
    )


def dijkstra(
    graph: dict[str, list[dict[str, Any]]], start: str, goal: str, shade_weight: float
) -> tuple[list[str], float, float]:
    queue: list[tuple[float, str]] = [(0.0, start)]
    cost = {start: 0.0}
    previous: dict[str, str] = {}
    used_edge: dict[str, dict[str, Any]] = {}

    while queue:
        current_cost, node = heapq.heappop(queue)
        if current_cost != cost.get(node):
            continue
        if node == goal:
            break
        for edge in graph[node]:
            edge_cost = edge["seconds"] * (1.0 + shade_weight * (1.0 - edge["shade"]))
            new_cost = current_cost + edge_cost
            target = edge["to"]
            if new_cost < cost.get(target, float("inf")):
                cost[target] = new_cost
                previous[target] = node
                used_edge[target] = edge
                heapq.heappush(queue, (new_cost, target))

    if goal not in cost:
        raise ValueError("No connected pedestrian route exists in the modeled network.")

    path = [goal]
    seconds = 0.0
    shaded_seconds = 0.0
    while path[-1] != start:
        current = path[-1]
        edge = used_edge[current]
        seconds += edge["seconds"]
        shaded_seconds += edge["seconds"] * edge["shade"]
        path.append(previous[current])
    path.reverse()
    return path, seconds, shaded_seconds / seconds if seconds else 0.0



def decode_google_polyline(encoded: str) -> list[list[float]]:
    """Decode a Google encoded polyline into MapLibre [lon, lat] coordinates."""
    coordinates: list[list[float]] = []
    index = 0
    latitude = 0
    longitude = 0

    while index < len(encoded):
        for coordinate_index in range(2):
            result = 0
            shift = 0
            while True:
                if index >= len(encoded):
                    raise ValueError("Google returned an invalid encoded route polyline.")
                value = ord(encoded[index]) - 63
                index += 1
                result |= (value & 0x1F) << shift
                shift += 5
                if value < 0x20:
                    break
            delta = ~(result >> 1) if result & 1 else result >> 1
            if coordinate_index == 0:
                latitude += delta
            else:
                longitude += delta
        coordinates.append([longitude / 100000.0, latitude / 100000.0])

    return coordinates


def parse_google_duration_seconds(value: str) -> float:
    if not value or not value.endswith("s"):
        raise ValueError("Google Routes returned an invalid duration.")
    return float(value[:-1])


def route_average_shade(coordinates: list[list[float]]) -> float:
    """Estimate shade along a OpenStreetMap pedestrian polyline using EcoTrace's heat model."""
    if not coordinates:
        return 0.0
    weighted_shade = 0.0
    total_weight = 0.0
    for index, coordinate in enumerate(coordinates):
        lon, lat = coordinate
        heat, _, _ = interpolate_heat(lat, lon)
        # Cooler modeled surfaces imply more likely canopy/shade. A small stable
        # local variation prevents every nearly identical alternative from tying.
        local_variation = 0.05 * math.sin(lat * 1700.0 + lon * 1300.0)
        shade = max(0.05, min(0.92, 1.02 - heat / 105.0 + local_variation))
        if index + 1 < len(coordinates):
            next_lon, next_lat = coordinates[index + 1]
            weight = max(1.0, _tree_qa_haversine_m(lat, lon, next_lat, next_lon))
        else:
            weight = 1.0
        weighted_shade += shade * weight
        total_weight += weight
    return weighted_shade / total_weight if total_weight else 0.0



def parse_departure_time(value: str | None, use_time_of_day: bool) -> datetime:
    return departure_time(value, use_time_of_day)


@_eco_memo
def segment_exposure(
    a: list[float],
    b: list[float],
    departure: datetime,
    use_time_of_day: bool,
) -> ExposureSegment:
    nighttime = night_segment(a, b, departure, use_time_of_day)
    if nighttime is not None:
        return ExposureSegment(**nighttime)
    lon1, lat1 = a[:2]
    lon2, lat2 = b[:2]
    mid_lat = (lat1 + lat2) / 2.0
    mid_lon = (lon1 + lon2) / 2.0
    heat, _, _ = interpolate_heat(mid_lat, mid_lon)

    if CANOPY_ENGINE.available:
        result = CANOPY_ENGINE.analyze_point(
            mid_lat,
            mid_lon,
            departure,
            use_time_of_day,
            heat,
        )

        # Optional WRI/Meta canopy-height correction. This does NOT replace the
        # geometric tree-shadow model. It provides an independent high-resolution
        # canopy-presence signal that can catch vegetation missing from point inventories.
        wri_signal = WRI_CANOPY.local_canopy_signal(mid_lat, mid_lon, radius_m=14.0)
        wri_fraction = wri_signal.get("canopy_sample_fraction")
        wri_height = wri_signal.get("max_height_m")
        canopy_points = float(result.canopy_shadow_percent)
        wri_note = ""
        if wri_fraction is not None:
            # Conservative: canopy-height presence can raise the tree-canopy component,
            # but never by itself imply complete shade.
            empirical_canopy_points = min(72.0, max(0.0, float(wri_fraction) * 72.0))
            if wri_height is not None and float(wri_height) < 2.0:
                empirical_canopy_points *= 0.25
            corrected_canopy_points = max(canopy_points, empirical_canopy_points)
            if corrected_canopy_points > canopy_points + 1.0:
                wri_note = (
                    f" WRI/Meta canopy-height data detected additional nearby canopy "
                    f"(max {float(wri_height or 0):.1f} m), so EcoTrace conservatively "
                    f"raised the canopy component from {canopy_points:.1f} to "
                    f"{corrected_canopy_points:.1f} points."
                )
            canopy_points = corrected_canopy_points

        canopy_fraction = max(0.0, min(1.0, canopy_points / 100.0))
        building_fraction = max(0.0, min(1.0, result.building_shadow_percent / 100.0))
        # EcoTrace route shade intentionally uses trees + buildings only.
        physical_shadow = 1.0 - (1.0 - canopy_fraction) * (1.0 - building_fraction)
        percent = max(0.0, min(100.0, physical_shadow * 100.0 - result.heat_penalty_percent))
        category = (
            "high-shade"
            if percent >= 62
            else "mixed"
            if percent >= 36
            else "high-exposure"
        )
        return ExposureSegment(
            coordinates=[a, b],
            category=category,
            shade_percent=round(percent, 1),
            canopy_shadow_percent=round(canopy_points, 1),
            building_shadow_percent=result.building_shadow_percent,
            terrain_shadow_percent=result.terrain_shadow_percent,
            heat_penalty_percent=result.heat_penalty_percent,
            solar_altitude_degrees=result.solar_altitude_degrees,
            solar_azimuth_degrees=result.solar_azimuth_degrees,
            tree_count_considered=result.tree_count_considered,
            building_count_considered=result.building_count_considered,
            wri_canopy_height_m=wri_height,
            wri_canopy_fraction=wri_fraction,
            reason=result.reason + wri_note,
            data_quality=(
                result.data_quality + " + WRI/Meta canopy-height cross-check"
                if wri_fraction is not None
                else result.data_quality
            ),
        )

    # Explicit fallback when canopy_model.json has not been generated.
    # WRI/Meta still provides a measured canopy-presence cross-check here, so
    # walking and bike routes can benefit from the raster independently.
    wri_signal = WRI_CANOPY.local_canopy_signal(mid_lat, mid_lon, radius_m=14.0)
    wri_fraction = wri_signal.get("canopy_sample_fraction")
    wri_height = wri_signal.get("max_height_m")

    base_shade = max(
        0.05,
        min(
            0.92,
            1.02
            - heat / 105.0
            + 0.05 * math.sin(mid_lat * 1700.0 + mid_lon * 1300.0),
        ),
    )
    hour = departure.hour + departure.minute / 60.0
    daylight_strength = max(0.0, math.sin(math.pi * (hour - 6.0) / 13.0))
    east = (lon2 - lon1) * math.cos(math.radians(mid_lat))
    north = lat2 - lat1
    street_axis = (math.degrees(math.atan2(east, north)) + 360.0) % 180.0
    sun_axis = (((hour - 6.0) / 13.0) * 180.0 + 90.0) % 180.0
    alignment = abs(math.cos(math.radians(street_axis - sun_axis)))
    orientation_penalty = (
        daylight_strength * alignment * 0.18
        if use_time_of_day
        else 0.0
    )
    percent = max(3.0, min(96.0, (base_shade - orientation_penalty) * 100.0))

    # If WRI detects nearby canopy, conservatively raise the fallback score.
    # The raster is evidence of canopy presence, not proof of exact instantaneous
    # sidewalk shadow, so it is capped below complete shade.
    wri_note = ""
    if wri_fraction is not None:
        wri_component = min(72.0, max(0.0, float(wri_fraction) * 72.0))
        if wri_height is not None and float(wri_height) < 2.0:
            wri_component *= 0.25
        if wri_component > percent:
            wri_note = (
                f" WRI/Meta canopy-height data detected nearby canopy "
                f"(max {float(wri_height or 0):.1f} m; local canopy fraction "
                f"{float(wri_fraction):.2f}), raising the fallback score "
                f"conservatively to {wri_component:.1f}/100."
            )
            percent = wri_component

    category = (
        "high-shade"
        if percent >= 62
        else "mixed"
        if percent >= 36
        else "high-exposure"
    )
    return ExposureSegment(
        coordinates=[a, b],
        category=category,
        shade_percent=round(percent, 1),
        canopy_shadow_percent=round(
            min(72.0, max(0.0, float(wri_fraction or 0.0) * 72.0)),
            1,
        ),
        wri_canopy_height_m=wri_height,
        wri_canopy_fraction=wri_fraction,
        reason=(
            f"Fallback estimated shade {percent:.0f}/100 from modeled heat and "
            "street orientation." + wri_note +
            " Generate canopy_model.json to restore mapped-tree/building/terrain "
            "shadow geometry."
        ),
        data_quality=(
            "fallback heat-orientation model + WRI/Meta canopy-height cross-check"
            if wri_fraction is not None
            else "fallback heat-orientation model"
        ),
    )



@_eco_memo
def build_exposure_segments(
    coordinates: list[list[float]],
    departure: datetime,
    use_time_of_day: bool,
) -> list[ExposureSegment]:
    output: list[ExposureSegment] = []
    for index in range(len(coordinates) - 1):
        a, b = coordinates[index], coordinates[index + 1]
        if _tree_qa_haversine_m(a[1], a[0], b[1], b[0]) < 1:
            continue
        output.append(segment_exposure(a, b, departure, use_time_of_day))
    return output


def route_component_average(
    segments: list[ExposureSegment],
    field_name: str,
) -> float:
    weighted = 0.0
    total = 0.0
    for segment in segments:
        a, b = segment.coordinates
        distance = max(1.0, _tree_qa_haversine_m(a[1], a[0], b[1], b[0]))
        weighted += float(getattr(segment, field_name, 0.0)) * distance
        total += distance
    return weighted / total if total else 0.0

def route_shadow_breakdown(
    coordinates: list[list[float]],
    departure: datetime,
    use_time_of_day: bool,
) -> dict[str, float]:
    segments = build_exposure_segments(coordinates, departure, use_time_of_day)
    if not segments:
        return {"tree": 0.0, "building": 0.0}
    total = tree = building = 0.0
    for segment in segments:
        a, b = segment.coordinates
        distance = max(1.0, _tree_qa_haversine_m(a[1], a[0], b[1], b[0]))
        total += distance
        tree += float(segment.canopy_shadow_percent or 0.0) * distance
        building += float(segment.building_shadow_percent or 0.0) * distance
    if total <= 0:
        return {"tree": 0.0, "building": 0.0}
    return {"tree": round(tree/total,1), "building": round(building/total,1)}



def route_why_text(
    label: str,
    segments: list[ExposureSegment],
    minutes: float,
    distance_miles: float,
    route_limit_description: str,
) -> str:
    if not segments:
        return f"{label} has no segment analysis."
    shade = route_component_average(segments, "shade_percent")
    canopy = route_component_average(segments, "canopy_shadow_percent")
    building = route_component_average(segments, "building_shadow_percent")
    terrain = route_component_average(segments, "terrain_shadow_percent")
    heat_penalty = route_component_average(segments, "heat_penalty_percent")
    wri_segments = sum(
        1 for segment in segments
        if segment.wri_canopy_fraction is not None
    )
    wri_text = (
        f" WRI/Meta canopy-height cross-checked {wri_segments}/{len(segments)} "
        "route segments."
        if wri_segments
        else ""
    )
    hottest = min(segments, key=lambda item: item.shade_percent)
    if CANOPY_ENGINE.available:
        component_text = (
            f"mapped tree canopy contributes {canopy:.1f} points, mapped building "
            f"shadows {building:.1f}, and heat risk subtracts "
            f"{heat_penalty:.1f}"
        )
    else:
        component_text = (
            f"tree-canopy evidence contributes {canopy:.1f} points where WRI/Meta "
            "data is available; mapped building/terrain shadow geometry is currently "
            "unavailable because the canopy model file was not loaded"
        )
    return (
        f"{label} is {distance_miles:.2f} miles and {minutes:.1f} minutes. "
        f"Its distance-weighted shade score is {shade:.1f}/100: {component_text}. "
        f"The lowest modeled-shade segment scores {hottest.shade_percent:.1f}/100 because "
        f"{hottest.reason}{wri_text} {route_limit_description}"
    )



def calculate_time_adjusted_route_shade_v525(
    coordinates: list[list[float]],
    departure: datetime,
    use_time_of_day: bool,
) -> float:
    """Calculate distance-weighted route shade using exactly three inputs."""
    segments = build_exposure_segments(
        coordinates,
        departure,
        use_time_of_day,
    )
    if not segments:
        return route_average_shade(coordinates)
    weighted = 0.0
    total = 0.0
    for segment in segments:
        a, b = segment.coordinates
        distance = max(1.0, _tree_qa_haversine_m(a[1], a[0], b[1], b[0]))
        weighted += (segment.shade_percent / 100.0) * distance
        total += distance
    return weighted / total if total else 0.0


def _normalize_route_coordinates(value: Any) -> list[list[float]]:
    """Normalize ORS LineString/MultiLineString coordinates to [lon, lat]."""
    if not isinstance(value, list) or not value:
        return []

    # Standard LineString: [[lon, lat], ...]
    if isinstance(value[0], (list, tuple)) and len(value[0]) >= 2 and isinstance(value[0][0], (int, float)):
        normalized: list[list[float]] = []
        for point in value:
            if not isinstance(point, (list, tuple)) or len(point) < 2:
                continue
            try:
                lon = float(point[0])
                lat = float(point[1])
            except (TypeError, ValueError):
                continue
            if -180 <= lon <= 180 and -90 <= lat <= 90:
                normalized.append([lon, lat])
        return normalized

    # MultiLineString or unexpectedly nested coordinates.
    flattened: list[list[float]] = []
    for part in value:
        part_coordinates = _normalize_route_coordinates(part)
        if not part_coordinates:
            continue
        if flattened and flattened[-1] == part_coordinates[0]:
            flattened.extend(part_coordinates[1:])
        else:
            flattened.extend(part_coordinates)
    return flattened


def _geometry_distance_m(coordinates: list[list[float]]) -> float:
    total = 0.0
    for index in range(len(coordinates) - 1):
        lon1, lat1 = coordinates[index][:2]
        lon2, lat2 = coordinates[index + 1][:2]
        total += _tree_qa_haversine_m(lat1, lon1, lat2, lon2)
    return total


def _extract_route_totals(properties: dict[str, Any], coordinates: list[list[float]]) -> tuple[float, float]:
    """Read ORS totals; safely derive them if a gateway omits summary fields."""
    summary = properties.get("summary") or {}
    segments = properties.get("segments") or []

    duration = summary.get("duration")
    distance = summary.get("distance")

    if duration is None and isinstance(segments, list):
        duration = sum(float((segment or {}).get("duration") or 0.0) for segment in segments if isinstance(segment, dict))
    if distance is None and isinstance(segments, list):
        distance = sum(float((segment or {}).get("distance") or 0.0) for segment in segments if isinstance(segment, dict))

    try:
        seconds = float(duration or 0.0)
    except (TypeError, ValueError):
        seconds = 0.0
    try:
        distance_m = float(distance or 0.0)
    except (TypeError, ValueError):
        distance_m = 0.0

    # A valid ORS geometry must never be thrown away merely because summary
    # metadata is absent. Derive distance from the polyline and use a normal
    # walking speed of 1.35 m/s only as a metadata fallback.
    if distance_m <= 0 and len(coordinates) >= 2:
        distance_m = _geometry_distance_m(coordinates)
    if seconds <= 0 and distance_m > 0:
        seconds = distance_m / 1.35

    return seconds, distance_m


def _looks_like_route_line(value: Any) -> bool:
    """Return True for a plausible ORS [longitude, latitude] route line."""
    if not isinstance(value, list) or len(value) < 2:
        return False
    valid_points = 0
    for point in value:
        if not isinstance(point, (list, tuple)) or len(point) < 2:
            return False
        try:
            lon = float(point[0])
            lat = float(point[1])
        except (TypeError, ValueError):
            return False
        # Broad Bay Area bounds prevent bbox arrays and unrelated numeric arrays
        # from being mistaken for route geometry.
        if not (-123.0 <= lon <= -121.0 and 36.5 <= lat <= 38.5):
            return False
        valid_points += 1
    return valid_points >= 2


def _find_route_lines(value: Any) -> list[list[list[float]]]:
    """Recursively recover route lines from GeoJSON, JSON, or proxy-wrapped ORS data."""
    found: list[list[list[float]]] = []

    if _looks_like_route_line(value):
        line = [[float(point[0]), float(point[1])] for point in value]
        found.append(line)
        return found

    if isinstance(value, dict):
        # Geometry is commonly encoded as a polyline string in the JSON endpoint.
        geometry = value.get("geometry")
        if isinstance(geometry, str) and geometry:
            try:
                decoded = decode_google_polyline(geometry)
                if _looks_like_route_line(decoded):
                    found.append(decoded)
            except (ValueError, TypeError):
                pass

        # Search likely geometry containers first, then any remaining wrappers.
        priority_keys = ("coordinates", "geometry", "features", "routes")
        visited: set[str] = set()
        for key in priority_keys:
            if key in value:
                visited.add(key)
                found.extend(_find_route_lines(value[key]))
        for key, child in value.items():
            if key not in visited and key not in {"bbox", "bounds"}:
                found.extend(_find_route_lines(child))

    elif isinstance(value, list):
        for child in value:
            found.extend(_find_route_lines(child))

    # Remove exact duplicates while preserving order.
    unique: list[list[list[float]]] = []
    signatures: set[tuple] = set()
    for line in found:
        signature = tuple((round(p[0], 6), round(p[1], 6)) for p in line)
        if signature not in signatures:
            signatures.add(signature)
            unique.append(line)
    return unique


def _candidate_from_line(
    coordinates: list[list[float]],
    properties: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    if len(coordinates) < 2:
        return None
    seconds, distance_m = _extract_route_totals(properties or {}, coordinates)
    if distance_m <= 0:
        return None
    if seconds <= 0:
        seconds = distance_m / 1.35
    return {
        "coordinates": coordinates,
        "seconds": seconds,
        "distance_m": distance_m,
        "shade": 0.0,
    }


def _extract_ors_candidates(result: dict[str, Any]) -> list[dict[str, Any]]:
    """Parse all known ORS response shapes, with recursive geometry recovery."""
    candidates: list[dict[str, Any]] = []

    # Preferred GeoJSON FeatureCollection shape.
    for feature in result.get("features") or []:
        if not isinstance(feature, dict):
            continue
        geometry = feature.get("geometry") or {}
        raw_coordinates = geometry.get("coordinates") if isinstance(geometry, dict) else geometry
        coordinates = _normalize_route_coordinates(raw_coordinates)
        if not coordinates and isinstance(raw_coordinates, str):
            try:
                coordinates = decode_google_polyline(raw_coordinates)
            except ValueError:
                coordinates = []
        candidate = _candidate_from_line(coordinates, feature.get("properties") or {})
        if candidate:
            candidates.append(candidate)

    # Standard JSON endpoint shape.
    for route in result.get("routes") or []:
        if not isinstance(route, dict):
            continue
        raw_geometry = route.get("geometry")
        if isinstance(raw_geometry, str):
            try:
                coordinates = decode_google_polyline(raw_geometry)
            except ValueError:
                coordinates = []
        elif isinstance(raw_geometry, dict):
            coordinates = _normalize_route_coordinates(raw_geometry.get("coordinates"))
        else:
            coordinates = _normalize_route_coordinates(raw_geometry)
        candidate = _candidate_from_line(coordinates, route)
        if candidate:
            candidates.append(candidate)

    # Last-resort recovery for API gateways or changed response wrappers.
    if not candidates:
        for line in _find_route_lines(result):
            candidate = _candidate_from_line(line)
            if candidate:
                candidates.append(candidate)

    # De-duplicate routes.
    unique: list[dict[str, Any]] = []
    signatures: set[tuple] = set()
    for candidate in candidates:
        coords = candidate["coordinates"]
        signature = (
            round(coords[0][0], 5), round(coords[0][1], 5),
            round(coords[-1][0], 5), round(coords[-1][1], 5),
            round(candidate["distance_m"], -1),
        )
        if signature not in signatures:
            signatures.add(signature)
            unique.append(candidate)
    return unique


def _post_ors(
    api_key: str,
    url: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    try:
        response = requests.post(
            url,
            headers={
                "Authorization": api_key,
                "Content-Type": "application/json",
                "Accept": "application/geo+json, application/json",
                "User-Agent": "EcoTrace-CA17/3.0",
            },
            json=payload,
            timeout=_eco_ors_timeout(),
            verify=certifi.where(),
        )
    except requests.exceptions.SSLError as error:
        raise RuntimeError(
            "Secure connection to OpenRouteService failed. Run: "
            "python3 -m pip install --upgrade certifi requests"
        ) from error
    except requests.exceptions.Timeout as error:
        raise RuntimeError("OpenRouteService timed out. Please try again.") from error
    except requests.exceptions.ConnectionError as error:
        raise RuntimeError(f"Could not connect to OpenRouteService: {error}") from error
    except requests.RequestException as error:
        raise RuntimeError(f"OpenRouteService request failed: {error}") from error

    if not response.ok:
        try:
            error_data = response.json()
            detail = (
                (error_data.get("error") or {}).get("message")
                or error_data.get("message")
                or json.dumps(error_data)
            )
        except ValueError:
            detail = response.text[:800] or response.reason
        raise RuntimeError(f"OpenRouteService returned HTTP {response.status_code}: {detail}")

    try:
        data = response.json()
    except ValueError as error:
        raise RuntimeError(f"OpenRouteService returned invalid JSON: {response.text[:500]}") from error
    if not isinstance(data, dict):
        raise RuntimeError("OpenRouteService returned an unexpected non-object response.")
    return data


def _nearby_coordinate_candidates(
    point: tuple[float, float],
) -> list[tuple[float, float]]:
    """
    Generate a compact search around a geocoded/clicked point.

    Nominatim may return a building or parcel center rather than the sidewalk.
    ORS can snap only within a limited radius, so EcoTrace retries nearby points
    without drawing a straight connector through private property.
    """
    latitude, longitude = point
    output: list[tuple[float, float]] = [point]

    # Small-to-large rings. Eight bearings cover cardinal and diagonal directions.
    for radius_m in (75.0, 150.0, 250.0, 325.0):
        for bearing_deg in (0, 45, 90, 135, 180, 225, 270, 315):
            bearing = math.radians(bearing_deg)
            north_m = math.cos(bearing) * radius_m
            east_m = math.sin(bearing) * radius_m
            lat_offset = north_m / 111_320.0
            lon_scale = 111_320.0 * max(math.cos(math.radians(latitude)), 0.2)
            lon_offset = east_m / lon_scale
            candidate = (latitude + lat_offset, longitude + lon_offset)
            if _inside_district(candidate[0], candidate[1], padding=0.08):
                output.append(candidate)

    return output


def _ors_unroutable_coordinate_index(error: Exception) -> int | None:
    message = str(error).lower()
    if "could not find routable point" not in message:
        return None
    match = re.search(r"coordinate\s+([01])", message)
    return int(match.group(1)) if match else None


def _local_route_snap(
    point: tuple[float, float],
) -> tuple[float, float]:
    snapped = BIKE_INFRASTRUCTURE.nearest_routable_point(
        point[0],
        point[1],
        search_radius_m=700.0,
    )
    if not snapped:
        return point
    return (
        float(snapped["latitude"]),
        float(snapped["longitude"]),
    )


def _request_ors_route_once(
    api_key: str,
    start: tuple[float, float],
    end: tuple[float, float],
    include_alternatives: bool,
) -> list[dict[str, Any]]:
    payload: dict[str, Any] = {
        "coordinates": [[start[1], start[0]], [end[1], end[0]]],
        "instructions": False,
        "geometry": True,
        "preference": "recommended",
        # Explicitly use the hosted-service snapping allowance.
        "radiuses": [350, 350],
    }
    if include_alternatives:
        payload["alternative_routes"] = {
            "target_count": 2,
            "weight_factor": 1.4,
            "share_factor": 0.65,
        }

    geojson_result = _post_ors(
        api_key,
        "https://api.openrouteservice.org/v2/directions/foot-walking/geojson",
        payload,
    )
    candidates = _extract_ors_candidates(geojson_result)
    if candidates:
        return candidates

    json_payload = dict(payload)
    json_result = _post_ors(
        api_key,
        "https://api.openrouteservice.org/v2/directions/foot-walking",
        json_payload,
    )
    candidates = _extract_ors_candidates(json_result)
    if candidates:
        return candidates

    geo_features = geojson_result.get("features") or []
    json_routes = json_result.get("routes") or []
    raise RuntimeError(
        "ORS returned HTTP 200 but no decodable walking-route geometry. "
        f"GeoJSON features={len(geo_features)}; JSON routes={len(json_routes)}."
    )


def _request_ors_route(api_key, start, end, include_alternatives):
    a, b = _local_route_snap(start), _local_route_snap(end)
    tried = {0: 0, 1: 0}
    for attempt in range(3):
        try:
            return _request_ors_route_once(api_key, a, b, include_alternatives)
        except RuntimeError as error:
            bad = _ors_unroutable_coordinate_index(error)
            if bad is None or attempt == 2:
                raise
            tried[bad] += 1
            choices = _nearby_coordinate_candidates(a if bad == 0 else b)
            if tried[bad] >= len(choices):
                raise
            if bad == 0:
                a = choices[tried[bad]]
            else:
                b = choices[tried[bad]]
    raise RuntimeError('Select an endpoint on a mapped street or path.')


def _request_ors_profile_once(
    api_key: str,
    profile: str,
    start: tuple[float, float],
    end: tuple[float, float],
    include_alternatives: bool,
) -> list[dict[str, Any]]:
    payload: dict[str, Any] = {
        "coordinates": [[start[1], start[0]], [end[1], end[0]]],
        "instructions": False,
        "geometry": True,
        "preference": "recommended",
        "radiuses": [350, 350],
    }
    if include_alternatives:
        payload["alternative_routes"] = {
            "target_count": 2,
            "weight_factor": 1.4,
            "share_factor": 0.65,
        }

    result = _post_ors(
        api_key,
        f"https://api.openrouteservice.org/v2/directions/{profile}/geojson",
        payload,
    )
    candidates = _extract_ors_candidates(result)
    if candidates:
        return candidates

    result = _post_ors(
        api_key,
        f"https://api.openrouteservice.org/v2/directions/{profile}",
        payload,
    )
    candidates = _extract_ors_candidates(result)
    if candidates:
        return candidates
    raise RuntimeError(
        f"OpenRouteService returned no decodable {profile} route geometry."
    )


def _request_ors_profile(api_key, profile, start, end, include_alternatives):
    a, b = _local_route_snap(start), _local_route_snap(end)
    tried = {0: 0, 1: 0}
    for attempt in range(3):
        try:
            return _request_ors_profile_once(api_key, profile, a, b, include_alternatives)
        except RuntimeError as error:
            bad = _ors_unroutable_coordinate_index(error)
            if bad is None or attempt == 2:
                raise
            tried[bad] += 1
            choices = _nearby_coordinate_candidates(a if bad == 0 else b)
            if tried[bad] >= len(choices):
                raise
            if bad == 0:
                a = choices[tried[bad]]
            else:
                b = choices[tried[bad]]
    raise RuntimeError('Select an endpoint on a mapped street or path.')



@_eco_memo
def bike_infrastructure_segments(
    coordinates: list[list[float]],
) -> list[dict[str, Any]]:
    """
    Classify every returned route segment with the same OSM-based matcher used
    for the aggregate percentage. The matcher uses the midpoint of each route
    segment and requires a mapped facility within 14 meters.
    """
    output: list[dict[str, Any]] = []
    for a, b in zip(coordinates, coordinates[1:]):
        if len(a) < 2 or len(b) < 2:
            continue
        lat = (float(a[1]) + float(b[1])) / 2.0
        lon = (float(a[0]) + float(b[0])) / 2.0
        category, explanation = BIKE_INFRASTRUCTURE.classify_point(
            lat,
            lon,
            tolerance_m=14.0,
        )
        output.append({
            "coordinates": [a, b],
            "category": category,
            "explanation": explanation,
        })
    return output


@_eco_route_work
def bike_routes_analysis(
    start: tuple[float, float],
    end: tuple[float, float],
    *,
    bike_type: str,
    speed_mph: float,
    max_detour: float,
    max_extra_distance_miles: float | None,
    departure_value: str | None,
    use_time_of_day: bool,
    quick_mode: bool = False,
) -> BikeRouteAnalysis:
    api_key = os.getenv("ORS_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("ORS_API_KEY is missing.")

    profile = {
        "regular": "cycling-regular",
        "road-bike": "cycling-road",
        "e-bike": "cycling-electric",
    }[bike_type]

    from ecotrace_route_requests import fetch_route_candidates
    candidates, routing_note = fetch_route_candidates(
        api_key, profile, start, end, _extract_ors_candidates,
        alternatives=not quick_mode)

    departure = parse_departure_time(departure_value, use_time_of_day)
    meters_per_second = speed_mph * 1609.344 / 3600.0
    for candidate in candidates:
        candidate["personal_seconds"] = candidate["distance_m"] / meters_per_second
        candidate["shade"] = calculate_time_adjusted_route_shade_v525(
            candidate["coordinates"], departure, use_time_of_day
        )

    baseline = min(
        candidates,
        key=lambda item: (item["personal_seconds"], item["distance_m"]),
    )

    if max_extra_distance_miles is None:
        limit_seconds = baseline["personal_seconds"] * (1 + max_detour)
        allowed = [
            item for item in candidates
            if item["personal_seconds"] <= limit_seconds + 0.001
        ]
        limit_description = (
            f"Cooler alternatives may take no more than "
            f"{round(max_detour * 100)}% longer."
        )
    else:
        limit_distance = (
            baseline["distance_m"] + max_extra_distance_miles * 1609.344
        )
        allowed = [
            item for item in candidates
            if item["distance_m"] <= limit_distance + 0.001
        ]
        limit_description = (
            f"Cooler alternatives may be up to "
            f"{max_extra_distance_miles:.2f} miles longer."
        )

    cooler = max(
        allowed or [baseline],
        key=lambda item: (
            item["shade"],
            -item["personal_seconds"],
            -item["distance_m"],
        ),
    )

    # Interactive bike classification uses EcoTrace's original local
    # OSM-derived bike infrastructure layer. VTA remains an independent
    # official reference link instead of a route-time dependency.
    fastest_infra = BIKE_INFRASTRUCTURE.analyze_route(
        baseline["coordinates"]
    )
    cooler_infra = BIKE_INFRASTRUCTURE.analyze_route(
        cooler["coordinates"]
    )
    fastest_infra["segments"] = bike_infrastructure_segments(
        baseline["coordinates"]
    )
    cooler_infra["segments"] = bike_infrastructure_segments(
        cooler["coordinates"]
    )
    fastest_infra["source"] = "OpenStreetMap-derived local bike layer"
    cooler_infra["source"] = "OpenStreetMap-derived local bike layer"
    fastest_infra["official_vta_2023"] = False
    cooler_infra["official_vta_2023"] = False

    fastest_minutes = baseline["personal_seconds"] / 60
    cooler_minutes = cooler["personal_seconds"] / 60
    fastest_shade = baseline["shade"] * 100
    cooler_shade = cooler["shade"] * 100
    detour = (
        (cooler["personal_seconds"] / baseline["personal_seconds"] - 1) * 100
        if baseline["personal_seconds"] > 0 else 0
    )

    weather = WeatherSnapshot(available=False, temperature_f=None,
        precipitation_probability=None, wind_speed_mph=None,
        source="Heat Map", warning=None,
        impact_summary="Open Heat Map for current weather context.")
    wind_text = ""
    if (
        weather.available
        and weather.wind_speed_mph is not None
        and weather.wind_speed_mph >= 15
    ):
        wind_text = (
            f" Current wind is about {weather.wind_speed_mph:.1f} mph "
            "and may noticeably affect effort."
        )

    warnings: list[str] = []
    if routing_note:
        warnings.append(routing_note)
    if speed_mph >= 25:
        warnings.append(
            f"{speed_mph:.1f} mph is unusually fast for ordinary urban cycling."
        )
    if not BIKE_INFRASTRUCTURE.available:
        warnings.append(
            "Bike-infrastructure coverage is unavailable because the local OSM-derived bike layer is not loaded."
        )
    elif (
        cooler_infra["shared_road_percent"]
        + cooler_infra["unknown_percent"]
        >= 50
    ):
        warnings.append(
            "The cooler route contains substantial shared-road or unmapped cycling sections."
        )

    fastest_breakdown = route_shadow_breakdown(
        baseline["coordinates"], departure, use_time_of_day
    )
    cooler_breakdown = route_shadow_breakdown(
        cooler["coordinates"], departure, use_time_of_day
    )
    meaningful_cooler = (
        cooler["coordinates"] != baseline["coordinates"]
        and (cooler_shade - fastest_shade) >= 2.0
        and (
            abs(cooler["distance_m"] - baseline["distance_m"]) >= 12.0
            or abs(cooler_minutes - fastest_minutes) >= 0.2
        )
    )

    return annotate_route(BikeRouteAnalysis(
        fastest_coordinates=baseline["coordinates"],
        cooler_coordinates=cooler["coordinates"],
        fastest_minutes=round(fastest_minutes, 1),
        cooler_minutes=round(cooler_minutes, 1),
        fastest_distance_miles=round(baseline["distance_m"] / 1609.344, 2),
        cooler_distance_miles=round(cooler["distance_m"] / 1609.344, 2),
        fastest_distance_km=round(baseline["distance_m"] / 1000, 2),
        cooler_distance_km=round(cooler["distance_m"] / 1000, 2),
        fastest_shade_score=round(fastest_shade, 1),
        cooler_shade_score=round(cooler_shade, 1),
        fastest_tree_shadow_percent=fastest_breakdown["tree"],
        cooler_tree_shadow_percent=cooler_breakdown["tree"],
        fastest_building_shadow_percent=fastest_breakdown["building"],
        cooler_building_shadow_percent=cooler_breakdown["building"],
        meaningful_cooler_route=meaningful_cooler,
        shade_gain=round(cooler_shade - fastest_shade, 1),
        extra_minutes=round(cooler_minutes - fastest_minutes, 1),
        detour_percent=round(max(0.0, detour), 1),
        bike_speed_mph=round(speed_mph, 1),
        bike_type=bike_type,
        fastest_infrastructure=fastest_infra,
        cooler_infrastructure=cooler_infra,
        fastest_why=(
            f"This is the fastest {bike_type} route at {speed_mph:.1f} mph. "
            f"EcoTrace matched {fastest_infra['dedicated_infrastructure_percent']:.1f}% "
            f"of this route to protected or painted bike infrastructure using {fastest_infra.get('source','mapped bike data')} and has a "
            f"modeled shade score of {fastest_shade:.1f}/100 using the same "
            f"WRI/Meta-enhanced shade engine as walking routes.{wind_text}"
        ),
        cooler_why=(
            "This route maximizes estimated shade within the selected limit. "
            f"It gains {cooler_shade - fastest_shade:.1f} shade-score points "
            f"and matched {cooler_infra['dedicated_infrastructure_percent']:.1f}% "
            f"to protected or painted infrastructure using {cooler_infra.get('source','mapped bike data')}. "
            f"{cooler_infra['warning']}{wind_text}"
        ),
        route_limit_description=limit_description,
        departure_time=departure.isoformat(timespec="minutes"),
        time_of_day_used=use_time_of_day,
        warning=" ".join(warnings) if warnings else None,
        weather=weather,
    ), departure, use_time_of_day, bike=True)




@_eco_route_work
def ors_routes_analysis(
    start: tuple[float, float],
    end: tuple[float, float],
    detour_cap: float,
    pace_mph: float,
    departure_value: str | None,
    use_time_of_day: bool,
    max_extra_distance_miles: float | None,
    user_profile: str,
) -> RouteAnalysis:
    """Get a reliable walking route first, then safely evaluate alternatives."""
    api_key = os.environ.get("ORS_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError(
            "ORS_API_KEY is missing. Set it in the same Terminal window, then restart EcoTrace."
        )

    from ecotrace_route_requests import fetch_route_candidates
    candidates, routing_note = fetch_route_candidates(
        api_key, "foot-walking", start, end, _extract_ors_candidates,
        alternatives=True)

    departure = parse_departure_time(departure_value, use_time_of_day)
    for candidate in candidates:
        candidate["shade"] = calculate_time_adjusted_route_shade_v525(
            candidate["coordinates"],
            departure,
            use_time_of_day,
        )
        candidate["personal_seconds"] = (
            candidate["distance_m"] / (pace_mph * 1609.344 / 3600.0)
        )

    baseline = min(
        candidates,
        key=lambda item: (item["personal_seconds"], item["distance_m"]),
    )
    if max_extra_distance_miles is not None:
        extra_distance_m = max_extra_distance_miles * 1609.344
        maximum_distance_m = baseline["distance_m"] + extra_distance_m
        allowed = [
            item
            for item in candidates
            if item["distance_m"] <= maximum_distance_m + 0.001
        ]
        route_limit_mode = "custom extra distance"
        route_limit_description = (
            f"Shaded alternatives may be up to {max_extra_distance_miles:.2f} "
            "miles longer than the shortest route."
        )
    else:
        allowed_seconds = baseline["personal_seconds"] * (1.0 + detour_cap)
        allowed = [
            item
            for item in candidates
            if item["personal_seconds"] <= allowed_seconds + 0.001
        ]
        route_limit_mode = "normal time cap"
        route_limit_description = (
            f"Shaded alternatives may take no more than "
            f"{round(detour_cap * 100)}% longer than the shortest route."
        )

    if not allowed:
        allowed = [baseline]
    shadiest = max(
        allowed,
        key=lambda item: (item["shade"], -item["seconds"], -item["distance_m"]),
    )

    shortest_minutes = baseline["personal_seconds"] / 60.0
    shadiest_minutes = shadiest["personal_seconds"] / 60.0

    shortest_shade_raw = baseline["shade"] * 100.0
    shadiest_shade_raw = shadiest["shade"] * 100.0
    shade_gain_raw = shadiest_shade_raw - shortest_shade_raw

    same_candidate = shadiest is baseline
    nearly_same_distance = abs(
        shadiest["distance_m"] - baseline["distance_m"]
    ) < 15.0
    nearly_same_time = abs(
        shadiest["personal_seconds"] - baseline["personal_seconds"]
    ) < 20.0

    meaningful_shadier_route = not (
        same_candidate
        or shade_gain_raw < 2.0
        or (
            nearly_same_distance
            and nearly_same_time
            and shade_gain_raw < 3.0
        )
    )
    detour_percent = (
        (shadiest["personal_seconds"] / baseline["personal_seconds"] - 1.0) * 100.0
        if baseline["personal_seconds"] > 0 else 0.0
    )
    shortest_shade = baseline["shade"] * 100.0
    shadiest_shade = shadiest["shade"] * 100.0

    same_route = (
        abs(shadiest["seconds"] - baseline["seconds"]) < 0.5
        and abs(shadiest["distance_m"] - baseline["distance_m"]) < 2.0
    )
    route_message = (
        "ORS returned one reliable walking route, so the shortest and shaded paths currently overlap."
        if same_route
        else (
            "The blue route is the fastest ORS walking route. The green route is "
            "the highest-shade alternative that satisfies the selected route limit."
        )
    )

    pace_warning = None
    if pace_mph >= 8.0:
        pace_warning = (
            f"{pace_mph:.1f} mph is too fast for normal walking and is likely "
            "running speed. EcoTrace allowed it, but the personalized walking time "
            "may be unrealistic."
        )
    elif pace_mph < 1.5:
        pace_warning = (
            f"{pace_mph:.1f} mph is a very slow pace. EcoTrace allowed it and "
            "increased the personalized travel time accordingly."
        )

    midpoint_lat = (start[0] + end[0]) / 2.0
    midpoint_lon = (start[1] + end[1]) / 2.0
    weather = WeatherSnapshot(available=False, temperature_f=None,
        precipitation_probability=None, wind_speed_mph=None,
        source="Heat Map", warning=None,
        impact_summary="Open Heat Map for current weather context.")

    shortest_segment_results = build_exposure_segments(
        baseline["coordinates"],
        departure,
        use_time_of_day,
    )
    shadiest_segment_results = build_exposure_segments(
        shadiest["coordinates"],
        departure,
        use_time_of_day,
    )

    shortest_distance_miles = baseline["distance_m"] / 1609.344
    shadiest_distance_miles = shadiest["distance_m"] / 1609.344
    shortest_why = route_why_text(
        "The shortest route",
        shortest_segment_results,
        shortest_minutes,
        shortest_distance_miles,
        route_limit_description,
    )
    shadiest_why = route_why_text(
        "The shade-oriented route",
        shadiest_segment_results,
        shadiest_minutes,
        shadiest_distance_miles,
        route_limit_description,
    )

    profile_guidance_map = {
        "child": (
            "Child profile prioritizes shade and allows a moderate detour. "
            "An adult should still supervise route choice and crossings."
        ),
        "adult": (
            "Adult profile uses balanced walking speed, shade priority, and detour."
        ),
        "runner": (
            "Runner profile uses a faster pace and a smaller detour because "
            "longer routes can add substantial exertion."
        ),
        "older_adult": (
            "Older Adult profile prioritizes shade more strongly, uses a slower "
            "pace, and permits a larger detour."
        ),
        "heat_sensitive": (
            "Heat-Sensitive profile gives shade the highest priority and permits "
            "the largest detour. This is planning guidance, not medical advice."
        ),
    }
    profile_guidance = profile_guidance_map.get(
        user_profile,
        profile_guidance_map["adult"],
    )

    if not meaningful_shadier_route:
        route_message = (
            "No meaningfully shadier alternative was found within the selected "
            "detour limit. EcoTrace is showing the shortest valid route only."
        )

    if routing_note:
        route_message += " " + routing_note

    return annotate_route(RouteAnalysis(
        shortest_coordinates=baseline["coordinates"],
        shadiest_coordinates=shadiest["coordinates"],
        shortest_minutes=round(shortest_minutes, 1),
        shadiest_minutes=round(shadiest_minutes, 1),
        shortest_distance_km=round(baseline["distance_m"] / 1000.0, 2),
        shadiest_distance_km=round(shadiest["distance_m"] / 1000.0, 2),
        shortest_distance_miles=round(baseline["distance_m"] / 1609.344, 2),
        shadiest_distance_miles=round(shadiest["distance_m"] / 1609.344, 2),
        shortest_shade_percent=round(shortest_shade, 1),
        shadiest_shade_percent=round(shadiest_shade, 1),
        shade_gain_percent=round(shadiest_shade - shortest_shade, 1),
        detour_percent=round(detour_percent, 1),
        extra_minutes=round(shadiest_minutes - shortest_minutes, 1),
        detour_cap_percent=round(detour_cap * 100.0, 1),
        snapped_start=[
            round(baseline["coordinates"][0][0], 6),
            round(baseline["coordinates"][0][1], 6),
        ],
        snapped_end=[
            round(baseline["coordinates"][-1][0], 6),
            round(baseline["coordinates"][-1][1], 6),
        ],
        message=(
            route_message
            + f" Personalized times use a {pace_mph:.1f} mph walking pace. "
            + " Start and destination coordinates are automatically snapped to "
              "the nearest routable pedestrian access when needed. Segment "
              "explanations are generated from modeled heat, local variation, "
              "street direction, and the selected departure time."
        ),
        personalized_pace_mph=round(pace_mph, 2),
        departure_time=departure.isoformat(timespec="minutes"),
        shortest_segments=shortest_segment_results,
        shadiest_segments=shadiest_segment_results,
        time_of_day_used=use_time_of_day,
        pace_warning=pace_warning,
        weather=weather,
        route_limit_mode=route_limit_mode,
        route_limit_description=route_limit_description,
        shortest_route_why=shortest_why,
        shadiest_route_why=shadiest_why,
        shortest_canopy_percent=round(
            route_component_average(shortest_segment_results, "canopy_shadow_percent"),
            1,
        ),
        shadiest_canopy_percent=round(
            route_component_average(shadiest_segment_results, "canopy_shadow_percent"),
            1,
        ),
        shortest_building_shadow_percent=round(
            route_component_average(shortest_segment_results, "building_shadow_percent"),
            1,
        ),
        shadiest_building_shadow_percent=round(
            route_component_average(shadiest_segment_results, "building_shadow_percent"),
            1,
        ),
        shortest_terrain_shadow_percent=round(
            route_component_average(shortest_segment_results, "terrain_shadow_percent"),
            1,
        ),
        shadiest_terrain_shadow_percent=round(
            route_component_average(shadiest_segment_results, "terrain_shadow_percent"),
            1,
        ),
        canopy_model_available=CANOPY_ENGINE.available,
        user_profile=user_profile,
        profile_guidance=profile_guidance,
        meaningful_shadier_route=meaningful_shadier_route,
        canopy_model_summary=(
            (
                f"Mapped shade model loaded from {Path(CANOPY_MODEL_PATH).name}: "
                f"{len(CANOPY_ENGINE.trees):,} trees and "
                f"{len(CANOPY_ENGINE.buildings):,} buildings."
                + (
                    " Official Santa Clara County building footprints/heights are active."
                    if Path(CANOPY_MODEL_PATH).name == "canopy_model_county_buildings.json"
                    else ""
                )
            )
            if CANOPY_ENGINE.available
            else "No canopy model was loaded; EcoTrace used the fallback heat-orientation model."
        ),
    ), departure, use_time_of_day, bike=False)



def _inside_district(latitude: float, longitude: float, padding: float = 0.03) -> bool:
    return (
        DISTRICT_BOUNDS["min_lat"] - padding <= latitude <= DISTRICT_BOUNDS["max_lat"] + padding
        and DISTRICT_BOUNDS["min_lon"] - padding <= longitude <= DISTRICT_BOUNDS["max_lon"] + padding
    )



def _geocoder_query_variants(query: str) -> list[str]:
    """Build local search variants without replacing the user's actual address."""
    cleaned = " ".join(query.strip().split())
    if not cleaned:
        return []

    lowered = cleaned.lower()
    local_names = (
        "santa clara", "sunnyvale", "cupertino", "milpitas",
        "san jose", "california", ", ca", " ca "
    )

    variants = [cleaned]

    # Some upstream geocoders occasionally rank oddly-cased street input
    # differently. Add a harmless title-case retry without changing what the
    # user sees or stores.
    title_case = cleaned.title()
    if title_case.casefold() == cleaned.casefold() and title_case != cleaned:
        variants.append(title_case)

    if not any(name in lowered for name in local_names):
        variants.append(f"{cleaned}, Santa Clara County, California")
        if title_case != cleaned:
            variants.append(f"{title_case}, Santa Clara County, California")

    # Common short local landmark searches benefit from a county-qualified retry.
    if len(cleaned.split()) <= 4:
        variants.append(f"{cleaned}, CA")

    unique: list[str] = []
    seen: set[str] = set()
    for value in variants:
        key = value.casefold()
        if key not in seen:
            seen.add(key)
            unique.append(value)
    return unique


def _geocode_rank(item: dict[str, Any], original_query: str) -> float:
    """Prefer exact addresses and named POIs over generic city-center matches."""
    display = str(item.get("display_name") or "").casefold()
    name = str(item.get("name") or "").casefold()
    item_type = str(item.get("type") or "").casefold()
    item_class = str(item.get("class") or item.get("category") or "").casefold()
    query = " ".join(original_query.casefold().split())
    tokens = [token for token in query.replace(",", " ").split() if len(token) > 1]

    score = float(item.get("importance") or 0.0) * 8.0

    if query and (query == name or display.startswith(query + ",")):
        score += 18.0
    score += sum(1.6 for token in tokens if token in display)

    # Strong preference for precise, useful destinations.
    if item_type in {"house", "residential", "building", "school", "university",
                     "college", "library", "station", "bus_stop", "park",
                     "stadium", "supermarket", "restaurant", "hospital"}:
        score += 8.0
    if item_class in {"amenity", "building", "shop", "tourism", "leisure",
                      "railway", "highway", "office", "place"}:
        score += 3.0

    # A numbered query is probably an address; city-only results are bad matches.
    contains_number = any(character.isdigit() for character in query)
    if contains_number and item_type in {"city", "town", "village", "administrative"}:
        score -= 30.0
    if len(tokens) >= 2 and item_type in {"city", "town", "administrative"}:
        score -= 12.0

    return score



def _mapbox_access_token() -> str:
    """Accept the common environment-variable names users already use for Mapbox."""
    for name in (
        "MAPBOX_ACCESS_TOKEN",
        "MAPBOX_TOKEN",
        "MAPBOX_API_KEY",
        "MAPBOX_KEY",
    ):
        value = os.getenv(name, "").strip()
        if value:
            return value
    return ""


def mapbox_geocode(query: str) -> GeocodeResult | None:
    """
    Primary production geocoder.

    Uses Mapbox Geocoding API v6 for both normal street addresses and landmarks.
    No residential address is resolved from EcoTrace's hard-coded alias dictionary.
    """
    cleaned = " ".join(query.strip().split())
    token = _mapbox_access_token()
    if not cleaned or not token:
        return None

    lowered = cleaned.lower()
    city_markers = (
        "santa clara",
        "sunnyvale",
        "cupertino",
        "milpitas",
        "san jose",
        "campbell",
    )

    # For an address like "481 Rosemont Drive", bias it toward Santa Clara,
    # while preserving the exact text the user typed as the first attempt.
    variants = [cleaned]
    if (
        re.match(r"^\d+\s+\S+", cleaned)
        and not any(city in lowered for city in city_markers)
    ):
        variants.insert(0, f"{cleaned}, Santa Clara, CA")

    for search_text in variants:
        params = {
            "q": search_text,
            "access_token": token,
            "country": "US",
            "language": "en",
            "limit": 5,
            # Center of the EcoTrace study area; only a ranking bias.
            "proximity": "-121.98,37.36",
            "bbox": (
                f'{DISTRICT_BOUNDS["min_lon"] - 0.12},'
                f'{DISTRICT_BOUNDS["min_lat"] - 0.12},'
                f'{DISTRICT_BOUNDS["max_lon"] + 0.12},'
                f'{DISTRICT_BOUNDS["max_lat"] + 0.12}'
            ),
        }

        try:
            response = requests.get(
                "https://api.mapbox.com/search/geocode/v6/forward",
                params=params,
                headers={
                    "Accept": "application/json",
                    "User-Agent": (
                        "EcoTrace-CA17/6.9 "
                        "(student Congressional App Challenge project)"
                    ),
                },
                timeout=(4, 8),
                verify=certifi.where(),
            )
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError):
            continue

        features = payload.get("features", []) if isinstance(payload, dict) else []
        if not isinstance(features, list):
            continue

        for feature in features:
            if not isinstance(feature, dict):
                continue

            properties = feature.get("properties") or {}
            geometry = feature.get("geometry") or {}
            coordinates = geometry.get("coordinates") or []

            try:
                longitude = float(coordinates[0])
                latitude = float(coordinates[1])
            except (IndexError, TypeError, ValueError):
                # v6 also exposes coordinates under properties.coordinates.
                prop_coords = properties.get("coordinates") or {}
                try:
                    longitude = float(prop_coords["longitude"])
                    latitude = float(prop_coords["latitude"])
                except (KeyError, TypeError, ValueError):
                    continue

            if not _inside_district(latitude, longitude, padding=0.12):
                continue

            display_name = (
                properties.get("full_address")
                or properties.get("name_preferred")
                or properties.get("name")
                or properties.get("place_formatted")
                or search_text
            )

            # Some results split name and place context.
            if (
                display_name == properties.get("name")
                and properties.get("place_formatted")
            ):
                display_name = (
                    f'{properties.get("name")}, '
                    f'{properties.get("place_formatted")}'
                )

            return GeocodeResult(
                query=query,
                latitude=latitude,
                longitude=longitude,
                display_name=str(display_name),
                source="Mapbox Geocoding API v6",
            )

    return None


def remote_geocode_results(query: str, limit: int = 8) -> list[GeocodeResult]:
    """Search all OSM-indexed addresses and landmarks inside the study area."""
    cleaned = " ".join(query.strip().split())
    if not cleaned:
        return []

    collected: list[tuple[float, GeocodeResult]] = []
    seen_coordinates: set[tuple[int, int]] = set()

    for search_query in _geocoder_query_variants(cleaned):
        params = {
            "q": search_query,
            "format": "jsonv2",
            "limit": max(8, limit),
            "countrycodes": "us",
            "addressdetails": 1,
            "namedetails": 1,
            "extratags": 1,
            "dedupe": 1,
            "viewbox": (
                f'{DISTRICT_BOUNDS["min_lon"]},{DISTRICT_BOUNDS["max_lat"]},'
                f'{DISTRICT_BOUNDS["max_lon"]},{DISTRICT_BOUNDS["min_lat"]}'
            ),
            "bounded": 0,
        }

        try:
            response = requests.get(
                "https://nominatim.openstreetmap.org/search",
                params=params,
                headers={
                    "User-Agent": (
                        "EcoTrace-CA17/4.1 "
                        "(student Congressional App Challenge project; local geocoding)"
                    ),
                    "Accept": "application/json",
                    "Accept-Language": "en",
                },
                timeout=(4, 8),
                verify=certifi.where(),
            )
            response.raise_for_status()
            results = response.json()
        except (requests.RequestException, ValueError):
            continue

        if not isinstance(results, list):
            continue

        for item in results:
            try:
                latitude = float(item["lat"])
                longitude = float(item["lon"])
            except (KeyError, TypeError, ValueError):
                continue

            if not _inside_district(latitude, longitude, padding=0.12):
                continue

            coordinate_key = (round(latitude * 100000), round(longitude * 100000))
            if coordinate_key in seen_coordinates:
                continue
            seen_coordinates.add(coordinate_key)

            display_name = str(
                item.get("display_name")
                or (item.get("namedetails") or {}).get("name")
                or cleaned
            )
            result = GeocodeResult(
                query=query,
                latitude=latitude,
                longitude=longitude,
                display_name=display_name,
                source="OpenStreetMap address and landmark search",
            )
            collected.append((_geocode_rank(item, cleaned), result))

    collected.sort(key=lambda pair: pair[0], reverse=True)
    return [result for _, result in collected[:limit]]


def census_geocode_address(query: str) -> GeocodeResult | None:
    """
    Secondary U.S. address fallback using the Census Geocoder.
    It is used only for address-like input after the normal OSM search fails.
    """
    cleaned = " ".join(query.strip().split())
    if not cleaned:
        return None

    # Census geocoding is for structured addresses, not landmark names.
    if not re.match(r"^\d+\s+\S+", cleaned):
        return None

    variants = [cleaned]
    title_case = cleaned.title()
    if title_case != cleaned:
        variants.append(title_case)
    lowered = cleaned.lower()
    city_markers = (
        "santa clara",
        "sunnyvale",
        "cupertino",
        "milpitas",
        "san jose",
        "campbell",
    )
    if not any(marker in lowered for marker in city_markers):
        variants.append(f"{cleaned}, Santa Clara, CA")

    for address in variants:
        try:
            response = requests.get(
                "https://geocoding.geo.census.gov/geocoder/locations/onelineaddress",
                params={
                    "address": address,
                    "benchmark": "Public_AR_Current",
                    "format": "json",
                },
                headers={
                    "User-Agent": (
                        "EcoTrace-CA17/6.9 "
                        "(student Congressional App Challenge project)"
                    ),
                    "Accept": "application/json",
                },
                timeout=(5, 10),
                verify=certifi.where(),
            )
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError):
            continue

        matches = (
            payload.get("result", {}).get("addressMatches", [])
            if isinstance(payload, dict)
            else []
        )
        if not matches:
            continue

        match = matches[0]
        coordinates = match.get("coordinates") or {}
        try:
            longitude = float(coordinates["x"])
            latitude = float(coordinates["y"])
        except (KeyError, TypeError, ValueError):
            continue

        if not _inside_district(latitude, longitude, padding=0.12):
            continue

        display_name = str(
            match.get("matchedAddress")
            or address
        ).title()

        return GeocodeResult(
            query=query,
            latitude=latitude,
            longitude=longitude,
            display_name=display_name,
            source="U.S. Census address geocoder fallback",
        )

    return None


def try_remote_geocode(query: str) -> GeocodeResult | None:
    """
    Geocoding priority:
      1. Mapbox v6 (primary)
      2. U.S. Census (numbered U.S. address fallback)
      3. OpenStreetMap/Nominatim (final fallback)

    This deliberately prevents hard-coded residential coordinates from
    overriding a real geocoder result.
    """
    cleaned = " ".join(query.strip().split())
    if not cleaned:
        return None

    mapbox_result = mapbox_geocode(cleaned)
    if mapbox_result:
        return mapbox_result

    census_result = census_geocode_address(cleaned)
    if census_result:
        return census_result

    variants = [cleaned]
    lowered = cleaned.lower()
    if (
        re.match(r"^\d+\s+\S+", cleaned)
        and not any(
            city in lowered
            for city in (
                "santa clara",
                "sunnyvale",
                "cupertino",
                "milpitas",
                "san jose",
                "campbell",
            )
        )
    ):
        variants.insert(0, f"{cleaned}, Santa Clara, CA")

    for variant in variants:
        results = remote_geocode_results(variant, limit=8)
        if results:
            first = results[0]
            return GeocodeResult(
                query=query,
                latitude=first.latitude,
                longitude=first.longitude,
                display_name=first.display_name,
                source=first.source,
            )

    return None



AUTH_DB_PATH = Path(
    os.getenv("ECOTRACE_AUTH_DB", "ecotrace_auth.sqlite3")
).expanduser()


def initialize_auth_database() -> None:
    AUTH_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(AUTH_DB_PATH) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE COLLATE NOCASE,
                password_salt TEXT NOT NULL,
                password_hash TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )

        existing = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(users)").fetchall()
        }
        additions = {
            "full_name": "TEXT",
            "nickname": "TEXT",
            "display_name": "TEXT",
            "role": "TEXT",
            "organization": "TEXT",
            "education_work": "TEXT",
            "bio": "TEXT",
            "share_history": "INTEGER NOT NULL DEFAULT 0",
        }
        for column, ddl in additions.items():
            if column not in existing:
                connection.execute(f"ALTER TABLE users ADD COLUMN {column} {ddl}")

        # Preserve existing original accounts: use username as initial name/display
        # until the user customizes the profile.
        connection.execute(
            """
            UPDATE users
            SET full_name=COALESCE(NULLIF(full_name,''), username),
                display_name=COALESCE(NULLIF(display_name,''), username),
                role=COALESCE(NULLIF(role,''), 'student')
            """
        )
        connection.execute(
            """
            UPDATE users
            SET display_name=substr(display_name,1,40)
            WHERE length(display_name)>40
            """
        )

        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS auth_sessions (
                token_hash TEXT PRIMARY KEY,
                username TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS validation_reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                reporter_username TEXT,
                display_name TEXT,
                report_type TEXT NOT NULL,
                location TEXT,
                note TEXT NOT NULL,
                evidence_url TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                reviewer_note TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                reviewed_at TEXT
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS validation_checks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                reviewer_username TEXT NOT NULL,
                check_type TEXT NOT NULL,
                result TEXT NOT NULL,
                location TEXT,
                note TEXT,
                source_report_id INTEGER,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        connection.commit()


initialize_auth_database()


def hash_password(password: str, salt_hex: str) -> str:
    return hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        bytes.fromhex(salt_hex),
        180_000,
    ).hex()


def _auth_token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _store_auth_session(token: str, username: str) -> None:
    with sqlite3.connect(AUTH_DB_PATH) as connection:
        connection.execute(
            """
            INSERT OR REPLACE INTO auth_sessions(token_hash, username, created_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
            """,
            (_auth_token_hash(token), username),
        )
        connection.commit()


def current_username(request: Request) -> str | None:
    token = request.cookies.get("ecotrace_session")
    if not token:
        return None
    with sqlite3.connect(AUTH_DB_PATH) as connection:
        row = connection.execute(
            "SELECT username FROM auth_sessions WHERE token_hash=?",
            (_auth_token_hash(token),),
        ).fetchone()
    return str(row[0]) if row else None


def _original_trusted_reviewer(username: str) -> bool:
    allowed = {
        value.strip().lower()
        for value in os.getenv("ECOTRACE_REVIEWER_USERS", "").split(",")
        if value.strip()
    }
    return username.strip().lower() in allowed


def _profile_for_username(username: str) -> dict[str, Any] | None:
    with sqlite3.connect(AUTH_DB_PATH) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            """
            SELECT username, full_name, nickname, display_name, role,
                   organization, education_work, bio, share_history
            FROM users
            WHERE username=? COLLATE NOCASE
            """,
            (username,),
        ).fetchone()
    if not row:
        return None
    return {
        "username": row["username"],
        "full_name": row["full_name"] or row["username"],
        "nickname": row["nickname"],
        "display_name": row["display_name"] or row["username"],
        "role": row["role"] or "student",
        "organization": row["organization"],
        "education_work": row["education_work"],
        "bio": row["bio"],
        "share_history": bool(row["share_history"]),
        "trusted_reviewer": _original_trusted_reviewer(row["username"]),
    }


def welcome_page(error: str = "") -> str:
    safe_error = html.escape(error)
    error_html = (
        f'<p class="mt-4 rounded-xl bg-red-400/10 p-3 text-sm text-red-200">{safe_error}</p>'
        if safe_error
        else ""
    )

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>EcoTrace — Welcome</title>
  <script src="https://cdn.tailwindcss.com"></script>
</head>
<body class="min-h-screen bg-slate-950 text-white">
  <main class="flex min-h-screen items-center justify-center p-5">
    <section class="w-full max-w-md rounded-3xl border border-white/10 bg-slate-900 p-6 shadow-2xl">
      <div class="text-center">
        <div class="text-5xl">🌿</div>
        <h1 class="mt-3 text-3xl font-black">EcoTrace</h1>
        <p class="mt-2 text-sm leading-6 text-slate-300">
          Choose how you want to enter.
        </p>
      </div>

      <form action="/guest" method="post" class="mt-6">
        <button
          type="submit"
          class="w-full rounded-xl bg-white/10 px-4 py-3 text-sm font-black hover:bg-white/20"
        >
          Continue as Guest
        </button>
      </form>

      <details class="mt-4 rounded-2xl bg-black/20 p-4">
        <summary class="cursor-pointer text-sm font-black text-violet-200">
          Sign In
        </summary>
        <form action="/signin" method="post" class="mt-3">
          <input
            name="username"
            required
            minlength="2"
            maxlength="30"
            class="w-full rounded-xl border border-white/10 bg-slate-950 px-3 py-2 text-sm"
            placeholder="Username"
          >
          <input
            name="password"
            required
            type="password"
            minlength="4"
            maxlength="80"
            class="mt-2 w-full rounded-xl border border-white/10 bg-slate-950 px-3 py-2 text-sm"
            placeholder="Password"
          >
          <button
            type="submit"
            class="mt-3 w-full rounded-xl bg-violet-400 px-4 py-2.5 text-sm font-black text-violet-950"
          >
            Sign In
          </button>
        </form>
      </details>

      <details class="mt-3 rounded-2xl bg-black/20 p-4">
        <summary class="cursor-pointer text-sm font-black text-emerald-200">
          Create Account
        </summary>
        <form action="/register" method="post" class="mt-3">
          <input
            name="username"
            required
            minlength="2"
            maxlength="30"
            class="w-full rounded-xl border border-white/10 bg-slate-950 px-3 py-2 text-sm"
            placeholder="Username"
          >
          <input
            name="password"
            required
            type="password"
            minlength="4"
            maxlength="80"
            class="mt-2 w-full rounded-xl border border-white/10 bg-slate-950 px-3 py-2 text-sm"
            placeholder="Password"
          >
          <button
            type="submit"
            class="mt-3 w-full rounded-xl bg-emerald-400 px-4 py-2.5 text-sm font-black text-emerald-950"
          >
            Create Account
          </button>
        </form>
      </details>

      {error_html}

      <p class="mt-5 text-[10px] leading-4 text-amber-200">
        Local demo accounts are stored in EcoTrace's local SQLite database.
        Do not reuse an important password.
      </p>
    </section>
  </main>
</body>
</html>"""


@app.get("/", response_class=HTMLResponse)
async def welcome(
    error: str | None = Query(default=None),
) -> HTMLResponse:
    return HTMLResponse(welcome_page(error or ""))


@app.get("/app", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    mode: str = Query("guest"),
) -> HTMLResponse:
    username = current_username(request)

    page = HTML_PAGE
    return HTMLResponse(page, headers={"Cache-Control": "no-store"})



@app.post("/guest")
async def continue_as_guest(request: Request) -> RedirectResponse:
    token = request.cookies.get("ecotrace_session")
    if token:
        with sqlite3.connect(AUTH_DB_PATH) as connection:
            connection.execute(
                "DELETE FROM auth_sessions WHERE token_hash=?",
                (_auth_token_hash(token),),
            )
            connection.commit()

    response = RedirectResponse(
        url="/app?mode=guest",
        status_code=303,
    )
    response.delete_cookie("ecotrace_session")
    return response


@app.post("/register")
async def register_account(
    username: str = Form(..., min_length=2, max_length=30),
    password: str = Form(..., min_length=4, max_length=80),
) -> RedirectResponse:
    cleaned_username = " ".join(username.strip().split())

    salt = secrets.token_hex(16)
    password_hash = hash_password(password, salt)

    try:
        with sqlite3.connect(AUTH_DB_PATH) as connection:
            connection.execute(
                """
                INSERT INTO users (
                    username,
                    password_salt,
                    password_hash
                )
                VALUES (?, ?, ?)
                """,
                (
                    cleaned_username,
                    salt,
                    password_hash,
                ),
            )
    except sqlite3.IntegrityError:
        return RedirectResponse(
            url="/?error=That%20username%20already%20exists.",
            status_code=303,
        )

    token = secrets.token_urlsafe(32)
    _store_auth_session(token, cleaned_username)

    response = RedirectResponse(
        url="/app?mode=profile",
        status_code=303,
    )
    response.set_cookie(
        "ecotrace_session",
        token,
        httponly=True,
        samesite="lax",
        max_age=60 * 60 * 24 * 30,
    )
    return response


@app.post("/signin")
async def sign_in_account(
    username: str = Form(..., min_length=2, max_length=30),
    password: str = Form(..., min_length=4, max_length=80),
) -> RedirectResponse:
    cleaned_username = " ".join(username.strip().split())

    with sqlite3.connect(AUTH_DB_PATH) as connection:
        connection.row_factory = sqlite3.Row
        user = connection.execute(
            """
            SELECT username, password_salt, password_hash
            FROM users
            WHERE username = ? COLLATE NOCASE
            """,
            (cleaned_username,),
        ).fetchone()

    if user is None:
        return RedirectResponse(
            url="/?error=Username%20or%20password%20is%20incorrect.",
            status_code=303,
        )

    candidate_hash = hash_password(
        password,
        str(user["password_salt"]),
    )

    if not hmac.compare_digest(
        candidate_hash,
        str(user["password_hash"]),
    ):
        return RedirectResponse(
            url="/?error=Username%20or%20password%20is%20incorrect.",
            status_code=303,
        )

    token = secrets.token_urlsafe(32)
    _store_auth_session(token, str(user["username"]))

    response = RedirectResponse(
        url="/app?mode=profile",
        status_code=303,
    )
    response.set_cookie(
        "ecotrace_session",
        token,
        httponly=True,
        samesite="lax",
        max_age=60 * 60 * 24 * 30,
    )
    return response


class SwitchAccountRequest(BaseModel):
    username: str = Field(min_length=2, max_length=30)
    password: str = Field(min_length=4, max_length=80)


@app.post("/api/profile/switch-account")
async def switch_account(
    payload: SwitchAccountRequest,
    request: Request,
):
    cleaned_username = " ".join(payload.username.strip().split())

    with sqlite3.connect(AUTH_DB_PATH) as connection:
        connection.row_factory = sqlite3.Row
        user = connection.execute(
            """
            SELECT username, password_salt, password_hash
            FROM users
            WHERE username=? COLLATE NOCASE
            """,
            (cleaned_username,),
        ).fetchone()

    if user is None:
        raise HTTPException(status_code=401, detail="Username or password is incorrect.")

    candidate_hash = hash_password(payload.password, str(user["password_salt"]))
    if not hmac.compare_digest(candidate_hash, str(user["password_hash"])):
        raise HTTPException(status_code=401, detail="Username or password is incorrect.")

    old_token = request.cookies.get("ecotrace_session")
    if old_token:
        with sqlite3.connect(AUTH_DB_PATH) as connection:
            connection.execute(
                "DELETE FROM auth_sessions WHERE token_hash=?",
                (_auth_token_hash(old_token),),
            )
            connection.commit()

    token = secrets.token_urlsafe(32)
    _store_auth_session(token, str(user["username"]))

    response = JSONResponse({"ok": True, "username": str(user["username"])})
    response.set_cookie(
        "ecotrace_session",
        token,
        httponly=True,
        samesite="lax",
        max_age=60 * 60 * 24 * 30,
    )
    return response


@app.post("/logout")
async def logout_account(
    request: Request,
) -> RedirectResponse:
    token = request.cookies.get("ecotrace_session")
    if token:
        with sqlite3.connect(AUTH_DB_PATH) as connection:
            connection.execute(
                "DELETE FROM auth_sessions WHERE token_hash=?",
                (_auth_token_hash(token),),
            )
            connection.commit()

    response = RedirectResponse(
        url="/",
        status_code=303,
    )
    response.delete_cookie("ecotrace_session")
    return response



@app.get("/api/profile/me")
async def original_profile_me(request: Request) -> dict[str, Any]:
    username = current_username(request)
    if not username:
        raise HTTPException(status_code=401, detail="Not signed in.")
    profile = _profile_for_username(username)
    if not profile:
        raise HTTPException(status_code=404, detail="Account profile not found.")
    return profile


class OriginalProfileUpdate(BaseModel):
    full_name: str | None = Field(default=None, max_length=80)
    nickname: str | None = Field(default=None, max_length=50)
    display_name: str | None = Field(default=None, max_length=40)
    role: str | None = Field(default=None, max_length=40)
    organization: str | None = Field(default=None, max_length=120)
    education_work: str | None = Field(default=None, max_length=220)
    bio: str | None = Field(default=None, max_length=500)
    share_history: bool | None = None


@app.patch("/api/profile/me")
async def original_profile_update(
    payload: OriginalProfileUpdate,
    request: Request,
) -> dict[str, Any]:
    username = current_username(request)
    if not username:
        raise HTTPException(status_code=401, detail="Not signed in.")

    values = payload.model_dump(exclude_none=True)
    if "share_history" in values:
        values["share_history"] = int(bool(values["share_history"]))

    allowed = {
        "full_name","nickname","display_name","role","organization",
        "education_work","bio","share_history"
    }
    values = {key:value for key,value in values.items() if key in allowed}

    if values:
        sets = ", ".join(f"{key}=?" for key in values)
        params = list(values.values()) + [username]
        with sqlite3.connect(AUTH_DB_PATH) as connection:
            connection.execute(
                f"UPDATE users SET {sets} WHERE username=? COLLATE NOCASE",
                params,
            )
            connection.commit()

    profile = _profile_for_username(username)
    if not profile:
        raise HTTPException(status_code=404, detail="Account profile not found.")
    return profile


class PasswordChangeRequest(BaseModel):
    current_password: str = Field(min_length=4, max_length=80)
    new_password: str = Field(min_length=8, max_length=128)


@app.post("/api/profile/change-password")
async def original_profile_change_password(
    payload: PasswordChangeRequest,
    request: Request,
) -> dict[str, bool]:
    username = current_username(request)
    if not username:
        raise HTTPException(status_code=401, detail="Not signed in.")

    with sqlite3.connect(AUTH_DB_PATH) as connection:
        connection.row_factory = sqlite3.Row
        user = connection.execute(
            """
            SELECT password_salt, password_hash
            FROM users
            WHERE username=? COLLATE NOCASE
            """,
            (username,),
        ).fetchone()
        if not user:
            raise HTTPException(status_code=404, detail="Account not found.")

        candidate = hash_password(payload.current_password, str(user["password_salt"]))
        if not hmac.compare_digest(candidate, str(user["password_hash"])):
            raise HTTPException(status_code=401, detail="Current password is incorrect.")

        salt = secrets.token_hex(16)
        new_hash = hash_password(payload.new_password, salt)
        connection.execute(
            """
            UPDATE users
            SET password_salt=?, password_hash=?
            WHERE username=? COLLATE NOCASE
            """,
            (salt, new_hash, username),
        )
        connection.commit()

    return {"ok": True}


@app.get("/api/profile/public")
async def original_public_profile(display_name: str) -> dict[str, Any]:
    with sqlite3.connect(AUTH_DB_PATH) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            """
            SELECT display_name, role, organization, education_work, bio, share_history
            FROM users
            WHERE display_name=? COLLATE NOCASE
            ORDER BY id DESC LIMIT 1
            """,
            (display_name.strip(),),
        ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="No public EcoTrace profile found.")
    return {
        "display_name": row["display_name"],
        "role": row["role"] or "Not shared",
        "organization": row["organization"],
        "education_work": row["education_work"],
        "bio": row["bio"],
        "share_history": bool(row["share_history"]),
    }


class OriginalValidationReport(BaseModel):
    report_type: str = Field(max_length=40)
    location: str | None = Field(default=None, max_length=220)
    note: str = Field(min_length=3, max_length=1500)
    evidence_url: str | None = Field(default=None, max_length=500)
    display_name: str | None = Field(default=None, max_length=80)


class OriginalValidationCheck(BaseModel):
    check_type: str = Field(max_length=40)
    result: str = Field(pattern="^(pass|fail)$")
    location: str | None = Field(default=None, max_length=220)
    note: str | None = Field(default=None, max_length=1000)


class OriginalValidationReview(BaseModel):
    status: str = Field(pattern="^(verified|rejected)$")
    reviewer_note: str | None = Field(default=None, max_length=1000)
    count_as_check: bool = True
    check_result: str | None = Field(default=None, pattern="^(pass|fail)$")


def _require_original_reviewer(request: Request) -> str:
    username = current_username(request)
    if not username:
        raise HTTPException(status_code=401, detail="Sign in required.")
    if not _original_trusted_reviewer(username):
        raise HTTPException(status_code=403, detail="Trusted reviewer access required.")
    return username


@app.post("/api/account/validation/reports")
async def original_validation_report(
    payload: OriginalValidationReport,
    request: Request,
) -> dict[str, Any]:
    username = current_username(request)
    profile = _profile_for_username(username) if username else None
    display_name = (
        profile["display_name"]
        if profile
        else ((payload.display_name or "").strip() or "Anonymous reporter")
    )
    with sqlite3.connect(AUTH_DB_PATH) as connection:
        cursor = connection.execute(
            """
            INSERT INTO validation_reports(
                reporter_username, display_name, report_type, location,
                note, evidence_url, status
            ) VALUES(?,?,?,?,?,?,?)
            """,
            (
                username,
                display_name,
                payload.report_type,
                payload.location,
                payload.note,
                payload.evidence_url,
                "pending",
            ),
        )
        connection.commit()
    return {"ok": True, "id": cursor.lastrowid, "status": "pending"}


@app.get("/api/account/validation/reports")
async def original_validation_reports(request: Request) -> list[dict[str, Any]]:
    _require_original_reviewer(request)
    with sqlite3.connect(AUTH_DB_PATH) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """
            SELECT * FROM validation_reports
            ORDER BY CASE status WHEN 'pending' THEN 0 ELSE 1 END, id DESC
            LIMIT 500
            """
        ).fetchall()
    return [dict(row) for row in rows]


@app.post("/api/account/validation/checks")
async def original_validation_check(
    payload: OriginalValidationCheck,
    request: Request,
) -> dict[str, Any]:
    reviewer = _require_original_reviewer(request)
    with sqlite3.connect(AUTH_DB_PATH) as connection:
        cursor = connection.execute(
            """
            INSERT INTO validation_checks(
                reviewer_username, check_type, result, location, note
            ) VALUES(?,?,?,?,?)
            """,
            (
                reviewer,
                payload.check_type,
                payload.result,
                payload.location,
                payload.note,
            ),
        )
        connection.commit()
    return {"ok": True, "id": cursor.lastrowid}


@app.post("/api/account/validation/reports/{report_id}/review")
async def original_validation_review(
    report_id: int,
    payload: OriginalValidationReview,
    request: Request,
) -> dict[str, Any]:
    reviewer = _require_original_reviewer(request)
    with sqlite3.connect(AUTH_DB_PATH) as connection:
        connection.row_factory = sqlite3.Row
        report = connection.execute(
            "SELECT * FROM validation_reports WHERE id=?",
            (report_id,),
        ).fetchone()
        if not report:
            raise HTTPException(status_code=404, detail="Validation report not found.")

        connection.execute(
            """
            UPDATE validation_reports
            SET status=?, reviewer_note=?, reviewed_at=CURRENT_TIMESTAMP
            WHERE id=?
            """,
            (payload.status, payload.reviewer_note, report_id),
        )

        if payload.count_as_check and payload.check_result in {"pass", "fail"}:
            connection.execute(
                """
                INSERT INTO validation_checks(
                    reviewer_username, check_type, result, location,
                    note, source_report_id
                ) VALUES(?,?,?,?,?,?)
                """,
                (
                    reviewer,
                    report["report_type"],
                    payload.check_result,
                    report["location"],
                    payload.reviewer_note or report["note"],
                    report_id,
                ),
            )
        connection.commit()
    return {"ok": True, "status": payload.status}


@app.get("/api/account/validation/stats")
async def original_validation_stats() -> dict[str, Any]:
    with sqlite3.connect(AUTH_DB_PATH) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """
            SELECT check_type, result, COUNT(*) AS n
            FROM validation_checks
            GROUP BY check_type, result
            """
        ).fetchall()
        pending = connection.execute(
            "SELECT COUNT(*) AS n FROM validation_reports WHERE status='pending'"
        ).fetchone()["n"]

    grouped: dict[str, dict[str, int]] = {}
    for row in rows:
        grouped.setdefault(row["check_type"], {"pass": 0, "fail": 0})
        grouped[row["check_type"]][row["result"]] = row["n"]

    def one(kind: str) -> dict[str, Any]:
        item = grouped.get(kind, {"pass": 0, "fail": 0})
        total = item["pass"] + item["fail"]
        return {
            **item,
            "total": total,
            "rate_percent": round(item["pass"] / total * 100, 1) if total else None,
        }

    return {
        "tree": one("tree"),
        "route": one("route"),
        "bike": one("bike"),
        "pending_reports": pending,
    }


def _parse_wind_mph(value: str | None) -> float | None:
    if not value:
        return None
    numbers = re.findall(r"\d+(?:\.\d+)?", value)
    if not numbers:
        return None
    return sum(float(number) for number in numbers) / len(numbers)


_NWS_WEATHER_CACHE: dict[tuple[float, float], tuple[float, WeatherSnapshot]] = {}


def _qv_to_fahrenheit(qv: dict[str, Any] | None) -> float | None:
    if not isinstance(qv, dict):
        return None
    value = qv.get("value")
    if value is None:
        return None
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    unit = str(qv.get("unitCode") or "").lower()
    if "degc" in unit:
        return value * 9 / 5 + 32
    if "degf" in unit:
        return value
    return None


def _qv_to_mph(qv: dict[str, Any] | None) -> float | None:
    if not isinstance(qv, dict):
        return None
    value = qv.get("value")
    if value is None:
        return None
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    unit = str(qv.get("unitCode") or "").lower()
    if "km_h" in unit or "km-h" in unit:
        return value * 0.621371
    if "m_s" in unit or "m-s" in unit:
        return value * 2.23694
    if "mi_h" in unit or "mph" in unit:
        return value
    return None


def fetch_nws_weather_cached(
    latitude_3dp: float,
    longitude_3dp: float,
) -> WeatherSnapshot:
    """
    Fetch the latest NWS station observation near the point and combine it
    with the first NWS hourly forecast period for precipitation/conditions.

    This is current area-level weather, not a thermometer reading from the
    exact sidewalk. Cache lifetime is five minutes.
    """
    import time as _time

    cache_key = (latitude_3dp, longitude_3dp)
    cached = _NWS_WEATHER_CACHE.get(cache_key)
    if cached and (_time.time() - cached[0]) < 300:
        return cached[1]

    headers = {
        "User-Agent": (
            "EcoTrace-CA17/6.12 "
            "(student environmental routing prototype)"
        ),
        "Accept": "application/geo+json",
    }

    try:
        point_response = requests.get(
            f"https://api.weather.gov/points/{latitude_3dp},{longitude_3dp}",
            headers=headers,
            timeout=(5, 12),
            verify=certifi.where(),
        )
        point_response.raise_for_status()
        point_props = point_response.json().get("properties", {})

        # Latest nearby station observation.
        station_collection_url = point_props.get("observationStations")
        if not station_collection_url:
            raise ValueError("NWS point response did not contain observation stations.")

        station_response = requests.get(
            station_collection_url,
            headers=headers,
            timeout=(5, 12),
            verify=certifi.where(),
        )
        station_response.raise_for_status()
        station_features = station_response.json().get("features", [])
        if not station_features:
            raise ValueError("NWS returned no nearby observation stations.")

        observation_props = None
        station_name = None

        # Try a few nearby stations in case the closest one has a missing reading.
        for station_feature in station_features[:5]:
            station_props = station_feature.get("properties", {})
            station_id = (
                station_props.get("stationIdentifier")
                or str(station_feature.get("id") or "").rstrip("/").split("/")[-1]
            )
            if not station_id:
                continue

            try:
                obs_response = requests.get(
                    f"https://api.weather.gov/stations/{station_id}/observations/latest",
                    headers=headers,
                    timeout=(5, 12),
                    verify=certifi.where(),
                )
                obs_response.raise_for_status()
                candidate = obs_response.json().get("properties", {})
                if _qv_to_fahrenheit(candidate.get("temperature")) is not None:
                    observation_props = candidate
                    station_name = (
                        station_props.get("name")
                        or station_props.get("stationIdentifier")
                        or station_id
                    )
                    break
            except requests.RequestException:
                continue

        if observation_props is None:
            raise ValueError("No nearby NWS station returned a usable current temperature.")

        temperature_f = _qv_to_fahrenheit(observation_props.get("temperature"))
        heat_index_f = _qv_to_fahrenheit(observation_props.get("heatIndex"))
        wind_chill_f = _qv_to_fahrenheit(observation_props.get("windChill"))
        apparent_f = (
            heat_index_f
            if heat_index_f is not None
            else wind_chill_f
            if wind_chill_f is not None
            else temperature_f
        )
        wind_mph = _qv_to_mph(observation_props.get("windSpeed"))
        wind_direction_deg = (
            (observation_props.get("windDirection") or {}).get("value")
            if isinstance(observation_props.get("windDirection"), dict)
            else None
        )
        observed_at = observation_props.get("timestamp")

        # Hourly forecast is used only for precipitation and short-condition text.
        forecast_url = point_props.get("forecastHourly") or point_props.get("forecast")
        precip = None
        forecast = None
        period_name = None
        if forecast_url:
            try:
                forecast_response = requests.get(
                    forecast_url,
                    headers=headers,
                    timeout=(5, 12),
                    verify=certifi.where(),
                )
                forecast_response.raise_for_status()
                periods = forecast_response.json().get("properties", {}).get("periods", [])
                if periods:
                    period = periods[0]
                    precip = (period.get("probabilityOfPrecipitation") or {}).get("value")
                    forecast = str(period.get("shortForecast") or "") or None
                    period_name = period.get("name")
            except requests.RequestException:
                pass

        impacts: list[str] = []
        if apparent_f is not None and apparent_f >= 95:
            impacts.append("current heat makes shaded travel especially valuable")
        elif apparent_f is not None and apparent_f >= 85:
            impacts.append("current warm conditions increase the value of shade")
        if precip is not None and float(precip) >= 40:
            impacts.append("rain probability may affect travel comfort")
        if wind_mph is not None and wind_mph >= 18:
            impacts.append("stronger wind may affect cycling or walking comfort")
        if not impacts:
            impacts.append("no major current-weather adjustment is indicated")

        snapshot = WeatherSnapshot(
            available=True,
            source="National Weather Service latest station observation + hourly forecast",
            temperature_f=round(temperature_f, 1) if temperature_f is not None else None,
            apparent_temperature_f=round(apparent_f, 1) if apparent_f is not None else None,
            wind_speed_mph=round(wind_mph, 1) if wind_mph is not None else None,
            wind_direction=(
                f"{float(wind_direction_deg):.0f}°"
                if wind_direction_deg is not None
                else None
            ),
            precipitation_probability=(
                int(round(float(precip))) if precip is not None else None
            ),
            short_forecast=forecast,
            period_name=period_name,
            observed_at=observed_at,
            observation_station=station_name,
            impact_summary="; ".join(impacts).capitalize() + ".",
        )
        _NWS_WEATHER_CACHE[cache_key] = (_time.time(), snapshot)
        return snapshot

    except (requests.RequestException, ValueError, TypeError) as error:
        snapshot = WeatherSnapshot(
            available=False,
            source="National Weather Service",
            impact_summary=(
                "Live NWS observation could not be loaded, so EcoTrace used "
                "its modeled environmental exposure data."
            ),
        )
        _NWS_WEATHER_CACHE[cache_key] = (_time.time(), snapshot)
        return snapshot


def fetch_nws_weather(latitude: float, longitude: float) -> WeatherSnapshot:
    return fetch_nws_weather_cached(
        round(latitude, 3),
        round(longitude, 3),
    )


@app.get("/api/weather", response_model=WeatherSnapshot)
def current_weather(
    lat: float = Query(..., ge=37.20, le=37.55),
    lon: float = Query(..., ge=-122.20, le=-121.75),
) -> WeatherSnapshot:
    return fetch_nws_weather(lat, lon)




def _readable_reverse_name(payload: dict[str, Any]) -> str:
    """Build a concise human-readable place label from Nominatim data."""
    address = payload.get("address") or {}
    name = str(payload.get("name") or "").strip()
    house_number = str(address.get("house_number") or "").strip()
    road = str(
        address.get("road")
        or address.get("pedestrian")
        or address.get("footway")
        or address.get("cycleway")
        or ""
    ).strip()
    neighborhood = str(
        address.get("neighbourhood")
        or address.get("suburb")
        or ""
    ).strip()
    city = str(
        address.get("city")
        or address.get("town")
        or address.get("village")
        or "Santa Clara County"
    ).strip()

    parts: list[str] = []
    if name and name.casefold() not in {road.casefold(), neighborhood.casefold()}:
        parts.append(name)
    if road:
        street = f"{house_number} {road}".strip()
        if street.casefold() not in {item.casefold() for item in parts}:
            parts.append(street)
    elif neighborhood:
        parts.append(neighborhood)

    if city and city.casefold() not in {item.casefold() for item in parts}:
        parts.append(city)

    return ", ".join(parts[:3]).strip()


def _local_clicked_place_name(
    latitude: float,
    longitude: float,
) -> str | None:
    nearest = BIKE_INFRASTRUCTURE.nearest_named_way(
        latitude,
        longitude,
        search_radius_m=600.0,
    )
    if not nearest:
        return None

    distance = float(nearest.get("distance_meters", 0.0))
    road_name = _clean_area_name(nearest.get("name"))
    if not road_name:
        return None
    if distance <= 35:
        return f"Near {road_name}, Santa Clara County"
    return (
        f"Near {road_name}, Santa Clara County "
        f"({distance:.0f} m from clicked point)"
    )


@app.get("/api/reverse-geocode", response_model=GeocodeResult)
async def reverse_geocode(
    lat: float = Query(..., ge=37.20, le=37.55),
    lon: float = Query(..., ge=-122.20, le=-121.75),
) -> GeocodeResult:
    """
    Convert a clicked coordinate into a readable nearby address/place.

    Nominatim is attempted at several zoom levels. If it is unavailable,
    EcoTrace uses the closest named way from the locally generated OSM database.
    """
    headers = {
        "User-Agent": (
            "EcoTrace-CA17/5.6.2 "
            "(student Congressional App Challenge project)"
        ),
        "Accept-Language": "en",
    }

    for zoom in (18, 17, 16):
        try:
            response = requests.get(
                "https://nominatim.openstreetmap.org/reverse",
                params={
                    "lat": lat,
                    "lon": lon,
                    "format": "jsonv2",
                    "zoom": zoom,
                    "addressdetails": 1,
                    "namedetails": 1,
                },
                headers=headers,
                timeout=(5, 16),
                verify=certifi.where(),
            )
            response.raise_for_status()
            payload = response.json()
            display_name = _readable_reverse_name(payload)
            if display_name:
                return GeocodeResult(
                    query=f"{lat:.6f}, {lon:.6f}",
                    latitude=lat,
                    longitude=lon,
                    display_name=display_name,
                    source="OpenStreetMap reverse geocoding",
                )
        except (requests.RequestException, ValueError, TypeError):
            continue

    local_name = _local_clicked_place_name(lat, lon)
    if local_name:
        return GeocodeResult(
            query=f"{lat:.6f}, {lon:.6f}",
            latitude=lat,
            longitude=lon,
            display_name=local_name,
            source="Local OpenStreetMap road-name fallback",
        )

    return GeocodeResult(
        query=f"{lat:.6f}, {lon:.6f}",
        latitude=lat,
        longitude=lon,
        display_name="Selected map point in Santa Clara County",
        source="Coordinate-preserving fallback",
    )


@app.get("/api/geocode-suggestions")
async def geocode_suggestions(
    q: str = Query(..., min_length=2, max_length=160),
    limit: int = Query(6, ge=1, le=10),
) -> dict[str, Any]:
    normalized = " ".join(q.lower().strip().split())
    suggestions: list[GeocodeResult] = []

    canonical_query = normalize_landmark_query(q)
    if canonical_query != q.strip():
        canonical_result = try_remote_geocode(canonical_query)
        if canonical_result:
            suggestions.append(GeocodeResult(
                query=q,
                latitude=canonical_result.latitude,
                longitude=canonical_result.longitude,
                display_name="Santa Clara High School, 3000 Benton Street, Santa Clara, CA 95051",
                source="OpenStreetMap canonical landmark address",
            ))

    # Exact local aliases appear instantly, while every other result is searched
    # through the complete bounded OpenStreetMap address/POI index.
    if normalized in LOCAL_GEOCODER and canonical_query == q.strip():
        lat, lon, display_name = LOCAL_GEOCODER[normalized]
        suggestions.append(
            GeocodeResult(
                query=q,
                latitude=lat,
                longitude=lon,
                display_name=display_name,
                source="EcoTrace verified local alias",
            )
        )

    for result in remote_geocode_results(q, limit=limit):
        if all(
            _tree_qa_haversine_m(
                result.latitude, result.longitude,
                existing.latitude, existing.longitude,
            ) > 8
            for existing in suggestions
        ):
            suggestions.append(result)
        if len(suggestions) >= limit:
            break

    return {"query": q, "results": [item.model_dump() for item in suggestions]}


LANDMARK_CANONICAL_QUERIES: dict[str, str] = {
    "santa clara high": "3000 Benton Street, Santa Clara, CA 95051",
    "santa clara high school": "3000 Benton Street, Santa Clara, CA 95051",
    "schs": "3000 Benton Street, Santa Clara, CA 95051",
    "satna clara high": "3000 Benton Street, Santa Clara, CA 95051",
    "satna clara high school": "3000 Benton Street, Santa Clara, CA 95051",
    "cabrillo middle": "2550 Cabrillo Avenue, Santa Clara, CA 95051",
    "juan cabrillo middle": "2550 Cabrillo Avenue, Santa Clara, CA 95051",
    "juan cabrillo middle school": "2550 Cabrillo Avenue, Santa Clara, CA 95051",
    "central park": "909 Kiely Boulevard, Santa Clara, CA 95051",
    "santa clara central park": "909 Kiely Boulevard, Santa Clara, CA 95051",
    "central park library": "2635 Homestead Road, Santa Clara, CA 95051",
    "santa clara central park library": "2635 Homestead Road, Santa Clara, CA 95051",
    "briarwood elementary": "1930 Townsend Avenue, Santa Clara, CA 95051",
    "briarwood elementary school": "1930 Townsend Avenue, Santa Clara, CA 95051",
    "great america": "4701 Great America Parkway, Santa Clara, CA 95054",
    "sunnyvale middle": "1080 Mango Avenue, Sunnyvale, CA 94087",
    "sunnyvale middle school": "1080 Mango Avenue, Sunnyvale, CA 94087",
    "bracher": "2700 Chromite Drive, Santa Clara, CA 95051",
    "bracher elementary": "2700 Chromite Drive, Santa Clara, CA 95051",
    "bracher elementary school": "2700 Chromite Drive, Santa Clara, CA 95051",
    "california's great america": "4701 Great America Parkway, Santa Clara, CA 95054",
}


def normalize_landmark_query(query: str) -> str:
    normalized = " ".join(query.lower().strip().split())
    return LANDMARK_CANONICAL_QUERIES.get(normalized, query.strip())


@app.get("/api/geocode", response_model=GeocodeResult)
async def geocode(q: str = Query(..., min_length=2, max_length=160)) -> GeocodeResult:
    normalized = " ".join(q.lower().strip().split())
    address_like = bool(re.match(r"^\d+\s+\S+", q.strip()))

    # Numbered street addresses ALWAYS go through a real geocoder.
    # They are never resolved from LOCAL_GEOCODER.
    if address_like:
        remote = try_remote_geocode(q)
        if remote:
            return remote
        raise HTTPException(
            status_code=404,
            detail=(
                "Address not found by Mapbox, the U.S. Census fallback, or "
                "OpenStreetMap. Check the street spelling, city, and ZIP code."
            ),
        )

    # A small alias list is retained only for well-known local landmarks.
    if normalized in LOCAL_GEOCODER:
        lat, lon, display_name = LOCAL_GEOCODER[normalized]
        return GeocodeResult(
            query=q,
            latitude=lat,
            longitude=lon,
            display_name=display_name,
            source="EcoTrace verified local landmark alias",
        )

    # All other place/landmark text gets a real geocoder attempt.
    remote = try_remote_geocode(q)
    if remote:
        return remote

    # Last-resort conservative landmark alias matching.
    generic_names = {
        "santa clara",
        "sunnyvale",
        "cupertino",
        "milpitas",
        "north san jose",
    }
    for key, value in sorted(
        LOCAL_GEOCODER.items(),
        key=lambda item: len(item[0]),
        reverse=True,
    ):
        if (
            key in generic_names
            or len(key) < 6
            or re.match(r"^\d+\b", key)
        ):
            continue
        if normalized == key or normalized.startswith(key + " "):
            lat, lon, display_name = value
            return GeocodeResult(
                query=q,
                latitude=lat,
                longitude=lon,
                display_name=display_name,
                source="EcoTrace verified local landmark alias",
            )

    raise HTTPException(
        status_code=404,
        detail=(
            "Place not found. Try the full street address, city, state, and ZIP code."
        ),
    )


class CommunityReportReason(BaseModel):
    post_id: int
    reason: str = Field(min_length=3, max_length=1000)


@app.post("/api/community-report-reason")
async def save_community_report_reason(
    payload: CommunityReportReason,
    request: Request,
) -> dict[str, bool]:
    username = current_username(request)
    profile = _profile_for_username(username) if username else None
    reporter = (
        profile.get("display_name")
        if profile
        else "Guest"
    )
    with sqlite3.connect(AUTH_DB_PATH) as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS community_report_reasons (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                post_id INTEGER NOT NULL,
                reporter_username TEXT,
                reporter_display_name TEXT,
                reason TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        connection.execute(
            """
            INSERT INTO community_report_reasons(
                post_id, reporter_username, reporter_display_name, reason
            ) VALUES(?,?,?,?)
            """,
            (payload.post_id, username, reporter, payload.reason.strip()),
        )
        connection.commit()
    return {"ok": True}


@app.get("/api/heat-grid")
async def heat_grid() -> dict[str, Any]:
    return {
        "bounds": DISTRICT_BOUNDS,
        "anchors": HEAT_ANCHORS,
    }


@app.get("/api/analyze-zone", response_model=ZoneAnalysis)
async def analyze_zone(
    lat: float = Query(..., ge=37.20, le=37.55),
    lon: float = Query(..., ge=-122.20, le=-121.75),
) -> ZoneAnalysis:
    heat, temperature, nearest_name = interpolate_heat(lat, lon)
    heat_index = round(heat)
    if heat_index >= 85:
        priority = "High"
        zone_type = "High-heat urban surface"
    elif heat_index >= 68:
        priority = "Moderate"
        zone_type = "Mixed urban heat exposure"
    else:
        priority = "Low"
        zone_type = "Cooler or higher-canopy area"

    if heat_index >= 68:
        spots, nearest_outside = generate_planting_spots(lat, lon)
    else:
        spots, nearest_outside = [], None
    return ZoneAnalysis(
        clicked_latitude=lat,
        clicked_longitude=lon,
        zone_name=f"{nearest_name} influence area",
        zone_type=zone_type,
        heat_index=heat_index,
        estimated_temperature_f=round(temperature, 1),
        priority=priority,
        message=(
            f"EcoTrace interpolated this location from nearby city heat anchors. "
            f"Tree pins are limited to explicit parkway strips, planting beds, public easements, "
            f"and plaza planters; they are never placed in buildings, roads, or sidewalk walking surfaces."
        ),
        planting_spots=spots,
        nearest_outside_range=nearest_outside,
    )


@app.get("/api/shadiest-route", response_model=RouteAnalysis)
def shadiest_route(
    start_lat: float = Query(..., ge=37.20, le=37.55),
    start_lon: float = Query(..., ge=-122.20, le=-121.75),
    end_lat: float = Query(..., ge=37.20, le=37.55),
    end_lon: float = Query(..., ge=-122.20, le=-121.75),
    max_detour: float = Query(0.20, ge=0.0, le=0.30),
    pace_mph: float = Query(3.0, ge=0.5, le=15.0),
    departure: str | None = Query(None, max_length=32),
    use_time_of_day: bool = Query(True),
    max_extra_distance_miles: float | None = Query(None, ge=0.0, le=10.0),
    user_profile: str = Query("adult", max_length=32),
) -> RouteAnalysis:
    try:
        return ors_routes_analysis(
            (start_lat, start_lon),
            (end_lat, end_lon),
            max_detour,
            pace_mph,
            departure,
            use_time_of_day,
            max_extra_distance_miles,
            user_profile,
        )
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except RuntimeError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    except Exception as error:
        print(
            "Unexpected EcoTrace route error:",
            type(error).__name__,
            repr(error),
        )
        raise HTTPException(
            status_code=500,
            detail=(
                f"Unexpected route error: {type(error).__name__}: {error}"
            ),
        ) from error



@app.get("/api/bike-route", response_model=BikeRouteAnalysis)
def bike_route(
    start_lat: float = Query(..., ge=37.20, le=37.55),
    start_lon: float = Query(..., ge=-122.20, le=-121.75),
    end_lat: float = Query(..., ge=37.20, le=37.55),
    end_lon: float = Query(..., ge=-122.20, le=-121.75),
    bike_type: str = Query("regular"),
    speed_mph: float = Query(12.0, ge=3.0, le=35.0),
    max_detour: float = Query(0.15, ge=0.0, le=0.50),
    max_extra_distance_miles: float | None = Query(None, ge=0.0, le=20.0),
    departure: str | None = Query(None, max_length=32),
    use_time_of_day: bool = Query(True),
    quick: bool = Query(False),
) -> BikeRouteAnalysis:
    normalized = bike_type.strip().lower()
    if normalized not in {"regular", "road-bike", "e-bike"}:
        raise HTTPException(
            status_code=422,
            detail="bike_type must be regular, road-bike, or e-bike.",
        )
    try:
        return bike_routes_analysis(
            (start_lat, start_lon),
            (end_lat, end_lon),
            bike_type=normalized,
            speed_mph=speed_mph,
            max_detour=max_detour,
            max_extra_distance_miles=max_extra_distance_miles,
            departure_value=departure,
            use_time_of_day=use_time_of_day,
            quick_mode=quick,
        )
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except RuntimeError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    except Exception as error:
        print("Unexpected bike route error:", type(error).__name__, repr(error))
        raise HTTPException(
            status_code=500,
            detail=f"Unexpected bike route error: {type(error).__name__}: {error}",
        ) from error


@app.get("/api/arcgis-tree-status")
async def arcgis_tree_status() -> dict[str, Any]:
    site_path = Path(
        os.getenv(
            "ECOTRACE_SITE_DB",
            "proposed_tree_sites_arcgis_strict.json",
        )
    )
    canopy_path = Path(CANOPY_MODEL_PATH)
    inventory_path = Path("merged_tree_inventory.json")

    status: dict[str, Any] = {
        "arcgis_site_database_active": (
            site_path.exists()
            and site_path.name == "proposed_tree_sites_arcgis_strict.json"
        ),
        "site_database_path": str(site_path),
        "arcgis_canopy_active": canopy_path.name in {
            "canopy_model_arcgis.json",
            "canopy_model_county_buildings.json",
        },
        "county_buildings_active": canopy_path.name == "canopy_model_county_buildings.json",
        "canopy_model_path": str(canopy_path),
        "loaded_tree_count": len(CANOPY_ENGINE.trees),
        "loaded_building_count": len(CANOPY_ENGINE.buildings),
        "environmental_fusion": environmental_status(),
        "merged_inventory_exists": inventory_path.exists(),
        "fallback_safe": True,
    }

    if site_path.exists():
        try:
            payload = json.loads(site_path.read_text(encoding="utf-8"))
            status["screened_site_count"] = len(payload.get("sites") or [])
            status["site_metadata"] = payload.get("metadata") or {}
        except Exception:
            status["screened_site_count"] = None

    if inventory_path.exists():
        try:
            payload = json.loads(inventory_path.read_text(encoding="utf-8"))
            status["merged_tree_count"] = len(payload.get("trees") or [])
            status["inventory_metadata"] = payload.get("metadata") or {}
        except Exception:
            status["merged_tree_count"] = None

    return status


ASSISTANT_PROMPT_LIBRARY: list[str] = [
    "Why did EcoTrace choose the green route?",
    "Why is the blue route faster?",
    "Which route has more mapped tree-canopy shade?",
    "How much shade comes from buildings?",
    "Does topography affect this trip?",
    "What is the sun position for this route?",
    "Which segment is most exposed?",
    "Why is a segment red?",
    "Why is a segment yellow?",
    "Why is a segment green?",
    "How accurate is the canopy model?",
    "What happens when tree dimensions are missing?",
    "How does tree opacity affect shade?",
    "How does building height affect shade?",
    "Would leaving later change the result?",
    "What happens when I ignore time of day?",
    "How does heat change the shade score?",
    "How does weather affect this walk?",
    "How does my walking pace affect total exposure?",
    "What route limit is active?",
    "Why was this tree location selected?",
    "Why was this tree species selected?",
    "Is this planting site legally approved?",
    "Which tree site is nearest?",
    "Which tree site has the strongest justification?",
    "What data is directly mapped?",
    "What data is inferred?",
    "What are the biggest limitations?",
    "Summarize the route for a judge.",
    "Summarize the tree recommendation for a city planner.",
    "Which parts of my bike route have protected or painted infrastructure?",
    "Compare bike infrastructure on the original and cooler routes.",
    "What does gray or unknown bike infrastructure mean?",
    "How should I compare EcoTrace bike colors with the VTA/511 map?",
    "If these route tree sites were planted, how much could mature shade improve?",
    "What does the community planning context mean here?",
    "How does this area's Census context compare with the county?",
    "Is the bike classification coming from VTA or the OSM fallback?",
]


def _contains_any(question: str, phrases: tuple[str, ...]) -> bool:
    return any(phrase in question for phrase in phrases)



TREE_SERVER_QA_EXCLUSIONS = [
    # User-verified bad point: center of a street intersection.
    (37.2947222, -121.8525278, 18.0),
]


def _local_xy_m(lat: float, lon: float, ref_lat: float) -> tuple[float, float]:
    scale_x = 111_320.0 * max(math.cos(math.radians(ref_lat)), 0.2)
    return lon * scale_x, lat * 111_320.0


def _point_to_route_metrics_m(
    latitude: float,
    longitude: float,
    coordinates: list[list[float]],
) -> tuple[float, float, float]:
    """
    Return:
      distance_to_route_m,
      progress_along_route_m,
      total_route_length_m

    Progress lets EcoTrace distribute planting candidates along the trip rather
    than returning a dense cluster simply because many candidates happen to
    exist near one short route section.
    """
    if len(coordinates) < 2:
        return float("inf"), 0.0, 0.0

    ref_lat = latitude
    px, py = _local_xy_m(latitude, longitude, ref_lat)

    segments: list[tuple[float, float, float, float, float]] = []
    total_length = 0.0
    for a, b in zip(coordinates, coordinates[1:]):
        if len(a) < 2 or len(b) < 2:
            continue
        ax, ay = _local_xy_m(float(a[1]), float(a[0]), ref_lat)
        bx, by = _local_xy_m(float(b[1]), float(b[0]), ref_lat)
        length = math.hypot(bx - ax, by - ay)
        segments.append((ax, ay, bx, by, length))
        total_length += length

    best_distance = float("inf")
    best_progress = 0.0
    cumulative = 0.0

    for ax, ay, bx, by, length in segments:
        abx, aby = bx - ax, by - ay
        denom = abx * abx + aby * aby
        t = 0.0 if denom <= 0 else max(
            0.0,
            min(1.0, ((px - ax) * abx + (py - ay) * aby) / denom),
        )
        nx, ny = ax + t * abx, ay + t * aby
        distance = math.hypot(px - nx, py - ny)
        if distance < best_distance:
            best_distance = distance
            best_progress = cumulative + t * length
        cumulative += length

    return best_distance, best_progress, total_length


def _point_to_route_distance_m(
    latitude: float,
    longitude: float,
    coordinates: list[list[float]],
) -> float:
    return _point_to_route_metrics_m(
        latitude,
        longitude,
        coordinates,
    )[0]


def _server_tree_qa_ok(latitude: float, longitude: float) -> bool:
    for bad_lat, bad_lon, radius_m in TREE_SERVER_QA_EXCLUSIONS:
        if _tree_qa_haversine_m(latitude, longitude, bad_lat, bad_lon) <= radius_m:
            return False
    return True


@app.post("/api/route-planting-sites")
async def route_planting_sites(payload: RoutePlantingRequest) -> list[dict[str, Any]]:
    """
    Return screened EcoTrace candidates near an already-calculated route.

    This does NOT create new coordinates. It only filters the authoritative
    reviewed candidate database by distance to the route geometry.
    """
    coordinates = payload.coordinates
    if len(coordinates) < 2:
        raise HTTPException(status_code=422, detail="Route must contain at least two coordinates.")

    candidates: list[tuple[float, float, float, dict[str, Any]]] = []
    route_length_m = 0.0

    for site in PLANTING_SITES:
        try:
            lat = float(site["lat"])
            lon = float(site["lon"])
        except (KeyError, TypeError, ValueError):
            continue

        if not _server_tree_qa_ok(lat, lon):
            continue

        distance, progress, total_length = _point_to_route_metrics_m(
            lat,
            lon,
            coordinates,
        )
        route_length_m = max(route_length_m, total_length)

        if distance <= payload.buffer_meters:
            try:
                canopy_fraction = float(site.get("wri_canopy_fraction_10m"))
            except (TypeError, ValueError):
                canopy_fraction = None
            try:
                canopy_height = float(site.get("wri_max_canopy_height_m"))
            except (TypeError, ValueError):
                canopy_height = None

            if (
                canopy_fraction is not None
                and canopy_height is not None
                and canopy_fraction >= 0.15
                and canopy_height >= 2.5
            ):
                continue

            # Higher confidence is only a tie-breaker; geometric validity and
            # route distribution remain the primary selection rules.
            confidence = str(site.get("evidence_confidence") or "").lower()
            confidence_rank = {
                "high": 0.0,
                "moderate": 1.0,
                "medium": 1.0,
                "low": 2.0,
            }.get(confidence, 1.5)
            candidates.append((progress, distance, confidence_rank, site))

    # Spread recommendations along the route rather than allowing one dense
    # candidate-rich neighborhood to dominate the results.
    if route_length_m > 0:
        desired_spacing_m = max(
            180.0,
            min(400.0, route_length_m / max(payload.max_sites, 1)),
        )
    else:
        desired_spacing_m = 250.0

    bins: dict[int, tuple[float, float, float, dict[str, Any]]] = {}
    for candidate in candidates:
        progress, distance, confidence_rank, site = candidate
        bin_index = int(progress // desired_spacing_m)
        existing = bins.get(bin_index)

        # Prefer closer-to-route candidates, then stronger evidence.
        score = (distance, confidence_rank)
        if existing is None or score < (existing[1], existing[2]):
            bins[bin_index] = candidate

    distributed = sorted(
        bins.values(),
        key=lambda item: item[0],
    )

    # Final 60 m pin-spacing guard handles candidates that fall on opposite
    # sides of a bin boundary.
    selected: list[tuple[float, float, float, dict[str, Any]]] = []
    for candidate in distributed:
        progress, distance, confidence_rank, site = candidate
        lat = float(site["lat"])
        lon = float(site["lon"])

        if any(
            _tree_qa_haversine_m(
                lat,
                lon,
                float(other_site["lat"]),
                float(other_site["lon"]),
            ) < 60.0
            for _, _, _, other_site in selected
        ):
            continue

        selected.append(candidate)
        if len(selected) >= payload.max_sites:
            break

    output: list[dict[str, Any]] = []
    for progress, distance, confidence_rank, site in selected:
        output.append({
            "candidate_id": str(site.get("candidate_id") or site.get("site_id") or ""),
            "latitude": round(float(site["lat"]), 7),
            "longitude": round(float(site["lon"]), 7),
            "distance_from_route_meters": round(distance, 1),
            "progress_along_route_meters": round(progress, 1),
            "route_length_meters": round(route_length_m, 1),
            "site_type": str(site.get("type") or site.get("site_type") or "planting opportunity"),
            "area_name": str(site.get("area_name") or ""),
            "evidence_confidence": str(site.get("evidence_confidence") or ""),
            "wri_canopy_fraction_10m": site.get("wri_canopy_fraction_10m"),
            "wri_max_canopy_height_m": site.get("wri_max_canopy_height_m"),
            "why_chosen": str(site.get("why_chosen") or ""),
            **public_tree_details(site),
        })
    return output


@app.get("/api/assistant-prompts")
async def assistant_prompts() -> dict[str, Any]:
    return {"prompts": ASSISTANT_PROMPT_LIBRARY, "count": len(ASSISTANT_PROMPT_LIBRARY)}


@app.post("/api/assistant")
async def ecotrace_assistant(payload: dict[str, Any]) -> dict[str, str]:
    question = " ".join(str(payload.get("question") or "").lower().split())
    route = payload.get("route") or {}
    analysis = payload.get("analysis") or {}
    sites = analysis.get("proposed_sites") or analysis.get("planting_spots") or []
    shortest = route.get("shortest_segments") or []
    shaded = route.get("shadiest_segments") or []

    def most_exposed():
        segments = shaded or shortest
        return min(segments, key=lambda item: item.get("shade_percent", 100)) if segments else None

    def average(field: str, segments: list[dict[str, Any]]) -> float:
        values = [float(item.get(field, 0)) for item in segments]
        return round(sum(values) / len(values), 1) if values else 0.0

    if not route and not analysis:
        answer = "Analyze a location or calculate a route first so EcoTrace has data to explain."
    elif route.get("nighttime") and any(term in question for term in ("shade", "shadow", "canopy", "building", "exposed", "sun", "route", "segment")):
        answer = route.get("lighting_note") or "At night, direct solar exposure is zero and daylight shade scores are not applicable."
    elif "green route" in question or "choose the green" in question:
        answer = route.get("shadiest_route_why") or "Calculate a route first."
    elif "blue route" in question or "faster" in question:
        answer = route.get("shortest_route_why") or "Calculate a route first."
    elif "mapped tree" in question or "tree-canopy shade" in question:
        answer = (
            f"The shortest route averages {route.get('shortest_canopy_percent', 0)} canopy-shadow points; "
            f"the shade-oriented route averages {route.get('shadiest_canopy_percent', 0)}. "
            "These values come from mapped tree positions plus inferred crown size, height, and opacity."
        )
    elif "building" in question and "height" not in question:
        answer = (
            f"Mapped building shadows contribute {route.get('shortest_building_shadow_percent', 0)} points "
            f"on the shortest route and {route.get('shadiest_building_shadow_percent', 0)} on the shaded route."
        )
    elif "topography" in question or "terrain" in question:
        answer = (
            "EcoTrace currently does not use terrain in the route shade score. "
            "The active shade model uses mapped tree canopy, mapped building shadows, "
            "sun position, and heat exposure so the result stays transparent and supported "
            "by the data currently loaded."
        )
    elif "sun position" in question:
        segment = most_exposed()
        answer = (
            f"At the selected time, the most exposed segment uses a solar altitude of "
            f"{segment.get('solar_altitude_degrees')}° and azimuth of "
            f"{segment.get('solar_azimuth_degrees')}°."
            if segment else "Calculate a route first."
        )
    elif "most exposed" in question:
        segment = most_exposed()
        answer = (
            f"The most exposed segment scores {segment.get('shade_percent')}/100. "
            f"{segment.get('reason')}"
            if segment else "Calculate a route first."
        )
    elif "segment red" in question or "why is a segment red" in question:
        segment = next((item for item in shaded if item.get("category") == "high-exposure"), None)
        answer = segment.get("reason") if segment else "This route has no red high-exposure segment."
    elif "segment yellow" in question:
        segment = next((item for item in shaded if item.get("category") == "mixed"), None)
        answer = segment.get("reason") if segment else "This route has no yellow mixed-exposure segment."
    elif "segment green" in question:
        segment = next((item for item in shaded if item.get("category") == "high-shade"), None)
        answer = segment.get("reason") if segment else "This route has no green high-shade segment."
    elif "accurate" in question or "canopy model" in question:
        answer = (
            f"{route.get('canopy_model_summary', 'No route model is loaded.')} "
            "Tree and building positions are mapped data; missing crown dimensions, opacity, heights, "
            "and exact shadows use explicit approximations and should be validated."
        )
    elif "tree dimensions" in question or "dimensions are missing" in question:
        answer = (
            "When OSM lacks tree height or crown diameter, EcoTrace uses documented defaults "
            "(typically 8 m height and 3.5 m crown radius) and marks the attributes as inferred."
        )
    elif "tree opacity" in question:
        answer = (
            "Opacity controls how much sunlight a modeled crown blocks. Broadleaf and evergreen tags "
            "receive higher opacity; unknown trees use a moderate default, so they never become perfectly opaque."
        )
    elif "building height" in question:
        answer = (
            "EcoTrace uses mapped building height when available, otherwise building levels × 3.1 m, "
            "then a type-based default. Taller buildings project longer shadows at low sun angles."
        )
    elif "leaving later" in question:
        answer = (
            "Change the departure time and recalculate. Solar altitude and azimuth will change every "
            "segment's projected tree/building shadow; EcoTrace does not assume later is always better."
        )
    elif "ignore time" in question:
        answer = (
            "Ignoring time disables actual solar-position effects and uses a neutral comparison, "
            "so results reflect mapped canopy/buildings without claiming a specific shadow at that hour."
        )
    elif "heat change" in question or "heat score" in question:
        answer = (
            "Heat is a risk penalty, not physical shade. Hotter modeled surfaces subtract points from "
            "otherwise shaded segments because the walking environment can remain uncomfortable."
        )
    elif "weather" in question:
        weather = route.get("weather") or {}
        answer = weather.get("impact_summary") or "Current weather is unavailable for this route."
    elif "pace" in question:
        answer = (
            f"Travel times use {route.get('personalized_pace_mph')} mph. Pace changes minutes of exposure, "
            "but it does not change where trees or buildings cast shadows."
        )
    elif "route limit" in question or "detour" in question:
        answer = route.get("route_limit_description") or "Calculate a route first."
    elif "tree location" in question or "location selected" in question:
        if sites:
            site = sites[0]
            answer = (
                f"{site.get('where_location') or site.get('description')} "
                f"{site.get('why_chosen') or site.get('reason')}"
            )
        else:
            answer = "Analyze a location first."
    elif "tree species" in question or "species selected" in question:
        if sites:
            site = sites[0]
            answer = (
                f"EcoTrace treats this as a potential planting opportunity and provides site constraints rather than claiming one exact approved species. "
                f"{site.get('why_chosen') or site.get('reason')}"
            )
        else:
            answer = "Analyze a location first."
    elif "legally approved" in question:
        answer = (
            "No. Every result is a screened potential planting opportunity. A city must verify ownership, "
            "utilities, permits, irrigation, accessibility, and current site conditions."
        )
    elif "nearest" in question and "tree" in question:
        if sites:
            site = min(sites, key=lambda item: item.get("distance_from_click_meters", item.get("distance_meters", 10**9)))
            answer = f"The nearest displayed site is {site.get('distance_from_click_meters', site.get('distance_meters'))} meters away: {site.get('where_location') or site.get('description')}."
        else:
            answer = "Analyze a location first."
    elif "strongest justification" in question:
        if sites:
            site = max(sites, key=lambda item: item.get("screening_score", 0))
            answer = f"The strongest internally ranked site is {site.get('where_location')}. {site.get('why_chosen')}"
        else:
            answer = "Analyze a location first."
    elif "directly mapped" in question:
        answer = (
            "Directly mapped inputs include OSM tree coordinates, tree rows, building footprints, route geometry, "
            "roads, and available tags such as height, levels, species, crown diameter, and leaf type."
        )
    elif "inferred" in question:
        answer = (
            "Inferred inputs include missing crown radius, tree height, opacity, building height, solar shadows, "
            "terrain screening between coarse elevation points, heat risk, and planting suitability."
        )
    elif "limitations" in question:
        answer = (
            "Major limitations are incomplete OSM trees/buildings, approximate crown and height defaults, "
            "coarse or absent elevation, no utility/ownership verification, and no survey-grade 3D shadow model."
        )
    elif "judge" in question:
        answer = (
            f"EcoTrace compares a {route.get('shortest_minutes')} minute shortest route with a "
            f"{route.get('shadiest_minutes')} minute shade-oriented route, using mapped trees/buildings, "
            f"solar position, optional terrain, heat, weather, and a user-defined detour limit."
        )
    elif "city planner" in question:
        if sites:
            answer = (
                f"EcoTrace found {len(sites)} nearby screened opportunities. The strongest displayed location is "
                f"{sites[0].get('where_location')}. It remains a proposal requiring municipal verification."
            )
        else:
            answer = "Analyze a location first."
    else:
        answer = (
            "Ask about a specific route factor, segment color, sun position, tree/building shadow, topography, "
            "tree location/species, data source, or limitation."
        )

    return {
        "answer": answer,
        "basis": "Generated from the current EcoTrace route, canopy, weather, heat, and planting data.",
    }



@app.get("/api/build-info")
async def build_info() -> dict[str, Any]:
    return {
        "build": "6.14.9-final-input-polish",
        "port": 8093,
        "shade_function": "calculate_time_adjusted_route_shade_v525",
        "shade_function_argument_count": 3,
        "hidden_tree_candidate_count": len(PLANTING_SITES),
        "assistant_prompt_count": len(ASSISTANT_PROMPT_LIBRARY),
        "source_file": "ecotrace_final_input_polish_v6149.py",
    }


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "application": "EcoTrace"}


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    from fastapi.responses import Response
    return Response(status_code=204)


HTML_PAGE = r'''<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>EcoTrace | OpenStreetMap Routes + EcoTrace Shade</title>
  <script src="https://cdn.tailwindcss.com"></script>
  <link href="https://unpkg.com/maplibre-gl@4.7.1/dist/maplibre-gl.css" rel="stylesheet" />
  <script src="https://unpkg.com/maplibre-gl@4.7.1/dist/maplibre-gl.js"></script>
  <style>
    html,body{height:100%;margin:0;background:#07111f;color:#e5eefb;overflow:hidden;font-family:Inter,ui-sans-serif,system-ui}
    #map{width:100%;height:100%}.glass{background:rgba(7,17,31,.90);border:1px solid rgba(148,163,184,.18);backdrop-filter:blur(16px);box-shadow:0 20px 60px rgba(0,0,0,.3)}
    .eco-marker{width:25px;height:25px;border-radius:50%;background:#34d399;border:3px solid #ecfdf5;box-shadow:0 0 0 6px rgba(52,211,153,.18),0 8px 22px rgba(0,0,0,.45);cursor:pointer}
    .fallback-marker{width:25px;height:25px;border-radius:50%;background:#fb923c;border:3px solid #fff7ed;box-shadow:0 0 0 6px rgba(251,146,60,.22),0 8px 22px rgba(0,0,0,.45);cursor:pointer}.search-marker{width:20px;height:20px;border-radius:50%;background:#f8fafc;border:4px solid #2563eb;box-shadow:0 0 0 6px rgba(37,99,235,.2)}
    .maplibregl-popup-content{color:#0f172a;border-radius:12px;padding:12px 14px;max-width:300px}
    .tool-panel{display:none}.tool-panel.active{display:block}
    .tool-nav-button{display:flex;align-items:center;gap:.5rem;width:100%;padding:.65rem .75rem;border-radius:.8rem;font-size:.78rem;font-weight:700;color:#cbd5e1;background:rgba(255,255,255,.04);border:1px solid rgba(255,255,255,.08);transition:.16s}
    .tool-nav-button:hover{background:rgba(255,255,255,.08);color:#fff}
    .tool-nav-button.active{background:#34d399;color:#052e2b;border-color:#6ee7b7}
    .tool-nav-icon{width:1.25rem;text-align:center;font-size:1rem}
    @media(max-width:900px){body{overflow:auto}#app-shell{display:block;height:auto}#sidebar{width:100%;height:auto}#map-panel{height:65vh}}
  </style>
</head>
<body>
<main id="app-shell" class="flex h-screen w-screen">
  <aside id="sidebar" class="glass z-20 flex h-full w-[410px] shrink-0 flex-col overflow-y-auto p-5">
    <header class="mb-4">
      <div class="mb-3 flex items-center gap-3"><div class="flex h-11 w-11 items-center justify-center rounded-2xl bg-emerald-400/15 text-2xl">🌿</div><div><h1 class="text-2xl font-black text-white">EcoTrace</h1><p class="text-xs font-semibold uppercase tracking-[.18em] text-emerald-300">CA-17 Heat Intelligence</p></div></div>
      <p class="text-sm leading-6 text-slate-300">Search a local address or click the map to analyze heat, validate tree sites, or calculate a shade-aware pedestrian route.</p>
      <div class="mt-3 flex items-center justify-between rounded-xl bg-white/5 px-3 py-2 text-xs">
        <button id="current-profile-label" type="button" class="text-left font-bold text-emerald-200 underline decoration-dotted underline-offset-4">Guest</button>
        <button id="header-switch-profile" type="button" class="font-bold text-emerald-300">Switch account</button>
      </div>
    </header>

    <form id="search-form" class="mb-4">
      <label for="search-input" class="mb-2 block text-xs font-bold uppercase tracking-wider text-slate-400">Search CA-17</label>
      <div class="flex gap-2"><input id="search-input" list="search-suggestions" class="min-w-0 flex-1 rounded-xl border border-white/10 bg-white/5 px-3 py-2.5 text-sm text-white outline-none placeholder:text-slate-500 focus:border-emerald-400" placeholder="Address or landmark" autocomplete="off" /><button class="rounded-xl bg-emerald-400 px-4 py-2 text-sm font-black text-emerald-950 hover:bg-emerald-300" type="submit">Go</button></div>
      <datalist id="search-suggestions"></datalist>
      <div class="mt-2 grid grid-cols-2 gap-2">
        <button id="save-current-location" type="button" class="rounded-lg bg-amber-300/15 px-3 py-2 text-xs font-black text-amber-100 ring-1 ring-amber-300/20">☆ Save current location</button>
        <button id="cancel-active-request" type="button" class="rounded-lg bg-red-400/15 px-3 py-2 text-xs font-black text-red-100 ring-1 ring-red-300/20">Cancel request</button>
      </div>
      <p id="search-status" class="mt-2 min-h-4 text-xs text-slate-400"></p>
      <details open class="mt-2 rounded-xl bg-white/5 p-2">
        <summary class="cursor-pointer text-xs font-bold text-amber-100">Saved locations — click a saved place to reopen it</summary>
        <div id="saved-location-list" class="mt-2 space-y-2"></div>
      </details>
    </form>

    <nav class="mb-4 rounded-2xl bg-white/5 p-2 ring-1 ring-white/10">
      <p class="mb-2 px-2 text-[10px] font-black uppercase tracking-[.18em] text-slate-500">EcoTrace tools</p>
      <div class="grid grid-cols-2 gap-2">
        <button type="button" class="tool-nav-button active" data-panel-target="heat-panel"><span class="tool-nav-icon">🌡️</span><span>Heat Map</span></button>
        <button type="button" class="tool-nav-button" data-panel-target="walk-panel"><span class="tool-nav-icon">🚶</span><span>Walking</span></button>
        <button type="button" class="tool-nav-button" data-panel-target="tree-panel"><span class="tool-nav-icon">🌳</span><span>Tree Planting</span></button>
        <button type="button" class="tool-nav-button" data-panel-target="assistant-tool-panel"><span class="tool-nav-icon">💬</span><span>Assistant</span></button>
        <button type="button" class="tool-nav-button" data-panel-target="bike-panel"><span class="tool-nav-icon">🚲</span><span>Bike Mode</span></button>
        <button type="button" class="tool-nav-button" data-panel-target="community-panel"><span class="tool-nav-icon">🤝</span><span>Community</span></button>
        <button type="button" class="tool-nav-button col-span-2" data-panel-target="ewaste-panel"><span class="tool-nav-icon">♻️</span><span>E-Waste</span></button>
        <button type="button" class="tool-nav-button col-span-2" data-panel-target="tree-guide-panel"><span class="tool-nav-icon">🌱</span><span>What Trees Can I Plant?</span></button>
        <button type="button" class="tool-nav-button col-span-2" data-panel-target="guide-panel"><span class="tool-nav-icon">📘</span><span>Guide • Data • About</span></button>
      </div>
    </nav>

    <section id="walk-panel" class="tool-panel">
      <div class="mb-3 rounded-2xl bg-sky-400/10 p-3 ring-1 ring-sky-300/20">
        <h2 class="font-black text-sky-100">Walking Routes</h2>
        <p class="mt-1 text-xs leading-5 text-slate-300">Compare the fastest valid walking route with a shade-oriented alternative.</p>
      </div>
    <form id="route-form" class="mb-4 rounded-2xl bg-white/5 p-4 ring-1 ring-white/10">
      <p class="mb-3 text-xs font-bold uppercase tracking-wider text-slate-400">Route by address</p>
      <input id="route-start" list="route-start-suggestions" class="mb-2 w-full rounded-xl border border-white/10 bg-slate-950/60 px-3 py-2.5 text-sm text-white outline-none placeholder:text-slate-500 focus:border-emerald-400" placeholder="Starting address or landmark" autocomplete="off" />
      <input id="route-end" list="route-end-suggestions" class="mb-3 w-full rounded-xl border border-white/10 bg-slate-950/60 px-3 py-2.5 text-sm text-white outline-none placeholder:text-slate-500 focus:border-emerald-400" placeholder="Destination address or landmark" autocomplete="off" />
      <datalist id="route-start-suggestions"></datalist>
      <datalist id="route-end-suggestions"></datalist>
      <div class="mb-3 rounded-xl bg-emerald-400/10 p-3 ring-1 ring-emerald-300/20">
        <label class="block text-[11px] font-bold uppercase tracking-wider text-emerald-100">
          User profile
          <select id="walking-profile" class="mt-1 w-full rounded-xl border border-white/10 bg-slate-950/70 px-3 py-2 text-sm normal-case text-white">
            <option value="child">Child</option>
            <option value="adult" selected>Adult</option>
            <option value="runner">Runner</option>
            <option value="older_adult">Older Adult</option>
            <option value="heat_sensitive">Heat-Sensitive</option>
            <option value="other">Other / custom</option>
          </select><input id="walking-profile-other" class="mt-2 hidden w-full rounded-lg bg-slate-950/70 px-2 py-2 text-xs normal-case text-white ring-1 ring-white/10" placeholder="Describe the person/group for Gemini" autocomplete="off">
        </label>
        <p id="walking-profile-help" class="mt-2 text-xs leading-5 text-emerald-100"></p>
        <p class="mt-2 rounded-lg bg-black/15 p-2 text-[11px] leading-5 text-slate-300">
          Note: For a family or group, choose the profile of the person who needs the most shade, has the slowest pace, or is most sensitive to heat.
        </p>
      </div>

      <div class="mb-3 grid grid-cols-2 gap-2">
        <label class="text-[11px] font-bold uppercase tracking-wider text-slate-400">Walking pace
          <select id="walking-pace" class="mt-1 w-full rounded-xl border border-white/10 bg-slate-950/70 px-2 py-2 text-sm normal-case text-white">
            <option value="2">Slow — 2.0 mph</option>
            <option value="3" selected>Average — 3.0 mph</option>
            <option value="4">Fast — 4.0 mph</option>
            <option value="custom">Custom</option>
          </select>
        </label>
        <label class="text-[11px] font-bold uppercase tracking-wider text-slate-400">Custom mph
          <input id="custom-pace" type="number" min="0.5" step="0.1" value="3.0" disabled class="mt-1 w-full rounded-xl border border-white/10 bg-slate-950/70 px-2 py-2 text-sm normal-case text-white disabled:opacity-40" autocomplete="off">
        </label>
      </div>
      <p id="pace-warning-live" class="mb-3 hidden rounded-xl bg-amber-400/10 px-3 py-2 text-xs leading-5 text-amber-200 ring-1 ring-amber-300/20"></p>
      <label class="mb-3 flex cursor-pointer items-center gap-2 rounded-xl bg-white/5 p-3 text-sm text-slate-200 ring-1 ring-white/10">
        <input id="ignore-time" type="checkbox" class="h-4 w-4 accent-emerald-400" autocomplete="off">
        Ignore time of day
      </label>
      <div id="time-controls" class="mb-3 rounded-xl bg-white/5 p-3 ring-1 ring-white/10"><p class="mb-2 text-xs text-slate-300">Pacific time · defaults to 15 minutes from now. You can choose another departure.</p>
        <label class="block text-[11px] font-bold uppercase tracking-wider text-slate-400">Departure date
          <input id="departure-date" type="date" class="mt-1 w-full rounded-xl border border-white/10 bg-slate-950/70 px-3 py-2 text-sm normal-case text-white" autocomplete="off">
        </label>
        <div class="mt-2 grid grid-cols-[1fr_1fr_1fr] gap-2">
          <label class="text-[11px] font-bold uppercase tracking-wider text-slate-400">Hour
            <input id="departure-hour" type="number" min="1" max="12" value="3" class="mt-1 w-full rounded-xl border border-white/10 bg-slate-950/70 px-2 py-2 text-sm normal-case text-white" autocomplete="off">
          </label>
          <label class="text-[11px] font-bold uppercase tracking-wider text-slate-400">Minute
            <input id="departure-minute" type="number" min="0" max="59" value="30" class="mt-1 w-full rounded-xl border border-white/10 bg-slate-950/70 px-2 py-2 text-sm normal-case text-white" autocomplete="off">
          </label>
          <label class="text-[11px] font-bold uppercase tracking-wider text-slate-400">AM/PM
            <select id="departure-ampm" class="mt-1 w-full rounded-xl border border-white/10 bg-slate-950/70 px-2 py-2 text-sm normal-case text-white">
              <option>AM</option><option selected>PM</option>
            </select>
          </label>
        </div>
      </div>
      <div class="mb-3 rounded-xl bg-white/5 p-3 ring-1 ring-white/10">
        <label class="block text-[11px] font-bold uppercase tracking-wider text-slate-400">
          Shaded-route limit
          <select id="route-limit-mode" class="mt-1 w-full rounded-xl border border-white/10 bg-slate-950/70 px-3 py-2 text-sm normal-case text-white">
            <option value="normal" selected>Normal — no more than 20% longer in time</option>
            <option value="distance">Custom — limit extra distance</option>
          </select>
        </label>
        <label id="extra-distance-wrap" class="mt-2 hidden text-[11px] font-bold uppercase tracking-wider text-slate-400">
          Maximum extra miles
          <input id="max-extra-distance" type="number" min="0" max="10" step="0.1" value="0.5" class="mt-1 w-full rounded-xl border border-white/10 bg-slate-950/70 px-3 py-2 text-sm normal-case text-white" autocomplete="off">
        </label>
      </div>
      <button id="route-submit" type="submit" class="w-full rounded-xl bg-emerald-400 px-4 py-2.5 text-sm font-black text-emerald-950 hover:bg-emerald-300 disabled:cursor-not-allowed disabled:opacity-50">Find shadiest route</button>
      <p id="route-status" class="mt-2 min-h-4 text-xs text-slate-400">Addresses and short landmark names are snapped automatically to the nearest walkable network point.</p>
    </form>
      <section id="route-comparison" class="mt-4 hidden"></section>
      <section id="weather-panel" class="mt-4 hidden rounded-2xl bg-sky-400/10 p-4 ring-1 ring-sky-300/20"></section>
    </section>

    <section id="heat-panel" class="tool-panel active">
      <div class="mb-3 rounded-2xl bg-orange-400/10 p-3 ring-1 ring-orange-300/20">
        <h2 class="font-black text-orange-100">Hyper-Local Heat Map</h2>
        <p class="mt-1 text-xs leading-5 text-slate-300">Search or click anywhere to inspect the modeled heat surface and local conditions.</p>
      </div>
    <section class="mb-4 rounded-2xl bg-white/5 p-4 ring-1 ring-white/10"><div class="mb-2 flex justify-between text-xs text-slate-400"><span>Continuous heat gradient</span><span>Cool → Hot</span></div><div class="h-3 rounded-full" style="background:linear-gradient(90deg,#2563eb,#22c55e,#facc15,#f97316,#dc2626)"></div></section>

    <section class="mb-4 rounded-2xl bg-sky-400/10 p-3 text-xs leading-5 text-slate-300 ring-1 ring-sky-300/20"><p class="font-bold text-sky-200">Data confidence</p><p>Addresses and route geometry: live OpenStreetMap services. Routing and mapped tree/building geometry use live/current OSM-derived data; crown size, opacity, building height, shadows, heat, and planting suitability remain modeled estimates.</p></section>
    <section id="analysis-card" class="rounded-2xl bg-slate-950/60 p-4 ring-1 ring-slate-700/70"><h2 class="text-sm font-bold text-white">Ready</h2><p class="mt-2 text-sm leading-6 text-slate-300">Click a map location or search for a place.</p></section>
    </section>
    <section id="tree-panel" class="tool-panel">
<details class="my-3 rounded-xl bg-emerald-400/10 p-3 ring-1 ring-emerald-300/20">
<summary class="cursor-pointer text-sm font-bold text-emerald-100">How planting locations are selected</summary>
<ol class="mt-2 list-decimal space-y-2 pl-4 text-xs leading-5 text-slate-300">
<li><strong>Find possible gaps:</strong> combine mapped roads, buildings and existing canopy with aerial images to locate potential grass or soil planting space.</li>
<li><strong>Screen each candidate:</strong> check the recorded image and map evidence for paving, buildings, existing trees, playing areas and other conflicts. Apply spacing checks so nearby alternatives are not counted as separate trees.</li>
<li><strong>Refine the map:</strong> apply recorded location corrections and exclusions. Available dated surface-heat observations add context for prioritizing shade.</li>
</ol><p class="mt-2 text-xs leading-5 text-slate-300">These are planning candidates. Street names help locate the area; the coordinate marks the proposed spot. Owner/city permission, utilities and current ground conditions still need checking, including on unfenced lawns. Open Site details for the location evidence.</p></details>
      <div class="mb-3 rounded-2xl bg-emerald-400/10 p-3 ring-1 ring-emerald-300/20">
        <h2 class="font-black text-emerald-100">Tree Planting Opportunities</h2>
        <p class="mt-1 text-xs leading-5 text-slate-300">Click/search a place, or open a screened route candidate to jump directly to that exact location.</p>
      </div>
      <section id="recommendations" class="mt-4 hidden"><h2 class="mb-3 text-sm font-bold text-white">Nearest planting sites</h2><div id="recommendation-list" class="space-y-3"></div></section>
    </section>

    <section id="assistant-tool-panel" class="tool-panel">
      <div class="mb-3 rounded-2xl bg-violet-400/10 p-3 ring-1 ring-violet-300/20">
        <div class="flex items-center justify-between gap-2">
          <div>
            <h2 class="font-black text-violet-100">EcoTrace Assistant</h2>
            <p class="mt-1 text-[11px] leading-4 text-slate-400">
              Optional Gemini chat grounded in your current EcoTrace route, tree, heat, bike, and e-waste results.
            </p>
          </div>
          <button id="assistant-new-chat" type="button"
            class="shrink-0 rounded-lg bg-white/10 px-2.5 py-2 text-[11px] font-black text-white ring-1 ring-white/10">
            + New
          </button>
        </div>
      </div>

      <div class="grid min-h-[470px] grid-cols-[118px_minmax(0,1fr)] gap-2">
        <aside class="rounded-xl bg-black/20 p-2 ring-1 ring-white/10">
          <div class="mb-2 flex items-center justify-between gap-1">
            <p class="text-[9px] font-black uppercase tracking-wider text-slate-500">Saved chats</p>
          </div>
          <div id="assistant-chat-sidebar" class="max-h-[420px] space-y-1 overflow-y-auto pr-1">
            <p class="text-[10px] leading-4 text-slate-500">No saved chats yet.</p>
          </div>
        </aside>

        <div class="flex min-w-0 flex-col rounded-xl bg-white/5 p-2 ring-1 ring-white/10">
          <div class="mb-2 flex items-center justify-between gap-2">
            <p id="assistant-current-title" class="truncate text-[11px] font-black text-violet-100">New chat</p>
            <button id="assistant-save-chat" type="button"
              class="shrink-0 rounded-lg bg-violet-300/15 px-2 py-1.5 text-[10px] font-bold text-violet-100 ring-1 ring-violet-300/20">
              Save
            </button>
          </div>

          <div id="assistant-answer"
            class="min-h-[290px] flex-1 overflow-y-auto rounded-xl bg-slate-950/45 p-2 text-xs leading-5 text-slate-200">
            <div id="assistant-empty-state" class="rounded-xl bg-violet-300/5 p-3 text-[11px] leading-5 text-slate-400">
              Ask anything about EcoTrace. Follow-up questions stay in this conversation.
            </div>
          </div>

          <div class="mt-2 flex items-end gap-2">
            <textarea id="assistant-question" rows="2"
              class="min-h-[52px] min-w-0 flex-1 resize-none rounded-xl border border-white/10 bg-slate-950/70 px-3 py-2 text-xs text-white outline-none placeholder:text-slate-500 focus:border-violet-300"
              placeholder="Message EcoTrace Assistant…"></textarea>
            <button id="assistant-submit" type="button"
              class="rounded-xl bg-violet-400 px-3 py-2.5 text-xs font-black text-violet-950 hover:bg-violet-300">
              Send
            </button>
          </div>

          <p id="assistant-chat-status" class="mt-2 min-h-4 text-[9px] leading-4 text-slate-500">
            Enter sends • Shift+Enter adds a new line.
          </p>

          <details class="mt-2">
            <summary class="cursor-pointer text-[10px] font-bold text-violet-200">Example prompts</summary>
            <div id="assistant-prompt-grid" class="mt-2 flex max-h-28 flex-wrap gap-1.5 overflow-y-auto pr-1"></div>
          </details>
        </div>
      </div>
    </section>

    <section id="bike-panel" class="tool-panel">
      <div class="mb-3 rounded-2xl bg-cyan-400/10 p-3 ring-1 ring-cyan-300/20">
        <h2 class="font-black text-cyan-100">Bike Routes</h2>
        <p class="mt-1 text-xs leading-5 text-slate-300">Compare fastest and cooler cycling routes, personalized speed, and mapped bike-lane coverage.</p>
      </div>
      <form id="bike-form" class="rounded-2xl bg-white/5 p-3 ring-1 ring-white/10">
        <input id="bike-start" list="bike-start-suggestions" class="mb-2 w-full rounded-xl border border-white/10 bg-slate-950/70 px-3 py-2.5 text-sm text-white outline-none" placeholder="Starting address or click map" autocomplete="off">
        <input id="bike-end" list="bike-end-suggestions" class="mb-3 w-full rounded-xl border border-white/10 bg-slate-950/70 px-3 py-2.5 text-sm text-white outline-none" placeholder="Destination address or click map" autocomplete="off">
        <datalist id="bike-start-suggestions"></datalist>
        <datalist id="bike-end-suggestions"></datalist>
        <div class="grid grid-cols-2 gap-2">
          <label class="text-[11px] font-bold uppercase tracking-wider text-slate-400">Bike type
            <select id="bike-type" class="mt-1 w-full rounded-xl border border-white/10 bg-slate-950/70 px-2 py-2 text-sm normal-case text-white">
              <option value="regular" selected>Regular bike</option>
              <option value="road-bike">Road bike</option>
              <option value="e-bike">E-bike</option>
            <option value="other">Other / custom</option></select><input id="bike-type-other" class="mt-2 hidden w-full rounded-lg bg-slate-950/70 px-2 py-2 text-xs normal-case text-white ring-1 ring-white/10" placeholder="Describe bike/mobility type for Gemini" autocomplete="off">
          </label>
          <label class="text-[11px] font-bold uppercase tracking-wider text-slate-400">Speed
            <select id="bike-speed-mode" class="mt-1 w-full rounded-xl border border-white/10 bg-slate-950/70 px-2 py-2 text-sm normal-case text-white">
              <option value="10">Casual — 10 mph</option>
              <option value="12" selected>Average — 12 mph</option>
              <option value="16">Fast — 16 mph</option>
              <option value="18">E-bike — 18 mph</option>
              <option value="custom">Custom</option>
            </select>
          </label>
        </div>
        <label id="bike-custom-speed-wrap" class="mt-2 hidden block text-[11px] font-bold uppercase tracking-wider text-slate-400">Custom speed (mph)
          <input id="bike-custom-speed" type="number" min="3" max="35" step="0.5" value="12" class="mt-1 w-full rounded-xl border border-white/10 bg-slate-950/70 px-3 py-2 text-sm normal-case text-white" autocomplete="off">
        </label>
        <label class="mt-3 flex cursor-pointer items-center gap-2 rounded-xl bg-white/5 p-3 text-sm text-slate-200 ring-1 ring-white/10">
          <input id="bike-ignore-time" type="checkbox" class="h-4 w-4 accent-cyan-400" autocomplete="off">
          Ignore time of day
        </label>
        <div id="bike-time-controls" class="mt-2 rounded-xl bg-white/5 p-3 ring-1 ring-white/10"><p class="mb-2 text-xs text-slate-300">Pacific time · defaults to 15 minutes from now. You can choose another departure.</p>
          <p class="mb-2 text-[11px] font-black uppercase tracking-wider text-cyan-200">Bike departure time</p>
          <p class="mb-2 text-[11px] leading-4 text-slate-400">EcoTrace recalculates estimated shade for the sun position at this time.</p>
          <label class="block text-[11px] font-bold uppercase tracking-wider text-slate-400">Departure date
            <input id="bike-departure-date" type="date" class="mt-1 w-full rounded-xl border border-white/10 bg-slate-950/70 px-3 py-2 text-sm normal-case text-white" autocomplete="off">
          </label>
          <div class="mt-2 grid grid-cols-[1fr_1fr_1fr] gap-2">
            <label class="text-[11px] font-bold uppercase tracking-wider text-slate-400">Hour
              <input id="bike-departure-hour" type="number" min="1" max="12" value="3" class="mt-1 w-full rounded-xl border border-white/10 bg-slate-950/70 px-2 py-2 text-sm normal-case text-white" autocomplete="off">
            </label>
            <label class="text-[11px] font-bold uppercase tracking-wider text-slate-400">Minute
              <input id="bike-departure-minute" type="number" min="0" max="59" value="30" class="mt-1 w-full rounded-xl border border-white/10 bg-slate-950/70 px-2 py-2 text-sm normal-case text-white" autocomplete="off">
            </label>
            <label class="text-[11px] font-bold uppercase tracking-wider text-slate-400">AM/PM
              <select id="bike-departure-ampm" class="mt-1 w-full rounded-xl border border-white/10 bg-slate-950/70 px-2 py-2 text-sm normal-case text-white">
                <option>AM</option><option selected>PM</option>
              </select>
            </label>
          </div>
        </div>
        <div class="mt-3 rounded-xl bg-black/15 p-3">
          <label class="block text-[11px] font-bold uppercase tracking-wider text-slate-400">Cooler-route limit
            <select id="bike-limit-mode" class="mt-1 w-full rounded-xl border border-white/10 bg-slate-950/70 px-3 py-2 text-sm normal-case text-white">
              <option value="normal" selected>Normal — 15% longer maximum</option>
              <option value="distance">Custom extra miles</option>
            </select>
          </label>
          <label id="bike-extra-distance-wrap" class="mt-2 hidden block text-[11px] font-bold uppercase tracking-wider text-slate-400">Maximum extra miles
            <input id="bike-extra-distance" type="number" min="0" max="20" step="0.1" value="1.0" class="mt-1 w-full rounded-xl border border-white/10 bg-slate-950/70 px-3 py-2 text-sm normal-case text-white" autocomplete="off">
          </label>
        </div>
        <button type="submit" class="mt-3 w-full rounded-xl bg-cyan-400 px-4 py-2.5 text-sm font-black text-cyan-950 hover:bg-cyan-300">Find bike routes</button>
        <p id="bike-status" class="mt-2 min-h-[18px] text-xs text-slate-400"></p>
      </form>
      <div class="mt-3 rounded-xl bg-blue-300/5 p-3 ring-1 ring-blue-300/15">
        <p class="text-[10px] font-black uppercase tracking-wider text-blue-200">Official validation source</p>
        <p class="mt-1 text-[11px] leading-5 text-slate-400">
          EcoTrace's interactive bike-infrastructure colors come from local OpenStreetMap tags.
          Use VTA/511's Santa Clara Valley Bikeways Map as an independent official visual cross-check.
        </p>
        <div class="mt-2 grid grid-cols-2 gap-2">
          <a href="https://data.vta.org/datasets/santa-clara-county-bikeways-2023/about"
             target="_blank" rel="noopener noreferrer"
             class="block rounded-lg bg-blue-300/10 px-2 py-2 text-center text-[10px] font-black leading-4 text-blue-100 ring-1 ring-blue-300/20 hover:bg-blue-300/15">
            VTA 2023 Interactive GIS ↗
          </a>
          <a href="https://511.org/sites/default/files/bike_maps/vta-santa-clara-valley-bikeways-map.pdf"
             target="_blank" rel="noopener noreferrer"
             class="block rounded-lg bg-blue-300/10 px-2 py-2 text-center text-[10px] font-black leading-4 text-blue-100 ring-1 ring-blue-300/20 hover:bg-blue-300/15">
            VTA/511 Bikeways Map ↗
          </a>
          <a href="https://www.cyclosm.org/"
             target="_blank" rel="noopener noreferrer"
             class="col-span-2 block rounded-lg bg-cyan-300/10 px-2 py-2 text-center text-[10px] font-black leading-4 text-cyan-100 ring-1 ring-cyan-300/20 hover:bg-cyan-300/15">
            CyclOSM — OpenStreetMap Bicycle Map ↗
          </a>
        </div>
        <details class="mt-2 rounded-lg bg-black/20 p-2">
          <summary class="cursor-pointer text-[10px] font-bold text-blue-100">How to compare EcoTrace with VTA</summary>
          <div class="mt-2 space-y-1 text-[10px] leading-4 text-slate-400">
            <p><strong class="text-slate-200">Green:</strong> OSM-tagged protected/separated facility; compare with VTA off-street paths/separated facilities.</p>
            <p><strong class="text-slate-200">Blue:</strong> OSM-tagged painted bike lane; compare with VTA on-street bike lanes.</p>
            <p><strong class="text-slate-200">Yellow/orange:</strong> shared-street classification; compare with VTA bike routes/sharrows and street caution ratings.</p>
            <p><strong class="text-slate-200">Gray:</strong> no confident OSM match; check VTA rather than assuming no bikeway exists.</p>
          </div>
        </details>
        <p class="mt-2 text-[9px] leading-4 text-amber-200">
          The interactive VTA page exposes the official 2023 GIS dataset; the VTA/511 PDF is the rider-facing visual map. EcoTrace keeps both as independent validation references rather than route-time dependencies.
        </p>
      </div>

      <section id="bike-comparison" class="mt-4 hidden"></section>
    </section>

    <section id="tree-guide-panel" class="tool-panel">
      <div class="mb-3 rounded-2xl bg-green-400/10 p-3 ring-1 ring-green-300/20">
        <h2 class="font-black text-green-100">What Trees Can I Plant?</h2>
        <p class="mt-1 text-xs leading-5 text-slate-300">
          Browse EcoTrace’s curated 27-species list by site constraints. These are candidates for review, not automatic approval for a specific coordinate.
        </p>
      </div>

      <div class="rounded-2xl bg-white/5 p-3 ring-1 ring-white/10">
        <div class="grid grid-cols-2 gap-2">
          <label class="text-[10px] font-bold uppercase tracking-wider text-slate-400">Tree size
            <select id="tree-guide-size" class="mt-1 w-full rounded-xl border border-white/10 bg-slate-950/70 px-2 py-2 text-sm normal-case text-white">
              <option value="">All sizes</option>
              <option value="small">Small trees</option>
              <option value="medium">Medium trees</option>
              <option value="large">Large trees</option>
            </select>
          </label>
          <label class="text-[10px] font-bold uppercase tracking-wider text-slate-400">Foliage type
            <select id="tree-guide-type" class="mt-1 w-full rounded-xl border border-white/10 bg-slate-950/70 px-2 py-2 text-sm normal-case text-white">
              <option value="">All types</option>
              <option value="D">D — Deciduous</option>
              <option value="E">E — Evergreen</option>
              <option value="SE">SE — Semi-evergreen</option>
            </select>
          </label>
        </div>
        <div class="mt-2 grid grid-cols-2 gap-2">
          <label class="text-[10px] font-bold uppercase tracking-wider text-slate-400">Maximum height
            <select id="tree-guide-height" class="mt-1 w-full rounded-xl border border-white/10 bg-slate-950/70 px-2 py-2 text-sm normal-case text-white">
              <option value="">Any</option>
              <option value="18">18 ft</option>
              <option value="20">20 ft</option>
              <option value="25">25 ft</option>
              <option value="30">30 ft</option>
              <option value="35">35 ft</option>
              <option value="40">40 ft</option>
              <option value="50">50 ft</option>
            </select>
          </label>
          <label class="text-[10px] font-bold uppercase tracking-wider text-slate-400">Maximum width
            <select id="tree-guide-width" class="mt-1 w-full rounded-xl border border-white/10 bg-slate-950/70 px-2 py-2 text-sm normal-case text-white">
              <option value="">Any</option>
              <option value="15">15 ft</option>
              <option value="20">20 ft</option>
              <option value="25">25 ft</option>
              <option value="30">30 ft</option>
              <option value="35">35 ft</option>
              <option value="40">40 ft</option>
              <option value="50">50 ft</option>
            </select>
          </label>
        </div>
        <label class="mt-2 block text-[10px] font-bold uppercase tracking-wider text-slate-400">Flower
          <select id="tree-guide-flower" class="mt-1 w-full rounded-xl border border-white/10 bg-slate-950/70 px-2 py-2 text-sm normal-case text-white">
            <option value="">Any flower color</option>
            <option value="white">White</option>
            <option value="yellow">Yellow</option>
            <option value="pink">Pink / magenta</option>
            <option value="purple">Purple / lavender</option>
            <option value="n/a">n/a</option>
          </select>
        </label>
        <button id="tree-guide-search" type="button" class="mt-3 w-full rounded-xl bg-green-400 px-4 py-2.5 text-sm font-black text-green-950">Show matching trees</button>
        <p id="tree-guide-status" class="mt-2 text-xs leading-5 text-slate-400"></p>
      </div>

      <div id="tree-guide-notice" class="mt-3 rounded-xl bg-amber-400/10 p-3 text-xs leading-5 text-amber-100 ring-1 ring-amber-300/20"></div>
      <div id="tree-guide-results" class="mt-3 space-y-3"></div>

      <div class="mt-4 rounded-2xl bg-emerald-400/5 p-3 ring-1 ring-emerald-300/15">
        <p class="text-[10px] font-black uppercase tracking-wider text-emerald-200">Tree list scope</p>
        <p class="mt-1 text-[11px] leading-5 text-slate-400">
          EcoTrace highlights 27 recommended species for common urban planting situations. Use the filters above to compare mature size, foliage type, and site constraints.
        </p>
        <a href="https://files.santaclaracounty.gov/exjcpb1476/migrated/Tree-Master-Plant-List-Non-Expwy.pdf?VersionId=O0hXC9WWC9irr.3bMYwGhqcFJZ78yTet"
           target="_blank" rel="noopener noreferrer"
           class="mt-2 block rounded-lg bg-emerald-300/10 px-3 py-2 text-center text-[11px] font-black text-emerald-100 ring-1 ring-emerald-300/20 hover:bg-emerald-300/15">
          View Santa Clara County Full Master Plant List ↗
        </a>
      </div>
    </section>

    <section id="guide-panel" class="tool-panel">
      <div class="mb-3 rounded-2xl bg-sky-400/10 p-3 ring-1 ring-sky-300/20">
        <h2 class="font-black text-sky-100">EcoTrace Guide</h2>
        <p class="mt-1 text-xs leading-5 text-slate-300">
          Learn how to use each tool, understand its data, and manage your own posts.
        </p>
      </div>

      <div class="mb-3 grid grid-cols-3 gap-1 rounded-xl bg-white/5 p-1 ring-1 ring-white/10">
        <button type="button" class="guide-tab rounded-lg bg-sky-300/20 px-2 py-2 text-[10px] font-black text-sky-100" data-guide-tab="use">How to Use</button>
        <button type="button" class="guide-tab rounded-lg px-2 py-2 text-[10px] font-black text-slate-300" data-guide-tab="data">Data & Methods</button>
        <button type="button" class="guide-tab rounded-lg px-2 py-2 text-[10px] font-black text-slate-300" data-guide-tab="about">About</button>
      </div>

      <div id="guide-use" class="guide-subpanel space-y-2">

        <div class="rounded-xl bg-white/5 p-3 ring-1 ring-white/10"><strong class="text-orange-200">🌡️ Heat Map</strong><p class="mt-1 text-xs leading-5 text-slate-300">Search or click a location. EcoTrace shows a modeled spatial heat-exposure score separately from the latest nearby National Weather Service observation.</p></div>
        <div class="rounded-xl bg-white/5 p-3 ring-1 ring-white/10"><strong class="text-sky-200">🚶 Walking</strong><p class="mt-1 text-xs leading-5 text-slate-300">Type addresses, click two points, or mix typing/clicking. Compare the valid shortest route with a shade-oriented alternative, then show screened planting opportunities along both routes.</p></div>
        <div class="rounded-xl bg-white/5 p-3 ring-1 ring-white/10"><strong class="text-emerald-200">🌳 Tree Planting</strong><p class="mt-1 text-xs leading-5 text-slate-300">Search or click a location to see nearby planting candidates and exact coordinates. Open Site details for evidence, or use Browse tree species once above the results.</p></div>
        <div class="rounded-xl bg-white/5 p-3 ring-1 ring-white/10"><strong class="text-fuchsia-200">🚲 Bike Mode</strong><p class="mt-1 text-xs leading-5 text-slate-300">Compare fastest and cooler bike routes. View local OpenStreetMap-derived infrastructure and open the VTA reference links for comparison.</p></div>
        <div class="rounded-xl bg-white/5 p-3 ring-1 ring-white/10"><strong class="text-violet-200">💬 Assistant</strong><p class="mt-1 text-xs leading-5 text-slate-300">Ask Gemini about the currently loaded route, tree, heat, bike, community, and e-waste results. It receives EcoTrace context, not unrestricted private files.</p></div>
        <div class="rounded-xl bg-white/5 p-3 ring-1 ring-white/10"><strong class="text-amber-200">🤝 Community</strong><p class="mt-1 text-xs leading-5 text-slate-300">Explore environmental posts, events, and Census planning context.</p></div>
        <div class="rounded-xl bg-white/5 p-3 ring-1 ring-white/10"><strong class="text-lime-200">♻️ E-Waste</strong><p class="mt-1 text-xs leading-5 text-slate-300">Identify an item, find collection centers, and organize local events. Sign in to publish an exchange listing, message its author, or edit/delete your own listing.</p></div>
        <div class="rounded-xl bg-white/5 p-3 ring-1 ring-white/10"><strong class="text-green-200">🌱 Tree Guide</strong><p class="mt-1 text-xs leading-5 text-slate-300">Filter EcoTrace’s curated 27-species list by size and site constraints, then open Santa Clara County’s broader Master Plant List.</p></div>
        <div id="ecotrace-use-final-notes" class="space-y-2"><p class="rounded-xl bg-white/5 p-3 text-xs leading-5 text-slate-300">Sign in to create posts and edit or delete your own posts. Guests can read and report posts, and reply in community discussions. E-waste messages and event participation require sign-in. Reports are saved for review and do not automatically remove posts; moderator tools are future work.</p>
<p class="rounded-xl bg-white/5 p-3 text-xs leading-5 text-slate-300">Walking and cycling keep a valid basic route when the alternative search fails. If the routing provider returns no usable route, retry or select a nearby mapped entrance. Shade and travel times are estimates, and the comparison covers returned candidates.</p></div>
      </div>

      <div id="guide-data" class="guide-subpanel hidden space-y-2">

        <div class="rounded-xl bg-sky-400/10 p-3 ring-1 ring-sky-300/20">
          <p class="text-xs font-black text-sky-100">Exactly what EcoTrace uses</p>
          <p class="mt-1 text-[10px] leading-4 text-slate-300">
            Each tool below lists the external data/API, local datasets, and EcoTrace calculations that produce the result.
          </p>
        </div>

        <details class="rounded-xl bg-white/5 p-3 ring-1 ring-white/10">
          <summary class="cursor-pointer text-xs font-black text-orange-200">🌡️ Heat Map / Current Heat</summary>
          <div class="mt-2 space-y-1 text-[10px] leading-4 text-slate-300">
            <p><strong>Live weather:</strong> U.S. National Weather Service API at <code>api.weather.gov</code> — latest nearby station observation plus hourly forecast.</p>
            <p><strong>Tree canopy:</strong> WRI / Meta canopy-height GeoTIFF, about 2 m resolution, stored locally as <code>wri_tree_canopy_height.tif</code>.</p>
            <p><strong>Building shadow:</strong> Santa Clara County Buildings Footprints 2D dataset with <code>building_h</code> heights, merged into the local shade model.</p>
            <p><strong>Road/path geometry:</strong> OpenStreetMap-derived pedestrian/road network used to locate paths and route segments.</p>
            <p><strong>EcoTrace calculation:</strong> sun position + nearby tree canopy + building geometry/heights → modeled shade/exposure score. NWS weather is shown as current context; it does not turn the score into a measured air-temperature map.</p>
            <p><strong>Separate tree evidence:</strong> some planting candidates include dated satellite surface temperatures. These are not current air temperatures; observation dates are available in the location evidence.</p>
          </div>
        </details>

        <details class="rounded-xl bg-white/5 p-3 ring-1 ring-white/10">
          <summary class="cursor-pointer text-xs font-black text-sky-200">🚶 Walking Routes</summary>
          <div class="mt-2 space-y-1 text-[10px] leading-4 text-slate-300">
            <p><strong>Routing:</strong> OpenRouteService walking routes, constrained to pedestrian/road-crossing networks.</p>
            <p><strong>Route methods:</strong> OpenRouteService candidate routes, geocoding/reverse-geocoding and route snapping, Haversine segment-distance calculations, distance-weighted shade aggregation, alternative-route filtering, and detour-constrained multi-objective comparison.</p>
            <p><strong>EcoTrace optimization:</strong> route candidates → segment-level tree/building shadow calculation → distance-weighted exposure score → detour constraint → highest-shade meaningful alternative.</p>
            <p><strong>Base map/network evidence:</strong> OpenStreetMap road/path geometry.</p>
            <p><strong>Shade inputs:</strong> WRI/Meta canopy raster + Santa Clara County building footprints/heights.</p>
            <p><strong>EcoTrace calculation:</strong> scores route segments for modeled shade/exposure and compares the shortest route with a shade-oriented alternative under the configured detour limit.</p>
            <p><strong>Tree opportunities:</strong> the reviewed candidate dataset, searched within about 75 m of both route alternatives.</p>
          </div>
        </details>

        <details class="rounded-xl bg-white/5 p-3 ring-1 ring-white/10">
          <summary class="cursor-pointer text-xs font-black text-fuchsia-200">🚲 Bike Routes + Bike Infrastructure</summary>
          <div class="mt-2 space-y-1 text-[10px] leading-4 text-slate-300">
            <p><strong>Routing:</strong> OpenRouteService cycling routes.</p>
            <p><strong>Route methods:</strong> weighted graph routing through OpenRouteService, Haversine/distance-weighted shade scoring, alternative-route filtering, detour comparison, and spatial matching of route segments to OSM or VTA bike-infrastructure features.</p>
            <p><strong>OSM mode:</strong> local OpenStreetMap-derived bike tags/classes for protected, painted, shared/low-speed, shared road, or unknown.</p>
            <p><strong>VTA mode:</strong> VTA map links provide an independent reference; live VTA requests do not delay the route calculation.</p>
            <p><strong>Visual cross-checks:</strong> VTA interactive GIS / VTA-511 PDF and CyclOSM/OpenStreetMap bicycle map links.</p>
            <p><strong>Shade inputs:</strong> same WRI/Meta canopy + County building-height model used for walking.</p>
            <p><strong>EcoTrace calculation:</strong> compares fastest vs cooler candidates under your detour limit and colors infrastructure using the local OSM layer.</p>
          </div>
        </details>

        <details class="rounded-xl bg-white/5 p-3 ring-1 ring-white/10">
          <summary class="cursor-pointer text-xs font-black text-emerald-200">🌳 Tree Planting / Candidate Evidence</summary>
          <div class="mt-2 space-y-2 text-xs leading-5 text-slate-300">
            <p><strong>Coverage:</strong> 5,582 planning candidates in the installed reviewed dataset. The original dataset is preserved in the earlier update backups.</p>
            <p><strong>Location:</strong> nearby street names help you find the area; the coordinates identify the candidate, not a verified property address.</p>
            <p><strong>Evidence:</strong> mapped buildings, roads, canopy, imagery and recorded user decisions support screening. Open location evidence for the individual record, imagery dates and limitations.</p>
            <p><strong>Before planting:</strong> owner/city permission, utilities, usable space, current conditions and species suitability still need checking, including on unfenced lawns.</p>
          <p id="ecotrace-tree-historical-heat"><strong>Historical satellite surface temperature:</strong> Past surface-heat observations around a planting location help identify where more shade may be useful. They are not today’s temperature, current air temperature or an exact reading at the individual planting spot.</p>
          </div>
        </details>

        <details class="rounded-xl bg-white/5 p-3 ring-1 ring-white/10">
          <summary class="cursor-pointer text-xs font-black text-green-200">🌱 Tree Species Guide</summary>
          <div class="mt-2 space-y-1 text-[10px] leading-4 text-slate-300">
            <p><strong>EcoTrace list:</strong> curated 27-species recommendation set used for filtering by size, foliage, height, width, and flowers.</p>
            <p><strong>Official broader reference:</strong> Santa Clara County Master Plant List (non-expressway).</p>
            <p><strong>EcoTrace role:</strong> narrows the list for easier site planning; it does not replace an arborist or agency-approved planting list.</p>
          </div>
        </details>

        <details class="rounded-xl bg-white/5 p-3 ring-1 ring-white/10">
          <summary class="cursor-pointer text-xs font-black text-amber-200">🤝 Community / Planning Context</summary>
          <div class="mt-2 space-y-1 text-[10px] leading-4 text-slate-300">
            <p><strong>Census API:</strong> U.S. Census Bureau 2024 ACS 5-year API, using <code>CENSUS_API_KEY</code>.</p>
            <p><strong>Geocoding:</strong> Census geocoder for address → Census tract when Community context is requested.</p>
            <p><strong>Indicators:</strong> median household income, poverty, no-vehicle households, and age 65+.</p>
            <p><strong>Local posts/events:</strong> EcoTrace Community SQLite database.</p>
            <p><strong>EcoTrace calculation:</strong> county-relative descriptive context percentile; this is not an equity score, eligibility decision, or automatic planting-priority score.</p>
          </div>
        </details>

        <details class="rounded-xl bg-white/5 p-3 ring-1 ring-white/10">
          <summary class="cursor-pointer text-xs font-black text-lime-200">♻️ E-Waste</summary>
          <div class="mt-2 space-y-1 text-[10px] leading-4 text-slate-300">
            <p><strong>Local event/action data:</strong> EcoTrace’s own event records and archive workflow.</p>
            <p><strong>AI assistance:</strong> Gemini can estimate a broad item category/age when needed; it should be treated as an estimate, not an authoritative product database.</p>
            <p><strong>EcoTrace role:</strong> connects item identification → reuse/recycling decision → local collection event → completion/impact record.</p>
            <p><strong>Best final metric:</strong> items/devices collected, participants, and completed events rather than likes or post popularity.</p>
          </div>
        </details>

        <details class="rounded-xl bg-white/5 p-3 ring-1 ring-white/10">
          <summary class="cursor-pointer text-xs font-black text-violet-200">💬 Gemini Assistant</summary>
          <div class="mt-2 space-y-1 text-[10px] leading-4 text-slate-300">
            <p><strong>AI:</strong> Google Gemini API through EcoTrace’s Gemini assistant backend.</p>
            <p><strong>Context sent:</strong> current EcoTrace route, bike, heat/tree, route-planting, Community/Census, E-Waste, selected map center, Tree Guide, and Guide context when available.</p>
            <p><strong>EcoTrace rule:</strong> Gemini explains the app’s mapped/modelled results; it must not turn screened candidates, modeled shade, or descriptive Census context into guaranteed facts.</p>
          </div>
        </details>

        <div class="rounded-xl bg-slate-950/40 p-3 ring-1 ring-white/10">
          <p class="text-[10px] font-black uppercase tracking-wider text-slate-300">Quick meaning of the labels</p>
          <p class="mt-2 text-[10px] leading-4 text-slate-400"><strong>Mapped:</strong> comes from an external GIS/API/dataset.</p>
          <p class="text-[10px] leading-4 text-slate-400"><strong>Modeled:</strong> EcoTrace calculates a result from mapped inputs.</p>
          <p class="text-[10px] leading-4 text-slate-400"><strong>AI-assisted:</strong> Gemini explains/estimates; it is not the authoritative data source.</p>
        </div>
      </div>

      <div id="guide-about" class="guide-subpanel hidden space-y-2">
        
        
        
        <div class="rounded-xl bg-emerald-400/10 p-3 ring-1 ring-emerald-300/20">
          <p class="text-sm font-black text-emerald-100">What is EcoTrace?</p>
          <p class="mt-2 text-xs leading-5 text-slate-300">
            EcoTrace combines walking and cycling shade estimates, 5,582 mapped tree-planning candidates, community discussions and e-waste exchanges. Route shade is modeled; planting candidates still require site approval.
          </p>
        </div>
        <div class="rounded-xl bg-white/5 p-3 ring-1 ring-white/10">
          <p class="text-[10px] font-black uppercase tracking-wider text-slate-300">Design principle</p>
          <p class="mt-2 text-xs leading-5 text-slate-400">
            Environmental data → a specific location → an explainable recommendation → community action → measurable outcome.
          </p>
          <details class="mt-3 rounded-lg bg-black/20 p-2">
            <summary class="cursor-pointer text-xs font-bold text-sky-100">Technical pipeline</summary>
            <p class="mt-2 text-[10px] leading-4 text-slate-300">EcoTrace combines route-network search, geospatial screening, distance-weighted segment scoring, solar-position geometry, canopy/building shadow estimation, bike-infrastructure matching, multi-source GIS validation, and AI explanation.</p>
            <p class="mt-2 text-[9px] leading-4 text-slate-500"><em>*Note: OpenRouteService supplies route geometry; EcoTrace adds the environmental scoring, comparison, and validation layers.</em></p>
          </details>
        </div>
        <div class="rounded-xl bg-white/5 p-3 ring-1 ring-white/10">
          <p class="text-[10px] font-black uppercase tracking-wider text-slate-300">Before public deployment</p>
          <p class="mt-2 text-xs leading-5 text-slate-400">
            Public Community/E-Waste features should include moderation, verified organizers, privacy rules, abuse/rate limits, accessibility review, and agency/field validation workflows.
          </p>
        </div>
      </div>

      
    </section>

    <section id="ewaste-panel" class="tool-panel">
      <div class="mb-3 rounded-2xl bg-lime-400/10 p-3 ring-1 ring-lime-300/20">
        <h2 class="font-black text-lime-100">E-Waste Reuse & Recycling</h2>
        <p class="mt-1 text-xs leading-5 text-slate-300">Choose the device yourself, compare recommended actions, prepare it safely, exchange it locally, or find collection options.</p>
        <details class="mt-2 rounded-xl bg-black/15 p-2 text-xs text-slate-300"><summary class="cursor-pointer font-bold text-lime-100">Six major e-waste categories</summary><div class="mt-2 space-y-1 leading-5"><p><strong>Temperature Exchange:</strong> refrigerators, freezers, air conditioners, heat pumps.</p><p><strong>Screens & Monitors:</strong> TVs, monitors, laptops, notebooks, tablets.</p><p><strong>Lamps:</strong> fluorescent, HID, and LED lamps.</p><p><strong>Large Equipment:</strong> washers, dryers, dishwashers, stoves, large printers.</p><p><strong>Small Equipment:</strong> vacuums, microwaves, kettles, shavers, toys.</p><p><strong>Small IT & Telecom:</strong> phones, GPS devices, routers, calculators, personal computers.</p></div></details>
      </div>

      <div class="mb-3 grid grid-cols-3 gap-1 rounded-xl bg-white/5 p-1 ring-1 ring-white/10">
        <button class="ewaste-tab rounded-lg bg-lime-400 px-2 py-2 text-[11px] font-bold text-lime-950" data-ewaste-tab="recommend">Recommend</button>
        <button class="ewaste-tab rounded-lg px-2 py-2 text-[11px] font-bold text-slate-300" data-ewaste-tab="exchange">Exchange</button>
        <button class="ewaste-tab rounded-lg px-2 py-2 text-[11px] font-bold text-slate-300" data-ewaste-tab="events">Events</button>
        <button class="ewaste-tab rounded-lg px-2 py-2 text-[11px] font-bold text-slate-300" data-ewaste-tab="archive">Archive</button>
        <button class="ewaste-tab rounded-lg px-2 py-2 text-[11px] font-bold text-slate-300" data-ewaste-tab="centers">Centers</button>
      </div>

      <section id="ewaste-recommend" class="ewaste-section">
        <div class="rounded-2xl bg-white/5 p-3 ring-1 ring-white/10">
          <div class="grid grid-cols-2 gap-2">
            <label class="text-[10px] font-bold uppercase tracking-wider text-slate-400">Device
              <select id="ewaste-device" class="mt-1 w-full rounded-xl border border-white/10 bg-slate-950/70 px-2 py-2 text-sm normal-case text-white">
                <option value="" selected disabled>Select device/item</option><option value="temperature_exchange">Temperature Exchange Equipment</option><option value="screens_monitors">Screens and Monitors</option><option value="lamps">Lamps</option><option value="large_equipment">Large Equipment</option><option value="small_equipment">Small Equipment</option><option value="small_it_telecom">Small IT and Telecommunication Equipment</option><option value="custom">Custom device / item</option>
              </select>
            </label>
            <label class="text-[10px] font-bold uppercase tracking-wider text-slate-400">Condition
              <select id="ewaste-condition" class="mt-1 w-full rounded-xl border border-white/10 bg-slate-950/70 px-2 py-2 text-sm normal-case text-white">
                <option value="" selected disabled>Select condition</option><option value="working_good">Working well</option><option value="working_worn">Works but worn</option><option value="not_working">Not working</option><option value="unknown">Unknown</option>
              <option value="other">Other / describe</option></select><input id="ewaste-condition-other" class="mt-2 hidden w-full rounded-lg bg-slate-950/70 px-2 py-2 text-xs normal-case text-white ring-1 ring-white/10" placeholder="Describe the condition for Gemini" autocomplete="off">
            </label>
          </div>
          <div class="mt-2 grid grid-cols-2 gap-2">
            <label class="text-[10px] font-bold uppercase tracking-wider text-slate-400">Approximate age
              <select id="ewaste-age-mode" class="mt-1 w-full rounded-xl border border-white/10 bg-slate-950/70 px-2 py-2 text-xs normal-case text-white">
                <option value="single" selected>Single estimate</option>
                <option value="range">Age range</option>
              </select>
              <input id="ewaste-age" type="number" min="0" max="100" step="0.5" placeholder="Example: 6 years" class="mt-2 w-full rounded-xl border border-white/10 bg-slate-950/70 px-3 py-2 text-sm normal-case text-white" autocomplete="off">
              <div id="ewaste-age-range-wrap" class="mt-2 hidden grid grid-cols-2 gap-2">
                <input id="ewaste-age-min" type="number" min="0" max="100" step="0.5" class="w-full rounded-xl border border-white/10 bg-slate-950/70 px-2 py-2 text-xs normal-case text-white" placeholder="Min years" autocomplete="off">
                <input id="ewaste-age-max" type="number" min="0" max="100" step="0.5" class="w-full rounded-xl border border-white/10 bg-slate-950/70 px-2 py-2 text-xs normal-case text-white" placeholder="Max years" autocomplete="off">
              </div>
              <p id="ewaste-age-help" class="mt-1 text-[9px] normal-case leading-4 text-slate-500">Use a single best estimate, or choose a range when you are genuinely unsure.</p>
            </label>
            <label class="text-[10px] font-bold uppercase tracking-wider text-slate-400">Brand/model
              <input id="ewaste-model" maxlength="120" class="mt-1 w-full rounded-xl border border-white/10 bg-slate-950/70 px-3 py-2 text-sm normal-case text-white" placeholder="Optional" autocomplete="off">
            </label>
          </div>          <div id="ewaste-custom-item-fields" class="mt-2 hidden rounded-xl bg-violet-300/5 p-3 ring-1 ring-violet-300/15">
            <p class="text-[10px] font-black uppercase tracking-wider text-violet-200">Custom item details</p>
            <div class="mt-2 grid grid-cols-2 gap-2"><input id="ewaste-custom-category" class="rounded-lg bg-black/20 px-2 py-2 text-xs text-white ring-1 ring-white/10" placeholder="Exact item/category" autocomplete="off"><input id="ewaste-custom-brand" class="rounded-lg bg-black/20 px-2 py-2 text-xs text-white ring-1 ring-white/10" placeholder="Brand" autocomplete="off"><input id="ewaste-custom-model" class="rounded-lg bg-black/20 px-2 py-2 text-xs text-white ring-1 ring-white/10" placeholder="Model" autocomplete="off"><input id="ewaste-custom-year" class="rounded-lg bg-black/20 px-2 py-2 text-xs text-white ring-1 ring-white/10" placeholder="Approx. year / age" autocomplete="off"><select id="ewaste-custom-condition" class="rounded-lg bg-black/20 px-2 py-2 text-xs text-white ring-1 ring-white/10"><option value="">Condition unknown</option><option>Working</option><option>Partially working</option><option>Broken</option><option>Visible damage</option><option>Battery concern</option><option>Other</option></select><input id="ewaste-custom-battery" class="rounded-lg bg-black/20 px-2 py-2 text-xs text-white ring-1 ring-white/10" placeholder="Battery / power info" autocomplete="off"></div>
            <input id="ewaste-custom-accessories" class="mt-2 w-full rounded-lg bg-black/20 px-2 py-2 text-xs text-white ring-1 ring-white/10" placeholder="Accessories / charger / cables" autocomplete="off"><textarea id="ewaste-custom-notes" rows="2" class="mt-2 w-full rounded-lg bg-black/20 px-2 py-2 text-xs text-white ring-1 ring-white/10" placeholder="Other details"></textarea>
          </div>
          <div class="mt-3 rounded-xl bg-violet-400/10 p-3 ring-1 ring-violet-300/20">
            <div class="flex items-start justify-between gap-2">
              <div>
                <h3 class="text-sm font-black text-violet-100">Gemini device photo helper</h3>
                <p class="mt-1 text-[11px] leading-5 text-slate-300">Upload a photo and optionally describe the item. EcoTrace first checks whether it is actually e-waste; clearly unrelated or non-electronic images are labeled Not e-waste.</p>
              </div>
              <span class="rounded-full bg-violet-400/15 px-2 py-1 text-[9px] font-bold text-violet-200">Photo + text</span>
            </div>

            <div id="gemini-ewaste-photo-drop" class="mt-3 cursor-pointer rounded-xl border-2 border-dashed border-violet-300/25 bg-slate-950/50 p-4 text-center text-xs leading-5 text-slate-400">
              Drag a JPG, PNG, or WebP device photo here, or click to choose.
              <input id="gemini-ewaste-photo-file" type="file" accept="image/jpeg,image/png,image/webp" class="hidden" autocomplete="off">
            </div>

            <div id="gemini-ewaste-photo-preview-wrap" class="mt-2 hidden rounded-xl bg-black/20 p-2">
              <img id="gemini-ewaste-photo-preview" class="max-h-44 w-full rounded-lg object-contain" alt="Device photo preview" />
              <button id="gemini-ewaste-photo-remove" type="button" class="mt-2 text-xs font-bold text-red-300">Remove photo</button>
            </div>

            <label class="mt-2 block text-[10px] font-bold uppercase tracking-wider text-slate-400">
              Optional description
              <textarea id="gemini-ewaste-description" maxlength="1200" rows="2" class="mt-1 w-full rounded-xl border border-white/10 bg-slate-950/70 px-3 py-2 text-sm normal-case text-white" placeholder="Example: This is an old Dell laptop; it turns on but the screen is cracked."></textarea>
            </label>

            <button id="gemini-ewaste-analyze" type="button" class="mt-3 w-full rounded-xl bg-violet-400 px-4 py-2.5 text-sm font-black text-violet-950">Analyze device photo</button>
            <p id="gemini-ewaste-status" class="mt-2 min-h-[18px] text-xs leading-5 text-slate-400"></p>
            <div id="gemini-ewaste-result" class="mt-3 hidden"></div>

            <p class="mt-3 rounded-lg bg-amber-400/10 p-2 text-[10px] leading-4 text-amber-100">
              Note: Photo results can be wrong when the image is blurry, dark, obstructed, missing labels, or shows only part of the device. Confirm the device type, condition, and whether it powers on before following instructions.
            </p>
          </div>

          <button id="ewaste-recommend-button" type="button" class="mt-3 w-full rounded-xl bg-lime-400 px-4 py-2.5 text-sm font-black text-lime-950">Select item details, then get recommendations</button>
          <p id="ewaste-recommend-status" class="mt-2 text-xs text-slate-400"></p>
        </div>
        <div id="ewaste-recommendations" class="mt-3 space-y-3"></div>
      </section>

      <section id="ewaste-exchange" class="ewaste-section hidden">
        <div class="rounded-2xl bg-white/5 p-3 ring-1 ring-white/10">
          <h3 class="text-sm font-black text-white">Create exchange listing</h3>
          <div class="mt-2 grid grid-cols-2 gap-2">
            <input id="ewaste-listing-name" class="rounded-xl border border-white/10 bg-slate-950/70 px-3 py-2 text-sm text-white" placeholder="Display name" autocomplete="off">
            <select id="ewaste-listing-type" class="rounded-xl border border-white/10 bg-slate-950/70 px-3 py-2 text-sm text-white"><option value="sell">Sell</option><option value="give_away">Give away</option><option value="wanted">Wanted</option></select>
          </div>
          <div class="mt-2 grid grid-cols-2 gap-2">
            <select id="ewaste-listing-device" class="rounded-xl border border-white/10 bg-slate-950/70 px-3 py-2 text-sm text-white"><option value="temperature_exchange">Temperature Exchange Equipment</option><option value="screens_monitors">Screens and Monitors</option><option value="lamps">Lamps</option><option value="large_equipment">Large Equipment</option><option value="small_equipment">Small Equipment</option><option value="small_it_telecom">Small IT and Telecommunication Equipment</option></select>
            <input id="ewaste-listing-price" type="number" min="0" step="1" class="rounded-xl border border-white/10 bg-slate-950/70 px-3 py-2 text-sm text-white" placeholder="Approx. price $" autocomplete="off">
          </div>
          <input id="ewaste-listing-title" class="mt-2 w-full rounded-xl border border-white/10 bg-slate-950/70 px-3 py-2 text-sm text-white" placeholder="Listing title" autocomplete="off">
          <textarea id="ewaste-listing-description" rows="3" class="mt-2 w-full rounded-xl border border-white/10 bg-slate-950/70 px-3 py-2 text-sm text-white" placeholder="Condition, accessories, defects, and pickup details"></textarea>
          <input id="ewaste-listing-location" class="mt-2 w-full rounded-xl border border-white/10 bg-slate-950/70 px-3 py-2 text-sm text-white" placeholder="General pickup area, not a home address" autocomplete="off">
          <div id="ewaste-photo-drop" class="mt-2 cursor-pointer rounded-xl border-2 border-dashed border-white/15 bg-slate-950/50 p-4 text-center text-xs text-slate-400">Drag a photo here or click to choose<input id="ewaste-photo-file" type="file" accept="image/jpeg,image/png,image/webp" class="hidden" autocomplete="off"></div>
          <img id="ewaste-photo-preview" class="mt-2 hidden max-h-40 w-full rounded-lg object-contain" alt="E-waste listing photo preview" />
          <input id="ewaste-image-url" type="hidden" autocomplete="off">
          <button id="ewaste-listing-submit" type="button" class="mt-3 w-full rounded-xl bg-lime-400 px-4 py-2.5 text-sm font-black text-lime-950">Post listing</button>
          <p id="ewaste-listing-status" class="mt-2 text-xs text-slate-400"></p>
          <p class="mt-3 rounded-lg bg-amber-400/10 p-2 text-[10px] leading-4 text-amber-100">
            Exchange messages are public. Do not post phone numbers, home addresses, payment details, passwords, or account credentials; arrange any meetup in a safe public place with adult supervision when appropriate.
          </p>
        </div>
        <div id="ewaste-listings" class="mt-3 space-y-3"></div>
      </section>

      <section id="ewaste-events" class="ewaste-section hidden">
        <div class="rounded-2xl bg-white/5 p-3 ring-1 ring-white/10">
          <h3 class="text-sm font-black text-white">Host e-waste collection event</h3>
          <label class="mt-2 block text-[10px] font-bold uppercase tracking-wider text-slate-400">Host name
            <input id="ewaste-event-host" class="mt-1 w-full rounded-xl border border-white/10 bg-slate-950/70 px-3 py-2 text-sm normal-case text-white" placeholder="Organization or display name" autocomplete="off">
          </label>
          <label class="mt-2 block text-[10px] font-bold uppercase tracking-wider text-slate-400">Event title
            <input id="ewaste-event-title" class="mt-1 w-full rounded-xl border border-white/10 bg-slate-950/70 px-3 py-2 text-sm normal-case text-white" placeholder="Community e-waste collection" autocomplete="off">
          </label>
          <label class="mt-2 block text-[10px] font-bold uppercase tracking-wider text-slate-400">Accepted devices and details
            <textarea id="ewaste-event-description" rows="3" class="mt-1 w-full rounded-xl border border-white/10 bg-slate-950/70 px-3 py-2 text-sm normal-case text-white" placeholder="What people can bring and any instructions"></textarea>
          </label>
          <div class="mt-2 grid grid-cols-2 gap-2">
            <label class="text-[10px] font-bold uppercase tracking-wider text-slate-400">Date and start time
              <input id="ewaste-event-date" type="datetime-local" class="mt-1 w-full rounded-xl border border-white/10 bg-slate-950/70 px-3 py-2 text-sm normal-case text-white" autocomplete="off">
            </label>
            <label class="text-[10px] font-bold uppercase tracking-wider text-slate-400">Maximum people
              <input id="ewaste-event-max" type="number" min="1" value="50" class="mt-1 w-full rounded-xl border border-white/10 bg-slate-950/70 px-3 py-2 text-sm normal-case text-white" autocomplete="off">
            </label>
          </div>
          <label class="mt-2 block text-[10px] font-bold uppercase tracking-wider text-slate-400">Approximate duration
            <select id="ewaste-event-duration" class="mt-1 w-full rounded-xl border border-white/10 bg-slate-950/70 px-3 py-2 text-sm normal-case text-white">
              <option value="Undecided">Undecided</option>
              <option value="About 30 minutes">About 30 minutes</option>
              <option value="About 1 hour">About 1 hour</option>
              <option value="About 2 hours">About 2 hours</option>
              <option value="About 3 hours">About 3 hours</option>
              <option value="Half day">Half day</option>
              <option value="All day">All day</option>
            </select>
          </label>
          <label class="mt-2 block text-[10px] font-bold uppercase tracking-wider text-slate-400">Public event location
            <input id="ewaste-event-location" class="mt-1 w-full rounded-xl border border-white/10 bg-slate-950/70 px-3 py-2 text-sm normal-case text-white" placeholder="Library, school, park, or other public place" autocomplete="off">
          </label>
          <button id="ewaste-event-submit" type="button" class="mt-3 w-full rounded-xl bg-lime-400 px-4 py-2.5 text-sm font-black text-lime-950">Create event</button>
          <p id="ewaste-event-status" class="mt-2 text-xs text-slate-400"></p>
        </div>
        <div id="ewaste-event-list" class="mt-3 space-y-3"></div>
      </section>

      <section id="ewaste-archive" class="ewaste-section hidden">
        <div class="rounded-2xl bg-white/5 p-3 ring-1 ring-white/10">
          <h3 class="text-sm font-black text-white">Past collection events</h3>
          <p class="mt-1 text-xs leading-5 text-slate-400">Past events appear here automatically. Dates use Pacific time; events with no clear end time archive after their scheduled day.</p>
        </div>
        <div id="ewaste-archive-list" class="mt-3 space-y-3"></div>
      </section>

      <section id="ewaste-centers" class="ewaste-section hidden">
        <div class="rounded-2xl bg-white/5 p-3 ring-1 ring-white/10">
          <p class="text-xs leading-5 text-slate-300">Choose a point on the map, then EcoTrace will search all mapped recycling-related locations within the selected radius and highlight the two closest.</p>
          <div class="mt-3 grid grid-cols-2 gap-2">
            <button id="ewaste-center-use-point" type="button" class="rounded-xl bg-white/5 px-3 py-2.5 text-sm font-bold text-lime-100 ring-1 ring-lime-300/20">Choose map point</button>
            <div class="rounded-xl bg-black/20 px-3 py-2.5 text-xs leading-5 text-slate-300">Curated Santa Clara-area directory</div>
          </div>
          <div class="mt-2 flex gap-2">
            <input id="ewaste-center-address" class="min-w-0 flex-1 rounded-xl border border-white/10 bg-slate-950/70 px-3 py-2 text-sm text-white" placeholder="Enter an address or place" autocomplete="off">
            <button id="ewaste-center-address-search" type="button" class="rounded-xl bg-lime-300/15 px-3 py-2 text-xs font-black text-lime-100 ring-1 ring-lime-300/20">Use address</button>
          </div>
          <div id="ewaste-center-point-box" class="mt-2 rounded-xl bg-black/20 p-3 text-xs text-slate-300">Using current map center.</div>
          <button id="ewaste-center-search" type="button" class="mt-3 w-full rounded-xl bg-lime-400 px-4 py-2.5 text-sm font-black text-lime-950">Find all nearby centers</button>
          <p id="ewaste-center-status" class="mt-2 text-xs leading-5 text-slate-400"></p>
        </div>
        <div id="ewaste-closest-centers" class="mt-3 space-y-3"></div>
        <div class="mt-4 flex items-center justify-between">
          <h3 class="text-sm font-black text-white">All mapped locations</h3>
          <button id="ewaste-center-map-toggle" type="button" class="rounded-lg bg-red-400/15 px-2.5 py-1.5 text-[11px] font-bold text-red-100 ring-1 ring-red-300/20">Hide E-Waste center points</button>
        </div>
        <div id="ewaste-center-list" class="mt-3 space-y-3"></div>
      </section>
    </section>

    <section id="profile-panel" class="tool-panel">
      <div class="mb-3 rounded-2xl bg-cyan-400/10 p-3 ring-1 ring-cyan-300/20">
        <div class="flex items-start justify-between gap-2">
          <div>
            <h2 class="font-black text-cyan-100">Account Settings</h2>
            <p id="profile-account-summary" class="mt-1 text-xs leading-5 text-slate-300">Loading your EcoTrace account…</p>
          </div>
          <button id="profile-back" type="button" class="rounded-lg bg-white/5 px-2 py-1 text-[10px] font-bold text-slate-300 ring-1 ring-white/10">Back</button>
        </div>
      </div>

      <div id="profile-signedin-settings" class="hidden rounded-2xl bg-white/5 p-3 ring-1 ring-white/10">
        <div class="grid grid-cols-2 gap-2">
          <label class="text-[9px] font-black uppercase text-slate-500">Username
            <input id="profile-username" disabled class="mt-1 w-full rounded-lg bg-slate-900/70 px-2 py-2 text-xs text-slate-400 ring-1 ring-white/10" autocomplete="off">
          </label>
          <label class="text-[9px] font-black uppercase text-slate-500">Full name
            <input id="profile-full-name" class="mt-1 w-full rounded-lg bg-slate-950/70 px-2 py-2 text-xs text-white ring-1 ring-white/10" autocomplete="off">
          </label>
          <label class="text-[9px] font-black uppercase text-slate-500">Nickname / name you go by
            <input id="profile-nickname" class="mt-1 w-full rounded-lg bg-slate-950/70 px-2 py-2 text-xs text-white ring-1 ring-white/10" autocomplete="off">
          </label>
          <label class="text-[9px] font-black uppercase text-slate-500">Display name
            <input id="profile-display-name" maxlength="40" class="mt-1 w-full rounded-lg bg-slate-950/70 px-2 py-2 text-xs text-white ring-1 ring-white/10" autocomplete="off">
          </label>
        </div>

        <div class="mt-2 grid grid-cols-3 gap-2">
          <button id="profile-display-full" type="button" class="rounded-lg bg-cyan-300/10 px-2 py-2 text-[10px] font-bold text-cyan-100 ring-1 ring-cyan-300/20">Use full name</button>
          <button id="profile-display-nickname" type="button" class="rounded-lg bg-cyan-300/10 px-2 py-2 text-[10px] font-bold text-cyan-100 ring-1 ring-cyan-300/20">Use nickname</button>
          <button id="profile-display-custom" type="button" class="rounded-lg bg-cyan-300/10 px-2 py-2 text-[10px] font-bold text-cyan-100 ring-1 ring-cyan-300/20">Custom display</button>
        </div>

        <label class="mt-2 block text-[9px] font-black uppercase text-slate-500">Role
          <select id="profile-role" class="mt-1 w-full rounded-lg bg-slate-950/70 px-2 py-2 text-xs text-white ring-1 ring-white/10">
            <option value="student">Student</option><option value="adult_volunteer">Adult volunteer</option><option value="organizer">Organizer</option><option value="school_staff">School / teacher / staff</option><option value="nonprofit">Nonprofit / community organization</option><option value="government">Government / public agency</option><option value="business">Business / company</option><option value="other">Other</option>
          </select>
        </label>
        <input id="profile-organization" class="mt-2 w-full rounded-lg bg-slate-950/70 px-2 py-2 text-xs text-white ring-1 ring-white/10" placeholder="School / organization / employer" autocomplete="off">
        <input id="profile-education-work" class="mt-2 w-full rounded-lg bg-slate-950/70 px-2 py-2 text-xs text-white ring-1 ring-white/10" placeholder="Education / work / title" autocomplete="off">
        <textarea id="profile-bio" rows="3" class="mt-2 w-full rounded-lg bg-slate-950/70 px-2 py-2 text-xs text-white ring-1 ring-white/10" placeholder="Short public bio"></textarea>
        <label class="mt-2 flex items-center gap-2 text-[10px] text-slate-300"><input id="profile-share-history" type="checkbox" autocomplete="off"> Share my public event history</label>
        <label class="mt-2 flex items-center gap-2 text-[10px] text-cyan-100"><input id="profile-autofill" type="checkbox" checked autocomplete="off"> Automatically fill my name/profile into Community and E-Waste forms</label>

        <button id="profile-save" type="button" class="mt-3 w-full rounded-lg bg-cyan-400 px-3 py-2 text-xs font-black text-cyan-950">Save profile</button>
        <p id="profile-save-status" class="mt-2 text-[10px] text-slate-500"></p>

        <details class="mt-3 rounded-xl bg-black/20 p-3">
          <summary class="cursor-pointer text-xs font-black text-violet-100">Change password</summary>
          <input id="profile-current-password" type="password" class="mt-2 w-full rounded-lg bg-slate-950/70 px-2 py-2 text-xs text-white ring-1 ring-white/10" placeholder="Current password" autocomplete="off">
          <input id="profile-new-password" type="password" class="mt-2 w-full rounded-lg bg-slate-950/70 px-2 py-2 text-xs text-white ring-1 ring-white/10" placeholder="New password (8+ characters)" autocomplete="off">
          <button id="profile-change-password" type="button" class="mt-2 w-full rounded-lg bg-violet-400 px-2 py-2 text-xs font-black text-violet-950">Change password</button>
          <p id="profile-password-status" class="mt-2 text-[10px] text-slate-500"></p>
        </details>

        <div class="mt-3 grid grid-cols-2 gap-2">
          <button id="profile-switch-account" type="button" class="rounded-lg bg-amber-300/15 px-3 py-2 text-xs font-black text-amber-100 ring-1 ring-amber-300/20">Switch account</button>
          <form action="/logout" method="post"><button type="submit" class="w-full rounded-lg bg-red-400/15 px-3 py-2 text-xs font-black text-red-100 ring-1 ring-red-300/20">Sign out</button></form>
        </div>
      </div>

      <div id="profile-guest-settings" class="hidden rounded-2xl bg-white/5 p-3 ring-1 ring-white/10">
        <p class="text-xs leading-5 text-slate-300">You are using EcoTrace as a guest.</p>
        <a href="/" class="mt-3 block rounded-lg bg-cyan-400 px-3 py-2 text-center text-xs font-black text-cyan-950">Sign in or create account</a>
      </div>


    </section>

    <section id="community-panel" class="tool-panel">
      <div class="mb-3 rounded-2xl bg-amber-400/10 p-3 ring-1 ring-amber-300/20">
        <h2 class="font-black text-amber-100">Community Environmental Hub</h2>
        <p class="mt-1 text-xs leading-5 text-slate-300">
          Share local environmental ideas, suggest tree locations, organize events, and discuss community projects.
        </p>
      </div>

      <div class="mb-3 rounded-2xl bg-violet-400/5 p-3 ring-1 ring-violet-300/15">
        <div>
          <p class="text-[10px] font-black uppercase tracking-wider text-violet-200">Community Planning Context</p>
          <p class="mt-1 text-[11px] leading-5 text-slate-400">
            Search an address or choose a map point to view neutral Census tract context. This does not rank who deserves investment.
          </p>
        </div>
        <div class="mt-2 flex gap-2">
          <input id="community-context-address" list="community-context-suggestions"
            class="min-w-0 flex-1 rounded-lg border border-white/10 bg-slate-950/60 px-2.5 py-2 text-[11px] text-white outline-none placeholder:text-slate-500 focus:border-violet-300"
            placeholder="Address or landmark" autocomplete="off" />
          <datalist id="community-context-suggestions"></datalist>
          <button id="community-context-search" type="button"
            class="rounded-lg bg-violet-300/15 px-2.5 py-2 text-[10px] font-black text-violet-100 ring-1 ring-violet-300/20">
            Search
          </button>
        </div>
        <div class="mt-2 grid grid-cols-2 gap-2">
          <button id="community-context-map-point" type="button"
            class="rounded-lg bg-white/5 px-2.5 py-2 text-[10px] font-black text-slate-200 ring-1 ring-white/10">
            Choose map point
          </button>
          <button id="community-context-center" type="button"
            class="rounded-lg bg-white/5 px-2.5 py-2 text-[10px] font-black text-slate-200 ring-1 ring-white/10">
            Use map center
          </button>
        </div>
        <p id="community-context-status" class="mt-2 text-[10px] leading-4 text-slate-500"></p>
        <div id="community-context-result" class="mt-2 hidden"></div>
      </div>

      <div class="mb-3 grid grid-cols-4 gap-1 rounded-xl bg-white/5 p-1 ring-1 ring-white/10">
        <button type="button" class="community-subtab active rounded-lg px-2 py-2 text-[11px] font-bold" data-community-tab="feed">Current Posts</button>
        <button type="button" class="community-subtab rounded-lg px-2 py-2 text-[11px] font-bold" data-community-tab="trees">Trees</button>
        <button type="button" class="community-subtab rounded-lg px-2 py-2 text-[11px] font-bold" data-community-tab="events">Events</button>
        <button type="button" class="community-subtab rounded-lg px-2 py-2 text-[11px] font-bold" data-community-tab="archive">Archive</button>
        <button type="button" class="community-subtab rounded-lg px-2 py-2 text-[11px] font-bold" data-community-tab="discussions">Discuss</button><button type="button" class="community-subtab rounded-lg px-2 py-2 text-[11px] font-bold" data-community-tab="activity">My Activity</button><button type="button" class="community-subtab rounded-lg px-2 py-2 text-[11px] font-bold" data-community-tab="impact">Impact History</button><button type="button" class="community-subtab rounded-lg px-2 py-2 text-[11px] font-bold" data-community-tab="validation">Validate</button>
      </div>

      <section id="community-validation-card" class="mb-3 hidden space-y-2">
        <div class="rounded-xl bg-sky-400/10 p-3 ring-1 ring-sky-300/20">
          <p class="text-xs font-black text-sky-100">Environmental Validation & Reports</p>
          <p class="mt-1 text-[10px] leading-4 text-slate-300">
            Anyone may report a questionable result, but public reports never change EcoTrace data or official accuracy numbers by themselves.
          </p>
        </div>

        <div class="grid grid-cols-3 gap-2">
          <div class="rounded-xl bg-white/5 p-3 text-center"><p id="validation-tree-rate" class="text-lg font-black text-white">—</p><p class="text-[9px] text-slate-500">Verified tree-site validity</p></div>
          <div class="rounded-xl bg-white/5 p-3 text-center"><p id="validation-route-rate" class="text-lg font-black text-white">—</p><p class="text-[9px] text-slate-500">Verified route success</p></div>
          <div class="rounded-xl bg-white/5 p-3 text-center"><p id="validation-bike-rate" class="text-lg font-black text-white">—</p><p class="text-[9px] text-slate-500">Verified OSM ↔ VTA agreement</p></div>
        </div>

        <div class="rounded-xl bg-amber-300/5 p-3 ring-1 ring-amber-300/15">
          <p class="text-[10px] font-black uppercase tracking-wider text-amber-200">Report a result</p>
          <p class="mt-1 text-[9px] leading-4 text-slate-500">A report is a request for review, not a vote and not an automatic correction.</p>
          <div class="mt-2 grid grid-cols-2 gap-2">
            <select id="validation-report-type" class="rounded-lg bg-black/20 px-2 py-2 text-xs text-white ring-1 ring-white/10"><option value="tree">Tree site</option><option value="route">Walking/bike route</option><option value="bike">Bike-infrastructure classification</option></select>
            <input id="validation-report-name" class="rounded-lg bg-black/20 px-2 py-2 text-xs text-white ring-1 ring-white/10" placeholder="Your name (optional if signed in)" autocomplete="off">
          </div>
          <input id="validation-report-location" class="mt-2 w-full rounded-lg bg-black/20 px-2 py-2 text-xs text-white ring-1 ring-white/10" placeholder="Location / route / coordinate" autocomplete="off">
          <textarea id="validation-report-note" rows="2" class="mt-2 w-full rounded-lg bg-black/20 px-2 py-2 text-xs text-white ring-1 ring-white/10" placeholder="What looks wrong, and what did you observe?"></textarea>
          <input id="validation-report-evidence" class="mt-2 w-full rounded-lg bg-black/20 px-2 py-2 text-xs text-white ring-1 ring-white/10" placeholder="Optional evidence link" autocomplete="off">
          <button id="validation-report-submit" type="button" class="mt-2 w-full rounded-lg bg-amber-400 px-2 py-2 text-xs font-black text-amber-950">Submit for review</button>
          <p id="validation-report-status" class="mt-2 text-[10px] text-slate-500"></p>
        </div>

        <div id="validation-reviewer-panel" class="hidden rounded-xl bg-emerald-300/5 p-3 ring-1 ring-emerald-300/15">
          <div class="flex items-center justify-between gap-2"><p class="text-[10px] font-black uppercase tracking-wider text-emerald-200">Trusted reviewer workspace</p><span class="rounded-full bg-emerald-300/10 px-2 py-1 text-[9px] font-bold text-emerald-100">Reviewer-only</span></div>
          <p class="mt-1 text-[9px] leading-4 text-slate-500">Reviewer status is separate from roles such as Student, Government, Nonprofit, or Business; self-selecting a role never grants validation power.</p>
          <div class="mt-2 grid grid-cols-2 gap-2">
            <select id="validation-type" class="rounded-lg bg-black/20 px-2 py-2 text-xs text-white ring-1 ring-white/10"><option value="tree">Tree site</option><option value="route">Walking/bike route</option><option value="bike">OSM ↔ VTA segment</option></select>
            <select id="validation-result" class="rounded-lg bg-black/20 px-2 py-2 text-xs text-white ring-1 ring-white/10"><option value="pass">Valid / Success / Match</option><option value="fail">Invalid / Failure / Mismatch</option></select>
          </div>
          <input id="validation-location" class="mt-2 w-full rounded-lg bg-black/20 px-2 py-2 text-xs text-white ring-1 ring-white/10" placeholder="Location / route / coordinate" autocomplete="off">
          <input id="validation-note" class="mt-2 w-full rounded-lg bg-black/20 px-2 py-2 text-xs text-white ring-1 ring-white/10" placeholder="What did you independently compare or observe?" autocomplete="off">
          <button id="validation-add" type="button" class="mt-2 w-full rounded-lg bg-emerald-400 px-2 py-2 text-xs font-black text-emerald-950">Add official validation check</button>

          <div class="mt-3">
            <div class="flex items-center justify-between"><p class="text-[10px] font-black uppercase text-slate-400">Pending public reports</p><button id="validation-refresh-reports" type="button" class="rounded-lg bg-white/5 px-2 py-1 text-[9px] text-slate-300">Refresh</button></div>
            <div id="validation-review-list" class="mt-2 max-h-72 space-y-2 overflow-y-auto text-[10px] text-slate-300"></div>
          </div>
        </div>

        <p class="rounded-lg bg-slate-900/70 p-2 text-[9px] leading-4 text-slate-400">
          <strong>Permission rule:</strong> account roles describe who someone is; they do not decide what they are allowed to verify. Official validation is limited to a separate trusted-reviewer list configured by the project owner.
        </p>
      </div>
      </section>
      <section id="community-impact-card" class="mb-3 hidden rounded-2xl bg-emerald-400/5 p-3 ring-1 ring-emerald-300/15">
        <div class="flex items-start justify-between gap-2">
          <div><h3 class="text-sm font-black text-emerald-100">Impact History</h3><p class="mt-1 text-[10px] leading-4 text-slate-400">Completed actions become measurable records instead of disappearing into an archive.</p></div>
          <span id="impact-total-badge" class="rounded-full bg-emerald-300/10 px-2 py-1 text-[9px] font-bold text-emerald-200">0 records</span>
        </div>
        <div id="community-impact-list" class="mt-3 space-y-2"><p class="text-xs text-slate-500">No impact records yet.</p></div>
      </section>

      

      <section id="community-activity-card" class="mb-3 hidden rounded-2xl bg-white/5 p-3 ring-1 ring-white/10">
        <div class="flex items-center justify-between"><h3 class="text-sm font-black text-white">My Activity</h3><span class="rounded-full bg-amber-300/10 px-2 py-1 text-[9px] font-bold text-amber-200">This browser / test profile</span></div>
        <div class="mt-3 grid grid-cols-2 gap-2"><div class="rounded-xl bg-black/20 p-3"><p class="text-[9px] uppercase text-slate-500">Events/actions I host</p><div id="community-hosted-activity" class="mt-2 text-xs text-slate-300">None yet.</div></div><div class="rounded-xl bg-black/20 p-3"><p class="text-[9px] uppercase text-slate-500">Events I joined</p><div id="community-joined-activity" class="mt-2 text-xs text-slate-300">None yet.</div></div></div>
        <div class="mt-3 rounded-xl bg-emerald-300/5 p-3 ring-1 ring-emerald-300/15"><p class="text-[10px] font-black uppercase tracking-wider text-emerald-200">Service Hours Log</p><div class="mt-2 grid grid-cols-[1fr_80px_120px_auto] gap-2"><input id="service-hours-title" class="rounded-lg bg-black/20 px-2 py-2 text-xs text-white ring-1 ring-white/10" placeholder="Event / activity" autocomplete="off"><input id="service-hours-value" type="number" min="0" step="0.25" class="rounded-lg bg-black/20 px-2 py-2 text-xs text-white ring-1 ring-white/10" placeholder="Hours" autocomplete="off"><select id="service-hours-status" class="rounded-lg bg-black/20 px-2 py-2 text-xs text-white ring-1 ring-white/10"><option value="self">Self-reported</option><option value="verified">Organizer verified</option></select><button id="service-hours-add" type="button" class="rounded-lg bg-emerald-400 px-2 py-2 text-xs font-black text-emerald-950">Add</button></div><div id="service-hours-list" class="mt-2 space-y-1 text-xs text-slate-300"></div></div>
      </section>
      <button id="community-create-toggle" type="button" class="mb-3 w-full rounded-xl bg-amber-400 px-4 py-2.5 text-sm font-black text-amber-950">+ Create a new post</button>
      <section id="community-create-card" class="rounded-2xl bg-white/5 p-3 ring-1 ring-white/10">
        <div class="flex items-center justify-between gap-2">
          <div>
            <h3 id="community-create-title" class="text-sm font-black text-white">Create a community post</h3>
            <p id="community-create-help" class="mt-1 text-[11px] leading-5 text-slate-400">Share an environmental update, question, or local idea.</p>
          </div>
          <button id="community-use-map-point" type="button" class="rounded-lg bg-amber-300/15 px-2.5 py-1.5 text-[11px] font-bold text-amber-100 ring-1 ring-amber-300/20">
            Use map point
          </button>
        </div>
        <div id="community-edit-post-banner" class="mt-3 hidden rounded-xl bg-sky-400/10 p-3 ring-1 ring-sky-300/20">
          <div class="flex items-center justify-between gap-2">
            <div><p class="text-xs font-black text-sky-100">Editing existing post</p><p class="mt-1 text-[10px] text-slate-400">This is the normal Create a Community Post form, filled with your saved post.</p></div>
            <button id="community-cancel-post-edit" type="button" class="rounded-lg bg-white/5 px-2 py-1 text-[10px] font-bold text-slate-200 ring-1 ring-white/10">Cancel edit</button>
          </div>
        </div>

        <div class="mt-3 grid grid-cols-2 gap-2">
          <label class="text-[10px] font-bold uppercase tracking-wider text-slate-400">
            Name
            <input id="community-name" maxlength="80" class="mt-1 w-full rounded-xl border border-white/10 bg-slate-950/70 px-3 py-2 text-sm normal-case text-white" placeholder="Your name" autocomplete="off">
          </label>
          <label class="text-[10px] font-bold uppercase tracking-wider text-slate-400">
            Post type
            <select id="community-category" class="mt-1 w-full rounded-xl border border-white/10 bg-slate-950/70 px-3 py-2 text-sm normal-case text-white">
              <option value="general">General post</option>
              <option value="tree_suggestion">Tree suggestion</option>
              <option value="park_cleanup">Park cleanup</option>
              <option value="environmental_campaign">Environmental campaign</option>
              <option value="discussion">Discussion</option>
              <option value="heat_report">Heat/shade report</option><option value="custom">Custom action/post type</option>
            </select>
          </label>
        </div>

        <label id="community-custom-type-wrap" class="mt-2 hidden block text-[10px] font-bold uppercase tracking-wider text-slate-400">
          Custom post/action type
          <input id="community-custom-type" maxlength="80" class="mt-1 w-full rounded-xl border border-white/10 bg-slate-950/70 px-3 py-2 text-sm normal-case text-white" placeholder="Example: school shade audit" autocomplete="off">
        </label>
        <div class="mt-2 rounded-xl bg-amber-300/5 p-3 ring-1 ring-amber-300/15">
          <p class="text-[10px] font-black uppercase tracking-wider text-amber-200">Organizer / profile (prototype)</p>
          <div class="mt-2 grid grid-cols-2 gap-2">
            <select id="community-role" class="rounded-xl border border-white/10 bg-slate-950/70 px-2 py-2 text-xs text-white">
              <option value="student">Student</option><option value="adult_volunteer">Adult volunteer</option><option value="organizer">Organizer</option><option value="school_staff">School / teacher / staff</option><option value="nonprofit">Nonprofit / community organization</option><option value="government">Government / public agency</option><option value="business">Business / company</option><option value="other">Other</option>
            </select><input id="community-role-other" class="mt-2 hidden w-full rounded-lg bg-slate-950/70 px-2 py-2 text-xs text-white ring-1 ring-white/10" placeholder="Describe your role" autocomplete="off">
            <input id="community-affiliation" maxlength="100" class="rounded-xl border border-white/10 bg-slate-950/70 px-2 py-2 text-xs text-white" placeholder="School / organization / job (optional)" autocomplete="off">
          </div>
          <label class="mt-2 flex items-center gap-2 text-[10px] text-slate-300"><input id="community-share-history" type="checkbox" autocomplete="off"> Share my public event/action history</label>
          <p class="mt-1 text-[9px] text-slate-500">Role labels are unverified/test labels in this prototype.</p>
        </div>
        <label class="mt-2 block text-[10px] font-bold uppercase tracking-wider text-slate-400">
          Title
          <input id="community-title" maxlength="120" class="mt-1 w-full rounded-xl border border-white/10 bg-slate-950/70 px-3 py-2 text-sm normal-case text-white" placeholder="Title" autocomplete="off">
        </label>

        <label class="mt-2 block text-[10px] font-bold uppercase tracking-wider text-slate-400">
          Description
          <textarea id="community-description" maxlength="2000" rows="4" class="mt-1 w-full resize-y rounded-xl border border-white/10 bg-slate-950/70 px-3 py-2 text-sm normal-case text-white" placeholder="Describe the idea, location, activity, or question."></textarea>
        </label>

        <label id="community-tree-reason-wrap" class="mt-2 hidden block text-[10px] font-bold uppercase tracking-wider text-slate-400">
          Why does this location need a tree?
          <textarea id="community-tree-reason" maxlength="500" rows="2" class="mt-1 w-full resize-y rounded-xl border border-white/10 bg-slate-950/70 px-3 py-2 text-sm normal-case text-white" placeholder="No shade, hot bus stop, exposed school route, empty planting strip…"></textarea>
        </label>

        <div id="community-event-fields" class="mt-2 hidden rounded-xl bg-violet-300/5 p-3 ring-1 ring-violet-300/15">
          <div class="grid grid-cols-2 gap-2">
            <label class="text-[10px] font-bold uppercase tracking-wider text-slate-400">Event date + start time<input id="community-event-date" type="datetime-local" class="mt-1 w-full rounded-xl border border-white/10 bg-slate-950/70 px-2 py-2 text-sm normal-case text-white" autocomplete="off"></label>
            <label class="text-[10px] font-bold uppercase tracking-wider text-slate-400">Time setup<select id="community-time-mode" class="mt-1 w-full rounded-xl border border-white/10 bg-slate-950/70 px-2 py-2 text-sm normal-case text-white"><option value="start">Start time only</option><option value="range">Start + end time</option></select></label>
          </div>
          <label id="community-end-time-wrap" class="mt-2 hidden block text-[10px] font-bold uppercase tracking-wider text-slate-400">End time<input id="community-event-end-time" type="time" class="mt-1 w-full rounded-xl border border-white/10 bg-slate-950/70 px-2 py-2 text-sm normal-case text-white" autocomplete="off"></label>
          <div class="mt-2 grid grid-cols-2 gap-2">
            <label class="text-[10px] font-bold uppercase tracking-wider text-slate-400">Participant setup<select id="community-capacity-mode" class="mt-1 w-full rounded-xl border border-white/10 bg-slate-950/70 px-2 py-2 text-sm normal-case text-white"><option value="unlimited">No participant limit</option><option value="maximum">Maximum participants</option><option value="minimum">Minimum participants needed</option></select></label>
            <label id="community-capacity-number-wrap" class="hidden text-[10px] font-bold uppercase tracking-wider text-slate-400">Participant count<input id="community-max-participants" type="number" min="1" max="10000" value="30" class="mt-1 w-full rounded-xl border border-white/10 bg-slate-950/70 px-3 py-2 text-sm normal-case text-white" autocomplete="off"></label>
          </div>
          <label class="mt-2 block text-[10px] font-bold uppercase tracking-wider text-slate-400">Quick event note<input id="community-event-note" maxlength="300" class="mt-1 w-full rounded-xl border border-white/10 bg-slate-950/70 px-3 py-2 text-sm normal-case text-white" placeholder="Meet at entrance, bring gloves, etc." autocomplete="off"></label>
        </div>

        <div class="mt-2 grid grid-cols-2 gap-2">
          <label class="text-[10px] font-bold uppercase tracking-wider text-slate-400">
            Address or landmark
            <div class="mt-1 flex gap-1"><input id="community-location-text" maxlength="220" class="min-w-0 flex-1 rounded-xl border border-white/10 bg-slate-950/70 px-3 py-2 text-sm normal-case text-white" placeholder="Enter place or location" autocomplete="off"><button id="community-location-geocode" type="button" class="rounded-xl bg-sky-300/15 px-2 text-[10px] font-black text-sky-100 ring-1 ring-sky-300/20">Locate</button></div>
          </label>
          <label class="text-[10px] font-bold uppercase tracking-wider text-slate-400">
            Visibility
            <select id="community-visibility" class="mt-1 w-full rounded-xl border border-white/10 bg-slate-950/70 px-3 py-2 text-sm normal-case text-white">
              <option value="public" selected>Community post</option>
              <option value="private_draft">Private draft</option>
            </select>
          </label>
        </div>

        <div class="mt-2">
          <p class="text-[10px] font-bold uppercase tracking-wider text-slate-400">Optional photo</p>
          <div id="community-photo-drop" class="mt-1 cursor-pointer rounded-xl border-2 border-dashed border-white/15 bg-slate-950/50 p-4 text-center text-xs text-slate-400">
            Drag a JPG, PNG, or WebP here, or click to choose. Maximum 5 MB.
            <input id="community-photo-file" type="file" accept="image/jpeg,image/png,image/webp" class="hidden" autocomplete="off">
          </div>
          <div id="community-photo-preview-wrap" class="mt-2 hidden rounded-xl bg-black/20 p-2">
            <img id="community-photo-preview" class="max-h-40 w-full rounded-lg object-contain" alt="Selected community photo preview" />
            <button id="community-photo-remove" type="button" class="mt-2 text-xs font-bold text-red-300">Remove photo</button>
          </div>
          <input id="community-image-url" type="hidden" autocomplete="off">
          <p id="community-upload-status" class="mt-1 text-[11px] text-slate-400"></p>
        </div>

        <label class="mt-2 block text-[10px] font-bold uppercase tracking-wider text-slate-400">
          Tags
          <input id="community-tags" maxlength="220" class="mt-1 w-full rounded-xl border border-white/10 bg-slate-950/70 px-3 py-2 text-sm normal-case text-white" placeholder="trees, school, cleanup (comma separated)" autocomplete="off">
        </label>

        <div id="community-coordinate-box" class="mt-2 rounded-xl bg-black/20 p-3 text-xs leading-5 text-slate-300">
          No map point selected.
        </div>

        <button id="community-submit" type="button" class="mt-3 w-full rounded-xl bg-amber-400 px-4 py-2.5 text-sm font-black text-amber-950 hover:bg-amber-300">
          Publish post
        </button>
        <p id="community-form-status" class="mt-2 min-h-[18px] text-xs text-slate-400"></p>
      </section>

      <div class="mt-4 flex items-center justify-between gap-2">
        <div>
          <h3 id="community-list-title" class="text-sm font-black text-white">Community Feed</h3>
          <p id="community-list-help" class="mt-1 text-[11px] text-slate-400">All public environmental posts.</p>
        </div>
        <div class="flex flex-wrap justify-end gap-2">
          <select id="community-all-filter" class="rounded-lg border border-white/10 bg-slate-950/70 px-2 py-1.5 text-[11px] text-white">
            <option value="">All categories</option>
            <option value="tree_suggestion">Tree suggestions</option>
            <option value="park_cleanup">Park cleanups</option>
            <option value="environmental_campaign">Campaigns</option>
            <option value="discussion">Discussions</option>
            <option value="heat_report">Heat reports</option>
            <option value="general">General posts</option>
          </select>
          <select id="community-sort" class="rounded-lg border border-white/10 bg-slate-950/70 px-2 py-1.5 text-[11px] text-white">
            <option value="newest">Newest</option>
            <option value="oldest">Oldest</option>
            <option value="active">Most active</option>
          </select>
          <button id="community-map-toggle" type="button" class="rounded-lg bg-emerald-400/15 px-2.5 py-1.5 text-[11px] font-bold text-emerald-100 ring-1 ring-emerald-300/20">Hide map points</button>
          <button id="community-refresh" type="button" class="rounded-lg bg-white/5 px-2.5 py-1.5 text-[11px] font-bold text-slate-200 ring-1 ring-white/10">Refresh</button>
        </div>
      </div>

      <div id="community-post-list" class="mt-3 space-y-3">
        <p class="text-xs text-slate-400">Loading community feed…</p>
      </div>

      <p class="mt-4 rounded-xl bg-slate-900/70 p-3 text-[11px] leading-5 text-slate-400 ring-1 ring-white/10">
        Use display names only. Do not post home addresses, phone numbers, passwords, or private information. Tree suggestions are community ideas, not city-approved planting sites.
      </p>
    </section>
  </aside>

  <section id="map-panel" class="relative min-w-0 flex-1"><div id="map"></div><button id="bike-legend-toggle" type="button"
        class="hidden glass absolute right-4 top-16 z-30 rounded-xl px-3 py-2 text-[10px] font-black text-cyan-100 ring-1 ring-cyan-300/20">
        Bike legend ▾
      </button>
      <div id="bike-infra-map-legend" class="hidden pointer-events-none absolute right-4 top-28 z-20 w-64 rounded-2xl bg-slate-950/90 p-3 text-xs text-white shadow-2xl ring-1 ring-white/15 backdrop-blur"><div class="flex items-center justify-between gap-2"><p class="font-black">Bike Infrastructure</p><span class="text-[9px] font-bold uppercase tracking-wider text-slate-400">EcoTrace / OSM</span></div><p class="mt-1 text-[9px] leading-4 text-slate-400">OSM-derived categories: purple can mean a painted lane OR a shared/low-speed route, so the colored share can be much larger than the protected/painted percentage.</p><div class="mt-3 space-y-2"><div class="flex items-center gap-2"><span class="block h-[4px] w-10 rounded" style="background:#4CAF72"></span><span>Protected / off-street path</span></div><div class="flex items-center gap-2"><span class="block h-[4px] w-10 rounded" style="background:#A663B6"></span><span>Painted on-street bike lane</span></div><div class="flex items-center gap-2"><span class="block w-10 border-t-4 border-dotted" style="border-color:#A663B6"></span><span>Bike route / sharrow / shared low-speed</span></div><div class="flex items-center gap-2"><span class="block h-[4px] w-10 rounded" style="background:#F59E0B"></span><span>Shared road</span></div><div class="flex items-center gap-2"><span class="block h-[4px] w-10 rounded" style="background:#94A3B8"></span><span>Unmatched / unknown</span></div></div><p class="mt-3 text-[9px] leading-4 text-amber-200">VTA street caution ratings are not shown unless EcoTrace has an authoritative caution-rating dataset.</p></div><div class="glass pointer-events-none absolute right-16 top-4 z-10 rounded-full px-3 py-2 text-[10px] font-black uppercase tracking-wider text-emerald-200">Build 6.14.9 FINAL-INPUT-POLISH • Port 8093</div><div id="status-pill" class="glass pointer-events-none absolute left-4 top-4 z-10 rounded-full px-4 py-2 text-xs font-semibold">Loading EcoTrace…</div><div class="absolute bottom-8 right-4 z-20 flex items-center gap-2">
  <select id="reset-view-mode"
    class="glass rounded-xl border border-white/10 px-2 py-2 text-[10px] font-bold text-white outline-none">
    <option value="stay">Reset • stay here</option>
    <option value="overview">Reset • zoom out</option>
  </select>
  <button id="reset-button"
    class="glass rounded-xl px-4 py-2 text-sm font-semibold text-white">
    Reset
  </button>
</div></section>
</main>

<div id="community-edit-modal" class="hidden fixed inset-0 z-[110] items-center justify-center bg-black/65 p-4 backdrop-blur-sm">
  <div class="w-full max-w-lg rounded-2xl bg-slate-950 p-4 shadow-2xl ring-1 ring-white/15">
    <div class="flex items-center justify-between gap-3">
      <div>
        <p class="text-[10px] font-black uppercase tracking-wider text-sky-200">Edit post</p>
        <h3 class="mt-1 font-black text-white">Update your post</h3>
      </div>
      <button id="community-edit-cancel-top" type="button" class="rounded-lg bg-white/5 px-2 py-1 text-xs font-bold text-slate-300 ring-1 ring-white/10">Cancel</button>
    </div>
    <label class="mt-3 block text-[10px] font-black uppercase tracking-wider text-slate-400">
      Title
      <input id="community-edit-title" maxlength="120" class="mt-1 w-full rounded-lg bg-black/20 px-3 py-2 text-sm text-white ring-1 ring-white/10" autocomplete="off">
    </label>
    <label class="mt-3 block text-[10px] font-black uppercase tracking-wider text-slate-400">
      Description
      <textarea id="community-edit-description" maxlength="2000" rows="6" class="mt-1 w-full resize-y rounded-lg bg-black/20 px-3 py-2 text-sm text-white ring-1 ring-white/10"></textarea>
    </label>
    <p id="community-edit-status" class="mt-2 text-[10px] text-slate-500"></p>
    <div class="mt-3 grid grid-cols-2 gap-2">
      <button id="community-edit-cancel" type="button" class="rounded-lg bg-white/5 px-3 py-2 text-xs font-black text-slate-200 ring-1 ring-white/10">Cancel</button>
      <button id="community-edit-save" type="button" class="rounded-lg bg-sky-400 px-3 py-2 text-xs font-black text-sky-950">Save changes</button>
    </div>
  </div>
</div>

<div id="switch-account-modal" class="hidden fixed inset-0 z-[120] items-center justify-center bg-black/70 p-4 backdrop-blur-sm">
  <div class="w-full max-w-md rounded-2xl bg-slate-950 p-4 shadow-2xl ring-1 ring-white/15">
    <div class="flex items-start justify-between gap-3">
      <div>
        <p class="text-[10px] font-black uppercase tracking-wider text-amber-200">EcoTrace account</p>
        <h3 class="mt-1 text-lg font-black text-white">Switch account</h3>
        <p class="mt-1 text-xs leading-5 text-slate-400">Your current account stays signed in unless the new sign-in succeeds.</p>
      </div>
      <button id="switch-account-cancel-top" type="button" class="rounded-lg bg-white/5 px-2 py-1 text-xs font-bold text-slate-300 ring-1 ring-white/10">Cancel</button>
    </div>
    <form action="/guest" method="post" class="mt-3">
      <button type="submit" class="w-full rounded-xl bg-slate-800 px-3 py-2.5 text-xs font-black text-slate-100 ring-1 ring-white/10 hover:bg-slate-700">Continue as Guest</button>
    </form>
    <div class="my-3 flex items-center gap-2"><div class="h-px flex-1 bg-white/10"></div><span class="text-[9px] font-black uppercase tracking-wider text-slate-500">or another account</span><div class="h-px flex-1 bg-white/10"></div></div>
    <input id="switch-account-username" class="w-full rounded-xl bg-black/20 px-3 py-2.5 text-sm text-white ring-1 ring-white/10" placeholder="Other account username" autocomplete="username">
    <input id="switch-account-password" type="password" class="mt-2 w-full rounded-xl bg-black/20 px-3 py-2.5 text-sm text-white ring-1 ring-white/10" placeholder="Password" autocomplete="current-password">
    <p id="switch-account-status" class="mt-2 min-h-[16px] text-[10px] text-slate-500"></p>
    <div class="mt-3 grid grid-cols-2 gap-2">
      <button id="switch-account-cancel" type="button" class="rounded-xl bg-white/5 px-3 py-2.5 text-xs font-black text-slate-200 ring-1 ring-white/10">Cancel</button>
      <button id="switch-account-submit" type="button" class="rounded-xl bg-amber-400 px-3 py-2.5 text-xs font-black text-amber-950">Switch account</button>
    </div>
  </div>
</div>

<div id="community-delete-modal" class="hidden fixed inset-0 z-[112] items-center justify-center bg-black/65 p-4 backdrop-blur-sm">
  <div class="w-full max-w-md rounded-2xl bg-slate-950 p-4 shadow-2xl ring-1 ring-white/15">
    <p class="text-[10px] font-black uppercase tracking-wider text-red-200">Delete post</p>
    <h3 class="mt-1 font-black text-white">Delete this post?</h3>
    <p class="mt-2 text-xs leading-5 text-slate-400">This permanently removes the post and its replies.</p>
    <p id="community-delete-title" class="mt-2 rounded-lg bg-white/5 p-2 text-xs font-bold text-slate-200"></p>
    <p id="community-delete-status" class="mt-2 text-[10px] text-slate-500"></p>
    <div class="mt-3 grid grid-cols-2 gap-2">
      <button id="community-delete-cancel" type="button" class="rounded-lg bg-white/5 px-3 py-2 text-xs font-black text-slate-200 ring-1 ring-white/10">Cancel</button>
      <button id="community-delete-confirm" type="button" class="rounded-lg bg-red-400 px-3 py-2 text-xs font-black text-red-950">OK</button>
    </div>
  </div>
</div>

<div id="community-report-modal" class="hidden fixed inset-0 z-[111] items-center justify-center bg-black/65 p-4 backdrop-blur-sm">
  <div class="w-full max-w-md rounded-2xl bg-slate-950 p-4 shadow-2xl ring-1 ring-white/15">
    <div class="flex items-center justify-between gap-3">
      <div>
        <p class="text-[10px] font-black uppercase tracking-wider text-red-200">Report post</p>
        <h3 class="mt-1 font-black text-white">Why are you reporting this?</h3>
      </div>
    </div>
    <select id="community-report-category" class="mt-3 w-full rounded-lg bg-black/20 px-3 py-2 text-xs text-white ring-1 ring-white/10">
      <option value="spam">Spam / irrelevant</option>
      <option value="misinformation">Misleading or incorrect information</option>
      <option value="harassment">Harassment / inappropriate content</option>
      <option value="privacy">Private or unsafe personal information</option>
      <option value="scam">Scam / impersonation / suspicious solicitation</option>
      <option value="unsafe">Dangerous or unsafe content</option>
      <option value="other">Other</option>
    </select>
    <textarea id="community-report-reason" maxlength="1000" rows="4" class="mt-2 w-full rounded-lg bg-black/20 px-3 py-2 text-sm text-white ring-1 ring-white/10" placeholder="Briefly explain the reason for the report"></textarea>
    <p id="community-report-status" class="mt-2 text-[10px] text-slate-500"></p>
    <div class="mt-3 grid grid-cols-2 gap-2">
      <button id="community-report-cancel" type="button" class="rounded-lg bg-white/5 px-3 py-2 text-xs font-black text-slate-200 ring-1 ring-white/10">Cancel</button>
      <button id="community-report-submit" type="button" class="rounded-lg bg-red-400 px-3 py-2 text-xs font-black text-red-950">Submit report</button>
    </div>
  </div>
</div>

<div id="impact-modal" class="hidden fixed inset-0 z-[101] items-center justify-center bg-black/65 p-4 backdrop-blur-sm">
  <div class="w-full max-w-lg rounded-2xl bg-slate-950 p-4 ring-1 ring-white/15">
    <div class="flex items-center justify-between"><h3 class="font-black text-emerald-100">Record completed impact</h3><button id="impact-modal-close" type="button" class="rounded-lg bg-white/5 px-2 py-1 text-xs text-slate-300">Close</button></div>
    <input id="impact-title" class="mt-3 w-full rounded-lg bg-black/20 px-3 py-2 text-xs text-white ring-1 ring-white/10" placeholder="Action/event title" autocomplete="off">
    <textarea id="impact-summary" rows="2" class="mt-2 w-full rounded-lg bg-black/20 px-3 py-2 text-xs text-white ring-1 ring-white/10" placeholder="What actually happened?"></textarea>
    <div class="mt-2 grid grid-cols-2 gap-2">
      <input id="impact-participants" type="number" min="0" class="rounded-lg bg-black/20 px-3 py-2 text-xs text-white ring-1 ring-white/10" placeholder="Participants" autocomplete="off">
      <input id="impact-hours" type="number" min="0" step="0.25" class="rounded-lg bg-black/20 px-3 py-2 text-xs text-white ring-1 ring-white/10" placeholder="Volunteer hours" autocomplete="off">
      <input id="impact-primary-value" type="number" min="0" class="rounded-lg bg-black/20 px-3 py-2 text-xs text-white ring-1 ring-white/10" placeholder="Primary impact count" autocomplete="off">
      <input id="impact-primary-label" class="rounded-lg bg-black/20 px-3 py-2 text-xs text-white ring-1 ring-white/10" placeholder="e.g. sites verified / devices collected" autocomplete="off">
    </div>
    <input id="impact-secondary" class="mt-2 w-full rounded-lg bg-black/20 px-3 py-2 text-xs text-white ring-1 ring-white/10" placeholder="Optional result note (e.g. 11 valid, 7 rejected)" autocomplete="off">
    <button id="impact-save" type="button" class="mt-3 w-full rounded-lg bg-emerald-400 px-3 py-2 text-xs font-black text-emerald-950">Save impact record</button>
  </div>
</div>

<div id="community-profile-modal" class="hidden fixed inset-0 z-[100] items-center justify-center bg-black/65 p-4 backdrop-blur-sm">
  <div class="max-h-[85vh] w-full max-w-lg overflow-y-auto rounded-2xl bg-slate-950 p-4 shadow-2xl ring-1 ring-white/15">
    <div class="flex items-start justify-between gap-3">
      <div>
        <p class="text-[10px] font-black uppercase tracking-wider text-cyan-200">EcoTrace profile</p>
        <h3 id="community-profile-name" class="mt-1 text-lg font-black text-white">Profile</h3>
      </div>
      <button id="community-profile-close" type="button" class="rounded-lg bg-white/5 px-2 py-1 text-xs font-bold text-slate-300 ring-1 ring-white/10">Close</button>
    </div>
    <div id="community-profile-body" class="mt-3 space-y-3 text-xs text-slate-300"></div>
    <p class="mt-3 rounded-lg bg-amber-400/10 p-2 text-[10px] leading-4 text-amber-100">
      Public EcoTrace profile: self-provided role, school/work/organization, education/work/title, bio, and opted-in event history may be shown. Private addresses, phone numbers, passwords, and exact age are not displayed.
    </p>
  </div>
</div>
<script>
window.ecoSessionState={account:null,ready:false,temporary:{},chatGeneration:0,nonce:crypto.randomUUID()};

let ecotraceStartupComplete=false;
window.addEventListener('error',event=>{
  const rawMessage=event?.error?.message||event?.message||'';
  const message=String(rawMessage).trim();

  // Browsers intentionally hide details from some cross-origin CDN/map
  // resources and report only "Script error.". That does not identify an
  // EcoTrace application failure, so do not replace the normal status pill
  // with a misleading red error banner for that generic browser message.
  const lowerMessage=message.toLowerCase();
  if(
    !message ||
    lowerMessage==='script error.' ||
    lowerMessage.includes("m_id")
  ){
    console.warn('Ignored transient browser/map-library error.',event);
    return;
  }

  const pill=document.getElementById('status-pill');
  if(pill){
    pill.textContent=ecotraceStartupComplete
      ?`EcoTrace interaction error: ${message}`
      :`EcoTrace startup error: ${message}`;
  }
  console.error(
    ecotraceStartupComplete?'EcoTrace interaction error:':'EcoTrace startup error:',
    event.error||event.message
  );
});

window.addEventListener('unhandledrejection',event=>{
  const reason=event?.reason;
  const message=String(reason?.message||reason||'').trim();
  if(!message)return;
  if(message.toLowerCase().includes('m_id')){
    console.warn('Ignored transient MapLibre M_ID rejection.',reason);
    event.preventDefault?.();
    return;
  }
  const pill=document.getElementById('status-pill');
  if(pill){
    pill.textContent=`EcoTrace request error: ${message}`;
  }
  console.error('EcoTrace unhandled promise rejection:',reason);
});
// Top status is neutral for loading/normal states and red only for errors.
const ecoTraceStatusPill=document.getElementById('status-pill');
function refreshEcoTraceStatusColor(){
  if(!ecoTraceStatusPill)return;
  const text=String(ecoTraceStatusPill.textContent||'').trim();
  const isError=/\b(error|failed|failure)\b/i.test(text);
  ecoTraceStatusPill.style.background=isError
    ?'rgba(127,29,29,.92)'
    :'rgba(15,23,42,.88)';
  ecoTraceStatusPill.style.color=isError?'#fee2e2':'#e2e8f0';
  ecoTraceStatusPill.style.borderColor=isError
    ?'rgba(248,113,113,.45)'
    :'rgba(148,163,184,.20)';
}
if(ecoTraceStatusPill){
  new MutationObserver(refreshEcoTraceStatusColor).observe(
    ecoTraceStatusPill,{childList:true,subtree:true,characterData:true}
  );
  refreshEcoTraceStatusColor();
}
const INITIAL={center:[-121.965,37.379],zoom:10.2};
const map=new maplibregl.Map({container:'map',style:'https://tiles.openfreemap.org/styles/positron',center:INITIAL.center,zoom:INITIAL.zoom,minZoom:9.5,maxZoom:17,maxBounds:[[-122.20,37.20],[-121.75,37.55]]});
map.addControl(new maplibregl.NavigationControl(),'top-right');
let mode='heat',treeMarkers=[],routePlantingMarkers=[],searchMarker=null,routeClicks=[],routeEndpointMarkers=[],bikeClicks=[],bikeEndpointMarkers=[];
let walkingTypedStartLock='';
let bikeTypedStartLock='';
document.addEventListener('input',event=>{
  if(event.target?.id==='route-start'){
    walkingTypedStartLock=String(event.target.value||'').trim();
  }
  if(event.target?.id==='bike-start'){
    bikeTypedStartLock=String(event.target.value||'').trim();
  }
});

window.latestEcoTraceRoutePlantingImpact=null;
window.latestBikeValidationContext=null;
window.latestCommunityContext=null;
window.latestCustomInputs={};
function refreshCustomInputs(){
  window.latestCustomInputs={
    walking_profile:document.getElementById('walking-profile')?.value==='other'?(document.getElementById('walking-profile-other')?.value.trim()||null):null,
    bike_type:document.getElementById('bike-type')?.value==='other'?(document.getElementById('bike-type-other')?.value.trim()||null):null,
    ewaste_condition:document.getElementById('ewaste-condition')?.value==='other'?(document.getElementById('ewaste-condition-other')?.value.trim()||null):null,
    community_role:document.getElementById('community-role')?.value==='other'?(document.getElementById('community-role-other')?.value.trim()||null):null
  };
  return window.latestCustomInputs;
}
window.latestGuideContext={
  sections:["How to Use","Data & Methods","About"],
  core_message:"EcoTrace separates mapped data, modeled calculations, and AI-assisted explanation, and the Data & Methods tab lists exact APIs/datasets used by each tool.",
  exact_sources:{
    heat:["NWS api.weather.gov","WRI/Meta canopy-height raster","Santa Clara County building footprints/heights","OpenStreetMap geometry"],
    walking:["OpenRouteService","OpenStreetMap","WRI/Meta canopy","County buildings","EcoTrace screened tree-site database"],
    biking:["OpenRouteService","OpenStreetMap bike tags","VTA 2023 ArcGIS bikeways GIS","WRI/Meta canopy","County buildings"],
    tree_planting:["ecotrace_reviewed_tree_sites.json","ArcGIS/OSM tree data","WRI/Meta canopy","County soils","County parcels","County buildings","OpenStreetMap roads/paths/obstacles"],
    community:["2024 ACS 5-year Census API","Census geocoder","EcoTrace SQLite community database"],
    ewaste:["EcoTrace event/archive data","Gemini item-age/category estimate"],
    assistant:["Google Gemini API","live EcoTrace app context"]
  },
  limitations:[
    "Planting opportunities require field/utility/ownership/permit verification.",
    "Parcel boundaries do not by themselves prove public ownership.",
    "Soil is planning context, not a field soil test.",
    "VTA is an official 2023 snapshot; OSM may contain newer community edits."
  ]
};
window.latestTreeGuideContext={
  curated_species_count:27,
  scope:"EcoTrace curated urban-tree recommendations",
  official_county_master_list:"Santa Clara County Tree Master Plant List (Non-Expressway)",
  official_county_master_list_url:"https://files.santaclaracounty.gov/exjcpb1476/migrated/Tree-Master-Plant-List-Non-Expwy.pdf?VersionId=O0hXC9WWC9irr.3bMYwGhqcFJZ78yTet",
  guidance:"EcoTrace's 27 species are a curated subset; use the County master list for the broader official reference."
};
let walkOperationGeneration=0;
let bikeOperationGeneration=0;
let walkingPointLookupInProgress=false;
let bikePointLookupInProgress=false;
let bikeQuickRouteMode=false;
let communityMapSelectionMode=false;
let communityContextSelectionMode=false;
let communityContextMarker=null;
let communitySelectedPoint=null;
let communitySelectionMarker=null;
let communityPointsVisible=true;
let latestCommunityPosts=[];
let selectedCommunityFile=null;
let selectedEwasteFile=null;
let selectedGeminiEwasteFile=null;
let latestGeminiEwasteResult=null;
let ecoTraceAssistantHistory=[];
let latestSelectedEwasteCenter=null;
let ewasteCenterSelectionMode=false;
let ewasteCenterSearchPoint=null;
let ewasteCenterPointsVisible=localStorage.getItem('ecotraceEwasteCentersVisible')!=='false';

let activeRequestControllers=new Set();
let currentEcoTraceProfile=null;
let lastResolvedLocation=null;

function profileStorageKey(suffix){
  const mode=new URLSearchParams(window.location.search).get('mode')||'guest';
  const profileLabel=document.getElementById('current-profile-label')?.textContent||'Guest';
  const safeProfile=profileLabel.replace(/^Signed in:\s*/,'').trim().toLowerCase().replace(/[^a-z0-9_-]+/g,'_')||'guest';
  return `ecotrace:${mode}:${safeProfile}:${suffix}`;
}

function getSavedLocations(){
  try{return JSON.parse(localStorage.getItem(profileStorageKey('savedLocations'))||'[]')}catch(_){return[]}
}
function setSavedLocations(items){
  localStorage.setItem(profileStorageKey('savedLocations'),JSON.stringify(items));
}
function renderSavedLocations(){
  const container=document.getElementById('saved-location-list');
  if(!container)return;
  const items=getSavedLocations();
  container.innerHTML=items.length?items.map((item,index)=>`
    <div class="flex items-center gap-2 rounded-lg bg-black/20 p-2">
      <button type="button" class="saved-place-use min-w-0 flex-1 truncate text-left text-xs font-bold text-amber-100" data-index="${index}">${escapeHtml(item.label)}</button>
      <button type="button" class="saved-place-delete text-xs text-red-300" data-index="${index}">Delete</button>
    </div>`).join(''):'<p class="text-xs text-slate-500">No saved locations for this profile.</p>';
  container.querySelectorAll('.saved-place-use').forEach(button=>button.addEventListener('click',()=>{
    const item=getSavedLocations()[Number(button.dataset.index)];
    if(!item)return;
    lastResolvedLocation=item;
    document.getElementById('search-input').value=item.label;
    map.flyTo({center:[item.lon,item.lat],zoom:15,essential:true});
    if(searchMarker)searchMarker.remove();
    searchMarker=new maplibregl.Marker({color:'#fbbf24'}).setLngLat([item.lon,item.lat]).addTo(map);
  }));
  container.querySelectorAll('.saved-place-delete').forEach(button=>button.addEventListener('click',()=>{
    const items=getSavedLocations();items.splice(Number(button.dataset.index),1);setSavedLocations(items);renderSavedLocations();
  }));
}
document.getElementById('save-current-location').addEventListener('click',()=>{
  const center=lastResolvedLocation||{lat:map.getCenter().lat,lon:map.getCenter().lng,label:'Saved map location'};
  const label=prompt('Name this saved location:',center.label||'Saved location');
  if(!label)return;
  const items=getSavedLocations();
  items.push({label:label.trim(),lat:Number(center.lat),lon:Number(center.lon)});
  setSavedLocations(items);renderSavedLocations();
  document.getElementById('search-status').textContent='Location saved for this browser profile.';
});
document.getElementById('status-pill').textContent='Loading map, search, and assistant…';

function safeDisplayName(value){return String(value||'').trim().slice(0,40);}
function escapeHtml(v){return String(v).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;').replaceAll("'",'&#039;')}
function friendlyApiError(message){
  const text=String(message||'');
  if(/display_name.*at most 40 characters/i.test(text)){
    return 'Name/display name is too long. EcoTrace limits public display names to 40 characters.';
  }
  return text;
}
async function readApiResponse(response){
  const text=await response.text();
  let data;
  try{data=text?JSON.parse(text):{}}catch(_){
    throw new Error(response.ok?'The server returned invalid data.':`Server error ${response.status}: ${text.slice(0,180)||'No response body'}`);
  }
  if(!response.ok){
    let detail=data.detail||data.message||`Request failed (${response.status})`;
    if(Array.isArray(detail)){
      detail=detail.map(item=>{
        const location=Array.isArray(item.loc)?item.loc.filter(part=>part!=='body').join(' → '):'field';
        return `${location}: ${item.msg||'Invalid value'}`;
      }).join('; ');
    }else if(typeof detail==='object'&&detail!==null){
      detail=JSON.stringify(detail);
    }
    throw new Error(friendlyApiError(detail));
  }
  return data;
}
async function fetchWithTimeout(url, options={}, timeoutMs=30000){
  const controller=new AbortController();
  activeRequestControllers.add(controller);
  const timer=setTimeout(()=>controller.abort(),timeoutMs);
  try{
    return await fetch(url,{...options,signal:controller.signal,cache:'no-store'});
  }catch(error){
    if(error.name==='AbortError')throw new Error('Request canceled or timed out. Your selected points were kept.');
    throw error;
  }finally{
    clearTimeout(timer);
    activeRequestControllers.delete(controller);
  }
}
document.getElementById('cancel-active-request').addEventListener('click',()=>{
  for(const controller of activeRequestControllers)controller.abort();
  activeRequestControllers.clear();
  walkOperationGeneration++;
  bikeOperationGeneration++;
  document.getElementById('search-status').textContent='Active request canceled.';
  document.getElementById('status-pill').textContent='Canceled — you can search or calculate again.';
});
async function geocodeAddress(query){
  const response=await fetchWithTimeout(`/api/geocode?q=${encodeURIComponent(query)}`,{},14000);
  return readApiResponse(response);
}

async function reverseGeocodePoint(point){
  const response=await fetchWithTimeout(
    `/api/reverse-geocode?lat=${encodeURIComponent(point.lat)}&lon=${encodeURIComponent(point.lon)}`,
    {},
    40000
  );
  return readApiResponse(response);
}

const suggestionCache=new Map();
async function loadGeocodeSuggestions(query,datalistId){
  const cleaned=query.trim();
  const datalist=document.getElementById(datalistId);
  if(!datalist)return;
  if(cleaned.length<2){datalist.innerHTML='';return}

  try{
    const cacheKey=cleaned.toLowerCase();
    let data=suggestionCache.get(cacheKey);
    if(!data){
      const response=await fetchWithTimeout(
        `/api/geocode-suggestions?q=${encodeURIComponent(cleaned)}&limit=6`,
        {},
        16000
      );
      data=await readApiResponse(response);
      suggestionCache.set(cacheKey,data);
    }

    datalist.innerHTML='';
    for(const result of data.results||[]){
      const option=document.createElement('option');
      option.value=result.display_name;
      option.label=`${result.display_name} — ${result.source}`;
      datalist.appendChild(option);
    }
  }catch(_){
    // Autocomplete is optional. The main search still works when suggestions fail.
    datalist.innerHTML='';
  }
}
function attachAddressAutocomplete(inputId,datalistId){
  const input=document.getElementById(inputId);
  const datalist=document.getElementById(datalistId);
  if(!input||!datalist)return;
  let timer=null;
  input.addEventListener('input',()=>{
    clearTimeout(timer);
    timer=setTimeout(
      ()=>loadGeocodeSuggestions(input.value,datalistId),
      380
    );
  });
}
attachAddressAutocomplete('search-input','search-suggestions');
attachAddressAutocomplete('route-start','route-start-suggestions');
attachAddressAutocomplete('route-end','route-end-suggestions');
attachAddressAutocomplete('bike-start','bike-start-suggestions');
attachAddressAutocomplete('bike-end','bike-end-suggestions');
attachAddressAutocomplete('community-context-address','community-context-suggestions');
function clearTreeMarkers(){treeMarkers.forEach(m=>m.remove());treeMarkers=[]}

function clearRoutePlantingMarkers(){
  routePlantingMarkers.forEach(marker=>marker.remove());
  routePlantingMarkers=[];
}
function makeRoutePlantingMarker(site){
  // Keep the visual point compact while making the hit target forgiving.
  const hit=document.createElement('div');
  hit.className='route-planting-marker-hit';
  hit.style.width='46px';
  hit.style.height='46px';
  hit.style.display='flex';
  hit.style.alignItems='center';
  hit.style.justifyContent='center';
  hit.style.cursor='pointer';

  const dot=document.createElement('div');
  dot.className='route-planting-marker';
  dot.style.width='18px';
  dot.style.height='18px';
  dot.style.borderRadius='9999px';
  dot.style.background=site.route_color||'#86efac';
  dot.style.border='3px solid #14532d';
  dot.style.boxShadow='0 0 0 5px rgba(134,239,172,.20)';
  dot.style.pointerEvents='none';
  hit.appendChild(dot);

  const where=site.where_location|| (site.area_name&& !String(site.area_name).toLowerCase().includes('unnamed')
    ?site.area_name
    :`${site.latitude.toFixed(5)}, ${site.longitude.toFixed(5)}`);

  const popupRoot=document.createElement('div');
  popupRoot.innerHTML=`
    <strong>Screened planting opportunity</strong><br>
    ${site.route_label?`<strong>Route:</strong> ${escapeHtml(site.route_label)}<br>`:''}
    ${escapeHtml(where)}<br>
    ${escapeHtml(String(site.distance_from_route_meters))} m from route<br>
    ${treeEvidenceHtml(site)}
  `;

  const openButton=document.createElement('button');
  openButton.type='button';
  openButton.textContent='Open in Tree Planting';
  openButton.style.cssText='margin-top:8px;width:100%;border:0;border-radius:8px;padding:7px 8px;background:#166534;color:white;font-weight:700;cursor:pointer;';
  openButton.addEventListener('click',async event=>{
    event.preventDefault();
    event.stopPropagation();
    showToolPanel('tree-panel',{preserveMap:true});
    mode='tree';

    const exactLat=Number(site.latitude);
    const exactLon=Number(site.longitude);
    map.flyTo({center:[exactLon,exactLat],zoom:19,essential:true,duration:700});
    document.getElementById('status-pill').textContent='Opening route candidate in Tree Planting…';

    try{
      await analyzePoint(exactLat,exactLon);
      map.easeTo({center:[exactLon,exactLat],zoom:19,duration:250,essential:true});
      document.getElementById('status-pill').textContent='Selected route planting candidate loaded';
    }catch(error){
      document.getElementById('status-pill').textContent=error.message||'Tree analysis failed.';
    }
  });
  popupRoot.appendChild(openButton);

  const popup=new maplibregl.Popup({offset:22,closeOnClick:false})
    .setDOMContent(popupRoot);

  const marker=new maplibregl.Marker({element:hit})
    .setLngLat([site.longitude,site.latitude])
    .setPopup(popup)
    .addTo(map);

  // Explicitly open the popup when the user clicks anywhere in the generous
  // hit area. This avoids relying on MapLibre's default tiny-marker behavior.
  hit.addEventListener('click',event=>{
    event.preventDefault();
    event.stopPropagation();
    if(!popup.isOpen())marker.togglePopup();
  });

  for(const eventName of ['mousedown','mouseup','touchstart','pointerdown']){
    hit.addEventListener(eventName,event=>event.stopPropagation());
  }

  return marker;
}
function estimateRouteTreeShadeImpact(sites){
  if(!Array.isArray(sites)||!sites.length)return null;
  const routeLength=Math.max(1,Number(sites[0]?.route_length_meters)||0);
  if(!routeLength)return null;

  // Mature-tree scenario only: roughly 8–20 m of effective route shade per
  // surviving mature tree. This is a transparent range, not a guarantee.
  const low=Math.min(25,(sites.length*8/routeLength)*100);
  const mid=Math.min(25,(sites.length*14/routeLength)*100);
  const high=Math.min(25,(sites.length*20/routeLength)*100);

  return {
    site_count:sites.length,
    route_length_meters:Math.round(routeLength),
    mature_shade_gain_low_points:Number(low.toFixed(1)),
    mature_shade_gain_estimate_points:Number(mid.toFixed(1)),
    mature_shade_gain_high_points:Number(high.toFixed(1)),
    assumption:'Modeled mature-tree scenario using about 8–20 m of effective route shade per planted site. Actual shade depends on species, survival, crown size, pruning, sun angle, and exact placement.'
  };
}

async function fetchPlantingSitesForRoute(route){
  const response=await fetchWithTimeout('/api/route-planting-sites',{
    method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({
      coordinates:route.coordinates,
      buffer_meters:75,
      max_sites:30
    })
  },20000);
  const sites=await readApiResponse(response);
  return sites.map(site=>({
    ...site,
    route_label:route.label,
    route_key:route.key,
    route_color:route.color
  }));
}

async function showPlantingSitesAlongRoutes(routes,button,statusElement){
  const validRoutes=(routes||[]).filter(route=>
    Array.isArray(route.coordinates)&&route.coordinates.length>=2
  );
  if(!validRoutes.length)return;

  clearRoutePlantingMarkers();
  window.latestEcoTraceRoutePlantingImpact=null;

  if(button.dataset.enabled==='true'){
    button.dataset.enabled='false';
    button.textContent='🌳 Show screened tree sites along both routes';
    if(statusElement)statusElement.textContent='';
    return;
  }

  button.disabled=true;
  button.textContent='Finding screened sites on both routes…';

  try{
    const results=await Promise.all(validRoutes.map(fetchPlantingSitesForRoute));

    // Deduplicate a candidate appearing close to both route alternatives.
    const unique=new Map();
    results.flat().forEach(site=>{
      const key=site.site_id || `${Number(site.latitude).toFixed(6)},${Number(site.longitude).toFixed(6)}`;
      if(!unique.has(key)){
        unique.set(key,site);
      }else{
        const existing=unique.get(key);
        existing.route_label=`${existing.route_label} + ${site.route_label}`;
        existing.route_key='both';
        existing.route_color='#fde047';
      }
    });

    const sites=[...unique.values()];
    sites.forEach(site=>routePlantingMarkers.push(makeRoutePlantingMarker(site)));

    // Showing route planting opportunities should visibly take the user to them.
    if(sites.length){
      const siteBounds=new maplibregl.LngLatBounds();
      sites.forEach(site=>siteBounds.extend([site.longitude,site.latitude]));
      map.fitBounds(siteBounds,{
        padding:{top:90,bottom:90,left:440,right:90},
        maxZoom:17,
        duration:650
      });
    }

    const byRoute={};
    validRoutes.forEach((route,index)=>{
      const routeSites=results[index];
      byRoute[route.key]={
        label:route.label,
        site_count:routeSites.length,
        shade_impact:estimateRouteTreeShadeImpact(routeSites)
      };
    });

    window.latestEcoTraceRoutePlantingImpact={
      total_unique_sites:sites.length,
      routes:byRoute,
      note:'Screened opportunities are shown for both route alternatives; duplicate sites near both routes are counted once on the map.'
    };

    button.dataset.enabled='true';
    button.textContent='🌳 Hide screened tree sites along both routes';

    if(statusElement){
      const routeSummary=validRoutes.map(route=>{
        const info=byRoute[route.key];
        const gain=info?.shade_impact?.mature_shade_gain_estimate_points;
        return `<strong>${escapeHtml(route.label)}:</strong> ${info?.site_count||0} screened opportunities${gain!=null?` • modeled mature shade about +${gain} points`:''}`;
      }).join('<br>');

      statusElement.innerHTML=sites.length
        ?`${sites.length} unique screened planting opportunities are shown across both routes.<br>${routeSummary}<br><span class="text-slate-500">Yellow markers are candidates close to both alternatives. These are screened opportunities, not approved planting commitments.</span><br><button type="button" class="route-sites-open-tree mt-2 rounded-lg bg-emerald-400/15 px-2.5 py-1.5 text-[10px] font-black text-emerald-100 ring-1 ring-emerald-300/20">View these sites in Tree Planting</button>`
        :'No screened candidates were found within 75 m of either route.';

      const openAllButton=statusElement.querySelector('.route-sites-open-tree');
      if(openAllButton){
        openAllButton.addEventListener('click',()=>{
          showToolPanel('tree-panel',{preserveMap:true});
          mode='tree';
          if(sites.length){
            const bounds=new maplibregl.LngLatBounds();
            sites.forEach(site=>bounds.extend([site.longitude,site.latitude]));
            map.fitBounds(bounds,{padding:{top:80,bottom:80,left:440,right:80},maxZoom:17,duration:700});
          }
          document.getElementById('status-pill').textContent=
            `${sites.length} screened planting opportunities from both routes shown`;
        });
      }
    }
  }catch(error){
    if(statusElement)statusElement.textContent=error.message||'Could not load route planting sites.';
    button.dataset.enabled='false';
    button.textContent='🌳 Show screened tree sites along both routes';
  }finally{
    button.disabled=false;
  }
}

function clearRoute(){clearRoutePlantingMarkers();walkOperationGeneration++;routeClicks=[];routeEndpointMarkers.forEach(m=>m.remove());routeEndpointMarkers=[];document.getElementById('route-comparison')?.classList.add('hidden');if(map.getSource('shortest-route'))map.getSource('shortest-route').setData({type:'FeatureCollection',features:[]});if(map.getSource('shadiest-route'))map.getSource('shadiest-route').setData({type:'FeatureCollection',features:[]});if(map.getSource('exposure-segments'))map.getSource('exposure-segments').setData({type:'FeatureCollection',features:[]})}
function addRouteEndpoint(point,color,label){
  const marker=new maplibregl.Marker({color}).setLngLat([point.lon,point.lat]).setPopup(new maplibregl.Popup({offset:16}).setText(label)).addTo(map);
  routeEndpointMarkers.push(marker);
}
function clearBike(){
  clearRoutePlantingMarkers();
  bikeOperationGeneration++;
  bikeClicks=[];
  bikeEndpointMarkers.forEach(marker=>marker.remove());
  bikeEndpointMarkers=[];

  document.getElementById('bike-comparison')?.classList.add('hidden');

  // Reset existing sources only. Never recreate sources/layers during Reset.
  for(const sourceId of [
    'bike-fastest',
    'bike-cooler',
    'bike-infrastructure-segments',
    'bike-fastest-infra-color',
    'bike-cooler-infra-color'
  ]){
    const source=map.getSource(sourceId);
    if(source&&typeof source.setData==='function'){
      source.setData({type:'FeatureCollection',features:[]});
    }
  }

  for(const layerId of [
    'bike-infrastructure-segments-line',
    'bike-fastest-infra-color-line',
    'bike-cooler-infra-color-line'
  ]){
    if(map.getLayer(layerId)){
      map.setLayoutProperty(layerId,'visibility','none');
    }
  }

  window.latestBikeValidationContext=null;
  document.getElementById('bike-infra-map-legend')?.classList.add('hidden');

  const legendToggle=document.getElementById('bike-legend-toggle');
  if(legendToggle){
    legendToggle.textContent='Bike legend ▾';
  }
}

function addBikeEndpoint(point,color,label){
  const marker=new maplibregl.Marker({color})
    .setLngLat([point.lon,point.lat])
    .setPopup(new maplibregl.Popup({offset:16}).setText(label))
    .addTo(map);
  bikeEndpointMarkers.push(marker);
}
let activeToolPanel='heat-panel';
let panelBeforeAssistant=null;
function clearCommunityPlanningContext(){
  window.latestCommunityContext=null;
  for(const id of ['community-context-result','community-context-output']){
    const el=document.getElementById(id);
    if(el){
      el.innerHTML='';
      el.classList.add('hidden');
    }
  }
}


function closeGuideDetails(){
  document.querySelectorAll('#guide-panel details').forEach(details=>details.open=false);
}

function showToolPanel(panelId,options={}){


  const bikeLegend=document.getElementById('bike-infra-map-legend');
  const bikeLegendToggle=document.getElementById('bike-legend-toggle');
  if(panelId==='bike-panel'){
    bikeLegendToggle?.classList.remove('hidden');
    const hiddenByUser=localStorage.getItem('ecotraceBikeLegendHidden')==='1';
    bikeLegend?.classList.toggle('hidden',hiddenByUser);
    if(bikeLegendToggle)bikeLegendToggle.textContent=hiddenByUser?'Bike legend ▾':'Bike legend ▴';
  }else{
    bikeLegendToggle?.classList.add('hidden');
    bikeLegend?.classList.add('hidden');
  }

  const previousPanel=activeToolPanel;
  if(panelId==='ewaste-panel' && previousPanel!=='ewaste-panel'){
    resetEwasteRecommendationForm();
    setEwasteTab('recommend');
  }
  if(previousPanel==='community-panel' && panelId!=='community-panel'){
    walkingTypedStartLock='';
    clearCommunityPlanningContext();
  }
  if(panelId==='assistant-tool-panel'&&previousPanel!=='assistant-tool-panel'){
    panelBeforeAssistant=previousPanel;
  }
  if(previousPanel==='assistant-tool-panel'&&panelId!=='assistant-tool-panel'){
    clearAssistantComposer();
  }
  activeToolPanel=panelId;
  document.querySelectorAll('.tool-panel').forEach(panel=>{
    panel.classList.toggle('active',panel.id===panelId);
  });
function setGuideTab(name){
  for(const panel of document.querySelectorAll('.guide-subpanel')){
    panel.classList.toggle('hidden',panel.id!==`guide-${name}`);
  }
  for(const button of document.querySelectorAll('.guide-tab')){
    const active=button.dataset.guideTab===name;
    button.classList.toggle('bg-sky-300/20',active);
    button.classList.toggle('text-sky-100',active);
    button.classList.toggle('text-slate-300',!active);
  }
}
document.querySelectorAll('.guide-tab').forEach(button=>{
  button.addEventListener('click',()=>setGuideTab(button.dataset.guideTab));
});
setGuideTab('use');

document.querySelectorAll('[data-panel-target]').forEach(button=>{
    button.classList.toggle('active',button.dataset.panelTarget===panelId);
  });

  if(panelId==='guide-panel')closeGuideDetails();

  const assistantTransition=(
    panelId==='assistant-tool-panel' ||
    (previousPanel==='assistant-tool-panel'&&panelId===panelBeforeAssistant)
  );
  if(!assistantTransition && !options.preserveMap){
    clearRoute();
    clearBike();
  }

  if(panelId==='walk-panel'){
    mode='route';
    document.getElementById('status-pill').textContent='Walking mode — enter addresses or click two map points';
  }else if(panelId==='bike-panel'){
    mode='bike';
    document.getElementById('status-pill').textContent='Bike mode — enter addresses or click two map points';
  }else if(panelId==='tree-panel'){
    mode='tree';
    document.getElementById('status-pill').textContent='Tree mode — click or search a location';
  }else if(panelId==='heat-panel'){
    mode='heat';
    document.getElementById('status-pill').textContent='Heat map mode — click or search a location';
  }else{
    mode='none';
    if(panelId==='assistant-tool-panel'){
      document.getElementById('status-pill').textContent=
        'Assistant open — current route and overlays remain visible';
    }
  }

  if(panelId==='ewaste-panel'){
    setEwasteTab(activeEwasteTab);
  }
  if(panelId==='tree-guide-panel'){
    loadTreeGuide();
  }
  if(panelId==='community-panel'){
    loadCommunityPosts();
  }
}

document.querySelectorAll('[data-panel-target]').forEach(button=>{
  button.addEventListener('click',()=>showToolPanel(button.dataset.panelTarget));
});
showToolPanel('heat-panel');

async function analyzeHeatPoint(lat,lon){
  clearTreeMarkers();
  document.getElementById('status-pill').textContent='Analyzing local heat and current weather…';

  const [zoneResult,weatherResult]=await Promise.allSettled([
    fetch(`/api/analyze-zone?lat=${encodeURIComponent(lat)}&lon=${encodeURIComponent(lon)}`).then(readApiResponse),
    fetch(`/api/weather?lat=${encodeURIComponent(lat)}&lon=${encodeURIComponent(lon)}`).then(readApiResponse)
  ]);

  if(zoneResult.status==='rejected')throw zoneResult.reason;
  const data=zoneResult.value;
  const weather=weatherResult.status==='fulfilled'?weatherResult.value:null;

  window.latestEcoTraceAnalysis={...data,weather};
  const liveTemp=(weather&&weather.available&&weather.temperature_f!=null)
    ?weather.temperature_f
    :null;

  document.getElementById('status-pill').textContent=liveTemp!=null
    ?`Modeled heat ${data.heat_index}/100 • NWS ${liveTemp}°F`
    :`Modeled heat ${data.heat_index}/100`;

  const weatherHtml=(weather&&weather.available)
    ?`<div class="mt-3 rounded-xl bg-sky-400/10 p-3 ring-1 ring-sky-300/20">
        <p class="text-[10px] font-black uppercase tracking-wider text-sky-200">Latest NWS weather</p>
        <div class="mt-2 grid grid-cols-2 gap-2 text-xs">
          <div><span class="text-slate-400">Observed temp.</span><br><strong>${weather.temperature_f??'N/A'}°F</strong></div>
          <div><span class="text-slate-400">Feels like</span><br><strong>${weather.apparent_temperature_f??weather.temperature_f??'N/A'}°F</strong></div>
          <div><span class="text-slate-400">Wind</span><br><strong>${weather.wind_speed_mph==null?'N/A':weather.wind_speed_mph+' mph'}</strong></div>
          <div><span class="text-slate-400">Rain chance</span><br><strong>${weather.precipitation_probability==null?'N/A':weather.precipitation_probability+'%'}</strong></div>
        </div>
        <p class="mt-2 text-[9px] leading-4 text-slate-400">
          ${escapeHtml(weather.observation_station||'Nearby NWS station')}
          ${weather.observed_at?` • observation ${escapeHtml(weather.observed_at)}`:''}
        </p>
      </div>`
    :`<p class="mt-3 rounded-xl bg-amber-400/10 p-2 text-[10px] leading-4 text-amber-200">
        Live NWS observation unavailable; modeled heat remains available.
      </p>`;

  document.getElementById('analysis-card').innerHTML=`
    <div class="flex items-start justify-between gap-2">
      <div><p class="text-[11px] uppercase tracking-wider text-slate-400">Localized heat analysis</p><h2 class="mt-1 text-lg font-black text-white">${escapeHtml(data.zone_name)}</h2></div>
      <span class="rounded-full bg-orange-400/15 px-2.5 py-1 text-[11px] font-bold text-orange-300 ring-1 ring-orange-300/25">${escapeHtml(data.priority)}</span>
    </div>
    <div class="mt-4 grid grid-cols-2 gap-2">
      <div class="rounded-xl bg-white/5 p-3"><p class="text-[10px] uppercase text-slate-400">Modeled heat exposure</p><p class="text-2xl font-black">${data.heat_index}<span class="text-sm text-slate-400">/100</span></p></div>
      <div class="rounded-xl bg-white/5 p-3"><p class="text-[10px] uppercase text-slate-400">Model baseline temp.</p><p class="text-2xl font-black">${data.estimated_temperature_f}<span class="text-sm text-slate-400">°F</span></p></div>
    </div>
    ${weatherHtml}
    <p class="mt-3 text-sm leading-6 text-slate-300">${escapeHtml(data.message)}</p>
    <div class="mt-3 rounded-xl bg-amber-300/5 p-3 ring-1 ring-amber-300/15">
      <p class="text-[10px] font-black uppercase tracking-wider text-amber-200">How shade is estimated</p>
      <p class="mt-1 text-[10px] leading-4 text-slate-300">EcoTrace models direct solar blocking from WRI/Meta canopy height and Santa Clara County building geometry/heights at the selected date/time. Higher shade score means more modeled direct shade along the route. Lux, reflected light, surface albedo, diffuse sky radiation, terrain, and species-specific leaf density are not currently modeled.</p>
    </div>
    <p class="mt-2 text-[10px] leading-4 text-slate-500">The 0–100 heat score is a spatial EcoTrace exposure model; the NWS box is the latest nearby observed weather and is kept separate rather than pretending the modeled score is a live thermometer.</p>
    <p class="mt-3 text-xs leading-5 text-sky-200">Tree recommendations appear only in the Tree Planting panel.</p>`;
}

async function analyzePoint(lat,lon){
  clearTreeMarkers();
  document.getElementById('status-pill').textContent='Analyzing heat and nearby planting opportunities…';

  const [heatResponse, siteResponse] = await Promise.all([
    fetch(`/api/analyze-zone?lat=${encodeURIComponent(lat)}&lon=${encodeURIComponent(lon)}`),
    fetch('/api/proposed-sites',{
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({latitude:lat,longitude:lon})
    })
  ]);

  const heatData=await readApiResponse(heatResponse);
  const proposedSites=await readApiResponse(siteResponse);

  window.latestEcoTraceAnalysis={
    ...heatData,
    proposed_sites:proposedSites
  };

  await renderAnalysis(heatData,proposedSites);
}


function treeEvidenceHtml(site){
  const heat=site.surface_heat;
  const distance=site.mapped_building_clearance_m;
  const parts=[];
  if(distance!=null)parts.push(`Mapped building: ${Number(distance).toFixed(1)} m away`);
  if(heat&&Number.isFinite(heat.surface_temperature_f))parts.push(`Satellite surface temperature: ${heat.surface_temperature_f.toFixed(1)}°F`);
  const link='/tree-review?site='+encodeURIComponent(site.candidate_id||site.site_id||'');
  return `<details class="mt-2 text-xs leading-5" style="margin-top:8px;font-size:12px;line-height:1.5"><summary class="cursor-pointer font-bold">Site details</summary>${parts.map(p=>`<p>${escapeHtml(p)}</p>`).join('')}<a class="underline" href="${link}" target="_blank" rel="noopener">View location evidence ↗</a></details>`;
}
function applyPostPermissions(){
  const signed=Boolean(currentEcoTraceAccount);
  const ids=['community-create-toggle','community-submit','ewaste-listing-submit','ewaste-event-submit'];
  for(const id of ids){const e=document.getElementById(id);if(!e)continue;e.disabled=!signed;if(!signed)e.title='Sign in to post';else e.removeAttribute('title');}
  const create=document.getElementById('community-create-card');if(create&&!signed)create.classList.add('hidden');
  document.querySelectorAll('.ewaste-reply-submit,.community-join,.community-leave,.ewaste-event-join').forEach(e=>{e.disabled=!signed;e.title=signed?'':'Sign in to continue';});
  document.querySelectorAll('.community-edit-post,.community-delete-post,.community-edit-comment,.community-delete-comment,.community-complete-event,.ewaste-event-done,.ewaste-owner-action').forEach(e=>{if(!signed)e.hidden=true;});
  document.querySelectorAll('.community-comment-submit').forEach(e=>{const p=latestCommunityPosts.find(p=>String(p.id)===String(e.dataset.postId));e.disabled=!signed&&!['discussion'].includes(p?.category);e.title=e.disabled?'Sign in to reply here':'';});
  for(const id of ['community-create-toggle','ewaste-listing-submit']){const e=document.getElementById(id);if(e&&!e.nextElementSibling?.classList.contains('post-signin-note')){const n=document.createElement('p');n.className='post-signin-note text-xs text-slate-400 my-2';n.textContent='Sign in to create posts. Guests can report posts and reply in discussions.';e.after(n);}}
  document.querySelectorAll('.post-signin-note').forEach(e=>e.hidden=signed);
}
function attachEwasteOwnerActions(list,items){
  for(const item of items){
    const card=Array.from(list.querySelectorAll('article')).find(c=>c.querySelector('.ewaste-reply-submit')?.dataset.listingId===String(item.id));if(!card)continue;
    const row=document.createElement('div');row.className='mt-3 flex gap-3 text-xs';
    function button(label,action){const b=document.createElement('button');b.type='button';b.className=label==='Report'?'underline':'underline ewaste-owner-action';b.textContent=label;b.onclick=action;row.append(b);}
    if(item.can_edit){
      button('Edit',()=>{
        const dialog=document.createElement('dialog');dialog.className='rounded-xl bg-slate-900 text-white p-5 w-96';
        dialog.innerHTML='<form class="space-y-3"><h3 class="font-bold">Edit your listing</h3><input name="title" required minlength="3" maxlength="120" class="w-full bg-slate-800 p-2 rounded" aria-label="Title" autocomplete="off"><textarea name="description" required minlength="3" maxlength="1500" class="w-full bg-slate-800 p-2 rounded" aria-label="Description"></textarea><input name="price" type="number" min="0" max="100000" step="0.01" class="w-full bg-slate-800 p-2 rounded" aria-label="Price" autocomplete="off"><p class="error text-red-300 text-xs"></p><button type="submit" class="bg-lime-400 text-black rounded px-3 py-2">Save</button> <button type="button" class="cancel px-3 py-2">Cancel</button></form>';
        const form=dialog.querySelector('form');form.elements.title.value=item.title;form.elements.description.value=item.description;form.elements.price.value=item.asking_price??'';
        dialog.querySelector('.cancel').onclick=()=>dialog.close();dialog.onclose=()=>dialog.remove();form.onsubmit=async e=>{e.preventDefault();try{await readApiResponse(await fetchWithTimeout('/api/ewaste/listings/'+item.id,{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({title:form.elements.title.value,description:form.elements.description.value,asking_price:form.elements.price.value===''?null:Number(form.elements.price.value)})},12000));dialog.close();loadEwasteListings();}catch(error){dialog.querySelector('.error').textContent=error.message;}};document.body.append(dialog);dialog.showModal();
      });
      button('Delete',async()=>{if(!confirm('Delete your listing and its replies?'))return;try{await readApiResponse(await fetchWithTimeout('/api/ewaste/listings/'+item.id,{method:'DELETE'},12000));loadEwasteListings();}catch(e){alert(e.message);}});
    }
    button('Report',async()=>{const reason=prompt('Why are you reporting this listing?');if(!reason)return;try{await readApiResponse(await fetchWithTimeout('/api/ewaste/listings/'+item.id+'/report',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({reason})},12000));alert('Report submitted for review.');}catch(e){alert(e.message);}});
    card.append(row);
  }
  applyPostPermissions();
}

function plantingLocationNeedsRepair(value){
  const text=String(value||'').trim().toLowerCase();
  return !text
    || text.includes('unnamed')
    || text.includes('mapped street')
    || text==='unknown'
    || text.includes('screened mapped open-space edge');
}

async function repairPlantingLocation(spot){
  if(spot.location_source)return spot;
  if(!plantingLocationNeedsRepair(spot.where_location)){
    return spot;
  }

  try{
    const latitude=Number(spot.latitude);
    const longitude=Number(spot.longitude);
    const result=await reverseGeocodePoint({lat:latitude,lon:longitude});
    const repaired=String(result?.display_name||'').trim();

    if(repaired && !plantingLocationNeedsRepair(repaired)){
      spot.where_location=repaired;
      return spot;
    }

    // Even a coordinate-preserving fallback is cleaner than "Unnamed mapped street".
    if(repaired){
      spot.where_location=repaired;
      return spot;
    }
  }catch(_){}

  spot.where_location=`Near ${Number(spot.latitude).toFixed(5)}, ${Number(spot.longitude).toFixed(5)} in Santa Clara County`;
  return spot;
}


const TREE_QA_EXCLUSIONS=[
  {lat:37.2947222,lon:-121.8525278,radiusM:18,note:'street intersection'}
];
function treeQaDistanceMeters(lat1,lon1,lat2,lon2){
  const r=6371008.8,p1=lat1*Math.PI/180,p2=lat2*Math.PI/180;
  const dp=(lat2-lat1)*Math.PI/180,dl=(lon2-lon1)*Math.PI/180;
  const a=Math.sin(dp/2)**2+Math.cos(p1)*Math.cos(p2)*Math.sin(dl/2)**2;
  return 2*r*Math.atan2(Math.sqrt(a),Math.sqrt(Math.max(0,1-a)));
}
function passesTreeQaScreen(spot){
  const lat=Number(spot.latitude),lon=Number(spot.longitude);
  return Number.isFinite(lat)&&Number.isFinite(lon)&&!TREE_QA_EXCLUSIONS.some(item=>
    treeQaDistanceMeters(lat,lon,item.lat,item.lon)<=item.radiusM
  );
}

async function repairPlantingLocations(spots){
  const incoming=spots||[];
  const screened=incoming.filter(passesTreeQaScreen);
  return Promise.all(screened.map(spot=>repairPlantingLocation({...spot})));
}

async function renderAnalysis(data,proposedSites){
  proposedSites=await repairPlantingLocations(proposedSites);

  document.getElementById('status-pill').textContent=
    proposedSites.length && !proposedSites[0].within_preferred_radius
      ?`Heat ${data.heat_index}/100 • nearest screened fallback shown`
      :`Heat ${data.heat_index}/100 • ${proposedSites.length} nearest screened sites within 800 m`;

  document.getElementById('analysis-card').innerHTML=`
    <div class="flex items-start justify-between gap-2">
      <div>
        <p class="text-[11px] uppercase tracking-wider text-slate-400">Localized analysis</p>
        <h2 class="mt-1 text-lg font-black text-white">${escapeHtml(data.zone_name)}</h2>
      </div>
      <span class="rounded-full bg-orange-400/15 px-2.5 py-1 text-[11px] font-bold text-orange-300 ring-1 ring-orange-300/25">
        Heat context
      </span>
    </div>
    <div class="mt-4 grid grid-cols-2 gap-2">
      <div class="rounded-xl bg-white/5 p-3">
        <p class="text-[10px] uppercase text-slate-400">Heat index</p>
        <p class="text-2xl font-black">${data.heat_index}<span class="text-sm text-slate-400">/100</span></p>
      </div>
      <div class="rounded-xl bg-white/5 p-3">
        <p class="text-[10px] uppercase text-slate-400">Displayed sites</p>
        <p class="text-2xl font-black">${proposedSites.length}</p>
      </div>
    </div>
    <p class="mt-3 text-sm leading-6 text-slate-300">${escapeHtml(data.message)}</p>
    <p class="mt-3 text-xs leading-5 text-amber-200">
      These are screened potential planting opportunities, not legally approved sites.
      City and field verification are still required.
    </p>`;

  const section=document.getElementById('recommendations');
  const list=document.getElementById('recommendation-list');
  list.innerHTML='';
  clearTreeMarkers();

  if(!proposedSites.length){
    section.classList.remove('hidden');
    list.innerHTML=`
      <article class="rounded-2xl bg-amber-400/10 p-3 ring-1 ring-amber-300/20">
        <p class="text-sm font-bold text-amber-200">No screened site within 800 meters</p>
        <p class="mt-2 text-xs leading-5 text-slate-300">
          Try another nearby location or inspect the generated database coverage.
        </p>
      </article>`;
    return;
  }

  section.classList.remove('hidden');

  // Explain rules once instead of repeating the same disclaimer on every site.
  const existingGuide=document.getElementById('planting-site-guide');
  if(existingGuide)existingGuide.remove();

  const guide=document.createElement('details');
  guide.id='planting-site-guide';
  guide.className='mb-3 rounded-2xl bg-sky-400/10 p-3 ring-1 ring-sky-300/20';
  guide.innerHTML=`
    <summary class="cursor-pointer text-sm font-black text-sky-100">
      How to interpret EcoTrace planting sites
    </summary>
    <div class="mt-3 space-y-3 text-xs leading-5 text-slate-300">
      <div>
        <p class="font-bold text-white">What “screened” means</p>
        <p>
          EcoTrace uses mapped road, path, obstacle, canopy, soil, parcel, and other
          environmental evidence to identify possible planting locations. A screened
          point is a planning opportunity, not an approved planting permit.
        </p>
      </div>
      <div>
        <p class="font-bold text-white">What still needs verification</p>
        <p>
          Owner/city permission (including unfenced lawns), underground and overhead utilities, irrigation,
          ADA clearance, exact field conditions, permits, and final species choice
          must still be checked before planting.
        </p>
      </div>
      <div>
        <p class="font-bold text-white">How suitable trees are chosen</p>
        <p>
          The Tree Library uses estimated available site size as an initial filter.
          Soil, canopy, utilities, and local conditions provide context, but EcoTrace
          does not claim that one exact species is automatically approved for a coordinate.
        </p>
      </div>
    </div>`;
  list.parentElement.insertBefore(guide,list);

  // The backend returns the closest three inside 800 m, or one nearest
  // fallback outside the preferred range.
  const visibleSites=[...proposedSites];

  const speciesButton=document.createElement('button');
  speciesButton.type='button';
  speciesButton.className='w-full rounded-xl bg-green-400 px-3 py-2 text-xs font-black text-green-950 mb-3';
  speciesButton.textContent='Browse tree species →';
  speciesButton.onclick=()=>openSuitableTrees('');
  list.appendChild(speciesButton);

  visibleSites.forEach((spot,index)=>{
    const latitude=Number(spot.latitude);
    const longitude=Number(spot.longitude);
    const coordinates=`${latitude.toFixed(7)}, ${longitude.toFixed(7)}`;

    const preferred=Boolean(spot.within_preferred_radius);
    const card=document.createElement('article');
    card.className=preferred
      ?'cursor-pointer rounded-2xl bg-emerald-400/10 p-3 ring-1 ring-emerald-300/20'
      :'cursor-pointer rounded-2xl bg-orange-400/10 p-3 ring-1 ring-orange-300/25';
    const siteSpecificFacts=[];
    if(spot.wri_canopy_max_height_m!=null){
      siteSpecificFacts.push(
        `<span><strong>Nearby canopy height:</strong> ${escapeHtml(String(spot.wri_canopy_max_height_m))} m</span>`
      );
    }
    if(spot.county_canopy_percent!=null){
      siteSpecificFacts.push(
        `<span><strong>Area canopy context:</strong> ${escapeHtml(String(spot.county_canopy_percent))}%</span>`
      );
    }
    if(spot.soil_assessment && !String(spot.soil_assessment).toLowerCase().includes('not available')){
      siteSpecificFacts.push(
        `<span><strong>Soil:</strong> ${escapeHtml(spot.soil_assessment)}</span>`
      );
    }

    card.innerHTML=`
      <div class="flex items-center justify-between gap-2">
        <p class="text-sm font-bold ${preferred?'text-emerald-200':'text-orange-200'}">
          ${preferred?`Potential site ${index+1}`:'Nearest screened fallback'}
        </p>
        <span class="rounded-full px-2 py-1 text-[10px] font-bold ${preferred?'bg-emerald-300/15 text-emerald-200':'bg-orange-300/15 text-orange-200'}">
          ${Math.round(spot.distance_from_click_meters)} m away
        </span>
      </div>

      ${preferred?'':`
        <p class="mt-2 rounded-lg bg-orange-300/10 p-2 text-[11px] leading-5 text-orange-100">
          No candidate exists within 800 m, so EcoTrace is showing the nearest screened site.
        </p>
      `}

      <p class="mt-3 text-sm leading-5 text-white">
        <strong>Where:</strong> ${escapeHtml(spot.where_location||'Santa Clara County')}
      </p>

      <p class="mt-2 font-mono text-[11px] text-sky-200">
        ${coordinates}
      </p>

      ${treeEvidenceHtml(spot)}
      `;

    card.onclick=event=>{if(event.target.closest('a,button,details'))return;map.flyTo({
      center:[longitude,latitude],
      zoom:17,
      essential:true
    });
    };
    list.appendChild(card);

    const el=document.createElement('div');
    el.className=preferred?'eco-marker':'fallback-marker';
    el.setAttribute('role','button');
    el.setAttribute('aria-label',preferred?`Open potential planting site ${index+1}`:'Open nearest screened fallback');

    // A MapLibre marker sits above the map canvas. Without stopping propagation,
    // clicking it also fires map.on('click'), which starts a new analysis and
    // removes the marker before its popup can open.
    for(const eventName of ['click','mousedown','mouseup','touchstart','pointerdown']){
      el.addEventListener(eventName,event=>{
        event.stopPropagation();
      });
    }

    const popupHtml=`
      <strong>${preferred?`Potential planting site ${index+1}`:'Nearest screened fallback'}</strong><br>
      <strong>Where:</strong> ${escapeHtml(spot.where_location||'Santa Clara County')}<br>
      <strong>Coordinates:</strong> ${coordinates}<br>
      ${treeEvidenceHtml(spot)}`;

    const popup=new maplibregl.Popup({
      offset:18,
      closeButton:true,
      closeOnClick:false
    }).setHTML(popupHtml);

    popup.on('open',()=>{
      const popupElement=popup.getElement();
      if(!popupElement)return;
      for(const eventName of ['click','mousedown','mouseup','touchstart','pointerdown']){
        popupElement.addEventListener(eventName,event=>event.stopPropagation());
      }
    });

    treeMarkers.push(
      new maplibregl.Marker({element:el})
        .setLngLat([longitude,latitude])
        .setPopup(popup)
        .addTo(map)
    );
  });
}

function selectedWalkingPace(){
  const selection=document.getElementById('walking-pace').value;
  if(selection==='custom')return Math.max(0.5,Math.min(15,Number(document.getElementById('custom-pace').value)||3));
  return Number(selection)||3;
}
const WALKING_PROFILES={
  child:{
    label:'Child',
    pace:2.5,
    detour:0.25,
    help:'Prioritizes shade and allows up to 25% extra time. Adult supervision and safe crossings are still required.'
  },
  adult:{
    label:'Adult',
    pace:3.0,
    detour:0.20,
    help:'Balanced pace, shade priority, and a maximum 20% extra-time detour.'
  },
  runner:{
    label:'Runner',
    pace:6.0,
    detour:0.10,
    help:'Uses running speed and limits the cooler-route detour to 10% to avoid excessive added exertion.'
  },
  older_adult:{
    label:'Older Adult',
    pace:2.0,
    detour:0.30,
    help:'Uses a slower pace and allows up to 30% extra time to prioritize shade.'
  },
  heat_sensitive:{
    label:'Heat-Sensitive',
    pace:2.0,
    detour:0.30,
    help:'Gives shade the strongest priority and allows up to 30% extra time. This is not medical advice.'
  }
};

function selectedWalkingProfile(){
  const key=document.getElementById('walking-profile').value;
  return {key,...(WALKING_PROFILES[key]||WALKING_PROFILES.adult)};
}

function applyWalkingProfile(){
  const profile=selectedWalkingProfile();
  const paceSelect=document.getElementById('walking-pace');
  const custom=document.getElementById('custom-pace');
  const matching=[...paceSelect.options].find(option=>Number(option.value)===profile.pace);
  if(matching){
    paceSelect.value=matching.value;
    custom.disabled=true;
  }else{
    paceSelect.value='custom';
    custom.disabled=false;
  }
  custom.value=profile.pace.toFixed(1);
  document.getElementById('walking-profile-help').textContent=profile.help;
  const normalOption=document.querySelector('#route-limit-mode option[value="normal"]');
  if(normalOption){
    normalOption.textContent=`Profile default — no more than ${Math.round(profile.detour*100)}% longer in time`;
  }
  updatePaceWarning();
}

document.getElementById('walking-profile').addEventListener('change',applyWalkingProfile);
document.getElementById('walking-pace').addEventListener('change',event=>{
  const customInput=document.getElementById('custom-pace');
  const isCustom=event.target.value==='custom';
  customInput.disabled=!isCustom;
  if(!isCustom){
    customInput.value=Number(event.target.value).toFixed(1);
  }
  updatePaceWarning();
});
ecoResetDepartureDefaults();

function selectedDeparture(){
  if(document.getElementById('ignore-time').checked)return {value:'',use:false};
  ecoSetDefaultDeparture('');
  const date=document.getElementById('departure-date').value;
  let hour=Math.max(1,Math.min(12,Number(document.getElementById('departure-hour').value)||12));
  const minute=Math.max(0,Math.min(59,Number(document.getElementById('departure-minute').value)||0));
  const ampm=document.getElementById('departure-ampm').value;
  if(ampm==='AM'&&hour===12)hour=0;
  if(ampm==='PM'&&hour!==12)hour+=12;
  return {value:window.ecoDepartureManual?.['']?`${date}T${String(hour).padStart(2,'0')}:${String(minute).padStart(2,'0')}`:window.ecoDepartureAutoValues[''],use:true};
}
document.getElementById('ignore-time').addEventListener('change',event=>{
  document.getElementById('time-controls').classList.toggle('opacity-40',event.target.checked);
  document.querySelectorAll('#time-controls input,#time-controls select').forEach(el=>el.disabled=event.target.checked);
});

function selectedBikeDeparture(){
  if(document.getElementById('bike-ignore-time').checked)return {value:'',use:false};
  ecoSetDefaultDeparture('bike-');
  const date=document.getElementById('bike-departure-date').value;
  let hour=Math.max(1,Math.min(12,Number(document.getElementById('bike-departure-hour').value)||12));
  const minute=Math.max(0,Math.min(59,Number(document.getElementById('bike-departure-minute').value)||0));
  const ampm=document.getElementById('bike-departure-ampm').value;
  if(ampm==='AM'&&hour===12)hour=0;
  if(ampm==='PM'&&hour!==12)hour+=12;
  return {value:window.ecoDepartureManual?.['bike-']?`${date}T${String(hour).padStart(2,'0')}:${String(minute).padStart(2,'0')}`:window.ecoDepartureAutoValues['bike-'],use:true};
}
document.getElementById('bike-ignore-time').addEventListener('change',event=>{
  const disabled=event.target.checked;
  document.getElementById('bike-time-controls').classList.toggle('opacity-40',disabled);
  document.querySelectorAll('#bike-time-controls input,#bike-time-controls select').forEach(el=>el.disabled=disabled);
});

function updatePaceWarning(){
  const pace=selectedWalkingPace();
  const warning=document.getElementById('pace-warning-live');
  if(pace>=8){
    warning.textContent=`${pace.toFixed(1)} mph is too fast for normal walking and is likely running speed. EcoTrace will still calculate it.`;
    warning.classList.remove('hidden');
  }else if(pace<1.5){
    warning.textContent=`${pace.toFixed(1)} mph is a very slow walking pace. EcoTrace will still use it.`;
    warning.classList.remove('hidden');
  }else warning.classList.add('hidden');
}
document.getElementById('custom-pace').addEventListener('input',updatePaceWarning);
applyWalkingProfile();

document.getElementById('route-limit-mode').addEventListener('change',event=>{
  document.getElementById('extra-distance-wrap').classList.toggle(
    'hidden',
    event.target.value!=='distance'
  );
});

function selectedBikeSpeed(){
  const selected=document.getElementById('bike-speed-mode').value;
  if(selected==='custom'){
    return Math.max(3,Math.min(35,Number(document.getElementById('bike-custom-speed').value)||12));
  }
  return Number(selected)||12;
}
document.getElementById('bike-speed-mode').addEventListener('change',event=>{
  document.getElementById('bike-custom-speed-wrap').classList.toggle('hidden',event.target.value!=='custom');
});
document.getElementById('bike-limit-mode').addEventListener('change',event=>{
  document.getElementById('bike-extra-distance-wrap').classList.toggle('hidden',event.target.value!=='distance');
});


function bikeInfrastructureLabel(infra){
  if(!infra?.data_available)return 'Bike-infrastructure map unavailable';
  const unknown=Math.max(0,Math.min(100,Number(infra.unknown_percent||0)));
  const dedicated=Math.max(0,Math.min(100,Number(infra.dedicated_infrastructure_percent||0)));
  const classified=Math.max(0,100-unknown);
  return `${dedicated.toFixed(1)}% protected/painted • ${classified.toFixed(1)}% classified by the local OSM bike model`;
}


function infraFeatures(infra,routeName){
  return (infra?.segments||[]).map(segment=>({
    type:'Feature',
    properties:{
      category:segment.category||'unknown',
      explanation:segment.explanation||'No confident mapped bike-facility match',
      route_name:routeName
    },
    geometry:{type:'LineString',coordinates:segment.coordinates}
  }));
}

function hideBikeInfrastructureColors(){
  for(const sourceId of ['bike-fastest-infra-color','bike-cooler-infra-color']){
    const source=map.getSource(sourceId);
    if(source)source.setData({type:'FeatureCollection',features:[]});
  }
  for(const layerId of ['bike-fastest-infra-color-line','bike-cooler-infra-color-line']){
    if(map.getLayer(layerId))map.setLayoutProperty(layerId,'visibility','none');
  }
  if(map.getLayer('bike-fastest-line'))map.setLayoutProperty('bike-fastest-line','visibility','visible');
  if(map.getLayer('bike-cooler-line'))map.setLayoutProperty('bike-cooler-line','visibility','visible');
  document.getElementById('bike-infra-map-legend')?.classList.add('hidden');
}

function infrastructureSourceLabel(infra,sourceKey){
  if(!infra?.data_available)return `${sourceKey==='vta'?'VTA':'OSM'} bike classification unavailable`;
  const dedicated=Number(infra.dedicated_infrastructure_percent||0);
  const unknown=Number(infra.unknown_percent||0);
  const classified=Number(infra.classified_percent ?? Math.max(0,100-unknown));
  return sourceKey==='vta'
    ?`${dedicated.toFixed(1)}% VTA Class I/II/IV-style dedicated match • ${classified.toFixed(1)}% matched to VTA 2023 GIS`
    :`${dedicated.toFixed(1)}% protected/painted • ${classified.toFixed(1)}% classified by the local OSM bike model`;
}

async function selectedBikeInfrastructure(data){
  const sourceKey=document.getElementById('bike-infra-source')?.value||'osm';
  if(sourceKey==='osm'){
    return {
      sourceKey:'osm',
      sourceLabel:'OpenStreetMap bike data',
      fastest:data.fastest_infrastructure,
      cooler:data.cooler_infrastructure
    };
  }

  const status=document.getElementById('bike-dedicated-infra-status');
  if(status)status.textContent='Loading official VTA 2023 bikeway GIS near both routes…';

  const response=await fetchWithTimeout('/api/vta-bike/classify',{
    method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({
      fastest_coordinates:data.fastest_coordinates,
      cooler_coordinates:data.cooler_coordinates
    })
  },30000);
  const vta=await readApiResponse(response);

  return {
    sourceKey:'vta',
    sourceLabel:'Official VTA Santa Clara County Bikeways 2023 GIS',
    fastest:vta.fastest_infrastructure,
    cooler:vta.cooler_infrastructure
  };
}

async function toggleBothBikeInfrastructure(data,button){
  const fastestSource=map.getSource('bike-fastest-infra-color');
  const coolerSource=map.getSource('bike-cooler-infra-color');
  if(!fastestSource||!coolerSource)return;

  const enabled=button.dataset.enabled==='true';
  if(enabled){
    hideBikeInfrastructureColors();
    button.dataset.enabled='false';
    button.textContent='🚲 Color both routes by selected bike source';
    return;
  }

  button.disabled=true;
  button.textContent='Loading bike infrastructure…';

  try{
    const selected=await selectedBikeInfrastructure(data);

    fastestSource.setData({
      type:'FeatureCollection',
      features:infraFeatures(selected.fastest,'Original / fastest')
    });
    coolerSource.setData({
      type:'FeatureCollection',
      features:infraFeatures(selected.cooler,'Cooler')
    });

    map.setLayoutProperty('bike-fastest-infra-color-line','visibility','visible');
    map.setLayoutProperty('bike-cooler-infra-color-line','visibility','visible');
    if(map.getLayer('bike-fastest-line'))map.setLayoutProperty('bike-fastest-line','visibility','none');
    if(map.getLayer('bike-cooler-line'))map.setLayoutProperty('bike-cooler-line','visibility','none');

    map.moveLayer('bike-fastest-infra-color-line');
    map.moveLayer('bike-cooler-infra-color-line');
    document.getElementById('bike-infra-map-legend')?.classList.remove('hidden');

    button.dataset.enabled='true';
    button.textContent=`🚲 Hide ${selected.sourceKey==='vta'?'VTA':'OSM'} bike-infrastructure colors`;

    const fastestLabel=document.getElementById('bike-fastest-infra-label');
    const coolerLabel=document.getElementById('bike-cooler-infra-label');
    if(fastestLabel)fastestLabel.textContent=infrastructureSourceLabel(selected.fastest,selected.sourceKey);
    if(coolerLabel)coolerLabel.textContent=infrastructureSourceLabel(selected.cooler,selected.sourceKey);

    const status=document.getElementById('bike-dedicated-infra-status');
    if(status){
      status.textContent=`Currently colored using ${selected.sourceLabel}. Switch the dropdown to compare the other source.`;
    }

    window.latestBikeValidationContext={
      source:selected.sourceLabel,
      selected_source:selected.sourceKey,
      original_or_fastest:selected.fastest,
      cooler:selected.cooler,
      comparison_note:'OSM is community-maintained and may contain newer local edits; VTA is the official countywide 2023 Class I–IV GIS reference.',
      legend:{
        protected:'green',
        painted:'purple',
        shared_low_speed:'orange',
        shared_road:'orange',
        unknown:'gray'
      }
    };
  }catch(error){
    hideBikeInfrastructureColors();
    button.dataset.enabled='false';
    button.textContent='🚲 Color both routes by selected bike source';
    const status=document.getElementById('bike-dedicated-infra-status');
    if(status)status.textContent=error.message||'Selected bike infrastructure source could not be loaded.';
  }finally{
    button.disabled=false;
  }
}
function showDedicatedBikeInfrastructure(infra,button){
  const source=map.getSource('bike-infrastructure-segments');
  if(!source)return;

  const currently=button.dataset.enabled==='true';
  if(currently){
    source.setData({type:'FeatureCollection',features:[]});
    map.setLayoutProperty('bike-infrastructure-segments-line','visibility','none');
    document.getElementById('bike-infra-map-legend')?.classList.add('hidden');
    button.dataset.enabled='false';
    button.textContent='🚲 Color both routes by bike infrastructure';
    return;
  }

  const dedicatedCategories=new Set(['protected','painted']);
  const features=(infra?.segments||[])
    .filter(segment=>dedicatedCategories.has(segment.category))
    .map(segment=>({
      type:'Feature',
      properties:{
        category:segment.category,
        explanation:segment.explanation||'OSM-tagged bike infrastructure'
      },
      geometry:{
        type:'LineString',
        coordinates:segment.coordinates
      }
    }));

  source.setData({type:'FeatureCollection',features});
  map.setLayoutProperty('bike-infrastructure-segments-line','visibility','visible');
  map.moveLayer('bike-infrastructure-segments-line');
  document.getElementById('bike-infra-map-legend')?.classList.remove('hidden');

  button.dataset.enabled='true';
  button.textContent='🚲 Hide dedicated bike infrastructure';

  const status=document.getElementById('bike-dedicated-infra-status');
  if(status){
    status.textContent=features.length
      ?`${features.length} route segments were confidently matched to protected/separated or painted bike infrastructure.`
      :'No route segments were confidently matched to protected/separated or painted bike infrastructure. Check the VTA/511 reference map for possible OSM gaps.';
  }
}

function showBikeInfrastructureSegments(infra,button,label){
  const source=map.getSource('bike-infrastructure-segments');
  if(!source)return;
  const currently=button.dataset.enabled==='true';
  document.querySelectorAll('.bike-infra-show').forEach(b=>{
    b.dataset.enabled='false';
    b.textContent=b.dataset.baseLabel;
  });
  const dedicatedButton=document.getElementById('bike-dedicated-infra-toggle');
  if(dedicatedButton){
    dedicatedButton.dataset.enabled='false';
    dedicatedButton.textContent='🚲 Color both routes by bike infrastructure';
  }
  if(currently){
    source.setData({type:'FeatureCollection',features:[]});
    map.setLayoutProperty('bike-infrastructure-segments-line','visibility','none');
    document.getElementById('bike-infra-map-legend')?.classList.add('hidden');
    return;
  }
  const features=(infra?.segments||[]).map(segment=>({
    type:'Feature',
    properties:{
      category:segment.category||'unknown',
      explanation:segment.explanation||'No mapped bike facility matched'
    },
    geometry:{type:'LineString',coordinates:segment.coordinates}
  }));
  source.setData({type:'FeatureCollection',features});
  map.setLayoutProperty('bike-infrastructure-segments-line','visibility','visible');
  map.moveLayer('bike-infrastructure-segments-line');
  document.getElementById('bike-infra-map-legend')?.classList.remove('hidden');
  button.dataset.enabled='true';
  button.textContent=`Hide ${label} infrastructure`;
}

async function requestBikeRoute(){
  const [start,end]=bikeClicks;
  if(!start||!end)throw new Error('Both bike endpoints are required.');
  const operationGeneration=bikeOperationGeneration;
  const speed=selectedBikeSpeed();
  const type=document.getElementById('bike-type').value;
  const departure=selectedBikeDeparture();
  const limitMode=document.getElementById('bike-limit-mode').value;
  const extraMiles=Math.max(0,Number(document.getElementById('bike-extra-distance').value)||0);
  const customParam=limitMode==='distance'
    ?`&max_extra_distance_miles=${encodeURIComponent(extraMiles)}`
    :'';

  document.getElementById('status-pill').textContent='Calculating bike routes…';
  const response=await fetchWithTimeout(
    `/api/bike-route?start_lat=${encodeURIComponent(start.lat)}&start_lon=${encodeURIComponent(start.lon)}&end_lat=${encodeURIComponent(end.lat)}&end_lon=${encodeURIComponent(end.lon)}&bike_type=${encodeURIComponent(type)}&speed_mph=${encodeURIComponent(speed)}&max_detour=0.15&departure=${encodeURIComponent(departure.value)}&use_time_of_day=${encodeURIComponent(departure.use)}&quick=${encodeURIComponent(bikeQuickRouteMode)}${customParam}`,
    {},
    bikeQuickRouteMode ? 40000 : 60000
  );
  const data=await readApiResponse(response);
  if(operationGeneration!==bikeOperationGeneration||activeToolPanel!=='bike-panel'){
    return;
  }
  window.latestEcoTraceBikeRoute=data;
  window.latestBikeValidationContext={
    source:'OpenStreetMap-derived local bike layer; VTA 2023 is an external validation reference',
    original_or_fastest:data.fastest_infrastructure,
    cooler:data.cooler_infrastructure,
    legend:{
      protected:'green',
      painted:'purple',
      shared_low_speed:'purple',
      shared_road:'orange',
      unknown:'gray'
    }
  };

  const fastestSource=map.getSource('bike-fastest');
  const coolerSource=map.getSource('bike-cooler');
  if(!fastestSource||!coolerSource){
    throw new Error('Bike route layers are still initializing. Wait one second and retry.');
  }

  fastestSource.setData({type:'Feature',properties:{},geometry:{type:'LineString',coordinates:data.fastest_coordinates}});
  coolerSource.setData(
    data.meaningful_cooler_route
      ?{type:'Feature',properties:{},geometry:{type:'LineString',coordinates:data.cooler_coordinates}}
      :{type:'FeatureCollection',features:[]}
  );

  for(const layerId of ['bike-fastest-casing','bike-fastest-line','bike-cooler-casing','bike-cooler-line']){
    if(map.getLayer(layerId)){
      map.setLayoutProperty(layerId,'visibility','visible');
      map.moveLayer(layerId);
    }
  }

  const comparison=document.getElementById('bike-comparison');
  comparison.classList.remove('hidden');
  const bikeDepartureLabel=data.time_of_day_used
    ?new Date(data.departure_time).toLocaleString([],{timeZone:'America/Los_Angeles',timeZoneName:'short',
        year:'numeric',month:'short',day:'numeric',hour:'numeric',minute:'2-digit'
      })
    :'Time-of-day adjustment off';
  comparison.innerHTML=`
    <div class="mb-3 rounded-xl bg-blue-300/5 p-2 text-[10px] leading-4 text-blue-100 ring-1 ring-blue-300/15">
      <strong>Bike infrastructure evidence:</strong> ${data.fastest_infrastructure?.official_vta_2023
        ?'EcoTrace is using its local OpenStreetMap-derived bike layer.'
        :'EcoTrace is using its local OpenStreetMap-derived bike layer. VTA 2023 GIS and VTA/511 remain independent validation references.'}
    </div>
    <div class="mb-3 rounded-xl bg-cyan-300/10 p-3 text-xs leading-5 text-cyan-100 ring-1 ring-cyan-300/20">
      ${routeLightingGuide(data)}
      <strong>Shade time:</strong> ${escapeHtml(bikeDepartureLabel)}
      <br><span class="text-slate-300">${data.time_of_day_used
        ?'Bike shade was recalculated for this departure time.'
        :'Time-of-day shadow adjustment is disabled for this route.'}</span>
    </div>
    <article class="rounded-2xl border border-fuchsia-300/30 bg-fuchsia-400/10 p-3">
      <div class="flex items-center justify-between"><p class="font-black text-fuchsia-100">Fastest bike route</p><p class="text-xl font-black text-white">${data.fastest_minutes} min</p></div>
      <p class="mt-1 text-xs text-slate-300">${data.fastest_distance_miles} mi (${data.fastest_distance_km} km) • ${routeShadeLabel(data,data.fastest_shade_score)}</p>
        ${routeScoreExplanation(data,data.fastest_shade_score)}
      <p id="bike-fastest-infra-label" class="mt-1 text-xs text-slate-300">${escapeHtml(bikeInfrastructureLabel(data.fastest_infrastructure))}</p>
      <details class="mt-3 rounded-lg bg-black/15 p-2">
        <summary class="cursor-pointer text-xs font-bold text-fuchsia-100">Shade and sunlight</summary>
        <div class="mt-2 text-xs leading-5 text-slate-300">
          <p><strong>Tree-canopy shadow:</strong> ${routeShadowLabel(data,data.fastest_tree_shadow_percent)}</p>
          <p><strong>Building shadow:</strong> ${routeShadowLabel(data,data.fastest_building_shadow_percent)}</p>
          <p><strong>Evaluated:</strong> ${escapeHtml(bikeDepartureLabel)}</p>
          <p class="mt-1 text-[10px] text-slate-500">Direct-shadow estimate only; lux, reflected light, albedo, terrain, diffuse sky radiation, and species-specific leaf density are not modeled.</p>
        </div>
      </details>
      <details class="mt-2 rounded-lg bg-black/15 p-2"><summary class="cursor-pointer text-xs font-bold text-fuchsia-100">Why this route?</summary><p class="mt-2 text-xs leading-5 text-slate-300">${escapeHtml(data.fastest_why)}</p></details>
    </article>
    ${data.meaningful_cooler_route?`
    <article class="mt-3 rounded-2xl border border-cyan-300/30 bg-cyan-400/10 p-3">
      <div class="flex items-center justify-between"><p class="font-black text-cyan-100">Cooler bike route</p><p class="text-xl font-black text-white">${data.cooler_minutes} min</p></div>
      <p class="mt-1 text-xs text-slate-300">${data.cooler_distance_miles} mi (${data.cooler_distance_km} km) • ${routeShadeLabel(data,data.cooler_shade_score)}</p>
        ${routeScoreExplanation(data,data.cooler_shade_score)}
      <p id="bike-cooler-infra-label" class="mt-1 text-xs text-slate-300">${escapeHtml(bikeInfrastructureLabel(data.cooler_infrastructure))}</p>
      <details class="mt-3 rounded-lg bg-black/15 p-2"><summary class="cursor-pointer text-xs font-bold text-cyan-100">Shade and sunlight</summary>
        <div class="mt-2 text-xs leading-5 text-slate-300">
          <p><strong>Tree-canopy shadow:</strong> ${routeShadowLabel(data,data.cooler_tree_shadow_percent)}</p>
          <p><strong>Building shadow:</strong> ${routeShadowLabel(data,data.cooler_building_shadow_percent)}</p>
          <p><strong>Evaluated:</strong> ${escapeHtml(bikeDepartureLabel)}</p>
          <p class="mt-1 text-[10px] text-slate-500">Direct-shadow estimate only; lux, reflected light, albedo, terrain, diffuse sky radiation, and species-specific leaf density are not modeled.</p>
        </div>
      </details>
      <details class="mt-2 rounded-lg bg-black/15 p-2"><summary class="cursor-pointer text-xs font-bold text-cyan-100">Why this route?</summary><p class="mt-2 text-xs leading-5 text-slate-300">${escapeHtml(data.cooler_why)}</p></details>
    </article>`:`
    <article class="mt-3 rounded-2xl border border-slate-300/20 bg-white/5 p-3">
      <p class="font-black text-white">${data.nighttime?'Nighttime route':'No meaningfully better shaded bike route found'}</p>
      <p class="mt-1 text-xs leading-5 text-slate-400">${data.nighttime?'There is no modeled direct sunlight at this time, so a shade detour is not applicable.':'EcoTrace found no alternative with enough extra modeled shade to justify a second route, so only the original/fastest route is shown.'}</p>
    </article>`}
    <div class="mt-3 grid grid-cols-3 gap-2 text-center">
      <div class="rounded-xl bg-white/5 p-2"><p class="text-[10px] uppercase text-slate-400">Extra time</p><p class="font-black">+${data.extra_minutes} min</p></div>
      <div class="rounded-xl bg-white/5 p-2"><p class="text-[10px] uppercase text-slate-400">Detour</p><p class="font-black">${data.detour_percent}%</p></div>
      <div class="rounded-xl bg-white/5 p-2"><p class="text-[10px] uppercase text-slate-400">Shade gain</p><p class="font-black text-cyan-200">${data.nighttime?'N/A':'+'+data.shade_gain}</p></div>
    </div>
    <div class="mt-3 rounded-xl bg-blue-300/5 p-3 ring-1 ring-blue-300/15">
      <div class="mb-2 grid grid-cols-[1fr_auto] gap-2">
        <select id="bike-infra-source"
          class="rounded-lg border border-white/10 bg-slate-950/70 px-2 py-2 text-[10px] font-bold text-white">
          <option value="osm">OSM bike data</option>
          <option value="vta">VTA 2023 official GIS</option>
        </select>
        <a href="https://www.openstreetmap.org/#layers=Y"
           target="_blank" rel="noopener noreferrer"
           class="rounded-lg bg-white/5 px-2 py-2 text-[10px] font-black text-blue-100 ring-1 ring-white/10">
          OSM/CyclOSM ↗
        </a>
      </div>
      <button id="bike-dedicated-infra-toggle" type="button" data-enabled="false"
        class="w-full rounded-lg bg-blue-400/15 px-3 py-2 text-xs font-black text-blue-100 ring-1 ring-blue-300/20">
        🚲 Color both routes by selected bike source
      </button>
      <p id="bike-dedicated-infra-status" class="mt-2 text-[10px] leading-4 text-slate-400">
        Choose OSM for community-maintained, often newer bike tags, or VTA for the official 2023 countywide Class I–IV GIS layer.
      </p>
    </div>
    <div class="mt-3 rounded-xl bg-slate-950/35 p-3 ring-1 ring-white/10">
      <p class="text-[10px] font-black uppercase tracking-wider text-slate-400">Map verification tools</p>
      <div class="mt-2 grid grid-cols-2 gap-2">
        <button type="button" class="bike-infra-show rounded-lg bg-fuchsia-300/10 px-2 py-2 text-[10px] font-bold text-fuchsia-100 ring-1 ring-fuchsia-300/20"
          data-enabled="false" data-base-label="Show fastest infrastructure">Show fastest infrastructure</button>
        <button type="button" class="bike-infra-show rounded-lg bg-cyan-300/10 px-2 py-2 text-[10px] font-bold text-cyan-100 ring-1 ring-cyan-300/20"
          data-enabled="false" data-base-label="Show cooler infrastructure">Show cooler infrastructure</button>
      </div>
      <p class="mt-2 text-[9px] leading-4 text-slate-500">Green = protected/off-street, purple = painted lane or shared-route class, orange = shared road, gray = unmatched/unknown. This is mapped OSM-derived evidence; use VTA as an independent official cross-check, not a guarantee of current field conditions.</p>
    </div>
    <div class="mt-3 rounded-xl bg-emerald-300/5 p-2 ring-1 ring-emerald-300/15">
      <button id="bike-route-planting-toggle" type="button" data-enabled="false"
        class="w-full rounded-lg bg-emerald-400/15 px-3 py-2 text-xs font-black text-emerald-100 ring-1 ring-emerald-300/20">
        🌳 Show screened tree sites along cooler route
      </button>
      <p id="bike-route-planting-status" class="mt-2 text-[10px] leading-4 text-slate-400"></p>
    </div>
    ${data.warning?`<p class="mt-3 rounded-xl bg-amber-400/10 p-3 text-xs leading-5 text-amber-200 ring-1 ring-amber-300/20">${escapeHtml(data.warning)}</p>`:''}
    <p class="mt-3 text-xs leading-5 text-slate-400">${escapeHtml(data.route_limit_description)}</p>`;

  const dedicatedInfraButton=document.getElementById('bike-dedicated-infra-toggle');
  if(dedicatedInfraButton){
    dedicatedInfraButton.addEventListener('click',()=>toggleBothBikeInfrastructure(
      data,
      dedicatedInfraButton
    ));
  }

  const infraSourceSelect=document.getElementById('bike-infra-source');
  if(infraSourceSelect){
    infraSourceSelect.addEventListener('change',()=>{
      hideBikeInfrastructureColors();
      if(dedicatedInfraButton){
        dedicatedInfraButton.dataset.enabled='false';
        dedicatedInfraButton.textContent='🚲 Color both routes by selected bike source';
      }
      const status=document.getElementById('bike-dedicated-infra-status');
      if(status){
        status.textContent=infraSourceSelect.value==='vta'
          ?'VTA selected — click Color to query the official 2023 Class I–IV GIS layer near these routes.'
          :'OSM selected — click Color to use EcoTrace’s local OpenStreetMap-derived bike classification.';
      }
    });
  }

  const infraButtons=document.querySelectorAll('.bike-infra-show');
  if(infraButtons.length>=2){
    infraButtons[0].addEventListener('click',()=>showBikeInfrastructureSegments(
      data.fastest_infrastructure,infraButtons[0],'fastest'
    ));
    infraButtons[1].addEventListener('click',()=>showBikeInfrastructureSegments(
      data.cooler_infrastructure,infraButtons[1],'cooler'
    ));
  }
  const bikePlantingButton=document.getElementById('bike-route-planting-toggle');
  if(bikePlantingButton){
    bikePlantingButton.addEventListener('click',()=>showPlantingSitesAlongRoutes(
      [
        {
          key:'fastest',
          label:'Original / fastest',
          color:'#e879f9',
          coordinates:data.fastest_coordinates
        },
        {
          key:'cooler',
          label:'Cooler alternative',
          color:'#22d3ee',
          coordinates:data.cooler_coordinates
        }
      ],
      bikePlantingButton,
      document.getElementById('bike-route-planting-status')
    ));
  }

  const bounds=new maplibregl.LngLatBounds();
  data.fastest_coordinates.concat(data.cooler_coordinates).forEach(point=>bounds.extend(point));
  map.fitBounds(bounds,{padding:{top:90,bottom:90,left:440,right:90},maxZoom:16,duration:1000});
  document.getElementById('status-pill').textContent=`Bike route ready • ${data.bike_speed_mph} mph • ${data.time_of_day_used?'time-aware shade':'time ignored'}`;
}

async function requestRoute(){
  const [a,b]=routeClicks;
  if(!a||!b)throw new Error('Both route endpoints are required.');
  const operationGeneration=walkOperationGeneration;
  if(!map.getSource('shortest-route')||!map.getSource('shadiest-route')){
    throw new Error('The route layers are still initializing. Wait one second and try again.');
  }
  const profile=selectedWalkingProfile();
  const pace=selectedWalkingPace();
  const departure=selectedDeparture();
  const limitMode=document.getElementById('route-limit-mode').value;
  const extraMiles=Math.max(0,Number(document.getElementById('max-extra-distance').value)||0);
  const customDistanceParam=limitMode==='distance'
    ?`&max_extra_distance_miles=${encodeURIComponent(extraMiles)}`
    :'';
  const response=await fetchWithTimeout(`/api/shadiest-route?start_lat=${encodeURIComponent(a.lat)}&start_lon=${encodeURIComponent(a.lon)}&end_lat=${encodeURIComponent(b.lat)}&end_lon=${encodeURIComponent(b.lon)}&max_detour=${encodeURIComponent(profile.detour)}&pace_mph=${encodeURIComponent(pace)}&user_profile=${encodeURIComponent(profile.key)}&departure=${encodeURIComponent(departure.value)}&use_time_of_day=${encodeURIComponent(departure.use)}${customDistanceParam}`,{},75000);
  const data=await readApiResponse(response);
  window.latestEcoTraceRoute=data;
  map.getSource('shortest-route').setData({type:'Feature',geometry:{type:'LineString',coordinates:data.shortest_coordinates},properties:{}});
  map.getSource('shadiest-route').setData(
    data.meaningful_shadier_route
      ?{type:'Feature',geometry:{type:'LineString',coordinates:data.shadiest_coordinates},properties:{}}
      :{type:'FeatureCollection',features:[]}
  );
  map.getSource('exposure-segments').setData({
    type:'FeatureCollection',
    features:(data.meaningful_shadier_route?data.shadiest_segments:data.shortest_segments||[]).map(segment=>({
      type:'Feature',
      properties:{category:segment.category,shade:segment.shade_percent,reason:segment.reason,canopy:segment.canopy_shadow_percent,building:segment.building_shadow_percent,solarAltitude:segment.solar_altitude_degrees,solarAzimuth:segment.solar_azimuth_degrees,dataQuality:segment.data_quality},
      geometry:{type:'LineString',coordinates:segment.coordinates}
    }))
  });
  document.getElementById('status-pill').textContent=data.meaningful_shadier_route
    ?`Green route: +${data.extra_minutes} min • +${data.shade_gain_percent} shade-score points`
    :(data.nighttime?'Nighttime — shortest route shown':'No meaningfully shadier route found — shortest route shown');
  const walkingComparison=document.getElementById('route-comparison');
  walkingComparison.classList.remove('hidden');
  walkingComparison.innerHTML=`
    <h2 class="text-lg font-black text-white">${data.meaningful_shadier_route?'Walking route comparison':'Walking route result'}</h2>
    ${routeLightingGuide(data)}
    <div class="mt-2 rounded-xl bg-emerald-400/10 p-2 text-xs leading-5 text-emerald-100 ring-1 ring-emerald-300/20">
      <strong>${escapeHtml(WALKING_PROFILES[data.user_profile]?.label||'Adult')} profile:</strong>
      ${escapeHtml(data.profile_guidance)}
    </div>
    ${!data.nighttime&&!data.meaningful_shadier_route?`
      <div class="mt-3 rounded-xl bg-amber-400/10 p-3 text-sm leading-6 text-amber-100 ring-1 ring-amber-300/20">
        <strong>No meaningfully shadier alternative found.</strong><br>
        The available alternatives were the same route or improved the estimated shade score by less than 2 points within this profile's detour limit.
      </div>
    `:''}
    <p class="mt-3 text-xs leading-5 text-slate-400">${escapeHtml(data.canopy_model_summary)}</p>
    <div class="mt-3 space-y-3">
      <div class="rounded-xl border border-sky-300/30 bg-sky-400/10 p-3">
        <div class="flex items-center justify-between">
          <p class="font-bold text-sky-200">● Shortest route</p>
          <p class="text-lg font-black text-white">${data.shortest_minutes} min</p>
        </div>
        <p class="mt-1 text-xs text-slate-300">${data.shortest_distance_miles} miles (${data.shortest_distance_km} km) • ${routeShadeLabel(data,data.shortest_shade_percent)}</p>
        ${routeScoreExplanation(data,data.shortest_shade_percent)}
        <details class="mt-3 rounded-lg bg-black/15 p-2">
          <summary class="cursor-pointer text-xs font-bold text-sky-100">Why this route?</summary>
          <p class="mt-2 text-xs leading-5 text-slate-300">${escapeHtml(data.shortest_route_why)}</p>
          <div class="mt-2 grid grid-cols-2 gap-2 text-[11px] text-slate-300">
            <p><strong>${data.canopy_model_available?'Tree-canopy shadow score':'Tree-canopy evidence'}:</strong> ${routeShadowLabel(data,data.shortest_canopy_percent)}</p>
            <p><strong>Building shadow:</strong> ${data.nighttime?'Not applicable at night':data.canopy_model_available?routeShadowLabel(data,data.shortest_building_shadow_percent):'Unavailable — canopy model missing'}</p>
          </div>
        </details>
      </div>
${data.meaningful_shadier_route?`      <div class="rounded-xl border border-emerald-300/30 bg-emerald-400/10 p-3">
        <div class="flex items-center justify-between">
          <p class="font-bold text-emerald-200">● Shade-oriented route</p>
          <p class="text-lg font-black text-white">${data.shadiest_minutes} min</p>
        </div>
        <p class="mt-1 text-xs text-slate-300">${data.shadiest_distance_miles} miles (${data.shadiest_distance_km} km) • ${routeShadeLabel(data,data.shadiest_shade_percent)}</p>
        ${routeScoreExplanation(data,data.shadiest_shade_percent)}
        <details class="mt-3 rounded-lg bg-black/15 p-2">
          <summary class="cursor-pointer text-xs font-bold text-emerald-100">Why this route?</summary>
          <p class="mt-2 text-xs leading-5 text-slate-300">${escapeHtml(data.shadiest_route_why)}</p>
          <div class="mt-2 grid grid-cols-2 gap-2 text-[11px] text-slate-300">
            <p><strong>${data.canopy_model_available?'Tree-canopy shadow score':'Tree-canopy evidence'}:</strong> ${routeShadowLabel(data,data.shadiest_canopy_percent)}</p>
            <p><strong>Building shadow:</strong> ${data.nighttime?'Not applicable at night':data.canopy_model_available?routeShadowLabel(data,data.shadiest_building_shadow_percent):'Unavailable — canopy model missing'}</p>
          </div>
        </details>
      </div>`:''}
    </div>
    ${data.meaningful_shadier_route?`
    <div class="mt-3 grid grid-cols-3 gap-2">
      <div class="rounded-xl bg-white/5 p-2.5 text-center"><p class="text-[10px] uppercase text-slate-400">Extra time</p><p class="font-black text-white">+${data.extra_minutes} min</p></div>
      <div class="rounded-xl bg-white/5 p-2.5 text-center"><p class="font-black text-white">${data.detour_percent}%</p><p class="text-[10px] uppercase text-slate-400">Detour</p></div>
      <div class="rounded-xl bg-white/5 p-2.5 text-center"><p class="font-black text-emerald-300">+${data.shade_gain_percent}</p><p class="text-[10px] uppercase text-slate-400">Shade gain</p></div>
    </div>`:''}
    <p class="mt-3 text-sm leading-6 text-slate-300">${escapeHtml(data.message)}</p>
    <div class="mt-3 rounded-xl bg-emerald-300/5 p-2 ring-1 ring-emerald-300/15">
      <button id="walk-route-planting-toggle" type="button" data-enabled="false"
        class="w-full rounded-lg bg-emerald-400/15 px-3 py-2 text-xs font-black text-emerald-100 ring-1 ring-emerald-300/20">
        🌳 Show screened tree sites along both routes
      </button>
      <p id="walk-route-planting-status" class="mt-2 text-[10px] leading-4 text-slate-400"></p>
    </div>`;
  const walkPlantingButton=document.getElementById('walk-route-planting-toggle');
  if(walkPlantingButton){
    const routeSet=[
      {
        key:'shortest',
        label:'Original / shortest',
        color:'#38bdf8',
        coordinates:data.shortest_coordinates
      }
    ];
    if(data.meaningful_shadier_route){
      routeSet.push({
        key:'shadiest',
        label:'Shadier alternative',
        color:'#4ade80',
        coordinates:data.shadiest_coordinates
      });
    }
    walkPlantingButton.addEventListener('click',()=>showPlantingSitesAlongRoutes(
      routeSet,
      walkPlantingButton,
      document.getElementById('walk-route-planting-status')
    ));
  }

  const bounds=new maplibregl.LngLatBounds();
  (data.meaningful_shadier_route?data.shortest_coordinates.concat(data.shadiest_coordinates):data.shortest_coordinates).forEach(c=>bounds.extend(c));
  map.fitBounds(bounds,{padding:{top:90,bottom:90,left:440,right:90},maxZoom:16,duration:1100});
  for(const layerId of ['shortest-route-casing','shortest-route-line','shadiest-route-casing','shadiest-route-line','exposure-segments-line']){
    if(map.getLayer(layerId)){
      map.setLayoutProperty(layerId,'visibility','visible');
      map.moveLayer(layerId);
    }
  }
}


async function loadAssistantPrompts(){
  const grid=document.getElementById('assistant-prompt-grid');
  if(!grid)return;
  try{
    const response=await fetchWithTimeout('/api/assistant-prompts',{},10000);
    const data=await readApiResponse(response);
    grid.innerHTML='';
    for(const prompt of data.prompts||[]){
      const button=document.createElement('button');
      button.type='button';
      button.className='rounded-full bg-violet-300/10 px-2.5 py-1.5 text-[11px] font-semibold text-violet-100 ring-1 ring-violet-300/20 hover:bg-violet-300/20';
      button.textContent=prompt;
      button.addEventListener('click',()=>{
        document.getElementById('assistant-question').value=prompt;
        document.getElementById('assistant-submit').click();
      });
      grid.appendChild(button);
    }
  }catch(error){
    grid.innerHTML='<p class="text-xs text-slate-400">Example prompts could not be loaded.</p>';
  }
}
loadAssistantPrompts();


function cleanAssistantText(value){
  return String(value||'')
    .replace(/^#{1,6}\s*/gm,'')
    .replace(/^\s*\*\s+/gm,'• ')
    .replace(/^\s*-\s+/gm,'• ')
    .replace(/\*\*/g,'')
    .replace(/__/g,'')
    .replace(/`/g,'')
    .replace(/\*/g,'')
    .trim();
}

function renderAssistantConversation(){
  const box=document.getElementById('assistant-answer');
  if(!box)return;
  if(!ecoTraceAssistantHistory.length){
    box.classList.add('hidden');
    box.innerHTML='';
    return;
  }
  box.classList.remove('hidden');
  box.innerHTML=ecoTraceAssistantHistory.map(turn=>{
    const user=turn.role==='user';
    return `<div class="${user?'ml-5 bg-violet-300/10':'mr-5 bg-black/20'} mb-2 rounded-xl p-3 ring-1 ring-white/10">
      <p class="mb-1 text-[9px] font-black uppercase tracking-wider ${user?'text-violet-200':'text-emerald-200'}">${user?'You':'EcoTrace Assistant'}</p>
      <p class="whitespace-pre-wrap text-xs leading-5 text-slate-200">${escapeHtml(user?turn.text:cleanAssistantText(turn.text))}</p>
    </div>`;
  }).join('');
}
function clearAssistantComposer(){
  const el=document.getElementById('assistant-question');
  if(el)el.value='';
}

let activeSavedAssistantChatId=null;

function getSavedChats(){
  try{
    const key=profileStorageKey('assistantChats');
    const parsed=JSON.parse(localStorage.getItem(key)||'[]');
    if(!Array.isArray(parsed))return [];

    let changed=false;
    const chats=parsed.map((chat,index)=>{
      if(chat && typeof chat==='object' && chat.id){
        return chat;
      }
      changed=true;
      return {
        ...(chat&&typeof chat==='object'?chat:{}),
        id:`legacy_${Date.now()}_${index}_${Math.random().toString(36).slice(2,8)}`,
        title:String(chat?.title||'Saved EcoTrace chat'),
        turns:Array.isArray(chat?.turns)?chat.turns:[],
        savedAt:chat?.savedAt||new Date().toISOString()
      };
    });

    if(changed){
      localStorage.setItem(key,JSON.stringify(chats.slice(-30)));
    }
    return chats;
  }catch(_){
    return [];
  }
}

function setSavedChats(chats){
  localStorage.setItem(
    profileStorageKey('assistantChats'),
    JSON.stringify((chats||[]).slice(-30))
  );
}

function assistantChatTitle(turns){
  const first=(turns||[]).find(turn=>turn.role==='user')?.text?.trim()||'New EcoTrace chat';
  return first.length>42?first.slice(0,42)+'…':first;
}

function renderAssistantConversation(){
  const box=document.getElementById('assistant-answer');
  const title=document.getElementById('assistant-current-title');
  if(!box)return;

  title.textContent=assistantChatTitle(ecoTraceAssistantHistory);

  if(!ecoTraceAssistantHistory.length){
    box.innerHTML=`
      <div id="assistant-empty-state" class="rounded-xl bg-violet-300/5 p-3 text-[11px] leading-5 text-slate-400">
        Ask anything about EcoTrace. Follow-up questions stay in this conversation.
      </div>`;
    return;
  }

  box.innerHTML=ecoTraceAssistantHistory.map(turn=>{
    const user=turn.role==='user';
    return `
      <div class="${user?'ml-5 bg-violet-300/10 ring-violet-300/20':'mr-5 bg-emerald-300/5 ring-emerald-300/10'} mb-2 rounded-xl p-2.5 ring-1">
        <p class="mb-1 text-[8px] font-black uppercase tracking-wider ${user?'text-violet-200':'text-emerald-200'}">
          ${user?'You':'EcoTrace Assistant'}
        </p>
        <p class="whitespace-pre-wrap text-[11px] leading-5 text-slate-200">${escapeHtml(user?turn.text:cleanAssistantText(turn.text))}</p>
      </div>`;
  }).join('');

  box.scrollTop=box.scrollHeight;
}

function clearAssistantComposer(){
  const el=document.getElementById('assistant-question');
  if(el)el.value='';
}

function refreshSavedChats(){
  const sidebar=document.getElementById('assistant-chat-sidebar');
  if(!sidebar)return;
  const chats=getSavedChats();

  if(!chats.length){
    sidebar.innerHTML='<p class="text-[10px] leading-4 text-slate-500">No saved chats yet.</p>';
    return;
  }

  sidebar.innerHTML=chats
    .map(chat=>`
      <div class="group rounded-lg ${chat.id===activeSavedAssistantChatId?'bg-violet-300/15 ring-violet-300/30':'bg-white/5 ring-white/10'} p-1.5 ring-1">
        <button type="button"
          class="assistant-load-chat block w-full truncate text-left text-[10px] font-semibold ${chat.id===activeSavedAssistantChatId?'text-violet-100':'text-slate-300'}"
          data-chat-id="${escapeHtml(chat.id)}"
          title="${escapeHtml(chat.title||'Saved chat')}">
          ${escapeHtml(chat.title||'Saved chat')}
        </button>
        <div class="mt-1 flex items-center justify-between gap-1">
          <span class="truncate text-[8px] text-slate-600">${chat.savedAt?new Date(chat.savedAt).toLocaleDateString():''}</span>
          <button type="button"
            class="assistant-delete-chat rounded px-1 text-[9px] font-bold text-red-300 hover:bg-red-300/10"
            data-chat-id="${escapeHtml(chat.id)}"
            title="Delete saved chat">Delete</button>
        </div>
      </div>`)
    .join('');

  sidebar.querySelectorAll('.assistant-load-chat').forEach(button=>{
    button.addEventListener('click',()=>{
      const chats=getSavedChats();
      const chat=chats.find(item=>item.id===button.dataset.chatId);
      if(!chat)return;
      activeSavedAssistantChatId=chat.id;
      ecoTraceAssistantHistory=Array.isArray(chat.turns)?chat.turns:[];
      clearAssistantComposer();
      renderAssistantConversation();
      refreshSavedChats();
      document.getElementById('assistant-chat-status').textContent='Saved chat loaded — continue with a follow-up.';
    });
  });

  sidebar.querySelectorAll('.assistant-delete-chat').forEach(button=>{
    button.addEventListener('click',event=>{
      event.preventDefault();
      event.stopPropagation();

      const deletingId=String(button.dataset.chatId||'');
      const before=getSavedChats();
      const after=before.filter(item=>String(item.id)!==deletingId);

      if(!deletingId || after.length===before.length){
        document.getElementById('assistant-chat-status').textContent=
          'Could not identify that saved chat. Reload once and try again.';
        return;
      }

      setSavedChats(after);

      if(String(activeSavedAssistantChatId||'')===deletingId){
        activeSavedAssistantChatId=null;
        ecoTraceAssistantHistory=[];
        clearAssistantComposer();
        renderAssistantConversation();
      }

      refreshSavedChats();
      const persisted=getSavedChats();
      const stillExists=persisted.some(item=>String(item.id)===deletingId);
      document.getElementById('assistant-chat-status').textContent=
        stillExists?'Delete did not persist — try again.':'Saved chat permanently deleted.';
    });
  });
}

function saveCurrentAssistantChat(){
  if(!ecoTraceAssistantHistory.length){
    document.getElementById('assistant-chat-status').textContent='Ask at least one question before saving.';
    return;
  }

  let chats=getSavedChats();
  const now=new Date().toISOString();
  const title=assistantChatTitle(ecoTraceAssistantHistory);

  if(activeSavedAssistantChatId){
    const index=chats.findIndex(chat=>chat.id===activeSavedAssistantChatId);
    if(index>=0){
      chats[index]={
        ...chats[index],
        title,
        turns:ecoTraceAssistantHistory.slice(-30),
        savedAt:now
      };
    }else{
      activeSavedAssistantChatId=null;
    }
  }

  if(!activeSavedAssistantChatId){
    activeSavedAssistantChatId=`chat_${Date.now()}_${Math.random().toString(36).slice(2,8)}`;
    chats.push({
      id:activeSavedAssistantChatId,
      title,
      turns:ecoTraceAssistantHistory.slice(-30),
      savedAt:now
    });
  }

  setSavedChats(chats);
  refreshSavedChats();
  document.getElementById('assistant-chat-status').textContent='Chat saved.';
}

document.getElementById('assistant-new-chat').addEventListener('click',()=>{
  activeSavedAssistantChatId=null;
  activeSavedAssistantChatId=null;
  ecoTraceAssistantHistory=[];
  clearAssistantComposer();
  renderAssistantConversation();
  refreshSavedChats();
  refreshSavedChats();
  document.getElementById('assistant-chat-status').textContent='New chat started.';
});

document.getElementById('assistant-save-chat').addEventListener('click',saveCurrentAssistantChat);

document.getElementById('assistant-question').addEventListener('keydown',event=>{
  if(event.key==='Enter'&&!event.shiftKey){
    event.preventDefault();
    document.getElementById('assistant-submit').click();
  }
});

refreshSavedChats();
renderAssistantConversation();

const assistantButton=document.getElementById('assistant-submit');
if(assistantButton){
  assistantButton.addEventListener('click',async()=>{
    const chatGeneration=window.ecoSessionState.chatGeneration;
    const answer=document.getElementById('assistant-answer');
    const question=document.getElementById('assistant-question').value.trim();

    if(!question){
      answer.classList.remove('hidden');
      answer.textContent='Enter a question first.';
      return;
    }

    const pendingHistory=[...ecoTraceAssistantHistory,{role:'user',text:question}];
    answer.innerHTML=pendingHistory.map(turn=>{
      const user=turn.role==='user';
      return `<div class="${user?'ml-5 bg-violet-300/10':'mr-5 bg-emerald-300/5'} mb-2 rounded-xl p-2.5 ring-1 ring-white/10">
        <p class="mb-1 text-[8px] font-black uppercase tracking-wider ${user?'text-violet-200':'text-emerald-200'}">${user?'You':'EcoTrace Assistant'}</p>
        <p class="whitespace-pre-wrap text-[11px] leading-5 text-slate-200">${escapeHtml(user?turn.text:cleanAssistantText(turn.text))}</p>
      </div>`;
    }).join('')+'<p class="px-2 text-[10px] text-violet-200">EcoTrace Assistant is thinking…</p>';
    answer.scrollTop=answer.scrollHeight;
    assistantButton.disabled=true;

    try{
      const response=await fetchWithTimeout(
        '/api/gemini-assistant/ask',
        {
          method:'POST',
          headers:{'Content-Type':'application/json'},
          body:JSON.stringify({
            question,
            active_panel:activeToolPanel,
            route:window.latestEcoTraceRoute||null,
            bike_route:window.latestEcoTraceBikeRoute||null,
            analysis:window.latestEcoTraceAnalysis||null,
            ewaste_photo:latestGeminiEwasteResult||null,
            selected_center:latestSelectedEwasteCenter||null,
            route_planting:window.latestEcoTraceRoutePlantingImpact||null,
            bike_validation:window.latestBikeValidationContext||null,
            community_context:window.latestCommunityContext||null,
            tree_guide:window.latestTreeGuideContext||null,
            guide_context:window.latestGuideContext||null,
            custom_inputs:refreshCustomInputs(),
            recent_turns:ecoTraceAssistantHistory.slice(-20)
          })
        },
        90000
      );

      const data=await readApiResponse(response);
      if(chatGeneration!==window.ecoSessionState.chatGeneration)return;
      ecoTraceAssistantHistory.push(
        {role:'user',text:question},
        {role:'assistant',text:cleanAssistantText(data.answer)}
      );
      ecoTraceAssistantHistory=ecoTraceAssistantHistory.slice(-20);
      clearAssistantComposer();
      renderAssistantConversation();

      // If this conversation was already saved, keep the saved version current.
      if(activeSavedAssistantChatId){
        let chats=getSavedChats();
        const index=chats.findIndex(chat=>chat.id===activeSavedAssistantChatId);
        if(index>=0){
          chats[index]={
            ...chats[index],
            title:assistantChatTitle(ecoTraceAssistantHistory),
            turns:ecoTraceAssistantHistory.slice(-30),
            savedAt:new Date().toISOString()
          };
          setSavedChats(chats);
          refreshSavedChats();
        }
      }

      document.getElementById('assistant-chat-status').textContent=
        activeSavedAssistantChatId
          ?'Answer added and saved chat updated.'
          :'Answer added — ask a follow-up or save this chat.';
    }catch(error){
      if(chatGeneration!==window.ecoSessionState.chatGeneration)return;
      answer.textContent=error.message||'Gemini assistant request failed.';
    }finally{
      if(chatGeneration===window.ecoSessionState.chatGeneration)assistantButton.disabled=false;
    }
  });
}


async function uploadSelectedImage(file,purpose,statusId){
  if(!file)return null;
  if(file.size>5*1024*1024)throw new Error('Image must be 5 MB or smaller.');
  if(!['image/jpeg','image/png','image/webp'].includes(file.type)){
    throw new Error('Use JPG, PNG, or WebP.');
  }
  document.getElementById(statusId).textContent='Uploading photo…';
  const dataUrl=await new Promise((resolve,reject)=>{
    const reader=new FileReader();
    reader.onload=()=>resolve(reader.result);
    reader.onerror=()=>reject(new Error('Photo could not be read.'));
    reader.readAsDataURL(file);
  });
  const response=await fetchWithTimeout('/api/uploads/image',{
    method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({data_url:dataUrl,purpose})
  },30000);
  const result=await readApiResponse(response);
  document.getElementById(statusId).textContent='Photo uploaded.';
  return result.image_url;
}

function configureDropZone(dropId,inputId,previewId,onFile,statusId){
  const drop=document.getElementById(dropId);
  const input=document.getElementById(inputId);
  const preview=document.getElementById(previewId);
  const select=file=>{
    if(!file)return;
    onFile(file);
    preview.src=URL.createObjectURL(file);
    preview.classList.remove('hidden');
    const wrap=document.getElementById(`${previewId}-wrap`);
    if(wrap)wrap.classList.remove('hidden');
    document.getElementById(statusId).textContent=`Selected ${file.name}`;
  };
  drop.addEventListener('click',()=>input.click());
  input.addEventListener('change',()=>select(input.files[0]));
  for(const name of ['dragenter','dragover']){
    drop.addEventListener(name,event=>{event.preventDefault();drop.classList.add('border-emerald-300');});
  }
  for(const name of ['dragleave','drop']){
    drop.addEventListener(name,event=>{event.preventDefault();drop.classList.remove('border-emerald-300');});
  }
  drop.addEventListener('drop',event=>select(event.dataTransfer.files[0]));
}

configureDropZone('community-photo-drop','community-photo-file','community-photo-preview',file=>selectedCommunityFile=file,'community-upload-status');
document.getElementById('community-photo-remove').addEventListener('click',()=>{
  selectedCommunityFile=null;
  document.getElementById('community-photo-file').value='';
  document.getElementById('community-photo-preview-wrap').classList.add('hidden');
  document.getElementById('community-upload-status').textContent='';
  document.getElementById('community-image-url').value='';
});
configureDropZone('ewaste-photo-drop','ewaste-photo-file','ewaste-photo-preview',file=>selectedEwasteFile=file,'ewaste-listing-status');
configureDropZone(
  'gemini-ewaste-photo-drop',
  'gemini-ewaste-photo-file',
  'gemini-ewaste-photo-preview',
  file=>selectedGeminiEwasteFile=file,
  'gemini-ewaste-status'
);
document.getElementById('gemini-ewaste-photo-file').addEventListener('change',()=>{
  const file=document.getElementById('gemini-ewaste-photo-file').files[0];
  if(!file)return;
  document.getElementById('gemini-ewaste-photo-preview-wrap').classList.remove('hidden');
});
document.getElementById('gemini-ewaste-photo-drop').addEventListener('drop',()=>{
  setTimeout(()=>{
    if(selectedGeminiEwasteFile){
      document.getElementById('gemini-ewaste-photo-preview-wrap').classList.remove('hidden');
    }
  },0);
});
document.getElementById('gemini-ewaste-photo-remove').addEventListener('click',()=>{
  selectedGeminiEwasteFile=null;
  latestGeminiEwasteResult=null;
  document.getElementById('gemini-ewaste-photo-file').value='';
  document.getElementById('gemini-ewaste-photo-preview-wrap').classList.add('hidden');
  document.getElementById('gemini-ewaste-result').classList.add('hidden');
  document.getElementById('gemini-ewaste-result').innerHTML='';
  document.getElementById('gemini-ewaste-status').textContent='';
});


function communityContextValue(value,suffix=''){
  return value==null?'Not available':`${value}${suffix}`;
}

async function loadCommunityPlanningContextAt(lat,lon,label='Selected location'){
  const result=document.getElementById('community-context-result');
  const status=document.getElementById('community-context-status');
  if(!result)return;

  result.classList.remove('hidden');
  result.innerHTML='<p class="text-[10px] text-slate-400">Loading U.S. Census planning context…</p>';
  if(status)status.textContent=`Analyzing ${label}…`;

  try{
    const response=await fetchWithTimeout(
      `/api/community-context/point?lat=${encodeURIComponent(lat)}&lon=${encodeURIComponent(lon)}`,
      {},
      30000
    );
    const data=await readApiResponse(response);
    window.latestCommunityContext={...data,latitude:lat,longitude:lon,label};

    const level=data.community_context_level||'Moderate';
    const levelClass=level==='Higher'
      ?'text-amber-200 bg-amber-300/10'
      :level==='Lower'
        ?'text-emerald-200 bg-emerald-300/10'
        :'text-violet-200 bg-violet-300/10';

    result.innerHTML=`
      <div class="rounded-xl bg-black/20 p-3 ring-1 ring-white/10">
        <div class="flex items-center justify-between gap-2">
          <div>
            <p class="text-[9px] text-slate-500">${escapeHtml(label)}</p>
            <p class="text-[10px] font-black text-white">${escapeHtml(data.geography||'Census tract')}</p>
          </div>
          <span class="rounded-full px-2 py-1 text-[9px] font-black ${levelClass}">
            ${escapeHtml(level)} context
          </span>
        </div>
        <div class="mt-2 grid grid-cols-2 gap-2 text-[10px]">
          <div class="rounded-lg bg-white/5 p-2"><span class="text-slate-500">Median household income</span><br><strong class="text-slate-200">${data.median_household_income==null?'N/A':'$'+Number(data.median_household_income).toLocaleString()}</strong></div>
          <div class="rounded-lg bg-white/5 p-2"><span class="text-slate-500">Poverty</span><br><strong class="text-slate-200">${communityContextValue(data.poverty_percent,'%')}</strong></div>
          <div class="rounded-lg bg-white/5 p-2"><span class="text-slate-500">No-vehicle households</span><br><strong class="text-slate-200">${communityContextValue(data.households_without_vehicle_percent,'%')}</strong></div>
          <div class="rounded-lg bg-white/5 p-2"><span class="text-slate-500">Age 65+</span><br><strong class="text-slate-200">${communityContextValue(data.age_65_plus_percent,'%')}</strong></div>
        </div>
        <div class="mt-2 rounded-lg bg-orange-300/5 p-2 ring-1 ring-orange-300/10">
          <p class="text-[9px] font-black uppercase tracking-wider text-orange-200">Use with environmental evidence</p>
          <p class="mt-1 text-[9px] leading-4 text-slate-400">
            Compare this context with EcoTrace heat exposure and existing canopy. Do not combine socioeconomic indicators into a single investment or eligibility score.
          </p>
        </div>
        <p class="mt-2 text-[9px] leading-4 text-slate-500">${escapeHtml(data.interpretation||'Planning context only.')}</p>
        <p class="mt-1 text-[9px] text-violet-300">${escapeHtml(data.source||'U.S. Census ACS')}</p>
      </div>`;

    if(status)status.textContent='Community context loaded';
  }catch(error){
    window.latestCommunityContext=null;
    result.innerHTML=`<p class="rounded-lg bg-red-400/10 p-2 text-[10px] leading-4 text-red-200">${escapeHtml(error.message||'Community context unavailable.')}</p>`;
    if(status)status.textContent='Community context error';
  }
}

document.getElementById('community-context-search')?.addEventListener('click',async()=>{
  const input=document.getElementById('community-context-address');
  const query=(input?.value||'').trim();
  if(!query){
    document.getElementById('community-context-status').textContent='Enter an address or landmark.';
    return;
  }
  try{
    document.getElementById('community-context-status').textContent='Finding address…';
    const place=await geocodeAddress(query);
    const lat=Number(place.latitude);
    const lon=Number(place.longitude);
    if(!Number.isFinite(lat)||!Number.isFinite(lon))throw new Error('Address did not return valid coordinates.');

    if(communityContextMarker)communityContextMarker.remove();
    communityContextMarker=new maplibregl.Marker({color:'#a78bfa'})
      .setLngLat([lon,lat])
      .setPopup(new maplibregl.Popup({offset:16}).setText('Community context location'))
      .addTo(map);

    map.flyTo({center:[lon,lat],zoom:15,essential:true});
    await loadCommunityPlanningContextAt(lat,lon,place.display_name||query);
  }catch(error){
    document.getElementById('community-context-status').textContent=error.message||'Could not find address.';
  }
});

document.getElementById('community-context-address')?.addEventListener('keydown',event=>{
  if(event.key==='Enter'){
    event.preventDefault();
    document.getElementById('community-context-search')?.click();
  }
});

document.getElementById('community-context-map-point')?.addEventListener('click',()=>{
  communityContextSelectionMode=true;
  document.getElementById('community-context-status').textContent='Click a location on the map…';
  document.getElementById('status-pill').textContent='Community context — choose a map point';
});

document.getElementById('community-context-center')?.addEventListener('click',async()=>{
  const center=map.getCenter();
  await loadCommunityPlanningContextAt(center.lat,center.lng,'Current map center');
});

const COMMUNITY_POST_TOKENS=JSON.parse(localStorage.getItem('ecotraceCommunityPostTokens')||'{}');
const COMMUNITY_COMMENT_TOKENS=JSON.parse(localStorage.getItem('ecotraceCommunityCommentTokens')||'{}');
function saveCommunityOwnerTokens(){
  localStorage.setItem('ecotraceCommunityPostTokens',JSON.stringify(COMMUNITY_POST_TOKENS));
  localStorage.setItem('ecotraceCommunityCommentTokens',JSON.stringify(COMMUNITY_COMMENT_TOKENS));
}
function ownsCommunityPost(postId){const p=latestCommunityPosts.find(p=>String(p.id)===String(postId));return Boolean(currentEcoTraceAccount&&(p?.can_edit||(p?.legacy_author_token&&COMMUNITY_POST_TOKENS[String(postId)])));}
const communityCommentPermissions=new Map();
function ownsCommunityComment(commentId){const p=communityCommentPermissions.get(String(commentId));return Boolean(currentEcoTraceAccount&&(p?.can_edit||(p?.legacy_author_token&&COMMUNITY_COMMENT_TOKENS[String(commentId)])));}

const COMMUNITY_TAB_CONFIG={
  feed:{
    title:'Current Posts',
    help:'Browse current posts and upcoming events. Past events are in Archive.',
    section:'',
    category:'general',
    createTitle:'Create a community post',
    createHelp:'Share an environmental update, question, or local idea.'
  },
  trees:{
    title:'Tree Suggestions',
    help:'Resident-proposed places that may need shade or canopy.',
    section:'trees',
    category:'tree_suggestion',
    createTitle:'Suggest a tree location',
    createHelp:'Choose a map point, add an optional picture, and explain why shade is needed.'
  },
  events:{
    title:'Events & Campaigns',
    help:'Join or host park cleanups and environmental campaigns.',
    section:'events',
    category:'park_cleanup',
    createTitle:'Host an environmental event',
    createHelp:'Create a park cleanup or environmental campaign with a date and participant limit.'
  },
  archive:{
    title:'Environmental Event Archive',
    help:'Review past, completed, and cancelled environmental events.',
    section:'events',
    category:'park_cleanup',
    createTitle:'Archived environmental events',
    createHelp:'Past environmental events remain here as a record of community activity.'
  },
  discussions:{
    title:'Environmental Discussions',
    help:'Ask questions and reply to local environmental ideas.',
    section:'discussions',
    category:'discussion',
    createTitle:'Start a discussion',
    createHelp:'Start a topic about heat, canopy, waste, bikes, wildlife, water, or local sustainability.'
  }
};

const PROFILE_AUTOFILL_KEY='ecotraceOriginalProfileAutofillV1';
let currentEcoTraceAccount=null;
let profileReturnPanel='heat-panel';

function accountRoleLabel(value){
  return {
    student:'Student',adult_volunteer:'Adult volunteer',organizer:'Organizer',
    school_staff:'School / teacher / staff',nonprofit:'Nonprofit / community organization',
    government:'Government / public agency',business:'Business / company',other:'Other'
  }[value]||value||'Not shared';
}

async function loadOriginalProfile(){
  const previousUsername=currentEcoTraceAccount?.username;
  try{
    currentEcoTraceAccount=await readApiResponse(await fetchWithTimeout('/api/profile/me',{},8000));
  }catch(_){
    currentEcoTraceAccount=null;
  }
  renderOriginalProfile();
  if(previousUsername!==currentEcoTraceAccount?.username){
    document.querySelectorAll('.community-edit-post,.community-delete-post,.community-edit-comment,.community-delete-comment,.community-complete-event,.ewaste-event-done,.ewaste-owner-action').forEach(e=>e.hidden=true);
    loadCommunityPosts();loadEwasteListings();loadEwasteEvents();
  }
}

function renderOriginalProfile(){
  const signed=Boolean(currentEcoTraceAccount);
  const display=signed
    ?(currentEcoTraceAccount.display_name||currentEcoTraceAccount.full_name||currentEcoTraceAccount.username)
    :'Guest';

  const header=document.getElementById('current-profile-label');
  if(header)header.textContent=display;
  const switchButton=document.getElementById('header-switch-profile');
  if(switchButton)switchButton.textContent=signed?'Switch account':'Sign in';

  document.getElementById('profile-signedin-settings')?.classList.toggle('hidden',!signed);
  document.getElementById('profile-guest-settings')?.classList.toggle('hidden',signed);
  document.getElementById('profile-switch-confirm')?.classList.add('hidden');

  const summary=document.getElementById('profile-account-summary');
  if(summary)summary.textContent=signed
    ?`Signed in as ${display} (@${currentEcoTraceAccount.username}).`
    :'You are using EcoTrace as a guest.';

  if(signed){
    document.getElementById('profile-username').value=currentEcoTraceAccount.username||'';
    document.getElementById('profile-full-name').value=currentEcoTraceAccount.full_name||'';
    document.getElementById('profile-nickname').value=currentEcoTraceAccount.nickname||'';
    document.getElementById('profile-display-name').value=currentEcoTraceAccount.display_name||'';
    document.getElementById('profile-role').value=currentEcoTraceAccount.role||'student';
    document.getElementById('profile-organization').value=currentEcoTraceAccount.organization||'';
    document.getElementById('profile-education-work').value=currentEcoTraceAccount.education_work||'';
    document.getElementById('profile-bio').value=currentEcoTraceAccount.bio||'';
    document.getElementById('profile-share-history').checked=Boolean(currentEcoTraceAccount.share_history);
    document.getElementById('profile-autofill').checked=localStorage.getItem(PROFILE_AUTOFILL_KEY)!=='0';
  }

  applyOriginalProfileAutofill();
  applyPostPermissions();
}

function applyOriginalProfileAutofill(root=document){
  if(!currentEcoTraceAccount||localStorage.getItem(PROFILE_AUTOFILL_KEY)==='0')return;
  const name=currentEcoTraceAccount.display_name||currentEcoTraceAccount.full_name||currentEcoTraceAccount.username||'';
  const role=currentEcoTraceAccount.role||'student';
  const org=currentEcoTraceAccount.organization||currentEcoTraceAccount.education_work||'';

  const communityName=document.getElementById('community-name');
  if(communityName){
    communityName.placeholder=name||'Your name';
    if(!communityName.value)communityName.value=name;
  }
  const communityRole=document.getElementById('community-role');
  if(communityRole)communityRole.value=role;
  const affiliation=document.getElementById('community-affiliation');
  if(affiliation&&!affiliation.value)affiliation.value=org;
  const share=document.getElementById('community-share-history');
  if(share)share.checked=Boolean(currentEcoTraceAccount.share_history);

  root.querySelectorAll?.('.community-signedin-join').forEach(el=>el.classList.remove('hidden'));
  root.querySelectorAll?.('.community-guest-join').forEach(el=>el.classList.add('hidden'));
  root.querySelectorAll?.('.community-join-self-name').forEach(el=>el.textContent=name);
  root.querySelectorAll?.('.community-join-self-meta').forEach(el=>el.textContent=[
    accountRoleLabel(role),org
  ].filter(Boolean).join(' • '));

  const ewHost=document.getElementById('ewaste-event-host');
  if(ewHost&&!ewHost.value)ewHost.value=name;
  const ewListing=document.getElementById('ewaste-listing-name');
  if(ewListing&&!ewListing.value)ewListing.value=name;
}

function openOriginalAccountSettings(){
  profileReturnPanel=activeToolPanel==='profile-panel'?'heat-panel':activeToolPanel;
  showToolPanel('profile-panel',{preserveMap:true});
  renderOriginalProfile();
}

document.getElementById('current-profile-label')?.addEventListener('click',openOriginalAccountSettings);

function openSwitchAccountModal(){
  if(!currentEcoTraceAccount){
    window.location.href='/';
    return;
  }
  const modal=document.getElementById('switch-account-modal');
  document.getElementById('switch-account-username').value='';
  document.getElementById('switch-account-password').value='';
  document.getElementById('switch-account-status').textContent='';
  modal.classList.remove('hidden');
  modal.classList.add('flex');
  setTimeout(()=>document.getElementById('switch-account-username')?.focus(),0);
}
function closeSwitchAccountModal(){
  const modal=document.getElementById('switch-account-modal');
  modal?.classList.add('hidden');
  modal?.classList.remove('flex');
  document.getElementById('switch-account-status').textContent='';
}
async function submitSwitchAccount(){
  const username=document.getElementById('switch-account-username').value.trim();
  const password=document.getElementById('switch-account-password').value;
  const status=document.getElementById('switch-account-status');
  const button=document.getElementById('switch-account-submit');
  if(!username||!password){
    status.textContent='Enter the username and password for the account you want to use.';
    return;
  }
  button.disabled=true;
  button.textContent='Switching…';
  status.textContent='Checking account…';
  try{
    const result=await readApiResponse(await fetchWithTimeout('/api/profile/switch-account',{
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({username,password})
    },12000));
    window.location.replace('/app?mode=profile');
    return;
    closeSwitchAccountModal();
    document.getElementById('status-pill').textContent=`Switched to ${currentEcoTraceAccount?.display_name||result.username}.`;
  }catch(error){
    status.textContent=error.message||'Could not switch accounts.';
  }finally{
    button.disabled=false;
    button.textContent='Switch account';
  }
}
document.getElementById('header-switch-profile')?.addEventListener('click',openSwitchAccountModal);
document.getElementById('profile-switch-account')?.addEventListener('click',openSwitchAccountModal);
document.getElementById('switch-account-cancel')?.addEventListener('click',closeSwitchAccountModal);
document.getElementById('switch-account-cancel-top')?.addEventListener('click',closeSwitchAccountModal);
document.getElementById('switch-account-submit')?.addEventListener('click',submitSwitchAccount);
document.getElementById('switch-account-password')?.addEventListener('keydown',event=>{
  if(event.key==='Enter')submitSwitchAccount();
});
document.getElementById('switch-account-modal')?.addEventListener('click',event=>{
  if(event.target.id==='switch-account-modal')closeSwitchAccountModal();
});

document.getElementById('profile-back')?.addEventListener('click',()=>{
  showToolPanel(profileReturnPanel||'heat-panel',{preserveMap:true});
});

document.getElementById('profile-display-full')?.addEventListener('click',()=>{
  document.getElementById('profile-display-name').value=document.getElementById('profile-full-name').value.trim();
});
document.getElementById('profile-display-nickname')?.addEventListener('click',()=>{
  const nick=document.getElementById('profile-nickname').value.trim();
  if(!nick){alert('Add a nickname first.');return;}
  document.getElementById('profile-display-name').value=nick;
});
document.getElementById('profile-display-custom')?.addEventListener('click',()=>{
  document.getElementById('profile-display-name').focus();
});

document.getElementById('profile-save')?.addEventListener('click',async()=>{
  const status=document.getElementById('profile-save-status');
  status.textContent='Saving…';
  try{
    currentEcoTraceAccount=await readApiResponse(await fetchWithTimeout('/api/profile/me',{
      method:'PATCH',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({
        full_name:document.getElementById('profile-full-name').value.trim(),
        nickname:document.getElementById('profile-nickname').value.trim()||null,
        display_name:safeDisplayName(document.getElementById('profile-display-name').value),
        role:document.getElementById('profile-role').value,
        organization:document.getElementById('profile-organization').value.trim()||null,
        education_work:document.getElementById('profile-education-work').value.trim()||null,
        bio:document.getElementById('profile-bio').value.trim()||null,
        share_history:document.getElementById('profile-share-history').checked
      })
    },12000));
    localStorage.setItem(PROFILE_AUTOFILL_KEY,document.getElementById('profile-autofill').checked?'1':'0');
    status.textContent='Profile saved.';
    renderOriginalProfile();
  }catch(error){status.textContent=error.message||'Could not save profile.';}
});

document.getElementById('profile-autofill')?.addEventListener('change',event=>{
  localStorage.setItem(PROFILE_AUTOFILL_KEY,event.target.checked?'1':'0');
  if(event.target.checked)applyOriginalProfileAutofill();
});

document.getElementById('profile-change-password')?.addEventListener('click',async()=>{
  const status=document.getElementById('profile-password-status');
  const current=document.getElementById('profile-current-password').value;
  const next=document.getElementById('profile-new-password').value;
  if(!current||next.length<8){
    status.textContent='Enter your current password and a new password with at least 8 characters.';
    return;
  }
  status.textContent='Changing password…';
  try{
    await readApiResponse(await fetchWithTimeout('/api/profile/change-password',{
      method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({current_password:current,new_password:next})
    },12000));
    document.getElementById('profile-current-password').value='';
    document.getElementById('profile-new-password').value='';
    status.textContent='Password changed.';
  }catch(error){status.textContent=error.message||'Could not change password.';}
});

loadOriginalProfile();


let activeCommunityTab='feed';

function communityCategoryLabel(category){
  const labels={
    general:'General post',
    tree_suggestion:'Tree suggestion',
    park_cleanup:'Park cleanup',
    environmental_campaign:'Environmental campaign',
    discussion:'Discussion',
    heat_report:'Heat/shade report'
  };
  return labels[category]||category;
}

function communityEscapeDate(value){
  if(!value)return '';
  const date=new Date(value);
  return Number.isNaN(date.getTime())
    ?escapeHtml(value)
    :date.toLocaleString();
}

function isCommunityEvent(category){
  return category==='park_cleanup'||category==='environmental_campaign';
}

function setCommunityTab(tab){
  const activityCard=document.getElementById('community-activity-card');if(activityCard)activityCard.classList.toggle('hidden',tab!=='activity');if(tab==='activity')renderCommunityActivity();
  const impactCard=document.getElementById('community-impact-card');if(impactCard)impactCard.classList.toggle('hidden',tab!=='impact');if(tab==='impact')renderImpactHistory();
  const validationCard=document.getElementById('community-validation-card');if(validationCard)validationCard.classList.toggle('hidden',tab!=='validation');if(tab==='validation')renderValidation();

  activeCommunityTab=tab;
  const config=COMMUNITY_TAB_CONFIG[tab]||COMMUNITY_TAB_CONFIG.feed;

  document.querySelectorAll('.community-subtab').forEach(button=>{
    const active=button.dataset.communityTab===tab;
    button.classList.toggle('active',active);
    button.classList.toggle('bg-amber-400',active);
    button.classList.toggle('text-amber-950',active);
    button.classList.toggle('text-slate-300',!active);
  });

  document.getElementById('community-list-title').textContent=config.title;
  document.getElementById('community-list-help').textContent=config.help;
  document.getElementById('community-create-title').textContent=config.createTitle;
  document.getElementById('community-create-help').textContent=config.createHelp;
  document.getElementById('community-category').value=config.category;
  const archiveMode=tab==='archive';
  document.getElementById('community-create-card').classList.toggle('hidden',tab!=='feed');
  document.getElementById('community-create-toggle').classList.toggle('hidden',archiveMode);
  document.getElementById('community-create-toggle').textContent=tab==='feed'
    ?'+ Create a new post'
    :`+ Create ${config.title}`;
  document.getElementById('community-all-filter').classList.toggle('hidden',tab!=='feed');
  document.getElementById('community-sort').classList.toggle('hidden',tab!=='feed');
  updateCommunityFormFields();
  applyPostPermissions();
  loadCommunityPosts();
}

function updateCommunityFormFields(){
  const category=document.getElementById('community-category').value;
  const treeMode=category==='tree_suggestion';
  const eventMode=isCommunityEvent(category);

  document.getElementById('community-tree-reason-wrap').classList.toggle('hidden',!treeMode);
  document.getElementById('community-event-fields').classList.toggle('hidden',!eventMode);

  const mapButton=document.getElementById('community-use-map-point');
  mapButton.textContent=treeMode
    ?'Choose required map point'
    :'Use map point';

  if(treeMode){
    document.getElementById('community-title').placeholder='Example: Tree needed near this bus stop';
  }else if(eventMode){
    document.getElementById('community-title').placeholder='Example: Saturday park cleanup';
  }else if(category==='discussion'){
    document.getElementById('community-title').placeholder='Example: How can we increase shade near schools?';
  }else{
    document.getElementById('community-title').placeholder='Title';
  }
}


function ecoPacificDay(value){
  const parts=new Intl.DateTimeFormat('en-US',{timeZone:'America/Los_Angeles',year:'numeric',month:'2-digit',day:'2-digit'}).formatToParts(value);
  const get=k=>parts.find(p=>p.type===k).value;
  return `${get('year')}-${get('month')}-${get('day')}`;
}
function ecoCalendarDay(y,m,d){
  y=Number(y);m=Number(m);d=Number(d);
  if(y<1000||y>9999||m<1||m>12||d<1||d>31)return null;
  const test=new Date(Date.UTC(y,m-1,d));
  if(test.getUTCFullYear()!==y||test.getUTCMonth()+1!==m||test.getUTCDate()!==d)return null;
  return `${y}-${String(m).padStart(2,'0')}-${String(d).padStart(2,'0')}`;
}
function ecoActivityDay(raw){
  if(typeof raw!=='string')return null;
  const s=raw.trim().replace(/\s+/g,' ');
  let m;
  if(/^\d{4}-\d{2}-\d{2}[T ]/.test(s)&&/(Z|[+-]\d{2}:?\d{2})$/i.test(s)){
    const instant=new Date(s);
    return Number.isFinite(instant.getTime())?ecoPacificDay(instant):null;
  }
  m=s.match(/^(\d{4})-(\d{2})-(\d{2})(?:[T ](?:[01]\d|2[0-3]):[0-5]\d(?::[0-5]\d(?:\.\d+)?)?)?$/);
  if(m)return ecoCalendarDay(m[1],m[2],m[3]);
  m=s.match(/^(\d{1,2})\/(\d{1,2})\/(\d{4})(?:(?:,? )(?:\d{1,2}):\d{2}(?::\d{2})?(?: [AP]M)?)?$/i);
  if(m)return ecoCalendarDay(m[3],m[1],m[2]);
  const months=['jan','feb','mar','apr','may','jun','jul','aug','sep','oct','nov','dec'];
  m=s.match(/^([A-Za-z]+) (\d{1,2}), (\d{4})$/);
  if(m)return ecoCalendarDay(m[3],months.indexOf(m[1].slice(0,3).toLowerCase())+1,m[2]);
  m=s.match(/^(\d{1,2}) ([A-Za-z]+) (\d{4})$/);
  if(m)return ecoCalendarDay(m[3],months.indexOf(m[2].slice(0,3).toLowerCase())+1,m[1]);
  return null;
}
function ecoEventPastDate(event,now=new Date()){
  if(event.auto_archived||event.event_status==='archived')return true;
  const day=ecoActivityDay(event.event_date);
  return day!==null && day<ecoPacificDay(now);
}
function ecoEventArchived(event){
  return ['completed','cancelled','archived'].includes(event.event_status)||ecoEventPastDate(event);
}

let ecoArchiveRefreshing=false;
async function ecoRefreshEventArchives(){
  if(document.hidden||ecoArchiveRefreshing)return;
  ecoArchiveRefreshing=true;
  try{
    if(document.getElementById('community-post-list')?.offsetParent && ['feed','events','archive'].includes(activeCommunityTab))await loadCommunityPosts();
    if(document.getElementById('ewaste-event-list')?.offsetParent)await loadEwasteEvents();
    if(document.getElementById('ewaste-archive-list')?.offsetParent)await loadEwasteArchive();
  }finally{ecoArchiveRefreshing=false;}
}
setInterval(()=>{ecoRefreshEventArchives().catch(()=>{});},60000);
document.addEventListener('visibilitychange',()=>{ecoRefreshEventArchives().catch(()=>{});});


function ecoCommunityVisiblePosts(posts,tab){
  if(tab==='events')return posts.filter(p=>isCommunityEvent(p.category)&&!ecoEventArchived(p));
  if(tab==='archive')return posts.filter(p=>isCommunityEvent(p.category)&&ecoEventArchived(p));
  if(tab==='feed')return posts.filter(p=>!isCommunityEvent(p.category)||!ecoEventArchived(p));
  return posts;
}

async function loadCommunityPosts(){
  if(activeCommunityTab==='activity'){renderCommunityActivity();return;}
  if(activeCommunityTab==='impact'){renderImpactHistory();return;}
  if(activeCommunityTab==='validation'){renderValidation();return;}

  const list=document.getElementById('community-post-list');
  if(!list)return;

  const config=COMMUNITY_TAB_CONFIG[activeCommunityTab]||COMMUNITY_TAB_CONFIG.feed;
  list.innerHTML=`<p class="rounded-xl bg-white/5 p-3 text-xs text-slate-400">Loading ${escapeHtml(config.title.toLowerCase())}…</p>`;

  try{
    const query=config.section
      ?`?section=${encodeURIComponent(config.section)}`
      :'';
    const response=await fetchWithTimeout(
      `/api/community/posts${query}`,
      {},
      12000
    );
    let posts=await readApiResponse(response);

    posts=ecoCommunityVisiblePosts(posts,activeCommunityTab);

    if(activeCommunityTab==='feed'){
      const category=document.getElementById('community-all-filter').value;
      const sortMode=document.getElementById('community-sort').value;
      if(category)posts=posts.filter(post=>post.category===category);
      posts=[...posts].sort((a,b)=>{
        if(sortMode==='oldest')return new Date(a.created_at)-new Date(b.created_at);
        if(sortMode==='active')return (b.comment_count+b.participant_count)-(a.comment_count+a.participant_count);
        return new Date(b.created_at)-new Date(a.created_at);
      });
    }
    latestCommunityPosts=posts;
    updateCommunityMapPoints(posts);

    if(!posts.length){
      list.innerHTML=activeCommunityTab==='archive'
        ?'<p class="rounded-xl bg-white/5 p-3 text-xs text-slate-400">No archived environmental events yet.</p>'
        :`<p class="rounded-xl bg-white/5 p-3 text-xs text-slate-400">No posts yet in ${escapeHtml(config.title)}.</p>`;
      return;
    }

    list.innerHTML='';

    for(const post of posts){
      const card=document.createElement('article');
      card.className='rounded-2xl bg-white/5 p-3 ring-1 ring-white/10';

      const coordinateText=(
        post.latitude!==null&&post.longitude!==null
          ?`${Number(post.latitude).toFixed(5)}, ${Number(post.longitude).toFixed(5)}`
          :''
      );
      const event=isCommunityEvent(post.category);
      const archivedEvent=event&&(ecoEventArchived(post));
      const participantText=event
        ?`${post.participant_count}${post.max_participants?` / ${post.max_participants}`:''} joined`
        :'';

      card.innerHTML=`
        <div class="flex items-start justify-between gap-2">
          <div>
            <div class="flex items-center gap-2">
              <p class="text-[10px] font-black uppercase tracking-wider text-amber-200">${escapeHtml(communityCategoryLabel(post.category))}</p>
              ${archivedEvent?`<span class="rounded-full bg-slate-400/10 px-2 py-0.5 text-[9px] font-bold text-slate-300">${ecoEventPastDate(post)?'Past date':post.event_status==='cancelled'?'Cancelled':'Done'}</span>`:''}
            </div>
            <h4 class="mt-1 text-sm font-black text-white">${escapeHtml(post.title)}</h4>
          </div>
          <div class="text-right">
            ${Number(post.comment_count||0)>0
              ?`<span class="whitespace-nowrap rounded-full bg-white/5 px-2 py-1 text-[10px] text-slate-400">${post.comment_count} ${Number(post.comment_count)===1?'reply':'replies'}</span>`
              :''}
            ${event?`<p class="mt-1 text-[10px] font-bold text-cyan-200">${escapeHtml(participantText)}</p>`:''}
          </div>
        </div>

        <p class="mt-2 text-xs leading-5 text-slate-300">${escapeHtml(parseCommunityStoredDetails(post.description).base)}</p>

        ${post.tree_reason?`<p class="mt-2 rounded-lg bg-emerald-400/10 p-2 text-xs leading-5 text-emerald-100"><strong>Why a tree is needed:</strong> ${escapeHtml(post.tree_reason)}</p>`:''}

        ${post.tags?.length?`<div class="mt-2 flex flex-wrap gap-1">${post.tags.map(tag=>`<span class="rounded-full bg-sky-400/10 px-2 py-1 text-[10px] text-sky-200">#${escapeHtml(tag)}</span>`).join('')}</div>`:''}

        ${post.location_text?`<p class="mt-2 text-xs text-sky-200"><strong>Location:</strong> ${escapeHtml(post.location_text)}</p>`:''}

        ${coordinateText?`<button type="button" class="community-fly-to mt-2 text-xs font-bold text-emerald-200" data-lat="${post.latitude}" data-lon="${post.longitude}">📍 Show this location on map</button>`:''}

        ${post.event_date?`<p class="mt-2 text-xs text-violet-200"><strong>When:</strong> ${escapeHtml(communityEscapeDate(post.event_date))}</p>`:''}

        ${post.image_url?`
          <details class="mt-2 rounded-lg bg-sky-400/5 p-2 ring-1 ring-sky-300/10">
            <summary class="cursor-pointer text-xs font-bold text-sky-300">View optional picture</summary>
            <img src="${escapeHtml(post.image_url)}" alt="Community post optional picture" class="mt-2 max-h-72 w-full rounded-lg object-contain" loading="lazy" />
          </details>
        `:''}

        ${event&&post.participant_names?.length?`
          <details class="mt-2 rounded-lg bg-cyan-400/10 p-2">
            <summary class="cursor-pointer text-xs font-bold text-cyan-100">View ${post.participant_names.length} signed-up people</summary>
            <div class="mt-2 flex flex-wrap gap-2">
              ${post.participant_names.map(person=>{
                const parsed=parseCommunityParticipant(person);
                return `<button type="button" class="community-profile-link rounded-full bg-cyan-300/10 px-2 py-1 text-[10px] font-bold text-cyan-100 ring-1 ring-cyan-300/20"
                  data-profile="${escapeHtml(encodeURIComponent(JSON.stringify(parsed)))}">${escapeHtml(parsed.name)}</button>`;
              }).join('')}
            </div>
            <p class="mt-2 text-[9px] leading-4 text-slate-500">Click a name to view the public prototype profile. Only voluntarily supplied role/affiliation and public event history are shown.</p>
          </details>
        `:''}
        ${event&&!archivedEvent?`
          <div class="mt-3 rounded-lg bg-cyan-400/5 p-2 ring-1 ring-cyan-300/10">
            <div class="community-signedin-join hidden">
              <p class="text-[10px] text-slate-400">You are joining as</p>
              <p class="community-join-self-name mt-1 text-sm font-black text-cyan-100">Signed-in user</p>
              <p class="community-join-self-meta mt-1 text-[10px] text-slate-500"></p>
            </div>
            <div class="community-guest-join">
              <p class="mb-2 text-[10px] text-slate-400">Not signed in? You may join as a guest using your own name.</p>
              <input class="community-join-name w-full rounded-lg border border-white/10 bg-slate-950/70 px-2 py-1.5 text-xs text-white" maxlength="80" placeholder="Your name" autocomplete="off">
              <select class="community-join-role mt-2 w-full rounded-lg border border-white/10 bg-slate-950/70 px-2 py-1.5 text-xs text-white"><option value="">Role optional</option><option>Student</option><option>Adult volunteer</option><option>School/staff</option><option>Nonprofit</option><option>Government/public agency</option><option>Business</option><option>Other</option></select>
              <input class="community-join-affiliation mt-2 w-full rounded-lg border border-white/10 bg-slate-950/70 px-2 py-1.5 text-xs text-white" maxlength="100" placeholder="School / organization / job (optional)" autocomplete="off">
              <label class="mt-2 flex items-center gap-2 text-[9px] text-slate-400"><input class="community-join-history" type="checkbox" autocomplete="off"> Share my public event history</label>
            </div>
            <div class="mt-2 grid grid-cols-2 gap-2">
              <button type="button" class="community-join rounded-lg bg-cyan-400 px-2 py-1.5 text-xs font-black text-cyan-950" data-post-id="${post.id}">Join event</button>
              <button type="button" class="community-leave rounded-lg bg-white/5 px-2 py-1.5 text-xs font-bold text-slate-300 ring-1 ring-white/10" data-post-id="${post.id}">Leave</button>
            </div>
          </div>
        `:''}

        <p class="mt-2 text-[10px] text-slate-500">Posted by ${escapeHtml(post.display_name)} • ${escapeHtml(communityEscapeDate(post.created_at))}</p>

        <details class="mt-3 rounded-xl bg-black/20 p-2">
          <summary class="cursor-pointer text-xs font-bold text-amber-100">Replies and comments</summary>
          <div class="community-comments mt-2 space-y-2" data-post-id="${post.id}">
            <p class="text-xs text-slate-500">Open to load replies.</p>
          </div>
          <div class="mt-2 grid grid-cols-[95px_1fr_auto] gap-2">
            <input class="community-comment-name rounded-lg border border-white/10 bg-slate-950/70 px-2 py-1.5 text-xs text-white" maxlength="40" placeholder="Name" autocomplete="off">
            <input class="community-comment-message rounded-lg border border-white/10 bg-slate-950/70 px-2 py-1.5 text-xs text-white" maxlength="1000" placeholder="Write a reply" autocomplete="off">
            <button type="button" class="community-comment-submit rounded-lg bg-amber-400 px-2 py-1.5 text-xs font-black text-amber-950" data-post-id="${post.id}">Reply</button>
          </div>
        </details>

        <div class="mt-3 flex flex-wrap gap-3">
          ${ownsCommunityPost(post.id)?`
            ${event&&!archivedEvent?`<button type="button" class="community-complete-event text-[10px] font-bold text-emerald-300" data-post-id="${post.id}">Mark done</button>`:''}
            <button type="button" class="community-edit-post text-[10px] font-bold text-sky-300" data-post-id="${post.id}">Edit post</button>
            <button type="button" class="community-delete-post text-[10px] font-bold text-red-300" data-post-id="${post.id}">Delete post</button>
          `:''}
          <button type="button" class="community-report text-[10px] font-bold text-red-300" data-post-id="${post.id}">Report post</button>
        </div>
      `;

      list.appendChild(card);

      const details=card.querySelector('.community-comments')?.closest('details');
      details.addEventListener('toggle',async()=>{
        if(details.open){
          await loadCommunityComments(post.id,card);
        }
      });
    }

    applyOriginalProfileAutofill(list);
    attachCommunityPostEvents(list);
    applyPostPermissions();
  }catch(error){
    list.innerHTML=`<p class="rounded-xl bg-red-400/10 p-3 text-xs text-red-200 ring-1 ring-red-300/20">${escapeHtml(error.message||'Community posts could not be loaded.')}</p>`;
  }
}

function parseCommunityParticipant(raw){
  const parts=String(raw||'').split(' • ').map(value=>value.trim()).filter(Boolean);
  return {
    raw:String(raw||''),
    name:parts[0]||'Community participant',
    role:parts[1]||'Not shared',
    affiliation:parts[2]||'Not shared',
    share_history:parts.includes('history:on')
  };
}

function communityProfileHistoryFor(name){
  const normalized=String(name||'').trim().toLowerCase();
  if(!normalized)return [];
  const rows=[];
  for(const post of (latestCommunityPosts||[])){
    if(!isCommunityEvent(post.category))continue;
    const joined=(post.participant_names||[]).some(raw=>
      parseCommunityParticipant(raw).name.trim().toLowerCase()===normalized
    );
    if(joined){
      rows.push({
        title:post.title,
        date:post.event_date||post.created_at,
        status:post.event_status||'active',
        relationship:'Participated'
      });
    }
    if(String(post.display_name||'').trim().toLowerCase()===normalized){
      rows.push({
        title:post.title,
        date:post.event_date||post.created_at,
        status:post.event_status||'active',
        relationship:'Hosted / posted'
      });
    }
  }
  return rows.slice(0,12);
}

async function openCommunityProfile(profile){
  const modal=document.getElementById('community-profile-modal');
  const body=document.getElementById('community-profile-body');
  document.getElementById('community-profile-name').textContent=profile.name;
  body.innerHTML='<p class="text-slate-500">Loading public profile…</p>';
  modal.classList.remove('hidden');
  modal.classList.add('flex');

  let accountProfile=null;
  try{
    accountProfile=await readApiResponse(await fetchWithTimeout(
      `/api/profile/public?display_name=${encodeURIComponent(profile.name)}`,
      {},8000
    ));
  }catch(_){}

  const role=accountProfile?.role||profile.role||'Not shared';
  const organization=accountProfile?.organization||profile.affiliation||'Not shared';
  const educationWork=accountProfile?.education_work||'Not shared';
  const bio=accountProfile?.bio||'Not shared';
  const historyAllowed=accountProfile
    ?Boolean(accountProfile.share_history)
    :Boolean(profile.share_history);
  const history=historyAllowed?communityProfileHistoryFor(profile.name):[];

  body.innerHTML=`
    <div class="grid grid-cols-2 gap-2">
      <div class="rounded-xl bg-white/5 p-3"><p class="text-[9px] uppercase text-slate-500">Role</p><p class="mt-1 font-bold text-white">${escapeHtml(role)}</p></div>
      <div class="rounded-xl bg-white/5 p-3"><p class="text-[9px] uppercase text-slate-500">School / work / organization</p><p class="mt-1 font-bold text-white">${escapeHtml(organization)}</p></div>
    </div>
    <div class="rounded-xl bg-white/5 p-3"><p class="text-[9px] uppercase text-slate-500">Education / work / title</p><p class="mt-1 text-slate-200">${escapeHtml(educationWork)}</p></div>
    <div class="rounded-xl bg-white/5 p-3"><p class="text-[9px] uppercase text-slate-500">About</p><p class="mt-1 text-slate-200">${escapeHtml(bio)}</p></div>
    <div class="rounded-xl bg-white/5 p-3">
      <p class="text-[9px] font-black uppercase tracking-wider text-slate-400">Public event history</p>
      <div class="mt-2 space-y-2">${!historyAllowed
        ?'<p class="text-slate-500">This person chose not to share their event history.</p>'
        :history.length?history.map(row=>`
          <div class="rounded-lg bg-black/20 p-2">
            <strong class="text-cyan-100">${escapeHtml(row.title)}</strong>
            <p class="mt-1 text-[10px] text-slate-400">${escapeHtml(row.relationship)} • ${escapeHtml(communityEscapeDate(row.date))} • ${escapeHtml(row.status)}</p>
          </div>`).join('')
        :'<p class="text-slate-500">No public event history is currently visible.</p>'}
      </div>
    </div>`;
}
document.getElementById('community-profile-close')?.addEventListener('click',()=>{
  const modal=document.getElementById('community-profile-modal');
  modal.classList.add('hidden');modal.classList.remove('flex');
});
document.getElementById('community-profile-modal')?.addEventListener('click',event=>{
  if(event.target.id==='community-profile-modal'){
    event.currentTarget.classList.add('hidden');
    event.currentTarget.classList.remove('flex');
  }
});

function closeCommunityEditModal(){
  const modal=document.getElementById('community-edit-modal');
  modal?.classList.add('hidden');
  modal?.classList.remove('flex');
  window.ecotraceEditingPost=null;
}
document.getElementById('community-edit-cancel')?.addEventListener('click',closeCommunityEditModal);
document.getElementById('community-edit-cancel-top')?.addEventListener('click',closeCommunityEditModal);
document.getElementById('community-edit-modal')?.addEventListener('click',event=>{
  if(event.target.id==='community-edit-modal')closeCommunityEditModal();
});
document.getElementById('community-edit-save')?.addEventListener('click',async()=>{
  const editing=window.ecotraceEditingPost;
  if(!editing)return;
  const title=document.getElementById('community-edit-title').value.trim();
  const description=document.getElementById('community-edit-description').value.trim();
  const status=document.getElementById('community-edit-status');
  const save=document.getElementById('community-edit-save');

  if(!title||!description){
    status.textContent='Title and description cannot be empty.';
    return;
  }

  save.disabled=true;
  save.textContent='Saving…';
  status.textContent='Saving changes…';

  try{
    const post=editing.post;
    const response=await fetchWithTimeout(
      `/api/community/posts/${encodeURIComponent(editing.id)}?edit_token=${encodeURIComponent(COMMUNITY_POST_TOKENS[editing.id])}`,
      {
        method:'PATCH',
        headers:{'Content-Type':'application/json'},
        body:JSON.stringify({
          title,
          description,
          location_text:post.location_text,
          image_url:post.image_url,
          tree_reason:post.tree_reason,
          event_date:post.event_date
        })
      },
      12000
    );
    await readApiResponse(response);
    closeCommunityEditModal();
    await loadCommunityPosts();
  }catch(error){
    status.textContent=error.message||'Post could not be updated.';
  }finally{
    save.disabled=false;
    save.textContent='Save changes';
  }
});

function closeCommunityDeleteModal(){
  const modal=document.getElementById('community-delete-modal');
  modal?.classList.add('hidden');
  modal?.classList.remove('flex');
  window.ecotraceDeletingPostId=null;
}
document.getElementById('community-delete-cancel')?.addEventListener('click',closeCommunityDeleteModal);
document.getElementById('community-delete-modal')?.addEventListener('click',event=>{
  if(event.target.id==='community-delete-modal')closeCommunityDeleteModal();
});
document.getElementById('community-delete-confirm')?.addEventListener('click',async()=>{
  const id=window.ecotraceDeletingPostId;
  if(!id)return;
  const button=document.getElementById('community-delete-confirm');
  const status=document.getElementById('community-delete-status');
  button.disabled=true;
  button.textContent='Deleting…';
  status.textContent='Deleting post…';
  try{
    await readApiResponse(await fetchWithTimeout(
      `/api/community/posts/${encodeURIComponent(id)}?edit_token=${encodeURIComponent(COMMUNITY_POST_TOKENS[id])}`,
      {method:'DELETE'},
      12000
    ));
    delete COMMUNITY_POST_TOKENS[id];
    saveCommunityOwnerTokens();
    closeCommunityDeleteModal();
    await loadCommunityPosts();
  }catch(error){
    status.textContent=error.message||'Post could not be deleted.';
  }finally{
    button.disabled=false;
    button.textContent='OK';
  }
});

function closeCommunityReportModal(){
  const modal=document.getElementById('community-report-modal');
  modal?.classList.add('hidden');
  modal?.classList.remove('flex');
  window.ecotraceReportingPostId=null;
}
document.getElementById('community-report-cancel')?.addEventListener('click',closeCommunityReportModal);
document.getElementById('community-report-modal')?.addEventListener('click',event=>{
  if(event.target.id==='community-report-modal')closeCommunityReportModal();
});
document.getElementById('community-report-submit')?.addEventListener('click',async()=>{
  const postId=window.ecotraceReportingPostId;
  if(!postId)return;
  const category=document.getElementById('community-report-category').value;
  const explanation=document.getElementById('community-report-reason').value.trim();
  const status=document.getElementById('community-report-status');
  const submit=document.getElementById('community-report-submit');

  if(explanation.length<3){
    status.textContent='Please briefly explain why you are reporting this post.';
    return;
  }

  submit.disabled=true;
  submit.textContent='Submitting…';
  status.textContent='Submitting report…';

  try{
    await readApiResponse(await fetchWithTimeout(
      '/api/community-report-reason',
      {
        method:'POST',
        headers:{'Content-Type':'application/json'},
        body:JSON.stringify({
          post_id:Number(postId),
          reason:`${category}: ${explanation}`
        })
      },
      10000
    ));

    status.textContent='Report submitted for review. The post stays visible unless a moderator decides action is needed.';
    setTimeout(()=>{
      closeCommunityReportModal();
      loadCommunityPosts();
    },900);
  }catch(error){
    status.textContent=error.message||'Report could not be submitted.';
  }finally{
    submit.disabled=false;
    submit.textContent='Submit report';
  }
});

function parseCommunityStoredDetails(description){
  const raw=String(description||'');
  const parts=raw.split('\n\n[EcoTrace action details]\n');
  const meta={};
  for(const line of (parts[1]||'').split('\n')){
    const idx=line.indexOf(':');
    if(idx>0)meta[line.slice(0,idx).trim().toLowerCase()]=line.slice(idx+1).trim();
  }
  return {base:parts[0]||'',meta};
}
function clearCommunityPostFormForEdit(){
  selectedCommunityFile=null;
  document.getElementById('community-photo-file').value='';
  document.getElementById('community-upload-status').textContent='';
  document.getElementById('community-role-other')?.classList.add('hidden');
  document.getElementById('community-custom-type-wrap')?.classList.add('hidden');
}
function communityEditPostInOriginalForm(post){
  const stored=parseCommunityStoredDetails(post.description);
  window.ecotraceEditingCommunityPost={id:String(post.id),post};
  clearCommunityPostFormForEdit();

  // Use the exact normal Create a Community Post screen.
  setCommunityTab('feed');
  const card=document.getElementById('community-create-card');
  card.classList.remove('hidden');
  document.getElementById('community-edit-post-banner').classList.remove('hidden');
  document.getElementById('community-create-title').textContent='Create a community post';
  document.getElementById('community-create-help').textContent='Share an environmental update, question, or local idea.';
  document.getElementById('community-create-toggle').textContent='− Hide post form';

  document.getElementById('community-name').value=post.display_name||'';
  document.getElementById('community-category').value=post.category||'general';
  document.getElementById('community-title').value=post.title||'';
  document.getElementById('community-description').value=stored.base;
  document.getElementById('community-location-text').value=post.location_text||'';
  document.getElementById('community-visibility').value=post.visibility||'public';
  document.getElementById('community-image-url').value=post.image_url||'';
  document.getElementById('community-tags').value=(post.tags||[]).join(', ');
  document.getElementById('community-tree-reason').value=post.tree_reason||'';
  document.getElementById('community-event-date').value=post.event_date?String(post.event_date).slice(0,16):'';
  document.getElementById('community-event-note').value=stored.meta['quick note']||'';

  // Restore optional photo.
  const previewWrap=document.getElementById('community-photo-preview-wrap');
  const preview=document.getElementById('community-photo-preview');
  if(post.image_url){
    preview.src=post.image_url;
    previewWrap.classList.remove('hidden');
  }else{
    preview.src='';
    previewWrap.classList.add('hidden');
  }

  // Restore custom post type.
  if(post.category==='custom'){
    document.getElementById('community-custom-type').value=stored.meta['custom type']||'';
    document.getElementById('community-custom-type-wrap').classList.remove('hidden');
  }else{
    document.getElementById('community-custom-type').value='';
  }

  // Restore organizer/profile metadata.
  const roleLine=stored.meta['role']||'';
  if(roleLine){
    const pieces=roleLine.replace(' (unverified/test)','').split(' • ');
    const savedRole=(pieces[0]||'').trim();
    const roleSelect=document.getElementById('community-role');
    const known=[...roleSelect.options].some(option=>option.value===savedRole);
    roleSelect.value=known?savedRole:'other';
    document.getElementById('community-role-other').value=known?'':savedRole;
    document.getElementById('community-role-other').classList.toggle('hidden',known);
    document.getElementById('community-affiliation').value=(pieces[1]||'').trim();
  }else{
    document.getElementById('community-affiliation').value='';
  }
  if(stored.meta['public history sharing']){
    document.getElementById('community-share-history').checked=/enabled/i.test(stored.meta['public history sharing']);
  }

  // Restore event-specific fields if this is an event.
  if(isCommunityEvent(post.category)){
    if(stored.meta['end time']){
      document.getElementById('community-time-mode').value='range';
      document.getElementById('community-event-end-time').value=stored.meta['end time'];
    }else{
      document.getElementById('community-time-mode').value='start';
      document.getElementById('community-event-end-time').value='';
    }

    if(stored.meta['minimum needed']){
      document.getElementById('community-capacity-mode').value='minimum';
      document.getElementById('community-max-participants').value=parseInt(stored.meta['minimum needed'],10)||1;
    }else if(stored.meta['maximum']){
      document.getElementById('community-capacity-mode').value='maximum';
      document.getElementById('community-max-participants').value=parseInt(stored.meta['maximum'],10)||30;
    }else if(post.max_participants&&Number(post.max_participants)<10000){
      document.getElementById('community-capacity-mode').value='maximum';
      document.getElementById('community-max-participants').value=post.max_participants;
    }else{
      document.getElementById('community-capacity-mode').value='unlimited';
    }
  }else{
    document.getElementById('community-time-mode').value='start';
    document.getElementById('community-event-end-time').value='';
    document.getElementById('community-capacity-mode').value='unlimited';
  }

  // Restore map point.
  if(post.latitude!=null&&post.longitude!=null){
    communitySelectedPoint={lat:Number(post.latitude),lon:Number(post.longitude)};
    document.getElementById('community-coordinate-box').textContent=
      `Saved location • ${Number(post.latitude).toFixed(5)}, ${Number(post.longitude).toFixed(5)}`;
    if(communitySelectionMarker)communitySelectionMarker.remove();
    communitySelectionMarker=new maplibregl.Marker({color:'#f59e0b'})
      .setLngLat([Number(post.longitude),Number(post.latitude)])
      .setPopup(new maplibregl.Popup({offset:16}).setText('Saved community post location'))
      .addTo(map);
  }else{
    communitySelectedPoint=null;
    document.getElementById('community-coordinate-box').textContent='No map point selected.';
  }

  updateCommunityFormFields();
  updateCommunityCapacityFields();
  document.getElementById('community-end-time-wrap').classList.toggle(
    'hidden',
    document.getElementById('community-time-mode').value!=='range'
  );

  document.getElementById('community-submit').textContent='Republish changes';
  document.getElementById('community-form-status').textContent='Everything from the saved post is loaded below. Change anything, then republish the changes.';
  card.scrollIntoView({behavior:'smooth',block:'start'});
  setTimeout(()=>document.getElementById('community-title')?.focus(),250);
}
function cancelCommunityPostEdit(clear=false){
  window.ecotraceEditingCommunityPost=null;
  document.getElementById('community-edit-post-banner')?.classList.add('hidden');
  document.getElementById('community-submit').textContent='Publish post';
  document.getElementById('community-form-status').textContent='';
  if(clear){
    for(const id of [
      'community-title','community-description','community-tree-reason',
      'community-event-date','community-event-end-time','community-event-note',
      'community-location-text','community-image-url','community-tags',
      'community-custom-type'
    ]){
      const el=document.getElementById(id);
      if(el)el.value='';
    }
    communitySelectedPoint=null;
    if(communitySelectionMarker){communitySelectionMarker.remove();communitySelectionMarker=null;}
    document.getElementById('community-coordinate-box').textContent='No map point selected.';
    document.getElementById('community-photo-preview-wrap').classList.add('hidden');
  }
  applyOriginalProfileAutofill();
}
document.getElementById('community-cancel-post-edit')?.addEventListener('click',()=>cancelCommunityPostEdit(true));

function attachCommunityPostEvents(list){
  list.querySelectorAll('.community-profile-link').forEach(button=>{
    button.addEventListener('click',()=>{
      try{
        openCommunityProfile(JSON.parse(decodeURIComponent(button.dataset.profile||'')));
      }catch(error){
        console.warn('Could not open community profile',error);
      }
    });
  });
  list.querySelectorAll('.community-fly-to').forEach(button=>{
    button.addEventListener('click',()=>{
      const lat=Number(button.dataset.lat);
      const lon=Number(button.dataset.lon);
      map.flyTo({center:[lon,lat],zoom:17,essential:true});
      if(communitySelectionMarker)communitySelectionMarker.remove();
      communitySelectionMarker=new maplibregl.Marker({color:'#f59e0b'})
        .setLngLat([lon,lat])
        .setPopup(new maplibregl.Popup({offset:16}).setText('Community location'))
        .addTo(map);
    });
  });

  list.querySelectorAll('.community-comment-submit').forEach(button=>{
    button.addEventListener('click',async()=>{
      const card=button.closest('article');
      const name=card.querySelector('.community-comment-name').value.trim();
      const message=card.querySelector('.community-comment-message').value.trim();
      if(!name||!message)return;

      button.disabled=true;
      button.textContent='Posting…';

      try{
        const response=await fetchWithTimeout(
          `/api/community/posts/${encodeURIComponent(button.dataset.postId)}/comments`,
          {
            method:'POST',
            headers:{'Content-Type':'application/json'},
            body:JSON.stringify({
              display_name:safeDisplayName(name),
              message,
              parent_comment_id:null
            })
          },
          12000
        );
        const createdComment=await readApiResponse(response);
        if(createdComment.edit_token){
          COMMUNITY_COMMENT_TOKENS[String(createdComment.id)]=createdComment.edit_token;
          saveCommunityOwnerTokens();
        }
        card.querySelector('.community-comment-message').value='';
        await loadCommunityComments(
          Number(button.dataset.postId),
          card
        );
      }finally{
        button.disabled=false;
        button.textContent='Reply';
      }
    });
  });

  list.querySelectorAll('.community-join').forEach(button=>{
    button.addEventListener('click',async()=>{
      const card=button.closest('article');
      const signedIn=Boolean(currentEcoTraceAccount);
      const baseName=signedIn
        ?(currentEcoTraceAccount.display_name||currentEcoTraceAccount.full_name||'')
        :card.querySelector('.community-join-name')?.value.trim();
      const joinRole=signedIn
        ?accountRoleLabel(currentEcoTraceAccount.role)
        :card.querySelector('.community-join-role')?.value.trim();
      const joinAffiliation=signedIn
        ?(currentEcoTraceAccount.organization||currentEcoTraceAccount.education_work||'')
        :card.querySelector('.community-join-affiliation')?.value.trim();
      const shareHistory=signedIn
        ?Boolean(currentEcoTraceAccount.share_history)
        :Boolean(card.querySelector('.community-join-history')?.checked);
      const name=[
        baseName,
        joinRole||'Not shared',
        joinAffiliation||'Not shared',
        shareHistory?'history:on':'history:off'
      ].join(' • ');

      if(!baseName){
        alert('Sign in, or enter your own name to join as a guest.');
        return;
      }

      button.disabled=true;
      button.textContent='Joining…';

      try{
        const response=await fetchWithTimeout(
          `/api/community/posts/${encodeURIComponent(button.dataset.postId)}/join`,
          {
            method:'POST',
            headers:{'Content-Type':'application/json'},
            body:JSON.stringify({display_name:safeDisplayName(name)})
          },
          12000
        );
        await readApiResponse(response);
        recordCommunityActivity('joined',{
            id:button.dataset.postId,
            title:button.closest('article')?.querySelector('h3')?.textContent||'Community event',
            role:button.closest('article')?.querySelector('.community-join-role')?.value||'Participant'
          });
          await loadCommunityPosts();
      }finally{
        button.disabled=false;
        button.textContent='Join';
      }
    });
  });

  list.querySelectorAll('.community-leave').forEach(button=>{
    button.addEventListener('click',async()=>{
      const card=button.closest('article');
      const name=currentEcoTraceAccount
        ?(currentEcoTraceAccount.display_name||currentEcoTraceAccount.full_name||'')
        :(card.querySelector('.community-join-name')?.value.trim()||'');

      if(!name){
        alert('Sign in, or enter the same guest name used to join.');
        return;
      }

      button.disabled=true;
      button.textContent='Leaving…';

      try{
        const response=await fetchWithTimeout(
          `/api/community/posts/${encodeURIComponent(button.dataset.postId)}/join?display_name=${encodeURIComponent(name)}`,
          {method:'DELETE'},
          12000
        );
        await readApiResponse(response);
        await loadCommunityPosts();
      }finally{
        button.disabled=false;
        button.textContent='Leave';
      }
    });
  });

  list.querySelectorAll('.community-complete-event').forEach(button=>{
    button.addEventListener('click',async()=>{
      const id=String(button.dataset.postId);
      const token=COMMUNITY_POST_TOKENS[id];
      if(!ownsCommunityPost(id))return;
      button.disabled=true;
      button.textContent='Archiving…';
      try{
        const completedPost=latestCommunityPosts.find(item=>String(item.id)===id);
        await readApiResponse(await fetchWithTimeout(
          `/api/community/posts/${encodeURIComponent(id)}/complete?edit_token=${encodeURIComponent(token)}`,
          {method:'POST'},
          12000
        ));
        openImpactRecorder({
          id,
          kind:'community',
          title:completedPost?.title||'Completed environmental action',
          participants:completedPost?.participant_count||0
        });
        await loadCommunityPosts();
      }catch(error){
        button.disabled=false;
        button.textContent='Mark done';
        alert(error.message||'Environmental event could not be archived.');
      }
    });
  });

  list.querySelectorAll('.community-edit-post').forEach(button=>{
    button.addEventListener('click',()=>{
      const id=String(button.dataset.postId);
      const post=latestCommunityPosts.find(item=>String(item.id)===id);
      if(!post)return;
      communityEditPostInOriginalForm(post);
    });
  });
  list.querySelectorAll('.community-delete-post').forEach(button=>{
    button.addEventListener('click',()=>{
      const id=String(button.dataset.postId);
      const post=latestCommunityPosts.find(item=>String(item.id)===id);
      window.ecotraceDeletingPostId=id;
      document.getElementById('community-delete-title').textContent=post?.title||'This post';
      document.getElementById('community-delete-status').textContent='';
      const modal=document.getElementById('community-delete-modal');
      modal.classList.remove('hidden');modal.classList.add('flex');
    });
  });
  list.querySelectorAll('.community-report').forEach(button=>{
    button.addEventListener('click',()=>{
      window.ecotraceReportingPostId=String(button.dataset.postId);
      document.getElementById('community-report-category').value='spam';
      document.getElementById('community-report-reason').value='';
      document.getElementById('community-report-status').textContent='';
      const modal=document.getElementById('community-report-modal');
      modal.classList.remove('hidden');
      modal.classList.add('flex');
      document.getElementById('community-report-reason').focus();
    });
  });

}

async function loadCommunityComments(postId,card){
  const container=card.querySelector('.community-comments');
  container.innerHTML='<p class="text-xs text-slate-500">Loading replies…</p>';

  try{
    const response=await fetchWithTimeout(
      `/api/community/posts/${encodeURIComponent(postId)}/comments`,
      {},
      10000
    );
    const comments=await readApiResponse(response);
    for(const c of comments)communityCommentPermissions.set(String(c.id),c);

    container.innerHTML=comments.length
      ?comments.map(comment=>`
        <div class="rounded-lg bg-white/5 p-2 text-xs leading-5 text-slate-300">
          <strong class="text-amber-100">${escapeHtml(comment.display_name)}</strong>:
          ${escapeHtml(comment.message)}
          <p class="mt-1 text-[9px] text-slate-500">${escapeHtml(communityEscapeDate(comment.created_at))}</p>
          ${ownsCommunityComment(comment.id)?`
            <div class="mt-2 flex gap-3">
              <button class="community-edit-comment text-[10px] font-bold text-sky-300" data-comment-id="${comment.id}" data-message="${escapeHtml(comment.message)}">Edit</button>
              <button class="community-delete-comment text-[10px] font-bold text-red-300" data-comment-id="${comment.id}">Delete</button>
            </div>
          `:''}
        </div>
      `).join('')
      :'<p class="text-xs text-slate-500">No replies yet.</p>';

    container.querySelectorAll('.community-edit-comment').forEach(button=>{
      button.addEventListener('click',async()=>{
        const id=String(button.dataset.commentId);
        const message=prompt('Edit reply:',button.dataset.message);
        if(message===null)return;
        await readApiResponse(await fetchWithTimeout(
          `/api/community/comments/${encodeURIComponent(id)}?edit_token=${encodeURIComponent(COMMUNITY_COMMENT_TOKENS[id])}`,
          {
            method:'PATCH',
            headers:{'Content-Type':'application/json'},
            body:JSON.stringify({message:message.trim()})
          },
          12000
        ));
        await loadCommunityComments(postId,card);
      });
    });
    container.querySelectorAll('.community-delete-comment').forEach(button=>{
      button.addEventListener('click',async()=>{
        const id=String(button.dataset.commentId);
        if(!confirm('Delete this reply?'))return;
        await readApiResponse(await fetchWithTimeout(
          `/api/community/comments/${encodeURIComponent(id)}?edit_token=${encodeURIComponent(COMMUNITY_COMMENT_TOKENS[id])}`,
          {method:'DELETE'},
          12000
        ));
        delete COMMUNITY_COMMENT_TOKENS[id];
        saveCommunityOwnerTokens();
        await loadCommunityComments(postId,card);
      });
    });
  }catch(error){
    container.innerHTML=`<p class="text-xs text-red-300">${escapeHtml(error.message||'Replies unavailable.')}</p>`;
  }
}

document.querySelectorAll('.community-subtab').forEach(button=>{
  button.addEventListener('click',()=>{
    setCommunityTab(button.dataset.communityTab);
  });
});

document.getElementById('community-category').addEventListener('change',()=>{
  updateCommunityFormFields();
  document.getElementById('community-custom-type-wrap')?.classList.toggle('hidden',document.getElementById('community-category').value!=='custom');
  updateCommunityCapacityFields();
});
function updateCommunityCapacityFields(){
  const mode=document.getElementById('community-capacity-mode')?.value||'unlimited';
  document.getElementById('community-capacity-number-wrap')?.classList.toggle('hidden',mode==='unlimited');
}
document.getElementById('community-capacity-mode')?.addEventListener('change',updateCommunityCapacityFields);
document.getElementById('community-time-mode')?.addEventListener('change',()=>{
  document.getElementById('community-end-time-wrap')?.classList.toggle('hidden',document.getElementById('community-time-mode').value!=='range');
});
updateCommunityCapacityFields();

document.getElementById('community-location-geocode')?.addEventListener('click',async()=>{
  const status=document.getElementById('community-form-status');
  const query=document.getElementById('community-location-text').value.trim();
  if(!query){status.textContent='Type an address or landmark first.';return;}
  status.textContent='Locating place…';
  try{
    const place=await readApiResponse(await fetchWithTimeout(`/api/geocode?q=${encodeURIComponent(query)}`,{},12000));
    communitySelectedPoint={lat:Number(place.latitude),lon:Number(place.longitude)};
    if(communitySelectionMarker)communitySelectionMarker.remove();
    communitySelectionMarker=new maplibregl.Marker({color:'#38bdf8'}).setLngLat([communitySelectedPoint.lon,communitySelectedPoint.lat]).addTo(map);
    document.getElementById('community-coordinate-box').textContent=`${place.display_name} • ${communitySelectedPoint.lat.toFixed(5)}, ${communitySelectedPoint.lon.toFixed(5)}`;
    map.flyTo({center:[communitySelectedPoint.lon,communitySelectedPoint.lat],zoom:16,duration:600});
    status.textContent='Location resolved.';
  }catch(error){status.textContent=error.message||'Could not locate that place.';}
});

const IMPACT_HISTORY_KEY='ecotraceImpactHistoryV1';
let pendingImpactContext=null;
function impactRecords(){try{return JSON.parse(localStorage.getItem(IMPACT_HISTORY_KEY)||'[]');}catch(_){return [];}}
function saveImpactRecords(rows){localStorage.setItem(IMPACT_HISTORY_KEY,JSON.stringify(rows));}
function renderImpactHistory(){
  const rows=impactRecords();
  const list=document.getElementById('community-impact-list');
  const badge=document.getElementById('impact-total-badge');
  if(badge)badge.textContent=`${rows.length} record${rows.length===1?'':'s'}`;
  if(!list)return;
  list.innerHTML=rows.length?rows.map(row=>`
    <article class="rounded-xl bg-white/5 p-3 ring-1 ring-white/10">
      <div class="flex items-start justify-between gap-2"><strong class="text-emerald-100">${escapeHtml(row.title)}</strong><span class="text-[9px] text-slate-500">${escapeHtml(new Date(row.completed_at).toLocaleDateString())}</span></div>
      <p class="mt-1 text-xs text-slate-300">${escapeHtml(row.summary||'Completed action')}</p>
      <div class="mt-2 flex flex-wrap gap-1 text-[9px]"><span class="rounded-full bg-cyan-300/10 px-2 py-1 text-cyan-100">${Number(row.participants||0)} participants</span><span class="rounded-full bg-violet-300/10 px-2 py-1 text-violet-100">${Number(row.hours||0)} volunteer h</span>${row.primary_value?`<span class="rounded-full bg-emerald-300/10 px-2 py-1 text-emerald-100">${escapeHtml(String(row.primary_value))} ${escapeHtml(row.primary_label||'impact units')}</span>`:''}</div>
      ${row.secondary?`<p class="mt-2 text-[10px] text-slate-400">${escapeHtml(row.secondary)}</p>`:''}
    </article>`).join(''):'<p class="text-xs text-slate-500">No impact records yet. Mark an event done and record what happened.</p>';
}
function openImpactRecorder(context){
  pendingImpactContext=context;
  document.getElementById('impact-title').value=context.title||'';
  document.getElementById('impact-summary').value='';
  document.getElementById('impact-participants').value=context.participants||'';
  document.getElementById('impact-hours').value='';
  document.getElementById('impact-primary-value').value='';
  document.getElementById('impact-primary-label').value=context.kind==='ewaste'?'devices collected':'sites/actions completed';
  document.getElementById('impact-secondary').value='';
  const modal=document.getElementById('impact-modal');modal.classList.remove('hidden');modal.classList.add('flex');
}
document.getElementById('impact-modal-close')?.addEventListener('click',()=>{const m=document.getElementById('impact-modal');m.classList.add('hidden');m.classList.remove('flex');pendingImpactContext=null;});
document.getElementById('impact-save')?.addEventListener('click',async()=>{
  const rows=impactRecords();
  rows.unshift({
    id:`${Date.now()}-${Math.random().toString(16).slice(2)}`,
    source_id:pendingImpactContext?.id||null,
    kind:pendingImpactContext?.kind||'community',
    title:document.getElementById('impact-title').value.trim()||'Completed action',
    summary:document.getElementById('impact-summary').value.trim(),
    participants:Number(document.getElementById('impact-participants').value)||0,
    hours:Number(document.getElementById('impact-hours').value)||0,
    primary_value:Number(document.getElementById('impact-primary-value').value)||0,
    primary_label:document.getElementById('impact-primary-label').value.trim(),
    secondary:document.getElementById('impact-secondary').value.trim(),
    completed_at:new Date().toISOString()
  });
  saveImpactRecords(rows);renderImpactHistory();
  const m=document.getElementById('impact-modal');m.classList.add('hidden');m.classList.remove('flex');
  pendingImpactContext=null;
});
renderImpactHistory();

async function renderValidation(){
  try{
    const stats=await readApiResponse(await fetchWithTimeout('/api/account/validation/stats',{},8000));
    const text=item=>item?.rate_percent==null?'—':`${Number(item.rate_percent).toFixed(1)}% (${item.total})`;
    document.getElementById('validation-tree-rate')&&(document.getElementById('validation-tree-rate').textContent=text(stats.tree));
    document.getElementById('validation-route-rate')&&(document.getElementById('validation-route-rate').textContent=text(stats.route));
    document.getElementById('validation-bike-rate')&&(document.getElementById('validation-bike-rate').textContent=text(stats.bike));
  }catch(error){
    console.warn('Validation stats unavailable',error);
  }

  const reviewer=false;
  document.getElementById('validation-reviewer-panel')?.classList.toggle('hidden',!reviewer);
  if(reviewer)loadValidationReports();
}
document.getElementById('validation-report-submit')?.addEventListener('click',async()=>{
  const status=document.getElementById('validation-report-status');
  const note=document.getElementById('validation-report-note').value.trim();
  if(!note){status.textContent='Describe what looks wrong first.';return;}
  status.textContent='Submitting report…';
  try{
    const headers={'Content-Type':'application/json',...{}};
    const result=await readApiResponse(await fetchWithTimeout('/api/account/validation/reports',{
      method:'POST',headers,
      body:JSON.stringify({
        report_type:document.getElementById('validation-report-type').value,
        location:document.getElementById('validation-report-location').value.trim()||null,
        note,
        evidence_url:document.getElementById('validation-report-evidence').value.trim()||null,
        display_name:safeDisplayName(document.getElementById('validation-report-name').value)||null
      })
    },10000));
    status.textContent=`Report #${result.id} submitted for review. It does not change official data automatically.`;
    document.getElementById('validation-report-location').value='';
    document.getElementById('validation-report-note').value='';
    document.getElementById('validation-report-evidence').value='';
    if(currentEcoTraceAccount)document.getElementById('validation-report-name').value='';
    renderValidation();
  }catch(error){status.textContent=error.message||'Report could not be submitted.';}
});

document.getElementById('validation-add')?.addEventListener('click',async()=>{
  if(!currentEcoTraceAccount?.trusted_reviewer)return;
  try{
    await readApiResponse(await fetchWithTimeout('/api/account/validation/checks',{
      method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({
        check_type:document.getElementById('validation-type').value,
        result:document.getElementById('validation-result').value,
        location:document.getElementById('validation-location').value.trim()||null,
        note:document.getElementById('validation-note').value.trim()||null
      })
    },10000));
    document.getElementById('validation-location').value='';
    document.getElementById('validation-note').value='';
    renderValidation();
  }catch(error){alert(error.message||'Validation check could not be saved.');}
});

async function loadValidationReports(){
  const list=document.getElementById('validation-review-list');
  if(!list||!currentEcoTraceAccount?.trusted_reviewer)return;
  list.innerHTML='<p class="text-slate-500">Loading reports…</p>';
  try{
    const rows=await readApiResponse(await fetchWithTimeout('/api/account/validation/reports',{},10000));
    const pending=rows.filter(row=>row.status==='pending');
    list.innerHTML=pending.length?pending.map(row=>`
      <article class="rounded-lg bg-black/20 p-2">
        <div class="flex items-start justify-between gap-2"><strong class="text-emerald-100">#${row.id} • ${escapeHtml(row.report_type)}</strong><span class="text-[9px] text-slate-500">${escapeHtml(row.display_name||'Anonymous')}</span></div>
        <p class="mt-1">${escapeHtml(row.location||'No location supplied')}</p>
        <p class="mt-1 text-slate-400">${escapeHtml(row.note)}</p>
        ${row.evidence_url?`<a class="mt-1 block text-sky-300 underline" href="${escapeHtml(row.evidence_url)}" target="_blank" rel="noopener">Evidence ↗</a>`:''}
        <div class="mt-2 grid grid-cols-2 gap-2">
          <button class="validation-accept rounded-lg bg-emerald-400 px-2 py-1 font-black text-emerald-950" data-id="${row.id}">Verify / count</button>
          <button class="validation-reject rounded-lg bg-red-400/15 px-2 py-1 font-bold text-red-200 ring-1 ring-red-300/20" data-id="${row.id}">Reject report</button>
        </div>
      </article>`).join(''):'<p class="text-slate-500">No pending reports.</p>';

    list.querySelectorAll('.validation-accept').forEach(button=>button.addEventListener('click',async()=>{
      const result=prompt('Official validation result for this report: type "pass" or "fail".','fail');
      if(!['pass','fail'].includes(String(result||'').toLowerCase()))return;
      const note=prompt('Reviewer note / what you verified:','')||'';
      await readApiResponse(await fetchWithTimeout(`/api/account/validation/reports/${button.dataset.id}/review`,{
        method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({status:'verified',reviewer_note:note,count_as_check:true,check_result:String(result).toLowerCase()})
      },10000));
      renderValidation();
    }));
    list.querySelectorAll('.validation-reject').forEach(button=>button.addEventListener('click',async()=>{
      const note=prompt('Why reject this report?','Insufficient or incorrect evidence.')||'';
      await readApiResponse(await fetchWithTimeout(`/api/account/validation/reports/${button.dataset.id}/review`,{
        method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({status:'rejected',reviewer_note:note,count_as_check:false,check_result:null})
      },10000));
      renderValidation();
    }));
  }catch(error){list.innerHTML=`<p class="text-red-300">${escapeHtml(error.message||'Reports unavailable.')}</p>`;}
}
document.getElementById('validation-refresh-reports')?.addEventListener('click',loadValidationReports);


const COMMUNITY_ACTIVITY_KEY='ecotraceCommunityActivityV1';
function loadCommunityActivityState(){try{return JSON.parse(localStorage.getItem(COMMUNITY_ACTIVITY_KEY)||'{"hosted":[],"joined":[],"hours":[]}');}catch(_){return {hosted:[],joined:[],hours:[]};}}
function saveCommunityActivityState(s){localStorage.setItem(COMMUNITY_ACTIVITY_KEY,JSON.stringify(s));}
function recordCommunityActivity(kind,item){const s=loadCommunityActivityState();s[kind]=s[kind]||[];if(!s[kind].some(r=>String(r.id)===String(item.id)))s[kind].unshift(item);saveCommunityActivityState(s);renderCommunityActivity();}
function renderCommunityActivity(){
  const s=loadCommunityActivityState();
  const render=rows=>rows?.length?rows.slice(0,20).map(r=>`<div class="mb-1 rounded-lg bg-white/5 p-2"><strong>${escapeHtml(r.title||'Community action')}</strong><br><span class="text-[10px] text-slate-500">${escapeHtml(r.role||'')}</span></div>`).join(''):'None yet.';
  document.getElementById('community-hosted-activity')&&(document.getElementById('community-hosted-activity').innerHTML=render(s.hosted));
  document.getElementById('community-joined-activity')&&(document.getElementById('community-joined-activity').innerHTML=render(s.joined));
  const h=document.getElementById('service-hours-list');if(h)h.innerHTML=s.hours?.length?s.hours.map(r=>`<div class="rounded-lg bg-white/5 p-2">${escapeHtml(r.title)} • <strong>${Number(r.hours).toFixed(2)} h</strong> • ${r.status==='verified'?'Organizer verified':'Self-reported'}</div>`).join(''):'<span class="text-slate-500">No service hours logged yet.</span>';
}
document.getElementById('service-hours-add')?.addEventListener('click',()=>{const title=document.getElementById('service-hours-title').value.trim();const hours=Number(document.getElementById('service-hours-value').value);if(!title||!Number.isFinite(hours)||hours<=0)return;const s=loadCommunityActivityState();s.hours=s.hours||[];s.hours.unshift({title,hours,status:document.getElementById('service-hours-status').value});saveCommunityActivityState(s);document.getElementById('service-hours-title').value='';document.getElementById('service-hours-value').value='';renderCommunityActivity();});
renderCommunityActivity();

document.getElementById('community-use-map-point').addEventListener('click',()=>{
  communityMapSelectionMode=true;
  showToolPanel('community-panel');
  document.getElementById('community-coordinate-box').textContent='Loading map selection — click a location…';
  document.getElementById('status-pill').textContent='Community location — click the map';
});

document.getElementById('community-create-toggle').addEventListener('click',()=>{
  const card=document.getElementById('community-create-card');
  card.classList.toggle('hidden');
  document.getElementById('community-create-toggle').textContent=card.classList.contains('hidden')
    ?'+ Create a new post'
    :'− Hide post form';
});
document.getElementById('community-refresh').addEventListener(
  'click',
  loadCommunityPosts
);
document.getElementById('community-all-filter').addEventListener('change',loadCommunityPosts);
document.getElementById('community-sort').addEventListener('change',loadCommunityPosts);


document.getElementById('community-submit').addEventListener('click',async()=>{
  if(!currentEcoTraceAccount){alert('Sign in to create or change posts.');return;}
  const button=document.getElementById('community-submit');
  const status=document.getElementById('community-form-status');
  const category=document.getElementById('community-category').value;
  const tags=document.getElementById('community-tags').value
    .split(',')
    .map(value=>value.trim())
    .filter(Boolean);

  let uploadedCommunityImage=document.getElementById('community-image-url').value||null;
  if(selectedCommunityFile){
    uploadedCommunityImage=await uploadSelectedImage(selectedCommunityFile,'community','community-upload-status');
  }

  const payload={
    display_name:safeDisplayName(document.getElementById('community-name').value),
    category,
    title:document.getElementById('community-title').value.trim(),
    description:(()=>{
      const base=document.getElementById('community-description').value.trim();
      const meta=[];
      const customType=document.getElementById('community-custom-type')?.value.trim();
      if(category==='custom'&&customType)meta.push(`Custom type: ${customType}`);
      if(isCommunityEvent(category)){
        const mode=document.getElementById('community-capacity-mode')?.value||'unlimited';
        const count=Number(document.getElementById('community-max-participants')?.value)||0;
        meta.push(mode==='unlimited'?'Participants: no stated limit':`${mode==='minimum'?'Minimum needed':'Maximum'}: ${count}`);
        if(document.getElementById('community-time-mode')?.value==='range'){
          const end=document.getElementById('community-event-end-time')?.value;if(end)meta.push(`End time: ${end}`);
        }
        const note=document.getElementById('community-event-note')?.value.trim();if(note)meta.push(`Quick note: ${note}`);
      }
      const role=document.getElementById('community-role')?.value||'student';
      const roleText=role==='other'?(document.getElementById('community-role-other')?.value.trim()||'other'):role;
      const affiliation=document.getElementById('community-affiliation')?.value.trim();
      meta.push(`Role: ${roleText}${affiliation?` • ${affiliation}`:''} (unverified/test)`);
      meta.push(`Public history sharing: ${document.getElementById('community-share-history')?.checked?'enabled':'private'}`);
      return meta.length?`${base}\n\n[EcoTrace action details]\n${meta.join('\n')}`:base;
    })(),
    visibility:document.getElementById('community-visibility').value,
    event_date:document.getElementById('community-event-date').value||null,
    max_participants:isCommunityEvent(category)
      ?(document.getElementById('community-capacity-mode')?.value==='maximum'?Math.max(1,Number(document.getElementById('community-max-participants').value)||30):10000)
      :null,
    location_text:document.getElementById('community-location-text').value.trim()||null,
    image_url:uploadedCommunityImage,
    latitude:communitySelectedPoint?.lat??null,
    longitude:communitySelectedPoint?.lon??null,
    tags,
    tree_reason:category==='tree_suggestion'
      ?document.getElementById('community-tree-reason').value.trim()||null
      :null
  };

  if(!payload.display_name||!payload.title||!payload.description){
    status.textContent='Enter a display name, title, and description.';
    return;
  }

  if(category==='tree_suggestion'&&!communitySelectedPoint){
    status.textContent='Tree suggestions require a map point.';
    return;
  }

  if(isCommunityEvent(category)&&!payload.event_date){
    status.textContent='Events and campaigns require a date and time.';
    return;
  }

  button.disabled=true;
  button.textContent='Saving…';
  status.textContent=window.ecotraceEditingCommunityPost?'Republishing changes…':'Saving your community post…';

  try{
    if(window.ecotraceEditingCommunityPost){
      const editing=window.ecotraceEditingCommunityPost;
      const response=await fetchWithTimeout(
        `/api/community/posts/${encodeURIComponent(editing.id)}?edit_token=${encodeURIComponent(COMMUNITY_POST_TOKENS[editing.id])}`,
        {
          method:'PATCH',
          headers:{'Content-Type':'application/json'},
          body:JSON.stringify(payload)
        },
        12000
      );
      await readApiResponse(response);
      status.textContent='Changes republished.';
      cancelCommunityPostEdit(true);
      await loadCommunityPosts();
      return;
    }

    const response=await fetchWithTimeout(
      '/api/community/posts',
      {
        method:'POST',
        headers:{'Content-Type':'application/json'},
        body:JSON.stringify(payload)
      },
      12000
    );
    const post=await readApiResponse(response);
    if(post.edit_token){
      COMMUNITY_POST_TOKENS[String(post.id)]=post.edit_token;
      saveCommunityOwnerTokens();
    }
    recordCommunityActivity('hosted',{id:post.id,title:post.title,role:document.getElementById('community-role')?.value||'organizer'});

    status.textContent=post.visibility==='public'
      ?'Post published.'
      :'Private draft saved locally.';

    for(const id of [
      'community-title',
      'community-description',
      'community-tree-reason',
      'community-event-date',
      'community-location-text',
      'community-image-url',
      'community-tags'
    ]){
      document.getElementById(id).value='';
    }

    document.getElementById('community-name').value='';
    document.getElementById('community-category').selectedIndex=0;
    document.getElementById('community-custom-type').value='';
    document.getElementById('community-custom-type-wrap').classList.add('hidden');
    document.getElementById('community-role').selectedIndex=0;
    document.getElementById('community-affiliation').value='';
    document.getElementById('community-share-history').checked=false;
    document.getElementById('community-capacity-mode').value='unlimited';
    document.getElementById('community-time-mode').value='start';
    document.getElementById('community-event-end-time').value='';
    document.getElementById('community-event-note').value='';
    updateCommunityCapacityFields();
    document.getElementById('community-end-time-wrap').classList.add('hidden');
    selectedCommunityFile=null;
    document.getElementById('community-photo-preview-wrap').classList.add('hidden');
    document.getElementById('community-photo-file').value='';
    communitySelectedPoint=null;
    communityMapSelectionMode=false;
    document.getElementById('community-coordinate-box').textContent='No map point selected.';

    if(communitySelectionMarker){
      communitySelectionMarker.remove();
      communitySelectionMarker=null;
    }

    applyOriginalProfileAutofill();
    await loadCommunityPosts();
  }catch(error){
    status.textContent=error.message||'Post could not be saved.';
  }finally{
    button.disabled=false;
    button.textContent=window.ecotraceEditingCommunityPost?'Republish changes':'Publish post';
  }
});

setCommunityTab('feed');


function updateEwasteAgeMode(){
  const range=document.getElementById('ewaste-age-mode')?.value==='range';
  document.getElementById('ewaste-age')?.classList.toggle('hidden',range);
  document.getElementById('ewaste-age-range-wrap')?.classList.toggle('hidden',!range);
  const help=document.getElementById('ewaste-age-help');
  if(help)help.textContent=range
    ?'Enter the youngest and oldest plausible age. Keep the range within 10 years so the recommendation is still meaningful.'
    :'Enter your best single estimate. It does not need to be exact.';
}
function getEwasteAgeSelection(){
  const mode=document.getElementById('ewaste-age-mode')?.value||'single';
  if(mode==='range'){
    const minRaw=String(document.getElementById('ewaste-age-min')?.value||'').trim();
    const maxRaw=String(document.getElementById('ewaste-age-max')?.value||'').trim();
    if(!minRaw||!maxRaw)return {ok:false,error:'Enter both ends of the approximate age range.'};
    const min=Number(minRaw);
    const max=Number(maxRaw);
    if(!Number.isFinite(min)||!Number.isFinite(max)||min<0||max<0){
      return {ok:false,error:'Enter both ends of the approximate age range.'};
    }
    if(min>max)return {ok:false,error:'The minimum age cannot be greater than the maximum age.'};
    if(max-min>10)return {ok:false,error:'That age range is too broad. Narrow it to 10 years or less, or use one rough estimate.'};
    const midpoint=(min+max)/2;
    return {ok:true,mode:'range',min,max,midpoint,context:`Approximate age range: ${min}–${max} years. The rule-based recommendation uses midpoint ${midpoint.toFixed(1)} years; Gemini should preserve the full range.`};
  }
  const singleRaw=String(document.getElementById('ewaste-age')?.value||'').trim();
  if(!singleRaw)return {ok:false,error:'Enter an approximate age first.'};
  const value=Number(singleRaw);
  if(!Number.isFinite(value)||value<0)return {ok:false,error:'Enter an approximate age first.'};
  return {ok:true,mode:'single',min:value,max:value,midpoint:value,context:`Approximate age: ${value} years.`};
}
document.getElementById('ewaste-age-mode')?.addEventListener('change',updateEwasteAgeMode);
updateEwasteAgeMode();

function resetEwasteRecommendationForm(){
  const device=document.getElementById('ewaste-device');
  const condition=document.getElementById('ewaste-condition');
  const age=document.getElementById('ewaste-age');
  const model=document.getElementById('ewaste-model');
  if(device)device.value='';
  if(condition)condition.value='';
  if(age)age.value='';
  if(document.getElementById('ewaste-age-min'))document.getElementById('ewaste-age-min').value='';
  if(document.getElementById('ewaste-age-max'))document.getElementById('ewaste-age-max').value='';
  if(document.getElementById('ewaste-age-mode'))document.getElementById('ewaste-age-mode').value='single';
  updateEwasteAgeMode();
  if(model)model.value='';
  for(const id of [
    'ewaste-custom-category','ewaste-custom-brand','ewaste-custom-model',
    'ewaste-custom-year','ewaste-custom-condition','ewaste-custom-battery',
    'ewaste-custom-accessories','ewaste-custom-notes','gemini-ewaste-description'
  ]){
    const el=document.getElementById(id);
    if(el)el.value='';
  }
  document.getElementById('ewaste-custom-item-fields')?.classList.add('hidden');
  const status=document.getElementById('ewaste-recommend-status');
  if(status)status.textContent='';
  const recs=document.getElementById('ewaste-recommendations');
  if(recs)recs.innerHTML='';
  latestGeminiEwasteResult=null;
  const gemini=document.getElementById('gemini-ewaste-result');
  if(gemini){gemini.innerHTML='';gemini.classList.add('hidden');}
  const geminiStatus=document.getElementById('gemini-ewaste-status');
  if(geminiStatus)geminiStatus.textContent='';
}

let activeEwasteTab='recommend';
function setEwasteTab(tab){
  activeEwasteTab=tab;
  document.querySelectorAll('.ewaste-section').forEach(section=>section.classList.add('hidden'));
  document.getElementById(`ewaste-${tab}`).classList.remove('hidden');
  document.querySelectorAll('.ewaste-tab').forEach(button=>{
    const active=button.dataset.ewasteTab===tab;
    button.classList.toggle('bg-lime-400',active);
    button.classList.toggle('text-lime-950',active);
    button.classList.toggle('text-slate-300',!active);
  });
  if(tab==='exchange')loadEwasteListings();
  if(tab==='events')loadEwasteEvents();
  if(tab==='archive')loadEwasteArchive();
}
document.querySelectorAll('.ewaste-tab').forEach(button=>button.addEventListener('click',()=>setEwasteTab(button.dataset.ewasteTab)));


function ensureEstimatedEwasteAge(data){
  const min=Number(data?.approximate_age_min_years);
  const max=Number(data?.approximate_age_max_years);
  if(Number.isFinite(min)&&Number.isFinite(max)&&min>0&&max>0){
    return {min:Math.min(min,max),max:Math.max(min,max),fallback:false};
  }

  const name=String(data?.device_name||'').toLowerCase();
  const category=String(data?.category||'').toLowerCase();
  let range=[2,15];

  if(/phone|smartphone|tablet/.test(name))range=[1,8];
  else if(/laptop|notebook/.test(name))range=[2,12];
  else if(/monitor|television|\btv\b|display/.test(name))range=[2,15];
  else if(/router|modem|gps|calculator/.test(name))range=[2,12];
  else if(/desktop|computer|\bpc\b/.test(name))range=[3,15];
  else if(/refrigerator|freezer|air conditioner|heat pump/.test(name))range=[3,20];
  else if(/washer|dryer|dishwasher|stove|printer/.test(name))range=[3,18];
  else if(/microwave|vacuum|toaster|kettle|shaver|toy/.test(name))range=[2,15];
  else if(/lamp|fluorescent|\bhid\b|\bled\b/.test(name))range=[1,12];
  else if(/cd|dvd|vinyl|record|box set|media|album|cassette|packaging|case|booklet/.test(name))range=[5,30];
  else{
    const byCategory={
      temperature_exchange:[3,20],
      screens_monitors:[2,15],
      lamps:[1,12],
      large_equipment:[3,18],
      small_equipment:[2,15],
      small_it_telecom:[2,12]
    };
    range=byCategory[category]||[2,15];
  }

  return {min:range[0],max:range[1],fallback:true};
}

function geminiCategoryLabel(value){
  const labels={
    temperature_exchange:'Temperature Exchange Equipment',
    screens_monitors:'Screens and Monitors',
    lamps:'Lamps',
    large_equipment:'Large Equipment',
    small_equipment:'Small Equipment',
    small_it_telecom:'Small IT and Telecommunication Equipment'
  };
  return labels[value]||value;
}

function geminiConditionLabel(value){
  const labels={
    working_good:'Working well',
    working_worn:'Works but worn',
    not_working:'Not working',
    unknown:'Unknown — user confirmation needed'
  };
  return labels[value]||value;
}

async function fileToDataUrl(file){
  return await new Promise((resolve,reject)=>{
    const reader=new FileReader();
    reader.onload=()=>resolve(reader.result);
    reader.onerror=()=>reject(new Error('The selected photo could not be read.'));
    reader.readAsDataURL(file);
  });
}

document.getElementById('gemini-ewaste-analyze').addEventListener('click',async()=>{
  const status=document.getElementById('gemini-ewaste-status');
  const result=document.getElementById('gemini-ewaste-result');
  const button=document.getElementById('gemini-ewaste-analyze');

  if(!selectedGeminiEwasteFile){
    status.textContent='Choose or drag in a device photo first.';
    return;
  }
  if(selectedGeminiEwasteFile.size>8*1024*1024){
    status.textContent='Use an image smaller than 8 MB.';
    return;
  }

  button.disabled=true;
  button.textContent='Analyzing photo…';
  status.textContent='Gemini is examining the visible device and your description…';
  result.classList.add('hidden');

  try{
    const dataUrl=await fileToDataUrl(selectedGeminiEwasteFile);
    const response=await fetchWithTimeout(
      '/api/gemini-ewaste/analyze',
      {
        method:'POST',
        headers:{'Content-Type':'application/json'},
        body:JSON.stringify({
          data_url:dataUrl,
          user_description:[document.getElementById('gemini-ewaste-description').value.trim(),ewasteCustomContext(),'Only identify visible condition, brand/model, or age when supported; otherwise return Unknown.'].filter(Boolean).join('\n\n')||null
        })
      },
      90000
    );
    const data=await readApiResponse(response);
    latestGeminiEwasteResult=data;

    if(data.is_ewaste===false){
      result.innerHTML=`
        <article class="rounded-xl bg-amber-400/10 p-3 ring-1 ring-amber-300/25">
          <p class="text-[10px] font-black uppercase tracking-wider text-amber-200">Not e-waste</p>
          <h4 class="mt-1 text-base font-black text-white">${escapeHtml(data.device_name||'Non-electronic item')}</h4>
          <p class="mt-2 text-xs leading-5 text-slate-300">${escapeHtml(data.non_ewaste_reason||'This image does not appear to show electronic or electrical equipment.')}</p>
          <p class="mt-2 text-[10px] leading-4 text-slate-500">EcoTrace will not generate e-waste recycling recommendations for this image.</p>
        </article>`;
      result.classList.remove('hidden');
      status.textContent='This image does not appear to show e-waste.';
      return;
    }

    const qualityWarning=data.photo_quality==='blurry_or_obstructed'
      ?'<p class="mt-2 rounded-lg bg-amber-400/10 p-2 text-xs leading-5 text-amber-100"><strong>Photo quality warning:</strong> The image appears blurry, incomplete, or obstructed, so verify every field carefully.</p>'
      :'';

    result.innerHTML=`
      <article class="rounded-xl bg-black/25 p-3 ring-1 ring-violet-300/20">
        <div class="flex items-start justify-between gap-2">
          <div>
            <p class="text-[10px] font-black uppercase tracking-wider text-violet-200">Gemini photo result</p>
            <h4 class="mt-1 text-base font-black text-white">${escapeHtml(data.device_name)}</h4>
            ${data.possible_brand_model?`<p class="mt-1 text-xs text-slate-300">${escapeHtml(data.possible_brand_model)}</p>`:''}
          </div>
          <span class="rounded-full bg-violet-400/15 px-2 py-1 text-[10px] font-bold text-violet-200">${escapeHtml(data.confidence)} confidence</span>
        </div>

        <div class="mt-3 grid grid-cols-2 gap-2">
          <div class="rounded-lg bg-white/5 p-2">
            <p class="text-[9px] uppercase text-slate-500">Category</p>
            <p class="mt-1 text-xs font-black text-white">${escapeHtml(geminiCategoryLabel(data.category))}</p>
          </div>
          <div class="rounded-lg bg-white/5 p-2">
            <p class="text-[9px] uppercase text-slate-500">Visible condition</p>
            <p class="mt-1 text-xs font-black text-white">${escapeHtml(geminiConditionLabel(data.visible_condition))}</p>
          </div>
        </div>

        <div class="mt-2 rounded-lg bg-white/5 p-2">
          <p class="text-[9px] uppercase text-slate-500">Approximate item age</p>
          <p class="mt-1 text-xs font-black text-white">${
            (()=>{const age=ensureEstimatedEwasteAge(data);return `${age.min}–${age.max} years old${age.fallback?' (broad estimate)':''}`;})()
          }</p>
          ${(data.age_reasoning||[]).length?`
            <ul class="mt-2 list-disc space-y-1 pl-4 text-[11px] leading-5 text-slate-300">
              ${data.age_reasoning.map(item=>`<li>${escapeHtml(item)}</li>`).join('')}
            </ul>
          `:''}
          <p class="mt-2 text-[10px] leading-4 text-amber-200">This is a broad visual estimate of the photographed item’s age, not a verified manufacturing or release year.</p>
        </div>

        ${qualityWarning}

        <details class="mt-3 rounded-lg bg-white/5 p-2">
          <summary class="cursor-pointer text-xs font-bold text-violet-100">Visible observations</summary>
          <ul class="mt-2 list-disc space-y-1 pl-4 text-xs leading-5 text-slate-300">
            ${(data.observations||[]).map(item=>`<li>${escapeHtml(item)}</li>`).join('')}
          </ul>
        </details>

        <details class="mt-2 rounded-lg bg-white/5 p-2">
          <summary class="cursor-pointer text-xs font-bold text-violet-100">Questions to confirm</summary>
          <ul class="mt-2 list-disc space-y-1 pl-4 text-xs leading-5 text-slate-300">
            ${(data.confirmation_questions||[]).map(item=>`<li>${escapeHtml(item)}</li>`).join('')}
          </ul>
        </details>

        <p class="mt-3 text-xs leading-5 text-amber-100">${escapeHtml(data.safety_note)}</p>

        <button id="gemini-ewaste-use-result" type="button" class="mt-3 w-full rounded-lg bg-violet-400 px-3 py-2 text-xs font-black text-violet-950">Use these details in the form</button>
      </article>`;

    result.classList.remove('hidden');
    status.textContent='Review the result, answer the confirmation questions, then use the details in the form.';

    document.getElementById('gemini-ewaste-use-result').addEventListener('click',()=>{
      if(!latestGeminiEwasteResult)return;
      document.getElementById('ewaste-device').value=latestGeminiEwasteResult.category;
      document.getElementById('ewaste-condition').value=latestGeminiEwasteResult.visible_condition;
      if(latestGeminiEwasteResult.possible_brand_model){
        document.getElementById('ewaste-model').value=latestGeminiEwasteResult.possible_brand_model;
      }else{
        document.getElementById('ewaste-model').value=latestGeminiEwasteResult.device_name;
      }
      if(
        latestGeminiEwasteResult.approximate_age_min_years!==null&&
        latestGeminiEwasteResult.approximate_age_max_years!==null
      ){
        const ageMin=Number(latestGeminiEwasteResult.approximate_age_min_years);
        const ageMax=Number(latestGeminiEwasteResult.approximate_age_max_years);
        if(Number.isFinite(ageMin)&&Number.isFinite(ageMax)&&Math.abs(ageMax-ageMin)>0.01){
          document.getElementById('ewaste-age-mode').value='range';
          document.getElementById('ewaste-age-min').value=Math.min(ageMin,ageMax).toFixed(1);
          document.getElementById('ewaste-age-max').value=Math.max(ageMin,ageMax).toFixed(1);
          updateEwasteAgeMode();
          status.textContent=(Math.abs(ageMax-ageMin)>10)
            ?'Details copied. Gemini’s age range is broad; narrow it to 10 years or less, or switch to one rough estimate before recommendations.'
            :'Details copied. Confirm the category, condition, model, and age range before recommendations.';
          document.getElementById('ewaste-age-min').focus();
        }else if(Number.isFinite(ageMin)){
          document.getElementById('ewaste-age-mode').value='single';
          document.getElementById('ewaste-age').value=ageMin.toFixed(1);
          updateEwasteAgeMode();
          status.textContent='Details copied. Confirm the category, condition, model, and age before recommendations.';
          document.getElementById('ewaste-age').focus();
        }
      }
    });
  }catch(error){
    status.textContent=error.message||'The device photo could not be analyzed.';
  }finally{
    button.disabled=false;
    button.textContent='Analyze device photo';
  }
});

document.getElementById('walking-profile')?.addEventListener('change',e=>{document.getElementById('walking-profile-other')?.classList.toggle('hidden',e.target.value!=='other');refreshCustomInputs();});
document.getElementById('bike-type')?.addEventListener('change',e=>{document.getElementById('bike-type-other')?.classList.toggle('hidden',e.target.value!=='other');refreshCustomInputs();});
document.getElementById('ewaste-condition')?.addEventListener('change',e=>{document.getElementById('ewaste-condition-other')?.classList.toggle('hidden',e.target.value!=='other');refreshCustomInputs();});
document.getElementById('community-role')?.addEventListener('change',e=>{document.getElementById('community-role-other')?.classList.toggle('hidden',e.target.value!=='other');refreshCustomInputs();});
for(const id of ['walking-profile-other','bike-type-other','ewaste-condition-other','community-role-other'])document.getElementById(id)?.addEventListener('input',refreshCustomInputs);
document.getElementById('ewaste-device')?.addEventListener('change',()=>{document.getElementById('ewaste-custom-item-fields')?.classList.toggle('hidden',document.getElementById('ewaste-device').value!=='custom');});
function ewasteCustomContext(){
  const pairs=[];
  if(document.getElementById('ewaste-device')?.value==='custom'){
    pairs.push(
      ['Item/category',document.getElementById('ewaste-custom-category')?.value],
      ['Brand',document.getElementById('ewaste-custom-brand')?.value],
      ['Model',document.getElementById('ewaste-custom-model')?.value],
      ['Year/age',document.getElementById('ewaste-custom-year')?.value],
      ['Condition',document.getElementById('ewaste-custom-condition')?.value],
      ['Battery/power',document.getElementById('ewaste-custom-battery')?.value],
      ['Accessories',document.getElementById('ewaste-custom-accessories')?.value],
      ['Notes',document.getElementById('ewaste-custom-notes')?.value]
    );
  }
  if(document.getElementById('ewaste-condition')?.value==='other'){
    pairs.push(['Other condition',document.getElementById('ewaste-condition-other')?.value]);
  }
  return pairs.filter(([,v])=>String(v||'').trim()).map(([k,v])=>`${k}: ${String(v).trim()}`).join('\\n');
}
document.getElementById('ewaste-recommend-button').addEventListener('click',async()=>{
  try{
  const status=document.getElementById('ewaste-recommend-status');
  const selectedDevice=document.getElementById('ewaste-device').value;
  const selectedCondition=document.getElementById('ewaste-condition').value;
  const ageSelection=getEwasteAgeSelection();
  if(!selectedDevice||!selectedCondition){
    status.textContent='Select a device/item and condition first.';
    return;
  }
  if(!ageSelection.ok){
    status.textContent=ageSelection.error;
    return;
  }
  status.textContent='Building recommendations…';
  const response=await fetchWithTimeout('/api/ewaste/recommend',{
    method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({
      device_type:document.getElementById('ewaste-device').value==='custom'?'small_equipment':document.getElementById('ewaste-device').value,
      condition:document.getElementById('ewaste-condition').value==='other'?'unknown':document.getElementById('ewaste-condition').value,
      age_years:Math.max(0,Math.min(100,ageSelection.midpoint)),
      powers_on:null,
      brand_model:[document.getElementById('ewaste-model').value.trim(),ewasteCustomContext(),ageSelection.context].filter(Boolean).join(' | ')||null
    })
  },12000);
  const data=await readApiResponse(response);
  status.textContent=data.disclaimer;
  document.getElementById('ewaste-recommendations').innerHTML=data.options.map(option=>`
    <article class="rounded-2xl bg-white/5 p-3 ring-1 ring-white/10">
      <div class="flex items-start justify-between gap-2"><h3 class="font-black text-lime-100">${escapeHtml(option.title)}</h3>${option.estimated_price_low!==null?`<span class="text-sm font-black text-white">$${option.estimated_price_low}–$${option.estimated_price_high}</span>`:''}</div>
      <details class="mt-2 rounded-lg bg-black/20 p-2"><summary class="cursor-pointer text-xs font-bold text-lime-200">Why EcoTrace recommends this</summary><p class="mt-2 text-xs leading-5 text-slate-300">${escapeHtml(option.why)}</p></details>
      <button class="ewaste-action-select mt-3 w-full rounded-lg bg-lime-400/15 px-3 py-2 text-xs font-bold text-lime-100 ring-1 ring-lime-300/20" data-action="${option.action}">Select this option</button>
      <div class="ewaste-instructions mt-2 hidden rounded-lg bg-slate-950/60 p-3"><ol class="list-decimal space-y-1 pl-4 text-xs leading-5 text-slate-300">${option.instructions.map(step=>`<li>${escapeHtml(step)}</li>`).join('')}</ol></div>
    </article>`).join('');
  document.querySelectorAll('.ewaste-action-select').forEach(button=>button.addEventListener('click',()=>{
    button.nextElementSibling.classList.toggle('hidden');
    button.textContent=button.nextElementSibling.classList.contains('hidden')?'Select this option':'Hide instructions';
  }));
  try{
    const advice=await readApiResponse(await fetchWithTimeout('/api/ewaste-gemini/advice',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({device_type:document.getElementById('ewaste-device').value,brand_model:document.getElementById('ewaste-model').value.trim()||null,age_years:ageSelection.midpoint,condition:document.getElementById('ewaste-condition').value||null,custom_context:[ewasteCustomContext(),ageSelection.context].filter(Boolean).join(' | ')||null})},45000));
    if(advice.available){
      document.getElementById('ewaste-recommendations').insertAdjacentHTML('afterbegin',`<article class="rounded-2xl bg-violet-400/10 p-3 ring-1 ring-violet-300/20"><p class="text-[10px] font-black uppercase tracking-wider text-violet-200">Gemini • item-specific recommendation</p><h3 class="mt-1 font-black text-white">${escapeHtml(advice.recommended_path||'Recommendation')}</h3><p class="mt-2 text-xs leading-5 text-slate-300">${escapeHtml(advice.summary||'')}</p><p class="mt-2 text-xs leading-5 text-slate-300"><strong>Why:</strong> ${escapeHtml(advice.why||'')}</p><details class="mt-2 rounded-lg bg-black/20 p-2" open><summary class="cursor-pointer text-xs font-bold text-violet-100">Specific preparation instructions</summary><ol class="mt-2 list-decimal space-y-1 pl-4 text-xs leading-5 text-slate-300">${(advice.instructions||[]).map(step=>`<li>${escapeHtml(step)}</li>`).join('')}</ol></details>${(advice.unknowns||[]).length?`<p class="mt-2 text-[10px] text-amber-200"><strong>Still unknown:</strong> ${(advice.unknowns||[]).map(escapeHtml).join(', ')}</p>`:''}</article>`);
    }
  }catch(error){console.warn('Gemini item-specific advice unavailable',error);}
  }catch(error){
    document.getElementById('ewaste-recommend-status').textContent=
      error.message||'Recommendations could not be built.';
  }
});

function ewasteReplyTypeLabel(value){
  const labels={
    question:'Question',
    interested:'Interested',
    offer:'Price offer',
    pickup:'Pickup coordination',
    seller_reply:'Seller reply'
  };
  return labels[value]||value;
}

async function loadEwasteListingReplies(listingId,card){
  const container=card.querySelector('.ewaste-listing-replies');
  container.innerHTML='<p class="text-xs text-slate-500">Loading messages…</p>';

  try{
    const response=await fetchWithTimeout(
      `/api/ewaste/listings/${encodeURIComponent(listingId)}/replies`,
      {},
      12000
    );
    const replies=await readApiResponse(response);

    container.innerHTML=replies.length
      ?replies.map(reply=>`
        <div class="rounded-lg bg-white/5 p-2 text-xs leading-5 text-slate-300">
          <div class="flex items-start justify-between gap-2">
            <p><strong class="text-lime-100">${escapeHtml(reply.display_name)}</strong></p>
            <span class="rounded-full bg-white/5 px-2 py-0.5 text-[9px] text-slate-400">${escapeHtml(ewasteReplyTypeLabel(reply.reply_type))}</span>
          </div>
          <p class="mt-1">${escapeHtml(reply.message)}</p>
          <p class="mt-1 text-[9px] text-slate-500">${escapeHtml(communityEscapeDate(reply.created_at))}</p>
        </div>
      `).join('')
      :'<p class="text-xs text-slate-500">No messages yet.</p>';
  }catch(error){
    container.innerHTML=`<p class="text-xs text-red-300">${escapeHtml(error.message||'Messages could not be loaded.')}</p>`;
  }
}

async function loadEwasteListings(){
  const list=document.getElementById('ewaste-listings');
  list.innerHTML='<p class="text-xs text-slate-400">Loading exchange listings…</p>';

  try{
    const response=await fetchWithTimeout('/api/ewaste/listings',{},12000);
    const items=await readApiResponse(response);

    if(!items.length){
      list.innerHTML='<p class="text-xs text-slate-400">No exchange listings yet.</p>';
      return;
    }

    list.innerHTML='';

    for(const item of items){
      const card=document.createElement('article');
      card.className='rounded-xl bg-white/5 p-3 ring-1 ring-white/10';
      card.innerHTML=`
        ${item.image_url?`<img src="${escapeHtml(item.image_url)}" class="mb-2 max-h-40 w-full rounded-lg object-contain" alt="${escapeHtml(item.title)}">`:''}
        <div class="flex justify-between gap-2">
          <div>
            <p class="text-[10px] font-black uppercase tracking-wider text-lime-200">${escapeHtml(item.listing_type.replace('_',' '))}</p>
            <h3 class="font-black text-white">${escapeHtml(item.title)}</h3>
          </div>
          <span class="font-black text-lime-200">${item.asking_price!==null?'$'+item.asking_price:'No price listed'}</span>
        </div>
        <p class="mt-2 text-xs leading-5 text-slate-300">${escapeHtml(item.description)}</p>
        <p class="mt-2 text-[10px] text-slate-500">Posted by ${escapeHtml(item.display_name)} • ${escapeHtml(item.location_text||'General location not listed')} • ${escapeHtml(communityEscapeDate(item.created_at))}</p>

        <details class="mt-3 rounded-xl bg-black/20 p-2">
          <summary class="cursor-pointer text-xs font-bold text-lime-100">Messages and interest (${item.reply_count||0})</summary>
          <div class="ewaste-listing-replies mt-2 space-y-2">
            <p class="text-xs text-slate-500">Open to load messages.</p>
          </div>

          <div class="mt-3 grid grid-cols-2 gap-2">
            <input class="ewaste-reply-name rounded-lg border border-white/10 bg-slate-950/70 px-2 py-1.5 text-xs text-white" maxlength="40" placeholder="Display name" autocomplete="off">
            <select class="ewaste-reply-type rounded-lg border border-white/10 bg-slate-950/70 px-2 py-1.5 text-xs text-white">
              <option value="question">Ask a question</option>
              <option value="interested">I'm interested</option>
              <option value="offer">Make a price offer</option>
              <option value="pickup">Discuss pickup</option>
              <option value="seller_reply">Seller reply</option>
            </select>
          </div>
          <textarea class="ewaste-reply-message mt-2 w-full rounded-lg border border-white/10 bg-slate-950/70 px-2 py-2 text-xs text-white" maxlength="1000" rows="2" placeholder="Write a public message about the item, price, or safe public pickup details."></textarea>
          <button class="ewaste-reply-submit mt-2 w-full rounded-lg bg-lime-400 px-3 py-2 text-xs font-black text-lime-950" data-listing-id="${item.id}">Post message</button>
          <p class="ewaste-reply-status mt-2 text-[10px] text-slate-400"></p>
        </details>
      `;

      if(!item.can_edit)card.querySelector('option[value="seller_reply"]')?.remove();
      list.appendChild(card);

      const details=card.querySelector('details');
      details.addEventListener('toggle',async()=>{
        if(details.open){
          await loadEwasteListingReplies(item.id,card);
        }
      });

      const submit=card.querySelector('.ewaste-reply-submit');
      submit.addEventListener('click',async()=>{
        if(!currentEcoTraceAccount){alert('Sign in to send e-waste messages.');return;}
        const name=card.querySelector('.ewaste-reply-name').value.trim();
        const message=card.querySelector('.ewaste-reply-message').value.trim();
        const replyType=card.querySelector('.ewaste-reply-type').value;
        const status=card.querySelector('.ewaste-reply-status');

        if(!name||!message){
          status.textContent='Enter a display name and message.';
          return;
        }

        submit.disabled=true;
        submit.textContent='Posting…';
        status.textContent='Posting public message…';

        try{
          const replyResponse=await fetchWithTimeout(
            `/api/ewaste/listings/${encodeURIComponent(item.id)}/replies`,
            {
              method:'POST',
              headers:{'Content-Type':'application/json'},
              body:JSON.stringify({
                display_name:safeDisplayName(name),
                message,
                reply_type:replyType
              })
            },
            12000
          );
          await readApiResponse(replyResponse);
          card.querySelector('.ewaste-reply-message').value='';
          status.textContent='Message posted.';
          await loadEwasteListingReplies(item.id,card);
        }catch(error){
          status.textContent=error.message||'Message could not be posted.';
        }finally{
          submit.disabled=false;
          submit.textContent='Post message';
        }
      });
    }
    attachEwasteOwnerActions(list,items);
  }catch(error){
    list.innerHTML=`<p class="rounded-xl bg-red-400/10 p-3 text-xs text-red-200">${escapeHtml(error.message||'Exchange listings could not be loaded.')}</p>`;
  }
}

document.getElementById('ewaste-listing-submit').addEventListener('click',async()=>{
  if(!currentEcoTraceAccount){alert('Sign in to create or change posts.');return;}
  const status=document.getElementById('ewaste-listing-status');status.textContent='Posting listing…';
  let imageUrl=null;if(selectedEwasteFile)imageUrl=await uploadSelectedImage(selectedEwasteFile,'ewaste','ewaste-listing-status');
  const payload={display_name:safeDisplayName(document.getElementById('ewaste-listing-name').value),listing_type:document.getElementById('ewaste-listing-type').value,device_type:document.getElementById('ewaste-listing-device').value,title:document.getElementById('ewaste-listing-title').value.trim(),description:document.getElementById('ewaste-listing-description').value.trim(),asking_price:document.getElementById('ewaste-listing-price').value?Number(document.getElementById('ewaste-listing-price').value):null,image_url:imageUrl,location_text:document.getElementById('ewaste-listing-location').value.trim()||null,latitude:null,longitude:null};
  const response=await fetchWithTimeout('/api/ewaste/listings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)},12000);await readApiResponse(response);
  status.textContent='Listing posted.';selectedEwasteFile=null;document.getElementById('ewaste-photo-preview').classList.add('hidden');loadEwasteListings();
});

const EWASTE_EVENT_TOKENS=JSON.parse(localStorage.getItem('ecotraceEwasteEventTokens')||'{}');
function saveEwasteEventTokens(){
  localStorage.setItem('ecotraceEwasteEventTokens',JSON.stringify(EWASTE_EVENT_TOKENS));
}
function ownsEwasteEvent(id){
  const e=(window.latestEwasteEvents||[]).find(e=>String(e.id)===String(id));
  return Boolean(currentEcoTraceAccount&&(e?.can_edit||(e?.legacy_author_token&&EWASTE_EVENT_TOKENS[String(id)])));
}

async function markEwasteEventDone(eventId,button){
  const token=EWASTE_EVENT_TOKENS[String(eventId)];
  if(!ownsEwasteEvent(eventId))return;
  button.disabled=true;
  button.textContent='Archiving…';
  try{
    const eventRecord=(window.latestEwasteEvents||[]).find(item=>String(item.id)===String(eventId));
    await readApiResponse(await fetchWithTimeout(
      `/api/ewaste/events/${encodeURIComponent(eventId)}/complete?edit_token=${encodeURIComponent(token)}`,
      {method:'POST'},
      10000
    ));
    openImpactRecorder({
      id:eventId,
      kind:'ewaste',
      title:eventRecord?.title||'Completed e-waste event',
      participants:eventRecord?.participant_count||0
    });
    await loadEwasteEvents();
    await loadEwasteArchive();
  }catch(error){
    button.disabled=false;
    button.textContent='Mark done';
    document.getElementById('ewaste-event-status').textContent=
      error.message||'Could not archive event.';
  }
}

function ewasteEventCard(event,{archived=false}={}){
  return `<article class="rounded-xl ${archived?'bg-slate-400/5':'bg-white/5'} p-3 ring-1 ring-white/10">
    <div class="flex items-start justify-between gap-2">
      <div>
        <h3 class="font-black text-white">${escapeHtml(event.title)}</h3>
        <p class="mt-1 text-xs text-violet-200">${escapeHtml(communityEscapeDate(event.event_date))}</p>
      </div>
      ${archived?`<span class="rounded-full bg-slate-400/10 px-2 py-1 text-[9px] font-bold text-slate-300">${ecoEventPastDate(event)?'Past date':event.event_status==='cancelled'?'Cancelled':'Done'}</span>`:''}
    </div>
    <p class="mt-1 text-xs text-lime-200">${escapeHtml(event.duration_text||'Undecided')} • Maximum ${event.max_participants} people</p>
    <p class="mt-1 text-xs text-slate-400">${escapeHtml(event.location_text)}</p>
    <p class="mt-2 text-xs leading-5 text-slate-300">${escapeHtml(event.description)}</p>
    ${!archived
      ?`<div class="mt-2 flex gap-2">
          <input class="ewaste-join-name min-w-0 flex-1 rounded-lg border border-white/10 bg-slate-950/70 px-2 py-1.5 text-xs text-white" placeholder="Display name" autocomplete="off">
          <button class="ewaste-event-join rounded-lg bg-lime-400 px-2 py-1.5 text-xs font-black text-lime-950" data-id="${event.id}">Join (${event.participant_count}/${event.max_participants})</button>
        </div>
        ${ownsEwasteEvent(event.id)
          ?`<button class="ewaste-event-done mt-2 w-full rounded-lg bg-slate-300/10 px-3 py-2 text-xs font-bold text-slate-200 ring-1 ring-white/10" data-id="${event.id}">Mark done</button>`
          :''}`
      :`<p class="mt-2 text-[10px] text-slate-500">${event.participant_count} participant${event.participant_count===1?'':'s'} recorded.</p>`}
  </article>`;
}

async function loadEwasteArchive(){
  const list=document.getElementById('ewaste-archive-list');
  if(!list)return;
  list.innerHTML='<p class="text-xs text-slate-400">Loading archive…</p>';
  try{
    const events=await readApiResponse(await fetchWithTimeout('/api/ewaste/events',{},8000));
    const archived=events.filter(event=>
      ecoEventArchived(event)
    );
    list.innerHTML=archived.length
      ?archived.map(event=>ewasteEventCard(event,{archived:true})).join('')
      :'<p class="text-xs text-slate-400">No archived collection events yet.</p>';
  }catch(error){
    list.innerHTML=`<p class="text-xs text-red-300">${escapeHtml(error.message||'Archive unavailable.')}</p>`;
  }
}

async function loadEwasteEvents(){
  const list=document.getElementById('ewaste-event-list');
  list.innerHTML='<p class="text-xs text-slate-400">Loading collection events…</p>';
  try{
    const events=await readApiResponse(await fetchWithTimeout('/api/ewaste/events',{},8000));
    window.latestEwasteEvents=events;
    const active=events.filter(event=>
      !ecoEventArchived(event)
    );

    list.innerHTML=active.length
      ?active.map(event=>ewasteEventCard(event)).join('')
      :'<p class="text-xs text-slate-400">No active collection events yet.</p>';

    list.querySelectorAll('.ewaste-event-join').forEach(button=>{
      button.addEventListener('click',async()=>{
        const name=button.parentElement.querySelector('.ewaste-join-name').value.trim();
        if(!name)return;
        button.disabled=true;
        button.textContent='Joining…';
        try{
          await readApiResponse(await fetchWithTimeout(
            `/api/ewaste/events/${button.dataset.id}/join`,
            {
              method:'POST',
              headers:{'Content-Type':'application/json'},
              body:JSON.stringify({display_name:safeDisplayName(name)})
            },
            8000
          ));
          await loadEwasteEvents();
        }catch(error){
          button.disabled=false;
          button.textContent='Join';
        }
      });
    });

    list.querySelectorAll('.ewaste-event-done').forEach(button=>{
      button.addEventListener('click',()=>markEwasteEventDone(button.dataset.id,button));
    });
    applyPostPermissions();
  }catch(error){
    list.innerHTML=`<p class="rounded-lg bg-red-400/10 p-2 text-xs text-red-200">${escapeHtml(error.message||'Events could not be loaded.')}</p>`;
  }
}

document.getElementById('ewaste-event-submit').addEventListener('click',async()=>{
  if(!currentEcoTraceAccount){alert('Sign in to create or change posts.');return;}
  const status=document.getElementById('ewaste-event-status');
  const button=document.getElementById('ewaste-event-submit');
  const payload={
    host_name:document.getElementById('ewaste-event-host').value.trim(),
    title:document.getElementById('ewaste-event-title').value.trim(),
    description:document.getElementById('ewaste-event-description').value.trim(),
    event_date:document.getElementById('ewaste-event-date').value,
    duration_text:document.getElementById('ewaste-event-duration').value||'Undecided',
    location_text:document.getElementById('ewaste-event-location').value.trim(),
    max_participants:Number(document.getElementById('ewaste-event-max').value)||50,
    image_url:null,latitude:null,longitude:null
  };
  if(!payload.host_name||!payload.title||!payload.description||!payload.event_date||!payload.location_text){
    status.textContent='Complete the host, title, details, date/time, and public location.';
    return;
  }
  button.disabled=true;
  button.textContent='Creating…';
  status.textContent='Creating event…';
  try{
    const response=await fetchWithTimeout('/api/ewaste/events',{
      method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)
    },8000);
    const createdEvent=await readApiResponse(response);
    if(createdEvent.edit_token){
      EWASTE_EVENT_TOKENS[String(createdEvent.id)]=createdEvent.edit_token;
      saveEwasteEventTokens();
    }
    status.textContent='Event created.';
    button.textContent='Created ✓';
    setTimeout(()=>{button.textContent='Create event';},1200);
    loadEwasteEvents();
  }catch(error){
    status.textContent=error.message||'Event could not be created.';
    button.textContent='Create event';
  }finally{
    button.disabled=false;
  }
});

function updateEwasteCenterMap(centers){
  const source=map.getSource('ewaste-centers');
  if(!source)return;
  source.setData({
    type:'FeatureCollection',
    features:(centers||[]).map((center,index)=>({
      type:'Feature',
      properties:{
        name:center.name,
        short_name:String(center.name).slice(0,28),
        distance_miles:center.distance_miles,
        opening_hours:center.opening_hours||'',
        description:center.description,
        verification_note:center.verification_note,
        rank:index+1
      },
      geometry:{
        type:'Point',
        coordinates:[center.longitude,center.latitude]
      }
    }))
  });
}

function centerHoursText(center){
  return center.opening_hours
    ?center.opening_hours
    :'Opening hours are not mapped';
}

function renderCenterCard(center,index,closest=false){
  const items=center.accepted_items?.length
    ?`<p class="mt-2 text-xs text-cyan-200"><strong>Mapped accepted items:</strong> ${center.accepted_items.map(escapeHtml).join(', ')}</p>`
    :'';
  return `<article class="rounded-2xl ${closest?'bg-lime-400/10 ring-lime-300/30':'bg-white/5 ring-white/10'} p-3 ring-1">
    <div class="flex items-start justify-between gap-2">
      <div>
        ${closest?`<p class="text-[10px] font-black uppercase tracking-wider text-lime-200">Closest #${index+1}</p>`:''}
        <h3 class="font-black text-white">${escapeHtml(center.name)}</h3>
      </div>
      <span class="whitespace-nowrap text-sm font-black text-lime-200">${center.distance_miles} mi</span>
    </div>
    <p class="mt-1 text-xs text-slate-300">${escapeHtml(center.display_name||'Address not mapped')}</p>
    <p class="mt-2 text-xs leading-5 text-slate-300">${escapeHtml(center.description)}</p>
    <div class="mt-2 rounded-lg bg-black/20 p-2 text-xs leading-5 text-violet-200">
      <p><strong>Open / closed schedule:</strong> ${escapeHtml(centerHoursText(center))}</p>
      <p class="mt-1 text-[10px] text-slate-400">Use the official-hours link when available because holiday hours and closures can change.</p>
    </div>
    ${items}
    ${center.phone?`<p class="mt-2 text-xs text-slate-300"><strong>Phone:</strong> ${escapeHtml(center.phone)}</p>`:''}
    ${center.website?`<a class="mt-2 block text-xs font-bold text-sky-300 underline" href="${escapeHtml(center.website)}" target="_blank" rel="noopener noreferrer">Open facility website</a>`:''}
    <div class="mt-3 grid grid-cols-2 gap-2">
      <button class="ewaste-center-map rounded-lg bg-lime-400/15 px-2 py-2 text-xs font-bold text-lime-100 ring-1 ring-lime-300/20" data-lat="${center.latitude}" data-lon="${center.longitude}">Show on map</button>
      ${center.website
        ?`<a class="rounded-lg bg-violet-400/15 px-2 py-2 text-center text-xs font-bold text-violet-100 ring-1 ring-violet-300/20" href="${escapeHtml(center.website)}" target="_blank" rel="noopener noreferrer">Official hours / website</a>`
        :`<div class="rounded-lg bg-white/5 px-2 py-2 text-center text-xs text-slate-400 ring-1 ring-white/10">No official hours link</div>`}
      <button class="ewaste-center-walk rounded-lg bg-sky-400/15 px-2 py-2 text-xs font-bold text-sky-100 ring-1 ring-sky-300/20" data-lat="${center.latitude}" data-lon="${center.longitude}" data-name="${escapeHtml(center.name)}" data-address="${escapeHtml(center.display_name||center.name)}">Walk here</button>
      <button class="ewaste-center-bike rounded-lg bg-cyan-400/15 px-2 py-2 text-xs font-bold text-cyan-100 ring-1 ring-cyan-300/20" data-lat="${center.latitude}" data-lon="${center.longitude}" data-name="${escapeHtml(center.name)}" data-address="${escapeHtml(center.display_name||center.name)}">Bike here</button>
    </div>
    <p class="mt-2 text-[10px] leading-4 text-amber-200">${escapeHtml(center.verification_note)}</p>
  </article>`;
}

function attachCenterButtons(container){
  container.querySelectorAll('.ewaste-center-map').forEach(button=>{
    button.addEventListener('click',()=>{
      latestSelectedEwasteCenter={
        name:button.closest('article')?.querySelector('h3')?.textContent||'Selected center',
        latitude:Number(button.dataset.lat),
        longitude:Number(button.dataset.lon)
      };
      map.flyTo({center:[Number(button.dataset.lon),Number(button.dataset.lat)],zoom:16,essential:true});
    });
  });
  container.querySelectorAll('.ewaste-center-walk').forEach(button=>{
    button.addEventListener('click',()=>{
      showToolPanel('walk-panel');
      clearRoute();
      const destination={
        lat:Number(button.dataset.lat),
        lon:Number(button.dataset.lon)
      };
      document.getElementById('route-end').value=button.dataset.address;
      routeClicks=[null,destination];
      document.getElementById('route-status').textContent=`Destination selected: ${button.dataset.name}. Enter a starting address or click your starting point.`;
      document.getElementById('status-pill').textContent='Walking destination ready — choose your starting point';
    });
  });
  container.querySelectorAll('.ewaste-center-bike').forEach(button=>{
    button.addEventListener('click',()=>{
      showToolPanel('bike-panel');
      clearBike();
      bikeQuickRouteMode=true;
      const destination={
        lat:Number(button.dataset.lat),
        lon:Number(button.dataset.lon)
      };
      document.getElementById('bike-end').value=button.dataset.address;
      bikeClicks=[null,destination];
      document.getElementById('bike-status').textContent=`Destination selected: ${button.dataset.name}. Enter a starting address or click your starting point.`;
      document.getElementById('status-pill').textContent='Bike destination ready — choose your starting point';
    });
  });

}

document.getElementById('ewaste-center-use-point').addEventListener('click',()=>{
  ewasteCenterSelectionMode=true;
  document.getElementById('ewaste-center-point-box').textContent='Click a point on the map…';
  document.getElementById('status-pill').textContent='E-waste centers — choose a search point';
});

function applyEwasteCenterVisibility(){
  for(const id of ['ewaste-center-points','ewaste-center-labels']){
    if(map.getLayer(id)){
      map.setLayoutProperty(id,'visibility',ewasteCenterPointsVisible?'visible':'none');
    }
  }
  const button=document.getElementById('ewaste-center-map-toggle');
  if(button){
    button.textContent=ewasteCenterPointsVisible
      ?'Hide E-Waste center points'
      :'Show E-Waste center points';
    button.classList.toggle('bg-red-400/15',ewasteCenterPointsVisible);
    button.classList.toggle('text-red-100',ewasteCenterPointsVisible);
    button.classList.toggle('ring-red-300/20',ewasteCenterPointsVisible);
    button.classList.toggle('bg-lime-400/15',!ewasteCenterPointsVisible);
    button.classList.toggle('text-lime-100',!ewasteCenterPointsVisible);
    button.classList.toggle('ring-lime-300/20',!ewasteCenterPointsVisible);
  }
}

document.getElementById('ewaste-center-map-toggle').addEventListener('click',()=>{
  ewasteCenterPointsVisible=!ewasteCenterPointsVisible;
  localStorage.setItem('ecotraceEwasteCentersVisible',String(ewasteCenterPointsVisible));
  applyEwasteCenterVisibility();
});

document.getElementById('ewaste-center-address-search').addEventListener('click',async()=>{
  const input=document.getElementById('ewaste-center-address');
  const status=document.getElementById('ewaste-center-status');
  const query=input.value.trim();
  if(!query){
    status.textContent='Enter an address or place first.';
    return;
  }
  status.textContent='Searching for that address…';
  try{
    const data=await geocodeAddress(query);
    lastResolvedLocation={lat:Number(data.latitude),lon:Number(data.longitude),label:data.display_name||query};
    const result=Array.isArray(data)?data[0]:data;
    const lat=Number(result.latitude??result.lat);
    const lon=Number(result.longitude??result.lon);
    if(!Number.isFinite(lat)||!Number.isFinite(lon))throw new Error('Address was not found.');
    ewasteCenterSearchPoint={lat,lon};
    document.getElementById('ewaste-center-point-box').textContent=`Using address: ${result.display_name||query}`;
    map.flyTo({center:[lon,lat],zoom:14,essential:true});
    status.textContent='Address found. Searching nearby E-Waste centers…';
    document.getElementById('ewaste-center-search').click();
  }catch(error){
    status.textContent=error.message||'Address could not be found.';
  }
});

document.getElementById('ewaste-center-search').addEventListener('click',async()=>{
  const rawPoint=ewasteCenterSearchPoint||map.getCenter();
  const point={
    lat:Number(rawPoint.lat),
    lon:Number(rawPoint.lon??rawPoint.lng)
  };
  const status=document.getElementById('ewaste-center-status');
  const closest=document.getElementById('ewaste-closest-centers');
  const all=document.getElementById('ewaste-center-list');

  if(!Number.isFinite(point.lat)||!Number.isFinite(point.lon)){
    status.textContent='The selected address did not return valid coordinates. Please try another address.';
    return;
  }

  status.textContent='Searching all mapped recycling locations around the selected point…';
  closest.innerHTML='<p class="text-xs text-slate-400">Finding the two closest locations…</p>';
  all.innerHTML='<p class="text-xs text-slate-400">Loading all mapped locations…</p>';

  try{
    const data=await readApiResponse(await fetchWithTimeout(
      `/api/ewaste/centers?lat=${encodeURIComponent(point.lat)}&lon=${encodeURIComponent(point.lon)}`,
      {},
      55000
    ));

    status.textContent=`${data.message} ${data.data_note}`;
    updateEwasteCenterMap(data.centers);

    closest.innerHTML=data.closest_two.length
      ?`<h3 class="mb-2 text-sm font-black text-lime-100">Two closest locations</h3>${data.closest_two.map((center,index)=>renderCenterCard(center,index,true)).join('')}`
      :'<p class="rounded-xl bg-amber-400/10 p-3 text-xs text-amber-200">No mapped recycling locations were found in this radius.</p>';

    all.innerHTML=data.centers.length
      ?data.centers.map((center,index)=>renderCenterCard(center,index,false)).join('')
      :'<p class="text-xs text-slate-400">No mapped locations found. Increase the radius or choose another point.</p>';

    attachCenterButtons(closest);
    attachCenterButtons(all);

    if(data.centers.length){
      const bounds=new maplibregl.LngLatBounds();
      bounds.extend([point.lon,point.lat]);
      data.centers.forEach(center=>bounds.extend([center.longitude,center.latitude]));
      map.fitBounds(bounds,{padding:{top:80,bottom:80,left:440,right:80},maxZoom:13,duration:900});
    }
  }catch(error){
    status.textContent=error.message||'Center search failed.';
    closest.innerHTML='';
    all.innerHTML='';
  }
});


function openSuitableTrees(size){
  showToolPanel('tree-guide-panel');
  const normalizedSize=String(size||'').toLowerCase();
  const sizeSelect=document.getElementById('tree-guide-size');
  if(sizeSelect && ['small','medium','large'].includes(normalizedSize)){
    sizeSelect.value=normalizedSize;
  }
  const panel=document.getElementById('tree-guide-panel');
  if(panel){
    panel.scrollIntoView({behavior:'smooth',block:'start'});
  }
  const status=document.getElementById('tree-guide-status');
  if(status){
    status.textContent=normalizedSize
      ?`Showing ${normalizedSize}-size tree candidates for this site…`
      :'Opening the Tree Library for site-specific review…';
  }
  loadTreeGuide();
}

async function loadTreeGuide(){
  const results=document.getElementById('tree-guide-results');
  const status=document.getElementById('tree-guide-status');
  status.textContent='Loading the complete Santa Clara tree list…';
  results.innerHTML='<p class="rounded-xl bg-white/5 p-3 text-xs text-slate-400">Loading tree descriptions and verified species images…</p>';

  const params=new URLSearchParams();
  const size=document.getElementById('tree-guide-size').value;
  const typeCode=document.getElementById('tree-guide-type').value;
  const height=document.getElementById('tree-guide-height').value;
  const width=document.getElementById('tree-guide-width').value;
  const flower=document.getElementById('tree-guide-flower').value;
  if(size)params.set('size',size);
  if(typeCode)params.set('type_code',typeCode);
  if(height)params.set('max_height_ft',height);
  if(width)params.set('max_width_ft',width);
  if(flower)params.set('flower',flower);

  try{
    const [trees,notice]=await Promise.all([
      readApiResponse(await fetchWithTimeout(`/api/tree-guide/trees?${params.toString()}`,{},12000)),
      readApiResponse(await fetchWithTimeout('/api/tree-guide/notice',{},12000))
    ]);

    const definitions=Object.entries(notice.type_definitions||{})
      .map(([code,definition])=>`<p><strong>${escapeHtml(code)}:</strong> ${escapeHtml(definition)}</p>`)
      .join('');
    const notes=(notice.notes||[])
      .map(note=>`<li>${escapeHtml(note)}</li>`)
      .join('');

    document.getElementById('tree-guide-notice').innerHTML=`
      <strong>${escapeHtml(notice.title)}</strong>
      <p class="mt-1">${escapeHtml(notice.message)}</p>
      <details class="mt-2 rounded-lg bg-black/15 p-2">
        <summary class="cursor-pointer font-bold">City notes and type definitions</summary>
        <ul class="mt-2 list-disc space-y-1 pl-4">${notes}</ul>
        <div class="mt-2 space-y-1">${definitions}<p><strong>n/a:</strong> ${escapeHtml(notice.na_definition||'not applicable')}</p></div>
        <p class="mt-2"><strong>Questions:</strong> ${escapeHtml(notice.contact?.department||'City Street Division')} • ${escapeHtml(notice.contact?.phone||'')} • <a class="underline" href="mailto:${escapeHtml(notice.contact?.email||'')}">${escapeHtml(notice.contact?.email||'')}</a></p>
      </details>`;

    status.textContent=`Showing ${trees.length} matching trees.`;
    results.innerHTML=trees.length?trees.map(tree=>`
      <article class="overflow-hidden rounded-2xl bg-white/5 ring-1 ring-white/10">
        <div class="tree-photo-wrap relative h-48 bg-slate-900" data-tree-id="${escapeHtml(tree.id)}" data-botanical="${escapeHtml(tree.botanical_name)}" data-common="${escapeHtml(tree.common_name)}" data-direct-image="${escapeHtml(tree.image_url||'')}">
          <div class="absolute inset-0 flex items-center justify-center text-5xl">🌳</div>
          <img class="tree-guide-photo relative hidden h-full w-full object-cover" alt="${escapeHtml(tree.common_name)} tree" />
        </div>
        <div class="p-3">
          <div class="flex items-start justify-between gap-2">
            <div>
              <h3 class="font-black text-white">${escapeHtml(tree.common_name)}</h3>
              <p class="text-xs italic text-slate-400">${escapeHtml(tree.botanical_name)}</p>
            </div>
            <div class="text-right">
              <span class="rounded-full bg-green-400/15 px-2 py-1 text-[10px] font-bold text-green-200">${escapeHtml(tree.size.toUpperCase())}</span>
              ${tree.approved_list?'':`<p class="mt-1 text-[9px] font-bold text-amber-300">Supplemental — confirm approval</p>`}
            </div>
          </div>

          <div class="mt-3 grid grid-cols-4 gap-2 text-center">
            <div class="rounded-lg bg-black/20 p-2"><p class="text-[9px] uppercase text-slate-500">Height</p><p class="text-xs font-black">${tree.height_ft} ft</p></div>
            <div class="rounded-lg bg-black/20 p-2"><p class="text-[9px] uppercase text-slate-500">Width</p><p class="text-xs font-black">${tree.width_ft} ft</p></div>
            <div class="rounded-lg bg-black/20 p-2"><p class="text-[9px] uppercase text-slate-500">Type</p><p class="text-xs font-black">${escapeHtml(({D:'Deciduous',E:'Evergreen',SE:'Semi-evergreen'})[tree.type_code]||tree.type_code)}</p></div>
            <div class="rounded-lg bg-black/20 p-2"><p class="text-[9px] uppercase text-slate-500">Flower</p><p class="text-xs font-black">${escapeHtml(tree.flower)}</p></div>
          </div>

          <p class="mt-3 text-xs leading-5 text-slate-300">${escapeHtml(tree.description)}</p>

          <details class="mt-3 rounded-lg bg-black/20 p-2">
            <summary class="cursor-pointer text-xs font-bold text-green-100">Full tree details</summary>
            <div class="mt-2 space-y-1 text-xs leading-5 text-slate-300">
              <p><strong>Foliage:</strong> ${escapeHtml(tree.type_description)}</p>
              <p><strong>Growth:</strong> ${escapeHtml(tree.growth_rate)}</p>
              <p><strong>Water use:</strong> ${escapeHtml(tree.water_use)}</p>
              <p><strong>Canopy:</strong> ${escapeHtml(tree.canopy)}</p>
              <p><strong>Planting notes:</strong> ${escapeHtml(tree.planting_notes||'Final selection depends on City review and seasonal inventory.')}</p>
            </div>
          </details>
        </div>
      </article>`).join(''):'<p class="rounded-xl bg-amber-400/10 p-3 text-xs text-amber-200">No trees match every selected filter.</p>';

    document.querySelectorAll('.tree-photo-wrap').forEach(async wrap=>{
      const img=wrap.querySelector('img');
      const direct=wrap.dataset.directImage;
      if(direct){
        img.src=direct;
        img.onload=()=>img.classList.remove('hidden');
        img.onerror=()=>img.classList.add('hidden');
        return;
      }

      try{
        const botanical=wrap.dataset.botanical||'';
        const common=wrap.dataset.common||'';
        const treeId=wrap.dataset.treeId||'';
        const exactTerms={
          'crape-myrtle':'Lagerstroemia indica',
          'geijera':'Geijera parviflora',
          'chinese-hackberry':'Celtis sinensis',
          'saratoga-laurel':'Laurus nobilis',
          'ginkgo-male':'Ginkgo biloba',
          'black-scarlet-oak':'Quercus coccinea',
          'columbia-sycamore':'Platanus acerifolia',
          'autumn-purple-ash':'Fraxinus americana'
        };
        const searchTerm=exactTerms[treeId]||botanical.replace(/['“”].*?['“”]/g,'').replace(/\bcultivars?\b/gi,'').trim();
        const genus=(searchTerm.split(' ')[0]||'').replace(/[^A-Za-z]/g,'').toLowerCase();

        try{
          const summaryResponse=await fetch(`https://en.wikipedia.org/api/rest_v1/page/summary/${encodeURIComponent(searchTerm)}`);
          if(summaryResponse.ok){
            const summary=await summaryResponse.json();
            const thumbnail=summary.thumbnail?.source||summary.originalimage?.source||null;
            if(thumbnail){
              img.src=thumbnail;
              img.onload=()=>img.classList.remove('hidden');
              img.onerror=()=>img.classList.add('hidden');
              return;
            }
          }
        }catch(_){}

        const queries=[`"${searchTerm}" tree`,`"${common}" tree`];
        const banned=['map','range','distribution','historic','archive','document','catalog','seed','list','page','book','herbarium','diagram','logo','stamp','drawing','illustration','poster','advertisement','black and white','engraving'];
        let source=null;

        for(const query of queries){
          const api=`https://commons.wikimedia.org/w/api.php?action=query&generator=search&gsrsearch=${encodeURIComponent(query)}&gsrnamespace=6&gsrlimit=15&prop=imageinfo&iiprop=url&iiurlwidth=1000&format=json&origin=*`;
          const response=await fetch(api);
          if(!response.ok)continue;
          const data=await response.json();
          const pages=Object.values(data.query?.pages||{});
          const valid=pages.find(page=>{
            const title=(page.title||'').toLowerCase();
            if(banned.some(word=>title.includes(word)))return false;
            const ext=title.split('.').pop();
            if(!['jpg','jpeg','png','webp'].includes(ext))return false;
            return title.includes(genus)||title.includes(common.split(' ')[0].toLowerCase());
          });
          source=valid?.imageinfo?.[0]?.thumburl||valid?.imageinfo?.[0]?.url||null;
          if(source)break;
        }

        if(!source)return;
        img.src=source;
        img.onload=()=>img.classList.remove('hidden');
        img.onerror=()=>img.classList.add('hidden');
      }catch(_){}
    });
  }catch(error){
    status.textContent=error.message||'Tree guide could not be loaded.';
    results.innerHTML='';
  }
}

document.getElementById('tree-guide-search').addEventListener('click',loadTreeGuide);
for(const id of ['tree-guide-size','tree-guide-type','tree-guide-height','tree-guide-width','tree-guide-flower']){
  document.getElementById(id).addEventListener('change',loadTreeGuide);
}


function updateCommunityMapPoints(posts){
  const source=map.getSource('community-points');
  if(!source)return;
  const features=(posts||[])
    .filter(post=>post.latitude!==null&&post.longitude!==null)
    .map(post=>({
      type:'Feature',
      properties:{
        id:post.id,
        title:post.title,
        category:post.category,
        description:parseCommunityStoredDetails(post.description).base,
        image_url:post.image_url||'',
        location_text:post.location_text||''
      },
      geometry:{type:'Point',coordinates:[post.longitude,post.latitude]}
    }));
  source.setData({type:'FeatureCollection',features});
}

document.getElementById('community-map-toggle').addEventListener('click',event=>{
  communityPointsVisible=!communityPointsVisible;
  for(const id of ['community-clusters','community-cluster-count','community-unclustered']){
    if(map.getLayer(id))map.setLayoutProperty(id,'visibility',communityPointsVisible?'visible':'none');
  }
  event.currentTarget.textContent=communityPointsVisible?'Hide map points':'Show map points';
});

map.on('load',async()=>{
  map.addSource('community-points',{type:'geojson',data:{type:'FeatureCollection',features:[]},cluster:true,clusterMaxZoom:14,clusterRadius:45});
  map.addSource('ewaste-centers',{type:'geojson',data:{type:'FeatureCollection',features:[]}});
  map.addLayer({id:'ewaste-center-points',type:'circle',source:'ewaste-centers',paint:{'circle-color':['case',['==',['get','rank'],1],'#84cc16',['==',['get','rank'],2],'#bef264','#22c55e'],'circle-radius':['case',['<=',['get','rank'],2],10,7],'circle-stroke-color':'#ffffff','circle-stroke-width':2}});
  map.addLayer({id:'ewaste-center-labels',type:'symbol',source:'ewaste-centers',minzoom:11,layout:{'text-field':['get','short_name'],'text-size':11,'text-offset':[0,1.3],'text-anchor':'top'},paint:{'text-color':'#ecfccb','text-halo-color':'#052e16','text-halo-width':1.5}});
  applyEwasteCenterVisibility();
  map.on('click','ewaste-center-points',event=>{
    const feature=event.features?.[0];if(!feature)return;
    const p=feature.properties;
    new maplibregl.Popup().setLngLat(event.lngLat).setHTML(`<strong>${escapeHtml(p.name)}</strong><br>${escapeHtml(p.distance_miles)} miles away<br><br><strong>Hours:</strong> ${escapeHtml(p.opening_hours||'Not mapped')}<br><br>${escapeHtml(p.description)}<br><br><small>${escapeHtml(p.verification_note)}</small>`).addTo(map);
  });

  map.addLayer({id:'community-clusters',type:'circle',source:'community-points',filter:['has','point_count'],paint:{'circle-color':'#f59e0b','circle-radius':['step',['get','point_count'],16,10,22,30,28],'circle-stroke-color':'#fff7ed','circle-stroke-width':2}});
  map.addLayer({id:'community-cluster-count',type:'symbol',source:'community-points',filter:['has','point_count'],layout:{'text-field':['get','point_count_abbreviated'],'text-size':12},paint:{'text-color':'#111827'}});
  map.addLayer({id:'community-unclustered',type:'circle',source:'community-points',filter:['!',['has','point_count']],paint:{'circle-color':['match',['get','category'],'tree_suggestion','#22c55e','park_cleanup','#f59e0b','environmental_campaign','#eab308','discussion','#a855f7','heat_report','#ef4444','#38bdf8'],'circle-radius':8,'circle-stroke-color':'#ffffff','circle-stroke-width':2}});
  map.on('click','community-unclustered',event=>{
    const feature=event.features?.[0];if(!feature)return;
    const p=feature.properties;
    new maplibregl.Popup().setLngLat(event.lngLat).setHTML(`<strong>${escapeHtml(p.title)}</strong><br><span>${escapeHtml(communityCategoryLabel(p.category))}</span><br><br>${escapeHtml(p.description)}${p.image_url?`<br><img src="${escapeHtml(p.image_url)}" style="max-width:220px;max-height:140px;margin-top:8px;border-radius:8px">`:''}`).addTo(map);
  });
  map.on('click','community-clusters',event=>{
    const feature=event.features?.[0];if(!feature)return;
    const clusterId=feature.properties.cluster_id;
    map.getSource('community-points').getClusterExpansionZoom(clusterId,(error,zoom)=>{
      if(error)return;map.easeTo({center:feature.geometry.coordinates,zoom});
    });
  });

  // Critical interactive layers are initialized first. Routing and search remain
  // available even if the optional visual heat surface cannot load.
  map.addSource('shortest-route',{type:'geojson',data:{type:'FeatureCollection',features:[]}});
  map.addLayer({id:'shortest-route-casing',type:'line',source:'shortest-route',layout:{'line-join':'round','line-cap':'round'},paint:{'line-color':'#020617','line-width':14,'line-opacity':0.95}});
  map.addLayer({id:'shortest-route-line',type:'line',source:'shortest-route',layout:{'line-join':'round','line-cap':'round'},paint:{'line-color':'#38bdf8','line-width':8,'line-opacity':1}});

  map.addSource('shadiest-route',{type:'geojson',data:{type:'FeatureCollection',features:[]}});
  map.addLayer({id:'shadiest-route-casing',type:'line',source:'shadiest-route',layout:{'line-join':'round','line-cap':'round'},paint:{'line-color':'#022c22','line-width':16,'line-opacity':0.96}});
  map.addLayer({id:'shadiest-route-line',type:'line',source:'shadiest-route',layout:{'line-join':'round','line-cap':'round'},paint:{'line-color':'#4ade80','line-width':9,'line-opacity':1,'line-dasharray':[1.6,1.0]}});

  map.addSource('exposure-segments',{type:'geojson',data:{type:'FeatureCollection',features:[]}});
  map.addLayer({id:'exposure-segments-line',type:'line',source:'exposure-segments',layout:{'line-join':'round','line-cap':'round'},paint:{'line-color':['match',['get','category'],'high-shade','#22c55e','mixed','#facc15','high-exposure','#ef4444','#94a3b8'],'line-width':7,'line-opacity':0.97}});
  map.on('click','exposure-segments-line',event=>{
    const feature=event.features&&event.features[0];
    if(!feature)return;
    new maplibregl.Popup()
      .setLngLat(event.lngLat)
      .setHTML(`<strong>${escapeHtml(feature.properties.category.replaceAll('-',' '))}</strong><br>${feature.properties.category==='nighttime'?'Shade not applicable at night':escapeHtml(feature.properties.shade)+'/100 modeled shade'}<br><br>${escapeHtml(feature.properties.reason)}`)
      .addTo(map);
  });
  map.on('mouseenter','exposure-segments-line',()=>map.getCanvas().style.cursor='pointer');
  map.on('mouseleave','exposure-segments-line',()=>map.getCanvas().style.cursor='');
  map.addSource('bike-fastest',{type:'geojson',data:{type:'FeatureCollection',features:[]}});
  map.addLayer({id:'bike-fastest-casing',type:'line',source:'bike-fastest',layout:{'line-join':'round','line-cap':'round'},paint:{'line-color':'#4a044e','line-width':14,'line-opacity':0.95}});
  map.addLayer({id:'bike-fastest-line',type:'line',source:'bike-fastest',layout:{'line-join':'round','line-cap':'round'},paint:{'line-color':'#e879f9','line-width':8,'line-opacity':1}});
  map.addSource('bike-cooler',{type:'geojson',data:{type:'FeatureCollection',features:[]}});
  map.addLayer({id:'bike-cooler-casing',type:'line',source:'bike-cooler',layout:{'line-join':'round','line-cap':'round'},paint:{'line-color':'#083344','line-width':16,'line-opacity':0.96}});
  map.addLayer({id:'bike-cooler-line',type:'line',source:'bike-cooler',layout:{'line-join':'round','line-cap':'round'},paint:{'line-color':'#22d3ee','line-width':9,'line-opacity':1,'line-dasharray':[1.6,1.0]}});

  // Separate overlay sources for coloring BOTH bike route alternatives.
  if(!map.getSource('bike-fastest-infra-color')){
    map.addSource('bike-fastest-infra-color',{
      type:'geojson',
      data:{type:'FeatureCollection',features:[]}
    });
  }
  if(!map.getSource('bike-cooler-infra-color')){
    map.addSource('bike-cooler-infra-color',{
      type:'geojson',
      data:{type:'FeatureCollection',features:[]}
    });
  }

  const bikeInfraColorExpression=[
    'match',['get','category'],
    'protected','#4CAF72',
    'painted','#A663B6',
    'shared_low_speed','#A663B6',
    'shared_road','#F59E0B',
    '#94A3B8'
  ];

  if(!map.getLayer('bike-fastest-infra-color-line')){
    map.addLayer({
      id:'bike-fastest-infra-color-line',
      type:'line',
      source:'bike-fastest-infra-color',
      layout:{'line-join':'round','line-cap':'round','visibility':'none'},
      paint:{
        'line-color':bikeInfraColorExpression,
        'line-width':9,
        'line-opacity':1
      }
    });
  }

  if(!map.getLayer('bike-cooler-infra-color-line')){
    map.addLayer({
      id:'bike-cooler-infra-color-line',
      type:'line',
      source:'bike-cooler-infra-color',
      layout:{'line-join':'round','line-cap':'round','visibility':'none'},
      paint:{
        'line-color':bikeInfraColorExpression,
        'line-width':9,
        'line-opacity':1,
        'line-dasharray':[1.5,0.8]
      }
    });
  }

  map.addSource('bike-infrastructure-segments',{type:'geojson',data:{type:'FeatureCollection',features:[]}});
  map.addLayer({
    id:'bike-infrastructure-segments-line',
    type:'line',
    source:'bike-infrastructure-segments',
    layout:{'line-join':'round','line-cap':'round'},
    paint:{
      'line-color':[
        'match',['get','category'],
        'protected','#4CAF72',
        'painted','#A663B6',
        'shared_low_speed','#A663B6',
        'shared_road','#F59E0B',
        '#94A3B8'
      ],
      'line-width':8,
      'line-opacity':0.96
    }
  });
  map.setLayoutProperty('bike-infrastructure-segments-line','visibility','none');
  map.on('click','bike-infrastructure-segments-line',event=>{
    const feature=event.features&&event.features[0];
    if(!feature)return;
    new maplibregl.Popup()
      .setLngLat(event.lngLat)
      .setHTML(`<strong>${escapeHtml(String(feature.properties.category||'unknown').replaceAll('_',' '))}</strong><br>${escapeHtml(feature.properties.explanation||'OSM bike-infrastructure classification')}`)
      .addTo(map);
  });


  ecotraceStartupComplete=true;
  document.getElementById('status-pill').textContent='Map ready — loading heat layer…';

  // Defer the optional heat image so it cannot freeze startup.
  setTimeout(async()=>{
    try{
      const response=await fetchWithTimeout('/api/heat-grid',{},12000);
      const heat=await readApiResponse(response);

      // Lower resolution is visually smooth after raster interpolation and is much
      // faster than the previous 900×700 blocking calculation.
      const canvas=document.createElement('canvas');
      canvas.width=360;
      canvas.height=280;
      const ctx=canvas.getContext('2d',{alpha:true});
      const image=ctx.createImageData(canvas.width,canvas.height);

      const stops=[
        [35,[43,91,220]],
        [49,[16,173,196]],
        [62,[50,190,102]],
        [74,[240,204,46]],
        [85,[238,125,46]],
        [96,[220,55,55]]
      ];

      function colorFor(value){
        let lower=stops[0],upper=stops[stops.length-1];
        for(let index=0;index<stops.length-1;index++){
          if(value>=stops[index][0]&&value<=stops[index+1][0]){
            lower=stops[index];
            upper=stops[index+1];
            break;
          }
        }
        const amount=Math.max(0,Math.min(1,(value-lower[0])/(upper[0]-lower[0]||1)));
        return lower[1].map((channel,index)=>Math.round(channel+(upper[1][index]-channel)*amount));
      }

      for(let y=0;y<canvas.height;y++){
        for(let x=0;x<canvas.width;x++){
          const lon=heat.bounds.min_lon+(heat.bounds.max_lon-heat.bounds.min_lon)*x/(canvas.width-1);
          const lat=heat.bounds.max_lat-(heat.bounds.max_lat-heat.bounds.min_lat)*y/(canvas.height-1);
          let weightedHeat=0,totalWeight=0;
          for(const anchor of heat.anchors){
            const dx=(lon-anchor.lon)*Math.cos(lat*Math.PI/180);
            const dy=lat-anchor.lat;
            const distanceSquared=dx*dx+dy*dy+0.00004;
            const weight=1/distanceSquared;
            weightedHeat+=anchor.heat*weight;
            totalWeight+=weight;
          }
          const color=colorFor(weightedHeat/totalWeight);
          const pixel=(y*canvas.width+x)*4;
          image.data[pixel]=color[0];
          image.data[pixel+1]=color[1];
          image.data[pixel+2]=color[2];
          image.data[pixel+3]=92;
        }
      }
      ctx.putImageData(image,0,0);

      if(map.getSource('heat-surface')){
        document.getElementById('status-pill').textContent='EcoTrace ready — search, plant, route, or ask';
        return;
      }
      map.addSource('heat-surface',{
        type:'image',
        url:canvas.toDataURL('image/png'),
        coordinates:[
          [heat.bounds.min_lon,heat.bounds.max_lat],
          [heat.bounds.max_lon,heat.bounds.max_lat],
          [heat.bounds.max_lon,heat.bounds.min_lat],
          [heat.bounds.min_lon,heat.bounds.min_lat]
        ]
      });

      const firstSymbol=map.getStyle().layers.find(layer=>layer.type==='symbol');
      map.addLayer({
        id:'heat-surface-fill',
        type:'raster',
        source:'heat-surface',
        paint:{
          'raster-opacity':0.42,
          'raster-contrast':0.04,
          'raster-saturation':0.08,
          'raster-fade-duration':250
        }
      },firstSymbol?firstSymbol.id:undefined);

      for(const layer of map.getStyle().layers){
        if(layer.type==='symbol'){
          try{
            map.setPaintProperty(layer.id,'text-halo-color','rgba(255,255,255,0.98)');
            map.setPaintProperty(layer.id,'text-halo-width',2.2);
            map.setPaintProperty(layer.id,'text-opacity',1);
          }catch(_){}
        }
      }
      for(const layerId of [
        'shortest-route-casing','shortest-route-line',
        'shadiest-route-casing','shadiest-route-line','exposure-segments-line',
        'bike-fastest-casing','bike-fastest-line',
        'bike-cooler-casing','bike-cooler-line'
      ]){
        if(map.getLayer(layerId))map.moveLayer(layerId);
      }
      document.getElementById('status-pill').textContent='EcoTrace ready — search, plant, route, or ask';
    }catch(error){
      console.error('Heat layer failed:',error);
      document.getElementById('status-pill').textContent='Map ready — heat overlay unavailable, routing still works';
    }
  },150);
});


document.getElementById('bike-legend-toggle')?.addEventListener('click',event=>{
  const legend=document.getElementById('bike-infra-map-legend');
  const button=event.currentTarget;
  if(!legend)return;
  const nowHidden=!legend.classList.contains('hidden');
  legend.classList.toggle('hidden',nowHidden);
  button.textContent=nowHidden?'Bike legend ▾':'Bike legend ▴';
  localStorage.setItem('ecotraceBikeLegendHidden',nowHidden?'1':'0');
});

map.on('click',async e=>{
  try{
    const originalTarget=e.originalEvent&&e.originalEvent.target;
    if(
      originalTarget&&
      (
        originalTarget.closest?.('.eco-marker')||
        originalTarget.closest?.('.maplibregl-marker')||
        originalTarget.closest?.('.maplibregl-popup')||
        originalTarget.closest?.('.maplibregl-ctrl')
      )
    ){
      return;
    }

    if(activeToolPanel==='ewaste-panel'&&activeEwasteTab==='centers'&&ewasteCenterSelectionMode){
      ewasteCenterSearchPoint={lat:e.lngLat.lat,lng:e.lngLat.lng};
      ewasteCenterSelectionMode=false;
      document.getElementById('ewaste-center-point-box').textContent=`Selected search point: ${ewasteCenterSearchPoint.lat.toFixed(5)}, ${ewasteCenterSearchPoint.lng.toFixed(5)}`;
      document.getElementById('status-pill').textContent='E-waste center search point selected';
      return;
    }

    if(activeToolPanel==='community-panel'&&communityContextSelectionMode){
      communityContextSelectionMode=false;
      const point={lat:e.lngLat.lat,lon:e.lngLat.lng};

      if(communityContextMarker)communityContextMarker.remove();
      communityContextMarker=new maplibregl.Marker({color:'#a78bfa'})
        .setLngLat([point.lon,point.lat])
        .setPopup(new maplibregl.Popup({offset:16}).setText('Community context location'))
        .addTo(map);

      const place=await reverseGeocodePoint(point);
      document.getElementById('community-context-address').value=place.display_name;
      await loadCommunityPlanningContextAt(
        point.lat,
        point.lon,
        place.display_name||'Selected map point'
      );
      return;
    }

    if(activeToolPanel==='community-panel'&&communityMapSelectionMode){
      communitySelectedPoint={lat:e.lngLat.lat,lon:e.lngLat.lng};
      communityMapSelectionMode=false;
      if(communitySelectionMarker)communitySelectionMarker.remove();
      communitySelectionMarker=new maplibregl.Marker({color:'#f59e0b'})
        .setLngLat([communitySelectedPoint.lon,communitySelectedPoint.lat])
        .setPopup(new maplibregl.Popup({offset:16}).setText('Community post location'))
        .addTo(map);
      const place=await reverseGeocodePoint(communitySelectedPoint);
      document.getElementById('community-coordinate-box').innerHTML=`
        <strong>Selected:</strong> ${escapeHtml(place.display_name)}<br>
        <span class="font-mono">${communitySelectedPoint.lat.toFixed(6)}, ${communitySelectedPoint.lon.toFixed(6)}</span>`;
      if(!document.getElementById('community-location-text').value.trim()){
        document.getElementById('community-location-text').value=place.display_name;
      }
      document.getElementById('status-pill').textContent='Community location selected';
      return;
    }

    // Dispatch from the ACTIVE PANEL, not the cached mode variable.
    // This prevents Tree/Walking/Bike clicks from ever falling back to Heat
    // because of stale state from a previous panel.
    if(activeToolPanel==='heat-panel'){
      await analyzeHeatPoint(e.lngLat.lat,e.lngLat.lng);
    }else if(activeToolPanel==='tree-panel'){
      await analyzePoint(e.lngLat.lat,e.lngLat.lng);
    }else if(activeToolPanel==='bike-panel'){
      if(bikePointLookupInProgress){
        document.getElementById('status-pill').textContent='Still loading the previous bike point…';
        return;
      }
      if(bikeClicks.length===2&&bikeClicks[0]!==null){
        clearBike();
        document.getElementById('bike-status').textContent='Starting a new bike route…';
      }

      const point={lat:e.lngLat.lat,lon:e.lngLat.lng};
      const typedStart=bikeTypedStartLock||document.getElementById('bike-start').value.trim();
      const bikeEndInput=document.getElementById('bike-end');

      // Destination-first mode: focus Destination, then click the map.
      if(bikeClicks.length===0 && document.activeElement===bikeEndInput){
        document.getElementById('status-pill').textContent='Loading clicked bike destination…';
        const generation=bikeOperationGeneration;
        bikePointLookupInProgress=true;
        let destinationPlace;
        try{
          destinationPlace=await reverseGeocodePoint(point);
        }finally{
          bikePointLookupInProgress=false;
        }
        if(generation!==bikeOperationGeneration||activeToolPanel!=='bike-panel')return;
        bikeClicks=[null,point];
        bikeEndInput.value=destinationPlace.display_name;
        addBikeEndpoint(point,'#22d3ee',destinationPlace.display_name);
        document.getElementById('bike-status').textContent=
          `${destinationPlace.display_name} selected as destination — type a starting address or click the starting point.`;
        document.getElementById('status-pill').textContent='Bike destination ready — enter or click a starting point';
        document.getElementById('bike-start')?.focus();
        return;
      }

      // A typed bike start always wins: keep its text and make THIS click the destination.
      let typedStartResolvedThisClick=false;
      if(bikeTypedStartLock && bikeClicks.length!==0){
        bikeClicks=[];
        bikeEndpointMarkers.forEach(marker=>marker.remove());
        bikeEndpointMarkers=[];
      }
      if(bikeClicks.length===0&&typedStart){
        document.getElementById('status-pill').textContent='Loading typed bike starting address…';
        const generation=bikeOperationGeneration;
        bikePointLookupInProgress=true;
        let startPlace;
        try{
          startPlace=await geocodeAddress(typedStart);
        }finally{
          bikePointLookupInProgress=false;
        }
        if(generation!==bikeOperationGeneration||activeToolPanel!=='bike-panel')return;
        const startPoint={lat:Number(startPlace.latitude),lon:Number(startPlace.longitude)};
        bikeClicks=[startPoint];
        typedStartResolvedThisClick=true;
        addBikeEndpoint(startPoint,'#f0abfc',startPlace.display_name);
      }

      const hasPresetDestination=bikeClicks.length===2&&bikeClicks[0]===null&&bikeClicks[1];
      if(hasPresetDestination){
        const destination=bikeClicks[1];
        bikeClicks=[point,destination];
      }
      const isStart=!typedStartResolvedThisClick&&(hasPresetDestination||bikeClicks.length===0);
      document.getElementById('status-pill').textContent=isStart
        ?'Loading bike point 1 of 2…'
        :'Loading bike destination…';

      if(!hasPresetDestination)bikeClicks.push(point);
      addBikeEndpoint(point,isStart?'#f0abfc':'#22d3ee',isStart?'Bike start':'Bike destination');
      if(hasPresetDestination){
        addBikeEndpoint(destination,'#22d3ee','Bike destination');
      }

      const clickGeneration=bikeOperationGeneration;
      bikePointLookupInProgress=true;
      let place;
      try{
        place=await reverseGeocodePoint(point);
      }finally{
        bikePointLookupInProgress=false;
      }
      if(clickGeneration!==bikeOperationGeneration||activeToolPanel!=='bike-panel')return;

      if(typedStartResolvedThisClick||bikeTypedStartLock){
        document.getElementById('bike-end').value=place.display_name;
        if(bikeTypedStartLock)document.getElementById('bike-start').value=bikeTypedStartLock;
      }else{
        document.getElementById(isStart?'bike-start':'bike-end').value=place.display_name;
      }

      document.getElementById('bike-status').textContent=isStart
        ?`${place.display_name} selected — click destination`
        :`${document.getElementById('bike-start').value} → ${place.display_name}`;
      document.getElementById('status-pill').textContent=isStart
        ?'Bike point 1 ready — click point 2'
        :'Bike destination ready — calculating routes…';
      if(bikeClicks.length===2)await requestBikeRoute();
    }else if(activeToolPanel==='walk-panel'){
      if(walkingPointLookupInProgress){
        document.getElementById('status-pill').textContent='Still loading the previous walking point…';
        return;
      }
      if(routeClicks.length===2&&routeClicks[0]!==null){
        clearRoute();
        document.getElementById('route-status').textContent='Starting a new walking route…';
      }

      const point={lat:e.lngLat.lat,lon:e.lngLat.lng};
      const typedStart=walkingTypedStartLock||document.getElementById('route-start').value.trim();
      const routeEndInput=document.getElementById('route-end');

      // Destination-first mode: focus the Destination box, then click the map.
      // That map click becomes the destination; afterward the user can type
      // the starting address and submit.
      if(routeClicks.length===0 && document.activeElement===routeEndInput){
        document.getElementById('status-pill').textContent='Loading clicked destination…';
        const generation=walkOperationGeneration;
        walkingPointLookupInProgress=true;
        let destinationPlace;
        try{
          destinationPlace=await reverseGeocodePoint(point);
        }finally{
          walkingPointLookupInProgress=false;
        }
        if(generation!==walkOperationGeneration||activeToolPanel!=='walk-panel')return;

        routeClicks=[null,point];
        routeEndInput.value=destinationPlace.display_name;
        addRouteEndpoint(point,'#34d399',destinationPlace.display_name);
        document.getElementById('route-status').textContent=
          `${destinationPlace.display_name} selected as destination — type a starting address or click the starting point.`;
        document.getElementById('status-pill').textContent=
          'Walking destination ready — enter or click a starting point';
        document.getElementById('route-start')?.focus();
        return;
      }

      // A user-typed start has priority over stale click state: resolve it,
      // keep its text exactly, and make THIS map click the destination.
      let typedStartResolvedThisClick=false;
      if(walkingTypedStartLock && routeClicks.length!==0){
        routeClicks=[];
        routeEndpointMarkers.forEach(marker=>marker.remove());
        routeEndpointMarkers=[];
      }
      if(routeClicks.length===0&&typedStart){
        document.getElementById('status-pill').textContent='Loading typed starting address…';
        const generation=walkOperationGeneration;
        walkingPointLookupInProgress=true;
        let startPlace;
        try{
          startPlace=await geocodeAddress(typedStart);
        }finally{
          walkingPointLookupInProgress=false;
        }
        if(generation!==walkOperationGeneration||activeToolPanel!=='walk-panel')return;

        const startPoint={
          lat:Number(startPlace.latitude),
          lon:Number(startPlace.longitude)
        };
        routeClicks=[startPoint];
        typedStartResolvedThisClick=true;
        // Preserve the exact start text the user typed; only resolve its coordinates.
        addRouteEndpoint(startPoint,'#f8fafc',startPlace.display_name);
      }

      const hasPresetDestination=routeClicks.length===2&&routeClicks[0]===null&&routeClicks[1];
      if(hasPresetDestination){
        const destination=routeClicks[1];
        routeClicks=[point,destination];
      }
      const isStart=!typedStartResolvedThisClick&&(hasPresetDestination||routeClicks.length===0);
      document.getElementById('status-pill').textContent=isStart
        ?'Loading walking point 1 of 2…'
        :'Loading walking destination…';

      if(!hasPresetDestination)routeClicks.push(point);
      addRouteEndpoint(
        point,
        isStart?'#f8fafc':'#34d399',
        isStart?'Finding nearby start address…':'Finding nearby destination…'
      );
      if(hasPresetDestination){
        addRouteEndpoint(destination,'#34d399','Walking destination');
      }

      const clickGeneration=walkOperationGeneration;
      walkingPointLookupInProgress=true;
      let place;
      try{
        place=await reverseGeocodePoint(point);
      }finally{
        walkingPointLookupInProgress=false;
      }
      if(clickGeneration!==walkOperationGeneration||activeToolPanel!=='walk-panel')return;

      if(typedStartResolvedThisClick||walkingTypedStartLock){
        document.getElementById('route-end').value=place.display_name;
        if(walkingTypedStartLock){
          document.getElementById('route-start').value=walkingTypedStartLock;
        }
      }else{
        document.getElementById(isStart?'route-start':'route-end').value=place.display_name;
      }
      const newestWalkingMarker=routeEndpointMarkers[routeEndpointMarkers.length-1];
      if(newestWalkingMarker){
        newestWalkingMarker.setPopup(
          new maplibregl.Popup({offset:16}).setText(place.display_name)
        );
      }
      document.getElementById('route-status').textContent=isStart
        ?`${place.display_name} selected — click destination`
        :`${document.getElementById('route-start').value} → ${place.display_name}`;
      document.getElementById('status-pill').textContent=isStart
        ?'Walking point 1 ready — click point 2'
        :'Walking destination ready — calculating routes…';

      if(routeClicks.length===2)await requestRoute();
    }
  }catch(error){
    const message=error?.message||'Operation failed';
    if(activeToolPanel==='walk-panel'){
      clearRoute();
      document.getElementById('route-status').textContent=`Route attempt failed: ${message}. Click a new start point to retry.`;
    }else if(activeToolPanel==='bike-panel'){
      clearBike();
      document.getElementById('bike-status').textContent=`Bike route attempt failed: ${message}. Click a new start point to retry.`;
    }
    document.getElementById('status-pill').textContent=message;
  }
});

document.getElementById('search-form').addEventListener('submit',async e=>{
  e.preventDefault();
  const input=document.getElementById('search-input');
  const status=document.getElementById('search-status');
  const query=input.value.trim();
  const submitButton=e.currentTarget.querySelector('button[type="submit"]');

  if(!query){
    status.textContent='Enter an address, school, library, park, business, or landmark.';
    return;
  }

  status.textContent='Searching…';
  submitButton.disabled=true;
  submitButton.textContent='Searching…';

  try{
    const place=await geocodeAddress(query);
    const latitude=Number(place.latitude);
    const longitude=Number(place.longitude);

    if(!Number.isFinite(latitude)||!Number.isFinite(longitude)){
      throw new Error('Place not found. Please enter a full or valid address.');
    }

    lastResolvedLocation={
      lat:latitude,
      lon:longitude,
      label:place.display_name||query
    };

    if(searchMarker)searchMarker.remove();
    searchMarker=new maplibregl.Marker({color:'#38bdf8'})
      .setLngLat([longitude,latitude])
      .setPopup(new maplibregl.Popup({offset:16}).setText(place.display_name))
      .addTo(map);

    map.flyTo({
      center:[longitude,latitude],
      zoom:15,
      essential:true
    });

    status.textContent=`Found: ${place.display_name}`;

    if(activeToolPanel==='walk-panel'){
      if(walkingTypedStartLock){
        document.getElementById('route-start').value=walkingTypedStartLock;
        document.getElementById('route-end').value=place.display_name;
      }else{
        document.getElementById(routeClicks.length===0?'route-start':'route-end').value=place.display_name;
      }
    }else if(activeToolPanel==='bike-panel'){
      if(bikeTypedStartLock){
        document.getElementById('bike-start').value=bikeTypedStartLock;
        document.getElementById('bike-end').value=place.display_name;
      }else{
        document.getElementById(bikeClicks.length===0?'bike-start':'bike-end').value=place.display_name;
      }
    }else if(activeToolPanel==='tree-panel'){
      status.textContent='Location found. Loading nearby screened tree opportunities…';
      await analyzePoint(latitude,longitude);
      status.textContent=`Found: ${place.display_name}`;
    }else if(activeToolPanel==='heat-panel'){
      status.textContent='Location found. Loading heat analysis…';
      await analyzeHeatPoint(latitude,longitude);
      status.textContent=`Found: ${place.display_name}`;
    }else{
      status.textContent=`Found: ${place.display_name}`;
    }
  }catch(error){
    const message=String(error.message||'').includes('404')
      ?'Place not found. Please enter a full or valid address.'
      :(error.message||'Place not found. Please enter a full or valid address.');
    status.textContent=message;
  }finally{
    submitButton.disabled=false;
    submitButton.textContent='Go';
  }
});

document.getElementById('route-form').addEventListener('submit',async e=>{
  e.preventDefault();
  const startText=document.getElementById('route-start').value.trim();
  const endText=document.getElementById('route-end').value.trim();
  const status=document.getElementById('route-status');
  const button=document.getElementById('route-submit');

  const clickedDestination=(
    routeClicks.length===2 && routeClicks[0]===null && routeClicks[1]
  ) ? {...routeClicks[1]} : null;

  if(!startText){
    status.textContent='Enter a starting address or click a starting point.';
    return;
  }
  if(!endText && !clickedDestination){
    status.textContent='Enter a destination, or focus the Destination box and click the map.';
    return;
  }

  // Start a fresh route while preserving a destination explicitly clicked first.
  const preservedDestinationLabel=endText;
  if(clickedDestination){
    walkOperationGeneration++;
    clearRoutePlantingMarkers();
    if(map.getSource('shortest-route'))map.getSource('shortest-route').setData({type:'FeatureCollection',features:[]});
    if(map.getSource('shadiest-route'))map.getSource('shadiest-route').setData({type:'FeatureCollection',features:[]});
    if(map.getSource('exposure-segments'))map.getSource('exposure-segments').setData({type:'FeatureCollection',features:[]});
    document.getElementById('route-comparison')?.classList.add('hidden');

    // Keep only the clicked destination marker.
    if(routeEndpointMarkers.length){
      const destinationMarker=routeEndpointMarkers[routeEndpointMarkers.length-1];
      routeEndpointMarkers.slice(0,-1).forEach(marker=>{try{marker.remove();}catch(_){}});
      routeEndpointMarkers=[destinationMarker];
    }
    routeClicks=[null,clickedDestination];
  }else{
    clearRoute();
  }

  const formGeneration=walkOperationGeneration;
  button.disabled=true;
  button.textContent='Finding route…';
  status.textContent='Finding and validating locations…';
  document.getElementById('status-pill').textContent='Calculating walking routes…';

  try{
    const startPlace=await geocodeAddress(startText);
    let endPlace=null;

    if(!clickedDestination){
      endPlace=await geocodeAddress(endText);
    }

    if(formGeneration!==walkOperationGeneration||activeToolPanel!=='walk-panel')return;

    const startPoint={
      lat:Number(startPlace.latitude),
      lon:Number(startPlace.longitude)
    };
    const destinationPoint=clickedDestination || {
      lat:Number(endPlace.latitude),
      lon:Number(endPlace.longitude)
    };
    const destinationLabel=clickedDestination
      ? (preservedDestinationLabel||'Clicked destination')
      : endPlace.display_name;

    routeClicks=[startPoint,destinationPoint];

    addRouteEndpoint(startPoint,'#f8fafc',startPlace.display_name);
    if(!clickedDestination){
      addRouteEndpoint(destinationPoint,'#34d399',destinationLabel);
    }

    status.textContent=`${startPlace.display_name} → ${destinationLabel}`;
    await requestRoute();
    status.textContent=`Route ready: ${startPlace.display_name} → ${destinationLabel}`;
  }catch(error){
    clearRoute();
    const message=error?.message||'The route could not be calculated.';
    status.textContent=`Could not calculate route: ${message}`;
    document.getElementById('status-pill').textContent='Route attempt failed — edit either address and try again';
    document.getElementById('analysis-card').innerHTML=`<h2 class="text-sm font-bold text-red-300">Route not created</h2><p class="mt-2 text-sm leading-6 text-slate-300">${escapeHtml(message)}</p><p class="mt-3 text-xs leading-5 text-slate-400">This failed attempt was cleared automatically. You can immediately correct either address and retry.</p>`;
  }finally{
    button.disabled=false;
    button.textContent='Find shadiest route';
  }
});

document.getElementById('bike-form').addEventListener('submit',async e=>{
  e.preventDefault();
  const status=document.getElementById('bike-status');
  const startText=document.getElementById('bike-start').value.trim();
  const endText=document.getElementById('bike-end').value.trim();

  if(!startText){
    status.textContent='Enter a starting address or click a starting point.';
    return;
  }
  if(!endText && !bikeClicks[1]){
    status.textContent='Enter a destination or select an E-Waste center.';
    return;
  }

  status.textContent=bikeQuickRouteMode
    ?'Finding a fast bike route to the selected E-Waste center…'
    :'Finding bike routes…';

  const savedCenterDestination=(
    bikeQuickRouteMode && bikeClicks[1]
      ?{...bikeClicks[1]}
      :null
  );
  const savedCenterLabel=endText;

  clearBike();
  if(savedCenterDestination){
    bikeQuickRouteMode=true;
    bikeClicks=[null,savedCenterDestination];
  }

  const formGeneration=bikeOperationGeneration;

  try{
    const start=await geocodeAddress(startText);
    let end;

    if(savedCenterDestination){
      end={
        latitude:savedCenterDestination.lat,
        longitude:savedCenterDestination.lon,
        display_name:savedCenterLabel||'Selected E-Waste center'
      };
    }else{
      bikeQuickRouteMode=false;
      end=await geocodeAddress(endText);
    }

    if(formGeneration!==bikeOperationGeneration||activeToolPanel!=='bike-panel')return;

    bikeClicks=[
      {lat:Number(start.latitude),lon:Number(start.longitude)},
      {lat:Number(end.latitude),lon:Number(end.longitude)}
    ];

    if(
      !bikeClicks.every(point=>
        Number.isFinite(point.lat)&&Number.isFinite(point.lon)
      )
    ){
      throw new Error('The route endpoints did not return valid coordinates.');
    }

    addBikeEndpoint(bikeClicks[0],'#f0abfc',start.display_name);
    addBikeEndpoint(bikeClicks[1],'#22d3ee',end.display_name);

    await requestBikeRoute();

    status.textContent=bikeQuickRouteMode
      ?'Fast bike route to the E-Waste center calculated.'
      :'Bike routes calculated.';
  }catch(error){
    const retainedDestination=savedCenterDestination;
    clearBike();

    if(retainedDestination){
      bikeQuickRouteMode=true;
      bikeClicks=[null,retainedDestination];
      document.getElementById('bike-end').value=savedCenterLabel;
    }

    status.textContent=error.message||'Bike route failed.';
  }
});

function resetTreeGuideFilters(){
  document.getElementById('tree-guide-size').value='';
  document.getElementById('tree-guide-type').value='';
  document.getElementById('tree-guide-height').value='';
  document.getElementById('tree-guide-width').value='';
  document.getElementById('tree-guide-flower').value='';
  document.getElementById('tree-guide-status').textContent='';
  loadTreeGuide();
}

function clearEwasteCenterResults(){
  ewasteCenterSearchPoint=null;
  ewasteCenterSelectionMode=false;
  document.getElementById('ewaste-center-point-box').textContent='Using current map center.';
  document.getElementById('ewaste-center-status').textContent='';
  document.getElementById('ewaste-closest-centers').innerHTML='';
  document.getElementById('ewaste-center-list').innerHTML='';
  const source=map.getSource('ewaste-centers');
  if(source)source.setData({type:'FeatureCollection',features:[]});
  applyEwasteCenterVisibility();
}

document.getElementById('reset-button').onclick=()=>{
  const panelBeforeReset=activeToolPanel;

  try{
    // Clear only tracked MapLibre objects. Do not directly remove marker DOM
    // nodes; doing that can leave MapLibre's internal marker state inconsistent.
    clearTreeMarkers();
    clearRoutePlantingMarkers();
    clearRoute();
    clearBike();
    clearEwasteCenterResults();

    if(searchMarker){searchMarker.remove();searchMarker=null;}
    if(communitySelectionMarker){communitySelectionMarker.remove();communitySelectionMarker=null;}
    if(communityContextMarker){communityContextMarker.remove();communityContextMarker=null;}

    communitySelectedPoint=null;
    communityMapSelectionMode=false;
    communityContextSelectionMode=false;

    window.latestEcoTraceRoute=null;
    window.latestEcoTraceBikeRoute=null;
    window.latestEcoTraceAnalysis=null;
    window.latestEcoTraceRoutePlantingImpact=null;
    window.latestBikeValidationContext=null;
    window.latestCommunityContext=null;
    latestGeminiEwasteResult=null;
    latestSelectedEwasteCenter=null;
    resetEwasteRecommendationForm();

    for(const id of [
      'search-input','route-start','route-end','bike-start','bike-end',
      'community-context-address'
    ]){
      const el=document.getElementById(id);
      if(el)el.value='';
    }

    for(const id of [
      'search-status','route-status','bike-status',
      'ewaste-recommend-status','gemini-ewaste-status',
      'community-context-status'
    ]){
      const el=document.getElementById(id);
      if(el)el.textContent='';
    }

    const routeComparison=document.getElementById('route-comparison');
    if(routeComparison)routeComparison.innerHTML='';
    const bikeComparison=document.getElementById('bike-comparison');
    if(bikeComparison)bikeComparison.innerHTML='';
    const analysisCard=document.getElementById('analysis-card');
    if(analysisCard)analysisCard.innerHTML='<h2 class="text-sm font-bold text-white">Ready</h2><p class="mt-2 text-sm leading-6 text-slate-300">Click a map location or search for a place.</p>';
    const recList=document.getElementById('recommendation-list');
    if(recList)recList.innerHTML='';
    document.getElementById('recommendations')?.classList.add('hidden');

    const plantingGuide=document.getElementById('planting-site-guide');
    if(plantingGuide)plantingGuide.remove();

    for(const id of ['ewaste-model','ewaste-age','gemini-ewaste-description']){
      const el=document.getElementById(id);
      if(el)el.value='';
    }

    const ewasteRecommendations=document.getElementById('ewaste-recommendations');
    if(ewasteRecommendations)ewasteRecommendations.innerHTML='';
    const geminiResult=document.getElementById('gemini-ewaste-result');
    if(geminiResult){
      geminiResult.innerHTML='';
      geminiResult.classList.add('hidden');
    }
    selectedGeminiEwasteFile=null;
    const photoInput=document.getElementById('gemini-ewaste-photo-file');
    if(photoInput)photoInput.value='';
    document.getElementById('gemini-ewaste-photo-preview-wrap')?.classList.add('hidden');

    ecoTraceAssistantHistory=[];
    clearAssistantComposer();
    renderAssistantConversation();
    const assistantStatus=document.getElementById('assistant-chat-status');
    if(assistantStatus)assistantStatus.textContent='Current chat reset; saved chats kept.';

    walkingTypedStartLock='';
    bikeTypedStartLock='';
    const walkingProfile=document.getElementById('walking-profile');
    if(walkingProfile)walkingProfile.value='adult';
    applyWalkingProfile();
    resetTreeGuideFilters();

    for(const sourceId of [
      'community-points','ewaste-centers','route-planting-sites',
      'selected-tree-site','tree-recommendations'
    ]){
      const source=map.getSource(sourceId);
      if(source&&typeof source.setData==='function'){
        source.setData({type:'FeatureCollection',features:[]});
      }
    }

    // Reset always keeps the current tool panel. The user chooses whether the
    // map stays at the current view or returns to the EcoTrace overview.
    clearCommunityPlanningContext();
    closeGuideDetails();
    showToolPanel(panelBeforeReset,{preserveMap:true});
    const resetViewMode=document.getElementById('reset-view-mode')?.value||'stay';

    if(resetViewMode==='overview'){
      map.flyTo({
        center:INITIAL.center,
        zoom:INITIAL.zoom,
        essential:true,
        duration:700
      });
    }

    const sidebar=document.getElementById('sidebar');
    if(sidebar)sidebar.scrollTo({top:0,behavior:'smooth'});

    const pill=document.getElementById('status-pill');
    if(pill)pill.textContent=resetViewMode==='overview'
      ?'Current panel reset — map returned to overview'
      :'Current panel reset — map position preserved';
  }catch(error){
    console.error('EcoTrace reset error:',error);
    const pill=document.getElementById('status-pill');
    if(pill)pill.textContent=`Reset error: ${error?.message||'Unknown reset problem'}`;
  }
};

// Session state is initialized before the original app script runs.
function ecoReadPersonal(suffix,fallback){
  const s=window.ecoSessionState;
  if(!s.account)return structuredClone(s.temporary[suffix]??fallback);
  try{return JSON.parse(localStorage.getItem(profileStorageKey(suffix))||JSON.stringify(fallback));}catch(_){return structuredClone(fallback);}
}
function ecoWritePersonal(suffix,value){
  const s=window.ecoSessionState;
  if(!s.account){s.temporary[suffix]=structuredClone(value);return;}
  localStorage.setItem(profileStorageKey(suffix),JSON.stringify(value));
}
function profileStorageKey(suffix){
  const user=window.ecoSessionState?.account?.username;
  return user?`ecotrace:v8:account:${encodeURIComponent(user)}:${suffix}`:`ecotrace:v8:temporary:${suffix}`;
}
function getSavedLocations(){const rows=ecoReadPersonal('savedLocations',[]);return Array.isArray(rows)?rows:[];}
function setSavedLocations(items){ecoWritePersonal('savedLocations',items);}
function getSavedChats(){
  const rows=ecoReadPersonal('assistantChats',[]);
  return (Array.isArray(rows)?rows:[]).filter(x=>x&&typeof x==='object').map((x,i)=>({...x,id:String(x.id||`legacy-${i}`),title:String(x.title||'Saved chat'),turns:Array.isArray(x.turns)?x.turns:[]}));
}
function setSavedChats(chats){ecoWritePersonal('assistantChats',chats.slice(-30));}
function impactRecords(){return ecoReadPersonal('impactHistory',[]);}
function saveImpactRecords(rows){ecoWritePersonal('impactHistory',rows);}
function loadCommunityActivityState(){return ecoReadPersonal('communityActivity',{hosted:[],joined:[],hours:[]});}
function saveCommunityActivityState(rows){ecoWritePersonal('communityActivity',rows);}
function ecoMigrateNamedChats(){
  const account=window.ecoSessionState.account;if(!account)return;
  // Only the old username namespace can be associated with this account.
  // Ambiguous display-name namespaces are not silently assigned to someone.
  const safe=account.username.trim().toLowerCase().replace(/[^a-z0-9_-]+/g,'_');
  const marker=profileStorageKey('chatMigration');
  if(localStorage.getItem(marker))return;
  const chats=getSavedChats();const ids=new Set(chats.map(c=>c.id));
  for(const mode of ['profile','account']){
    const key=`ecotrace:${mode}:${safe}:assistantChats`;
    try{
      const old=JSON.parse(localStorage.getItem(key)||'[]');
      if(Array.isArray(old))for(const c of old)if(c&&Array.isArray(c.turns)){
        const id=String(c.id||crypto.randomUUID());if(ids.has(id))continue;
        chats.push({...c,id});ids.add(id);
      }
      setSavedChats(chats);localStorage.removeItem(key);
    }catch(_){return;}
  }
  localStorage.setItem(marker,'1');
}
function ecoToast(message){
  let toast=document.getElementById('eco-session-toast');
  if(!toast){toast=document.createElement('div');toast.id='eco-session-toast';toast.setAttribute('role','status');toast.setAttribute('aria-live','polite');toast.style.cssText='position:fixed;bottom:24px;left:50%;transform:translateX(-50%);z-index:10000;max-width:90vw;width:360px;padding:14px 18px;background:#172538;color:#fff;border:1px solid #73849b;border-radius:14px;box-shadow:0 8px 30px #0007;font:14px/1.5 system-ui';document.body.append(toast);}
  toast.replaceChildren();const text=document.createElement('span');text.textContent=message;toast.append(text);toast.hidden=false;
  clearTimeout(window.ecoToastTimer);window.ecoToastTimer=setTimeout(()=>{toast.hidden=true;},4500);
}
function ecoRestrictedMessage(button){
  if(!button||window.ecoSessionState.account)return '';
  if(['community-create-toggle','community-submit'].includes(button.id))return 'Sign in to create or change a community post.';
  if(['ewaste-listing-submit','ewaste-event-submit'].includes(button.id))return 'Sign in to create a listing or event.';
  if(button.matches('.ewaste-reply-submit'))return 'Sign in to send e-waste messages.';
  if(button.matches('.community-join,.community-leave,.ewaste-event-join'))return 'Sign in to join or leave an event.';
  if(button.matches('.community-comment-submit')){
    const post=latestCommunityPosts.find(p=>String(p.id)===String(button.dataset.postId));
    if(post?.category!=='discussion')return 'Sign in to reply here. Guests can reply in Discussions.';
  }
  return '';
}
function applyPostPermissions(){
  const signed=Boolean(window.ecoSessionState.account);
  for(const button of document.querySelectorAll('#community-create-toggle,#community-submit,#ewaste-listing-submit,#ewaste-event-submit,.ewaste-reply-submit,.community-join,.community-leave,.ewaste-event-join,.community-comment-submit')){
    const message=ecoRestrictedMessage(button);
    if(message){button.disabled=false;button.dataset.ecoSignInRequired='true';button.setAttribute('aria-disabled','true');button.title=message;}
    else if(button.dataset.ecoSignInRequired){delete button.dataset.ecoSignInRequired;button.removeAttribute('aria-disabled');button.disabled=false;button.removeAttribute('title');}
  }
  if(!signed)document.getElementById('community-create-card')?.classList.add('hidden');
  document.querySelectorAll('.community-edit-post,.community-delete-post,.community-edit-comment,.community-delete-comment,.community-complete-event,.ewaste-event-done,.ewaste-owner-action').forEach(e=>{if(!signed)e.hidden=true;});
  document.querySelectorAll('.post-signin-note').forEach(e=>e.remove());
}
function ecoClearFreshInputs(){
  for(const element of document.querySelectorAll('input,textarea')){
    // Stored account settings are populated from the authenticated profile.
    if(element.closest('#profile-panel')||element.id.startsWith('switch-account-')||/^(bike-)?departure-/.test(element.id))continue;
    element.autocomplete='off';
    if(!['checkbox','radio','button','submit','hidden','range'].includes(element.type)){
      if(!element.dataset.ecoFieldName){element.name=`eco-${element.id||'field'}-${window.ecoSessionState.nonce}`;element.dataset.ecoFieldName='1';}
      if(['date','datetime-local','time','number','color'].includes(element.type))element.value=element.defaultValue;
      else element.value='';
    }
  }
  document.querySelectorAll('form').forEach(f=>f.autocomplete='off');
  document.querySelectorAll('datalist').forEach(d=>d.replaceChildren());
  ecoResetDepartureDefaults();
}
async function loadOriginalProfile(){
  const state=window.ecoSessionState;
  let account=null;
  try{
    const response=await fetchWithTimeout('/api/profile/me',{},8000);
    if(response.status!==401)account=await readApiResponse(response);
  }catch(_){return;}
  if(state.ready&&state.account?.username!==account?.username){window.location.replace('/app?mode='+ (account?'profile':'guest'));return;}
  const first=!state.ready;state.account=account;state.ready=true;currentEcoTraceAccount=account;
  if(first){ecoClearFreshInputs();ecoMigrateNamedChats();}
  renderOriginalProfile();refreshSavedChats();renderSavedLocations();
  if(first&&account){loadCommunityPosts();loadEwasteListings();loadEwasteEvents();}
}
function ecoInvalidateChat(){
  window.ecoSessionState.chatGeneration++;
  const button=document.getElementById('assistant-submit');if(button)button.disabled=false;
}
function refreshSavedChats(){
  const sidebar=document.getElementById('assistant-chat-sidebar');if(!sidebar)return;
  const signed=Boolean(window.ecoSessionState.account);
  const save=document.getElementById('assistant-save-chat');if(save)save.textContent=signed?'Save chat':'Keep in this tab';
  let hint=document.getElementById('eco-chat-storage-hint');
  if(!hint&&save){hint=document.createElement('p');hint.id='eco-chat-storage-hint';hint.className='mt-2 text-[11px] leading-4 text-slate-400';save.parentElement.after(hint);}
  if(hint)hint.textContent=signed?'Saved chats stay with this account on this browser. Delete removes a saved conversation.':'Guest chats are temporary and disappear when this page is closed, left or reloaded.';
  const chats=getSavedChats();sidebar.replaceChildren();
  if(!chats.length){sidebar.innerHTML='<p class="text-xs text-slate-400">No saved chats yet.</p>';return;}
  for(const chat of chats){
    const row=document.createElement('div');row.className='mb-2 rounded-lg bg-white/5 p-2 ring-1 ring-white/10';
    const load=document.createElement('button');load.type='button';load.className='block w-full truncate text-left text-xs font-bold text-violet-100';load.textContent=chat.title;load.title=chat.title;
    load.onclick=()=>{ecoInvalidateChat();activeSavedAssistantChatId=chat.id;ecoTraceAssistantHistory=structuredClone(chat.turns);clearAssistantComposer();renderAssistantConversation();refreshSavedChats();document.getElementById('assistant-chat-status').textContent='Conversation opened.';};
    const remove=document.createElement('button');remove.type='button';remove.className='assistant-delete-chat mt-2 rounded bg-red-400/10 px-2 py-1 text-xs text-red-200';remove.textContent='Delete';remove.setAttribute('aria-label','Delete '+chat.title);
    remove.onclick=event=>{event.preventDefault();event.stopPropagation();setSavedChats(getSavedChats().filter(c=>c.id!==chat.id));if(activeSavedAssistantChatId===chat.id){ecoInvalidateChat();activeSavedAssistantChatId=null;ecoTraceAssistantHistory=[];clearAssistantComposer();renderAssistantConversation();}refreshSavedChats();document.getElementById('assistant-chat-status').textContent='Saved chat deleted.';};
    row.append(load,remove);sidebar.append(row);
  }
}
function saveCurrentAssistantChat(){
  if(!window.ecoSessionState.ready){ecoToast('Checking your sign-in. Please try again in a moment.');return;}
  if(!ecoTraceAssistantHistory.length){ecoToast('Ask a question before saving a chat.');return;}
  const chats=getSavedChats();const index=chats.findIndex(c=>c.id===activeSavedAssistantChatId);
  const chat={id:index>=0?chats[index].id:crypto.randomUUID(),title:assistantChatTitle(ecoTraceAssistantHistory),turns:structuredClone(ecoTraceAssistantHistory.slice(-30)),savedAt:new Date().toISOString()};
  if(index>=0)chats[index]=chat;else chats.push(chat);
  setSavedChats(chats);activeSavedAssistantChatId=chat.id;refreshSavedChats();
  document.getElementById('assistant-chat-status').textContent=window.ecoSessionState.account?'Chat saved for this account on this browser.':'Chat kept for this tab only.';
}
function routeShadeLabel(data,value){return data.nighttime?'Nighttime — shade score not applicable':`${Number(value).toFixed(1)}/100 modeled shade`;}
function routeShadowLabel(data,value){return data.nighttime?'Not applicable at night':`${Number(value).toFixed(1)}/100`;}
function routeLightingGuide(data){return `<div class="my-3 rounded-xl bg-sky-300/10 p-3 text-xs leading-5 text-sky-100">${escapeHtml(data.lighting_note||'Shade: 0 = low modeled shade; 100 = high modeled shade. Tree/building values describe shadow contributions, not counts.')}</div>`;}
function ecoSetupSession(){
  ecoClearFreshInputs();
  for(const key of ['ecotrace:guest:guest:assistantChats','ecotrace:guest:guest:savedLocations'])localStorage.removeItem(key);
  document.addEventListener('click',event=>{const button=event.target.closest('button');const message=ecoRestrictedMessage(button);if(message){event.preventDefault();event.stopImmediatePropagation();ecoToast(message);}},true);
  document.getElementById('assistant-new-chat')?.addEventListener('click',ecoInvalidateChat,true);
  document.getElementById('reset-button')?.addEventListener('click',()=>{ecoInvalidateChat();activeSavedAssistantChatId=null;},true);
  window.addEventListener('pageshow',event=>{if(event.persisted){window.location.reload();return;}ecoClearFreshInputs();});
  // A different account may have signed in through another tab.
  window.addEventListener('focus',()=>{if(window.ecoSessionState.ready)loadOriginalProfile();});
  applyPostPermissions();
}
ecoSetupSession();

function ecoDefaultDepartureParts(now=Date.now()){
  const target=new Date(Math.ceil((now+15*60*1000)/60000)*60000);
  const parts=Object.fromEntries(new Intl.DateTimeFormat('en-US',{
    timeZone:'America/Los_Angeles',year:'numeric',month:'2-digit',day:'2-digit',
    hour:'2-digit',minute:'2-digit',hourCycle:'h23',timeZoneName:'longOffset'
  }).formatToParts(target).filter(p=>p.type!=='literal').map(p=>[p.type,p.value]));
  const hour=Number(parts.hour)%24;
  const date=`${parts.year}-${parts.month}-${parts.day}`;
  const offset=parts.timeZoneName.replace('GMT','')||'+00:00';
  return {date,hour:String(hour%12||12),minute:parts.minute,ampm:hour>=12?'PM':'AM',value:`${date}T${String(hour).padStart(2,'0')}:${parts.minute}${offset}`};
}
function ecoSetDefaultDeparture(prefix='',force=false){
  window.ecoDepartureManual??={};
  if(force)window.ecoDepartureManual[prefix]=false;
  if(window.ecoDepartureManual[prefix])return;
  const parts=ecoDefaultDepartureParts();
  window.ecoDepartureAutoValues??={};window.ecoDepartureAutoValues[prefix]=parts.value;
  for(const field of ['date','hour','minute','ampm']){
    const el=document.getElementById(prefix+'departure-'+field);if(!el)continue;
    el.value=parts[field];
    if(field!=='ampm')el.defaultValue=parts[field];
  }
}
function ecoResetDepartureDefaults(){
  for(const prefix of ['','bike-']){
    ecoSetDefaultDeparture(prefix,true);
    const toggle=document.getElementById(prefix+'ignore-time');if(toggle)toggle.checked=false;
    const controls=document.getElementById(prefix+'time-controls');
    controls?.classList.remove('opacity-40');
    controls?.querySelectorAll('input,select').forEach(el=>el.disabled=false);
  }
}
function routeLightingGuide(data){
  return '<p class="my-2 text-xs text-slate-300">Shade scale: <strong>0/100 = low modeled shade · 100/100 = high modeled shade.</strong></p>';
}
function routeScoreExplanation(data,value){
  if(data.nighttime||data.shade_applicable===false)return '';
  const score=Number(value);if(!Number.isFinite(score))return '';
  const level=score<10?'very little':score<36?'low':score<62?'moderate':'high';
  return `<p class="mt-2 text-xs leading-5 text-slate-300">${score.toFixed(1)}/100 suggests ${level} modeled shade across this route. Longer stretches count more than brief shaded spots. Nearby trees and buildings help when their modeled shadows reach the path; the score is an index, not an exact percentage of shaded pavement.</p>`;
}
function ecoAttachDepartureControls(){
  for(const prefix of ['','bike-']){
    for(const field of ['date','hour','minute','ampm']){
      const el=document.getElementById(prefix+'departure-'+field);
      for(const eventName of ['input','change'])el?.addEventListener(eventName,()=>{
        window.ecoDepartureManual??={};window.ecoDepartureManual[prefix]=true;
      });
    }
    document.getElementById(prefix+'ignore-time')?.addEventListener('change',event=>{
      if(!event.target.checked)ecoSetDefaultDeparture(prefix);
    });
  }
}
ecoAttachDepartureControls();

</script>
</body>
</html>'''


@app.get("/tree-review", response_class=HTMLResponse)
def tree_review_page():
    path = Path(__file__).resolve().parent / "tree_review.html"
    if not path.is_file():
        return RedirectResponse("/canopy-review", status_code=307)
    return HTMLResponse(path.read_text(encoding="utf-8"))

# ECOTRACE_POLISH_V7
from ecotrace_post_policy import install_post_policy
install_post_policy(app, current_username, AUTH_DB_PATH)

# ECOTRACE_SESSION_LIGHTING_V8
from ecotrace_lighting import departure_time, night_segment, annotate_route

# ECOTRACE_EVENT_ARCHIVE_STARTUP_V2
from ecotrace_event_archive import install as _install_event_archive
_install_event_archive(app)

if __name__ == "__main__":
    import uvicorn

    print("Starting EcoTrace FINAL-INPUT-POLISH build 6.14.9 on http://127.0.0.1:8093")
    print(
        "Tree site database:",
        os.getenv("ECOTRACE_SITE_DB", "default router selection"),
    )
    print(
        "Authoritative planting-site database:",
        Path(os.getenv("ECOTRACE_SITE_DB", str(_experimental_site_db))).name,
        f"({len(PLANTING_SITES):,} loaded sites)",
    )
    print(
        "Mapbox geocoding:",
        "enabled" if _mapbox_access_token() else "disabled (set MAPBOX_ACCESS_TOKEN or MAPBOX_TOKEN)",
    )
    print("Environmental fusion layers:", environmental_status())
    print(
        "Shade model:",
        Path(CANOPY_MODEL_PATH).resolve(),
        f"| {len(CANOPY_ENGINE.trees):,} trees",
        f"| {len(CANOPY_ENGINE.buildings):,} buildings",
        "| County building model active:",
        Path(CANOPY_MODEL_PATH).name == "canopy_model_county_buildings.json",
    )
    print(
        "ArcGIS enhanced site DB:",
        Path(os.getenv("ECOTRACE_SITE_DB", "proposed_tree_sites_arcgis_strict.json")).exists(),
    )
    print(
        "OpenRouteService key loaded:",
        bool(os.getenv("ORS_API_KEY")),
        "| OpenAI key loaded:",
        bool(os.getenv("OPENAI_API_KEY")),
        "| Gemini key loaded:",
        bool(os.getenv("GEMINI_API_KEY")),
    )
    uvicorn.run(app, host="127.0.0.1", port=8093)

# EcoTrace routing performance patch v1

# ECOTRACE_ROUTE_POLISH_V9

# ECOTRACE_AUTO_ARCHIVE_V2

# ECOTRACE_CURRENT_POSTS_V3
