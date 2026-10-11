"""Consistent Pacific-time and nighttime semantics for EcoTrace route models."""
from datetime import datetime
from zoneinfo import ZoneInfo
from canopy_engine import solar_position
PACIFIC=ZoneInfo('America/Los_Angeles')
NIGHT_NOTE=('The Sun is below the modeled horizon at the selected time. '
    'Direct solar exposure is zero, so a daylight shade score is not applicable. '
    'Tree and building shadow contributions are not counts of nearby objects.')

def departure_time(value,use_time_of_day):
    if not use_time_of_day:return datetime.now(PACIFIC).replace(hour=12,minute=0,second=0,microsecond=0)
    try:moment=datetime.fromisoformat(value) if value else datetime.now(PACIFIC)
    except (ValueError,TypeError):moment=datetime.now(PACIFIC)
    return moment.replace(tzinfo=PACIFIC) if moment.tzinfo is None else moment.astimezone(PACIFIC)

def night_segment(a,b,departure,use_time_of_day):
    if not use_time_of_day:return None
    lat=(a[1]+b[1])/2;lon=(a[0]+b[0])/2
    altitude,azimuth=solar_position(lat,lon,departure)
    if altitude>0:return None
    return dict(coordinates=[a,b],category='nighttime',shade_percent=0.0,
        shade_applicable=False,solar_altitude_degrees=round(altitude,2),
        solar_azimuth_degrees=round(azimuth,2),reason=NIGHT_NOTE,
        data_quality='Nighttime solar-position result; shadow scoring not applicable')

def annotate_route(result,departure,use_time_of_day,bike=False):
    coords=result.fastest_coordinates if bike else result.shortest_coordinates
    nighttime=bool(use_time_of_day and len(coords)>1 and all(
        solar_position((a[1]+b[1])/2,(a[0]+b[0])/2,departure)[0]<=0
        for a,b in zip(coords,coords[1:])))
    result.nighttime=nighttime;result.shade_applicable=not nighttime
    result.lighting_note=NIGHT_NOTE if nighttime else (
        'Shade score: 0 means low modeled shade; 100 means high modeled shade. '
        'Tree and building figures describe shadow contributions, not object counts.'
        + (' Ignore time uses a neutral comparison rather than the actual Sun position.' if not use_time_of_day else ''))
    if not nighttime:return result
    # Zero remains an internal ranking value for old clients. The applicability
    # flag and updated interface prevent it being presented as a daylight score.
    if bike:
        result.meaningful_cooler_route=False
        result.fastest_why=f'This route is {result.fastest_distance_miles:.2f} miles and {result.fastest_minutes:.1f} minutes. '+NIGHT_NOTE
        result.cooler_why=NIGHT_NOTE
        result.warning=' '.join(filter(None,[result.warning,'Nighttime: use distance, travel time and bike infrastructure to compare routes.']))
    else:
        result.meaningful_shadier_route=False
        result.shortest_route_why=f'This route is {result.shortest_distance_miles:.2f} miles and {result.shortest_minutes:.1f} minutes. '+NIGHT_NOTE
        result.shadiest_route_why=NIGHT_NOTE
        result.message='Nighttime route: the shortest returned route is shown. '+NIGHT_NOTE
    return result
