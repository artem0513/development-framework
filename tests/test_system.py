"""Public-CLI integration tests for project state, time records, retention, the pre-push hook and the launcher.

The v1 execution engine (start/check/verify/run/gate/publish on v1 contracts) was retired; its receipt-protocol
tests went with it. v2 execution, gate and publishing are covered by their own suites.
"""
import json
from contextlib import closing
import os
from pathlib import Path
import sqlite3
import shutil
import subprocess
import sys
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]


class SystemTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ase-test-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.repo = self.base / "repo"
        self.repo.mkdir()
        self.home = self.base / "state"
        self.env = dict(os.environ, PYTHONPATH=str(ROOT), PYTHONDONTWRITEBYTECODE="1")
        self.git("init", "-b", "feature/test")
        self.git("config", "user.email", "test@example.invalid")
        self.git("config", "user.name", "ASE Tests")
        (self.repo / "app.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
        (self.repo / "test_app.py").write_text(
            "import unittest\nfrom app import add\nclass AddTests(unittest.TestCase):\n"
            "    def test_add(self): self.assertEqual(add(2, 3), 5)\n", encoding="utf-8")
        self.git("add", ".")
        self.git("commit", "-m", "baseline")
        self.cli("init")

    def git(self, *args):
        result = subprocess.run(["git", *args], cwd=self.repo, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def cli(self, *args, ok=(0,)):
        result = subprocess.run(
            [sys.executable, "-B", "-m", "ase", "--repo", str(self.repo), "--home", str(self.home), *args],
            cwd=ROOT, env=self.env, capture_output=True, text=True, timeout=60)
        self.assertIn(result.returncode, ok, result.stdout + result.stderr)
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError:
            self.fail("CLI did not return JSON: " + result.stdout + result.stderr)

    def start(self):
        contract = {
            "id": "task-one", "schema_version": 2, "profile": "software",
            "request": "Implement integer addition with executable evidence.", "risk": "MEDIUM",
            "acceptance": [{"id": "AC1", "expected": "2 + 3 returns 5", "mandatory": True, "commands": ["unit"]}],
            "evidence": [{"id": "unit", "argv": [sys.executable, "-m", "unittest", "discover", "-v"],
                          "kind": "test", "required": True, "parser": "unittest"}],
            "scope": {"resources": ["app.py", "test_app.py"], "effects": []},
            "stages": [{"id": "verify", "acceptance_ids": ["AC1"]}],
        }
        source = self.base / "task.json"
        source.write_text(json.dumps(contract), encoding="utf-8")
        result = self.cli("start", str(source))
        self.task = result["task_id"]
        self.state = next(self.home.rglob("memory.sqlite3")).parent / "tasks" / self.task
        self.state.mkdir(parents=True, exist_ok=True)
        return result

    def project_protects(self, *patterns):
        folder = self.repo / ".devframework"
        folder.mkdir(exist_ok=True)
        (folder / "protected-paths.txt").write_text("# shared, cross-cutting code\n" + "\n".join(patterns) + "\n", encoding="utf-8")
        self.git("add", ".")
        self.git("commit", "-m", "project protected paths")

    def test_project_protected_paths_parse_globs_and_refuse_escapes(self):
        from ase.core import project_protected_paths
        self.assertEqual(project_protected_paths(self.repo), [])
        self.project_protects("src/Jobs/**", "  # comment", "src\\Data\\*DbContext.cs", "./src/Infra/")
        self.assertEqual(project_protected_paths(self.repo), ["src/Jobs/**", "src/Data/*DbContext.cs", "src/Infra/*"])
        (self.repo / ".devframework" / "protected-paths.txt").write_bytes("\ufeffsrc/Jobs/*\n".encode("utf-8"))
        self.assertEqual(project_protected_paths(self.repo), ["src/Jobs/*"])
        (self.repo / ".devframework" / "protected-paths.txt").write_text("../outside\n", encoding="utf-8")
        with self.assertRaises(ValueError):
            project_protected_paths(self.repo)

    def test_task_and_context_persist_across_processes(self):
        self.start()
        first = self.cli("status", self.task)
        second = self.cli("status", self.task)
        self.assertEqual((first["contract"], first["state"]), (second["contract"], second["state"]))
        self.assertIn(self.task, json.dumps(self.cli("context")))
        self.cli("backup")
        self.assertTrue(any(self.home.rglob("*.db")) or any(self.home.rglob("*.sqlite*")))

    def test_project_isolation(self):
        self.start()
        original = self.repo
        self.repo = self.base / "other-repo"
        self.repo.mkdir()
        self.git("init", "-b", "feature/other")
        self.cli("init")
        result = self.cli("status", self.task, ok=(2, 3))
        self.assertNotIn("Implement integer addition", json.dumps(result))
        self.repo = original
        self.assertIn(self.task, json.dumps(self.cli("status", self.task)))

    def test_human_and_agent_time_remain_separate(self):
        self.start()
        for source, confirmed in (("agent", False), ("human", False), ("human", True)):
            args = ["time", self.task, "--activity", "review", "--start", "2026-11-01T10:00:00+02:00",
                    "--end", "2026-11-01T10:30:00+02:00", "--source", source]
            if confirmed:
                args.append("--confirmed")
            self.cli(*args)
        report = self.cli("daily", "--date", "2026-11-01")
        self.assertEqual(report["agent_seconds"], 1800)
        self.assertEqual(report["draft_human_seconds"], 1800)
        self.assertEqual(report["confirmed_human_seconds"], 1800)

    def install_hook(self):
        result = subprocess.run([sys.executable, str(ROOT / "scripts" / "install_hook.py"),
            "--repo", str(self.repo), "--task", self.task, "--home", str(self.home)],
            capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_native_git_hook_blocks_main_divergent_head_and_failed_gate(self):
        self.start()
        remote = self.base / "hook-remote.git"
        self.git("init", "--bare", str(remote))
        self.git("remote", "add", "origin", str(remote))
        self.install_hook()
        self.git("checkout", "-b", "main")
        push = subprocess.run(["git", "push", "origin", "main"], cwd=self.repo,
                              env=self.env, capture_output=True, text=True)
        self.assertNotEqual(push.returncode, 0)
        self.assertIn("feature branches only", push.stdout + push.stderr)
        self.git("checkout", "feature/test")
        push = subprocess.run(["git", "push", "origin", "HEAD:refs/heads/main"], cwd=self.repo,
                              env=self.env, capture_output=True, text=True)
        self.assertNotEqual(push.returncode, 0)
        self.assertIn("destination must match", push.stdout + push.stderr)
        self.git("branch", "feature/old")
        (self.repo / "new.txt").write_text("new revision", encoding="utf-8")
        self.git("add", ".")
        self.git("commit", "-m", "new head")
        push = subprocess.run(["git", "push", "origin", "feature/old:refs/heads/feature/test"], cwd=self.repo,
                              env=self.env, capture_output=True, text=True)
        self.assertNotEqual(push.returncode, 0)
        self.assertIn("only verified HEAD", push.stdout + push.stderr)
        push = subprocess.run(["git", "push", "origin", "feature/test"], cwd=self.repo,
                              env=self.env, capture_output=True, text=True)
        self.assertNotEqual(push.returncode, 0)
        self.assertIn("BLOCKED", push.stdout + push.stderr)
        self.assertFalse(self.git("--git-dir", str(remote), "for-each-ref").strip())

    def test_existing_git_hook_is_preserved(self):
        self.start()
        hook = self.repo / ".git" / "hooks" / "pre-push"
        hook.write_text("#!/bin/sh\nexit 42\n", encoding="utf-8")
        result = subprocess.run([sys.executable, str(ROOT / "scripts" / "install_hook.py"),
            "--repo", str(self.repo), "--task", self.task, "--home", str(self.home)],
            capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(hook.read_text(encoding="utf-8"), "#!/bin/sh\nexit 42\n")

    def test_weekly_preserves_human_agent_attribution_and_date_window(self):
        self.start()
        for day, source, confirmed in (("2026-10-25", "human", True), ("2026-10-26", "human", True),
                                       ("2026-11-01", "agent", False), ("2026-11-01", "human", False)):
            args = ["time", self.task, "--activity", "review", "--start", day + "T10:00:00+02:00",
                    "--end", day + "T10:30:00+02:00", "--source", source]
            if confirmed:
                args.append("--confirmed")
            self.cli(*args)
        report = self.cli("weekly", "--date", "2026-11-01")
        self.assertEqual(report["agent_seconds"], 1800)
        self.assertEqual(report["draft_human_seconds"], 1800)
        self.assertEqual(report["confirmed_human_seconds"], 1800)
        self.assertEqual(sum(len(day["entries"]) for day in report["days"]), 3)

    def test_time_correction_is_audited_and_confirmation_resets(self):
        self.start()
        entry = self.cli("time", self.task, "--activity", "review", "--start", "2026-11-01T10:00:00+02:00",
                         "--end", "2026-11-01T10:30:00+02:00", "--source", "human", "--confirmed")
        result = self.cli("correct-time", str(entry["entry_id"]), "--end", "2026-11-01T11:00:00+02:00",
                          "--reason", "User corrected measured end time")
        self.assertEqual(result["entry"]["seconds"], 3600)
        self.assertFalse(result["entry"]["confirmed"])
        report = self.cli("daily", "--date", "2026-11-01")
        self.assertEqual(report["confirmed_human_seconds"], 0)
        self.assertEqual(report["draft_human_seconds"], 3600)
        database = next(self.home.rglob("memory.sqlite3"))
        with closing(sqlite3.connect(database)) as db:
            row = db.execute("SELECT before_json,after_json,reason FROM time_entry_audit WHERE id=?", (result["audit_id"],)).fetchone()
        self.assertEqual(json.loads(row[0])["seconds"], 1800)
        self.assertEqual(json.loads(row[1])["seconds"], 3600)
        self.assertEqual(row[2], "User corrected measured end time")
        # Another project's numeric entry IDs must not access this project's record.
        original = self.repo
        self.repo = self.base / "isolated-corrections"
        self.repo.mkdir()
        self.git("init", "-b", "feature/other")
        self.cli("init")
        self.cli("correct-time", str(entry["entry_id"]), "--activity", "leak", ok=(3,))
        self.repo = original

    def test_session_summary_decisions_and_unresolved_questions_persist(self):
        self.start()
        self.cli("session-summary", self.task, "--summary", "Reviewed API boundaries",
                 "--decision", "Use integer arithmetic", "--unresolved", "Clarify overflow behavior")
        context = self.cli("context")
        self.assertIn("Reviewed API boundaries", json.dumps(context["sessions"]))
        self.assertIn("Use integer arithmetic", json.dumps(context["sessions"]))
        self.assertIn("Clarify overflow behavior", json.dumps(context["sessions"]))

    def test_v1_memory_migration_backs_up_and_preserves_tasks(self):
        self.start()
        database = next(self.home.rglob("memory.sqlite3"))
        # Reconstruct the actual v1 schema from a freshly populated database.
        with closing(sqlite3.connect(database)) as db, db:
            db.execute("DROP TABLE knowledge_claims")
            db.execute("DROP TABLE time_entry_audit")
            db.execute("DROP TABLE task_identities")
            db.execute("DROP TABLE checkpoints")
            db.execute("DROP TABLE source_history")
            db.execute("ALTER TABLE sessions DROP COLUMN important_decisions")
            db.execute("ALTER TABLE sessions DROP COLUMN unresolved_questions")
            db.execute("PRAGMA user_version=1")
        self.assertIn(self.task, json.dumps(self.cli("status", self.task)))
        with closing(sqlite3.connect(database)) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 5)
            self.assertEqual(db.execute("SELECT request FROM tasks WHERE id=?", (self.task,)).fetchone()[0],
                             "Implement integer addition with executable evidence.")
        backups = list(database.parent.glob("backups/*.sqlite3"))
        self.assertTrue(backups)
        with closing(sqlite3.connect(backups[0])) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 1)

    def test_old_raw_evidence_expires_but_structured_memory_survives(self):
        self.start()
        self.cli("session-summary", self.task, "--summary", "Retain the concise project decision")
        old_log, handoff = self.state / "ac.session.log", self.state / "handoff.json"
        unknown, current = self.state / "user-notes.log", self.state / "review.session.log"
        for path in (old_log, handoff, unknown, current):
            path.write_text("potential source excerpt", encoding="utf-8")
        expired = time.time() - 40 * 86400
        for path in (old_log, handoff, unknown):
            os.utime(path, (expired, expired))
        context = self.cli("context")
        self.assertFalse(old_log.exists())
        self.assertFalse(handoff.exists())
        self.assertTrue(current.exists())
        self.assertTrue(unknown.exists())
        self.assertIn("Retain the concise project decision", json.dumps(context))

    def test_active_operation_preserves_old_logs_during_retention(self):
        self.start()
        log = self.state / "ac.session.log"
        log.write_text("running evidence", encoding="utf-8")
        expired = time.time() - 40 * 86400
        os.utime(log, (expired, expired))
        (self.state / "operation.lock").write_text(json.dumps({"pid": os.getpid()}), encoding="utf-8")
        self.cli("context")
        self.assertTrue(log.exists())

    def test_cycle_verbose_retention_preserves_receipts_unknown_files_and_hardlinks(self):
        self.start()
        cycle = self.state / "cycles" / "0001-example"
        cycle.mkdir(parents=True)
        logs = [cycle / "ac.session.log", cycle / "review.session.log",
                cycle / "implementation-1.log", cycle / "handoff.json"]
        receipt, unknown = cycle / "verify.json", cycle / "unrelated.log"
        for path in [*logs, receipt, unknown]:
            path.write_text("retained or expired evidence", encoding="utf-8")
        outside = self.base / "outside.log"
        outside.write_text("never delete the hardlink alias", encoding="utf-8")
        linked = cycle / "implementation-2.log"
        os.link(outside, linked)
        expired = time.time() - 40 * 86400
        for path in [*logs, receipt, unknown, linked]:
            os.utime(path, (expired, expired))
        self.cli("context")
        self.assertTrue(all(not p.exists() for p in logs))
        self.assertTrue(receipt.exists())
        self.assertTrue(unknown.exists())
        self.assertTrue(linked.exists())
        self.assertEqual(outside.read_text(), "never delete the hardlink alias")

    def test_invalid_log_retention_is_blocked(self):
        self.env["ASE_LOG_RETENTION_DAYS"] = "0"
        self.cli("context", ok=(3,))

    def test_installed_launcher_canonicalizes_state_home_through_directory_alias(self):
        # Windows runners may supply TEMP through an 8.3 alias; a directory
        # reparse alias reproduces the same lexical-vs-canonical path boundary.
        alias = self.base / "launcher-base-alias"
        try:
            alias.symlink_to(self.base, target_is_directory=True)
        except OSError:
            if os.name != "nt":
                raise
            result = subprocess.run(["cmd", "/c", "mklink", "/J", str(alias), str(self.base)],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.base = alias
        self.test_installed_launcher_recovers_missing_environment_and_preserves_explicit_state_home()

    def test_installed_launcher_recovers_missing_environment_and_preserves_explicit_state_home(self):
        codex, skills = self.base / "codex", self.base / "skills"
        install_env = dict(self.env, LOCALAPPDATA=str(self.base / "local"))
        result = subprocess.run([sys.executable, str(ROOT / "scripts" / "install.py"),
            "--global", "--home", str(codex), "--skills", str(skills)], env=install_env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        launch_env = dict(install_env)
        for name in ("HOME", "USERPROFILE", "CODEX_HOME"):
            launch_env.pop(name, None)
        if os.name == "nt":
            powershell = shutil.which("pwsh") or shutil.which("powershell")
            self.assertIsNotNone(powershell)
            command = [powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(codex / "bin" / "ase.ps1")]
        else:
            command = [str(codex / "bin" / "ase")]
        def launcher(*args):
            result = subprocess.run([*command, "--repo", str(self.repo), *args], env=launch_env,
                                    cwd=self.base, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            return json.loads(result.stdout)
        def assert_state_home(state_dir, target):
            actual = Path(state_dir)
            # Engine returns canonical paths. Compare to the same filesystem
            # identity instead of a runner TEMP alias spelling (e.g. RUNNER~1).
            self.assertEqual(actual, actual.resolve())
            self.assertTrue(actual.is_relative_to(target.resolve()),
                            f"State path {actual} is outside canonical target {target.resolve()}")
        initialized = launcher("init")
        self.assertEqual(Path(initialized["root"]), self.repo.resolve())
        for args, target in ((("--home", str(self.base / "explicit")), self.base / "explicit"),
                             (("--home=" + str(self.base / "absolute-equals"),), self.base / "absolute-equals"),
                             (("--home=equals",), self.base / "equals")):
            initialized_at_target = launcher(*args, "init")
            self.assertEqual(Path(initialized_at_target["root"]), self.repo.resolve())
            self.assertTrue((target.resolve() / "registry.sqlite3").is_file())
        script = ("import os, unittest\nclass EnvironmentTests(unittest.TestCase):\n"
                  "    def test_codex_home(self): self.assertIsNone(os.environ.get('CODEX_HOME'))\n"
                  "    def test_home(self): self.assertIsNone(os.environ.get('HOME'))\n")
        if os.name == "nt":
            script += "    def test_userprofile(self): self.assertIsNone(os.environ.get('USERPROFILE'))\n"
        (self.repo / "test_launcher_env.py").write_text(script, encoding="utf-8")
        contract = {"id": "launcher", "request": "Verify environment in actual child process",
                    "acceptance": [{"id": "AC1", "expected": "Codex has an explicit home", "commands": ["unit"]}],
                    "commands": [{"id": "unit", "argv": [sys.executable, "-m", "unittest", "test_launcher_env", "-v"],
                                  "kind": "test", "parser": "unittest", "required": True}]}
        contract.update(schema_version=2, profile="software", scope={"resources": ["test_launcher_env.py"], "effects": []},
                        evidence=contract.pop("commands"), stages=[{"id": "verify", "acceptance_ids": ["AC1"]}])
        path = self.base / "launcher-task.json"
        path.write_text(json.dumps(contract), encoding="utf-8")
        launcher("start", str(path))
        proposal = self.base / "launcher-proposal.json"
        proposal.write_text(json.dumps({"summary": "Run the explicit child environment tests", "stage_ids": ["verify"]}))
        proposed = launcher("task", "propose", "launcher", "--file", str(proposal), "--expected-revision", "1")
        launcher("task", "approve", "launcher", "--proposal-hash", proposed["proposal_hash"], "--user-message-ref", "test:explicit-approved-environment-check", "--stage", "verify")
        receipt = launcher("check", "launcher")
        self.assertEqual(receipt["results"][0]["exit_code"], 0)


class LegacyHistoryTests(unittest.TestCase):
    """Retired v1 engine: v1 contracts are refused, recorded v1 history stays readable and untouched."""
    setUp = SystemTests.setUp
    git = SystemTests.git
    cli = SystemTests.cli

    def test_retired_v1_flags_are_rejected(self):
        for args in (("verify", "t", "--parallel"), ("run", "t", "--parallel"), ("resume", "t", "--recover")):
            with self.subTest(args=args):
                result = subprocess.run([sys.executable, "-B", "-m", "ase", "--repo", str(self.repo), "--home", str(self.home), *args],
                                        cwd=ROOT, env=self.env, capture_output=True, text=True, timeout=60)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertEqual(json.loads(result.stdout)["status"], "USAGE_ERROR")
                self.assertIn("unrecognized arguments", result.stdout)

    def test_v1_contract_is_refused_without_creating_a_task(self):
        source = self.base / "v1.json"
        source.write_text(json.dumps({"id": "old-style", "request": "Implement addition",
                                      "acceptance": [{"id": "AC1", "expected": "2 + 3 returns 5", "commands": ["unit"]}],
                                      "commands": [{"id": "unit", "argv": [sys.executable, "-m", "unittest"], "kind": "test"}]}),
                          encoding="utf-8")
        result = self.cli("start", str(source), ok=(3,))
        self.assertIn("v1 task contracts are no longer accepted", result["error"])
        self.assertNotIn("old-style", json.dumps(self.cli("status")))
        source.write_text(json.dumps({"schema_version": 3, "id": "future"}), encoding="utf-8")
        self.assertIn("Unsupported contract schema_version 3", self.cli("start", str(source), ok=(3,))["error"])
        source.write_text("[]", encoding="utf-8")
        self.assertIn("JSON object", self.cli("start", str(source), ok=(3,))["error"])

    def test_recorded_v1_report_is_shown_as_stored_without_reevaluation(self):
        from ase.store import Store
        from legacy_v1 import make_legacy_task
        gate = {"task_id": "old", "status": "APPROVED", "reasons": [], "fingerprint": "recorded"}
        with closing(Store(self.repo, self.home)) as store:
            folder = make_legacy_task(store, {"id": "old", "request": "Historical v1 task",
                                              "acceptance": [{"id": "AC1", "expected": "kept", "commands": []}], "commands": []},
                                      status="APPROVED", files={"gate.json": gate, "ac.json": {"criteria": []}})
        before = {path.name: path.read_bytes() for path in folder.iterdir()}
        database = next(self.home.rglob("memory.sqlite3"))
        with closing(sqlite3.connect(database)) as db:
            rows = tuple(db.execute("SELECT * FROM " + table).fetchall() for table in ("tasks", "transitions", "verification_runs", "reports"))
        report = self.cli("report", "old")
        self.assertEqual(report["status"], "LEGACY_READ_ONLY")
        self.assertEqual(report["recorded_gate"], gate)
        self.assertEqual(report["acceptance"], {"criteria": []})
        self.assertIsNone(report["review"])
        self.assertIn("not re-evaluated", report["note"])
        self.assertEqual({path.name: path.read_bytes() for path in folder.iterdir()}, before)
        with closing(sqlite3.connect(database)) as db:
            self.assertEqual(tuple(db.execute("SELECT * FROM " + table).fetchall() for table in ("tasks", "transitions", "verification_runs", "reports")), rows)
        for command in ("check", "verify", "run", "gate", "publish"):
            with self.subTest(command=command):
                self.assertIn("v2", json.dumps(self.cli(command, "old", ok=(3,))))


class InstallationTests(unittest.TestCase):
    def test_preserves_existing_configuration_and_backs_up_managed_changes(self):
        with tempfile.TemporaryDirectory(prefix="ase-install-") as temp:
            codex = Path(temp) / "codex"
            skills = Path(temp) / "skills"
            codex.mkdir()
            config = codex / "config.toml"
            config.write_text('[desktop]\nexisting = "retain me"\n', encoding="utf-8")
            agents = codex / "AGENTS.md"
            original = "# Personal rules\nPreserve user choices.\n"
            agents.write_text(original, encoding="utf-8")
            def install():
                result = subprocess.run([sys.executable, str(ROOT / "scripts" / "install.py"),
                    "--global", "--home", str(codex), "--skills", str(skills)], capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                return json.loads(result.stdout)
            install()
            self.assertTrue((codex / "autonomous-se" / "runtime" / "scripts" / "vendor_skills.py").is_file())
            self.assertTrue(agents.read_text(encoding="utf-8").startswith(original.rstrip()))
            self.assertEqual(config.read_text(encoding="utf-8"), '[desktop]\nexisting = "retain me"\n')
            self.assertTrue(any(p.read_text(encoding="utf-8") == original
                                for p in (codex / "backups").rglob("AGENTS.md")))
            profile = codex / "autonomous.config.toml"
            profile.write_text("# customized managed profile\n", encoding="utf-8")
            install()
            install()
            self.assertEqual(agents.read_text(encoding="utf-8").count("<!-- autonomous-se managed begin -->"), 1)
            self.assertEqual(profile.read_text(encoding="utf-8"), "# customized managed profile\n")
            self.assertFalse(json.loads((codex / "autonomous-se" / "installation.json").read_text())["remote_push_enabled"])


class ProcessExecutionTests(unittest.TestCase):
    def test_completed_parent_does_not_wait_for_child_stdout_to_close(self):
        from ase.core import execute
        with tempfile.TemporaryDirectory(prefix="ase-child-") as temp:
            script = ("import subprocess,sys; subprocess.Popen([sys.executable,'-c',"
                      "'import time; time.sleep(5)']); print('parent done')")
            started = time.monotonic()
            result = execute([sys.executable, "-c", script], ROOT, Path(temp) / "execution.log", timeout=1)
            elapsed = time.monotonic() - started
            self.assertLess(elapsed, 2)
            self.assertEqual(result["exit_code"], 0)
            self.assertFalse(result["timed_out"])


if __name__ == "__main__":
    unittest.main()
