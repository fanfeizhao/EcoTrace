"""
Santa Clara approved-tree catalog used by EcoTrace.

The entries and explanatory notes below are based on the City list supplied
for the project. EcoTrace uses this module both for the resident tree guide and
for runtime species assignment to screened planting candidates.
"""

from __future__ import annotations

import hashlib
from typing import Any


TYPE_DEFINITIONS = {
    "D": (
        "Deciduous — seasonally sheds leaves, usually in autumn; may flower "
        "and fruit, and is generally bare during winter."
    ),
    "E": (
        "Evergreen — foliage remains green and functional through more than "
        "one growing season and is retained almost all year."
    ),
    "SE": (
        "Semi-evergreen — may shed most, but not all, foliage for a short "
        "portion of the year."
    ),
}

CITY_NOTES = [
    (
        "Trees not listed can be discussed with the City Arborist/Designate "
        "as a possible replacement."
    ),
    (
        "Tree selection requests are accommodated by the City of Santa Clara "
        "based on seasonal availability and City inventory."
    ),
    (
        "The City Arborist may replace a resident's selection with a similar "
        "or comparable tree."
    ),
]

CITY_CONTACT = {
    "department": (
        "City of Santa Clara, Department of Public Works, Street Division"
    ),
    "phone": "408-615-3080",
    "email": "Street@SantaClaraCA.gov",
}

# Direct Commons images are used for the three species that previously showed
# incorrect or monochrome results. Other trees use exact botanical-name search
# in the frontend and fall back to a tree icon if no trustworthy image appears.
DIRECT_IMAGES = {
    "fern-pine": (
        "https://commons.wikimedia.org/wiki/Special:FilePath/"
        "Podocarpus%20gracilior%201zz.jpg"
    ),
    "swan-hill-olive": (
        "https://commons.wikimedia.org/wiki/Special:FilePath/"
        "Olea%20europaea%20006.JPG"
    ),
    "eastern-redbud": (
        "https://commons.wikimedia.org/wiki/Special:FilePath/"
        "Cercis%20canadensis%20redbud%20tree%20bloom.jpg"
    ),
}


def _tree(
    *,
    id: str,
    common_name: str,
    botanical_name: str,
    size: str,
    height_ft: int,
    width_ft: int,
    type_code: str,
    flower: str,
    description: str,
    growth_rate: str = "Moderate",
    water_use: str = "Moderate",
    canopy: str = "Medium",
    planting_notes: str = "",
    image_url: str | None = None,
    approved_list: bool = True,
) -> dict[str, Any]:
    return {
        "id": id,
        "common_name": common_name,
        "botanical_name": botanical_name,
        "size": size,
        "height_ft": height_ft,
        "width_ft": width_ft,
        "type_code": type_code,
        "type_description": TYPE_DEFINITIONS[type_code],
        "flower": flower,
        "description": description,
        "growth_rate": growth_rate,
        "water_use": water_use,
        "canopy": canopy,
        "planting_notes": planting_notes,
        "image_url": image_url,
        "approved_list": approved_list,
    }


APPROVED_TREES: list[dict[str, Any]] = [
    _tree(
        id="chinese-fringe-tree",
        common_name="Chinese Fringe Tree",
        botanical_name="Chionanthus retusus",
        size="small",
        height_ft=20,
        width_ft=20,
        type_code="D",
        flower="White",
        description="Early flowering, fragrant, and well suited as a small tree.",
        growth_rate="Slow to moderate",
        canopy="Medium",
        planting_notes="Allow room for a rounded crown and seasonal leaf drop.",
    ),
    _tree(
        id="chinese-scholar-tree",
        common_name="Chinese Scholar Tree",
        botanical_name="Styphnolobium japonicum",
        size="small",
        height_ft=25,
        width_ft=25,
        type_code="D",
        flower="White",
        description="Summer flowers, yellow fall foliage, and an open airy canopy.",
        canopy="Light to medium",
        planting_notes="Use where a lighter canopy and seasonal interest are desired.",
    ),
    _tree(
        id="chinese-flame-tree",
        common_name="Chinese Flame Tree",
        botanical_name="Koelreuteria bipinnata",
        size="small",
        height_ft=25,
        width_ft=25,
        type_code="D",
        flower="Yellow",
        description=(
            "Develops colorful flower capsules in summer; a good small tree "
            "that tolerates poorer soils."
        ),
        water_use="Low to moderate",
        planting_notes="Allow for seasonal capsules and leaf litter.",
    ),
    _tree(
        id="chitalpa",
        common_name="Chitalpa",
        botanical_name="× Chitalpa tashkentensis",
        size="small",
        height_ft=25,
        width_ft=30,
        type_code="D",
        flower="Pink, white, or lavender",
        description="Open growth with showy flowers.",
        water_use="Low to moderate",
        canopy="Light",
        planting_notes="Its broad crown needs more lateral space than many small trees.",
    ),
    _tree(
        id="crape-myrtle",
        common_name="Crape Myrtle",
        botanical_name="Lagerstroemia indica hybrids",
        size="small",
        height_ft=25,
        width_ft=25,
        type_code="D",
        flower="Pink, white, or lavender",
        description=(
            "Vivid flowers in many colors; Indian-series cultivars are mildew "
            "resistant and it is commonly used as a street tree."
        ),
        water_use="Low to moderate",
        canopy="Light to medium",
        planting_notes="Avoid severe topping and choose mildew-resistant cultivars.",
    ),
    _tree(
        id="geijera",
        common_name="Geijera",
        botanical_name="Geijera parviflora",
        size="small",
        height_ft=25,
        width_ft=20,
        type_code="E",
        flower="White",
        description=(
            "Open canopy with upright branches and weeping leaves; a useful "
            "small tree."
        ),
        water_use="Low",
        canopy="Light",
        planting_notes="Works in warm, sunny sites with good drainage.",
    ),
    _tree(
        id="eastern-redbud",
        common_name="Eastern Redbud",
        botanical_name="Cercis canadensis",
        size="small",
        height_ft=25,
        width_ft=30,
        type_code="D",
        flower="Magenta",
        description=(
            "Winter-to-spring magenta flowers; Oklahoma selections tolerate "
            "poorer soils."
        ),
        canopy="Medium",
        planting_notes="Provide enough width for its broad rounded crown.",
        image_url=DIRECT_IMAGES["eastern-redbud"],
    ),
    _tree(
        id="western-redbud",
        common_name="Western Redbud",
        botanical_name="Cercis occidentalis",
        size="small",
        height_ft=18,
        width_ft=15,
        type_code="D",
        flower="Magenta",
        description=(
            "A California native redbud with vivid flowers and a compact form."
        ),
        water_use="Low",
        canopy="Light",
        planting_notes="A good fit for water-conscious California landscapes.",
    ),
    _tree(
        id="brisbane-box",
        common_name="Brisbane Box",
        botanical_name="Lophostemon confertus",
        size="medium",
        height_ft=30,
        width_ft=25,
        type_code="E",
        flower="White",
        description=(
            "Upright growth with reddish peeling bark that reveals smooth pale "
            "bark underneath."
        ),
        water_use="Low to moderate",
        canopy="Medium",
    ),
    _tree(
        id="chinese-hackberry",
        common_name="Chinese Hackberry",
        botanical_name="Celtis sinensis",
        size="medium",
        height_ft=35,
        width_ft=35,
        type_code="D",
        flower="n/a",
        description=(
            "Grows in poorer soils, develops an upright oval canopy, and is a "
            "good shade tree."
        ),
        canopy="Dense",
    ),
    _tree(
        id="chinese-pistache",
        common_name="Chinese Pistache",
        botanical_name="Pistacia chinensis",
        size="medium",
        height_ft=30,
        width_ft=30,
        type_code="D",
        flower="n/a",
        description=(
            "Rounded canopy with vivid fall color; male selections are seedless."
        ),
        water_use="Low",
        canopy="Medium to dense",
        planting_notes="Specify a male cultivar when seedlessness is required.",
    ),
    _tree(
        id="chinese-tallow-tree",
        common_name="Chinese Tallow Tree",
        botanical_name="Triadica sebifera",
        size="medium",
        height_ft=30,
        width_ft=30,
        type_code="D",
        flower="Yellow",
        description=(
            "Rounded shade canopy and vivid fall color where temperatures are cool."
        ),
        canopy="Dense",
        planting_notes=(
            "Discuss current availability and suitability with the City Arborist "
            "because regional planting guidance can change."
        ),
    ),
    _tree(
        id="european-hornbeam",
        common_name="European Hornbeam",
        botanical_name="Carpinus betulus",
        size="medium",
        height_ft=30,
        width_ft=30,
        type_code="D",
        flower="n/a",
        description=(
            "Upright vase-shaped canopy, tolerant of poorer soils, and useful "
            "for screening."
        ),
        canopy="Dense",
    ),
    _tree(
        id="jacaranda",
        common_name="Jacaranda",
        botanical_name="Jacaranda mimosifolia",
        size="medium",
        height_ft=35,
        width_ft=30,
        type_code="D",
        flower="Purple",
        description=(
            "Spreading canopy, vivid purple flowers before full leaf-out, and "
            "decorative seed pods."
        ),
        water_use="Low to moderate",
        canopy="Medium",
        planting_notes="Flowers and seed pods create seasonal litter.",
    ),
    _tree(
        id="swan-hill-olive",
        common_name="Olive — non-fruiting only (Swan Hill type)",
        botanical_name="Olea europaea fruitless cultivar",
        size="medium",
        height_ft=25,
        width_ft=25,
        type_code="E",
        flower="n/a",
        description=(
            "Upright, open form with light-green foliage; suited to larger lawn "
            "areas at maturity."
        ),
        water_use="Low",
        canopy="Light to medium",
        planting_notes="Use a verified non-fruiting cultivar only.",
        image_url=DIRECT_IMAGES["swan-hill-olive"],
    ),
    _tree(
        id="saratoga-laurel",
        common_name="Saratoga Laurel",
        botanical_name="Laurus nobilis 'Saratoga'",
        size="medium",
        height_ft=25,
        width_ft=25,
        type_code="E",
        flower="Yellow",
        description=(
            "Upright growth, glossy leaves, berries, and a dense rounded canopy "
            "for smaller lawns or streets."
        ),
        canopy="Dense",
    ),
    _tree(
        id="sour-gum-tupelo",
        common_name="Sour Gum / Tupelo",
        botanical_name="Nyssa sylvatica",
        size="medium",
        height_ft=30,
        width_ft=25,
        type_code="D",
        flower="n/a",
        description=(
            "Yellow-to-red fall color, tolerance of poorer soils, and a scenic "
            "lawn form; slow growing."
        ),
        growth_rate="Slow",
        canopy="Medium",
    ),
    _tree(
        id="autumn-purple-ash",
        common_name="Ash — Autumn Purple",
        botanical_name="Fraxinus americana 'Autumn Purple'",
        size="large",
        height_ft=45,
        width_ft=40,
        type_code="D",
        flower="n/a",
        description="Upright-spreading habit and a broad shade canopy.",
        canopy="Dense",
        planting_notes="Discuss current pest and disease suitability with the City Arborist.",
    ),
    _tree(
        id="camphor",
        common_name="Camphor",
        botanical_name="Cinnamomum camphora",
        size="large",
        height_ft=50,
        width_ft=50,
        type_code="E",
        flower="White",
        description=(
            "Evergreen shade tree with an open spreading canopy and strong "
            "resistance to many insects and diseases."
        ),
        canopy="Dense",
        planting_notes="Requires substantial root and crown space.",
    ),
    _tree(
        id="drake-dynasty-elm",
        common_name="Elm — Drake or Dynasty",
        botanical_name="Ulmus parvifolia cultivars",
        size="large",
        height_ft=40,
        width_ft=40,
        type_code="SE",
        flower="n/a",
        description=(
            "Semi-evergreen shade tree with mottled shedding bark and an open "
            "spreading canopy."
        ),
        canopy="Dense",
    ),
    _tree(
        id="ginkgo-male",
        common_name="Ginkgo — male only",
        botanical_name="Ginkgo biloba male cultivar",
        size="large",
        height_ft=40,
        width_ft=35,
        type_code="D",
        flower="n/a",
        description=(
            "Upright open canopy with golden-yellow fall foliage; a good shade tree."
        ),
        growth_rate="Slow",
        canopy="Medium",
        planting_notes="Specify a verified male cultivar to avoid fruit odor.",
    ),
    _tree(
        id="linden",
        common_name="Linden",
        botanical_name="Tilia species",
        size="large",
        height_ft=40,
        width_ft=30,
        type_code="D",
        flower="Pale yellow",
        description=(
            "Upright pyramidal form with dense foliage and strong lawn-shade value."
        ),
        canopy="Dense",
    ),
    _tree(
        id="black-scarlet-oak",
        common_name="Oak — Black or Scarlet",
        botanical_name="Quercus velutina or Quercus coccinea",
        size="large",
        height_ft=50,
        width_ft=50,
        type_code="D",
        flower="n/a",
        description=(
            "Deep-rooted, broad-canopy shade tree with some fall color in colder "
            "winters and seasonal acorn litter."
        ),
        canopy="Dense",
        planting_notes="Needs a very large planting area away from infrastructure.",
    ),
    _tree(
        id="red-maple",
        common_name="Red Maple",
        botanical_name="Acer rubrum",
        size="large",
        height_ft=40,
        width_ft=30,
        type_code="D",
        flower="n/a",
        description=(
            "Uniform vase form with vivid fall color; cultivars express different hues."
        ),
        canopy="Dense",
    ),
    _tree(
        id="columbia-sycamore",
        common_name="Sycamore — Columbia",
        botanical_name="Platanus × acerifolia 'Columbia'",
        size="large",
        height_ft=50,
        width_ft=45,
        type_code="D",
        flower="n/a",
        description=(
            "Upright-spreading open canopy; Columbia is selected for disease "
            "resistance and strong shade value."
        ),
        canopy="Dense",
        planting_notes="Requires generous root volume and clearance.",
    ),
    _tree(
        id="zelkova",
        common_name="Zelkova",
        botanical_name="Zelkova serrata",
        size="large",
        height_ft=50,
        width_ft=45,
        type_code="D",
        flower="n/a",
        description=(
            "Yellow-to-red fall color, tolerance of poorer soil, and a scenic "
            "lawn form; slow growing."
        ),
        growth_rate="Slow",
        canopy="Dense",
    ),
    # Retained from the earlier utility-compatible guide. It is clearly labeled
    # supplemental rather than part of the supplied street-tree list.
    _tree(
        id="fern-pine",
        common_name="Fern Pine",
        botanical_name="Afrocarpus gracilior",
        size="medium",
        height_ft=30,
        width_ft=15,
        type_code="E",
        flower="n/a",
        description=(
            "Fine-textured evergreen foliage with a tidy upright form."
        ),
        water_use="Moderate",
        canopy="Medium",
        planting_notes=(
            "Supplemental prior-guide species; confirm approval and mature size "
            "with the City Arborist before selection."
        ),
        image_url=DIRECT_IMAGES["fern-pine"],
        approved_list=False,
    ),
]


APPROVED_ONLY = [tree for tree in APPROVED_TREES if tree["approved_list"]]
BY_ID = {tree["id"]: tree for tree in APPROVED_TREES}


def choose_tree_for_site(site: dict[str, Any]) -> dict[str, Any]:
    """
    Deterministically assign a City-list tree to a screened candidate.

    This is not legal approval. The selection uses mapped context only:
    compact streetscape sites receive small trees; wider open-space edges can
    receive medium or, conservatively, large trees.
    """
    site_id = str(site.get("site_id") or "")
    road_class = str(site.get("road_class") or "").lower()
    open_space = str(site.get("mapped_open_space") or "").lower()
    surface = str(site.get("surface_context") or "").lower()

    try:
        offset = float(site.get("centerline_offset_meters") or 0.0)
    except (TypeError, ValueError):
        offset = 0.0
    try:
        open_depth = float(site.get("open_space_interior_depth_meters") or 0.0)
    except (TypeError, ValueError):
        open_depth = 0.0
    try:
        tree_gap = float(site.get("nearest_mapped_tree_meters") or 0.0)
    except (TypeError, ValueError):
        tree_gap = 0.0

    size = "small"
    if (
        any(token in open_space for token in ("park", "grass", "green", "meadow"))
        and max(open_depth, offset) >= 6.0
    ):
        size = "medium"
    if (
        any(token in open_space for token in ("park", "meadow", "recreation"))
        and max(open_depth, offset) >= 9.0
        and tree_gap >= 18.0
        and road_class not in {"motorway", "trunk", "primary"}
    ):
        size = "large"

    # Narrow curb/sidewalk contexts stay small even if other fields are generous.
    if any(token in surface for token in ("parkway", "curb", "sidewalk")):
        if max(open_depth, offset) < 7.0:
            size = "small"

    pool = [tree for tree in APPROVED_ONLY if tree["size"] == size]
    digest = hashlib.sha256(site_id.encode("utf-8")).digest()
    index = int.from_bytes(digest[:4], "big") % len(pool)
    return pool[index]
