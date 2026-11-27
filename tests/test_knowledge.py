"""Dated project knowledge behavior, not semantic truth validation."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest

from ase.store import Store, now
from ase.knowledge import Knowledge


class KnowledgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ase-knowledge-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.repo = self.base / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-b", "feature/test", str(self.repo)], check=True, capture_output=True)
        self.store = Store(self.repo, self.base / "state")
        self.addCleanup(self.store.close)
        self.api = Knowledge(self.store)
        self.claim = {"topic": "storage", "statement": "Use SQLite", "observed_at": "2026-01-01T12:00:00+00:00", "source_refs": ["local:decision.txt"]}

    def test_dated_claim_roundtrip_and_history_remains_after_supersession(self):
        first = self.api.record(self.claim)
        second = self.api.record(dict(self.claim, statement="Use a remote store", observed_at="2026-01-02T12:00:00+00:00", supersedes=first["id"]))
        rows = self.api.query("storage")["claims"]
        self.assertEqual([r["statement"] for r in rows], ["Use SQLite", "Use a remote store"])
        self.assertEqual(rows[0]["superseded_by"], second["id"])
        self.assertIsNone(rows[1]["superseded_by"])
        self.assertIn("historical", self.api.query()["authority"].lower())

    def test_supersession_rejects_stale_wrong_topic_unknown_and_older_date(self):
        first = self.api.record(self.claim)
        for change in ({"topic": "other"}, {"observed_at": "2025-01-01T12:00:00Z"}, {"supersedes": 999}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.api.record({**self.claim, "supersedes": first["id"], **change})
        self.api.record(dict(self.claim, supersedes=first["id"], statement="Changed"))
        with self.assertRaisesRegex(ValueError, "superseded"):
            self.api.record(dict(self.claim, supersedes=first["id"]))
        self.assertEqual(len(self.api.query()["claims"]), 2)

    def test_invalid_dates_sources_revision_and_credentials_rejected_without_writes(self):
        for change in ({"observed_at": "2026-01-01"}, {"observed_at": "bad"}, {"source_refs": []}, {"source_refs": "url"}, {"repository_revision": "a" * 40}, {"repository": "local:repo", "repository_revision": "bad"}, {"statement": "token=ghp_" + "x" * 40}, {"unknown": True}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.api.record(dict(self.claim, **change))
        self.assertEqual(self.api.query()["claims"], [])

    def test_claim_fields_are_concise_and_reference_count_bounded(self):
        for change in ({"statement": "x" * 4001}, {"topic": "x" * 201}, {"source_refs": ["local:x"] * 21}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.api.record(dict(self.claim, **change))
        self.assertEqual(self.api.query()["claims"], [])

    def test_repository_revision_task_binding_and_project_isolation(self):
        with self.store.db:
            self.store.db.execute("INSERT INTO tasks VALUES(?,?,?,?,?,?)", ("one", "request", "NEW", "{}", now(), now()))
        record = self.api.record(dict(self.claim, repository="https://example.invalid/o/r", repository_revision="a" * 40, task_id="one"))
        self.assertEqual(record["repository_revision"], "a" * 40)
        with self.assertRaises(ValueError):
            self.api.record(dict(self.claim, task_id="missing"))
        other = self.base / "other"
        other.mkdir()
        with closing(Store(other, self.base / "state")) as store:
            self.assertEqual(Knowledge(store).query()["claims"], [])
            with self.assertRaises(ValueError):
                Knowledge(store).record(dict(self.claim, supersedes=record["id"]))

    def test_concurrent_superseders_have_one_winner(self):
        first = self.api.record(self.claim)
        barrier = threading.Barrier(2)
        outcomes = []
        def writer():
            with closing(Store(self.repo, self.base / "state")) as store:
                barrier.wait()
                try:
                    Knowledge(store).record(dict(self.claim, supersedes=first["id"]))
                    outcomes.append("saved")
                except ValueError:
                    outcomes.append("stale")
        threads = [threading.Thread(target=writer) for _ in range(2)]
        for thread in threads: thread.start()
        for thread in threads: thread.join(timeout=20)
        self.assertEqual(sorted(outcomes), ["saved", "stale"])
        self.assertEqual(len(self.api.query()["claims"]), 2)

    def test_lint_distinguishes_parallel_claims_and_missing_local_reference(self):
        self.api.record(self.claim)
        self.api.record(dict(self.claim, statement="Alternative decision", source_refs=["https://example.invalid/doc"]))
        findings = self.api.lint()["findings"]
        self.assertEqual({x["kind"] for x in findings}, {"parallel_claims", "missing_local_reference"})
        self.assertFalse(self.api.lint()["semantic_validation"])
        (self.repo / "decision.txt").write_text("Evidence", encoding="utf-8")
        self.assertNotIn("missing_local_reference", [x["kind"] for x in self.api.lint()["findings"]])

    def test_lint_never_reads_outside_project_and_empty_inventory_is_unconfigured(self):
        self.assertEqual(self.api.lint()["status"], "UNCONFIGURED")
        self.api.record(dict(self.claim, source_refs=["local:../outside.txt"]))
        self.assertEqual(self.api.lint()["findings"][0]["kind"], "out_of_scope_reference")

    def test_v3_migration_preserves_claims_predecessor_data_and_backs_up(self):
        self.store.close()
        database = self.store.root / "memory.sqlite3"
        with closing(sqlite3.connect(database)) as db, db:
            db.execute("DROP TABLE knowledge_claims")
            db.execute("INSERT INTO decisions(task_id,context,decision,rationale,created) VALUES(NULL,?,?,?,?)", ("context", "Old decision", "why", now()))
            db.execute("PRAGMA user_version=3")
        self.store = Store(self.repo, self.base / "state")
        self.addCleanup(self.store.close)
        self.assertEqual(self.store.db.execute("PRAGMA user_version").fetchone()[0], 5)
        self.assertEqual(self.store.db.execute("SELECT decision FROM decisions").fetchone()[0], "Old decision")
        backups = list((self.store.root / "backups").glob("*.sqlite3"))
        self.assertEqual(len(backups), 1)
        with closing(sqlite3.connect(backups[0])) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 3)

    def test_cli_records_queries_and_lints_without_network(self):
        payload = self.base / "claim.json"
        payload.write_text(json.dumps(self.claim), encoding="utf-8")
        def cli(*args):
            run = subprocess.run([sys.executable, "-m", "ase", "--repo", str(self.repo), "--home", str(self.base / "state"), *args], capture_output=True, text=True)
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
            return json.loads(run.stdout)
        self.assertEqual(cli("knowledge-record", "--file", str(payload))["id"], 1)
        self.assertEqual(cli("knowledge-query", "--topic", "storage")["claims"][0]["statement"], "Use SQLite")
        self.assertEqual(cli("knowledge-lint")["status"], "REVIEW_NEEDED")
