"""Read the installed tree layer's actual review result for the in-app assistant."""
import json
from pathlib import Path

def visual_filter_context():
    path = Path(__file__).resolve().parent / 'ecotrace_reviewed_tree_sites.json'
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
        metadata = data['metadata']
        count = len(data['sites'])
        if metadata.get('site_count') != count:
            return {'available': False, 'error': 'Tree dataset count mismatch; do not claim a verified result.'}
        return {
            'available': True,
            'retained_planning_candidates': count,
            'audit_records': metadata.get('audit_record_count'),
            'new_coordinates_visually_reviewed': metadata.get('expansion_round5', {}).get('visually_reviewed_new_coordinates'),
            'individual_close_reviews': metadata.get('visual_close_reviews'),
            'expansion_results': metadata.get('expansion_round5'),
            'website_update': metadata.get('website_round6'),
            'decision_counts': metadata.get('counts'),
            'new_proposal_results': metadata.get('new_proposal_counts'),
            'imagery_capture_range': metadata.get('imagery_capture_range'),
            'review_method': metadata.get('reviewer'),
            'review_policy': metadata.get('review_policy'),
            'heat_source': metadata.get('heat_source'),
            'limitations': metadata.get('limitations'),
            'evidence_page': '/tree-review',
            'district': metadata.get('district'),
            'recent_satellite_context': metadata.get('recent_satellite_context'),
            'permission_note': 'Owner/city permission and site checks required, including unfenced lawns.',
            'original_3000_preserved': True,
            'planting_approval': False,
        }
    except (OSError, ValueError, KeyError, TypeError):
        return {'available': False, 'error': 'Reviewed tree data unavailable; do not invent counts or approval.'}
