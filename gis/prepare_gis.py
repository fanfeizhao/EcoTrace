"""Validate and losslessly tile four local TIFFs without altering the sources."""
import argparse
import hashlib
import json
import math
import os
import shutil
import time
from pathlib import Path
import numpy as np
import rasterio
from rasterio.windows import Window
from rasterio.warp import transform_bounds
from pyproj import Transformer

FILES = [
    ('San Jose canopy 2022', 'CA17_San_Jose_canopy2022_fixed.tif', 'cover', None),
    ('SF Oakland canopy 2022', 'CA_SF_Oakalnd_canopy2022_fixed.tif', 'cover', None),
    ('Santa Clara canopy height 2020', 'CA17_canopy_height.tif', 'height', 'unknown'),
    ('Alameda canopy height, mixed survey years', 'CA17_Alameda_canopy_height.tif', 'height', 'feet'),
]


def fingerprint(path):
    s = path.stat()
    return dict(path=str(path.resolve()), bytes=s.st_size, modified_ns=s.st_mtime_ns)


def inspect(path, kind):
    with rasterio.open(path) as ds:
        if ds.count != 1 or min(ds.width, ds.height) < 100 or ds.crs is None:
            raise ValueError(f'{path.name}: missing CRS, not single-band, or too few pixels. Check the QGIS export.')
        bounds = transform_bounds(ds.crs, 'EPSG:4326', *ds.bounds, densify_pts=21)
        if not (bounds[0] < -121.78 and bounds[2] > -122.12 and bounds[1] < 37.56 and bounds[3] > 37.28):
            raise ValueError(f'{path.name}: bounds do not overlap the expected CA-17 area: {bounds}')
        if ds.transform.b or ds.transform.d or ds.transform.a <= 0 or ds.transform.e >= 0:
            raise ValueError(f'{path.name}: unexpected rotated/flipped raster grid.')
        if kind == 'cover' and (ds.dtypes[0] != 'uint8' or ds.scales[0] != 1 or ds.offsets[0] != 0):
            raise ValueError(f'{path.name}: expected unscaled byte canopy classes.')
        return dict(width=ds.width, height=ds.height, crs=str(ds.crs),
                    pixel_size=list(ds.res), bounds_wgs84=list(bounds),
                    nodata=ds.nodata, dtype=ds.dtypes[0])


def copy_validate(source, destination, kind):
    """One streaming pass: full value checks, lossless compression, sample locations."""
    temp = destination.with_suffix('.partial.tif')
    total_valid = positive = 0
    minimum = math.inf
    maximum = -math.inf
    examples = []
    last_update = 0
    try:
        with rasterio.open(source) as src:
            profile = src.profile.copy()
            profile.update(driver='GTiff', tiled=True, blockxsize=256, blockysize=256,
                           compress='deflate', zlevel=6, predictor=3 if src.dtypes[0].startswith('float') else 2,
                           BIGTIFF='IF_SAFER')
            # Each stripe is at most ~52 MB for the known height rasters.
            xy_to_ll = Transformer.from_crs(src.crs, 'EPSG:4326', always_xy=True)
            with rasterio.open(temp, 'w', **profile) as dst:
                dst.scales, dst.offsets = src.scales, src.offsets
                dst.update_tags(**src.tags())
                dst.update_tags(1, **src.tags(1))
                for row in range(0, src.height, 256):
                    w = Window(0, row, src.width, min(256, src.height-row))
                    data = src.read(1, window=w, masked=True)
                    values = data.compressed()
                    values = values[np.isfinite(values)]
                    if kind == 'cover' and len(values) and np.any((values != 0) & (values != 1)):
                        raise ValueError(f'{source.name}: unexpected valid canopy classes; expected 0/1.')
                    total_valid += int(len(values))
                    if len(values):
                        minimum = min(minimum, float(values.min()))
                        maximum = max(maximum, float(values.max()))
                    hits = (~np.ma.getmaskarray(data)) & np.isfinite(data.data) & (data.data > 0)
                    positive += int(np.count_nonzero(hits))
                    if kind == 'cover' and len(examples) < 8 and np.any(hits):
                        rr, cc = np.argwhere(hits)[len(np.argwhere(hits))//2]
                        x,y = src.transform*(float(cc)+.5, float(rr+row)+.5)
                        lon,lat = xy_to_ll.transform(x,y)
                        examples.append(dict(latitude=lat, longitude=lon))
                    dst.write(data.filled(src.nodata if src.nodata is not None else 0), 1, window=w)
                    # Explicit mask preserves external/alpha mask validity as well as nodata.
                    if np.any(np.ma.getmaskarray(data)) and src.nodata is None:
                        dst.write_mask((~np.ma.getmaskarray(data)).astype('uint8')*255, window=w)
                    if time.monotonic()-last_update > 5:
                        print(f'  {source.name}: {min(100,100*(row+w.height)/src.height):.0f}%', flush=True)
                        last_update = time.monotonic()
            if not total_valid or not positive:
                raise ValueError(f'{source.name}: no positive valid data. Import stopped.')
            # Spatial metadata and representative original pixel values must survive copying.
            with rasterio.open(temp) as check:
                if (check.crs, check.transform, check.width, check.height, check.nodata) != (src.crs, src.transform, src.width, src.height, src.nodata):
                    raise ValueError('Copy changed raster metadata unexpectedly.')
                for fy, fx in [(0,0),(.25,.25),(.5,.5),(.75,.75),(.95,.95)]:
                    w = Window(int(fx*(src.width-16)), int(fy*(src.height-16)), 16, 16)
                    a,b = src.read(1,window=w), check.read(1,window=w)
                    if not np.array_equal(a,b,equal_nan=True):
                        raise ValueError('Lossless copy verification failed.')
        os.replace(temp, destination)
    finally:
        if temp.exists():
            temp.unlink()
    return dict(valid_pixels=total_valid, positive_pixels=positive, minimum=minimum, maximum=maximum,
                examples=examples, compressed_bytes=destination.stat().st_size)


def prepare(project, source, scc_unit='unknown'):
    project,source = Path(project).resolve(),Path(source).resolve()
    if not (project/'ecotrace_repaired.py').exists():
        raise ValueError('Expected ecotrace_repaired.py in the project folder.')
    entries = []
    for name,filename,kind,unit in FILES:
        path = source/filename
        if not path.is_file():
            raise FileNotFoundError(f'Missing actual TIFF: {path}')
        info = inspect(path,kind)
        entries.append(dict(name=name, source=path, kind=kind,
                            height_unit=scc_unit if unit=='unknown' else unit,
                            fingerprint=fingerprint(path), **info))
    print('All four TIFF headers passed. Creating lossless, tiled app copies.',flush=True)
    print('The original TIFFs and existing app source stay unchanged.',flush=True)
    cache = project/'ecotrace_gis_data'
    cache.mkdir(exist_ok=True)
    rasters, verification = [], []
    for entry in entries:
        path = entry.pop('source')
        signature = hashlib.sha256(json.dumps(entry['fingerprint'],sort_keys=True).encode()).hexdigest()[:16]
        output = cache/(signature+'.tif')
        report_file = cache/(signature+'.json')
        if output.exists() and report_file.exists():
            report = json.loads(report_file.read_text())
            if report.get('compressed_bytes') != output.stat().st_size:
                raise ValueError(f'Cached TIFF changed: {output}. Move it aside and rerun preparation.')
            inspect(output,entry['kind'])
            print('Reusing verified copy: '+entry['name'],flush=True)
        else:
            report = copy_validate(path,output,entry['kind'])
            report_file.write_text(json.dumps(report,indent=2))
        entry['path'] = str(output)
        entry['source_filename'] = path.name
        entry.update({k:v for k,v in report.items() if k != 'examples'})
        rasters.append(entry)
        for p in report.get('examples',[]):
            verification.append(dict(source=entry['name'],**p))
    manifest = dict(version=1, rasters=rasters, verification_points=verification,
                    assumptions=dict(ray_spacing_m=.6, ray_limit_m=120, receptor_height_m=1.5,
                    canopy_attenuation=.65, missing_height_m=8, terrain='flat local ground',
                    vegetation_geometry='vertical columns', route_sample_spacing_max_m=15,
                    fusion='maximum of existing tree score and raster contribution; no double addition',
                    nodata='unknown; never used to remove mapped trees',
                    santa_clara_height_unit=scc_unit,
                    santa_clara_unit_evidence='Unconfirmed unless explicitly selected using source documentation.',
                    alameda_unit_evidence='ALCC_UNIFIED_CHM_1M.tif.xml explicitly specifies vegetation height in feet.',
                    survey_dates='2022 crowns, Santa Clara 2020 heights, Alameda mixed acquisition years'))
    # Exercise actual coordinate conversion and ray sampling before activating manifest.
    from ecotrace_gis_runtime import Provider
    provider = Provider(manifest)
    checked=[]
    for p in verification:
        detail=provider.shadow(p['latitude'],p['longitude'],89.,180.)
        checked.append(dict(**p, **detail))
    if not any(x['hit'] for x in checked):
        raise ValueError('Canopy pixels were found, but no test ray detected usable height. Inspect the exported layers.')
    manifest['preflight_samples'] = checked
    target = project/'ecotrace_gis_manifest.json'
    if target.exists():
        shutil.copy2(target, project/('ecotrace_gis_manifest_backup_'+str(time.time_ns())+'.json'))
    temp = target.with_suffix('.tmp')
    temp.write_text(json.dumps(manifest,indent=2,allow_nan=False))
    os.replace(temp,target)
    print('READY: four raster copies prepared; coordinate and canopy-ray checks passed.',flush=True)
    if scc_unit=='unknown':
        print('Santa Clara height units are unconfirmed: those heights are not yet used; 8 m modeled heights fill missing heights.',flush=True)
    print('Report: '+str(target),flush=True)
    print('Next: run start_gis.py using the SAME EcoTrace virtual environment.',flush=True)
    return manifest


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--project',type=Path,default=Path.home()/'Downloads/EcoTrace')
    parser.add_argument('--source',type=Path,default=Path.home()/'Desktop/EcoTrace_QGIS_Experiment')
    parser.add_argument('--santa-clara-height-unit',choices=['unknown','feet','meters'],default='unknown',
                        help='Use feet/meters only when confirmed by source documentation.')
    args=parser.parse_args()
    try:
        prepare(args.project,args.source,args.santa_clara_height_unit)
    except Exception as exc:
        raise SystemExit('GIS preparation stopped: '+str(exc))
