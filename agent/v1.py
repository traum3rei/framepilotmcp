#!/usr/bin/env python3
"""FramePilot v1: scene-independent Director -> Camera/Lighting -> Vision Reviewer.

Runs against the CURRENT Blender Live scene through existing MCP server. The model
may propose edits, but Python validates every action and only applies with --apply.
Automatic v1 only adjusts current camera or existing lights; no geometry creation.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import copy
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from shutil import copy2
from typing import Any, TypedDict

from dotenv import load_dotenv
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_ollama import ChatOllama
from langgraph.graph import START, END, StateGraph

from v1_core import (camera_snapshot, choose_candidate, light_snapshot, parse_json,
                     parse_verdict, read_tool, validate_director, validate_proposal)

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
load_dotenv(HERE / '.env')

REQUIRED = {'inspect_scene', 'get_object', 'list_lights', 'list_scene_objects',
            'set_camera', 'set_camera_pose', 'set_light_properties', 'render_preview'}


class State(TypedDict, total=False):
    scene: dict[str,Any]
    lights: dict[str, dict[str,Any]]
    director: dict[str,str]
    specialist: str
    feedback: str
    proposal: dict[str,Any]
    attempted: list[dict[str,Any]]
    attempt: int
    current_image: str
    selected_image: str
    best_camera: dict[str,Any]
    best_lights: dict[str,dict[str,Any]]
    winner: str
    approved: bool
    history: list[dict[str,Any]]
    final: bool


def make_image_content(description: str, png: bytes) -> list[dict[str,Any]]:
    if not png.startswith(b'\x89PNG\r\n\x1a\n'):
        raise ValueError('Blender preview is not PNG')
    return [
        {'type':'text', 'text':description},
        {'type':'image_url','image_url':{'url':'data:image/png;base64,'+base64.b64encode(png).decode('ascii')}}
    ]


async def run(request: str, model_name: str, vision_model: str, apply: bool, attempts: int, inspect_only: bool = False):
    tsx = ROOT / 'node_modules' / '.bin' / 'tsx'
    if not tsx.exists(): raise RuntimeError('Run npm install in the FramePilot folder first')
    client = MultiServerMCPClient({'framepilot': {'transport':'stdio','command':str(tsx),
                                                 'args':[str(ROOT/'src'/'server.ts')], 'cwd':str(ROOT)}})
    tools = {tool.name: tool for tool in await client.get_tools()}
    required = {'inspect_scene', 'list_scene_objects', 'list_lights'} if inspect_only else REQUIRED
    missing = required - tools.keys()
    if missing: raise RuntimeError('FramePilot v1 MCP tools not registered: '+', '.join(sorted(missing)))

    async def tool(name: str, args: dict[str,Any] | None = None) -> dict[str,Any]:
        return read_tool(await tools[name].ainvoke(args or {}))

    async def scene_snapshot() -> tuple[dict[str,Any],dict[str,dict[str,Any]]]:
        scene = await tool('inspect_scene')
        if scene.get('mode')!='live': raise RuntimeError('Enable the Blender Live Bridge in the OPEN Blender window')
        if not inspect_only and scene.get('camera'):
            camera_snapshot(scene['camera'])
        lights: dict[str,dict[str,Any]] = {}
        offset = 0
        while True:
            page = await tool('list_lights', {'offset':offset,'limit':100})
            if page.get('mode')!='live': raise RuntimeError('Live bridge disconnected')
            for entry in page['items']:
                lights[entry['name']]=entry
            if page['next_offset'] is None: break
            offset=page['next_offset']
            if offset >= 400: raise RuntimeError('Too many scene lights for safe restoration; reduce scene scope first')
        return scene, lights

    scene, light_infos = await scene_snapshot()
    if inspect_only:
        # No LLM, no renders and no changes. Read the OPEN scene using MCP.
        objects = []
        offset = 0
        while True:
            page = await tool('list_scene_objects', {'offset': offset, 'limit': 75})
            objects.extend(page['items'])
            if page.get('next_offset') is None:
                break
            offset = page['next_offset']
            if offset >= 500:
                raise RuntimeError('Scene inspection exceeded 500 objects; use the paginated MCP tools directly')
        report = {
            'scene': scene.get('scene'),
            'blend_file': scene.get('blend_file'),
            'render_engine': scene.get('render_engine'),
            'camera': scene.get('camera'),
            'object_count': scene.get('object_count'),
            'objects': objects,
            'lights': list(light_infos.values()),
            'note': 'Object inventory does not prove render visibility. Use a preview for visual evaluation.',
        }
        print('[INSPECT] Read-only scene inventory (no LLM, edits or renders):', flush=True)
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return
    if not scene.get('camera'): raise RuntimeError('Select an active scene camera in Blender before starting v1')
    initial_camera = camera_snapshot(scene['camera'])
    initial_lights = {name:light_snapshot(data) for name,data in light_infos.items()}
    best_camera = copy.deepcopy(initial_camera)
    best_lights = copy.deepcopy(initial_lights)
    touched_lights: set[str] = set()
    camera_touched = False
    did_finish = False

    # Summarize inventory. No assumption about object names or type of subject.
    scene_objects = []
    offset = 0
    more_objects = False
    while True:
        page = await tool('list_scene_objects', {'offset':offset,'limit':75})
        scene_objects.extend(page['items'])
        more_objects = page.get('next_offset') is not None
        if not more_objects or len(scene_objects) >= 300: break
        offset = page['next_offset']
    matches = [x for x in scene_objects if x['name'].lower() in request.lower()]
    seen = {x['name'] for x in matches}
    scene_subset = (matches + [x for x in scene_objects if x['name'] not in seen])[:40]
    summary = {
        'scene':scene['scene'], 'object_count':scene.get('object_count'),
        'camera':scene.get('camera'), 'object_catalog':scene_subset,
        'objects_more':more_objects, 'lights_more':len(light_infos)>40,
        'lights':[{'name':x['name'],'light_type':x['light_type'],'energy':x['energy'],
                   'color':x['color'],'location':x['location'],'size':x.get('size')}
                  for x in list(light_infos.values())[:40]],
        'note':'Object existence does NOT prove that it is visible in a render. Never invent objects.',
    }
    print(f'[Scene] {summary["scene"]}: {summary["object_count"]} objects, {len(light_infos)} lights, mode=live',flush=True)

    planner = ChatOllama(model=model_name, temperature=0, reasoning=False, format='json', num_predict=800)
    reviewer = ChatOllama(model=vision_model, temperature=0, reasoning=False, format='json', num_predict=650)
    timestamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S_%fZ')
    run_dir=ROOT/'runtime'/'v1_runs'/timestamp
    preview=ROOT/'runtime'/'preview.png'

    async def ask(system: str, body: dict[str,Any]) -> dict[str,Any]:
        reply=await planner.ainvoke([SystemMessage(content=system),
                                    HumanMessage(content=json.dumps(body,ensure_ascii=False))])
        return parse_json(reply)

    async def restore(camera:dict[str,Any], lights:dict[str,dict[str,Any]]) -> None:
        # Changes are scene-local and unsaved. Restore only objects this agent touched.
        if camera_touched:
            out=await tool('set_camera_pose',camera)
            if out.get('mode')!='live':raise RuntimeError('Camera restoration was not confirmed live')
        for name in sorted(touched_lights):
            if name not in lights: raise RuntimeError(f'Cannot restore missing original light {name}')
            out=await tool('set_light_properties',lights[name])
            if out.get('mode')!='live':raise RuntimeError('Light restoration was not confirmed live')
        print('[Restore] Selected best camera and lighting applied in Blender',flush=True)

    async def render_image(name: str) -> Path:
        result=await tools['render_preview'].ainvoke({})
        if isinstance(result,dict) and result.get('isError'):
            raise RuntimeError(str(result['content'])[:400])
        # MCP returns text + image, and also writes this file in both live/headless modes.
        if not preview.exists(): raise RuntimeError('Blender returned no preview.png')
        data=preview.read_bytes()
        if not data.startswith(b'\x89PNG\r\n\x1a\n'): raise RuntimeError('Invalid Blender preview PNG')
        run_dir.mkdir(parents=True,exist_ok=True)
        path=run_dir/name
        path.write_bytes(data)
        print('[Render]',path,flush=True)
        return path

    async def director_node(state: State)->State:
        print('[Director] Planning...',flush=True)
        data=await ask(
            'You are a scene-agnostic Blender art director. Choose exactly one specialist: camera or lighting, '
            'to best address the user request. Do NOT assume any particular scene or subject. '
            'Use existing scene data only. Set specialist to either camera OR lighting. Return JSON {"specialist":"camera", '
            '"intent":"specific visual goal","subject":"exact object name or empty"}.',
            {'user_request':request,'scene':summary})
        plan=validate_director(data)
        if plan['specialist']=='lighting' and not light_infos:
            raise RuntimeError('This scene has no lights to edit. Add a light in Blender or use create_light with explicit approval.')
        return {'director':plan,'specialist':plan['specialist'], 'feedback':'',
                'attempt':0,'attempted':[],'history':[]}

    async def proposal_node(state:State)->State:
        role=state['specialist']
        prior=state.get('attempted',[])
        print(f'[{role.title()} Agent] Proposing attempt {state.get("attempt",0)+1}/{attempts}',flush=True)
        if role=='camera':
            system=(
              'You are a professional Blender camera operator. Given the user intent and CURRENT scene, '
              'propose a camera shot using world-space Blender coordinates. NO fixed floor height, angle or target. '
              'Use subject locations and dimensions; aim at real scene objects. '
              'Never repeat a previously rejected proposal. Return ONLY JSON with position:[x,y,z], '
              'target:[x,y,z], lens_mm:number. No other fields required.')
        else:
            system=(
              'You are a Blender lighting designer. This is a GENERAL scene, not a corridor demo. '
              'Choose exactly ONE existing light by its name, and propose changes to its energy (0..100000), '
              'RGB color (0..1) and optionally AREA size. No scene changes other than these properties. '
              'Do NOT create new lights or use unknown names. Do not repeat rejected values. '
              'Return ONLY JSON {"name":"exact light name","energy":number,"color":[r,g,b]}; '
              'size optional for AREA light only.')
        selected_subject = state['director'].get('subject','')
        subject_details = None
        if role=='camera' and selected_subject:
            # An invalid guess should not crash planning or silently create geometry.
            try:
                result=await tool('get_object',{'name':selected_subject})
                subject_details=result.get('object')
            except Exception:
                subject_details={'warning':'Object name not found in active scene; do not use it as a target'}
        data=await ask(system, {'request':request,'intent':state['director']['intent'],
                'subject_details':subject_details,
                'scene':summary,'specialist':role,'feedback':state.get('feedback',''),
                'previous_proposals':prior[-5:], 'best_camera':best_camera,
                'best_lights':dict(list(best_lights.items())[:40]) if role=='lighting' else {'count':len(light_infos)}})
        proposal=validate_proposal(role,data,light_infos)
        if {'role':role,'proposal':proposal} in prior:
            raise ValueError('Camera/Lighting Agent repeated an identical rejected proposal; stopping')
        return {'proposal':proposal,'attempt':state.get('attempt',0)+1,
                'attempted':[*prior,{'role':role,'proposal':proposal}]}

    async def apply_node(state:State)->State:
        nonlocal camera_touched
        # Always generate next candidate FROM the selected best, not the last rejected edit.
        if state['attempt'] > 1:
            await restore(best_camera,best_lights)
        role=state['specialist']
        if role=='camera':
            # Set this BEFORE tool call: Blender could modify the scene then fail on response.
            camera_touched=True
            output=await tool('set_camera',state['proposal'])
        else:
            touched_lights.add(state['proposal']['name'])
            output=await tool('set_light_properties',state['proposal'])
        if output.get('mode')!='live':raise RuntimeError('Edit did not confirm Blender Live')
        print(f'[Apply] {role} changed in open Blender',flush=True)
        return {}

    async def capture_node(state:State)->State:
        shot=await render_image(f'attempt_{state["attempt"]:02d}.png')
        return {'current_image':str(shot)}

    async def review_node(state:State)->State:
        nonlocal best_camera,best_lights
        base=Path(state['selected_image']).read_bytes()
        new=Path(state['current_image']).read_bytes()
        instruction=(
            'You are a visual QA agent reviewing TWO real Blender render images. '
            'A = CURRENT BEST; B = NEW CANDIDATE. Judge against the user request, subject clarity, '
            'framing, depth and lighting. Do not assume a particular scene. '
            'If B is worse or tied, keep A. If B is better, choose B even if it still needs work. '
            'Output ONLY JSON: {"winner":"A|B", "approved":true|false, '
            '"feedback":"concrete improvement based on pixels", '
            '"next_specialist":"camera|lighting|none"}. '
            'Do not recommend changing geometry because it is dark without considering lighting. '
            'Only claim approved if the request is met.')
        content=[{'type':'text','text': instruction+'\nUSER REQUEST: '+request+\
                  '\nCURRENT PROPOSAL: '+json.dumps(state['proposal'])},
                 *make_image_content('IMAGE A: current best image',base),
                 *make_image_content('IMAGE B: new candidate image',new)]
        msg=await reviewer.ainvoke([SystemMessage(content='Review the actual images, not filenames. JSON only.'),
                                   HumanMessage(content=content)])
        verdict=parse_verdict(parse_json(msg))
        winner='B' if choose_candidate(verdict) else 'A'
        record={'attempt':state['attempt'],'role':state['specialist'],'proposal':state['proposal'],
                'image':state['current_image'],'review':verdict}
        if winner=='B':
            live, lights=await scene_snapshot()
            best_camera=camera_snapshot(live['camera'])
            best_lights={n:light_snapshot(x) for n,x in lights.items()}
            selected=state['current_image']
        else:
            selected=state['selected_image']
        print(f'[Vision Reviewer] Chose {winner}; approved={verdict["approved"]}: {verdict["feedback"]}',flush=True)
        return {'selected_image':selected, 'best_camera':copy.deepcopy(best_camera),
                'best_lights':copy.deepcopy(best_lights), 'approved':verdict['approved'] and winner=='B',
                'winner':winner,'feedback':verdict['feedback'],
                'specialist':verdict['next_specialist'] if verdict['next_specialist']!='none' else state['specialist'],
                'final':verdict['next_specialist']=='none',
                'history':[*state.get('history',[]),record]}

    def after_review(state:State)->str:
        if state.get('approved') or state.get('final') or state['attempt']>=attempts:
            return 'finish'
        if state['specialist']=='lighting' and not light_infos:
            return 'finish'
        return 'proposal'

    async def finish_node(state:State)->State:
        nonlocal did_finish
        await restore(best_camera,best_lights)
        run_dir.mkdir(parents=True,exist_ok=True)
        metadata={'request':request,'selected_image':state['selected_image'],
                  'approved':state.get('approved',False), 'attempts':state.get('history',[]),
                  'best_camera':best_camera, 'best_lights':best_lights}
        report=run_dir/'evaluation.json'
        report.write_text(json.dumps(metadata,indent=2,ensure_ascii=False),encoding='utf8')
        did_finish=True
        print(f'RESULT: selected={state["selected_image"]}, approved={state.get("approved",False)}')
        print(f'EVALUATION: {report}')
        return {'final':True}

    graph=StateGraph(State)
    graph.add_node('director',director_node)
    graph.add_node('camera_or_lighting',proposal_node)
    if apply:
        graph.add_node('edit_scene',apply_node)
        graph.add_node('render',capture_node)
        graph.add_node('vision_reviewer',review_node)
        graph.add_node('restore_best',finish_node)
        graph.add_edge(START,'director')
        graph.add_edge('director','camera_or_lighting')
        graph.add_edge('camera_or_lighting','edit_scene')
        graph.add_edge('edit_scene','render')
        graph.add_edge('render','vision_reviewer')
        graph.add_conditional_edges('vision_reviewer',after_review,{'proposal':'camera_or_lighting','finish':'restore_best'})
        graph.add_edge('restore_best',END)
    else:
        graph.add_edge(START,'director')
        graph.add_edge('director','camera_or_lighting')
        graph.add_edge('camera_or_lighting',END)
    app=graph.compile()
    try:
        initial={'scene':scene,'lights':light_infos,'selected_image':'',
                 'best_camera':best_camera,'best_lights':best_lights}
        if apply:
            # Baseline image A must be the open scene BEFORE any edits.
            baseline=await render_image('baseline.png')
            initial['selected_image']=str(baseline)
        result=await app.ainvoke(initial,config={'recursion_limit':min(80,8*attempts+10)})
        if not apply:
            print('[DRY RUN] Nothing modified. Proposed plan:')
            print(json.dumps({'role':result['specialist'],'proposal':result['proposal']},indent=2))
    except BaseException:
        if apply and not did_finish:
            try:
                print('[Safety] Attempting to restore best previously selected state...',flush=True)
                await restore(best_camera,best_lights)
            except Exception as restore_exc:
                print(f'[WARNING] Automatic restoration failed: {restore_exc}. Inspect Blender before saving.',flush=True)
        raise


def main():
    parser=argparse.ArgumentParser(description='Scene-independent FramePilot Director + Camera/Lighting + Vision QA')
    parser.add_argument('request', nargs='?', help='Describe camera or lighting intent for any current Blender scene')
    parser.add_argument('--inspect', action='store_true', help='Read the live scene only: no LLM, camera proposal, edits or rendering')
    parser.add_argument('--model',default=os.getenv('OLLAMA_MODEL','qwen3.5:4b'))
    parser.add_argument('--vision-model',default=os.getenv('OLLAMA_VISION_MODEL',os.getenv('OLLAMA_MODEL','qwen3.5:4b')))
    parser.add_argument('--apply',action='store_true', help='Allow Blender edits and renders (otherwise dry-run)')
    parser.add_argument('--attempts',type=int,default=2)
    args=parser.parse_args()
    if not args.inspect and not args.request: parser.error('Provide a camera/lighting request, or use --inspect')
    if args.inspect and args.apply: parser.error('--inspect cannot be combined with --apply')
    if not 1<=args.attempts<=5: parser.error('--attempts must be between 1 and 5')
    try:
        asyncio.run(run(args.request or '', args.model, args.vision_model, args.apply,
                        args.attempts, inspect_only=args.inspect))
    except (ValueError,RuntimeError,KeyboardInterrupt) as exc:
        parser.exit(1,f'[FramePilot v1] {type(exc).__name__}: {exc}\n')

if __name__=='__main__':main()
