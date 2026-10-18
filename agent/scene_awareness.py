"""Compact grounding for camera planning and visual review; no Blender/LLM dependency."""
from __future__ import annotations

from typing import Any

ARCH_TOKENS = (
    "wall", "pillar", "column", "floor", "ceiling", "arch", "beam", "corridor",
    "stair", "hall", "door", "window", "säule", "wand", "boden", "decke",
)


def scene_visual_context(scene: dict[str, Any], *, limit: int = 24) -> dict[str, Any]:
    """Summarize known scene geometry/lights without hallucinating off-frame visibility."""
    objects = scene.get("objects", [])
    if not isinstance(objects, list):
        objects = []
    architecture = []
    lights = []
    for obj in objects:
        if not isinstance(obj, dict) or not isinstance(obj.get("name"), str):
            continue
        name = obj["name"]
        typ = obj.get("type")
        if typ == "LIGHT":
            lights.append({"name": name, "position": obj.get("location"),
                           "energy": obj.get("energy", "unknown")})
        elif typ == "MESH" and any(token in name.lower() for token in ARCH_TOKENS):
            architecture.append({"name": name, "position": obj.get("location")})
    return {
        "geometry_known_to_exist": architecture[:limit],
        "architecture_count_from_inspection": len(architecture),
        "lights_known_to_exist": lights[:12],
        "inspection_truncated": bool(scene.get("truncated", False)),
        "visibility_note": (
            "Existence in scene DOES NOT establish visibility in the render. "
            "Darkness, occlusion and camera framing may hide objects."
        ),
    }
