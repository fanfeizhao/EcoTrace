"""
EcoTrace local image-upload backend.

Accepts JSON with a base64 data URL to avoid an external multipart dependency.
Stores validated JPG, PNG, or WebP files under uploads/community.
"""

from __future__ import annotations

import base64
import binascii
import os
import secrets
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field


router = APIRouter(prefix="/api/uploads", tags=["uploads"])

UPLOAD_ROOT = Path(
    os.getenv("ECOTRACE_UPLOAD_DIR", "uploads/community")
).expanduser()
UPLOAD_ROOT.mkdir(parents=True, exist_ok=True)

MAX_BYTES = 5 * 1024 * 1024

_ALLOWED = {
    "image/jpeg": (".jpg", b"\xff\xd8\xff"),
    "image/png": (".png", b"\x89PNG\r\n\x1a\n"),
    "image/webp": (".webp", b"RIFF"),
}


class ImageUploadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    data_url: str = Field(min_length=20)
    purpose: Literal["community", "ewaste"] = "community"


class ImageUploadResponse(BaseModel):
    image_url: str
    bytes_saved: int
    media_type: str


@router.post("/image", response_model=ImageUploadResponse)
async def upload_image(
    payload: ImageUploadRequest,
) -> ImageUploadResponse:
    if not payload.data_url.startswith("data:"):
        raise HTTPException(
            status_code=422,
            detail="The uploaded image was not encoded correctly.",
        )

    try:
        header, encoded = payload.data_url.split(",", 1)
    except ValueError as error:
        raise HTTPException(
            status_code=422,
            detail="Invalid image data.",
        ) from error

    media_type = header[5:].split(";", 1)[0].lower()
    if media_type not in _ALLOWED:
        raise HTTPException(
            status_code=415,
            detail="Only JPG, PNG, and WebP images are supported.",
        )

    try:
        raw = base64.b64decode(
            encoded,
            validate=True,
        )
    except (binascii.Error, ValueError) as error:
        raise HTTPException(
            status_code=422,
            detail="The image data could not be decoded.",
        ) from error

    if not raw:
        raise HTTPException(
            status_code=422,
            detail="The selected image is empty.",
        )
    if len(raw) > MAX_BYTES:
        raise HTTPException(
            status_code=413,
            detail="The image is larger than 5 MB.",
        )

    extension, signature = _ALLOWED[media_type]
    if not raw.startswith(signature):
        raise HTTPException(
            status_code=415,
            detail="The file contents do not match its image type.",
        )
    if media_type == "image/webp":
        if len(raw) < 12 or raw[8:12] != b"WEBP":
            raise HTTPException(
                status_code=415,
                detail="Invalid WebP image.",
            )

    purpose_dir = UPLOAD_ROOT / payload.purpose
    purpose_dir.mkdir(parents=True, exist_ok=True)

    filename = f"{secrets.token_hex(18)}{extension}"
    destination = purpose_dir / filename
    temporary = purpose_dir / f".{filename}.tmp"
    temporary.write_bytes(raw)
    temporary.replace(destination)

    return ImageUploadResponse(
        image_url=f"/uploads/{payload.purpose}/{filename}",
        bytes_saved=len(raw),
        media_type=media_type,
    )
