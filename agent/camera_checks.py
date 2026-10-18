"""Pure, deterministic camera checks: no LLM or Blender dependency."""
from __future__ import annotations

import math
from typing import Sequence


def pitch_degrees(position: Sequence[float], target: Sequence[float]) -> float:
    """Positive pitch is UPWARD from the camera towards the target."""
    dz = float(target[2]) - float(position[2])
    horizontal = math.hypot(float(target[0]) - float(position[0]), float(target[1]) - float(position[1]))
    return math.degrees(math.atan2(dz, horizontal))


def validate_camera(position: Sequence[float], target: Sequence[float], lens_mm: float,
                    min_pitch: float | None = None) -> list[str]:
    errors: list[str] = []
    if len(position) != 3 or len(target) != 3:
        return ["Camera position and target must each contain 3 numbers."]
    if not all(isinstance(v, (int, float)) and math.isfinite(v) for v in [*position, *target, lens_mm]):
        return ["Nonfinite or nonnumeric camera coordinates/lens."]
    if not 18 <= lens_mm <= 120:
        errors.append("Lens must be between 18 and 120 mm.")
    if math.dist(position, target) < 0.2:
        errors.append("Camera must not coincide with its target.")
    if position[2] < 0.15:
        errors.append("Camera is too close to or below the demo scene floor (z < 0.15).")
    if min_pitch is not None:
        pitch = pitch_degrees(position, target)
        if pitch < min_pitch:
            errors.append(f"Low-angle requirement FAILED: {pitch:.1f} degrees upwards; needs >= {min_pitch:.1f}.")
    return errors


def wants_low_angle(prompt: str) -> bool:
    text = prompt.lower()
    terms = ("low-angle", "low angle", "from below", "looking up", "look up",
             "untersicht", "von unten", "nach oben schauen", "froschperspektive")
    return any(term in text for term in terms)


def repair_low_angle_position(position: Sequence[float], target: Sequence[float],
                              min_pitch: float, margin: float = 3.0) -> list[float] | None:
    """Move a camera toward its target in the XY plane until it looks sufficiently up.

    Retains azimuth and camera height; never modifies Blender. Returns None if a
    safe geometric repair is impossible (e.g. camera is above its subject).
    Does not check collisions, occlusion or aesthetic quality.
    """
    if len(position) != 3 or len(target) != 3:
        return None
    if not all(isinstance(v, (int, float)) and math.isfinite(v) for v in (*position, *target)):
        return None
    if not 0 < min_pitch + margin < 89:
        return None
    if position[2] < 0.15:
        return None
    dz = target[2] - position[2]
    if dz <= 0:
        return None
    dx, dy = position[0] - target[0], position[1] - target[1]
    horizontal = math.hypot(dx, dy)
    if horizontal == 0:
        return [float(v) for v in position]
    max_horizontal = dz / math.tan(math.radians(min_pitch + margin))
    if horizontal <= max_horizontal:
        return [float(v) for v in position]
    factor = max_horizontal / horizontal
    return [float(target[0] + dx * factor), float(target[1] + dy * factor), float(position[2])]


def normalize_camera_geometry(position: Sequence[float], target: Sequence[float],
                              min_pitch: float | None,
                              *, floor_clearance: float = 0.35,
                              pitch_margin: float = 3.0) -> list[float] | None:
    """Repair minor floor/pitch violations without another LLM call.

    First clamps camera height above the demo-scene floor, then moves the
    camera horizontally closer to the target if a requested minimum upward
    viewing angle is not met. Never changes target/lens. Only checks geometry;
    Blender collision/visibility and visual composition still need review.

    Returns None if a safe geometric repair is impossible.
    """
    if len(position) != 3 or len(target) != 3:
        return None
    if not all(isinstance(v, (int, float)) and math.isfinite(v)
               for v in (*position, *target)):
        return None
    if not 0.15 <= floor_clearance < 100:
        raise ValueError("floor_clearance must be >= 0.15")

    corrected = [float(v) for v in position]
    corrected[2] = max(corrected[2], floor_clearance)

    if min_pitch is not None:
        if corrected[2] >= float(target[2]):
            return None
        repaired = repair_low_angle_position(corrected, target, min_pitch,
                                             margin=pitch_margin)
        if repaired is None:
            return None
        corrected = repaired

    # Use a valid placeholder lens solely to check geometric constraints.
    # The actual proposal lens is validated by the caller.
    if validate_camera(corrected, target, 35, min_pitch):
        return None
    return corrected
