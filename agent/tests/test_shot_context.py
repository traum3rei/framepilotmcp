import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from shot_context import shot_context


class ShotContextTests(unittest.TestCase):
    def test_at_floor_cannot_lower_further(self):
        result = shot_context({"position": [0, 1, 0.35], "target": [0, 5, 2.1], "lens_mm": 35})
        self.assertEqual(result["remaining_safe_downward_movement"], 0)
        self.assertFalse(result["can_lower_meaningfully"])
        self.assertGreater(result["upward_pitch_deg"], 20)

    def test_higher_camera_can_lower(self):
        result = shot_context({"position": [0, -2, 1.2], "target": [0, 5, 2.1], "lens_mm": 50})
        self.assertTrue(result["can_lower_meaningfully"])
        self.assertEqual(result["remaining_safe_downward_movement"], 0.85)

    def test_invalid_coordinates_rejected(self):
        with self.assertRaises(ValueError):
            shot_context({"position": [0, 1, float("nan")], "target": [0, 5, 2.1]})

if __name__ == "__main__":
    unittest.main()
