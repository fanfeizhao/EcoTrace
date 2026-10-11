"""Supplement EcoTrace's canopy shadows with local crown/height rasters.

Experimental: vertical vegetation columns, 1.5 m receptor, flat local ground,
0.6 m ray spacing, 120 m search limit, fixed 0.65 canopy attenuation.
Only class 1 is positive canopy evidence. NoData is never absence evidence.
Existing mapped-tree evidence is retained with max(), never added twice.
"""
from __future__ import annotations
import json
import math
import threading
from collections import OrderedDict
from pathlib import Path
import numpy as np
import rasterio
from rasterio.windows import Window
from pyproj import Transformer

VERSION = "ecotrace-local-gis-2-methods"
_provider = None
_engine = None


class Raster:
    def __init__(self, entry):
        self.entry = entry
        self.path = Path(entry['path'])
        self.local = threading.local()
        with rasterio.open(self.path) as ds:
            self.crs = ds.crs
            self.transform = ds.transform
            self.width, self.height = ds.width, ds.height
            self.scale, self.offset = ds.scales[0], ds.offsets[0]
        self.convert = Transformer.from_crs('EPSG:4326', self.crs, always_xy=True)

    def sample(self, lons, lats):
        """Nearest original pixels, bounded per-thread block cache, masked to NaN."""
        x, y = self.convert.transform(lons, lats)
        inv = ~self.transform
        cols = np.floor(inv.a * np.asarray(x) + inv.b * np.asarray(y) + inv.c).astype(int)
        rows = np.floor(inv.d * np.asarray(x) + inv.e * np.asarray(y) + inv.f).astype(int)
        out = np.full(len(cols), np.nan)
        valid = (cols >= 0) & (cols < self.width) & (rows >= 0) & (rows < self.height)
        if not np.any(valid):
            return out
        if not hasattr(self.local, 'ds'):
            self.local.ds = rasterio.open(self.path)
            self.local.cache = OrderedDict()
        cache = self.local.cache
        indices = np.flatnonzero(valid)
        keys = np.stack((rows[indices] // 256, cols[indices] // 256), axis=1)
        for br, bc in np.unique(keys, axis=0):
            key = (int(br), int(bc))
            block = cache.pop(key, None)
            if block is None:
                w = Window(bc*256, br*256,
                           min(256, self.width-bc*256), min(256, self.height-br*256))
                block = self.local.ds.read(1, window=w, masked=True).astype('float32').filled(np.nan)
                block = block * self.scale + self.offset
            cache[key] = block
            while len(cache) > 64:
                cache.popitem(last=False)
            take = indices[(keys[:, 0] == br) & (keys[:, 1] == bc)]
            out[take] = block[rows[take]-br*256, cols[take]-bc*256]
        return out


class Provider:
    def __init__(self, manifest):
        self.manifest = manifest
        self.cover = [Raster(x) for x in manifest['rasters'] if x['kind'] == 'cover']
        self.heights = [Raster(x) for x in manifest['rasters'] if x['kind'] == 'height']
        self.lock = threading.Lock()
        self.calls = self.hits = self.raises = 0
        self.height_samples = self.measured_samples = self.missing_samples = self.unconfirmed_samples = 0
        self.last = None

    def shadow(self, lat, lon, altitude, azimuth, base=0.):
        if not all(math.isfinite(v) for v in (lat, lon, altitude, azimuth)):
            raise ValueError('Coordinates and sun angles must be finite.')
        if not -85 <= lat <= 85 or not -180 <= lon <= 180:
            raise ValueError('Coordinates outside supported bounds.')
        if not 0 < altitude <= 90:
            return {'raster_canopy_score': 0., 'hit': False, 'reason': 'Sun below horizon.'}
        # Look TOWARD the sun; objects cast their shadows in the opposite direction.
        distances = np.arange(0., 120.01, .6)
        angle = math.radians(azimuth)
        lats = lat + np.cos(angle)*distances/111320.
        lons = lon + np.sin(angle)*distances/(111320.*math.cos(math.radians(lat)))
        crown = np.zeros(len(distances), dtype=bool)
        for raster in self.cover:
            crown |= raster.sample(lons, lats) == 1
        indices = np.flatnonzero(crown)
        detail = {'raster_canopy_score': 0., 'hit': False,
                  'canopy_ray_samples': int(len(indices)), 'height_source': None,
                  'height_m': None, 'assumed_height': False,
                  'measured_height_samples': 0, 'missing_or_invalid_height_samples': 0,
                  'unconfirmed_unit_height_samples': 0,
                  'reason': 'No additional raster canopy shadow detected; missing pixels are unknown.'}
        if len(indices):
            heights = np.full(len(indices), np.nan)
            sources = np.full(len(indices), '', dtype=object)
            unconfirmed = np.zeros(len(indices), dtype=bool)
            for raster in self.heights:
                unit = raster.entry.get('height_unit', 'unknown')
                values = raster.sample(lons[indices], lats[indices])
                if unit not in ('feet', 'meters'):
                    unconfirmed |= np.isfinite(values) & (values >= 0)
                    continue
                if unit == 'feet':
                    values *= .3048
                use = np.isfinite(values) & (values >= 0) & (values <= 100) & ~np.isfinite(heights)
                heights[use] = values[use]
                sources[use] = raster.entry['name']
            assumed = ~np.isfinite(heights)
            detail.update(measured_height_samples=int(np.count_nonzero(~assumed)),
                          missing_or_invalid_height_samples=int(np.count_nonzero(assumed & ~unconfirmed)),
                          unconfirmed_unit_height_samples=int(np.count_nonzero(assumed & unconfirmed)))
            heights[assumed] = 8.0
            sources[assumed] = 'modeled 8 m height (local height unavailable or units unconfirmed)'
            ray_height = 1.5 + distances[indices]*math.tan(math.radians(altitude))
            hits = np.flatnonzero(heights > ray_height)
            if len(hits):
                j = int(hits[0])
                detail.update(raster_canopy_score=65., hit=True,
                              height_source=str(sources[j]), height_m=round(float(heights[j]), 2),
                              assumed_height=bool(assumed[j]),
                              distance_to_canopy_m=round(float(distances[indices[j]]), 1),
                              reason='2022 canopy intersects the modeled sun ray; fixed 65/100 attenuation assumption.')
        with self.lock:
            self.calls += 1
            self.hits += int(detail['hit'])
            self.raises += int(detail['raster_canopy_score'] > base*100 + .001)
            self.height_samples += detail['canopy_ray_samples']
            self.measured_samples += detail['measured_height_samples']
            self.missing_samples += detail['missing_or_invalid_height_samples']
            self.unconfirmed_samples += detail['unconfirmed_unit_height_samples']
            self.last = dict(detail)
        return detail

    def status(self):
        with self.lock:
            counts = dict(sample_calls=self.calls, raster_shadow_hits=self.hits,
                          canopy_score_increases=self.raises, last_sample=self.last,
                          height_sample_counts=dict(total=self.height_samples, measured=self.measured_samples,
                              missing_or_invalid=self.missing_samples, unconfirmed_units=self.unconfirmed_samples,
                              scope='Canopy ray samples since this server started, including probes; repeated pixels may be counted. Not whole-area coverage.'))
        return dict(version=VERSION, active=True, **counts,
                    mapped_tree_count=len(_engine.trees) if _engine else None,
                    mapped_building_count=len(_engine.buildings) if _engine else None,
                    rasters=[{k:v for k,v in x.items() if k != 'path'} for x in self.manifest['rasters']],
                    assumptions=self.manifest['assumptions'],
                    verification_points=self.manifest.get('verification_points', []))


def summary():
    unknown = any(r.entry.get('height_unit') == 'unknown' for r in _provider.heights)
    text = 'Local 2022 canopy + county height rasters active. '
    if unknown:
        text += 'Santa Clara height units await confirmation; modeled 8 m heights fill gaps. '
    return text


def activate(root):
    """Called BEFORE the existing app imports/creates CanopyEngine."""
    global _provider
    manifest = json.loads((Path(root)/'ecotrace_gis_manifest.json').read_text())
    _provider = Provider(manifest)
    import canopy_engine
    original = canopy_engine.CanopyEngine
    if getattr(original, '_gis_version', None):
        raise RuntimeError('GIS already activated in this process.')

    class GISCanopyEngine(original):
        _gis_version = VERSION

        def __init__(self, *args, **kwargs):
            global _engine
            super().__init__(*args, **kwargs)
            self._gis_local = threading.local()
            self.available = True
            _engine = self

        def tree_shadow_probability(self, lat, lon, altitude, azimuth):
            base, count = super().tree_shadow_probability(lat, lon, altitude, azimuth)
            detail = _provider.shadow(lat, lon, altitude, azimuth, base)
            self._gis_local.detail = detail
            return max(base, detail['raster_canopy_score']/100.), count

        def analyze_point(self, *args, **kwargs):
            self._gis_local.detail = None
            result = super().analyze_point(*args, **kwargs)
            detail = self._gis_local.detail
            if detail is not None:
                note = ' Local 2022 canopy raster checked.'
                if detail['hit']:
                    note += (' Sun-ray canopy height: '+str(detail['height_m'])+' m; '
                             + detail['height_source'] + '. Raster canopy contribution uses 65/100 attenuation.')
                result.reason += note
                result.data_quality += ' + local 2022 canopy/height raster check'
            return result

    canopy_engine.CanopyEngine = GISCanopyEngine


def probe(lat, lon, altitude=45., azimuth=180.):
    if not _engine:
        raise RuntimeError('App engine is not ready.')
    base, _ = super(type(_engine), _engine).tree_shadow_probability(lat, lon, altitude, azimuth)
    detail = _provider.shadow(lat, lon, altitude, azimuth, base)
    return dict(latitude=lat, longitude=lon, altitude=altitude, azimuth=azimuth,
                existing_tree_score=round(base*100, 2),
                combined_tree_score=round(max(base*100, detail['raster_canopy_score']), 2),
                **detail)


def status():
    return _provider.status()


def densified_builder(segment, distance):
    def build(coordinates, departure, use_time_of_day):
        result = []
        for a, b in zip(coordinates, coordinates[1:]):
            length = distance(a[1], a[0], b[1], b[0])
            if length < 1:
                continue
            count = max(1, math.ceil(length/15.))
            for i in range(count):
                left = [a[j]+(b[j]-a[j])*i/count for j in (0,1)]
                right = [a[j]+(b[j]-a[j])*(i+1)/count for j in (0,1)]
                result.append(segment(left, right, departure, use_time_of_day))
        return result
    return build


def augment_source(source):
    """Change in-memory source only; preserve all existing files and repair hooks."""
    import ast
    tree = ast.parse(source)
    summaries = [n.value for n in ast.walk(tree) if isinstance(n, ast.keyword) and n.arg == 'canopy_model_summary']
    if len(summaries) != 1:
        raise RuntimeError('This app version has an unexpected route summary; source files remain unchanged.')
    # AST locations use UTF-8 byte offsets; tolerate whitespace/unparse changes
    # made by the existing route repair, without relying on comment substrings.
    value = summaries[0]
    raw_lines = source.encode('utf-8').splitlines(keepends=True)
    start = sum(map(len,raw_lines[:value.lineno-1])) + value.col_offset
    end = sum(map(len,raw_lines[:value.end_lineno-1])) + value.end_col_offset
    raw = source.encode('utf-8')
    source = (raw[:start]+b'GIS_SUMMARY + ('+raw[start:end]+b')'+raw[end:]).decode('utf-8')
    # Import after any future statements rather than prepending arbitrary Python.
    tree = ast.parse(source)
    position = 0
    for node in tree.body:
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            position = node.end_lineno
        elif isinstance(node, ast.ImportFrom) and node.module == '__future__':
            position = node.end_lineno
        else:
            break
    lines = source.splitlines(keepends=True)
    lines.insert(position, 'from ecotrace_gis_runtime import summary as _gis_summary\nGIS_SUMMARY = _gis_summary()\n')
    source = ''.join(lines)
    import re
    # Remove both legacy forms from About, including notes left by earlier builds.
    # Limit deletion to the About panel; preserve the new Data & Methods content.
    about_match = re.search(r'<div\b[^>]*id="guide-about"[^>]*>', source)
    if about_match:
        about_start = about_match.end()
        about_end = source.find('<section id="ewaste-panel"', about_start)
        if about_end < 0:
            raise RuntimeError('Cannot bound the About panel safely; app files remain unchanged.')
        about = source[about_start:about_end]
        about = re.sub(r'<section\b[^>]*>\s*<p\b[^>]*>\s*<strong>\s*Local canopy data:\s*</strong>.*?</section>',
                       '', about, flags=re.S|re.I)
        about = re.sub(r'<p\b[^>]*>\s*<strong>\s*(?:Walking comfort|Local canopy data):\s*</strong>.*?</p>',
                       '', about, flags=re.S|re.I)
        source = source[:about_start] + about + source[about_end:]
    # Move only the previously added comfort note out of About.
    source = re.sub(r'<p\b[^>]*><strong>Walking comfort:</strong>.*?</p>', '', source, flags=re.S)
    # Replace the older detailed comfort paragraph with the combined methods text.
    source = re.sub(r'<p\b[^>]*><strong>Experimental walking comfort:</strong>.*?</p>', '', source, flags=re.S)
    pattern = r'(<summary\b[^>]*>[^<]*Walking Routes</summary>)'
    if len(re.findall(pattern, source)) != 1:
        raise RuntimeError('Cannot identify Data & Methods Walking Routes; no app files changed.')
    note = Path(__file__).with_name('gis_methods.html').read_text()
    source = re.sub(pattern, lambda match: match.group(1)+note, source, count=1)
    source = source.replace('WRI/Meta canopy raster + Santa Clara County building footprints/heights.',
        'Local 2022 canopy and available county canopy heights + mapped trees + WRI/Meta canopy evidence + Santa Clara County buildings/heights.')
    source = source.replace('same WRI/Meta canopy + County building-height model used for walking.',
        'the same local canopy, available county heights, mapped trees, WRI/Meta evidence and building shadows used for walking.')
    source += '''\n
from ecotrace_gis_runtime import status as _gis_status, probe as _gis_probe, densified_builder
build_exposure_segments = densified_builder(segment_exposure, _tree_qa_haversine_m)
@app.get('/api/ecotrace-gis-status')
def ecotrace_gis_status():
    return _gis_status()
@app.get('/api/ecotrace-gis-probe')
def ecotrace_gis_probe(lat: float, lon: float, altitude: float=45., azimuth: float=180.):
    if not (-85 <= lat <= 85 and -180 <= lon <= 180 and 0 < altitude <= 90 and 0 <= azimuth <= 360):
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail='Invalid coordinate or sun angle.')
    return _gis_probe(lat, lon, altitude, azimuth)
'''
    compile(source, 'EcoTrace GIS integration', 'exec')
    return source
