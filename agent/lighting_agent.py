#!/usr/bin/env python3
"""FramePilot Lighting Agent: local LangGraph + Ollama + EXISTING MCP tools.

Operates only on FramePilot / area lights manually created via preview_corridor_lights.py.
Never moves cameras, creates geometry, saves the .blend file or touches other lights.
Keeps the best A/B-reviewed lighting setup; attempts to restore it on failure.
"""
from __future__ import annotations

import argparse
import asyncio
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

from lighting_checks import managed_lights, validate_light_plan, choose_best, changed_energies
from model_json import parse_model_json
from vision_review import load_fresh_png, preview_mtime
from comparison_review import image_pair_content, parse_visual_verdict
from orchestrator import parse_tool_text

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
load_dotenv(HERE / ".env")


class LightingState(TypedDict, total=False):
    scene: dict[str, Any]
    initial: dict[str, float]
    best: dict[str, float]
    current: dict[str, float]
    proposal: dict[str, float]
    previous_plans: list[dict[str, float]]
    feedback: str
    attempts: int
    best_approved: bool
    best_image: str
    current_image: str
    run_dir: str
    history: list[dict[str, Any]]
    finished: bool


def validate_mode(scene: dict[str, Any], tools: dict[str, Any]):
    if scene.get("mode") != "live":
        raise RuntimeError("FramePilot Lighting requires Blender Live (mode=live). Start Live Bridge in the open scene.")
    for name in ("inspect_scene", "set_light_energy", "render_preview"):
        if name not in tools:
            raise RuntimeError(f"Missing MCP tool {name}. Complete its registration in src/server.ts first.")
    lights = managed_lights(scene)
    if len(lights) != 3:
        raise RuntimeError(
            "Cannot find all three named FramePilot corridor lights. In Blender > Scripting, "
            "run scripts/preview_corridor_lights.py ONCE in the demo scene, then retry. "
            "The Lighting Agent intentionally does not touch 'Warm eye light'."
        )
    if scene.get("truncated"):
        raise RuntimeError("Scene inventory is truncated; refusing to edit lights whose state may be incomplete.")
    return lights


async def run(prompt: str, *, model_name: str, vision_model_name: str | None,
              attempts: int, apply: bool) -> None:
    tsx = ROOT / "node_modules" / ".bin" / "tsx"
    server = ROOT / "src" / "server.ts"
    if not tsx.is_file() or not server.is_file():
        raise RuntimeError("FramePilot project incomplete; put agent/ next to src/ and run npm install.")
    client = MultiServerMCPClient({
        "framepilot": {"transport": "stdio", "command": str(tsx),
                       "args": [str(server)], "cwd": str(ROOT)}
    })
    tools = {tool.name: tool for tool in await client.get_tools()}
    if "inspect_scene" not in tools:
        raise RuntimeError("MCP server does not expose inspect_scene.")
    scene = parse_tool_text(await tools["inspect_scene"].ainvoke({}))
    original = validate_mode(scene, tools)
    print("[Scene] Blender Live connected; managed lights:", original, flush=True)

    planner = ChatOllama(model=model_name, temperature=0, reasoning=False, num_predict=650)
    vision = ChatOllama(model=vision_model_name or model_name,
                        temperature=0, reasoning=False, num_predict=450)
    preview = ROOT / "runtime" / "preview.png"
    run_dir = ROOT / "runtime" / "lighting_runs" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")

    # Track best applied state OUTSIDE of LangGraph too, so a model/tool error cannot
    # accidentally leave a partially modified scene that is misreported as successful.
    last_confirmed = dict(original)
    best_confirmed = dict(original)

    async def read_lights() -> dict[str, float]:
        checked_scene = parse_tool_text(await tools["inspect_scene"].ainvoke({}))
        if checked_scene.get("mode") != "live":
            raise RuntimeError("Blender Live was disconnected; stopping.")
        seen = managed_lights(checked_scene)
        if set(seen) != set(original):
            raise RuntimeError("Available FramePilot lights changed; stopping to avoid untracked edits.")
        return seen

    async def apply_energies(wanted: dict[str, float]) -> None:
        nonlocal last_confirmed
        current = await read_lights()
        for name, value in changed_energies(current, wanted).items():
            result = parse_tool_text(await tools["set_light_energy"].ainvoke({
                "light_name": name, "energy": value,
            }))
            if result.get("mode") != "live" or result.get("light") != name:
                raise RuntimeError(f"Light update result unexpectedly differs for {name}: {result}")
        actual = await read_lights()
        for name, value in wanted.items():
            if abs(actual[name] - value) > 0.05:
                raise RuntimeError(f"Light energy verification failed: {name}: {actual[name]} != {value}")
        last_confirmed = dict(wanted)

    async def render_frame(dest: Path) -> bytes:
        previous = preview_mtime(preview)
        result = await tools["render_preview"].ainvoke({})
        if isinstance(result, dict) and result.get("isError"):
            raise RuntimeError(str(result.get("content", "render failed")))
        png = load_fresh_png(preview, previous)
        dest.parent.mkdir(parents=True, exist_ok=True)
        copy2(preview, dest)
        return png

    async def baseline(state: LightingState) -> LightingState:
        path = run_dir / "baseline.png"
        await render_frame(path)
        print(f"[Baseline] Saved {path}", flush=True)
        return {"scene": scene, "initial": dict(original), "best": dict(original),
                "best_image": str(path), "current": dict(original), "attempts": 0,
                "best_approved": False, "history": [], "previous_plans": [dict(original)],
                "feedback": "Background is too dark; architecture exists but is underlit.",
                "run_dir": str(run_dir)}

    async def propose(state: LightingState) -> LightingState:
        step = state["attempts"] + 1
        print(f"[Lighting Agent] Planning attempt {step}/{attempts}...", flush=True)
        res = await planner.ainvoke([
            SystemMessage(content=(
                "You are a Blender lighting designer, NOT a camera operator. "
                "The floating eye is already prominent; expose the EXISTING dark pillars, walls and floor "
                "without flattening shadows or overexposing the eye. You may ONLY set the energy "
                "for every managed light explicitly named in the input. You CANNOT change location, color, "
                "materials, camera, world or nonmanaged lights. Propose REAL numbers between 50 and 4000. "
                "Make a MEANINGFUL change from previous lighting configurations. "
                "Output ONLY JSON: {\"energies\":{\"exact light name\":number,...},\"reason\":\"short\"}."
            )),
            HumanMessage(content=json.dumps({
                "user_request": prompt,
                "allowed_lights_and_initial_energies": original,
                "current_best_energies": state["best"],
                "tried": state["previous_plans"],
                "previous_review_feedback": state["feedback"],
                "camera_unchanged": scene.get("camera"),
            }, ensure_ascii=False)),
        ])
        data = parse_model_json(res)
        plan = validate_light_plan(data, original, state["previous_plans"])
        print("[Lighting Agent] Proposed:", plan, flush=True)
        return {"proposal": plan, "attempts": step,
                "previous_plans": [*state["previous_plans"], plan]}

    async def update(state: LightingState) -> LightingState:
        # Always start new candidate from current best, not a previously rejected one.
        await apply_energies(state["proposal"])
        return {"current": dict(state["proposal"])}

    async def render(state: LightingState) -> LightingState:
        path = run_dir / f"lighting_{state['attempts']:02d}.png"
        await render_frame(path)
        print(f"[Blender] Rendered {path}", flush=True)
        return {"current_image": str(path)}

    async def review(state: LightingState) -> LightingState:
        nonlocal best_confirmed
        best_png = load_fresh_png(Path(state["best_image"]), None)
        candidate_png = load_fresh_png(Path(state["current_image"]), None)
        instructions = (
            "You are the Vision Lighting Reviewer. IMAGE A is the currently best view, "
            "IMAGE B is the NEW light configuration at THE SAME CAMERA POSITION. "
            "Compare VISIBLE pillar/floor/wall readability, eye prominence, atmospheric depth, "
            "shadow and highlight preservation and the user's request. "
            "The geometry exists: do not suggest creating new pillars; you are ONLY evaluating LIGHTING. "
            "If B is worse, winner=best. Do not claim improvement simply because B is brighter. "
            "Return exactly one JSON object with: visible (boolean), approved (boolean), "
            "score (0..10 number), feedback (actionable string), issue_source (camera|scene|both), "
            "winner (best|candidate|tie). All fields refer to IMAGE B except winner.\n"
            "Request: " + prompt + "\n"
            "Light energies of IMAGE A: " + json.dumps(state["best"]) + "\n"
            "Light energies of IMAGE B: " + json.dumps(state["proposal"])
        )
        response = await vision.ainvoke([
            SystemMessage(content="Evaluate the two actual image inputs. JSON only."),
            HumanMessage(content=image_pair_content(instructions, best_png, candidate_png)),
        ])
        verdict = parse_visual_verdict(parse_model_json(response), has_best=True)
        updated = choose_best(verdict.winner or "tie", candidate_approved=verdict.approved,
                              best_approved=state.get("best_approved", False))
        print(f"[Vision Reviewer] {'NEW BEST' if updated else 'KEEP PREVIOUS'}; winner={verdict.winner}; "
              f"approved={verdict.approved}; {verdict.feedback}", flush=True)
        record = {"attempt": state["attempts"], "energies": state["proposal"],
                  "image": state["current_image"], "winner": verdict.winner,
                  "approved": verdict.approved, "score": verdict.score,
                  "issue_source": verdict.issue_source, "feedback": verdict.feedback,
                  "selected_as_best": updated}
        update_state: LightingState = {"history": [*state.get("history", []), record],
                                      "feedback": verdict.feedback}
        if updated:
            best_confirmed = dict(state["proposal"])
            update_state.update({"best": dict(state["proposal"]),
                                 "best_image": state["current_image"],
                                 "best_approved": verdict.approved})
        return update_state

    def review_route(state: LightingState) -> str:
        if state["attempts"] >= attempts or state.get("best_approved"):
            return "finish"
        return "propose"

    async def finish(state: LightingState) -> LightingState:
        nonlocal best_confirmed
        await apply_energies(state["best"])
        best_confirmed = dict(state["best"])
        # Keep preview.png in sync with selected preview; do not claim it is a fresh Blender render.
        copy2(state["best_image"], preview)
        evaluation = run_dir / "lighting_evaluation.json"
        evaluation.write_text(json.dumps({
            "request": prompt, "initial_energies": original,
            "selected_energies": state["best"], "selected_image": state["best_image"],
            "approved": state.get("best_approved", False),
            "attempts": state["history"],
        }, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"[Lighting Agent] Best lighting restored. Preview: {state['best_image']}")
        print(f"[Lighting Agent] Evaluation: {evaluation}")
        return {"finished": True}

    if not apply:
        print("[Dry run] No light will be changed. Use --apply to test lighting and render previews.")
        result = await planner.ainvoke([
            SystemMessage(content="Suggest only a safe named-light-energy plan as JSON, no other edits."),
            HumanMessage(content=json.dumps({"goal": prompt, "allowed_lights": original})),
        ])
        print("[Plan]", parse_model_json(result))
        return

    graph = StateGraph(LightingState)
    graph.add_node("baseline", baseline)
    graph.add_node("lighting_designer", propose)
    graph.add_node("apply_lighting", update)
    graph.add_node("render", render)
    graph.add_node("vision_reviewer", review)
    graph.add_node("restore_best", finish)
    graph.add_edge(START, "baseline")
    graph.add_edge("baseline", "lighting_designer")
    graph.add_edge("lighting_designer", "apply_lighting")
    graph.add_edge("apply_lighting", "render")
    graph.add_edge("render", "vision_reviewer")
    graph.add_conditional_edges("vision_reviewer", review_route, {
        "propose": "lighting_designer", "finish": "restore_best"
    })
    graph.add_edge("restore_best", END)

    try:
        result = await graph.compile().ainvoke({}, config={"recursion_limit": 10 * attempts + 10})
        if not result.get("finished"):
            raise RuntimeError("Lighting workflow stopped without restoring selected lights.")
    except BaseException:
        # Attempt to restore a known, verified configuration even when tools/LLM fail.
        try:
            await apply_energies(best_confirmed)
            print("[Safety] Last selected lighting restored after error.", flush=True)
        except Exception as restore_error:
            print(f"[WARNING] Could not restore lighting automatically: {restore_error}. "
                  "Restore energies manually from initial values: " + json.dumps(original), flush=True)
        raise


def main() -> None:
    p = argparse.ArgumentParser(description="FramePilot: local Lighting Agent with MCP and A/B visual review")
    p.add_argument("prompt", nargs="?", default="Make the existing corridor pillars, walls and floor legible while keeping the floating eye dramatic and mysterious.")
    p.add_argument("--model", default=os.getenv("OLLAMA_MODEL", "qwen3.5:4b"))
    p.add_argument("--vision-model", default=os.getenv("OLLAMA_VISION_MODEL"))
    p.add_argument("--attempts", type=int, default=2, help="Number of lighting candidates, 1..3. Baseline adds one render.")
    p.add_argument("--apply", action="store_true", help="Actually change lights in the running Blender scene")
    args = p.parse_args()
    if not 1 <= args.attempts <= 3:
        p.error("--attempts must be 1..3")
    try:
        asyncio.run(run(args.prompt, model_name=args.model, vision_model_name=args.vision_model,
                        attempts=args.attempts, apply=args.apply))
    except Exception as exc:
        p.exit(1, f"[FramePilot Lighting] {type(exc).__name__}: {exc}\n")

if __name__ == "__main__":
    main()
