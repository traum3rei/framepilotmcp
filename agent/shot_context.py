"""Numerical context for the camera operator and vision reviewer.

No Blender/LLM dependency. This does not itself accept or reject a shot.
"""
from __future__ import annotations

import math
from typing import Any


def shot_context(proposal: dict[str, Any], *, floor_z: float = 0.35) -> dict[str, Any]:
    """Summarize safe camera changes for a rendered shot.

    Valid geometry is a prerequisite: this helper is used after geometry review.
    """
    pos = proposal["position"]
    target = proposal["target"]
    if (not isinstance(pos, (list, tuple)) or not isinstance(target, (list, tuple))
            or len(pos) != 3 or len(target) != 3
            or not all(isinstance(x, (int, float)) and math.isfinite(x)
                       for x in (*pos, *target))):
        raise ValueError("shot_context needs finite 3D camera and target positions")
    horizontal = math.hypot(target[0] - pos[0], target[1] - pos[1])
    upward = math.degrees(math.atan2(target[2] - pos[2], horizontal))
    safe_lower = max(0.0, pos[2] - floor_z)
    return {
        "camera_z": round(pos[2], 3),
        "subject_z": round(target[2], 3),
        "horizontal_subject_distance": round(horizontal, 3),
        "upward_pitch_deg": round(upward, 2),
        "lens_mm": proposal.get("lens_mm"),
        "minimum_camera_z": floor_z,
        "remaining_safe_downward_movement": round(safe_lower, 3),
        "can_lower_meaningfully": safe_lower >= 0.2,
        "allowed_adjustments": [
            "move closer horizontally while staying above the floor",
            "offset the camera left or right for stronger perspective",
            "try a wider focal length together with a closer position",
        ],
    }
