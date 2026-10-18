"""Pure offline tests for the pairwise model contract and safety-first selection."""
import base64
import sys
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from comparison_review import (
    VisualVerdict, image_pair_content, parse_visual_verdict,
    prefer_new_best, should_continue,
)

PNG_A = b'\x89PNG\r\n\x1a\n' + b'a' * 100
PNG_B = b'\x89PNG\r\n\x1a\n' + b'b' * 100


def verdict(*, approved=False, visible=True, winner=None, score=5, issue_source='camera'):
    return VisualVerdict(visible=visible, approved=approved, feedback='some visual feedback',
                         score=score, issue_source=issue_source, winner=winner)


class VisionComparisonTests(unittest.TestCase):
    def test_two_real_images_in_correct_order(self):
        content = image_pair_content('Compare now', PNG_A, PNG_B)
        self.assertEqual([c['type'] for c in content],
                         ['text', 'text', 'image_url', 'text', 'image_url'])
        self.assertIn('IMAGE A', content[1]['text'])
        self.assertIn('IMAGE B', content[3]['text'])
        self.assertEqual(base64.b64decode(content[2]['image_url'].split(',')[1]), PNG_A)
        self.assertEqual(base64.b64decode(content[4]['image_url'].split(',')[1]), PNG_B)

    def test_first_verdict_does_not_require_winner(self):
        first = parse_visual_verdict({'visible': True, 'approved': False,
            'score': 6, 'feedback': 'too far', 'issue_source': 'camera'}, has_best=False)
        self.assertEqual(first.score, 6)
        self.assertIsNone(first.winner)

    def test_pair_requires_winner(self):
        with self.assertRaisesRegex(ValueError, 'winner'):
            parse_visual_verdict({'visible': True, 'approved': True, 'feedback': 'okay'}, has_best=True)

    def test_pair_accepts_preference(self):
        compared = parse_visual_verdict({'visible': True, 'approved': True,
            'score': 8, 'winner': 'best', 'feedback': 'B is smaller than A',
            'issue_source': 'scene'}, has_best=True)
        self.assertEqual(compared.winner, 'best')
        self.assertEqual(compared.issue_source, 'scene')

    def test_approval_requires_visibility(self):
        compared = parse_visual_verdict({'visible': False, 'approved': True,
            'score': 4, 'winner': 'candidate', 'feedback': 'cannot see subject'}, has_best=True)
        self.assertFalse(compared.approved)

    def test_bad_scores_refused(self):
        for bad in [True, '9', -1, 11, float('nan')]:
            with self.subTest(score=bad), self.assertRaises(ValueError):
                parse_visual_verdict({'visible': True, 'approved': True,
                    'score': bad, 'feedback': 'okay'}, has_best=False)

    def test_better_frame_replaces_old(self):
        self.assertTrue(prefer_new_best(verdict(approved=True),
                                        verdict(approved=True, winner='candidate')))

    def test_worse_frame_keeps_best_even_with_higher_score(self):
        self.assertFalse(prefer_new_best(verdict(approved=True, score=4),
                                         verdict(approved=True, score=9, winner='best')))

    def test_visible_approved_beats_unapproved_even_if_model_prefers_old(self):
        self.assertTrue(prefer_new_best(verdict(approved=False),
                                        verdict(approved=True, winner='best')))

    def test_invisible_cannot_displace_visible(self):
        self.assertFalse(prefer_new_best(verdict(approved=False, visible=True),
                                         verdict(approved=False, visible=False, winner='candidate')))

    def test_tie_preserves_current_best(self):
        self.assertFalse(prefer_new_best(verdict(), verdict(winner='tie')))

    def test_scene_only_issue_stops_camera_loop(self):
        self.assertFalse(should_continue(attempt=1, maximum=3, approved=False,
                                         best_of=True, issue_source='scene'))

    def test_best_of_continues_after_approval_if_budget_remains(self):
        self.assertTrue(should_continue(attempt=1, maximum=3, approved=True,
                                        best_of=True, issue_source='camera'))

    def test_normal_mode_stops_on_approval(self):
        self.assertFalse(should_continue(attempt=1, maximum=3, approved=True,
                                         best_of=False, issue_source='camera'))

    def test_attempt_cap(self):
        self.assertFalse(should_continue(attempt=3, maximum=3, approved=False,
                                         best_of=True, issue_source='both'))


if __name__ == '__main__':
    unittest.main()
