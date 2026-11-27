"""Owned crash staging does not consume the admitted source-inventory budget."""
import hashlib
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest

from ase.edit_application import plan_edits
from ase.gates import source_snapshot
from ase.local_edit_intent import _guard
from ase.workflow import _target


class LocalEditInventoryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='ase-edit-inventory-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for number in range(1000):
            (self.root / ('f%04d.txt' % number)).write_bytes(b'old')
        database = sqlite3.connect(':memory:')
        self.addCleanup(database.close)
        database.execute('CREATE TABLE task_discoveries(task_id TEXT, revision INTEGER, payload TEXT)')
        self.store = SimpleNamespace(db=database, repo=self.root, workspace_id='disposable')
        self.controller = SimpleNamespace(store=self.store)
        self.task = {'task_id': 'edge', 'contract': {'profile': 'document',
            'scope': {'resources': ['.'], 'effects': ['file_write']}}}
        edits = [{'path': 'f0000.txt', 'operation': 'write',
            'before_sha256': hashlib.sha256(b'old').hexdigest(), 'content': 'after'}]
        self.plan = plan_edits(self.root, self.task['contract']['scope'], edits, 'a' * 32, _target)

    def test_owned_partial_stage_does_not_block_recovery_at_source_limit(self):
        before = _guard(self.controller, self.task, self.plan)
        stage = self.root / self.plan['edits'][0]['temporary_path']
        stage.write_bytes(b'a')
        self.assertEqual(_guard(self.controller, self.task, self.plan), before)
        self.assertEqual(stage.read_bytes(), b'a')
        with self.assertRaisesRegex(ValueError, 'inventory exceeds limit'):
            source_snapshot(self.store, self.task['contract'], task_id='edge')

    def test_foreign_stage_name_still_consumes_source_inventory_budget(self):
        foreign = self.root / ('f0000.txt.ase-' + 'b' * 32 + '-0.tmp')
        foreign.write_bytes(b'a')
        with self.assertRaisesRegex(ValueError, 'inventory exceeds limit'):
            _guard(self.controller, self.task, self.plan)
        self.assertEqual(foreign.read_bytes(), b'a')


class PrerequisiteInventoryRecoveryTests(unittest.TestCase):
    """Real 1000-source resume; scheduler seams never grant semantic AC."""

    def prepared(self, *, inherited=False):
        import copy
        import json
        from unittest.mock import patch
        import test_audit_prerequisite as fixtures
        from ase import edit_application
        from ase.discovery import record_discovery
        from ase.gates import GateService
        from ase.state import read_record

        fixture = fixtures.PrerequisiteResumeTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        actual_create = fixture.tasks.create
        def create_with_exact_observer(spec, *args, **kwargs):
            spec = copy.deepcopy(spec)
            for item in spec['stages']:
                for action in item.get('actions', []):
                    if action['id'] == 'dependent-effect':
                        action['observation_argv'][-1] = (
                            "from pathlib import Path;import json;p=Path('dependent.count');"
                            "ok=p.is_file() and p.read_text()=='1';"
                            "print(json.dumps({'exists':ok,'resource_id':'disposable-dependent' if ok else None}))")
            return actual_create(spec, *args, **kwargs)
        with patch.object(fixture.tasks, 'create', side_effect=create_with_exact_observer):
            if inherited:
                controller, parent, task = fixture.inherited_fixture(depth=2)
            else:
                controller, task = fixture.fixture()
                parent = task
        root = fixture.store.repo
        for number in range(996):
            (root / ('f%04d.txt' % number)).write_bytes(b'ordinary source\n')
        (root / 'dependent.txt').write_bytes(b'old dependent\n')
        (root / 'dependent.count').write_bytes(b'0')
        for owner in controller._ancestry(task['task_id']):
            row = fixture.store.db.execute('SELECT revision,payload FROM task_discoveries WHERE task_id=? ORDER BY revision DESC LIMIT 1',
                (owner['task_id'],)).fetchone()
            discovery = json.loads(row[1])
            discovery['scope']['resources'] = ['.']
            record_discovery(fixture.store, owner['task_id'], discovery, expected_revision=row[0])
        self.assertEqual(len(source_snapshot(fixture.store, task['contract'], task_id=task['task_id'])['files']), 1000)
        fixture.dependent_file_candidate(controller)
        provider = Path(controller.runner.config['executable_args'][0])
        before = hashlib.sha256(b'old dependent\n').hexdigest()
        provider.write_text(provider.read_text().replace("'before_sha256':''", "'before_sha256':" + repr(before)))
        real_verify = controller.verify
        observed = []

        def current_scheduler_judgment(task_id, stage_id=None):
            if stage_id in {'prepare', 'bridge'}:
                # Exercise the real packet and gate paths before the external
                # scheduler judgment seam. Protocol receipts stay unapproved.
                packet = controller._packet(task_id, stage_id)
                self.assertEqual(len([entry for entry in packet['files'] if not entry['path'].startswith('_ase_')]), 1000)
                self.assertNotEqual(GateService(fixture.store).gate(task_id, stage_id=stage_id)['status'], 'APPROVED')
                observed.append(task_id)
                return fixture.scheduler_only_approved(controller.tasks.snapshot(task_id), stage_id)
            return real_verify(task_id, stage_id)

        original = edit_application._stage_bytes
        def interrupt_after_stage(*args, **kwargs):
            original(*args, **kwargs)
            raise KeyboardInterrupt('Controlled interruption after staging')
        with patch.object(controller, 'verify', side_effect=current_scheduler_judgment), \
                patch.object(edit_application, '_stage_bytes', side_effect=interrupt_after_stage):
            stopped = controller.run(task['task_id'], resume=True)
        self.assertEqual(stopped['status'], 'BLOCKED')
        progress = read_record(fixture.store.db, 'cycle', task['task_id'] + ':dependent')['payload']
        self.assertEqual(progress['phase'], 'APPLYING')
        intent = read_record(fixture.store.db, 'local_edit_application', progress['local_edit_application_id'])['payload']
        stage = next(item for item in task['contract']['stages'] if item['id'] == 'dependent')
        temporary = root / intent['file_plan']['edits'][0]['temporary_path']
        self.assertEqual(temporary.read_bytes(), b'dependent candidate write')
        return fixture, controller, parent, task, stage, progress, temporary, current_scheduler_judgment, observed

    def assert_resumes_once(self, inherited):
        from unittest.mock import patch
        from ase.state import read_record
        fixture, controller, parent, task, stage, progress, temporary, judgment, observed = self.prepared(inherited=inherited)
        before_budget = read_record(fixture.store.db, 'budget', parent['task_id'])['payload']
        observed.clear()
        with patch.object(controller, 'verify', side_effect=judgment):
            result = controller.run(task['task_id'], resume=True)
        self.assertEqual((fixture.store.repo / 'dependent.txt').read_bytes(), b'dependent', result)
        self.assertFalse(temporary.exists())
        self.assertEqual(read_record(fixture.store.db, 'local_edit_application', progress['local_edit_application_id'])['payload']['state'], 'APPLIED')
        self.assertEqual(read_record(fixture.store.db, 'budget', parent['task_id'])['payload']['task_cycles'], before_budget['task_cycles'])
        self.assertIn(parent['task_id'], observed)
        self.assertEqual((fixture.store.repo / 'dependent.count').read_text(), '1')
        with patch.object(controller, 'verify', side_effect=judgment):
            controller.run(task['task_id'], resume=True)
        self.assertEqual((fixture.store.repo / 'dependent.count').read_text(), '1')

    def test_saved_intent_resumes_at_1000_source_boundary(self):
        self.assert_resumes_once(False)

    def test_saved_child_intent_refreshes_all_ancestor_source_owners(self):
        self.assert_resumes_once(True)

    def test_untrusted_staging_and_authority_or_proof_drift_hold_without_writes(self):
        from unittest.mock import patch
        from ase.state import read_record
        for change in ('tampered', 'foreign', 'authority', 'proof'):
            with self.subTest(change=change):
                fixture, controller, parent, task, stage, progress, temporary, judgment, observed = self.prepared()
                if change == 'tampered':
                    temporary.write_bytes(b'foreign content')
                elif change == 'foreign':
                    (fixture.store.repo / 'foreign.ase-temp.tmp').write_bytes(b'foreign content')
                elif change == 'authority':
                    fixture.tasks.approve(parent['task_id'], proposal_hash=parent['proposal_hash'],
                        user_message_ref='test:changed-approval', approved_stage_ids=['prepare', 'dependent'])
                else:
                    controller._save('stage', parent['task_id'] + ':prepare', None)
                staged = temporary.read_bytes()
                with patch.object(controller, 'verify', side_effect=judgment):
                    result = controller.run(task['task_id'], resume=True)
                self.assertEqual(result['status'], 'BLOCKED')
                self.assertEqual((fixture.store.repo / 'dependent.txt').read_bytes(), b'old dependent\n')
                self.assertEqual(temporary.read_bytes(), staged)
                self.assertEqual((fixture.store.repo / 'dependent.count').read_bytes(), b'0')
                self.assertEqual(read_record(fixture.store.db, 'local_edit_application', progress['local_edit_application_id'])['payload']['state'], 'PREPARED')

    def test_recovery_inventory_context_is_exact_scoped_and_resets_on_error(self):
        import concurrent.futures
        from unittest.mock import patch
        from ase.local_edit_intent import recovery_inventory
        from ase.workflow import EDIT_SCHEMA
        from ase.store import Store
        fixture, controller, parent, task, stage, progress, temporary, judgment, observed = self.prepared()
        other = Store(fixture.store.repo, fixture.store.state_home)
        self.addCleanup(other.close)
        # Use a real cross-thread-capable connection solely for the isolation
        # observation. The workflow itself retains normal connection ownership.
        original = fixture.store.db
        database = original.execute('PRAGMA database_list').fetchone()[2]
        shared = sqlite3.connect(database, check_same_thread=False)
        shared.row_factory = sqlite3.Row
        fixture.store.db = shared
        try:
            with self.assertRaisesRegex(RuntimeError, 'controlled exit'):
                with recovery_inventory(controller, task, stage, progress, _target, EDIT_SCHEMA):
                    self.assertEqual(len(source_snapshot(fixture.store, task['contract'], task_id=task['task_id'])['files']), 1000)
                    with self.assertRaisesRegex(ValueError, 'inventory exceeds limit'):
                        source_snapshot(fixture.store, {**task['contract'], 'scope': {'resources': ['.'], 'effects': ['file_write']}}, task_id='unrelated')
                    with self.assertRaisesRegex(ValueError, 'inventory exceeds limit'):
                        source_snapshot(other, task['contract'], task_id=task['task_id'])
                    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                        future = pool.submit(source_snapshot, fixture.store, task['contract'], task_id=task['task_id'])
                        with self.assertRaisesRegex(ValueError, 'inventory exceeds limit'):
                            future.result()
                    raise RuntimeError('controlled exit')
            with self.assertRaisesRegex(ValueError, 'inventory exceeds limit'):
                source_snapshot(fixture.store, task['contract'], task_id=task['task_id'])
        finally:
            fixture.store.db = original
            shared.close()
