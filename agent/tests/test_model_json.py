import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from model_json import parse_model_json


class ParserTest(unittest.TestCase):
    def test_plain_json(self):
        self.assertEqual(parse_model_json(SimpleNamespace(content='{"target_name":"Eye"}')),
                         {"target_name": "Eye"})

    def test_fenced(self):
        msg = SimpleNamespace(content='```json\n{"position":[0,2,1],"lens_mm":35}\n```')
        self.assertEqual(parse_model_json(msg)["lens_mm"], 35)

    def test_preamble(self):
        self.assertEqual(parse_model_json(SimpleNamespace(content='Here is the result: {"approved":true}')),
                         {"approved": True})

    def test_thinking_block(self):
        msg = SimpleNamespace(content='<think>consider alternatives</think>\n{"approved":true}')
        self.assertTrue(parse_model_json(msg)["approved"])

    def test_empty_reports_diagnostics(self):
        msg = SimpleNamespace(content='', response_metadata={"done_reason": "length", "eval_count": 350})
        with self.assertRaisesRegex(ValueError, 'empty JSON response.*done_reason=length'):
            parse_model_json(msg)

    def test_malformed_gives_useful_error(self):
        with self.assertRaisesRegex(ValueError, 'not valid JSON'):
            parse_model_json(SimpleNamespace(content='not a json response'))

    def test_object_required(self):
        with self.assertRaisesRegex(ValueError, 'Expected JSON object'):
            parse_model_json(SimpleNamespace(content='[1,2]'))


if __name__ == '__main__':
    unittest.main()
