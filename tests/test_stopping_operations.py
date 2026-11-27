"""Real effect/stop/resume integration; protocol roles never grant approval."""
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import unittest

from ase.discovery import discover_capabilities, record_discovery
from ase.state import hash_value, read_record, transaction, write_record
from ase.workflow import WorkflowController
import test_stopping as base


class StoppingOperationTests(unittest.TestCase):
    def setUp(self):
        # Reuse the fixture without importing/discovering or inheriting its tests.
        base.StopTests.setUp(self)
        self.ready = self.base / "effect-ready"
        self.polls = self.base / "owner-polls.jsonl"
        self.cancel_receipt = self.base / "cancelled-effect.json"
        self.environment = {**os.environ, "PYTHONPATH": str(base.ROOT), "PYTHONDONTWRITEBYTECODE": "1"}
        self.waiter = threading.Event()
        self.observation = [sys.executable, "-c",
            "from pathlib import Path;import json;p=Path('resource.counter');exists=p.exists();"
            "print(json.dumps({'exists':exists,'resource_id':'controlled-local-counter' if exists else None,'count':int(p.read_text()) if exists else 0}))"]
        self.effect = [sys.executable, "-c",
            "from pathlib import Path;import threading;p=Path('resource.counter');"
            "p.write_text(str((int(p.read_text()) if p.exists() else 0)+1));"
            + "Path(" + repr(str(self.ready)) + ").write_text('effect happened');threading.Event().wait(60)"]
        task = self.tasks.snapshot("stop-task")
        contract = dict(task["contract"], id="stop-task", profile="infrastructure", request="Create one controlled local resource without duplicating its effect",
                        scope={"resources": ["resource.counter"], "effects": ["command"]},
                        acceptance=[{"id": "RESOURCE", "expected": "The actual controlled resource exists with mutation count one", "evidence_ids": ["state"]}],
                        evidence=[{"id": "state", "kind": "probe", "argv": self.observation,
                                   "predicate": {"path": "count", "op": "eq", "value": 1}}],
                        stages=[{"id": "draft", "acceptance_ids": ["RESOURCE"], "actions": [{
                            "id": "create-counter", "argv": self.effect, "observation_argv": self.observation, "timeout": 20}]}])
        task = self.tasks.revise("stop-task", contract, expected_revision=task["revision"])
        task = self.tasks.propose("stop-task", {"summary": "Create the controlled disposable resource once", "stage_ids": ["draft"]}, expected_revision=task["revision"])
        self.tasks.approve("stop-task", proposal_hash=task["proposal_hash"], user_message_ref="test:explicit-local-resource-scope", approved_stage_ids=["draft"])
        self._configure_protocol_only_roles()

    def _configure_protocol_only_roles(self):
        """Capture valid transport with UNVERIFIED ACs, never semantic PASS."""
        trusted = self.home / "runner"
        trusted.mkdir(exist_ok=True)
        fixture = self.base / "roles.py"
        payload = """payload = {'verdict':'PASS'}
if 'REQUEST_JSON:\\n' in input_text:
 request = json.loads(input_text.split('REQUEST_JSON:\\n',1)[1])
 role = request['role']
 if role == 'implementation': raise AssertionError('No additional implementation is allowed in this resume fixture')
 if role == 'ac':
  task = json.loads(next(item['content'] for item in request['source_packet']['files'] if item['path']=='_ase_context/contract.json'))
  payload = {'criteria':[{'id':ac['id'],'expected':ac['expected'],'status':'UNVERIFIED','evidence':['protocol transport only'],
    'method':'controlled protocol fixture','limitations':['No model judgment was executed'],'missing_coverage':['Independent semantics unavailable']}
    for ac in task['acceptance']]}
 elif role == 'review': payload = {'findings':[],'summary':'Protocol transport only; no independent semantic review'}
"""
        self.assertEqual(base.FIXTURE.count("payload = {'verdict':'PASS'}"), 1)
        fixture.write_text(base.FIXTURE.replace("payload = {'verdict':'PASS'}", payload), encoding="utf-8")
        with transaction(self.store.db) as db:
            write_record(db, "backend_config", "codex", {"executable": sys.executable,
                         "executable_args": [str(fixture), "success", "codex"], "trusted_cwd": str(trusted), "protocol_only": True}, expected_revision=0)

    def _barrier(self, predicate, *, process=None, timeout=10, description):
        deadline = time.monotonic() + timeout
        while True:
            value = predicate()
            if value:
                return value
            if process is not None and process.poll() is not None:
                stdout, stderr = process.communicate(timeout=5)
                self.fail(description + ": worker ended before barrier: " + stdout + stderr)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self.fail("Timed out waiting for actual barrier: " + description)
            self.waiter.wait(min(0.01, remaining))

    def _latest_poll(self):
        if not self.polls.exists():
            return None
        lines = self.polls.read_text(encoding="utf-8").splitlines()
        for line in reversed(lines):
            try:
                return json.loads(line)
            except ValueError:
                continue
        return None

    def _checking_fixture(self, task_id):
        task = self.tasks.snapshot(task_id)
        controller = WorkflowController(self.store)
        controller.reserve_cycle(task_id, "draft")
        controller._save("cycle", task_id + ":draft", {"phase": "CHECKING", "contract_hash": task["contract_hash"], "proposal_hash": task["proposal_hash"]})
        record_discovery(self.store, task_id, discover_capabilities(self.store.project,
            scope={"resources": ["resource.counter"]}, backend_config={}),
            expected_revision=self.store.db.execute("SELECT COALESCE(MAX(revision),0) FROM task_discoveries WHERE task_id=?", (task_id,)).fetchone()[0])

    def _start_effect(self, task_id):
        self._checking_fixture(task_id)
        helper = self.base / "owner.py"
        helper.write_text("\n".join([
            "import json", "from pathlib import Path", "import ase.core as core", "from ase.store import Store", "from ase.workflow import WorkflowController",
            "original_execute=core.execute", "original_stop=WorkflowController._stop_requested", "poll_count=0",
            "effect=" + repr(self.effect), "poll_path=Path(" + repr(str(self.polls)) + ")", "receipt_path=Path(" + repr(str(self.cancel_receipt)) + ")",
            "def execute(*args,**kwargs):", " result=original_execute(*args,**kwargs)",
            " if list(args[0])==effect: receipt_path.write_text(json.dumps(result),encoding='utf-8')", " return result",
            "def stop(self,task_id):", " global poll_count", " result=original_stop(self,task_id)", " poll_count+=1",
            " with poll_path.open('a',encoding='utf-8') as stream: stream.write(json.dumps({'count':poll_count,'task_id':task_id,'stop':result})+'\\n')", " return result",
            "core.execute=execute", "WorkflowController._stop_requested=stop",
            "store=Store(" + repr(str(self.repo)) + "," + repr(str(self.home)) + ")",
            "try: print(json.dumps(WorkflowController(store).run(" + repr(task_id) + ",resume=True)),flush=True)", "finally: store.close()",
        ]), encoding="utf-8")
        process = subprocess.Popen([sys.executable, str(helper)], cwd=base.ROOT, env=self.environment,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        def cleanup():
            if process.poll() is None:
                if os.name == "nt":
                    subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True, timeout=5)
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=5)
        self.addCleanup(cleanup)
        self._barrier(self.ready.exists, process=process, description="controlled effect wrote counter and ready marker")
        self.assertEqual((self.repo / "resource.counter").read_text(), "1")
        return process

    def _public(self, *arguments, expected=0):
        result = subprocess.run([sys.executable, "-m", "ase", "--repo", str(self.repo), "--home", str(self.home), *arguments],
                                cwd=base.ROOT, env=self.environment, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        return json.loads(result.stdout)

    def _stop_effect(self, process, task_id, *, stop_id=None):
        stopped = self._public("task", "stop", stop_id or task_id, "--reason", "Stop controlled resource effect")
        self.assertEqual(stopped["state"], "PAUSED")
        stdout, stderr = process.communicate(timeout=5)
        self.assertEqual(process.returncode, 0, stdout + stderr)
        outcome = json.loads(stdout)
        self.assertEqual(outcome["closure"]["finish_reason"], "paused")
        receipt = json.loads(self.cancel_receipt.read_text(encoding="utf-8"))
        self.assertTrue(receipt["cancelled"])
        self.assertEqual(receipt["error"], "PROCESS_CANCELLED")
        self.assertEqual(self._operation(task_id)["state"], "UNKNOWN")
        self.assertEqual(read_record(self.store.db, "cycle", task_id + ":draft")["payload"]["phase"], "CHECKING")
        return outcome

    def _operation(self, task_id):
        key = hash_value({"task": task_id, "stage": "draft", "action": "create-counter"})
        operation = read_record(self.store.db, "operation", key)["payload"]
        self.assertIsNotNone(operation)
        return operation

    def test_stop_after_effect_then_one_resume_reconciles_without_duplicate_or_cycle(self):
        process = self._start_effect("stop-task")
        self._stop_effect(process, "stop-task")
        self.assertEqual((self.repo / "resource.counter").read_text(), "1")
        before = read_record(self.store.db, "budget", "stop-task")["payload"]
        self.assertEqual(before["task_cycles"], 1)
        self.assertEqual(before["stages"], {"stop-task:draft": 1})
        resumed = self._public("task", "resume", "stop-task", expected=3)
        self.assertEqual(resumed["status"], "BLOCKED")
        self.assertTrue(any("Protocol fixtures" in reason for reason in resumed["blocked_reasons"]))
        self.assertEqual(resumed["profile_evidence"]["by_id"]["state"]["status"], "PASS")
        self.assertEqual(resumed["profile_evidence"]["by_id"]["state"]["observation"]["count"], 1)
        operation = self._operation("stop-task")
        self.assertEqual(operation["state"], "APPLIED")
        self.assertEqual(operation["observation"]["resource_id"], "controlled-local-counter")
        self.assertIn("#sha256=", operation["observation"]["proof_ref"])
        self.assertEqual((self.repo / "resource.counter").read_text(), "1")
        after = read_record(self.store.db, "budget", "stop-task")["payload"]
        self.assertEqual(after["task_cycles"], before["task_cycles"])
        self.assertEqual(after["stages"], before["stages"])
        proofs = [json.loads(path.read_text()) for path in (self.store.root / "tasks" / "stop-task" / "sessions").glob("*/receipt.json")]
        self.assertEqual(sorted(proof["role"] for proof in proofs), ["ac", "review"])
        self.assertTrue(all(proof["evidence_kind"] == "protocol_only" for proof in proofs))
        self.assertTrue(all(proof["status"] == "COMPLETE" for proof in proofs))

    def test_root_stop_reaches_active_child_resource_effect(self):
        root = self.tasks.snapshot("stop-task")
        child = dict(root["contract"], id="active-child", parent_task_id="stop-task", parent_stage_id="draft")
        self.tasks.create(child)
        process = self._start_effect("active-child")
        self._stop_effect(process, "active-child", stop_id="stop-task")
        self.assertTrue(read_record(self.store.db, "stop", "stop-task")["payload"])
        self.assertEqual(self.tasks.snapshot("active-child")["state"], "PAUSED")
        self.assertEqual((self.repo / "resource.counter").read_text(), "1")

    def test_unrelated_stop_does_not_cancel_active_resource_worker(self):
        other = base.contract("unrelated")
        other["unknowns"], other["stages"] = [], other["stages"][:1]
        self.tasks.create(other)
        process = self._start_effect("stop-task")
        self._public("task", "stop", "unrelated", "--reason", "Pause independent task")
        previous = self._latest_poll()
        def subsequent_owner_poll():
            current = self._latest_poll()
            return current if current and (previous is None or current["count"] > previous["count"]) else None
        current = self._barrier(subsequent_owner_poll, process=process, description="active owner polled after unrelated stop committed")
        self.assertFalse(current["stop"])
        self.assertIsNone(process.poll())
        self.assertIsNone(read_record(self.store.db, "stop", "stop-task")["payload"])
        self._stop_effect(process, "stop-task")


if __name__ == "__main__":
    unittest.main()
