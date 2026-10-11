from __future__ import annotations
import json, os, requests
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
router=APIRouter(prefix="/api/ewaste-gemini",tags=["ewaste-gemini"])
class AdviceRequest(BaseModel):
    device_type:str
    brand_model:str|None=None
    age_years:float|None=None
    condition:str|None=None
    custom_context:str|None=None
@router.post("/advice")
async def advice(payload:AdviceRequest):
    key=os.getenv("GEMINI_API_KEY","").strip()
    if not key:return {"available":False,"summary":"Gemini-specific advice unavailable because GEMINI_API_KEY is not set.","instructions":[]}
    prompt=f"""Give device-specific EcoTrace e-waste guidance. Never invent unknown facts. If brand/model, age, condition, battery status, or hazards are unknown, say Unknown. No battery disassembly or hazardous repair instructions.
Device category: {payload.device_type}
Brand/model: {payload.brand_model or 'Unknown'}
Age: {payload.age_years if payload.age_years is not None else 'Unknown'}
Condition: {payload.condition or 'Unknown'}
Custom details: {payload.custom_context or 'None'}
If Custom details contains an age range, preserve that uncertainty rather than pretending the midpoint is exact.
Return strict JSON with keys summary, recommended_path, why, instructions (array), unknowns (array)."""
    try:
        url=f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={key}"
        r=requests.post(url,json={"contents":[{"parts":[{"text":prompt}]}],"generationConfig":{"responseMimeType":"application/json","temperature":0.2}},timeout=35)
        r.raise_for_status()
        txt=r.json()["candidates"][0]["content"]["parts"][0]["text"]
        return {"available":True,**json.loads(txt)}
    except Exception as exc:
        raise HTTPException(status_code=503,detail=f"Gemini e-waste advice unavailable: {exc}") from exc
