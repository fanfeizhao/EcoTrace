"""Offline location labels and recorded evidence for EcoTrace tree candidates."""
from functools import lru_cache
from pathlib import Path
import json
import math
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

router = APIRouter()
ROOT = Path(__file__).resolve().parent

@lru_cache(maxsize=1)
def tree_data():
    data = json.loads((ROOT / 'ecotrace_reviewed_tree_sites.json').read_text(encoding='utf-8'))
    sites = data['sites']
    if len(sites) != data['metadata']['site_count'] or any(not s.get('recommendation_eligible') for s in sites):
        raise RuntimeError('Reviewed tree dataset is inconsistent.')
    return data, {s['site_id']: s for s in sites}


def public_tree_details(site):
    record = site.get('original_record') or site
    sid = str(record.get('site_id') or site.get('candidate_id') or '')
    location = record.get('location_label') or record.get('where_location')
    if not location:
        area = str(record.get('road_name') or record.get('area_name') or '').strip()
        location = 'Near ' + area if area and 'unnamed' not in area.lower() else 'Use the exact coordinate marker'
    user = record.get('manual_review_status') == 'USER_IMAGE_REVIEWED'
    hillside = record.get('candidate_kind') == 'hillside_conditional'
    explanation = str(record.get('explanation_summary') or record.get('why_chosen') or record.get('filter_reason') or 'Recorded planting candidate; see its review evidence.')
    heat = record.get('surface_heat')
    return {
        'candidate_id': sid,
        'where_location': location,
        'location_source': (record.get('location_context') or {}).get('method', 'stored_area_or_coordinates'),
        'location_note': (record.get('location_context') or {}).get('note', 'Nearby place reference; the coordinate marks the candidate.'),
        'location_street_distance_m': (record.get('location_context') or {}).get('distance_m'),
        'why_chosen': explanation,
        'site_type': 'Green hillside gap' if hillside else 'User-reviewed planting gap' if user else 'Planting candidate',
        'evidence_confidence': 'User reviewed' if user else 'Recorded image evidence',
        'review_status_label': 'User reviewed' if user else 'Recorded image evidence',
        'imagery_capture_range': record.get('imagery_capture_range') or 'Capture date unavailable',
        'imagery_source': record.get('imagery_source') or 'Recorded aerial evidence',
        'mapped_building_clearance_m': record.get('mapped_building_clearance_m'),
        'permission_note': record.get('permission_note') or 'Owner/city permission and current site checks required.',
        'mapped_buffer_exception_note': record.get('mapped_buffer_exception_note'),
        'screening_status': 'Planning candidate; confirm current ground conditions, utilities and permission before planting.',
        'surface_heat': heat,
        'suitable_tree_size': '',
        'field_verified': False,
        'planting_approved': False,
    }


class TreePoint(BaseModel):
    latitude: float = Field(ge=-90, le=90, allow_inf_nan=False)
    longitude: float = Field(ge=-180, le=180, allow_inf_nan=False)


def distance_m(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    h = math.sin((p2-p1)/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(math.radians(lon2-lon1)/2)**2
    return 12742017.6 * math.asin(min(1, math.sqrt(h)))


@router.post('/api/proposed-sites')
def proposed_sites(point: TreePoint):
    data, _ = tree_data()
    ranked = sorted((distance_m(point.latitude, point.longitude, s['latitude'], s['longitude']), s['site_id'], s) for s in data['sites'])
    close = [item for item in ranked if item[0] <= 800]
    selected = close[:3] if close else ranked[:1]
    return [{**public_tree_details(s), 'latitude': s['latitude'], 'longitude': s['longitude'], 'distance_from_click_meters': round(d, 1), 'distance_meters': round(d), 'within_preferred_radius': d <= 800} for d, _, s in selected]


@router.get('/api/tree-site/{site_id}')
def tree_site(site_id: str):
    _, by_id = tree_data()
    site = by_id.get(site_id)
    if site is None:
        raise HTTPException(status_code=404, detail='Active tree candidate not found.')
    return {**public_tree_details(site), 'latitude': site['latitude'], 'longitude': site['longitude']}


@router.get('/api/tree-layer-summary')
def tree_layer_summary():
    data, _ = tree_data()
    return {'site_count': len(data['sites']), 'version': data['metadata']['filter_version'], 'review_method': 'Dated aerial imagery and recorded user review', 'planting_approved': False}
