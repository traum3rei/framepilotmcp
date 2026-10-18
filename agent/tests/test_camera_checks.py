import sys
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from camera_checks import pitch_degrees, validate_camera, wants_low_angle


class CameraChecksTest(unittest.TestCase):
    def test_wrong_high_angle_is_rejected(self):
        pos, target = [0, -3, 3], [0, 5.6, 2.1]
        self.assertLess(pitch_degrees(pos, target), 0)
        self.assertTrue(any("FAILED" in e for e in validate_camera(pos, target, 35, 20)))

    def test_real_low_angle_is_approved(self):
        self.assertGreater(pitch_degrees([0, 2.6, 0.55], [0, 5.6, 2.1]), 20)
        self.assertEqual(validate_camera([0, 2.6, 0.55], [0, 5.6, 2.1], 35, 20), [])

    def test_floor_and_lens(self):
        issues = validate_camera([0, 2, -1], [0, 5, 2], 200)
        self.assertEqual(len(issues), 2)

    def test_semantics(self):
        self.assertTrue(wants_low_angle("dramatic low-angle shot"))
        self.assertTrue(wants_low_angle("Kamera von unten"))
        self.assertFalse(wants_low_angle("move the camera closer"))


if __name__ == '__main__':
    unittest.main()
