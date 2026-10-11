from pathlib import Path
from vta_official_bikeways_v2 import VTABikewayIndex

target = Path("vta_bikeways_2023_cache.json")
index = VTABikewayIndex(target)
print("Downloading the official VTA Santa Clara County Bikeways 2023 layer once...")
index.refresh()
print(f"Saved {len(index.features):,} line features/parts to {target.resolve()}")
print("Restart EcoTrace. Bike routes will now use the local VTA cache without waiting on ArcGIS.")
