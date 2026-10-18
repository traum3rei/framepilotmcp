import math
import sys
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from camera_checks import repair_low_angle_position, pitch_degrees, validate_camera

class RepairTests(unittest.TestCase):
    def test_repeated_bad_position_is_repairable(self):
        pos = [0, -1.5, 0.16]
        target = [0, 5.6, 2.1]
        corrected = repair_low_angle_position(pos, target, 20)
        self.assertIsNotNone(corrected)
        self.assertGreaterEqual(pitch_degrees(corrected, target), 22.99)
        self.assertEqual(validate_camera(corrected, target, 35, 20), [])
        self.assertAlmostEqual(corrected[2], 0.16)
        self.assertGreater(corrected[1], pos[1])

    def test_valid_low_angle_unchanged(self):
        pos = [0, 2.6, 0.55]
        self.assertEqual(repair_low_angle_position(pos, [0, 5.6, 2.1], 20), pos)

    def test_too_high_not_silently_repaired(self):
        self.assertIsNone(repair_low_angle_position([0, -3, 3], [0, 5.6, 2.1], 20))

    def test_azimuth_preserved(self):
        pos = [3, -3, 0.2]
        target = [0, 5.6, 2.1]
        new = repair_low_angle_position(pos, target, 20)
        self.assertGreaterEqual(pitch_degrees(new,target), 20)
        self.assertAlmostEqual(new[0]/(new[1]-target[1]),pos[0]/(pos[1]-target[1]))

    def test_nan_is_rejected(self):
        self.assertIsNone(repair_low_angle_position([math.nan,0,0.2],[0,5,2],20))

if __name__ == '__main__':
    unittest.main()
