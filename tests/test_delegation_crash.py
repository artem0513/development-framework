"""Owned coordinator process kills and CAS races, without native/model verdicts.

Native handles in this deterministic registry fixture are observations supplied
by the caller. Actual Codex/native-subagent validation is a separate live test.
"""
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

REPOSITORY = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(REPOSITORY), str(REPOSITORY / 'tests')]

from ase.state import read_record
from ase.store import Store
import test_delegations as fixture


def save(path, value):
    path.write_text(json.dumps(value, indent=2), encoding='utf-8')


def hold(base, value):
    save(base / 'ready.json', {'pid': os.getpid(), **value})
    while True:
        time.sleep(0.02)


def worker(base, mode, ordinal=None):
    with closing(Store(base / 'work', base / 'state')) as store:
        service = fixture.registry(store)
        if mode == 'claim':
            save(base / ('claim-ready-' + ordinal + '.json'), {'pid': os.getpid()})
            deadline = time.monotonic() + 20
            while not (base / 'go.json').exists():
                if time.monotonic() >= deadline:
                    raise ValueError('NOT_TESTED: CAS claim release signal not received')
                time.sleep(0.02)
            try:
                result = service.dispatch('delegated', 'race', expected_revision=1,
                    coordinator='lead-' + ordinal, backend='codex')
                value = {'status': 'DISPATCHED', 'attempt_id': result['payload']['attempt']['id']}
            except ValueError as error:
                value = {'status': 'HELD', 'reason': str(error)}
            save(base / ('claim-result-' + ordinal + '.json'), value)
            return
        value = service.register('delegated', fixture.spec('crash'))
        if mode == 'outer':
            store.db.execute('BEGIN IMMEDIATE')
            try:
                ticket = service.dispatch('delegated', 'crash', expected_revision=value['revision'],
                    coordinator='lead', backend='codex')
            except ValueError as error:
                hold(base, {'boundary': 'outer-transaction-before-commit', 'handoff_returned': False, 'reason': str(error)})
            else:
                hold(base, {'boundary': 'outer-transaction-before-commit', 'handoff_returned': True,
                    'attempt_id': ticket['payload']['attempt']['id']})
        dispatched = service.dispatch('delegated', 'crash', expected_revision=value['revision'],
            coordinator='lead', backend='codex')
        attempt = dispatched['payload']['attempt']['id']
        if mode == 'dispatch':
            hold(base, {'boundary': 'dispatch-committed-before-native-spawn', 'attempt_id': attempt})
        current = service.attach('delegated', 'crash', attempt,
            {'backend': 'codex', 'agent_id': 'fixture-worker-completed'}, expected_revision=dispatched['revision'])
        output = Path(dispatched['handoff']['artifact_dir'])
        output.mkdir(parents=True, exist_ok=True)
        content = b'Bounded returned registry fixture result\n'
        (output / 'finding.txt').write_bytes(content)
        payload = {'attempt_id': attempt, 'agent_id': 'fixture-worker-completed',
            'status': 'COMPLETE', 'summary': 'The fixture returned its exact private artifact',
            'artifacts': [{'path': 'finding.txt', 'sha256': hashlib.sha256(content).hexdigest()}]}
        import ase.delegations as delegation_module
        original = delegation_module.write_record

        def checkpoint(db, kind, key, value, *, expected_revision):
            if kind == 'delegation' and value.get('state') == 'RESULT_READY':
                result = value['result']
                path = Path(result['path'])
                assert path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == result['sha256']
                json.loads(path.read_bytes())
                hold(base, {'boundary': 'sealed-result-before-db-commit', 'attempt_id': attempt,
                    'result': result, 'artifact': str(output / 'finding.txt')})
            return original(db, kind, key, value, expected_revision=expected_revision)

        with patch.object(delegation_module, 'write_record', new=checkpoint):
            service.result('delegated', 'crash', attempt, payload, expected_revision=current['revision'])
        raise AssertionError('NOT_TESTED: sealed-result checkpoint not reached')


class DelegationCrashTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='ase-delegation-crash-')
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        store = fixture.prepare(self.base)
        store.close()

    def launch(self, mode, ordinal=None):
        argv = [sys.executable, '-B', str(Path(__file__).resolve()), '--worker', str(self.base), mode]
        if ordinal is not None:
            argv.append(ordinal)
        name = mode + ('-' + ordinal if ordinal is not None else '') + '.log'
        with (self.base / name).open('ab') as log:
            process = subprocess.Popen(argv, cwd=REPOSITORY, stdout=log, stderr=subprocess.STDOUT)
        self.addCleanup(self.stop, process)
        return process

    @staticmethod
    def stop(process):
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)

    def wait_json(self, path, processes, timeout=25):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for process in processes:
                self.assertIsNone(process.poll(), 'Coordinator exited before the selected readiness signal')
            if path.exists():
                try:
                    return json.loads(path.read_bytes())
                except json.JSONDecodeError:
                    pass
            time.sleep(0.02)
        self.fail('NOT_TESTED: selected readiness point was not reached')

    def kill_ready(self, mode):
        process = self.launch(mode)
        ready = self.wait_json(self.base / 'ready.json', [process])
        self.assertEqual(ready['pid'], process.pid)
        process.kill()
        process.wait(timeout=10)
        self.assertNotEqual(process.returncode, 0)
        return ready

    def test_two_real_processes_cannot_claim_the_same_pending_assignment(self):
        with closing(Store(self.base / 'work', self.base / 'state')) as store:
            fixture.registry(store).register('delegated', fixture.spec('race'))
        processes = [self.launch('claim', str(index)) for index in (1, 2)]
        for index, process in zip((1, 2), processes):
            ready = self.wait_json(self.base / ('claim-ready-' + str(index) + '.json'), processes)
            self.assertEqual(ready['pid'], process.pid)
        save(self.base / 'go.json', {'dispatch': True})
        for process in processes:
            process.wait(timeout=25)
            self.assertEqual(process.returncode, 0)
        results = [json.loads((self.base / ('claim-result-' + str(index) + '.json')).read_bytes()) for index in (1, 2)]
        self.assertEqual(sorted(row['status'] for row in results), ['DISPATCHED', 'HELD'])
        with closing(Store(self.base / 'work', self.base / 'state')) as store:
            actual = fixture.registry(store).get('delegated', 'race')
            self.assertEqual(actual['payload']['state'], 'DISPATCHING')
            self.assertEqual(actual['payload']['attempt']['id'], next(row['attempt_id'] for row in results if row['status'] == 'DISPATCHED'))
            budget = read_record(store.db, 'budget', 'delegated')['payload'] or {}
            self.assertEqual(budget.get('task_cycles', 0), 0)
            self.assertEqual(budget.get('stages', {}), {})
            self.assertEqual(budget.get('diagnostics', {}), {})
            self.assertEqual(budget.get('active_seconds', 0), 0)
            self.assertEqual(budget.get('active_by_task', {}), {})

    def test_hard_kill_after_dispatch_intent_never_repeats_unknown_native_launch(self):
        ready = self.kill_ready('dispatch')
        with closing(Store(self.base / 'work', self.base / 'state')) as store:
            service = fixture.registry(store)
            before = service.get('delegated', 'crash')
            self.assertEqual(before['payload']['state'], 'DISPATCHING')
            self.assertEqual(before['payload']['attempt']['id'], ready['attempt_id'])
            alias = service.register('delegated', fixture.spec('alias-before-recovery'))
            with self.assertRaisesRegex(ValueError, 'unresolved launch'):
                service.dispatch('delegated', 'alias-before-recovery', expected_revision=alias['revision'],
                    coordinator='new-coordinator', backend='codex')
            self.assertIsNone(service.get('delegated', 'alias-before-recovery')['payload']['attempt'])
            for _ in range(2):
                service.recover('delegated', 'crash')
                current = service.get('delegated', 'crash')
                self.assertEqual(current['payload']['state'], 'UNKNOWN')
                self.assertEqual(current['payload']['attempt']['id'], ready['attempt_id'])
                with self.assertRaises(ValueError):
                    service.dispatch('delegated', 'crash', expected_revision=current['revision'], coordinator='lead', backend='codex')
                self.assertTrue(service.closure_blockers('delegated'))
            self.assertEqual(store.db.execute('PRAGMA integrity_check').fetchone()[0], 'ok')

    def test_outer_transaction_kill_never_exposes_an_uncommitted_launch_ticket(self):
        ready = self.kill_ready('outer')
        self.assertFalse(ready['handoff_returned'], 'A caller could launch native work from a ticket lost by rollback')
        self.assertIn('transaction', ready['reason'])
        with closing(Store(self.base / 'work', self.base / 'state')) as store:
            service = fixture.registry(store)
            pending = service.get('delegated', 'crash')
            self.assertEqual(pending['payload']['state'], 'PENDING')
            self.assertIsNone(pending['payload']['attempt'])
            first = service.dispatch('delegated', 'crash', expected_revision=pending['revision'], coordinator='lead', backend='codex')
            self.assertEqual(first['payload']['state'], 'DISPATCHING')

    def test_hard_kill_after_result_seal_recovers_exact_uncommitted_result(self):
        ready = self.kill_ready('seal')
        path = Path(ready['result']['path'])
        raw, stamp = path.read_bytes(), path.stat().st_mtime_ns
        with closing(Store(self.base / 'work', self.base / 'state')) as store:
            service = fixture.registry(store)
            before = service.get('delegated', 'crash')
            self.assertEqual(before['payload']['state'], 'RUNNING')
            self.assertIsNone(before['payload'].get('result'))
            service.recover('delegated', 'crash')
            recovered = service.get('delegated', 'crash')
            self.assertEqual(recovered['payload']['state'], 'RESULT_READY')
            self.assertEqual(recovered['payload']['attempt']['id'], ready['attempt_id'])
            self.assertEqual(recovered['payload']['result'], ready['result'])
            accepted = service.accept('delegated', 'crash', ready['attempt_id'],
                expected_revision=recovered['revision'], coordinator='lead', summary='Compared recovered private result with exact dispatch')
            self.assertEqual(accepted['payload']['state'], 'ACCEPTED')
            service.recover('delegated', 'crash')
            self.assertEqual(service.get('delegated', 'crash'), accepted)
            self.assertEqual(path.read_bytes(), raw)
            self.assertEqual(path.stat().st_mtime_ns, stamp)
            self.assertEqual(service.closure_blockers('delegated'), [])
            self.assertEqual(store.db.execute('PRAGMA integrity_check').fetchone()[0], 'ok')


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == '--worker':
        worker(Path(sys.argv[2]), sys.argv[3], sys.argv[4] if len(sys.argv) > 4 else None)
    else:
        unittest.main()
