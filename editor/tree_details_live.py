"""Keep legacy tree-detail routes in sync with the authoritative edited database."""
import copy,json,math
from pathlib import Path
from functools import lru_cache
from canopy_editor import fields,identity,point,records
from transmission import Screening

def install(project):
    import ecotrace_tree_details as module
    root=Path(project).resolve();path=root/'ecotrace_reviewed_tree_sites.json'
    if getattr(module,'_live_editor_root',None)==str(root):return
    original_details=module.public_tree_details
    @lru_cache(maxsize=1)
    def load(stamp,config_stamp):
        payload=json.loads(path.read_text());rows=records(payload);seen=set();active=[]
        screen=Screening(root)
        for i,row in enumerate(rows,1):
            key=identity(row,i);p=fields(row);lat,lon=point(row)
            if key in seen:raise RuntimeError('Duplicate candidate ID: '+key)
            if not (-90<=lat<=90 and -180<=lon<=180):raise RuntimeError('Invalid candidate coordinates: '+key)
            seen.add(key)
            decision=p.get('canopy_manual_review',{}).get('decision')
            if decision=='exclude' or screen.blocked(row):continue
            # Manual additions are unapproved planning records and lack the old generator flag.
            if p.get('recommendation_eligible') is False and not p.get('canopy_editor_created'):continue
            current=dict(p,site_id=key,latitude=lat,longitude=lon)
            active.append(current)
        meta=copy.deepcopy(payload.get('metadata',{})) if isinstance(payload,dict) else {}
        meta.update(site_count=len(active),database_record_count=len(rows),filter_version='canopy-editor-live-details-v11')
        return {'metadata':meta,'sites':active},{p['site_id']:p for p in active}
    def stamp(p):
        if not p.exists():return None
        s=p.stat();return s.st_mtime_ns,s.st_size
    def tree_data():return load(stamp(path),stamp(root/'ecotrace_transmission_filter.json'))
    tree_data.cache_clear=load.cache_clear
    def details(site):
        from visual_review import result as visual_result
        record=site.get('original_record') or site
        decision=record.get('canopy_manual_review',{}).get('decision')
        if decision in ('relocate','added') or record.get('canopy_editor_created') or record.get('spatial_evidence_status'):
            # Old geocoded labels and imagery evidence can refer to the pre-edit point.
            current=copy.deepcopy(record)
            for key in ('location_label','location_context','where_location','road_name','explanation_summary','imagery_source','imagery_capture_range','manual_review_status'):
                current.pop(key,None)
            current['location_label']='Use the saved coordinate marker'
            result=original_details(current)
            result.update(evidence_confidence='Location-specific checks pending',review_status_label='Manually selected location',imagery_source='Review editor satellite imagery',imagery_capture_range='Capture date unavailable')
        else:
            result=original_details(site)
        visual=visual_result(record,record.get('site_id') or record.get('candidate_id') or '')
        if visual:
            result['visual_canopy_review']=visual
            result['review_status_label']=('Manually kept · ' if visual['manual_resolved'] else '')+visual['label']
            result['evidence_confidence']='Satellite desktop screening; not field verified'
        note='Some candidate points may overlap existing tree canopy. Nearby planting space may be available; confirm the exact spot on site.'
        result['canopy_location_note']=note
        result['why_chosen']=str(result.get('why_chosen') or '').rstrip()+' '+note
        return result
    module.tree_data=tree_data;module.public_tree_details=details;module._live_editor_root=str(root)
