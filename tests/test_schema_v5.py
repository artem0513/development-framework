"""Additive, atomic migration of independently constructed legacy memory."""
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest

from ase.projects import ProjectRegistry
from ase.store import Store


V4_SCHEMA = """
CREATE TABLE projects(id TEXT PRIMARY KEY,repo TEXT NOT NULL,configuration TEXT NOT NULL,created TEXT NOT NULL);
CREATE TABLE tasks(id TEXT PRIMARY KEY,request TEXT NOT NULL,status TEXT NOT NULL,contract TEXT NOT NULL,created TEXT NOT NULL,updated TEXT NOT NULL);
CREATE TABLE sessions(id INTEGER PRIMARY KEY,task_id TEXT REFERENCES tasks(id),started TEXT NOT NULL,ended TEXT,summary TEXT NOT NULL,important_decisions TEXT NOT NULL DEFAULT '',unresolved_questions TEXT NOT NULL DEFAULT '');
CREATE TABLE decisions(id INTEGER PRIMARY KEY,task_id TEXT REFERENCES tasks(id),context TEXT NOT NULL,decision TEXT NOT NULL,rationale TEXT NOT NULL,created TEXT NOT NULL);
CREATE TABLE verification_runs(id INTEGER PRIMARY KEY,task_id TEXT REFERENCES tasks(id),fingerprint TEXT NOT NULL,outcome TEXT NOT NULL,artifact TEXT NOT NULL,created TEXT NOT NULL);
CREATE TABLE time_entries(id INTEGER PRIMARY KEY,task_id TEXT REFERENCES tasks(id),activity TEXT NOT NULL,started TEXT NOT NULL,ended TEXT NOT NULL,seconds REAL NOT NULL,source TEXT NOT NULL,confirmed INTEGER NOT NULL DEFAULT 0);
CREATE TABLE reports(id INTEGER PRIMARY KEY,task_id TEXT REFERENCES tasks(id),type TEXT NOT NULL,content TEXT NOT NULL,created TEXT NOT NULL);
CREATE TABLE transitions(id INTEGER PRIMARY KEY,task_id TEXT REFERENCES tasks(id),status TEXT NOT NULL,detail TEXT NOT NULL,created TEXT NOT NULL);
CREATE TABLE time_entry_audit(id INTEGER PRIMARY KEY,entry_id INTEGER NOT NULL REFERENCES time_entries(id),before_json TEXT NOT NULL,after_json TEXT NOT NULL,reason TEXT NOT NULL,created TEXT NOT NULL);
CREATE TABLE task_identities(task_id TEXT PRIMARY KEY REFERENCES tasks(id),locator TEXT NOT NULL UNIQUE);
CREATE TABLE checkpoints(task_id TEXT NOT NULL REFERENCES tasks(id),revision INTEGER NOT NULL,payload TEXT NOT NULL,observation TEXT NOT NULL,created TEXT NOT NULL,PRIMARY KEY(task_id,revision));
CREATE TABLE source_history(sequence INTEGER PRIMARY KEY,task_id TEXT NOT NULL REFERENCES tasks(id),source_id TEXT NOT NULL,payload TEXT NOT NULL,created TEXT NOT NULL);
CREATE TABLE knowledge_claims(id INTEGER PRIMARY KEY,topic TEXT NOT NULL,statement TEXT NOT NULL,observed_at TEXT NOT NULL,source_refs TEXT NOT NULL,task_id TEXT REFERENCES tasks(id),repository TEXT,repository_revision TEXT,supersedes INTEGER UNIQUE REFERENCES knowledge_claims(id),created TEXT NOT NULL);
PRAGMA user_version=4;
"""


class SchemaV5Tests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="ase-schema5-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.repo = self.base / "repo"
        self.repo.mkdir()
        self.home = self.base / "state"
        self.legacy_key = hashlib.sha256(os.path.normcase(str(self.repo)).encode()).hexdigest()[:24]
        self.folder = self.home / "projects" / self.legacy_key
        self.folder.mkdir(parents=True)
        self.database = self.folder / "memory.sqlite3"
        self.receipt = self.folder / "tasks" / "old" / "checks.json"
        self.receipt.parent.mkdir(parents=True)
        self.receipt.write_text('{"historical": true}', encoding="utf-8")
        with closing(sqlite3.connect(self.database)) as db, db:
            db.executescript(V4_SCHEMA)
            db.execute("INSERT INTO projects VALUES(?,?,?,?)", (self.legacy_key, str(self.repo), '{"legacy":true}', "2026-01-01"))
            db.execute("INSERT INTO tasks VALUES(?,?,?,?,?,?)", ("old", "Original request", "CHECKED", '{"max_attempts":2}', "2026-01-01", "2026-01-01"))
            db.execute("INSERT INTO verification_runs(task_id,fingerprint,outcome,artifact,created) VALUES(?,?,?,?,?)",
                       ("old", "fingerprint", "PASS", str(self.receipt), "2026-01-01"))
            db.execute("INSERT INTO knowledge_claims(id,topic,statement,observed_at,source_refs,created) VALUES(?,?,?,?,?,?)",
                       (17, "storage", "Historical claim", "2026-01-01T00:00:00Z", "[]", "2026-01-01"))

    def test_v4_migration_keeps_artifact_paths_and_is_recoverable(self):
        with closing(Store(self.repo, self.home)) as store:
            self.assertEqual(store.root, self.folder)
            self.assertNotEqual(store.project_id, self.legacy_key)
            self.assertEqual(store.project.storage_key, self.legacy_key)
            self.assertEqual(store.db.execute("PRAGMA user_version").fetchone()[0], 5)
            self.assertEqual(store.db.execute("SELECT artifact FROM verification_runs").fetchone()[0], str(self.receipt))
            self.assertEqual(store.db.execute("SELECT request FROM tasks").fetchone()[0], "Original request")
            self.assertEqual(store.db.execute("SELECT configuration FROM projects WHERE id=?", (self.legacy_key,)).fetchone()[0], '{"legacy":true}')
            validity = store.db.execute("SELECT payload FROM runtime_records WHERE kind='knowledge_validity' AND key='17'").fetchone()
            self.assertEqual(json.loads(validity[0])["validity"], "UNKNOWN")
        backups = list((self.folder / "backups").glob("*.sqlite3"))
        self.assertEqual(len(backups), 1)
        with closing(sqlite3.connect(backups[0])) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 4)
            self.assertEqual(db.execute("SELECT id,statement FROM knowledge_claims").fetchone(), (17, "Historical claim"))
            self.assertEqual(db.execute("SELECT count(*) FROM sqlite_master WHERE name='runtime_records'").fetchone()[0], 0)
        self.assertEqual(self.receipt.read_text(encoding="utf-8"), '{"historical": true}')
        with closing(Store(self.repo, self.home)) as second:
            self.assertEqual(second.root, self.folder)
        self.assertEqual(len(list((self.folder / "backups").glob("*.sqlite3"))), 1)

    def test_sql_failure_rolls_back_all_v5_changes_and_recovery_retries(self):
        # A real incompatible table forces SQL failure after migration starts.
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("CREATE TABLE runtime_records(unrelated TEXT)")
        with self.assertRaises(sqlite3.DatabaseError):
            Store(self.repo, self.home)
        with closing(sqlite3.connect(self.database)) as db, db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 4)
            self.assertEqual(db.execute("SELECT count(*) FROM sqlite_master WHERE name='runtime_events'").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT statement FROM knowledge_claims WHERE id=17").fetchone()[0], "Historical claim")
            db.execute("DROP TABLE runtime_records")
        with closing(ProjectRegistry(self.home)) as registry:
            with self.assertRaisesRegex(ValueError, "[Ii]nactive|PREPARING"):
                registry.resolve(str(self.repo))
            recovered = registry.recover()
            self.assertEqual(len(recovered), 1)
        with closing(Store(recovered[0])) as store:
            self.assertEqual(store.db.execute("PRAGMA user_version").fetchone()[0], 5)

    def test_v5_tables_enforce_ledger_uniqueness_and_discovery_task_binding(self):
        with closing(Store(self.repo, self.home)) as store:
            db = store.db
            with db:
                db.execute("INSERT INTO runtime_records VALUES('example','one',1,'{}','now')")
                db.execute("INSERT INTO runtime_events(event_key,kind,task_id,payload,created) VALUES('event','example','old','{}','now')")
                db.execute("INSERT INTO task_discoveries VALUES('old',1,'{}','now')")
            for sql in (
                "INSERT INTO runtime_records VALUES('example','one',2,'{}','now')",
                "INSERT INTO runtime_events(event_key,kind,payload,created) VALUES('event','example','{}','now')",
                "INSERT INTO task_discoveries VALUES('old',1,'{}','now')",
                "INSERT INTO task_discoveries VALUES('missing',1,'{}','now')",
            ):
                with self.subTest(sql=sql), self.assertRaises(sqlite3.IntegrityError), db:
                    db.execute(sql)

    def test_future_schema_is_rejected_without_rewriting_project(self):
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("PRAGMA user_version=6")
        with self.assertRaisesRegex(ValueError, "newer"):
            Store(self.repo, self.home)
        with closing(sqlite3.connect(self.database)) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 6)
            self.assertEqual(db.execute("SELECT id FROM projects").fetchone()[0], self.legacy_key)

    def test_foreign_legacy_database_is_rejected_before_migration_or_fact_access(self):
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("UPDATE projects SET repo=?", (str(self.base / "another-client"),))
        with self.assertRaisesRegex(ValueError, "identity|Identity|legacy|Legacy"):
            with closing(Store(self.repo, self.home)):
                pass
        with closing(sqlite3.connect(self.database)) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 4)
            self.assertEqual(db.execute("SELECT count(*) FROM sqlite_master WHERE name='runtime_records'").fetchone()[0], 0)
