"""Actual process/seal recovery; native judgments here are protocol fixtures."""
from pathlib import Path
import json
import os
import subprocess
import sys
import unittest
from unittest.mock import patch

import test_lead_review as support
from ase.store import Store
from ase.native_paths import logical_path


class LeadReviewCrashTests(unittest.TestCase):
    def setUp(self):
        self.case = support.LeadReviewTests()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)

    def reopen(self):
        c = self.case
        c.store.close()
        c.store = Store(c.root, c.home)
        c.service = c.module.LeadReview(c.store)

    def test_completed_seal_survives_projection_exception_and_recovers_once(self):
        c = self.case; ticket = c.prepare(); data = c.payload(ticket)
        with patch.object(c.service, '_commit_capture', side_effect=RuntimeError('crash after seal')):
            with self.assertRaisesRegex(RuntimeError, 'crash after seal'):
                c.record(ticket, data)
        self.reopen()
        c.service.recover('lead')
        self.assertEqual(c.service.gate('lead')['completed_passes'], 1)
        before = c.service.snapshot('lead')
        self.assertEqual(c.service.recover('lead'), before)

    def test_complete_pending_capture_is_recovered_without_native_replay(self):
        c = self.case; ticket = c.prepare(); data = c.payload(ticket)
        with patch('ase.lead_review.os.replace', side_effect=RuntimeError('crash before rename')):
            with self.assertRaises(RuntimeError):
                c.record(ticket, data)
        self.reopen()
        c.service.recover('lead')
        self.assertEqual(c.service.gate('lead')['completed_passes'], 1)

    def test_partial_capture_is_retained_and_same_response_can_finish_it(self):
        c = self.case; ticket = c.prepare(); data = c.payload(ticket)
        with patch('ase.lead_review.os.replace', side_effect=RuntimeError('crash before rename')):
            with self.assertRaises(RuntimeError):
                c.record(ticket, data)
        path = c.service._path('lead', ticket['handoff']['attempt_id']).with_suffix('.json.pending')
        raw = path.read_bytes(); path.write_bytes(raw[:len(raw)//2])
        c.service.recover('lead')
        self.assertEqual(c.service.gate('lead')['completed_passes'], 0)
        c.record(ticket, data)
        self.assertEqual(c.service.gate('lead')['completed_passes'], 1)

    def test_process_death_after_model_capture_before_sql_recovers_exact_attempt(self):
        c = self.case; ticket = c.prepare(); data = c.payload(ticket)
        input_file = c.base / 'capture-input.json'
        input_file.write_text(json.dumps({'ticket': ticket, 'result': data}), encoding='utf-8')
        code = """import json,os,sys
from ase.store import Store
from ase.lead_review import LeadReview
x=json.load(open(sys.argv[3],encoding='utf-8'))
s=Store(sys.argv[1],sys.argv[2]);r=LeadReview(s)
r._commit_capture=lambda *args:os._exit(86)
r.record('lead',x['ticket']['handoff']['attempt_id'],x['result'],thread_id='original-main',expected_revision=x['ticket']['revision'])
"""
        result = subprocess.run([sys.executable, '-B', '-c', code, str(c.root), str(c.home), str(input_file)],
            cwd=Path(__file__).resolve().parents[1], capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 86, result.stderr.decode('utf-8', 'replace'))
        self.reopen()
        c.service.recover('lead')
        self.assertEqual(c.service.gate('lead')['completed_passes'], 1)
        self.assertIsNone(c.service.snapshot('lead')['payload']['inflight'])
        self.assertEqual(c.service.snapshot('lead')['payload']['passes'][0]['path'],
                         str(logical_path(c.service._path('lead', ticket['handoff']['attempt_id']))))


if __name__ == '__main__':
    unittest.main()
