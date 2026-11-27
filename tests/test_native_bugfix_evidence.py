"""Validator tests only: synthetic captures never establish native acceptance."""
from copy import deepcopy
from contextlib import closing
from datetime import datetime, timezone
import importlib.util
import io
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from scripts import evaluate_reasoning_rules as evaluator
from ase.protocol import canonical, sha256
from ase.runtime_policy import bind_policy
from ase.testing.adequacy import _case_execution

CHECKER = Path(__file__).resolve().parents[1] / 'scripts' / 'check_native_bugfix_evidence.py'
spec = importlib.util.spec_from_file_location('native_bugfix_checker', CHECKER)
checker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checker)


class NativeEvidenceValidatorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'source'
        for name in evaluator.DEFAULT_SKILLS:
            path = self.root / 'skills' / name / 'SKILL.md'
            path.parent.mkdir(parents=True)
            path.write_text('Current guide: ' + name, encoding='utf-8')
        fixture = {'cases': [{'id': name, 'task': 'Stipulated unit fixture'} for name in checker.CASE_IDS],
                   'rubric': {name: ['Requires independent reading'] for name in checker.CASE_IDS}}
        source = self.root / 'tests' / 'fixtures' / 'bugfix-evidence-cases.json'
        source.parent.mkdir(parents=True)
        source.write_bytes(canonical(fixture))
        self.folder = Path(self.temp.name) / 'cases'
        args = SimpleNamespace(output=self.folder, cases=source, guide=None, repetitions=1)
        with patch.object(evaluator, 'ROOT', self.root):
            template = evaluator.capture(args)
        self.execution = '1' * 32
        self.session = 'synthetic-provider-session'
        run = self.folder / 'rep-01' / 'sessions' / self.execution
        run.mkdir(parents=True)
        self.request = bind_policy(template)
        self.write(run / 'request.json', self.request)
        self.receipt = {'receipt_version': 2, 'execution_id': self.execution,
            'session_id': self.session, 'artifact_root': str(run), 'status': 'COMPLETE',
            'evidence_kind': 'independent_cli', 'backend': 'codex', 'role': 'diagnostic',
            'bindings': template['bindings'], 'executable': str(Path(__file__).resolve()),
            'cwd': str(Path(self.temp.name) / 'trusted'),
            'artifacts': {'request': {'path': str(run / 'request.json')}}}
        self.result = {'status': 'COMPLETE', 'blocker': None, 'session_id': self.session,
            'payload': {'items': [{'id': name, 'decision': 'Arbitrary raw decision',
                'next_operation': '', 'artifact': '', 'owner_message': '', 'evidence': [], 'limits': []}
                for name in checker.CASE_IDS]}}
        self.write(run / 'receipt.json', self.receipt)
        self.capture = {'index': 1, 'status': 'COMPLETE', 'blocker': None,
            'receipt_integrity_verified': True, 'execution_id': self.execution,
            'session_id': self.session, 'artifact_root': str(run),
            'receipt_sha256': sha256((run / 'receipt.json').read_bytes()), 'result': self.result}
        self.write(self.folder / 'rep-01' / 'capture.json', self.capture)
        self.write(self.folder / 'sessions-summary.json', {'sessions': [
            {key: value for key, value in self.capture.items() if key != 'result'}]})
        self.write(self.folder / 'launch.json', {'repetitions': 1, 'model_override': None,
            'configuration_overrides': None, 'executable': self.receipt['executable'],
            'trusted_cwd': self.receipt['cwd'],
            'input_manifest_sha256': sha256((self.folder / 'input-manifest.json').read_bytes())})
        self.calls = []

    def write(self, path, value):
        path.write_bytes(canonical(value))

    def fake_verify(self, receipt, **kwargs):
        self.calls.append((deepcopy(receipt), kwargs))
        return {'valid': True, 'evidence_kind': receipt['evidence_kind'],
            'execution_id': receipt['execution_id'], 'policy_current': True,
            'result': deepcopy(self.result)}

    def validate(self):
        return checker.validate_cases(self.folder, source_root=self.root, verify=self.fake_verify)

    def test_writer_compatible_capture_exposes_raw_outputs_without_semantic_verdict(self):
        evidence = self.validate()
        self.assertEqual(evidence['items'], self.result['payload']['items'])
        self.assertNotIn('semantic_verdict', evidence)
        self.assertEqual(self.calls[0][1], {'expected_bindings': self.request['bindings'],
                                          'require_current_policy': True})
        self.assertEqual(evidence['capture']['session_id'], self.session)

    def test_receipt_verifier_failure_propagates(self):
        def rejected(*args, **kwargs):
            raise ValueError('Tampered captured stdout')
        with self.assertRaisesRegex(ValueError, 'Tampered'):
            checker.validate_cases(self.folder, source_root=self.root, verify=rejected)

    def test_protocol_receipt_is_rejected(self):
        self.receipt['evidence_kind'] = 'protocol_only'
        run = Path(self.receipt['artifact_root'])
        self.write(run / 'receipt.json', self.receipt)
        self.capture['receipt_sha256'] = sha256((run / 'receipt.json').read_bytes())
        self.write(self.folder / 'rep-01' / 'capture.json', self.capture)
        with self.assertRaisesRegex(ValueError, 'independent_cli'):
            self.validate()

    def test_current_guides_include_new_files_and_reject_stale_bytes(self):
        guide = self.root / 'skills' / evaluator.DEFAULT_SKILLS[0] / 'SKILL.md'
        guide.write_text('changed policy', encoding='utf-8')
        with self.assertRaises(ValueError):
            self.validate()

    def test_omitted_new_guide_is_rejected(self):
        (self.root / 'skills' / evaluator.DEFAULT_SKILLS[0] / 'new.md').write_text('new', encoding='utf-8')
        with self.assertRaises(ValueError):
            self.validate()

    def test_changed_original_fixture_is_rejected(self):
        fixture = self.root / 'tests' / 'fixtures' / 'bugfix-evidence-cases.json'
        value = json.loads(fixture.read_bytes())
        value['rubric'][checker.CASE_IDS[0]] = ['Changed rubric']
        self.write(fixture, value)
        with self.assertRaises(ValueError):
            self.validate()

    def test_changed_capture_payload_is_not_trusted(self):
        self.capture['result']['payload']['items'][0]['decision'] = 'Edited after capture'
        self.write(self.folder / 'rep-01' / 'capture.json', self.capture)
        # Restore the verifier's independently reconstructed result.
        self.result['payload']['items'][0]['decision'] = 'Arbitrary raw decision'
        with self.assertRaisesRegex(ValueError, 'result'):
            self.validate()

    def test_duplicate_or_reordered_case_ids_rejected(self):
        for ids in ((checker.CASE_IDS[0],) * len(checker.CASE_IDS), tuple(reversed(checker.CASE_IDS))):
            with self.subTest(ids=ids):
                self.result['payload']['items'] = [{**row, 'id': name} for row, name in
                    zip(self.result['payload']['items'], ids)]
                self.capture['result'] = deepcopy(self.result)
                self.write(self.folder / 'rep-01' / 'capture.json', self.capture)
                with self.assertRaisesRegex(ValueError, 'case IDs'):
                    self.validate()

    def test_failed_missing_stale_policy_and_duplicate_captures_rejected(self):
        capture_path = self.folder / 'rep-01' / 'capture.json'
        self.capture['status'] = 'BLOCKED'
        self.write(capture_path, self.capture)
        with self.assertRaises(ValueError):
            self.validate()
        self.capture['status'] = 'COMPLETE'
        self.write(capture_path, self.capture)
        with patch.object(self, 'fake_verify', return_value={'valid': True,
                'evidence_kind': 'independent_cli', 'execution_id': self.execution,
                'policy_current': False, 'result': deepcopy(self.result)}):
            with self.assertRaisesRegex(ValueError, 'policy'):
                self.validate()
        extra = self.folder / 'rep-02'
        extra.mkdir()
        self.write(extra / 'capture.json', self.capture)
        with self.assertRaises(ValueError):
            self.validate()
        (extra / 'capture.json').unlink()
        extra.rmdir()
        Path(self.receipt['artifact_root'], 'receipt.json').unlink()
        with self.assertRaises((ValueError, OSError)):
            self.validate()

    def test_distinct_provider_sessions_and_executions_required(self):
        checker.validate_identities([('a', 'session-a'), ('b', 'session-b')])
        for rows in ([('a', 'same'), ('b', 'same')], [('a', 'one'), ('a', 'two')], [('a', '')]):
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                checker.validate_identities(rows)

    def test_saved_approved_result_must_match_current_gate(self):
        gate = {key: key for key in checker.GATE_BINDINGS}
        gate['status'] = 'APPROVED'
        checker.validate_saved_gate(deepcopy(gate), gate)
        for field in ('status', *checker.GATE_BINDINGS):
            bad = {**gate, field: 'changed'}
            with self.subTest(field=field), self.assertRaises(ValueError):
                checker.validate_saved_gate(bad, gate)
        with self.assertRaises(ValueError):
            checker.validate_saved_gate(gate, {**gate, 'status': 'BLOCKED'})
        with self.assertRaises(ValueError):
            checker.validate_saved_gate({**gate, 'profile_evidence': {'status': 'PASS', 'counts': 99}},
                                        {**gate, 'profile_evidence': {'status': 'PASS', 'counts': 1}})

    def test_judgment_summary_must_equal_verified_receipt_not_aggregate_pass(self):
        receipt = {'artifact_root': str(self.folder), 'session_id': 's'}
        verified = {'evidence_kind': 'independent_cli', 'result': {'session_id': 's', 'payload': {'raw': 'judgment'}}}
        row = {'kind': 'independent_cli', 'receipt': str(self.folder), 'session': 's', 'payload': {'raw': 'judgment'}}
        checker.validate_judgment(row, receipt, verified)
        row['payload'] = {'raw': 'PASS'}
        with self.assertRaises(ValueError):
            checker.validate_judgment(row, receipt, verified)

    def test_native_suite_names_each_advisory_case_and_labels_integrity_only(self):
        suite = checker.build_suite(self.folder, self.folder)
        ids = [test.id() for test in suite]
        for case_id in checker.CASE_IDS:
            self.assertTrue(any(case_id.replace('-', '_') in name for name in ids), case_id)
        self.assertEqual(len(ids), len(checker.CASE_IDS) + 2)

    def test_full_consumer_assertion_detects_weakened_discount_and_missing_transport(self):
        checker.validate_consumer_assertions(checker.CONSUMER_TEST)
        for source in (checker.CONSUMER_TEST.replace("'Discount':17", "'Discount':0"),
                       checker.CONSUMER_TEST.replace('  self.assertEqual(observed,[original])\n', ''),
                       checker.CONSUMER_TEST.replace('result=consumer(original,transport)', 'result=transport(original)')):
            with self.subTest(source=source), self.assertRaises(ValueError):
                checker.validate_consumer_assertions(source)

    def test_native_suite_prints_full_raw_case_payload_once_and_only_checks_integrity(self):
        advisory = {'items': deepcopy(self.result['payload']['items']), 'identities': [('case-run', 'case-session')]}
        consumer = {'identities': [('consumer-run', 'consumer-session')]}
        output, runner = io.StringIO(), io.StringIO()
        with patch.object(checker, 'validate_cases', return_value=advisory) as cases, \
             patch.object(checker, 'validate_consumer', return_value=consumer) as native, \
             patch('sys.stdout', output):
            result = unittest.TextTestRunner(stream=runner).run(checker.build_suite(self.folder, self.folder))
        self.assertTrue(result.wasSuccessful(), runner.getvalue())
        self.assertEqual(result.testsRun, len(checker.CASE_IDS) + 2)
        cases.assert_called_once()
        native.assert_called_once()
        emitted = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(len(emitted), 2)
        raw = next(row['advisory'] for row in emitted if 'advisory' in row)
        self.assertEqual(raw['items'], self.result['payload']['items'])
        self.assertTrue(all(row['evidence_integrity_only'] for row in emitted))

    def test_native_verbose_cases_bind_to_static_source_and_exact_script_command(self):
        """Unit seam: run the real suite with mocked evidence, never native acceptance."""
        advisory = {'items': deepcopy(self.result['payload']['items']), 'identities': [('case-run', 'case-session')]}
        consumer = {'identities': [('consumer-run', 'consumer-session')]}
        output = runner = io.StringIO()
        suite = checker.build_suite(self.folder, self.folder)
        case_class = type(next(iter(suite)))
        started = datetime.now(timezone.utc).isoformat()
        # Direct script execution uses __main__; leave the class qualname intact
        # so nested/dynamic implementations cannot pass this source/log binding.
        with patch.object(case_class, '__module__', '__main__'), \
             patch.object(checker, 'validate_cases', return_value=advisory), \
             patch.object(checker, 'validate_consumer', return_value=consumer), \
             patch('sys.stdout', output):
            status = checker.main(['--consumer', str(self.folder), '--cases', str(self.folder)])
        ended = datetime.now(timezone.utc).isoformat()
        self.assertEqual(status, 0)
        log = Path(self.temp.name) / 'unit-suite-verbose.log'
        log.write_bytes(runner.getvalue().encode('utf-8'))
        argv = [sys.executable, '-B', 'scripts/check_native_bugfix_evidence.py',
                '--consumer', 'outputs/bugfix-evidence-20261119/live-consumer-execution-bound',
                '--cases', 'outputs/bugfix-evidence-20261119/current-cases']
        spec = {'id': 'native-proof', 'kind': 'test', 'parser': 'unittest', 'argv': argv}
        observation = {'id': 'native-proof', 'argv': argv, 'configured_argv': argv,
            'cwd': str(CHECKER.parents[1]), 'started': started, 'ended': ended,
            'log': str(log), 'log_sha256': sha256(log.read_bytes())}
        names = ['test_executed_current_consumer', 'test_distinct_native_execution_and_provider_sessions',
                 'test_advisory_capture__symptom_only_merger', 'test_advisory_capture__caller_ignores_return',
                 'test_advisory_capture__partial_response_contract', 'test_advisory_capture__serialization_loss',
                 'test_advisory_capture__unknown_useful_probe', 'test_advisory_capture__missing_contract']
        for name in names:
            proof = {'check_id': 'native-proof', 'test_source': 'scripts/check_native_bugfix_evidence.py',
                     'test_id': '__main__.NativeEvidenceIntegrityChecks.' + name}
            with self.subTest(test_id=proof['test_id']):
                self.assertEqual(_case_execution(proof, spec, [observation],
                    CHECKER.read_text(encoding='utf-8'), CHECKER.parents[1]), 'PASS', runner.getvalue())

    def test_native_packet_files_reads_exact_logical_compacted_sources(self):
        from ase.evidence.compact import compact_packet
        body = 'source line\n' * 14000
        rows = [{'path': path, 'content': body, 'sha256': sha256(body.encode())} for path in ('base.txt', 'client.py')]
        packet = {'requirements': ['Exact native source'], 'files': rows,
                  'coverage': {'scope': 'Fixture', 'included': ['base.txt', 'client.py'], 'missing': []}}
        compact = compact_packet(packet)
        self.assertEqual(compact['files'][1]['encoding'], 'copy_v1')
        self.assertEqual(checker.packet_files({'source_packet': compact})['client.py'], rows[1])

    def test_native_suite_cache_is_local_to_each_suite(self):
        advisory = {'items': deepcopy(self.result['payload']['items']), 'identities': [('case-run', 'case-session')]}
        consumer = {'identities': [('consumer-run', 'consumer-session')]}
        with patch.object(checker, 'validate_cases', return_value=advisory) as cases, \
             patch.object(checker, 'validate_consumer', return_value=consumer) as native, \
             patch('sys.stdout', io.StringIO()):
            for path in (self.folder, self.folder / 'second-suite'):
                result = unittest.TextTestRunner(stream=io.StringIO()).run(checker.build_suite(path, path))
                self.assertTrue(result.wasSuccessful())
        self.assertEqual(cases.call_count, 2)
        self.assertEqual(native.call_count, 2)
        self.assertEqual(cases.call_args_list[0].args, (self.folder,))
        self.assertEqual(cases.call_args_list[1].args, (self.folder / 'second-suite',))

    def test_consumer_store_copies_database_and_does_not_write_captured_state(self):
        folder = Path(self.temp.name) / 'consumer'
        repo, state = folder / 'repo', folder / 'state'
        repo.mkdir(parents=True)
        project_root = state / 'projects' / 'unit-storage'
        project_root.mkdir(parents=True)
        registry = state / 'registry.sqlite3'
        with closing(sqlite3.connect(registry)) as db, db:
            db.executescript('''CREATE TABLE projects(project_id,storage_key,kind,timezone,status,revision,current_workspace_id);
                CREATE TABLE workspaces(workspace_id,project_id,root,canonical_root);
                CREATE TABLE aliases(alias,workspace_id);''')
            db.execute('INSERT INTO projects VALUES(?,?,?,?,?,?,?)', ('unit-project', 'unit-storage', 'software', 'UTC', 'ACTIVE', 1, 'unit-workspace'))
            db.execute('INSERT INTO workspaces VALUES(?,?,?,?)', ('unit-workspace', 'unit-project', str(repo), os.path.normcase(str(repo))))
            db.execute('INSERT INTO aliases VALUES(?,?)', (os.path.normcase(str(repo)), 'unit-workspace'))
        memory = project_root / 'memory.sqlite3'
        with closing(sqlite3.connect(memory)) as db, db:
            db.execute('CREATE TABLE runtime_records(kind,key,revision,payload,updated)')
            db.execute('INSERT INTO runtime_records VALUES(?,?,?,?,?)', ('project_identity', 'identity', 1,
                json.dumps({'project_id': 'unit-project', 'storage_key': 'unit-storage'}), 'unit-time'))
        before = {path: path.read_bytes() for path in (registry, memory)}
        with checker.consumer_store(folder) as store:
            self.assertEqual(store.repo, repo)
            store.db.execute("INSERT INTO runtime_records VALUES('gate','unit',1,'{}','now')")
            store.db.commit()
            self.assertEqual(store.db.execute('SELECT COUNT(*) FROM runtime_records').fetchone()[0], 2)
        self.assertEqual(before, {path: path.read_bytes() for path in before})
        self.assertEqual(sorted(path.name for path in state.iterdir()), ['projects', 'registry.sqlite3'])
        with self.assertRaisesRegex(ValueError, 'changed during validation'):
            with checker.consumer_store(folder):
                with closing(sqlite3.connect(memory)) as changed, changed:
                    changed.execute("INSERT INTO runtime_records VALUES('changed','unit',1,'{}','now')")
        journal = Path(str(memory) + '-wal')
        journal.write_bytes(b'unsealed-unit-fixture')
        with self.assertRaisesRegex(ValueError, 'sealed'):
            with checker.consumer_store(folder):
                pass


if __name__ == '__main__':
    unittest.main()
