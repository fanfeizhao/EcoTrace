"""Read-only planning-priority extension. No candidate/database mutations."""
from pathlib import Path
from functools import lru_cache
import ast,json,re,hashlib
ROOT=Path(__file__).resolve().parent
@lru_cache(maxsize=1)
def data():
 d=json.loads((ROOT/'ecotrace_priority_data.json').read_text());return d,{s['site_id']:s for s in d['sites']}
def fingerprint(sites):
 rows=sorted((s['site_id'],s['latitude'],s['longitude'],s.get('surface_heat')) for s in sites)
 return hashlib.sha256(json.dumps(rows,sort_keys=True,separators=(',',':')).encode()).hexdigest()
def validate(project):
 d=json.loads((project/'ecotrace_reviewed_tree_sites.json').read_text());expected=json.loads((ROOT/'ecotrace_priority_data.json').read_text())['metadata']['source_fingerprint']
 if fingerprint(d['sites'])!=expected:raise ValueError('Candidate coordinates or heat records differ from this update. Nothing changed. Rebuild statistics for your current dataset.')
def get_priority(sid):return data()[1].get(str(sid))
def income_text(r):
 v=r.get('median_household_income')
 return 'Unavailable' if v is None else '$250,000+' if r.get('income_topcoded') else '${:,.0f}'.format(v)
def fields(r):
 return {'surface_heat':{'surface_temperature_c':r['heat_c'],'surface_temperature_f':r['heat_f'],'observation_count':r['heat_observations'],'dates':r['heat_dates'],'source':'USGS Landsat C2 L2; containing pixel, about 100 m thermal detail','method':r['heat_method']},'priority_heat_uncertainty_k_max':r.get('heat_uncertainty_k_max'),'planning_priority':r['priority'],'priority_reason':r['reason'],'priority_heat_status':r['heat_status'],'priority_heat_c':r['heat_c'],'priority_tract':r['tract_geoid'],'priority_poverty_percent':r['poverty_percent'],'priority_poverty_moe_pp':r['poverty_moe_pp'],'priority_poverty_status':r['poverty_status'],'priority_income_text':income_text(r),'priority_income_moe':r['income_moe'],'priority_estimate_sensitive':r['poverty_threshold_sensitive'],'priority_basis':'Experimental heat + tract poverty rule; ACS 2020-2024. Income descriptive. Not planting approval or current weather.'}
def install_hooks():
 import ecotrace_tree_details as td
 if not getattr(td,'_priority_hook',False):
  original=td.public_tree_details
  def wrapped(site):
   result=original(site);r=get_priority(result.get('candidate_id'))
   if r:result.update(fields(r))
   return result
  td.public_tree_details=wrapped;td._priority_hook=True
 # Append verified priority facts only for candidate IDs in the live assistant context.
 import gemini_assistant_backend_v3 as ga
 if not getattr(ga,'_priority_hook',False):
  original_trim=ga._trim_value
  def trim(value,*,depth=0):
   out=original_trim(value,depth=depth)
   if depth==0 and isinstance(out,dict):
    found={}
    def walk(v,level=0):
     if level>8:return
     if isinstance(v,dict):
      sid=v.get('candidate_id') or v.get('site_id');r=get_priority(sid) if sid else None
      if r and len(found)<10:found[r['site_id']]={'site_id':r['site_id'],**fields(r)}
      for key,item in v.items():
       if 'coordinates' not in key and 'segments' not in key:walk(item,level+1)
     elif isinstance(v,list):
      for item in v[:20]:walk(item,level+1)
    walk(value)
    if found:out['verified_tree_priority_context']={'method':data()[0]['metadata']['method'],'sites':list(found.values())}
   return out
  ga._trim_value=trim;ga._priority_hook=True


def rebuild_app_models(namespace):
 """Resolve every local Pydantic model explicitly before the server accepts requests."""
 from pydantic import BaseModel
 from typing import Any
 resolved=dict(namespace)
 resolved.setdefault('Any',Any)
 required=('PlantingSpot','ZoneAnalysis','ExposureSegment','RouteAnalysis','BikeRouteAnalysis','WeatherSnapshot')
 missing=[name for name in required if name not in resolved]
 if missing:raise RuntimeError('Startup model repair cannot find: '+', '.join(missing))
 rebuilt=[]
 for name,value in list(resolved.items()):
  if isinstance(value,type) and value is not BaseModel and issubclass(value,BaseModel) and value.__module__==resolved.get('__name__'):
   value.model_rebuild(force=True,_types_namespace=resolved)
   if not value.__pydantic_complete__:raise RuntimeError('Model remains incomplete: '+name)
   rebuilt.append(name)
 for name in required:
  model=resolved[name]
  model.model_rebuild(force=True,_types_namespace=resolved)
  model.model_json_schema()
 namespace['ECOTRACE_MODEL_REPAIR_VERSION']='priority-v3-model-repair'
 namespace['ECOTRACE_REBUILT_MODELS']=rebuilt
 print('EcoTrace priority v3: response models rebuilt successfully ('+str(len(rebuilt))+').',flush=True)
 return rebuilt

def transform(source):
 marker='function treeEvidenceHtml(site)'
 pattern=r'function\s+treeEvidenceHtml\s*\(\s*site\s*\)\s*\{'
 if len(re.findall(pattern,source))!=1:raise ValueError('Expected installed tree-evidence UI was not found. Your current app was not modified.')
 js=(ROOT/'ecotrace_priority_ui.js').read_text()
 source=re.sub(pattern,lambda m:js+'\nfunction treeEvidenceHtmlBase(site){',source,count=1)
 about_pattern=r'(<summary\b[^>]*>[^<]*Tree Planting / Candidate Evidence</summary>)'
 if len(re.findall(about_pattern,source))!=1:raise ValueError('Expected About tree-planting section was not found. Your current app was not modified.')
 about='<p id="ecotrace-priority-about" class="mt-2 text-xs leading-5 text-slate-300"><strong>Planning priority:</strong> High = relatively high historical heat and neighborhood poverty; Elevated = one; Standard = neither. Income is context only. Open Planning priority &amp; why on a candidate to see its label and statistics. Labels indicate potential need, not planting suitability or approval.</p>'
 source=re.sub(about_pattern,lambda m:m.group(1)+'\n'+about,source,count=1)

 nodes=ast.parse(source).body
 node=next((n for n in nodes if isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='app' for t in n.targets)),None)
 if node is None:raise ValueError('FastAPI app definition was not found.')
 lines=source.splitlines(keepends=True)
 lines.insert(node.end_lineno,'\nfrom ecotrace_priority_runtime import priority_router\napp.include_router(priority_router())\n')
 source=''.join(lines)
 guards=[n for n in ast.parse(source).body if isinstance(n,ast.If) and isinstance(n.test,ast.Compare) and isinstance(n.test.left,ast.Name) and n.test.left.id=='__name__' and any(isinstance(c,ast.Constant) and c.value=='__main__' for c in n.test.comparators)]
 if len(guards)!=1:raise ValueError('Cannot identify server startup for model repair; no files changed.')
 lines=source.splitlines(keepends=True)
 lines.insert(guards[0].lineno-1,'\nfrom ecotrace_priority_runtime import rebuild_app_models\nrebuild_app_models(globals())\n')
 source=''.join(lines);compile(source,'priority app','exec');return source

def priority_router():
 from fastapi import APIRouter
 from fastapi.responses import HTMLResponse
 router=APIRouter()
 @router.get('/api/tree-priority')
 def summary():return data()[0]
 @router.get('/tree-priority',response_class=HTMLResponse)
 def page():return (ROOT/'ecotrace_priority_dashboard.html').read_text()
 return router
