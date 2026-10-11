"""Reuse only fresh, complete nearby NWS observations for walking comfort."""
from datetime import datetime,timezone
import math,time

def field(value,key):
    return value.get(key) if isinstance(value,dict) else getattr(value,key,None)

def fresh(snapshot,now=None):
    try:
        if not field(snapshot,'available'):return False
        for name in ('temperature_f','relative_humidity_percent','wind_speed_mph'):
            n=field(snapshot,name)
            if n is None or isinstance(n,bool) or not math.isfinite(float(n)):return False
        at=datetime.fromisoformat(field(snapshot,'observed_at').replace('Z','+00:00'))
        if at.tzinfo is None:return False
        age=((now or datetime.now(timezone.utc))-at).total_seconds()
        return -900<=age<=10800
    except (TypeError,ValueError,AttributeError):return False

def distance_km(a,b):
    p,q=map(math.radians,(a[0],b[0]))
    dlat=q-p;dlon=math.radians(b[1]-a[1])
    h=math.sin(dlat/2)**2+math.cos(p)*math.cos(q)*math.sin(dlon/2)**2
    return 12742*math.asin(min(1,math.sqrt(h)))

def install(namespace):
    if namespace.get('ECOTRACE_WEATHER_REPAIR'):return
    original=namespace['fetch_nws_weather']
    cache=namespace['_NWS_WEATHER_CACHE']
    def repaired(latitude,longitude):
        key=(round(latitude,3),round(longitude,3))
        entry=cache.get(key)
        # A failed NWS request must not hide recovered weather for five minutes.
        if entry and not fresh(entry[1]) and time.time()-entry[0]>=30:
            cache.pop(key,None)
        snapshot=original(latitude,longitude)
        if fresh(snapshot):return snapshot
        candidates=[(distance_km(key,point),row[1]) for point,row in list(cache.items())
                    if isinstance(point,tuple) and len(point)==2 and fresh(row[1])]
        candidates=[row for row in candidates if row[0]<=15]
        if candidates:
            return min(candidates,key=lambda row:row[0])[1]
        return snapshot
    namespace['fetch_nws_weather']=repaired
    namespace['ECOTRACE_WEATHER_REPAIR']='weather-repair-1'
