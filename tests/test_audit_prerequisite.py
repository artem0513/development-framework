"""Real checks/effects with protocol-only historical scheduler seams.

The seeded historical APPROVED marker and explicitly named current scheduler
judgment seams are not independent semantic acceptance. The actual receipt gate
continues to reject these protocol fixtures.
"""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from ase.adapters import get_adapter
from ase.discovery import discover_capabilities, record_discovery
from ase.gates import GateService, source_snapshot, verification_context_hash
from ase.profiles import evaluate_contract_profiles
from ase.runner import Runner
from ase.state import read_record
from ase.store import Store
from ase.tasks import TaskService
from ase.workflow import AC_SCHEMA, EDIT_SCHEMA, REVIEW_SCHEMA, WorkflowController
from test_runner import FIXTURE


class PrerequisiteResumeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="ase-prerequisite-regression-")
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        work = self.base / "work"
        work.mkdir()
        self.store = Store(work, self.base / "state")
        self.addCleanup(self.store.close)
        self.tasks = TaskService(self.store)

    def fixture(self, *, independent=False, transitive=False, candidate_deletes_prepare=False, action_deletes_prepare=False, retry=False):
        names = ["prepare", "dependent"]
        if transitive:
            names.insert(1, "bridge")
        if independent:
            names.append("independent")
        resources = [name + ".txt" for name in names] + ["dependent.count", "prepare.count"]
        evidence = [{"id": name, "kind": "artifact", "path": name + ".txt"} for name in names]
        evidence.append({"id": "policy-check", "kind": "command", "argv": [sys.executable, "-c",
            "from pathlib import Path;import sys;sys.exit(0 if Path('policy.txt').read_text() == 'valid' else 1)"]})
        action = {"id": "dependent-effect", "argv": [sys.executable, "-c",
            "from pathlib import Path;p=Path('dependent.count');p.write_text(str(int(p.read_text())+1) if p.exists() else '1');Path('dependent.txt').write_text('dependent')"],
            "observation_argv": [sys.executable, "-c",
                "from pathlib import Path;import json;print(json.dumps({'exists':Path('dependent.count').exists(),'resource_id':'disposable-dependent'}))"]}
        stages = []
        acceptance = []
        for name in names:
            acceptance.append({"id": "AC-" + name, "expected": name + " is valid",
                "evidence_ids": [name, "policy-check"] if name == "prepare" else [name]})
            stage = {"id": name, "scope": {"resources": [name + ".txt"], "effects": ["file_write"]},
                "acceptance_ids": ["AC-" + name], "depends_on": []}
            if name == "prepare":
                stage["scope"]["resources"].append("prepare.count")
                stage["scope"]["effects"].append("command")
                stage["actions"] = [{"id": "must-not-replay-prepare", "argv": [sys.executable, "-c",
                    "from pathlib import Path;p=Path('prepare.count');p.write_text(p.read_text()+'x' if p.exists() else 'x')"],
                    "observation_argv": [sys.executable, "-c", "print('{\"exists\":true}')"]}]
            elif name == "dependent":
                stage["depends_on"] = ["bridge" if transitive else "prepare"]
                stage["scope"]["resources"].append("dependent.count")
                stage["scope"]["effects"].append("command")
                stage["actions"] = [action]
                if candidate_deletes_prepare or action_deletes_prepare:
                    stage["scope"]["resources"].append("prepare.txt")
                if action_deletes_prepare:
                    resources.append("first-action.count")
                    stage["scope"]["resources"].append("first-action.count")
                    stage["actions"].insert(0, {"id": "regresses-prerequisite", "argv": [sys.executable, "-c",
                        "from pathlib import Path;Path('prepare.txt').unlink();Path('first-action.count').write_text('1')"],
                        "observation_argv": [sys.executable, "-c",
                            "from pathlib import Path;import json;print(json.dumps({'exists':Path('first-action.count').exists(),'resource_id':'disposable-first-action'}))"]})
            elif name == "bridge":
                stage["depends_on"] = ["prepare"]
            stages.append(stage)
        spec = {"id": "prerequisite-resume", "schema_version": 2, "profile": "document",
            "request": "Establish valid prerequisites before executing the dependent effect.",
            "scope": {"resources": resources, "effects": ["file_write", "command"]},
            "acceptance": acceptance, "evidence": evidence, "stages": stages,
            "execution_policy": {"recovery_mode": "progress", "max_stage_cycles": 2 if retry else 1}}
        task = self.tasks.create(spec)
        task = self.tasks.propose(task["task_id"], {"summary": spec["request"], "stage_ids": names}, expected_revision=task["revision"])
        task = self.tasks.approve(task["task_id"], proposal_hash=task["proposal_hash"],
            user_message_ref="test:explicit-disposable-scope", approved_stage_ids=names)
        (self.store.repo / "policy.txt").write_text("valid")
        (self.store.repo / "prepare.txt").write_text("valid prerequisite")
        if independent:
            (self.store.repo / "independent.txt").write_text("independent work")
        if transitive:
            (self.store.repo / "bridge.txt").write_text("completed bridge")
        discovery = discover_capabilities(self.store.project,
            scope={"resources": resources, "dependencies": [{"path": "policy.txt", "from": "prepare.txt", "reason": "Governs the prerequisite"}]},
            backend_config={})
        record_discovery(self.store, task["task_id"], discovery, expected_revision=0)
        trusted = self.base / "fixture"
        trusted.mkdir()
        fixture = trusted / "provider.py"
        body = """
schema = json.loads(pathlib.Path(args[9]).read_text())
if 'edits' in schema['properties']:
 payload = {'edits':[],'summary':'No edits for the protocol scheduler fixture'}
elif 'criteria' in schema['properties']:
 payload = {'criteria': []}
else:
 payload = {'findings':[],'summary':'Protocol only; not independent semantic approval'}
"""
        if candidate_deletes_prepare:
            body = body.replace(" payload = {'edits':[],'summary':'No edits for the protocol scheduler fixture'}", """ payload = {'edits':[],'summary':'No edits for the protocol scheduler fixture'}
 request = json.loads(input_text.split('REQUEST_JSON:\\n',1)[1])
 if request.get('handoff',{}).get('stage',{}).get('id') == 'dependent':
  original = next(item for item in request['source_packet']['files'] if item['path'] == 'prepare.txt')
  payload = {'edits':[{'path':'prepare.txt','before_sha256':original['sha256'],'content':'','operation':'delete'}],
             'summary':'Approved-scope candidate accidentally removes prerequisite'}""")
        fixture.write_text(FIXTURE.replace("payload = {'verdict':'PASS'}", "payload = {'verdict':'PASS'}\n" + body))
        runner = Runner({"executable": sys.executable, "executable_args": [str(fixture), "success", "codex"],
            "trusted_cwd": str(trusted), "protocol_only": True}, self.store.root / "tasks" / task["task_id"] / "sessions")
        controller = WorkflowController(self.store, runner=runner)
        for stage in task["contract"]["stages"]:
            if stage["id"] not in {"prepare", "bridge"}:
                continue
            implementation = runner.run(get_adapter("codex"), controller._request(task["task_id"], "implementation", EDIT_SCHEMA,
                {"stage": stage}), timeout=3)
            controller._save("implementation", task["task_id"], implementation["receipt"])
            checks = controller.check(task["task_id"], stage["id"])
            self.assertEqual(evaluate_contract_profiles(task["contract"],
                [entry for entry in evidence if entry["id"] in ({"prepare", "policy-check"} if stage["id"] == "prepare" else {"bridge"})],
                checks["results"], stage_id=stage["id"])["status"], "PASS")
            # Real protocol receipts keep history authentic, but cannot approve.
            self.assertNotEqual(controller.verify(task["task_id"], stage["id"])["status"], "APPROVED")
            controller._capture_stage(task["task_id"], stage["id"], self.scheduler_only_approved(task, stage["id"]))
        return controller, task

    def scheduler_only_approved(self, task, stage_id="prepare"):
        """Explicit scheduler judgment seam, never independent semantic evidence."""
        return {"status": "APPROVED", "fingerprint": source_snapshot(self.store, task["contract"], task_id=task["task_id"])["fingerprint"],
            "blocked_reasons": [], "rejected_reasons": [],
            "verification_context_sha256": verification_context_hash(self.store, task["contract"], task["task_id"], stage_id)}

    def implementation_stages(self, task_id):
        return [request["handoff"]["stage"]["id"]
            for path in (self.store.root / "tasks" / task_id / "sessions").glob("*/request.json")
            if (request := json.loads(path.read_text()))["role"] == "implementation"]

    def dependent_file_candidate(self, controller):
        """Keep candidate receipt/payload real; request one disposable write."""
        fixture = Path(controller.runner.config["executable_args"][0])
        old = " payload = {'edits':[],'summary':'No edits for the protocol scheduler fixture'}"
        replacement = old + """
 request = json.loads(input_text.split('REQUEST_JSON:\\n',1)[1])
 if request.get('handoff',{}).get('stage',{}).get('id') == 'dependent':
  payload = {'edits':[{'path':'dependent.txt','before_sha256':'','content':'dependent candidate write','operation':'write'}],
             'summary':'Write the approved dependent artifact'}"""
        content = fixture.read_text()
        self.assertIn(old, content)
        fixture.write_text(content.replace(old, replacement))

    def test_deleted_prerequisite_blocks_dependent_effect_without_replaying_prepare(self):
        controller, task = self.fixture()
        (self.store.repo / "prepare.txt").unlink()
        result = controller.run(task["task_id"], resume=True)
        self.assertFalse((self.store.repo / "dependent.count").exists(), result)
        self.assertFalse((self.store.repo / "prepare.count").exists())
        self.assertEqual(self.implementation_stages(task["task_id"]), ["prepare"])
        self.assertIn("dependent", result["stage_blockers"])

    def test_changed_relevant_dependency_blocks_effect_but_runs_independent_stage(self):
        controller, task = self.fixture(independent=True)
        (self.store.repo / "policy.txt").write_text("regressed")
        result = controller.run(task["task_id"], resume=True)
        self.assertFalse((self.store.repo / "dependent.count").exists(), result)
        self.assertFalse((self.store.repo / "prepare.count").exists())
        self.assertCountEqual(self.implementation_stages(task["task_id"]), ["prepare", "independent"])
        self.assertIn("dependent", result["stage_blockers"])

    def test_transitive_completed_prerequisite_is_reassessed(self):
        controller, task = self.fixture(transitive=True)
        (self.store.repo / "prepare.txt").unlink()
        result = controller.run(task["task_id"], resume=True)
        self.assertFalse((self.store.repo / "dependent.count").exists(), result)
        self.assertFalse((self.store.repo / "prepare.count").exists())
        self.assertEqual(set(self.implementation_stages(task["task_id"])), {"prepare", "bridge"})

    def test_current_independent_judgment_is_required_even_when_artifact_check_passes(self):
        controller, task = self.fixture()
        result = controller.run(task["task_id"], resume=True)
        self.assertFalse((self.store.repo / "dependent.count").exists(), result)
        self.assertIn("dependent", result["stage_blockers"])
        self.assertTrue(any("Protocol fixtures" in reason for reason in result["blocked_reasons"]), result)

    def test_unrelated_change_allows_dependent_after_fresh_scheduler_only_judgment(self):
        controller, task = self.fixture()
        (self.store.repo / "unrelated.txt").write_text("unrelated change after prepare completed")
        checked = []
        real_verify = controller.verify
        def scheduler_only_current_judgment(task_id, stage_id=None):
            checked.append(stage_id)
            if stage_id == "prepare":
                return self.scheduler_only_approved(task)
            return real_verify(task_id, stage_id)
        with patch.object(controller, "verify", side_effect=scheduler_only_current_judgment):
            result = controller.run(task["task_id"], resume=True)
        self.assertEqual((self.store.repo / "dependent.count").read_text(), "1", result)
        self.assertFalse((self.store.repo / "prepare.count").exists())
        self.assertEqual(checked[0], "prepare")
        self.assertCountEqual(self.implementation_stages(task["task_id"]), ["prepare", "dependent"])
        # The real final gate remains fail-closed for protocol-only receipts.
        self.assertEqual(result["status"], "BLOCKED")
        self.assertNotEqual(GateService(self.store).gate(task["task_id"])["status"], "APPROVED")

    def test_repaired_prerequisite_can_resume_without_replaying_completed_mutation(self):
        controller, task = self.fixture()
        (self.store.repo / "prepare.txt").unlink()
        held = controller.run(task["task_id"], resume=True)
        self.assertFalse((self.store.repo / "dependent.count").exists(), held)
        self.assertEqual(read_record(self.store.db, "stage", task["task_id"] + ":prepare")["payload"]["state"], "COMPLETED")
        (self.store.repo / "prepare.txt").write_text("repaired prerequisite")
        real_verify = controller.verify
        def scheduler_only_current_judgment(task_id, stage_id=None):
            return self.scheduler_only_approved(task) if stage_id == "prepare" else real_verify(task_id, stage_id)
        with patch.object(controller, "verify", side_effect=scheduler_only_current_judgment):
            resumed = controller.run(task["task_id"], resume=True)
        self.assertEqual((self.store.repo / "dependent.count").read_text(), "1", resumed)
        self.assertFalse((self.store.repo / "prepare.count").exists())
        self.assertCountEqual(self.implementation_stages(task["task_id"]), ["prepare", "dependent"])

    def test_candidate_edit_regression_blocks_effect_after_current_prerequisite_judgment(self):
        controller, task = self.fixture(candidate_deletes_prepare=True, independent=True)
        real_verify = controller.verify
        def scheduler_only_current_judgment(task_id, stage_id=None):
            return self.scheduler_only_approved(task) if stage_id == "prepare" else real_verify(task_id, stage_id)
        with patch.object(controller, "verify", side_effect=scheduler_only_current_judgment):
            result = controller.run(task["task_id"], resume=True)
        self.assertFalse((self.store.repo / "prepare.txt").exists())
        self.assertFalse((self.store.repo / "dependent.count").exists(), result)
        self.assertFalse((self.store.repo / "prepare.count").exists())
        self.assertCountEqual(self.implementation_stages(task["task_id"]), ["prepare", "dependent", "independent"])
        self.assertTrue(any("Artifact missing or altered: prepare" in reason for reason in result["blocked_reasons"]), result)

    def test_earlier_action_regression_blocks_later_effect_in_same_stage(self):
        controller, task = self.fixture(action_deletes_prepare=True)
        real_verify = controller.verify
        def scheduler_only_current_judgment(task_id, stage_id=None):
            return self.scheduler_only_approved(task) if stage_id == "prepare" else real_verify(task_id, stage_id)
        with patch.object(controller, "verify", side_effect=scheduler_only_current_judgment):
            result = controller.run(task["task_id"], resume=True)
        self.assertEqual((self.store.repo / "first-action.count").read_text(), "1", result)
        self.assertFalse((self.store.repo / "prepare.txt").exists())
        self.assertFalse((self.store.repo / "dependent.count").exists(), result)
        self.assertFalse((self.store.repo / "prepare.count").exists())

    def test_corrective_iteration_rechecks_prerequisite_before_new_candidate(self):
        controller, task = self.fixture(retry=True)
        real_verify = controller.verify
        def scheduler_only_rejected_dependent(task_id, stage_id=None):
            if stage_id == "prepare":
                return self.scheduler_only_approved(task)
            if stage_id == "dependent":
                # A real approved-scope artifact deletion at the decision seam
                # models a prerequisite changing before the corrective loop.
                (self.store.repo / "prepare.txt").unlink(missing_ok=True)
                return {"status": "REJECTED", "blocked_reasons": [], "rejected_reasons": ["Dependent still requires correction"]}
            return real_verify(task_id, stage_id)
        with patch.object(controller, "verify", side_effect=scheduler_only_rejected_dependent):
            result = controller.run(task["task_id"], resume=True)
        self.assertEqual((self.store.repo / "dependent.count").read_text(), "1", result)
        self.assertFalse((self.store.repo / "prepare.count").exists())
        self.assertCountEqual(self.implementation_stages(task["task_id"]), ["prepare", "dependent"])
        budget = read_record(self.store.db, "budget", task["task_id"])["payload"]
        self.assertEqual(budget["stages"][task["task_id"] + ":dependent"], 1)

    def test_prerequisite_drift_during_candidate_blocks_dependent_file_write(self):
        controller, task = self.fixture(independent=True)
        self.dependent_file_candidate(controller)
        actual_run, actual_verify = controller.runner.run, controller.verify
        def candidate_then_drift(adapter, request, **kwargs):
            outcome = actual_run(adapter, request, **kwargs)
            if request["role"] == "implementation" and request.get("handoff", {}).get("stage", {}).get("id") == "dependent":
                self.assertTrue((self.store.repo / "prepare.txt").exists())
                (self.store.repo / "prepare.txt").unlink()
            return outcome
        def scheduler_only_current_judgment(task_id, stage_id=None):
            return self.scheduler_only_approved(task) if stage_id == "prepare" else actual_verify(task_id, stage_id)
        with patch.object(controller.runner, "run", side_effect=candidate_then_drift), patch.object(controller, "verify", side_effect=scheduler_only_current_judgment):
            result = controller.run(task["task_id"], resume=True)
        self.assertFalse((self.store.repo / "dependent.txt").exists(), result)
        self.assertFalse((self.store.repo / "dependent.count").exists(), result)
        self.assertFalse((self.store.repo / "prepare.count").exists())
        self.assertCountEqual(self.implementation_stages(task["task_id"]), ["prepare", "dependent", "independent"])

    def test_prerequisite_binding_is_checked_inside_final_file_write_guard(self):
        controller, task = self.fixture()
        self.dependent_file_candidate(controller)
        actual_authority, actual_verify = controller._stage_authority, controller.verify
        drifted = []
        def authority_then_drift(current, stage):
            authority = actual_authority(current, stage)
            if stage["id"] == "dependent" and self.store.db.in_transaction:
                drifted.append(True)
                (self.store.repo / "prepare.txt").unlink()
            return authority
        def scheduler_only_current_judgment(task_id, stage_id=None):
            return self.scheduler_only_approved(task) if stage_id == "prepare" else actual_verify(task_id, stage_id)
        with patch.object(controller, "_stage_authority", side_effect=authority_then_drift), patch.object(controller, "verify", side_effect=scheduler_only_current_judgment):
            result = controller.run(task["task_id"], resume=True)
        self.assertEqual(drifted, [True])
        self.assertFalse((self.store.repo / "dependent.txt").exists(), result)
        self.assertFalse((self.store.repo / "dependent.count").exists(), result)
        self.assertFalse((self.store.repo / "prepare.count").exists())

    def test_action_binding_comes_from_verified_observation_before_verifier_return_drift(self):
        controller, task = self.fixture()
        stage = next(stage for stage in task["contract"]["stages"] if stage["id"] == "dependent")
        actual_verify = controller.verify
        def scheduler_only_judgment_then_drift(task_id, stage_id=None):
            if stage_id == "prepare":
                verdict = self.scheduler_only_approved(task)
                (self.store.repo / "prepare.txt").unlink()
                return verdict
            return actual_verify(task_id, stage_id)
        with patch.object(controller, "verify", side_effect=scheduler_only_judgment_then_drift):
            with self.assertRaisesRegex(ValueError, "Prerequisite|prerequisite"):
                controller.execute_actions(task["task_id"], stage)
        self.assertFalse((self.store.repo / "dependent.count").exists())
        self.assertFalse((self.store.repo / "prepare.count").exists())

    def test_dependent_effect_recipe_cannot_launch_without_prerequisite_binding(self):
        controller, task = self.fixture()
        stage = next(stage for stage in task["contract"]["stages"] if stage["id"] == "dependent")
        action = stage["actions"][0]
        for argv in (action["argv"], tuple(action["argv"])):
            with self.subTest(argv_type=type(argv).__name__), self.assertRaisesRegex(ValueError, "Prerequisite|prerequisite"):
                controller._execute_command(task["task_id"], action, argv, self.base / "missing-binding.log", 3, stage["id"])
        self.assertFalse((self.store.repo / "dependent.count").exists())

    def test_owned_effect_can_change_bound_outputs_after_process_launch(self):
        from ase.core import execute
        actual_create = self.tasks.create
        def declare_slow_effect(spec):
            spec = copy.deepcopy(spec)
            stage = next(stage for stage in spec["stages"] if stage["id"] == "dependent")
            stage["actions"][0]["argv"][-1] += ";import time;time.sleep(0.25)"
            return actual_create(spec)
        with patch.object(self.tasks, "create", side_effect=declare_slow_effect):
            controller, task = self.fixture()
        stage = next(stage for stage in task["contract"]["stages"] if stage["id"] == "dependent")
        actual_verify, submissions = controller.verify, []
        def capture_execution(argv, *args, **kwargs):
            result = execute(argv, *args, **kwargs)
            if list(argv) == stage["actions"][0]["argv"]:
                submissions.append(result)
            return result
        def scheduler_only_current_judgment(task_id, stage_id=None):
            return self.scheduler_only_approved(task) if stage_id == "prepare" else actual_verify(task_id, stage_id)
        with patch.object(controller, "verify", side_effect=scheduler_only_current_judgment), patch("ase.core.execute", side_effect=capture_execution):
            observed = controller.execute_actions(task["task_id"], stage)
        self.assertTrue(observed[0]["exists"])
        self.assertEqual(len(submissions), 1)
        self.assertEqual(submissions[0]["exit_code"], 0)
        self.assertFalse(submissions[0]["cancelled"])
        self.assertIsNone(submissions[0]["error"])
        self.assertEqual((self.store.repo / "dependent.count").read_text(), "1")

    @unittest.skipUnless(os.name == "nt", "requires actual Windows DPAPI")
    def test_action_prerequisite_binding_is_checked_after_actual_credential_resolution(self):
        from ase.access import CredentialAccess
        from ase.credentials import CredentialVault
        target = "https://prerequisite-fixture.example"
        access = {"credential_ref": "prerequisite-fixture", "target": target}
        actual_create = self.tasks.create
        def declare_authenticated_effect(spec):
            spec = copy.deepcopy(spec)
            spec["scope"]["resources"].append(target)
            dependent = next(stage for stage in spec["stages"] if stage["id"] == "dependent")
            dependent["scope"]["resources"].append(target)
            dependent["actions"][0]["access"] = access
            return actual_create(spec)
        with patch.object(self.tasks, "create", side_effect=declare_authenticated_effect):
            controller, task = self.fixture()
        credential_file = self.base / "disposable-credential.json"
        credential_file.write_text(json.dumps({"schema_version": 1, "credentials": [{
            "id": access["credential_ref"], "target": target, "kind": "environment",
            "secrets": {"token": "disposable-prerequisite-fixture-token"}, "env": {"ASE_PREREQUISITE_FIXTURE_TOKEN": "token"}}]}))
        CredentialVault(self.store.state_home).import_file(credential_file)
        stage = next(stage for stage in task["contract"]["stages"] if stage["id"] == "dependent")
        action = stage["actions"][0]
        actual_ensure, actual_verify, actual_popen = CredentialAccess._ensure, controller.verify, subprocess.Popen
        resolutions, effects = [], []
        def resolve_then_drift(client, *args, **kwargs):
            resolved = actual_ensure(client, *args, **kwargs)
            resolutions.append(True)
            if len(resolutions) == 2:  # First resolution observes; second prepares the effect.
                self.assertFalse(self.store.db.in_transaction)
                (self.store.repo / "prepare.txt").unlink()
            return resolved
        def observe_process(argv, *args, **kwargs):
            if list(argv) == action["argv"]:
                effects.append(True)
            return actual_popen(argv, *args, **kwargs)
        def scheduler_only_current_judgment(task_id, stage_id=None):
            return self.scheduler_only_approved(task) if stage_id == "prepare" else actual_verify(task_id, stage_id)
        with patch.object(CredentialAccess, "_ensure", resolve_then_drift), patch.object(controller, "verify", side_effect=scheduler_only_current_judgment), patch("ase.core.subprocess.Popen", side_effect=observe_process):
            with self.assertRaisesRegex(ValueError, "Prerequisite|prerequisite"):
                controller.execute_actions(task["task_id"], stage)
        self.assertEqual(resolutions, [True, True])
        self.assertEqual(effects, [], "The effect process was created before detecting stale prerequisite proof")
        self.assertFalse((self.store.repo / "dependent.count").exists())
        self.assertFalse((self.store.repo / "prepare.count").exists())


    def external_observation_guard(self, *, inherited=False, return_drift=False, unrelated=False):
        """Actual observations/process guard; judgment remains a scheduler seam."""
        external = self.base / "external-identity"
        external.write_text("original-resource")
        actual_create = self.tasks.create
        def declare_observer(spec):
            spec = copy.deepcopy(spec)
            for stage in spec["stages"]:
                if stage["id"] == "prepare":
                    stage["actions"][0]["observation_argv"] = [sys.executable, "-c",
                        "from pathlib import Path;import json;print(json.dumps({'exists':True,'resource_id':Path(" + repr(str(external)) + ").read_text()}))"]
                if unrelated and stage["id"] == "independent":
                    stage["scope"]["effects"].append("command")
                    stage["actions"] = [{"id": "unrelated-effect", "argv": [sys.executable, "-c", "raise AssertionError('must not replay')"],
                        "observation_argv": [sys.executable, "-c", "print('{\"exists\":true,\"resource_id\":\"unrelated\"}')"]}]
            return actual_create(spec)
        with patch.object(self.tasks, "create", side_effect=declare_observer):
            if inherited:
                controller, parent, task = self.inherited_fixture(depth=2, transitive=True)
            else:
                controller, task = self.fixture(independent=unrelated)
                parent = task
        prepare = next(s for s in parent["contract"]["stages"] if s["id"] == "prepare")
        stage = next(s for s in task["contract"]["stages"] if s["id"] == "dependent")
        controller.execute_actions(parent["task_id"], prepare)
        from ase.operations import OperationLedger
        from ase.state import hash_value
        ledger = OperationLedger(self.store)
        observed_stage = prepare
        if unrelated:
            observed_stage = next(s for s in parent["contract"]["stages"] if s["id"] == "independent")
        operation_id = hash_value({"task": parent["task_id"], "stage": observed_stage["id"],
            "action": observed_stage["actions"][0]["id"]})
        external.write_text("different-resource")
        controller.execute_actions(parent["task_id"], observed_stage)
        changed_observation = read_record(self.store.db, "operation", operation_id)["payload"]["observation"]
        external.write_text("original-resource")
        controller.execute_actions(parent["task_id"], observed_stage)
        actual_verify = controller.verify
        changed = []
        def change_observation():
            changed.append(True)
            external.write_text("different-resource")
            # Reconcile an actual captured observer proof; never start a nested
            # executor call inside the final-launch ownership interval.
            ledger.reconcile(operation_id, changed_observation)
        def scheduler_only_verified_context(task_id, stage_id=None):
            if task_id == parent["task_id"] and stage_id in {"prepare", "bridge"}:
                verdict = self.scheduler_only_approved(parent, stage_id)
                if return_drift and stage_id == "prepare":
                    change_observation()
                return verdict
            return actual_verify(task_id, stage_id)
        from ase.core import execute
        def drift_at_launch(argv, *args, **kwargs):
            if list(argv) == stage["actions"][0]["argv"]:
                change_observation()
            return execute(argv, *args, **kwargs)
        with patch.object(controller, "verify", side_effect=scheduler_only_verified_context), patch("ase.core.execute", side_effect=drift_at_launch):
            if unrelated:
                controller.execute_actions(task["task_id"], stage)
                self.assertEqual((self.store.repo / "dependent.count").read_text(), "1")
            else:
                with self.assertRaisesRegex(ValueError, "Prerequisite|prerequisite"):
                    controller.execute_actions(task["task_id"], stage)
                self.assertFalse((self.store.repo / "dependent.count").exists())
        self.assertTrue(changed)
        self.assertFalse((self.store.repo / "prepare.count").exists())

    def test_action_observation_drift_at_final_guard_blocks_dependent(self):
        self.external_observation_guard()

    def test_ancestor_action_observation_drift_at_final_guard_blocks_dependent(self):
        self.external_observation_guard(inherited=True)

    def test_action_observation_drift_after_verifier_return_blocks_dependent(self):
        self.external_observation_guard(return_drift=True)

    def test_unrelated_action_observation_preserves_dependent_binding(self):
        self.external_observation_guard(unrelated=True)

    def inherited_fixture(self, *, depth=1, transitive=False):
        """Real parent/child records and effects; parent history is protocol-only."""
        controller, parent = self.fixture(transitive=transitive)
        current = parent
        for number in range(depth):
            stage = next(s for s in current['contract']['stages'] if s['id'] == 'dependent')
            child_id = 'child-' + str(number)
            selected = [a for a in current['contract']['acceptance'] if a['id'] in stage['acceptance_ids']]
            child = {'id': child_id, 'schema_version': 2, 'profile': 'document',
                'request': 'Implement the approved dependent stage without dropping its prerequisites',
                'scope': copy.deepcopy(stage['scope']), 'parent_task_id': current['task_id'],
                'parent_stage_id': 'dependent', 'acceptance': copy.deepcopy(selected),
                'evidence': [copy.deepcopy(e) for e in current['contract']['evidence']
                             if e['id'] in {ref for ac in selected for ref in ac['evidence_ids']}],
                'stages': [{'id': 'dependent', 'acceptance_ids': stage['acceptance_ids'],
                            'actions': copy.deepcopy(stage['actions'])}]}
            current = self.tasks.create(child)
            record_discovery(self.store, child_id, discover_capabilities(self.store.project,
                scope={'resources': child['scope']['resources']}, backend_config={}), expected_revision=0)
        runner = Runner(copy.deepcopy(controller.runner.config), self.store.root / 'tasks' / current['task_id'] / 'sessions')
        return WorkflowController(self.store, runner=runner), parent, current

    def test_inherited_missing_parent_prerequisite_holds_real_effect(self):
        controller, parent, child = self.inherited_fixture()
        controller._save('stage', parent['task_id'] + ':prepare', None)
        with self.assertRaisesRegex(ValueError, 'Prerequisite|prerequisite'):
            controller.execute_actions(child['task_id'], child['contract']['stages'][0])
        self.assertFalse((self.store.repo / 'dependent.count').exists())
        self.assertFalse((self.store.repo / 'prepare.count').exists())

    def test_inherited_prerequisite_staleness_after_child_creation_holds_real_effect(self):
        controller, parent, child = self.inherited_fixture()
        (self.store.repo / 'prepare.txt').unlink()
        with self.assertRaisesRegex(ValueError, 'Prerequisite|prerequisite'):
            controller.execute_actions(child['task_id'], child['contract']['stages'][0])
        self.assertFalse((self.store.repo / 'dependent.count').exists())
        self.assertFalse((self.store.repo / 'prepare.count').exists())

    def test_inherited_deep_ancestor_transitive_prerequisite_holds_real_effect(self):
        controller, parent, child = self.inherited_fixture(depth=2, transitive=True)
        controller._save('stage', parent['task_id'] + ':prepare', None)
        with self.assertRaisesRegex(ValueError, 'Prerequisite|prerequisite'):
            controller.execute_actions(child['task_id'], child['contract']['stages'][0])
        self.assertFalse((self.store.repo / 'dependent.count').exists())
        self.assertFalse((self.store.repo / 'prepare.count').exists())

    def test_inherited_valid_prerequisite_allows_effect_without_completing_mapped_parent(self):
        controller, parent, child = self.inherited_fixture(depth=2, transitive=True)
        actual_verify = controller.verify
        def scheduler_only_ancestor_judgment(task_id, stage_id=None):
            if task_id == parent['task_id'] and stage_id in {'prepare', 'bridge'}:
                return self.scheduler_only_approved(parent, stage_id)
            return actual_verify(task_id, stage_id)
        with patch.object(controller, 'verify', side_effect=scheduler_only_ancestor_judgment):
            observed = controller.execute_actions(child['task_id'], child['contract']['stages'][0])
        self.assertTrue(observed[0]['exists'])
        self.assertEqual((self.store.repo / 'dependent.count').read_text(), '1')
        self.assertFalse((self.store.repo / 'prepare.count').exists())
        self.assertIsNone(read_record(self.store.db, 'stage', parent['task_id'] + ':dependent')['payload'])
        self.assertNotEqual(GateService(self.store).gate(child['task_id'])['status'], 'APPROVED')

    def test_inherited_proof_drift_after_verifier_return_holds_real_effect(self):
        controller, parent, child = self.inherited_fixture()
        actual_verify = controller.verify
        def scheduler_only_ancestor_judgment_then_drift(task_id, stage_id=None):
            if task_id == parent['task_id'] and stage_id == 'prepare':
                verdict = self.scheduler_only_approved(parent)
                (self.store.repo / 'prepare.txt').unlink()
                return verdict
            return actual_verify(task_id, stage_id)
        with patch.object(controller, 'verify', side_effect=scheduler_only_ancestor_judgment_then_drift):
            with self.assertRaisesRegex(ValueError, 'Prerequisite|prerequisite'):
                controller.execute_actions(child['task_id'], child['contract']['stages'][0])
        self.assertFalse((self.store.repo / 'dependent.count').exists())

    def test_inherited_proof_drift_at_final_process_guard_holds_real_effect(self):
        from ase.core import execute
        controller, parent, child = self.inherited_fixture()
        stage = child['contract']['stages'][0]
        actual_verify = controller.verify
        def scheduler_only_ancestor_judgment(task_id, stage_id=None):
            return self.scheduler_only_approved(parent) if task_id == parent['task_id'] and stage_id == 'prepare' else actual_verify(task_id, stage_id)
        def drift_before_process(argv, *args, **kwargs):
            if list(argv) == stage['actions'][0]['argv']:
                (self.store.repo / 'prepare.txt').unlink()
            return execute(argv, *args, **kwargs)
        with patch.object(controller, 'verify', side_effect=scheduler_only_ancestor_judgment), patch('ase.core.execute', side_effect=drift_before_process):
            with self.assertRaisesRegex(ValueError, 'Prerequisite|prerequisite'):
                controller.execute_actions(child['task_id'], stage)
        self.assertFalse((self.store.repo / 'dependent.count').exists())

    def test_inherited_direct_effect_recipe_cannot_omit_parent_prerequisite_binding(self):
        controller, parent, child = self.inherited_fixture()
        stage = child['contract']['stages'][0]
        action = stage['actions'][0]
        with self.assertRaisesRegex(ValueError, 'Prerequisite|prerequisite'):
            controller._execute_command(child['task_id'], action, action['argv'], self.base / 'unbound-child.log', 3, stage['id'])
        self.assertFalse((self.store.repo / 'dependent.count').exists())

    def test_inherited_stale_parent_holds_child_candidate_file_write(self):
        controller, parent, child = self.inherited_fixture()
        self.dependent_file_candidate(controller)
        (self.store.repo / 'prepare.txt').unlink()
        result = controller.run(child['task_id'])
        self.assertFalse((self.store.repo / 'dependent.txt').exists(), result)
        self.assertFalse((self.store.repo / 'dependent.count').exists(), result)
        self.assertFalse((self.store.repo / 'prepare.count').exists())

    def test_inherited_parent_binding_is_checked_in_final_candidate_write_guard(self):
        controller, parent, child = self.inherited_fixture()
        self.dependent_file_candidate(controller)
        actual_authority, actual_verify = controller._stage_authority, controller.verify
        def scheduler_only_ancestor_judgment(task_id, stage_id=None):
            return self.scheduler_only_approved(parent) if task_id == parent['task_id'] and stage_id == 'prepare' else actual_verify(task_id, stage_id)
        def authority_then_drift(current, stage):
            authority = actual_authority(current, stage)
            if current['task_id'] == child['task_id'] and self.store.db.in_transaction:
                (self.store.repo / 'prepare.txt').unlink(missing_ok=True)
            return authority
        with patch.object(controller, 'verify', side_effect=scheduler_only_ancestor_judgment), patch.object(controller, '_stage_authority', side_effect=authority_then_drift):
            result = controller.run(child['task_id'])
        self.assertFalse((self.store.repo / 'dependent.txt').exists(), result)
        self.assertFalse((self.store.repo / 'dependent.count').exists(), result)

    def test_inherited_missing_parent_prerequisite_holds_native_delegation(self):
        from ase.delegations import DelegationRegistry
        controller, parent, child = self.inherited_fixture()
        controller._save('stage', parent['task_id'] + ':prepare', None)
        with self.assertRaisesRegex(ValueError, 'Prerequisite|prerequisite'):
            DelegationRegistry(self.store).register(child['task_id'], {'id': 'inspect-child', 'stage_id': 'dependent',
                'goal': 'Inspect the dependent implementation', 'expected_result': 'Bounded source review',
                'resources': ['dependent.txt']})
        self.assertFalse((self.store.repo / 'dependent.count').exists())


    def test_actual_recipe_rejects_changed_action_argv_before_effect(self):
        controller, parent, child = self.inherited_fixture()
        stage = child['contract']['stages'][0]
        action = stage['actions'][0]
        changed = [*action['argv'][:-1], "from pathlib import Path;Path('undeclared.txt').write_text('wrong')"]
        for argv in (changed, tuple(changed)):
            with self.subTest(type=type(argv).__name__):
                with self.assertRaisesRegex(ValueError, 'argv|recipe|command'):
                    controller._execute_command(child['task_id'], action, argv, self.base/'wrong-action.log', 3, stage['id'])
                self.assertFalse((self.store.repo/'undeclared.txt').exists())

    def test_actual_recipe_rejects_changed_check_argv_before_effect(self):
        controller, task = self.fixture()
        stage = task['contract']['stages'][0]
        check = next(spec for spec in task['contract']['evidence'] if spec['id'] == 'policy-check')
        changed = [sys.executable, '-c', "from pathlib import Path;Path('undeclared.txt').write_text('wrong')"]
        with self.assertRaisesRegex(ValueError, 'argv|recipe|command'):
            controller._execute_command(task['task_id'], check, changed, self.base/'wrong-check.log', 3, stage['id'])
        self.assertFalse((self.store.repo/'undeclared.txt').exists())

    def test_actual_recipe_keeps_declared_observation_without_mutation_binding(self):
        controller, parent, child = self.inherited_fixture()
        stage = child['contract']['stages'][0]
        action = stage['actions'][0]
        result = controller._execute_command(child['task_id'], action, tuple(action['observation_argv']), self.base/'actual-observer.log', 3, stage['id'])
        self.assertEqual(result['exit_code'], 0, result)
        self.assertFalse((self.store.repo/'dependent.count').exists())
        self.assertFalse(json.loads(Path(result['log']).read_text())['exists'])

    def test_actual_recipe_keeps_declared_check_without_mutation_binding(self):
        controller, task = self.fixture()
        stage = task['contract']['stages'][0]
        check = next(spec for spec in task['contract']['evidence'] if spec['id'] == 'policy-check')
        result = controller._execute_command(task['task_id'], check, tuple(check['argv']), self.base/'actual-check.log', 3, stage['id'])
        self.assertEqual(result['exit_code'], 0, result)
        self.assertFalse((self.store.repo/'dependent.count').exists())


class PrerequisiteDelegationClosureTests(unittest.TestCase):
    """Real gate/registry/writers; protocol-kind seam is never semantic AC."""
    def setUp(self):
        from test_gate_regression import VerificationContextTests
        self.fx = VerificationContextTests()
        self.fx.setUp()
        self.addCleanup(self.fx.doCleanups)
        self.store, self.tasks = self.fx.store, self.fx.tasks
        from ase.runner import verify_receipt
        def protocol_kind_seam(receipt, **kwargs):
            verified = verify_receipt(receipt, **kwargs)
            self.assertEqual(verified['evidence_kind'], 'protocol_only')
            return {**verified, 'evidence_kind': 'independent_cli'}
        semantic = patch('ase.gates.verify_receipt', side_effect=protocol_kind_seam)
        semantic.start()
        self.addCleanup(semantic.stop)

    def fixture(self, *, inherited=False, transitive=False):
        contract = copy.deepcopy(self.fx.task['contract'])
        contract['id'] = 'prerequisite-closure'
        contract['scope']['resources'] += ['dependent.count', 'dependent.txt']
        for stage in contract['stages']:
            stage['scope'] = copy.deepcopy(contract['scope'])
        dependent = contract['stages'][1]
        dependent['depends_on'] = ['bridge' if transitive else 'a']
        dependent['actions'] = [{'id': 'dependent-effect', 'argv': [sys.executable, '-c',
            "from pathlib import Path;p=Path('dependent.count');p.write_text(str(int(p.read_text())+1) if p.exists() else '1')"],
            'observation_argv': [sys.executable, '-c',
                "from pathlib import Path;import json;print(json.dumps({'exists':Path('dependent.count').exists(),'resource_id':'disposable-dependent'}))"]}]
        if transitive:
            contract['stages'].insert(1, {'id': 'bridge', 'scope': copy.deepcopy(contract['scope']),
                'acceptance_ids': ['A'], 'depends_on': ['a']})
        contract['stages'].append({'id': 'sibling', 'scope': copy.deepcopy(contract['scope']),
            'acceptance_ids': [], 'depends_on': []})
        parent = self.tasks.create(contract)
        parent = self.tasks.propose(parent['task_id'], {'summary': contract['request'],
            'stage_ids': [stage['id'] for stage in contract['stages']]}, expected_revision=parent['revision'])
        parent = self.tasks.approve(parent['task_id'], proposal_hash=parent['proposal_hash'],
            user_message_ref='test:disposable-prerequisite-closure', approved_stage_ids=parent['proposal']['stage_ids'])
        record_discovery(self.store, parent['task_id'], discover_capabilities(self.store.project,
            scope={'resources': contract['scope']['resources']}, backend_config={}), expected_revision=0)
        runner = Runner(copy.deepcopy(self.fx.runner.config), self.store.root / 'tasks' / parent['task_id'] / 'sessions')
        controller = WorkflowController(self.store, runner=runner)
        for stage in parent['contract']['stages']:
            if stage['id'] not in {'a', 'bridge'}:
                continue
            receipt = runner.run(get_adapter('codex'), controller._request(parent['task_id'], 'implementation',
                EDIT_SCHEMA, {'stage': stage}), timeout=5)['receipt']
            controller._save('implementation', parent['task_id'], receipt)
            controller._save('stage_implementation', parent['task_id'] + ':' + stage['id'], receipt)
            controller.check(parent['task_id'], stage['id'])
            verdict = controller.verify(parent['task_id'], stage['id'])
            self.assertEqual(verdict['status'], 'APPROVED', verdict)
            controller._capture_stage(parent['task_id'], stage['id'], verdict)
        task = parent
        if inherited:
            child = {'id': 'closure-child', 'schema_version': 2, 'profile': 'document',
                'request': 'Implement the mapped dependent stage', 'scope': copy.deepcopy(contract['scope']),
                'parent_task_id': parent['task_id'], 'parent_stage_id': 'b',
                'acceptance': copy.deepcopy(contract['acceptance']), 'evidence': copy.deepcopy(contract['evidence']),
                'stages': [{'id': 'b', 'acceptance_ids': ['B'], 'actions': copy.deepcopy(dependent['actions'])}]}
            task = self.tasks.create(child)
            record_discovery(self.store, task['task_id'], discover_capabilities(self.store.project,
                scope={'resources': child['scope']['resources']}, backend_config={}), expected_revision=0)
            runner = Runner(copy.deepcopy(runner.config), self.store.root / 'tasks' / task['task_id'] / 'sessions')
            controller = WorkflowController(self.store, runner=runner)
        stage = next(item for item in task['contract']['stages'] if item['id'] == 'b')
        return controller, parent, task, stage

    def dispatch(self, parent, stage_id='a', *, settle=False):
        from ase.delegations import DelegationRegistry
        registry = DelegationRegistry(self.store)
        registered = registry.register(parent['task_id'], {'id': 'late-review', 'stage_id': stage_id,
            'goal': 'Review the captured prerequisite', 'expected_result': 'Bounded advisory findings',
            'resources': ['proof.txt']})
        ticket = registry.dispatch(parent['task_id'], 'late-review', expected_revision=registered['revision'],
            coordinator='test:closure', backend='codex')
        if settle:
            attempt = ticket['payload']['attempt']['id']
            result = registry.result(parent['task_id'], 'late-review', attempt, {'attempt_id': attempt,
                'agent_id': 'disposable-native', 'status': 'COMPLETE', 'summary': 'Captured advisory findings',
                'artifacts': []}, expected_revision=ticket['revision'])
            registry.accept(parent['task_id'], 'late-review', attempt, expected_revision=result['revision'],
                coordinator='test:closure', summary='Compared the advisory with its original context')
        return registry

    def late_effect_work(self, *, inherited=False, transitive=False, sibling=False):
        from ase.core import execute
        controller, parent, task, stage = self.fixture(inherited=inherited, transitive=transitive)
        dispatched = []
        def dispatch_before_process(argv, *args, **kwargs):
            if list(argv) == stage['actions'][0]['argv'] and not dispatched:
                before = GateService(self.store).gate(parent['task_id'], stage_id='bridge' if transitive else 'a')
                self.assertEqual(before['status'], 'APPROVED', before)
                self.dispatch(parent, 'sibling' if sibling else 'a')
                dispatched.append(True)
                after = GateService(self.store).gate(parent['task_id'], stage_id='a')
                if transitive:
                    # The shared receipt slot now covers bridge; a's closure
                    # still reports the new work independently of receipt scope.
                    self.assertIn('Delegated work is unsettled: late-review (DISPATCHING)', after['blocked_reasons'])
                else:
                    self.assertEqual(after['blocked_reasons'], [] if sibling else
                        ['Delegated work is unsettled: late-review (DISPATCHING)'])
            return execute(argv, *args, **kwargs)
        with patch('ase.core.execute', side_effect=dispatch_before_process):
            if sibling:
                controller.execute_actions(task['task_id'], stage)
            else:
                with self.assertRaisesRegex(ValueError, 'Prerequisite|prerequisite|Delegated'):
                    controller.execute_actions(task['task_id'], stage)
        self.assertEqual(dispatched, [True])
        count = self.store.repo / 'dependent.count'
        if sibling:
            self.assertEqual(count.read_text(), '1')
        else:
            self.assertFalse(count.exists(), 'The dependent effect must not launch across unsettled prerequisite work')

    def test_late_prerequisite_dispatch_holds_actual_effect(self):
        self.late_effect_work()

    def test_late_inherited_transitive_dispatch_holds_actual_effect(self):
        self.late_effect_work(inherited=True, transitive=True)

    def test_unsettled_sibling_dispatch_keeps_dependent_effect_available(self):
        self.late_effect_work(sibling=True)

    def test_settled_prerequisite_advisory_keeps_dependent_effect_available(self):
        controller, parent, task, stage = self.fixture()
        self.dispatch(parent, settle=True)
        controller.execute_actions(task['task_id'], stage)
        self.assertEqual((self.store.repo / 'dependent.count').read_text(), '1')

    def test_dispatch_after_verifier_return_blocks_prerequisite_capture(self):
        controller, parent, task, stage = self.fixture()
        actual_verify = controller.verify
        def verify_then_dispatch(task_id, stage_id=None):
            verdict = actual_verify(task_id, stage_id)
            if task_id == parent['task_id'] and stage_id == 'a':
                self.assertEqual(verdict['status'], 'APPROVED', verdict)
                self.dispatch(parent)
            return verdict
        with patch.object(controller, 'verify', side_effect=verify_then_dispatch):
            blockers, binding = controller._revalidate_prerequisites(task, stage)
        self.assertTrue(any('Delegated work is unsettled' in reason for reason in blockers), blockers)
        self.assertIsNone(binding)

    def file_candidate(self, controller, task, stage):
        provider = Path(controller.runner.config['executable_args'][0])
        content = provider.read_text()
        content = content.replace("payload = {'edits':[],'summary':'Protocol-only empty candidate'}",
            "payload = {'edits':[{'path':'dependent.txt','operation':'write','before_sha256':'','content':'dependent candidate'}], 'summary':'Disposable dependent candidate'}")
        provider.write_text(content)
        reservation = controller.reserve_cycle(task['task_id'], stage['id'])
        authority = controller._stage_authority(task, stage)
        progress = {'candidate_id': reservation['candidate_id'], 'candidate_authority': authority}
        receipt = controller.runner.run(get_adapter('codex'), controller._request(task['task_id'],
            'implementation', EDIT_SCHEMA, {'stage': stage, 'candidate_id': reservation['candidate_id']}), timeout=5)['receipt']
        return progress, receipt, authority

    def test_late_prerequisite_dispatch_holds_candidate_admission(self):
        from ase.local_edit_intent import prepare_local_edits
        from ase.workflow import _target
        controller, parent, task, stage = self.fixture()
        blockers, binding = controller._revalidate_prerequisites(task, stage)
        self.assertEqual(blockers, [])
        progress, receipt, authority = self.file_candidate(controller, task, stage)
        self.dispatch(parent)
        with self.assertRaisesRegex(ValueError, 'Prerequisite|prerequisite|Delegated'):
            prepare_local_edits(controller, task, stage, progress, receipt, authority, binding, _target, EDIT_SCHEMA)
        self.assertFalse((self.store.repo / 'dependent.txt').exists())
        self.assertIsNone(read_record(self.store.db, 'local_edit_application', progress['candidate_id'])['payload'])

    def test_late_prerequisite_dispatch_holds_committed_candidate_file_writer(self):
        from ase.local_edit_intent import finish_local_edits, prepare_local_edits
        from ase.workflow import _target, apply_edits
        controller, parent, task, stage = self.fixture(inherited=True)
        blockers, binding = controller._revalidate_prerequisites(task, stage)
        self.assertEqual(blockers, [])
        progress, receipt, authority = self.file_candidate(controller, task, stage)
        progress = prepare_local_edits(controller, task, stage, progress, receipt, authority, binding, _target, EDIT_SCHEMA)
        self.dispatch(parent)
        actual_authority = controller._stage_authority
        writers = []
        def writer_authority(current, selected):
            writers.append(self.store.db.in_transaction)
            return actual_authority(current, selected)
        with patch.object(controller, '_stage_authority', side_effect=writer_authority):
            with self.assertRaisesRegex(ValueError, 'Prerequisite|prerequisite|Delegated'):
                finish_local_edits(controller, task, stage, progress, binding, _target, apply_edits, EDIT_SCHEMA)
        self.assertEqual(writers, [True])
        self.assertFalse((self.store.repo / 'dependent.txt').exists())
        self.assertEqual(read_record(self.store.db, 'local_edit_application', progress['candidate_id'])['payload']['state'], 'PREPARED')


class LateStageCaptureTests(unittest.TestCase):
    """Authentic transport/effects; capture APPROVED is a scheduler-only seam."""
    def setUp(self):
        # Reuse the disposable runner/edit helper without exposing its TestCase
        # as a module-level unittest discovery alias or changing its owner file.
        from test_action_packet_evidence import ActionPacketEvidenceTests
        self.fx = ActionPacketEvidenceTests()
        self.fx.setUp()
        self.addCleanup(self.fx.doCleanups)
        self.other = self.fx.two_stages()
        self.first = self.fx.candidate_until_recheck(self.fx.stage, "guide.py", "first-stage candidate\n")
        self.second = self.fx.candidate_until_recheck(self.other, "other.txt", "independent candidate\n")
        self.fx.controller.execute_actions("evidence", self.fx.stage)
        self.fx.controller.check("evidence", "deliver")
        trusted = self.fx.base / "late-capture-provider"
        trusted.mkdir()
        provider = trusted / "provider.py"
        body = """
schema = json.loads(pathlib.Path(args[9]).read_text())
payload = {'criteria':[]} if 'criteria' in schema['properties'] else {'findings':[],'summary':'Protocol capture fixture only'}
"""
        provider.write_text(FIXTURE.replace("payload = {'verdict':'PASS'}", body))
        self.runner = Runner({"executable": sys.executable, "executable_args": [str(provider), "success", "codex"],
            "trusted_cwd": str(trusted), "protocol_only": True}, self.fx.store.root / "tasks/evidence/sessions")

    def scheduler_only_judgment(self, task_id="evidence", stage_id="deliver"):
        for role, schema in (("ac", AC_SCHEMA), ("review", REVIEW_SCHEMA)):
            result = self.runner.run(get_adapter("codex"), self.fx.controller._request(task_id, role, schema,
                {"stage_id": stage_id}), timeout=5)
            self.assertEqual(result["result"]["status"], "COMPLETE")
            self.fx.controller._save("verification", task_id + ":" + role, result["receipt"])
        return {"status": "APPROVED", "blocked_reasons": [], "rejected_reasons": [],
            "fingerprint": source_snapshot(self.fx.store, self.fx.task["contract"], task_id=task_id)["fingerprint"],
            "fixture_boundary": "Scheduler-only capture marker; protocol roles do not establish semantic acceptance"}

    def test_readonly_resume_captures_own_retained_candidate_after_independent_overwrite(self):
        self.assertEqual(self.fx.manifest()[1]["coverage"]["missing"], [])
        sessions = list((self.fx.store.root / "tasks/evidence/sessions").glob("*/request.json"))
        original_candidates = sum(json.loads(path.read_bytes())["role"] == "implementation" for path in sessions)
        with patch.object(self.fx.controller, "verify", side_effect=self.scheduler_only_judgment):
            self.fx.controller._run_stage(self.fx.tasks.snapshot("evidence"), self.fx.stage, self.runner, get_adapter("codex"), resume=True)
        saved = read_record(self.fx.store.db, "stage", "evidence:deliver")["payload"]
        self.assertEqual(saved["proof"]["roles"]["implementation"], self.first)
        self.assertEqual(read_record(self.fx.store.db, "implementation", "evidence")["payload"], self.second)
        self.assertEqual((self.fx.repo / "count").read_text(), "1")
        sessions = list((self.fx.store.root / "tasks/evidence/sessions").glob("*/request.json"))
        self.assertEqual(sum(json.loads(path.read_bytes())["role"] == "implementation" for path in sessions), original_candidates)
        manifest, packet = self.fx.manifest()
        self.assertEqual(packet["coverage"]["missing"], [])
        self.assertEqual(manifest["stages"][0]["implementation_proposal"]["execution_id"], self.first["execution_id"])
        self.assertEqual([item["stage_id"] for item in self.fx.controller._stage_history(self.fx.tasks.snapshot("evidence"))[0]["stages"]], ["deliver"])

    def test_legacy_global_fallback_must_prove_the_selected_stage(self):
        self.fx.controller._save("stage_implementation", "evidence:deliver", None)
        marker = self.scheduler_only_judgment()
        with self.assertRaisesRegex(ValueError, "stage"):
            self.fx.controller._capture_stage("evidence", "deliver", marker)
        self.assertIsNone(read_record(self.fx.store.db, "stage", "evidence:deliver")["payload"])

    def test_legacy_global_fallback_accepts_an_authentic_own_stage_receipt(self):
        self.fx.controller._save("stage_implementation", "evidence:deliver", None)
        self.fx.controller._save("implementation", "evidence", self.first)
        self.fx.controller._capture_stage("evidence", "deliver", self.scheduler_only_judgment())
        saved = read_record(self.fx.store.db, "stage", "evidence:deliver")["payload"]
        self.assertEqual(saved["proof"]["roles"]["implementation"], self.first)
        self.assertEqual(self.fx.manifest()[1]["coverage"]["missing"], [])

    def test_tampered_retained_receipt_is_not_replaced_with_another_stage(self):
        marker = self.scheduler_only_judgment()
        Path(self.first["artifacts"]["request"]["path"]).write_text("tampered retained request")
        with self.assertRaises(ValueError):
            self.fx.controller._capture_stage("evidence", "deliver", marker)
        self.assertIsNone(read_record(self.fx.store.db, "stage", "evidence:deliver")["payload"])

    def test_existing_wrong_completed_history_is_not_silently_rewritten(self):
        marker = self.scheduler_only_judgment()
        # Explicit historical-data fixture, not an authenticated completion.
        self.fx.controller._save("stage", "evidence:deliver", {"state": "COMPLETED",
            "contract_hash": self.fx.task["contract_hash"], "proposal_hash": self.fx.task["proposal_hash"],
            "proof": {"roles": {"implementation": self.second}}})
        before = read_record(self.fx.store.db, "stage", "evidence:deliver")
        with self.assertRaisesRegex(ValueError, "completed|Completed|history"):
            self.fx.controller._capture_stage("evidence", "deliver", marker)
        self.assertEqual(read_record(self.fx.store.db, "stage", "evidence:deliver"), before)
        self.assertTrue(self.fx.manifest()[1]["coverage"]["missing"])


if __name__ == "__main__":
    unittest.main()
