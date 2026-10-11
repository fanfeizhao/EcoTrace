"""Read-only screening of every record in EcoTrace's planting candidate JSON.

Uses the prepared 2022 canopy rasters. Writes separate JSON/GeoJSON/HTML reports.
No candidate database, manual approval, or app source is changed.
"""
import argparse
import collections
import datetime
import html
import json
import math
import os
import sys
from pathlib import Path
import numpy as np


def records(payload):
    if isinstance(payload,list):return payload
    if isinstance(payload,dict):
        for key in ('sites','candidates','features'):
            if isinstance(payload.get(key),list):return payload[key]
    raise ValueError('Candidate JSON must be a list or contain sites, candidates, or features.')


def location(row):
    if not isinstance(row,dict):return None
    props=row.get('properties') if isinstance(row.get('properties'),dict) else {}
    merged={**props,**row}
    lat=merged.get('latitude',merged.get('lat'))
    lon=merged.get('longitude',merged.get('lon',merged.get('lng')))
    geometry=row.get('geometry') if isinstance(row.get('geometry'),dict) else {}
    if (lat is None or lon is None) and geometry.get('type')=='Point':
        coords=geometry.get('coordinates') or []
        if len(coords)>=2:lon,lat=coords[:2]
    try:
        lat,lon=float(lat),float(lon)
        if math.isfinite(lat) and math.isfinite(lon) and -85<=lat<=85 and -180<=lon<=180:
            return lat,lon
    except (ValueError,TypeError,OverflowError):pass
    return None


def screen(rows,rasters,progress=True):
    # Center plus a 0.6-meter grid in a 3-meter disk. Grid hits indicate nearby
    # canopy evidence, not an exact crown clearance measurement or area fraction.
    offsets=[(0.,0.)]+[(i*.6,j*.6) for i in range(-5,6) for j in range(-5,6)
                      if (i or j) and i*i+j*j<=25]
    dx=np.array([x for x,y in offsets]);dy=np.array([y for x,y in offsets])
    results=[]
    for index,row in enumerate(rows,1):
        props=row.get('properties') if isinstance(row,dict) and isinstance(row.get('properties'),dict) else {}
        fields={**props,**row} if isinstance(row,dict) else {}
        entry=dict(record_index=index,
                   candidate_id=str(fields.get('site_id') or fields.get('candidate_id') or fields.get('id') or f'record-{index}'),
                   area_name=str(fields.get('area_name') or fields.get('name') or ''),
                   screening_status=str(fields.get('screening_status') or ''),
                   original_review_metadata={k:v for k,v in fields.items()
                       if any(word in k.lower() for word in ('review','approv','manual'))},
                   latitude=None,longitude=None,canopy_at_candidate=False,
                   canopy_grid_hits_3m=0,grid_samples_3m=len(offsets),
                   nearest_detected_canopy_sample_m=None,canopy_sources=[],
                   status='invalid_coordinates')
        point=location(row)
        if point:
            lat,lon=point
            entry.update(latitude=lat,longitude=lon)
            lats=lat+dy/111320.
            lons=lon+dx/(111320.*math.cos(math.radians(lat)))
            hits=np.zeros(len(offsets),dtype=bool)
            explicit_zero_at_center=False
            for raster in rasters:
                values=raster.sample(lons,lats)
                found=values==1
                if np.any(found):entry['canopy_sources'].append(raster.entry['name'])
                hits|=found
                explicit_zero_at_center |= bool(values[0]==0)
            entry.update(canopy_at_candidate=bool(hits[0]),canopy_grid_hits_3m=int(hits.sum()))
            if np.any(hits):
                entry['nearest_detected_canopy_sample_m']=round(float(np.hypot(dx[hits],dy[hits]).min()),2)
            if hits[0]:entry['status']='canopy_at_candidate_review'
            elif np.any(hits):entry['status']='nearby_canopy_within_3m_review'
            elif explicit_zero_at_center:entry['status']='no_canopy_detected_center_explicit_zero'
            else:entry['status']='no_canopy_detected_coverage_unconfirmed'
        results.append(entry)
        if progress and (index%250==0 or index==len(rows)):
            print(f'Checked {index:,}/{len(rows):,} candidate records',flush=True)
    return results


def save_report(output,results,source,layer_names):
    output.mkdir(parents=True,exist_ok=False)
    counts=dict(collections.Counter(x['status'] for x in results))
    summary=dict(candidate_database=str(source),records_checked=len(results),counts=counts,
        canopy_layers=layer_names,app_and_candidates_modified=False,
        scope='All records in the named JSON database; app-only runtime additions/filters are not evaluated.',
        method='Exact center pixel plus 0.6 m sample grid within 3 m; positive canopy is class 1.',
        interpretation='Review flags only. NoData is unknown, not evidence of open planting space. No-canopy findings do not establish planting suitability. Counts are candidate records, not unique trees.',
        report_folder=str(output))
    (output/'summary.json').write_text(json.dumps(summary,indent=2,ensure_ascii=False))
    features=[]
    for result in results:
        geometry=None if result['latitude'] is None else dict(type='Point',coordinates=[result['longitude'],result['latitude']])
        features.append(dict(type='Feature',geometry=geometry,properties=result))
    (output/'candidate_canopy_review.geojson').write_text(json.dumps(dict(type='FeatureCollection',features=features),ensure_ascii=False))
    rank={'canopy_at_candidate_review':0,'nearby_canopy_within_3m_review':1,'invalid_coordinates':2,
          'no_canopy_detected_coverage_unconfirmed':3,'no_canopy_detected_center_explicit_zero':4}
    ordered=sorted(results,key=lambda x:(rank[x['status']],x['record_index']))
    esc=lambda x:html.escape(str(x))
    body=''.join('<tr>'+''.join('<td>'+esc(x.get(k,''))+'</td>' for k in
        ('record_index','candidate_id','area_name','status','canopy_grid_hits_3m','nearest_detected_canopy_sample_m','latitude','longitude'))+'</tr>' for x in ordered)
    page='''<!doctype html><html><meta charset="utf-8"><title>EcoTrace candidate canopy review</title>
<style>body{font:15px system-ui;margin:28px;color:#172b24}table{border-collapse:collapse;font-size:12px}td,th{border:1px solid #cad7ce;padding:7px;text-align:left}th{position:sticky;top:0;background:#dcefe3}tr:nth-child(even){background:#f4f8f5}pre{white-space:pre-wrap}</style>
<h1>Candidate canopy review</h1><p>Read-only screening against the prepared 2022 canopy maps. Review overlap flags before making changes. Existing approvals are preserved.</p>
<p>A nearby crown can overhang a candidate without a trunk at that coordinate. NoData does not establish absence of canopy. No result proves permission, soil, utility clearance, or plantable space.</p>
<pre>'''+esc(json.dumps(summary,indent=2,ensure_ascii=False))+'''</pre><table><thead><tr><th>Record</th><th>Candidate ID</th><th>Area</th><th>Result</th><th>Canopy grid hits</th><th>Nearest hit (m)</th><th>Latitude</th><th>Longitude</th></tr></thead><tbody>'''+body+'</tbody></table></html>'
    (output/'review.html').write_text(page)
    return summary


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project',type=Path,default=Path.home()/'Downloads/EcoTrace')
    parser.add_argument('--update-folder',type=Path,default=Path.home()/'Downloads/EcoTrace_GIS_Update')
    parser.add_argument('--candidates',type=Path)
    parser.add_argument('--output',type=Path)
    args=parser.parse_args()
    root=args.project.expanduser().resolve()
    sys.path.insert(0,str(args.update_folder.expanduser().resolve()))
    from ecotrace_gis_runtime import Raster
    manifest=json.loads((root/'ecotrace_gis_manifest.json').read_text())
    layers=[x for x in manifest['rasters'] if x['kind']=='cover']
    if len(layers)!=2:raise ValueError('Expected the two prepared 2022 canopy layers.')
    rasters=[Raster(x) for x in layers]
    selected=args.candidates or Path(os.environ.get('ECOTRACE_SITE_DB',str(root/'ecotrace_reviewed_tree_sites.json')))
    source=selected.expanduser()
    if not source.is_absolute():source=root/source
    rows=records(json.loads(source.read_text()))
    if not rows:raise ValueError('No candidate records found; no report written.')
    print(f'Checking all {len(rows):,} records in {source.name}. No app data will be changed.',flush=True)
    results=screen(rows,rasters)
    stamp=datetime.datetime.now().strftime('%Y%m%d-%H%M%S-%f')
    output=args.output or Path.home()/'Desktop/EcoTrace_QGIS_Experiment/canopy_candidate_checks'/stamp
    summary=save_report(output,results,source,[x['name'] for x in layers])
    print(json.dumps(summary,indent=2,ensure_ascii=False),flush=True)
    print('\nDONE. Open review.html for the list, or add candidate_canopy_review.geojson to QGIS.',flush=True)
    print('Send summary.json back to review the counts.',flush=True)


if __name__=='__main__':
    try:main()
    except Exception as exc:raise SystemExit('Candidate check stopped: '+str(exc))
