"""Local tests: no Ollama/Blender/LangGraph needed."""
import math
import sys
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from camera_checks import normalize_camera_geometry, pitch_degrees, validate_camera

class FirstShotRepairTests(unittest.TestCase):
    target = [0, 5.6, 2.1]

    def test_low_camera_below_floor_and_undershoots_angle(self):
        pos = [0, -1.5, 0.10]
        new = normalize_camera_geometry(pos, self.target, 20)
        self.assertIsNotNone(new)
        self.assertGreaterEqual(new[2], 0.35)
        self.assertGreaterEqual(pitch_degrees(new, self.target), 22.99)
        self.assertEqual(validate_camera(new, self.target, 35, 20), [])

    def test_19_point_8_degree_shot_fixed_in_first_attempt(self):
        z = 0.14
        distance = (2.1-z)/math.tan(math.radians(19.8))
        old = [0, 5.6-distance, z]
        self.assertAlmostEqual(pitch_degrees(old, self.target), 19.8, places=4)
        new = normalize_camera_geometry(old, self.target, 20)
        self.assertGreaterEqual(new[2], 0.35)
        self.assertGreaterEqual(pitch_degrees(new, self.target), 22.99)

    def test_good_position_unchanged(self):
        pos = [0, 2.6, .55]
        self.assertEqual(normalize_camera_geometry(pos, self.target, 20), pos)

    def test_floor_fix_without_low_angle(self):
        new = normalize_camera_geometry([0, -1, .05], self.target, None)
        self.assertEqual(new, [0, -1, .35])

    def test_above_target_not_silently_repositioned(self):
        self.assertIsNone(normalize_camera_geometry([0, -3, 3], self.target, 20))

    def test_impossible_target(self):
        self.assertIsNone(normalize_camera_geometry([0, -3, 0.1], [0, 2, .2], 20))

    def test_nan_rejected(self):
        self.assertIsNone(normalize_camera_geometry([math.nan, 0, 0.1], self.target, 20))

if __name__ == "__main__":
    unittest.main()
