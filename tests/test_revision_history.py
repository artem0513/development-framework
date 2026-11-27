"""Authorized supersession preserves verified history without approving new work.

Subprocess sessions below are protocol fixtures, never independent semantic AC.
"""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest

from ase.adapters.codex import CodexAdapter
from ase.gates import GateService, source_snapshot
from ase.runner import Runner
from ase.state import hash_value, read_record, write_record
from ase.store import Store
from ase.tasks import TaskService
from ase.workflow import EDIT_SCHEMA, WorkflowController
import test_runner as transport
from test_journal import contract


class _RevisionHistoryFixture(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="ase-revision-history-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.repo = self.base / "work"
        self.repo.mkdir()
        (self.repo / "guide.txt").write_text("original draft")
        self.store = Store(self.repo, self.base / "state")
        self.addCleanup(lambda: self.store.close())
        self.tasks = TaskService(self.store)
        spec = contract("revision")
        spec["scope"]["resources"].append("delivery.txt")
        spec["acceptance"].append({"id": "DELIVERY", "expected": "Guide delivered", "evidence_ids": ["delivery"]})
        spec["evidence"].append({"id": "delivery", "kind": "artifact", "path": "delivery.txt", "required": True})
        spec["stages"][0]["scope"] = {"resources": ["guide.txt"], "effects": ["file_write"]}
        spec["stages"].append({"id": "deliver", "scope": {"resources": ["delivery.txt"], "effects": ["file_write"]},
            "acceptance_ids": ["DELIVERY"], "depends_on": ["draft"]})
        self.create_task(spec)
        self.controller = WorkflowController(self.store)
        self.controller._save("baseline", "revision", source_snapshot(self.store, self.task["contract"], task_id="revision"))
        self.controller.check("revision", "draft")
        self.trusted = self.base / "trusted"
        self.trusted.mkdir()
        self.fixture = self.base / "fixture.py"
        self.runner = Runner({"executable": sys.executable,
            "executable_args": [str(self.fixture), "success", "codex"],
            "trusted_cwd": str(self.trusted), "protocol_only": True}, self.store.root / "tasks/revision/sessions")
        self.original_implementation = self.capture_implementation()
        for role in ("ac", "review"):
            self.fixture.write_text(transport.FIXTURE)
            request = self.controller._request("revision", role, transport.SCHEMA, {"stage_id": "draft"})
            result = self.runner.run(CodexAdapter(), request, timeout=5)
            self.assertEqual(result["result"]["status"], "COMPLETE")
            self.controller._save("verification", "revision:" + role, result["receipt"])
        self.controller._capture_stage("revision", "draft", {"status": "APPROVED", "fingerprint": "protocol-scheduler-fixture"})
        self.original = copy.deepcopy(self.task)

    def create_task(self, spec):
        self.approve(self.tasks.create(spec), "Original draft")

    def approve(self, task, summary):
        task = self.tasks.propose("revision", {"summary": summary, "stage_ids": ["draft"]}, expected_revision=task["revision"])
        self.task = self.tasks.approve("revision", proposal_hash=task["proposal_hash"],
            user_message_ref="test:owner-update-" + str(task["revision"]), approved_stage_ids=["draft"])

    def capture_implementation(self):
        self.fixture.write_text(transport.FIXTURE.replace("payload = {'verdict':'PASS'}",
            "payload = {'edits': [], 'summary': 'Protocol proposal only'}"))
        stage = self.task["contract"]["stages"][0]
        request = self.controller._request("revision", "implementation", EDIT_SCHEMA, {"stage": stage})
        result = self.runner.run(CodexAdapter(), request, timeout=5)
        self.assertEqual(result["result"]["status"], "COMPLETE")
        self.controller._save("stage_implementation", "revision:draft", result["receipt"])
        return result["receipt"]

    def revise(self, *, approve=True):
        spec = copy.deepcopy(self.tasks.snapshot("revision")["contract"])
        spec["request"] += "; use the new owner answer"
        spec["acceptance"][0]["expected"] = "Guide reflects the new answer"
        spec["acceptance"][0]["evidence_ids"].append("new-answer")
        spec["evidence"].append({"id": "new-answer", "kind": "artifact", "path": "guide.txt", "required": True})
        task = self.tasks.revise("revision", spec, expected_revision=self.tasks.snapshot("revision")["revision"])
        (self.repo / "guide.txt").write_text("new owner answer")
        if approve:
            self.approve(task, "Refresh draft against the new owner answer")

    def packet(self):
        packet = self.controller._packet("revision", stage_id="draft")
        supplied = {item["path"]: item["content"] for item in packet["files"]}
        history = json.loads(supplied["_ase_context/stage_history.json"])
        manifest = json.loads(supplied["_ase_context/action_evidence.json"])
        return packet, supplied, history, manifest["stages"][0]["implementation_proposal"]


class RevisionHistoryTests(_RevisionHistoryFixture):
    def test_additive_check_plan_keeps_old_capture_historical_and_allows_fresh_checks(self):
        original_plan = self.tasks.execution_plan("revision")
        extra = {"id": "extra-guide-check", "kind": "artifact", "path": "guide.txt", "required": True}
        plan = self.tasks.update_execution_plan("revision", {"checks": [*original_plan["checks"], extra]},
            expected_revision=original_plan["revision"])
        packet, _, history, proposal = self.packet()
        self.assertEqual(packet["coverage"]["missing"], [])
        self.assertEqual(proposal["status"], "HISTORICAL_VERIFIED_CAPTURE")
        self.assertFalse(proposal["current_identity"])
        self.assertEqual(proposal["bindings"]["plan_sha256"], original_plan["plan_hash"])
        self.assertEqual(proposal["plan_supersession"], {
            "original_plan_hash": original_plan["plan_hash"], "current_plan_hash": plan["plan_hash"]})
        self.assertFalse(history["stages"][0]["current_identity"])
        self.assertFalse(history["stages"][0]["semantic_approval"])
        self.assertEqual(history["stages"][0]["plan_supersession"], proposal["plan_supersession"])
        gate = GateService(self.store).gate("revision", "draft")
        self.assertIn("Stale check evidence: plan_hash", gate["reasons"])
        checks = self.controller.check("revision", "draft")
        self.assertEqual({item["id"] for item in checks["results"]}, {"guide", "extra-guide-check"})
        self.assertIn("Independent ac receipt invalid: Receipt inputs are stale",
            GateService(self.store).gate("revision", "draft")["reasons"])
        self.fixture.write_text(transport.FIXTURE)
        request = self.controller._request("revision", "ac", transport.SCHEMA, {"stage_id": "draft"})
        fresh = self.runner.run(CodexAdapter(), request, timeout=5)
        self.assertEqual(fresh["result"]["status"], "COMPLETE")
        self.controller._save("verification", "revision:ac", fresh["receipt"])
        request = self.controller._request("revision", "review", transport.SCHEMA, {"stage_id": "draft"})
        fresh_review = self.runner.run(CodexAdapter(), request, timeout=5)
        self.controller._save("verification", "revision:review", fresh_review["receipt"])
        self.capture_implementation()
        self.controller._capture_stage("revision", "draft", {"status": "APPROVED", "fingerprint": "protocol-new-plan-fixture"})
        captured = read_record(self.store.db, "stage", "revision:draft")["payload"]
        self.assertEqual(captured["proof"]["checks"]["plan_hash"], plan["plan_hash"])
        with self.assertRaisesRegex(ValueError, "cannot be replaced"):
            self.controller._capture_stage("revision", "draft", {"status": "APPROVED", "fingerprint": "protocol-duplicate-fixture"})
        self.assertFalse((self.repo / "delivery.txt").exists())

    def test_changed_weakened_or_bad_hash_plan_cannot_authorize_old_capture(self):
        saved = read_record(self.store.db, "execution_plan", "revision")
        for change in ("changed", "weakened", "bad-hash"):
            with self.subTest(change=change):
                changed = copy.deepcopy(saved["payload"])
                if change == "changed":
                    changed["checks"][0]["path"] = "delivery.txt"
                elif change == "weakened":
                    changed["checks"][0]["required"] = False
                changed["plan_hash"] = "0" * 64 if change == "bad-hash" else hash_value(changed["checks"])
                record = read_record(self.store.db, "execution_plan", "revision")
                with self.store.db:
                    write_record(self.store.db, "execution_plan", "revision", changed, expected_revision=record["revision"])
                packet, _, _, proposal = self.packet()
                self.assertEqual(proposal["status"], "INVALID")
                self.assertTrue(packet["coverage"]["missing"])
        record = read_record(self.store.db, "execution_plan", "revision")
        with self.store.db:
            write_record(self.store.db, "execution_plan", "revision", saved["payload"], expected_revision=record["revision"])

    def test_new_current_candidate_replaces_old_plan_candidate_in_verifier_packet(self):
        original = self.tasks.execution_plan("revision")
        extra = {"id": "extra-guide-check", "kind": "artifact", "path": "guide.txt", "required": True}
        self.tasks.update_execution_plan("revision", {"checks": [*original["checks"], extra]}, expected_revision=original["revision"])
        latest = self.capture_implementation()
        packet, _, history, proposal = self.packet()
        self.assertEqual(packet["coverage"]["missing"], [])
        self.assertEqual(proposal["status"], "VERIFIED_CAPTURE")
        self.assertEqual(proposal["execution_id"], latest["execution_id"])
        self.assertNotEqual(proposal["execution_id"], self.original_implementation["execution_id"])
        self.assertFalse(history["stages"][0]["current_identity"])

    def test_approved_revision_transports_old_proof_as_history_without_current_approval(self):
        self.revise()
        self.store.close()
        self.store = Store(self.repo, self.base / "state")
        self.tasks = TaskService(self.store)
        self.controller = WorkflowController(self.store)
        packet, supplied, history, proposal = self.packet()
        self.assertEqual(packet["coverage"]["missing"], [])
        self.assertEqual(supplied["guide.txt"], "new owner answer")
        stage = history["stages"][0]
        self.assertFalse(stage["current_identity"])
        self.assertFalse(stage["semantic_approval"])
        self.assertEqual(stage["contract_hash"], self.original["contract_hash"])
        self.assertEqual(proposal["status"], "HISTORICAL_VERIFIED_CAPTURE")
        self.assertFalse(proposal["current_identity"])
        self.assertEqual(proposal["execution_id"], self.original_implementation["execution_id"])
        gate = GateService(self.store).gate("revision", "draft")
        self.assertNotEqual(gate["status"], "APPROVED")
        self.assertIn("Stale check evidence: contract_hash", gate["reasons"])
        self.assertIn("Independent ac receipt invalid: Receipt inputs are stale", gate["reasons"])
        checks = self.controller.check("revision", "draft")
        self.assertEqual({item["id"] for item in checks["results"]}, {"guide", "new-answer"})
        self.assertEqual(checks["contract_hash"], self.task["contract_hash"])
        self.assertIn("Independent ac receipt invalid: Receipt inputs are stale",
            GateService(self.store).gate("revision", "draft")["reasons"])
        self.assertIn("Stage incomplete: draft", GateService(self.store).gate("revision")["reasons"])
        self.assertFalse((self.repo / "delivery.txt").exists())

    def test_new_current_candidate_replaces_historical_candidate_in_verifier_packet(self):
        self.revise()
        latest = self.capture_implementation()
        packet, _, history, proposal = self.packet()
        self.assertEqual(packet["coverage"]["missing"], [])
        self.assertEqual(proposal["status"], "VERIFIED_CAPTURE")
        self.assertEqual(proposal["execution_id"], latest["execution_id"])
        self.assertNotEqual(proposal["execution_id"], self.original_implementation["execution_id"])
        self.assertFalse(history["stages"][0]["current_identity"])

    def test_new_approved_proposal_on_same_contract_has_verified_historical_origin(self):
        self.approve(self.tasks.snapshot("revision"), "Owner reapproved the updated execution approach")
        packet, _, history, proposal = self.packet()
        self.assertEqual(packet["coverage"]["missing"], [])
        self.assertFalse(history["stages"][0]["current_identity"])
        self.assertEqual(proposal["status"], "HISTORICAL_VERIFIED_CAPTURE")

    def test_revision_without_current_approval_does_not_authorize_old_capture(self):
        self.revise(approve=False)
        packet, _, history, proposal = self.packet()
        self.assertEqual(history["stages"], [])
        self.assertEqual(proposal["status"], "INVALID")
        self.assertTrue(packet["coverage"]["missing"])

    def test_unknown_historical_identity_still_fails_closed_after_legitimate_revision(self):
        self.revise()
        record = read_record(self.store.db, "stage", "revision:draft")
        with self.store.db:
            write_record(self.store.db, "stage", "revision:draft", {**record["payload"], "proposal_hash": "unknown"},
                expected_revision=record["revision"])
        packet, _, history, proposal = self.packet()
        self.assertEqual(history["stages"], [])
        self.assertEqual(proposal["status"], "INVALID")
        self.assertTrue(any("Stage history unavailable" in item for item in packet["coverage"]["missing"]))

    def test_archived_authority_and_receipt_tampering_still_block_coverage(self):
        self.revise()
        records = (("runtime_records", "kind='task_version' AND key='revision:1'"),
            ("runtime_records", "kind='approval_version' AND key='revision:2'"),
            ("runtime_events", "event_key='revision:proposal:2'"),
            ("runtime_events", "event_key='revision:approval:4'"))
        for table, selector in records:
            with self.subTest(table=table, selector=selector):
                row = self.store.db.execute("SELECT * FROM " + table + " WHERE " + selector).fetchone()
                self.assertIsNotNone(row)
                with self.store.db:
                    self.store.db.execute("DELETE FROM " + table + " WHERE " + selector)
                packet, _, history, proposal = self.packet()
                self.assertEqual(history["stages"], [])
                self.assertEqual(proposal["status"], "INVALID")
                self.assertTrue(packet["coverage"]["missing"])
                with self.store.db:
                    placeholders = ",".join("?" for _ in row)
                    self.store.db.execute("INSERT INTO " + table + " VALUES(" + placeholders + ")", tuple(row))
        request = Path(self.original_implementation["artifacts"]["request"]["path"])
        request.write_text("tampered historical request")
        packet, _, history, proposal = self.packet()
        self.assertEqual(history["stages"], [])
        self.assertEqual(proposal["status"], "INVALID")
        self.assertTrue(packet["coverage"]["missing"])


class InheritedRevisionHistoryTests(_RevisionHistoryFixture):
    def create_task(self, spec):
        ancestor = copy.deepcopy(spec)
        ancestor["id"] = "ancestor"
        ancestor["stages"] = [{"id": "parent-stage", "acceptance_ids": [ac["id"] for ac in ancestor["acceptance"]], "depends_on": []}]
        task = self.tasks.create(ancestor)
        task = self.tasks.propose("ancestor", {"summary": "Authorized parent work", "stage_ids": ["parent-stage"]},
            expected_revision=task["revision"])
        self.tasks.approve("ancestor", proposal_hash=task["proposal_hash"], user_message_ref="test:ancestor",
            approved_stage_ids=["parent-stage"])
        parent = {**ancestor, "id": "parent", "parent_task_id": "ancestor", "parent_stage_id": "parent-stage"}
        self.tasks.create(parent)
        self.task = self.tasks.create({**spec, "parent_task_id": "parent", "parent_stage_id": "parent-stage"})

    def test_revised_inherited_child_keeps_verified_original_ancestry_without_replay(self):
        self.assertEqual(self.packet()[0]["coverage"]["missing"], [])
        self.assertIsNone(read_record(self.store.db, "approval", "revision")["payload"])
        self.revise()
        self.assertEqual(self.tasks.require_approval("revision", "draft")["state"], "READY")
        self.store.close()
        self.store = Store(self.repo, self.base / "state")
        self.tasks, self.controller = TaskService(self.store), WorkflowController(self.store)
        packet, _, history, proposal = self.packet()
        self.assertEqual(packet["coverage"]["missing"], [])
        self.assertEqual(proposal["status"], "HISTORICAL_VERIFIED_CAPTURE")
        self.assertFalse(proposal["current_identity"])
        self.assertFalse(history["stages"][0]["semantic_approval"])
        self.assertEqual(proposal["execution_id"], self.original_implementation["execution_id"])
        self.capture_implementation()
        self.assertEqual(self.packet()[3]["status"], "VERIFIED_CAPTURE")
        self.assertNotEqual(GateService(self.store).gate("revision", "draft")["status"], "APPROVED")
        self.assertFalse((self.repo / "delivery.txt").exists())

    def test_inherited_history_requires_archived_versions_and_original_ancestor_grant(self):
        self.revise()
        selectors = (("runtime_records", "kind='task_version' AND key='revision:1'"),
            ("runtime_records", "kind='task_version' AND key='parent:1'"),
            ("runtime_records", "kind='task_version' AND key='ancestor:1'"),
            ("runtime_records", "kind='approval_version' AND key='ancestor:2'"),
            ("runtime_events", "event_key='ancestor:proposal:2'"),
            ("runtime_events", "event_key='ancestor:approval:2'"))
        for table, selector in selectors:
            with self.subTest(table=table, selector=selector):
                row = self.store.db.execute("SELECT * FROM " + table + " WHERE " + selector).fetchone()
                self.assertIsNotNone(row)
                with self.store.db:
                    self.store.db.execute("DELETE FROM " + table + " WHERE " + selector)
                packet, _, history, proposal = self.packet()
                self.assertEqual(history["stages"], [])
                self.assertEqual(proposal["status"], "INVALID")
                self.assertTrue(packet["coverage"]["missing"])
                with self.store.db:
                    placeholders = ",".join("?" for _ in row)
                    self.store.db.execute("INSERT INTO " + table + " VALUES(" + placeholders + ")", tuple(row))

    def test_inherited_history_rejects_unknown_parent_identity_scope_and_late_grant(self):
        self.revise()
        selectors = (("runtime_records", "kind='task_version' AND key='revision:1'", "parent-hash"),
            ("runtime_records", "kind='task_version' AND key='parent:1'", "scope"),
            ("runtime_events", "event_key='ancestor:approval:2'", "late-grant"))
        for table, selector, change in selectors:
            with self.subTest(change=change):
                row = self.store.db.execute("SELECT * FROM " + table + " WHERE " + selector).fetchone()
                value = json.loads(row["payload"])
                grant_archive = None
                if change == "parent-hash":
                    value["parent_proposal_hash"] = "0" * 64
                elif change == "scope":
                    value["contract"]["stages"][0]["scope"]["resources"] = ["delivery.txt"]
                    value["contract_hash"] = hash_value(value["contract"])
                    value["proposal_hash"] = hash_value({"parent_proposal_hash": value["parent_proposal_hash"],
                        "contract_hash": value["contract_hash"]})
                else:
                    value["approved_at_utc"] = "2099-01-01T00:00:00+00:00"
                    grant_archive = self.store.db.execute("SELECT payload FROM runtime_records WHERE kind='approval_version' AND key='ancestor:2'").fetchone()[0]
                with self.store.db:
                    self.store.db.execute("UPDATE " + table + " SET payload=? WHERE " + selector, (json.dumps(value),))
                    if grant_archive is not None:
                        self.store.db.execute("UPDATE runtime_records SET payload=? WHERE kind='approval_version' AND key='ancestor:2'", (json.dumps(value),))
                packet, _, history, proposal = self.packet()
                self.assertEqual(history["stages"], [])
                self.assertEqual(proposal["status"], "INVALID")
                self.assertTrue(packet["coverage"]["missing"])
                with self.store.db:
                    self.store.db.execute("UPDATE " + table + " SET payload=? WHERE " + selector, (row["payload"],))
                    if grant_archive is not None:
                        self.store.db.execute("UPDATE runtime_records SET payload=? WHERE kind='approval_version' AND key='ancestor:2'", (grant_archive,))

    def test_current_parent_revocation_does_not_reuse_original_inherited_grant(self):
        self.revise()
        parent = self.tasks.snapshot("ancestor")
        self.tasks.propose("ancestor", {"summary": "Changed parent authority awaits owner approval", "stage_ids": ["parent-stage"]},
            expected_revision=parent["revision"])
        packet, _, history, proposal = self.packet()
        self.assertEqual(history["stages"], [])
        self.assertEqual(proposal["status"], "INVALID")
        self.assertTrue(packet["coverage"]["missing"])


if __name__ == "__main__":
    unittest.main()
