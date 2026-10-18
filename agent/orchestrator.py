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

from camera_checks import pitch_degrees, repair_low_angle_position, validate_camera, wants_low_angle
from model_json import parse_model_json
from vision_review import build_vision_content, load_fresh_png, preview_mtime, validate_vision_verdict, same_camera_proposal

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
              max_attempts: int) -> None:
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
        min_pitch = 20.0 if wants_low_angle(prompt) else None
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
            "the supplied min_pitch degrees. Keep the camera above the floor (z >= 0.15). "
            "Do not change Blender yourself. Output JSON: "
            "{\"position\":[x,y,z],\"lens_mm\":35,\"reason\":\"short\"}.",
            {"original_request": prompt, "director": director, "previous_camera": state["scene"].get("camera"),
             "attempt": attempts, "previous_feedback": state.get("feedback", "")},
        )
        proposal = {"position": result.get("position"), "target": director["target"],
                    "lens_mm": result.get("lens_mm"), "reason": str(result.get("reason", ""))[:300]}
        print(f"[Camera Operator] Proposed position={proposal['position']}, target={proposal['target']}, lens={proposal['lens_mm']}")

        # Give the model a genuine opportunity to react to detailed feedback.
        # If it proposes another invalid low angle, repair the geometric constraint
        # without spending further model calls on the same avoidable error.
        if (attempts > 1 and "Low-angle requirement FAILED" in state.get("feedback", "")
                and director.get("min_pitch") is not None
                and isinstance(proposal["position"], list)):
            pos = proposal["position"]
            target = director["target"]
            if len(pos) == 3 and all(isinstance(v, (float, int)) and math.isfinite(v)
                                     for v in pos):
                if pitch_degrees(pos, target) < director["min_pitch"]:
                    corrected = repair_low_angle_position(pos, target, director["min_pitch"])
                    if corrected is not None:
                        old_pos = state.get("proposal", {}).get("position")
                        if isinstance(old_pos, list) and len(old_pos) == 3 and math.dist(old_pos, pos) < 1e-6:
                            print("[Guardrail] Repeated identical invalid camera proposal detected")
                        proposal["position"] = [round(v, 4) for v in corrected]
                        proposal["reason"] = (proposal["reason"] +
                                              " [Geometry correction: moved camera closer to meet pitch constraint]")
                        print(f"[Guardrail] Auto-corrected low-angle to {proposal['position']} "
                              f"({pitch_degrees(proposal['position'], target):.1f} degrees up)")
        duplicate_visual = (
            state.get("visual_approved") is False
            and same_camera_proposal(state.get("proposal"), proposal)
        )
        return {"proposal": proposal, "attempts": attempts,
                "duplicate_visual": duplicate_visual}

    async def reviewer(state: ShotState) -> ShotState:
        if state.get("duplicate_visual"):
            feedback = ("Repeated almost identical camera position and lens after the image "
                        "was visually rejected. You MUST propose a visibly different framing. "
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
        return "operator" if state.get("attempts", 0) < max_attempts else "stop"

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
        return {"render_count": count}

    async def visual_reviewer(state: ShotState) -> ShotState:
        assert visual_model is not None
        print("[Vision Reviewer] Assessing actual Blender preview with local Ollama...", flush=True)
        instructions = (
            "You are an independent cinematography reviewer. You ARE receiving a real rendered "
            "Blender image in this message. Evaluate what is visually present, not only the "
            "camera metadata. The user asked: " + state["request"] + "\n"
            "The desired subject is '" + state["director"]["target_name"] + "'. "
            "The director's brief: " + state["director"]["brief"] + ".\n"
            "Look for: subject actually visible and identifiable, not blocked or cropped away, "
            "framing and scale matching the request, and visual feel (e.g. low-angle, intimidating). "
            "Be realistic: do not claim you can prove precise pitch from pixels. "
            "Geometric pitch is already checked separately. "
            "If the image is nearly black or subject cannot be identified, reject. "
            "When rejecting, give ONE or TWO specific actionable camera changes "
            "(e.g. closer, more left, wider lens, adjust height). "
            "Do not ask to modify meshes or lighting: the Camera Operator can only move the camera. "
            "Return ONLY JSON: "
            '{"visible":true/false,"approved":true/false,"feedback":"actionable visual assessment"}.'
        )
        # Send real PNG pixels, not a path or a textual summary.
        image = load_fresh_png(preview_path, None)
        message = HumanMessage(content=build_vision_content(instructions, image))
        response = await visual_model.ainvoke([
            SystemMessage(content="You are a strict visual film reviewer. Answer only valid JSON."),
            message,
        ])
        verdict = parse_model_json(response)
        approved, feedback = validate_vision_verdict(verdict)
        print(f"[Vision Reviewer] {'Approved' if approved else 'Rejected'}: {feedback}")
        return {"visual_approved": approved, "visual_feedback": feedback,
                "accepted": approved, "feedback": "Visual feedback from rendered image: " + feedback,
                "report": ("Camera change visually approved after "
                           f"{state.get('render_count', 0)} render(s).") if approved else ""}

    def verify_route(state: ShotState) -> str:
        if state.get("error") or not state.get("applied"):
            return "done"
        return "render" if render or vision else "done"

    def vision_route(state: ShotState) -> str:
        if state.get("visual_approved"):
            return "done"
        return "operator" if state.get("attempts", 0) < max_attempts else "stop"

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
    graph.add_edge(START, "inspect")
    graph.add_edge("inspect", "director")
    graph.add_edge("director", "operator")
    graph.add_edge("operator", "reviewer")
    graph.add_conditional_edges("reviewer", review_route, {
        "operator": "operator", "apply": "apply", "stop": "stop"
    })
    graph.add_edge("apply", "verify")
    graph.add_conditional_edges("verify", verify_route, {"render": "render", "done": END})
    graph.add_conditional_edges("render", lambda state: "vision" if vision else "done",
                                {"vision": "vision", "done": END})
    graph.add_conditional_edges("vision", vision_route,
                                {"operator": "operator", "done": END, "stop": "stop"})
    graph.add_edge("stop", END)
    app = graph.compile()
    result = await app.ainvoke({"request": prompt}, config={"recursion_limit": max(24, 8 * max_attempts + 12)})
    if result.get("error"):
        raise RuntimeError(result["error"])
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
    parser.add_argument("--max-attempts", type=int, default=3, help="Max camera proposals (1..6)")
    args = parser.parse_args()
    if args.max_attempts < 1 or args.max_attempts > 6:
        parser.error("--max-attempts must be between 1 and 6")
    if (args.render or args.vision) and not args.apply:
        parser.error("--render and --vision require --apply (they modify the open Blender scene)")
    asyncio.run(run(args.prompt, model_name=args.model, apply=args.apply,
                    render=args.render, vision=args.vision, vision_model=args.vision_model,
                    max_attempts=args.max_attempts))


if __name__ == "__main__":
    main()
