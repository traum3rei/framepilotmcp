import sys
import unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from v1_core import (finite_vector, parse_json, parse_verdict, validate_director,
                     validate_proposal, choose_candidate, camera_snapshot, light_snapshot)

class CoreTests(unittest.TestCase):
    def test_json_fenced(self):
        self.assertEqual(parse_json('```json\n{"specialist":"camera"}\n```')['specialist'],'camera')
    def test_json_thinking(self):
        self.assertEqual(parse_json('<think>yes</think>{"a":1}')['a'],1)
    def test_empty(self):
        with self.assertRaises(ValueError): parse_json('')
    def test_camera_any_target(self):
        p=validate_proposal('camera',{'position':[0,0,2],'target':[0,0,0],'lens_mm':50},{})
        self.assertEqual(p['target'],[0,0,0])
    def test_camera_equal_position(self):
        with self.assertRaises(ValueError):validate_proposal('camera',{'position':[0,0,0],'target':[0,0,0],'lens_mm':50},{})
    def test_light_any_name(self):
        p=validate_proposal('lighting',{'name':'Area.Custom','energy':333},{'Area.Custom':{'light_type':'AREA'}})
        self.assertEqual(p['energy'],333)
    def test_light_name_not_guessed(self):
        with self.assertRaises(ValueError):validate_proposal('lighting',{'name':'Corridor','energy':333},{'Other':{'light_type':'AREA'}})
    def test_light_no_new(self):
        with self.assertRaises(ValueError):validate_proposal('lighting',{'name':'A','energy':333},{})
    def test_area_size(self):
        with self.assertRaises(ValueError):validate_proposal('lighting',{'name':'Sun','size':5},{'Sun':{'light_type':'SUN'}})
    def test_invalid_rgb(self):
        with self.assertRaises(ValueError):validate_proposal('lighting',{'name':'Sun','color':[1,1,2]},{'Sun':{'light_type':'SUN'}})
    def test_winner_conservative(self):
        self.assertFalse(choose_candidate(parse_verdict({'winner':'maybe','approved':True,'feedback':'okay'})))
    def test_enum_unknown_nonfatal(self):
        self.assertEqual(parse_verdict({'winner':'candidate','issue_source':'geometry','next_specialist':'more'})['next_specialist'],'none')
    def test_director(self):
        self.assertEqual(validate_director({'specialist':'lighting','intent':'Change this light'})['specialist'],'lighting')
    def test_camera_snapshot(self):
        self.assertEqual(camera_snapshot({'name':'Cam','position':[0,0,0],'rotation_degrees':[0,0,0],'lens_mm':50})['lens_mm'],50)
    def test_lights_snapshot(self):
        l=light_snapshot({'name':'Key','energy':1,'color':[1,1,1],'location':[1,2,3],'rotation_degrees':[0,0,0],'light_type':'POINT','size':None})
        self.assertNotIn('size',l)

if __name__=='__main__': unittest.main()
