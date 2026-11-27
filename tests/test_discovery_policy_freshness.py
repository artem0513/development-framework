"""Real snapshots/transport invalidate stale policy; no semantic approval mock."""
import copy
import hashlib
import sys
import unittest

from ase.adapters.codex import CodexAdapter
from ase.gates import GateService, source_snapshot
from ase.runner import Runner, verify_receipt
import test_audit_source_evidence as source_fixtures
import test_runner as transport


class DiscoveryPolicyFreshnessTests(unittest.TestCase):
    setUp = source_fixtures.SourceEvidenceAuditTests.setUp
    write = source_fixtures.SourceEvidenceAuditTests.write
    task = source_fixtures.SourceEvidenceAuditTests.task
    discovery = source_fixtures.SourceEvidenceAuditTests.discovery
    save = source_fixtures.SourceEvidenceAuditTests.save
    intake = source_fixtures.SourceEvidenceAuditTests.intake

    def test_required_policy_refresh_invalidates_real_old_transport_binding_and_checks(self):
        value = self.task()
        self.write("policy.md", "Mandatory governing policy")
        self.save(self.discovery(dependencies=[{"path": "policy.md", "reason": "Governs guide", "required": False}]))
        self.controller.check("source-audit", "draft")
        before = source_snapshot(self.store, value, "source-audit")
        request = self.controller._request("source-audit", "ac", transport.SCHEMA, {"stage_id": "draft"})
        self.assertNotIn("policy.md", request["source_packet"]["coverage"]["included"])
        fixture = self.base / "provider.py"
        fixture.write_text(transport.FIXTURE, encoding="utf-8")
        trusted = self.base / "trusted"
        trusted.mkdir()
        runner = Runner({"executable": sys.executable, "executable_args": [str(fixture), "success", "codex"],
                         "trusted_cwd": str(trusted), "protocol_only": True}, self.base / "sessions")
        receipt = runner.run(CodexAdapter(), request, timeout=5)["receipt"]
        self.assertEqual(receipt["evidence_kind"], "protocol_only")
        self.assertTrue(verify_receipt(receipt, expected_bindings=request["bindings"])["valid"])
        self.save(self.discovery(dependencies=[{"path": "policy.md", "reason": "Governs guide", "required": True}]), 1)
        after = source_snapshot(self.store, value, "source-audit")
        current = self.controller._request("source-audit", "ac", transport.SCHEMA, {"stage_id": "draft"})
        self.assertEqual(before["files"], after["files"])
        self.assertIn("policy.md", current["source_packet"]["coverage"]["included"])
        self.assertNotEqual(request["bindings"], current["bindings"])
        with self.assertRaises(ValueError):
            verify_receipt(receipt, expected_bindings=current["bindings"])
        gate = GateService(self.store).gate("source-audit", stage_id="draft")
        self.assertTrue(any("Stale check evidence: fingerprint" in reason for reason in gate["reasons"]))

    def test_equivalent_refresh_ignores_probe_observations_order_defaults_and_acquisition_times(self):
        value = self.task()
        self.write("policy.md", "Mandatory governing policy")
        self.write("other.md", "Another governing policy")
        payload = self.discovery(resources=["guide.md", "other.md"], dependencies=[
            {"path": "policy.md", "reason": "Governs guide", "blocking_stage_ids": ["draft", "publish"]}])
        payload["intake"] = self.intake("AVAILABLE")
        self.save(payload)
        before = source_snapshot(self.store, value, "source-audit")
        equivalent = copy.deepcopy(payload)
        equivalent["generated_at"] = "2026-11-13T13:00:00Z"
        equivalent["scope"]["resources"].reverse()
        equivalent["resources"].reverse()
        equivalent["resources"][0]["status"] = "UNKNOWN"
        for dependency in [*equivalent["scope"]["dependencies"], *equivalent["dependencies"]]:
            dependency["required"] = True
            if "blocking_stage_ids" in dependency:
                dependency["blocking_stage_ids"].reverse()
        equivalent["intake"]["source_refs"][0].update(status=" available ", observed_at="2026-11-13T13:00:00Z")
        for tool in equivalent["tools"]:
            tool.update(version="different observed probe version", available=False)
        equivalent["limitations"] = ["Fresh capability probe observation"]
        self.save(equivalent, 1)
        after = source_snapshot(self.store, value, "source-audit")
        self.assertEqual(before["fingerprint"], after["fingerprint"])
        self.assertEqual(before.get("discovery_policy_sha256"), after.get("discovery_policy_sha256"))
        self.assertIsNotNone(after.get("discovery_policy_sha256"))

    def test_coverage_scope_dependency_representation_and_intake_content_are_material(self):
        value = self.task()
        self.write("policy.md", "Mandatory governing policy")
        readable = self.write("guide.txt", "Confirmed guide")
        payload = self.discovery(dependencies=[{"path": "policy.md", "reason": "Governs guide"}])
        payload["representations"] = [{"artifact_path": "guide.md", "artifact_sha256": hashlib.sha256((self.repo / "guide.md").read_bytes()).hexdigest(),
                                       "path": "guide.txt", "sha256": hashlib.sha256(readable.read_bytes()).hexdigest(), "method": "Initial structural extraction"}]
        payload["intake"] = self.intake("AVAILABLE")
        self.save(payload)
        baseline = source_snapshot(self.store, value, "source-audit")
        cases = ("required", "stage_impact", "reason", "origin", "output", "representation_method", "intake_claim", "intake_reference", "intake_cue")
        for revision, case in enumerate(cases, 1):
            with self.subTest(case=case):
                changed = copy.deepcopy(payload)
                declarations = [*changed["scope"]["dependencies"], *changed["dependencies"]]
                if case == "required":
                    for item in declarations: item["required"] = False
                elif case == "stage_impact":
                    for item in declarations: item["blocking_stage_ids"] = ["publish"]
                elif case == "reason":
                    for item in declarations: item["reason"] = "New governing purpose"
                elif case == "origin":
                    for item in declarations: item["from"] = "guide.md"
                elif case == "output": changed["scope"]["outputs"] = ["guide.md"]
                elif case == "representation_method": changed["representations"][0]["method"] = "Revised structural interpretation"
                elif case == "intake_claim": changed["intake"]["outcomes"][0]["text"] = "Prepare guide with the new required content"
                elif case == "intake_reference": changed["intake"]["source_refs"][0]["reference"] = "local:policy.md#different-source-version"
                elif case == "intake_cue": changed["intake"]["source_refs"][0]["timestamp"] = "00:01:00-00:02:00"
                self.save(changed, revision)
                observed = source_snapshot(self.store, value, "source-audit")
                self.assertEqual(baseline["files"], observed["files"])
                self.assertNotEqual(baseline["fingerprint"], observed["fingerprint"])
                self.assertNotEqual(baseline.get("discovery_policy_sha256"), observed.get("discovery_policy_sha256"))

    def test_no_discovery_and_no_task_observation_keep_original_byte_fingerprint(self):
        value = self.task()
        without_task = source_snapshot(self.store, value)
        without_discovery = source_snapshot(self.store, value, "source-audit")
        self.assertEqual(without_task["fingerprint"], without_discovery["fingerprint"])
        self.assertNotIn("discovery_policy_sha256", without_task)
        self.assertNotIn("discovery_policy_sha256", without_discovery)
        self.save(self.discovery())
        scoped = source_snapshot(self.store, value, "source-audit")
        self.assertNotEqual(scoped["fingerprint"], without_task["fingerprint"])
        self.assertEqual(source_snapshot(self.store, value)["fingerprint"], without_task["fingerprint"])


if __name__ == "__main__":
    unittest.main()
