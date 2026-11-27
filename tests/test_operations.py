import json
from pathlib import Path
import tempfile
import unittest

from ase.operations import OperationLedger
from ase.store import Store
from ase.tasks import TaskService
from test_task_contract_v2 import contract


class OperationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ase-operation-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        work = self.root / "work"
        work.mkdir()
        self.store = Store(work, self.root / "state")
        self.addCleanup(self.store.close)
        TaskService(self.store).create(contract())
        self.ops = OperationLedger(self.store)

    def test_lost_create_ack_reconciles_without_duplicate(self):
        first = self.ops.prepare("approval-task", "draft", "create-resource", {"resource": "vm-1"})
        self.assertTrue(first["new"])
        resource = self.root / "resource.json"
        resource.write_text(json.dumps({"id": "vm-1", "running": True}))
        self.ops.started("create-resource")
        second = self.ops.prepare("approval-task", "draft", "create-resource", {"resource": "vm-1"})
        self.assertFalse(second["new"])
        self.assertEqual(second["state"], "UNKNOWN")
        result = self.ops.reconcile("create-resource", {"exists": resource.exists(), "resource_id": "vm-1", "proof_ref": str(resource)})
        self.assertEqual(result["state"], "APPLIED")
        self.assertFalse(self.ops.prepare("approval-task", "draft", "create-resource", {"resource": "vm-1"})["new"])

    def test_same_id_different_intent_conflicts(self):
        self.ops.prepare("approval-task", "draft", "create", {"resource": "vm-1"})
        with self.assertRaisesRegex(ValueError, "CONFLICT"):
            self.ops.prepare("approval-task", "draft", "create", {"resource": "vm-2"})

    def test_missing_observation_does_not_prove_absence(self):
        self.ops.prepare("approval-task", "draft", "create", {"resource": "vm-1"})
        with self.assertRaises(ValueError):
            self.ops.reconcile("create", {"exists": False})


class UriEffectOverlapRepairTests(unittest.TestCase):
    """Actual approved loopback HTTP effects with a deliberately invisible listing."""
    def setUp(self):
        import threading
        from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
        from ase.workflow import WorkflowController
        self.temp = tempfile.TemporaryDirectory(prefix="ru-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.repo = self.base / "r"
        self.repo.mkdir()
        self.counts = {}
        counts = self.counts
        repo = self.repo
        class Provider(SimpleHTTPRequestHandler):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, directory=str(repo), **kwargs)
            def log_message(self, *args):
                pass
            def do_POST(self):
                target = self.translate_path(self.path)
                counts[target] = counts.get(target, 0) + 1
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"{}")
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
        self.worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.worker.start()
        self.addCleanup(self.close_server)
        self.store = Store(self.repo, self.base / "s")
        self.addCleanup(self.store.close)
        self.tasks = TaskService(self.store)
        self.controller = WorkflowController(self.store)

    def close_server(self):
        self.server.shutdown()
        self.worker.join(2)
        self.server.server_close()

    def execute(self, path, action_id):
        import sys
        from ase.state import read_record
        url = "http://127.0.0.1:" + str(self.server.server_port) + path
        observer = [sys.executable, "-c", 'print(\'{"exists":false}\')']
        action = {"id": action_id, "argv": [sys.executable, "-c", "import urllib.request;print(urllib.request.urlopen(urllib.request.Request(" + repr(url) + ",data=b'',method='POST')).read().decode())"], "observation_argv": observer}
        spec = {"id": "u", "schema_version": 2, "profile": "infrastructure", "request": "Apply one approved local provider effect", "scope": {"resources": [url], "effects": ["command"]}, "acceptance": [{"id": "ONE", "expected": "One local effect", "evidence_ids": ["visible"]}], "evidence": [{"id": "visible", "kind": "probe", "argv": observer, "predicate": {"path": "exists", "op": "eq", "value": True}}], "stages": [{"id": "draft", "acceptance_ids": ["ONE"], "actions": [action]}], "unknowns": []}
        old = read_record(self.store.db, "task", "u")["payload"]
        task = self.tasks.revise("u", spec, expected_revision=self.tasks.snapshot("u")["revision"]) if old else self.tasks.create(spec)
        task = self.tasks.propose("u", {"summary": "One approved local target", "stage_ids": ["draft"]}, expected_revision=task["revision"])
        task = self.tasks.approve("u", proposal_hash=task["proposal_hash"], approved_stage_ids=["draft"],
                                  user_message_ref=f"test:uri-effect:{task['revision']}")
        self.controller.execute_actions("u", task["contract"]["stages"][0])

    def test_percent_unreserved_alias_cannot_duplicate_unknown_actual_http_effect(self):
        from ase.state import read_record, hash_value
        with self.assertRaisesRegex(ValueError, "postcondition"):
            self.execute("/resource", "first")
        first_id = hash_value({"task": "u", "stage": "draft", "action": "first"})
        self.assertEqual(read_record(self.store.db, "operation", first_id)["payload"]["state"], "UNKNOWN")
        error = None
        try:
            self.execute("/%72esource", "second")
        except ValueError as ex:
            error = str(ex)
        self.assertEqual(sum(self.counts.values()), 1, "URI alias repeated the same actual HTTP target: " + str(error))
        self.assertRegex(error or "", "CONFLICT")

    def test_known_disjoint_uri_path_can_mutate_while_other_is_unknown(self):
        for path, action_id in (("/resource", "first"), ("/resource-two", "second")):
            with self.assertRaisesRegex(ValueError, "postcondition"):
                self.execute(path, action_id)
        self.assertEqual(len(self.counts), 2)
        self.assertEqual(sorted(self.counts.values()), [1, 1])

    def test_overlap_normalization_never_merges_exact_uri_opaque_or_reserved_identity(self):
        pairs = [("http://example.invalid/resource", "http://example.invalid/%72esource"),
                 ("http://example.invalid/Resource", "http://example.invalid/resource"),
                 ("http://example.invalid/a%2Fb", "http://example.invalid/a/b"),
                 ("opaque:%72esource", "opaque:resource")]
        for left, right in pairs:
            self.assertNotEqual(OperationLedger._scope({"resources": [left]}), OperationLedger._scope({"resources": [right]}))


if __name__ == "__main__":
    unittest.main()


class HostNonlaunchProofTests(unittest.TestCase):
    """Exact-attempt protocol tests; no provider/semantic acceptance claims."""
    setUp = OperationTests.setUp

    def prepare(self):
        return self.ops.prepare('approval-task', 'draft', 'host-effect', {'resource': 'disposable'})

    def witness(self):
        self.prepare()
        return self.ops.start_launch('host-effect')

    def current(self):
        from ase.state import read_record
        return read_record(self.store.db, 'operation', 'host-effect')['payload']

    def test_only_a_completed_host_scope_can_supply_nonlaunch_proof(self):
        witness = self.witness()
        with self.assertRaisesRegex(ValueError, 'completed host'):
            self.ops.record_nonlaunch(witness)
        with witness:
            with self.assertRaisesRegex(ValueError, 'completed host'):
                self.ops.record_nonlaunch(witness)
        proof = self.ops.record_nonlaunch(witness)
        self.assertEqual(proof['state'], 'ABSENT')
        self.assertTrue(proof['ever_attempted'])
        self.assertEqual(proof['attempt_number'], 1)
        self.assertEqual(self.ops.record_nonlaunch(witness), proof)

    def test_stale_host_proof_cannot_cover_the_next_attempt(self):
        witness = self.witness()
        with witness:
            pass
        self.ops.record_nonlaunch(witness)
        self.ops.reconcile('host-effect', {'exists': False, 'proof_ref': 'fixture:actual-absence'})
        second = self.ops.start_launch('host-effect')
        self.assertEqual(self.current()['attempt_number'], 2)
        self.assertNotIn('host_nonlaunch_proof', self.current())
        with self.assertRaisesRegex(ValueError, 'current attempt'):
            self.ops.record_nonlaunch(witness)
        self.assertEqual(self.current()['state'], 'UNKNOWN')
        self.assertNotEqual(second.binding['host_attempt_id'], witness.binding['host_attempt_id'])

    def test_final_process_boundary_prevents_nonlaunch_certification(self):
        from contextlib import nullcontext
        witness = self.witness()
        with witness:
            with witness.guard(nullcontext)():
                pass
        with self.assertRaisesRegex(ValueError, 'completed host'):
            self.ops.record_nonlaunch(witness)
        self.assertEqual(self.current()['state'], 'UNKNOWN')

    def test_prior_unknown_attempt_cannot_acquire_a_new_witness(self):
        self.prepare()
        self.ops.started('host-effect')
        with self.assertRaisesRegex(ValueError, 'replayed'):
            self.ops.start_launch('host-effect')
        self.assertNotIn('host_nonlaunch_proof', self.current())
        self.assertEqual(self.current()['attempt_number'], 1)

    def test_changed_intent_rejects_an_otherwise_completed_host_witness(self):
        from ase.state import read_record, transaction, write_record
        witness = self.witness()
        with witness:
            pass
        record = read_record(self.store.db, 'operation', 'host-effect')
        changed = dict(record['payload'], intent_hash='changed-current-intent')
        with transaction(self.store.db) as db:
            write_record(db, 'operation', 'host-effect', changed, expected_revision=record['revision'])
        with self.assertRaisesRegex(ValueError, 'current attempt or intent'):
            self.ops.record_nonlaunch(witness)
        self.assertEqual(self.current()['state'], 'UNKNOWN')

    def test_receipt_blocks_nonlaunch_even_if_a_scope_did_not_enter_its_guard(self):
        import hashlib
        witness = self.witness()
        with witness:
            pass
        log = self.root / 'submission.log'
        log.write_text('protocol-only submission receipt')
        self.ops.record_submission('host-effect', {'log': str(log), 'log_sha256': hashlib.sha256(log.read_bytes()).hexdigest()})
        with self.assertRaisesRegex(ValueError, 'submitted'):
            self.ops.record_nonlaunch(witness)
        self.assertEqual(self.current()['state'], 'UNKNOWN')

    def test_prior_submission_and_high_water_survive_nonlaunch_retry(self):
        import hashlib
        self.prepare()
        self.ops.started('host-effect')
        log = self.root / 'submission.log'
        log.write_text(json.dumps({'provider_attempt_id': 'actual-protocol-fixture-submission-1'}))
        self.ops.record_submission('host-effect', {'log': str(log), 'log_sha256': hashlib.sha256(log.read_bytes()).hexdigest()})
        self.ops.reconcile('host-effect', {'exists': False, 'absence_confirmed': True,
            'provider_attempt_id': 'actual-protocol-fixture-submission-1', 'proof_ref': 'fixture:terminal-rejection'})
        witness = self.ops.start_launch('host-effect')
        with witness:
            pass
        self.ops.record_nonlaunch(witness)
        self.assertEqual(self.current()['attempt_number'], 2)
        self.assertIn('actual-protocol-fixture-submission-1', self.current()['consumed_provider_attempt_ids'])
        self.ops.started('host-effect')
        self.assertEqual(self.current()['attempt_number'], 3)
        self.assertNotIn('host_nonlaunch_proof', self.current())
        self.assertIn('actual-protocol-fixture-submission-1', self.current()['consumed_provider_attempt_ids'])
        with self.assertRaisesRegex(ValueError, 'current attempt'):
            self.ops.record_nonlaunch(witness)

    def test_rolled_back_started_cannot_supply_committed_nonlaunch_proof(self):
        from ase.state import transaction
        self.prepare()
        with self.assertRaisesRegex(ValueError, 'fixture rollback'):
            with transaction(self.store.db):
                witness = self.ops.start_launch('host-effect')
                raise ValueError('fixture rollback')
        with witness:
            pass
        with self.assertRaisesRegex(ValueError, 'current attempt'):
            self.ops.record_nonlaunch(witness)
        self.assertEqual(self.current()['state'], 'PREPARED')
