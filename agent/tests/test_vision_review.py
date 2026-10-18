"""Offline tests: no Blender, LangGraph or Ollama required."""
import base64
import os
import tempfile
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from vision_review import (
    build_vision_content, load_fresh_png, preview_mtime,
    same_camera_proposal, validate_vision_verdict,
)

PNG = b"\x89PNG\r\n\x1a\n" + b"PNG data for offline tests" * 3


class VisionReviewTests(unittest.TestCase):
    def test_missing_preview_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaisesRegex(RuntimeError, "does not exist"):
                load_fresh_png(Path(td) / "preview.png", None)

    def test_stale_preview_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "preview.png"
            p.write_bytes(PNG)
            with self.assertRaisesRegex(RuntimeError, "not updated"):
                load_fresh_png(p, preview_mtime(p))

    def test_fresh_preview_read(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "preview.png"
            p.write_bytes(PNG)
            self.assertEqual(load_fresh_png(p, None), PNG)

    def test_not_png_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "preview.png"
            p.write_bytes(b"not png" * 10)
            with self.assertRaisesRegex(RuntimeError, "not a valid PNG"):
                load_fresh_png(p, None)

    def test_multimodal_content_contains_pixels(self):
        result = build_vision_content("look carefully", PNG)
        self.assertEqual(result[0]["text"], "look carefully")
        self.assertEqual(result[1]["type"], "image_url")
        payload = result[1]["image_url"].removeprefix("data:image/png;base64,")
        self.assertEqual(base64.b64decode(payload), PNG)

    def test_vision_approval(self):
        approved, feedback = validate_vision_verdict(
            {"approved": True, "visible": True, "feedback": "Good framing"})
        self.assertTrue(approved)
        self.assertEqual(feedback, "Good framing")

    def test_missing_subject_never_approved(self):
        approved, feedback = validate_vision_verdict(
            {"approved": True, "visible": False, "feedback": "Move right"})
        self.assertFalse(approved)
        self.assertIn("Subject not clearly visible", feedback)

    def test_invalid_verdict_rejected(self):
        with self.assertRaises(ValueError):
            validate_vision_verdict({"approved": "yes", "visible": True, "feedback": "good"})
        with self.assertRaises(ValueError):
            validate_vision_verdict({"approved": True, "visible": True, "feedback": ""})

    def test_identical_shot_detected(self):
        a = {"position": [0, 2, .5], "lens_mm": 35}
        b = {"position": [0.01, 2, .5], "lens_mm": 35.1}
        self.assertTrue(same_camera_proposal(a, b))
        self.assertFalse(same_camera_proposal(a, {"position": [0, 3, .5], "lens_mm": 35}))


if __name__ == "__main__":
    unittest.main()
