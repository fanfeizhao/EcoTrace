"""Experimental walking comfort. Scenario estimates, not observed sidewalk MRT."""
from __future__ import annotations
import ast
import math
from pathlib import Path
from datetime import datetime, timezone
from functools import lru_cache

VERSION = 'walking-comfort-3'
ROOT = Path(__file__).resolve().parent

@lru_cache(maxsize=1)
def models():
    from pythermalcomfort.models import solar_gain, utci
    # Warm up the compiled UTCI implementation before the first route request.
    utci(tdb=25, tr=25, v=1, rh=50)
    return solar_gain, utci

def number(value):
    if value is None or isinstance(value, bool):
        raise ValueError('Missing numeric input')
    n = float(value)
    if not math.isfinite(n):
        raise ValueError('Non-finite input')
    return n

def item(value, name, default=None):
    return value.get(name, default) if isinstance(value, dict) else getattr(value, name, default)

def distance_bearing(a, b):
    lon1, lat1 = map(math.radians, map(number, a[:2]))
    lon2, lat2 = map(math.radians, map(number, b[:2]))
    dlat, dlon = lat2-lat1, lon2-lon1
    h = math.sin(dlat/2)**2 + math.cos(lat1)*math.cos(lat2)*math.sin(dlon/2)**2
    d = 12742000 * math.asin(math.sqrt(min(1.0, max(0.0, h))))
    bearing = math.degrees(math.atan2(math.sin(dlon)*math.cos(lat2),
        math.cos(lat1)*math.sin(lat2)-math.sin(lat1)*math.cos(lat2)*math.cos(dlon))) % 360
    return d, bearing

def category(c):
    for upper, label in [(-40,'Extreme cold stress'),(-27,'Very strong cold stress'),
        (-13,'Strong cold stress'),(0,'Moderate cold stress'),(9,'Slight cold stress'),
        (26,'No thermal stress'),(32,'Moderate heat stress'),(38,'Strong heat stress'),
        (46,'Very strong heat stress')]:
        if c < upper: return label
    return 'Extreme heat stress'

def route_summary(segments, minutes, tdb, rh, wind):
    solar_gain, utci = models()
    lengths, alts, angles, exposure = [], [], [], []
    for segment in segments:
        coords = item(segment, 'coordinates')
        if not coords or len(coords) != 2: raise ValueError('Invalid segment geometry')
        length, heading = distance_bearing(*coords)
        if length < 0.01: continue
        altitude = number(item(segment, 'solar_altitude_degrees'))
        if not 5 <= altitude <= 90:
            raise ValueError('Choose a daytime departure with the sun at least 5 degrees above the horizon.')
        azimuth = number(item(segment, 'solar_azimuth_degrees'))
        tree = number(item(segment, 'canopy_shadow_percent')) / 100
        building = number(item(segment, 'building_shadow_percent')) / 100
        if not 0 <= tree <= 1 or not 0 <= building <= 1:
            raise ValueError('Invalid shade inputs')
        lengths.append(length); alts.append(altitude)
        angles.append(abs((azimuth-heading+180) % 360-180))
        # Use the components, not the heat-penalized 0–100 route score.
        exposure.append((1-tree)*(1-building))
    if not lengths: raise ValueError('No route segments available')
    # Standard SolarCal implementation with explicit sunny-scenario assumptions.
    # We have no measured street radiation, sky view, or surface temperatures.
    gain = solar_gain(sol_altitude=alts, sharp=angles, sol_radiation_dir=800,
        sol_transmittance=1, f_svv=0.5, f_bes=exposure,
        asw=0.7, posture='standing', floor_reflectance=0.2, round_output=False)
    radiant = [tdb + float(v) for v in gain.delta_mrt]
    values = utci(tdb=tdb, tr=radiant, v=wind, rh=rh,
                  limit_inputs=True, round_output=False).utci
    values = [number(v) for v in values]
    radiant = [number(v) for v in radiant]
    total = sum(lengths)
    avg = sum(v*d for v,d in zip(values,lengths))/total
    mrt = sum(v*d for v,d in zip(radiant,lengths))/total
    peak_index = max(range(len(values)), key=values.__getitem__)
    minutes = number(minutes)
    if minutes <= 0: raise ValueError('Invalid trip duration')
    # Equal pace makes distance weights equivalent to time weights.
    hot_minutes = minutes * sum(d for v,d in zip(values,lengths) if v>=32)/total
    return {'mean_utci_c':round(avg,1), 'mean_utci_f':round(avg*1.8+32,1),
        'mean_mrt_c':round(mrt,1), 'mean_mrt_f':round(mrt*1.8+32,1),
        'peak_utci_c':round(values[peak_index],1),
        'peak_utci_f':round(values[peak_index]*1.8+32,1),
        'mean_category':category(avg), 'minutes_utci_ge_32c':round(hot_minutes,1),
        'trip_minutes':minutes, 'sample_count':len(values)}


def modeled_exposure(segments, minutes, heat_model, shade_available=True):
    rows=[]
    minutes=number(minutes)
    if minutes<=0:raise ValueError('Invalid trip duration')
    for segment in segments:
        coords=item(segment,'coordinates')
        if not coords or len(coords)!=2:raise ValueError('Invalid segment geometry')
        length,_=distance_bearing(*coords)
        if length<0.01:continue
        lat=(number(coords[0][1])+number(coords[1][1]))/2
        lon=(number(coords[0][0])+number(coords[1][0]))/2
        heat,temperature,_=heat_model(lat,lon)
        heat=number(heat);temperature=number(temperature)
        if not 0<=heat<=100:raise ValueError('Invalid modeled heat score')
        sun=1.0
        for key in ('canopy_shadow_percent','building_shadow_percent','terrain_shadow_percent'):
            shadow=number(item(segment,key,0))/100
            if not 0<=shadow<=1:raise ValueError('Invalid shade fraction')
            sun*=1-shadow
        altitude=item(segment,'solar_altitude_degrees')
        nighttime=item(segment,'shade_applicable',True) is False or (altitude is not None and number(altitude)<=0)
        if nighttime:sun=0.0
        rows.append((length,heat,temperature,sun,nighttime))
    if not rows:raise ValueError('No route samples')
    total=sum(row[0] for row in rows)
    mean=lambda index:sum(row[0]*row[index] for row in rows)/total
    night=sum(row[0] for row in rows if row[4])/total
    return {'mean_heat_score':round(mean(1),1),'peak_heat_score':round(max(row[1] for row in rows),1),
            'model_baseline_temperature_f':round(mean(2),1),
            'sun_minutes':round(minutes*mean(3),1) if shade_available else None,'shade_minutes':round(max(0,minutes*(1-night-mean(3))),1) if shade_available else None,
            'night_minutes':round(minutes*night,1) if shade_available else None,'trip_minutes':minutes,'sample_count':len(rows)}

def walking_comfort(shortest, shadiest, shortest_minutes, shadiest_minutes,
                    weather, use_time_of_day, canopy_available, heat_model=None):
    base = {'version':VERSION, 'available':False}
    try:
        if not use_time_of_day:
            raise ValueError('Enable departure-time shade to compare thermal conditions.')
        if not canopy_available:
            raise ValueError('The mapped tree/building shade model is unavailable.')
        if not item(weather,'available',False):
            raise ValueError('Nearby weather is unavailable; shade comparison still works.')
        tdb = (number(item(weather,'temperature_f'))-32)/1.8
        rh = number(item(weather,'relative_humidity_percent'))
        speed = number(item(weather,'wind_speed_mph'))*0.44704
        if not -50 < tdb < 50 or not 0 <= rh <= 100 or not 0 <= speed < 17:
            raise ValueError('Weather is outside the supported UTCI input range.')
        observed = item(weather,'observed_at')
        if not observed: raise ValueError('Weather observation time is missing.')
        at = datetime.fromisoformat(observed.replace('Z','+00:00'))
        if at.tzinfo is None: raise ValueError('Weather observation time has no timezone.')
        age = (datetime.now(timezone.utc)-at).total_seconds()
        if age > 10800 or age < -900:
            raise ValueError('Nearby weather is more than 3 hours old or has an invalid timestamp.')
        wind = max(0.5, speed)
        a = route_summary(shortest,shortest_minutes,tdb,rh,wind)
        b = route_summary(shadiest,shadiest_minutes,tdb,rh,wind)
        return {**base, 'available':True, 'mode':'utci', 'shortest':a, 'shadiest':b,
            'observation_time':observed,
            'station':str(item(weather,'observation_station') or 'Nearby NWS station'),
            'air_temperature_c':round(tdb,1), 'relative_humidity_percent':rh,
            'wind_ms_used':round(wind,2), 'wind_floor_applied':speed<0.5,
            'assumptions':'Sunny scenario: 800 W/m² direct beam; sky-view fraction 0.5; ground reflectance 0.2; absorptivity 0.7; background radiant temperature equals air temperature. Current nearby weather held constant; shade at selected departure held constant through the trip. Not a forecast or measured sidewalk temperature.'}
    except (ValueError,TypeError,KeyError,AttributeError,ImportError,OverflowError) as exc:
        # Thermal inputs must never make an otherwise valid route fail.
        if isinstance(exc, ImportError):
            reason='Thermal-comfort dependency unavailable; run the update installer with the app Python.'
        elif str(exc) in ('Missing numeric input','Non-finite input'):
            reason='Temperature, humidity, wind, or shade inputs are missing. No substitute weather was invented.'
        else: reason=str(exc)
        if callable(heat_model):
            try:
                return {'version':VERSION,'available':True,'mode':'modeled_exposure',
                    'shortest':modeled_exposure(shortest,shortest_minutes,heat_model,canopy_available),
                    'shadiest':modeled_exposure(shadiest,shadiest_minutes,heat_model,canopy_available),
                    'time_of_day_used':bool(use_time_of_day),
                    'assumptions':'Uses the existing heat-map model sampled at route-segment midpoints. Sun/shade time uses mapped shadow components and a constant walking pace. Model baselines are not current weather or body temperature.'}
            except (ValueError,TypeError,KeyError,AttributeError,OverflowError):pass
        return {**base,'reason':reason}

def replace_once(source, old, new):
    if source.count(old) != 1:
        raise ValueError('App version differs at a required walking/weather anchor; nothing changed: '+old[:70])
    return source.replace(old,new,1)

def transform(source):
    if 'ecotrace_walking_comfort' in source:
        raise ValueError('Walking comfort already present in source; avoid installing twice.')
    source=replace_once(source,'import sqlite3','import sqlite3\nfrom ecotrace_walking_comfort import walking_comfort')
    source=replace_once(source,'class RouteAnalysis(BaseModel):','class RouteAnalysis(BaseModel):\n    thermal_comfort: dict[str, Any] | None = None')
    source=replace_once(source,'class WeatherSnapshot(BaseModel):','class WeatherSnapshot(BaseModel):\n    relative_humidity_percent: float | None = None')
    source=replace_once(source,'        wind_mph = _qv_to_mph(observation_props.get("windSpeed"))',
        '        humidity_percent = (observation_props.get("relativeHumidity") or {}).get("value")\n        wind_mph = _qv_to_mph(observation_props.get("windSpeed"))')
    source=replace_once(source,'            apparent_temperature_f=round(apparent_f, 1) if apparent_f is not None else None,',
        '            relative_humidity_percent=humidity_percent,\n            apparent_temperature_f=round(apparent_f, 1) if apparent_f is not None else None,')
    source=replace_once(source,'RouteAnalysis(\n        shortest_coordinates=baseline["coordinates"],',
        'RouteAnalysis(\n        thermal_comfort=walking_comfort(shortest_segment_results, shadiest_segment_results, shortest_minutes, shadiest_minutes, weather, use_time_of_day, CANOPY_ENGINE.available, interpolate_heat),\n        shortest_coordinates=baseline["coordinates"],')
    source=replace_once(source,'async function requestRoute(){',(ROOT/'walking_comfort_ui.js').read_text()+'\nasync function requestRoute(){')
    source=replace_once(source,"  const walkPlantingButton=document.getElementById('walk-route-planting-toggle');",
        "  walkingComparison.insertAdjacentHTML('beforeend', walkingComfortHtml(data));\n  const walkPlantingButton=document.getElementById('walk-route-planting-toggle');")
    about='<p class="mt-2 text-xs leading-5 text-slate-300"><strong>Experimental walking comfort:</strong> With complete nearby weather, SolarCal estimates mean radiant temperature (MRT), then UTCI combines it with NWS air temperature, humidity and wind. Without those inputs, EcoTrace uses the existing heat-map model at each route-segment midpoint and estimates sun/shade minutes from mapped shadow components. Heat scores are weighted by segment length; at constant pace this equals weighting by time. A sunny scenario is assumed because street radiation is not measured. Results show route-average UTCI, the hottest sampled segment, and estimated minutes at UTCI ≥32°C. Departure-time shade and current weather are held fixed during the trip; other departure times are scenarios, not forecasts. UTCI represents a standard walking person, not individual body temperature or an age/fitness-specific prediction. Route choice still uses shade and detour limits. The heat map’s existing NWS “feels like” is heat index/wind chill, separate from UTCI.</p>'
    import re
    pattern=r'(<summary\b[^>]*>[^<]*Walking Routes</summary>)'
    if len(re.findall(pattern,source))!=1: raise ValueError('Walking About section not found; nothing changed.')
    source=re.sub(pattern,lambda m:m.group(1)+about,source,count=1)
    about_pattern=r'(<div\b[^>]*id="guide-about"[^>]*>)'
    if len(re.findall(about_pattern,source))!=1: raise ValueError('About panel not found; nothing changed.')
    about_note='<p class="rounded-xl bg-orange-400/10 p-3 text-xs leading-5 text-slate-300"><strong>Walking comfort:</strong> Compare shade alongside experimental radiant-temperature (MRT) and UTCI estimates. When nearby weather is available, these use a sunny scenario. Otherwise, heat-map scores and estimated sun/shade minutes describe modeled exposure. Open Data &amp; Methods → Walking Routes for assumptions.</p>'
    source=re.sub(about_pattern,lambda m:m.group(1)+about_note,source,count=1)
    source=source.replace('return HTMLResponse(page)', 'return HTMLResponse(page, headers={\"Cache-Control\": \"no-store\"})')
    compile(source,'EcoTrace walking comfort','exec')
    return source

def rebuild(namespace):
    from pydantic import BaseModel
    for value in list(namespace.values()):
        if isinstance(value,type) and value is not BaseModel and issubclass(value,BaseModel) and value.__module__==namespace.get('__name__'):
            value.model_rebuild(force=True,_types_namespace=namespace)

def prepare_source(root):
    source=(root/'ecotrace_final_input_polish_v6149.py').read_text()
    if (root/'ecotrace_priority_v3.py').exists():
        from ecotrace_priority_runtime import validate, transform as priority_transform
        validate(root)
        source=priority_transform(source)
    source=transform(source)
    from ecotrace_event_archive import transform_ui
    source=transform_ui(source)
    from ecotrace_route_compare import transform as transform_route_comparison
    source=transform_route_comparison(source)
    guards=[n for n in ast.parse(source).body if isinstance(n,ast.If) and isinstance(n.test,ast.Compare) and isinstance(n.test.left,ast.Name) and n.test.left.id=='__name__']
    if len(guards)!=1: raise ValueError('Cannot identify app startup; nothing changed.')
    lines=source.splitlines(keepends=True)
    lines.insert(guards[0].lineno-1,'\nfrom ecotrace_walking_comfort import rebuild\nrebuild(globals())\nfrom ecotrace_weather_repair import install as install_weather_repair\ninstall_weather_repair(globals())\nfrom ecotrace_event_archive import install as install_event_archiving\ninstall_event_archiving(app)\n')
    source=''.join(lines)
    source=source.replace('return HTMLResponse(page)', 'return HTMLResponse(page, headers={\"Cache-Control\": \"no-store\"})')
    compile(source,'EcoTrace walking comfort','exec')
    return source
