"""Directory source selection uses real Git/files; no semantic model doubles."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from ase.discovery import discover_capabilities, record_discovery
from ase.gates import source_snapshot
from ase.store import Store
from ase.tasks import TaskService
from ase.workflow import WorkflowController
from test_task_contract_v2 import contract


class DirectorySourceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="ase-directory-source-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.repo = self.base / "workspace"
        self.repo.mkdir()
        self.write("guide.md", "Confirmed operating guide")
        self.store = Store(self.repo, self.base / "state")
        self.addCleanup(self.store.close)

    def write(self, relative, content):
        path = self.repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.repo), *args], check=True, capture_output=True)

    def task(self, *, software=False, resources=None, dependencies=None):
        if software:
            self.git("init", "-b", "feature/directory")
        c = contract("directory-task")
        c["profile"] = "software" if software else "document"
        task = TaskService(self.store).create(c)
        discovery = discover_capabilities(self.store.project, scope={"resources": resources or ["guide.md"],
            "dependencies": dependencies or []}, backend_config={})
        record_discovery(self.store, task["task_id"], discovery, expected_revision=0)
        return c, WorkflowController(self.store)

    def test_software_root_discovery_preserves_git_ignore_and_private_metadata(self):
        self.write(".gitignore", "cache/\n")
        ignored = self.write("cache/ignored.txt", "private ignored output")
        c, controller = self.task(software=True, resources=["."])
        original = Path.read_bytes
        def guarded(path):
            # The framework also reads its own bundled policy outside this
            # client. This guard checks private/ignored client traversal.
            if path == ignored or (path.is_relative_to(self.repo) and ".git" in path.relative_to(self.repo).parts):
                raise AssertionError("Private or ignored file bytes were read")
            return original(path)
        with patch.object(Path, "read_bytes", guarded):
            snapshot = source_snapshot(self.store, c, task_id="directory-task")
            packet = controller._packet("directory-task")
        self.assertIn("guide.md", snapshot["files"])
        self.assertNotIn("cache/ignored.txt", snapshot["files"])
        self.assertIn("guide.md", packet["coverage"]["included"])
        self.assertFalse(any(".git/" in name for name in snapshot["files"]))
        self.assertEqual(packet["coverage"]["missing"], [])

    def test_material_dependency_directory_bodies_and_fingerprints_stay_aligned(self):
        policy = self.write("policies/nested/policy.md", "Version one policy")
        self.write("unrelated.txt", "Unrelated material")
        c, controller = self.task(dependencies=[{"path": "policies", "reason": "Governs the operating guide"}])
        before = source_snapshot(self.store, c, task_id="directory-task")
        packet = controller._packet("directory-task")
        item = next((item for item in packet["files"] if item["path"] == "policies/nested/policy.md"), None)
        self.assertIsNotNone(item, "Material dependency body was silently omitted")
        self.assertEqual(item["sha256"], before["files"][item["path"]])
        self.assertNotIn("unrelated.txt", packet["coverage"]["included"])
        policy.write_text("Version two policy", encoding="utf-8")
        after = source_snapshot(self.store, c, task_id="directory-task")
        self.assertNotEqual(before["fingerprint"], after["fingerprint"])
        updated = next(item for item in controller._packet("directory-task")["files"] if item["path"] == "policies/nested/policy.md")
        self.assertEqual(updated["content"], "Version two policy")

    def test_binary_dependency_directory_reports_explicit_packet_gap(self):
        path = self.repo / "policies" / "binary.dat"
        path.parent.mkdir()
        path.write_bytes(b"\xff\x00policy")
        c, controller = self.task(dependencies=[{"path": "policies", "reason": "Contains source policy"}])
        self.assertIn("policies/binary.dat", source_snapshot(self.store, c, task_id="directory-task")["files"])
        self.assertTrue(any("policies/binary.dat" in gap for gap in controller._packet("directory-task")["coverage"]["missing"]))

    def test_directory_skips_nested_private_git_but_explicit_private_path_blocks(self):
        self.write("policies/.GiT/private.txt", "Private metadata")
        self.write("policies/policy.md", "Policy")
        c, controller = self.task(dependencies=[{"path": "policies", "reason": "Contains policy"}])
        snapshot = source_snapshot(self.store, c, task_id="directory-task")
        self.assertIn("policies/policy.md", snapshot["files"])
        self.assertFalse(any(".git" in path.lower() for path in snapshot["files"]))
        self.assertEqual(controller._packet("directory-task")["coverage"]["missing"], [])
        private_contract = {**c, "scope": {"resources": ["policies/.GiT/private.txt"], "effects": []}}
        with self.assertRaisesRegex(ValueError, "private|escapes|Git"):
            source_snapshot(self.store, private_contract)

    def test_explicit_ignored_dependency_file_is_still_captured(self):
        self.write(".gitignore", "policy.txt\n")
        policy = self.write("policy.txt", "Explicitly required policy")
        c, controller = self.task(software=True, dependencies=[{"path": "policy.txt", "reason": "Explicit governing policy"}])
        snapshot = source_snapshot(self.store, c, task_id="directory-task")
        self.assertEqual(snapshot["files"]["policy.txt"], hashlib.sha256(policy.read_bytes()).hexdigest())
        self.assertIn("policy.txt", controller._packet("directory-task")["coverage"]["included"])

    def test_explicit_required_ignored_directory_is_hashed_and_reviewed(self):
        self.write(".gitignore", "policies/\ncache/\n")
        policy = self.write("policies/nested/policy.md", "Mandatory current policy")
        self.write("cache/secret.txt", "Unrelated ignored secret")
        c, controller = self.task(software=True, resources=["."], dependencies=[
            {"path": "policies", "reason": "Contains required governing policy"}])
        before = source_snapshot(self.store, c, task_id="directory-task")
        packet = controller._packet("directory-task")
        self.assertEqual(before["files"].get("policies/nested/policy.md"), hashlib.sha256(policy.read_bytes()).hexdigest())
        self.assertIn("policies/nested/policy.md", packet["coverage"]["included"])
        self.assertNotIn("cache/secret.txt", before["files"])
        self.assertNotIn("cache/secret.txt", packet["coverage"]["included"])
        policy.write_text("Materially revised policy", encoding="utf-8")
        self.assertNotEqual(before["fingerprint"], source_snapshot(self.store, c, task_id="directory-task")["fingerprint"])

    def test_required_manifest_ignored_directory_supplies_bodies_and_current_fingerprint(self):
        self.write(".gitignore", "vendor/\ncache/\n")
        self.write("package.json", '{"dependencies":{"local-policy":"file:vendor/local"}}')
        policy = self.write("vendor/local/policy.py", "ALLOWED = True")
        self.write("cache/secret.txt", "Unrelated ignored secret")
        c, controller = self.task(software=True, resources=["guide.md", "package.json"])
        before = source_snapshot(self.store, c, task_id="directory-task")
        packet = controller._packet("directory-task")
        source = next((item for item in packet["files"] if item["path"] == "vendor/local/policy.py"), None)
        self.assertIsNotNone(source, "Required local package body was silently omitted")
        self.assertEqual(source["content"], "ALLOWED = True")
        self.assertEqual(before["files"].get("vendor/local/policy.py"), hashlib.sha256(policy.read_bytes()).hexdigest())
        self.assertEqual(packet["coverage"]["missing"], [])
        self.assertNotIn("cache/secret.txt", before["files"])
        self.assertNotIn("cache/secret.txt", packet["coverage"]["included"])
        policy.write_text("ALLOWED = False", encoding="utf-8")
        after = source_snapshot(self.store, c, task_id="directory-task")
        self.assertNotEqual(before["fingerprint"], after["fingerprint"])
        current = next(item for item in controller._packet("directory-task")["files"] if item["path"] == "vendor/local/policy.py")
        self.assertEqual(current["content"], "ALLOWED = False")

    def test_optional_manifest_ignored_directory_does_not_expand_source_bodies(self):
        self.write(".gitignore", "vendor/\n")
        self.write("optional/package.json", '{"dependencies":{"local-policy":"file:../vendor/local"}}')
        self.write("vendor/local/secret.txt", "Optional ignored material")
        c, controller = self.task(software=True, dependencies=[
            {"path": "optional/package.json", "reason": "Optional package example", "required": False}])
        snapshot = source_snapshot(self.store, c, task_id="directory-task")
        packet = controller._packet("directory-task")
        self.assertNotIn("vendor/local/secret.txt", snapshot["files"])
        self.assertNotIn("vendor/local/secret.txt", packet["coverage"]["included"])
        self.assertEqual(packet["coverage"]["missing"], [])

    def test_manifest_root_reference_preserves_ignored_secret_isolation(self):
        self.write(".gitignore", "cache/\n")
        self.write("package.json", '{"dependencies":{"workspace":"file:."}}')
        self.write("cache/secret.txt", "Unrelated ignored secret")
        c, controller = self.task(software=True, resources=["guide.md", "package.json"])
        snapshot = source_snapshot(self.store, c, task_id="directory-task")
        packet = controller._packet("directory-task")
        self.assertNotIn("cache/secret.txt", snapshot["files"])
        self.assertNotIn("cache/secret.txt", packet["coverage"]["included"])
        self.assertEqual(packet["coverage"]["missing"], [])

    def test_required_manifest_directory_unions_stage_impact_from_distinct_origins(self):
        self.write(".gitignore", "vendor/\n")
        for origin in ("draft", "publish"):
            self.write(origin + "/package.json", '{"dependencies":{"local-policy":"file:../vendor/local"}}')
        self.write("vendor/local/policy.md", "Policy for both stages")
        c, controller = self.task(software=True, dependencies=[
            {"path": "draft/package.json", "reason": "Draft package", "blocking_stage_ids": ["draft"]},
            {"path": "publish/package.json", "reason": "Publication package", "blocking_stage_ids": ["publish"]}])
        for stage in ("draft", "publish"):
            with self.subTest(stage=stage):
                packet = controller._packet("directory-task", stage_id=stage)
                self.assertIn("vendor/local/policy.md", packet["coverage"]["included"])
                self.assertEqual(packet["coverage"]["missing"], [])
                availability = next(item for item in packet["files"] if item["path"] == "_ase_context/source_availability.json")
                policy = next(item for item in json.loads(availability["content"]) if item["path"] == "vendor/local")
                self.assertTrue(policy["required"])
                self.assertEqual(set(policy["blocking_stage_ids"]), {"draft", "publish"})

    def test_required_manifest_ignored_directory_rejects_hardlinked_body(self):
        self.write(".gitignore", "vendor/\n")
        self.write("package.json", '{"dependencies":{"local-policy":"file:vendor/local"}}')
        origin = self.base / "outside-policy.txt"
        origin.write_text("Outside linked policy", encoding="utf-8")
        linked = self.repo / "vendor/local/policy.txt"
        linked.parent.mkdir(parents=True)
        os.link(origin, linked)
        c, controller = self.task(software=True, resources=["guide.md", "package.json"])
        with self.assertRaisesRegex(ValueError, "Hardlinked"):
            source_snapshot(self.store, c, task_id="directory-task")

    def test_required_manifest_ignored_directory_oversized_body_is_an_explicit_gap(self):
        self.write(".gitignore", "vendor/\n")
        self.write("package.json", '{"dependencies":{"local-policy":"file:vendor/local"}}')
        policy = self.write("vendor/local/policy.txt", "x" * 512001)
        c, controller = self.task(software=True, resources=["guide.md", "package.json"])
        snapshot = source_snapshot(self.store, c, task_id="directory-task")
        packet = controller._packet("directory-task")
        self.assertEqual(snapshot["files"].get("vendor/local/policy.txt"), hashlib.sha256(policy.read_bytes()).hexdigest())
        self.assertTrue(any("vendor/local/policy.txt" in gap and "bound" in gap for gap in packet["coverage"]["missing"]))

    def test_optional_ignored_directory_does_not_expand_workspace_reads(self):
        self.write(".gitignore", "policies/\n")
        self.write("policies/secret.txt", "Optional ignored material")
        c, controller = self.task(software=True, resources=["."], dependencies=[
            {"path": "policies", "reason": "Optional background", "required": False}])
        self.assertNotIn("policies/secret.txt", source_snapshot(self.store, c, task_id="directory-task")["files"])
        self.assertNotIn("policies/secret.txt", controller._packet("directory-task")["coverage"]["included"])

    def test_required_ignored_directory_read_failure_is_explicit(self):
        self.write(".gitignore", "policies/\n")
        policy = self.write("policies/policy.md", "Required ignored policy")
        c, controller = self.task(software=True, dependencies=[{"path": "policies", "reason": "Required governing input"}])
        original = Path.open
        def denied(path, *args, **kwargs):
            if path == policy:
                raise PermissionError(13, "fixture source denied", str(path))
            return original(path, *args, **kwargs)
        before = source_snapshot(self.store, c, task_id="directory-task")
        with patch.object(Path, "open", denied):
            observed = source_snapshot(self.store, c, task_id="directory-task")
            self.assertIn("policies", observed["unavailable"])
            self.assertNotEqual(before["fingerprint"], observed["fingerprint"])
            self.assertTrue(any("policies" in gap for gap in controller._packet("directory-task")["coverage"]["missing"]))

    def test_required_ignored_directory_still_enforces_inventory_bound(self):
        self.write(".gitignore", "policies/\n")
        for index in range(1001):
            self.write("policies/policy-" + str(index) + ".txt", "Required policy")
        c, controller = self.task(software=True, dependencies=[{"path": "policies", "reason": "Required governing inputs"}])
        with self.assertRaisesRegex(ValueError, "inventory exceeds limit"):
            source_snapshot(self.store, c, task_id="directory-task")

    def test_required_ignored_directory_rejects_redirected_descendants(self):
        self.write(".gitignore", "policies/\n")
        outside = self.base / "outside"
        outside.mkdir()
        (outside / "canary.txt").write_text("Outside source", encoding="utf-8")
        alias = self.repo / "policies" / "redirect"
        alias.parent.mkdir()
        if os.name == "nt":
            made = subprocess.run(["cmd", "/c", "mklink", "/J", str(alias), str(outside)], capture_output=True)
            self.assertEqual(made.returncode, 0, made.stderr)
            self.addCleanup(alias.rmdir)
        else:
            alias.symlink_to(outside, target_is_directory=True)
            self.addCleanup(alias.unlink)
        c, controller = self.task(software=True, dependencies=[{"path": "policies", "reason": "Required governing inputs"}])
        with self.assertRaisesRegex(ValueError, "escapes|redirect|link"):
            source_snapshot(self.store, c, task_id="directory-task")

    def test_redirected_dependency_directory_never_reads_external_file(self):
        outside = self.base / "external"
        outside.mkdir()
        canary = outside / "canary.txt"
        canary.write_text("Outside source")
        alias = self.repo / "policies" / "redirect"
        alias.parent.mkdir()
        if os.name == "nt":
            made = subprocess.run(["cmd", "/c", "mklink", "/J", str(alias), str(outside)], capture_output=True)
            self.assertEqual(made.returncode, 0, made.stderr)
            self.addCleanup(alias.rmdir)
        else:
            alias.symlink_to(outside, target_is_directory=True)
            self.addCleanup(alias.unlink)
        c, _ = self.task(dependencies=[{"path": "policies", "reason": "Contains governing policy"}])
        original = Path.read_bytes
        def guarded(path):
            if path.resolve() == canary:
                raise AssertionError("External canary was read")
            return original(path)
        with patch.object(Path, "read_bytes", guarded), self.assertRaisesRegex(ValueError, "escapes|redirect|link"):
            source_snapshot(self.store, c, task_id="directory-task")


if __name__ == "__main__":
    unittest.main()
