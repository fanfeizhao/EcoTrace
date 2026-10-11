"""Local, indexed transmission proximity screening; no candidate DB mutations.
Distances: local equirectangular metres at 37.42 N, accurate to about 0.3% here.
20 m is a configurable review buffer, not an electrical clearance standard.
"""
import json
import math
from functools import lru_cache
from pathlib import Path

HERE=Path(__file__).resolve().parent
SCALE=6371008.8*math.pi/180
COS=math.cos(math.radians(37.42))
CELL=200

def xy(lat,lon):return ((lon+122)*SCALE*COS,(lat-37)*SCALE)

def segment_distance(p,a,b):
    dx,dy=b[0]-a[0],b[1]-a[1];d=dx*dx+dy*dy
    t=max(0,min(1,((p[0]-a[0])*dx+(p[1]-a[1])*dy)/d)) if d else 0
    return math.hypot(p[0]-a[0]-t*dx,p[1]-a[1]-t*dy)

class Lines:
    def __init__(self):
        self.geojson=json.loads((HERE/'transmission_lines.geojson').read_text())
        self.segments=[];self.grid={}
        for f in self.geojson['features']:
            g=f['geometry'];parts=g['coordinates'] if g['type']=='MultiLineString' else [g['coordinates']]
            for part in parts:
                pts=[xy(lat,lon) for lon,lat in part]
                for a,b in zip(pts,pts[1:]):
                    i=len(self.segments);self.segments.append((a,b,f['properties']))
                    for x in range(math.floor(min(a[0],b[0])/CELL),math.floor(max(a[0],b[0])/CELL)+1):
                        for y in range(math.floor(min(a[1],b[1])/CELL),math.floor(max(a[1],b[1])/CELL)+1):self.grid.setdefault((x,y),[]).append(i)
    @lru_cache(maxsize=20000)
    def nearby(self,lat,lon):
        p=xy(lat,lon);ids=set()
        for x in range(math.floor((p[0]-101)/CELL),math.floor((p[0]+101)/CELL)+1):
            for y in range(math.floor((p[1]-101)/CELL),math.floor((p[1]+101)/CELL)+1):ids.update(self.grid.get((x,y),[]))
        found={}
        for i in ids:
            a,b,props=self.segments[i];d=segment_distance(p,a,b);kind=props['Type']
            if d<=100 and (kind not in found or d<found[kind]['distance_m']):found[kind]={'distance_m':d,'type':kind,'owner':props['Owner'],'kv':props['kV']}
        return list(found.values())

@lru_cache(maxsize=1)
def lines():return Lines()

class Screening:
    def __init__(self,root):
        self.path=Path(root)/'ecotrace_transmission_filter.json'
        self.config={'enabled':True,'threshold_m':20.0}
        if self.path.exists():self.config=self.validate(json.loads(self.path.read_text()))
        self.data=lines()
    @staticmethod
    def validate(body):
        enabled=body.get('enabled');threshold=body.get('threshold_m')
        if not isinstance(enabled,bool) or isinstance(threshold,bool):raise ValueError('Choose enabled/disabled and a distance from 0 to 100 m.')
        threshold=float(threshold)
        if not math.isfinite(threshold) or not 0<=threshold<=100:raise ValueError('Distance must be between 0 and 100 m.')
        return {'enabled':enabled,'threshold_m':threshold}
    def check(self,lat,lon):
        nearby=self.data.nearby(float(lat),float(lon))
        hits=[n for n in nearby if n['distance_m']<=self.config['threshold_m']]
        nearest=min(nearby,key=lambda n:n['distance_m']) if nearby else None
        return {'filtered':self.config['enabled'] and bool(hits),'within_threshold':bool(hits),
                'nearest':{**nearest,'distance_m':round(nearest['distance_m'],1)} if nearest else None}
    def blocked(self,row):
        from canopy_editor import point
        try:return self.check(*point(row))['filtered']
        except (ValueError,TypeError,KeyError):return False
    def save(self,body):
        from canopy_editor import atomic_write
        config=self.validate(body);atomic_write(self.path,config);self.config=config
