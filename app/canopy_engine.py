"""
Pure-Python EcoTrace canopy and shadow engine.

The engine combines:
- real mapped OSM tree positions;
- modeled crown radius, height, and opacity;
- mapped building footprints and estimated height;
- calculated solar altitude/azimuth;
- the existing EcoTrace heat index as an exposure-risk modifier.

It reports separate components so the UI can explain every route score.
"""

from __future__ import annotations

import json
import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable


EARTH_RADIUS_M = 6_371_008.8


@dataclass
class ShadeResult:
    shade_percent: float
    canopy_shadow_percent: float
    building_shadow_percent: float
    terrain_shadow_percent: float
    heat_penalty_percent: float
    solar_altitude_degrees: float
    solar_azimuth_degrees: float
    tree_count_considered: int
    building_count_considered: int
    reason: str
    data_quality: str


class BucketIndex:
    def __init__(self, cell_m: float = 60.0) -> None:
        self.cell_m = cell_m
        self.buckets: dict[tuple[int, int], list[Any]] = defaultdict(list)

    def key(self, lat: float, lon: float) -> tuple[int, int]:
        # Fixed reference latitude keeps nearby points in consistent longitude buckets.
        reference_latitude = 37.37
        lon_scale = 111_320 * max(
            math.cos(math.radians(reference_latitude)), 0.2
        )
        return (
            math.floor(lat * 111_320 / self.cell_m),
            math.floor(lon * lon_scale / self.cell_m),
        )

    def add(self, lat: float, lon: float, value: Any) -> None:
        self.buckets[self.key(lat, lon)].append(value)

    def nearby(self, lat: float, lon: float, radius_m: float) -> Iterable[Any]:
        x, y = self.key(lat, lon)
        reach = max(1, math.ceil(radius_m / self.cell_m) + 1)
        for dx in range(-reach, reach + 1):
            for dy in range(-reach, reach + 1):
                yield from self.buckets.get((x + dx, y + dy), ())


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = phi2 - phi1
    dlambda = math.radians(lon2 - lon1)
    value = (
        math.sin(dphi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    )
    return 2 * EARTH_RADIUS_M * math.atan2(
        math.sqrt(value), math.sqrt(max(0, 1 - value))
    )


def local_xy(
    lat: float, lon: float, origin_lat: float, origin_lon: float
) -> tuple[float, float]:
    return (
        (lon - origin_lon)
        * 111_320
        * max(math.cos(math.radians(origin_lat)), 0.2),
        (lat - origin_lat) * 111_320,
    )


def destination(
    lat: float, lon: float, bearing_deg: float, distance_m: float
) -> tuple[float, float]:
    bearing = math.radians(bearing_deg)
    return (
        lat + math.cos(bearing) * distance_m / 111_320,
        lon
        + math.sin(bearing)
        * distance_m
        / (111_320 * max(math.cos(math.radians(lat)), 0.2)),
    )


def point_in_polygon_xy(
    point: tuple[float, float], polygon: list[tuple[float, float]]
) -> bool:
    if len(polygon) < 3:
        return False
    x, y = point
    inside = False
    j = len(polygon) - 1
    for i, (xi, yi) in enumerate(polygon):
        xj, yj = polygon[j]
        if (yi > y) != (yj > y):
            crossing = (xj - xi) * (y - yi) / (yj - yi) + xi
            if x < crossing:
                inside = not inside
        j = i
    return inside


def convex_hull(points: list[tuple[float, float]]) -> list[tuple[float, float]]:
    unique = sorted(set(points))
    if len(unique) <= 2:
        return unique

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (
            a[1] - o[1]
        ) * (b[0] - o[0])

    lower = []
    for point in unique:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], point) <= 0:
            lower.pop()
        lower.append(point)
    upper = []
    for point in reversed(unique):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], point) <= 0:
            upper.pop()
        upper.append(point)
    return lower[:-1] + upper[:-1]


def solar_position(
    latitude: float,
    longitude: float,
    moment: datetime,
) -> tuple[float, float]:
    """
    Approximate NOAA-style solar position using local clock time.

    Longitude-based solar correction is included. This is sufficient for route
    comparison, but not a survey-grade shadow study.
    """
    day = moment.timetuple().tm_yday
    hour = moment.hour + moment.minute / 60 + moment.second / 3600
    gamma = 2 * math.pi / 365 * (day - 1 + (hour - 12) / 24)

    equation_of_time = 229.18 * (
        0.000075
        + 0.001868 * math.cos(gamma)
        - 0.032077 * math.sin(gamma)
        - 0.014615 * math.cos(2 * gamma)
        - 0.040849 * math.sin(2 * gamma)
    )
    declination = (
        0.006918
        - 0.399912 * math.cos(gamma)
        + 0.070257 * math.sin(gamma)
        - 0.006758 * math.cos(2 * gamma)
        + 0.000907 * math.sin(2 * gamma)
        - 0.002697 * math.cos(3 * gamma)
        + 0.00148 * math.sin(3 * gamma)
    )

    timezone_offset_hours = (
        moment.utcoffset().total_seconds() / 3600 if moment.utcoffset() else -7
    )
    time_offset = equation_of_time + 4 * longitude - 60 * timezone_offset_hours
    true_solar_minutes = hour * 60 + time_offset
    hour_angle = math.radians(true_solar_minutes / 4 - 180)

    latitude_rad = math.radians(latitude)
    cos_zenith = (
        math.sin(latitude_rad) * math.sin(declination)
        + math.cos(latitude_rad)
        * math.cos(declination)
        * math.cos(hour_angle)
    )
    zenith = math.acos(max(-1, min(1, cos_zenith)))
    altitude = 90 - math.degrees(zenith)

    azimuth = math.degrees(
        math.atan2(
            math.sin(hour_angle),
            math.cos(hour_angle) * math.sin(latitude_rad)
            - math.tan(declination) * math.cos(latitude_rad),
        )
    )
    azimuth = (azimuth + 180) % 360
    return altitude, azimuth


class CanopyEngine:
    def __init__(self, model_path: str | Path) -> None:
        self.path = Path(model_path)
        self.available = False
        self.metadata: dict[str, Any] = {}
        self.trees: list[dict[str, Any]] = []
        self.buildings: list[dict[str, Any]] = []
        self.elevations: list[dict[str, float]] = []
        self.tree_index = BucketIndex(45)
        self.building_index = BucketIndex(90)
        self.elevation_index = BucketIndex(800)
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        self.metadata = payload.get("metadata") or {}
        self.trees = payload.get("trees") or []
        self.buildings = payload.get("buildings") or []
        self.elevations = payload.get("elevation_grid") or []

        for tree in self.trees:
            self.tree_index.add(
                float(tree["latitude"]), float(tree["longitude"]), tree
            )
        for building in self.buildings:
            coordinates = building.get("coordinates") or []
            if not coordinates:
                continue
            lat = sum(point[1] for point in coordinates) / len(coordinates)
            lon = sum(point[0] for point in coordinates) / len(coordinates)
            building["_centroid_lat"] = lat
            building["_centroid_lon"] = lon
            self.building_index.add(lat, lon, building)
        for elevation in self.elevations:
            self.elevation_index.add(
                float(elevation["latitude"]),
                float(elevation["longitude"]),
                elevation,
            )
        self.available = bool(self.trees or self.buildings)

    def nearest_elevation(self, lat: float, lon: float) -> float | None:
        candidates = list(self.elevation_index.nearby(lat, lon, 1500))
        if not candidates:
            return None
        nearest = min(
            candidates,
            key=lambda item: haversine_m(
                lat,
                lon,
                float(item["latitude"]),
                float(item["longitude"]),
            ),
        )
        return float(nearest["elevation_m"])

    def tree_shadow_probability(
        self,
        lat: float,
        lon: float,
        altitude: float,
        azimuth: float,
    ) -> tuple[float, int]:
        if altitude <= 0:
            return 0.0, 0

        unshaded = 1.0
        count = 0
        for tree in self.tree_index.nearby(lat, lon, 90):
            tree_lat = float(tree["latitude"])
            tree_lon = float(tree["longitude"])
            height = float(tree.get("height_m", 8))
            radius = float(tree.get("crown_radius_m", 3.5))
            opacity = float(tree.get("opacity", 0.62))
            shadow_length = min(90.0, height / max(math.tan(math.radians(altitude)), 0.08))
            shadow_bearing = (azimuth + 180) % 360
            end_lat, end_lon = destination(
                tree_lat, tree_lon, shadow_bearing, shadow_length
            )

            px, py = local_xy(lat, lon, tree_lat, tree_lon)
            ex, ey = local_xy(end_lat, end_lon, tree_lat, tree_lon)
            denominator = ex * ex + ey * ey
            projection = (
                max(0.0, min(1.0, (px * ex + py * ey) / denominator))
                if denominator
                else 0.0
            )
            nearest_x = ex * projection
            nearest_y = ey * projection
            distance_to_axis = math.hypot(px - nearest_x, py - nearest_y)
            effective_radius = radius * (1.0 - 0.25 * projection)
            inside_crown = math.hypot(px, py) <= radius
            inside_shadow = (
                0 <= projection <= 1
                and distance_to_axis <= effective_radius
            )
            if inside_crown or inside_shadow:
                count += 1
                effective_opacity = opacity * (
                    1.0 if inside_crown else max(0.35, 1 - projection * 0.45)
                )
                unshaded *= 1 - min(0.92, effective_opacity)

        return 1 - unshaded, count

    def building_shadow_probability(
        self,
        lat: float,
        lon: float,
        altitude: float,
        azimuth: float,
    ) -> tuple[float, int]:
        if altitude <= 0:
            return 0.0, 0

        considered = 0
        for building in self.building_index.nearby(lat, lon, 180):
            coordinates = building.get("coordinates") or []
            if len(coordinates) < 3:
                continue
            height = float(building.get("height_m", 6.2))
            shadow_length = min(
                220.0,
                height / max(math.tan(math.radians(altitude)), 0.06),
            )
            shadow_bearing = (azimuth + 180) % 360
            origin_lat = lat
            origin_lon = lon
            polygon_xy = [
                local_xy(point[1], point[0], origin_lat, origin_lon)
                for point in coordinates
            ]
            projected_xy = []
            for point in coordinates:
                projected_lat, projected_lon = destination(
                    point[1], point[0], shadow_bearing, shadow_length
                )
                projected_xy.append(
                    local_xy(
                        projected_lat,
                        projected_lon,
                        origin_lat,
                        origin_lon,
                    )
                )
            considered += 1
            hull = convex_hull(polygon_xy + projected_xy)
            if point_in_polygon_xy((0.0, 0.0), hull):
                return 1.0, considered
        return 0.0, considered

    def terrain_shadow_probability(
        self,
        lat: float,
        lon: float,
        altitude: float,
        azimuth: float,
    ) -> float:
        if altitude <= 0 or not self.elevations:
            return 0.0
        current = self.nearest_elevation(lat, lon)
        if current is None:
            return 0.0

        # Terrain toward the sun can block low-angle light.
        for distance_m in (100, 250, 500):
            ahead_lat, ahead_lon = destination(lat, lon, azimuth, distance_m)
            ahead = self.nearest_elevation(ahead_lat, ahead_lon)
            if ahead is None:
                continue
            terrain_angle = math.degrees(
                math.atan2(ahead - current, distance_m)
            )
            if terrain_angle > altitude:
                return min(0.35, 0.12 + (terrain_angle - altitude) / 40)
        return 0.0

    def analyze_point(
        self,
        lat: float,
        lon: float,
        moment: datetime,
        use_time_of_day: bool,
        heat_index: float,
    ) -> ShadeResult:
        if use_time_of_day:
            altitude, azimuth = solar_position(lat, lon, moment)
        else:
            altitude, azimuth = 45.0, 180.0

        if altitude <= 0:
            return ShadeResult(
                shade_percent=100.0,
                canopy_shadow_percent=0.0,
                building_shadow_percent=0.0,
                terrain_shadow_percent=0.0,
                heat_penalty_percent=0.0,
                solar_altitude_degrees=round(altitude, 1),
                solar_azimuth_degrees=round(azimuth, 1),
                tree_count_considered=0,
                building_count_considered=0,
                reason="The sun is below the modeled horizon, so direct solar exposure is zero.",
                data_quality="mapped canopy/buildings; nighttime result",
            )

        canopy, tree_count = self.tree_shadow_probability(
            lat, lon, altitude, azimuth
        )
        building, building_count = self.building_shadow_probability(
            lat, lon, altitude, azimuth
        )
        # Terrain is intentionally excluded from the active EcoTrace route
        # shade model. The current model uses trees + buildings + sun + heat.
        terrain = 0.0

        physical_shadow = 1 - (
            (1 - canopy) * (1 - building)
        )
        heat_penalty = max(0.0, min(0.18, (heat_index - 60) / 220))
        score = max(0.0, min(1.0, physical_shadow - heat_penalty))

        dominant = max(
            (
                ("mapped tree-canopy shadow", canopy),
                ("mapped building shadow", building),
                ("terrain screening", terrain),
            ),
            key=lambda item: item[1],
        )
        reason = (
            f"Estimated shade {score * 100:.0f}/100. "
            f"Mapped tree canopy contributes {canopy * 100:.0f} points, "
            f"building geometry contributes {building * 100:.0f}, and terrain "
            f"contributes {terrain * 100:.0f}. The heat-risk adjustment subtracts "
            f"{heat_penalty * 100:.0f} points. The strongest factor is {dominant[0]}. "
            f"Sun altitude is {altitude:.1f}° and azimuth is {azimuth:.1f}°."
        )
        quality = (
            f"{tree_count} nearby mapped trees; "
            f"{building_count} nearby mapped buildings"
        )
        return ShadeResult(
            shade_percent=round(score * 100, 1),
            canopy_shadow_percent=round(canopy * 100, 1),
            building_shadow_percent=round(building * 100, 1),
            terrain_shadow_percent=round(terrain * 100, 1),
            heat_penalty_percent=round(heat_penalty * 100, 1),
            solar_altitude_degrees=round(altitude, 1),
            solar_azimuth_degrees=round(azimuth, 1),
            tree_count_considered=tree_count,
            building_count_considered=building_count,
            reason=reason,
            data_quality=quality,
        )
