import sys
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lighting_checks import managed_lights, validate_light_plan, choose_best, changed_energies


class LightingTests(unittest.TestCase):
    def setUp(self):
        self.allowed = {"FramePilot / Blue corridor fill": 1350., "FramePilot / Warm corridor fill": 1150.,
                        "FramePilot / Back pillar rim": 1250.}
        self.scene = {"objects": [
            {"name": k, "type": "LIGHT", "energy": v} for k, v in self.allowed.items()
        ] + [{"name": "Warm eye light", "type": "LIGHT", "energy": 500.},
             {"name": "FramePilot / prop", "type": "MESH", "energy": 200.}]}

    def test_only_managed(self):
        self.assertEqual(managed_lights(self.scene), self.allowed)

    def test_missing_lights(self):
        with self.assertRaisesRegex(ValueError, "exactly"):
            validate_light_plan({"energies": {"FramePilot / Blue corridor fill": 1000}}, self.allowed, [])

    def test_other_light_forbidden(self):
        with self.assertRaises(ValueError):
            validate_light_plan({"energies": {**self.allowed, "Warm eye light": 800}}, self.allowed, [])

    def test_non_finite(self):
        with self.assertRaises(ValueError):
            validate_light_plan({"energies": {**self.allowed, "FramePilot / Blue corridor fill": float('nan')}}, self.allowed, [])

    def test_bounds(self):
        for v in (-5, 5000):
            with self.assertRaises(ValueError):
                validate_light_plan({"energies": {**self.allowed, "FramePilot / Blue corridor fill": v}}, self.allowed, [])

    def test_duplicate(self):
        with self.assertRaisesRegex(ValueError, "repeats"):
            validate_light_plan({"energies": self.allowed}, self.allowed, [self.allowed])

    def test_valid(self):
        result = validate_light_plan({"energies": {**self.allowed, "FramePilot / Blue corridor fill": 2000}}, self.allowed, [self.allowed])
        self.assertEqual(result["FramePilot / Blue corridor fill"], 2000.)

    def test_better(self):
        self.assertTrue(choose_best("candidate", candidate_approved=False, best_approved=False))
        self.assertFalse(choose_best("best", candidate_approved=False, best_approved=False))
        self.assertFalse(choose_best("candidate", candidate_approved=False, best_approved=True))
        self.assertTrue(choose_best("best", candidate_approved=True, best_approved=False))

    def test_changed_only(self):
        self.assertEqual(changed_energies(self.allowed, {**self.allowed, "FramePilot / Blue corridor fill": 2000}),
                         {"FramePilot / Blue corridor fill": 2000})

if __name__ == '__main__':
    unittest.main()
