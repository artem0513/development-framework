"""Mixed tasks preserve each concrete domain and every declared obligation.

Receipt-validator doubles isolate deterministic gate mapping only; they never
stand in for independent semantic acceptance.
"""
from contextlib import closing
import copy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

from ase.__main__ import _task_command
from ase.core import execute
from ase.gates import GateService, source_snapshot, stage_checks, verification_context_hash
from ase.journal import Journal
from ase import profiles
from ase.state import hash_value, read_record, write_record
from ase.store import Store
from ase.tasks import TaskService, validate_contract_v2
from ase.workflow import WorkflowController


def mixed_contract(task_id="mixed"):
    return {"id": task_id, "schema_version": 2, "profile": "mixed", "request": "Produce a verified guide and sourced findings",
            "scope": {"resources": ["guide.md", "findings.md"], "effects": ["file_write"]},
            "acceptance": [{"id": "DOC", "expected": "Guide describes the operating process", "evidence_ids": ["guide"]},
                           {"id": "RESEARCH", "expected": "Findings cite inspected sources", "evidence_ids": ["findings"]}],
            "evidence": [{"id": "guide", "kind": "artifact", "path": "guide.md"},
                         {"id": "findings", "kind": "artifact", "path": "findings.md"}],
            "stages": [{"id": "guide", "profile": "document", "acceptance_ids": ["DOC"], "evidence_ids": ["guide"],
                        "scope": {"resources": ["guide.md"], "effects": ["file_write"]}},
                       {"id": "findings", "profile": "research", "acceptance_ids": ["RESEARCH"], "evidence_ids": ["findings"],
                        "scope": {"resources": ["findings.md"], "effects": ["file_write"]}}]}


class MixedProfileTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="ase-mixed-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.root = self.base / "workspace"
        self.root.mkdir()
        (self.root / "guide.md").write_text("Confirmed process", encoding="utf-8")
        (self.root / "findings.md").write_text("Findings based on local source.md", encoding="utf-8")
        self.store = Store(self.root, self.base / "state")
        self.addCleanup(self.store.close)
        self.tasks = TaskService(self.store)

    def artifact(self, evidence_id, name):
        path = self.root / name
        return {"id": evidence_id, "kind": "artifact", "path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}

    def evaluate(self, contract, observations, stage_id=None):
        self.assertTrue(hasattr(profiles, "evaluate_contract_profiles"), "mixed stage-domain evaluator is required")
        return profiles.evaluate_contract_profiles(contract, contract["evidence"], observations, stage_id=stage_id)

    def approve(self, task_id):
        task = self.tasks.snapshot(task_id)
        task = self.tasks.propose(task_id, {"summary": "Implement the confirmed domain stages", "stage_ids": [s["id"] for s in task["contract"]["stages"]]}, expected_revision=task["revision"])
        return self.tasks.approve(task_id, proposal_hash=task["proposal_hash"], user_message_ref="chat:reviewed-mixed-plan", approved_stage_ids=[s["id"] for s in task["contract"]["stages"]])

    def captured_command(self, spec, label):
        result = execute(spec["argv"], self.root, self.base / (label + ".log"), timeout=10)
        return {**result, "id": spec["id"], "kind": spec["kind"], "configured_argv": spec["argv"]}

    def gate_evidence(self, task_id, *, completed_stages=None, include_roles=True):
        """Real files/DB/check hashes; stage and receipt fixtures isolate gate mapping."""
        task = self.tasks.snapshot(task_id)
        plan = self.tasks.execution_plan(task_id)
        source = source_snapshot(self.store, task["contract"], task_id=task_id)
        results = []
        for spec in plan["checks"]:
            if spec["kind"] == "artifact":
                results.append(self.artifact(spec["id"], spec["path"]))
            else:
                log = self.store.root / "tasks" / task_id / "checks" / "controlled" / (spec["id"] + ".log")
                result = execute(spec["argv"], self.root, log, timeout=10)
                results.append({**result, "id": spec["id"], "kind": spec["kind"], "configured_argv": spec["argv"]})
        bundle = {"kind": "checks", "task_id": task_id, "workspace_id": self.store.workspace_id,
                  "contract_hash": task["contract_hash"], "plan_hash": plan["plan_hash"], "fingerprint": source["fingerprint"], "results": results}
        with self.store.db:
            previous = read_record(self.store.db, "checks", task_id)
            write_record(self.store.db, "checks", task_id, bundle, expected_revision=previous["revision"])
            for role in ("ac", "review"):
                if not include_roles:
                    continue
                key = task_id + ":" + role
                old = read_record(self.store.db, "verification", key)
                write_record(self.store.db, "verification", key, {"role": role, "execution_id": task_id + "-" + role,
                             "artifact_root": str(self.store.root / "tasks" / task_id / "fixture"), "fixture_task_id": task_id,
                             "bindings": {"fingerprint": source["fingerprint"], "contract_sha256": task["contract_hash"],
                             "proposal_sha256": task["proposal_hash"], "plan_sha256": plan["plan_hash"],
                             "check_sha256": hash_value(bundle), "workspace_id": self.store.workspace_id,
                             "verification_context_sha256": verification_context_hash(self.store, task["contract"], task_id)}}, expected_revision=old["revision"])
            for stage in task["contract"]["stages"]:
                if completed_stages is not None and stage["id"] not in completed_stages:
                    continue
                key = task_id + ":" + stage["id"]
                old = read_record(self.store.db, "stage", key)
                write_record(self.store.db, "stage", key, {"state": "COMPLETED", "contract_hash": task["contract_hash"],
                             "proposal_hash": task["proposal_hash"]}, expected_revision=old["revision"])

    def validator_fixture(self, receipt, **kwargs):
        """A validator-only double is not independent semantic acceptance."""
        task = self.tasks.snapshot(receipt["fixture_task_id"])
        payload = {"criteria": [{"id": ac["id"], "expected": ac["expected"], "status": "PASS",
                   "evidence": ["actual local artifact hashes"], "method": "fixture isolates domain mapping", "missing_coverage": []}
                   for ac in task["contract"]["acceptance"]]} if receipt["role"] == "ac" else {"findings": [], "summary": "Protocol-only fixture for mixed-profile routing; no independent semantic acceptance."}
        return {"evidence_kind": "independent_cli", "result": {"payload": payload, "session_id": receipt["execution_id"]}}

    def test_flat_mixed_cannot_pass_as_artifact_only(self):
        result = profiles.evaluate_profile("mixed", [{"id": "guide", "kind": "artifact", "path": "guide.md"}], [self.artifact("guide", "guide.md")])
        self.assertEqual(result["status"], "BLOCKED")
        self.assertIn("MIXED_PROFILE_UNMAPPED", " ".join(result["blocked_reasons"]))

    def test_mixed_contract_requires_concrete_stages_and_complete_maps(self):
        variations = []
        c = mixed_contract(); del c["stages"][0]["profile"]; variations.append(c)
        c = mixed_contract(); c["stages"][0]["profile"] = "mixed"; variations.append(c)
        c = mixed_contract(); del c["stages"][0]["evidence_ids"]; variations.append(c)
        c = mixed_contract(); c["stages"][1]["acceptance_ids"] = []; variations.append(c)
        c = mixed_contract(); c["stages"][0]["evidence_ids"] = ["findings"]; variations.append(c)
        c = mixed_contract(); c["evidence"].append({"id": "unmapped", "kind": "artifact", "path": "guide.md"}); variations.append(c)
        c = mixed_contract(); c["stages"][0]["required_child_task_id"] = "../escape"; variations.append(c)
        c = mixed_contract(); c["stages"][0]["evidence_ids"] = [{}]; variations.append(c)
        for index, value in enumerate(variations):
            with self.subTest(index=index), self.assertRaises(ValueError):
                validate_contract_v2(value)

    def test_document_and_research_profiles_aggregate_without_software_rules(self):
        c = validate_contract_v2(mixed_contract())
        result = self.evaluate(c, [self.artifact("guide", "guide.md"), self.artifact("findings", "findings.md")])
        self.assertEqual(result["status"], "PASS")
        self.assertEqual({r["profile"] for r in result["stage_profiles"].values()}, {"document", "research"})

    def test_mixed_software_requires_actual_positive_tests_and_infra_requires_probe(self):
        c = mixed_contract()
        c["stages"][0]["profile"] = "software"
        c["stages"][1]["profile"] = "infrastructure"
        observations = [self.artifact("guide", "guide.md"), self.artifact("findings", "findings.md")]
        result = self.evaluate(c, observations)
        self.assertEqual(result["status"], "BLOCKED")
        self.assertTrue(any("executable tests" in reason for reason in result["blocked_reasons"]))
        self.assertTrue(any("postcondition probe" in reason for reason in result["blocked_reasons"]))
        unit = {"id": "unit", "kind": "test", "parser": "unittest", "argv": [sys.executable, "-c",
            "import unittest\nclass Proof(unittest.TestCase):\n def test_actual(self): self.assertEqual(2+2,4)\nunittest.main()"]}
        probe = {"id": "probe", "kind": "probe", "argv": [sys.executable, "-c", "print('{\"ready\":true}')"], "predicate": {"path": "ready", "op": "eq", "value": True}}
        c["evidence"] += [unit, probe]
        c["stages"][0]["evidence_ids"].append("unit")
        c["stages"][1]["evidence_ids"].append("probe")
        observations += [self.captured_command(unit, "actual-unit"), self.captured_command(probe, "actual-probe")]
        result = self.evaluate(c, observations)
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["stage_profiles"]["guide"]["by_id"]["unit"]["counts"]["total"], 1)
        observations[-1] = self.captured_command({**probe, "argv": [sys.executable, "-c", "print('{\"ready\":false}')"]}, "false-probe")
        observations[-1]["configured_argv"] = probe["argv"]
        self.assertEqual(self.evaluate(c, observations)["status"], "REJECTED")

    def test_selected_document_stage_cannot_borrow_other_stage_tests(self):
        c = mixed_contract()
        c["stages"][0]["profile"] = "software"
        observations = [self.artifact("guide", "guide.md"), self.artifact("findings", "findings.md")]
        self.assertEqual(self.evaluate(c, observations, "findings")["status"], "PASS")
        self.assertEqual(self.evaluate(c, observations, "guide")["status"], "BLOCKED")
        self.assertEqual([s["id"] for s in stage_checks(c, c["evidence"], "findings")], ["findings"])

    def test_mixed_software_zero_tests_remain_blocked(self):
        c = mixed_contract()
        c["stages"][0]["profile"] = "software"
        zero = {"id": "zero", "kind": "test", "parser": "unittest", "argv": [sys.executable, "-c", "import unittest; unittest.TextTestRunner().run(unittest.TestSuite())"]}
        c["evidence"].append(zero)
        c["stages"][0]["evidence_ids"].append("zero")
        observations = [self.artifact("guide", "guide.md"), self.artifact("findings", "findings.md"), self.captured_command(zero, "zero-tests")]
        self.assertEqual(self.evaluate(c, observations)["status"], "BLOCKED")

    def test_all_four_domains_have_real_evidence_but_no_fabricated_semantic_approval(self):
        subprocess.run(["git", "init", str(self.root)], check=True, capture_output=True)
        c = mixed_contract("four-domains")
        unit = {"id": "unit", "kind": "test", "parser": "unittest", "argv": [sys.executable, "-c",
                "import unittest\nclass Actual(unittest.TestCase):\n def test_result(self): self.assertTrue(True)\nunittest.main()"]}
        probe = {"id": "ready", "kind": "probe", "argv": [sys.executable, "-c", "print('{\"ready\":true}')"],
                 "predicate": {"path": "ready", "op": "eq", "value": True}}
        c["evidence"] += [unit, probe]
        c["acceptance"] += [{"id": "CODE", "expected": "Actual software tests execute", "evidence_ids": ["unit"]},
                            {"id": "INFRA", "expected": "Actual resource readiness is observed", "evidence_ids": ["ready"]}]
        c["stages"] += [{"id": "code", "profile": "software", "acceptance_ids": ["CODE"], "evidence_ids": ["unit"]},
                        {"id": "resource", "profile": "infrastructure", "acceptance_ids": ["INFRA"], "evidence_ids": ["ready"]}]
        self.tasks.create(c)
        self.approve("four-domains")
        self.gate_evidence("four-domains", completed_stages=[], include_roles=False)
        result = GateService(self.store).gate("four-domains")
        self.assertEqual(result["profile_evidence"]["status"], "PASS")
        self.assertEqual({r["profile"] for r in result["profile_evidence"]["stage_profiles"].values()},
                         {"software", "infrastructure", "document", "research"})
        self.assertEqual(result["status"], "BLOCKED")
        self.assertTrue(any("Independent ac" in reason for reason in result["blocked_reasons"]))

    def test_full_gate_requires_all_stage_domains_and_fresh_required_child(self):
        c = mixed_contract("parent")
        c["stages"][0]["required_child_task_id"] = "required-guide"
        self.tasks.create(c)
        self.approve("parent")
        child = {"id": "required-guide", "schema_version": 2, "profile": "document", "request": "Implement the required guide",
                 "scope": c["stages"][0]["scope"], "parent_task_id": "parent", "parent_stage_id": "guide",
                 "acceptance": [c["acceptance"][0]], "evidence": [c["evidence"][0]], "stages": [{"id": "write", "acceptance_ids": ["DOC"]}]}
        self.tasks.create(child)
        self.gate_evidence("required-guide")
        self.gate_evidence("parent", completed_stages=["guide"])
        with patch("ase.gates.verify_receipt", side_effect=self.validator_fixture):
            # Complete the child through the actual terminal gate, not a stored PASS.
            current = self.tasks.snapshot("required-guide")
            self.tasks.transition("required-guide", "COMPLETED", expected_revision=current["revision"], reason="Controlled deterministic gate-mapping probe")
            partial = GateService(self.store).gate("parent")
            self.assertEqual(partial["status"], "BLOCKED")
            self.assertIn("Stage incomplete: findings", partial["blocked_reasons"])
            self.gate_evidence("parent")
            full = GateService(self.store).gate("parent")
            self.assertEqual(full["status"], "APPROVED")
            self.assertEqual(set(full["profile_evidence"]["stage_profiles"]), {"guide", "findings"})
            (self.root / "guide.md").write_text("Changed after the child's verification", encoding="utf-8")
            stale = GateService(self.store).gate("parent")
            self.assertNotEqual(stale["status"], "APPROVED")
            self.assertTrue(any("Required child evidence" in reason for reason in stale["blocked_reasons"]))

    def test_existing_flat_mixed_records_get_typed_blocker(self):
        self.tasks.create(mixed_contract("old-flat"))
        self.approve("old-flat")
        with self.store.db:
            record = read_record(self.store.db, "task", "old-flat")
            del record["payload"]["contract"]["stages"][0]["profile"]
            write_record(self.store.db, "task", "old-flat", record["payload"], expected_revision=record["revision"])
        result = GateService(self.store).gate("old-flat")
        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual(result["blocker"], "MIXED_PROFILE_UNMAPPED")
        with self.assertRaisesRegex(ValueError, "MIXED_PROFILE_UNMAPPED"):
            self.tasks.require_approval("old-flat", "guide")

    def test_completed_software_child_can_refresh_read_only_proof_after_document_stage(self):
        """Validator fixtures isolate refresh routing; actual software tests rerun."""
        subprocess.run(["git", "init", str(self.root)], check=True, capture_output=True)
        c = mixed_contract("refresh-parent")
        c["stages"][0].update(profile="software", required_child_task_id="software-child")
        unit = {"id": "unit", "kind": "test", "parser": "unittest", "argv": [sys.executable, "-c",
                "import unittest\nclass Actual(unittest.TestCase):\n def test_result(self): self.assertEqual(3+3,6)\nunittest.main()"]}
        c["evidence"].append(unit)
        c["stages"][0]["evidence_ids"].append("unit")
        self.tasks.create(c)
        self.approve("refresh-parent")
        child = {"id": "software-child", "schema_version": 2, "profile": "software", "request": "Implement the software guide obligation",
                 "scope": c["stages"][0]["scope"], "parent_task_id": "refresh-parent", "parent_stage_id": "guide",
                 "acceptance": [c["acceptance"][0]], "evidence": [c["evidence"][0], unit],
                 "stages": [{"id": "build", "acceptance_ids": ["DOC"]}]}
        self.tasks.create(child)
        WorkflowController(self.store).reserve_cycle("software-child", "build")
        self.gate_evidence("software-child")
        journal = Journal(self.store)
        journal.start_session("software-child", "original-session")

        def bound_validator(receipt, *, expected_bindings=None):
            if receipt.get("bindings") != expected_bindings:
                raise ValueError("Stale controlled validator binding")
            return self.validator_fixture(receipt)

        roles = []

        def refresh_role(adapter, request, **kwargs):
            roles.append(request["role"])
            return {"receipt": {"role": request["role"], "execution_id": uuid.uuid4().hex,
                    "artifact_root": str(self.store.root / "tasks" / "software-child" / "fixture"),
                    "fixture_task_id": "software-child", "bindings": request["bindings"]}}

        runner = SimpleNamespace(config={}, run=refresh_role)
        args = SimpleNamespace(task_id="software-child")
        with patch("ase.gates.verify_receipt", side_effect=bound_validator):
            journal.finish_session("software-child", "original-session", {"finish_reason": "completed", "summary": "Controlled child result checked",
                "next_action": "Write the parent research findings", "context": {"summary": "Software obligation completed"}},
                closure_key="immutable-child-closure", expected_revision=0, expected_context_revision=0)
            original_closure = read_record(self.store.db, "session_closure", "immutable-child-closure")["payload"]
            original_task = self.tasks.snapshot("software-child")
            original_budget = read_record(self.store.db, "budget", "refresh-parent")["payload"]
            (self.root / "findings.md").write_text("Later document stage changes the full Git fingerprint", encoding="utf-8")
            self.assertTrue(GateService(self.store).required_child_blockers("refresh-parent", "guide"))
            with patch.object(WorkflowController, "run", side_effect=AssertionError("Terminal run/resume cannot implement again")):
                self.assertNotEqual(_task_command(self.store, "run", args)["status"], "APPROVED")
                self.assertNotEqual(_task_command(self.store, "resume", args)["status"], "APPROVED")
            # Refresh actual configured test receipts before requesting fresh roles.
            _task_command(self.store, "check", args)
            self.assertTrue(GateService(self.store).required_child_blockers("refresh-parent", "guide"))
            with patch.object(WorkflowController, "_runner", return_value=runner):
                refreshed = _task_command(self.store, "verify", args)
            self.assertEqual(roles, ["ac", "review"])
            self.assertEqual(refreshed["status"], "APPROVED")
            self.assertEqual(GateService(self.store).required_child_blockers("refresh-parent", "guide"), [])
            self.assertEqual(read_record(self.store.db, "session_closure", "immutable-child-closure")["payload"], original_closure)
            current = self.tasks.snapshot("software-child")
            self.assertEqual((current["state"], current["revision"]), ("COMPLETED", original_task["revision"]))
            budget = read_record(self.store.db, "budget", "refresh-parent")["payload"]
            self.assertEqual(budget["task_cycles"], original_budget["task_cycles"])
            self.assertEqual(budget["stages"], original_budget["stages"])
            with self.assertRaisesRegex(ValueError, "terminal"):
                self.tasks.require_approval("software-child", "build")

    def test_software_stage_cannot_be_weakened_by_document_or_mixed_child(self):
        c = mixed_contract("parent")
        c["stages"][0]["profile"] = "software"
        self.tasks.create(c)
        self.approve("parent")
        child = {"id": "child", "schema_version": 2, "profile": "document", "request": "Implement the software obligation",
                 "scope": c["stages"][0]["scope"], "parent_task_id": "parent", "parent_stage_id": "guide",
                 "acceptance": [c["acceptance"][0]], "evidence": [c["evidence"][0]], "stages": [{"id": "write", "acceptance_ids": ["DOC"]}]}
        with self.assertRaisesRegex(ValueError, "profile|domain"):
            self.tasks.create(child)
        child["profile"] = "mixed"
        child["stages"][0].update(profile="document", evidence_ids=["guide"])
        with self.assertRaisesRegex(ValueError, "profile|domain"):
            self.tasks.create(child)

    def test_required_child_missing_incomplete_mismatched_and_stale_are_blocked(self):
        c = mixed_contract("parent")
        c["stages"][0]["required_child_task_id"] = "required-guide"
        self.tasks.create(c)
        self.approve("parent")
        readiness = GateService(self.store).required_child_blockers("parent", stage_id="guide")
        self.assertEqual(readiness, ["Required child missing: required-guide"])
        gate = GateService(self.store).gate("parent", stage_id="guide")
        self.assertTrue(any("Required child missing" in reason for reason in gate["blocked_reasons"]))
        child = {"id": "required-guide", "schema_version": 2, "profile": "document", "request": "Implement the required guide",
                 "scope": c["stages"][0]["scope"], "parent_task_id": "parent", "parent_stage_id": "guide",
                 "acceptance": [c["acceptance"][0]], "evidence": [c["evidence"][0]], "stages": [{"id": "write", "acceptance_ids": ["DOC"]}]}
        wrong = copy.deepcopy(child); wrong["acceptance"][0]["expected"] = "Weakened business obligation"
        with self.assertRaisesRegex(ValueError, "obligation|acceptance"):
            self.tasks.create(wrong)
        self.tasks.create(child)
        gate = GateService(self.store).gate("parent", stage_id="guide")
        self.assertTrue(any("Required child incomplete" in reason for reason in gate["blocked_reasons"]))
        with self.store.db:
            current = read_record(self.store.db, "task", "required-guide")
            write_record(self.store.db, "task", "required-guide", {**current["payload"], "state": "COMPLETED"}, expected_revision=current["revision"])
        gate = GateService(self.store).gate("parent", stage_id="guide")
        self.assertTrue(any("Required child evidence" in reason for reason in gate["blocked_reasons"]))

    def test_mixed_with_software_uses_git_snapshot_and_protected_guards(self):
        subprocess.run(["git", "init", str(self.root)], check=True, capture_output=True)
        c = mixed_contract("software-parent")
        c["stages"][0]["profile"] = "software"
        self.tasks.create(c)
        self.approve("software-parent")
        before = source_snapshot(self.store, self.tasks.snapshot("software-parent")["contract"], task_id="software-parent")
        (self.root / "outside-declared-scope.py").write_text("changed = True", encoding="utf-8")
        after = source_snapshot(self.store, self.tasks.snapshot("software-parent")["contract"], task_id="software-parent")
        self.assertNotEqual(before["fingerprint"], after["fingerprint"])
        with self.store.db:
            write_record(self.store.db, "baseline", "software-parent", before, expected_revision=0)
        protected = self.root / ".github" / "workflows" / "ci.yml"
        protected.parent.mkdir(parents=True)
        protected.write_text("name: changed", encoding="utf-8")
        verdict = GateService(self.store).gate("software-parent", stage_id="guide")
        self.assertTrue(any("Unauthorized protected" in reason for reason in verdict["rejected_reasons"]))


if __name__ == "__main__":
    unittest.main()
