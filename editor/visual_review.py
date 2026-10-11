"""Coordinate-bound advisory image review; never mutates candidate records."""
import json
from functools import lru_cache
from pathlib import Path

@lru_cache(maxsize=1)
def data():
    path=Path(__file__).with_name('visual_review_results.json')
    if not path.exists():return {'metadata':{},'rows':[]},{}
    payload=json.loads(path.read_text())
    return payload,{r['candidate_id']:r for r in payload['rows']}

def result(row,key):
    from canopy_editor import fields,point
    old=data()[1].get(str(key))
    if old is None:return None
    lat,lon=point(row);p=fields(row);review=p.get('canopy_manual_review',{})
    same=(lat==old['latitude'] and lon==old['longitude'])
    current=dict(old,coordinate_match=same,advisory=True)
    if not same:
        current.update(status='uncertain',label='Location changed — review needed',reason='The image review applies to the previous coordinate. Check the saved point before retaining it.',index=None)
    resolved=review.get('decision')=='keep' and str(review.get('at',''))>old['reviewed_at']
    current['manual_resolved']=resolved
    current['needs_review']=current['status']=='uncertain' and not resolved and review.get('decision')!='exclude'
    return current
