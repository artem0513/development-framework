"""Real default CLI refreshes retain declared input authority, not stale imports."""
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest

from ase.gates import source_resources, source_snapshot
import test_audit_source_evidence as fixtures


ROOT = Path(__file__).resolve().parents[1]


class DiscoveryRefreshRetentionTests(unittest.TestCase):
    setUp = fixtures.SourceEvidenceAuditTests.setUp
    write = fixtures.SourceEvidenceAuditTests.write
    task = fixtures.SourceEvidenceAuditTests.task
    discovery = fixtures.SourceEvidenceAuditTests.discovery
    save = fixtures.SourceEvidenceAuditTests.save
    represented_document = fixtures.SourceEvidenceAuditTests.represented_document

    def cli_refresh(self, revision):
        result = subprocess.run([sys.executable, "-B", "-m", "ase", "--home", str(self.store.state_home),
                                 "--repo", str(self.repo), "task", "discover", "source-audit", "--expected-revision", str(revision)],
                                cwd=ROOT, env={**os.environ, "PYTHONPATH": str(ROOT)}, capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return json.loads(result.stdout)["payload"]

    def test_default_cli_refresh_keeps_missing_declared_required_dependency_blocked(self):
        value = self.task()
        policy = self.write("policy.md", "Mandatory guide policy")
        self.save(self.discovery(dependencies=[{"path": "policy.md", "reason": "Governs the guide", "blocking_stage_ids": ["draft"]}]))
        policy.unlink()
        refreshed = self.cli_refresh(1)
        self.assertEqual(next(item for item in refreshed["resources"] if item["path"] == "policy.md")["status"], "MISSING")
        self.assertIn("policy.md", source_resources(self.store, value, "source-audit"))
        self.assertEqual(self.tasks.readiness("source-audit", stage_id="draft")["clarity"], "BLOCKED")
        with self.assertRaisesRegex(ValueError, "source|gap"):
            self.tasks.require_approval("source-audit", "draft")
        self.assertTrue(any("policy.md" in gap for gap in self.controller._packet("source-audit", stage_id="draft")["coverage"]["missing"]))

    def test_default_cli_scans_retained_existing_dependency_bytes_again(self):
        self.task()
        policy = self.write("policy.md", "Original governing text")
        self.save(self.discovery(dependencies=[{"path": "policy.md", "reason": "Governs the guide"}]))
        policy.write_text("Current governing text", encoding="utf-8")
        refreshed = self.cli_refresh(1)
        resource = next(item for item in refreshed["resources"] if item["path"] == "policy.md")
        self.assertEqual(resource["sha256"], hashlib.sha256(b"Current governing text").hexdigest())
        packet = self.controller._packet("source-audit")
        self.assertEqual(next(item["content"] for item in packet["files"] if item["path"] == "policy.md"), "Current governing text")

    def test_structured_refresh_cannot_silently_drop_a_required_declaration(self):
        self.task()
        policy = self.write("policy.md", "Mandatory governing policy")
        self.save(self.discovery(dependencies=[{"path": "policy.md", "reason": "Governs the guide"}]))
        policy.unlink()
        recorded = self.save(self.discovery(), 1)["payload"]
        self.assertEqual(next(item for item in recorded["resources"] if item["path"] == "policy.md")["status"], "MISSING")
        self.assertTrue(self.tasks.readiness("source-audit")["required_source_gaps"])

    def test_standalone_top_level_declared_root_is_retained_on_default_refresh(self):
        self.task()
        policy = self.write("policy.md", "Explicit governing policy")
        payload = self.discovery()
        payload["dependencies"].append({"path": "policy.md", "reason": "Explicit governing policy"})
        self.save(payload)
        policy.unlink()
        self.assertTrue(self.tasks.readiness("source-audit")["required_source_gaps"])
        refreshed = self.cli_refresh(1)
        self.assertIn("policy.md", [item["path"] for item in refreshed["resources"]])
        self.assertTrue(self.tasks.readiness("source-audit")["required_source_gaps"])
        self.assertTrue(any("policy.md" in gap for gap in self.controller._packet("source-audit")["coverage"]["missing"]))

    def test_top_level_optional_reconciliation_overrides_prior_scope_requirement(self):
        self.task()
        self.save(self.discovery(dependencies=[{"path": "policy.md", "reason": "Required governing policy"}]))
        payload = self.discovery()
        payload["dependencies"].append({"path": "policy.md", "reason": "Explicitly optional background now", "required": False})
        self.save(payload, 1)
        self.assertEqual(self.tasks.readiness("source-audit")["required_source_gaps"], [])
        refreshed = self.cli_refresh(2)
        self.assertFalse(next(item for item in refreshed["scope"]["dependencies"] if item["path"] == "policy.md")["required"])

    def test_top_level_stage_reconciliation_preserves_only_the_new_stage_impact(self):
        self.task()
        self.save(self.discovery(dependencies=[{"path": "policy.md", "reason": "Required governing policy"}]))
        payload = self.discovery()
        payload["dependencies"].append({"path": "policy.md", "reason": "Publication-only governing policy now", "blocking_stage_ids": ["publish"]})
        self.save(payload, 1)
        self.assertEqual(self.tasks.readiness("source-audit", stage_id="draft")["required_source_gaps"], [])
        self.assertTrue(self.tasks.readiness("source-audit", stage_id="publish")["required_source_gaps"])
        self.cli_refresh(2)
        self.assertEqual(self.tasks.readiness("source-audit", stage_id="draft")["required_source_gaps"], [])

    def test_explicit_optional_and_stage_reconciliation_survives_later_default_refresh(self):
        value = self.task()
        self.write("policy.md", "Current policy")
        self.save(self.discovery(dependencies=[{"path": "policy.md", "reason": "Governs the guide"}]))
        before = source_snapshot(self.store, value, "source-audit")
        self.save(self.discovery(dependencies=[{"path": "policy.md", "reason": "Explicitly optional publication background", "required": False, "blocking_stage_ids": ["publish"]}]), 1)
        reconciled = source_snapshot(self.store, value, "source-audit")
        self.assertNotEqual(before["fingerprint"], reconciled["fingerprint"])
        refreshed = self.cli_refresh(2)
        declaration = next(item for item in refreshed["scope"]["dependencies"] if item["path"] == "policy.md")
        self.assertFalse(declaration["required"])
        self.assertEqual(declaration["blocking_stage_ids"], ["publish"])
        self.assertEqual(self.tasks.readiness("source-audit")["required_source_gaps"], [])
        self.assertEqual(reconciled["fingerprint"], source_snapshot(self.store, value, "source-audit")["fingerprint"])

    def test_obsolete_inferred_import_disappears_after_fresh_scan_of_retained_root(self):
        value = self.task()
        project = self.write("policy/Policy.csproj", '<Project><ProjectReference Include="obsolete/Child.csproj" /></Project>')
        child = self.write("policy/obsolete/Child.csproj", "<Project />")
        self.save(self.discovery(dependencies=[{"path": "policy/Policy.csproj", "reason": "Governs publication"}]))
        self.assertIn("policy/obsolete/Child.csproj", source_resources(self.store, value, "source-audit"))
        project.write_text("<Project />", encoding="utf-8")
        child.unlink()
        refreshed = self.cli_refresh(1)
        self.assertIn("policy/Policy.csproj", [item["path"] for item in refreshed["resources"]])
        self.assertNotIn("policy/obsolete/Child.csproj", source_resources(self.store, value, "source-audit"))
        self.assertEqual(self.controller._packet("source-audit")["coverage"]["missing"], [])

    def test_replaced_output_scope_drops_old_output_but_keeps_prior_explicit_read_root(self):
        self.task("old.md")
        old = self.write("old.md", "Former task output")
        self.write("policy.md", "Declared read-only governing source")
        self.save(self.discovery(resources=["old.md", "policy.md"]))
        current = self.tasks.snapshot("source-audit")
        changed = copy.deepcopy(current["contract"])
        changed["scope"]["resources"] = ["new.md"]
        changed["evidence"][0]["path"] = "new.md"
        for stage in changed["stages"]:
            stage["scope"]["resources"] = ["new.md"]
        revised = self.tasks.revise("source-audit", changed, expected_revision=current["revision"])
        old.unlink()
        self.cli_refresh(1)
        resources = source_resources(self.store, revised["contract"], "source-audit")
        self.assertIn("new.md", resources)
        self.assertIn("policy.md", resources)
        self.assertNotIn("old.md", resources)
        self.assertEqual(self.tasks.readiness("source-audit")["required_source_gaps"], [])
        self.assertEqual(self.controller._packet("source-audit")["coverage"]["missing"], [])

    def test_default_refresh_preserves_scoped_representation_and_explicit_update(self):
        payload = self.represented_document()
        self.save(payload)
        value = self.tasks.snapshot("source-audit")["contract"]
        before = source_snapshot(self.store, value, "source-audit")
        refreshed = self.cli_refresh(1)
        self.assertEqual(refreshed["representations"], payload["representations"])
        self.assertEqual(self.controller._packet("source-audit")["coverage"]["missing"], [])
        self.assertEqual(before["fingerprint"], source_snapshot(self.store, value, "source-audit")["fingerprint"])
        refreshed["representations"][0]["method"] = "Updated explicit extraction method"
        self.save(refreshed, 2)
        changed = source_snapshot(self.store, value, "source-audit")
        self.assertNotEqual(before["fingerprint"], changed["fingerprint"])
        refreshed["representations"] = []
        self.save(refreshed, 3)
        self.assertTrue(any("guide.docx" in gap for gap in self.controller._packet("source-audit")["coverage"]["missing"]))


if __name__ == "__main__":
    unittest.main()
