"""Real candidate transport and file writes, without semantic fixture approval."""
import copy
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

from ase.discovery import discover_capabilities, record_discovery
from ase.journal import Journal
from ase.runner import Runner
from ase.store import Store
from ase.tasks import TaskService
from ase.workflow import WorkflowController, apply_edits
from test_runner import FIXTURE


class CandidateAuthorityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="ase-candidate-authority-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.repo = self.base / "work"
        self.repo.mkdir()
        self.home = self.base / "state"
        self.store = Store(self.repo, self.home)
        self.addCleanup(self.store.close)
        self.tasks = TaskService(self.store)
        value = {"id": "candidate", "schema_version": 2, "profile": "document", "request": "Write the original guide",
            "scope": {"resources": ["guide.md"], "effects": ["file_write"]},
            "acceptance": [{"id": "A", "expected": "Guide exists", "evidence_ids": ["guide"]}],
            "evidence": [{"id": "guide", "kind": "artifact", "path": "guide.md"}],
            "stages": [{"id": "draft", "acceptance_ids": ["A"]}], "unknowns": []}
        task = self.tasks.create(value)
        self.approve(self.tasks, task)
        record_discovery(self.store, "candidate", discover_capabilities(self.store.project,
            scope={"resources": ["guide.md"]}, backend_config={}), expected_revision=0)
        trusted = self.base / "trusted"
        trusted.mkdir()
        provider = self.base / "provider.py"
        provider.write_text(FIXTURE.replace("payload = {'verdict':'PASS'}", """request = json.loads(input_text.split('REQUEST_JSON:\\n',1)[1])
if request['role'] == 'implementation': payload = {'edits':[{'path':'guide.md','operation':'write','before_sha256':'','content':'Original candidate'}],'summary':'Original candidate'}
elif request['role'] == 'ac': payload = {'criteria':[]}
else: payload = {'findings':[],'summary':'Protocol fixture, never semantic approval'}"""), encoding="utf-8")
        self.runner = Runner({"executable": sys.executable, "executable_args": [str(provider), "success", "codex"],
            "trusted_cwd": str(trusted), "protocol_only": True}, self.store.root / "tasks/candidate/sessions")
        self.controller = WorkflowController(self.store, runner=self.runner)
        self.other = Store(self.repo, self.home)
        self.addCleanup(self.other.close)

    @staticmethod
    def approve(tasks, task):
        task = tasks.propose("candidate", {"summary": "Exact current document scope", "stage_ids": ["draft"]},
                             expected_revision=task["revision"])
        return tasks.approve("candidate", proposal_hash=task["proposal_hash"],
                             user_message_ref=f"test:exact-scope:{task['revision']}",
                             approved_stage_ids=["draft"])

    def revise(self, reapprove=False):
        tasks = TaskService(self.other)
        current = tasks.snapshot("candidate")
        revised = copy.deepcopy(current["contract"])
        revised["scope"]["resources"] = ["replacement.md"]
        revised["stages"][0]["scope"]["resources"] = ["replacement.md"]
        revised["evidence"][0]["path"] = "replacement.md"
        revised["request"] = "Write only the replacement document"
        task = tasks.revise("candidate", revised, expected_revision=current["revision"])
        if reapprove:
            record_discovery(self.other, "candidate", discover_capabilities(self.other.project,
                scope={"resources": ["replacement.md"]}, backend_config={}), expected_revision=1)
            self.approve(tasks, task)

    def change_after_candidate(self, change):
        original = self.runner.run
        changed = []

        def run_then_change(adapter, request, **kwargs):
            result = original(adapter, request, **kwargs)
            if request["role"] == "implementation":
                self.assertEqual(result["result"]["status"], "COMPLETE")
                change()
                changed.append(True)
            return result

        with patch.object(self.runner, "run", side_effect=run_then_change):
            result = self.controller.run("candidate")
        self.assertEqual(changed, [True])
        self.assertEqual(result["status"], "BLOCKED")
        self.assertFalse((self.repo / "guide.md").exists(), "Old candidate wrote outside current authority")
        self.assertFalse((self.repo / "replacement.md").exists())

    def test_new_approval_cannot_authorize_candidate_from_old_scope(self):
        self.change_after_candidate(lambda: self.revise(reapprove=True))
        self.assertEqual(self.tasks.snapshot("candidate")["contract"]["scope"]["resources"], ["replacement.md"])

    def test_revoked_candidate_does_not_write(self):
        self.change_after_candidate(self.revise)

    def test_cancelled_candidate_does_not_write(self):
        def cancel():
            journal = Journal(self.other)
            journal.start_session("candidate", "owner-cancel")
            revision = self.other.db.execute("SELECT COALESCE(MAX(revision),0) FROM checkpoints WHERE task_id='candidate'").fetchone()[0]
            journal.finish_session("candidate", "owner-cancel", {"finish_reason": "cancelled", "summary": "Owner cancelled guide",
                "next_action": "Do not write", "context": {"summary": "Cancelled guide"}}, closure_key="cancel:finish",
                expected_revision=revision, expected_context_revision=journal.context()["revision"])
        self.change_after_candidate(cancel)
        self.assertEqual(self.tasks.snapshot("candidate")["state"], "CANCELLED")

    def test_final_validation_and_file_write_serialize_with_revision(self):
        self.other.db.execute("PRAGMA busy_timeout=0")
        blocked = []

        def write_inside_boundary(*args, **kwargs):
            self.assertTrue(self.store.db.in_transaction)
            try:
                self.revise()
            except sqlite3.OperationalError as error:
                self.assertIn("locked", str(error))
                blocked.append(True)
            return apply_edits(*args, **kwargs)

        with patch("ase.workflow.apply_edits", side_effect=write_inside_boundary):
            result = self.controller.run("candidate")
        self.assertEqual(blocked, [True])
        self.assertEqual((self.repo / "guide.md").read_text(), "Original candidate")
        self.assertEqual(result["status"], "BLOCKED")  # Protocol fixture never grants semantic acceptance.
        self.revise()  # Writer lock is not held during later checks, providers or after closure.


if __name__ == "__main__":
    unittest.main()
