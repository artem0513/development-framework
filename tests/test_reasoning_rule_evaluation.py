"""Offline evaluator input integrity checks, never semantic policy scoring."""
import argparse
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ase.protocol import canonical, sha256
from scripts import evaluate_reasoning_rules as evaluation


class ReasoningEvaluationCaptureTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='ase-reasoning-capture-test-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.guide = self.root / 'current.md'
        self.guide_bytes = b'\xef\xbb\xbf# Actual current guide\r\nA current body.\r\n'
        self.guide.write_bytes(self.guide_bytes)
        self.cases = self.root / 'cases.json'
        self.cases.write_text(json.dumps({'cases': [{'id': 'one', 'assignment': 'Prepare a local draft.'}],
                                        'rubric': {'one': 'WITHHELD-EVALUATOR-ONLY-CRITERION'}}), encoding='utf-8')
        self.args = argparse.Namespace(output=self.root / 'output', cases=self.cases,
                                       guide=[self.guide], repetitions=5)

    def test_capture_withholds_rubric_and_never_runs_backend(self):
        with patch.object(evaluation, 'Runner') as runner:
            request = evaluation.capture(self.args)
        runner.assert_not_called()
        self.assertNotIn(b'WITHHELD-EVALUATOR-ONLY-CRITERION', canonical(request))
        self.assertEqual(json.loads((self.args.output / 'rubric.json').read_bytes())['criteria'],
                         {'one': 'WITHHELD-EVALUATOR-ONLY-CRITERION'})
        self.assertFalse((self.args.output / 'launch.json').exists())

    def test_capture_binds_actual_guide_bytes_and_packet_text(self):
        request = evaluation.capture(self.args)
        manifest = json.loads((self.args.output / 'input-manifest.json').read_bytes())
        source = manifest['guide_sources'][0]
        actual = request['source_packet']['files'][0]
        self.assertEqual(source['source_path'], str(self.guide.resolve()))
        self.assertEqual(source['source_bytes_sha256'], sha256(self.guide_bytes))
        self.assertEqual(actual['content'], '# Actual current guide\nA current body.\n')
        self.assertEqual(actual['sha256'], sha256(actual['content'].encode('utf-8')))
        self.assertEqual(source['packet_text_sha256'], actual['sha256'])
        self.assertEqual(manifest['request_sha256'], sha256(canonical(request)))

    def test_existing_capture_is_not_overwritten(self):
        evaluation.capture(self.args)
        before = {path.name: path.read_bytes() for path in self.args.output.iterdir()}
        self.guide.write_text('Changed later guide.', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'fresh output directory'):
            evaluation.capture(self.args)
        self.assertEqual({path.name: path.read_bytes() for path in self.args.output.iterdir()}, before)


if __name__ == '__main__':
    unittest.main()
