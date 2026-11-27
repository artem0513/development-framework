"""Real protocol receipts prove history integrity, never semantic acceptance."""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from ase.adapters.codex import CodexAdapter
from ase.gates import source_snapshot
from ase.runner import Runner
from ase.state import read_record, write_record
from ase.store import Store
from ase.tasks import TaskService
from ase.workflow import WorkflowController
import test_runner as transport
from test_journal import contract


class StageHistoryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="ase-stage-history-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.root = self.base / "work"; self.root.mkdir()
        (self.root / "guide.txt").write_text("before")
        self.store = Store(self.root, self.base / "state")
        self.addCleanup(self.store.close)
        tasks = TaskService(self.store)
        task = tasks.create(contract("history"))
        task = tasks.propose("history", {"summary": "Write guide", "stage_ids": ["draft"]}, expected_revision=task["revision"])
        tasks.approve("history", proposal_hash=task["proposal_hash"], user_message_ref="test:approved", approved_stage_ids=["draft"])
        self.controller = WorkflowController(self.store)
        self.controller._save("baseline", "history", source_snapshot(self.store, tasks.snapshot("history")["contract"], task_id="history"))
        (self.root / "guide.txt").write_text("after")
        self.controller.check("history")

    def capture(self, *, whole=False, alter_verifier=None):
        self.assertTrue(hasattr(self.controller, "_capture_stage"), "stage proof capture is required")
        fixture = self.base / "fixture.py"; fixture.write_text(transport.FIXTURE)
        trusted = self.base / "trusted"; trusted.mkdir(exist_ok=True)
        runner = Runner({"executable": __import__("sys").executable, "executable_args": [str(fixture), "success", "codex"],
                         "trusted_cwd": str(trusted), "protocol_only": True}, self.store.root / "tasks" / "history" / "sessions")
        for role in ("implementation", "ac", "review"):
            handoff = ({"stage": {"id": "draft"}, "stage_id": "draft"}
                       if role == "implementation" or not whole else {"stage_id": None})
            request = self.controller._request("history", role, transport.SCHEMA, handoff)
            if role != "implementation" and alter_verifier:
                alter_verifier(request)
            receipt = runner.run(CodexAdapter(), request, timeout=5)["receipt"]
            self.assertEqual(receipt["status"], "COMPLETE")
            self.controller._save("implementation" if role == "implementation" else "verification",
                                  "history" if role == "implementation" else "history:" + role, receipt)
        self.controller._capture_stage("history", "draft", {"status": "APPROVED", "fingerprint": "fixture-stage-result"})

    def history_packet(self):
        packet = self.controller._packet("history")
        item = next(item for item in packet["files"] if item["path"] == "_ase_context/stage_history.json")
        self.assertEqual(hashlib.sha256(item["content"].encode()).hexdigest(), item["sha256"])
        return json.loads(item["content"]), packet["coverage"]["missing"]

    def test_capture_retains_real_receipts_and_compact_verified_observations(self):
        self.capture()
        proof = read_record(self.store.db, "stage", "history:draft")["payload"]
        self.assertIn("checks", proof["proof"])
        history, missing = self.history_packet()
        self.assertEqual(missing, [])
        stage = history["stages"][0]
        self.assertEqual(stage["stage_id"], "draft")
        self.assertFalse(stage["semantic_approval"])
        self.assertEqual({role["evidence_kind"] for role in stage["roles"].values()}, {"protocol_only"})
        self.assertTrue(all(role["started_at_utc"] and role["ended_at_utc"] for role in stage["roles"].values()))
        self.assertIn("_ase_context/source_changes.json", {item["path"] for item in stage["observations"]})
        self.assertNotIn("guide.txt", {item["path"] for item in stage["observations"]})
        self.assertLess(len(json.dumps(history)), 262144)

    @staticmethod
    def with_large_log(size):
        def alter(request):
            content = "test_case (suite.Tests.test_case) ... ok\n" * (size // 40)
            row = {"path": "_ase_context/logs/suite.txt", "content": content,
                   "sha256": hashlib.sha256(content.encode()).hexdigest()}
            request["source_packet"]["files"].append(row)
            request["source_packet"]["coverage"]["included"].append(row["path"])
        return alter

    def test_observation_above_the_old_64_kib_bound_keeps_the_stage(self):
        self.capture(alter_verifier=self.with_large_log(300000))
        history, missing = self.history_packet()
        self.assertEqual(missing, [])
        logs = [item for item in history["stages"][0]["observations"] if item["path"] == "_ase_context/logs/suite.txt"]
        self.assertEqual(len(logs), 1)
        self.assertGreater(len(logs[0]["content"].encode()), 65536)
        self.assertEqual(sorted(logs[0]["roles"]), ["ac", "review"])

    def test_observation_above_the_protocol_bound_is_explicit_missing_history(self):
        from unittest import mock
        import ase.workflow as workflow
        self.capture(alter_verifier=self.with_large_log(150000))
        with mock.patch.object(workflow, "MAX_DECLARED_PACKET_BYTES", 100000):
            history, missing = self.controller._stage_history(TaskService(self.store).snapshot("history"))
        self.assertEqual(history["stages"], [])
        self.assertTrue(any("historical observation exceeds packet bound" in item for item in missing))

    def test_whole_task_receipts_keep_verified_single_stage_history(self):
        self.capture(whole=True)
        history, missing = self.history_packet()
        self.assertEqual(missing, [])
        self.assertEqual([row["stage_id"] for row in history["stages"]], ["draft"])
        self.assertFalse(history["stages"][0]["semantic_approval"])

    def test_whole_task_receipts_keep_history_after_approved_revision(self):
        self.capture(whole=True)
        tasks = TaskService(self.store)
        task = tasks.snapshot("history")
        spec = json.loads(json.dumps(task["contract"]))
        spec["request"] += "; owner clarified the next review"
        task = tasks.revise("history", spec, expected_revision=task["revision"])
        task = tasks.propose("history", {"summary": "Refresh the original review", "stage_ids": ["draft"]},
                             expected_revision=task["revision"])
        tasks.approve("history", proposal_hash=task["proposal_hash"], user_message_ref="test:owner-update",
                      approved_stage_ids=["draft"])
        history, missing = self.history_packet()
        self.assertEqual(missing, [])
        self.assertEqual(len(history["stages"]), 1)
        self.assertFalse(history["stages"][0]["current_identity"])
        self.assertFalse(history["stages"][0]["semantic_approval"])

    def test_whole_task_receipts_use_authenticated_multistage_original(self):
        tasks = TaskService(self.store)
        task = tasks.snapshot("history")
        spec = json.loads(json.dumps(task["contract"]))
        spec["stages"].append({"id": "later", "acceptance_ids": ["AC1"], "depends_on": ["draft"]})
        task = tasks.revise("history", spec, expected_revision=task["revision"])
        task = tasks.propose("history", {"summary": "Review both stages", "stage_ids": ["draft", "later"]},
                             expected_revision=task["revision"])
        tasks.approve("history", proposal_hash=task["proposal_hash"], user_message_ref="test:owner-update",
                      approved_stage_ids=["draft", "later"])
        self.controller.check("history")
        self.capture(whole=True)
        history, missing = self.controller._stage_history(tasks.snapshot("history"))
        self.assertEqual(missing, [])
        self.assertEqual([row["stage_id"] for row in history["stages"]], ["draft"])
        self.assertFalse(history["stages"][0]["semantic_approval"])

    def test_whole_task_fallback_does_not_accept_another_explicit_stage(self):
        self.capture(whole=True, alter_verifier=lambda request: request["handoff"].update(stage_id="other"))
        history, missing = self.history_packet()
        self.assertEqual(history["stages"], [])
        self.assertTrue(any("stage boundary mismatch" in row for row in missing))

    def test_whole_task_history_requires_the_sealed_original_contract(self):
        def alter(request):
            source = next(row for row in request["source_packet"]["files"]
                          if row["path"] == "_ase_context/contract.json")
            contract = json.loads(source["content"])
            contract["stages"][0]["id"] = "other"
            source["content"] = json.dumps(contract)
            source["sha256"] = hashlib.sha256(source["content"].encode()).hexdigest()
        self.capture(whole=True, alter_verifier=alter)
        history, missing = self.history_packet()
        self.assertEqual(history["stages"], [])
        self.assertTrue(any("stage boundary mismatch" in row for row in missing))

    def test_whole_task_history_rejects_a_foreign_project(self):
        self.capture(whole=True, alter_verifier=lambda request: request["project"].update(project_id="foreign"))
        history, missing = self.history_packet()
        self.assertEqual(history["stages"], [])
        self.assertTrue(any("stage boundary mismatch" in row for row in missing))

    def test_whole_task_history_rejects_a_foreign_workspace_root(self):
        foreign = self.base / "foreign"
        foreign.mkdir()
        self.capture(whole=True, alter_verifier=lambda request: request["project"].update(root=str(foreign)))
        history, missing = self.history_packet()
        self.assertEqual(history["stages"], [])
        self.assertTrue(any("stage boundary mismatch" in row for row in missing))

    def test_implementation_requires_exact_stage_in_capture_and_history(self):
        for identity in (None, "missing", "other"):
            with self.subTest(identity=identity):
                fixture = StageHistoryTests("test_capture_retains_real_receipts_and_compact_verified_observations")
                fixture.setUp()
                try:
                    fixture.capture(whole=True)
                    original = read_record(fixture.store.db, "stage", "history:draft")["payload"]
                    request = fixture.controller._request("history", "implementation", transport.SCHEMA,
                                                          {"stage": {"id": "draft"}})
                    if identity == "missing":
                        request["handoff"].pop("stage")
                    else:
                        request["handoff"]["stage"]["id"] = identity
                    runner = Runner({"executable": __import__("sys").executable,
                                     "executable_args": [str(fixture.base / "fixture.py"), "success", "codex"],
                                     "trusted_cwd": str(fixture.base / "trusted"), "protocol_only": True},
                                    fixture.store.root / "tasks" / "history" / "sessions")
                    receipt = runner.run(CodexAdapter(), request, timeout=5)["receipt"]
                    fixture.controller._save("stage_implementation", "history:draft", receipt)
                    fixture.controller._save("stage", "history:draft", None)
                    with self.assertRaisesRegex(ValueError, "another stage/project identity"):
                        fixture.controller._capture_stage("history", "draft", original["gate"])
                    # Also exercise the history reader against a misassociated
                    # sealed receipt, rather than relying on the capture guard.
                    original["proof"]["roles"]["implementation"] = receipt
                    fixture.controller._save("stage", "history:draft", original)
                    history, missing = fixture.controller._stage_history(TaskService(fixture.store).snapshot("history"))
                    self.assertEqual(history["stages"], [])
                    self.assertTrue(any("stage boundary mismatch" in row for row in missing))
                finally:
                    fixture.doCleanups()

    def test_tamper_and_stale_history_become_explicit_missing_coverage(self):
        self.capture()
        stage = read_record(self.store.db, "stage", "history:draft")["payload"]
        request = Path(stage["proof"]["roles"]["ac"]["artifacts"]["request"]["path"])
        request.write_text("tampered")
        history, missing = self.history_packet()
        self.assertEqual(history["stages"], [])
        self.assertTrue(any("Stage history unavailable: draft" in item for item in missing))
        record = read_record(self.store.db, "stage", "history:draft")
        with self.store.db:
            write_record(self.store.db, "stage", "history:draft", {**record["payload"], "proposal_hash": "stale"}, expected_revision=record["revision"])
        history, missing = self.history_packet()
        self.assertEqual(history["stages"], [])
        self.assertTrue(any("stale" in item.lower() for item in missing))

    def test_legacy_completed_stage_does_not_invent_historical_evidence(self):
        task = TaskService(self.store).snapshot("history")
        self.controller._save("stage", "history:draft", {"state": "COMPLETED", "contract_hash": task["contract_hash"], "proposal_hash": task["proposal_hash"]})
        history, missing = self.history_packet()
        self.assertEqual(history["stages"], [])
        self.assertTrue(any("Stage history unavailable: draft" in item for item in missing))

    # A newer runtime policy with unchanged artifacts refreshes independent proof only.
    @staticmethod
    def newer_policy():
        from unittest import mock
        return mock.patch("ase.runtime_policy._identity", return_value="e" * 64)

    def done(self):
        return read_record(self.store.db, "stage", "history:draft")["payload"]

    def fresh_verification(self):
        fixture = self.base / "fixture.py"
        trusted = self.base / "trusted"
        runner = Runner({"executable": __import__("sys").executable, "executable_args": [str(fixture), "success", "codex"],
                         "trusted_cwd": str(trusted), "protocol_only": True}, self.store.root / "tasks" / "history" / "sessions")
        calls = []

        def verify(task_id, stage_id):
            calls.append(stage_id)
            for role in ("ac", "review"):
                request = self.controller._request("history", role, transport.SCHEMA, {"stage_id": stage_id})
                self.controller._save("verification", "history:" + role, runner.run(CodexAdapter(), request, timeout=5)["receipt"])
            fingerprint = read_record(self.store.db, "checks", "history")["payload"]["fingerprint"]
            return {"status": "APPROVED", "blocked_reasons": [], "rejected_reasons": [], "fingerprint": fingerprint}
        return verify, calls

    @staticmethod
    def as_independent():
        # Seam: models independent CLI receipts for the protocol unit only; the
        # protocol-only tests below prove the real evidence requirement.
        from unittest import mock
        return mock.patch("ase.workflow._REFRESH_EVIDENCE", frozenset({"independent_cli", "protocol_only"}))

    def test_policy_change_with_unchanged_source_needs_refresh_but_current_policy_does_not(self):
        self.capture()
        task = TaskService(self.store).snapshot("history")
        self.assertFalse(self.controller._stage_policy_refresh_needed(task, self.done()))
        with self.newer_policy():
            # A stage proven only by protocol fixtures is never upgraded by a refresh.
            self.assertFalse(self.controller._stage_policy_refresh_needed(task, self.done()))
        with self.newer_policy(), self.as_independent():
            self.assertTrue(self.controller._stage_policy_refresh_needed(task, self.done()))
            (self.root / "guide.txt").write_text("changed after approval")
            self.assertFalse(self.controller._stage_policy_refresh_needed(task, self.done()))

    def test_refresh_keeps_the_implementation_and_records_current_independent_proof(self):
        from unittest import mock
        self.capture()
        original = self.done()
        task = TaskService(self.store).snapshot("history")
        stage = task["contract"]["stages"][0]
        verify, calls = self.fresh_verification()
        with self.newer_policy(), mock.patch.object(self.controller, "verify", side_effect=verify), self.as_independent():
            verdict = self.controller._refresh_stage_policy(task, stage, original)
            self.assertEqual(verdict["status"], "APPROVED")
            refreshed = self.done()
            history, missing = self.controller._stage_history(task)
        self.assertEqual(calls, ["draft"])
        self.assertEqual(refreshed["proof"]["roles"]["implementation"], original["proof"]["roles"]["implementation"])
        self.assertNotEqual(refreshed["proof"]["roles"]["ac"], original["proof"]["roles"]["ac"])
        self.assertEqual(refreshed["proof"]["policy_refresh"]["previous_proof_sha256"],
                         __import__("ase.state", fromlist=["hash_value"]).hash_value(original["proof"]))
        self.assertEqual(missing, [])
        entry = history["stages"][0]
        self.assertTrue(entry["policy_current"])
        self.assertFalse(entry["roles"]["implementation"]["policy_current"])

    def test_protocol_only_refresh_never_replaces_the_stage_proof(self):
        from unittest import mock
        self.capture()
        original = self.done()
        task = TaskService(self.store).snapshot("history")
        verify, calls = self.fresh_verification()
        # The original stage is treated as independent; only the fresh receipts are protocol-only.
        with self.newer_policy(), mock.patch.object(self.controller, "verify", side_effect=verify), \
                mock.patch.object(self.controller, "_unchanged_under_historical_policy", return_value=True):
            verdict = self.controller._refresh_stage_policy(task, task["contract"]["stages"][0], original)
        self.assertEqual(verdict["status"], "BLOCKED")
        self.assertIn("independent evidence", verdict["blocked_reasons"][0])
        self.assertEqual(self.done(), original)

    def test_protocol_only_original_stage_is_never_refreshed(self):
        from unittest import mock
        self.capture()
        original = self.done()
        task = TaskService(self.store).snapshot("history")
        verify, calls = self.fresh_verification()
        with self.newer_policy(), mock.patch.object(self.controller, "verify", side_effect=verify):
            verdict = self.controller._refresh_stage_policy(task, task["contract"]["stages"][0], original)
        self.assertEqual(verdict["status"], "BLOCKED")
        self.assertEqual(calls, [])
        self.assertEqual(self.done(), original)

    def test_refresh_rejection_demotes_the_stage_so_repair_can_capture(self):
        from unittest import mock
        self.capture()
        original = self.done()
        task = TaskService(self.store).snapshot("history")
        fresh, _ = self.fresh_verification()
        # A current rejection that arrives beside a blocker (e.g. the other role timed out) is still durable.
        rejected = {"status": "REJECTED", "blocked_reasons": ["review session timed out"],
                    "rejected_reasons": ["P2: newer reviewer found a defect"]}

        def reject(task_id, stage_id):
            fresh(task_id, stage_id)
            return rejected
        with self.newer_policy(), mock.patch.object(self.controller, "verify", side_effect=reject), self.as_independent():
            verdict = self.controller._refresh_stage_policy(task, task["contract"]["stages"][0], original)
        self.assertEqual(verdict, rejected)
        demoted = self.done()
        self.assertEqual(demoted["state"], "REFRESH_REJECTED")
        self.assertEqual(demoted["previous"], original)
        self.assertFalse(self.controller._stage_policy_refresh_needed(task, demoted))
        self.capture()  # a repaired stage can be captured again instead of "cannot be replaced implicitly"
        self.assertEqual(self.done()["state"], "COMPLETED")

    def test_refresh_ignores_a_rejection_without_current_independent_receipts(self):
        from unittest import mock
        self.capture()
        original = self.done()
        task = TaskService(self.store).snapshot("history")
        rejected = {"status": "REJECTED", "blocked_reasons": [], "rejected_reasons": ["P2: fixture claim"]}
        with self.newer_policy(), mock.patch.object(self.controller, "verify", return_value=rejected), self.as_independent():
            verdict = self.controller._refresh_stage_policy(task, task["contract"]["stages"][0], original)
        self.assertEqual(verdict["status"], "BLOCKED")  # never a repair cycle from an untrusted rejection
        self.assertEqual(self.done(), original)

    def test_refresh_refuses_receipts_bound_to_other_checks(self):
        from unittest import mock
        self.capture()
        original = self.done()
        task = TaskService(self.store).snapshot("history")
        fresh, _ = self.fresh_verification()

        def verify_then_recheck(task_id, stage_id):
            verdict = fresh(task_id, stage_id)
            self.controller.check("history")  # new checks record: the saved receipts now bind stale checks
            return verdict
        with self.newer_policy(), mock.patch.object(self.controller, "verify", side_effect=verify_then_recheck), self.as_independent():
            verdict = self.controller._refresh_stage_policy(task, task["contract"]["stages"][0], original)
        self.assertEqual(verdict["status"], "BLOCKED")
        self.assertIn("bound to these checks", verdict["blocked_reasons"][0])
        self.assertEqual(self.done(), original)

    def test_refresh_refuses_changed_source_without_new_receipts(self):
        from unittest import mock
        self.capture()
        original = self.done()
        task = TaskService(self.store).snapshot("history")
        verify, calls = self.fresh_verification()
        (self.root / "guide.txt").write_text("changed after approval")
        with self.newer_policy(), mock.patch.object(self.controller, "verify", side_effect=verify), self.as_independent():
            verdict = self.controller._refresh_stage_policy(task, task["contract"]["stages"][0], original)
        self.assertEqual(verdict["status"], "BLOCKED")
        self.assertIn("fresh implementation cycle", verdict["blocked_reasons"][0])
        self.assertEqual(calls, [])
        self.assertEqual(self.done(), original)


if __name__ == "__main__":
    unittest.main()
