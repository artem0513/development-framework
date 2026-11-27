"""Real disposable effects and protocol-only proposal capture, not semantic AC."""
import hashlib
import json
import copy
import os
from pathlib import Path
import sys
import subprocess
import shutil
import tempfile
import unittest
from unittest.mock import patch

from ase.adapters import get_adapter
from ase.core import execute
from ase.core import validate_command_argv
from ase.gates import source_snapshot
from ase.operations import OperationLedger
from ase.runner import Runner
from ase.state import hash_value, read_record, transaction, write_record
from ase.store import Store
from ase.tasks import TaskService
from ase.workflow import AC_SCHEMA, EDIT_SCHEMA, REVIEW_SCHEMA, WorkflowController, apply_edits
from test_runner import FIXTURE


class ActionPacketEvidenceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="ase-action-packet-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.repo = self.base / "work"
        self.repo.mkdir()
        self.store = Store(self.repo, self.base / "state")
        self.addCleanup(self.store.close)
        self.tasks = TaskService(self.store)
        self.controller = WorkflowController(self.store)
        self.action = {"id": "deliver", "argv": [sys.executable, "-c",
            "from pathlib import Path;p=Path('count');p.write_text(str(int(p.read_text())+1) if p.exists() else '1');Path('result.txt').write_text('delivered');print('actual executor delivery')"],
            "observation_argv": [sys.executable, "-c",
                "from pathlib import Path;import json;print(json.dumps({'exists':Path('result.txt').exists(),'resource_id':'disposable-delivery'}))"]}
        scope = {"resources": ["guide.py", "result.txt", "count"], "effects": ["file_write", "command"]}
        spec = {"id": "evidence", "schema_version": 2, "profile": "document", "request": "Write a guide, then execute delivery exactly once.",
            "scope": scope, "acceptance": [{"id": "A", "expected": "Actual delivery matches the guide", "evidence_ids": ["result"]}],
            "evidence": [{"id": "result", "kind": "artifact", "path": "result.txt"}],
            "stages": [{"id": "deliver", "scope": scope, "acceptance_ids": ["A"], "depends_on": [], "actions": [self.action]}]}
        task = self.tasks.create(spec)
        task = self.tasks.propose("evidence", {"summary": spec["request"], "stage_ids": ["deliver"]}, expected_revision=task["revision"])
        self.task = self.tasks.approve("evidence", proposal_hash=task["proposal_hash"], user_message_ref="test:disposable-action", approved_stage_ids=["deliver"])
        self.stage = self.task["contract"]["stages"][0]
        (self.repo / "guide.py").write_text("before guide\n")
        self.controller._save("baseline", "evidence", source_snapshot(self.store, self.task["contract"], task_id="evidence"))
        self.operation_id = hash_value({"task": "evidence", "stage": "deliver", "action": "deliver"})

    def proposal(self, *, stage=None, empty=False):
        stage = stage or self.stage
        trusted = self.base / "trusted"
        trusted.mkdir(exist_ok=True)
        fixture = trusted / "provider.py"
        before = hashlib.sha256((self.repo / "guide.py").read_bytes()).hexdigest()
        payload = {"edits": [{"path": "guide.py", "before_sha256": before, "content": "after guide\n", "operation": "write"}],
            "summary": "Protocol-only implementation proposal"}
        if empty:
            payload["edits"] = []
        fixture.write_text(FIXTURE.replace("payload = {'verdict':'PASS'}", "payload = " + repr(payload)))
        runner = Runner({"executable": sys.executable, "executable_args": [str(fixture), "success", "codex"],
            "trusted_cwd": str(trusted), "protocol_only": True}, self.store.root / "tasks/evidence/sessions")
        self.provider = runner
        result = runner.run(get_adapter("codex"), self.controller._request("evidence", "implementation", EDIT_SCHEMA, {"stage": stage}), timeout=3)
        self.assertEqual(result["result"]["status"], "COMPLETE")
        self.controller._save("implementation", "evidence", result["receipt"])
        # As in _run_stage: the task's first candidate is its pre-edit source capture.
        if not read_record(self.store.db, "baseline_implementation", "evidence")["payload"]:
            self.controller._save("baseline_implementation", "evidence", result["receipt"])
        apply_edits(self.repo, stage["scope"], payload["edits"])
        return result["receipt"]

    def manifest(self):
        packet = self.controller._packet("evidence", stage_id=self.stage["id"])
        item = next((item for item in packet["files"] if item["path"] == "_ase_context/action_evidence.json"), None)
        self.assertIsNotNone(item, "Action and proposal provenance must be supplied to actual verifier packets")
        return json.loads(item["content"]), packet

    def test_generic_execution_receipt_is_durable_without_provider_attempt_identity(self):
        self.controller.execute_actions("evidence", self.stage)
        operation = read_record(self.store.db, "operation", self.operation_id)["payload"]
        self.assertIn("execution_receipt", operation, "A generic successful command must retain its actual execution metadata")
        self.assertEqual(operation["state"], "APPLIED")
        self.assertNotIn("submission_binding", operation)
        self.assertEqual(operation["execution_receipt"]["attempt_number"], 1)
        self.assertEqual(operation["execution_receipt"]["execution"]["exit_code"], 0)
        self.controller.execute_actions("evidence", self.stage)
        self.assertEqual((self.repo / "count").read_text(), "1")

    def npm_action(self):
        (self.repo / "package.json").write_text(json.dumps({"private": True, "scripts": {
            "deliver": "node deliver.cjs", "observe": "node observe.cjs"}}), encoding="utf-8")
        (self.repo / "deliver.cjs").write_text("const fs=require('fs');fs.writeFileSync('count',String(fs.existsSync('count')?Number(fs.readFileSync('count'))+1:1));fs.writeFileSync('result.txt','delivered');console.log('owned offline npm delivery');", encoding="utf-8")
        (self.repo / "observe.cjs").write_text("const fs=require('fs');console.log(JSON.stringify({exists:fs.existsSync('result.txt'),resource_id:'disposable-delivery'}));", encoding="utf-8")
        contract = copy.deepcopy(self.task["contract"])
        contract["scope"]["resources"] += ["package.json", "deliver.cjs", "observe.cjs"]
        contract["stages"][0]["scope"] = copy.deepcopy(contract["scope"])
        action = contract["stages"][0]["actions"][0]
        action["argv"], action["observation_argv"] = ["npm", "run", "--silent", "deliver"], ["npm", "run", "--silent", "observe"]
        self.revise(contract)
        self.stage = self.tasks.snapshot("evidence")["contract"]["stages"][0]
        self.action = self.stage["actions"][0]
        with patch.dict(os.environ, {"npm_config_cache": str(self.base / "npm-cache"), "npm_config_update_notifier": "false",
                "npm_config_audit": "false", "npm_config_fund": "false", "npm_config_offline": "true"}):
            self.controller.execute_actions("evidence", self.stage)

    @unittest.skipUnless(os.name == "nt" and shutil.which("npm") and shutil.which("node"), "requires actual Windows npm and node")
    def test_real_windows_npm_action_and_observer_capture_launch_mapping(self):
        self.npm_action()
        manifest, packet = self.manifest()
        self.assertEqual((self.repo / "count").read_text(), "1")
        self.assertEqual(packet["coverage"]["missing"], [])
        operation = manifest["stages"][0]["actions"][0]
        self.assertEqual(operation["state"], "APPLIED")
        execution = operation["execution_receipt"]["execution"]
        self.assertEqual(execution["configured_argv"], self.action["argv"])
        self.assertNotEqual(execution["argv"], execution["configured_argv"])
        self.assertEqual(execution["launch_normalization"]["manager"], "npm")
        for observation in operation["observations"]:
            command = observation["observation"]["provenance"]["command_receipt"]
            self.assertEqual(command["configured_argv"], self.action["observation_argv"])
            self.assertEqual(command["launch_normalization"]["manager"], "npm")
            validate_command_argv(command, self.action["observation_argv"])
        with patch("ase.core.shutil.which", side_effect=AssertionError("Historical launch proof must not resolve current PATH")):
            validate_command_argv(execution, self.action["argv"])

    @unittest.skipUnless(os.name == "nt" and shutil.which("npm") and shutil.which("node"), "requires actual Windows npm and node")
    def test_mapped_package_manager_receipt_rejects_changed_tail_shape_and_identity(self):
        self.npm_action()
        receipt = read_record(self.store.db, "operation", self.operation_id)["payload"]["execution_receipt"]["execution"]
        def modify(value, case):
            proof = value["launch_normalization"]
            if case == "tail": value["argv"][-1] = "different-script"
            elif case == "extra-runtime-option": value["argv"].insert(1, "--eval")
            elif case == "manager": proof["manager"] = "pnpm"
            elif case == "entrypoint":
                proof["entrypoint"]["path"] = str(Path(proof["entrypoint"]["path"]).with_name("other.js"))
                value["argv"][1] = proof["entrypoint"]["path"]
            elif case == "runtime":
                proof["runtime"]["path"] = str(Path(proof["runtime"]["path"]).with_name("node.cmd"))
                value["argv"][0] = proof["runtime"]["path"]
            elif case == "device-runtime":
                proof["runtime"]["path"] = "//?/C:/Program Files/nodejs/node.exe"
                value["argv"][0] = proof["runtime"]["path"]
            elif case == "wrapper": proof["wrapper"]["path"] = str(Path(proof["wrapper"]["path"]).with_name("other.cmd"))
            elif case == "identity-hash": proof["runtime"]["sha256"] = "not-a-file-hash"
            elif case == "configured": value["configured_argv"][-1] = "different-script"
            elif case == "version": proof["version"] = 2
            elif case == "boolean-version": proof["version"] = True
            proof["executed_sha256"] = hash_value(value["argv"])
            proof["sha256"] = hash_value({key: item for key, item in proof.items() if key != "sha256"})
        for case in ("tail", "extra-runtime-option", "manager", "entrypoint", "runtime", "device-runtime", "wrapper", "identity-hash", "configured", "version", "boolean-version"):
            with self.subTest(case=case):
                changed = copy.deepcopy(receipt)
                modify(changed, case)
                with self.assertRaises(ValueError):
                    validate_command_argv(changed, self.action["argv"])

    @unittest.skipUnless(os.name == "nt" and shutil.which("npm") and shutil.which("node"), "requires actual Windows npm and node")
    def test_legacy_transformed_execution_without_launch_proof_is_unknown_and_not_replayed(self):
        self.npm_action()
        record = read_record(self.store.db, "operation", self.operation_id)
        legacy = copy.deepcopy(record["payload"])
        capture = legacy["execution_receipt"]
        capture["execution"].pop("configured_argv")
        capture["execution"].pop("launch_normalization")
        capture["sha256"] = hash_value({key: item for key, item in capture.items() if key != "sha256"})
        with transaction(self.store.db) as db:
            write_record(db, "operation", self.operation_id, legacy, expected_revision=record["revision"])
        manifest, packet = self.manifest()
        self.assertTrue(any("launch proof" in reason for reason in packet["coverage"]["missing"]))
        self.assertEqual(manifest["stages"][0]["actions"][0]["execution_coverage"], "UNVERIFIED_CAPTURE")
        with patch.dict(os.environ, {"npm_config_cache": str(self.base / "npm-cache"), "npm_config_update_notifier": "false", "npm_config_offline": "true"}):
            self.controller.execute_actions("evidence", self.stage)
        self.assertEqual((self.repo / "count").read_text(), "1")

    @unittest.skipUnless(os.name == "nt" and shutil.which("npm") and shutil.which("node"), "requires actual Windows npm and node")
    def test_legacy_transformed_observer_without_launch_proof_is_not_fabricated(self):
        self.npm_action()
        def legacy_observation(observation):
            proof = observation["provenance"]
            proof["command_receipt"].pop("configured_argv")
            proof["command_receipt"].pop("launch_normalization")
            proof["command_receipt_sha256"] = hash_value(proof["command_receipt"])
            proof["sha256"] = hash_value({key: item for key, item in proof.items() if key != "sha256"})
        record = read_record(self.store.db, "operation", self.operation_id)
        legacy = copy.deepcopy(record["payload"])
        legacy_observation(legacy["observation"])
        with transaction(self.store.db) as db:
            write_record(db, "operation", self.operation_id, legacy, expected_revision=record["revision"])
            for row in db.execute("SELECT sequence,payload FROM runtime_events WHERE kind='operation_reconciled'").fetchall():
                event = json.loads(row["payload"])
                legacy_observation(event["observation"])
                db.execute("UPDATE runtime_events SET payload=? WHERE sequence=?", (json.dumps(event), row["sequence"]))
        manifest, packet = self.manifest()
        self.assertTrue(any("launch proof" in reason for reason in packet["coverage"]["missing"]))
        self.assertEqual(manifest["stages"][0]["actions"][0]["observations"], [])
        self.assertEqual((self.repo / "count").read_text(), "1")

    def test_legacy_untransformed_execution_retains_truthful_direct_argv(self):
        self.controller.execute_actions("evidence", self.stage)
        record = read_record(self.store.db, "operation", self.operation_id)
        legacy = copy.deepcopy(record["payload"])
        capture = legacy["execution_receipt"]
        capture["execution"].pop("configured_argv")
        capture["sha256"] = hash_value({key: item for key, item in capture.items() if key != "sha256"})
        with transaction(self.store.db) as db:
            write_record(db, "operation", self.operation_id, legacy, expected_revision=record["revision"])
        manifest, packet = self.manifest()
        self.assertEqual(packet["coverage"]["missing"], [])
        self.assertEqual(manifest["stages"][0]["actions"][0]["execution_coverage"], "RECORDED")

    def test_packet_contains_hashed_before_after_and_execution_logs_plus_proposal_summary(self):
        receipt = self.proposal()
        self.controller.execute_actions("evidence", self.stage)
        manifest, packet = self.manifest()
        entry = manifest["stages"][0]
        proposal = entry["implementation_proposal"]
        self.assertEqual(proposal["execution_id"], receipt["execution_id"])
        self.assertTrue(proposal["proposal_only"])
        self.assertEqual([edit["path"] for edit in proposal["edits"]], ["guide.py"])
        self.assertEqual(proposal["edits"][0]["after_sha256"], hashlib.sha256(b"after guide\n").hexdigest())
        operation = entry["actions"][0]
        self.assertEqual(operation["state"], "APPLIED")
        self.assertEqual(operation["execution_coverage"], "RECORDED")
        self.assertEqual([item["observation"]["exists"] for item in operation["observations"]], [False, True])
        bodies = [item["content"] for item in packet["files"] if item["path"].startswith("_ase_context/action_logs/")]
        self.assertTrue(any("actual executor delivery" in body for body in bodies))
        self.assertEqual(packet["coverage"]["missing"], [])

    def test_pending_action_is_disclosed_without_missing_coverage(self):
        manifest, packet = self.manifest()
        self.assertEqual(manifest["stages"][0]["actions"][0]["state"], "NOT_STARTED")
        self.assertEqual(packet["coverage"]["missing"], [])

    def test_legacy_applied_action_retains_observations_without_inventing_or_replaying_execution(self):
        self.controller.execute_actions("evidence", self.stage)
        record = read_record(self.store.db, "operation", self.operation_id)
        legacy = {key: value for key, value in record["payload"].items() if key != "execution_receipt"}
        with transaction(self.store.db) as db:
            write_record(db, "operation", self.operation_id, legacy, expected_revision=record["revision"])
            db.execute("DELETE FROM runtime_events WHERE kind='operation_execution_recorded'")
        self.controller.execute_actions("evidence", self.stage)
        manifest, packet = self.manifest()
        operation = manifest["stages"][0]["actions"][0]
        self.assertEqual(operation["execution_coverage"], "LEGACY_NOT_RECORDED")
        self.assertNotIn("execution_receipt", operation)
        self.assertTrue(operation["observations"])
        self.assertEqual((self.repo / "count").read_text(), "1")
        self.assertEqual(packet["coverage"]["missing"], [])

    def test_tampered_execution_log_is_missing_coverage_and_not_supplied(self):
        self.controller.execute_actions("evidence", self.stage)
        operation = read_record(self.store.db, "operation", self.operation_id)["payload"]
        self.assertIn("execution_receipt", operation)
        log = Path(operation["execution_receipt"]["execution"]["log"])
        log.write_text("tampered material must not be supplied")
        _, packet = self.manifest()
        self.assertTrue(any("execution" in item.lower() for item in packet["coverage"]["missing"]))
        self.assertFalse(any("tampered material must not" in item["content"] for item in packet["files"]))

    def test_unrelated_or_abbreviated_action_folder_cannot_supply_observation_log(self):
        self.controller.execute_actions("evidence", self.stage)
        foreign = self.store.root / "tasks/evidence/actions" / self.operation_id[:12] / ("0" * 32 + ".log")
        foreign.parent.mkdir()
        foreign.write_text("PRIVATE_UNRELATED_LOG")
        record = read_record(self.store.db, "operation", self.operation_id)
        changed = dict(record["payload"])
        changed["observation"] = dict(changed["observation"], proof_ref=str(foreign) + "#sha256=" + hashlib.sha256(foreign.read_bytes()).hexdigest())
        with transaction(self.store.db) as db:
            write_record(db, "operation", self.operation_id, changed, expected_revision=record["revision"])
        _, packet = self.manifest()
        self.assertTrue(packet["coverage"]["missing"])
        self.assertFalse(any("PRIVATE_UNRELATED_LOG" in item["content"] for item in packet["files"]))

    def revise(self, contract):
        task = self.tasks.snapshot("evidence")
        task = self.tasks.revise("evidence", contract, expected_revision=task["revision"])
        task = self.tasks.propose("evidence", {"summary": "Explicit disposable evidence regression", "stage_ids": [stage["id"] for stage in contract["stages"]]}, expected_revision=task["revision"])
        self.task = self.tasks.approve("evidence", proposal_hash=task["proposal_hash"],
            user_message_ref="test:disposable-revision-" + str(task["revision"]),
            approved_stage_ids=[stage["id"] for stage in contract["stages"]])
        self.stage = self.task["contract"]["stages"][0]

    def two_stages(self):
        contract = copy.deepcopy(self.task["contract"])
        contract["scope"]["resources"].append("other.txt")
        other = {"id": "other", "scope": {"resources": ["other.txt"], "effects": ["file_write", "command"]},
            "acceptance_ids": ["A"], "depends_on": [], "actions": [{"id": "other", "argv": [sys.executable, "-c",
                "from pathlib import Path;Path('other.txt').write_text('done');print('other-stage log')"],
                "observation_argv": [sys.executable, "-c", "from pathlib import Path;import json;print(json.dumps({'exists':Path('other.txt').exists(),'resource_id':'other'}))"]}]}
        contract["stages"].append(other)
        self.revise(contract)
        return self.task["contract"]["stages"][1]

    def test_stage_filter_excludes_unrelated_action_logs(self):
        other = self.two_stages()
        self.controller.execute_actions("evidence", other)
        manifest, packet = self.manifest()
        self.assertEqual([stage["stage_id"] for stage in manifest["stages"]], ["deliver"])
        self.assertFalse(any("other-stage log" in item["content"] for item in packet["files"] if item["path"].startswith("_ase_context/action_logs/")))

    def test_captured_prior_stage_proposal_does_not_use_latest_other_stage_candidate(self):
        other = self.two_stages()
        original = self.proposal()
        self.controller.execute_actions("evidence", self.stage)
        self.controller.check("evidence", "deliver")
        fixture = Path(self.provider.config["executable_args"][0])
        body = """
schema = json.loads(pathlib.Path(args[9]).read_text())
payload = {'criteria':[]} if 'criteria' in schema['properties'] else {'findings':[],'summary':'Protocol history fixture only'}
"""
        fixture.write_text(FIXTURE.replace("payload = {'verdict':'PASS'}", body))
        for role, schema in (("ac", AC_SCHEMA), ("review", REVIEW_SCHEMA)):
            result = self.provider.run(get_adapter("codex"), self.controller._request("evidence", role, schema, {"stage_id": "deliver"}), timeout=3)
            self.assertEqual(result["result"]["status"], "COMPLETE")
            self.controller._save("verification", "evidence:" + role, result["receipt"])
        # Historical scheduler marker is a protocol-only seam, not semantic AC.
        self.controller._capture_stage("evidence", "deliver", {"status": "APPROVED", "fingerprint": "protocol-scheduler-fixture"})
        latest = self.proposal(stage=other, empty=True)
        self.assertNotEqual(original["execution_id"], latest["execution_id"])
        manifest, _ = self.manifest()
        proposal = manifest["stages"][0]["implementation_proposal"]
        self.assertEqual(proposal["execution_id"], original["execution_id"])
        self.assertEqual([edit["path"] for edit in proposal["edits"]], ["guide.py"])

    def test_log_truncation_is_explicit_missing_coverage(self):
        contract = copy.deepcopy(self.task["contract"])
        contract["stages"][0]["actions"][0]["argv"][-1] += ";print('x'*40000)"
        self.revise(contract)
        self.controller.execute_actions("evidence", self.stage)
        manifest, packet = self.manifest()
        self.assertTrue(any("truncated" in item.lower() for item in packet["coverage"]["missing"]))
        self.assertTrue(manifest["stages"][0]["actions"][0]["execution_receipt"]["log_evidence"]["truncated"])
        self.assertTrue(all(len(item["content"].encode()) <= 32768 for item in packet["files"] if item["path"].startswith("_ase_context/action_logs/")))

    def test_tampered_proposal_is_not_presented_as_verified_capture(self):
        receipt = self.proposal()
        Path(receipt["artifacts"]["request"]["path"]).write_text("tampered request")
        manifest, packet = self.manifest()
        self.assertEqual(manifest["stages"][0]["implementation_proposal"]["status"], "INVALID")
        self.assertTrue(any("Implementation proposal" in item for item in packet["coverage"]["missing"]))

    def test_pending_action_manifest_is_available_for_every_concrete_profile(self):
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True, capture_output=True)
        for profile in ("document", "research", "infrastructure", "software"):
            with self.subTest(profile=profile):
                spec = dict(copy.deepcopy(self.task["contract"]), id="profile-" + profile, profile=profile)
                task = self.tasks.create(spec)
                task = self.tasks.propose(task["task_id"], {"summary": "Profile transport fixture only", "stage_ids": ["deliver"]}, expected_revision=task["revision"])
                self.tasks.approve(task["task_id"], proposal_hash=task["proposal_hash"], user_message_ref="test:profile-transport", approved_stage_ids=["deliver"])
                packet = self.controller._packet(task["task_id"], stage_id="deliver")
                manifest = json.loads(next(item["content"] for item in packet["files"] if item["path"] == "_ase_context/action_evidence.json"))
                self.assertEqual(manifest["stages"][0]["actions"][0]["state"], "NOT_STARTED")
                self.assertEqual(packet["coverage"]["missing"], [])

    def test_same_root_renamed_stage_preserves_historical_execution_without_replay(self):
        self.controller.execute_actions("evidence", self.stage)
        contract = copy.deepcopy(self.task["contract"])
        contract["stages"][0]["id"] = "renamed"
        self.revise(contract)
        self.controller.execute_actions("evidence", self.stage)
        manifest, packet = self.manifest()
        operation = manifest["stages"][0]["actions"][0]
        self.assertEqual(packet["coverage"]["missing"], [])
        self.assertEqual(operation["execution_coverage"], "RECORDED")
        self.assertEqual(operation["execution_receipt"]["context"]["stage_id"], "deliver")
        self.assertEqual((self.repo / "count").read_text(), "1")

    def test_observer_only_recipe_change_keeps_origin_and_fresh_observation_distinct(self):
        self.controller.execute_actions("evidence", self.stage)
        contract = copy.deepcopy(self.task["contract"])
        contract["stages"][0]["actions"][0]["observation_argv"][-1] = contract["stages"][0]["actions"][0]["observation_argv"][-1].replace("disposable-delivery", "fresh-delivery")
        self.revise(contract)
        self.controller.execute_actions("evidence", self.stage)
        manifest, packet = self.manifest()
        self.assertEqual(packet["coverage"]["missing"], [])
        observations = manifest["stages"][0]["actions"][0]["observations"]
        self.assertTrue(any(item["observation"]["resource_id"] == "fresh-delivery" for item in observations))
        self.assertEqual((self.repo / "count").read_text(), "1")

    def test_same_root_child_may_observe_parent_execution_without_relabeling_it(self):
        self.controller.execute_actions("evidence", self.stage)
        spec = dict(copy.deepcopy(self.task["contract"]), id="child", parent_task_id="evidence", parent_stage_id="deliver")
        child = self.tasks.create(spec)
        self.controller.execute_actions("child", child["contract"]["stages"][0])
        packet = self.controller._packet("child", stage_id="deliver")
        manifest = json.loads(next(item["content"] for item in packet["files"] if item["path"] == "_ase_context/action_evidence.json"))
        self.assertEqual(packet["coverage"]["missing"], [])
        self.assertEqual(manifest["stages"][0]["actions"][0]["execution_receipt"]["context"]["task_id"], "evidence")
        self.assertEqual((self.repo / "count").read_text(), "1")

    def test_execution_argv_and_context_are_validated_against_original_declared_recipe(self):
        self.controller.execute_actions("evidence", self.stage)
        record = read_record(self.store.db, "operation", self.operation_id)
        changed = copy.deepcopy(record["payload"])
        capture = changed["execution_receipt"]
        capture["execution"]["argv"] = self.action["observation_argv"]
        capture["sha256"] = hash_value({key: value for key, value in capture.items() if key != "sha256"})
        with transaction(self.store.db) as db:
            write_record(db, "operation", self.operation_id, changed, expected_revision=record["revision"])
        _, packet = self.manifest()
        self.assertTrue(packet["coverage"]["missing"])

    def test_observation_claim_must_match_its_hashed_json_bytes(self):
        self.controller.execute_actions("evidence", self.stage)
        record = read_record(self.store.db, "operation", self.operation_id)
        changed = copy.deepcopy(record["payload"])
        changed["observation"]["exists"] = False
        with transaction(self.store.db) as db:
            write_record(db, "operation", self.operation_id, changed, expected_revision=record["revision"])
        _, packet = self.manifest()
        self.assertTrue(packet["coverage"]["missing"])

    def test_credential_redaction_and_hardlinked_log_rejection_remain(self):
        contract = copy.deepcopy(self.task["contract"])
        contract["stages"][0]["actions"][0]["argv"][-1] += ";print('gh'+'p_'+'x'*40)"
        self.revise(contract)
        self.controller.execute_actions("evidence", self.stage)
        manifest, packet = self.manifest()
        self.assertFalse(any("gh" + "p_" + "x" * 40 in item["content"] for item in packet["files"]))
        self.assertTrue(any("[REDACTED]" in item["content"] for item in packet["files"] if item["path"].startswith("_ase_context/action_logs/")))
        log = Path(manifest["stages"][0]["actions"][0]["execution_receipt"]["execution"]["log"])
        os.link(log, self.base / "hardlink.log")
        _, packet = self.manifest()
        self.assertTrue(packet["coverage"]["missing"])

    def test_later_different_execution_cannot_attach_provider_identity_to_first_attempt(self):
        actual_observe, calls = self.controller._observe_action, []
        def lose_post_observation(*args, **kwargs):
            calls.append(True)
            if len(calls) == 2:
                raise ValueError("Disposable lost post-effect observation")
            return actual_observe(*args, **kwargs)
        from unittest.mock import patch
        with patch.object(self.controller, "_observe_action", side_effect=lose_post_observation):
            with self.assertRaises(ValueError):
                self.controller.execute_actions("evidence", self.stage)
        before = read_record(self.store.db, "operation", self.operation_id)
        self.assertEqual(before["payload"]["state"], "UNKNOWN")
        self.assertEqual((self.repo / "count").read_text(), "1")
        folder = self.store.root / "tasks/evidence/actions" / self.operation_id
        later = execute([sys.executable, "-c", "import json;print(json.dumps({'provider_attempt_id':'later-ack'}))"],
            self.repo, folder / ("e" * 32 + ".log"), 3)
        with self.assertRaisesRegex(ValueError, "execution|receipt|[Ss]ubmission"):
            OperationLedger(self.store).record_submission(self.operation_id, later, context=before["payload"]["execution_receipt"]["context"])
        self.assertEqual(read_record(self.store.db, "operation", self.operation_id), before)

    def test_redacting_a_source_filename_keeps_manifest_json_valid_and_capture_immutable(self):
        name = "to" + "ken=disposable-not-real-value.py"
        (self.repo / name).write_text("source = 1\n")
        contract = copy.deepcopy(self.task["contract"])
        contract["scope"]["resources"] = ["."]
        contract["stages"][0]["scope"]["resources"] = ["."]
        self.revise(contract)
        self.controller.execute_actions("evidence", self.stage)
        before = read_record(self.store.db, "operation", self.operation_id)
        manifest, packet = self.manifest()
        self.assertTrue(manifest["metadata_strings_redacted"])
        self.assertEqual(packet["coverage"]["missing"], [])
        self.assertIn(name, before["payload"]["execution_receipt"]["context"]["source_files"])
        self.assertEqual(read_record(self.store.db, "operation", self.operation_id), before)

    def test_redacted_key_collision_is_explicitly_omitted_with_missing_coverage(self):
        for suffix in ("first", "second"):
            (self.repo / ("to" + "ken=disposable-" + suffix + ".py")).write_text("source = 1\n")
        contract = copy.deepcopy(self.task["contract"])
        contract["scope"]["resources"] = ["."]
        contract["stages"][0]["scope"]["resources"] = ["."]
        self.revise(contract)
        self.controller.execute_actions("evidence", self.stage)
        manifest, packet = self.manifest()
        self.assertEqual(manifest["status"], "OMITTED_REDACTION_COLLISION")
        self.assertTrue(packet["coverage"]["missing"])

    def test_large_pending_action_inventory_is_honestly_disclosed_without_blocking(self):
        contract = copy.deepcopy(self.task["contract"])
        contract["stages"][0]["actions"] = [dict(self.action, id="future-" + str(index)) for index in range(40)]
        self.revise(contract)
        manifest, packet = self.manifest()
        self.assertEqual(len(manifest["stages"][0]["actions"]), 40)
        self.assertTrue(all(action["state"] == "NOT_STARTED" for action in manifest["stages"][0]["actions"]))
        self.assertEqual(packet["coverage"]["missing"], [])

    def test_repeated_stage_renames_keep_each_observation_origin_without_replay(self):
        self.controller.execute_actions("evidence", self.stage)
        for name in ("middle", "current"):
            contract = copy.deepcopy(self.task["contract"])
            contract["stages"][0]["id"] = name
            self.revise(contract)
            self.controller.execute_actions("evidence", self.stage)
        manifest, packet = self.manifest()
        self.assertEqual(packet["coverage"]["missing"], [])
        self.assertEqual(len(manifest["stages"][0]["actions"][0]["observations"]), 4)
        self.assertEqual((self.repo / "count").read_text(), "1")

    def test_repeated_observer_recipe_changes_keep_all_captured_declarations(self):
        self.controller.execute_actions("evidence", self.stage)
        for name in ("middle-resource", "current-resource"):
            contract = copy.deepcopy(self.task["contract"])
            contract["stages"][0]["actions"][0]["observation_argv"][-1] = self.action["observation_argv"][-1].replace("disposable-delivery", name)
            self.revise(contract)
            self.controller.execute_actions("evidence", self.stage)
        manifest, packet = self.manifest()
        self.assertEqual(packet["coverage"]["missing"], [])
        self.assertEqual(len(manifest["stages"][0]["actions"][0]["observations"]), 4)
        self.assertEqual((self.repo / "count").read_text(), "1")

    def test_legacy_intermediate_origin_is_omitted_honestly_not_relabelled_or_replayed(self):
        self.controller.execute_actions("evidence", self.stage)
        for name in ("middle", "current"):
            contract = copy.deepcopy(self.task["contract"])
            contract["stages"][0]["id"] = name
            self.revise(contract)
            self.controller.execute_actions("evidence", self.stage)
        # Legacy-format fixture: this historical observer predates prospective
        # declaration capture. Its old scope must not be invented retroactively.
        rows = self.store.db.execute("SELECT sequence,payload FROM runtime_events WHERE kind='operation_reconciled'").fetchall()
        with transaction(self.store.db) as db:
            for row in rows:
                value = json.loads(row[1])
                if value["observation"]["observing_stage_id"] == "middle":
                    value["observation"].pop("provenance", None)
                    db.execute("UPDATE runtime_events SET payload=? WHERE sequence=?", (json.dumps(value), row[0]))
        manifest, packet = self.manifest()
        action = manifest["stages"][0]["actions"][0]
        self.assertEqual(packet["coverage"]["missing"], [])
        self.assertEqual(len(action["omitted_historical_observations"]), 1)
        self.assertFalse(any(item["observation"]["observing_stage_id"] == "middle" for item in action["observations"]))
        self.assertEqual((self.repo / "count").read_text(), "1")

    def test_request_reread_after_receipt_validation_still_checks_captured_bytes(self):
        from unittest.mock import patch
        from ase.runner import verify_receipt
        receipt = self.proposal()
        request_path = Path(receipt["artifacts"]["request"]["path"])
        def verify_then_change(*args, **kwargs):
            result = verify_receipt(*args, **kwargs)
            request = json.loads(request_path.read_bytes())
            request["handoff"]["untrusted_marker"] = True
            request_path.write_text(json.dumps(request))
            return result
        with patch("ase.action_evidence.verify_receipt", side_effect=verify_then_change):
            manifest, packet = self.manifest()
        self.assertEqual(manifest["stages"][0]["implementation_proposal"]["status"], "INVALID")
        self.assertTrue(packet["coverage"]["missing"])

    def capture_protocol_prerequisite(self):
        """Authentic history with a scheduler marker, never semantic approval."""
        from ase.gates import GateService
        trusted = self.base / "prerequisite-provider"
        trusted.mkdir()
        fixture = trusted / "provider.py"
        body = "schema = json.loads(pathlib.Path(args[9]).read_text())\npayload = ({'criteria':[]} if 'criteria' in schema['properties'] else {'findings':[],'summary':'Protocol-only prerequisite history'})"
        fixture.write_text(FIXTURE.replace("payload = {'verdict':'PASS'}", body))
        runner = Runner({"executable": sys.executable, "executable_args": [str(fixture), "success", "codex"],
            "trusted_cwd": str(trusted), "protocol_only": True}, self.store.root / "tasks/evidence/sessions")
        self.controller.check("evidence", "deliver")
        for role, schema in (("ac", AC_SCHEMA), ("review", REVIEW_SCHEMA)):
            receipt = runner.run(get_adapter("codex"), self.controller._request("evidence", role, schema,
                {"stage_id": "deliver"}), timeout=5)["receipt"]
            self.assertEqual(receipt["status"], "COMPLETE")
            self.controller._save("verification", "evidence:" + role, receipt)
        verdict = GateService(self.store).gate("evidence", "deliver")
        self.assertEqual(verdict["status"], "BLOCKED", "Protocol history cannot supply actual AC")
        self.controller._capture_stage("evidence", "deliver", {**verdict, "status": "APPROVED"})

    def candidate_until_recheck(self, stage, path, content, inspect=None):
        """Real runner/edit/DB path; stop at the scheduler-only recheck seam.

        This supplies no semantic verdict and never declares a stage complete.
        """
        from unittest.mock import patch
        trusted = self.base / ("candidate-" + stage["id"])
        trusted.mkdir(exist_ok=True)
        fixture = trusted / "provider.py"
        target = self.repo / path
        payload = {"edits": [{"path": path, "operation": "write", "content": content,
            "before_sha256": hashlib.sha256(target.read_bytes()).hexdigest() if target.exists() else ""}],
            "summary": "Protocol-only stage attribution fixture"}
        body = "schema = json.loads(pathlib.Path(args[9]).read_text())\npayload = (" + repr(payload) + " if 'edits' in schema['properties'] else {'criteria':[]} if 'criteria' in schema['properties'] else {'findings':[],'summary':'Protocol-only scheduler seam'})"
        fixture.write_text(FIXTURE.replace("payload = {'verdict':'PASS'}", body))
        runner = Runner({"executable": sys.executable, "executable_args": [str(fixture), "success", "codex"],
            "trusted_cwd": str(trusted), "protocol_only": True}, self.store.root / "tasks/evidence/sessions")
        calls = []
        actual_recheck, actual_verify = self.controller._revalidate_prerequisites, self.controller.verify
        def protocol_only_scheduler_judgment(task_id, stage_id=None):
            # Execute/capture the real protocol sessions; only the scheduler
            # decision is substituted, while real receipts remain unapproved.
            verdict = actual_verify(task_id, stage_id)
            self.assertEqual(verdict["status"], "BLOCKED")
            return {**verdict, "status": "APPROVED"}
        def scheduler_recheck(task, selected):
            calls.append(selected["id"])
            if len(calls) == 3:
                self.assertEqual(target.read_text(), content)
                if inspect:
                    inspect()
                return ["Protocol-only stop after real candidate application"], None
            return actual_recheck(task, selected)
        with patch.object(self.controller, "runner", runner), \
                patch.object(self.controller, "verify", side_effect=protocol_only_scheduler_judgment), \
                patch.object(self.controller, "_revalidate_prerequisites", side_effect=scheduler_recheck):
            result = self.controller._run_stage(self.tasks.snapshot("evidence"), stage, runner, get_adapter("codex"), resume=False)
        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual(len(calls), 3)
        return read_record(self.store.db, "implementation", "evidence")["payload"]

    def test_prerequisite_packet_discloses_inflight_other_stage_proposal_without_action_logs(self):
        other = self.two_stages()
        contract = copy.deepcopy(self.task["contract"])
        contract["stages"][1]["depends_on"] = ["deliver"]
        # This is guide attribution, so its prerequisite has no pending effect.
        contract["stages"][0]["actions"] = []
        contract["evidence"][0]["path"] = "guide.py"
        self.revise(contract)
        other = self.task["contract"]["stages"][1]
        original = self.proposal()
        self.capture_protocol_prerequisite()
        inspected = []
        def inspect():
            manifest, packet = self.manifest()
            inspected.append(manifest)
            self.assertEqual([item["stage_id"] for item in manifest["stages"]], ["deliver"])
            proposal = manifest["other_approved_stage_proposals"][0]
            self.assertEqual(proposal["stage_id"], "other")
            self.assertEqual(proposal["stage"]["scope"], other["scope"])
            self.assertEqual(proposal["current_approval"]["status"], "CURRENT_APPROVAL")
            self.assertEqual(proposal["cycle_observation"]["phase"], "CHECKING")
            self.assertTrue(proposal["implementation_proposal"]["proposal_only"])
            edit = proposal["implementation_proposal"]["edits"][0]
            self.assertEqual(edit["path"], "other.txt")
            self.assertEqual(edit["after_sha256"], edit["current_source_sha256"])
            self.assertEqual(manifest["verification_context"]["stage_id"], "deliver")
            self.assertEqual(manifest["verification_context"]["inflight_dependents"], ["other"])
            self.assertFalse(any(item["path"].startswith("_ase_context/action_logs/") for item in packet["files"]))
            self.assertFalse((self.repo / "count").exists())
            self.assertEqual(packet["coverage"]["missing"], [])
        receipt = self.candidate_until_recheck(other, "other.txt", "real other-stage candidate\n", inspect)
        self.assertTrue(inspected)
        self.assertNotEqual(receipt["execution_id"], original["execution_id"])

    def test_unfinished_stage_candidate_survives_global_overwrite_and_controller_restart(self):
        other = self.two_stages()
        first = self.candidate_until_recheck(self.stage, "guide.py", "first candidate\n")
        second = self.candidate_until_recheck(other, "other.txt", "second candidate\n")
        self.assertNotEqual(first["execution_id"], second["execution_id"])
        retained = read_record(self.store.db, "stage_implementation", "evidence:deliver")["payload"]
        self.assertIsNotNone(retained, "Unfinished stage captures must survive a later global candidate")
        self.assertEqual(retained["execution_id"], first["execution_id"])
        packet = WorkflowController(self.store)._packet("evidence", stage_id="other")
        manifest = json.loads(next(item["content"] for item in packet["files"] if item["path"] == "_ase_context/action_evidence.json"))
        proposal = manifest["other_approved_stage_proposals"][0]["implementation_proposal"]
        self.assertEqual(proposal["execution_id"], first["execution_id"])
        self.assertEqual(proposal["capture_source"], "LATEST_STAGE_CAPTURE")
        self.assertEqual(packet["coverage"]["missing"], [])

    def test_other_stage_inventory_uses_current_grant_and_rejects_invalid_retained_receipt(self):
        other = self.two_stages()
        receipt = self.candidate_until_recheck(other, "other.txt", "other candidate\n")
        task = self.tasks.approve("evidence", proposal_hash=self.task["proposal_hash"],
            user_message_ref="test:reapprove-deliver", approved_stage_ids=["deliver"])
        manifest, _ = self.manifest()
        self.assertEqual([entry["stage_id"] for entry in manifest["other_approved_stage_proposals"]], ["other"])
        invalid = dict(receipt, artifact_root=str(self.base / "unrelated-private"))
        self.controller._save("stage_implementation", "evidence:other", invalid)
        manifest, packet = self.manifest()
        entry = manifest["other_approved_stage_proposals"][0]
        self.assertEqual(entry["implementation_proposal"]["status"], "INVALID")
        self.assertTrue(packet["coverage"]["missing"])
        task = self.tasks.propose("evidence", {**task["proposal"], "stage_ids": ["deliver"]},
                                  expected_revision=task["revision"])
        self.tasks.approve("evidence", proposal_hash=task["proposal_hash"],
            user_message_ref="test:narrow-current-grant", approved_stage_ids=["deliver"])
        manifest, _ = self.manifest()
        self.assertEqual(manifest["other_approved_stage_proposals"], [])

    def test_legacy_global_other_stage_capture_is_attributed_without_inventing_retention(self):
        other = self.two_stages()
        receipt = self.proposal(stage=other, empty=True)
        manifest, packet = self.manifest()
        proposal = manifest["other_approved_stage_proposals"][0]["implementation_proposal"]
        self.assertEqual(proposal["execution_id"], receipt["execution_id"])
        self.assertEqual(proposal["capture_source"], "LATEST_TASK_CAPTURE")
        self.assertIsNone(read_record(self.store.db, "stage_implementation", "evidence:other")["payload"])
        self.assertEqual(packet["coverage"]["missing"], [])

    def test_retained_proposal_from_replaced_contract_is_not_relabelled_current(self):
        other = self.two_stages()
        self.candidate_until_recheck(other, "other.txt", "older context\n")
        contract = copy.deepcopy(self.task["contract"])
        contract["request"] += " Revised owner request."
        self.revise(contract)
        manifest, packet = self.manifest()
        proposal = manifest["other_approved_stage_proposals"][0]["implementation_proposal"]
        self.assertEqual(proposal["status"], "NOT_CURRENT")
        self.assertNotIn("edits", proposal)
        self.assertEqual(packet["coverage"]["missing"], [])


if __name__ == "__main__":
    unittest.main()
