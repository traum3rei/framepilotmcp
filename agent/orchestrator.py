#!/usr/bin/env python3
"""FramePilot local multi-role orchestration via LangGraph + Ollama + existing MCP server.

Different model roles: Director -> Camera Operator -> Reviewer.
MCP read -> Director -> Operator -> Review -> [retry Operator] -> Apply -> Verify.
Default is a dry-run; --apply edits the currently connected Blender scene.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
from datetime import datetime, timezone
from shutil import copy2
from pathlib import Path
from typing import Any, TypedDict

from dotenv import load_dotenv
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_ollama import ChatOllama
from langgraph.graph import START, END, StateGraph

from camera_checks import pitch_degrees, normalize_camera_geometry, repair_low_angle_position, validate_camera, wants_low_angle
from model_json import parse_model_json
from shot_context import shot_context
from vision_review import build_vision_content, load_fresh_png, preview_mtime, same_camera_proposal
from comparison_review import (VisualVerdict, image_pair_content, parse_visual_verdict,
                               prefer_new_best, should_continue)
from scene_awareness import scene_visual_context

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
load_dotenv(HERE / ".env")


class ShotState(TypedDict, total=False):
    request: str
    scene: dict[str, Any]
    director: dict[str, Any]
    proposal: dict[str, Any]
    feedback: str
    accepted: bool
    attempts: int
    report: str
    error: str
    applied: bool
    visual_approved: bool
    visual_feedback: str
    render_count: int
    duplicate_visual: bool
    current_preview: str
    best_preview: str
    best_proposal: dict[str, Any]
    best_verdict: dict[str, Any]
    current_verdict: dict[str, Any]
    best_count: int
    history: list[dict[str, Any]]


def parse_tool_text(result: Any) -> dict[str, Any]:
    """Support common MCP adapter return shapes without printing image base64."""
    if hasattr(result, "content"):
        result = result.content
    if isinstance(result, dict) and "content" in result:
        if result.get("isError"):
            raise RuntimeError(str(result["content"]))
        result = result["content"]
    if isinstance(result, list):
        text = next((v.get("text") for v in result if isinstance(v, dict) and v.get("type") == "text"), None)
        if text is None:
            raise RuntimeError("Tool response has no text block")
        result = text
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Non-JSON response from MCP: {result[:300]}") from exc
    if not isinstance(result, dict):
        raise RuntimeError(f"Unexpected MCP response: {type(result).__name__}")
    if result.get("isError"):
        raise RuntimeError(str(result.get("content", "MCP error")))
    return result


def objects_summary(scene: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {"name": ob["name"], "type": ob.get("type"), "location": ob.get("location")}
        for ob in scene.get("objects", []) if ob.get("name") and ob.get("location")
    ]


async def run(prompt: str, *, model_name: str, apply: bool, render: bool,
              vision: bool = False, vision_model: str | None = None,
              max_attempts: int, best_of: bool = False,
              min_low_angle_pitch: float = 20.0) -> None:
    tsx = ROOT / "node_modules" / ".bin" / "tsx"
    server = ROOT / "src" / "server.ts"
    if not tsx.is_file() or not server.is_file():
        raise RuntimeError("Cannot find FramePilot. Put agent/ next to src/ and run npm install.")

    client = MultiServerMCPClient({
        "framepilot": {
            "transport": "stdio", "command": str(tsx),
            "args": [str(server)], "cwd": str(ROOT),
        }
    })
    all_tools = {tool.name: tool for tool in await client.get_tools()}
    required = {"inspect_scene", "set_camera"}
    if render or vision:
        required.add("render_preview")
    missing = required - all_tools.keys()
    if missing:
        raise RuntimeError(f"Missing MCP tools: {', '.join(sorted(missing))}")

    # Qwen 3.5 may exhaust a small output budget on internal thinking and
    # return empty message.content. Disable thinking for these short JSON jobs.
    # Do not force format="json": some Ollama/Qwen 3.5 combinations ignore it
    # when think=False. The parser below accepts valid JSON in plain text too.
    model = ChatOllama(model=model_name, temperature=0, reasoning=False, num_predict=650)
    visual_model = ChatOllama(model=vision_model or model_name, temperature=0,
                              reasoning=False, num_predict=450) if vision else None
    preview_path = ROOT / "runtime" / "preview.png"
    preview_archive = (ROOT / "runtime" / "vision_runs" /
                       datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ"))

    async def ask(role: str, system: str, user: dict[str, Any]) -> dict[str, Any]:
        print(f"[{role}] Working...", flush=True)
        response = await model.ainvoke([
            SystemMessage(content=system + " Return only a valid JSON object. No markdown."),
            HumanMessage(content=json.dumps(user, ensure_ascii=False)),
        ])
        return parse_model_json(response)

    async def inspect(state: ShotState) -> ShotState:
        scene = parse_tool_text(await all_tools["inspect_scene"].ainvoke({}))
        if not isinstance(scene.get("objects"), list):
            raise RuntimeError("inspect_scene returned no objects list")
        print(f"[MCP] inspect_scene: {scene.get('object_count', len(scene['objects']))} objects; mode={scene.get('mode', 'unknown')}")
        return {"scene": scene, "attempts": 0, "feedback": ""}

    async def director(state: ShotState) -> ShotState:
        scene = state["scene"]
        result = await ask(
            "Director", 
            "You are a cinematography director. Choose the EXACT EXISTING object that is the subject, "
            "then articulate the framing aesthetic. Never invent object names or coordinates. "
            "Output JSON: {\"target_name\":\"existing object name\",\"brief\":\"concise shot direction\"}.",
            {"request": prompt, "objects": objects_summary(scene), "camera": scene.get("camera")},
        )
        name = str(result.get("target_name", ""))
        names = [ob["name"] for ob in objects_summary(scene)]
        if name not in names:
            raise ValueError(f"Director selected unknown object {name!r}. Existing objects: {names}")
        subject = next(ob for ob in scene["objects"] if ob["name"] == name)
        if len(subject.get("location", [])) != 3:
            raise ValueError("Selected object has no valid center")
        min_pitch = min_low_angle_pitch if wants_low_angle(prompt) else None
        print(f"[Director] Target: {name}; min upward angle: {min_pitch if min_pitch is not None else 'none'}")
        return {"director": {"target_name": name, "target": subject["location"],
                             "brief": str(result.get("brief", ""))[:500], "min_pitch": min_pitch}}

    async def operator(state: ShotState) -> ShotState:
        # A specialized LLM role translates a cinematic brief to numeric camera coordinates.
        attempts = state.get("attempts", 0) + 1
        director = state["director"]
        result = await ask(
            "Camera Operator",
            "You are a Blender camera operator. Propose a REAL camera position and lens. "
            "The exact target is fixed and will be filled in by code. "
            "For a low-angle shot, place camera noticeably BELOW the target, pointing UP at least "
            "the supplied min_pitch degrees. Keep camera z >= 0.35 for safe floor clearance. "
            "Remember low-angle means the camera is physically BELOW the subject; a very steep "
            "upward pitch can crop out nearby architecture, so balance pitch and background visibility. "
            "If previous visual feedback asks to lower a camera already at floor clearance, "
            "DO NOT lower it further: move closer horizontally, offset left/right, or change "
            "focal length instead. Use previous_rendered_camera to make a MEANINGFULLY DIFFERENT "
            "shot after visual rejection; do not repeat coordinates and lens. "
            "Do not change Blender yourself. Output JSON: "
            "{\"position\":[x,y,z],\"lens_mm\":35,\"reason\":\"short\"}.",
            {"original_request": prompt, "director": director, "initial_camera": state["scene"].get("camera"),
             "existing_scene_geometry_and_lighting": scene_visual_context(state["scene"]),
             "attempt": attempts, "previous_feedback": state.get("feedback", ""),
             "best_rendered_camera": (
                 {"proposal": state["best_proposal"],
                  "constraints": shot_context(state["best_proposal"]),
                  "visual_review": state.get("best_verdict")}
                 if state.get("best_proposal") else None),
             "previous_rendered_camera": (
                 {"proposal": state["proposal"],
                  "constraints": shot_context(state["proposal"]),
                  "visual_feedback": state.get("visual_feedback")}
                 if state.get("visual_approved") is False and state.get("proposal") else None)},
        )
        proposal = {"position": result.get("position"), "target": director["target"],
                    "lens_mm": result.get("lens_mm"), "reason": str(result.get("reason", ""))[:300]}
        print(f"[Camera Operator] Proposed position={proposal['position']}, target={proposal['target']}, lens={proposal['lens_mm']}")

        # Correct simple geometric mistakes deterministically on the FIRST
        # proposal too, so --max-attempts 1 can still produce a valid shot.
        # This does not guarantee collision-free placement or good composition;
        # numerical reviewer and (if enabled) visual reviewer still run.
        pos, target = proposal["position"], director["target"]
        if isinstance(pos, list) and isinstance(target, list):
            corrected = normalize_camera_geometry(pos, target, director.get("min_pitch"))
            if corrected is not None:
                corrected = [round(v, 4) for v in corrected]
                if len(pos) == 3 and any(abs(a - b) > 1e-5 for a, b in zip(corrected, pos)):
                    proposal["position"] = corrected
                    proposal["reason"] += " [Auto-repaired geometry: floor clearance / upward pitch]"
                    print(f"[Guardrail] Geometry corrected on attempt {attempts}: "
                          f"{pos} -> {corrected}; "
                          f"upward pitch {pitch_degrees(corrected, target):.1f}°")

        duplicate_visual = (
            state.get("render_count", 0) > 0
            and (same_camera_proposal(state.get("proposal"), proposal)
                 or same_camera_proposal(state.get("best_proposal"), proposal))
        )
        return {"proposal": proposal, "attempts": attempts,
                "duplicate_visual": duplicate_visual}

    async def reviewer(state: ShotState) -> ShotState:
        if state.get("duplicate_visual"):
            feedback = ("Repeated an almost identical rendered camera position and lens. "
                        "You MUST propose a visibly different framing to justify another render. "
                        + state.get("visual_feedback", ""))
            print(f"[Guardrail] Rejected duplicate: {feedback}")
            return {"accepted": False, "feedback": feedback}
        director = state["director"]
        proposal = state["proposal"]
        pos, target, lens = proposal["position"], proposal["target"], proposal["lens_mm"]
        if not isinstance(pos, list) or not isinstance(target, list) or not isinstance(lens, (int, float)):
            reasons = ["Operator did not produce [x,y,z] position and numeric lens."]
        else:
            reasons = validate_camera(pos, target, lens, director["min_pitch"])
        if reasons:
            feedback = " ".join(reasons)
            # Actionable numerical feedback, not just "try again".
            if (director.get("min_pitch") is not None and isinstance(pos, list)
                    and len(pos) == 3
                    and all(isinstance(v, (int, float)) and math.isfinite(v) for v in pos)
                    and isinstance(target, list) and len(target) == 3
                    and all(isinstance(v, (int, float)) and math.isfinite(v) for v in target)
                    and pitch_degrees(pos, target) < director["min_pitch"]):
                suggested = repair_low_angle_position(pos, target, director["min_pitch"])
                if suggested is not None:
                    feedback += (f" Suggested valid position: {[round(v, 3) for v in suggested]}; "
                                 f"upward pitch: {pitch_degrees(suggested, target):.1f} deg. "
                                 "Keep z above floor and reduce XY distance to the target.")
                else:
                    feedback += " Ensure camera is BELOW the target and above the floor."
            print(f"[Guardrail] Rejected: {feedback}")
            return {"accepted": False, "feedback": feedback}
        pitch = pitch_degrees(pos, target)
        review = await ask(
            "Reviewer",
            "You are an independent camera composition reviewer. You only have numerical scene "
            "information, NOT a rendered image. Check conceptual framing and intent, not visual appearance. "
            "Never say you saw a render. Camera pitch is positive when looking UP. "
            "Output JSON: {\"approved\":true/false,\"feedback\":\"concise reason\"}. "
            "Approve plausible shots that match constraints; do not invent extra requirements.",
            {"user_request": prompt, "director": director, "proposal": proposal,
             "computed_upward_pitch_deg": round(pitch, 1)},
        )
        approved = review.get("approved") is True
        feedback = str(review.get("feedback", ""))[:350]
        print(f"[Reviewer] {'Approved' if approved else 'Rejected'}; pitch {pitch:.1f}°; {feedback}")
        return {"accepted": approved, "feedback": feedback}

    def review_route(state: ShotState) -> str:
        if state["accepted"]:
            return "apply"
        return "operator" if state.get("attempts", 0) < max_attempts else ("finish" if state.get("best_preview") else "stop")

    async def apply_camera(state: ShotState) -> ShotState:
        p = state["proposal"]
        if not apply:
            print("[Dry run] No Blender changes. Pass --apply to update the open scene.")
            return {"applied": False, "report": "Validated camera proposal (dry-run; no scene changes)."}
        payload = {"position": p["position"], "target": p["target"], "lens_mm": p["lens_mm"]}
        response = parse_tool_text(await all_tools["set_camera"].ainvoke(payload))
        if "camera" not in response:
            raise RuntimeError(f"set_camera returned unexpected result: {response}")
        print(f"[MCP] set_camera succeeded; mode={response.get('mode', 'unknown')}")
        return {"applied": True}

    async def verify(state: ShotState) -> ShotState:
        if not state.get("applied"):
            return {}
        response = parse_tool_text(await all_tools["inspect_scene"].ainvoke({}))
        actual = (response.get("camera") or {}).get("position")
        expected = state["proposal"]["position"]
        if not isinstance(actual, list) or len(actual) != 3 or math.dist(actual, expected) > 0.03:
            return {"error": f"Camera position verification failed: actual={actual}; expected={expected}"}
        print(f"[Verify] Blender camera position matches proposal; mode={response.get('mode', 'unknown')}")
        return {"report": "Camera change applied and position verified with inspect_scene."}

    async def render_node(state: ShotState) -> ShotState:
        # The MCP tool writes runtime/preview.png and returns its image as MCP content.
        # We load the known, local output path for portability across MCP adapter versions.
        old_time = preview_mtime(preview_path)
        result = await all_tools["render_preview"].ainvoke({})
        if isinstance(result, dict) and result.get("isError"):
            raise RuntimeError(str(result.get("content", "render failed")))
        # Always require a fresh, real PNG from the current camera.
        image = load_fresh_png(preview_path, old_time)
        count = state.get("render_count", 0) + 1
        preview_archive.mkdir(parents=True, exist_ok=True)
        archived = preview_archive / f"attempt_{count:02d}.png"
        copy2(preview_path, archived)
        print(f"[MCP] Render {count}: {preview_path} ({len(image)} bytes); saved {archived}")
        return {"render_count": count, "current_preview": str(archived)}

    async def visual_reviewer(state: ShotState) -> ShotState:
        assert visual_model is not None
        best_path = state.get("best_preview")
        comparison = bool(best_path)
        print(f"[Vision Reviewer] {'Comparing candidate against best frame' if comparison else 'Assessing first frame'}...", flush=True)
        instructions = (
            "You are a visual cinematography reviewer looking at ACTUAL PNG renderings. "
            "User request: " + state["request"] + "\n"
            "Subject: " + state["director"]["target_name"] + ". "
            "Art direction: " + state["director"]["brief"] + ".\n"
            "Evaluate subject visibility, relative screen-space size, depth cues, perspective, "
            "framing, image readability and adherence to the requested mood. "
            "Never infer artistic success just because geometric camera pitch passed. "
            "If the background is too dark or lacks useful scenery, distinguish CAMERA framing "
            "from SCENE lighting/geometry issues. If only scene lighting can fix the issue, "
            "set issue_source=scene. If both matter, use both. "
            "The Camera Operator can only reposition the camera or change its lens. "
            "You are also supplied with an inventory of ACTUAL EXISTING objects. If pillars, "
            "walls or ceiling exist in this inventory, NEVER claim they must be created or added "
            "just because they are invisible in the render. Say that the existing architecture "
            "is dark, occluded or outside the frame (or that the cause is uncertain). "
            "Lighting changes may be needed separately from camera changes. "
            "Never suggest moving it below minimum_camera_z. "
            "Avoid vague criticism: mention what visually changed and give a concrete next step. "
            "Rate NEW candidate from 0-10 based on this request, independently of technical pitch. "
            "Return ONLY one JSON object with boolean visible, boolean approved, numeric score, "
            "string feedback, and issue_source as camera|scene|both. "
        )
        if comparison:
            instructions += (
                "You are receiving TWO REAL IMAGES in order: IMAGE A is the saved BEST so far; "
                "IMAGE B is the NEW candidate. Compare them directly. "
                "Output an additional winner field, exactly best|candidate|tie. "
                "winner describes which image better meets the user request, not which has "
                "a larger numerical score. Compare subject relative size explicitly when relevant. "
                "If the new shot is smaller or less dramatic, prefer best, even if it is newer. "
                "'visible', 'approved', 'score', 'issue_source' and 'feedback' refer to IMAGE B. "
                "Your feedback should explain the comparison and how to beat IMAGE A."
            )
        instructions += "\nCandidate camera constraints: " + json.dumps(shot_context(state["proposal"]), ensure_ascii=False)
        instructions += "\nBlender scene inspection (known geometry/lights, NOT proof of visibility): " + json.dumps(
            scene_visual_context(state["scene"]), ensure_ascii=False)
        current_png = load_fresh_png(Path(state["current_preview"]), None)
        if comparison:
            best_png = load_fresh_png(Path(best_path), None)
            content = image_pair_content(instructions, best_png, current_png)
        else:
            content = build_vision_content(instructions, current_png)
        response = await visual_model.ainvoke([
            SystemMessage(content="Assess the supplied image(s) carefully. Respond only in JSON."),
            HumanMessage(content=content),
        ])
        try:
            verdict = parse_visual_verdict(parse_model_json(response), has_best=comparison)
        except (ValueError, KeyError) as exc:
            if not comparison:
                raise RuntimeError(f"First visual review was invalid: {exc}") from exc
            # A local 4B model may occasionally omit the pairwise preference.
            # Never replace an existing best based on unparseable feedback.
            print(f"[Vision Reviewer] Invalid comparison ({exc}); preserving best frame.")
            verdict = VisualVerdict(visible=False, approved=False, score=None,
                                    feedback=f"Pairwise review failed: {exc}; keep previous best.",
                                    issue_source="scene", winner="best")
        best_old = (VisualVerdict(**state["best_verdict"])
                    if state.get("best_verdict") else None)
        update_best = prefer_new_best(best_old, verdict)
        info = {
            "visible": verdict.visible, "approved": verdict.approved,
            "score": verdict.score, "feedback": verdict.feedback,
            "issue_source": verdict.issue_source, "winner": verdict.winner,
        }
        print(f"[Vision Reviewer] {'Approved' if verdict.approved else 'Rejected'}; "
              f"score={verdict.score}; winner={verdict.winner or 'first'}; "
              f"issue={verdict.issue_source}: {verdict.feedback}")
        result: ShotState = {
            "visual_approved": verdict.approved,
            "visual_feedback": verdict.feedback,
            "current_verdict": info,
            "history": [*state.get("history", []), {
                "attempt": state.get("attempts"), "render": state.get("render_count"),
                "file": state.get("current_preview"), "proposal": state["proposal"],
                "review": info, "selected_as_best": update_best,
            }],
            "accepted": verdict.approved,
            "feedback": "Visual feedback: " + verdict.feedback,
        }
        if update_best:
            result.update({"best_preview": state["current_preview"],
                           "best_proposal": state["proposal"],
                           "best_verdict": info,
                           "best_count": state.get("render_count", 0)})
            print(f"[Best-of-N] New best: attempt {state.get('render_count', 0)}")
        else:
            print(f"[Best-of-N] Kept attempt {state.get('best_count')} as best")
        return result

    def verify_route(state: ShotState) -> str:
        if state.get("error") or not state.get("applied"):
            return "done"
        return "render" if render or vision else "done"

    def vision_route(state: ShotState) -> str:
        verdict = state.get("current_verdict") or {}
        if should_continue(attempt=state.get("attempts", 0), maximum=max_attempts,
                           approved=state.get("visual_approved", False),
                           best_of=best_of, issue_source=verdict.get("issue_source", "camera")):
            return "operator"
        return "finish"

    async def finish(state: ShotState) -> ShotState:
        best = state.get("best_proposal")
        if not best:
            return {"error": "No rendered camera candidate was available."}
        selected = state.get("best_verdict") or {}
        current = state.get("proposal") or {}
        restored = (current.get("position") != best.get("position")
                    or current.get("target") != best.get("target")
                    or current.get("lens_mm") != best.get("lens_mm"))
        if restored:
            payload = {"position": best["position"], "target": best["target"],
                       "lens_mm": best["lens_mm"]}
            parsed = parse_tool_text(await all_tools["set_camera"].ainvoke(payload))
            if "camera" not in parsed:
                raise RuntimeError("Unable to restore best camera: set_camera returned unexpected response")
            actual_scene = parse_tool_text(await all_tools["inspect_scene"].ainvoke({}))
            actual = (actual_scene.get("camera") or {}).get("position")
            if not isinstance(actual, list) or len(actual) != 3 or math.dist(actual, best["position"]) > 0.03:
                raise RuntimeError(f"Best camera restoration could not be verified: {actual}")
            # This image corresponds to the selected camera; it is an archived render,
            # not a claim that Blender re-rendered during restoration.
            copy2(state["best_preview"], preview_path)
            print(f"[Best-of-N] Restored camera from attempt {state['best_count']} (verified)")
        verdict = "APPROVED" if selected.get("approved") else "NOT APPROVED"
        print(f"[Best-of-N] Selected attempt {state['best_count']} / {state.get('render_count',0)} "
              f"render(s), score={selected.get('score')}, {verdict}.")
        preview_archive.mkdir(parents=True, exist_ok=True)
        manifest = preview_archive / "evaluation.json"
        manifest.write_text(json.dumps({
            "request": state["request"],
            "selected_render": state.get("best_count"),
            "selected_preview": state["best_preview"],
            "selected_camera": best,
            "selected_review": selected,
            "runs": state.get("history", []),
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[Best-of-N] Comparison report: {manifest}")
        report = (f"Selected attempt {state['best_count']} ({verdict}). "
                  f"Image: {state['best_preview']}. "
                  f"Review: {selected.get('feedback', '')}")
        if not selected.get("approved"):
            report += (" Warning: no candidate fully met the artistic requirements. "
                       "The best available camera remains applied; it is not claimed as a successful shot.")
        if selected.get("issue_source") in ("scene", "both"):
            report += " Some remaining issues may require editing scene lighting or geometry."
        return {"proposal": best, "report": report}

    async def stop(state: ShotState) -> ShotState:
        extra = (" Last attempted camera remains in the Blender scene; use Undo or reload "
                 "your original file if you want to revert. No further changes were made."
                 if state.get("applied") else " No camera was applied.")
        return {"error": (f"Shot rejected after {state.get('attempts')} attempt(s): "
                          f"{state.get('feedback')}" + extra)}

    graph = StateGraph(ShotState)
    graph.add_node("inspect", inspect)
    graph.add_node("director", director)
    graph.add_node("operator", operator)
    graph.add_node("reviewer", reviewer)
    graph.add_node("apply", apply_camera)
    graph.add_node("verify", verify)
    graph.add_node("render", render_node)
    graph.add_node("vision", visual_reviewer)
    graph.add_node("stop", stop)
    graph.add_node("finish", finish)
    graph.add_edge(START, "inspect")
    graph.add_edge("inspect", "director")
    graph.add_edge("director", "operator")
    graph.add_edge("operator", "reviewer")
    graph.add_conditional_edges("reviewer", review_route, {
        "operator": "operator", "apply": "apply", "stop": "stop", "finish": "finish"
    })
    graph.add_edge("apply", "verify")
    graph.add_conditional_edges("verify", verify_route, {"render": "render", "done": END})
    graph.add_conditional_edges("render", lambda state: "vision" if vision else "done",
                                {"vision": "vision", "done": END})
    graph.add_conditional_edges("vision", vision_route,
                                {"operator": "operator", "finish": "finish"})
    graph.add_edge("finish", END)
    graph.add_edge("stop", END)
    app = graph.compile()
    result = await app.ainvoke({"request": prompt}, config={"recursion_limit": max(24, 8 * max_attempts + 12)})
    if result.get("error"):
        # A rejected shot is a valid workflow outcome, not a Python programming error.
        # Exit with a concise nonzero status instead of an unreadable traceback.
        raise SystemExit("[FramePilot] " + result["error"])
    print("\nRESULT:", result.get("report", "No result"))
    print("PROPOSAL:", json.dumps(result.get("proposal"), ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser(description="FramePilot: local multi-agent camera direction")
    parser.add_argument("prompt", help="Natural-language camera direction")
    parser.add_argument("--provider", choices=["ollama"], default="ollama",
                        help="Orchestrator intentionally uses local Ollama only; cloud quota stays protected.")
    parser.add_argument("--model", default=os.getenv("OLLAMA_MODEL", "qwen3.5:9b"))
    parser.add_argument("--apply", action="store_true", help="Actually call set_camera after approval")
    parser.add_argument("--render", action="store_true", help="Render once after successful camera update (without vision evaluation)")
    parser.add_argument("--vision", action="store_true", help="Review rendered PNG with local Ollama vision model and refine camera if needed")
    parser.add_argument("--vision-model", default=os.getenv("OLLAMA_VISION_MODEL"),
                        help="Ollama vision model (defaults to --model)")
    parser.add_argument("--max-attempts", type=int, default=3, help="Max camera proposals (1..6) for normal mode")
    parser.add_argument("--min-low-angle-pitch", type=float, default=20.0,
                        help="Minimum upward pitch for low-angle requests (degrees); "
                             "try 10-12 when you need more of a corridor in frame")
    parser.add_argument("--best-of", type=int, metavar="N", default=None,
                        help="Visually compare up to N proposals (1..6) and restore the best shot; requires --apply --vision")
    args = parser.parse_args()
    if not math.isfinite(args.min_low_angle_pitch) or not 1 <= args.min_low_angle_pitch <= 60:
        parser.error("--min-low-angle-pitch must be between 1 and 60 degrees")
    if args.max_attempts < 1 or args.max_attempts > 6:
        parser.error("--max-attempts must be between 1 and 6")
    if args.best_of is not None and (not 1 <= args.best_of <= 6 or not args.vision):
        parser.error("--best-of requires --vision and a value between 1 and 6")
    if (args.render or args.vision) and not args.apply:
        parser.error("--render and --vision require --apply (they modify the open Blender scene)")
    asyncio.run(run(args.prompt, model_name=args.model, apply=args.apply,
                    render=args.render, vision=args.vision, vision_model=args.vision_model,
                    max_attempts=args.best_of or args.max_attempts,
                    best_of=args.best_of is not None,
                    min_low_angle_pitch=args.min_low_angle_pitch))


if __name__ == "__main__":
    main()
