"""Completed-state fixtures isolate fresh auth facts, never semantic approval."""
import unittest
import os
from pathlib import Path

import test_credential_integration as fixtures
from ase.gates import GateService
from ase.state import transaction
from ase.state import hash_value, read_record, write_record


@unittest.skipUnless(os.name == 'nt', 'requires actual Windows DPAPI')
class CompletedAccessRevalidationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.CredentialIntegrationTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def completed_fixture(self, task):
        service = self.fixture.tasks
        current = service.snapshot(task['task_id'])
        payload = {k: v for k, v in current.items() if k not in {'revision', 'legacy_v1'}}
        payload['state'] = 'COMPLETED'
        with transaction(self.fixture.store.db):
            service._save(payload, current['revision'])

    def test_completed_check_rotation_requires_fresh_check_without_reopening(self):
        f = self.fixture
        task = f.create(f.contract('completed-check'))
        f.controller.check(task['task_id'])
        self.completed_fixture(task)
        f.vault.update_secrets('lab', f.target, {'token':'new-completed-fixture-token'}, 1)
        self.assertIn('Credential evidence is missing or stale: observe', GateService(f.store).gate(task['task_id'])['reasons'])
        f.controller.check(task['task_id'])
        self.assertNotIn('Credential evidence is missing or stale: observe', GateService(f.store).gate(task['task_id'])['reasons'])
        self.assertEqual(f.tasks.snapshot(task['task_id'])['state'], 'COMPLETED')

    def test_completed_check_refreshes_action_observation_without_replaying_effect(self):
        f = self.fixture
        task = f.create(f.contract('completed-action', action=True))
        f.controller.execute_actions(task['task_id'], task['contract']['stages'][0])
        self.completed_fixture(task)
        f.vault.update_secrets('lab', f.target, {'token':'new-completed-fixture-token'}, 1)
        f.controller.check(task['task_id'])
        reasons = GateService(f.store).gate(task['task_id'])['reasons']
        self.assertFalse(any('Credential evidence' in reason and 'create-counter' in reason for reason in reasons), reasons)
        self.assertEqual((f.repo / 'resource.counter').read_text(), '1')
        self.assertEqual(f.tasks.snapshot(task['task_id'])['state'], 'COMPLETED')

    def test_completed_legacy_applied_action_refreshes_after_rotation_without_replay(self):
        f = self.fixture
        task = f.create(f.contract('legacy-action', action=True))
        f.controller.execute_actions(task['task_id'], task['contract']['stages'][0])
        action_id = hash_value({'task':task['task_id'], 'stage':'work', 'action':'create-counter'})
        record = read_record(f.store.db, 'operation', action_id)
        operation = dict(record['payload'])
        operation.pop('effect_scope', None)
        operation['intent'] = dict(operation['intent'])
        operation['intent'].pop('effect_scope', None)
        operation['intent_hash'] = hash_value(operation['intent'])
        with transaction(f.store.db) as db:
            write_record(db, 'operation', action_id, operation, expected_revision=record['revision'])
        self.completed_fixture(task)
        f.vault.update_secrets('lab', f.target, {'token':'new-legacy-fixture-token'}, 1)
        f.controller.check(task['task_id'])
        reasons = GateService(f.store).gate(task['task_id'])['reasons']
        self.assertFalse(any('Credential evidence' in reason and 'create-counter' in reason for reason in reasons), reasons)
        stored = read_record(f.store.db, 'operation', action_id)['payload']
        self.assertNotEqual(stored['observation']['proof_ref'], operation['observation']['proof_ref'])
        self.assertNotIn('effect_scope', stored)
        self.assertNotIn('effect_scope', stored['intent'])
        self.assertEqual((f.repo / 'resource.counter').read_text(), '1')
        self.assertEqual(f.tasks.snapshot(task['task_id'])['state'], 'COMPLETED')

    def test_declared_action_without_observation_cannot_be_approved(self):
        f = self.fixture
        task = f.create(f.contract('missing-action', action=True))
        reasons = GateService(f.store).gate(task['task_id'])['reasons']
        self.assertIn('Action evidence is missing or stale: create-counter', reasons)

    def test_completed_refresh_does_not_accept_a_missing_resource(self):
        f = self.fixture
        task = f.create(f.contract('missing-resource', action=True))
        f.controller.execute_actions(task['task_id'], task['contract']['stages'][0])
        self.completed_fixture(task)
        (f.repo / 'resource.counter').unlink()
        f.controller.check(task['task_id'])
        self.assertIn('Action postcondition is not established: create-counter', GateService(f.store).gate(task['task_id'])['reasons'])
        self.assertFalse((f.repo / 'resource.counter').exists())

    def test_authenticated_action_revalidates_actual_observation_log(self):
        f = self.fixture
        task = f.create(f.contract('tampered-observation', action=True))
        f.controller.execute_actions(task['task_id'], task['contract']['stages'][0])
        action_id = hash_value({'task':task['task_id'], 'stage':'work', 'action':'create-counter'})
        operation = read_record(f.store.db, 'operation', action_id)['payload']
        path = Path(operation['observation']['proof_ref'].rsplit('#sha256=', 1)[0])
        path.write_text('altered proof')
        reasons = GateService(f.store).gate(task['task_id'])['reasons']
        self.assertIn('Action observation proof is missing or invalid: create-counter', reasons)



@unittest.skipUnless(os.name == 'nt', 'requires actual Windows DPAPI')
class EffectNonlaunchRecoveryTests(unittest.TestCase):
    """Actual host launches and disposable credentials; never semantic approval."""
    def setUp(self):
        self.f = fixtures.CredentialIntegrationTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)

    def operation(self, task):
        key = hash_value({'task': task['task_id'], 'stage': 'work', 'action': 'create-counter'})
        return read_record(self.f.store.db, 'operation', key)['payload']

    def assert_stop_before_launch_resumes_once(self, phase, *, session=False, authenticated=True):
        import json, subprocess, sys
        from unittest.mock import patch
        from ase.access import CredentialAccess
        from ase.core import execute
        f = self.f
        if session:
            marker = f.root / 'disposable-session.marker'
            recipes = {
                'probe': {'argv': [sys.executable, '-c', 'from pathlib import Path;import json;print(json.dumps({"status":"authenticated" if Path(' + repr(str(marker)) + ').exists() else "login_required"}))'], 'timeout': 5},
                'login': {'argv': [sys.executable, '-c', 'from pathlib import Path;Path(' + repr(str(marker)) + ').write_text("fixture session");print(\'{"status":"authenticated"}\')'], 'timeout': 5}}
            f.vault.configure('lab', f.target, {'session': recipes})
        contract = f.contract('no-launch-' + phase, action=True)
        if not authenticated:
            plain = contract['stages'][0]['actions'][0]
            plain.pop('access')
            plain['argv'] = [sys.executable, '-c', 'from pathlib import Path;p=Path("resource.counter");p.write_text(str(int(p.read_text())+1) if p.exists() else "1")']
            plain['observation_argv'] = [sys.executable, '-c', 'from pathlib import Path;import json;p=Path("resource.counter");print(json.dumps({"exists":p.exists(),"resource_id":"counter" if p.exists() else None}))']
        task = f.create(contract)
        stage = task['contract']['stages'][0]
        action = stage['actions'][0]
        real_ensure, real_popen = CredentialAccess._ensure, subprocess.Popen
        resolutions, effects, statuses = [], [], []
        def ensure_then_stop(client, *args, **kwargs):
            value = real_ensure(client, *args, **kwargs)
            resolutions.append(True)
            if phase == 'auth' and len(resolutions) == 2:
                f.controller.request_stop(task['task_id'], reason='fixture before effect launch')
            return value
        def execute_then_guard(argv, *args, **kwargs):
            if phase == 'guard' and list(argv) == action['argv']:
                f.controller.request_stop(task['task_id'], reason='fixture at actual final guard')
            return execute(argv, *args, **kwargs)
        def observe_launch(argv, *args, **kwargs):
            if list(argv) == action['argv']:
                import sqlite3
                from contextlib import closing
                db_path = f.store.db.execute('PRAGMA database_list').fetchone()[2]
                with closing(sqlite3.connect(db_path)) as independent_reader:
                    committed = json.loads(independent_reader.execute(
                        "SELECT payload FROM runtime_records WHERE kind='operation' AND key=?",
                        (self.operation(task)['action_id'],)).fetchone()[0])
                self.assertEqual(committed['state'], 'UNKNOWN', 'Write-ahead uncertainty must be committed before Popen')
                self.assertNotIn('host_nonlaunch_proof', committed)
                effects.append(True)
            elif session and any(list(argv) == item['argv'] for item in recipes.values()):
                statuses.append(True)
            return real_popen(argv, *args, **kwargs)
        with patch.object(CredentialAccess, '_ensure', ensure_then_stop), patch('ase.core.execute', side_effect=execute_then_guard), patch('ase.core.subprocess.Popen', side_effect=observe_launch):
            with self.assertRaises(ValueError):
                f.controller.execute_actions(task['task_id'], stage)
        stopped = self.operation(task)
        self.assertEqual(effects, [])
        if session:
            self.assertEqual(len(statuses), 4, 'Actual login and probe processes must stay distinct from the effect process')
        self.assertEqual(stopped['state'], 'ABSENT')
        self.assertEqual(stopped['attempt_number'], 1)
        self.assertTrue(stopped['ever_attempted'], 'Keep the attempt high-water and history')
        self.assertIn('host_nonlaunch_proof', stopped)
        f.controller._save('stop', task['task_id'], None)  # _run(resume=True)'s explicit stop clearing.
        with patch('ase.core.subprocess.Popen', side_effect=observe_launch):
            observation = f.controller.execute_actions(task['task_id'], stage)[0]
            f.controller.execute_actions(task['task_id'], stage)
        self.assertTrue(observation['exists'])
        self.assertEqual(effects, [True])
        self.assertEqual((f.repo / 'resource.counter').read_text(), '1')
        applied = self.operation(task)
        self.assertEqual(applied['attempt_number'], 2)
        self.assertNotIn('host_nonlaunch_proof', applied)
        self.assertEqual(applied['state'], 'APPLIED')

    def test_action_entry_rejects_an_uncommitted_caller_transaction(self):
        f = self.f
        task = f.create(f.contract('uncommitted-caller', action=True))
        with transaction(f.store.db):
            with self.assertRaisesRegex(ValueError, 'committed write-ahead'):
                f.controller.execute_actions(task['task_id'], task['contract']['stages'][0])
            self.assertTrue(f.store.db.in_transaction)
        self.assertFalse((f.repo / 'resource.counter').exists())

    def test_auth_time_stop_resumes_the_unlaunched_effect_once(self):
        self.assert_stop_before_launch_resumes_once('auth')

    def test_final_launch_guard_denial_resumes_the_unlaunched_effect_once(self):
        self.assert_stop_before_launch_resumes_once('guard')

    def test_plain_command_final_guard_denial_resumes_once(self):
        self.assert_stop_before_launch_resumes_once('guard', authenticated=False)

    def test_auth_status_processes_are_not_effect_launches(self):
        self.assert_stop_before_launch_resumes_once('auth', session=True)

    def hidden_task(self, name, *, timeout=False):
        import sys
        contract = self.f.contract(name, action=True)
        action = contract['stages'][0]['actions'][0]
        action['observation_argv'] = [sys.executable, '-c', 'print(\'{"exists":false}\')']
        if timeout:
            action['timeout'] = 1
            action['argv'][-1] += ';import time;time.sleep(2)'
        task = self.f.create(contract)
        return task, task['contract']['stages'][0]

    def assert_unknown_not_replayed(self, task, stage, *, count='1'):
        f = self.f
        operation = self.operation(task)
        self.assertEqual(operation['state'], 'UNKNOWN')
        self.assertNotIn('host_nonlaunch_proof', operation)
        f.controller._save('stop', task['task_id'], None)
        with self.assertRaisesRegex(ValueError, 'UNKNOWN'):
            f.controller.execute_actions(task['task_id'], stage)
        self.assertEqual((f.repo / 'resource.counter').read_text(), count)
        self.assertEqual(self.operation(task)['attempt_number'], 1)

    def test_actual_effect_with_lost_ack_stays_unknown_and_is_not_replayed(self):
        from unittest.mock import patch
        from ase.core import execute
        task, stage = self.hidden_task('lost-effect-ack')
        def lose_ack(argv, *args, **kwargs):
            result = execute(argv, *args, **kwargs)
            if list(argv) == stage['actions'][0]['argv']:
                raise ValueError('fixture lost actual effect acknowledgement')
            return result
        with patch('ase.core.execute', side_effect=lose_ack), self.assertRaisesRegex(ValueError, 'acknowledgement'):
            self.f.controller.execute_actions(task['task_id'], stage)
        self.assert_unknown_not_replayed(task, stage)

    def test_timed_out_actual_effect_stays_unknown_and_is_not_replayed(self):
        task, stage = self.hidden_task('timed-out-effect', timeout=True)
        with self.assertRaises(ValueError):
            self.f.controller.execute_actions(task['task_id'], stage)
        self.assertTrue(self.operation(task)['execution_receipt']['execution']['timed_out'])
        self.assert_unknown_not_replayed(task, stage)

    def test_possible_launch_error_is_not_nonlaunch_proof(self):
        import subprocess
        from unittest.mock import patch
        task, stage = self.hidden_task('possible-launch')
        real_popen = subprocess.Popen
        def lost_process_handle(argv, *args, **kwargs):
            proc = real_popen(argv, *args, **kwargs)
            if list(argv) == stage['actions'][0]['argv']:
                proc.wait(timeout=5)
                raise OSError('fixture handle lost after actual Popen')
            return proc
        with patch('ase.core.subprocess.Popen', side_effect=lost_process_handle), self.assertRaises(ValueError):
            self.f.controller.execute_actions(task['task_id'], stage)
        self.assert_unknown_not_replayed(task, stage)

    def test_hard_crash_before_effect_launch_retains_unknown_without_a_witness(self):
        import subprocess, sys
        from ase.operations import OperationLedger
        task, stage = self.hidden_task('hard-crash')
        script = """import os,sys
from unittest.mock import patch
from ase.core import execute
from ase.store import Store
from ase.workflow import WorkflowController
store=Store(sys.argv[1],sys.argv[2]);controller=WorkflowController(store)
stage=controller.tasks.snapshot('hard-crash')['contract']['stages'][0]
def crash(argv,*args,**kwargs):
 if list(argv)==stage['actions'][0]['argv']:os._exit(23)
 return execute(argv,*args,**kwargs)
with patch('ase.core.execute',side_effect=crash):controller.execute_actions('hard-crash',stage)
"""
        result = subprocess.run([sys.executable, '-B', '-c', script, str(self.f.repo), str(self.f.home)], timeout=10)
        self.assertEqual(result.returncode, 23)
        operation = self.operation(task)
        self.assertEqual(operation['state'], 'UNKNOWN')
        self.assertNotIn('host_nonlaunch_proof', operation)
        with self.assertRaisesRegex(ValueError, 'replayed'):
            OperationLedger(self.f.store).started(operation['action_id'])
        self.assertFalse((self.f.repo / 'resource.counter').exists())


if __name__ == '__main__':
    unittest.main()
