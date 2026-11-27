"""Real hard-kill recovery with protocol-only CLI and judgment fixtures.

The parent kills only its own Popen coordinator after an exact filesystem
boundary. Approval, SQLite/WAL, execution locks, receipts, checks and recovery
accounting are real. No fixture supplies independent semantic acceptance.
"""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

REPOSITORY = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(REPOSITORY), str(REPOSITORY / "tests")]

from ase.discovery import discover_capabilities, record_discovery
from ase.runner import Runner, verify_receipt
from ase.state import read_record
from ase.store import Store
from ase.tasks import TaskService
from ase.workflow import WorkflowController
from test_runner import FIXTURE
from test_task_contract_v2 import contract


CONTENTS = {
    "appendix.md": "Corrective appendix: preserve these exact bytes.\n",
    "guide.md": "Corrective guide: preserve these exact bytes.\n",
}
EXPECTED = {name: hashlib.sha256(content.encode()).hexdigest()
            for name, content in CONTENTS.items()}


def _save(path, value):
    path.write_text(json.dumps(value, indent=2), encoding="utf-8")


def _counter(base):
    path = base / "state/provider/candidates"
    return int(path.read_text()) if path.exists() else 0


def _snapshot(base):
    database, = (base / "state/projects").glob("*/memory.sqlite3")
    connection = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    records = {row["kind"] + ":" + row["key"]: {
        "revision": row["revision"], "payload": json.loads(row["payload"])}
        for row in connection.execute("SELECT kind,key,revision,payload FROM runtime_records")}
    integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
    connection.close()
    files = {}
    for name in CONTENTS:
        path = base / "work" / name
        files[name] = {
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None,
            "mtime_ns": path.stat().st_mtime_ns if path.is_file() else None,
        }
    return {
        "records": records, "integrity": integrity, "files": files,
        "candidates": _counter(base),
        "roles": [json.loads(path.read_text())["role"]
                  for path in database.parent.glob("tasks/loop/sessions/*/request.json")],
        "temporary_files": [str(path) for path in (base / "work").iterdir()
                            if ".ase-" in path.name],
        "replacements": [json.loads(line) for line in (base / "replacements.jsonl").read_text().splitlines()]
                        if (base / "replacements.jsonl").exists() else [],
        "submission": (base / "work/submitted.txt").read_text()
                      if (base / "work/submitted.txt").is_file() else None,
    }


def _prepare(base, *, with_action=False, candidate_at=4):
    workspace = base / "work"
    workspace.mkdir()
    store = Store(workspace, base / "state")
    service = TaskService(store)
    spec = contract("loop")
    spec["unknowns"], spec["stages"] = [], spec["stages"][:1]
    spec["request"] = "Create the exact approved guide and appendix in one corrective candidate."
    spec["scope"] = {"resources": list(CONTENTS), "effects": ["file_write", "command"]}
    spec["stages"][0]["scope"] = dict(spec["scope"])
    spec["acceptance"][0]["expected"] = "Guide and appendix both match their original required exact contents."
    command = ("from pathlib import Path;import hashlib,sys;expected=" + repr(EXPECTED)
               + ";sys.exit(0 if all(Path(p).is_file() and hashlib.sha256(Path(p).read_bytes()).hexdigest()==h "
                 "for p,h in expected.items()) else 1)")
    spec["evidence"].append({"id": "exact-batch", "kind": "command", "required": True,
                             "argv": [sys.executable, "-c", command]})
    spec["acceptance"][0]["evidence_ids"].append("exact-batch")
    spec["execution_policy"] = {
        "recovery_mode": "progress", "max_stage_cycles": 4, "max_task_cycles": 4,
    }
    if with_action:
        spec["request"] += " Then submit the declared disposable local marker exactly once."
        spec["scope"]["resources"].append("submitted.txt")
        # Both approved scopes share the resource list and already declare
        # file_write and command. The marker is the full local action footprint.
        submission = ("from pathlib import Path;p=Path('submitted.txt');"
                      "p.write_text(p.read_text()+'x' if p.exists() else 'x')")
        observation = ("from pathlib import Path;import json;p=Path('submitted.txt');"
                       "ok=p.is_file() and p.read_text()=='x';"
                       "print(json.dumps({'exists':ok,'resource_id':'submitted.txt' if ok else None}))")
        spec["stages"][0]["actions"] = [{
            "id": "submit-disposable-marker", "argv": [sys.executable, "-c", submission],
            "observation_argv": [sys.executable, "-c", observation],
        }]
        spec["evidence"].append({"id": "submission-once", "kind": "command", "required": True,
            "argv": [sys.executable, "-c", "from pathlib import Path;import sys;p=Path('submitted.txt');"
                     "sys.exit(0 if p.is_file() and p.read_text()=='x' else 1)"]})
        spec["acceptance"][0]["evidence_ids"].append("submission-once")
        spec["acceptance"][0]["expected"] += " The declared local marker is submitted exactly once."
    task = service.create(spec)
    task = service.propose("loop", {"summary": "Perform the confirmed exact batch", "stage_ids": ["draft"]},
                           expected_revision=task["revision"])
    service.approve("loop", proposal_hash=task["proposal_hash"],
                    user_message_ref="test:scope-authority", approved_stage_ids=["draft"])
    record_discovery(store, "loop", discover_capabilities(store.project,
                     scope={"resources": list(CONTENTS)}, backend_config={}), expected_revision=0)
    provider = store.state_home / "provider"
    provider.mkdir()
    edits = [{"path": name, "operation": "write", "before_sha256": "", "content": value}
             for name, value in CONTENTS.items()]
    # Reuse the actual Runner protocol fixture. It is transport evidence only.
    body = """
schema = json.loads(pathlib.Path(args[9]).read_text())
if 'edits' in schema['properties']:
 counter = pathlib.Path('candidates')
 count = int(counter.read_text()) + 1 if counter.exists() else 1
 counter.write_text(str(count))
 payload = {'edits':EDITS if count == CANDIDATE_AT else [],'summary':'Approved exact candidate batch'}
elif 'cause' in schema['properties']:
 payload = {'cause':'Original captured exact-batch checks remain unsatisfied', 'recommendation':'Apply the already approved two-file corrective batch once', 'requires_new_approval':False}
""".replace("EDITS", repr(edits)).replace("CANDIDATE_AT", str(candidate_at))
    (provider / "provider.py").write_text(FIXTURE.replace("payload = {'verdict':'PASS'}",
        "payload = {'verdict':'PASS'}\n" + body), encoding="utf-8")
    return store


class _PartialWriter:
    """Test-only interruption inside a real binary temp-file write."""

    def __init__(self, stream, pause, path):
        self.stream, self.pause, self.path = stream, pause, path

    def __getattr__(self, name):
        return getattr(self.stream, name)

    def __enter__(self):
        self.stream.__enter__()
        return self

    def __exit__(self, *args):
        return self.stream.__exit__(*args)

    def write(self, data):
        fragment = data[:13]
        self.stream.write(fragment)
        self.stream.flush()
        os.fsync(self.stream.fileno())
        self.pause({"boundary": "partial-temp", "temporary": str(self.path), "prefix_bytes": len(fragment)})


def _coordinator(base, boundary, resume):
    ordinary = boundary == "ordinary-drift-before-actions"
    candidate_at = 1 if ordinary else 4
    with_action = boundary in {"after-checkpoint-before-actions", "action-unknown", "ordinary-drift-before-actions"}
    store = Store(base / "work", base / "state") if resume else _prepare(
        base, with_action=with_action, candidate_at=candidate_at)
    provider = store.state_home / "provider"
    runner = Runner({"executable": sys.executable,
        "executable_args": [str(provider / "provider.py"), "success", "codex"],
        "trusted_cwd": str(provider), "protocol_only": True},
        store.root / "tasks/loop/sessions")
    controller = WorkflowController(store, runner=runner)
    work = (base / "work").resolve()
    original_open, original_fdopen = os.open, os.fdopen
    original_replace, original_write = os.replace, Path.write_text
    original_save = controller._save
    original_execute_actions = controller.execute_actions
    original_execute_command = controller._execute_command
    owned_descriptors = {}

    def protocol_judgment(*args, **kwargs):
        # Rejections drive the actual original recovery accounting. Once the
        # corrective candidate exists, stop at verification without fake PASS.
        if _counter(base) >= candidate_at:
            return {"status": "BLOCKED", "blocked_reasons": [
                "Protocol fixture provides no independent semantic acceptance"], "rejected_reasons": []}
        return {"status": "REJECTED", "blocked_reasons": [],
                "rejected_reasons": ["Mandatory AC failed: AC1"]}

    def pause(details):
        _save(base / "ready.json", {"pid": os.getpid(), "details": details})
        while True:
            time.sleep(0.02)

    def owned_stage(path):
        path = Path(path).resolve()
        return path.parent == work and ".ase-" in path.name

    def open_source_stage(path, *args, **kwargs):
        descriptor = original_open(path, *args, **kwargs)
        if not isinstance(path, int) and owned_stage(path):
            owned_descriptors[descriptor] = Path(path).resolve()
        return descriptor

    def fdopen_source_stage(descriptor, *args, **kwargs):
        stream = original_fdopen(descriptor, *args, **kwargs)
        path = owned_descriptors.pop(descriptor, None)
        if path is not None and not resume and boundary == "partial-temp":
            return _PartialWriter(stream, pause, path)
        return stream

    def legacy_source_temp(path, content, *args, **kwargs):
        # Keeps the regression meaningful on the original pre-journal runtime,
        # which used Path.write_text instead of binary staged writes.
        if not resume and boundary == "partial-temp" and owned_stage(path):
            with path.open("wb") as stream:
                stream.write(content.encode()[:13])
                stream.flush()
                os.fsync(stream.fileno())
            pause({"boundary": "partial-temp", "temporary": str(path), "prefix_bytes": 13})
        return original_write(path, content, *args, **kwargs)

    def replace_source(source, destination, *args, **kwargs):
        result = original_replace(source, destination, *args, **kwargs)
        target = Path(destination).resolve()
        if target.parent == work and target.name in CONTENTS:
            with (base / "replacements.jsonl").open("a", encoding="utf-8") as log:
                log.write(json.dumps({"path": target.name, "pid": os.getpid(),
                    "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
                    "mtime_ns": target.stat().st_mtime_ns}) + "\n")
            if not resume and boundary == "between-replaces" and target.name == "appendix.md":
                pause({"boundary": boundary, "replaced": target.name})
        return result

    def save_checkpoint(kind, key, value):
        if (not resume and boundary == "after-all-before-checkpoint" and kind == "cycle"
                and key == "loop:draft" and value.get("phase") == "CHECKING"
                and _counter(base) == 4):
            pause({"boundary": boundary})
        return original_save(kind, key, value)

    def execute_pending_action(task_id, stage):
        if with_action and _counter(base) < candidate_at:
            # Arrange the pending fourth-candidate boundary honestly: earlier
            # three protocol-only candidates exercise real stalled checks, but
            # intentionally do not submit this declared local fixture action.
            return []
        if with_action and not resume and boundary in {"after-checkpoint-before-actions", "ordinary-drift-before-actions"}:
            progress = read_record(store.db, "cycle", "loop:draft")["payload"]
            pause({"boundary": boundary, "phase": progress["phase"]})
        return original_execute_actions(task_id, stage)

    def stop_uncertain_submission(task_id, spec, argv, *args, **kwargs):
        if (not resume and boundary == "action-unknown" and spec.get("id") == "submit-disposable-marker"
                and list(argv) == spec["argv"]):
            # The existing OperationLedger has genuinely committed started()
            # as UNKNOWN. A hard kill loses whether process launch happened;
            # later absence alone must not authorize a new submission.
            pause({"boundary": boundary, "phase": read_record(store.db, "cycle", "loop:draft")["payload"]["phase"]})
        return original_execute_command(task_id, spec, argv, *args, **kwargs)

    with patch.object(controller, "verify", side_effect=protocol_judgment), \
            patch.object(controller, "_save", side_effect=save_checkpoint), \
            patch.object(controller, "execute_actions", side_effect=execute_pending_action), \
            patch.object(controller, "_execute_command", side_effect=stop_uncertain_submission), \
            patch.object(os, "open", new=open_source_stage), \
            patch.object(os, "fdopen", new=fdopen_source_stage), \
            patch.object(os, "replace", new=replace_source), \
            patch.object(Path, "write_text", new=legacy_source_temp):
        verdict = controller.run("loop", resume=resume)
    _save(base / "result.json", verdict)
    store.close()


class LocalEditHardKillRecoveryTests(unittest.TestCase):
    def _launch(self, base, boundary, resume=False):
        args = [sys.executable, str(Path(__file__).resolve()), "--coordinator", str(base), boundary]
        if resume:
            args.append("--resume")
        with (base / ("resume.log" if resume else "coordinator.log")).open("ab") as log:
            process = subprocess.Popen(args, stdout=log, stderr=subprocess.STDOUT, cwd=REPOSITORY)
        self.addCleanup(self._stop, process)
        return process

    @staticmethod
    def _stop(process):
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)

    def _assert_accounting(self, snapshot, *, candidates=4, diagnostics=1):
        self.assertEqual(snapshot["integrity"], "ok")
        self.assertEqual(snapshot["candidates"], candidates)
        self.assertEqual(snapshot["roles"].count("implementation"), candidates)
        self.assertEqual(snapshot["roles"].count("diagnostic"), diagnostics)
        budget = snapshot["records"]["budget:loop"]["payload"]
        self.assertEqual(budget["task_cycles"], candidates)
        self.assertEqual(budget["stages"]["loop:draft"], candidates)
        self.assertEqual(budget.get("diagnostics", {}).get("loop:draft", 0), diagnostics)

    def _exercise(self, boundary):
        ordinary = boundary == "ordinary-drift-before-actions"
        accounting = {"candidates": 1, "diagnostics": 0} if ordinary else {}
        with tempfile.TemporaryDirectory(prefix="ase-hard-kill-") as temporary:
            base = Path(temporary)
            process = self._launch(base, boundary)
            ready = base / "ready.json"
            deadline = time.monotonic() + 30
            marker = None
            while marker is None and time.monotonic() < deadline:
                self.assertIsNone(process.poll(), (base / "coordinator.log").read_text())
                if ready.exists():
                    try:
                        marker = json.loads(ready.read_text())
                    except json.JSONDecodeError:
                        pass
                if marker is None:
                    time.sleep(0.02)
            self.assertIsNotNone(marker, "Coordinator never reached the requested mutation boundary")
            self.assertEqual(marker["pid"], process.pid)
            self.assertEqual(marker["details"]["boundary"], boundary)
            process.kill()  # TerminateProcess on Windows; no Python cleanup.
            process.wait(timeout=10)
            self.assertNotEqual(process.returncode, 0)
            before = _snapshot(base)
            self._assert_accounting(before, **accounting)
            if boundary == "partial-temp":
                self.assertTrue(before["temporary_files"])
                temporary_path = Path(marker["details"]["temporary"])
                self.assertEqual(temporary_path.stat().st_size, 13)
                self.assertIsNone(before["files"]["appendix.md"]["sha256"])
            elif boundary == "between-replaces":
                self.assertEqual(before["files"]["appendix.md"]["sha256"], EXPECTED["appendix.md"])
                self.assertIsNone(before["files"]["guide.md"]["sha256"])
            else:
                self.assertEqual({name: data["sha256"] for name, data in before["files"].items()}, EXPECTED)

            cycle_before = before["records"]["cycle:loop:draft"]["payload"]
            candidate_id = cycle_before["candidate_id"]
            self.assertEqual(cycle_before["corrective_probe"], not ordinary)
            with_action = boundary in {"after-checkpoint-before-actions", "action-unknown", "ordinary-drift-before-actions"}
            self.assertEqual(cycle_before["phase"], "CHECKING" if with_action else "APPLYING")
            self.assertEqual(cycle_before["local_edit_application_id"], candidate_id)
            application_key = "local_edit_application:" + candidate_id
            self.assertIn(application_key, before["records"])
            operations_before = [record["payload"] for key, record in before["records"].items()
                                 if key.startswith("operation:")]
            if with_action:
                self.assertEqual(before["records"][application_key]["payload"]["state"], "APPLIED")
                self.assertIsNone(before["submission"])
                if boundary in {"after-checkpoint-before-actions", "ordinary-drift-before-actions"}:
                    self.assertEqual(operations_before, [], "The pending action must be genuinely unsubmitted")
                else:
                    self.assertEqual(len(operations_before), 1)
                    self.assertEqual(operations_before[0]["state"], "UNKNOWN")
                    self.assertEqual(operations_before[0]["attempt_number"], 1)
                    self.assertTrue(operations_before[0]["ever_attempted"])
            candidate = before["records"]["stage_implementation:loop:draft"]["payload"]
            receipts = [candidate]
            if not ordinary:
                receipts.append(before["records"]["diagnostic:loop"]["payload"]["receipt"])
            for receipt in receipts:
                validation = verify_receipt(receipt)
                self.assertTrue(validation["valid"], validation)
                self.assertEqual(validation["evidence_kind"], "protocol_only")

            expected_after = dict(EXPECTED)
            if ordinary:
                # A real owner edit after the coordinator died invalidates the
                # already-applied candidate's postimage. Never overwrite it or
                # use the old CHECKING phase as permission for the pending action.
                owner_bytes = b"Owner changed the applied guide after the crash.\n"
                (base / "work/guide.md").write_bytes(owner_bytes)
                expected_after["guide.md"] = hashlib.sha256(owner_bytes).hexdigest()
                owner_files = _snapshot(base)["files"]

            resumed = self._launch(base, boundary, resume=True)
            resumed.wait(timeout=30)
            self.assertEqual(resumed.returncode, 0, (base / "resume.log").read_text())
            after = _snapshot(base)
            self._assert_accounting(after, **accounting)
            self.assertEqual({name: data["sha256"] for name, data in after["files"].items()}, expected_after)
            self.assertEqual(after["temporary_files"], [])
            self.assertEqual(after["records"]["cycle:loop:draft"]["payload"]["candidate_id"], candidate_id)
            self.assertEqual(after["records"][application_key]["payload"]["state"], "APPLIED")
            checks = after["records"]["checks:loop"]["payload"]["results"]
            if boundary != "action-unknown":
                self.assertEqual(next(item for item in checks if item["id"] == "exact-batch")["exit_code"], 1 if ordinary else 0)
            self.assertEqual(json.loads((base / "result.json").read_text())["status"], "BLOCKED")
            self.assertIsNone(after["records"]["active_workflow:loop"]["payload"])
            if ordinary:
                self.assertIsNone(after["submission"])
                self.assertFalse(any(key.startswith("operation:") for key in after["records"]))
                self.assertEqual(after["files"], owner_files)
                cycle_after = after["records"]["cycle:loop:draft"]["payload"]
                self.assertTrue(cycle_after["read_only_recovery"])
                self.assertTrue(cycle_after["local_edit_continuation_blocker"])
            elif with_action:
                operations_after = [record["payload"] for key, record in after["records"].items()
                                    if key.startswith("operation:")]
                self.assertEqual(len(operations_after), 1)
                operation = operations_after[0]
                self.assertEqual(operation["attempt_number"], 1)
                if boundary == "after-checkpoint-before-actions":
                    self.assertEqual(after["submission"], "x")
                    self.assertEqual(operation["state"], "APPLIED")
                    self.assertTrue(operation["ever_applied"])
                    self.assertEqual(operation["execution_receipt"]["execution"]["exit_code"], 0)
                    self.assertTrue(operation["observation"]["exists"])
                    self.assertEqual(next(item for item in checks if item["id"] == "submission-once")["exit_code"], 0)
                else:
                    self.assertIsNone(after["submission"])
                    self.assertEqual(operation["state"], "UNKNOWN")
                    self.assertFalse(operation["ever_applied"])
                    self.assertFalse(operation["observation"]["exists"])
                    verdict = json.loads((base / "result.json").read_text())
                    self.assertTrue(any("UNKNOWN" in reason for reason in verdict["blocked_reasons"]), verdict)
            for name in CONTENTS:
                replacements = [item for item in after["replacements"] if item["path"] == name]
                self.assertEqual(len(replacements), 1, after["replacements"])
                if not ordinary and before["files"][name]["sha256"] == EXPECTED[name]:
                    self.assertEqual(after["files"][name]["mtime_ns"], before["files"][name]["mtime_ns"])

            repeated = self._launch(base, boundary, resume=True)
            repeated.wait(timeout=30)
            self.assertEqual(repeated.returncode, 0, (base / "resume.log").read_text())
            again = _snapshot(base)
            self._assert_accounting(again, **accounting)
            self.assertEqual(again["files"], after["files"])
            self.assertEqual(again["replacements"], after["replacements"])
            if ordinary:
                self.assertIsNone(again["submission"])
                self.assertFalse(any(key.startswith("operation:") for key in again["records"]))
                self.assertEqual(again["records"][application_key]["payload"]["state"], "APPLIED")
            elif with_action:
                self.assertEqual(again["submission"], after["submission"])
                operations_again = [record["payload"] for key, record in again["records"].items()
                                    if key.startswith("operation:")]
                self.assertEqual(len(operations_again), 1)
                self.assertEqual(operations_again[0]["attempt_number"], 1)
                self.assertEqual(operations_again[0]["state"], operation["state"])

    def test_hard_kill_during_partial_temporary_write_resumes_same_candidate(self):
        self._exercise("partial-temp")

    def test_hard_kill_between_replacements_skips_already_applied_file(self):
        self._exercise("between-replaces")

    def test_hard_kill_after_all_replacements_does_not_rewrite_postimages(self):
        self._exercise("after-all-before-checkpoint")

    def test_hard_kill_after_checking_commit_executes_pending_action_once(self):
        self._exercise("after-checkpoint-before-actions")

    def test_hard_kill_with_unknown_action_does_not_submit_again(self):
        self._exercise("action-unknown")

    def test_hard_kill_ordinary_candidate_owner_drift_blocks_pending_action(self):
        self._exercise("ordinary-drift-before-actions")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--coordinator":
        _coordinator(Path(sys.argv[2]).resolve(), sys.argv[3], "--resume" in sys.argv[4:])
    else:
        unittest.main()
