"""Local intent admission/recovery boundaries using real protocol-only captures.

These fixtures establish receipt and ledger behavior. They do not provide an
independent semantic acceptance or review verdict.
"""
import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

from ase.adapters import get_adapter
from ase.discovery import discover_capabilities, record_discovery
from ase.local_edit_intent import KIND, finish_local_edits, prepare_local_edits
from ase.runner import Runner, verify_receipt
from ase.state import encode, hash_value, read_record, transaction, write_record
from ase.store import Store
from ase.tasks import TaskService
from ase.workflow import EDIT_SCHEMA, WorkflowController, _target, apply_edits
from test_runner import FIXTURE


class LocalEditAuthorityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="ase-local-edit-authority-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.repo = self.base / "work"
        self.repo.mkdir()
        (self.repo / "a.txt").write_bytes(b"oldA")
        (self.repo / "b.txt").write_bytes(b"oldB")
        (self.repo / "requirements.txt").write_bytes(b"Declared read-only original input\n")
        self.store = Store(self.repo, self.base / "state")
        self.addCleanup(self.store.close)
        self.tasks = TaskService(self.store)
        contract = {"id": "local-edit", "schema_version": 2, "profile": "document",
            "request": "Correct both approved documents while preserving their declared read-only input",
            "scope": {"resources": ["a.txt", "b.txt"], "effects": ["file_write"]},
            "acceptance": [{"id": "BOTH", "expected": "Both documents contain the agreed correction",
                            "evidence_ids": ["a", "b"]}],
            "evidence": [{"id": "a", "kind": "artifact", "path": "a.txt"},
                         {"id": "b", "kind": "artifact", "path": "b.txt"}],
            "stages": [{"id": "draft", "acceptance_ids": ["BOTH"]}], "unknowns": [],
            "execution_policy": {"recovery_mode": "progress", "max_stage_cycles": 1,
                                 "max_task_cycles": 1, "active_time_budget_seconds": 60}}
        task = self.tasks.create(contract)
        task = self.tasks.propose("local-edit", {"summary": "Correct the two named local documents",
                                  "stage_ids": ["draft"]}, expected_revision=task["revision"])
        self.task = self.tasks.approve("local-edit", proposal_hash=task["proposal_hash"],
            user_message_ref="test:exact-two-file-authority", approved_stage_ids=["draft"])
        self.stage = self.task["contract"]["stages"][0]
        record_discovery(self.store, "local-edit", discover_capabilities(self.store.project,
            scope={"resources": ["a.txt", "b.txt"], "dependencies": [{"path": "requirements.txt",
                   "reason": "Required read-only source for both documents", "required": True,
                   "blocking_stage_ids": ["draft"]}]}, backend_config={}), expected_revision=0)
        trusted = self.base / "trusted"
        trusted.mkdir()
        edits = [{"path": name, "operation": "write", "before_sha256": hashlib.sha256(before).hexdigest(),
                  "content": after} for name, before, after in (("a.txt", b"oldA", "newA"), ("b.txt", b"oldB", "newB"))]
        payload = {"edits": edits, "summary": "Protocol fixture proposes two exact local edits"}
        provider = self.base / "provider.py"
        provider.write_text(FIXTURE.replace("payload = {'verdict':'PASS'}",
            "request = json.loads(input_text.split('REQUEST_JSON:\\n', 1)[1])\n"
            "assert request['role'] == 'implementation', 'No semantic verifier is provided'\n"
            "payload = " + repr(payload)), encoding="utf-8")
        runner = Runner({"executable": sys.executable,
            "executable_args": [str(provider), "success", "codex"], "trusted_cwd": str(trusted),
            "protocol_only": True}, self.store.root / "tasks/local-edit/sessions")
        self.controller = WorkflowController(self.store, runner=runner)
        self.controller.reserve_cycle("local-edit", "draft")
        self.progress = read_record(self.store.db, "cycle", "local-edit:draft")["payload"]
        self.authority = self.controller._stage_authority(self.task, self.stage)
        self.progress["candidate_authority"] = self.authority
        self.controller._save("cycle", "local-edit:draft", self.progress)
        request = self.controller._request("local-edit", "implementation", EDIT_SCHEMA,
            {"stage": self.stage, "candidate_id": self.progress["candidate_id"]})
        result = runner.run(get_adapter("codex"), request, timeout=5)
        self.assertEqual(result["result"]["status"], "COMPLETE")
        self.receipt = result["receipt"]
        self.assertEqual(verify_receipt(self.receipt)["evidence_kind"], "protocol_only")
        self.controller._save("implementation", "local-edit", self.receipt)
        self.controller._save("stage_implementation", "local-edit:draft", self.receipt)

    def prepare(self):
        self.progress = prepare_local_edits(self.controller, self.task, self.stage, self.progress,
            self.receipt, self.authority, None, _target, EDIT_SCHEMA)
        return self.progress

    def finish(self, *, require_applied=False):
        self.progress = finish_local_edits(self.controller, self.task, self.stage, self.progress,
            None, _target, apply_edits, EDIT_SCHEMA, require_applied=require_applied)
        return self.progress

    def intent(self):
        return read_record(self.store.db, KIND, self.progress["candidate_id"])

    @staticmethod
    def immutable(value):
        return {key: item for key, item in value.items() if key not in {"state", "applied_at", "intent_sha256"}}

    def replace_intent(self, value, *, rehash=False):
        if rehash:
            value["intent_sha256"] = hash_value(self.immutable(value))
        record = self.intent()
        with transaction(self.store.db) as db:
            write_record(db, KIND, self.progress["candidate_id"], value, expected_revision=record["revision"])

    def source_state(self):
        return {name: ((self.repo / name).read_bytes(), (self.repo / name).stat().st_mtime_ns,
                       (self.repo / name).stat().st_ino) for name in ("a.txt", "b.txt", "requirements.txt")}

    def assert_finish_holds_without_writes(self, *, require_applied=False):
        files, intent = self.source_state(), self.intent()
        cycle = read_record(self.store.db, "cycle", "local-edit:draft")
        with self.assertRaises((ValueError, OSError)):
            self.finish(require_applied=require_applied)
        self.assertEqual(self.source_state(), files)
        self.assertEqual(self.intent(), intent)
        self.assertEqual(read_record(self.store.db, "cycle", "local-edit:draft"), cycle)
        self.assertEqual(read_record(self.store.db, "budget", "local-edit")["payload"]["task_cycles"], 1)

    def test_exact_beforeimages_finish_once_and_repeat_preserves_mtime_and_intent(self):
        self.prepare()
        before = self.intent()["payload"]
        self.assertEqual(self.progress["phase"], "APPLYING")
        self.assertEqual(before["state"], "PREPARED")
        self.assertEqual((self.repo / "a.txt").read_bytes(), b"oldA")
        self.assertEqual((self.repo / "b.txt").read_bytes(), b"oldB")
        self.assertNotIn("oldA", encode(before))
        self.assertNotIn("oldB", encode(before))
        self.assertNotIn('"content":', encode(before))
        self.assertNotIn('"argv":', encode(before))
        self.assertNotIn('"terminal":', encode(before))
        self.assertNotIn("receipt", before)
        receipt_path = Path(self.receipt["artifact_root"]) / "receipt.json"
        self.assertEqual(before["receipt_ref"], {"path": str(receipt_path),
            "sha256": hashlib.sha256(receipt_path.read_bytes()).hexdigest()})
        self.assertEqual(before["receipt_sha256"], hash_value(self.receipt))
        self.finish()
        after = self.intent()["payload"]
        self.assertEqual(after["state"], "APPLIED")
        self.assertEqual(self.immutable(after), self.immutable(before))
        self.assertEqual(after["intent_sha256"], before["intent_sha256"])
        self.assertEqual(self.progress["phase"], "CHECKING")
        self.assertEqual((self.repo / "a.txt").read_bytes(), b"newA")
        self.assertEqual((self.repo / "b.txt").read_bytes(), b"newB")
        state = self.source_state()
        applied_record = self.intent()
        self.finish(require_applied=True)
        self.assertEqual(self.source_state(), state)
        self.assertEqual(self.intent(), applied_record)
        budget = read_record(self.store.db, "budget", "local-edit")["payload"]
        self.assertEqual(budget["task_cycles"], 1)
        self.assertEqual(budget["stages"]["local-edit:draft"], 1)
        requests = [json.loads(path.read_text(encoding="utf-8")) for path in
                    (self.store.root / "tasks/local-edit/sessions").glob("*/request.json")]
        self.assertEqual([request["role"] for request in requests], ["implementation"])
        self.assertFalse(list(self.repo.glob("*.ase-*.tmp")))

    def test_third_state_on_later_file_blocks_earlier_pending_write(self):
        self.prepare()
        (self.repo / "b.txt").write_bytes(b"independent edit")
        self.assert_finish_holds_without_writes()
        self.assertEqual((self.repo / "a.txt").read_bytes(), b"oldA")
        self.assertEqual((self.repo / "b.txt").read_bytes(), b"independent edit")

    def test_changed_declared_read_only_dependency_blocks_recovery(self):
        self.prepare()
        (self.repo / "requirements.txt").write_bytes(b"Changed dependency after admission\n")
        self.assert_finish_holds_without_writes()

    def test_same_proposal_reapproval_changes_authority_and_blocks_old_intent(self):
        self.prepare()
        original = read_record(self.store.db, "approval", "local-edit")
        renewed = self.tasks.approve("local-edit", proposal_hash=self.task["proposal_hash"],
            user_message_ref="test:replacement-approval-same-proposal", approved_stage_ids=["draft"])
        self.assertEqual(renewed["proposal_hash"], self.task["proposal_hash"])
        self.assertGreater(read_record(self.store.db, "approval", "local-edit")["revision"], original["revision"])
        self.assert_finish_holds_without_writes()

    def test_explicit_stop_blocks_prepared_intent(self):
        self.prepare()
        self.controller.request_stop("local-edit", reason="Owner paused this disposable task")
        self.assert_finish_holds_without_writes()

    def test_exhausted_active_time_blocks_existing_intent_without_new_cycle(self):
        self.prepare()
        self.controller.charge_time("local-edit", 60)
        self.assert_finish_holds_without_writes()
        self.assertEqual(read_record(self.store.db, "budget", "local-edit")["payload"]["active_seconds"], 60)

    def test_missing_journal_cannot_be_reconstructed_from_latest_receipt(self):
        self.prepare()
        self.replace_intent(None)
        self.assert_finish_holds_without_writes()

    def test_missing_captured_receipt_file_blocks_recovery(self):
        self.prepare()
        Path(self.receipt["artifact_root"], "receipt.json").unlink()
        self.assert_finish_holds_without_writes()

    def test_tampered_captured_result_bytes_block_recovery(self):
        self.prepare()
        Path(self.receipt["artifacts"]["result"]["path"]).write_bytes(b"{}")
        self.assert_finish_holds_without_writes()

    def test_rehashed_receipt_metadata_still_requires_authentic_captured_receipt(self):
        self.prepare()
        value = self.intent()["payload"]
        altered_receipt = copy.deepcopy(self.receipt)
        altered_receipt["artifacts"]["result"]["sha256"] = "0" * 64
        raw = encode(altered_receipt).encode("utf-8")
        Path(value["receipt_ref"]["path"]).write_bytes(raw)
        value["receipt_ref"]["sha256"] = hashlib.sha256(raw).hexdigest()
        value["receipt_sha256"] = hash_value(altered_receipt)
        self.replace_intent(value, rehash=True)
        self.assert_finish_holds_without_writes()

    def test_missing_receipt_reference_cannot_fall_back_to_latest_capture(self):
        self.prepare()
        value = self.intent()["payload"]
        value["receipt_ref"] = None
        self.replace_intent(value, rehash=True)
        self.assert_finish_holds_without_writes()

    def test_rehashed_reference_to_foreign_task_cannot_authorize_identical_receipt(self):
        self.prepare()
        value = self.intent()["payload"]
        foreign = self.store.root / "tasks/another-task/sessions/foreign/receipt.json"
        foreign.parent.mkdir(parents=True)
        foreign.write_bytes(Path(value["receipt_ref"]["path"]).read_bytes())
        value["receipt_ref"]["path"] = str(foreign)
        self.replace_intent(value, rehash=True)
        self.assert_finish_holds_without_writes()

    def test_rehashed_reference_to_nonreceipt_artifact_is_rejected(self):
        self.prepare()
        value = self.intent()["payload"]
        wrong = Path(self.receipt["artifacts"]["request"]["path"])
        value["receipt_ref"] = {"path": str(wrong), "sha256": hashlib.sha256(wrong.read_bytes()).hexdigest()}
        self.replace_intent(value, rehash=True)
        self.assert_finish_holds_without_writes()

    def test_rehashed_reference_with_wrong_raw_digest_is_rejected(self):
        self.prepare()
        value = self.intent()["payload"]
        value["receipt_ref"]["sha256"] = "0" * 64
        self.replace_intent(value, rehash=True)
        self.assert_finish_holds_without_writes()

    def test_rehashed_reference_with_extra_command_field_is_rejected(self):
        self.prepare()
        value = self.intent()["payload"]
        value["receipt_ref"]["argv"] = ["unauthorized command"]
        self.replace_intent(value, rehash=True)
        self.assert_finish_holds_without_writes()

    def test_rehashed_prepared_plan_must_still_match_captured_edits(self):
        self.prepare()
        value = self.intent()["payload"]
        value["file_plan"]["edits"][0]["after_sha256"] = hashlib.sha256(b"different desired bytes").hexdigest()
        self.replace_intent(value, rehash=True)
        self.assert_finish_holds_without_writes()

    def test_rehashed_applied_plan_is_validated_even_when_postimages_match(self):
        self.prepare()
        self.finish()
        value = self.intent()["payload"]
        value["file_plan"]["edits"][0]["before_sha256"] = "0" * 64
        self.replace_intent(value, rehash=True)
        self.assert_finish_holds_without_writes()

    def test_applied_file_regressed_to_original_is_never_replayed(self):
        self.prepare()
        self.finish()
        (self.repo / "a.txt").write_bytes(b"oldA")
        self.assert_finish_holds_without_writes()
        self.assertEqual((self.repo / "a.txt").read_bytes(), b"oldA")
        self.assertEqual((self.repo / "b.txt").read_bytes(), b"newB")

    def test_applied_file_changed_to_third_state_is_never_overwritten(self):
        self.prepare()
        self.finish()
        (self.repo / "b.txt").write_bytes(b"later owner revision")
        self.assert_finish_holds_without_writes()

    def test_known_checking_resume_rejects_prepared_intent_without_source_writes(self):
        self.prepare()
        self.progress["phase"] = "CHECKING"
        self.controller._save("cycle", "local-edit:draft", self.progress)
        self.assert_finish_holds_without_writes(require_applied=True)

    def test_known_checking_resume_does_not_replay_downgraded_application_state(self):
        self.prepare()
        self.finish()
        value = self.intent()["payload"]
        value["state"] = "PREPARED"
        value.pop("applied_at")
        self.replace_intent(value)
        (self.repo / "a.txt").write_bytes(b"oldA")
        self.assert_finish_holds_without_writes(require_applied=True)
        self.assertEqual((self.repo / "a.txt").read_bytes(), b"oldA")
        self.assertEqual((self.repo / "b.txt").read_bytes(), b"newB")

    def test_changed_dependency_before_admission_creates_no_intent_or_writes(self):
        (self.repo / "requirements.txt").write_bytes(b"Changed dependency before admission\n")
        files, cycle = self.source_state(), read_record(self.store.db, "cycle", "local-edit:draft")
        with self.assertRaises(ValueError):
            self.prepare()
        self.assertIsNone(self.intent()["payload"])
        self.assertEqual(self.source_state(), files)
        self.assertEqual(read_record(self.store.db, "cycle", "local-edit:draft"), cycle)

    def test_missing_original_candidate_authority_cannot_admit_captured_edits(self):
        self.progress = copy.deepcopy(self.progress)
        self.progress.pop("candidate_authority")
        files = self.source_state()
        with self.assertRaises(ValueError):
            self.prepare()
        self.assertIsNone(self.intent()["payload"])
        self.assertEqual(self.source_state(), files)


if __name__ == "__main__":
    unittest.main()
