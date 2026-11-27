"""`ase task finish`: one pre-push answer from gate, clean tree and ci-check."""
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ase.finish import finish


@unittest.skipUnless(shutil.which('git'), 'git required')
class FinishTests(unittest.TestCase):
    def setUp(self):
        self.repo = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.repo, ignore_errors=True)
        # The full evidence body is for the framework's own repository; other repositories are covered by
        # tests/test_private_install.py.
        for args in (('init', '-q', '-b', 'main'), ('config', 'user.email', 'ci@example.invalid'), ('config', 'user.name', 'ci'),
                     ('remote', 'add', 'origin', 'https://github.com/artem0513/DevelopmentFramework.git')):
            self.git(*args)
        self.write('app.py', 'X = 1\n')
        self.write('tests/test_app.py', 'def test_x():\n    assert True\n')
        self.git('add', '-A'); self.git('commit', '-q', '-m', 'base')
        self.git('checkout', '-q', '-b', 'feature')
        self.write('app.py', 'X = 2\n')
        self.git('add', '-A'); self.git('commit', '-q', '-m', 'change')
        self.store = type('Store', (), {})()
        self.store.repo = self.repo
        self.store.project_id = 'p'
        self.store.task = lambda task_id: {'contract': {}}
        self.store.db = sqlite3.connect(':memory:')
        self.store.db.execute('CREATE TABLE runtime_records(kind TEXT, key TEXT, revision INT, payload TEXT, updated TEXT, '
                              'PRIMARY KEY(kind, key))')
        self.task = {'task_id': 't', 'contract': {'id': 't', 'request': 'Change X'}, 'proposal': {'summary': 'Set X to 2'},
                     'contract_hash': 'contract', 'proposal_hash': 'proposal'}
        snapshot = patch('ase.tasks.TaskService.snapshot', side_effect=lambda task_id: self.task)
        snapshot.start(); self.addCleanup(snapshot.stop)

    def git(self, *args):
        subprocess.run(['git', '-C', str(self.repo), *args], check=True, capture_output=True)

    def write(self, rel, text):
        path = self.repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8', newline='\n')

    def run_finish(self, gate_status):
        gate = {'status': gate_status, 'reasons': [] if gate_status == 'APPROVED' else ['Independent ac session missing']}
        with patch('ase.finish.GateService.gate', return_value=gate):
            return finish(self.store, 't', 'main')

    def test_approved_clean_tree_with_passing_ci_check_is_ready(self):
        result = self.run_finish('APPROVED')
        self.assertEqual(result['status'], 'READY', result['reasons'])
        self.assertEqual(result['ci_check']['status'], 'PASS')
        self.assertEqual([f['level'] for f in result['ci_check']['findings'] if f['check'] == 'no_test_changes'], ['INFO'])
        self.assertIn('## t: gate APPROVED', result['pr_body'])
        self.assertEqual(len(result['head']), 40)
        self.assertEqual(result['pending_events'], [])
        self.assertIsNone(result['pending_events_hint'])

    def test_unprocessed_inbox_events_are_reported_without_changing_ready(self):
        from ase.coordinator import CoordinatorEvents
        inbox = CoordinatorEvents(self.store)
        owner = inbox.bind_local('t', backend='copilot', user_message_ref='test:lead', expected_revision=0)
        thread = owner['payload']['destination']['thread_id']
        for role in ('ac', 'review'):
            inbox.record_receipt('t', role, {'execution_id': role + '-1', 'status': 'APPROVED',
                                             'artifact_root': str(self.repo / role)})
        first, second = [row['payload']['id'] for row in inbox.snapshot('t')['events']]
        claimed = inbox.claim('t', first, thread_id=thread, expected_revision=1)
        result = self.run_finish('APPROVED')
        self.assertEqual(result['status'], 'READY', result['reasons'])
        by_id = {row['id']: row for row in result['pending_events']}
        self.assertEqual(set(by_id), {first, second})
        self.assertEqual(by_id[second]['next_command'],
                         f'ase events claim t {second} --thread-id {thread} --expected-revision 1')
        self.assertEqual(by_id[first]['processing'], 'CLAIMED')
        self.assertIn(f"ase events ack t {first} --processing-id {claimed['payload']['processing']['id']} "
                      f'--thread-id {thread} ', by_id[first]['next_command'])
        self.assertIn('--expected-revision 2', by_id[first]['next_command'])
        self.assertIn('2 coordinator event(s)', result['pending_events_hint'])
        self.assertIn('does not change READY', result['pending_events_hint'])
        self.assertNotIn('bind-local', result['pending_events_hint'])
        self.assertNotIn(first, result['pr_body'])
        inbox.ack('t', first, processing_id=claimed['payload']['processing']['id'], thread_id=thread,
                  outcome='handled', evidence='checkpoint', expected_revision=2)
        self.assertEqual([row['id'] for row in self.run_finish('APPROVED')['pending_events']], [second])

    def test_unbound_inbox_hint_says_to_bind_the_original_lead_first(self):
        from ase.coordinator import CoordinatorEvents
        CoordinatorEvents(self.store).record_receipt('t', 'adequacy', {'execution_id': 'a-1', 'status': 'APPROVED',
                                                                        'artifact_root': str(self.repo / 'a')})
        result = self.run_finish('APPROVED')
        self.assertEqual(result['status'], 'READY', result['reasons'])
        self.assertEqual(result['pending_events'][0]['source_kind'], 'adequacy')
        self.assertIn('--thread-id <thread_id from bind-local>', result['pending_events'][0]['next_command'])
        self.assertTrue(result['pending_events_hint'].startswith('Bind this original lead chat as the task coordinator'))
        self.assertIn('`ase events bind-local t --backend', result['pending_events_hint'])
        self.assertIn('--expected-revision 0`', result['pending_events_hint'])

    def test_quick_lane_task_is_held_to_the_small_diff_budget(self):
        self.write('big.py', ''.join(f'v{i} = {i}\n' for i in range(400)))
        self.git('add', '-A'); self.git('commit', '-q', '-m', 'grow')
        quick = {'task_id': 't', 'contract': {'id': 't', 'request': 'r', 'lane': 'quick'}, 'proposal': {'summary': 's'}}
        with patch('ase.tasks.TaskService.snapshot', side_effect=lambda task_id: quick):
            result = self.run_finish('APPROVED')
        self.assertEqual(result['status'], 'NOT_READY')
        self.assertTrue(any('diff_budget' in r for r in result['reasons']))
        gate = {'status': 'APPROVED', 'reasons': []}
        with patch('ase.tasks.TaskService.snapshot', side_effect=lambda task_id: quick), \
                patch('ase.finish.GateService.gate', return_value=gate):
            waived = finish(self.store, 't', 'main', allow_large=True, override_ref='chat:9', override_quote='Размер ок')
        self.assertEqual(waived['status'], 'NOT_READY')
        self.assertTrue(any('Quick lane exceeded' in r for r in waived['reasons']))
        self.assertIn('no separate test-adequacy session', waived['pr_body'])
        self.assertEqual(self.run_finish('APPROVED')['status'], 'READY')

    def test_quick_lane_needs_a_changed_test_file(self):
        quick = {'task_id': 't', 'contract': {'id': 't', 'request': 'r', 'lane': 'quick'}, 'proposal': {'summary': 's'}}
        with patch('ase.tasks.TaskService.snapshot', side_effect=lambda task_id: quick):
            result = self.run_finish('APPROVED')
            self.assertEqual(result['status'], 'NOT_READY')
            self.assertTrue(any('without adding or changing a test file' in r for r in result['reasons']), result['reasons'])
            self.assertEqual(result['ci_check']['status'], 'FAIL')
            self.assertEqual([f['level'] for f in result['ci_check']['findings'] if f['check'] == 'no_test_changes'], ['FAIL'])
            self.assertIn(': FAIL', result['pr_body'])
            self.write('tests/test_app.py', 'def test_x():\n    assert 2 == 2\n')
            self.git('add', '-A'); self.git('commit', '-q', '-m', 'test')
            self.assertEqual(self.run_finish('APPROVED')['status'], 'READY')

    def test_owner_overrides_are_reported_in_result_and_pr_body(self):
        gate = {'status': 'APPROVED', 'reasons': []}
        with patch('ase.finish.GateService.gate', return_value=gate):
            result = finish(self.store, 't', 'main', allow_large=True, override_ref='chat:9', override_quote='Размер ок')
        self.assertEqual(result['overrides'], ['allow-large'])
        self.assertIn('PASS with owner overrides: allow-large (chat:9, owner-asserted, not verified: "Размер ок")', result['pr_body'])
        self.assertEqual(result['status'], 'REVIEW')

    def test_overrides_need_an_owner_reference_validated_like_dispositions(self):
        gate = {'status': 'APPROVED', 'reasons': []}
        with patch('ase.finish.GateService.gate', return_value=gate):
            for kwargs in ({}, {'override_ref': 'chat:9'}, {'override_ref': 'chat:9', 'override_quote': '  '}):
                with self.subTest(kwargs=kwargs), self.assertRaisesRegex(ValueError, 'override'):
                    finish(self.store, 't', 'main', allow_protected=True, **kwargs)
            from ase import prompts
            from ase.state import write_record
            write_record(self.store.db, 'verification', 't:review', {'ended_at_utc': '2026-11-21T10:00:00+00:00'}, expected_revision=0)
            with patch('ase.prompts.now', return_value='2026-11-21T09:00:00+00:00'):
                early = prompts.capture(self.store, {'prompt': 'ok', 'session_id': 's'})['id']
            with self.assertRaisesRegex(ValueError, 'predates the review'):
                finish(self.store, 't', 'main', allow_large=True, override_ref=early)
            with patch('ase.prompts.now', return_value='2026-11-21T11:00:00+00:00'):
                later = prompts.capture(self.store, {'prompt': 'large is fine', 'session_id': 's'})['id']
            result = finish(self.store, 't', 'main', allow_large=True, override_ref=later)
        self.assertEqual(result['override_owner'], {'user_message_ref': later, 'verified': True, 'owner_words': 'large is fine'})
        self.assertEqual(result['status'], 'READY')
        self.assertIn('(' + later + ', captured owner prompt: "large is fine")', result['pr_body'])

    def test_private_body_lists_exceptions_but_keeps_owner_review_for_the_owner(self):
        disposed = [{'severity': 'P1', 'disposition': 'disprove', 'user_message_ref': 'chat:7', 'verified': False,
                     'message': 'Null dereference'}]
        gate = {'status': 'APPROVED', 'reasons': [], 'disposed_findings': disposed}
        with patch('ase.finish.private_repository', return_value=True), \
                patch('ase.finish.GateService.gate', return_value=gate):
            result = finish(self.store, 't', 'main', allow_large=True, override_ref='chat:9', override_quote='Размер ок')
        self.assertEqual(result['status'], 'REVIEW', result['reasons'])
        self.assertIn('- allow-large: approved by the owner', result['pr_body'])
        for internal in ('chat:7', 'chat:9', 'Null dereference', 'Needs owner review', 'not verified'):
            self.assertNotIn(internal, result['pr_body'])
        self.assertTrue(any('chat:7' in item for item in result['needs_owner_review']))

    def test_agent_body_is_not_extended_twice_on_rerun(self):
        body = self.repo.parent / (self.repo.name + '-body.md')
        self.addCleanup(lambda: body.unlink(missing_ok=True))
        body.write_text('My change.\n', encoding='utf-8')
        gate = {'status': 'APPROVED', 'reasons': []}
        with patch('ase.finish.private_repository', return_value=True), \
                patch('ase.finish.GateService.gate', return_value=gate):
            first = finish(self.store, 't', 'main', allow_large=True, override_ref='chat:9', override_quote='ok',
                           body_file=str(body))['pr_body']
            body.write_text(first, encoding='utf-8')
            second = finish(self.store, 't', 'main', allow_large=True, override_ref='chat:9', override_quote='ok',
                            body_file=str(body))['pr_body']
        self.assertEqual(second.count('## Owner exceptions'), 1)

    def test_remote_tracking_base_is_accepted_and_option_like_base_refused(self):
        self.git('update-ref', 'refs/remotes/origin/main', 'main')
        self.assertEqual(self.run_finish_base('origin/main')['status'], 'READY')
        with self.assertRaisesRegex(ValueError, 'must be a branch name'):
            finish(self.store, 't', '--all')

    def run_finish_base(self, base):
        with patch('ase.finish.GateService.gate', return_value={'status': 'APPROVED', 'reasons': []}):
            return finish(self.store, 't', base)

    def test_owner_asserted_p0_p1_disproof_is_review_not_ready(self):
        row = {'severity': 'P1', 'disposition': 'disprove', 'user_message_ref': 'chat:7', 'verified': False, 'message': 'Null deref'}
        for verified, status in ((False, 'REVIEW'), (True, 'READY')):
            gate = {'status': 'APPROVED', 'reasons': [], 'disposed_findings': [{**row, 'verified': verified}]}
            with self.subTest(verified=verified), patch('ase.finish.GateService.gate', return_value=gate):
                result = finish(self.store, 't', 'main')
                self.assertEqual(result['status'], status)
                self.assertEqual(bool(result['needs_owner_review']), not verified)
                self.assertEqual('**Needs owner review**' in result['pr_body'], not verified)

    def test_asserted_plan_approval_is_surfaced(self):
        from ase.state import write_record
        binding = {key: self.task[key] for key in ('contract_hash', 'proposal_hash')}
        first = {**binding, 'approved_stage_ids': ['draft'], 'user_message_ref': 'test:ok',
                 'provenance': 'owner_asserted', 'owner_preapproved': True}
        write_record(self.store.db, 'approval_version', 't:1', first, expected_revision=0)
        write_record(self.store.db, 'approval', 't', first, expected_revision=0)
        write_record(self.store.db, 'approval', 't', {**binding, 'approved_stage_ids': ['draft', 'publish'],
            'earlier_stage_grants': {'draft': 1}, 'user_message_ref': 'prompt:publish', 'provenance': 'captured_prompt'},
            expected_revision=1)
        result = self.run_finish('APPROVED')
        self.assertEqual(result['approval_provenance'], 'owner_asserted')
        self.assertIn('Plan approval by owner (test:ok, owner-asserted, not verified)', result['pr_body'])
        self.assertIn('test:ok', result['owner_review'])

    def test_base_must_leave_a_nonempty_change_against_its_merge_base(self):
        for base, message in (('HEAD', 'already contains HEAD'), ('feature', 'already contains HEAD'),
                              ('no-such-ref', 'does not resolve'), ('HEAD~1', 'is not a branch')):
            with self.subTest(base=base), self.assertRaisesRegex(ValueError, message):
                finish(self.store, 't', base)
        self.git('checkout', '-q', '--orphan', 'unrelated'); self.git('rm', '-rqf', '.')
        self.write('other.txt', 'x\n'); self.git('add', '-A'); self.git('commit', '-q', '-m', 'unrelated')
        self.git('checkout', '-q', 'feature')
        with self.assertRaisesRegex(ValueError, 'shares no history'):
            finish(self.store, 't', 'unrelated')

    def test_uncommitted_change_on_a_base_head_says_to_commit_it(self):
        self.git('update-ref', 'refs/remotes/origin/main', 'main')
        self.git('checkout', '-q', '-b', 'fix', 'origin/main')
        with self.assertRaises(ValueError) as clean:
            finish(self.store, 't', 'origin/main')
        self.assertIn('change against it is empty', str(clean.exception))
        self.assertNotIn('e.g. origin/main', str(clean.exception))
        self.write('app.py', 'X = 5\n')
        with self.assertRaises(ValueError) as dirty:
            finish(self.store, 't', 'origin/main')
        self.assertIn('not committed yet (app.py); commit it on the feature branch', str(dirty.exception))
        self.assertNotIn('pass the branch', str(dirty.exception))

    def test_diverged_base_is_measured_from_the_merge_base(self):
        fork = subprocess.run(['git', '-C', str(self.repo), 'rev-parse', 'main'], check=True, capture_output=True).stdout.decode().strip()
        self.git('checkout', '-q', 'main')
        self.write('later.py', ''.join(f'v{i} = {i}\n' for i in range(3000)))
        self.git('add', '-A'); self.git('commit', '-q', '-m', 'main moved on')
        self.git('checkout', '-q', 'feature')
        result = self.run_finish('APPROVED')
        self.assertEqual((result['status'], result['merge_base']), ('READY', fork), result['reasons'])
        self.assertIn('(merge-base ' + fork[:12] + ')', result['pr_body'])

    def test_a_commit_during_the_gate_is_not_reported_ready(self):
        def gate_and_commit(_self, task_id, stage_id=None):
            self.write('app.py', 'X = 9\n')
            self.git('commit', '-qam', 'racing commit')
            return {'status': 'APPROVED', 'reasons': []}
        with patch('ase.finish.GateService.gate', gate_and_commit):
            result = finish(self.store, 't', 'main')
        self.assertEqual(result['status'], 'NOT_READY')
        self.assertTrue(any('changed while the gate ran' in r for r in result['reasons']))

    def test_uncommitted_changes_mean_the_gate_did_not_judge_head(self):
        self.write('app.py', 'X = 3\n')
        result = self.run_finish('APPROVED')
        self.assertEqual(result['status'], 'NOT_READY')
        self.assertEqual(result['dirty'], ['app.py'])

    def test_untracked_bytecode_cache_is_not_uncommitted_work(self):
        cache = self.repo / 'tests' / '__pycache__' / 'test_app.cpython-311.pyc'
        cache.parent.mkdir()
        cache.write_bytes(b'\x00bytecode')
        self.assertEqual(self.run_finish('APPROVED')['status'], 'READY')
        self.git('mv', 'app.py', 'renamed.py')
        self.write('tests/__pycache__/notes.txt', 'x\n')
        result = self.run_finish('APPROVED')
        self.assertEqual(result['status'], 'NOT_READY')
        self.assertEqual(sorted(result['dirty']), ['renamed.py', 'tests/__pycache__/notes.txt'])

    def test_blocked_gate_and_ci_failures_are_reported_together(self):
        self.write('tests/test_app.py', 'import pytest\n@pytest.mark.skip\ndef test_x():\n    assert True\n')
        self.git('add', '-A'); self.git('commit', '-q', '-m', 'skip')
        result = self.run_finish('BLOCKED')
        self.assertEqual(result['status'], 'BLOCKED')
        self.assertTrue(any(r.startswith('Gate is BLOCKED') for r in result['reasons']))
        self.assertTrue(any('test_skip_added' in r for r in result['reasons']))


if __name__ == '__main__':
    unittest.main()
