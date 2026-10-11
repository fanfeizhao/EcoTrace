"""
Gemini-assisted E-Waste photo identification for EcoTrace.

Uses the Gemini generateContent REST API directly, so no extra Python package
is required. The API key must be stored in GEMINI_API_KEY on the backend.

This endpoint does not make disposal decisions. It extracts a broad device
category and visible condition for user confirmation before EcoTrace's
rule-based recommendation engine runs.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
from typing import Literal

import certifi
import requests
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field


router = APIRouter(prefix="/api/gemini-ewaste", tags=["gemini-ewaste"])

DeviceCategory = Literal[
    "temperature_exchange",
    "screens_monitors",
    "lamps",
    "large_equipment",
    "small_equipment",
    "small_it_telecom",
]

VisibleCondition = Literal[
    "working_good",
    "working_worn",
    "not_working",
    "unknown",
]


class GeminiDevicePhotoRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    data_url: str = Field(min_length=20)
    user_description: str | None = Field(default=None, max_length=1200)


class GeminiDevicePhotoResult(BaseModel):
    is_ewaste: bool = True
    non_ewaste_reason: str | None = None
    category: DeviceCategory
    device_name: str
    possible_brand_model: str | None = None
    approximate_age_min_years: int | None = None
    approximate_age_max_years: int | None = None
    age_reasoning: list[str]
    visible_condition: VisibleCondition
    confidence: Literal["low", "medium", "high"]
    photo_quality: Literal["clear", "usable", "blurry_or_obstructed"]
    observations: list[str]
    confirmation_questions: list[str]
    safety_note: str
    model_used: str


_ALLOWED_MEDIA = {
    "image/jpeg",
    "image/png",
    "image/webp",
}


def _decode_data_url(data_url: str) -> tuple[str, str]:
    if not data_url.startswith("data:"):
        raise HTTPException(
            status_code=422,
            detail="The selected photo was not encoded correctly.",
        )

    try:
        header, encoded = data_url.split(",", 1)
    except ValueError as error:
        raise HTTPException(
            status_code=422,
            detail="Invalid photo data.",
        ) from error

    media_type = header[5:].split(";", 1)[0].lower()
    if media_type not in _ALLOWED_MEDIA:
        raise HTTPException(
            status_code=415,
            detail="Use a JPG, PNG, or WebP image.",
        )

    try:
        raw = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as error:
        raise HTTPException(
            status_code=422,
            detail="The photo could not be decoded.",
        ) from error

    if not raw:
        raise HTTPException(status_code=422, detail="The photo is empty.")
    if len(raw) > 8 * 1024 * 1024:
        raise HTTPException(
            status_code=413,
            detail="The Gemini device photo must be 8 MB or smaller.",
        )

    return media_type, encoded


@router.post("/analyze", response_model=GeminiDevicePhotoResult)
async def analyze_device_photo(
    payload: GeminiDevicePhotoRequest,
) -> GeminiDevicePhotoResult:
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise HTTPException(
            status_code=503,
            detail=(
                "GEMINI_API_KEY is not configured on the EcoTrace backend."
            ),
        )

    media_type, encoded = _decode_data_url(payload.data_url)
    configured_model = os.getenv("GEMINI_MODEL", "").strip()
    model_candidates = [
        configured_model,
        "gemini-3.1-flash-lite",
        "gemini-3-flash-preview",
    ]
    model_candidates = [
        model_name
        for index, model_name in enumerate(model_candidates)
        if model_name
        and model_name not in model_candidates[:index]
    ]

    category_descriptions = """
Use exactly one of these six category values:
- temperature_exchange: refrigerators, freezers, air conditioners, heat pumps
- screens_monitors: televisions, monitors, laptops, notebooks, tablets
- lamps: fluorescent tubes, HID lamps, LED lamps
- large_equipment: washers, dryers, dishwashers, electric stoves, large printers
- small_equipment: vacuums, microwaves, toasters, kettles, shavers, electronic toys
- small_it_telecom: phones, GPS devices, calculators, routers, personal computers
"""

    user_description = (
        payload.user_description.strip()
        if payload.user_description
        else "No written description was provided."
    )

    prompt = f"""
You are helping EcoTrace inspect an uploaded physical item for e-waste triage
and approximate age estimation. Inspect only what is visible and use the user's
description.

FIRST determine whether the photographed item is actually electronic/electrical
equipment or an e-waste item.

If the image is clearly NOT e-waste or electronic equipment — for example food,
clothing, furniture, paper, ordinary packaging, a book, a plant, a person,
non-electronic household objects, or an unrelated scene:
- set is_ewaste to false
- briefly explain why in non_ewaste_reason
- do NOT pretend it belongs to an e-waste category
- do NOT estimate device condition, brand/model, or device age
- use category "small_equipment" only as an internal schema placeholder
- set device_name to a plain visible object name
- set visible_condition to "unknown"
- set approximate age fields to 1 and 1 only as schema placeholders
- make age_reasoning empty
- keep confirmation_questions empty
- safety_note should simply say the item does not appear to be e-waste

If it IS plausibly e-waste/electronic equipment, set is_ewaste to true and
continue with the e-waste analysis below.

{category_descriptions}

Return conservative structured data:
- device_name: a plain broad name such as laptop, monitor, router, microwave,
  refrigerator, fluorescent lamp, or unknown electronic item.
- possible_brand_model: only provide this when visible evidence supports it;
  otherwise return an empty string.
- visible_condition:
  working_good only when the photo and text clearly indicate it works and looks
  intact; working_worn for visible wear/damage but apparently usable;
  not_working only when the user explicitly says it does not work or damage is
  clearly severe; otherwise unknown.
- approximate_age_min_years and approximate_age_max_years:
  ALWAYS provide a broad estimated age range in integer years relative to the
  current date, even when exact model/year evidence is unavailable. Infer from
  device category, visible design era, ports, screen proportions, keyboard or
  chassis style, labels, and the user's description. When evidence is weak,
  use a deliberately wide conservative range rather than 0 or null. Examples:
  a modern-looking phone might be 1-6 years; a generic flat-screen monitor
  2-12 years; a visibly older desktop appliance 5-20 years. Never return 0 for
  both fields merely because the exact age is unknown.
- age_reasoning: 1 to 4 short reasons for the estimate. If evidence is weak,
  explicitly say the estimate is broad and category/design based. Do not
  pretend the photo proves a manufacturing year.
- confidence: low, medium, or high.
- photo_quality: clear, usable, or blurry_or_obstructed.
- observations: short factual visible observations, no hidden assumptions.
- confirmation_questions: 1 to 4 short questions needed before resale, repair,
  donation, or recycling guidance.
- safety_note: mention any visible battery swelling, broken glass, leaking
  appliance, damaged cord, or other hazard; otherwise state that hidden hazards
  cannot be checked from a photo.

Do not claim an exact model, working status, data security, resale value, or
recycling eligibility unless the evidence supports it.

User description:
{user_description}
"""

    response_schema = {
        "type": "OBJECT",
        "properties": {
            "is_ewaste": {"type": "BOOLEAN"},
            "non_ewaste_reason": {"type": "STRING"},
            "category": {
                "type": "STRING",
                "enum": [
                    "temperature_exchange",
                    "screens_monitors",
                    "lamps",
                    "large_equipment",
                    "small_equipment",
                    "small_it_telecom",
                ],
            },
            "device_name": {"type": "STRING"},
            "possible_brand_model": {
                "type": "STRING",
                "description": (
                    "Possible brand/model when visibly supported; otherwise "
                    "return an empty string."
                ),
            },
            "approximate_age_min_years": {
                "type": "INTEGER",
                "description": (
                    "Required broad minimum estimated age in years; never use 0 just because the exact age is unknown."
                ),
            },
            "approximate_age_max_years": {
                "type": "INTEGER",
                "description": (
                    "Required broad maximum estimated age in years; never use 0 just because the exact age is unknown."
                ),
            },
            "age_reasoning": {
                "type": "ARRAY",
                "items": {"type": "STRING"},
            },
            "visible_condition": {
                "type": "STRING",
                "enum": [
                    "working_good",
                    "working_worn",
                    "not_working",
                    "unknown",
                ],
            },
            "confidence": {
                "type": "STRING",
                "enum": ["low", "medium", "high"],
            },
            "photo_quality": {
                "type": "STRING",
                "enum": ["clear", "usable", "blurry_or_obstructed"],
            },
            "observations": {
                "type": "ARRAY",
                "items": {"type": "STRING"},
            },
            "confirmation_questions": {
                "type": "ARRAY",
                "items": {"type": "STRING"},
            },
            "safety_note": {"type": "STRING"},
        },
        "required": [
            "is_ewaste",
            "non_ewaste_reason",
            "category",
            "device_name",
            "possible_brand_model",
            "approximate_age_min_years",
            "approximate_age_max_years",
            "age_reasoning",
            "visible_condition",
            "confidence",
            "photo_quality",
            "observations",
            "confirmation_questions",
            "safety_note",
        ],
    }

    body = {
        "contents": [
            {
                "role": "user",
                "parts": [
                    {"text": prompt},
                    {
                        "inline_data": {
                            "mime_type": media_type,
                            "data": encoded,
                        }
                    },
                ],
            }
        ],
        "generationConfig": {
            "temperature": 0.1,
            "responseMimeType": "application/json",
            "responseSchema": response_schema,
        },
    }

    response = None
    model = None
    last_message = "No Gemini model was attempted."

    for candidate_model in model_candidates:
        url = (
            "https://generativelanguage.googleapis.com/v1beta/models/"
            f"{candidate_model}:generateContent"
        )

        try:
            candidate_response = requests.post(
                url,
                params={"key": api_key},
                json=body,
                timeout=(10, 75),
                verify=certifi.where(),
            )
        except requests.RequestException as error:
            last_message = f"{candidate_model}: {error}"
            continue

        if candidate_response.ok:
            response = candidate_response
            model = candidate_model
            break

        try:
            error_payload = candidate_response.json()
            message = (
                error_payload.get("error", {}).get("message")
                or candidate_response.text[:300]
            )
        except ValueError:
            message = candidate_response.text[:300]

        last_message = f"{candidate_model}: {message}"

        # Continue only for model availability/deprecation/not-found errors.
        lowered = message.lower()
        availability_error = any(
            phrase in lowered
            for phrase in (
                "no longer available",
                "not found",
                "not supported",
                "not available to new users",
                "model is not available",
                "deprecated",
                "retired",
            )
        )
        if not availability_error:
            raise HTTPException(
                status_code=503,
                detail=f"Gemini request failed: {message}",
            )

    if response is None or model is None:
        raise HTTPException(
            status_code=503,
            detail=(
                "None of the configured Gemini Flash models were available. "
                f"Last response: {last_message}"
            ),
        )

    try:
        result_payload = response.json()
        raw_text = (
            result_payload["candidates"][0]["content"]["parts"][0]["text"]
        )
        parsed = json.loads(raw_text)
    except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise HTTPException(
            status_code=502,
            detail="Gemini returned an unreadable device result.",
        ) from error


    parsed["model_used"] = model

    def _fallback_age_range(device_name: str, category: str) -> tuple[int, int, str]:
        """
        Last-resort broad estimate used only when Gemini did not return a usable
        positive age range. This prevents the UI from displaying N/A while still
        clearly treating the value as an estimate.
        """
        name = (device_name or "").casefold()
        category = (category or "").casefold()

        rules = [
            (("phone", "smartphone", "tablet"), (1, 8),
             "Broad fallback based on the typical visible design lifespan of mobile electronics."),
            (("laptop", "notebook"), (2, 12),
             "Broad fallback based on typical laptop design generations."),
            (("monitor", "television", "tv", "display"), (2, 15),
             "Broad fallback based on flat-panel display design generations."),
            (("router", "modem", "gps", "calculator"), (2, 12),
             "Broad fallback based on common small IT/telecom hardware generations."),
            (("desktop", "computer", "pc"), (3, 15),
             "Broad fallback based on common personal-computer hardware generations."),
            (("refrigerator", "freezer", "air conditioner", "heat pump"), (3, 20),
             "Broad fallback based on typical household temperature-exchange equipment lifespans."),
            (("washer", "dryer", "dishwasher", "stove", "printer"), (3, 18),
             "Broad fallback based on typical large-equipment design and service lifespans."),
            (("microwave", "vacuum", "toaster", "kettle", "shaver", "electronic toy"), (2, 15),
             "Broad fallback based on typical small-appliance generations."),
            (("lamp", "fluorescent", "hid", "led"), (1, 12),
             "Broad fallback based on common lamp and fixture technology generations."),
        ]

        for words, age_range, reason in rules:
            if any(word in name for word in words):
                return age_range[0], age_range[1], reason

        category_defaults = {
            "temperature_exchange": (3, 20),
            "screens_monitors": (2, 15),
            "lamps": (1, 12),
            "large_equipment": (3, 18),
            "small_equipment": (2, 15),
            "small_it_telecom": (2, 12),
        }
        minimum, maximum = category_defaults.get(category, (2, 15))
        return (
            minimum,
            maximum,
            "Broad fallback based on the device category because the exact model year is not visible.",
        )
    if not str(parsed.get("possible_brand_model") or "").strip():
        parsed["possible_brand_model"] = None

    try:
        minimum_age = int(parsed.get("approximate_age_min_years") or 0)
        maximum_age = int(parsed.get("approximate_age_max_years") or 0)
    except (TypeError, ValueError):
        minimum_age = maximum_age = 0

    age_reasoning = [
        str(item)[:300]
        for item in (parsed.get("age_reasoning") or [])[:4]
    ]

    if parsed.get("is_ewaste") is False:
        parsed["possible_brand_model"] = None
        parsed["visible_condition"] = "unknown"
        parsed["approximate_age_min_years"] = 1
        parsed["approximate_age_max_years"] = 1
        parsed["age_reasoning"] = []
        parsed["confirmation_questions"] = []
        parsed["confidence"] = "high" if parsed.get("non_ewaste_reason") else "medium"
        minimum_age = maximum_age = 1
        age_reasoning = []
    elif minimum_age <= 0 or maximum_age <= 0:
        minimum_age, maximum_age, fallback_reason = _fallback_age_range(
            str(parsed.get("device_name") or ""),
            str(parsed.get("category") or ""),
        )
        age_reasoning = [fallback_reason, *age_reasoning][:4]
        # A fallback estimate should never be presented as high confidence.
        if str(parsed.get("confidence") or "").casefold() == "high":
            parsed["confidence"] = "low"

    parsed["approximate_age_min_years"] = min(minimum_age, maximum_age)
    parsed["approximate_age_max_years"] = max(minimum_age, maximum_age)
    parsed["age_reasoning"] = age_reasoning

    try:
        return GeminiDevicePhotoResult(**parsed)
    except Exception as error:
        raise HTTPException(
            status_code=502,
            detail=f"Gemini returned incomplete device fields: {error}",
        ) from error
