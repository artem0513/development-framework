"""Durable result delivery is distinct from processing and task authorization."""
from contextlib import closing
import concurrent.futures
import hashlib
import json
import tempfile
import threading
import subprocess
import sys
from pathlib import Path
import unittest

from test_delegations import prepare, registry, spec
from ase.store import Store
from ase.state import read_record, transaction


class CoordinatorEventTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='ase-inbox-')
        self.addCleanup(self.temp.cleanup)
        self.store = prepare(Path(self.temp.name))
        self.addCleanup(self.store.close)
        from ase.coordinator import CoordinatorEvents
        self.service = CoordinatorEvents(self.store)
        self.endpoint = {'backend': 'codex', 'thread_id': 'original-main', 'host_id': 'local'}

    def bind(self):
        return self.service.bind('delegated', self.endpoint,
            user_message_ref='test:owner-asks-return-to-main', expected_revision=0)

    def enqueue(self, source_id='attempt-one', **changes):
        value = {'source_kind': 'delegation', 'source_id': source_id, 'status': 'COMPLETE',
            'summary': 'Investigated original inputs; result ready',
            'evidence': [{'ref': 'private-result', 'sha256': 'a' * 64}]}
        value.update(changes)
        return self.service.enqueue('delegated', **value)

    def test_result_survives_reopen_before_coordinator_is_bound(self):
        event = self.enqueue()
        with closing(Store(self.store.project)) as reopened:
            from ase.coordinator import CoordinatorEvents
            inbox = CoordinatorEvents(reopened).snapshot('delegated')
        self.assertEqual([row['payload']['id'] for row in inbox['events']], [event['payload']['id']])
        self.assertIsNone(inbox['coordinator'])

    def test_identical_source_deduplicates_but_changed_payload_conflicts(self):
        original = self.enqueue()
        self.assertEqual(self.enqueue(), original)
        with self.assertRaisesRegex(ValueError, 'conflict|different'):
            self.enqueue(summary='A different result under the same source identity')
        self.assertEqual(len(self.service.snapshot('delegated')['events']), 1)

    def test_binding_cannot_silently_redirect_original_chat(self):
        bound = self.bind()
        with self.assertRaisesRegex(ValueError, 'original|immutable|different'):
            self.service.bind('delegated', {**self.endpoint, 'thread_id': 'another-chat'},
                user_message_ref='test:unsolicited-redirect', expected_revision=bound['revision'])
        event = self.enqueue()
        ticket = self.service.dispatch('delegated', event['payload']['id'], expected_revision=event['revision'])
        self.assertEqual(ticket['handoff']['destination'], self.endpoint)
        self.assertIn(event['payload']['id'], ticket['handoff']['prompt'])
        self.assertIn('not user authorization', ticket['handoff']['prompt'])

    def test_dispatch_requires_bound_destination(self):
        event = self.enqueue()
        with self.assertRaisesRegex(ValueError, 'coordinator|destination'):
            self.service.dispatch('delegated', event['payload']['id'], expected_revision=event['revision'])

    def test_local_owner_can_process_without_a_provider_chat_id_or_transport(self):
        owner = self.service.bind_local('delegated', backend='copilot',
            user_message_ref='test:original-local-lead', expected_revision=0)
        destination = owner['payload']['destination']
        self.assertEqual(destination['transport'], 'local')
        self.assertEqual(destination['backend'], 'copilot')
        event = self.enqueue()
        with self.assertRaisesRegex(ValueError, 'local inbox|native.*unavailable'):
            self.service.dispatch('delegated', event['payload']['id'], expected_revision=event['revision'])
        claimed = self.service.claim('delegated', event['payload']['id'],
            thread_id=destination['thread_id'], expected_revision=event['revision'])
        done = self.service.ack('delegated', event['payload']['id'],
            processing_id=claimed['payload']['processing']['id'], thread_id=destination['thread_id'],
            outcome='handled', evidence='checkpoint:original-local-lead', expected_revision=claimed['revision'])
        self.assertEqual(done['payload']['processing']['state'], 'DONE')
        self.assertEqual(self.service.bind_local('delegated', backend='copilot',
            user_message_ref='test:original-local-lead', expected_revision=owner['revision']), owner)

    def test_pending_lists_unprocessed_events_with_their_exact_next_command(self):
        event = self.enqueue()
        event_id = event['payload']['id']
        unbound = self.service.pending('delegated')
        self.assertFalse(unbound['bound'])
        self.assertIn('--thread-id <thread_id from bind-local>', unbound['events'][0]['next_command'])
        self.bind()
        [row] = self.service.pending('delegated')['events']
        self.assertEqual((row['id'], row['source_kind'], row['status'], row['processing'], row['revision']),
                         (event_id, 'delegation', 'COMPLETE', 'PENDING', event['revision']))
        claim = row['next_command'].split()
        self.assertEqual(claim[:5], ['ase', 'events', 'claim', 'delegated', event_id])
        claimed = self.service.claim('delegated', event_id, thread_id=claim[claim.index('--thread-id') + 1],
            expected_revision=int(claim[claim.index('--expected-revision') + 1]))
        [row] = self.service.pending('delegated')['events']
        ack = row['next_command'].split()
        self.assertEqual(ack[:5], ['ase', 'events', 'ack', 'delegated', event_id])
        self.service.ack('delegated', event_id, processing_id=ack[ack.index('--processing-id') + 1],
            thread_id=ack[ack.index('--thread-id') + 1], outcome='handled', evidence='checkpoint',
            expected_revision=int(ack[ack.index('--expected-revision') + 1]))
        self.assertEqual(claimed['revision'] + 1, self.service.snapshot('delegated')['events'][0]['revision'])
        self.assertEqual(self.service.pending('delegated')['events'], [])

    def crafted_receipt(self, payload, role='review', stage_id=None):
        import uuid
        from ase.protocol import canonical, sha256
        execution = uuid.uuid4().hex
        root = self.store.root / 'tasks' / 'delegated' / 'sessions' / execution
        root.mkdir(parents=True)
        data = canonical({'status': 'COMPLETE', 'payload': payload, 'blocker': None, 'session_id': 's',
            'terminal': None, 'permission_denials': [], 'missing_coverage': []})
        (root / 'result.json').write_bytes(data)
        request = canonical({'role': role, 'handoff': {'stage_id': stage_id}})
        (root / 'request.json').write_bytes(request)
        receipt = {'receipt_version': 2, 'execution_id': execution, 'role': role, 'status': 'COMPLETE',
            'evidence_kind': 'independent_cli', 'artifact_root': str(root),
            'artifacts': {'result': {'path': str(root / 'result.json'), 'sha256': sha256(data), 'bytes': len(data)},
                          'request': {'path': str(root / 'request.json'), 'sha256': sha256(request), 'bytes': len(request)}}}
        (root / 'receipt.json').write_bytes(canonical(receipt))
        return receipt

    def test_pending_and_claim_show_safe_captured_counts_from_the_bound_receipt(self):
        self.bind()
        receipt = self.crafted_receipt({'findings': [
            {'severity': 'P1', 'path': 'a.py', 'line': 1, 'message': 'RAW-FINDING-TEXT'},
            {'severity': 'P3', 'path': 'b.py', 'line': 2, 'message': 'minor'},
            {'severity': 'P1', 'path': 'c.py', 'line': 3, 'message': 'other'}],
            'summary': 'RAW-SUMMARY-TEXT', 'unverified_material': ['RAW-MATERIAL']})
        event = self.service.record_receipt('delegated', 'review', receipt)
        self.enqueue()
        pending = self.service.pending('delegated')
        rows = {row['source_kind']: row for row in pending['events']}
        self.assertEqual(rows['verification']['captured_result'], {'role': 'review', 'evidence_kind': 'independent_cli',
            'result_status': 'COMPLETE', 'findings': {'P1': 2, 'P3': 1}, 'unverified_material_count': 1})
        self.assertNotIn('captured_result', rows['delegation'])
        self.assertNotIn('superseded_by', rows['verification'])
        self.assertIn('not a gate verdict', pending['classification_note'])
        self.assertNotRegex(json.dumps(pending), 'RAW-')
        claimed = self.service.claim('delegated', event['payload']['id'], thread_id='original-main',
            expected_revision=event['revision'])
        self.assertEqual(claimed['payload']['processing']['state'], 'CLAIMED')
        self.assertEqual(claimed['captured_result']['findings'], {'P1': 2, 'P3': 1})
        self.assertNotRegex(json.dumps(claimed), 'RAW-')

    def test_altered_or_unbound_result_is_not_summarized(self):
        receipt = self.crafted_receipt({'criteria': [{'id': 'AC1', 'status': 'PASS'}]}, role='ac')
        self.service.record_receipt('delegated', 'ac', receipt)
        [row] = self.service.pending('delegated')['events']
        self.assertEqual(row['captured_result']['criteria'], {'PASS': 1})
        result = Path(receipt['artifact_root']) / 'result.json'
        result.write_bytes(result.read_bytes().replace(b'PASS', b'FAIL'))
        [row] = self.service.pending('delegated')['events']
        self.assertNotIn('captured_result', row)
        self.assertNotIn('superseded_by', row)

    def test_result_is_superseded_only_by_a_later_recorded_result_of_the_same_scope(self):
        from unittest.mock import patch
        from ase.workflow import WorkflowController
        controller = WorkflowController(self.store)
        older = self.crafted_receipt({'criteria': [{'id': 'AC1', 'status': 'UNVERIFIED'}]}, role='ac')
        newer = self.crafted_receipt({'criteria': [{'id': 'AC1', 'status': 'PASS'}]}, role='ac')
        adequacy = self.crafted_receipt({'scenarios': [{'id': 'S1', 'status': 'PASS'}], 'missing_coverage': [],
            'additional_scenarios': [], 'summary': 'ok'})
        with patch('ase.coordinator.now', return_value='2026-11-24T10:00:00+00:00'):
            first = controller._save('verification', 'delegated:ac', older)
            controller._save('verification', 'delegated:adequacy:draft', adequacy)
        rows = {row['id']: row for row in self.service.pending('delegated')['events']}
        self.assertFalse(any('superseded_by' in row for row in rows.values()))
        with patch('ase.coordinator.now', return_value='2026-11-24T11:00:00+00:00'):
            controller._save('verification', 'delegated:ac', newer)
        events = {row['payload']['content']['source_id']: row['payload']['id']
                  for row in self.service.snapshot('delegated')['events']}
        rows = {row['id']: row for row in self.service.pending('delegated')['events']}
        older_id, newer_id = events['verification:' + older['execution_id']], events['verification:' + newer['execution_id']]
        self.assertEqual(rows[older_id]['superseded_by'], newer_id)
        self.assertEqual(rows[older_id]['captured_result']['criteria'], {'UNVERIFIED': 1})
        self.assertNotIn('superseded_by', rows[newer_id])
        adequacy_row = rows[events['verification:' + adequacy['execution_id']]]
        self.assertEqual(adequacy_row['captured_result']['role'], 'adequacy')
        self.assertEqual(adequacy_row['captured_result']['scenarios'], {'PASS': 1})
        self.assertNotIn('superseded_by', adequacy_row)
        self.assertEqual(first['execution_id'], older['execution_id'])
        # A request no longer matching its receipt hash leaves the scope undeterminable: no hint.
        request = Path(newer['artifact_root']) / 'request.json'
        request.write_bytes(request.read_bytes() + b' ')
        rows = {row['id']: row for row in self.service.pending('delegated')['events']}
        self.assertNotIn('superseded_by', rows[older_id])

    def test_deeply_nested_result_is_not_summarized(self):
        from ase.protocol import canonical, sha256
        receipt = self.crafted_receipt({'criteria': []}, role='ac')
        root = Path(receipt['artifact_root'])
        nested = b'[' * 200000 + b']' * 200000
        (root / 'result.json').write_bytes(nested)
        receipt['artifacts']['result'].update(sha256=sha256(nested), bytes=len(nested))
        (root / 'receipt.json').write_bytes(canonical(receipt))
        self.service.record_receipt('delegated', 'ac', receipt)
        [row] = self.service.pending('delegated')['events']
        self.assertNotIn('captured_result', row)

    def test_newer_stage_result_never_supersedes_an_older_whole_task_result(self):
        from unittest.mock import patch
        from ase.workflow import WorkflowController
        controller = WorkflowController(self.store)
        whole = self.crafted_receipt({'criteria': [{'id': 'AC1', 'status': 'FAIL'}]}, role='ac')
        stage = self.crafted_receipt({'criteria': [{'id': 'AC1', 'status': 'PASS'}]}, role='ac', stage_id='draft')
        with patch('ase.coordinator.now', return_value='2026-11-24T10:00:00+00:00'):
            controller._save('verification', 'delegated:ac', whole)
        with patch('ase.coordinator.now', return_value='2026-11-24T11:00:00+00:00'):
            controller._save('verification', 'delegated:ac', stage)
        rows = self.service.pending('delegated')['events']
        self.assertEqual(len(rows), 2)
        self.assertFalse(any('superseded_by' in row for row in rows))
        self.assertEqual({row['captured_result']['criteria'].get('FAIL') for row in rows}, {1, None})
        self.assertNotIn('whole-task', self.service.pending('delegated')['classification_note'])

    def test_older_recorded_binding_never_marks_a_newer_event_superseded(self):
        from unittest.mock import patch
        from ase.workflow import WorkflowController
        current = self.crafted_receipt({'criteria': []}, role='ac')
        orphan = self.crafted_receipt({'criteria': []}, role='ac')
        with patch('ase.coordinator.now', return_value='2026-11-24T10:00:00+00:00'):
            WorkflowController(self.store)._save('verification', 'delegated:ac', current)
        with patch('ase.coordinator.now', return_value='2026-11-24T11:00:00+00:00'):
            self.service.record_receipt('delegated', 'ac', orphan)
        self.assertFalse(any('superseded_by' in row for row in self.service.pending('delegated')['events']))

    def test_legacy_prerequisite_snapshot_keeps_existing_binding_shape(self):
        from ase.tasks import TaskService
        from ase.workflow import WorkflowController
        task = TaskService(self.store).snapshot('delegated')
        controller = WorkflowController(self.store)
        controller._save('stage', 'delegated:draft', {'state': 'COMPLETED',
            'contract_hash': task['contract_hash'], 'proposal_hash': task['proposal_hash']})
        observed = controller._prerequisite_current_inputs(task, 'draft')
        self.assertNotIn('test_adequacy_sha256', observed)

    def test_concurrent_claims_yield_one_processing_owner(self):
        from ase.coordinator import CoordinatorEvents
        self.bind()
        event = self.enqueue()
        ready = threading.Barrier(2)
        def claim():
            from ase.coordinator import CoordinatorEvents
            with closing(Store(self.store.project)) as other:
                ready.wait(timeout=10)
                try:
                    return CoordinatorEvents(other).claim('delegated', event['payload']['id'],
                        thread_id='original-main', expected_revision=event['revision'])['payload']['processing']['id']
                except ValueError:
                    return None
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: claim(), range(2)))
        self.assertEqual(sum(value is not None for value in results), 1)
        with closing(Store(self.store.project)) as reopened:
            durable = CoordinatorEvents(reopened).snapshot('delegated')['events'][0]
        self.assertEqual(durable['payload']['processing']['state'], 'CLAIMED')
        self.assertEqual(durable['payload']['processing']['id'], next(value for value in results if value))

    def test_unrelated_roots_and_child_results_keep_the_original_root_inbox(self):
        import copy
        from ase.tasks import TaskService
        tasks = TaskService(self.store)
        original = tasks.snapshot('delegated')['contract']
        tasks.create({'id': 'other-root', **copy.deepcopy(original)})
        tasks.create({'id': 'child-result', **copy.deepcopy(original),
            'parent_task_id': 'delegated', 'parent_stage_id': 'draft'})
        self.bind()
        self.service.bind('other-root', {**self.endpoint, 'thread_id': 'other-main'},
            user_message_ref='test:other-original-lead', expected_revision=0)
        root_event = self.enqueue()
        child_event = self.service.enqueue('child-result', source_kind='verification',
            source_id='child-attempt', status='COMPLETE', summary='Child result for original lead',
            evidence=[{'ref': 'child-result-proof', 'sha256': 'b' * 64}])
        other_event = self.service.enqueue('other-root', source_kind='delegation',
            source_id='attempt-one', status='COMPLETE', summary='Other root result',
            evidence=[{'ref': 'other-root-proof', 'sha256': 'c' * 64}])
        expected = {root_event['payload']['id'], child_event['payload']['id']}
        self.assertEqual({row['payload']['id'] for row in self.service.snapshot('delegated')['events']}, expected)
        child_inbox = self.service.snapshot('child-result')
        self.assertEqual(child_inbox['root_task_id'], 'delegated')
        self.assertEqual(child_inbox['coordinator']['destination'], self.endpoint)
        self.assertEqual({row['payload']['id'] for row in child_inbox['events']}, expected)
        self.assertEqual([row['payload']['id'] for row in self.service.snapshot('other-root')['events']],
            [other_event['payload']['id']])
        with self.assertRaisesRegex(ValueError, 'Missing event'):
            self.service.claim('other-root', child_event['payload']['id'],
                thread_id='other-main', expected_revision=child_event['revision'])
        with self.assertRaisesRegex(ValueError, 'original coordinator'):
            self.service.claim('child-result', child_event['payload']['id'],
                thread_id='other-main', expected_revision=child_event['revision'])
        claimed = self.service.claim('delegated', child_event['payload']['id'],
            thread_id='original-main', expected_revision=child_event['revision'])
        with closing(Store(self.store.project)) as reopened:
            from ase.coordinator import CoordinatorEvents
            restored = CoordinatorEvents(reopened).snapshot('child-result')
        saved = next(row for row in restored['events'] if row['payload']['id'] == child_event['payload']['id'])
        self.assertEqual(saved['payload']['processing'], claimed['payload']['processing'])

    def test_separate_projects_isolate_identical_task_and_source_ids(self):
        from ase.coordinator import CoordinatorEvents
        self.bind()
        first = self.enqueue()
        base = Path(self.temp.name) / 'second-project'
        base.mkdir()
        with closing(prepare(base)) as other:
            events = CoordinatorEvents(other)
            self.assertNotEqual(other.project_id, self.store.project_id)
            self.assertEqual(events.snapshot('delegated')['events'], [])
            with self.assertRaisesRegex(ValueError, 'Missing event'):
                events.claim('delegated', first['payload']['id'],
                    thread_id='original-main', expected_revision=first['revision'])
            events.bind('delegated', {**self.endpoint, 'thread_id': 'second-project-main'},
                user_message_ref='test:second-project-lead', expected_revision=0)
            own = events.enqueue('delegated', source_kind='delegation', source_id='attempt-one',
                status='COMPLETE', summary='Isolated second project result',
                evidence=[{'ref': 'second-project-proof', 'sha256': 'd' * 64}])
            self.assertEqual(own['payload']['id'], first['payload']['id'])
            self.assertEqual(own['payload']['content']['project_id'], other.project_id)
            with self.assertRaisesRegex(ValueError, 'original coordinator'):
                events.claim('delegated', own['payload']['id'],
                    thread_id='original-main', expected_revision=own['revision'])
            claimed = events.claim('delegated', own['payload']['id'],
                thread_id='second-project-main', expected_revision=own['revision'])
            events.ack('delegated', own['payload']['id'],
                processing_id=claimed['payload']['processing']['id'], thread_id='second-project-main',
                outcome='handled', evidence='checkpoint:second-project', expected_revision=claimed['revision'])
        original = self.service.snapshot('delegated')['events']
        self.assertEqual(len(original), 1)
        self.assertEqual(original[0]['payload']['content']['project_id'], self.store.project_id)
        self.assertEqual(original[0]['payload']['processing']['state'], 'PENDING')

    def test_wrong_chat_cannot_claim_or_acknowledge(self):
        self.bind()
        event = self.enqueue()
        with self.assertRaisesRegex(ValueError, 'coordinator|thread'):
            self.service.claim('delegated', event['payload']['id'], thread_id='worker', expected_revision=event['revision'])
        claimed = self.service.claim('delegated', event['payload']['id'],
            thread_id='original-main', expected_revision=event['revision'])
        with self.assertRaisesRegex(ValueError, 'coordinator|thread'):
            self.service.ack('delegated', event['payload']['id'],
                processing_id=claimed['payload']['processing']['id'], thread_id='worker',
                outcome='handled', evidence='private-checkpoint', expected_revision=claimed['revision'])

    def test_delivery_and_processing_are_separate_and_processing_is_not_restarted(self):
        self.bind()
        event = self.enqueue()
        ticket = self.service.dispatch('delegated', event['payload']['id'], expected_revision=event['revision'])
        delivered = self.service.observe('delegated', event['payload']['id'],
            attempt_id=ticket['payload']['delivery']['id'], expected_revision=ticket['revision'],
            observation={'state': 'DELIVERED', 'destination': self.endpoint, 'reference': 'host:message-123'})
        self.assertEqual(delivered['payload']['processing']['state'], 'PENDING')
        claimed = self.service.claim('delegated', event['payload']['id'],
            thread_id='original-main', expected_revision=delivered['revision'])
        with self.assertRaisesRegex(ValueError, 'claimed|processing'):
            self.service.claim('delegated', event['payload']['id'],
                thread_id='original-main', expected_revision=claimed['revision'])
        done = self.service.ack('delegated', event['payload']['id'], thread_id='original-main',
            processing_id=claimed['payload']['processing']['id'], outcome='handled',
            evidence='checkpoint:result-integrated', expected_revision=claimed['revision'])
        self.assertEqual(done['payload']['processing']['state'], 'DONE')
        self.assertEqual(self.service.snapshot('delegated')['pending_count'], 0)

    def test_dispatch_survives_crash_and_unknown_cannot_be_resent(self):
        self.bind()
        event = self.enqueue()
        ticket = self.service.dispatch('delegated', event['payload']['id'], expected_revision=event['revision'])
        with closing(Store(self.store.project)) as reopened:
            from ase.coordinator import CoordinatorEvents
            service = CoordinatorEvents(reopened)
            unknown = service.observe('delegated', event['payload']['id'],
                attempt_id=ticket['payload']['delivery']['id'], expected_revision=ticket['revision'],
                observation={'state': 'UNKNOWN', 'destination': self.endpoint, 'reference': 'crash:missing-host-response'})
            with self.assertRaisesRegex(ValueError, 'reconcile|already|UNKNOWN'):
                service.dispatch('delegated', event['payload']['id'], expected_revision=unknown['revision'])
            observed = service.observe('delegated', event['payload']['id'],
                attempt_id=ticket['payload']['delivery']['id'], expected_revision=unknown['revision'],
                observation={'state': 'DELIVERED', 'destination': self.endpoint, 'reference': 'history:exact-event-marker'})
            self.assertEqual(observed['payload']['delivery']['state'], 'DELIVERED')

    def test_foreign_destination_cannot_resolve_unknown_delivery(self):
        self.bind()
        event = self.enqueue()
        ticket = self.service.dispatch('delegated', event['payload']['id'], expected_revision=event['revision'])
        with self.assertRaisesRegex(ValueError, 'destination'):
            self.service.observe('delegated', event['payload']['id'],
                attempt_id=ticket['payload']['delivery']['id'], expected_revision=ticket['revision'],
                observation={'state': 'DELIVERED', 'destination': {**self.endpoint, 'thread_id': 'other'},
                    'reference': 'host:unrelated'})

    def test_queue_writes_roll_back_with_producer_transaction(self):
        with self.assertRaisesRegex(RuntimeError, 'producer failed'):
            with transaction(self.store.db):
                self.enqueue()
                raise RuntimeError('producer failed')
        self.assertEqual(self.service.snapshot('delegated')['events'], [])

    def test_queue_processing_does_not_grant_task_approval(self):
        self.bind()
        before = read_record(self.store.db, 'approval', 'delegated')
        event = self.enqueue()
        claimed = self.service.claim('delegated', event['payload']['id'],
            thread_id='original-main', expected_revision=event['revision'])
        self.service.ack('delegated', event['payload']['id'], thread_id='original-main',
            processing_id=claimed['payload']['processing']['id'], outcome='handled',
            evidence='checkpoint:evaluated', expected_revision=claimed['revision'])
        self.assertEqual(read_record(self.store.db, 'approval', 'delegated'), before)

    def test_dispatch_and_processing_cannot_escape_an_uncommitted_outer_transaction(self):
        self.bind()
        event = self.enqueue()
        with transaction(self.store.db):
            with self.assertRaisesRegex(ValueError, 'commit|transaction'):
                self.service.dispatch('delegated', event['payload']['id'], expected_revision=event['revision'])
            with self.assertRaisesRegex(ValueError, 'commit|transaction'):
                self.service.claim('delegated', event['payload']['id'],
                    thread_id='original-main', expected_revision=event['revision'])

    def test_delegation_result_and_recovery_emit_one_durable_event(self):
        service = registry(self.store)
        row = service.register('delegated', spec())
        ticket = service.dispatch('delegated', 'research', expected_revision=row['revision'],
            coordinator='original-main', backend='codex')
        path = Path(ticket['handoff']['artifact_dir']) / 'finding.txt'
        path.write_text('Actual bounded finding', encoding='utf-8')
        payload = {'attempt_id': ticket['handoff']['attempt_id'], 'agent_id': 'real-worker',
            'status': 'COMPLETE', 'summary': 'Finished investigation',
            'artifacts': [{'path': 'finding.txt', 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}]}
        result = service.result('delegated', 'research', payload['attempt_id'], payload,
            expected_revision=ticket['revision'])
        service.recover('delegated')
        events = self.service.snapshot('delegated')['events']
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]['payload']['content']['source_id'], 'delegation:' + payload['attempt_id'])
        self.assertEqual(events[0]['payload']['content']['evidence'][0]['sha256'], result['payload']['result']['sha256'])

    def test_crash_after_result_seal_recovers_result_and_notification_without_worker(self):
        from unittest.mock import patch
        service = registry(self.store)
        row = service.register('delegated', spec())
        ticket = service.dispatch('delegated', 'research', expected_revision=row['revision'],
            coordinator='original-main', backend='codex')
        path = Path(ticket['handoff']['artifact_dir']) / 'finding.txt'
        path.write_text('Result survives failed SQL publication', encoding='utf-8')
        payload = {'attempt_id': ticket['handoff']['attempt_id'], 'agent_id': 'existing-worker',
            'status': 'COMPLETE', 'summary': 'Sealed before simulated crash',
            'artifacts': [{'path': 'finding.txt', 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}]}
        with patch.object(self.service.__class__, 'enqueue', side_effect=RuntimeError('crash before queue commit')):
            with self.assertRaisesRegex(RuntimeError, 'crash before queue'):
                service.result('delegated', 'research', payload['attempt_id'], payload,
                    expected_revision=ticket['revision'])
        self.assertEqual(service.get('delegated', 'research')['payload']['state'], 'DISPATCHING')
        service.recover('delegated')
        self.assertEqual(service.get('delegated', 'research')['payload']['state'], 'RESULT_READY')
        self.assertEqual(len(self.service.snapshot('delegated')['events']), 1)

    def test_tied_creation_times_keep_distinct_results_in_stable_order(self):
        from unittest.mock import patch
        with patch('ase.coordinator.now', return_value='2026-11-16T12:00:00+00:00'):
            self.enqueue('second')
            self.enqueue('first')
        first = self.service.snapshot('delegated')['events']
        with closing(Store(self.store.project)) as reopened:
            from ase.coordinator import CoordinatorEvents
            second = CoordinatorEvents(reopened).snapshot('delegated')['events']
        self.assertEqual(len(first), 2)
        self.assertEqual([e['payload']['id'] for e in first], sorted(e['payload']['id'] for e in first))
        self.assertEqual(first, second)

    def test_cli_binds_original_chat_lists_event_and_claims_without_host_transport(self):
        config = Path(self.temp.name) / 'endpoint.json'
        config.write_text(json.dumps(self.endpoint), encoding='utf-8')
        base = [sys.executable, '-B', '-m', 'ase', '--repo', str(self.store.repo), '--home', str(self.store.state_home), 'events']
        bound = subprocess.run([*base, 'bind', 'delegated', '--file', str(config),
            '--user-message-ref', 'test:original-chat', '--expected-revision', '0'], capture_output=True, text=True)
        self.assertEqual(bound.returncode, 0, bound.stdout + bound.stderr)
        event = self.enqueue()
        listed = subprocess.run([*base, 'list', 'delegated'], capture_output=True, text=True)
        self.assertEqual(listed.returncode, 0, listed.stdout + listed.stderr)
        self.assertEqual(json.loads(listed.stdout)['pending_count'], 1)
        claimed = subprocess.run([*base, 'claim', 'delegated', event['payload']['id'],
            '--thread-id', 'original-main', '--expected-revision', str(event['revision'])], capture_output=True, text=True)
        self.assertEqual(claimed.returncode, 0, claimed.stdout + claimed.stderr)
        self.assertEqual(json.loads(claimed.stdout)['payload']['processing']['state'], 'CLAIMED')

    def test_cli_local_owner_needs_no_provider_chat_id(self):
        base = [sys.executable, '-B', '-m', 'ase', '--repo', str(self.store.repo),
            '--home', str(self.store.state_home), 'events']
        result = subprocess.run([*base, 'bind-local', 'delegated', '--backend', 'claude',
            '--user-message-ref', 'test:original-local-chat', '--expected-revision', '0'],
            capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout)['payload']['destination']['transport'], 'local')
        event = self.enqueue()
        result = subprocess.run([*base, 'dispatch', 'delegated', event['payload']['id'],
            '--expected-revision', str(event['revision'])], capture_output=True, text=True)
        self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
        self.assertIn('local inbox', json.loads(result.stdout)['error'])

    def test_process_death_after_committed_dispatch_preserves_original_attempt(self):
        self.bind()
        event = self.enqueue()
        script = '''
from contextlib import closing
from pathlib import Path
import os,sys
from ase.store import Store
from ase.coordinator import CoordinatorEvents
with closing(Store(Path(sys.argv[1]),Path(sys.argv[2]))) as s:
 CoordinatorEvents(s).dispatch('delegated',sys.argv[3],expected_revision=int(sys.argv[4]))
 os._exit(17)
'''
        died = subprocess.run([sys.executable, '-B', '-c', script, str(self.store.repo),
            str(self.store.state_home), event['payload']['id'], str(event['revision'])],
            capture_output=True, text=True)
        self.assertEqual(died.returncode, 17, died.stdout + died.stderr)
        restored = self.service.snapshot('delegated')['events'][0]
        self.assertEqual(restored['payload']['delivery']['state'], 'DISPATCHING')
        with self.assertRaisesRegex(ValueError, 'reconcile'):
            self.service.dispatch('delegated', event['payload']['id'], expected_revision=restored['revision'])

    def test_orphan_captured_verifier_result_recovers_as_evidence_without_relaunch(self):
        from ase.adapters.codex import CodexAdapter
        from ase.runner import Runner
        from ase.workflow import WorkflowController
        from test_runner import TransportFixture, SCHEMA
        fixture = TransportFixture()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        request = WorkflowController(self.store)._request('delegated', 'review', SCHEMA, {'stage_id': 'draft'})
        outcome = Runner(fixture.config(), self.store.root / 'tasks' / 'delegated' / 'sessions').run(
            CodexAdapter(), request, timeout=3)
        self.assertEqual(outcome['result']['status'], 'COMPLETE')
        self.assertIsNone(read_record(self.store.db, 'verification', 'delegated:review')['payload'])
        recovered = self.service.recover('delegated')
        self.assertEqual(len(recovered['events']), 1)
        self.assertEqual(recovered['events'][0]['payload']['content']['source_id'],
            'verification:' + outcome['receipt']['execution_id'])
        self.assertEqual(self.service.recover('delegated')['events'], recovered['events'])
        self.assertIsNone(read_record(self.store.db, 'verification', 'delegated:review')['payload'])

    def test_orphan_failed_unavailable_and_cancelled_verifier_attempts_are_not_lost(self):
        from ase.adapters.codex import CodexAdapter
        from ase.runner import Runner, verify_receipt
        from ase.workflow import WorkflowController
        from test_runner import TransportFixture, SCHEMA
        fixture = TransportFixture()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        for case in ('exit', 'unavailable', 'cancelled'):
            request = WorkflowController(self.store)._request('delegated', 'review', SCHEMA, {'stage_id': 'draft'})
            config = fixture.config(case='exit' if case == 'exit' else 'success')
            if case == 'unavailable':
                config['executable_args'] = ['-c', "print('unsupported CLI')"]
            outcome = Runner(config, self.store.root / 'tasks' / 'delegated' / 'sessions').run(
                CodexAdapter(), request, timeout=3, cancel_requested=lambda: case == 'cancelled')
            with self.subTest(case=case):
                self.assertEqual(outcome['result']['status'], 'BLOCKED', outcome)
                with self.assertRaises(ValueError):
                    verify_receipt(outcome['receipt'])
                recovered = self.service.recover('delegated')
                matches = [e for e in recovered['events'] if e['payload']['content']['source_id'] ==
                    'verification:' + outcome['receipt']['execution_id']]
                self.assertEqual(len(matches), 1, recovered)
                self.assertEqual(matches[0]['payload']['content']['status'], 'BLOCKED')
        recovered = self.service.recover('delegated')
        self.assertEqual(len(recovered['events']), 3)
        self.assertEqual(self.service.recover('delegated')['events'], recovered['events'])
        self.assertIsNone(read_record(self.store.db, 'verification', 'delegated:review')['payload'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
