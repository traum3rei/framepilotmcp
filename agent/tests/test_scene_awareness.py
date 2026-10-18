import sys
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scene_awareness import scene_visual_context


class SceneAwarenessTests(unittest.TestCase):
    def test_architecture_and_lights_are_grounded(self):
        scene = {'objects': [
            {'name': 'Pillar -1 / 3', 'type': 'MESH', 'location': [-2.8, 3, 1.9]},
            {'name': 'Ceiling', 'type': 'MESH', 'location': [0, 0, 4.2]},
            {'name': 'Warm eye light', 'type': 'LIGHT', 'location': [0, 4.2, 3.5], 'energy': 800},
            {'name': 'The Watcher / iris', 'type': 'MESH', 'location': [0, 5.6, 2.1]},
        ]}
        result = scene_visual_context(scene)
        self.assertEqual(result['architecture_count_from_inspection'], 2)
        self.assertEqual([v['name'] for v in result['geometry_known_to_exist']],
                         ['Pillar -1 / 3', 'Ceiling'])
        self.assertEqual(result['lights_known_to_exist'][0]['energy'], 800)
        self.assertIn('DOES NOT', result['visibility_note'])

    def test_empty_scene_does_not_invent_geometry(self):
        result = scene_visual_context({'objects': []})
        self.assertEqual(result['architecture_count_from_inspection'], 0)
        self.assertFalse(result['lights_known_to_exist'])

    def test_limits_and_truncation_are_honored(self):
        scene = {'objects': [{'name': f'Pillar {i}', 'type': 'MESH'} for i in range(50)],
                 'truncated': True}
        result = scene_visual_context(scene, limit=8)
        self.assertEqual(result['architecture_count_from_inspection'], 50)
        self.assertEqual(len(result['geometry_known_to_exist']), 8)
        self.assertTrue(result['inspection_truncated'])


if __name__ == '__main__':
    unittest.main()
