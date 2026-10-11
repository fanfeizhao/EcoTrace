from __future__ import annotations

import math
import os
from functools import lru_cache
from typing import Any

import certifi
import requests
from fastapi import APIRouter, HTTPException, Query

router = APIRouter(prefix="/api/community-context", tags=["community-context"])

ACS_YEAR = "2024"
ACS_URL = f"https://api.census.gov/data/{ACS_YEAR}/acs/acs5"
CENSUS_GEO_URL = "https://geocoding.geo.census.gov/geocoder/geographies/coordinates"

AGE65_VARS = [
    "B01001_020E","B01001_021E","B01001_022E","B01001_023E","B01001_024E","B01001_025E",
    "B01001_044E","B01001_045E","B01001_046E","B01001_047E","B01001_048E","B01001_049E",
]
VARIABLES = [
    "NAME",
    "B19013_001E",  # median household income
    "B17001_001E",  # poverty universe
    "B17001_002E",  # below poverty
    "B08201_001E",  # households
    "B08201_002E",  # no vehicle available
    "B01001_001E",  # population
    *AGE65_VARS,
]


def _number(value: Any) -> float | None:
    try:
        n = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(n) or n < 0:
        return None
    return n


def _percentile(values: list[float], target: float, inverse: bool = False) -> float:
    vals = sorted(v for v in values if math.isfinite(v))
    if not vals:
        return 50.0
    below = sum(v <= target for v in vals)
    pct = below / len(vals) * 100.0
    return 100.0 - pct if inverse else pct


def _level(p: float) -> str:
    if p < 33.34:
        return "Lower"
    if p < 66.67:
        return "Moderate"
    return "Higher"


def _tract_for_point(lat: float, lon: float) -> tuple[str, str, str]:
    r = requests.get(
        CENSUS_GEO_URL,
        params={
            "x": f"{lon:.7f}",
            "y": f"{lat:.7f}",
            "benchmark": "Public_AR_Current",
            "vintage": "Current_Current",
            "format": "json",
        },
        timeout=(5, 15),
        verify=certifi.where(),
        headers={"User-Agent": "EcoTrace-CA17/6.12"},
    )
    r.raise_for_status()
    geogs = r.json().get("result", {}).get("geographies", {})
    tracts = geogs.get("Census Tracts") or geogs.get("2020 Census Tracts") or []
    if not tracts:
        raise RuntimeError("Could not resolve a Census tract for this point.")

    t = tracts[0]
    state = str(t.get("STATE") or t.get("STATEFP") or "")
    county = str(t.get("COUNTY") or t.get("COUNTYFP") or "")
    tract = str(t.get("TRACT") or t.get("TRACTCE") or "")
    if not (state and county and tract):
        geoid = str(t.get("GEOID") or "")
        if len(geoid) >= 11:
            state, county, tract = geoid[:2], geoid[2:5], geoid[5:11]
    if not (state and county and tract):
        raise RuntimeError("Census tract identifiers were incomplete.")
    return state, county, tract


@lru_cache(maxsize=16)
def _county_rows(state: str, county: str) -> list[dict[str, Any]]:
    key = os.getenv("CENSUS_API_KEY", "").strip()
    if not key:
        raise RuntimeError("CENSUS_API_KEY is not set.")

    r = requests.get(
        ACS_URL,
        params={
            "get": ",".join(VARIABLES),
            "for": "tract:*",
            "in": f"state:{state} county:{county}",
            "key": key,
        },
        timeout=(8, 25),
        verify=certifi.where(),
        headers={"User-Agent": "EcoTrace-CA17/6.12"},
    )
    r.raise_for_status()
    rows = r.json()
    if not isinstance(rows, list) or len(rows) < 2:
        raise RuntimeError("Census ACS returned no tract rows.")

    headers = rows[0]
    output = []
    for values in rows[1:]:
        row = dict(zip(headers, values))
        pop = _number(row.get("B01001_001E"))
        pov_total = _number(row.get("B17001_001E"))
        pov_below = _number(row.get("B17001_002E"))
        households = _number(row.get("B08201_001E"))
        no_vehicle = _number(row.get("B08201_002E"))
        income = _number(row.get("B19013_001E"))
        age65 = sum(_number(row.get(v)) or 0 for v in AGE65_VARS)

        output.append({
            **row,
            "median_income": income,
            "poverty_percent": (pov_below / pov_total * 100) if pov_total and pov_below is not None else None,
            "no_vehicle_percent": (no_vehicle / households * 100) if households and no_vehicle is not None else None,
            "age65_percent": (age65 / pop * 100) if pop else None,
        })
    return output


@router.get("/point")
async def point_context(
    lat: float = Query(..., ge=-90, le=90),
    lon: float = Query(..., ge=-180, le=180),
) -> dict[str, Any]:
    try:
        state, county, tract = _tract_for_point(lat, lon)
        rows = _county_rows(state, county)
    except (requests.RequestException, RuntimeError, ValueError) as e:
        raise HTTPException(status_code=503, detail=f"Community context unavailable: {e}") from e

    selected = next((r for r in rows if str(r.get("tract")) == tract), None)
    if selected is None:
        raise HTTPException(status_code=404, detail="Census tract record not found.")

    income_vals = [r["median_income"] for r in rows if r["median_income"] is not None]
    poverty_vals = [r["poverty_percent"] for r in rows if r["poverty_percent"] is not None]
    vehicle_vals = [r["no_vehicle_percent"] for r in rows if r["no_vehicle_percent"] is not None]
    age_vals = [r["age65_percent"] for r in rows if r["age65_percent"] is not None]

    parts = []
    if selected["median_income"] is not None:
        parts.append(_percentile(income_vals, selected["median_income"], inverse=True))
    if selected["poverty_percent"] is not None:
        parts.append(_percentile(poverty_vals, selected["poverty_percent"]))
    if selected["no_vehicle_percent"] is not None:
        parts.append(_percentile(vehicle_vals, selected["no_vehicle_percent"]))
    if selected["age65_percent"] is not None:
        parts.append(_percentile(age_vals, selected["age65_percent"]))

    context_pct = sum(parts) / len(parts) if parts else 50.0

    return {
        "source": f"U.S. Census Bureau ACS {ACS_YEAR} 5-year estimates",
        "geography": selected.get("NAME"),
        "tract": tract,
        "median_household_income": round(selected["median_income"]) if selected["median_income"] is not None else None,
        "poverty_percent": round(selected["poverty_percent"], 1) if selected["poverty_percent"] is not None else None,
        "households_without_vehicle_percent": round(selected["no_vehicle_percent"], 1) if selected["no_vehicle_percent"] is not None else None,
        "age_65_plus_percent": round(selected["age65_percent"], 1) if selected["age65_percent"] is not None else None,
        "community_context_percentile": round(context_pct, 1),
        "community_context_level": _level(context_pct),
        "interpretation": (
            "Neutral planning context from transparent Census indicators. "
            "This is not an equity score, eligibility decision, or planting-priority ranking."
        ),
    }
