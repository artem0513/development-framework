"""Behavioral tests for project-local handoff and source coverage."""
import json
from unittest.mock import patch
from contextlib import closing
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import sys
import threading
import unittest

from ase.store import Store, now
from ase.continuity import Continuity


class ContinuityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ase-continuity-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.repo = self.base / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-b", "feature/test", str(self.repo)], check=True, capture_output=True)
        self.store = Store(self.repo, self.base / "state")
        self.addCleanup(self.store.close)
        for task in ("one", "two"):
            with self.store.db:
                self.store.db.execute("INSERT INTO tasks VALUES(?,?,?,?,?,?)", (task, "request", "NEW", "{}", now(), now()))
        self.api = Continuity(self.store)
        self.payload = {"stage": "implementation", "summary": "Changed API", "next_action": "Run unit checks", "blockers": [], "source_refs": []}

    def test_safe_observation_allows_exact_selected_repo_under_different_sandbox_owner(self):
        from ase import inspection
        environment = inspection._environment()
        environment["GIT_TEST_ASSUME_DIFFERENT_OWNER"] = "1"
        with patch("ase.inspection._environment", return_value=environment):
            restored = self.api.restore("one")
        self.assertEqual(restored["current"]["repository"], str(self.repo.resolve()))
        self.assertEqual(restored["current"]["branch"], "feature/test")

    def test_checkpoint_compare_and_swap_preserves_history_and_rejects_stale_writer(self):
        self.assertEqual(self.api.snapshot("one")["revision"], 0)
        saved = self.api.checkpoint("one", 0, self.payload)
        self.assertEqual(saved["revision"], 1)
        second = Store(self.repo, self.base / "state")
        self.addCleanup(second.close)
        with self.assertRaisesRegex(ValueError, "Stale"):
            Continuity(second).checkpoint("one", 0, {**self.payload, "summary": "stale"})
        saved2 = Continuity(second).checkpoint("one", 1, {**self.payload, "summary": "fresh"})
        self.assertEqual(saved2["revision"], 2)
        rows = self.store.db.execute("SELECT payload FROM checkpoints WHERE task_id='one' ORDER BY revision").fetchall()
        self.assertEqual([json.loads(r[0])["summary"] for r in rows], ["Changed API", "fresh"])

    def test_restore_reports_unborn_commit_then_content_drift_without_mutation(self):
        saved = self.api.checkpoint("one", 0, self.payload)
        self.assertIsNone(saved["observation"]["commit"])
        self.assertFalse(self.api.restore("one")["drift"])
        (self.repo / "new.txt").write_text("new", encoding="utf-8")
        result = self.api.restore("one")
        self.assertIn("fingerprint", result["drift"])
        self.assertEqual(result["checkpoint"]["payload"]["next_action"], "Run unit checks")
        self.assertEqual(result["current"]["branch"], "feature/test")
        self.assertIn("new.txt", result["current"]["worktree"])
        self.assertEqual(self.api.snapshot("one")["revision"], 1)

    def test_task_identity_is_unique_inside_project_and_scope_is_isolated(self):
        self.api.checkpoint("one", 0, {**self.payload, "task_url": "https://example.invalid/issues/1"})
        self.api.checkpoint("one", 1, self.payload)
        with self.store.db:
            self.store.db.execute("INSERT INTO decisions(task_id,context,decision,rationale,created) VALUES(?,?,?,?,?)", ("one", "design", "Use parser", "Evidence", now()))
            self.store.db.execute("INSERT INTO sessions(task_id,started,summary,important_decisions,unresolved_questions) VALUES(?,?,?,?,?)", ("one", now(), "resumed", "Use parser", "Clarify limit"))
        restored = self.api.restore("one")
        self.assertEqual(restored["task_url"], "https://example.invalid/issues/1")
        self.assertEqual(restored["task"]["request"], "request")
        self.assertEqual(restored["decisions"][0]["decision"], "Use parser")
        self.assertEqual(restored["sessions"][0]["unresolved_questions"], "Clarify limit")
        with self.assertRaisesRegex(ValueError, "identity"):
            self.api.checkpoint("two", 0, {**self.payload, "task_url": "https://example.invalid/issues/1"})
        other_repo = self.base / "other"
        other_repo.mkdir()
        other = Store(other_repo, self.base / "state")
        self.addCleanup(other.close)
        self.assertNotEqual(other.project_id, self.store.project_id)
        self.assertEqual(other.db.execute("SELECT count(*) FROM checkpoints").fetchone()[0], 0)

    def test_missing_next_action_unknown_fields_and_negative_revision_are_rejected(self):
        for payload in ({**self.payload, "next_action": ""}, {**self.payload, "approval": True}, {**self.payload, "blockers": "none"}):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                self.api.checkpoint("one", 0, payload)
        with self.assertRaises(ValueError):
            self.api.checkpoint("one", -1, self.payload)

    def source(self, **changes):
        return {"id": "spec", "locator": "local:spec.md", "revision": "sha256:abc", "extent": 10, "unit": "lines",
                "ranges": [[1, 5]], "gaps": [], "status": "acquired", **changes}

    def record(self, task_id, payload):
        sources = self.api.coverage(task_id)["sources"]
        sequence = next((s["sequence"] for s in sources if s["id"] == payload["id"]), 0)
        return self.api.source_record(task_id, payload, sequence)

    def test_empty_source_inventory_is_unconfigured_and_never_complete(self):
        result = self.api.coverage("one")
        self.assertEqual(result["status"], "UNCONFIGURED")
        self.assertFalse(result["complete"])

    def test_source_requires_full_read_before_analyzed_and_preserves_revision_history(self):
        self.record("one", self.source())
        self.assertEqual(self.api.coverage("one")["sources"][0]["unread_ranges"], [[6, 10]])
        with self.assertRaisesRegex(ValueError, "complete"):
            self.record("one", self.source(status="analyzed"))
        full = self.source(ranges=[[4, 10], [1, 5]], status="analyzed", analysis="outputs/spec-findings.md")
        self.record("one", full)
        self.assertTrue(self.api.coverage("one")["complete"])
        self.record("one", self.source(revision="sha256:new", ranges=[]))
        result = self.api.coverage("one")
        self.assertFalse(result["complete"])
        self.assertEqual(result["sources"][0]["unread_ranges"], [[1, 10]])
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM source_history").fetchone()[0], 3)

    def test_source_gaps_block_success_and_bad_ranges_and_missing_blocker_rejected(self):
        self.record("one", self.source(ranges=[[1, 10]], gaps=["linked appendix unavailable"], status="fully_read"))
        self.assertFalse(self.api.coverage("one")["complete"])
        for changes in ({"ranges": [[0, 2]]}, {"ranges": [[3, 2]]}, {"ranges": [[1, 11]]},
                        {"extent": True}, {"status": "blocked"}, {"status": "fully_read"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.record("one", self.source(**changes))
        self.record("one", self.source(status="blocked", blocker="Owner must provide appendix"))
        self.assertEqual(self.api.coverage("one")["status"], "BLOCKED")

    def test_source_revision_or_locator_change_cannot_reuse_old_ranges_implicitly(self):
        self.record("one", self.source(ranges=[[1, 10]], status="analyzed", analysis="findings.md"))
        self.record("one", self.source(revision="new", ranges=[], status="discovered"))
        self.assertFalse(self.api.coverage("one")["complete"])
        self.assertEqual(self.api.coverage("two")["sources"], [])

    def test_stale_source_writer_cannot_overwrite_new_revision_or_gaps(self):
        initial = self.record("one", self.source(ranges=[[1, 10]], status="analyzed", analysis="analysis.md"))
        stale = initial["sources"][0]["sequence"]
        updated = self.api.source_record("one", self.source(revision="new", ranges=[], status="blocked", blocker="Missing revised document"), stale)
        with self.assertRaisesRegex(ValueError, "Stale source"):
            self.api.source_record("one", self.source(ranges=[[1, 10]], status="analyzed", analysis="old.md"), stale)
        result = self.api.coverage("one")
        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual(result["sources"][0]["revision"], "new")
        self.assertEqual(result["sources"][0]["sequence"], updated["sources"][0]["sequence"])
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM source_history").fetchone()[0], 2)

    def test_simultaneous_writers_have_exactly_one_winner(self):
        barrier = threading.Barrier(2)
        results = []
        def writer(label):
            store = Store(self.repo, self.base / "state")
            try:
                barrier.wait(timeout=10)
                try:
                    Continuity(store).checkpoint("one", 0, {**self.payload, "summary": label})
                    results.append("saved")
                except ValueError as error:
                    results.append(str(error))
            finally:
                store.close()
        workers = [threading.Thread(target=writer, args=(label,)) for label in ("first", "second")]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=20)
            self.assertFalse(worker.is_alive())
        self.assertEqual(results.count("saved"), 1)
        self.assertEqual(sum("Stale" in value for value in results), 1)
        self.assertEqual(self.api.snapshot("one")["revision"], 1)

    def test_cli_checkpoint_restore_and_coverage_round_trip_and_stale_exit(self):
        payload = self.base / "handoff.json"
        payload.write_text(json.dumps(self.payload), encoding="utf-8")
        prefix = [sys.executable, "-m", "ase", "--repo", str(self.repo), "--home", str(self.base / "state")]
        def cli(*args, expected=0):
            result = subprocess.run([*prefix, *args], capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
            return json.loads(result.stdout)
        self.assertEqual(cli("continuity-snapshot", "one")["revision"], 0)
        self.assertEqual(cli("checkpoint", "one", "--expected-revision", "0", "--file", str(payload))["revision"], 1)
        self.assertEqual(cli("restore", "one")["checkpoint"]["payload"]["next_action"], "Run unit checks")
        self.assertIn("Stale", cli("checkpoint", "one", "--expected-revision", "0", "--file", str(payload), expected=3)["error"])
        self.assertEqual(cli("coverage", "one")["status"], "UNCONFIGURED")
        payload.write_text(json.dumps(self.source()), encoding="utf-8")
        self.assertEqual(cli("source-record", "one", "--expected-sequence", "0", "--file", str(payload))["status"], "PARTIAL")

    def test_v2_migration_preserves_data_and_backs_up_original_schema(self):
        self.store.close()
        database = self.store.root / "memory.sqlite3"
        with closing(sqlite3.connect(database)) as db, db:
            for table in ("knowledge_claims", "task_identities", "checkpoints", "source_history"):
                db.execute("DROP TABLE " + table)
            db.execute("PRAGMA user_version=2")
        self.store = Store(self.repo, self.base / "state")
        self.addCleanup(self.store.close)
        self.assertEqual(self.store.db.execute("PRAGMA user_version").fetchone()[0], 5)
        self.assertEqual(self.store.task("one")["request"], "request")
        backups = list((self.store.root / "backups").glob("*.sqlite3"))
        self.assertEqual(len(backups), 1)
        with closing(sqlite3.connect(backups[0])) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 2)


if __name__ == "__main__":
    unittest.main()
