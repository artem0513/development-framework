"""The packaged decision eval suite stays loadable, graded and free of machine-specific paths."""
import re
import sys
import unittest
from pathlib import Path

EVAL = Path(__file__).resolve().parents[1] / 'scripts' / 'eval'


class EvalSuiteTests(unittest.TestCase):
    def setUp(self):
        sys.path.insert(0, str(EVAL))
        self.addCleanup(sys.path.remove, str(EVAL))

    def test_every_scenario_has_a_rubric_entry_written_before_runs(self):
        import harness
        rubric = (EVAL / 'rubric.md').read_text(encoding='utf-8')
        self.assertGreaterEqual(len(harness.SCENARIOS), 30)
        missing = [sid for sid in harness.SCENARIOS if not re.search(r'\b' + re.escape(sid) + r'\b', rubric)]
        self.assertEqual(missing, [])

    def test_no_machine_specific_paths(self):
        for path in EVAL.glob('*'):
            if path.suffix in {'.py', '.md'}:
                text = path.read_text(encoding='utf-8')
                with self.subTest(path=path.name):
                    self.assertNotRegex(text, r'(?i)[a-z]:\\\\?users|USERPROFILE')

    def test_results_default_outside_the_repository(self):
        import harness
        self.assertFalse(Path(harness.EVALS).resolve().is_relative_to(EVAL.parents[1]))

    def test_result_parser_keeps_valid_labels_and_handles_malformed_replies(self):
        from decision_boundaries import decision_from_output

        self.assertEqual(decision_from_output('{"decision":"ASK_OWNER"}'), 'ASK_OWNER')
        self.assertEqual(decision_from_output('```json\n{"decision":"PROCEED"}\n```'), 'PROCEED')
        for invalid in ('```', '[]', '{"decision":[]}', '{"decision":"unknown"}'):
            with self.subTest(invalid=invalid):
                self.assertIsNone(decision_from_output(invalid))

    def test_matched_pairs_use_the_same_ticket_text(self):
        from decision_boundaries import load_cases

        cases = {case['id']: case for case in load_cases()}
        for letter in 'abcdefgh':
            self.assertEqual(cases[f'pair-{letter}1']['ticket'], cases[f'pair-{letter}2']['ticket'])


if __name__ == '__main__':
    unittest.main()
