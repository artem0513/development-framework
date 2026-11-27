"""Stable identity, explicit workspace attachment and recoverable registration."""
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
import uuid

from ase.projects import ProjectContext, ProjectRegistry
from ase.store import Store, default_home, now
from ase.timezones import TimezoneDataUnavailable


class ProjectTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="ase-project-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.home = self.base / "state"
        self.root = self.base / "workspace"
        self.root.mkdir()
        self.registry = ProjectRegistry(self.home)
        self.addCleanup(self.registry.close)

    def register(self, root=None, **changes):
        return self.registry.register(root or self.root, **{
            "kind": "document", "timezone": "Europe/Bucharest", **changes})

    def test_rename_keeps_identity_and_history(self):
        project = self.register()
        self.assertEqual(str(uuid.UUID(project.project_id)), project.project_id)
        self.assertEqual(project.storage_key, project.project_id)
        with closing(Store(project)) as store, store.db:
            store.db.execute("INSERT INTO knowledge_claims(topic,statement,observed_at,source_refs,created) VALUES(?,?,?,?,?)",
                             ("decision", "Preserve history", now(), "[]", now()))
            store.db.execute("INSERT INTO runtime_records VALUES(?,?,?,?,?)", (
                "source_observation", project.workspace_id, 1,
                json.dumps({"workspace_id": project.workspace_id, "version": "old"}), now()))
        moved = self.base / "renamed"
        self.root.rename(moved)
        attached = self.registry.attach(project.project_id, moved, expected_revision=1)
        self.assertEqual(attached.project_id, project.project_id)
        self.assertNotEqual(attached.workspace_id, project.workspace_id)
        self.assertEqual(self.registry.resolve(project.project_id).root, moved)
        self.assertEqual(self.registry.resolve(str(self.root)).workspace_id, project.workspace_id)
        with closing(Store(attached)) as store:
            self.assertEqual(store.db.execute("SELECT id,statement FROM knowledge_claims").fetchone()[1], "Preserve history")
            self.assertEqual(store.db.execute("SELECT count(*) FROM runtime_records WHERE kind='source_observation' AND key=?",
                                             (attached.workspace_id,)).fetchone()[0], 0)

    def test_additional_workspace_and_other_project_do_not_share_identity(self):
        project = self.register()
        additional = self.base / "another-worktree"
        additional.mkdir()
        attached = self.registry.attach(project.project_id, additional, expected_revision=1)
        self.assertEqual(self.registry.resolve(str(self.root)).project_id, attached.project_id)
        self.assertEqual(self.registry.resolve(str(self.root)).workspace_id, project.workspace_id)
        self.assertNotEqual(attached.workspace_id, project.workspace_id)
        other_root = self.base / "other-project"
        other_root.mkdir()
        other = self.register(other_root)
        with closing(Store(other)) as store:
            self.assertNotEqual(store.project_id, project.project_id)
            self.assertEqual(store.db.execute("SELECT count(*) FROM knowledge_claims").fetchone()[0], 0)

    def test_registration_is_idempotent_but_conflicting_registration_is_rejected(self):
        project = self.register()
        self.assertEqual(self.register(self.root / ".").project_id, project.project_id)
        with self.assertRaisesRegex(ValueError, "[Cc]onflict"):
            self.register(kind="software")
        other_root = self.base / "other"
        other_root.mkdir()
        other = self.register(other_root)
        with self.assertRaisesRegex(ValueError, "[Cc]onflict"):
            self.registry.attach(project.project_id, other.root, expected_revision=1)
        with self.assertRaisesRegex(ValueError, "[Ss]tale"):
            self.registry.attach(project.project_id, self.root, expected_revision=0)

    def test_registry_recovery_and_isolation(self):
        with patch.object(Store, "_initialize", side_effect=RuntimeError("power interrupted")):
            with self.assertRaisesRegex(RuntimeError, "power interrupted"):
                self.register()
        with closing(sqlite3.connect(self.home / "registry.sqlite3")) as db:
            project_id, status = db.execute("SELECT project_id,status FROM projects").fetchone()
        self.assertEqual(status, "PREPARING")
        with self.assertRaisesRegex(ValueError, "[Ii]nactive|PREPARING"):
            self.registry.resolve(project_id)
        recovered = self.register()
        self.assertEqual(recovered.project_id, project_id)
        self.assertEqual(self.registry.recover(), [])
        with closing(Store(recovered)) as store:
            self.assertEqual(store.db.execute("PRAGMA user_version").fetchone()[0], 5)

    def test_recovery_after_project_commit_does_not_allocate_second_identity(self):
        with patch.object(ProjectRegistry, "_activate", side_effect=RuntimeError("registry interrupted")):
            with self.assertRaisesRegex(RuntimeError, "registry interrupted"):
                self.register()
        with closing(sqlite3.connect(self.home / "registry.sqlite3")) as db:
            project_id = db.execute("SELECT project_id FROM projects").fetchone()[0]
        recovered = self.registry.recover()
        self.assertEqual([project.project_id for project in recovered], [project_id])
        self.assertEqual(self.register().project_id, project_id)

    def test_recovery_after_schema_commit_before_identity_commit(self):
        migrate = Store.migrate
        def interrupted(store):
            migrate(store)
            raise RuntimeError("identity not yet committed")
        with patch.object(Store, "migrate", interrupted):
            with self.assertRaisesRegex(RuntimeError, "identity not yet committed"):
                self.register()
        with closing(sqlite3.connect(self.home / "registry.sqlite3")) as db:
            project_id = db.execute("SELECT project_id FROM projects").fetchone()[0]
        try:
            recovered = self.registry.recover()
        except ValueError as exc:
            self.fail("Committed schema without identity must remain recoverable: " + str(exc))
        self.assertEqual([project.project_id for project in recovered], [project_id])
        with closing(Store(recovered[0])) as store:
            self.assertEqual(store.db.execute("PRAGMA user_version").fetchone()[0], 5)

    def test_concurrent_registration_has_one_identity(self):
        barrier = threading.Barrier(2)
        identities, errors = [], []
        def register():
            try:
                with closing(ProjectRegistry(self.home)) as registry:
                    barrier.wait(timeout=10)
                    identities.append(registry.register(self.root, kind="document", timezone="Europe/Bucharest").project_id)
            except Exception as exc:
                errors.append(str(exc))
        writers = [threading.Thread(target=register) for _ in range(2)]
        for writer in writers:
            writer.start()
        for writer in writers:
            writer.join(timeout=40)
        self.assertEqual(errors, [])
        self.assertEqual(len(identities), 2)
        self.assertEqual(len(set(identities)), 1)

    def test_non_git_workspace_opens_without_git_or_subprocess(self):
        with patch("subprocess.run", side_effect=AssertionError("Git must not run")):
            project = self.register()
            with closing(Store(project)) as store:
                self.assertIsInstance(store.project, ProjectContext)
                self.assertEqual(store.workspace_id, project.workspace_id)
                self.assertEqual(store.repo, self.root)

    def test_home_priority_is_explicit_then_environment_then_platform(self):
        environment_home = self.base / "environment-home"
        with patch.dict(os.environ, {"ASE_HOME": str(environment_home), "LOCALAPPDATA": str(self.base / "local")}, clear=False):
            self.assertEqual(default_home(), environment_home)
            with closing(Store(self.root, self.home)) as explicit:
                self.assertEqual(explicit.state_home, self.home)
            with closing(Store(self.root)) as environment:
                self.assertEqual(environment.state_home, environment_home)
                self.assertNotEqual(environment.project_id, explicit.project_id)
        with patch.dict(os.environ, {"LOCALAPPDATA": str(self.base / "local")}, clear=False):
            os.environ.pop("ASE_HOME", None)
            self.assertEqual(default_home(), self.base / "local" / "AutonomousSE")

    def test_state_home_inside_workspace_and_redirected_state_files_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "outside"):
            Store(self.root, self.root / "state")
        project = self.register()
        database = self.home / "projects" / project.storage_key / "memory.sqlite3"
        alias = self.base / "database-alias"
        os.link(database, alias)
        with self.assertRaisesRegex(ValueError, "State|state"):
            Store(project)
        alias.unlink()
        registry_alias = self.base / "registry-alias"
        os.link(self.home / "registry.sqlite3", registry_alias)
        with self.assertRaisesRegex(ValueError, "State|state"):
            ProjectRegistry(self.home)

    def test_active_registration_with_missing_or_foreign_database_fails_closed(self):
        project = self.register()
        database = self.home / "projects" / project.storage_key / "memory.sqlite3"
        with closing(sqlite3.connect(database)) as db, db:
            db.execute("UPDATE runtime_records SET payload=? WHERE kind='project_identity'",
                       (json.dumps({"project_id": str(uuid.uuid4()), "storage_key": project.storage_key}),))
        with self.assertRaisesRegex(ValueError, "identity|Identity"):
            Store(project)
        database.unlink()
        with self.assertRaisesRegex(ValueError, "[Mm]issing"):
            Store(project)

    def test_unknown_timezone_rejected_before_registry_identity_is_persisted(self):
        with self.assertRaises(TimezoneDataUnavailable) as unavailable:
            self.register(timezone="Europe/Buchresst")
        self.assertEqual(unavailable.exception.code, "TZ_DATA_UNAVAILABLE")
        for table in ("projects", "workspaces", "aliases"):
            self.assertEqual(self.registry.db.execute("SELECT count(*) FROM " + table).fetchone()[0], 0)
        self.assertFalse(self.registry.db.in_transaction)
        project = self.register()
        self.assertEqual(project.timezone, "Europe/Bucharest")

    def test_cli_timezone_failure_leaves_no_project_and_valid_retry_reports(self):
        checkout = Path(__file__).resolve().parents[1]
        def cli(*args):
            return subprocess.run([sys.executable, "-B", "-m", "ase", "--home", str(self.home), *map(str, args)],
                cwd=checkout, env={**os.environ, "PYTHONPATH": str(checkout), "PYTHONDONTWRITEBYTECODE": "1"},
                capture_output=True, text=True, timeout=30)
        invalid = cli("project", "register", "--root", self.root, "--kind", "document", "--timezone", "Europe/Buchresst")
        self.assertEqual(invalid.returncode, 3, invalid.stdout + invalid.stderr)
        self.assertIn("TZ_DATA_UNAVAILABLE", json.loads(invalid.stdout)["error"])
        self.assertEqual(self.registry.db.execute("SELECT count(*) FROM projects").fetchone()[0], 0)
        valid = cli("project", "register", "--root", self.root, "--kind", "document", "--timezone", "Europe/Bucharest")
        self.assertEqual(valid.returncode, 0, valid.stdout + valid.stderr)
        report = cli("--project", self.root, "worklog", "--from", "2026-11-14", "--until", "2026-11-15")
        self.assertEqual(report.returncode, 0, report.stdout + report.stderr)
        self.assertEqual(json.loads(report.stdout)["timezone"], "Europe/Bucharest")

    def test_compatibility_registration_preserves_existing_timezone(self):
        project = self.register(timezone="UTC")
        self.assertEqual(self.registry.for_root(self.root).timezone, "UTC")
        self.assertEqual(self.registry.resolve(project.project_id).timezone, "UTC")
