"""Bounded ORS lookups with a real basic-route fallback and cached geometry."""
import copy,hashlib,json,threading,time
from concurrent.futures import ThreadPoolExecutor,TimeoutError
import requests,certifi
_POOL=ThreadPoolExecutor(max_workers=4,thread_name_prefix='ecotrace-routing')
_LOCK=threading.Lock();_CACHE={};_PENDING={}

def _request(api_key,profile,start,end,alternative):
    payload={'coordinates':[[start[1],start[0]],[end[1],end[0]]],'instructions':False,'geometry':True,'preference':'recommended','radiuses':[350,350]}
    if alternative:payload['alternative_routes']={'target_count':2,'weight_factor':1.4,'share_factor':0.65}
    try:
        r=requests.post(f'https://api.openrouteservice.org/v2/directions/{profile}/geojson',headers={'Authorization':api_key,'Content-Type':'application/json'},json=payload,timeout=(3,4 if alternative else 7),verify=certifi.where())
        if r.status_code in [401,403]:raise RuntimeError('OpenRouteService rejected the API key. Check ORS_API_KEY and restart EcoTrace.')
        if r.status_code==429:raise RuntimeError('The routing service rate limit was reached. Wait briefly, then retry.')
        r.raise_for_status();data=r.json()
        if not isinstance(data,dict):raise ValueError('Invalid route response')
        return data
    except RuntimeError:raise
    except (requests.RequestException,ValueError) as e:raise RuntimeError('The routing service could not return a route. Try again or choose a nearby mapped entrance.') from e

def _submit(api_key,profile,start,end,alternative):
    key=hashlib.sha256(json.dumps([api_key,profile,start,end,alternative]).encode()).hexdigest()
    with _LOCK:
        cached=_CACHE.get(key)
        if cached and time.monotonic()-cached[0]<600:return None,copy.deepcopy(cached[1])
        if key in _PENDING:return _PENDING[key],None
        if len(_PENDING)>=4:raise RuntimeError('Routing is busy. Please retry after the current requests finish.')
        future=_POOL.submit(_request,api_key,profile,start,end,alternative);_PENDING[key]=future
    def completed(f):
        with _LOCK:
            _PENDING.pop(key,None)
            if not f.cancelled() and f.exception() is None:
                _CACHE[key]=(time.monotonic(),f.result())
                while len(_CACHE)>128:_CACHE.pop(next(iter(_CACHE)))
    future.add_done_callback(completed);return future,None

def fetch_route_candidates(api_key,profile,start,end,parse,alternatives=True):
    deadline=time.monotonic()+12
    basic=_submit(api_key,profile,start,end,False);alternate=None;note=''
    if alternatives:
        try:alternate=_submit(api_key,profile,start,end,True)
        except RuntimeError:note='Alternative-route search is busy; the available basic route is shown.'
    def read(job,seconds):
        f,cached=job;return parse(copy.deepcopy(cached if f is None else f.result(timeout=max(.01,seconds))))
    candidates=[];error=None
    try:candidates=read(basic,10)
    except (RuntimeError,ValueError,TypeError,KeyError,AttributeError,TimeoutError) as e:error=e
    if alternate:
        try:
            extra=read(alternate,max(.01,deadline-time.monotonic()))
            for candidate in extra:
                if not any(candidate['coordinates']==s['coordinates'] for s in candidates):candidates.append(candidate)
        except (RuntimeError,ValueError,TypeError,KeyError,AttributeError,TimeoutError):note='Alternative-route search was unavailable; the available basic route is shown.'
    if not candidates:
        if isinstance(error,RuntimeError):raise error
        raise RuntimeError('Routing timed out or returned no usable route. Please retry or select a nearby mapped entrance.')
    return candidates,note
