"""Original recovery obligations survive ambiguous contract relabeling."""
import copy
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ase.recovery import _overlap, bind_stage, initial_history
from ase.state import read_record
from ase.store import Store
from ase.tasks import TaskService
from ase.workflow import WorkflowController


class AuditRecoveryIdentityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="ase-audit-recovery-identity-")
        self.addCleanup(temporary.cleanup)
        base = Path(temporary.name)
        workspace = base / "work"
        workspace.mkdir()
        self.store = Store(workspace, base / "state")
        self.addCleanup(self.store.close)
        self.tasks = TaskService(self.store)
        self.controller = WorkflowController(self.store)

    def contract(self, **policy):
        return {
            "id": "identity", "schema_version": 2, "profile": "document",
            "request": "Document the deployment sequence and the support procedure.",
            "scope": {"resources": ["a.md", "b.md"], "effects": ["file_write"]},
            "acceptance": [
                {"id": "AC1", "expected": "a.md lists the approved deployment sequence.", "evidence_ids": ["a"]},
                {"id": "AC2", "expected": "b.md lists the support procedure.", "evidence_ids": ["b"]},
            ],
            "evidence": [{"id": side, "kind": "artifact", "path": side + ".md"} for side in ("a", "b")],
            "stages": [
                {"id": "a-stage", "scope": {"resources": ["a.md"], "effects": ["file_write"]},
                 "acceptance_ids": ["AC1"], "depends_on": []},
                {"id": "b-stage", "scope": {"resources": ["b.md"], "effects": ["file_write"]},
                 "acceptance_ids": ["AC2"], "depends_on": []},
            ],
            "execution_policy": {"recovery_mode": "progress", **policy},
        }

    def approve(self, spec):
        old = read_record(self.store.db, "task", spec["id"])["payload"]
        task = (self.tasks.revise(spec["id"], spec, expected_revision=self.tasks.snapshot(spec["id"])["revision"])
                if old else self.tasks.create(spec))
        stages = [stage["id"] for stage in task["contract"]["stages"]]
        task = self.tasks.propose(task["task_id"], {"summary": "Document the approved procedures", "stage_ids": stages},
                                  expected_revision=task["revision"])
        return self.tasks.approve(task["task_id"], proposal_hash=task["proposal_hash"],
                                  user_message_ref=f"test:recovery-identity-scope:{task['revision']}",
                                  approved_stage_ids=stages)

    @staticmethod
    def relabel(spec):
        revised = copy.deepcopy(spec)
        # Keep A's original checks/resources while borrowing B's old criterion ID.
        revised["acceptance"][0].update(id="AC2", expected="a.md documents the approved deployment steps.")
        revised["acceptance"][1]["id"] = "AC-support"
        revised["stages"][0].update(id="renamed-a-stage", acceptance_ids=["AC2"])
        revised["stages"][1]["acceptance_ids"] = ["AC-support"]
        return revised

    def test_reused_foreign_criterion_id_cannot_reset_original_cycle_cap(self):
        spec = self.contract(max_stage_cycles=3)
        self.approve(spec)
        for _ in range(3):
            self.controller.reserve_cycle("identity", "a-stage")
        with self.assertRaisesRegex(ValueError, "budget"):
            self.controller.reserve_cycle("identity", "a-stage")
        self.approve(self.relabel(spec))
        with self.assertRaisesRegex(ValueError, "budget"):
            WorkflowController(self.store).reserve_cycle("identity", "renamed-a-stage")
        self.assertEqual(read_record(self.store.db, "budget", "identity")["payload"]["task_cycles"], 3)

    def test_reused_foreign_criterion_id_cannot_reset_original_stagnation(self):
        spec = self.contract()
        self.approve(spec)
        for _ in range(3):
            self.controller.reserve_cycle("identity", "a-stage")
            self.controller.record_failure("identity", "a-stage", ["Mandatory AC failed: AC1"])
        with self.assertRaisesRegex(ValueError, "Stagnation"):
            self.controller.reserve_cycle("identity", "a-stage")
        self.approve(self.relabel(spec))
        with self.assertRaisesRegex(ValueError, "Stagnation"):
            WorkflowController(self.store).reserve_cycle("identity", "renamed-a-stage")
        self.assertEqual(read_record(self.store.db, "budget", "identity")["payload"]["task_cycles"], 3)

    @staticmethod
    def resource_alias(spec, path):
        revised = AuditRecoveryIdentityTests.relabel(spec)
        revised["scope"]["resources"][0] = path
        revised["stages"][0]["scope"]["resources"] = [path]
        revised["evidence"][0]["path"] = path
        return revised

    @unittest.skipUnless(os.name == "nt", "Actual Windows filesystem alias")
    def test_windows_case_alias_cannot_reset_original_cycle_cap(self):
        spec = self.contract(max_stage_cycles=1)
        original = self.store.repo / "a.md"
        original.write_text("Original deployment procedure\n")
        self.assertTrue(original.samefile(self.store.repo / "A.MD"))
        self.approve(spec)
        self.controller.reserve_cycle("identity", "a-stage")
        self.approve(self.resource_alias(spec, "A.MD"))
        with self.assertRaisesRegex(ValueError, "budget"):
            WorkflowController(self.store).reserve_cycle("identity", "renamed-a-stage")
        self.assertEqual(read_record(self.store.db, "budget", "identity")["payload"]["task_cycles"], 1)

    @unittest.skipUnless(os.name == "nt", "Actual Windows filesystem alias")
    def test_windows_case_alias_cannot_reset_original_stagnation(self):
        spec = self.contract()
        original = self.store.repo / "a.md"
        original.write_text("Original deployment procedure\n")
        self.assertTrue(original.samefile(self.store.repo / "A.MD"))
        self.approve(spec)
        for _ in range(3):
            self.controller.reserve_cycle("identity", "a-stage")
            self.controller.record_failure("identity", "a-stage", ["Mandatory AC failed: AC1"])
        self.approve(self.resource_alias(spec, "A.MD"))
        with self.assertRaisesRegex(ValueError, "Stagnation"):
            WorkflowController(self.store).reserve_cycle("identity", "renamed-a-stage")
        self.assertEqual(read_record(self.store.db, "budget", "identity")["payload"]["task_cycles"], 3)

    def test_local_dot_alias_cannot_reset_original_cycle_cap(self):
        spec = self.contract(max_stage_cycles=1)
        (self.store.repo / "a.md").write_text("Original deployment procedure\n")
        self.approve(spec)
        self.controller.reserve_cycle("identity", "a-stage")
        self.approve(self.resource_alias(spec, "./a.md"))
        with self.assertRaisesRegex(ValueError, "budget"):
            WorkflowController(self.store).reserve_cycle("identity", "renamed-a-stage")

    def test_resource_case_rules_preserve_uri_opaque_and_posix_identity(self):
        self.assertFalse(_overlap(["https://example.invalid/Resource"], ["https://example.invalid/resource"]))
        self.assertFalse(_overlap(["urn:resource:Identifier"], ["urn:resource:identifier"]))
        with patch("os.name", "posix"):
            self.assertFalse(_overlap(["A.md"], ["a.md"]))

    def test_conflicting_criterion_identity_keeps_exact_check_and_scope_links(self):
        spec = self.contract()
        history = initial_history(spec)
        selected = bind_stage(history, "identity", self.relabel(spec), "renamed-a-stage", root_id="identity")
        # Conflicting identity evidence conservatively shares both histories.
        self.assertEqual(selected, ["a-stage", "b-stage"])

    def test_reused_foreign_criterion_id_cannot_hide_original_scope_when_check_changes(self):
        spec = self.contract(max_stage_cycles=1)
        self.approve(spec)
        self.controller.reserve_cycle("identity", "a-stage")
        revised = self.relabel(spec)
        revised["evidence"][0]["required"] = False
        self.approve(revised)
        with self.assertRaisesRegex(ValueError, "budget"):
            self.controller.reserve_cycle("identity", "renamed-a-stage")

    def test_unchanged_original_check_links_history_even_when_revised_scope_matches_another_stage(self):
        spec = self.contract()
        history = initial_history(spec)
        revised = self.relabel(spec)
        revised["stages"][0]["scope"] = copy.deepcopy(spec["stages"][1]["scope"])
        selected = bind_stage(history, "identity", revised, "renamed-a-stage", root_id="identity")
        self.assertEqual(selected, ["a-stage", "b-stage"])

    def test_unchanged_stage_keeps_independent_allowance_despite_shared_check_and_resource(self):
        spec = self.contract(max_stage_cycles=1)
        spec["scope"]["resources"].append("shared.md")
        for stage in spec["stages"]:
            stage["scope"]["resources"].append("shared.md")
        # Unlinked project-wide evidence belongs to both stages.
        spec["evidence"].append({"id": "shared", "kind": "artifact", "path": "shared.md"})
        self.approve(spec)
        self.controller.reserve_cycle("identity", "a-stage")
        result = WorkflowController(self.store).reserve_cycle("identity", "b-stage")
        self.assertEqual(result["stage_cycles"], 1)
        history = read_record(self.store.db, "recovery", "identity")["payload"]
        self.assertEqual(history["aliases"]["identity:b-stage"], ["b-stage"])

    def test_delegate_of_relabelled_stage_inherits_original_cycle_cap(self):
        spec = self.contract(max_stage_cycles=1)
        self.approve(spec)
        self.controller.reserve_cycle("identity", "a-stage")
        revised = self.relabel(spec)
        self.approve(revised)
        child = copy.deepcopy(revised)
        child.update(id="delegate", parent_task_id="identity", parent_stage_id="renamed-a-stage")
        child["scope"] = copy.deepcopy(revised["stages"][0]["scope"])
        child["stages"] = [copy.deepcopy(revised["stages"][0])]
        child["stages"][0]["id"] = "delegated-a-stage"
        child["evidence"], child["acceptance"] = child["evidence"][:1], child["acceptance"][:1]
        self.tasks.create(child)
        with self.assertRaisesRegex(ValueError, "budget"):
            WorkflowController(self.store).reserve_cycle("delegate", "delegated-a-stage")


if __name__ == "__main__":
    unittest.main()
