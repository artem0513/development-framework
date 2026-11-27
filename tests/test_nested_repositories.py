"""One workspace installation, several independent nested Git clones (not submodules).

The gate and sessions below are protocol boundaries (patched GateService.gate); Git, snapshots,
baselines, packets, finish, ci-check and publication run for real.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from ase import ci_check
from ase.finish import finish
from ase.gates import source_snapshot
from ase.publishing import Publisher
from ase.repositories import declared_repositories, task_repository
from ase.state import read_record
from ase.store import Store
from ase.tasks import TaskService, record_baseline
from ase.workflow import WorkflowController


def contract(task_id, resources, repository=None):
    scope = {"resources": resources, "effects": ["file_write"]}
    value = {"id": task_id, "schema_version": 2, "profile": "software", "risk": "LOW",
             "request": "Change the order total rounding in the orders service.", "scope": scope,
             "acceptance": [{"id": "AC1", "expected": "Totals are rounded.", "evidence_ids": ["tests"]}],
             "evidence": [{"id": "tests", "kind": "test", "parser": "unittest", "argv": [sys.executable, "-m", "unittest", "-v"]}],
             "stages": [{"id": "change", "scope": scope, "acceptance_ids": ["AC1"], "depends_on": []}], "unknowns": []}
    if repository:
        value["repository"] = repository
    return value


@unittest.skipUnless(shutil.which("git"), "git required")
class NestedRepositoryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="ase-nested-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.root = self.base / "workspace"
        self.root.mkdir()
        self.git(self.root, "init", "-q", "-b", "main")
        self.identity(self.root)
        (self.root / ".gitignore").write_text("src/\n", encoding="utf-8")
        (self.root / ".meta").write_text(json.dumps({"projects": {"src/orders": "git@example.invalid:team/orders.git",
                                                                   "src/billing": "git@example.invalid:team/billing.git"}}), encoding="utf-8")
        (self.root / "README.md").write_text("Workspace\n", encoding="utf-8")
        self.git(self.root, "add", "-A")
        self.git(self.root, "commit", "-q", "-m", "Workspace")
        self.orders = self.clone("orders", {"app.py": "def total(x):\n    return x\n",
                                            "tests/test_app.py": "import unittest\n\nclass T(unittest.TestCase):\n    def test_total(self):\n        pass\n"})
        self.billing = self.clone("billing", {"invoice.py": "RATE = 1\n"})
        self.git(self.orders, "checkout", "-q", "-b", "feature/rounding")
        (self.orders / "app.py").write_text("def total(x):\n    return round(x, 2)\n", encoding="utf-8", newline="\n")
        self.git(self.orders, "commit", "-q", "-am", "Round totals")
        build = self.orders / "bin"
        build.mkdir()
        for index in range(1100):
            (build / f"artifact{index}.dll").write_bytes(b"\0build")
        self.store = Store(self.root, self.base / "state")
        self.addCleanup(self.store.close)
        self.tasks = TaskService(self.store)

    def git(self, repo, *args):
        done = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        return done.stdout.strip()

    def identity(self, repo):
        self.git(repo, "config", "user.name", "Nested Fixture")
        self.git(repo, "config", "user.email", "nested@example.invalid")
        self.git(repo, "config", "core.autocrlf", "false")

    def clone(self, name, files):
        repo = self.root / "src" / name
        repo.mkdir(parents=True)
        self.git(repo, "init", "-q", "-b", "master")
        self.identity(repo)
        (repo / ".gitignore").write_text("bin/\nobj/\n", encoding="utf-8")
        for relative, text in files.items():
            path = repo / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8", newline="\n")
        self.git(repo, "add", "-A")
        self.git(repo, "commit", "-q", "-m", "Initial " + name)
        remote = self.base / (name + ".git")
        self.git(self.base, "init", "-q", "--bare", str(remote))
        self.git(repo, "remote", "add", "origin", str(remote))
        self.git(repo, "push", "-q", "origin", "master")
        self.git(repo, "fetch", "-q", "origin")
        return repo

    def approved(self, task_id="orders", repository={"path": "src/orders", "base": "origin/master"}, resources=("src/orders",)):
        task = self.tasks.create(contract(task_id, list(resources), repository))
        task = self.tasks.propose(task_id, {"summary": "Round totals", "stage_ids": ["change"]}, expected_revision=task["revision"])
        task = self.tasks.approve(task_id, proposal_hash=task["proposal_hash"], user_message_ref="test:owner-approved",
                                  approved_stage_ids=["change"])
        record_baseline(self.store, task_id, task["contract"])
        return task

    def gate(self, task_id="orders"):
        task = self.tasks.snapshot(task_id)
        return {"status": "APPROVED", "reasons": [], "blocked_reasons": [], "rejected_reasons": [],
                "fingerprint": source_snapshot(self.store, task["contract"], task_id=task_id)["fingerprint"],
                "contract_hash": task["contract_hash"], "proposal_hash": task["proposal_hash"],
                "plan_hash": self.tasks.execution_plan(task_id)["plan_hash"]}

    def test_manifest_declares_the_nested_clones_and_scope_selects_one(self):
        self.assertEqual(declared_repositories(self.root), ["src/billing", "src/orders"])
        self.assertEqual(task_repository(self.root, contract("t", ["src/orders/app.py"])), {"path": "src/orders", "base": None})
        self.assertEqual(task_repository(self.root, contract("t", ["README.md"])), {"path": None, "base": None})

    def test_a_foreign_meta_file_declares_nothing(self):
        (self.root / ".meta").write_text("not json: another tool's settings\n", encoding="utf-8")
        self.assertEqual(declared_repositories(self.root), [])
        (self.root / ".meta").write_text(json.dumps({"projects": ["src/orders"]}), encoding="utf-8")
        self.assertEqual(declared_repositories(self.root), [])

    def test_clones_the_root_does_not_ignore_are_refused_with_a_clear_reason(self):
        (self.root / ".gitignore").write_text("", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "must ignore its nested repositories: src/orders"):
            self.tasks.create(contract("orders", ["src/orders"]))
        with self.assertRaisesRegex(ValueError, "must ignore"):
            source_snapshot(self.store, contract("root", ["README.md"]))

    def test_inferred_repository_is_pinned_into_the_contract(self):
        task = self.tasks.create(contract("orders", ["src/orders/app.py"]))
        self.assertEqual(task["contract"]["repository"], {"path": "src/orders"})
        self.assertNotIn("repository", self.tasks.create(contract("root", ["README.md"]))["contract"])

    def test_non_software_tasks_may_span_repositories(self):
        document = contract("notes", ["README.md", "src/orders", "src/billing"])
        document["profile"] = "document"
        document["evidence"] = [{"id": "tests", "kind": "artifact", "path": "README.md", "required": True}]
        self.assertNotIn("repository", self.tasks.create(document)["contract"])

    def test_base_baseline_marks_branch_additions_and_deletions_but_keeps_preexisting_untracked_files(self):
        (self.orders / "notes.txt").write_text("local notes\n", encoding="utf-8")
        (self.orders / "added.py").write_text("NEW = 1\n", encoding="utf-8", newline="\n")
        self.git(self.orders, "add", "added.py")
        self.git(self.orders, "rm", "-q", "tests/test_app.py")
        self.git(self.orders, "commit", "-q", "-m", "Add and delete")
        self.approved()
        baseline = read_record(self.store.db, "baseline", "orders")["payload"]
        current = source_snapshot(self.store, self.tasks.snapshot("orders")["contract"], task_id="orders")["files"]
        changed = sorted(p for p in set(baseline["files"]) | set(current) if baseline["files"].get(p) != current.get(p))
        self.assertEqual(changed, ["src/orders/added.py", "src/orders/app.py", "src/orders/tests/test_app.py"])
        self.assertNotIn("src/orders/added.py", baseline["files"])
        self.assertNotIn("src/orders/tests/test_app.py", current)
        self.assertEqual(baseline["files"]["src/orders/notes.txt"], current["src/orders/notes.txt"])

    @unittest.skipUnless(os.name == "nt", "junction fixture")
    def test_linked_git_directory_is_refused(self):
        from ase.repositories import repository_path
        real = self.billing / ".git"
        moved = self.base / "billing-git"
        real.rename(moved)
        subprocess.run(["cmd", "/c", "mklink", "/J", str(real), str(moved)], check=True, capture_output=True)
        with self.assertRaisesRegex(ValueError, "symlink or junction"):
            repository_path(self.root, "src/billing")

    def test_project_local_config_can_declare_repositories_without_a_manifest(self):
        from ase.project_local import config_bytes
        (self.root / ".meta").unlink()
        config = self.root / ".devframework" / "config.toml"
        config.parent.mkdir()
        config.write_bytes(config_bytes(["claude"]) + b'repositories = ["src/billing"]\n')
        self.assertEqual(declared_repositories(self.root), ["src/billing"])
        config.write_bytes(config_bytes(["claude"]) + b'repositories = ["../outside"]\n')
        with self.assertRaisesRegex(ValueError, "workspace-relative"):
            declared_repositories(self.root)

    def test_a_task_spanning_two_repositories_is_rejected(self):
        for resources in (["src/orders", "src/billing"], ["src/orders/app.py", "README.md"]):
            with self.subTest(resources=resources), self.assertRaisesRegex(ValueError, "one repository"):
                self.tasks.create(contract("both", resources))
        with self.assertRaisesRegex(ValueError, "inside its declared repository"):
            self.tasks.create(contract("other", ["src/billing"], {"path": "src/orders"}))
        with self.assertRaisesRegex(ValueError, "not a commit"):
            self.tasks.create(contract("nobase", ["src/orders"], {"path": "src/orders", "base": "origin/missing"}))

    def revise_and_approve(self, task_id, resources, repository):
        task = self.tasks.snapshot(task_id)
        task = self.tasks.revise(task_id, contract(task_id, resources, repository), expected_revision=task["revision"])
        task = self.tasks.propose(task_id, {"summary": "Round totals", "stage_ids": ["change"]}, expected_revision=task["revision"])
        return self.tasks.approve(task_id, proposal_hash=task["proposal_hash"], user_message_ref="test:owner-approved-2",
                                  approved_stage_ids=["change"])

    def changed(self, task_id, kind="baseline"):
        baseline = read_record(self.store.db, kind, task_id)["payload"]
        current = source_snapshot(self.store, self.tasks.snapshot(task_id)["contract"], task_id=task_id)["files"]
        return sorted(p for p in set(baseline["files"]) | set(current) if baseline["files"].get(p) != current.get(p))

    def test_revised_repository_and_base_recompute_the_baseline_from_the_base_ref(self):
        # First approved against the workspace root: the baseline is the root's files.
        self.approved("moved", repository={"path": "."}, resources=["README.md"])
        self.assertIsNone(read_record(self.store.db, "baseline", "moved")["payload"].get("repository"))
        # A native edit after the first approval must stay a change after the recompute.
        (self.orders / "app.py").write_text("def total(x):\n    return round(x, 3)\n", encoding="utf-8", newline="\n")
        task = self.revise_and_approve("moved", ["src/orders"], {"path": "src/orders", "base": "origin/master"})
        self.assertNotIn("baseline_warning", task)
        for kind in ("approval_baseline", "baseline"):
            with self.subTest(kind=kind):
                baseline = read_record(self.store.db, kind, "moved")["payload"]
                self.assertEqual((baseline["repository"], baseline["base_ref"]), ("src/orders", "origin/master"))
                self.assertEqual(baseline["commit"], self.git(self.orders, "rev-parse", "origin/master"))
                self.assertEqual(self.changed("moved", kind), ["src/orders/app.py"])
        events = [json.loads(row[0]) for row in self.store.db.execute(
            "SELECT payload FROM runtime_events WHERE task_id='moved' AND kind='baseline_replaced' ORDER BY rowid")]
        self.assertEqual({event["kind"] for event in events}, {"approval_baseline", "baseline"})
        self.assertTrue(all(event["previous"] == {"path": None, "base": None}
                            and event["current"] == {"path": "src/orders", "base": "origin/master"} for event in events))
        # The same repository/base is kept as it is: a later run does not move it.
        before = read_record(self.store.db, "baseline", "moved")
        record_baseline(self.store, "moved", self.tasks.snapshot("moved")["contract"])
        self.assertEqual(read_record(self.store.db, "baseline", "moved")["revision"], before["revision"])

    def test_a_changed_base_ref_on_the_same_repository_also_recomputes_and_unchanged_tasks_keep_theirs(self):
        self.approved("rebased")
        self.git(self.orders, "branch", "release", "origin/master")
        task = self.revise_and_approve("rebased", ["src/orders"], {"path": "src/orders", "base": "release"})
        self.assertNotIn("baseline_warning", task)
        baseline = read_record(self.store.db, "baseline", "rebased")["payload"]
        self.assertEqual((baseline["repository"], baseline["base_ref"]), ("src/orders", "release"))
        self.assertEqual(self.changed("rebased"), ["src/orders/app.py"])
        events = self.store.db.execute("SELECT COUNT(*) FROM runtime_events WHERE task_id='rebased' AND kind='baseline_replaced'").fetchone()[0]
        self.assertEqual(events, 2)
        # A revision before any approval has no baseline to protect; it may move between repositories freely.
        draft = self.tasks.create(contract("draft", ["README.md"], {"path": "."}))
        self.tasks.revise("draft", contract("draft", ["src/orders"], {"path": "src/orders"}), expected_revision=draft["revision"])
        # A non-software task records its baseline once, as before.
        document = contract("doc", ["README.md"])
        document["profile"] = "document"
        document["evidence"] = [{"id": "tests", "kind": "artifact", "path": "README.md"}]
        self.tasks.create(document)
        self.assertIsNotNone(record_baseline(self.store, "doc", self.tasks.snapshot("doc")["contract"]))
        self.assertIsNone(record_baseline(self.store, "doc", self.tasks.snapshot("doc")["contract"]))

    def test_revised_repository_without_a_base_ref_is_refused_not_kept(self):
        self.approved("moved", repository={"path": "."}, resources=["README.md"])
        task = self.tasks.snapshot("moved")
        with self.assertRaisesRegex(ValueError, "create a new task or set a base ref"):
            self.tasks.revise("moved", contract("moved", ["src/orders"], {"path": "src/orders"}), expected_revision=task["revision"])
        # A baseline that no longer matches the contract is never silently used by a run either.
        changed = contract("moved", ["src/orders"], {"path": "src/orders"})
        with self.assertRaisesRegex(ValueError, "create a new task or set a base ref"):
            record_baseline(self.store, "moved", changed)
        self.assertIsNone(read_record(self.store.db, "baseline", "moved")["payload"].get("repository"))

    def test_snapshot_uses_the_nested_git_and_excludes_ignored_build_output(self):
        task = self.tasks.create(contract("orders", ["src/orders"]))
        snapshot = source_snapshot(self.store, task["contract"], task_id="orders")
        self.assertEqual(sorted(snapshot["files"]), ["src/orders/.gitignore", "src/orders/app.py", "src/orders/tests/test_app.py"])
        self.assertEqual(snapshot["repository"], "src/orders")
        self.assertEqual(snapshot["commit"], self.git(self.orders, "rev-parse", "HEAD"))

    def test_base_ref_baseline_and_packet_carry_the_branch_diff(self):
        self.approved()
        baseline = read_record(self.store.db, "baseline", "orders")["payload"]
        self.assertEqual(baseline["commit"], self.git(self.orders, "rev-parse", "origin/master"))
        self.assertEqual(baseline["base_ref"], "origin/master")
        packet = WorkflowController(self.store)._packet("orders")
        supplied = {item["path"]: item["content"] for item in packet["files"]}
        changes = json.loads(supplied["_ase_context/source_changes.json"])
        self.assertEqual([entry["path"] for entry in changes["changes"]], ["src/orders/app.py"])
        self.assertIn("round(x, 2)", supplied["src/orders/app.py"])
        self.assertEqual(supplied["_ase_before/src/orders/app.py"], "def total(x):\n    return x\n")
        self.assertIn("+    return round(x, 2)", supplied["_ase_context/source_diff.patch"])
        self.assertFalse([path for path in supplied if "/bin/" in path or "billing" in path or path == "README.md"])

    def test_finish_ci_check_and_publication_run_in_the_nested_repository(self):
        self.approved()
        with patch("ase.finish.GateService.gate", return_value={"status": "APPROVED", "reasons": []}):
            ready = finish(self.store, "orders", "origin/master")
            self.assertEqual(ready["status"], "READY", ready["reasons"])
            self.assertEqual(Path(ready["repository"]), self.orders)
            self.assertEqual(ready["head"], self.git(self.orders, "rev-parse", "HEAD"))
            (self.orders / "app.py").write_text("def total(x):\n    return round(x, 3)\n", encoding="utf-8", newline="\n")
            dirty = finish(self.store, "orders", "origin/master")
            self.assertEqual(dirty["status"], "NOT_READY")
            self.assertEqual(dirty["dirty"], ["app.py"])
        self.git(self.orders, "checkout", "--", "app.py")
        self.assertEqual(ci_check.run(self.orders, "origin/master")["status"], "PASS")

        with patch("ase.publishing.GateService.gate", side_effect=lambda *a, **k: self.gate()):
            task = self.tasks.snapshot("orders")
            self.tasks.transition("orders", "COMPLETED", expected_revision=task["revision"], reason="Protocol boundary fixture")
            publisher = Publisher(self.store)
            plan = publisher.publish("orders")
            self.assertEqual(plan["status"], "APPROVED", plan)
            self.assertEqual(plan["commands"][-1][-1], "HEAD:refs/heads/feature/rounding")
            self.assertEqual(Publisher(self.store).authorize(repository="src/orders")["repository"], "src/orders")
            result = Publisher(self.store).publish("orders", execute_push=True)
        self.assertTrue(result["published"], result)
        remote = self.git(self.base, "--git-dir", str(self.base / "orders.git"), "rev-parse", "refs/heads/feature/rounding")
        self.assertEqual(remote, self.git(self.orders, "rev-parse", "HEAD"))
        self.assertEqual(self.git(self.base, "--git-dir", str(self.base / "billing.git"), "for-each-ref", "refs/heads/feature"), "")

    def test_root_authorization_does_not_authorize_a_nested_remote(self):
        self.git(self.root, "remote", "add", "origin", str(self.base / "orders.git"))
        Publisher(self.store).authorize()
        publisher = Publisher(self.store)
        publisher._select("src/orders")
        self.assertFalse(publisher._authorized("origin", publisher._urls("origin")[0]))

    def test_required_nested_dependency_uses_its_git_inventory(self):
        from ase.state import encode
        task = self.tasks.create(contract("orders", ["src/orders"]))
        (self.billing / "obj").mkdir()
        for index in range(1100):
            (self.billing / "obj" / f"{index}.o").write_bytes(b"\0")
        discovery = {"resources": [], "scope": {"resources": ["src/orders"], "dependencies": [
            {"path": "src/billing", "reason": "Orders imports the billing client", "required": True}]}, "dependencies": []}
        with self.store.db:
            self.store.db.execute("INSERT INTO task_discoveries(task_id,revision,payload,created) VALUES(?,?,?,?)",
                                  ("orders", 1, encode(discovery), "2026-11-24T00:00:00+00:00"))
        snapshot = source_snapshot(self.store, task["contract"], task_id="orders")
        self.assertIn("src/billing/invoice.py", snapshot["files"])
        self.assertFalse([path for path in snapshot["files"] if "/obj/" in path or "/bin/" in path])


@unittest.skipUnless(os.name == "nt", "Windows 8.3 short names")
class ShortHostPathTests(unittest.TestCase):
    def short(self, path):
        import ctypes
        size = ctypes.windll.kernel32.GetShortPathNameW(str(path), None, 0)
        buffer = ctypes.create_unicode_buffer(size)
        ctypes.windll.kernel32.GetShortPathNameW(str(path), buffer, size)
        return Path(buffer.value)

    def test_short_temp_path_is_accepted_and_junctions_are_still_rejected(self):
        from ase.runner import checked_path
        temporary = tempfile.TemporaryDirectory(prefix="ase long host name ")
        self.addCleanup(temporary.cleanup)
        long = Path(temporary.name).resolve() / "trusted working directory"
        long.mkdir()
        short = self.short(long)
        if "~" not in str(short):
            self.skipTest("8.3 short names are disabled on this volume")
        self.assertEqual(os.path.normcase(str(checked_path(str(short), directory=True))), os.path.normcase(str(long)))
        target = Path(temporary.name).resolve() / "elsewhere"
        target.mkdir()
        junction = long / "linked"
        subprocess.run(["cmd", "/c", "mklink", "/J", str(junction), str(target)], check=True, capture_output=True)
        with self.assertRaisesRegex(ValueError, "Reparse points"):
            checked_path(str(self.short(long) / "linked"), directory=True)


if __name__ == "__main__":
    unittest.main()
