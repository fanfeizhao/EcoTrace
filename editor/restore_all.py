"""User-requested restoration of saved candidates; keeps locations and evidence."""
import copy,json,fcntl
from datetime import datetime,timezone
from pathlib import Path
from canopy_editor import records,fields,identity,point,atomic_write

def restore(project):
    root=Path(project).resolve();path=root/'ecotrace_reviewed_tree_sites.json'
    with (root/'.canopy-editor.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        before=path.read_bytes();payload=json.loads(before);rows=records(payload);seen=set()
        for i,r in enumerate(rows,1):
            key=identity(r,i);lat,lon=point(r)
            if key in seen or not (-90<=lat<=90 and -180<=lon<=180):raise ValueError('Duplicate ID or invalid coordinate: '+key)
            seen.add(key)
        now=datetime.now(timezone.utc).isoformat();changed=0
        for r in rows:
            p=fields(r)
            if p.get('canopy_manual_review',{}).get('decision')!='exclude':continue
            snapshot=copy.deepcopy(r);fields(snapshot).pop('canopy_review_undo',None);fields(snapshot).pop('canopy_review_history',None)
            history=copy.deepcopy(p.get('canopy_review_history',[]))
            if not isinstance(history,list):raise ValueError('Unexpected history; original file unchanged.')
            history.append({'action':'restore_all','at':now,'from':list(point(r)),'to':list(point(r)),'note':'User requested restoring all saved candidates. No new suitability verification.'})
            p['canopy_review_undo']=snapshot;p['canopy_review_history']=history
            p['canopy_manual_review']={'decision':'pending','at':now,'location':list(point(r)),'note':'Restored at user request; not newly verified.','reviewer':'user-requested restore'}
            changed+=1
        config_path=root/'ecotrace_transmission_filter.json'
        config=json.loads(config_path.read_text()) if config_path.exists() else {'enabled':True,'threshold_m':20.0}
        needs_config=config.get('enabled') is not False
        backup=None
        if changed or needs_config:
            backup=root/'canopy_review_backups'/('before-restore-all-'+datetime.now().strftime('%Y%m%d-%H%M%S-%f'));backup.mkdir(parents=True)
            (backup/path.name).write_bytes(before)
            (backup/'transmission_previous.json').write_text(json.dumps({'existed':config_path.exists(),'config':config},indent=2))
            if path.read_bytes()!=before:raise ValueError('Candidate file changed; restart restoration.')
            if changed:atomic_write(path,payload)
            config['enabled']=False;atomic_write(config_path,config)
        # A GIS layer of the full saved set; retains original positions and advisory labels.
        from visual_review import result as visual_result
        from transmission import Screening
        screen=Screening(root);features=[]
        for i,r in enumerate(rows,1):
            p=fields(r);key=identity(r,i);lat,lon=point(r);visual=visual_result(r,key);utility=screen.check(lat,lon)
            props={'candidate_id':key,'area_name':p.get('area_name',''),'review_decision':p.get('canopy_manual_review',{}).get('decision','pending'),'visual_label':visual['label'] if visual else 'Not in direct-canopy visual review','visual_reason':visual['reason'] if visual else '', 'near_transmission':utility['within_threshold'],'latitude':lat,'longitude':lon,'planting_approved':False}
            heat=p.get('surface_heat') or {};props['surface_temperature_c']=heat.get('surface_temperature_c')
            features.append({'type':'Feature','geometry':{'type':'Point','coordinates':[lon,lat]},'properties':props})
        export=root/'EcoTrace_all_candidates.geojson';atomic_write(export,{'type':'FeatureCollection','features':features})
        print(f'Restored {changed} excluded candidates. All {len(rows)} saved points exported to {export}',flush=True)
        print('Transmission proximity remains labeled; automatic hiding is off.',flush=True)
        if backup:print('Backup: '+str(backup),flush=True)
        return {'restored':changed,'total':len(rows),'export':str(export)}
