"""
EcoTrace bike-infrastructure proximity analyzer.
"""

from __future__ import annotations

import json
import math
from collections import defaultdict
from pathlib import Path
from typing import Any


class BikeInfrastructureIndex:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.available = False
        self.metadata: dict[str, Any] = {}
        self.cell_deg = 0.003
        self.buckets: dict[tuple[int, int], list[dict[str, Any]]] = defaultdict(list)
        self.ways: list[dict[str, Any]] = []
        self._load()

    def _key(self, latitude: float, longitude: float) -> tuple[int, int]:
        return (
            math.floor(latitude / self.cell_deg),
            math.floor(longitude / self.cell_deg),
        )

    def _load(self) -> None:
        if not self.path.exists():
            return
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        self.metadata = payload.get("metadata") or {}
        self.ways = [
            way for way in (payload.get("ways") or [])
            if isinstance(way, dict)
        ]
        for way in self.ways:
            coordinates = way.get("coordinates") or []
            if len(coordinates) < 2:
                continue
            min_lon = min(point[0] for point in coordinates)
            max_lon = max(point[0] for point in coordinates)
            min_lat = min(point[1] for point in coordinates)
            max_lat = max(point[1] for point in coordinates)
            min_key = self._key(min_lat, min_lon)
            max_key = self._key(max_lat, max_lon)
            for x in range(min_key[0] - 1, max_key[0] + 2):
                for y in range(min_key[1] - 1, max_key[1] + 2):
                    self.buckets[(x, y)].append(way)
        self.available = bool(self.buckets)

    @staticmethod
    def _segment_distance_m(
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
        abx, aby = bx - ax, by - ay
        denominator = abx * abx + aby * aby
        if denominator == 0:
            return math.hypot(px - ax, py - ay)
        t = max(
            0.0,
            min(1.0, ((px - ax) * abx + (py - ay) * aby) / denominator),
        )
        return math.hypot(px - (ax + t * abx), py - (ay + t * aby))

    def classify_point(
        self,
        latitude: float,
        longitude: float,
        tolerance_m: float = 14.0,
    ) -> tuple[str, str]:
        if not self.available:
            return "unknown", "Bike-infrastructure model unavailable"

        candidates = self.buckets.get(self._key(latitude, longitude), ())
        best_way: dict[str, Any] | None = None
        best_distance = float("inf")

        for way in candidates:
            coordinates = way.get("coordinates") or []
            for a, b in zip(coordinates, coordinates[1:]):
                distance = self._segment_distance_m(
                    latitude,
                    longitude,
                    a,
                    b,
                )
                if distance < best_distance:
                    best_distance = distance
                    best_way = way

        if best_way is None or best_distance > tolerance_m:
            return "unknown", "No nearby mapped bike facility"

        return (
            str(best_way.get("category") or "unknown"),
            str(best_way.get("explanation") or "Mapped bicycle route"),
        )

    def nearest_routable_point(
        self,
        latitude: float,
        longitude: float,
        search_radius_m: float = 700.0,
    ) -> dict[str, Any] | None:
        """Project a coordinate onto the nearest cached mapped road segment."""
        if not self.available:
            return None

        center_key = self._key(latitude, longitude)
        reach = max(
            1,
            math.ceil(search_radius_m / (self.cell_deg * 111_320.0)) + 1,
        )
        mean_lat = math.radians(latitude)
        sx = 111_320.0 * max(math.cos(mean_lat), 0.2)
        sy = 111_320.0
        px, py = longitude * sx, latitude * sy

        seen_ids: set[int] = set()
        best: dict[str, Any] | None = None
        best_distance = float("inf")

        for dx in range(-reach, reach + 1):
            for dy in range(-reach, reach + 1):
                for way in self.buckets.get(
                    (center_key[0] + dx, center_key[1] + dy),
                    (),
                ):
                    osm_id = int(way.get("osm_id", 0) or 0)
                    if osm_id in seen_ids:
                        continue
                    seen_ids.add(osm_id)

                    coordinates = way.get("coordinates") or []
                    for a, b in zip(coordinates, coordinates[1:]):
                        ax, ay = a[0] * sx, a[1] * sy
                        bx, by = b[0] * sx, b[1] * sy
                        abx, aby = bx - ax, by - ay
                        denom = abx * abx + aby * aby
                        t = 0.0 if denom == 0 else max(
                            0.0,
                            min(1.0, ((px - ax) * abx + (py - ay) * aby) / denom),
                        )
                        nx = ax + t * abx
                        ny = ay + t * aby
                        distance = math.hypot(px - nx, py - ny)
                        if distance < best_distance:
                            best_distance = distance
                            best = {
                                "latitude": ny / sy,
                                "longitude": nx / sx,
                                "distance_meters": round(distance, 1),
                                "name": way.get("name"),
                                "category": way.get("category"),
                                "highway": way.get("highway"),
                                "osm_id": osm_id,
                            }

        if best is None or best_distance > search_radius_m:
            return None
        return best


    def nearest_named_way(
        self,
        latitude: float,
        longitude: float,
        search_radius_m: float = 500.0,
    ) -> dict[str, Any] | None:
        """
        Return the closest named mapped road/cycleway.

        This provides a local reverse-geocoding fallback when Nominatim is
        rate-limited or temporarily unavailable.
        """
        if not self.available:
            return None

        center_key = self._key(latitude, longitude)
        reach = max(
            1,
            math.ceil(
                search_radius_m
                / (
                    self.cell_deg
                    * 111_320.0
                )
            )
            + 1,
        )

        seen_ids: set[int] = set()
        best: dict[str, Any] | None = None
        best_distance = float("inf")

        for dx in range(-reach, reach + 1):
            for dy in range(-reach, reach + 1):
                for way in self.buckets.get(
                    (center_key[0] + dx, center_key[1] + dy),
                    (),
                ):
                    osm_id = int(way.get("osm_id", 0) or 0)
                    if osm_id in seen_ids:
                        continue
                    seen_ids.add(osm_id)

                    name = str(way.get("name") or "").strip()
                    if not name:
                        continue

                    coordinates = way.get("coordinates") or []
                    for a, b in zip(coordinates, coordinates[1:]):
                        distance = self._segment_distance_m(
                            latitude,
                            longitude,
                            a,
                            b,
                        )
                        if distance < best_distance:
                            best_distance = distance
                            best = {
                                "name": name,
                                "distance_meters": round(distance, 1),
                                "category": way.get("category"),
                                "highway": way.get("highway"),
                                "osm_id": osm_id,
                            }

        if best is None or best_distance > search_radius_m:
            return None
        return best


    def analyze_route(self, coordinates: list[list[float]]) -> dict[str, Any]:
        categories = {
            "protected": 0.0,
            "painted": 0.0,
            "shared_low_speed": 0.0,
            "shared_road": 0.0,
            "unknown": 0.0,
        }
        total = 0.0

        for a, b in zip(coordinates, coordinates[1:]):
            lat = (a[1] + b[1]) / 2.0
            lon = (a[0] + b[0]) / 2.0
            distance = self._segment_distance_m(lat, lon, a, b)
            category, _ = self.classify_point(lat, lon)
            categories[category] = categories.get(category, 0.0) + distance
            total += distance

        if total <= 0:
            total = 1.0

        percentages = {
            key: round(value / total * 100.0, 1)
            for key, value in categories.items()
        }
        dedicated = percentages["protected"] + percentages["painted"]
        warning = (
            "Most of this route uses mapped dedicated bike infrastructure."
            if dedicated >= 60
            else "This route contains substantial shared-road or unmapped cycling sections."
        )

        return {
            "protected_percent": percentages["protected"],
            "painted_lane_percent": percentages["painted"],
            "shared_low_speed_percent": percentages["shared_low_speed"],
            "shared_road_percent": percentages["shared_road"],
            "unknown_percent": percentages["unknown"],
            "dedicated_infrastructure_percent": round(dedicated, 1),
            "warning": warning,
            "data_available": self.available,
        }
