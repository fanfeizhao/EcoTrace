"""Actual 119th-Congress CA-17 boundary, replacing the old prototype rectangle."""
import json,math
from pathlib import Path
from functools import lru_cache

DATA=json.loads(Path(__file__).with_name('ca17_boundary.geojson').read_text())
GEOMETRY=DATA['features'][0]['geometry']
POLYGONS=GEOMETRY['coordinates'] if GEOMETRY['type']=='MultiPolygon' else [GEOMETRY['coordinates']]

def ring_status(x,y,ring):
    """0 outside, 1 inside, 2 on boundary. Coordinates in lon/lat."""
    inside=False
    for a,b in zip(ring,ring[1:]+ring[:1]):
        ax,ay=a;bx,by=b;dx=bx-ax;dy=by-ay
        cross=(x-ax)*dy-(y-ay)*dx
        if abs(cross)<=1e-12 and min(ax,bx)-1e-10<=x<=max(ax,bx)+1e-10 and min(ay,by)-1e-10<=y<=max(ay,by)+1e-10:return 2
        if (ay>y)!=(by>y) and x<(bx-ax)*(y-ay)/(by-ay)+ax:inside=not inside
    return int(inside)

BOUNDS=[]
for poly in POLYGONS:
    xs=[p[0] for p in poly[0]];ys=[p[1] for p in poly[0]]
    BOUNDS.append((min(xs),min(ys),max(xs),max(ys)))

@lru_cache(maxsize=20000)
def contains(lat,lon):
    if not math.isfinite(lat+lon):return False
    for poly,b in zip(POLYGONS,BOUNDS):
        if not b[0]<=lon<=b[2] or not b[1]<=lat<=b[3]:continue
        status=ring_status(lon,lat,poly[0])
        if status==2:return True
        if status==1:
            holes=[ring_status(lon,lat,r) for r in poly[1:]]
            if 2 in holes or 1 not in holes:return True
    return False
