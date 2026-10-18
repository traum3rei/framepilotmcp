"""Helpers for FramePilot's local Ollama vision feedback loop.

Only a freshly rendered local PNG is sent to the local Ollama vision model.
No third-party vision API is contacted by this module.
"""
from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

MAX_PREVIEW_BYTES = 15 * 1024 * 1024
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def preview_mtime(path: Path) -> int | None:
    try:
        return path.stat().st_mtime_ns
    except FileNotFoundError:
        return None


def load_fresh_png(path: Path, previous_mtime: int | None) -> bytes:
    """Fail closed rather than accidentally letting the LLM assess an old preview."""
    try:
        stat = path.stat()
    except FileNotFoundError as exc:
        raise RuntimeError(
            f"Blender returned from render_preview but {path} does not exist. "
            "Check the FramePilot Live add-on Project directory."
        ) from exc
    if previous_mtime is not None and stat.st_mtime_ns <= previous_mtime:
        raise RuntimeError(
            f"Preview {path} was not updated by the last render_preview call. "
            "Stopping to avoid reviewing an old image."
        )
    if stat.st_size < 32 or stat.st_size > MAX_PREVIEW_BYTES:
        raise RuntimeError(f"Unexpected PNG preview size: {stat.st_size} bytes")
    png = path.read_bytes()
    if not png.startswith(PNG_SIGNATURE):
        raise RuntimeError(f"Preview {path} is not a valid PNG file")
    return png


def build_vision_content(instructions: str, png: bytes) -> list[dict[str, Any]]:
    if not png.startswith(PNG_SIGNATURE):
        raise ValueError("Vision input must be PNG bytes")
    return [
        {"type": "text", "text": instructions},
        {"type": "image_url", "image_url": "data:image/png;base64," + base64.b64encode(png).decode("ascii")},
    ]


def validate_vision_verdict(value: dict[str, Any]) -> tuple[bool, str]:
    """Require an explicit verdict; never turn malformed output into approval."""
    if type(value.get("visible")) is not bool or type(value.get("approved")) is not bool:
        raise ValueError("Vision Reviewer must return boolean 'visible' and 'approved' fields")
    feedback = value.get("feedback")
    if not isinstance(feedback, str) or not feedback.strip():
        raise ValueError("Vision Reviewer returned no actionable feedback")
    approved = value["visible"] and value["approved"]
    if not value["visible"]:
        feedback = "Subject not clearly visible. " + feedback
    return approved, feedback.strip()[:650]


def same_camera_proposal(previous: dict[str, Any] | None, current: dict[str, Any]) -> bool:
    """Prevent wasting time re-rendering an already visually rejected shot."""
    import math

    if not previous:
        return False
    pos1, pos2 = previous.get("position"), current.get("position")
    lens1, lens2 = previous.get("lens_mm"), current.get("lens_mm")
    if not isinstance(pos1, (list, tuple)) or not isinstance(pos2, (list, tuple)):
        return False
    if len(pos1) != 3 or len(pos2) != 3:
        return False
    values = [*pos1, *pos2, lens1, lens2]
    if not all(type(x) in (int, float) and math.isfinite(x) for x in values):
        return False
    return math.dist(pos1, pos2) < 0.05 and abs(lens1 - lens2) < 0.5
