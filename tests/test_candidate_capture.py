"""Host capture identity and timing controls; protocol captures are not AC."""
import copy
from datetime import datetime, timedelta
import json
from pathlib import Path
import unittest

from ase.candidate_capture import completed, completed_interval, reference
from ase.protocol import canonical
from ase.runner import Runner
from ase.workflow import EDIT_SCHEMA
from ase.state import read_record
import test_local_edit_authority as authority_fixture
from test_runner import TransportFixture
from ase.adapters.codex import CodexAdapter


class HostExecutionIDTests(TransportFixture):
    def test_reserved_execution_is_used_once_and_cannot_be_reexecuted(self):
        runner = Runner(self.config(), self.base / 'artifacts')
        execution = 'a' * 32
        result = runner.run(CodexAdapter(), self.request(), execution_id=execution)
        self.assertEqual(result['receipt']['execution_id'], execution)
        self.assertEqual(Path(result['receipt']['artifact_root']).name, execution)
        with self.assertRaises(FileExistsError):
            runner.run(CodexAdapter(), self.request(), execution_id=execution)
        self.assertEqual(len(list((self.base / 'artifacts').glob('*/receipt.json'))), 1)

    def test_invalid_host_ids_reject_before_transport_or_artifact_creation(self):
        runner = Runner(self.config(), self.base / 'artifacts')
        for execution in ('../escape', 'A' * 32, '0' * 31, 1, True, [], ''):
            with self.subTest(execution=execution), self.assertRaises(ValueError):
                runner.run(CodexAdapter(), self.request(), execution_id=execution)
        self.assertFalse(runner.artifact_root.exists())

    def test_target_request_cannot_select_execution_identity(self):
        runner = Runner(self.config(), self.base / 'artifacts')
        request = dict(self.request(), execution_id='a' * 32)
        with self.assertRaises(ValueError):
            runner.run(CodexAdapter(), request)
        self.assertFalse(runner.artifact_root.exists())


class CandidateCaptureTests(unittest.TestCase):
    def setUp(self):
        fixture = authority_fixture.LocalEditAuthorityTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.fixture, self.controller = fixture, fixture.controller
        receipt = fixture.receipt
        self.path = Path(receipt['artifact_root']) / 'receipt.json'
        self.raw = self.path.read_bytes()
        request = json.loads(Path(receipt['artifacts']['request']['path']).read_bytes())
        self.ref, frozen = reference('local-edit', 'draft', fixture.progress['candidate_id'], receipt['execution_id'], request)
        self.assertEqual(canonical(frozen), canonical(request))

    def test_exact_capture_reloads_the_original_sealed_response(self):
        result = completed(self.controller, self.ref)
        self.assertEqual(result['receipt'], self.fixture.receipt)
        self.assertEqual(result['result']['status'], 'COMPLETE')
        self.assertEqual(len(result['result']['payload']['edits']), 2)

    def test_nonobject_receipt_is_a_controlled_validation_failure(self):
        for value in ([], ['receipt'], 'receipt', None, 1):
            with self.subTest(value=value):
                self.path.write_bytes(canonical(value))
                with self.assertRaises(ValueError):
                    completed(self.controller, self.ref)
        self.path.write_bytes(self.raw)

    def test_malformed_reference_is_a_controlled_validation_failure(self):
        for value in ([], ['capture'], 'capture', None, dict(self.ref, version=True),
                dict(self.ref, task_id=[]), dict(self.ref, stage_id=1), dict(self.ref, extra='foreign')):
            with self.subTest(value=value), self.assertRaises(ValueError):
                completed(self.controller, value)

    def test_malformed_receipt_artifact_map_is_a_controlled_validation_failure(self):
        for value in ([], ['artifact'], 'artifact', None):
            with self.subTest(value=value):
                receipt = json.loads(self.raw)
                receipt['artifacts'] = value
                self.path.write_bytes(canonical(receipt))
                with self.assertRaises(ValueError):
                    completed(self.controller, self.ref)
        self.path.write_bytes(self.raw)

    def test_completed_interval_uses_original_start_and_sealed_process_end(self):
        receipt = self.fixture.receipt
        launched = datetime.fromisoformat(receipt['started_at_utc'])
        start = launched - timedelta(seconds=2)
        active = {'task_id': 'local-edit', 'started': start.isoformat(), 'implementation_capture': self.ref}
        expected = (datetime.fromisoformat(receipt['ended_at_utc']) - start).total_seconds()
        self.assertEqual(completed_interval(self.controller, active, 'local-edit'), expected)
        self.assertGreaterEqual(expected, 2)

    def test_completed_interval_rejects_wrong_active_task_or_root(self):
        receipt = self.fixture.receipt
        active = {'task_id': 'local-edit', 'started': receipt['started_at_utc'], 'implementation_capture': self.ref}
        for value, root in ((dict(active, task_id='other'), 'local-edit'), (active, 'other')):
            with self.subTest(value=value, root=root), self.assertRaises(ValueError):
                completed_interval(self.controller, value, root)

    def test_completed_interval_rejects_capture_before_active_start(self):
        receipt = self.fixture.receipt
        start = datetime.fromisoformat(receipt['ended_at_utc']) + timedelta(seconds=1)
        active = {'task_id': 'local-edit', 'started': start.isoformat(), 'implementation_capture': self.ref}
        with self.assertRaises(ValueError):
            completed_interval(self.controller, active, 'local-edit')

    def test_completed_child_capture_is_resolved_under_its_root_accounting_key(self):
        fixture = self.fixture
        tasks = fixture.tasks
        parent_spec = dict(copy.deepcopy(fixture.task['contract']), id='parent')
        parent = tasks.create(parent_spec)
        parent = tasks.propose('parent', {'summary': 'Same approved two-file domain', 'stage_ids': ['draft']},
            expected_revision=parent['revision'])
        tasks.approve('parent', proposal_hash=parent['proposal_hash'], user_message_ref='test:parent-domain', approved_stage_ids=['draft'])
        child_spec = dict(copy.deepcopy(parent_spec), id='child', parent_task_id='parent', parent_stage_id='draft')
        child = tasks.create(child_spec)
        reservation = self.controller.reserve_cycle('child', 'draft')
        stage = child['contract']['stages'][0]
        request = self.controller._request('child', 'implementation', EDIT_SCHEMA,
            {'stage': stage, 'candidate_id': reservation['candidate_id']})
        runner = Runner(self.controller.runner.config, fixture.store.root / 'tasks/child/sessions')
        capture = runner.run(CodexAdapter(), request)
        receipt = capture['receipt']
        ref, _ = reference('child', 'draft', reservation['candidate_id'], receipt['execution_id'], request)
        active = {'task_id': 'child', 'started': receipt['started_at_utc'], 'implementation_capture': ref}
        self.assertGreaterEqual(completed_interval(self.controller, active, 'parent'), 0)
        self.assertEqual(read_record(fixture.store.db, 'budget', 'parent')['payload']['task_cycles'], 1)
        with self.assertRaises(ValueError):
            completed_interval(self.controller, active, 'child')
