"""Rebuild priority statistics for edited candidates without reusing moved-point evidence."""
import copy
import json
import math
from collections import Counter
from datetime import datetime,timezone
from pathlib import Path
from canopy_editor import atomic_write,fields,identity,point,records

VERSION='canopy-editor-priority-sync-1'


def quartile(values):
    values=sorted(values)
    if not values:return None
    k=(len(values)-1)*.75;i=int(k)
    return values[i]+(values[min(i+1,len(values)-1)]-values[i])*(k-i)


def rebuild(payload,baseline,fingerprint,screening=None):
    rows=records(payload);original={r['site_id']:r for r in baseline['sites']}
    sites=[];excluded=0;missing=0;seen=set();utility_excluded=0
    for index,row in enumerate(rows,1):
        p=fields(row);key=identity(row,index);lat,lon=point(row)
        if key in seen:raise ValueError('Duplicate candidate ID: '+key)
        seen.add(key)
        if p.get('canopy_manual_review',{}).get('decision')=='exclude':
            excluded+=1;continue
        if screening and screening.blocked(row):
            utility_excluded+=1;continue
        old=original.get(key)
        same=bool(old and float(old['latitude'])==lat and float(old['longitude'])==lon)
        if same:
            result=copy.deepcopy(old)
        else:
            missing+=1
            result=dict(site_id=key,latitude=lat,longitude=lon,heat_c=None,heat_f=None,
                heat_dates=[],heat_observations=0,heat_method='Not yet checked at this coordinate',
                heat_scene_ids=[],heat_uncertainty_k_max=None,tract_geoid=None,tract_name=None,county=None,
                poverty_percent=None,poverty_moe_pp=None,poverty_threshold=None,median_household_income=None,
                income_moe=None,poverty_threshold_sensitive=False,income_topcoded=False,
                boundary_status='Location-specific census match pending')
        result['location']=p.get('area_name') or p.get('road_name') or (old or {}).get('location') or 'Planning candidate'
        result['evidence_coordinate_verified']=same
        sites.append(result)
    values=[s['heat_c'] for s in sites if isinstance(s.get('heat_c'),(int,float)) and math.isfinite(s['heat_c'])]
    cutoff=quartile(values)
    reasons=['Neither indicator reaches its higher-priority cutoff; planting could still help.',
             'One of historical surface heat and neighborhood poverty meets its higher-priority cutoff.',
             'Both historical surface heat and neighborhood poverty meet the higher-priority cutoffs.']
    for s in sites:
        t=s.get('heat_c');p=s.get('poverty_percent');threshold=s.get('poverty_threshold')
        heat=t is not None and cutoff is not None and t>=cutoff
        poverty=p is not None and threshold is not None and p>=threshold
        incomplete=t is None or p is None or threshold is None
        s['heat_status']='Historical heat check pending' if t is None else 'Higher historical heat' if heat else 'Below higher-heat cutoff'
        s['poverty_status']='Census match pending' if p is None else 'Higher neighborhood poverty' if poverty else 'Below higher-poverty cutoff'
        level=int(heat)+int(poverty)
        s['priority']='Data incomplete' if incomplete else ['Priority 3 - Standard','Priority 2 - Elevated','Priority 1 - High'][level]
        s['reason']='Location-specific heat/census evidence is incomplete; previous-coordinate evidence is not reused.' if incomplete else reasons[level]
    meta=copy.deepcopy(baseline['metadata'])
    meta.update(version=VERSION,candidate_count=len(sites),database_record_count=len(rows),excluded_count=excluded,
                transmission_filtered_count=utility_excluded,heat_count=len(values),poverty_count=sum(s.get('poverty_percent') is not None for s in sites),
                income_count=sum(s.get('median_household_income') is not None for s in sites),
                heat_upper_quartile_c=cutoff,priority_counts=dict(Counter(s['priority'] for s in sites)),
                sensitive_classified_count=sum(bool(s.get('poverty_threshold_sensitive')) and s['priority']!='Data incomplete' for s in sites),
                unique_tracts=len({s['tract_geoid'] for s in sites if s.get('tract_geoid')}),
                ambiguous_points=sum(s.get('boundary_status')=='ambiguous' for s in sites),
                source_fingerprint=fingerprint(rows),rebuilt_at=datetime.now(timezone.utc).isoformat(),
                pending_coordinate_evidence_count=missing,
                evidence_note='Historical evidence retained only for matching ID and exact coordinates in the backed-up priority dataset. New/moved points await spatial resampling. Heat quartile and counts recalculated over active candidates; county poverty reference thresholds unchanged.')
    meta['heat_retrieval']={'current_missing':len(sites)-len(values),'refreshed_all_sites':False,
                          'note':'Statistics rebuilt; no new satellite imagery retrieved.'}
    return {'metadata':meta,'sites':sites}


def sync(project,runtime=None):
    project=Path(project)
    if not (project/'ecotrace_priority_v3.py').exists():return None
    if runtime is None:
        import ecotrace_priority_runtime as runtime
    base=Path(runtime.ROOT);path=base/'ecotrace_priority_data.json'
    archive=base/'ecotrace_priority_before_canopy_editor.json'
    if not archive.exists():
        # Preserve the source-coordinate evidence permanently, including for Undo.
        with archive.open('xb') as f:f.write(path.read_bytes())
    baseline=json.loads(archive.read_text())
    payload=json.loads((project/'ecotrace_reviewed_tree_sites.json').read_text())
    from transmission import Screening
    result=rebuild(payload,baseline,runtime.fingerprint,Screening(project))
    atomic_write(path,result)
    runtime.data.cache_clear()
    # Keep the original strict validation: rebuilt statistics must match the DB.
    runtime.validate(project)
    dashboard=base/'ecotrace_priority_dashboard.html'
    old=base/'ecotrace_priority_dashboard_before_canopy_editor.html'
    if dashboard.exists() and not old.exists():
        with old.open('xb') as f:f.write(dashboard.read_bytes())
    template=Path(__file__).with_name('priority_live.html').read_text()
    if not dashboard.exists() or dashboard.read_text()!=template:dashboard.write_text(template)
    return result['metadata']
