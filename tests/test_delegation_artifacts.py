"""Exact native-result storage, without semantic or provider-authentication claims."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from ase.store import Store


class DelegationArtifactTests(unittest.TestCase):
    def setUp(self):
        from ase import delegation_artifacts
        self.api = delegation_artifacts
        temporary = tempfile.TemporaryDirectory(prefix='ase-delegate-result-')
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        repo = self.base / 'repo'
        repo.mkdir()
        self.store = Store(repo, self.base / 'state')
        self.addCleanup(self.store.close)
        self.entry = {'id': 'inspect', 'task_id': 'root', 'attempt': {'id': 'a' * 32,
            'context_sha256': 'b' * 64, 'backend': 'codex', 'native': {'backend': 'codex', 'agent_id': '/root/inspect'}}}
        self.directory = self.api.attempt_root(self.store, self.entry)
        (self.directory / 'output').mkdir(parents=True)
        (self.directory / 'output/facts.txt').write_bytes(b'Observed original facts\n')
        self.payload = {'attempt_id': 'a' * 32, 'agent_id': '/root/inspect', 'status': 'COMPLETE',
            'summary': 'Inspected the bounded original input', 'artifacts': [{'path': 'facts.txt',
                'sha256': hashlib.sha256(b'Observed original facts\n').hexdigest()}]}

    def test_sealed_result_reloads_and_repeat_keeps_exact_file(self):
        ref = self.api.seal_result(self.store, self.entry, self.payload)
        before = Path(ref['path']).stat().st_mtime_ns
        self.entry['result'] = ref
        result = self.api.load_result(self.store, self.entry)
        self.assertEqual(result['attempt_id'], self.payload['attempt_id'])
        self.assertEqual(result['artifacts'][0]['bytes'], len(b'Observed original facts\n'))
        self.assertEqual(self.api.seal_result(self.store, self.entry, self.payload), ref)
        self.assertEqual(Path(ref['path']).stat().st_mtime_ns, before)

    def test_orphan_complete_result_loads_from_exact_attempt_without_db_reference(self):
        ref = self.api.seal_result(self.store, self.entry, self.payload)
        self.assertNotIn('result', self.entry)
        self.assertEqual(self.api.load_result(self.store, self.entry)['summary'], self.payload['summary'])
        self.assertTrue(Path(ref['path']).is_file())

    def test_tampered_artifact_or_seal_is_rejected(self):
        ref = self.api.seal_result(self.store, self.entry, self.payload)
        self.entry['result'] = ref
        original = (self.directory / 'output/facts.txt').read_bytes()
        (self.directory / 'output/facts.txt').write_bytes(b'Changed after result')
        with self.assertRaises(ValueError):
            self.api.load_result(self.store, self.entry)
        (self.directory / 'output/facts.txt').write_bytes(original)
        Path(ref['path']).write_bytes(b'{}')
        with self.assertRaises(ValueError):
            self.api.load_result(self.store, self.entry)

    def test_foreign_attempt_native_and_artifact_path_cannot_be_sealed(self):
        for value in (dict(self.payload, attempt_id='c' * 32), dict(self.payload, agent_id='/root/other'),
                dict(self.payload, artifacts=[{'path': '../escape', 'sha256': 'd' * 64}]),
                dict(self.payload, artifacts=[{'path': 'facts.txt', 'sha256': 'd' * 64}])):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.api.seal_result(self.store, self.entry, value)
        self.assertFalse((self.directory / 'result.json').exists())

    def test_partial_matching_staging_can_finish_but_unknown_staging_is_preserved(self):
        ref = self.api.seal_result(self.store, self.entry, self.payload)
        raw = Path(ref['path']).read_bytes()
        Path(ref['path']).unlink()
        pending = self.directory / 'result.json.pending'
        pending.write_bytes(raw[:13])
        self.assertEqual(self.api.seal_result(self.store, self.entry, self.payload), ref)
        self.assertFalse(pending.exists())
        Path(ref['path']).unlink()
        pending.write_bytes(b'Unknown owner data')
        with self.assertRaises(ValueError):
            self.api.seal_result(self.store, self.entry, self.payload)
        self.assertEqual(pending.read_bytes(), b'Unknown owner data')

    def test_foreign_reference_and_malformed_manifest_are_controlled_failures(self):
        ref = self.api.seal_result(self.store, self.entry, self.payload)
        self.entry['result'] = dict(ref, path=str(self.base / 'elsewhere.json'))
        with self.assertRaises(ValueError):
            self.api.load_result(self.store, self.entry)
        self.entry['result'] = ref
        for value in ([], 'result', None, {'version': True}):
            with self.subTest(value=value):
                Path(ref['path']).write_text(json.dumps(value), encoding='utf-8')
                with self.assertRaises(ValueError):
                    self.api.load_result(self.store, self.entry)
