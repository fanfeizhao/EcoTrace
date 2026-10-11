#!/usr/bin/env python3
from pathlib import Path
import math
import numpy as np
import rasterio

path = Path("wri_tree_canopy_height.tif")
if not path.exists():
    raise SystemExit(f"Missing: {path.resolve()}")

with rasterio.open(path) as src:
    print("File:", path.resolve())
    print("Size:", f"{path.stat().st_size/1024/1024:.1f} MB")
    print("Raster:", src.width, "x", src.height, "| bands:", src.count)
    print("CRS:", src.crs)
    print("Bounds:", src.bounds)
    print("Resolution:", src.res)
    print("NoData:", src.nodata)

    # Read a decimated sample instead of the entire raster.
    out_h = min(1200, src.height)
    out_w = min(1200, src.width)
    arr = src.read(1, out_shape=(out_h, out_w), masked=True).astype("float64")
    values = arr.compressed()
    values = values[np.isfinite(values)]

    if values.size == 0:
        raise SystemExit("FAIL: raster contains no valid numeric pixels.")

    print("Valid sampled pixels:", f"{values.size:,}")
    print("Min:", round(float(np.min(values)), 3))
    print("Median:", round(float(np.median(values)), 3))
    print("P90:", round(float(np.percentile(values, 90)), 3))
    print("P99:", round(float(np.percentile(values, 99)), 3))
    print("Max:", round(float(np.max(values)), 3))
    print("Pixels > 2 m:", f"{100*np.mean(values>2):.1f}%")
    print("Pixels > 10 m:", f"{100*np.mean(values>10):.1f}%")

    plausible = (
        np.max(values) > 2
        and np.percentile(values, 99) < 100
        and np.mean(values >= 0) > 0.99
    )

    # Check approximate overlap with the EcoTrace study rectangle.
    # If source is WGS84, do this directly. Otherwise report only.
    overlaps = None
    if src.crs and src.crs.to_epsg() == 4326:
        b = src.bounds
        overlaps = not (
            b.right < -122.12 or b.left > -121.82 or
            b.top < 37.27 or b.bottom > 37.47
        )

    if overlaps is not None:
        print("Overlaps EcoTrace area:", overlaps)

    print("Plausibility:", "PASS" if plausible else "CHECK MANUALLY")
