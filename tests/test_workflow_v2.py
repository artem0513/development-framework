"""Budgets and filesystem effects are tested without semantic model stubs."""
import hashlib
import json
from pathlib import Path
import sys
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from ase.store import Store
from ase.tasks import TaskService, record_baseline
from ase.workflow import WorkflowController, apply_edits
from ase.state import read_record, transaction, write_record
from ase.gates import GateService, source_snapshot
from ase.discovery import discover_capabilities, record_discovery
from ase.operations import OperationLedger
from ase.runner import Runner
from ase.journal import Journal
from test_runner import FIXTURE
from test_task_contract_v2 import contract


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ase-workflow-")
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        work = root / "work"
        work.mkdir()
        self.store = Store(work, root / "state")
        self.addCleanup(self.store.close)
        self.tasks = TaskService(self.store)
        legacy = contract()
        legacy["execution_policy"] = {"recovery_mode": "fixed"}
        task = self.tasks.create(legacy)
        task = self.tasks.propose(task["task_id"], {"summary": "Create guide; publication deferred", "stage_ids": ["draft"]}, expected_revision=task["revision"])
        self.tasks.approve(task["task_id"], proposal_hash=task["proposal_hash"], user_message_ref="chat:approved", approved_stage_ids=["draft"])
        self.controller = WorkflowController(self.store, runner=None)

    def test_three_stage_cycles_survive_resume(self):
        for i in range(3):
            self.assertEqual(self.controller.reserve_cycle("approval-task", "draft")["stage_cycles"], i + 1)
        new_controller = WorkflowController(self.store, runner=None)
        with self.assertRaisesRegex(ValueError, "budget"):
            new_controller.reserve_cycle("approval-task", "draft")

    def test_second_same_cause_uses_one_diagnostic_pass(self):
        self.assertFalse(self.controller.record_failure("approval-task", "draft", ["same cause"])["diagnostic_required"])
        self.assertTrue(self.controller.record_failure("approval-task", "draft", ["same cause"])["diagnostic_required"])
        self.controller.reserve_diagnostic("approval-task", "draft")
        with self.assertRaisesRegex(ValueError, "diagnostic"):
            self.controller.reserve_diagnostic("approval-task", "draft")

    def test_apply_edits_checks_scope_and_preimage_before_any_write(self):
        path = self.store.repo / "guide.md"
        path.write_text("old", encoding="utf-8")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        scope = {"resources": ["guide.md"], "effects": ["file_write"]}
        with self.assertRaisesRegex(ValueError, "scope"):
            apply_edits(self.store.repo, scope, [{"path": "guide.md", "before_sha256": digest, "content": "new", "operation": "write"},
                                               {"path": "../escape", "before_sha256": None, "content": "bad", "operation": "write"}])
        self.assertEqual(path.read_text(), "old")
        with self.assertRaisesRegex(ValueError, "preimage"):
            apply_edits(self.store.repo, scope, [{"path": "guide.md", "before_sha256": "stale", "content": "new", "operation": "write"}])
        apply_edits(self.store.repo, scope, [{"path": "guide.md", "before_sha256": digest, "content": "new", "operation": "write"}])
        self.assertEqual(path.read_text(), "new")

    def test_stop_persists_and_never_marks_completed(self):
        self.controller.request_stop("approval-task", reason="finish today")
        self.assertEqual(self.tasks.snapshot("approval-task")["state"], "PAUSED")
        with self.assertRaisesRegex(ValueError, "stop|paused"):
            self.controller.reserve_cycle("approval-task", "draft")
        self.assertIn("`ase task resume approval-task`", self.controller._run("approval-task")["blocked_reasons"][0])

    def test_unready_causal_repair_names_the_dossier_and_resume_commands(self):
        blocked = {"ready": False, "reason": "Save the bugfix investigation before proposing a causal repair",
                   "needs_owner_input": False, "next_action": "Continue authorized investigation."}
        with patch("ase.testing.bugfix.repair_readiness", return_value=blocked):
            result = self.controller._repair_readiness("approval-task", "draft")
        self.assertTrue(result["next_action"].startswith("Continue authorized investigation."))
        self.assertIn("`ase adequacy plan approval-task --expected-revision 0 --file <plan.json>`", result["next_action"])
        self.assertIn("`ase task resume approval-task`", result["next_action"])

    def test_blocked_run_closure_reuses_the_composed_next_action(self):
        hint = "Save the dossier: `ase adequacy plan approval-task --expected-revision 0 --file <plan.json>`, then `ase task resume approval-task`."
        verdict = {"status": "BLOCKED", "blocked_reasons": ["Bugfix investigation missing"], "next_action": hint}
        with patch.object(WorkflowController, "_run", return_value=verdict):
            result = self.controller.run("approval-task")
        self.assertEqual((result["next_action"], result["closure"]["next_action"]), (hint, hint))
        with patch.object(WorkflowController, "_run", return_value={"status": "BLOCKED", "blocked_reasons": ["Other"]}):
            result = self.controller.run("approval-task", resume=True)
        self.assertIn("resume", result["closure"]["next_action"])

    def test_approved_whole_task_verify_names_the_finish_command(self):
        import re, shlex
        from ase.__main__ import parser
        with patch.object(WorkflowController, "_verify", return_value={"status": "APPROVED", "next_action": None}):
            result = self.controller.verify("approval-task")
            stage = self.controller.verify("approval-task", "draft")
        command = re.search(r"`(ase task finish [^`]+)`", result["next_action"]).group(1)
        args = parser().parse_args(shlex.split(re.sub(r"<[^>]+>", "x", command))[1:])
        self.assertEqual((args.action, args.task_id, args.base, args.body_file), ("finish", "approval-task", "x", "x"))
        self.assertIsNone(stage["next_action"])

    def test_packet_only_reads_approved_files_and_reasoned_dependencies(self):
        (self.store.repo / "guide.md").write_text("scoped content")
        (self.store.repo / "secret.bin").write_bytes(b"\xffPRIVATE")
        packet = self.controller._packet("approval-task")
        self.assertIn("guide.md", packet["coverage"]["included"])
        self.assertNotIn("secret.bin", packet["coverage"]["included"])
        self.assertEqual(packet["coverage"]["missing"], [])

    def test_directory_discovery_never_reads_a_directory_as_source_bytes(self):
        (self.store.repo / "guide.md").write_text("actual source")
        discovery = discover_capabilities(self.store.project, scope={"resources": ["."]}, backend_config={})
        record_discovery(self.store, "approval-task", discovery, expected_revision=0)
        packet = self.controller._packet("approval-task")
        self.assertEqual(packet["coverage"]["missing"], [])
        self.assertNotIn(".", packet["coverage"]["included"])
        self.assertIn("guide.md", packet["coverage"]["included"])

    def test_structured_windows_resource_path_remains_usable_after_recording(self):
        policy = self.store.repo / "shared" / "policy.md"
        policy.parent.mkdir()
        policy.write_text("Required policy", encoding="utf-8")
        payload = discover_capabilities(self.store.project,
            scope={"resources": ["guide.md", "shared/policy.md"]}, backend_config={})
        next(item for item in payload["resources"] if item["path"] == "shared/policy.md")["path"] = "shared\\policy.md"
        record_discovery(self.store, "approval-task", payload, expected_revision=0)
        self.assertEqual(self.tasks.readiness("approval-task", "draft")["required_source_gaps"], [])
        self.tasks.require_approval("approval-task", "draft")
        self.assertIn("shared/policy.md", self.controller._packet("approval-task", "draft")["coverage"]["included"])

    def representation(self, raw, text="Prepared summary"):
        artifact = self.store.repo / "guide.md"
        artifact.write_bytes(raw)
        readable = self.store.repo / "guide.txt"
        readable.write_text(text, encoding="utf-8")
        payload = discover_capabilities(self.store.project, scope={"resources": ["guide.md"]}, backend_config={})
        payload["representations"] = [{"artifact_path": "guide.md", "artifact_sha256": hashlib.sha256(raw).hexdigest(),
            "path": "guide.txt", "sha256": hashlib.sha256(readable.read_bytes()).hexdigest(), "method": "Prepared text extraction"}]
        record_discovery(self.store, "approval-task", payload, expected_revision=0)

    def test_readable_raw_authority_is_supplied_despite_representation(self):
        self.representation(b"Authoritative policy: deployment is prohibited.", "Deployment is approved.")
        packet = self.controller._packet("approval-task", "draft")
        files = {item["path"]: item for item in packet["files"]}
        self.assertIn("guide.md", files)
        self.assertEqual(files["guide.md"]["content"], "Authoritative policy: deployment is prohibited.")
        self.assertEqual(packet["coverage"]["missing"], [])

    def test_oversized_readable_raw_authority_is_an_explicit_gap(self):
        self.representation(b"x" * 512001)
        packet = self.controller._packet("approval-task", "draft")
        self.assertTrue(any("guide.md" in gap and "bound" in gap for gap in packet["coverage"]["missing"]))

    def test_binary_nul_artifact_keeps_its_bound_representation(self):
        self.representation(b"native\x00artifact", "Extracted native content")
        packet = self.controller._packet("approval-task", "draft")
        self.assertNotIn("guide.md", packet["coverage"]["included"])
        self.assertEqual(next(item["content"] for item in packet["files"] if item["path"] == "guide.txt"), "Extracted native content")
        self.assertEqual(packet["coverage"]["missing"], [])

    def test_v2_gate_rejects_changes_to_project_protected_shared_code(self):
        repo = self.store.repo
        (repo / "Jobs").mkdir()
        (repo / "Jobs" / "JobRunner.cs").write_text("class JobRunner {}")
        (repo / "guide.md").write_text("old content")
        (repo / ".devframework").mkdir()
        (repo / ".devframework" / "protected-paths.txt").write_text("# shared code\nJobs\\*\n")
        for args in (("init", "-b", "feature/protect"), ("config", "user.email", "test@example.invalid"), ("config", "user.name", "Protect test"), ("add", "."), ("commit", "-m", "baseline")):
            subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)
        c = contract("protect")
        c["profile"] = "software"
        self.tasks.create(c)
        self.controller._save("baseline", "protect", source_snapshot(self.store, c, task_id="protect"))
        (repo / "guide.md").write_text("feature change")
        reasons = GateService(self.store).gate("protect")["reasons"]
        self.assertFalse(any("protected" in r.lower() for r in reasons), reasons)
        (repo / "Jobs" / "JobRunner.cs").write_text("class JobRunner { /* atomic claim */ }")
        reasons = GateService(self.store).gate("protect")["reasons"]
        self.assertIn("Unauthorized protected file changes: Jobs/JobRunner.cs", reasons)
        # The list protects itself: a task cannot unprotect shared code by editing it.
        (repo / "Jobs" / "JobRunner.cs").write_text("class JobRunner {}")
        (repo / ".devframework" / "protected-paths.txt").write_text("# nothing protected now\n")
        reasons = GateService(self.store).gate("protect")["reasons"]
        self.assertIn("Unauthorized protected file changes: .devframework/protected-paths.txt", reasons)

    def test_v2_high_risk_approval_binds_to_the_current_source(self):
        repo = self.store.repo
        (repo / "guide.md").write_text("old content")
        for args in (("init", "-b", "feature/high"), ("config", "user.email", "test@example.invalid"), ("config", "user.name", "High test"), ("add", "."), ("commit", "-m", "baseline")):
            subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)
        c = contract("high")
        c.update(profile="software", risk="HIGH")
        self.tasks.create(c)
        task = self.tasks.snapshot("high")
        self.controller._save("baseline", "high", source_snapshot(self.store, task["contract"], task_id="high"))
        reason = "HIGH risk requires current explicit approval"
        self.assertIn(reason, GateService(self.store).gate("high")["reasons"])
        approved = source_snapshot(self.store, task["contract"], task_id="high")["fingerprint"]
        with transaction(self.store.db) as db:
            write_record(db, "high_risk_approval", "high", {"fingerprint": approved, "contract_hash": task["contract_hash"]}, expected_revision=0)
        self.assertNotIn(reason, GateService(self.store).gate("high")["reasons"])
        (repo / "guide.md").write_text("changed after approval")
        self.assertIn(reason, GateService(self.store).gate("high")["reasons"])

    def test_gate_never_completes_unexecuted_stages(self):
        verdict = GateService(self.store).gate("approval-task")
        self.assertIn("Stage incomplete: draft", verdict["reasons"])
        self.assertIn("Stage incomplete: publish", verdict["reasons"])

    def test_check_requires_approval_and_preserves_actual_artifact_path(self):
        c = contract("unapproved")
        c["evidence"][0]["path"] = "guide.md"
        self.tasks.create(c)
        with self.assertRaisesRegex(ValueError, "approval"):
            self.controller.check("unapproved")

    def test_active_time_budget_cannot_reset_on_new_controller(self):
        record = read_record(self.store.db, "task", "approval-task")
        record["payload"]["contract"]["execution_policy"]["active_time_budget_seconds"] = 1
        with transaction(self.store.db) as db:
            write_record(db, "task", "approval-task", record["payload"], expected_revision=record["revision"])
        self.controller.charge_time("approval-task", 1.1)
        with self.assertRaisesRegex(ValueError, "time budget"):
            WorkflowController(self.store).reserve_cycle("approval-task", "draft")

    def test_real_infrastructure_action_reconciles_without_duplicate_effect(self):
        c = contract("resource-task")
        c["unknowns"] = []
        c["profile"] = "infrastructure"
        c["scope"] = {"resources": ["resource"], "effects": ["command"]}
        c["stages"] = [{"id": "provision", "scope": c["scope"], "acceptance_ids": ["AC1"], "actions": [{
            "id": "provision-resource", "argv": [sys.executable, "-c", "from pathlib import Path; p=Path('resource'); p.write_text(p.read_text()+'x' if p.exists() else 'x')"],
            "observation_argv": [sys.executable, "-c", "from pathlib import Path; import json; print(json.dumps({'exists':Path('resource').exists(),'resource_id':'controlled-test-resource'}))"]}]}]
        c["evidence"] = [{"id": "guide", "kind": "artifact", "path": "resource"}]
        task = self.tasks.create(c)
        task = self.tasks.propose(task["task_id"], {"summary": "Provision controlled local test resource", "stage_ids": ["provision"]}, expected_revision=task["revision"])
        self.tasks.approve(task["task_id"], proposal_hash=task["proposal_hash"], user_message_ref="chat:approved", approved_stage_ids=["provision"])
        stage = self.tasks.snapshot(task["task_id"])["contract"]["stages"][0]
        self.controller.execute_actions(task["task_id"], stage)
        self.controller.execute_actions(task["task_id"], stage)
        self.assertEqual((self.store.repo / "resource").read_text(), "x")

    def test_stage_checks_do_not_require_deferred_artifacts(self):
        c = contract("partial-checks")
        c["scope"]["resources"].append("published.txt")
        c["evidence"].append({"id": "publication", "kind": "artifact", "path": "published.txt"})
        c["acceptance"].append({"id": "AC2", "expected": "Publication exists", "evidence_ids": ["publication"]})
        c["stages"][1]["acceptance_ids"] = ["AC2"]
        task = self.tasks.create(c)
        task = self.tasks.propose(task["task_id"], {"summary": "Draft only", "stage_ids": ["draft"]}, expected_revision=task["revision"])
        self.tasks.approve(task["task_id"], proposal_hash=task["proposal_hash"], user_message_ref="chat:approve", approved_stage_ids=["draft"])
        (self.store.repo / "guide.md").write_text("actual draft")
        result = self.controller.check(task["task_id"], "draft")
        self.assertEqual([r["id"] for r in result["results"]], ["guide"])

    def test_discovered_dependency_changes_invalidate_non_git_evidence(self):
        (self.store.repo / "guide.md").write_text("guide")
        dependency = self.store.repo / "policy.md"
        dependency.write_text("first")
        discovery = discover_capabilities(self.store.project, scope={"resources": ["guide.md"], "dependencies": [{"path": "policy.md", "reason": "Governs guide policy"}]}, backend_config={})
        record_discovery(self.store, "approval-task", discovery, expected_revision=0)
        c = self.tasks.snapshot("approval-task")["contract"]
        before = source_snapshot(self.store, c, task_id="approval-task")
        dependency.write_text("changed")
        after = source_snapshot(self.store, c, task_id="approval-task")
        self.assertNotEqual(before["fingerprint"], after["fingerprint"])

    def test_unknown_effect_blocks_gate_independently_of_local_source(self):
        ledger = OperationLedger(self.store)
        ledger.prepare("approval-task", "draft", "remote-effect", {"create": "resource"})
        ledger.started("remote-effect")
        verdict = GateService(self.store).gate("approval-task")
        self.assertTrue(any("Uncertain external operation" in r for r in verdict["reasons"]))

    def test_packet_log_preserves_captured_windows_newlines_and_hash(self):
        folder = self.store.root / "tasks" / "approval-task" / "checks"
        folder.mkdir(parents=True)
        log = folder / "actual.log"
        raw = b"Ran 1 test in 0.001s\r\n\r\nOK\r\n"
        log.write_bytes(raw)
        self.controller._save("checks", "approval-task", {"results": [{"id": "unit", "log": str(log), "log_sha256": hashlib.sha256(raw).hexdigest()}]})
        packet = self.controller._packet("approval-task")
        captured = next(f for f in packet["files"] if f["path"] == "_ase_context/logs/unit.txt")
        self.assertEqual(captured["sha256"], hashlib.sha256(raw).hexdigest())

    def bound_task(self, task_id, transport, *, parts=()):
        spec = contract(task_id)
        if transport is not None:
            spec["context_transport"] = transport
        for name in parts:  # extra required artifacts, each under the per-file source bound
            for owner in (spec["scope"], spec["stages"][0]["scope"], spec["stages"][1]["scope"]):
                owner["resources"].append(name)
            spec["evidence"].append({"id": name.split(".")[0], "kind": "artifact", "path": name, "required": True})
            spec["acceptance"][0]["evidence_ids"].append(name.split(".")[0])
        task = self.tasks.create(spec)
        task = self.tasks.propose(task_id, {"summary": "Bound packet", "stage_ids": ["draft"]}, expected_revision=task["revision"])
        self.tasks.approve(task_id, proposal_hash=task["proposal_hash"], user_message_ref="chat:approved", approved_stage_ids=["draft"])

    def test_runner_keeps_the_fixed_ceiling_and_backend_config_wins(self):
        self.bound_task("bound-task", {"version": 1, "mode": "lossless", "max_packet_bytes": 2097152})
        with transaction(self.store.db) as db:
            record = read_record(db, "backend_config", "codex")
            write_record(db, "backend_config", "codex", {"executable": sys.executable}, expected_revision=record["revision"])
        bound = self.controller._runner("bound-task").config
        self.assertEqual((bound["max_packet_bytes"], bound["max_input_bytes"]), (2097152, 3145728))
        # Packets are sized for their role's model by the builder; the Runner keeps the fixed ceiling.
        default = self.controller._runner("approval-task").config
        self.assertEqual((default["max_packet_bytes"], default["max_input_bytes"]), (2097152, 3145728))
        with transaction(self.store.db) as db:
            record = read_record(db, "backend_config", "codex")
            write_record(db, "backend_config", "codex", {"executable": sys.executable, "max_packet_bytes": 1048576},
                         expected_revision=record["revision"])
        self.assertEqual(self.controller._runner("bound-task").config["max_packet_bytes"], 1048576)

    def test_packet_admission_uses_the_contract_bound_through_the_real_packet_service(self):
        parts = ("guide.md", "part1.md", "part2.md")
        seed = b"guide"
        for name in parts:  # incompressible unique text, about 420 KB per file
            chunks = []
            while sum(map(len, chunks)) < 420000:
                seed = hashlib.sha256(seed).digest()
                chunks.append(seed.hex() + "\n")
            (self.store.repo / name).write_text("".join(chunks), encoding="utf-8")
        self.bound_task("bound-task", {"version": 1, "mode": "lossless", "max_packet_bytes": 2097152}, parts=parts[1:])
        self.bound_task("plain-lossless", {"version": 1, "mode": "lossless"}, parts=parts[1:])
        packet = self.controller._packet("bound-task")
        self.assertEqual(packet["coverage"]["missing"], [])
        self.assertGreater(len(json.dumps(packet).encode()), 1048576)
        for name in parts:
            self.assertIn(name, packet["coverage"]["included"])
        for task_id in ("plain-lossless",):
            with self.subTest(task_id=task_id), self.assertRaisesRegex(ValueError, "byte limit"):
                self.controller._packet(task_id)

    def test_packet_reports_a_replaced_check_log_instead_of_its_new_text(self):
        folder = self.store.root / "tasks" / "approval-task" / "checks"
        folder.mkdir(parents=True)
        log = folder / "actual.log"
        raw = b"Ran 1 test in 0.001s\r\n\r\nFAILED (failures=1)\r\n"
        log.write_bytes(b"Ran 1 test in 0.001s\r\n\r\nOK\r\n")
        self.controller._save("checks", "approval-task", {"results": [{"id": "unit", "log": str(log), "log_sha256": hashlib.sha256(raw).hexdigest()}]})
        packet = self.controller._packet("approval-task")
        self.assertNotIn("_ase_context/logs/unit.txt", packet["coverage"]["included"])
        self.assertIn("Actual check log unavailable: unit", packet["coverage"]["missing"])

    def test_full_child_gate_revalidates_changed_parent(self):
        c = contract("child")
        c.update(parent_task_id="approval-task", parent_stage_id="draft")
        c["stages"] = c["stages"][:1]
        c["unknowns"] = []
        self.tasks.create(c)
        parent = self.tasks.snapshot("approval-task")
        self.tasks.propose("approval-task", {"summary": "Changed parent proposal", "stage_ids": ["draft"]}, expected_revision=parent["revision"])
        verdict = GateService(self.store).gate("child")
        self.assertTrue(any("Inherited parent approval invalid" in r for r in verdict["reasons"]))

    def test_resume_final_cycle_verifies_without_another_implementation(self):
        """Real protocol children deliberately cannot provide semantic approval."""
        for _ in range(3):
            self.controller.reserve_cycle("approval-task", "draft")
        task = self.tasks.snapshot("approval-task")
        (self.store.repo / "guide.md").write_text("draft")
        discovery = discover_capabilities(self.store.project, scope={"resources": ["guide.md"]}, backend_config={})
        record_discovery(self.store, task["task_id"], discovery, expected_revision=0)
        self.controller._save("cycle", "approval-task:draft", {"phase": "VERIFYING", "contract_hash": task["contract_hash"], "proposal_hash": task["proposal_hash"]})
        base = self.store.state_home
        trusted = base / "trusted"
        trusted.mkdir()
        fixture = base / "fixture.py"
        fixture.write_text(FIXTURE)
        artifacts = self.store.root / "tasks" / task["task_id"] / "sessions"
        runner = Runner({"executable": sys.executable, "executable_args": [str(fixture), "success", "codex"], "trusted_cwd": str(trusted), "protocol_only": True}, artifacts)
        result = WorkflowController(self.store, runner=runner).run(task["task_id"], resume=True)
        self.assertEqual(result["status"], "BLOCKED")
        roles = [json.loads(p.read_text())["role"] for p in artifacts.glob("*/request.json")]
        self.assertEqual(sorted(roles), ["ac", "review"])
        self.assertEqual(read_record(self.store.db, "budget", task["task_id"])["payload"]["task_cycles"], 3)
        self.assertEqual(self.tasks.snapshot(task["task_id"])["state"], "BLOCKED")

    def test_crashed_workflow_session_is_closed_as_interrupted(self):
        journal = Journal(self.store)
        journal.start_session("approval-task", "crashed-session")
        self.controller._save("active_workflow", "approval-task", {"task_id": "approval-task", "session_id": "crashed-session"})
        # Missing discovery blocks before any model can be launched.
        result = self.controller.run("approval-task", resume=True)
        self.assertEqual(result["status"], "BLOCKED")
        recovered = read_record(self.store.db, "session_closure", "crashed-session:recovered")["payload"]
        self.assertEqual(recovered["result"]["finish_reason"], "interrupted")
        self.assertEqual(journal.session("approval-task", "crashed-session")["payload"]["status"], "INTERRUPTED")

    def test_review_packet_proves_changed_and_immutable_files_from_real_baseline(self):
        repo = self.store.repo
        (repo / "guide.md").write_text("old content")
        (repo / "test_immutable.py").write_text("immutable test source")
        for args in (("init", "-b", "feature/evidence"), ("config", "user.email", "test@example.invalid"), ("config", "user.name", "Evidence test"), ("add", "."), ("commit", "-m", "baseline")):
            subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)
        c = contract("baseline-evidence")
        c["profile"] = "software"
        self.tasks.create(c)
        self.controller._save("baseline", "baseline-evidence", source_snapshot(self.store, c, task_id="baseline-evidence"))
        (repo / "guide.md").write_text("new content")
        packet = self.controller._packet("baseline-evidence")
        change_file = next(f for f in packet["files"] if f["path"] == "_ase_context/source_changes.json")
        changes = json.loads(change_file["content"])
        self.assertEqual([r["path"] for r in changes["changes"]], ["guide.md"])
        self.assertIn("test_immutable.py", changes["unchanged_paths"])
        self.assertEqual(changes["changes"][0]["before_sha256"], hashlib.sha256(b"old content").hexdigest())

    def test_software_baseline_is_recorded_at_approval_so_native_edits_reach_the_packet(self):
        self.assertIsNone(read_record(self.store.db, "approval_baseline", "approval-task")["payload"])
        repo = self.store.repo
        (repo / "guide.md").write_text("old content")
        for args in (("init", "-b", "feature/native"), ("config", "user.email", "test@example.invalid"), ("config", "user.name", "Native test"), ("add", "."), ("commit", "-m", "baseline")):
            subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)
        c = contract("native-edit")
        c["profile"] = "software"
        task = self.tasks.create(c)
        task = self.tasks.propose("native-edit", {"summary": "Change the guide", "stage_ids": ["draft"]}, expected_revision=task["revision"])
        self.tasks.approve("native-edit", proposal_hash=task["proposal_hash"], user_message_ref="chat:approved", approved_stage_ids=["draft"])
        approved = read_record(self.store.db, "approval_baseline", "native-edit")
        self.assertEqual(approved["payload"]["dirty"], "")
        (repo / "guide.md").write_text("native edit before task run")
        task = self.tasks.propose("native-edit", {"summary": "Change the guide again", "stage_ids": ["draft"]}, expected_revision=self.tasks.snapshot("native-edit")["revision"])
        self.tasks.approve("native-edit", proposal_hash=task["proposal_hash"], user_message_ref="chat:approved-again", approved_stage_ids=["draft"])
        self.assertEqual(read_record(self.store.db, "approval_baseline", "native-edit")["revision"], approved["revision"])
        # The step `task run` takes before any stage work.
        record_baseline(self.store, "native-edit", self.tasks.snapshot("native-edit")["contract"])
        self.assertEqual(read_record(self.store.db, "baseline", "native-edit")["payload"], approved["payload"])
        changes = json.loads(next(f for f in self.controller._packet("native-edit")["files"]
                                  if f["path"] == "_ase_context/source_changes.json")["content"])
        self.assertEqual([r["path"] for r in changes["changes"]], ["guide.md"])
        self.assertEqual(changes["changes"][0]["before_sha256"], hashlib.sha256(b"old content").hexdigest())

    def native_fix_before_first_run(self, task_id, *, dirty_baseline=False, crlf=False, capture_implementation=True):
        """approve -> native edit -> `task run` baseline -> first implementer capture of the edited source."""
        repo = self.store.repo
        old = "old line one\nold line two\n" if crlf else "old content"
        (repo / "guide.md").write_text(old)
        if crlf:
            (repo / ".gitattributes").write_text("guide.md text eol=crlf\n")
        for args in (("init", "-b", "feature/" + task_id), ("config", "user.email", "test@example.invalid"), ("config", "user.name", "Native test"), ("config", "commit.gpgsign", "false"), ("add", "."), ("commit", "-m", "baseline")):
            subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)
        if crlf:
            (repo / "guide.md").unlink()
            subprocess.run(["git", "-C", str(repo), "checkout", "--", "guide.md"], check=True, capture_output=True)
            self.assertIn(b"\r\n", (repo / "guide.md").read_bytes())
        if dirty_baseline:
            (repo / "guide.md").write_text("uncommitted content")
        c = contract(task_id)
        c["profile"] = "software"
        task = self.tasks.create(c)
        task = self.tasks.propose(task_id, {"summary": "Fix the guide", "stage_ids": ["draft"]}, expected_revision=task["revision"])
        self.tasks.approve(task_id, proposal_hash=task["proposal_hash"], user_message_ref="chat:approved", approved_stage_ids=["draft"])
        (repo / "guide.md").write_text("fixed content")
        if not capture_implementation:
            self.controller.check(task_id, "draft")
            self.assertIsNone(read_record(self.store.db, "baseline", task_id)["payload"])
            return self.controller._packet(task_id, "draft", role="review")
        record_baseline(self.store, task_id, self.tasks.snapshot(task_id)["contract"])
        session = self.store.root / "tasks" / task_id / "sessions" / "first"
        session.mkdir(parents=True)
        (session / "request.json").write_text(json.dumps({"source_packet": self.controller._packet(task_id, "draft")}))
        self.controller._save("baseline_implementation", task_id, {"artifact_root": str(session),
                              "artifacts": {"request": {"path": str(session / "request.json")}}})
        # Receipt integrity is Runner's concern; this exercises which bytes become _ase_before.
        with patch("ase.evidence.packets.verify_receipt"):
            return self.controller._packet(task_id, "draft")

    def test_native_fix_after_approval_keeps_the_pre_fix_bytes_as_before(self):
        packet = self.native_fix_before_first_run("native-fix")
        files = {f["path"]: f for f in packet["files"]}
        self.assertEqual(files["_ase_before/guide.md"]["content"], "old content")
        self.assertEqual(files["guide.md"]["content"], "fixed content")
        self.assertFalse(any("guide.md" in gap for gap in packet["coverage"]["missing"]), packet["coverage"]["missing"])

    def test_direct_review_can_recover_a_matching_before_body_without_a_worker_receipt(self):
        packet = self.native_fix_before_first_run("direct-review", capture_implementation=False)
        files = {row["path"]: row["content"] for row in packet["files"]}
        self.assertEqual(files.get("_ase_before/guide.md"), "old content")
        self.assertEqual(files["guide.md"], "fixed content")
        self.assertFalse(any("guide.md" in gap for gap in packet["coverage"]["missing"]))

    def test_native_fix_with_crlf_checkout_rebuilds_the_checked_out_bytes(self):
        packet = self.native_fix_before_first_run("native-crlf", crlf=True)
        files = {f["path"]: f for f in packet["files"]}
        self.assertFalse(any("guide.md" in gap for gap in packet["coverage"]["missing"]), packet["coverage"]["missing"])
        self.assertIn("old line two", files["_ase_before/guide.md"]["content"])

    def test_unverifiable_pre_fix_bytes_fail_closed_instead_of_showing_the_fix(self):
        packet = self.native_fix_before_first_run("native-dirty", dirty_baseline=True)
        self.assertNotIn("_ase_before/guide.md", {f["path"] for f in packet["files"]})
        self.assertTrue(any(gap.startswith("Baseline source unavailable for guide.md") for gap in packet["coverage"]["missing"]),
                        packet["coverage"]["missing"])

    def test_baseline_blob_read_accepts_only_a_commit_and_contained_path(self):
        from ase.inspection import _validate
        commit = "a" * 40
        _validate(["cat-file", "blob", commit + ":src/guide.md"])
        for spec in ("HEAD:guide.md", commit + ":../x", commit + ":.git/config", commit + ":", commit + ":/abs",
                     commit + ":a\\b", commit + ":a//b", "--batch", "-" + commit[1:] + ":x"):
            with self.subTest(spec=spec), self.assertRaisesRegex(ValueError, "Unsupported"):
                _validate(["cat-file", "blob", spec])
        with self.assertRaisesRegex(ValueError, "Unsupported"):
            _validate(["cat-file", "-p", commit + ":guide.md"])

    def test_inherited_child_approval_records_the_baseline_and_reports_a_failed_snapshot(self):
        repo = self.store.repo
        (repo / "guide.md").write_text("old content")
        for args in (("init", "-b", "feature/child"), ("config", "user.email", "test@example.invalid"), ("config", "user.name", "Child test"), ("add", "."), ("commit", "-m", "baseline")):
            subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)
        with patch("ase.tasks.has_software", return_value=True):
            c = contract("child-native")
            c.update(parent_task_id="approval-task", parent_stage_id="draft", unknowns=[])
            c["stages"] = c["stages"][:1]
            child = self.tasks.create(c)
            self.assertNotIn("baseline_warning", child)
            self.assertIsNotNone(read_record(self.store.db, "approval_baseline", "child-native")["payload"])
            failing = contract("child-failing")
            failing.update(parent_task_id="approval-task", parent_stage_id="draft", unknowns=[])
            failing["stages"] = failing["stages"][:1]
            with patch("ase.tasks.record_baseline", side_effect=subprocess.TimeoutExpired("git", 60)):
                result = self.tasks.create(failing)
        self.assertIn("make no native edits", result["baseline_warning"])

    def test_verified_completion_resolves_owned_blocker_and_keeps_history(self):
        """Gate double isolates journal projection, never semantic approval."""
        c = contract("report-recovery")
        c["unknowns"], c["stages"] = [], c["stages"][:1]
        task = self.tasks.create(c)
        task = self.tasks.propose(task["task_id"], {"summary": "Create the guide", "stage_ids": ["draft"]}, expected_revision=task["revision"])
        self.tasks.approve(task["task_id"], proposal_hash=task["proposal_hash"], user_message_ref="chat:approved", approved_stage_ids=["draft"])
        self.assertEqual(self.controller.run(task["task_id"])["status"], "BLOCKED")
        journal = Journal(self.store)
        before = journal.worklog("2026-01-01", "2030-01-01", timezone="UTC")
        self.assertTrue(any(b["task_id"] == task["task_id"] for b in before["current_blockers"]))
        plan = self.tasks.execution_plan(task["task_id"])
        gate = {"status": "APPROVED", "contract_hash": task["contract_hash"], "plan_hash": plan["plan_hash"]}
        with patch.object(self.controller, "_run", return_value=gate), patch("ase.gates.GateService.gate", return_value=gate):
            self.controller.run(task["task_id"], resume=True)
        report = journal.worklog("2026-01-01", "2030-01-01", timezone="UTC")
        self.assertFalse(any(b["task_id"] == task["task_id"] for b in report["current_blockers"]))
        self.assertEqual([e["kind"] for e in report["period_blockers"] if e["task_id"] == task["task_id"]], ["blocker_opened", "blocker_resolved"])

    def test_completed_resume_resolves_effectively_reopened_execution_blocker(self):
        """Gate double isolates corrected blocker projection, not semantic AC."""
        c = contract("corrected-blocker-recovery")
        c["unknowns"], c["stages"] = [], c["stages"][:1]
        task = self.tasks.create(c)
        task = self.tasks.propose(task["task_id"], {"summary": "Create guide", "stage_ids": ["draft"]}, expected_revision=task["revision"])
        self.tasks.approve(task["task_id"], proposal_hash=task["proposal_hash"], user_message_ref="chat:approved", approved_stage_ids=["draft"])
        self.assertEqual(self.controller.run(task["task_id"])["status"], "BLOCKED")
        journal = Journal(self.store)
        journal.append({"kind": "blocker_resolved", "task_id": task["task_id"], "blocker_id": "execution-blocker", "summary": "Incorrect resolution"}, event_key="wrong-resolution")
        journal.append({"kind": "correction", "task_id": task["task_id"], "corrects": "wrong-resolution", "summary": "Resolution was premature"}, event_key="withdraw-resolution")
        self.assertTrue(any(b['task_id'] == task['task_id'] for b in journal.worklog('2026-01-01', '2030-01-01', timezone='UTC')['current_blockers']))
        plan = self.tasks.execution_plan(task["task_id"])
        gate = {"status": "APPROVED", "contract_hash": task["contract_hash"], "plan_hash": plan["plan_hash"]}
        with patch.object(self.controller, "_run", return_value=gate), patch("ase.gates.GateService.gate", return_value=gate):
            self.controller.run(task["task_id"], resume=True)
        report = journal.worklog('2026-01-01', '2030-01-01', timezone='UTC')
        self.assertFalse(any(b['task_id'] == task['task_id'] for b in report['current_blockers']))


if __name__ == "__main__":
    unittest.main()
