from __future__ import annotations
from ecotrace_route_performance import raster_dataset as _eco_raster_dataset

import json
import math
import os
from functools import lru_cache
from pathlib import Path
from typing import Any

try:
    import rasterio
except Exception:  # optional dependency
    rasterio = None

try:
    from shapely.geometry import Point, shape
    from shapely.strtree import STRtree
except Exception:  # optional dependency
    Point = shape = STRtree = None


DATA_DIR = Path(os.getenv("ECOTRACE_ENV_DATA_DIR", ".")).expanduser()
WRI_CANOPY_PATH = Path(
    os.getenv("ECOTRACE_WRI_CANOPY", str(DATA_DIR / "wri_tree_canopy_height.tif"))
).expanduser()
SOIL_PATH = Path(
    os.getenv("ECOTRACE_SOIL_GEOJSON", str(DATA_DIR / "soil_conditions.geojson"))
).expanduser()
PARCEL_PATH = Path(
    os.getenv("ECOTRACE_PARCEL_GEOJSON", str(DATA_DIR / "property_parcels.geojson"))
).expanduser()
COUNTY_CANOPY_PATH = Path(
    os.getenv("ECOTRACE_COUNTY_CANOPY_GEOJSON", str(DATA_DIR / "county_canopy.geojson"))
).expanduser()


def _safe_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _normalized_properties(properties: dict[str, Any]) -> dict[str, Any]:
    return {str(k).strip().lower(): v for k, v in properties.items()}


def _first_matching_value(props: dict[str, Any], keywords: tuple[str, ...]) -> Any:
    normalized = _normalized_properties(props)
    for keyword in keywords:
        for key, value in normalized.items():
            if keyword in key and value not in (None, ""):
                return value
    return None


class PolygonLayer:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.available = False
        self.features: list[dict[str, Any]] = []
        self.geometries: list[Any] = []
        self._tree: Any = None
        self.error: str | None = None
        self._load()

    def _load(self) -> None:
        if shape is None or STRtree is None:
            self.error = "shapely is not installed"
            return
        if not self.path.exists():
            self.error = f"missing {self.path.name}"
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            features = payload.get("features") if isinstance(payload, dict) else None
            if not isinstance(features, list):
                raise ValueError("GeoJSON must contain a features list")
            clean_features: list[dict[str, Any]] = []
            geometries: list[Any] = []
            for feature in features:
                if not isinstance(feature, dict) or not feature.get("geometry"):
                    continue
                try:
                    geometry = shape(feature["geometry"])
                except Exception:
                    continue
                if geometry.is_empty:
                    continue
                clean_features.append(feature)
                geometries.append(geometry)
            self.features = clean_features
            self.geometries = geometries
            self._tree = STRtree(geometries) if geometries else None
            self.available = bool(geometries)
        except Exception as exc:
            self.error = f"{type(exc).__name__}: {exc}"

    def containing(self, latitude: float, longitude: float) -> list[dict[str, Any]]:
        if not self.available or self._tree is None or Point is None:
            return []
        point = Point(longitude, latitude)
        results: list[dict[str, Any]] = []
        try:
            indexes = self._tree.query(point, predicate="intersects")
            for index in indexes:
                idx = int(index)
                if self.geometries[idx].intersects(point):
                    results.append(self.features[idx])
        except Exception:
            # Safe fallback for older Shapely behavior.
            for feature, geometry in zip(self.features, self.geometries):
                if geometry.intersects(point):
                    results.append(feature)
        return results


class WriCanopyHeight:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.available = bool(rasterio is not None and path.exists())
        self.error: str | None = None
        if rasterio is None:
            self.error = "rasterio is not installed"
        elif not path.exists():
            self.error = f"missing {path.name}"

    @lru_cache(maxsize=20000)
    def height_at(self, latitude_rounded: float, longitude_rounded: float) -> float | None:
        if not self.available or rasterio is None:
            return None
        try:
            with _eco_raster_dataset(self.path, rasterio) as dataset:
                x, y = longitude_rounded, latitude_rounded
                if dataset.crs and str(dataset.crs).upper() != "EPSG:4326":
                    from rasterio.warp import transform
                    xs, ys = transform("EPSG:4326", dataset.crs, [x], [y])
                    x, y = xs[0], ys[0]
                value = next(dataset.sample([(x, y)]))[0]
                if dataset.nodata is not None and value == dataset.nodata:
                    return None
                height = _safe_float(value)
                if height is None or height < 0 or height > 100:
                    return None
                return height
        except Exception as exc:
            self.error = f"{type(exc).__name__}: {exc}"
            return None

    def sample_height(self, latitude: float, longitude: float) -> float | None:
        return self.height_at(round(latitude, 6), round(longitude, 6))

    def local_canopy_signal(
        self,
        latitude: float,
        longitude: float,
        *,
        radius_m: float = 18.0,
    ) -> dict[str, Any]:
        """Sample center + eight surrounding points to detect existing canopy."""
        if not self.available:
            return {
                "available": False,
                "max_height_m": None,
                "mean_height_m": None,
                "canopy_sample_fraction": None,
            }
        cos_lat = max(math.cos(math.radians(latitude)), 0.2)
        dlat = radius_m / 111_320.0
        dlon = radius_m / (111_320.0 * cos_lat)
        offsets = [
            (0.0, 0.0),
            (dlat, 0.0), (-dlat, 0.0),
            (0.0, dlon), (0.0, -dlon),
            (dlat * 0.707, dlon * 0.707),
            (dlat * 0.707, -dlon * 0.707),
            (-dlat * 0.707, dlon * 0.707),
            (-dlat * 0.707, -dlon * 0.707),
        ]
        heights = [
            self.sample_height(latitude + dy, longitude + dx)
            for dy, dx in offsets
        ]
        valid = [h for h in heights if h is not None]
        if not valid:
            return {
                "available": True,
                "max_height_m": None,
                "mean_height_m": None,
                "canopy_sample_fraction": None,
            }
        canopy_count = sum(1 for h in valid if h >= 2.0)
        return {
            "available": True,
            "max_height_m": round(max(valid), 1),
            "mean_height_m": round(sum(valid) / len(valid), 1),
            "canopy_sample_fraction": round(canopy_count / len(valid), 3),
        }


SOIL_LAYER = PolygonLayer(SOIL_PATH)
PARCEL_LAYER = PolygonLayer(PARCEL_PATH)
COUNTY_CANOPY_LAYER = PolygonLayer(COUNTY_CANOPY_PATH)
WRI_CANOPY = WriCanopyHeight(WRI_CANOPY_PATH)


def _soil_assessment(latitude: float, longitude: float) -> dict[str, Any]:
    matches = SOIL_LAYER.containing(latitude, longitude)
    if not matches:
        return {
            "available": SOIL_LAYER.available,
            "rating": "unknown",
            "summary": "No cached County soil polygon covers this coordinate.",
            "map_unit": None,
            "constraints": [],
        }
    props = matches[0].get("properties") or {}
    map_unit = _first_matching_value(
        props,
        ("soil name", "map unit name", "muname", "soil type", "mapunit"),
    )
    drainage = _first_matching_value(props, ("drain",))
    hydrologic = _first_matching_value(props, ("hydrolog", "hydgrp"))
    slope = _first_matching_value(props, ("slope",))
    flooding = _first_matching_value(props, ("flood",))
    constraints: list[str] = []
    text = " ".join(str(v).lower() for v in (map_unit, drainage, hydrologic, flooding) if v)
    if any(term in text for term in ("poorly drained", "very poorly", "pond", "frequent flood")):
        constraints.append("Possible drainage/flooding constraint")
    if slope is not None:
        slope_value = _safe_float(slope)
        if slope_value is not None and slope_value >= 15:
            constraints.append("Steeper mapped slope")
    rating = "review" if constraints else "favorable_or_unknown"
    details = []
    if map_unit: details.append(f"soil/map unit: {map_unit}")
    if drainage: details.append(f"drainage: {drainage}")
    if hydrologic: details.append(f"hydrologic group: {hydrologic}")
    if slope: details.append(f"slope: {slope}")
    if flooding: details.append(f"flooding: {flooding}")
    return {
        "available": True,
        "rating": rating,
        "summary": "; ".join(details) if details else "County soil polygon found; field verification still required.",
        "map_unit": str(map_unit) if map_unit is not None else None,
        "constraints": constraints,
    }


PUBLIC_OWNER_KEYWORDS = (
    "city of ", "county of ", "state of california", "united states",
    "school district", "unified school", "elementary school district",
    "community college", "water district", "park district",
)


def _parcel_assessment(latitude: float, longitude: float) -> dict[str, Any]:
    matches = PARCEL_LAYER.containing(latitude, longitude)
    if not matches:
        return {
            "available": PARCEL_LAYER.available,
            "status": "no_parcel_match",
            "summary": "No cached parcel polygon covers this coordinate; ownership remains unverified.",
            "public_agency_detected": False,
        }
    props = matches[0].get("properties") or {}
    # Owner names are intentionally never returned to the UI.
    owner = _first_matching_value(props, ("owner", "agency", "jurisdiction"))
    owner_text = str(owner).lower() if owner is not None else ""
    public_agency = any(keyword in owner_text for keyword in PUBLIC_OWNER_KEYWORDS)
    if public_agency:
        status = "public_agency_candidate"
        summary = "Parcel attributes appear to indicate a public agency; planting still requires agency approval."
    else:
        status = "ownership_unverified_private_possible"
        summary = "A parcel record intersects this point, but EcoTrace cannot verify public planting rights; private property is possible."
    return {
        "available": True,
        "status": status,
        "summary": summary,
        "public_agency_detected": public_agency,
    }


def _county_canopy_assessment(latitude: float, longitude: float) -> dict[str, Any]:
    matches = COUNTY_CANOPY_LAYER.containing(latitude, longitude)
    if not matches:
        return {
            "available": COUNTY_CANOPY_LAYER.available,
            "canopy_percent": None,
            "plantable_percent": None,
            "priority": "unknown",
            "summary": "No cached County canopy planning polygon covers this point.",
        }
    props = matches[0].get("properties") or {}
    canopy = _first_matching_value(props, ("canopy percent", "canopy_pct", "canopy", "tree canopy"))
    plantable = _first_matching_value(props, ("plantable percent", "possible plantable", "plantable", "possible"))
    canopy_value = _safe_float(canopy)
    plantable_value = _safe_float(plantable)
    if canopy_value is not None:
        # Some ArcGIS layers store fractions 0-1 rather than percentages.
        if 0 <= canopy_value <= 1:
            canopy_value *= 100
        priority = "high" if canopy_value < 12 else "medium" if canopy_value < 20 else "lower"
    else:
        priority = "unknown"
    if plantable_value is not None and 0 <= plantable_value <= 1:
        plantable_value *= 100
    parts = []
    if canopy_value is not None: parts.append(f"neighborhood canopy {canopy_value:.1f}%")
    if plantable_value is not None: parts.append(f"possible plantable area {plantable_value:.1f}%")
    return {
        "available": True,
        "canopy_percent": round(canopy_value, 1) if canopy_value is not None else None,
        "plantable_percent": round(plantable_value, 1) if plantable_value is not None else None,
        "priority": priority,
        "summary": "; ".join(parts) if parts else "County canopy planning polygon found.",
    }


def assess_tree_site(latitude: float, longitude: float) -> dict[str, Any]:
    wri = WRI_CANOPY.local_canopy_signal(latitude, longitude)
    soil = _soil_assessment(latitude, longitude)
    parcel = _parcel_assessment(latitude, longitude)
    county = _county_canopy_assessment(latitude, longitude)

    reasons: list[str] = []
    cautions: list[str] = []
    score_adjustment = 0.0

    canopy_fraction = wri.get("canopy_sample_fraction")
    max_height = wri.get("max_height_m")
    if canopy_fraction is not None:
        if canopy_fraction <= 0.15 and (max_height or 0) < 3:
            reasons.append("WRI/Meta canopy-height samples indicate a local canopy gap")
            score_adjustment += 10.0
        elif canopy_fraction >= 0.45:
            cautions.append("WRI/Meta canopy-height samples indicate substantial existing canopy nearby")
            score_adjustment -= 20.0

    if soil.get("constraints"):
        cautions.extend(soil["constraints"])
        score_adjustment -= 8.0
    elif soil.get("available"):
        reasons.append("County soil layer shows no automatically detected major soil constraint")
        score_adjustment += 3.0

    if parcel.get("public_agency_detected"):
        reasons.append("Parcel attributes suggest a public-agency parcel")
        score_adjustment += 5.0
    elif parcel.get("status") == "ownership_unverified_private_possible":
        cautions.append("Parcel/public planting rights are not verified; private property is possible")
        score_adjustment -= 12.0

    if county.get("priority") == "high":
        reasons.append("County canopy planning layer indicates a low-canopy neighborhood")
        score_adjustment += 8.0
    elif county.get("priority") == "lower":
        score_adjustment -= 2.0

    available_sources = [
        name for name, available in (
            ("WRI/Meta canopy height", wri.get("available")),
            ("County soil", soil.get("available")),
            ("parcel/property", parcel.get("available")),
            ("County canopy planning", county.get("available")),
        ) if available
    ]

    confidence = (
        "higher" if len(available_sources) >= 3 and not cautions
        else "moderate" if len(available_sources) >= 2
        else "limited"
    )

    filters: list[str] = []
    if max_height is not None and max_height >= 2:
        filters.append("existing-canopy review required")
    if soil.get("constraints"):
        filters.append("soil/drainage tolerant")
    if parcel.get("public_agency_detected"):
        filters.append("public-agency approval required")
    else:
        filters.append("ownership verification required")

    return {
        "environmental_score_adjustment": round(score_adjustment, 1),
        "evidence_confidence": confidence,
        "available_sources": available_sources,
        "positive_evidence": reasons,
        "cautions": cautions,
        "wri_canopy": wri,
        "soil": soil,
        "parcel": parcel,
        "county_canopy": county,
        "suitable_tree_filters": filters,
    }


def environmental_status() -> dict[str, Any]:
    return {
        "wri_canopy_height": {
            "available": WRI_CANOPY.available,
            "path": str(WRI_CANOPY.path),
            "error": WRI_CANOPY.error,
        },
        "soil": {
            "available": SOIL_LAYER.available,
            "path": str(SOIL_LAYER.path),
            "feature_count": len(SOIL_LAYER.features),
            "error": SOIL_LAYER.error,
        },
        "parcels": {
            "available": PARCEL_LAYER.available,
            "path": str(PARCEL_LAYER.path),
            "feature_count": len(PARCEL_LAYER.features),
            "error": PARCEL_LAYER.error,
        },
        "county_canopy": {
            "available": COUNTY_CANOPY_LAYER.available,
            "path": str(COUNTY_CANOPY_LAYER.path),
            "feature_count": len(COUNTY_CANOPY_LAYER.features),
            "error": COUNTY_CANOPY_LAYER.error,
        },
    }
