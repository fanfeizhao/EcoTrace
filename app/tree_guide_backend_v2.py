"""
EcoTrace resident tree guide using the complete Santa Clara list supplied by
the project owner.
"""

from __future__ import annotations

from fastapi import APIRouter, Query
from pydantic import BaseModel

from approved_tree_catalog import (
    APPROVED_TREES,
    CITY_CONTACT,
    CITY_NOTES,
    TYPE_DEFINITIONS,
)


router = APIRouter(prefix="/api/tree-guide", tags=["tree-guide"])


class TreeGuideItem(BaseModel):
    id: str
    common_name: str
    botanical_name: str
    size: str
    height_ft: int
    width_ft: int
    type_code: str
    type_description: str
    flower: str
    description: str
    growth_rate: str
    water_use: str
    canopy: str
    planting_notes: str
    image_url: str | None = None
    approved_list: bool


@router.get("/trees", response_model=list[TreeGuideItem])
async def list_trees(
    size: str | None = Query(default=None),
    max_height_ft: int | None = Query(default=None, ge=5, le=100),
    max_width_ft: int | None = Query(default=None, ge=5, le=100),
    type_code: str | None = Query(default=None),
    flower: str | None = Query(default=None),
    approved_only: bool = Query(default=False),
) -> list[TreeGuideItem]:
    results = APPROVED_TREES

    if approved_only:
        results = [tree for tree in results if tree["approved_list"]]
    if size:
        results = [tree for tree in results if tree["size"] == size]
    if max_height_ft is not None:
        results = [
            tree for tree in results
            if tree["height_ft"] <= max_height_ft
        ]
    if max_width_ft is not None:
        results = [
            tree for tree in results
            if tree["width_ft"] <= max_width_ft
        ]
    if type_code:
        results = [
            tree for tree in results
            if tree["type_code"].casefold() == type_code.casefold()
        ]
    if flower:
        results = [
            tree for tree in results
            if flower.casefold() in tree["flower"].casefold()
        ]

    return [TreeGuideItem(**tree) for tree in results]


@router.get("/notice")
async def guide_notice() -> dict[str, object]:
    return {
        "title": "Santa Clara tree-selection information",
        "notes": CITY_NOTES,
        "type_definitions": TYPE_DEFINITIONS,
        "na_definition": "n/a — not applicable",
        "contact": CITY_CONTACT,
        "message": (
            "This guide is educational. City-maintained trees, public rights-of-way, "
            "park strips, easements, utilities, and final species availability still "
            "require City review and field verification."
        ),
    }
