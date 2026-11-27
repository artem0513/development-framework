"""Project-selected local state stays isolated from process and global context."""
from contextlib import closing, contextmanager
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from ase.projects import ProjectRegistry, resolve_state_home
from ase.store import Store, now

ROOT = Path(__file__).resolve().parents[1]


@contextmanager
def working_directory(path):
    old = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(old)


class ProjectLocalStateTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="ase-local-state-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.first = self.local_project("first")
        self.second = self.local_project("second", timezone="Europe/Bucharest", retention=17)
        self.shared = self.base / "hostile-global"
        self.environment = dict(os.environ, ASE_HOME=str(self.shared), PYTHONPATH=str(ROOT), PYTHONDONTWRITEBYTECODE="1")
        self.env_patch = patch.dict(os.environ, self.environment)
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)
        os.environ.pop("ASE_LOG_RETENTION_DAYS", None)

    def local_project(self, name, timezone="UTC", retention=9):
        root = self.base / name
        framework = root / ".devframework"
        framework.mkdir(parents=True)
        (framework / ".gitignore").write_text("/local/\n", encoding="utf-8")
        (framework / "config.toml").write_text(
            'schema_version = 1\nmode = "project-local"\ntargets = ["codex", "claude", "copilot"]\n'
            f'timezone = "{timezone}"\nlog_retention_days = {retention}\n', encoding="utf-8")
        (root / "nested" / "deeper").mkdir(parents=True)
        return root

    def cli(self, *args, cwd=None, ok=0):
        result = subprocess.run([sys.executable, "-m", "ase", *map(str, args)], cwd=cwd or self.base,
                                env=self.environment, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, ok, result.stdout + result.stderr)
        self.assertEqual(result.stderr, "")
        return json.loads(result.stdout)

    def test_selection_precedence_and_nested_cwd(self):
        with working_directory(self.first / "nested" / "deeper"):
            self.assertEqual(resolve_state_home(), self.first / ".devframework" / "local")
            self.assertEqual(resolve_state_home(project=self.second), self.second / ".devframework" / "local")
            self.assertEqual(resolve_state_home(self.shared, project=self.second), self.shared)
            self.assertEqual(resolve_state_home(project="nonexistent-project-id"), self.shared)

    def test_two_local_stores_preserve_independent_memory_and_defaults(self):
        identities = []
        for root, zone, retention in ((self.first, "UTC", 9), (self.second, "Europe/Bucharest", 17)):
            with closing(Store(root)) as store:
                identities.append(store.project_id)
                self.assertEqual(store.state_home, root / ".devframework" / "local")
                self.assertEqual(store.project.timezone, zone)
                self.assertEqual(store.log_retention_days, retention)
                with store.db:
                    store.db.execute("INSERT INTO decisions(task_id,context,decision,rationale,created) VALUES(NULL,?,?,?,?)",
                                     (root.name, "Keep separate history", "project-local", now()))
        self.assertNotEqual(*identities)
        with working_directory(self.first / "nested" / "deeper"), closing(Store(".")) as store:
            self.assertEqual(store.repo, self.first)
            self.assertEqual(store.project_id, identities[0])
            self.assertEqual([row[0] for row in store.db.execute("SELECT context FROM decisions")], ["first"])
        self.assertFalse(self.shared.exists())

    def test_local_retention_ignores_global_environment_and_shared_retains_it(self):
        with patch.dict(os.environ, {"ASE_LOG_RETENTION_DAYS": "25"}):
            with closing(Store(self.first)) as store:
                self.assertEqual(store.log_retention_days, 9)
            legacy = self.base / "legacy-retention"
            legacy.mkdir()
            with closing(Store(legacy)) as store:
                self.assertEqual(store.log_retention_days, 25)

    def test_cli_selects_repo_instead_of_unrelated_cwd_and_restarts(self):
        first = self.cli("--repo", self.first, "status", cwd=self.second / "nested")
        second = self.cli("--repo", self.second, "status", cwd=self.first / "nested")
        repeated = self.cli("status", cwd=self.first / "nested" / "deeper")
        self.assertEqual(first["project_id"], repeated["project_id"])
        self.assertNotEqual(first["project_id"], second["project_id"])
        by_alias = self.cli("--project", self.second, "status", cwd=self.first)
        self.assertEqual(by_alias["project_id"], second["project_id"])
        self.assertFalse(self.shared.exists())

    def test_local_uuid_selectors_use_cwd_or_explicit_repo(self):
        project = self.cli("--repo", self.first, "status")
        implicit = self.cli("--project", project["project_id"], "status", cwd=self.first / "nested")
        explicit = self.cli("--repo", self.first, "--project", project["project_id"], "status", cwd=self.second)
        self.assertEqual(implicit["project_id"], project["project_id"])
        self.assertEqual(explicit["project_id"], project["project_id"])
        self.assertFalse(self.shared.exists())

    def test_project_register_uses_selected_root_config_timezone(self):
        registered = self.cli("project", "register", "--root", self.second, "--kind", "document", cwd=self.first)
        self.assertEqual(registered["root"], str(self.second))
        self.assertEqual(registered["timezone"], "Europe/Bucharest")
        self.assertEqual(Path(registered["state_home"]), self.second / ".devframework" / "local")

    def test_legacy_uuid_selector_uses_global_registry_only_outside_local_project(self):
        legacy = self.base / "legacy"
        legacy.mkdir()
        with closing(ProjectRegistry(self.shared)) as registry:
            project = registry.register(legacy, kind="document", timezone="UTC")
        self.cli("--project", project.project_id, "status", cwd=self.first, ok=3)
        result = self.cli("--project", project.project_id, "status", cwd=self.base)
        self.assertEqual(result["project_id"], project.project_id)

    def test_relocated_local_state_requires_explicit_attach_and_fresh_workspace_evidence(self):
        from ase.knowledge import Knowledge
        (self.first / "guide.txt").write_bytes(b"Preserved source")
        source = {"source_id": "guide", "locator": "local:guide.txt", "kind": "file",
                  "version": "sha256:" + hashlib.sha256(b"Preserved source").hexdigest(),
                  "availability": "AVAILABLE", "observed_at_utc": now()}
        claim_data = {"topic": "guide", "statement": "Preserve this decision", "kind": "user_decision",
                      "observed_at": now(), "source_refs": ["local:guide.txt"]}
        with closing(Store(self.first)) as store:
            project = store.project
            source["workspace_id"] = project.workspace_id
            api = Knowledge(store)
            claim = api.record(claim_data)
            api.record_observations([source], [{"claim_id": claim["id"], "source_id": "guide",
                                               "observed_version": source["version"]}],
                                    expected_revision=api.retrieve(scope={}, refresh=False)["revision"])
            self.assertEqual(api.retrieve(scope={})["claims"][0]["validity"], "CURRENT")
        moved = self.base / "first-relocated"
        self.first.rename(moved)
        with self.assertRaisesRegex(ValueError, "[Aa]ttach|reattach|[Rr]ecovery"):
            with closing(Store(moved)):
                pass
        self.cli("--repo", moved, "status", ok=3)
        with closing(ProjectRegistry(moved / ".devframework" / "local")) as registry:
            self.assertEqual(registry.db.execute("SELECT count(*) FROM projects").fetchone()[0], 1)
            with self.assertRaisesRegex(ValueError, "[Aa]ttach|reattach|[Rr]ecovery"):
                registry.register(moved, kind="document", timezone="UTC")
        self.cli("project", "attach", project.project_id, "--root", moved,
                 "--expected-revision", "0", ok=3)
        attached = self.cli("project", "attach", project.project_id, "--root", moved,
                            "--expected-revision", str(project.revision))
        self.assertEqual(attached["project_id"], project.project_id)
        self.assertNotEqual(attached["workspace_id"], project.workspace_id)
        self.assertEqual(attached["revision"], project.revision + 1)
        with closing(Store(moved)) as store:
            self.assertEqual(store.project_id, project.project_id)
            self.assertEqual(store.project.storage_key, project.storage_key)
            api = Knowledge(store)
            history = api.retrieve(scope={})["claims"]
            self.assertEqual(history[0]["id"], claim["id"])
            self.assertEqual(history[0]["statement"], claim_data["statement"])
            self.assertEqual(history[0]["validity"], "UNKNOWN")
            with self.assertRaisesRegex(ValueError, "another workspace"):
                api.record_observations([source], [], expected_revision=0)
            fresh_claim = api.record({**claim_data, "statement": "Fresh workspace evidence"})
            api.record_observations([{**source, "workspace_id": store.workspace_id}],
                                    [{"claim_id": fresh_claim["id"], "source_id": "guide",
                                      "observed_version": source["version"]}],
                                    expected_revision=api.retrieve(scope={}, refresh=False)["revision"])
            claims = {row["id"]: row for row in api.retrieve(scope={})["claims"]}
            self.assertEqual(claims[claim["id"]]["validity"], "UNKNOWN")
            self.assertEqual(claims[fresh_claim["id"]]["validity"], "CURRENT")
        with closing(ProjectRegistry(moved / ".devframework" / "local")) as registry:
            self.assertEqual(registry.db.execute("SELECT count(*) FROM projects").fetchone()[0], 1)
            self.assertEqual(registry.db.execute("SELECT count(*) FROM workspaces").fetchone()[0], 2)

    def test_local_home_cannot_store_another_project_or_attachment(self):
        home = self.first / ".devframework" / "local"
        with closing(Store(self.first)) as store:
            project = store.project
        with self.assertRaises(ValueError):
            with closing(Store(self.second, home)):
                pass
        with closing(ProjectRegistry(home)) as registry:
            with self.assertRaises(ValueError):
                registry.register(self.second, kind="document", timezone="UTC")
            with self.assertRaises(ValueError):
                registry.attach(project.project_id, self.second, expected_revision=1)
        with closing(ProjectRegistry(home)) as registry:
            self.assertEqual(registry.db.execute("SELECT count(*) FROM projects").fetchone()[0], 1)

    def test_arbitrary_inside_project_home_fails_before_creating_registry(self):
        for home in (self.first / "state", self.first / ".devframework" / "local" / "nested"):
            with self.subTest(home=home), self.assertRaises(ValueError):
                Store(self.first, home)
            self.assertFalse(home.exists())

    def test_redirected_state_home_alias_is_rejected(self):
        with closing(Store(self.first)) as store:
            home = store.state_home
        alias = self.base / "state-alias"
        try:
            alias.symlink_to(home, target_is_directory=True)
        except OSError:
            if os.name != "nt":
                self.skipTest("Directory links unavailable")
            linked = subprocess.run(["cmd", "/c", "mklink", "/J", str(alias), str(home)], capture_output=True)
            if linked.returncode:
                self.skipTest("Directory links unavailable")
        with self.assertRaises(ValueError):
            with closing(Store(self.first, alias)):
                pass
        self.cli("--repo", self.first, "--home", alias, "status", ok=3)

    def test_dated_journal_history_survives_restarts_and_stays_project_local(self):
        from ase.journal import Journal
        from ase.tasks import TaskService
        from test_journal import contract
        for root in (self.first, self.second):
            with closing(Store(root)) as store:
                TaskService(store).create(contract("history"))
                Journal(store).append({"kind": "decision", "task_id": "history", "summary": root.name,
                                       "occurred_at_utc": "2026-11-12T10:00:00+00:00"}, event_key="dated-event")
        for root in (self.first, self.second):
            report = self.cli("--repo", root, "worklog", "--start", "2026-11-12", "--until", "2026-11-13")
            summaries = [event["summary"] for event in report["events"] if event["event_key"] == "dated-event"]
            self.assertEqual(summaries, [root.name])

    @unittest.skipUnless(os.name == "nt", "requires Windows DPAPI")
    def test_two_local_vaults_import_external_files_and_persist_separate_profiles(self):
        for root in (self.first, self.second):
            source = self.base / (root.name + "-import.json")
            source.write_text(json.dumps({"schema_version": 1, "credentials": [{
                "id": root.name, "target": "https://example.com", "kind": "http-bearer",
                "secrets": {"token": "project-local-test-canary"}}]}), encoding="utf-8")
            self.cli("--repo", root, "credentials", "import", "--file", source)
        for root in (self.first, self.second):
            profiles = self.cli("--repo", root, "credentials", "list")["credentials"]
            self.assertEqual([profile["id"] for profile in profiles], [root.name])
            self.assertNotIn(b"project-local-test-canary", (root / ".devframework" / "local" / "credentials" / "vault.json").read_bytes())
        self.assertFalse(self.shared.exists())

    def test_cli_rechecks_private_ignore_protection_before_state_or_vault_creation(self):
        (self.first / ".devframework" / ".gitignore").unlink()
        self.cli("--repo", self.first, "status", ok=3)
        self.cli("--repo", self.first, "credentials", "list", ok=3)
        self.assertFalse((self.first / ".devframework" / "local").exists())

    @unittest.skipUnless(os.name == "nt", "requires Windows DPAPI")
    def test_credential_import_rejects_selected_nonlocal_workspace_input(self):
        legacy = self.base / "legacy-credentials"
        legacy.mkdir()
        source = legacy / "secrets.json"
        source.write_text('{"schema_version": 1, "credentials": []}', encoding="utf-8")
        self.cli("--project", legacy, "credentials", "import", "--file", source, cwd=self.first, ok=3)
        self.assertFalse((self.shared / "credentials" / "vault.json").exists())

    @unittest.skipUnless(os.name == "nt", "requires Windows DPAPI")
    def test_credentials_select_local_project_without_registering_it(self):
        result = self.cli("--project", self.second, "credentials", "list", cwd=self.first)
        self.assertEqual(result["credentials"], [])
        self.assertFalse((self.second / ".devframework" / "local" / "registry.sqlite3").exists())
        self.assertFalse(self.shared.exists())

    @unittest.skipUnless(os.name == "nt", "requires Windows DPAPI")
    def test_credential_import_still_rejects_selected_workspace_input(self):
        source = self.second / "secrets.json"
        source.write_text('{"schema_version": 1, "credentials": []}', encoding="utf-8")
        self.cli("--project", self.second, "credentials", "import", "--file", source, cwd=self.first, ok=3)
        self.assertFalse((self.second / ".devframework" / "local" / "credentials" / "vault.json").exists())


if __name__ == "__main__":
    unittest.main()
