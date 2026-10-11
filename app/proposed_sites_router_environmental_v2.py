"""
FastAPI router for EcoTrace proposed tree sites.

Mount it in your main application:

    from fastapi import FastAPI
    from proposed_sites_router import router

    app = FastAPI()
    app.include_router(router)

The JSON path defaults to proposed_tree_sites.json. Override it with:

    export ECOTRACE_SITE_DB="/absolute/path/proposed_tree_sites.json"
"""

from __future__ import annotations

import json
import math
import os
import threading
from collections import defaultdict
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from approved_tree_catalog import choose_tree_for_site
from ecotrace_environmental_layers import assess_tree_site, environmental_status


EARTH_RADIUS_M = 6_371_008.8
SEARCH_RADIUS_M = 800.0
INDEX_CELL_M = 400.0

router = APIRouter(prefix="/api", tags=["proposed-tree-sites"])


class ClickRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    latitude: float = Field(ge=-90.0, le=90.0)
    longitude: float = Field(ge=-180.0, le=180.0)


class GridBlock(BaseModel):
    row: int = 0
    column: int = 0
    size_meters: float = 500.0
    existing_tree_count: int = 0


class ProposedTreeSite(BaseModel):
    """
    Compatible with original, strict, and newer generated databases.

    Missing presentation fields are normalized by SiteDatabase before validation.
    """
    model_config = ConfigDict(extra="ignore")

    site_id: str
    latitude: float
    longitude: float
    distance_from_click_meters: float
    within_preferred_radius: bool = True

    road_name: str = "Unnamed mapped street"
    road_osm_id: int = 0
    road_class: str | None = None
    sidewalk_tag: str = "not mapped"
    road_heading_degrees: float = 0.0
    offset_side: str = "unknown"
    centerline_offset_meters: float = 0.0

    grid_block: GridBlock = Field(default_factory=GridBlock)
    priority: str = "medium"
    planting_interval_meters: float = 30.0
    soil_profile: str = "Standard Valley Alluvial Soil"
    environmental_exposure: str = "Mixed Open-Space Exposure"
    surface_context: str = "Mapped Open-Space Edge"
    mapped_open_space: str | None = None

    recommended_species: str = "City-list species review required"
    recommended_tree_id: str | None = None
    recommended_tree_size: str | None = None
    recommended_tree_height_ft: int | None = None
    recommended_tree_width_ft: int | None = None
    recommended_tree_type: str | None = None
    recommended_tree_type_description: str | None = None
    recommended_tree_flower: str | None = None
    recommended_tree_description: str | None = None
    recommended_tree_planting_notes: str | None = None
    recommended_tree_image_url: str | None = None
    nearest_mapped_tree_meters: float | None = None
    nearest_mapped_intersection_meters: float | None = None

    source_type: str = "algorithm_screened"
    site_type: str = "potential planting opportunity"
    evidence_confidence: str = "limited"
    environmental_score: float = 0.0
    available_environmental_sources: list[str] = Field(default_factory=list)
    environmental_positive_evidence: list[str] = Field(default_factory=list)
    environmental_cautions: list[str] = Field(default_factory=list)
    wri_canopy_max_height_m: float | None = None
    wri_canopy_fraction: float | None = None
    soil_assessment: str = "Soil layer unavailable or not yet cached"
    parcel_assessment: str = "Ownership/public planting rights unverified"
    county_canopy_percent: float | None = None
    county_plantable_percent: float | None = None
    suitable_tree_filters: list[str] = Field(default_factory=list)
    suitable_tree_size: str | None = None

    where_location: str
    why_chosen: str
    screening_status: str = (
        "Potential site — municipal and field verification required"
    )
    screening_score: float = 0.0


def haversine_m(
    latitude_1: float,
    longitude_1: float,
    latitude_2: float,
    longitude_2: float,
) -> float:
    """Pure-Python great-circle distance."""
    lat1 = math.radians(latitude_1)
    lat2 = math.radians(latitude_2)
    delta_lat = lat2 - lat1
    delta_lon = math.radians(longitude_2 - longitude_1)
    value = (
        math.sin(delta_lat / 2.0) ** 2
        + math.cos(lat1)
        * math.cos(lat2)
        * math.sin(delta_lon / 2.0) ** 2
    )
    return 2.0 * EARTH_RADIUS_M * math.atan2(
        math.sqrt(value),
        math.sqrt(max(0.0, 1.0 - value)),
    )


def infer_site_type(site: dict[str, Any]) -> str:
    text = " ".join(
        str(site.get(key) or "")
        for key in ("mapped_open_space", "location_group", "where_location", "surface_context", "road_name")
    ).lower()
    if "school" in text or "campus" in text:
        return "school-campus opportunity"
    if "park" in text:
        return "park opportunity"
    if site.get("source_type") == "manual_user_selected":
        return "manual potential opportunity"
    if site.get("road_name") and str(site.get("road_name")).lower() not in {"unnamed mapped street", "none"}:
        return "street / public-right-of-way review"
    return "public/open-space review"


class SiteDatabase:
    """
    Thread-safe JSON loader with automatic reload and a lightweight bucket index.
    """

    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path
        self._lock = threading.RLock()
        self._mtime_ns: int | None = None
        self._metadata: dict[str, Any] = {}
        self._sites: list[dict[str, Any]] = []
        self._buckets: dict[tuple[int, int], list[int]] = defaultdict(list)

    @staticmethod
    def _bucket_key(latitude: float, longitude: float) -> tuple[int, int]:
        lat_m = latitude * 111_320.0
        lon_m = (
            longitude
            * 111_320.0
            * max(math.cos(math.radians(latitude)), 0.2)
        )
        return (
            math.floor(lat_m / INDEX_CELL_M),
            math.floor(lon_m / INDEX_CELL_M),
        )

    def _ensure_loaded(self) -> None:
        with self._lock:
            try:
                stat = self.database_path.stat()
            except FileNotFoundError as error:
                raise RuntimeError(
                    f"Tree-site database not found: {self.database_path}. "
                    "Run generate_tree_sites.py first."
                ) from error

            if self._mtime_ns == stat.st_mtime_ns:
                return

            try:
                payload = json.loads(
                    self.database_path.read_text(encoding="utf-8")
                )
            except (OSError, json.JSONDecodeError) as error:
                raise RuntimeError(
                    f"Could not parse tree-site database: {error}"
                ) from error

            sites = payload.get("sites")
            if not isinstance(sites, list):
                raise RuntimeError(
                    "Tree-site database must contain a top-level 'sites' list."
                )

            buckets: dict[tuple[int, int], list[int]] = defaultdict(list)
            clean_sites: list[dict[str, Any]] = []

            for raw_site in sites:
                if not isinstance(raw_site, dict):
                    continue
                try:
                    latitude = float(raw_site["latitude"])
                    longitude = float(raw_site["longitude"])
                except (KeyError, TypeError, ValueError):
                    continue

                clean_site = dict(raw_site)
                clean_site["latitude"] = latitude
                clean_site["longitude"] = longitude
                site_index = len(clean_sites)
                clean_sites.append(clean_site)
                buckets[self._bucket_key(latitude, longitude)].append(site_index)

            self._metadata = (
                payload.get("metadata")
                if isinstance(payload.get("metadata"), dict)
                else {}
            )
            self._sites = clean_sites
            self._buckets = buckets
            self._mtime_ns = stat.st_mtime_ns

    def query_within(
        self,
        latitude: float,
        longitude: float,
        radius_m: float = SEARCH_RADIUS_M,
    ) -> list[dict[str, Any]]:
        self._ensure_loaded()

        with self._lock:
            center = self._bucket_key(latitude, longitude)
            cell_radius = max(
                1,
                math.ceil(radius_m / INDEX_CELL_M) + 1,
            )
            candidate_indices: set[int] = set()

            for dx in range(-cell_radius, cell_radius + 1):
                for dy in range(-cell_radius, cell_radius + 1):
                    candidate_indices.update(
                        self._buckets.get(
                            (center[0] + dx, center[1] + dy),
                            (),
                        )
                    )

            results: list[dict[str, Any]] = []
            for site_index in candidate_indices:
                site = self._sites[site_index]
                distance = haversine_m(
                    latitude,
                    longitude,
                    site["latitude"],
                    site["longitude"],
                )
                if distance <= radius_m:
                    results.append(
                        self._normalize_site(
                            site,
                            distance,
                            within_preferred_radius=True,
                        )
                    )

            results.sort(
                key=lambda item: (
                    item["distance_from_click_meters"],
                    -float(item.get("screening_score", 0.0)),
                    item.get("site_id", ""),
                )
            )
            return results

    @staticmethod
    def _normalize_site(
        raw_site: dict[str, Any],
        distance_m: float,
        *,
        within_preferred_radius: bool,
    ) -> dict[str, Any]:
        """
        Normalize records produced by different generator versions.

        Older strict records may omit presentation-only fields such as priority,
        soil_profile, or screening_score. Those fields are derived here instead
        of rejecting an otherwise valid screened coordinate.
        """
        site = dict(raw_site)

        latitude = float(site["latitude"])
        longitude = float(site["longitude"])
        road_name = str(site.get("road_name") or "Unnamed mapped street")
        mapped_open_space = site.get("mapped_open_space")

        grid = site.get("grid_block")
        if not isinstance(grid, dict):
            grid = {}
        tree_count = int(
            grid.get(
                "existing_tree_count",
                site.get("existing_tree_count", 0),
            )
            or 0
        )
        grid = {
            "row": int(grid.get("row", 0) or 0),
            "column": int(grid.get("column", 0) or 0),
            "size_meters": float(grid.get("size_meters", 500.0) or 500.0),
            "existing_tree_count": tree_count,
        }

        priority = str(site.get("priority") or "").strip().lower()
        if priority not in {"high", "medium", "low"}:
            if tree_count < 8:
                priority = "high"
            elif tree_count > 30:
                priority = "low"
            else:
                priority = "medium"

        soil_profile = str(site.get("soil_profile") or "").strip()
        if not soil_profile:
            soil_profile = (
                "Heavy Clay-Alviso Loam Blend"
                if latitude > 37.450
                else "Standard Valley Alluvial Soil"
            )

        exposure = str(site.get("environmental_exposure") or "").strip()
        if not exposure:
            exposure = (
                "Full Sun Exposure"
                if priority == "high"
                else "Mixed Open-Space Exposure"
            )

        surface_context = str(site.get("surface_context") or "").strip()
        if not surface_context:
            surface_context = (
                "Mapped Open-Space Edge"
                if mapped_open_space
                else "Screened Streetscape Edge"
            )

        # Keep a deterministic example internally for compatibility, but the UI
        # no longer presents one exact species as the answer for a coordinate.
        tree = choose_tree_for_site(site)
        species = "Open Tree Library to review locally suitable species"

        environmental = assess_tree_site(latitude, longitude)

        score_value = site.get("screening_score")
        try:
            screening_score = float(score_value)
        except (TypeError, ValueError):
            priority_score = {"high": 100.0, "medium": 60.0, "low": 25.0}[priority]
            tree_gap = site.get("nearest_mapped_tree_meters")
            try:
                tree_gap_bonus = min(float(tree_gap), 250.0) / 5.0
            except (TypeError, ValueError):
                tree_gap_bonus = 0.0
            open_space_bonus = 15.0 if mapped_open_space else 0.0
            screening_score = priority_score + tree_gap_bonus + open_space_bonus

        screening_score += float(environmental.get("environmental_score_adjustment", 0.0) or 0.0)

        where_location = str(site.get("where_location") or "").strip()
        if not where_location:
            where_location = (
                f"Near {road_name} at "
                f"{latitude:.7f}, {longitude:.7f}, on a screened mapped "
                f"{mapped_open_space or 'open-space'} edge."
            )

        why_chosen = str(site.get("why_chosen") or "").strip()
        if not why_chosen:
            why_chosen = (
                f"This candidate passed the generator's mapped obstacle, "
                f"intersection, existing-tree, open-space, and spacing checks. "
                f"The block contains {tree_count} mapped trees, producing a "
                f"{priority}-priority classification. EcoTrace then adds any available "
                f"WRI/Meta canopy-height, County soil, parcel, and neighborhood-canopy "
                f"evidence. It does not claim that the site or a particular species is "
                f"approved. Municipal, utility, ownership, irrigation, ADA, and field "
                f"verification are still required."
            )

        site.update(
            {
                "latitude": latitude,
                "longitude": longitude,
                "distance_from_click_meters": round(distance_m, 1),
                "within_preferred_radius": within_preferred_radius,
                "road_name": road_name,
                "road_osm_id": int(site.get("road_osm_id", 0) or 0),
                "road_class": site.get("road_class"),
                "sidewalk_tag": str(site.get("sidewalk_tag") or "not mapped"),
                "road_heading_degrees": float(
                    site.get("road_heading_degrees", 0.0) or 0.0
                ),
                "offset_side": str(site.get("offset_side") or "unknown"),
                "centerline_offset_meters": float(
                    site.get("centerline_offset_meters", 0.0) or 0.0
                ),
                "grid_block": grid,
                "priority": priority,
                "planting_interval_meters": float(
                    site.get("planting_interval_meters", 30.0) or 30.0
                ),
                "soil_profile": soil_profile,
                "environmental_exposure": exposure,
                "surface_context": surface_context,
                "mapped_open_space": mapped_open_space,
                "source_type": str(site.get("source_type") or "algorithm_screened"),
                "site_type": infer_site_type(site),
                "evidence_confidence": str(environmental.get("evidence_confidence") or "limited"),
                "environmental_score": round(float(environmental.get("environmental_score_adjustment", 0.0) or 0.0), 1),
                "available_environmental_sources": list(environmental.get("available_sources") or []),
                "environmental_positive_evidence": list(environmental.get("positive_evidence") or []),
                "environmental_cautions": list(environmental.get("cautions") or []),
                "wri_canopy_max_height_m": (environmental.get("wri_canopy") or {}).get("max_height_m"),
                "wri_canopy_fraction": (environmental.get("wri_canopy") or {}).get("canopy_sample_fraction"),
                "soil_assessment": str((environmental.get("soil") or {}).get("summary") or "Soil layer unavailable or not yet cached"),
                "parcel_assessment": str((environmental.get("parcel") or {}).get("summary") or "Ownership/public planting rights unverified"),
                "county_canopy_percent": (environmental.get("county_canopy") or {}).get("canopy_percent"),
                "county_plantable_percent": (environmental.get("county_canopy") or {}).get("plantable_percent"),
                "suitable_tree_filters": list(environmental.get("suitable_tree_filters") or []),
                "suitable_tree_size": (
                    "large" if infer_site_type(site) in {"park opportunity", "school-campus opportunity"}
                    else "medium" if infer_site_type(site) == "street / public-right-of-way review"
                    else None
                ),
                "recommended_species": species,
                "recommended_tree_id": tree["id"],
                "recommended_tree_size": tree["size"],
                "recommended_tree_height_ft": tree["height_ft"],
                "recommended_tree_width_ft": tree["width_ft"],
                "recommended_tree_type": tree["type_code"],
                "recommended_tree_type_description": tree["type_description"],
                "recommended_tree_flower": tree["flower"],
                "recommended_tree_description": tree["description"],
                "recommended_tree_planting_notes": tree["planting_notes"],
                "recommended_tree_image_url": tree["image_url"],
                "where_location": where_location,
                "why_chosen": why_chosen,
                "screening_status": str(
                    site.get("screening_status")
                    or "Potential site — municipal and field verification required"
                ),
                "screening_score": round(screening_score, 2),
            }
        )
        return site

    def query_nearest(
        self,
        latitude: float,
        longitude: float,
    ) -> dict[str, Any] | None:
        self._ensure_loaded()
        with self._lock:
            if not self._sites:
                return None
            nearest_site = min(
                self._sites,
                key=lambda site: haversine_m(
                    latitude,
                    longitude,
                    site["latitude"],
                    site["longitude"],
                ),
            )
            distance = haversine_m(
                latitude,
                longitude,
                nearest_site["latitude"],
                nearest_site["longitude"],
            )
            return self._normalize_site(
                nearest_site,
                distance,
                within_preferred_radius=False,
            )

    @property
    def metadata(self) -> dict[str, Any]:
        self._ensure_loaded()
        with self._lock:
            return dict(self._metadata)


def _default_site_database() -> str:
    arcgis = Path("proposed_tree_sites_arcgis_strict.json")
    if arcgis.exists():
        return str(arcgis)
    strict = Path("proposed_tree_sites_strict.json")
    if strict.exists():
        return str(strict)
    return "proposed_tree_sites.json"


DATABASE_PATH = Path(
    os.getenv("ECOTRACE_SITE_DB", _default_site_database())
).expanduser()

database = SiteDatabase(DATABASE_PATH)


@router.post(
    "/proposed-sites",
    response_model=list[ProposedTreeSite],
    summary="Return screened proposed tree sites within 800 meters",
)
async def proposed_sites(request: ClickRequest) -> list[ProposedTreeSite]:
    """
    Return every generated candidate within a strict 800-meter radius.

    Results are sorted nearest-first. They are automated potential planning
    sites and still require municipal and field verification.
    """
    try:
        raw_results = database.query_within(
            request.latitude,
            request.longitude,
            SEARCH_RADIUS_M,
        )

        if raw_results:
            selected = raw_results[:3]
        else:
            fallback = database.query_nearest(
                request.latitude,
                request.longitude,
            )
            selected = [fallback] if fallback is not None else []

        return [
            ProposedTreeSite.model_validate(item)
            for item in selected
        ]
    except RuntimeError as error:
        raise HTTPException(
            status_code=503,
            detail=str(error),
        ) from error
    except Exception as error:
        raise HTTPException(
            status_code=500,
            detail=(
                f"Unexpected proposed-site database error: "
                f"{type(error).__name__}: {error}"
            ),
        ) from error


@router.get("/environmental-layers/status", summary="Return optional environmental-layer status")
async def environmental_layers_status() -> dict[str, Any]:
    return environmental_status()


@router.get(
    "/proposed-sites/metadata",
    summary="Return database generation metadata",
)
async def proposed_sites_metadata() -> dict[str, Any]:
    try:
        metadata = database.metadata
        metadata["search_radius_meters"] = SEARCH_RADIUS_M
        metadata["database_path"] = str(DATABASE_PATH)
        metadata["arcgis_database_active"] = (
            DATABASE_PATH.name == "proposed_tree_sites_arcgis_strict.json"
        )
        metadata["species_assignment"] = (
            "EcoTrace no longer presents one exact tree species as approved for a coordinate. "
            "Candidate cards provide site constraints and direct users to a filtered Tree Library."
        )
        metadata["environmental_layers"] = environmental_status()
        metadata["loaded_site_count"] = len(database._sites)
        return metadata
    except RuntimeError as error:
        raise HTTPException(
            status_code=503,
            detail=str(error),
        ) from error
