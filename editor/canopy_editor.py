"""Local EcoTrace canopy-review editor. Backed by the authoritative candidate JSON."""
import copy
import hashlib
import json
import math
import os
import secrets
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path

VERSION = 'canopy-editor-19-copy-audit'
HERE = Path(__file__).resolve().parent
DIRECT = 'canopy_at_candidate_review'
NEAR = 'nearby_canopy_within_3m_review'


def records(data):
    if isinstance(data, list): return data
    for key in ('sites', 'candidates', 'features'):
        if isinstance(data, dict) and isinstance(data.get(key), list): return data[key]
    raise ValueError('Unsupported candidate database layout.')


def fields(row):
    return row.get('properties', row) if row.get('type') == 'Feature' else row


def identity(row, index):
    p = fields(row)
    return str(p.get('site_id') or p.get('candidate_id') or p.get('id') or f'record-{index}')


def point(row):
    p = fields(row)
    lat = p.get('latitude', p.get('lat'))
    lon = p.get('longitude', p.get('lon', p.get('lng')))
    if lat is None or lon is None:
        g = row.get('geometry', {})
        if g.get('type') == 'Point': lon, lat = g['coordinates'][:2]
    lat, lon = float(lat), float(lon)
    if not math.isfinite(lat + lon): raise ValueError('Invalid coordinates.')
    return lat, lon


def revision(row):
    return hashlib.sha256(json.dumps(row, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def distance(a, b):
    p, q = map(math.radians, (a[0], b[0]))
    h = math.sin((q-p)/2)**2 + math.cos(p)*math.cos(q)*math.sin(math.radians(b[1]-a[1])/2)**2
    return 6371008.8 * 2 * math.asin(min(1, math.sqrt(h)))


def atomic_write(path, data):
    fd, name = tempfile.mkstemp(prefix=path.name+'.', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(data, f, ensure_ascii=False, indent=2, allow_nan=False)
            f.flush(); os.fsync(f.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name): os.unlink(name)


class Conflict(ValueError): pass


class Store:
    def __init__(self, root, queue=None, validator=None, checker=None):
        self.root = Path(root)
        self.path = self.root / 'ecotrace_reviewed_tree_sites.json'
        self.queue = queue if queue is not None else json.loads((HERE/'review_queue.json').read_text())
        self.validator, self.checker = validator, checker
        self.transmission = None
        self.lock = threading.RLock()
        self.backup = None
        self.generation = 0
        self._listing_cache = None
        self._listing_cache_key = None
        # Validate before the app opens; never create an empty candidate DB.
        records(json.loads(self.path.read_text()))

    def index(self, rows):
        out = {}
        for n, row in enumerate(rows, 1):
            key = identity(row, n)
            if key in out: out[key] = None
            else: out[key] = (n, row)
        return out

    def entry(self, audit, indexed, compact=False):
        item = indexed.get(audit['candidate_id'])
        result = {k:v for k,v in audit.items() if not compact or k in ('candidate_id','record_index','latitude','longitude','area_name','group','status')}
        result['group'] = audit.get('group') or ('direct' if audit['status'] == DIRECT else 'near')
        if item is None:
            result.update(available=False, decision='unavailable', message='Candidate missing or duplicate ID; editing disabled.')
            return result
        n, row = item
        p = fields(row)
        lat, lon = point(row)
        review = p.get('canopy_manual_review', {})
        changed = distance((audit['latitude'], audit['longitude']), (lat, lon)) > .1
        from visual_review import result as visual_result
        result['visual_review']=visual_result(row,audit['candidate_id'])
        result['transmission']=self.transmission.check(lat,lon) if self.transmission else None
        result.update(available=True, latitude=lat, longitude=lon, revision=revision(row),
                      decision=review.get('decision', 'pending'), review=review,
                      can_undo='canopy_review_undo' in p, coordinate_changed=changed,
                      live_canopy=p.get('canopy_recheck'),
                      original_review_metadata=None if compact else {k:v for k,v in p.items() if ('review' in k or 'approv' in k) and k not in ('canopy_review_undo','canopy_review_history')})
        return result

    def audits(self, rows):
        result=list(self.queue)
        known={a['candidate_id'] for a in result}
        for index,row in enumerate(rows,1):
            p=fields(row);created=p.get('canopy_editor_created')
            key=identity(row,index)
            if isinstance(created,dict) and key not in known:
                lat,lon=created['location']
                result.append({'candidate_id':key,'record_index':index,'latitude':lat,'longitude':lon,
                               'area_name':p.get('area_name','Added candidate'),'group':'added',
                               'status':'manually_added','screening_status':p.get('screening_status','')})
                known.add(key)
            elif key not in known:
                try:lat,lon=point(row)
                except (ValueError,TypeError,KeyError):continue
                result.append({'candidate_id':key,'record_index':index,'latitude':lat,'longitude':lon,
                               'area_name':p.get('area_name') or p.get('road_name') or 'Planting candidate',
                               'group':'other','status':'outside_original_canopy_review',
                               'screening_status':p.get('screening_status','')})
                known.add(key)
        return result

    def commit(self, original_bytes, data):
        if self.path.read_bytes()!=original_bytes: raise Conflict('Database changed during the edit; retry after refreshing.')
        if self.backup is None:
            folder=self.root/'canopy_review_backups';folder.mkdir(exist_ok=True)
            self.backup=folder/('candidates-'+datetime.now().strftime('%Y%m%d-%H%M%S-%f')+'.json')
            self.backup.write_bytes(original_bytes)
        atomic_write(self.path,data)
        self.generation+=1
        self._listing_cache = None
        self._listing_cache_key = None

    def add(self, lat, lon, name='', note='', request_id=''):
        import fcntl,re
        if not isinstance(name,str) or len(name)>160: raise ValueError('Name must be under 160 characters.')
        if not isinstance(note,str) or len(note)>2000: raise ValueError('Notes must be under 2,000 characters.')
        if not isinstance(request_id,str) or not re.fullmatch(r'[a-zA-Z0-9-]{16,80}',request_id): raise ValueError('Invalid save request. Reopen Add candidate.')
        if isinstance(lat,bool) or isinstance(lon,bool): raise ValueError('Invalid coordinates.')
        lat,lon=float(lat),float(lon)
        if not math.isfinite(lat+lon) or not (-85<lat<85 and -180<=lon<=180): raise ValueError('Invalid coordinates.')
        if self.validator and not self.validator(lat,lon): raise ValueError('This point is outside the CA-17 district boundary (119th Congress). Choose a location inside the district.')
        with self.lock, (self.root/'.canopy-editor.lock').open('a') as lockfile:
            fcntl.flock(lockfile,fcntl.LOCK_EX)
            original_bytes=self.path.read_bytes();data=json.loads(original_bytes);rows=records(data)
            for index,existing in enumerate(rows,1):
                created=fields(existing).get('canopy_editor_created',{})
                if created.get('request_id')==request_id:
                    if created.get('location')!=[lat,lon] or created.get('name')!=name.strip() or created.get('note')!=note:
                        raise Conflict('This save request already created a different point. Close and reopen Add candidate.')
                    audit=next(a for a in self.audits(rows) if a['candidate_id']==identity(existing,index))
                    return self.entry(audit,self.index(rows))
                try: nearby=distance(point(existing),(lat,lon))<1
                except (ValueError,TypeError,KeyError): nearby=False
                if nearby and fields(existing).get('canopy_manual_review',{}).get('decision')!='exclude':
                    raise ValueError('A candidate already exists within 1 m. Review that candidate or choose a different point.')
            key='MANUAL-'+secrets.token_hex(8).upper()
            now=datetime.now(timezone.utc).isoformat()
            row={'site_id':key,'candidate_id':key,'latitude':lat,'longitude':lon,
                 'area_name':name.strip() or 'Manually added candidate','site_type':'manually selected planting opportunity',
                 'source_type':'manual_canopy_editor','planting_approved':False,
                 'why_chosen':'User-selected planting candidate; requires location-specific verification.',
                 'screening_status':'Manually added planning candidate; field/city verification required.',
                 'evidence_confidence':'limited; manual location selection',
                 'canopy_editor_created':{'at':now,'location':[lat,lon],'name':name.strip(),'note':note,'request_id':request_id},
                 'canopy_manual_review':{'decision':'added','at':now,'note':note,'location':[lat,lon],'reviewer':'local manual review','baseline_group':'manually_added'}}
            row['canopy_recheck']=self.checker(row) if self.checker else {'status':'unavailable','reason':'Local canopy recheck unavailable.'}
            if isinstance(data,dict) and 'features' in data and records(data) is data['features']:
                row={'type':'Feature','properties':row,'geometry':{'type':'Point','coordinates':[lon,lat]}}
            # Undo an addition by excluding it, keeping its identity/history recoverable.
            undo=copy.deepcopy(row)
            fields(undo)['canopy_manual_review']={'decision':'exclude','at':now,'note':'Addition undone. Use Keep to restore.','location':[lat,lon]}
            fields(row)['canopy_review_undo']=undo
            fields(row)['canopy_review_history']=[{'action':'add','at':now,'note':note,'from':None,'to':[lat,lon]}]
            rows.append(row);self.commit(original_bytes,data)
            audit=next(a for a in self.audits(rows) if a['candidate_id']==key)
            return self.entry(audit,self.index(rows))

    def map_points(self, indexed):
        points=[]
        for key,item in indexed.items():
            if item is None:continue
            _,row=item
            if fields(row).get('canopy_manual_review',{}).get('decision')=='exclude':continue
            try:lat,lon=point(row)
            except (ValueError,TypeError,KeyError):continue
            points.append({'candidate_id':key,'latitude':lat,'longitude':lon})
        return points

    def detail(self, key):
        with self.lock:
            rows=records(json.loads(self.path.read_text()))
            audit=next((a for a in self.audits(rows) if a['candidate_id']==key),None)
            if audit is None:raise ValueError('Candidate not found.')
            return self.entry(audit,self.index(rows))

    def listing(self):
        with self.lock:
            stamp=self.path.stat();key=(stamp.st_mtime_ns,stamp.st_size)
            if self._listing_cache_key==key and self._listing_cache is not None:return self._listing_cache
            rows=records(json.loads(self.path.read_text()))
            indexed = self.index(rows)
            result=dict(version=VERSION, generation=self.generation, map_points=self.map_points(indexed),
                        rows=[self.entry(a,indexed,compact=True) for a in self.audits(rows)],
                        backup=str(self.backup) if self.backup else None,
                        transmission_config=self.transmission.config if self.transmission else None)
            self._listing_cache_key=key;self._listing_cache=result
            return result

    def update(self, key, expected, action, lat=None, lon=None, note=''):
        import fcntl
        if action not in ('keep','exclude','relocate','undo'): raise ValueError('Unknown action.')
        if not isinstance(note, str) or len(note)>2000: raise ValueError('Notes must be under 2,000 characters.')
        with self.lock, (self.root/'.canopy-editor.lock').open('a') as lockfile:
            fcntl.flock(lockfile, fcntl.LOCK_EX)
            original_bytes = self.path.read_bytes()
            data = json.loads(original_bytes)
            rows = records(data)
            indexed = self.index(rows)
            audit = next((a for a in self.audits(rows) if a['candidate_id']==key),None)
            if audit is None: raise ValueError('Candidate is not in this review queue.')
            item = indexed.get(key)
            if item is None: raise Conflict('Candidate is missing or has a duplicate ID. Refresh the list.')
            index, row = item
            if revision(row)!=expected: raise Conflict('This candidate changed since it was opened. Refresh before editing.')
            before = copy.deepcopy(row)
            p = fields(row)
            now = datetime.now(timezone.utc).isoformat()
            history = copy.deepcopy(p.get('canopy_review_history', []))
            if not isinstance(history, list): raise ValueError('Unexpected review history format; no changes made.')
            old_point = point(row)
            if action == 'undo':
                snapshot = p.get('canopy_review_undo')
                if not isinstance(snapshot, dict): raise ValueError('No previous action to undo.')
                row = copy.deepcopy(snapshot)
                rows[index-1] = row
                p = fields(row)
            else:
                snapshot = copy.deepcopy(before)
                fields(snapshot).pop('canopy_review_undo', None)
                fields(snapshot).pop('canopy_review_history', None)
                p['canopy_review_undo'] = snapshot
                if action == 'relocate':
                    if isinstance(lat, bool) or isinstance(lon, bool): raise ValueError('Invalid coordinates.')
                    lat, lon = float(lat), float(lon)
                    if not math.isfinite(lat+lon) or not (-85<lat<85 and -180<=lon<=180): raise ValueError('Invalid coordinates.')
                    if distance(old_point,(lat,lon))>500: raise ValueError('Choose a point within 500 m of this candidate.')
                    if self.validator and not self.validator(lat,lon): raise ValueError('This point is outside the CA-17 district boundary (119th Congress). Choose a location inside the district.')
                    # Update every existing alias, plus canonical fields used by EcoTrace.
                    for target in [p] + ([row] if p is not row else []):
                        target['latitude'],target['longitude']=lat,lon
                        for name,value in [('lat',lat),('lon',lon),('lng',lon)]:
                            if name in target: target[name]=value
                    if isinstance(row.get('geometry'),dict) and row['geometry'].get('type')=='Point':
                        row['geometry']['coordinates']=[lon,lat]
                    # Old spatial evidence is retained in the snapshot/backup, not attributed to the new point.
                    for field in ('nearest_road_m','nearest_path_m','nearest_existing_tree_m','wri_canopy_fraction_10m',
                                  'wri_max_canopy_height_m','county_canopy_percent','soil_context','parcel_context'):
                        if field in p: p[field]=None
                    p.pop('surface_heat',None)
                    p['planting_approved']=False
                    p['why_chosen']='User-selected replacement coordinate; planning candidate requiring location-specific verification.'
                    p['spatial_evidence_status']='Recheck required after manual relocation'
                    p['screening_status']='Manually relocated planning candidate; location-specific screening must be rechecked.'
                    p['canopy_recheck'] = self.checker(row) if self.checker else {'status':'unavailable','reason':'Local canopy recheck unavailable.'}
                p['canopy_manual_review']={'decision':action,'at':now,'note':note,'location':list(point(row)),
                                            'reviewer':'local manual review','baseline_group':audit['status']}
            history.append({'action':action,'at':now,'note':note,'from':list(old_point),'to':list(point(row))})
            p['canopy_review_history']=history
            self.commit(original_bytes,data)
            return self.entry(audit,self.index(rows))


def install(app, namespace, root):
    from fastapi import Request, HTTPException
    from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
    from starlette.concurrency import run_in_threadpool
    from starlette.middleware.gzip import GZipMiddleware
    # FastAPI resolves annotations against module globals.
    globals()['Request']=Request
    if getattr(app.state,'canopy_editor',None): return
    root=Path(root)
    token=secrets.token_urlsafe(32)
    from transmission import Screening
    screening=Screening(root)
    from planting_area import contains as inside_district
    original_normalize=namespace['_normalize_authoritative_site']
    def normalize(record,index):
        if fields(record).get('canopy_manual_review',{}).get('decision')=='exclude' or screening.blocked(record): return None
        normalized_input={**fields(record),**record}
        # The original normalizer already honors inside_ca17; derive it from the real polygon.
        lat,lon=point(record);b=namespace['DISTRICT_BOUNDS']
        in_old_box=b['min_lat']<=lat<=b['max_lat'] and b['min_lon']<=lon<=b['max_lon']
        if not in_old_box and not normalized_input.get('inside_ca17') and inside_district(lat,lon):
            normalized_input['inside_ca17']=True
        return original_normalize(normalized_input,index)
    namespace['_normalize_authoritative_site']=normalize
    def validate(lat,lon):
        return inside_district(lat,lon)
    raster_state={}
    def recheck(row):
        try:
            if 'rasters' not in raster_state:
                from ecotrace_gis_runtime import Raster
                manifest=json.loads((root/'ecotrace_gis_manifest.json').read_text())
                raster_state['rasters']=[Raster(e) for e in manifest['rasters'] if e['kind']=='cover']
                if not raster_state['rasters']: raise ValueError('No canopy layers configured.')
            from check_candidate_canopy import screen
            result=screen([row],raster_state['rasters'],progress=False)[0]
            return {k:result[k] for k in ('status','canopy_at_candidate','canopy_grid_hits_3m','canopy_sources')}
        except Exception as exc:
            return {'status':'unavailable','reason':str(exc)[:200]}
    store=Store(root,validator=validate,checker=recheck)
    store.transmission=screening
    app.state.canopy_editor=store
    app.add_middleware(GZipMiddleware,minimum_size=1500,compresslevel=3)
    def refresh(rebuild_priority=True):
        from priority_sync import sync
        if rebuild_priority:sync(root)
        # Same normalizer as the app, without its non-empty startup requirement.
        rows=records(json.loads(store.path.read_text()))
        namespace['PLANTING_SITES']=[s for i,r in enumerate(rows,1) if (s:=normalize(r,i)) is not None]
        # Coordinate-keyed geocoding caches remain valid; priority_sync clears only its own cache.
    refresh(rebuild_priority=False)
    def local(request):
        if not request.client or request.client.host not in ('127.0.0.1','::1','testclient'):
            raise HTTPException(403,'Review editing is available only on this computer.')
        if request.url.hostname not in ('127.0.0.1','localhost','::1','testserver'):
            raise HTTPException(403,'Use the local EcoTrace address.')
    @app.get('/api/canopy-review/health')
    async def editor_health(request:Request):
        local(request)
        return {'version':VERSION,'status':'ready','generation':store.generation}
    @app.get('/canopy-review',response_class=HTMLResponse)
    def page(request:Request):
        local(request)
        return HTMLResponse((HERE/'review.html').read_text().replace('__TOKEN__',token),headers={'Cache-Control':'no-store'})
    @app.get('/api/canopy-review/visual-image/{index}')
    def visual_image(request:Request,index:int):
        local(request)
        if not 0<=index<1578:raise HTTPException(404,'Review image not found.')
        path=HERE/'visual_report'/'images'/f'{index:04d}.jpg'
        if not path.is_file():raise HTTPException(404,'Review image not found.')
        return FileResponse(path,media_type='image/jpeg',headers={'Cache-Control':'private,max-age=3600'})
    @app.get('/api/canopy-review')
    def listing(request:Request):
        local(request)
        return JSONResponse(store.listing(),headers={'Cache-Control':'no-store'})
    @app.get('/api/canopy-review/transmission-lines')
    def transmission_lines(request:Request):
        local(request)
        return JSONResponse(screening.data.geojson,headers={'Cache-Control':'private,max-age=3600'})
    @app.get('/api/canopy-review/details')
    def details(request:Request,id:str):
        local(request)
        try:return JSONResponse(store.detail(id),headers={'Cache-Control':'no-store'})
        except ValueError as exc:raise HTTPException(404,str(exc))
    def save_and_refresh(body):
        # Blocking disk/raster work stays on one worker thread, with saves serialized.
        with store.lock:
            if body.get('action')=='transmission_config':
                screening.save(body)
                store._listing_cache=None
                store.generation+=1
                refresh()
                return {'saved':True,'config':screening.config}
            if body.get('action')=='add':
                result=store.add(body.get('latitude'),body.get('longitude'),body.get('name',''),body.get('note',''),body.get('request_id',''))
            else:
                result=store.update(body.get('id'),body.get('revision'),body.get('action'),body.get('latitude'),body.get('longitude'),body.get('note',''))
            refresh()
        return {'saved':True,'entry':result,'generation':store.generation}
    @app.post('/api/canopy-review')
    async def update(request:Request):
        local(request)
        if not secrets.compare_digest(request.headers.get('x-canopy-token',''),token): raise HTTPException(403,'Reopen the review page before saving.')
        if request.headers.get('origin') and request.headers['origin']!=str(request.base_url).rstrip('/'):
            raise HTTPException(403,'Use the local review page.')
        if len(await request.body())>12000: raise HTTPException(413,'Request is too large.')
        try:
            body=await request.json()
            if not isinstance(body,dict): raise ValueError('Expected an object.')
            return await run_in_threadpool(save_and_refresh,body)
        except Conflict as exc: raise HTTPException(409,str(exc))
        except (ValueError,TypeError,KeyError) as exc: raise HTTPException(422,str(exc))
    print('Canopy review ready: /canopy-review — '+str(len(store.queue))+' candidates',flush=True)
