"""
EcoTrace E-Waste backend.

Manual device selection only. No AI image recognition.
Provides:
- condition-based recommendations;
- approximate price ranges;
- action instructions;
- local exchange listings;
- e-waste collection events;
- nearby center search through OpenStreetMap/Nominatim.
"""

from __future__ import annotations

import os
import secrets
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

import certifi
import requests
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field


router = APIRouter(prefix="/api/ewaste", tags=["ewaste"])

DATABASE_PATH = Path(
    os.getenv("ECOTRACE_EWASTE_DB", "ecotrace_ewaste.db")
).expanduser()
_DB_LOCK = threading.RLock()

DeviceType = Literal[
    "temperature_exchange",
    "screens_monitors",
    "lamps",
    "large_equipment",
    "small_equipment",
    "small_it_telecom",
]

Condition = Literal[
    "working_good",
    "working_worn",
    "not_working",
    "unknown",
]

ActionType = Literal[
    "resell",
    "donate",
    "repair",
    "parts",
    "recycle",
]

ListingType = Literal[
    "sell",
    "give_away",
    "wanted",
]

EventStatus = Literal["open", "full", "completed", "cancelled"]


class RecommendationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    device_type: DeviceType
    condition: Condition
    age_years: float = Field(ge=0, le=100)
    powers_on: bool | None = None
    brand_model: str | None = Field(default=None, max_length=120)


class RecommendationOption(BaseModel):
    action: ActionType
    title: str
    why: str
    estimated_price_low: int | None = None
    estimated_price_high: int | None = None
    instructions: list[str]


class RecommendationResponse(BaseModel):
    device_type: DeviceType
    condition: Condition
    options: list[RecommendationOption]
    disclaimer: str


class ExchangeListingCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: str = Field(min_length=1, max_length=40)
    listing_type: ListingType
    device_type: DeviceType
    title: str = Field(min_length=3, max_length=120)
    description: str = Field(min_length=3, max_length=1500)
    asking_price: float | None = Field(default=None, ge=0, le=100000)
    image_url: str | None = Field(default=None, max_length=500)
    location_text: str | None = Field(default=None, max_length=180)
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)


class ExchangeListing(BaseModel):
    id: int
    display_name: str
    listing_type: ListingType
    device_type: DeviceType
    title: str
    description: str
    asking_price: float | None = None
    image_url: str | None = None
    location_text: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    created_at: str
    reply_count: int = 0


class ExchangeReplyCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: str = Field(min_length=1, max_length=40)
    message: str = Field(min_length=1, max_length=1000)
    reply_type: Literal[
        "question",
        "interested",
        "offer",
        "pickup",
        "seller_reply",
    ] = "question"


class ExchangeReply(BaseModel):
    id: int
    listing_id: int
    display_name: str
    message: str
    reply_type: str
    created_at: str


class EwasteEventCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    host_name: str = Field(min_length=1, max_length=40)
    title: str = Field(min_length=3, max_length=120)
    description: str = Field(min_length=3, max_length=1500)
    event_date: str = Field(min_length=4, max_length=40)
    duration_text: str = Field(default="Undecided", min_length=1, max_length=80)
    location_text: str = Field(min_length=2, max_length=180)
    max_participants: int = Field(default=50, ge=1, le=10000)
    image_url: str | None = Field(default=None, max_length=500)
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)


class EwasteEvent(BaseModel):
    id: int
    host_name: str
    title: str
    description: str
    event_date: str
    duration_text: str = "Undecided"
    location_text: str
    max_participants: int
    participant_count: int
    event_status: EventStatus
    image_url: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    created_at: str


class EwasteEventCreated(EwasteEvent):
    edit_token: str


class JoinRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    display_name: str = Field(min_length=1, max_length=40)


def _connect() -> sqlite3.Connection:
    connection = sqlite3.connect(
        DATABASE_PATH,
        check_same_thread=False,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    return connection


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def initialize_database() -> None:
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _DB_LOCK, _connect() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS ewaste_listings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                display_name TEXT NOT NULL,
                listing_type TEXT NOT NULL,
                device_type TEXT NOT NULL,
                title TEXT NOT NULL,
                description TEXT NOT NULL,
                asking_price REAL,
                image_url TEXT,
                location_text TEXT,
                latitude REAL,
                longitude REAL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS ewaste_listing_replies (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                listing_id INTEGER NOT NULL,
                display_name TEXT NOT NULL,
                message TEXT NOT NULL,
                reply_type TEXT NOT NULL DEFAULT 'question',
                created_at TEXT NOT NULL,
                FOREIGN KEY(listing_id)
                    REFERENCES ewaste_listings(id)
                    ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_ewaste_listing_replies
            ON ewaste_listing_replies(listing_id, created_at);

            CREATE TABLE IF NOT EXISTS ewaste_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                host_name TEXT NOT NULL,
                title TEXT NOT NULL,
                description TEXT NOT NULL,
                event_date TEXT NOT NULL,
                duration_text TEXT NOT NULL DEFAULT 'Undecided',
                location_text TEXT NOT NULL,
                max_participants INTEGER NOT NULL,
                event_status TEXT NOT NULL DEFAULT 'open',
                edit_token TEXT,
                image_url TEXT,
                latitude REAL,
                longitude REAL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS ewaste_event_participants (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id INTEGER NOT NULL,
                display_name TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(event_id, display_name),
                FOREIGN KEY(event_id)
                    REFERENCES ewaste_events(id)
                    ON DELETE CASCADE
            );
            """
        )

        event_columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(ewaste_events)")
        }
        if "duration_text" not in event_columns:
            connection.execute(
                "ALTER TABLE ewaste_events "
                "ADD COLUMN duration_text TEXT NOT NULL DEFAULT 'Undecided'"
            )
        if "edit_token" not in event_columns:
            connection.execute(
                "ALTER TABLE ewaste_events ADD COLUMN edit_token TEXT"
            )


initialize_database()


_PRICE_RANGES: dict[DeviceType, tuple[int, int]] = {
    "temperature_exchange": (0, 250),
    "screens_monitors": (0, 500),
    "lamps": (0, 20),
    "large_equipment": (0, 400),
    "small_equipment": (0, 150),
    "small_it_telecom": (5, 650),
}


_WIPE_INSTRUCTIONS: dict[DeviceType, list[str]] = {
    "temperature_exchange": [
        "Disconnect the appliance and remove food, water, and personal items.",
        "Do not puncture refrigerant lines or attempt to remove refrigerant yourself.",
        "Ask the recycler whether pickup, fees, and refrigerant handling are included.",
        "Remove connected-app accounts and saved Wi-Fi settings on smart appliances.",
    ],
    "screens_monitors": [
        "Back up important files on laptops or tablets.",
        "Sign out of cloud accounts and disable activation locks.",
        "Factory-reset portable devices and securely erase computer storage.",
        "Remove SIM cards, memory cards, discs, and external drives.",
        "Do not break screens; older displays may contain hazardous materials.",
    ],
    "lamps": [
        "Switch off power and allow the lamp to cool.",
        "Do not crush fluorescent or high-intensity-discharge lamps.",
        "Place tubes and bulbs in protective packaging for transport.",
        "Confirm whether the center accepts fluorescent, HID, and LED lamps separately.",
    ],
    "large_equipment": [
        "Disconnect power, water, and gas only according to manufacturer guidance.",
        "Remove personal items and clear smart-appliance accounts or Wi-Fi settings.",
        "Use professional help for heavy equipment and secure it during transport.",
        "Confirm pickup rules, fees, and appliance-size limits before visiting.",
    ],
    "small_equipment": [
        "Unplug the item and remove loose batteries when the facility permits.",
        "Clear saved settings or accounts on smart devices.",
        "Clean the item and describe missing parts or defects before reuse.",
        "Do not place lithium batteries in household trash.",
    ],
    "small_it_telecom": [
        "Back up files, contacts, photos, and authentication codes.",
        "Sign out of accounts and disable Find My or activation locks.",
        "Factory-reset the device and securely erase computer storage.",
        "Remove SIM cards, memory cards, and external storage.",
        "Confirm the device starts at its setup screen before transfer.",
    ],
}


def _condition_factor(condition: Condition) -> tuple[float, float]:
    return {
        "working_good": (0.60, 1.00),
        "working_worn": (0.30, 0.65),
        "not_working": (0.02, 0.20),
        "unknown": (0.10, 0.45),
    }[condition]


def _price_range(
    device_type: DeviceType,
    condition: Condition,
    age_years: float,
) -> tuple[int, int]:
    base_low, base_high = _PRICE_RANGES[device_type]
    low_factor, high_factor = _condition_factor(condition)
    age_factor = max(0.15, 1.0 - age_years * 0.09)
    low = round(base_low * low_factor * age_factor)
    high = round(base_high * high_factor * age_factor)
    return max(0, low), max(max(0, low), high)


def _instructions(
    device_type: DeviceType,
    action: ActionType,
) -> list[str]:
    wipe = _WIPE_INSTRUCTIONS[device_type]
    action_steps = {
        "resell": [
            "Photograph the device clearly and describe all defects.",
            "Compare local listings before setting a price.",
            "Meet in a safe public location and do not share account passwords.",
        ],
        "donate": [
            "Confirm the organization accepts this device type and condition.",
            "Include chargers only when safe and functional.",
            "Ask for a donation receipt if needed.",
        ],
        "repair": [
            "Request a written repair estimate before authorizing work.",
            "Compare repair cost with replacement and resale value.",
            "Do not attempt battery repair if the battery is swollen or damaged.",
        ],
        "parts": [
            "State clearly that the device is sold for parts or repair.",
            "Describe missing or failed components.",
            "Package batteries safely and do not ship damaged lithium batteries.",
        ],
        "recycle": [
            "Confirm the collection center accepts this exact device type.",
            "Remove loose batteries when the facility instructs you to do so.",
            "Do not place electronics or lithium batteries in household trash.",
        ],
    }[action]
    return wipe + action_steps


@router.post(
    "/recommend",
    response_model=RecommendationResponse,
)
async def recommend(
    payload: RecommendationRequest,
) -> RecommendationResponse:
    low, high = _price_range(
        payload.device_type,
        payload.condition,
        payload.age_years,
    )

    options: list[RecommendationOption] = []

    if payload.condition in {"working_good", "working_worn"}:
        options.append(
            RecommendationOption(
                action="resell",
                title="Resell or exchange locally",
                why=(
                    "The device still works, so reuse usually preserves more value "
                    "and avoids unnecessary recycling."
                ),
                estimated_price_low=low,
                estimated_price_high=high,
                instructions=_instructions(
                    payload.device_type,
                    "resell",
                ),
            )
        )
        options.append(
            RecommendationOption(
                action="donate",
                title="Donate for continued use",
                why=(
                    "A working device may still help a school, nonprofit, repair "
                    "program, or community member."
                ),
                instructions=_instructions(
                    payload.device_type,
                    "donate",
                ),
            )
        )

    if payload.condition in {"working_worn", "not_working", "unknown"}:
        options.append(
            RecommendationOption(
                action="repair",
                title="Repair before replacing",
                why=(
                    "A repair may extend the device's life when the battery, screen, "
                    "storage, or another replaceable component is the main problem."
                ),
                estimated_price_low=low,
                estimated_price_high=high,
                instructions=_instructions(
                    payload.device_type,
                    "repair",
                ),
            )
        )

    if payload.condition == "not_working":
        options.append(
            RecommendationOption(
                action="parts",
                title="Offer for parts or repair",
                why=(
                    "Nonworking electronics can still contain useful screens, boards, "
                    "cases, motors, cables, or other components."
                ),
                estimated_price_low=low,
                estimated_price_high=high,
                instructions=_instructions(
                    payload.device_type,
                    "parts",
                ),
            )
        )

    options.append(
        RecommendationOption(
            action="recycle",
            title="Use a verified e-waste collection center",
            why=(
                "Recycling is the final option when safe reuse, repair, donation, "
                "or parts recovery is not practical."
            ),
            instructions=_instructions(
                payload.device_type,
                "recycle",
            ),
        )
    )

    return RecommendationResponse(
        device_type=payload.device_type,
        condition=payload.condition,
        options=options,
        disclaimer=(
            "Prices are rough local estimates, not offers or guarantees. "
            "Always verify data-wiping steps with the device manufacturer and "
            "confirm that a collection center accepts the item."
        ),
    )


@router.get(
    "/listings",
    response_model=list[ExchangeListing],
)
async def list_exchange(
    limit: int = Query(default=50, ge=1, le=100),
) -> list[ExchangeListing]:
    with _DB_LOCK, _connect() as connection:
        rows = connection.execute(
            """
            SELECT
                l.*,
                COUNT(r.id) AS reply_count
            FROM ewaste_listings AS l
            LEFT JOIN ewaste_listing_replies AS r
                ON r.listing_id = l.id
            GROUP BY l.id
            ORDER BY l.created_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [ExchangeListing(**dict(row)) for row in rows]


@router.post(
    "/listings",
    response_model=ExchangeListing,
)
async def create_exchange(
    payload: ExchangeListingCreate,
) -> ExchangeListing:
    created_at = _utc_now()
    with _DB_LOCK, _connect() as connection:
        cursor = connection.execute(
            """
            INSERT INTO ewaste_listings (
                display_name,
                listing_type,
                device_type,
                title,
                description,
                asking_price,
                image_url,
                location_text,
                latitude,
                longitude,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                payload.display_name.strip(),
                payload.listing_type,
                payload.device_type,
                payload.title.strip(),
                payload.description.strip(),
                payload.asking_price,
                payload.image_url,
                payload.location_text,
                payload.latitude,
                payload.longitude,
                created_at,
            ),
        )
        listing_id = int(cursor.lastrowid)
    return ExchangeListing(
        id=listing_id,
        created_at=created_at,
        reply_count=0,
        **payload.model_dump(),
    )


@router.get(
    "/listings/{listing_id}/replies",
    response_model=list[ExchangeReply],
)
async def list_exchange_replies(
    listing_id: int,
) -> list[ExchangeReply]:
    with _DB_LOCK, _connect() as connection:
        listing = connection.execute(
            "SELECT id FROM ewaste_listings WHERE id = ?",
            (listing_id,),
        ).fetchone()
        if listing is None:
            raise HTTPException(
                status_code=404,
                detail="Exchange listing not found.",
            )

        rows = connection.execute(
            """
            SELECT *
            FROM ewaste_listing_replies
            WHERE listing_id = ?
            ORDER BY created_at ASC
            """,
            (listing_id,),
        ).fetchall()

    return [ExchangeReply(**dict(row)) for row in rows]


@router.post(
    "/listings/{listing_id}/replies",
    response_model=ExchangeReply,
)
async def create_exchange_reply(
    listing_id: int,
    payload: ExchangeReplyCreate,
) -> ExchangeReply:
    created_at = _utc_now()

    with _DB_LOCK, _connect() as connection:
        listing = connection.execute(
            "SELECT id FROM ewaste_listings WHERE id = ?",
            (listing_id,),
        ).fetchone()
        if listing is None:
            raise HTTPException(
                status_code=404,
                detail="Exchange listing not found.",
            )

        cursor = connection.execute(
            """
            INSERT INTO ewaste_listing_replies (
                listing_id,
                display_name,
                message,
                reply_type,
                created_at
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                listing_id,
                payload.display_name.strip(),
                payload.message.strip(),
                payload.reply_type,
                created_at,
            ),
        )
        reply_id = int(cursor.lastrowid)

    return ExchangeReply(
        id=reply_id,
        listing_id=listing_id,
        display_name=payload.display_name.strip(),
        message=payload.message.strip(),
        reply_type=payload.reply_type,
        created_at=created_at,
    )


@router.get(
    "/events",
    response_model=list[EwasteEvent],
)
async def list_events() -> list[EwasteEvent]:
    with _DB_LOCK, _connect() as connection:
        rows = connection.execute(
            """
            SELECT
                e.*,
                COUNT(p.id) AS participant_count
            FROM ewaste_events AS e
            LEFT JOIN ewaste_event_participants AS p
                ON p.event_id = e.id
            GROUP BY e.id
            ORDER BY e.event_date ASC
            """
        ).fetchall()
    return [EwasteEvent(**dict(row)) for row in rows]


@router.post(
    "/events",
    response_model=EwasteEventCreated,
)
async def create_event(
    payload: EwasteEventCreate,
) -> EwasteEventCreated:
    created_at = _utc_now()
    edit_token = secrets.token_urlsafe(24)
    with _DB_LOCK, _connect() as connection:
        cursor = connection.execute(
            """
            INSERT INTO ewaste_events (
                host_name,
                title,
                description,
                event_date,
                duration_text,
                location_text,
                max_participants,
                event_status,
                edit_token,
                image_url,
                latitude,
                longitude,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, ?, ?, ?)
            """,
            (
                payload.host_name.strip(),
                payload.title.strip(),
                payload.description.strip(),
                payload.event_date,
                payload.duration_text.strip() or "Undecided",
                payload.location_text.strip(),
                payload.max_participants,
                edit_token,
                payload.image_url,
                payload.latitude,
                payload.longitude,
                created_at,
            ),
        )
        event_id = int(cursor.lastrowid)
    return EwasteEventCreated(
        id=event_id,
        participant_count=0,
        event_status="open",
        edit_token=edit_token,
        created_at=created_at,
        **payload.model_dump(),
    )


@router.post("/events/{event_id}/join")
async def join_event(
    event_id: int,
    payload: JoinRequest,
) -> dict[str, Any]:
    with _DB_LOCK, _connect() as connection:
        event = connection.execute(
            """
            SELECT *
            FROM ewaste_events
            WHERE id = ?
            """,
            (event_id,),
        ).fetchone()
        if event is None:
            raise HTTPException(
                status_code=404,
                detail="E-waste event not found.",
            )
        count = int(
            connection.execute(
                """
                SELECT COUNT(*) AS count
                FROM ewaste_event_participants
                WHERE event_id = ?
                """,
                (event_id,),
            ).fetchone()["count"]
        )
        if count >= int(event["max_participants"]):
            raise HTTPException(
                status_code=409,
                detail="This event is full.",
            )
        connection.execute(
            """
            INSERT OR IGNORE INTO ewaste_event_participants (
                event_id,
                display_name,
                created_at
            )
            VALUES (?, ?, ?)
            """,
            (
                event_id,
                payload.display_name.strip(),
                _utc_now(),
            ),
        )
        count = int(
            connection.execute(
                """
                SELECT COUNT(*) AS count
                FROM ewaste_event_participants
                WHERE event_id = ?
                """,
                (event_id,),
            ).fetchone()["count"]
        )
    return {
        "event_id": event_id,
        "participant_count": count,
    }


@router.post("/events/{event_id}/complete")
async def complete_event(
    event_id: int,
    edit_token: str = Query(..., min_length=8, max_length=200),
) -> dict[str, Any]:
    with _DB_LOCK, _connect() as connection:
        event = connection.execute(
            "SELECT id, edit_token FROM ewaste_events WHERE id = ?",
            (event_id,),
        ).fetchone()
        if event is None:
            raise HTTPException(status_code=404, detail="E-waste event not found.")
        stored = event["edit_token"]
        if not stored or not secrets.compare_digest(str(stored), edit_token):
            raise HTTPException(
                status_code=403,
                detail="Only the event creator can mark this event done.",
            )
        connection.execute(
            "UPDATE ewaste_events SET event_status = 'completed' WHERE id = ?",
            (event_id,),
        )
    return {"event_id": event_id, "event_status": "completed"}


@router.get("/centers")
async def nearby_centers(
    lat: float = Query(..., ge=-90, le=90),
    lon: float = Query(..., ge=-180, le=180),
) -> dict[str, Any]:
    """
    Return a curated Santa Clara-area directory and rank it by distance.

    These entries are intentionally curated rather than discovered from a live
    map search. Users must still confirm current hours, fees, residency rules,
    and accepted materials before visiting.
    """
    import math

    centers = [
        {
            "id": "green-ewaste",
            "name": "Green E-Waste Recycling Center",
            "address": "1664 Watson Ct, Milpitas, CA 95035",
            "latitude": 37.4300234,
            "longitude": -121.9415337,
            "opening_hours": "Monday-Friday 8:30 AM-4:30 PM; closed weekends and major holidays",
            "phone": "(408) 287-3292",
            "website": "https://gerecycle.com/",
            "accepted_summary": "Electronics and major e-waste categories; contact the facility about item-specific fees and pickup.",
            "restrictions": "Verify fees and whether large appliances require advance arrangements.",
            "verified_source": "Official facility website",
        },
        {
            "id": "reducing-ewaste",
            "name": "Reducing E-Waste Recycling Center",
            "address": "3588 Cobbert Dr, San Jose, CA 95148",
            "latitude": 37.3653,
            "longitude": -121.8822,
            "opening_hours": "Reported as available for drop-off; call before visiting for current staffed hours",
            "phone": None,
            "website": None,
            "accepted_summary": "Electronics recycling; verify exact accepted categories directly.",
            "restrictions": "Hours and item acceptance should be confirmed by phone or current business listing.",
            "verified_source": "Local recycling-directory listing",
        },
        {
            "id": "aj-electronics",
            "name": "A&J Electronics Recycling E-Waste",
            "address": "763 Mabury Rd #30, San Jose, CA 95133",
            "latitude": 37.3659,
            "longitude": -121.8873,
            "opening_hours": "Weekday operation; check the official website or call for current hours",
            "phone": None,
            "website": "https://ajelectronicrecycling.com/",
            "accepted_summary": "Computers and electronic waste; confirm appliances, lamps, batteries, and fees.",
            "restrictions": "Some device types may require a fee or appointment.",
            "verified_source": "Official facility website",
        },
        {
            "id": "leghorn",
            "name": "Leghorn Electronic & Free TV Recycle Center",
            "address": "1948 Leghorn St, Mountain View, CA 94043",
            "latitude": 37.4173,
            "longitude": -122.0947,
            "opening_hours": "Check the official website for current drop-off hours",
            "phone": None,
            "website": "https://www.leghornrecycle.com/",
            "accepted_summary": "TVs, monitors, computers, cables, laptops, phones, and tablets are listed; other items vary.",
            "restrictions": "Not every household appliance is accepted; verify before transport.",
            "verified_source": "Official facility website",
        },
        {
            "id": "smart-station",
            "name": "SMaRT Station Recycling Center",
            "address": "301 Carl Rd, Sunnyvale, CA 94089",
            "latitude": 37.4207,
            "longitude": -122.0152,
            "opening_hours": "8:00 AM-5:00 PM daily; closed Thanksgiving, Christmas, and New Year's Day",
            "phone": "(408) 752-8530",
            "website": "https://www.sunnyvale.ca.gov/homes-streets-and-property/recycling-and-garbage/smart-station-recycling-center",
            "accepted_summary": "Recycling and disposal services; electronics eligibility and service rules vary.",
            "restrictions": "Some services have residency, quantity, appointment, or fee requirements.",
            "verified_source": "City of Sunnyvale",
        },
    ]

    def distance_m(a_lat: float, a_lon: float, b_lat: float, b_lon: float) -> float:
        radius = 6_371_000.0
        phi1 = math.radians(a_lat)
        phi2 = math.radians(b_lat)
        dphi = math.radians(b_lat - a_lat)
        dlambda = math.radians(b_lon - a_lon)
        value = (
            math.sin(dphi / 2) ** 2
            + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
        )
        return 2 * radius * math.atan2(
            math.sqrt(value),
            math.sqrt(max(0.0, 1 - value)),
        )

    ranked = []
    for center in centers:
        distance = distance_m(
            lat,
            lon,
            center["latitude"],
            center["longitude"],
        )
        ranked.append(
            {
                **center,
                "distance_meters": round(distance),
                "distance_miles": round(distance / 1609.344, 2),
                "description": center["accepted_summary"],
                "accepted_items": [],
                "verification_note": (
                    "Curated local directory. Confirm current hours, fees, "
                    "residency rules, and item acceptance before visiting."
                ),
                "display_name": center["address"],
                "source": center["verified_source"],
            }
        )

    ranked.sort(key=lambda center: center["distance_meters"])

    return {
        "selected_point": {"latitude": lat, "longitude": lon},
        "center_count": len(ranked),
        "closest_two": ranked[:2],
        "centers": ranked,
        "message": (
            f"Showing {len(ranked)} curated Santa Clara-area recycling locations, "
            "ranked from the selected point."
        ),
        "data_note": (
            "Danny's Recycling is not included because its published accepted "
            "materials do not include e-waste."
        ),
    }

