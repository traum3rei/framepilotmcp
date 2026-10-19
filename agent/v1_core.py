"""Pure validation/selection rules for scene-agnostic FramePilot v1."""
from __future__ import annotations
import json
import math
import re
from typing import Any


def parse_json(message: Any) -> dict[str, Any]:
    content = getattr(message, 'content', message)
    if isinstance(content, list):
        content = ''.join(x if isinstance(x, str) else str(x.get('text','')) for x in content if isinstance(x,(str,dict)))
    if not isinstance(content, str):
        raise ValueError('Model returned no text JSON content')
    cleaned = re.sub(r'<think\b[^>]*>.*?</think>', '', content, flags=re.I | re.S).strip()
    if not cleaned:
        raise ValueError('Model returned empty JSON content; use reasoning=False or increase num_predict')
    decoder = json.JSONDecoder()
    try:
        result = json.loads(cleaned)
    except json.JSONDecodeError:
        result = None
        for m in re.finditer(r'\{', cleaned):
            try:
                candidate, _ = decoder.raw_decode(cleaned, m.start())
            except json.JSONDecodeError:
                continue
            if isinstance(candidate, dict):
                result = candidate
                break
    if not isinstance(result, dict):
        raise ValueError(f'Expected JSON object; received: {cleaned[:180]!r}')
    return result


def read_tool(result: Any) -> dict[str, Any]:
    if hasattr(result, 'content'): result = result.content
    if isinstance(result, dict) and 'content' in result:
        if result.get('isError'):
            raise RuntimeError(str(result['content'])[:500])
        result = result['content']
    if isinstance(result, list):
        item = next((x.get('text') for x in result if isinstance(x, dict) and x.get('type')=='text'), None)
        if item is None: raise ValueError('MCP tool returned no text')
        result = item
    if isinstance(result, str):
        try: result = json.loads(result)
        except json.JSONDecodeError as exc: raise ValueError(f'MCP error: {result[:300]}') from exc
    if not isinstance(result, dict): raise ValueError('Unexpected MCP tool result')
    if result.get('isError'): raise RuntimeError(str(result))
    return result


def finite_vector(v: Any, label: str) -> list[float]:
    if not isinstance(v, (list,tuple)) or len(v)!=3 or any(type(x) not in (float,int) or not math.isfinite(x) for x in v):
        raise ValueError(f'{label} must be three finite numbers')
    if any(abs(x)>1_000_000 for x in v): raise ValueError(f'{label} is outside the coordinate safety range')
    return [float(x) for x in v]


def validate_director(plan: dict[str, Any]) -> dict[str, str]:
    role = plan.get('specialist')
    if role not in ('camera', 'lighting'): raise ValueError('Director must choose camera or lighting')
    intent = plan.get('intent')
    if not isinstance(intent,str) or not intent.strip(): raise ValueError('Director must describe the task')
    subject = plan.get('subject')
    return {'specialist': role, 'intent': intent[:1000],
            'subject':subject[:150] if isinstance(subject,str) and subject.strip() else ''}


def validate_proposal(role: str, plan: dict[str, Any], lights: dict[str,dict[str,Any]]) -> dict[str, Any]:
    if role=='camera':
        pos = finite_vector(plan.get('position'), 'position')
        target = finite_vector(plan.get('target'), 'target')
        if math.dist(pos,target)<0.05: raise ValueError('Camera position and target are too close')
        lens = plan.get('lens_mm')
        if type(lens) not in (float,int) or not math.isfinite(lens) or not 18 <= lens <=120:
            raise ValueError('lens_mm must be between 18 and 120')
        return {'position':pos, 'target':target, 'lens_mm':float(lens)}
    if role=='lighting':
        name=plan.get('name')
        if not isinstance(name,str) or name not in lights: raise ValueError('Lighting plan must use an exact EXISTING light name')
        updates={'name':name}
        if 'energy' in plan:
            e=plan['energy']
            if type(e) not in (float,int) or not math.isfinite(e) or not 0<=e<=100000:
                raise ValueError('Light energy out of range')
            updates['energy']=float(e)
        if 'color' in plan:
            color=finite_vector(plan['color'],'color')
            if any(not 0<=x<=1 for x in color): raise ValueError('Light color out of range')
            updates['color']=color
        if 'size' in plan:
            size=plan['size']
            if lights[name].get('light_type')!='AREA': raise ValueError('Size only supported for AREA lights')
            if type(size) not in (float,int) or not math.isfinite(size) or not 0.01<=size<=1000:
                raise ValueError('Invalid area size')
            updates['size']=float(size)
        if len(updates)<2: raise ValueError('Lighting plan needs at least one change')
        return updates
    raise ValueError('Unknown specialist')


def parse_verdict(data: dict[str, Any]) -> dict[str, Any]:
    """Recover from harmless local LLM enum drift; do not invent an approval."""
    winner=str(data.get('winner','A')).strip().lower()
    if winner in ('b','candidate','new','image b'): winner='B'
    elif winner in ('a','best','original','reference','image a'): winner='A'
    else: winner='A' # conservative on unknown output
    approved = data.get('approved') is True
    feedback = data.get('feedback')
    if not isinstance(feedback,str) or not feedback.strip():
        feedback = 'No actionable feedback returned; stop or request human review.'
    next_role = str(data.get('next_specialist','none')).lower()
    if next_role not in ('camera','lighting','none'): next_role = 'none'
    return {'winner': winner, 'approved': approved, 'feedback': feedback[:900], 'next_specialist': next_role}


def choose_candidate(verdict: dict[str,Any]) -> bool:
    return verdict['winner']=='B'


def camera_snapshot(info: dict[str,Any]) -> dict[str,Any]:
    if not isinstance(info,dict) or not info.get('name'):
        raise ValueError('A scene camera is required for v1 preview comparison; create/select one in Blender first')
    return {'position':finite_vector(info.get('position'),'camera position'),
            'rotation_degrees':finite_vector(info.get('rotation_degrees'),'camera rotation'),
            'lens_mm':float(info['lens_mm'])}


def light_snapshot(info: dict[str,Any]) -> dict[str,Any]:
    name=info['name']
    data={'name':name,'energy':float(info['energy']),'color':finite_vector(info['color'],'light color'),
          'location':finite_vector(info['location'],'light position'),
          'rotation_degrees':finite_vector(info['rotation_degrees'],'light rotation')}
    if info.get('light_type')=='AREA': data['size']=float(info['size'])
    return data
