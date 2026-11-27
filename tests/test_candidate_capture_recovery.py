"""Actual process termination before local-edit intent admission.

The backend fixture is transport-only. Actual SQLite, approval, captured CLI
artifacts, file effects and accounting are used; no semantic approval is supplied.
"""
from contextlib import closing
import copy
from datetime import datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

REPOSITORY = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(REPOSITORY), str(REPOSITORY / "tests")]

from ase.runner import Runner, verify_receipt
from ase.state import read_record
from ase.store import Store
from ase.tasks import TaskService
from ase.workflow import WorkflowController
from test_local_edit_recovery import CONTENTS, EXPECTED, _prepare, _save, _snapshot


def _coordinator(base, boundary, resume):
    future_clock = boundary == "receipt-written-clock-forward"
    if resume:
        store = Store(base / "work", base / "state")
    elif future_clock:
        original_create = TaskService.create

        def finite_task(service, spec, *args, **kwargs):
            spec = copy.deepcopy(spec)
            spec["execution_policy"]["active_time_budget_seconds"] = 60
            return original_create(service, spec, *args, **kwargs)

        with patch.object(TaskService, "create", new=finite_task):
            store = _prepare(base, candidate_at=1)
    else:
        store = _prepare(base, candidate_at=1)
    provider = store.state_home / "provider"
    runner = Runner({"executable": sys.executable,
        "executable_args": [str(provider / "provider.py"), "success", "codex"],
        "trusted_cwd": str(provider), "protocol_only": True},
        store.root / "tasks/loop/sessions")
    controller = WorkflowController(store, runner=runner)
    import ase.runner as runner_module
    import ase.workflow as workflow_module
    original_write = runner_module._write
    original_save = controller._save
    original_replace = os.replace

    def pause(details):
        _save(base / "ready.json", {"pid": os.getpid(), "boundary": boundary, **details})
        while True:
            time.sleep(0.02)

    def write_capture(path, data):
        result = original_write(path, data)
        if not resume and boundary in {"receipt-written", "receipt-written-clock-forward"} and Path(path).name == "receipt.json":
            receipt = json.loads(data)
            if receipt.get("role") == "implementation":
                pause({"receipt": str(path), "execution_id": receipt["execution_id"]})
        return result

    def save_checkpoint(kind, key, value):
        result = original_save(kind, key, value)
        if not resume and ((boundary == "task-receipt-saved" and kind == "implementation" and key == "loop")
                or (boundary in {"stage-receipt-saved", "stage-receipt-saved-stop"}
                    and kind == "stage_implementation" and key == "loop:draft")):
            pause({"receipt": str(Path(value["artifact_root"]) / "receipt.json"),
                "execution_id": value["execution_id"]})
        if resume and boundary == "stage-receipt-saved-stop" and kind == "stage_implementation" and key == "loop:draft":
            # Explicit resume legitimately clears an earlier stop. Commit a
            # new actual stop after that, at the final pre-admission boundary.
            controller.request_stop("loop", reason="Owner stopped after captured response recovery")
        return result

    def replace_source(source, destination, *args, **kwargs):
        result = original_replace(source, destination, *args, **kwargs)
        target = Path(destination).resolve()
        if target.parent == (base / "work").resolve() and target.name in CONTENTS:
            with (base / "replacements.jsonl").open("a", encoding="utf-8") as log:
                log.write(json.dumps({"path": target.name, "pid": os.getpid(),
                    "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
                    "mtime_ns": target.stat().st_mtime_ns}) + "\n")
        return result

    def protocol_judgment(*args, **kwargs):
        return {"status": "BLOCKED", "blocked_reasons": [
            "Protocol fixture provides no independent semantic acceptance"], "rejected_reasons": []}

    original_now = workflow_module.now
    advanced = (datetime.fromisoformat(original_now()) + timedelta(days=1)).isoformat()

    def resumed_clock():
        return advanced if future_clock and resume else original_now()

    with patch.object(workflow_module, "now", new=resumed_clock), \
            patch.object(runner_module, "_write", new=write_capture), \
            patch.object(controller, "_save", side_effect=save_checkpoint), \
            patch.object(controller, "verify", side_effect=protocol_judgment), \
            patch.object(os, "replace", new=replace_source):
        verdict = controller.run("loop", resume=resume)
    _save(base / "result.json", verdict)
    store.close()


class CandidateCaptureRecoveryTests(unittest.TestCase):
    def _launch(self, base, boundary, *, resume=False):
        args = [sys.executable, "-B", str(Path(__file__).resolve()), "--coordinator", str(base), boundary]
        if resume:
            args.append("--resume")
        log_path = base / ("resume.log" if resume else "coordinator.log")
        with log_path.open("ab") as log:
            process = subprocess.Popen(args, stdout=log, stderr=subprocess.STDOUT, cwd=REPOSITORY)
        self.addCleanup(self._stop, process)
        return process

    @staticmethod
    def _stop(process):
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)

    def _capture_kill(self, base, boundary):
        process = self._launch(base, boundary)
        ready_path = base / "ready.json"
        marker = None
        deadline = time.monotonic() + 30
        while marker is None and time.monotonic() < deadline:
            self.assertIsNone(process.poll(), (base / "coordinator.log").read_text())
            if ready_path.exists():
                try:
                    marker = json.loads(ready_path.read_text())
                except json.JSONDecodeError:
                    pass
            if marker is None:
                time.sleep(0.02)
        self.assertIsNotNone(marker, "NOT_TESTED: requested capture boundary was not reached")
        self.assertEqual(marker["pid"], process.pid)
        self.assertEqual(marker["boundary"], boundary)
        receipt = json.loads(Path(marker["receipt"]).read_bytes())
        self.assertEqual(verify_receipt(receipt)["evidence_kind"], "protocol_only")
        process.kill()
        process.wait(timeout=10)
        self.assertNotEqual(process.returncode, 0)
        before = _snapshot(base)
        cycle = before["records"]["cycle:loop:draft"]["payload"]
        self.assertEqual(cycle["phase"], "IMPLEMENTING")
        reference = cycle["implementation_capture"]
        self.assertEqual(set(reference), {"version", "task_id", "stage_id", "candidate_id", "execution_id", "request_sha256"})
        self.assertEqual(reference, {"version": 1, "task_id": "loop", "stage_id": "draft",
            "candidate_id": cycle["candidate_id"], "execution_id": receipt["execution_id"],
            "request_sha256": receipt["request_sha256"]})
        self.assertFalse(any(key.startswith("local_edit_application:") for key in before["records"]))
        self.assertTrue(all(data["sha256"] is None for data in before["files"].values()))
        return before, receipt

    def _assert_accounting(self, snapshot):
        self.assertEqual(snapshot["integrity"], "ok")
        self.assertEqual(snapshot["candidates"], 1)
        self.assertEqual(snapshot["roles"].count("implementation"), 1)
        self.assertEqual(snapshot["roles"].count("diagnostic"), 0)
        budget = snapshot["records"]["budget:loop"]["payload"]
        self.assertEqual(budget["task_cycles"], 1)
        self.assertEqual(budget["stages"]["loop:draft"], 1)

    def _resume(self, base, boundary):
        process = self._launch(base, boundary, resume=True)
        process.wait(timeout=30)
        self.assertEqual(process.returncode, 0, (base / "resume.log").read_text())
        self.assertEqual(json.loads((base / "result.json").read_bytes())["status"], "BLOCKED")
        return _snapshot(base)

    def _exercise(self, boundary):
        with tempfile.TemporaryDirectory(prefix="ase-candidate-capture-") as temporary:
            base = Path(temporary)
            before, receipt = self._capture_kill(base, boundary)
            self._assert_accounting(before)
            candidate = before["records"]["cycle:loop:draft"]["payload"]["candidate_id"]
            after = self._resume(base, boundary)
            self._assert_accounting(after)
            self.assertEqual({name: row["sha256"] for name, row in after["files"].items()}, EXPECTED)
            self.assertEqual(after["records"]["cycle:loop:draft"]["payload"]["candidate_id"], candidate)
            self.assertEqual(after["records"]["stage_implementation:loop:draft"]["payload"], receipt)
            self.assertEqual(after["records"]["local_edit_application:" + candidate]["payload"]["state"], "APPLIED")
            self.assertEqual(after["temporary_files"], [])
            self.assertEqual(len(after["replacements"]), 2)
            self.assertEqual({row["path"] for row in after["replacements"]}, set(CONTENTS))
            original = before["records"]["approval:loop"]
            self.assertEqual(after["records"]["approval:loop"], original)
            again = self._resume(base, boundary)
            self._assert_accounting(again)
            self.assertEqual(again["files"], after["files"])
            self.assertEqual(again["replacements"], after["replacements"])
            if boundary == "receipt-written-clock-forward":
                # Only the wall clock on resume moves. The captured receipt and
                # original active-call start stay authentic and unchanged.
                self.assertEqual(before["records"]["task:loop"]["payload"]["contract"]["execution_policy"]["active_time_budget_seconds"], 60)
                self.assertLess(after["records"]["budget:loop"]["payload"]["active_seconds"], 60)
                self.assertLess(again["records"]["budget:loop"]["payload"]["active_seconds"], 60)

    def _exercise_negative(self, change):
        with tempfile.TemporaryDirectory(prefix="ase-candidate-capture-deny-") as temporary:
            base = Path(temporary)
            # Even an available task/stage latest receipt is not permission to
            # replace an unavailable or invalid exact capture on recovery.
            boundary = "stage-receipt-saved-stop" if change == "stop-before-admission" else "stage-receipt-saved"
            before, receipt = self._capture_kill(base, boundary)
            self._assert_accounting(before)
            if change == "third-state":
                (base / "work/guide.md").write_bytes(b"Owner content after capture\n")
            elif change == "receipt-missing":
                Path(receipt["artifact_root"], "receipt.json").unlink()
            elif change == "result-tampered":
                Path(receipt["artifacts"]["result"]["path"]).write_bytes(b"{}")
            elif change == "reapproval":
                with closing(Store(base / "work", base / "state")) as store:
                    tasks = TaskService(store)
                    task = tasks.snapshot("loop")
                    tasks.approve("loop", proposal_hash=task["proposal_hash"],
                        user_message_ref="test:replacement-approval-after-capture", approved_stage_ids=["draft"])
            elif change == "stop-before-admission":
                pass  # The new owner stop is committed by the resume coordinator.
            elif change in {"pointer-missing", "request-hash", "candidate-binding", "foreign-execution",
                    "reference-list", "reference-string", "version-bool", "task-type", "stage-type",
                    "candidate-type", "execution-type", "request-type"}:
                with closing(Store(base / "work", base / "state")) as store:
                    controller = WorkflowController(store)
                    cycle = read_record(store.db, "cycle", "loop:draft")["payload"]
                    reference = cycle["implementation_capture"]
                    if change == "pointer-missing":
                        cycle.pop("implementation_capture")
                    elif change == "reference-list":
                        cycle["implementation_capture"] = [reference]
                    elif change == "reference-string":
                        cycle["implementation_capture"] = "invalid reference"
                    elif change in {"version-bool", "task-type", "stage-type", "candidate-type", "execution-type", "request-type"}:
                        field, value = {
                            "version-bool": ("version", True), "task-type": ("task_id", ["loop"]),
                            "stage-type": ("stage_id", None), "candidate-type": ("candidate_id", True),
                            "execution-type": ("execution_id", 123), "request-type": ("request_sha256", ["0" * 64]),
                        }[change]
                        reference[field] = value
                    elif change == "request-hash":
                        reference["request_sha256"] = "0" * 64
                    elif change == "candidate-binding":
                        reference["candidate_id"] = "f" * 32
                    else:
                        # Copy authentic metadata to another execution folder;
                        # it still verifies at its original root. A changed
                        # locator must not rebind that original capture.
                        foreign_id = "e" * 32
                        foreign = store.root / "tasks/loop/sessions" / foreign_id
                        foreign.mkdir()
                        (foreign / "receipt.json").write_bytes(
                            Path(receipt["artifact_root"], "receipt.json").read_bytes())
                        reference["execution_id"] = foreign_id
                    controller._save("cycle", "loop:draft", cycle)
            else:
                self.fail("Unknown negative fixture")
            owner_state = _snapshot(base)
            after = self._resume(base, boundary)
            self._assert_accounting(after)
            self.assertEqual(after["files"], owner_state["files"])
            self.assertEqual(after["replacements"], [])
            self.assertFalse(any(key.startswith("local_edit_application:") for key in after["records"]))
            self.assertEqual(after["records"]["approval:loop"], owner_state["records"]["approval:loop"])
            if change == "stop-before-admission":
                self.assertTrue(after["records"]["stop:loop"]["payload"])
            again = self._resume(base, boundary)
            self._assert_accounting(again)
            self.assertEqual(again["files"], after["files"])
            self.assertEqual(again["replacements"], [])

    def test_killed_after_complete_receipt_before_return_recovers_exact_candidate(self):
        self._exercise("receipt-written")

    def test_killed_after_task_receipt_before_stage_save_recovers_exact_candidate(self):
        self._exercise("task-receipt-saved")

    def test_killed_after_stage_receipt_before_intent_recovers_exact_candidate(self):
        self._exercise("stage-receipt-saved")

    def test_captured_candidate_third_state_holds_without_other_file_writes(self):
        self._exercise_negative("third-state")

    def test_missing_exact_receipt_does_not_fall_back_to_saved_latest(self):
        self._exercise_negative("receipt-missing")

    def test_tampered_result_does_not_fall_back_to_saved_latest(self):
        self._exercise_negative("result-tampered")

    def test_same_proposal_reapproval_cannot_authorize_original_capture(self):
        self._exercise_negative("reapproval")

    def test_missing_capture_pointer_does_not_adopt_latest_receipt(self):
        self._exercise_negative("pointer-missing")

    def test_changed_capture_request_digest_blocks_without_new_proposal(self):
        self._exercise_negative("request-hash")

    def test_changed_capture_candidate_binding_blocks_without_new_proposal(self):
        self._exercise_negative("candidate-binding")

    def test_foreign_execution_directory_cannot_rebind_original_receipt(self):
        self._exercise_negative("foreign-execution")

    def test_resumed_clock_does_not_charge_offline_day_to_finite_budget(self):
        self._exercise("receipt-written-clock-forward")

    def test_malformed_capture_reference_types_hold_without_effects(self):
        for change in ("reference-list", "reference-string", "version-bool", "task-type", "stage-type",
                "candidate-type", "execution-type", "request-type"):
            with self.subTest(change=change):
                self._exercise_negative(change)

    def test_new_stop_after_response_recovery_blocks_intent_admission(self):
        self._exercise_negative("stop-before-admission")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--coordinator":
        _coordinator(Path(sys.argv[2]).resolve(), sys.argv[3], "--resume" in sys.argv[4:])
    else:
        unittest.main()
