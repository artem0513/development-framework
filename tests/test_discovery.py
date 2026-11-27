"""Behavior checks for attributed intake and scoped, nonexecuting discovery."""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from ase import discovery


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ase-discovery-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "project"
        self.root.mkdir()
        self.project = SimpleNamespace(root=self.root, project_id="project-one", kind="documents")

    def write(self, path, content):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return target

    def meeting(self):
        return {
            "source_refs": [{"id": "meeting", "kind": "meeting", "reference": "meeting:2026-11-12", "observed_at": "2026-11-12T10:00:00Z", "timestamp": "00:03:20-00:05:12"}],
            "outcomes": [{"id": "outcome", "text": "Prepare a deployment recommendation", "source_refs": ["meeting"]}],
            "decisions": [],
            "proposals": [{"id": "suggestion", "text": "Participant suggested publishing immediately", "source_refs": ["meeting"]}],
            "contradictions": [],
            "gaps": [{"id": "decision-gap", "text": "The final decision was inaudible", "source_refs": ["meeting"], "required": True}],
        }

    def discover(self, resources, **kwargs):
        return discovery.discover_capabilities(self.project, scope={"resources": resources, **kwargs}, backend_config={})

    # Promoting a quoted suggestion, dropping its timestamp or closing a source gap must fail.
    def test_meeting_suggestion_is_not_decision(self):
        payload = self.meeting()
        result = discovery.validate_intake(payload)
        self.assertEqual(result["decisions"], [])
        self.assertEqual(result["proposals"][0]["id"], "suggestion")
        self.assertEqual(result["source_refs"][0]["timestamp"], "00:03:20-00:05:12")
        self.assertEqual(result["gaps"][0]["required"], True)
        self.assertFalse(result["authorization_granted"])
        self.assertFalse(result["semantic_validation"])
        result["gaps"][0]["required"] = False
        self.assertTrue(payload["gaps"][0]["required"])

    def test_private_git_case_variant_and_internal_alias_are_not_read(self):
        private = self.write(".git/config", "private configuration")
        with self.assertRaises(ValueError):
            self.discover([".GIT/config"])
        alias = self.root / "harmless"
        if os.name == "nt":
            made = subprocess.run(["cmd", "/c", "mklink", "/J", str(alias), str(private.parent)], capture_output=True)
            self.assertEqual(made.returncode, 0, made.stderr)
        else:
            alias.symlink_to(private.parent, target_is_directory=True)
        self.addCleanup(lambda: alias.rmdir() if os.name == "nt" else alias.unlink())
        with self.assertRaises(ValueError):
            self.discover(["harmless/config"])

    def test_stale_revision_names_expected_found_and_fix_command(self):
        from contextlib import closing
        from ase.store import Store
        from ase.tasks import TaskService
        repo = Path(self.temp.name) / "repo"
        repo.mkdir()
        (repo / "guide.md").write_text("guide", encoding="utf-8")
        with closing(Store(repo, Path(self.temp.name) / "state")) as store:
            TaskService(store).create({"id": "doc", "schema_version": 2, "profile": "document", "request": "Write a guide",
                "scope": {"resources": ["guide.md"], "effects": ["file_write"]},
                "acceptance": [{"id": "A", "expected": "Guide exists", "evidence_ids": ["guide"]}],
                "evidence": [{"id": "guide", "kind": "artifact", "path": "guide.md"}],
                "stages": [{"id": "draft", "acceptance_ids": ["A"]}]})
            payload = discovery.discover_capabilities(store.project, scope={"resources": ["guide.md"]}, backend_config={})
            with self.assertRaisesRegex(ValueError, r"--expected-revision 3 but task doc has discovery revision 0; .*"
                                        r"`ase task discover doc --expected-revision 0`"):
                discovery.record_discovery(store, "doc", payload, expected_revision=3)

    def test_malformed_intake_rejected_and_incomplete_meeting_retained(self):
        incomplete = self.meeting()
        incomplete["outcomes"] = []
        self.assertTrue(discovery.validate_intake(incomplete)["gaps"])
        for change in ({"source_refs": []}, {"gaps": []}, {"approved": True},
                       {"decisions": [{"id": "d", "text": "Publish", "source_refs": ["missing"]}]},
                       {"source_refs": [{"id": "meeting", "kind": "meeting", "reference": "meeting:x", "observed_at": "2026-11-12"}]}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                discovery.validate_intake({**incomplete, **change})
        for value in (None, [], "meeting"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                discovery.validate_intake(value)

    def test_intake_rejects_duplicate_ids_raw_transcripts_and_credentials(self):
        for change in ({"transcript": "raw recording text"},
                       {"proposals": [{"id": "outcome", "text": "Other", "source_refs": ["meeting"]}]},
                       {"gaps": [{"id": "g", "text": "token=ghp_" + "x" * 30, "source_refs": ["meeting"], "required": True}]},
                       {"source_refs": [{"id": "meeting", "kind": "meeting", "reference": "meeting:x", "observed_at": "2026-11-12T10:00:00Z"}]}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                discovery.validate_intake({**self.meeting(), **change})

    # A whole-tree read or accepting an unexplained expansion must fail.
    def test_discovery_is_scoped_and_expands_by_reason(self):
        self.write("app/package.json", json.dumps({"dependencies": {"shared": "file:../shared"}, "scripts": {"test": "node test.js"}}))
        self.write("shared/package.json", '{"name":"shared"}')
        canary = self.write("private/canary.txt", "UNRELATED PRIVATE CONTENT")
        original = Path.read_bytes
        def read(path):
            if path == canary:
                raise AssertionError("Unrelated canary was read")
            return original(path)
        with patch.object(Path, "read_bytes", read):
            result = self.discover(["app/package.json"])
        paths = {item["path"] for item in result["resources"]}
        self.assertIn("shared/package.json", paths)
        self.assertNotIn("private/canary.txt", paths)
        expansion = next(item for item in result["dependencies"] if item["path"] == "shared")
        self.assertIn("shared", expansion["reason"])
        self.assertEqual(expansion["from"], "app/package.json")
        with self.assertRaises(ValueError):
            self.discover(["app/package.json"], dependencies=[{"path": "private"}])

    def test_shared_metadata_inherits_later_required_origin(self):
        from ase.source_coverage import dependency_policy
        self.write("guide.md", "Guide")
        self.write("shared/first.txt", "Optional background")
        self.write("shared/second.txt", "Required publication input")
        self.write("shared/AGENTS.md", "Instructions for both inputs")
        optional = {"path": "shared/first.txt", "reason": "Optional background", "required": False}
        required = {"path": "shared/second.txt", "reason": "Required input", "blocking_stage_ids": ["publish"]}
        for declarations in ([optional, required], [required, optional]):
            with self.subTest(order=[item["path"] for item in declarations]):
                result = self.discover(["guide.md"], dependencies=declarations)
                governing = [item for item in result["dependencies"] if item["path"] == "shared/AGENTS.md"]
                self.assertEqual(dependency_policy(governing), {"required": True, "blocking_stage_ids": ["publish"]})

    def test_shared_metadata_unions_independent_required_stage_origins(self):
        from ase.source_coverage import dependency_policy
        self.write("guide.md", "Guide")
        self.write("shared/first.txt", "Draft input")
        self.write("shared/second.txt", "Publication input")
        self.write("shared/AGENTS.md", "Instructions for both stages")
        result = self.discover(["guide.md"], dependencies=[
            {"path": "shared/first.txt", "reason": "Draft input", "blocking_stage_ids": ["draft"]},
            {"path": "shared/second.txt", "reason": "Publication input", "blocking_stage_ids": ["publish"]}])
        governing = [item for item in result["dependencies"] if item["path"] == "shared/AGENTS.md"]
        self.assertEqual(dependency_policy(governing), {"required": True, "blocking_stage_ids": ["draft", "publish"]})

    def test_project_test_conventions_override_heuristics(self):
        self.write("pyproject.toml", '[tool.pytest.ini_options]\ntestpaths = ["specs"]\naddopts = "-q"\n')
        self.write("tests/test_legacy.py", "raise RuntimeError('must not import')")
        self.write("specs/test_new.py", "def test_ok(): pass")
        result = self.discover(["."])
        tests = [item for item in result["candidate_checks"] if item["kind"] == "test"]
        self.assertEqual(len(tests), 1)
        self.assertEqual(tests[0]["argv"], [sys.executable, "-m", "pytest", "-v", "specs"])
        self.assertEqual(tests[0]["parser"], "pytest")
        self.assertFalse(tests[0]["selected"])

    def test_package_manager_bun_lock_and_ambiguous_solutions(self):
        for package_manager, lock, expected in (("pnpm@10.1.0", "bun.lock", "pnpm"),
                                                 (None, "bun.lockb", "bun"),
                                                 ("bun@1.3.0", "package-lock.json", "bun")):
            with self.subTest(manager=package_manager):
                package = {"scripts": {"test": "bun test", "build": "echo build"}}
                if package_manager:
                    package["packageManager"] = package_manager
                folder = package_manager.split("@")[0] if package_manager else "locked-bun"
                self.write(folder + "/package.json", json.dumps(package))
                self.write(folder + "/" + lock, "lock")
                result = self.discover([folder])
                self.assertEqual(result["candidate_checks"][0]["argv"][0], expected)
        self.write("First.sln", "Microsoft Visual Studio Solution File")
        self.write("Second.slnx", "<Solution />")
        result = self.discover(["First.sln", "Second.slnx"])
        targets = {item["argv"][-1] for item in result["candidate_checks"] if item["argv"][0] == "dotnet"}
        self.assertEqual(targets, {"First.sln", "Second.slnx"})
        self.assertTrue(all(not item["selected"] for item in result["candidate_checks"]))
        self.assertTrue(any("multiple" in item.lower() for item in result["limitations"]))

    def test_non_git_unknown_access_and_escaped_scope(self):
        target = self.write("requirements.txt", "pytest==8.0.0")
        original = Path.read_bytes
        def denied(path):
            if path == target:
                raise PermissionError("fixture access denied")
            return original(path)
        with patch.object(Path, "read_bytes", denied):
            result = self.discover(["requirements.txt", "missing.txt"])
        self.assertFalse(result["git_repository"])
        self.assertEqual({item["status"] for item in result["resources"]}, {"UNKNOWN", "MISSING"})
        self.assertTrue(result["limitations"])
        for path in ("../outside", str(self.root.parent), ".git/config"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.discover([path])

    # Executing a version probe or repository script would create marker files.
    def test_read_only_probes_never_execute_install_or_modify(self):
        marker = self.root / "executed.txt"
        self.write("package.json", json.dumps({"scripts": {"test": 'python -c "open(\'executed.txt\',\'w\').write(\'yes\')"'}}))
        executable = self.write("fake-tool.py", "from pathlib import Path\nPath(" + repr(str(marker)) + ").write_text('executed')\n")
        before = {str(p.relative_to(self.root)): (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns) for p in self.root.rglob("*") if p.is_file()}
        result = discovery.discover_capabilities(self.project, scope={"resources": ["package.json"]}, backend_config={"tools": {"fake": {"executable": str(executable), "invocation_allowed": False}}})
        after = {str(p.relative_to(self.root)): (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns) for p in self.root.rglob("*") if p.is_file()}
        self.assertEqual(before, after)
        self.assertFalse(marker.exists())
        fake = next(item for item in result["tools"] if item["name"] == "fake")
        self.assertTrue(fake["available"])
        self.assertFalse(fake["invocation_allowed"])
        self.assertEqual(fake["version"], "unknown")
        self.assertEqual(fake["independent_run"], "unknown")
        self.assertTrue(result["candidate_checks"][0]["requires_inspection"])

    def test_malformed_configuration_and_unreadable_test_metadata_do_not_invent_checks(self):
        self.write("pyproject.toml", "[tool.pytest.ini_options\ninvalid")
        self.write("tests/test_old.py", "raise RuntimeError('must not import')")
        result = self.discover(["."])
        self.assertEqual(result["candidate_checks"], [])
        self.assertTrue(any("Malformed Python" in item for item in result["limitations"]))
        self.write("pyproject.toml", "[tool.pytest]\nini_options = 1\n")
        result = self.discover(["."])
        self.assertEqual(result["candidate_checks"], [])
        solution = self.write("Hidden.sln", "Microsoft Visual Studio Solution File")
        original = Path.read_bytes
        def denied(path):
            if path == solution:
                raise PermissionError("fixture access denied")
            return original(path)
        with patch.object(Path, "read_bytes", denied):
            result = self.discover(["Hidden.sln"])
        self.assertEqual(result["candidate_checks"], [])

    def test_local_solution_references_have_provenance_and_stay_inside_project(self):
        self.write("app/Main.slnx", '<Solution><Project Path="../shared/Shared.csproj"/><Project Path="../../outside/Other.csproj"/></Solution>')
        self.write("shared/Shared.csproj", '<Project><ItemGroup><ProjectReference Include="../lib/Library.csproj"/></ItemGroup></Project>')
        self.write("lib/Library.csproj", "<Project />")
        result = self.discover(["app/Main.slnx"])
        self.assertTrue({"shared/Shared.csproj", "lib/Library.csproj"}.issubset({item["path"] for item in result["resources"]}))
        reference = next(item for item in result["dependencies"] if item["path"] == "lib/Library.csproj")
        self.assertEqual(reference["from"], "shared/Shared.csproj")
        self.assertTrue(reference["reason"])
        self.assertTrue(any("outside project" in item for item in result["limitations"]))

    def test_external_symlink_is_not_read(self):
        outside = Path(self.temp.name) / "outside"
        outside.mkdir()
        canary = outside / "canary.txt"
        canary.write_text("private", encoding="utf-8")
        link = self.root / "linked"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except OSError:
            if os.name != "nt":
                raise
            result = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(outside)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        original = Path.read_bytes
        def guarded(path):
            if path.resolve() == canary:
                raise AssertionError("External canary was read")
            return original(path)
        with patch.object(Path, "read_bytes", guarded), self.assertRaises(ValueError):
            self.discover(["linked/canary.txt"])

    def test_unavailable_source_requires_explicit_intake_gap(self):
        payload = self.meeting()
        payload["source_refs"][0]["status"] = "unavailable"
        payload["gaps"] = []
        with self.assertRaises(ValueError):
            discovery.validate_intake(payload)

    def test_unreadable_python_convention_does_not_fall_back_to_unittest(self):
        configuration = self.write("pyproject.toml", '[tool.pytest.ini_options]\ntestpaths = ["specs"]\n')
        self.write("tests/test_old.py", "raise RuntimeError('must not import')")
        original = Path.read_bytes
        def denied(path):
            if path == configuration:
                raise PermissionError("fixture access denied")
            return original(path)
        with patch.object(Path, "read_bytes", denied):
            result = self.discover(["."])
        self.assertEqual(result["candidate_checks"], [])
        self.assertEqual(next(item for item in result["resources"] if item["path"] == "pyproject.toml")["status"], "UNKNOWN")


class DiscoveryRecordTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ase-discovery-db-")
        self.addCleanup(self.temp.cleanup)
        self.database = Path(self.temp.name) / "memory.sqlite3"
        self.db = sqlite3.connect(self.database, timeout=10)
        self.db.row_factory = sqlite3.Row
        self.addCleanup(self.db.close)
        self.db.executescript("CREATE TABLE tasks(id TEXT PRIMARY KEY, contract TEXT); INSERT INTO tasks VALUES('task','{}'); CREATE TABLE task_discoveries(task_id TEXT NOT NULL REFERENCES tasks(id),revision INTEGER NOT NULL,payload TEXT NOT NULL,created TEXT NOT NULL,PRIMARY KEY(task_id,revision));")
        self.store = SimpleNamespace(db=self.db)
        self.payload = {"scope": {"resources": ["notes.md"], "dependencies": []}, "resources": [{"path": "notes.md", "status": "MISSING"}], "dependencies": [], "candidate_checks": [], "selected_tools": [], "selected_skills": [], "limitations": ["Source missing"]}

    def test_record_preserves_intake_gaps_and_stale_record_does_not_write(self):
        self.payload["intake"] = DiscoveryTests.meeting(self)
        result = discovery.record_discovery(self.store, "task", self.payload, expected_revision=0)
        self.assertEqual(result["revision"], 1)
        self.assertTrue(result["payload"]["intake"]["gaps"][0]["required"])
        self.assertEqual(self.db.execute("SELECT contract FROM tasks").fetchone()[0], "{}")
        with self.assertRaisesRegex(ValueError, "[Ss]tale"):
            discovery.record_discovery(self.store, "task", self.payload, expected_revision=0)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM task_discoveries").fetchone()[0], 1)
        for task, revision, payload in (("missing", 0, self.payload), ("task", True, self.payload), ("task", 1, {**self.payload, "approved": True})):
            with self.subTest(task=task, revision=revision), self.assertRaises(ValueError):
                discovery.record_discovery(self.store, task, payload, expected_revision=revision)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM task_discoveries").fetchone()[0], 1)

    def test_concurrent_recorders_have_one_cas_winner(self):
        barrier = threading.Barrier(2)
        outcomes = []
        def writer():
            db = sqlite3.connect(self.database, timeout=10)
            try:
                barrier.wait()
                discovery.record_discovery(SimpleNamespace(db=db), "task", self.payload, expected_revision=0)
                outcomes.append("saved")
            except ValueError:
                outcomes.append("stale")
            finally:
                db.close()
        threads = [threading.Thread(target=writer) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)
        self.assertEqual(sorted(outcomes), ["saved", "stale"])

    def test_malformed_observations_and_unreasoned_selection_never_write(self):
        for index, change in enumerate(({"tools": ["installed"]},
                       {"tools": [{"name": "tool", "available": "maybe"}]},
                       {"skills": [{"name": "helper", "compatible": False}], "selected_skills": ["helper"]},
                       {"selected_tools": ["missing"]},
                       {"resources": [{"path": "notes.md", "status": "INSPECTED", "sha256": "a" * 64}, {"path": "notes.md", "status": "MISSING"}]},
                       {"candidate_checks": [{"id": "check", "argv": ["python"], "kind": "test", "parser": None, "cwd": ".", "source_refs": [], "selected": True, "requires_inspection": True}]})):
            task = "case-" + str(index)
            with self.db:
                self.db.execute("INSERT INTO tasks VALUES(?, '{}')", (task,))
            with self.subTest(change=change), self.assertRaises(ValueError):
                discovery.record_discovery(self.store, task, {**self.payload, **change}, expected_revision=0)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM task_discoveries").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
