"""Validate the installed tree website data using Python's standard library."""
from pathlib import Path
import json,hashlib,math,zipfile
R=Path(__file__).resolve().parent
D=json.load(open(R/'ecotrace_reviewed_tree_sites.json'));A=json.load(open(R/'ecotrace_tree_audit.json'));M=D['metadata'];rows=D['sites'];ids={s['site_id'] for s in rows}
assert len(rows)==M['site_count']==5582 and len(ids)==len(rows)
assert ids=={s['site_id'] for s in A['sites'] if s['recommendation_eligible']}
assert all(s['location_label'] and s['why_chosen'] and not s['field_verified'] and not s['planting_approved'] for s in rows)
assert not (ids & set(M['expansion_round5']['explicit_excluded_ids']))
approvals=M['website_round6']['approval_results'];assert len(approvals)==9 and all(s['user_approved'] for s in approvals)
assert sum(s['active'] for s in approvals)==6
assert len(json.load(open(R/'backups/prior_5576_reviewed_tree_sites.json'))['sites'])==5576
minimum=float('inf')
for i,s in enumerate(rows):
 assert s['mapped_building_clearance_m']>=3 and s['inside_ca17']
 for t in rows[i+1:]:
  p1,p2=math.radians(s['latitude']),math.radians(t['latitude']);h=math.sin((p1-p2)/2)**2+math.cos(p1)*math.cos(p2)*math.sin(math.radians(s['longitude']-t['longitude'])/2)**2
  d=12742017.6*math.asin(min(1,math.sqrt(h)));minimum=min(minimum,d);assert d>=15
for s in A['sites']:
 for key,path in s['visual_evidence'].items():
  if key.endswith('_sheet') and path:assert (R/'tree_visual_evidence'/path).is_file(),path
with zipfile.ZipFile(R/'backups/ecotrace_tree_review_original_3000.zip') as z:
 originals=json.loads(z.read('experimental_tree_sites_fusion_v2.json'))['sites'];assert len(originals)==3000
 by={s['site_id']:s for s in A['sites']}
 for s in originals:assert (s['latitude'],s['longitude'])==(by[s['site_id']]['latitude'],by[s['site_id']]['longitude'])
for name,digest in json.load(open(R/'tree_website_manifest.json'))['sha256'].items():assert hashlib.sha256((R/name).read_bytes()).hexdigest()==digest,name
print(f'PASS: {len(rows)} candidates, all nine user approvals recorded, original 3000 preserved, all evidence paths and payload hashes, minimum spacing {minimum:.3f} m.')
