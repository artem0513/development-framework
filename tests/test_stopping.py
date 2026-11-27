"""Real cancellation and durable stop records; no semantic fixture approvals."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from ase.core import execute
from ase.discovery import discover_capabilities, record_discovery
from ase.journal import Journal
from ase.runner import Runner
from ase.adapters.codex import CodexAdapter
from ase.state import read_record, transaction, write_record
from ase.store import Store
from ase.tasks import TaskService
from ase.workflow import WorkflowController
from test_task_contract_v2 import contract
from test_runner import FIXTURE, TransportFixture

ROOT = Path(__file__).resolve().parents[1]


class ProcessCancellationTests(unittest.TestCase):
    def test_check_command_cancels_after_real_start_even_with_positive_output(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            marker = root / "ready"
            result = execute([sys.executable, "-c", "from pathlib import Path;import time;print('Ran 1 test in 0.01s\\nOK',flush=True);Path('ready').write_text('ready');time.sleep(30)"],
                             root, root / "check.log", 20, cancel_requested=marker.exists)
            self.assertTrue(marker.is_file())
            self.assertTrue(result["cancelled"])
            self.assertLess(result["duration_seconds"], 5)
            self.assertEqual(result["error"], "PROCESS_CANCELLED")

    def test_exit_zero_after_stop_is_still_cancelled(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            result = execute([sys.executable, "-c", "from pathlib import Path;Path('ready').write_text('ready')"], root, root / "check.log", 10,
                             cancel_requested=(root / "ready").exists)
            self.assertTrue(result["cancelled"])
            self.assertEqual(result["error"], "PROCESS_CANCELLED")


class RunnerCancellationTests(TransportFixture):
    def test_real_model_process_and_descendant_are_cancelled(self):
        outcome = Runner(self.config(case="sleep"), self.base / "artifacts").run(CodexAdapter(), self.request(), timeout=20,
                    cancel_requested=(self.trusted / "descendant.pid").exists)
        self.assertTrue((self.trusted / "descendant.pid").exists())
        self.assertTrue(outcome["receipt"]["cancelled"])
        self.assertEqual(outcome["result"]["blocker"], "PROCESS_CANCELLED")
        self.assertLess(outcome["receipt"]["duration_seconds"], 5)


class StopTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ase-stop-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.repo = self.base / "workspace"
        self.repo.mkdir()
        self.home = self.base / "state"
        self.store = Store(self.repo, self.home)
        self.addCleanup(self.store.close)
        self.tasks = TaskService(self.store)
        c = contract("stop-task")
        c["unknowns"], c["stages"] = [], c["stages"][:1]
        task = self.tasks.create(c)
        task = self.tasks.propose(task["task_id"], {"summary": "Create guide", "stage_ids": ["draft"]}, expected_revision=task["revision"])
        self.tasks.approve(task["task_id"], proposal_hash=task["proposal_hash"], user_message_ref="test:authorized", approved_stage_ids=["draft"])
        record_discovery(self.store, task["task_id"], discover_capabilities(self.store.project, scope={"resources": ["."]}, backend_config={}), expected_revision=0)
        self.controller = WorkflowController(self.store)

    def test_idle_stop_is_atomic_dated_idempotent_closure(self):
        result = self.controller.request_stop("stop-task", reason="Finish today")
        self.assertEqual(result["state"], "PAUSED")
        checkpoint = self.store.db.execute("SELECT payload FROM checkpoints WHERE task_id='stop-task'").fetchone()
        self.assertIsNotNone(checkpoint)
        self.assertIn("resume", json.loads(checkpoint[0])["next_action"].lower())
        context = Journal(self.store).context()
        self.assertTrue(context["payload"]["next_action"])
        count = self.store.db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        self.controller.request_stop("stop-task", reason="Finish today")
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0], count)
        self.assertEqual(Journal(self.store).context()["revision"], context["revision"])

    def test_failed_stop_closure_rolls_back_flag_and_task(self):
        with patch("ase.journal.Journal.finish_session", side_effect=ValueError("fixture closure fault")):
            with self.assertRaisesRegex(ValueError, "fixture"):
                self.controller.request_stop("stop-task", reason="Finish today")
        self.assertIsNone(read_record(self.store.db, "stop", "stop-task")["payload"])
        self.assertEqual(self.tasks.snapshot("stop-task")["state"], "READY")

    def test_stop_committed_before_final_closure_wins_over_approved_result(self):
        def finish_race(*args, **kwargs):
            self.controller.request_stop("stop-task", reason="Stop at the final barrier")
            return {"status": "APPROVED", "reasons": []}
        with patch.object(self.controller, "_run", side_effect=finish_race):
            result = self.controller.run("stop-task")
        self.assertEqual(result["closure"]["finish_reason"], "paused")
        self.assertEqual(self.tasks.snapshot("stop-task")["state"], "PAUSED")
        status = self.tasks.readiness("stop-task", include_progress=True)["next_action"]
        self.assertEqual(result["closure"]["next_action"], status)
        self.assertIn("no source baseline is recorded for this task", status)
        self.assertNotIn("Inspect reported blockers", status)

    def test_stop_hint_snapshot_timeout_still_commits_the_paused_closure(self):
        from ase.tasks import record_baseline
        record_baseline(self.store, "stop-task", self.tasks.snapshot("stop-task")["contract"])
        in_transaction = []
        def slow_snapshot(*args, **kwargs):
            in_transaction.append(self.store.db.in_transaction)
            raise subprocess.TimeoutExpired("git", 60)
        def stop_then_fail(*args, **kwargs):
            self.controller.request_stop("stop-task", reason="Stop during run")
            return {"status": "BLOCKED", "blocked_reasons": ["stopped"]}
        with patch("ase.gates.source_snapshot", side_effect=slow_snapshot), \
                patch.object(self.controller, "_run", side_effect=stop_then_fail):
            result = self.controller.run("stop-task")
        self.assertEqual(result["closure"]["finish_reason"], "paused")
        self.assertIn("changed files could not be listed", result["closure"]["next_action"])
        self.assertTrue(in_transaction)
        self.assertNotIn(True, in_transaction)
        self.assertEqual(self.tasks.snapshot("stop-task")["state"], "PAUSED")
        self.assertIsNone(read_record(self.store.db, "active_workflow", "stop-task")["payload"])

    def test_stop_status_lists_files_changed_since_the_baseline(self):
        import re, shlex
        from ase.__main__ import parser
        from ase.tasks import record_baseline
        (self.repo / "guide.md").write_text("original", encoding="utf-8")
        task = self.tasks.snapshot("stop-task")
        record_baseline(self.store, "stop-task", task["contract"])
        (self.repo / "guide.md").write_text("edited by the still-running implementer", encoding="utf-8")
        self.controller.request_stop("stop-task", reason="Stop during run")
        status = self.tasks.readiness("stop-task", include_progress=True)["next_action"]
        self.assertIn("1 file(s) in the task scope differ from the recorded baseline: guide.md", status)
        args = parser().parse_args(shlex.split(re.search(r"`(ase task resume [^`]+)`", status).group(1))[1:])
        self.assertEqual((args.action, args.task_id), ("resume", "stop-task"))
        (self.repo / "guide.md").write_text("original", encoding="utf-8")
        self.assertIn("no file in the task scope differs from the recorded baseline",
                      self.tasks.readiness("stop-task", include_progress=True)["next_action"])

    def test_stop_hint_matches_absolute_in_repo_scope_resources(self):
        from ase.tasks import _baseline_changes, record_baseline
        (self.repo / "guide.md").write_text("original", encoding="utf-8")
        task = self.tasks.snapshot("stop-task")
        record_baseline(self.store, "stop-task", task["contract"])
        (self.repo / "guide.md").write_text("edited", encoding="utf-8")
        task["contract"]["scope"]["resources"] = [str(self.repo.resolve())]
        self.assertIn("1 file(s) in the task scope differ from the recorded baseline: guide.md",
                      _baseline_changes(self.store, task))

    def test_stop_status_bounds_the_changed_file_list(self):
        from ase.tasks import STOP_CHANGE_LIST_LIMIT, record_baseline
        c = contract("many")
        c["unknowns"], c["stages"] = [], c["stages"][:1]
        c["scope"] = c["stages"][0]["scope"] = {"resources": ["docs"], "effects": ["file_write"]}
        c["evidence"][0]["path"] = "docs/guide.md"
        task = self.tasks.create(c)
        task = self.tasks.propose("many", {"summary": "Docs", "stage_ids": ["draft"]}, expected_revision=task["revision"])
        self.tasks.approve("many", proposal_hash=task["proposal_hash"], user_message_ref="test:authorized", approved_stage_ids=["draft"])
        (self.repo / "docs").mkdir()
        record_baseline(self.store, "many", self.tasks.snapshot("many")["contract"])
        for index in range(STOP_CHANGE_LIST_LIMIT + 5):
            (self.repo / "docs" / f"f{index:02d}.md").write_text("new", encoding="utf-8")
        self.controller.request_stop("many", reason="Stop during run")
        status = self.tasks.readiness("many", include_progress=True)["next_action"]
        self.assertIn(f"{STOP_CHANGE_LIST_LIMIT + 5} file(s) in the task scope", status)
        self.assertIn("docs/f19.md (and 5 more)", status)
        self.assertNotIn("docs/f20.md", status)

    def test_public_stop_cancels_running_role_before_any_edit(self):
        trusted = self.home / "runner"
        trusted.mkdir()
        fixture = self.base / "fixture.py"
        fixture.write_text(FIXTURE)
        with transaction(self.store.db) as db:
            write_record(db, "backend_config", "codex", {"executable": sys.executable, "executable_args": [str(fixture), "sleep", "codex"],
                         "trusted_cwd": str(trusted), "protocol_only": True}, expected_revision=0)
        helper = self.base / "run.py"
        helper.write_text("from ase.store import Store\nfrom ase.workflow import WorkflowController\nimport json\n"
                          f"s=Store({str(self.repo)!r},{str(self.home)!r})\n"
                          "print(json.dumps(WorkflowController(s).run('stop-task')),flush=True)\ns.close()\n")
        env = {**os.environ, "PYTHONPATH": str(ROOT)}
        process = subprocess.Popen([sys.executable, str(helper)], cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        def cleanup_process():
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=5)
        self.addCleanup(cleanup_process)
        marker = trusted / "descendant.pid"
        deadline = time.monotonic() + 10
        while not marker.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(marker.exists(), "Actual model fixture never reached its ready barrier")
        start = time.monotonic()
        stopped = subprocess.run([sys.executable, "-m", "ase", "--repo", str(self.repo), "--home", str(self.home), "task", "stop", "stop-task", "--reason", "Stop active model"],
                                 cwd=ROOT, env=env, capture_output=True, text=True, timeout=5)
        self.assertEqual(stopped.returncode, 0, stopped.stdout + stopped.stderr)
        stdout, stderr = process.communicate(timeout=5)
        self.assertEqual(process.returncode, 0, stdout + stderr)
        result = json.loads(stdout)
        self.assertEqual(result["closure"]["finish_reason"], "paused")
        self.assertLess(time.monotonic() - start, 5)
        self.assertFalse((self.repo / "guide.md").exists())
        receipts = list((self.store.root / "tasks/stop-task/sessions").glob("*/receipt.json"))
        self.assertEqual(len(receipts), 1)
        self.assertTrue(json.loads(receipts[0].read_text())["cancelled"])

    def test_pause_withholds_independent_verification_from_root_and_child_alike(self):
        parent = self.tasks.snapshot("stop-task")
        child = contract("stop-child")
        child.update(unknowns=[], stages=child["stages"][:1], parent_task_id="stop-task",
                     parent_stage_id=parent["contract"]["stages"][0]["id"])
        self.tasks.create(child)
        self.controller.request_stop("stop-child", reason="Pause the child only")
        with self.assertRaisesRegex(ValueError, "paused by a stop request"):
            self.controller._request("stop-child", "review", {"type": "object"}, {"stage_id": "draft"})
        with self.assertRaisesRegex(ValueError, "paused by a stop request"):
            self.controller.verify("stop-child")
        self.controller.request_stop("stop-task", reason="Pause the root")
        with self.assertRaisesRegex(ValueError, "paused by a stop request"):
            self.controller.verify("stop-task")
