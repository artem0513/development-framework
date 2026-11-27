"""Real local delegation records; acceptance here is advisory, never semantic AC."""
import copy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from ase.discovery import discover_capabilities, record_discovery
from ase.gates import GateService, source_snapshot
from ase.state import read_record
from ase.store import Store
from ase.tasks import TaskService
from ase.workflow import WorkflowController

REPOSITORY = Path(__file__).resolve().parents[1]


def prepare(base, task_id='delegated'):
    repo, home = base / 'work', base / 'state'
    repo.mkdir()
    (repo / 'inputs').mkdir()
    (repo / 'inputs/a.txt').write_text('Original A\n', encoding='utf-8')
    (repo / 'inputs/b.txt').write_text('Original B\n', encoding='utf-8')
    (repo / 'guide.md').write_text('Original guide\n', encoding='utf-8')
    store = Store(repo, home)
    tasks = TaskService(store)
    contract = {'id': task_id, 'schema_version': 2, 'profile': 'document',
        'request': 'Investigate the approved inputs and propose a sourced guide update',
        'scope': {'resources': ['inputs', 'guide.md'], 'effects': ['file_write']},
        'acceptance': [{'id': 'GUIDE', 'expected': 'The guide reflects the original confirmed inputs',
            'evidence_ids': ['guide']}],
        'evidence': [{'id': 'guide', 'kind': 'artifact', 'path': 'guide.md', 'required': True}],
        'stages': [{'id': 'draft', 'acceptance_ids': ['GUIDE']}], 'unknowns': []}
    task = tasks.create(contract)
    task = tasks.propose(task_id, {'summary': 'Inspect approved inputs and return bounded proposals',
        'stage_ids': ['draft']}, expected_revision=task['revision'])
    tasks.approve(task_id, proposal_hash=task['proposal_hash'],
        user_message_ref='test:delegation-original-scope', approved_stage_ids=['draft'])
    record_discovery(store, task_id, discover_capabilities(store.project,
        scope={'resources': ['inputs', 'guide.md']}, backend_config={}), expected_revision=0)
    return store


def registry(store):
    from ase.delegations import DelegationRegistry
    return DelegationRegistry(store)


def spec(name='research', *, depends_on=(), mode='read_only', resources=None):
    return {'id': name, 'stage_id': 'draft', 'goal': 'Investigate ' + name,
        'expected_result': 'Return a source-backed bounded finding for ' + name,
        'mode': mode, 'resources': ['inputs'] if resources is None else resources,
        'depends_on': list(depends_on)}


class DelegationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='ase-delegations-')
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.store = prepare(self.base)
        self.addCleanup(self.store.close)
        self.tasks = TaskService(self.store)
        self.repo = self.store.repo

    def reg(self):
        return registry(self.store)

    def start(self, name='research', *, depends_on=(), mode='read_only'):
        service = self.reg()
        value = service.register('delegated', spec(name, depends_on=depends_on, mode=mode))
        dispatched = service.dispatch('delegated', name, expected_revision=value['revision'],
            coordinator='lead', backend='codex')
        return dispatched

    def result(self, dispatched, name='research', *, status='COMPLETE', agent_id=None):
        service = self.reg()
        attempt = dispatched['payload']['attempt']
        native = {'backend': 'codex', 'agent_id': agent_id or 'native-' + name}
        current = service.attach('delegated', name, attempt['id'], native,
            expected_revision=dispatched['revision'])
        output = Path(dispatched['handoff']['artifact_dir'])
        output.mkdir(parents=True, exist_ok=True)
        path = output / 'finding.txt'
        path.write_text('Verified bounded finding for ' + name + '\n', encoding='utf-8')
        payload = {'attempt_id': attempt['id'], 'agent_id': native['agent_id'], 'status': status,
            'summary': 'Original-input analysis for ' + name,
            'artifacts': [{'path': 'finding.txt', 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}]}
        recorded = service.result('delegated', name, attempt['id'], payload,
            expected_revision=current['revision'])
        return recorded, payload, path

    def accepted(self, name='research', *, depends_on=()):
        dispatched = self.start(name, depends_on=depends_on)
        recorded, payload, path = self.result(dispatched, name)
        accepted = self.reg().accept('delegated', name, dispatched['payload']['attempt']['id'],
            expected_revision=recorded['revision'], coordinator='lead', summary='Compared finding with declared inputs')
        return accepted, path

    def test_cli_exposes_persistent_registry_list(self):
        process = subprocess.run([sys.executable, '-B', '-m', 'ase', '--repo', str(self.repo),
            '--home', str(self.store.state_home), 'delegation', 'list', 'delegated'],
            cwd=REPOSITORY, capture_output=True, text=True, timeout=20)
        self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
        payload = json.loads(process.stdout)
        self.assertEqual(payload['task_id'], 'delegated')
        self.assertEqual(payload['work'], [])

    def test_registration_is_immutable_idempotent_and_does_not_grant_acceptance(self):
        service = self.reg()
        value = service.register('delegated', spec())
        self.assertEqual(value['payload']['state'], 'PENDING')
        self.assertEqual(service.register('delegated', spec()), value)
        changed = dict(spec(), goal='Different work under an old ID')
        with self.assertRaises(ValueError):
            service.register('delegated', changed)
        self.assertNotEqual(GateService(self.store).gate('delegated')['status'], 'APPROVED')

    def test_operational_registry_progress_does_not_change_source_fingerprint(self):
        task = self.tasks.snapshot('delegated')
        before = source_snapshot(self.store, task['contract'], task_id='delegated')
        dispatched = self.start()
        self.result(dispatched)
        after = source_snapshot(self.store, task['contract'], task_id='delegated')
        self.assertEqual(before['fingerprint'], after['fingerprint'])
        self.assertEqual(before['files'], after['files'])

    def test_results_are_recordable_while_parent_execution_lease_is_held(self):
        dispatched = self.start()
        with WorkflowController(self.store)._ownership('delegated'):
            recorded, _, _ = self.result(dispatched)
        self.assertEqual(recorded['payload']['state'], 'RESULT_READY')

    def test_forward_self_unknown_and_cross_root_dependencies_are_rejected(self):
        service = self.reg()
        for dependencies in (['research'], ['missing']):
            with self.subTest(dependencies=dependencies), self.assertRaises(ValueError):
                service.register('delegated', spec(depends_on=dependencies))
        other = dict(copy.deepcopy(self.tasks.snapshot('delegated')['contract']), id='other')
        task = self.tasks.create(other)
        task = self.tasks.propose('other', {'summary': 'Different managed root', 'stage_ids': ['draft']},
            expected_revision=task['revision'])
        self.tasks.approve('other', proposal_hash=task['proposal_hash'],
            user_message_ref='test:other-root', approved_stage_ids=['draft'])
        record_discovery(self.store, 'other', discover_capabilities(self.store.project,
            scope={'resources': ['inputs', 'guide.md']}, backend_config={}), expected_revision=0)
        service.register('other', spec('foreign'))
        with self.assertRaises(ValueError):
            service.register('delegated', spec(depends_on=['foreign']))

    def test_two_ready_analyses_must_be_accepted_before_dependent_dispatch(self):
        service = self.reg()
        a = service.register('delegated', spec('a'))
        b = service.register('delegated', spec('b'))
        c = service.register('delegated', spec('c', depends_on=['a', 'b']))
        with self.assertRaises(ValueError):
            service.dispatch('delegated', 'c', expected_revision=c['revision'], coordinator='lead', backend='codex')
        for name, record in (('a', a), ('b', b)):
            dispatched = service.dispatch('delegated', name, expected_revision=record['revision'], coordinator='lead', backend='codex')
            result, _, _ = self.result(dispatched, name)
            if name == 'a':
                with self.assertRaises(ValueError):
                    service.dispatch('delegated', 'c', expected_revision=c['revision'], coordinator='lead', backend='codex')
            service.accept('delegated', name, dispatched['payload']['attempt']['id'],
                expected_revision=result['revision'], coordinator='lead', summary='Checked real output against source')
        dispatched = service.dispatch('delegated', 'c', expected_revision=c['revision'], coordinator='lead', backend='codex')
        self.assertEqual(dispatched['payload']['state'], 'DISPATCHING')
        self.assertTrue(dispatched['payload']['attempt']['dependencies'])

    def test_dispatch_is_cas_and_one_exact_attempt_without_new_managed_cycle(self):
        service = self.reg()
        value = service.register('delegated', spec())
        budget = read_record(self.store.db, 'budget', 'delegated')
        dispatched = service.dispatch('delegated', 'research', expected_revision=value['revision'], coordinator='lead', backend='codex')
        with self.assertRaises(ValueError):
            service.dispatch('delegated', 'research', expected_revision=value['revision'], coordinator='lead', backend='codex')
        self.assertEqual(service.get('delegated', 'research')['payload']['attempt']['id'], dispatched['payload']['attempt']['id'])
        self.assertEqual(read_record(self.store.db, 'budget', 'delegated'), budget)

    def test_scope_and_modes_do_not_authorize_target_mutations(self):
        service = self.reg()
        for changed in (dict(spec(), mode='write'), dict(spec(), resources=['outside']),
                dict(spec(), resources=['../escape']), dict(spec(), effects=['file_write'])):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                service.register('delegated', changed)
        before = (self.repo / 'guide.md').read_bytes()
        self.accepted()
        self.assertEqual((self.repo / 'guide.md').read_bytes(), before)
        self.assertNotEqual(GateService(self.store).gate('delegated')['status'], 'APPROVED')

    def test_unknown_dispatch_cannot_be_restarted_cancelled_or_expired(self):
        dispatched = self.start()
        service = self.reg()
        service.recover('delegated', 'research')
        current = service.get('delegated', 'research')
        self.assertEqual(current['payload']['state'], 'UNKNOWN')
        attempt = current['payload']['attempt']['id']
        with self.assertRaises(ValueError):
            service.dispatch('delegated', 'research', expected_revision=current['revision'], coordinator='lead', backend='codex')
        with self.assertRaises(ValueError):
            service.cancel('delegated', 'research', expected_revision=current['revision'], reason='Nothing visible is not proof of absence')
        service.recover('delegated', 'research')
        self.assertEqual(service.get('delegated', 'research')['payload']['attempt']['id'], attempt)
        self.assertEqual(dispatched['payload']['attempt']['id'], attempt)

    def test_unacknowledged_dispatch_blocks_overlapping_alias_before_recovery(self):
        original = self.start('original')
        service = self.reg()
        alias = service.register('delegated', spec('alias'))
        with self.assertRaisesRegex(ValueError, 'unresolved launch'):
            service.dispatch('delegated', 'alias', expected_revision=alias['revision'], coordinator='lead', backend='codex')
        self.assertEqual(service.get('delegated', 'original')['payload']['attempt'], original['payload']['attempt'])
        self.assertIsNone(service.get('delegated', 'alias')['payload']['attempt'])

    def test_running_exact_intent_alias_is_held_but_distinct_analysis_can_share_inputs(self):
        original = self.start('original')
        service = self.reg()
        service.attach('delegated', 'original', original['payload']['attempt']['id'],
            {'backend': 'codex', 'agent_id': 'original-native'}, expected_revision=original['revision'])
        alias = service.register('delegated', dict(original['payload']['spec'], id='alias'))
        with self.assertRaisesRegex(ValueError, 'same delegated intent'):
            service.dispatch('delegated', 'alias', expected_revision=alias['revision'], coordinator='lead', backend='codex')
        distinct = self.start('independent-analysis')
        attached = service.attach('delegated', 'independent-analysis', distinct['payload']['attempt']['id'],
            {'backend': 'codex', 'agent_id': 'independent-native'}, expected_revision=distinct['revision'])
        self.assertEqual(attached['payload']['state'], 'RUNNING')

    def test_disjoint_unacknowledged_analyses_can_dispatch_in_parallel(self):
        service = self.reg()
        first = service.register('delegated', spec('first', resources=['inputs/a.txt']))
        second = service.register('delegated', spec('second', resources=['inputs/b.txt']))
        for name, value in [('first', first), ('second', second)]:
            actual = service.dispatch('delegated', name, expected_revision=value['revision'], coordinator='lead', backend='codex')
            self.assertEqual(actual['payload']['state'], 'DISPATCHING')

    def test_unknown_attempt_can_attach_and_return_that_same_worker(self):
        dispatched = self.start()
        service = self.reg()
        service.recover('delegated', 'research')
        current = service.get('delegated', 'research')
        restored = dict(current, handoff=dispatched['handoff'])
        recorded, _, _ = self.result(restored)
        self.assertEqual(recorded['payload']['state'], 'RESULT_READY')
        self.assertEqual(recorded['payload']['attempt']['id'], dispatched['payload']['attempt']['id'])

    def test_running_attempt_cannot_be_rebound_to_another_native_handle(self):
        dispatched = self.start()
        service = self.reg()
        attempt = dispatched['payload']['attempt']['id']
        native = {'backend': 'codex', 'agent_id': 'native-original'}
        current = service.attach('delegated', 'research', attempt, native, expected_revision=dispatched['revision'])
        self.assertEqual(service.attach('delegated', 'research', attempt, native, expected_revision=current['revision']), current)
        for changed in ({'backend': 'claude', 'agent_id': 'native-original'}, {'backend': 'codex', 'agent_id': 'native-other'}):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                service.attach('delegated', 'research', attempt, changed, expected_revision=current['revision'])

    def test_foreign_attempt_and_coordinator_cannot_accept_a_result(self):
        dispatched = self.start()
        recorded, _, _ = self.result(dispatched)
        for attempt, coordinator in (('f' * 32, 'lead'), (dispatched['payload']['attempt']['id'], 'other-lead')):
            with self.subTest(attempt=attempt, coordinator=coordinator), self.assertRaises(ValueError):
                self.reg().accept('delegated', 'research', attempt, expected_revision=recorded['revision'],
                    coordinator=coordinator, summary='This cannot adopt foreign work')

    def test_result_requires_correct_native_identity_and_real_artifact_hash(self):
        dispatched = self.start()
        service = self.reg()
        attempt = dispatched['payload']['attempt']['id']
        current = service.attach('delegated', 'research', attempt, {'backend': 'codex', 'agent_id': 'real-worker'},
            expected_revision=dispatched['revision'])
        output = Path(dispatched['handoff']['artifact_dir'])
        output.mkdir(parents=True, exist_ok=True)
        (output / 'finding.txt').write_bytes(b'actual finding')
        valid = {'attempt_id': attempt, 'agent_id': 'real-worker', 'status': 'COMPLETE',
            'summary': 'Actual finding', 'artifacts': [{'path': 'finding.txt', 'sha256': hashlib.sha256(b'actual finding').hexdigest()}]}
        cases = [dict(valid, attempt_id='e' * 32), dict(valid, agent_id='another-worker'),
            dict(valid, artifacts=[{'path': 'finding.txt', 'sha256': '0' * 64}]),
            dict(valid, artifacts=[{'path': '../escape', 'sha256': '0' * 64}])]
        for payload in cases:
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                service.result('delegated', 'research', attempt, payload, expected_revision=current['revision'])
        self.assertEqual(service.get('delegated', 'research')['payload']['state'], 'RUNNING')

    def test_result_is_separate_from_acceptance_and_exact_resubmission_is_idempotent(self):
        dispatched = self.start()
        recorded, payload, _ = self.result(dispatched)
        service = self.reg()
        self.assertEqual(recorded['payload']['state'], 'RESULT_READY')
        again = service.result('delegated', 'research', dispatched['payload']['attempt']['id'], payload,
            expected_revision=recorded['revision'])
        self.assertEqual(again, recorded)
        accepted = service.accept('delegated', 'research', dispatched['payload']['attempt']['id'],
            expected_revision=recorded['revision'], coordinator='lead', summary='Inspected actual artifact')
        self.assertEqual(accepted['payload']['state'], 'ACCEPTED')
        self.assertEqual(service.closure_blockers('delegated'), [])

    def test_failed_terminal_result_cannot_be_accepted_but_can_be_discarded(self):
        dispatched = self.start()
        recorded, _, _ = self.result(dispatched, status='FAILED')
        with self.assertRaises(ValueError):
            self.reg().accept('delegated', 'research', dispatched['payload']['attempt']['id'],
                expected_revision=recorded['revision'], coordinator='lead', summary='Failure is not acceptance')
        cancelled = self.reg().cancel('delegated', 'research', expected_revision=recorded['revision'],
            reason='Observed terminal worker failure; investigate separately')
        self.assertEqual(cancelled['payload']['state'], 'CANCELLED')

    def test_only_undispatched_or_recorded_terminal_work_can_be_cancelled(self):
        service = self.reg()
        pending = service.register('delegated', spec('unused'))
        cancelled = service.cancel('delegated', 'unused', expected_revision=pending['revision'], reason='No useful independent work remains')
        self.assertEqual(cancelled['payload']['state'], 'CANCELLED')
        current = self.start()
        with self.assertRaises(ValueError):
            service.cancel('delegated', 'research', expected_revision=current['revision'], reason='Cannot hide an uncertain launch')

    def test_changed_source_blocks_dispatch_and_result_acceptance(self):
        service = self.reg()
        pending = service.register('delegated', spec('pending'))
        dispatched = self.start()
        recorded, _, _ = self.result(dispatched)
        (self.repo / 'inputs/a.txt').write_bytes(b'Owner changed approved input\n')
        with self.assertRaises(ValueError):
            service.dispatch('delegated', 'pending', expected_revision=pending['revision'], coordinator='lead', backend='codex')
        with self.assertRaises(ValueError):
            service.accept('delegated', 'research', dispatched['payload']['attempt']['id'],
                expected_revision=recorded['revision'], coordinator='lead', summary='Stale source cannot be fresh acceptance')

    def test_same_proposal_reapproval_does_not_rebind_original_dispatch(self):
        service = self.reg()
        pending = service.register('delegated', spec('pending'))
        dispatched = self.start()
        recorded, _, _ = self.result(dispatched)
        task = self.tasks.snapshot('delegated')
        self.tasks.approve('delegated', proposal_hash=task['proposal_hash'],
            user_message_ref='test:replacement-same-proposal-approval', approved_stage_ids=['draft'])
        for action in (lambda: service.dispatch('delegated', 'pending', expected_revision=pending['revision'], coordinator='lead', backend='codex'),
                lambda: service.accept('delegated', 'research', dispatched['payload']['attempt']['id'],
                    expected_revision=recorded['revision'], coordinator='lead', summary='Original authority is stale')):
            with self.assertRaises(ValueError):
                action()

    def test_new_stop_blocks_dispatch_and_acceptance_but_preserves_returned_result(self):
        service = self.reg()
        pending = service.register('delegated', spec('pending'))
        dispatched = self.start()
        WorkflowController(self.store).request_stop('delegated', reason='Owner paused while worker returned')
        recorded, _, _ = self.result(dispatched)
        self.assertEqual(recorded['payload']['state'], 'RESULT_READY')
        with self.assertRaises(ValueError):
            service.dispatch('delegated', 'pending', expected_revision=pending['revision'], coordinator='lead', backend='codex')
        with self.assertRaises(ValueError):
            service.accept('delegated', 'research', dispatched['payload']['attempt']['id'],
                expected_revision=recorded['revision'], coordinator='lead', summary='Stopped task cannot accept')

    def test_tampered_result_or_dependency_artifact_blocks_downstream_and_closure(self):
        service = self.reg()
        _, artifact = self.accepted('a')
        pending = service.register('delegated', spec('b', depends_on=['a']))
        artifact.write_bytes(b'Unverified replacement result')
        with self.assertRaises(ValueError):
            service.dispatch('delegated', 'b', expected_revision=pending['revision'], coordinator='lead', backend='codex')
        self.assertTrue(service.closure_blockers('delegated'))

    def test_accepted_advisory_history_survives_later_legitimate_integration(self):
        service = self.reg()
        self.accepted()
        (self.repo / 'guide.md').write_text('Integrated guide\n', encoding='utf-8')
        self.assertEqual(service.closure_blockers('delegated'), [])
        self.assertNotEqual(GateService(self.store).gate('delegated')['status'], 'APPROVED')

    def test_parent_closure_includes_unsettled_child_work_and_root_unique_ids(self):
        parent = self.tasks.snapshot('delegated')
        child_spec = dict(copy.deepcopy(parent['contract']), id='child', parent_task_id='delegated', parent_stage_id='draft')
        self.tasks.create(child_spec)
        record_discovery(self.store, 'child', discover_capabilities(self.store.project,
            scope={'resources': ['inputs', 'guide.md']}, backend_config={}), expected_revision=0)
        service = self.reg()
        service.register('child', spec('child-analysis'))
        with self.assertRaises(ValueError):
            service.register('delegated', spec('child-analysis'))
        self.assertTrue(service.closure_blockers('delegated'))

    def test_parent_reapproval_cannot_refresh_a_child_assignment(self):
        parent = self.tasks.snapshot('delegated')
        child_spec = dict(copy.deepcopy(parent['contract']), id='child', parent_task_id='delegated', parent_stage_id='draft')
        self.tasks.create(child_spec)
        record_discovery(self.store, 'child', discover_capabilities(self.store.project,
            scope={'resources': ['inputs', 'guide.md']}, backend_config={}), expected_revision=0)
        service = self.reg()
        value = service.register('child', spec('child-analysis'))
        self.tasks.approve('delegated', proposal_hash=parent['proposal_hash'],
            user_message_ref='test:parent-reapproval-after-child-register', approved_stage_ids=['draft'])
        with self.assertRaises(ValueError):
            service.dispatch('child', 'child-analysis', expected_revision=value['revision'], coordinator='lead', backend='codex')


    def test_snapshot_excludes_overlapping_unacknowledged_dispatch(self):
        self.start('original')
        self.reg().register('delegated', spec('alias'))
        current = self.reg().snapshot('delegated')
        self.assertNotIn('alias', current['ready'])
        self.assertIn('unresolved launch', ' '.join(current['blocked']['alias']))

    def test_snapshot_excludes_exact_intent_of_running_worker(self):
        original = self.start('original')
        service = self.reg()
        service.attach('delegated', 'original', original['payload']['attempt']['id'],
            {'backend': 'codex', 'agent_id': 'original-native'}, expected_revision=original['revision'])
        service.register('delegated', dict(original['payload']['spec'], id='alias'))
        current = service.snapshot('delegated')
        self.assertNotIn('alias', current['ready'])
        self.assertIn('same delegated intent', ' '.join(current['blocked']['alias']))

    def test_broken_transitive_artifact_blocks_readiness_and_dispatch(self):
        _, artifact = self.accepted('b')
        self.accepted('a', depends_on=['b'])
        pending = self.reg().register('delegated', spec('c', depends_on=['a']))
        artifact.write_bytes(b'Accidentally replaced after the intermediate result was accepted')
        with self.assertRaises(ValueError):
            self.reg().dispatch('delegated', 'c', expected_revision=pending['revision'], coordinator='lead', backend='codex')
        self.assertNotIn('c', self.reg().snapshot('delegated')['ready'])

    def test_broken_transitive_artifact_blocks_acceptance_after_dispatch(self):
        _, artifact = self.accepted('b')
        self.accepted('a', depends_on=['b'])
        dispatched = self.start('c', depends_on=['a'])
        recorded, _, _ = self.result(dispatched, 'c')
        artifact.write_bytes(b'Accidental upstream change while the final worker was running')
        with self.assertRaises(ValueError):
            self.reg().accept('delegated', 'c', dispatched['payload']['attempt']['id'],
                expected_revision=recorded['revision'], coordinator='lead', summary='Upstream provenance is no longer valid')
        self.assertEqual(self.reg().get('delegated', 'c')['payload']['state'], 'RESULT_READY')

    def test_intact_diamond_retains_direct_dependency_ticket_shape(self):
        self.accepted('b')
        self.accepted('a', depends_on=['b'])
        self.accepted('d', depends_on=['b'])
        dispatched = self.start('c', depends_on=['a', 'd'])
        self.assertEqual(set(dispatched['handoff']['dependencies']), {'a', 'd'})
        recorded, _, _ = self.result(dispatched, 'c')
        accepted = self.reg().accept('delegated', 'c', dispatched['payload']['attempt']['id'],
            expected_revision=recorded['revision'], coordinator='lead', summary='Checked the intact shared prerequisite chain')
        self.assertEqual(accepted['payload']['state'], 'ACCEPTED')

    def test_stopped_transitive_dependency_owner_blocks_downstream_dispatch(self):
        parent = self.tasks.snapshot('delegated')
        self.tasks.create(dict(copy.deepcopy(parent['contract']), id='child',
            parent_task_id='delegated', parent_stage_id='draft'))
        record_discovery(self.store, 'child', discover_capabilities(self.store.project,
            scope={'resources': ['inputs', 'guide.md']}, backend_config={}), expected_revision=0)
        service = self.reg()
        pending = service.register('child', spec('child-b'))
        dispatched = service.dispatch('child', 'child-b', expected_revision=pending['revision'], coordinator='lead', backend='codex')
        attempt = dispatched['payload']['attempt']['id']
        result = service.result('child', 'child-b', attempt, {'attempt_id': attempt, 'agent_id': 'child-native',
            'status': 'COMPLETE', 'summary': 'Bounded advisory child facts', 'artifacts': []}, expected_revision=dispatched['revision'])
        service.accept('child', 'child-b', attempt, expected_revision=result['revision'], coordinator='lead', summary='Original child facts')
        self.accepted('a', depends_on=['child-b'])
        pending = service.register('delegated', spec('c', depends_on=['a']))
        WorkflowController(self.store).request_stop('child', reason='Owner stopped the prerequisite task')
        with self.assertRaises(ValueError):
            service.dispatch('delegated', 'c', expected_revision=pending['revision'], coordinator='lead', backend='codex')

    def test_dispatch_cannot_return_a_ticket_inside_caller_transaction(self):
        service = self.reg()
        pending = service.register('delegated', spec('nested'))
        self.store.db.execute('BEGIN IMMEDIATE')
        try:
            with self.assertRaisesRegex(ValueError, 'transaction'):
                service.dispatch('delegated', 'nested', expected_revision=pending['revision'], coordinator='lead', backend='codex')
            self.assertTrue(self.store.db.in_transaction, 'The registry must not commit its caller transaction')
        finally:
            self.store.db.rollback()
        self.assertEqual(service.get('delegated', 'nested'), pending)
        first = service.dispatch('delegated', 'nested', expected_revision=pending['revision'], coordinator='lead', backend='codex')
        self.assertEqual(first['payload']['state'], 'DISPATCHING')


if __name__ == '__main__':
    unittest.main()
