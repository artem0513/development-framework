"""Cross-service lifecycle checks with actual files/state, no model verdict stubs."""
import copy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import subprocess
import sys
from pathlib import Path
import tempfile
import unittest

from ase.discovery import discover_capabilities, record_discovery, validate_intake
from ase.gates import GateService
from ase.journal import Journal
from ase.knowledge import Knowledge
from ase.state import read_record
from ase.store import Store, now
from ase.tasks import TaskService
from ase.workflow import WorkflowController, apply_edits


def document_contract(task_id, *, parent=None):
    value = {'id': task_id, 'schema_version': 2, 'profile': 'document',
             'request': 'Prepare the agreed operating guide',
             'scope': {'resources': ['guide.md'], 'effects': ['file_write']},
             'acceptance': [{'id': 'AC1', 'expected': 'Guide contains the agreed operating steps', 'evidence_ids': ['guide']}],
             'evidence': [{'id': 'guide', 'kind': 'artifact', 'path': 'guide.md', 'required': True}],
             'stages': [{'id': 'draft', 'acceptance_ids': ['AC1']}], 'unknowns': []}
    if parent:
        value.update(parent_task_id=parent, parent_stage_id='draft')
    return value


class WorkflowScenarioTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='ase-scenarios-')
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.workspace, self.home = self.base / 'work', self.base / 'state'
        self.workspace.mkdir()
        self.store = Store(self.workspace, self.home)
        self.addCleanup(self.store.close)
        self.tasks = TaskService(self.store)

    def approve(self, value, stages=None):
        task = self.tasks.create(value)
        stages = stages or ['draft']
        task = self.tasks.propose(task['task_id'], {'summary': 'Prepare agreed scoped draft', 'stage_ids': stages}, expected_revision=task['revision'])
        return self.tasks.approve(task['task_id'], proposal_hash=task['proposal_hash'],
                                  user_message_ref='user:approved-demo-scope', approved_stage_ids=stages)

    def test_root_twelve_cycles_cover_children_and_survive_reopen_and_revision(self):
        original = document_contract('root-task')
        # Isolate the global ceiling: delegates now share the underlying stage
        # allowance, so this scenario explicitly gives that obligation12 cycles.
        original['execution_policy'] = {'recovery_mode': 'fixed', 'max_stage_cycles': 12}
        self.approve(original)
        controller = WorkflowController(self.store)
        for child_number in range(4):
            child = self.tasks.create(document_contract('child-' + str(child_number), parent='root-task'))
            for iteration in range(3):
                spent = controller.reserve_cycle(child['task_id'], 'draft')
                self.assertEqual(spent['task_cycles'], child_number * 3 + iteration + 1)
        with self.assertRaisesRegex(ValueError, 'budget'):
            controller.reserve_cycle('root-task', 'draft')
        self.store.close()
        self.store = Store(self.workspace, self.home)
        self.addCleanup(self.store.close)
        self.tasks = TaskService(self.store)
        current = self.tasks.snapshot('root-task')
        revised = {**original, 'assumptions': ['Scope is unchanged; a new session cannot reset spending.']}
        task = self.tasks.revise('root-task', revised, expected_revision=current['revision'])
        task = self.tasks.propose('root-task', {'summary': 'Keep original outcome and scope', 'stage_ids': ['draft']}, expected_revision=task['revision'])
        self.tasks.approve('root-task', proposal_hash=task['proposal_hash'], user_message_ref='user:revision-approval', approved_stage_ids=['draft'])
        fresh_child = self.tasks.create(document_contract('new-child', parent='root-task'))
        with self.assertRaisesRegex(ValueError, 'budget'):
            WorkflowController(self.store).reserve_cycle(fresh_child['task_id'], 'draft')
        self.assertEqual(read_record(self.store.db, 'budget', 'root-task')['payload']['task_cycles'], 12)

    def test_required_meeting_gap_maps_to_blocked_stage_while_draft_proceeds(self):
        intake = validate_intake({
            'source_refs': [{'id': 'meeting', 'kind': 'meeting', 'reference': 'meeting:scenario',
                             'observed_at': now(), 'timestamp': '00:01:00-00:01:30'}],
            'outcomes': [{'id': 'draft-guide', 'text': 'Prepare an operating guide', 'source_refs': ['meeting']}],
            'proposals': [{'id': 'publish-idea', 'text': 'A participant suggested immediate publication', 'source_refs': ['meeting']}],
            'decisions': [], 'contradictions': [],
            'gaps': [{'id': 'destination', 'text': 'Publication destination was inaudible', 'required': True, 'source_refs': ['meeting']}]})
        original = document_contract('meeting-task')
        original['stages'].append({'id': 'publish', 'acceptance_ids': [], 'depends_on': ['draft'],
                                   'scope': {'resources': ['guide.md'], 'effects': []}})
        task = self.tasks.create(original)
        discovery = discover_capabilities(self.store.project, scope={'resources': ['guide.md']}, backend_config={})
        record_discovery(self.store, 'meeting-task', {**discovery, 'intake': intake}, expected_revision=0)
        task = self.tasks.propose('meeting-task', {'summary': 'Draft while publication is unresolved', 'stage_ids': ['draft', 'publish']}, expected_revision=task['revision'])
        self.tasks.approve('meeting-task', proposal_hash=task['proposal_hash'], user_message_ref='user:draft', approved_stage_ids=['draft'])
        with self.assertRaisesRegex(ValueError, 'source gaps'):
            self.tasks.require_approval('meeting-task', 'draft')
        mapped = copy.deepcopy(original)
        mapped['unknowns'] = [{'id': 'destination', 'statement': 'Publication destination remains unknown', 'status': 'UNRESOLVED', 'blocking_stage_ids': ['publish']}]
        mapped['stages'][1]['unknown_ids'] = ['destination']
        task = self.tasks.revise('meeting-task', mapped, expected_revision=self.tasks.snapshot('meeting-task')['revision'])
        task = self.tasks.propose('meeting-task', {'summary': 'Only the independent draft is ready', 'stage_ids': ['draft', 'publish']}, expected_revision=task['revision'])
        self.tasks.approve('meeting-task', proposal_hash=task['proposal_hash'], user_message_ref='user:draft-confirmed', approved_stage_ids=['draft'])
        with self.assertRaisesRegex(ValueError, 'unknown'):
            self.tasks.approve('meeting-task', proposal_hash=task['proposal_hash'], user_message_ref='user:cannot-authorize-missing-decision', approved_stage_ids=['publish'])
        self.tasks.require_approval('meeting-task', 'draft')
        apply_edits(self.workspace, mapped['scope'], [{'path': 'guide.md', 'operation': 'write', 'before_sha256': '', 'content': 'Agreed operating steps\n'}])
        checks = WorkflowController(self.store).check('meeting-task', 'draft')
        self.assertEqual(checks['results'][0]['sha256'], hashlib.sha256(b'Agreed operating steps\n').hexdigest())
        journal = Journal(self.store)
        closed = journal.finish_session('meeting-task', 'scenario-session',
            {'finish_reason': 'blocked', 'summary': 'Draft saved; destination and independent review remain unresolved',
             'next_action': 'Confirm publication destination, then independently review the draft',
             'context': {'summary': 'Draft exists; parent obligations remain'},
             'blockers': [{'id': 'destination', 'reason': 'Publication destination remains unknown', 'next_probe': 'Obtain the missing user decision'}]},
            closure_key='scenario-close', expected_revision=0, expected_context_revision=0)
        self.assertEqual(closed['state'], 'BLOCKED')
        self.assertNotEqual(GateService(self.store).gate('meeting-task')['status'], 'APPROVED')
        today = datetime.now(timezone.utc).date()
        report = journal.meeting_report((today - timedelta(days=1)).isoformat(), (today + timedelta(days=2)).isoformat(), timezone='Europe/Bucharest')
        self.assertTrue(any(item['id'] == 'destination' for item in report['blockers']))
        self.assertEqual(self.tasks.readiness('meeting-task')['untracked_gap_ids'], [])
        self.assertEqual(self.tasks.snapshot('meeting-task')['contract']['unknowns'][0]['status'], 'UNRESOLVED')

    def test_same_basename_project_caches_do_not_share_source_freshness(self):
        stores = []
        for name, content in (('client-a', b'First operating policy'), ('client-b', b'Second operating policy')):
            root = self.base / name / 'workspace'
            root.mkdir(parents=True)
            (root / 'brief.md').write_bytes(content)
            store = Store(root, self.home)
            self.addCleanup(store.close)
            api = Knowledge(store)
            claim = api.record({'topic': 'policy', 'statement': name + ' policy', 'observed_at': now(),
                                'source_refs': ['local:brief.md'], 'scope': {'paths': ['brief.md']}})
            source = {'source_id': 'brief', 'locator': 'local:brief.md', 'version': 'sha256:' + hashlib.sha256(content).hexdigest(),
                      'observed_at_utc': now(), 'availability': 'AVAILABLE', 'kind': 'file'}
            api.record_observations([source], [{'claim_id': claim['id'], 'source_id': 'brief', 'observed_version': source['version']}],
                                    expected_revision=api.retrieve(scope={}, refresh=False)['revision'])
            stores.append((store, api))
        self.assertNotEqual(stores[0][0].project_id, stores[1][0].project_id)
        (stores[0][0].repo / 'brief.md').write_bytes(b'Dirty changed operating policy')
        first = stores[0][1].retrieve(scope={'paths': ['brief.md']})['claims']
        second = stores[1][1].retrieve(scope={'paths': ['brief.md']})['claims']
        self.assertEqual([(item['statement'], item['validity']) for item in first], [('client-a policy', 'STALE')])
        self.assertEqual([(item['statement'], item['validity']) for item in second], [('client-b policy', 'CURRENT')])

    def test_document_demo_without_user_approval_writes_no_deliverable(self):
        from examples.universal_workflow_demo import run_demo
        (self.workspace / 'brief.md').write_text('Use the confirmed procedure.\n')
        result = run_demo(self.workspace, self.home)
        self.assertEqual(result['task']['state'], 'AWAITING_APPROVAL')
        self.assertFalse((self.workspace / 'guide.md').exists())
        self.assertEqual(result['closure']['finish_reason'], 'awaiting_approval')

    def test_document_demo_with_scope_approval_closes_blocked_without_faking_review(self):
        from examples.universal_workflow_demo import run_demo
        (self.workspace / 'brief.md').write_text('Use the confirmed procedure.\n')
        result = run_demo(self.workspace, self.home, approval_user_message_ref='user:explicit-draft-approval')
        self.assertTrue((self.workspace / 'guide.md').is_file())
        guide = (self.workspace / 'guide.md').read_text()
        self.assertRegex(guide, r'Next action:.*publication destination')
        self.assertEqual(result['task']['approved_stage_ids'], ['draft'])
        self.assertEqual(result['closure']['finish_reason'], 'blocked')
        self.assertEqual(result['gate']['status'], 'BLOCKED')
        self.assertEqual(result['live_receipts'], [])
        self.assertTrue(result['meeting_report']['blockers'])

    def test_shipped_task_and_proposal_templates_are_valid_but_do_not_grant_approval(self):
        root = Path(__file__).resolve().parents[1]
        task = self.tasks.create(json.loads((root / 'templates/task-v2.json').read_text()))
        proposed = self.tasks.propose(task['task_id'], json.loads((root / 'templates/proposal.json').read_text()), expected_revision=task['revision'])
        self.assertEqual(proposed['state'], 'AWAITING_APPROVAL')
        with self.assertRaisesRegex(ValueError, 'approval'):
            self.tasks.require_approval(task['task_id'], 'draft')
        self.assertFalse((self.workspace / 'deliverable.md').exists())

    def test_demo_cli_cannot_start_live_review_without_observed_scope_approval(self):
        root = Path(__file__).resolve().parents[1]
        home = self.base / 'uncreated-live-state'
        result = subprocess.run([sys.executable, str(root / 'examples/universal_workflow_demo.py'),
                                 '--live-codex', sys.executable, '--state-home', str(home)],
                                cwd=root, capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 2)
        self.assertIn('--approval-user-message-ref', result.stderr)
        self.assertFalse(home.exists())


if __name__ == '__main__':
    unittest.main()
