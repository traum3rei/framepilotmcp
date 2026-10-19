"""Deterministic validation for FramePilot's demo-scene lighting experiments."""
from __future__ import annotations

import math
from typing import Any

LIGHT_NAMES = (
    "FramePilot / Blue corridor fill",
    "FramePilot / Warm corridor fill",
    "FramePilot / Back pillar rim",
)
MAX_ENERGY = 4000.0
MIN_ENERGY = 50.0


def managed_lights(scene: dict[str, Any]) -> dict[str, float]:
    """Only manage the three demo-fill lights; never touch eye/key lights."""
    result = {}
    for obj in scene.get("objects", []):
        if not isinstance(obj, dict) or obj.get("type") != "LIGHT":
            continue
        name, energy = obj.get("name"), obj.get("energy")
        if (isinstance(name, str) and name in LIGHT_NAMES
                and type(energy) in (int, float) and math.isfinite(energy) and energy >= 0):
            result[name] = float(energy)
    return result


def validate_light_plan(data: dict[str, Any], allowed: dict[str, float],
                        previous: list[dict[str, float]]) -> dict[str, float]:
    """Require ALL available managed lights, finite bounded energy and a meaningful change."""
    plan = data.get("energies")
    if not isinstance(plan, dict) or set(plan) != set(allowed):
        raise ValueError(f"Lighting proposal must specify exactly these light names: {list(allowed)}")
    selected = {}
    for name, value in plan.items():
        if type(value) not in (int, float) or not math.isfinite(value):
            raise ValueError(f"Non-finite or nonnumeric energy for {name}")
        if not MIN_ENERGY <= value <= MAX_ENERGY:
            raise ValueError(f"Energy for {name} must be between {MIN_ENERGY} and {MAX_ENERGY}")
        selected[name] = round(float(value), 3)
    if any(all(abs(selected[k] - past[k]) < 15 for k in allowed) for past in previous):
        raise ValueError("Lighting proposal repeats a previous configuration (within 15 W per light)")
    return selected


def choose_best(winner: str, *, candidate_approved: bool, best_approved: bool) -> bool:
    """Best-of selection: prefer an approved image, otherwise explicit A/B preference."""
    if best_approved and not candidate_approved:
        return False
    if candidate_approved and not best_approved:
        return True
    return winner == "candidate"


def changed_energies(current: dict[str, float], wanted: dict[str, float]) -> dict[str, float]:
    """Avoid redundant MCP writes and needless Blender redraws."""
    return {n: v for n, v in wanted.items() if abs(v - current[n]) >= 0.01}
