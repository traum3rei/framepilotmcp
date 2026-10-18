"""Offline-testable comparative vision scoring and best-shot selection.

The language model's aesthetic judgement is subjective; deterministic Python
rules preserve previously approved/visible frames and restore the selected shot.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from vision_review import build_vision_content


@dataclass(frozen=True)
class VisualVerdict:
    visible: bool
    approved: bool
    feedback: str
    score: float | None
    issue_source: str
    winner: str | None = None


def image_pair_content(instructions: str, best_png: bytes, candidate_png: bytes) -> list[dict[str, Any]]:
    """Send actual images with unambiguous A/B labels in one HumanMessage."""
    result = build_vision_content("IMAGE A = current BEST frame (reference).", best_png)
    result.extend(build_vision_content("IMAGE B = NEW candidate frame.", candidate_png))
    return [{"type": "text", "text": instructions}, *result]


def parse_visual_verdict(data: dict[str, Any], *, has_best: bool) -> VisualVerdict:
    if type(data.get("visible")) is not bool or type(data.get("approved")) is not bool:
        raise ValueError("Vision verdict must include boolean visible and approved")
    feedback = data.get("feedback")
    if not isinstance(feedback, str) or not feedback.strip():
        raise ValueError("Vision verdict requires nonempty feedback")
    score = data.get("score")
    if score is not None and (type(score) not in (int, float) or not 0 <= score <= 10):
        raise ValueError("Vision score must be between 0 and 10")
    source = data.get("issue_source", "camera")
    if source not in ("camera", "scene", "both"):
        raise ValueError("issue_source must be camera, scene or both")
    winner = data.get("winner")
    if has_best and winner not in ("best", "candidate", "tie"):
        raise ValueError("Pairwise vision verdict requires winner=best, candidate or tie")
    if not has_best:
        winner = None
    visible = data["visible"]
    approved = visible and data["approved"]
    return VisualVerdict(visible=visible, approved=approved,
                         feedback=feedback.strip()[:700], score=float(score) if score is not None else None,
                         issue_source=source, winner=winner)


def prefer_new_best(best: VisualVerdict | None, candidate: VisualVerdict) -> bool:
    """Safety/intent gates take precedence over an LLM's pairwise taste vote."""
    if best is None:
        return True
    if best.approved != candidate.approved:
        return candidate.approved
    if best.visible != candidate.visible:
        return candidate.visible
    # If both frames satisfy the same hard requirements, explicitly compare
    # images rather than judging them by independently assigned scalar scores.
    return candidate.winner == "candidate"


def should_continue(*, attempt: int, maximum: int, approved: bool,
                    best_of: bool, issue_source: str) -> bool:
    if attempt >= maximum:
        return False
    # Do not keep moving the camera for a pure lighting/scene issue.
    if issue_source == "scene":
        return False
    return best_of or not approved
