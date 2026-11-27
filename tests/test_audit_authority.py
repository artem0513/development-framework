"""Real local process regressions for revoked, cancelled and delegated authority."""
import copy
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from ase.journal import Journal
from ase.state import read_record
from ase.store import Store
from ase.tasks import TaskService
from ase.workflow import WorkflowController


class AuditAuthorityTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="ase-authority-regression-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.repo = self.root / "work"
        self.repo.mkdir()
        self.home = self.root / "state"
        self.store = Store(self.repo, self.home)
        self.addCleanup(self.store.close)
        self.tasks = TaskService(self.store)
        self.controller = WorkflowController(self.store)

    def contract(self, task_id="root", *, barrier=False, slow_action=False):
        observer = ("from pathlib import Path;import json,time;p=Path('resource');"
                    "gate=Path('observing');release=Path('continue');first=not gate.exists();"
                    "gate.write_text('1');deadline=time.monotonic()+8\n"
                    "while first and not release.exists() and time.monotonic()<deadline: time.sleep(.01)\n"
                    "print(json.dumps({'exists':p.exists(),'resource_id':'local-resource' if p.exists() else None}))")
        if not barrier:
            observer = ("from pathlib import Path;import json;p=Path('resource');"
                        "print(json.dumps({'exists':p.exists(),'resource_id':'local-resource' if p.exists() else None}))")
        action = "from pathlib import Path;Path('resource').write_text('created')"
        if slow_action:
            action = ("from pathlib import Path;import time;Path('running').write_text('1');"
                      "time.sleep(8);Path('resource').write_text('created too late')")
        return {"id": task_id, "schema_version": 2, "profile": "infrastructure",
                "request": "Create a disposable local resource only under current authority",
                "scope": {"resources": ["resource"], "effects": ["command"]},
                "acceptance": [{"id": "AC", "expected": "Resource exists", "evidence_ids": ["p"]}],
                "evidence": [{"id": "p", "kind": "artifact", "path": "resource"}],
                "stages": [{"id": "one", "acceptance_ids": ["AC"], "actions": [{
                    "id": "create", "argv": [sys.executable, "-c", action],
                    "observation_argv": [sys.executable, "-c", observer]}]}], "unknowns": []}

    def approve(self, value):
        task = self.tasks.create(value)
        task = self.tasks.propose(task["task_id"], {"summary": "Disposable regression scope",
            "stage_ids": [s["id"] for s in task["contract"]["stages"]]}, expected_revision=task["revision"])
        return self.tasks.approve(task["task_id"], proposal_hash=task["proposal_hash"],
            user_message_ref="test:explicit-local-authority", approved_stage_ids=["one"])

    def replace_approval(self, task_id="root", *, resources=None):
        """Real separate-connection CAS, proposal and approval; no state doubles."""
        other = Store(self.repo, self.home)
        try:
            tasks = TaskService(other)
            before = tasks.snapshot(task_id)
            proposal = copy.deepcopy(before["proposal"])
            current = before
            if resources is not None:
                revised = copy.deepcopy(before["contract"])
                revised["stages"][0]["scope"]["resources"] = resources
                current = tasks.revise(task_id, revised, expected_revision=before["revision"])
            current = tasks.propose(task_id, proposal, expected_revision=current["revision"])
            current = tasks.approve(task_id, proposal_hash=current["proposal_hash"],
                user_message_ref="test:replacement-local-authority", approved_stage_ids=before["approved_stage_ids"])
            self.assertEqual(current["contract"]["stages"][0].get("actions"), before["contract"]["stages"][0].get("actions"))
            if resources is None:
                self.assertEqual(current["contract_hash"], before["contract_hash"])
                self.assertEqual(current["proposal_hash"], before["proposal_hash"])
            return current
        finally:
            other.close()

    def cancel(self, store, task_id):
        journal = Journal(store)
        journal.start_session(task_id, "owner-cancel")
        journal.finish_session(task_id, "owner-cancel", {"finish_reason": "cancelled",
            "summary": "Owner cancelled this request", "next_action": "Do not create the resource",
            "context": {"summary": "Local cancellation regression"}, "blockers": []},
            closure_key="owner-cancel:finish", expected_revision=0, expected_context_revision=0)

    def race(self, task_id, trigger, change):
        errors = []
        committed = []

        def mutate():
            try:
                deadline = time.monotonic() + 8
                while not (self.repo / trigger).exists():
                    if time.monotonic() > deadline:
                        raise RuntimeError("Process barrier timed out")
                    time.sleep(.01)
                other = Store(self.repo, self.home)
                try:
                    change(other)
                    committed.append(True)
                finally:
                    other.close()
            except Exception as error:
                errors.append(repr(error))
            finally:
                (self.repo / "continue").write_text("authority updated")

        thread = threading.Thread(target=mutate)
        thread.start()
        try:
            try:
                stage = self.tasks.snapshot(task_id)["contract"]["stages"][0]
                self.controller.execute_actions(task_id, stage)
            except ValueError:
                pass
        finally:
            thread.join(10)
        self.assertFalse(thread.is_alive(), "Authority mutator did not finish")
        self.assertEqual(errors, [])
        self.assertEqual(committed, [True])
        self.assertFalse((self.repo / "resource").exists(), "Effect ran after authority withdrawal")

    def test_revision_during_observation_blocks_stale_action(self):
        self.approve(self.contract(barrier=True))

        def revise(store):
            tasks = TaskService(store)
            old = tasks.snapshot("root")
            revised = copy.deepcopy(old["contract"])
            revised["stages"][0]["actions"] = []
            tasks.revise("root", revised, expected_revision=old["revision"])

        self.race("root", "observing", revise)
        self.assertEqual(self.tasks.snapshot("root")["state"], "DRAFT")

    def test_same_recipe_reapproval_after_observation_does_not_rebind_old_action(self):
        value = self.contract()
        value["scope"]["resources"].append("safe")
        task = self.approve(value)
        actual_observe, changed = self.controller._observe_action, []
        def observe_then_replace(*args, **kwargs):
            observation = actual_observe(*args, **kwargs)
            if not changed:
                self.replace_approval(resources=["safe"])
                changed.append(True)
            return observation
        error = None
        with patch.object(self.controller, "_observe_action", side_effect=observe_then_replace):
            try:
                self.controller.execute_actions("root", task["contract"]["stages"][0])
            except ValueError as ex:
                error = str(ex)
        self.assertEqual(changed, [True])
        self.assertFalse((self.repo / "resource").exists(), "Old action launched under replacement narrower scope")
        self.assertIsNotNone(error)
        operation = json.loads(self.store.db.execute("SELECT payload FROM runtime_records WHERE kind='operation'").fetchone()[0])
        self.assertFalse(operation["ever_attempted"], "Stale action should be held before reserving its submission attempt")

    def test_reapproval_after_prerequisite_call_does_not_rebind_old_action(self):
        value = self.contract()
        value["scope"]["resources"].append("safe")
        task = self.approve(value)
        actual_revalidate = self.controller._revalidate_prerequisites
        def reassess_then_replace(*args, **kwargs):
            result = actual_revalidate(*args, **kwargs)
            self.replace_approval(resources=["safe"])
            return result
        error = None
        with patch.object(self.controller, "_revalidate_prerequisites", side_effect=reassess_then_replace):
            try:
                self.controller.execute_actions("root", task["contract"]["stages"][0])
            except ValueError as ex:
                error = str(ex)
        self.assertFalse((self.repo / "resource").exists(), "Prerequisite call silently rebound old action to replacement approval")
        self.assertIsNotNone(error)

    def test_same_recipe_ancestor_reapproval_does_not_rebind_leaf_action(self):
        value = self.contract()
        value["scope"]["resources"].append("safe")
        self.approve(value)
        child = self.contract("leaf")
        child.update(parent_task_id="root", parent_stage_id="one")
        leaf = self.tasks.create(child)
        actual_observe, changed = self.controller._observe_action, []
        def observe_then_replace(*args, **kwargs):
            observation = actual_observe(*args, **kwargs)
            if not changed:
                self.replace_approval()
                changed.append(True)
            return observation
        error = None
        with patch.object(self.controller, "_observe_action", side_effect=observe_then_replace):
            try:
                self.controller.execute_actions("leaf", leaf["contract"]["stages"][0])
            except ValueError as ex:
                error = str(ex)
        self.assertFalse((self.repo / "resource").exists(), "Leaf action adopted replacement ancestor approval")
        self.assertIsNotNone(error)

    def test_reapproval_between_actions_holds_remaining_old_batch(self):
        value = self.contract()
        value["scope"]["resources"] += ["second-resource", "safe"]
        value["stages"][0]["actions"].append({"id": "second", "argv": [sys.executable, "-c",
            "from pathlib import Path;Path('second-resource').write_text('created')"], "observation_argv": [sys.executable, "-c",
            "from pathlib import Path;import json;p=Path('second-resource');print(json.dumps({'exists':p.exists(),'resource_id':'second-resource' if p.exists() else None}))"]})
        task = self.approve(value)
        actual_observe, changed = self.controller._observe_action, []
        def observe_then_replace(task_id, stage, action, folder, **kwargs):
            observation = actual_observe(task_id, stage, action, folder, **kwargs)
            if action["id"] == "create" and observation["exists"] and not changed:
                self.replace_approval(resources=["resource", "safe"])
                changed.append(True)
            return observation
        error = None
        with patch.object(self.controller, "_observe_action", side_effect=observe_then_replace):
            try:
                self.controller.execute_actions("root", task["contract"]["stages"][0])
            except ValueError as ex:
                error = str(ex)
        self.assertTrue((self.repo / "resource").exists())
        self.assertFalse((self.repo / "second-resource").exists(), "Later old-batch action adopted replacement scope")
        self.assertIsNotNone(error)

    def test_reapproval_between_check_commands_holds_remaining_old_batch(self):
        from ase.core import execute
        value = self.contract()
        value["profile"] = "document"
        value["scope"]["resources"] = ["first-check", "second-check", "safe"]
        value["evidence"] = [{"id": name, "kind": "command", "argv": [sys.executable, "-c",
            "from pathlib import Path;Path('" + name + "').write_text('executed')"]} for name in ("first-check", "second-check")]
        value["acceptance"][0]["evidence_ids"] = ["first-check", "second-check"]
        value["stages"][0]["actions"] = []
        task = self.approve(value)
        changed = []
        def execute_then_replace(argv, *args, **kwargs):
            result = execute(argv, *args, **kwargs)
            if list(argv) == task["contract"]["evidence"][0]["argv"] and not changed:
                self.replace_approval(resources=["safe"])
                changed.append(True)
            return result
        error = None
        with patch("ase.core.execute", side_effect=execute_then_replace):
            try:
                self.controller.check("root")
            except ValueError as ex:
                error = str(ex)
        self.assertTrue((self.repo / "first-check").exists())
        self.assertFalse((self.repo / "second-check").exists(), "Later check silently adopted replacement batch authority")
        self.assertIsNotNone(error)

    def test_same_hash_reapproval_after_preparation_does_not_reuse_old_approval_epoch(self):
        from ase.core import execute
        task = self.approve(self.contract())
        action = task["contract"]["stages"][0]["actions"][0]
        original_epoch = read_record(self.store.db, "approval", "root")["revision"]
        changed = []
        def execute_after_regrant(argv, *args, **kwargs):
            if list(argv) == action["argv"] and not changed:
                self.replace_approval()
                changed.append(True)
            return execute(argv, *args, **kwargs)
        error = None
        with patch("ase.core.execute", side_effect=execute_after_regrant):
            try:
                self.controller.execute_actions("root", task["contract"]["stages"][0])
            except ValueError as ex:
                error = str(ex)
        self.assertGreater(read_record(self.store.db, "approval", "root")["revision"], original_epoch)
        self.assertFalse((self.repo / "resource").exists(), "Unchanged hashes silently adopted a new approval epoch")
        self.assertIsNotNone(error)

    def test_cancellation_during_observation_blocks_pending_action(self):
        self.approve(self.contract(barrier=True))
        self.race("root", "observing", lambda store: self.cancel(store, "root"))
        self.assertEqual(self.tasks.snapshot("root")["state"], "CANCELLED")

    def delegate_chain(self, *, slow_action=False):
        self.approve(self.contract(slow_action=slow_action))
        for child_id, parent in [("middle", "root"), ("leaf", "middle")]:
            child = self.contract(child_id, slow_action=slow_action)
            child.update(parent_task_id=parent, parent_stage_id="one")
            self.tasks.create(child)

    def test_stopped_intermediate_ancestor_blocks_leaf(self):
        self.delegate_chain()
        self.controller.request_stop("middle", reason="Stop this branch")
        with self.assertRaisesRegex(ValueError, "[Ss]top|[Pp]aus"):
            self.controller.execute_actions("leaf", self.tasks.snapshot("leaf")["contract"]["stages"][0])
        self.assertFalse((self.repo / "resource").exists())

    def test_cancelled_intermediate_ancestor_blocks_leaf(self):
        self.delegate_chain()
        self.cancel(self.store, "middle")
        with self.assertRaisesRegex(ValueError, "[Cc]ancel|terminal|[Ss]top"):
            self.controller.execute_actions("leaf", self.tasks.snapshot("leaf")["contract"]["stages"][0])
        self.assertFalse((self.repo / "resource").exists())

    def test_stop_intermediate_ancestor_cancels_active_leaf(self):
        self.delegate_chain(slow_action=True)
        started = time.monotonic()
        self.race("leaf", "running", lambda store: WorkflowController(store).request_stop("middle", reason="Stop active branch"))
        self.assertLess(time.monotonic() - started, 6)
        operation = self.store.db.execute("SELECT payload FROM runtime_records WHERE kind='operation'").fetchone()
        self.assertIsNotNone(operation)
        self.assertNotEqual(json.loads(operation[0])["state"], "APPLIED")

    def test_cancellation_of_intermediate_ancestor_cancels_active_leaf(self):
        self.delegate_chain(slow_action=True)
        started = time.monotonic()
        self.race("leaf", "running", lambda store: self.cancel(store, "middle"))
        self.assertLess(time.monotonic() - started, 6)

    def test_revision_cancels_active_command(self):
        self.approve(self.contract(slow_action=True))

        def revise(store):
            tasks = TaskService(store)
            current = tasks.snapshot("root")
            revised = copy.deepcopy(current["contract"])
            revised["stages"][0]["actions"] = []
            tasks.revise("root", revised, expected_revision=current["revision"])

        started = time.monotonic()
        self.race("root", "running", revise)
        self.assertLess(time.monotonic() - started, 6)

    def partial_contract(self):
        value = {"id": "partial", "schema_version": 2, "profile": "document",
            "request": "Draft now; production check is deferred",
            "scope": {"resources": ["draft", "deferred-command-ran"], "effects": ["command"]},
            "acceptance": [{"id": "A", "expected": "Draft exists", "evidence_ids": ["draft"]},
                           {"id": "B", "expected": "Production checked", "evidence_ids": ["production"]}],
            "evidence": [{"id": "draft", "kind": "artifact", "path": "draft"},
                {"id": "production", "kind": "command", "argv": [sys.executable, "-c",
                    "from pathlib import Path;Path('deferred-command-ran').write_text('ran')"]}],
            "stages": [{"id": "one", "scope": {"resources": ["draft"], "effects": ["command"]}, "acceptance_ids": ["A"]},
                {"id": "deferred", "scope": {"resources": ["deferred-command-ran"], "effects": ["command"]},
                 "acceptance_ids": ["B"], "unknown_ids": ["access"]}],
            "unknowns": [{"id": "access", "statement": "Production authority not yet granted",
                "status": "UNRESOLVED", "blocking_stage_ids": ["deferred"]}]}
        return value

    def test_task_wide_checks_skip_unapproved_stage(self):
        (self.repo / "draft").write_text("draft")
        value = self.partial_contract()
        self.approve(value)
        result = self.controller.check("partial")
        self.assertEqual([r["id"] for r in result["results"]], ["draft"])
        self.assertFalse((self.repo / "deferred-command-ran").exists())
        with self.assertRaisesRegex(ValueError, "approval|Unresolved"):
            self.controller.check("partial", "deferred")

    def test_software_required_test_of_deferred_stage_is_not_common_authority(self):
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True, capture_output=True)
        (self.repo / "draft").write_text("draft")
        value = self.partial_contract()
        value["profile"] = "software"
        value["evidence"][1].update(kind="test", parser="unittest")
        value["evidence"].append({"id": "shared-local", "kind": "test", "parser": "unittest", "argv": [sys.executable, "-c",
            "import unittest\nfrom pathlib import Path\nclass Local(unittest.TestCase):\n def test_draft(self): self.assertTrue(Path('draft').exists())\nunittest.main()"]})
        self.approve(value)
        result = self.controller.check("partial")
        self.assertEqual([r["id"] for r in result["results"]], ["draft", "shared-local"])
        self.assertFalse((self.repo / "deferred-command-ran").exists())
        from ase.profiles import evaluate_contract_profiles
        from ase.gates import stage_checks
        task = self.tasks.snapshot("partial")
        specs = stage_checks(task["contract"], self.tasks.execution_plan("partial")["checks"], "one")
        self.assertEqual(evaluate_contract_profiles(task["contract"], specs, result["results"], stage_id="one")["status"], "PASS")

    def test_empty_acceptance_stage_does_not_gain_deferred_check_authority(self):
        value = self.partial_contract()
        value["stages"][0]["acceptance_ids"] = []
        value["stages"][1]["acceptance_ids"] = ["A", "B"]
        self.approve(value)
        result = self.controller.check("partial")
        self.assertEqual(result["results"], [])
        self.assertFalse((self.repo / "deferred-command-ran").exists())

    def test_explicit_check_scope_cannot_be_borrowed_by_other_stage(self):
        value = self.partial_contract()
        value["acceptance"][1]["evidence_ids"] = ["deferred-artifact"]
        value["evidence"].append({"id": "deferred-artifact", "kind": "artifact", "path": "deferred-command-ran"})
        # Unlinked common check still has an explicit boundary.
        value["evidence"][1]["scope"] = value["stages"][1]["scope"]
        self.approve(value)
        self.controller.check("partial")
        self.assertFalse((self.repo / "deferred-command-ran").exists())

    def test_current_approved_action_still_runs(self):
        task = self.approve(self.contract())
        result = self.controller.execute_actions("root", task["contract"]["stages"][0])
        self.assertTrue((self.repo / "resource").exists())
        self.assertTrue(result[0]["exists"])

    def test_withdrawal_after_command_preparation_prevents_process_creation(self):
        from ase import core
        task = self.approve(self.contract())
        action = task["contract"]["stages"][0]["actions"][0]
        execute, popen = core.execute, subprocess.Popen
        mutations_started = []

        def withdraw_before_execute(argv, *args, **kwargs):
            if argv == action["argv"]:
                current = self.tasks.snapshot("root")
                revised = copy.deepcopy(current["contract"])
                revised["stages"][0]["actions"] = []
                self.tasks.revise("root", revised, expected_revision=current["revision"])
            return execute(argv, *args, **kwargs)

        def observe_spawn(argv, *args, **kwargs):
            if argv == action["argv"]:
                mutations_started.append(argv)
            return popen(argv, *args, **kwargs)

        with patch("ase.core.execute", side_effect=withdraw_before_execute), patch("ase.core.subprocess.Popen", side_effect=observe_spawn):
            with self.assertRaisesRegex(ValueError, "approval|current|authority"):
                self.controller.execute_actions("root", task["contract"]["stages"][0])
        self.assertEqual(mutations_started, [], "Revoked process was created before cancellation polling")
        self.assertFalse((self.repo / "resource").exists())

    def test_validation_and_spawn_share_short_writer_transaction(self):
        task = self.approve(self.contract())
        action = task["contract"]["stages"][0]["actions"][0]
        other = Store(self.repo, self.home)
        self.addCleanup(other.close)
        other.db.execute("PRAGMA busy_timeout=0")
        popen, blocked = subprocess.Popen, []

        def observe_spawn(argv, *args, **kwargs):
            if argv == action["argv"]:
                current = TaskService(other).snapshot("root")
                revised = copy.deepcopy(current["contract"])
                revised["stages"][0]["actions"] = []
                try:
                    TaskService(other).revise("root", revised, expected_revision=current["revision"])
                except sqlite3.OperationalError as error:
                    self.assertIn("locked", str(error))
                    blocked.append(True)
            return popen(argv, *args, **kwargs)

        with patch("ase.core.subprocess.Popen", side_effect=observe_spawn):
            self.controller.execute_actions("root", task["contract"]["stages"][0])
        self.assertEqual(blocked, [True], "Another connection committed revocation inside the launch boundary")
        current = TaskService(other).snapshot("root")
        TaskService(other).revise("root", current["contract"], expected_revision=current["revision"])


@unittest.skipUnless(os.name == "nt", "requires actual Windows DPAPI")
class AuditCredentialLaunchTests(unittest.TestCase):
    def setUp(self):
        import test_credential_integration as fixtures
        self.fixture = fixtures.CredentialIntegrationTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def test_revocation_after_actual_credential_resolution_blocks_effect(self):
        from ase.access import CredentialAccess
        from ase.credentials import CredentialError
        fixture = self.fixture
        task = fixture.create(fixture.contract("late-auth-revoke", action=True))
        original, calls = CredentialAccess._ensure, []

        def resolve_then_revoke(client, *args, **kwargs):
            result = original(client, *args, **kwargs)
            calls.append(True)
            if len(calls) == 2:  # First resolution is the actual pre-effect observer.
                current = fixture.tasks.snapshot(task["task_id"])
                revised = copy.deepcopy(current["contract"])
                revised["stages"][0]["actions"] = []
                fixture.tasks.revise(task["task_id"], revised, expected_revision=current["revision"])
            return result

        with patch.object(CredentialAccess, "_ensure", resolve_then_revoke):
            with self.assertRaises((CredentialError, ValueError)):
                fixture.controller.execute_actions(task["task_id"], task["contract"]["stages"][0])
        self.assertEqual(len(calls), 2)
        self.assertFalse((fixture.repo / "resource.counter").exists())

    def test_authenticated_effect_uses_atomic_launch_guard(self):
        fixture = self.fixture
        task = fixture.create(fixture.contract("auth-launch-boundary", action=True))
        action = task["contract"]["stages"][0]["actions"][0]
        other = Store(fixture.repo, fixture.home)
        self.addCleanup(other.close)
        other.db.execute("PRAGMA busy_timeout=0")
        popen, blocked = subprocess.Popen, []

        def observe_spawn(argv, *args, **kwargs):
            if argv == action["argv"]:
                current = TaskService(other).snapshot(task["task_id"])
                revised = copy.deepcopy(current["contract"])
                revised["stages"][0]["actions"] = []
                try:
                    TaskService(other).revise(task["task_id"], revised, expected_revision=current["revision"])
                except sqlite3.OperationalError:
                    blocked.append(True)
            return popen(argv, *args, **kwargs)

        with patch("ase.core.subprocess.Popen", side_effect=observe_spawn):
            fixture.controller.execute_actions(task["task_id"], task["contract"]["stages"][0])
        self.assertEqual(blocked, [True])
        self.assertEqual((fixture.repo / "resource.counter").read_text(), "1")

    def test_shared_access_check_selects_eligible_approved_stage(self):
        fixture = self.fixture
        value = fixture.contract("shared-access-stage")
        value["stages"].insert(0, {"id": "draft", "scope": {"resources": ["resource.counter"], "effects": ["command"]},
                                    "acceptance_ids": ["AC1"], "depends_on": []})
        task = fixture.tasks.create(value)
        task = fixture.tasks.propose(task["task_id"], {"summary": "Both stages approved within their boundaries",
            "stage_ids": ["draft", "work"]}, expected_revision=task["revision"])
        fixture.tasks.approve(task["task_id"], proposal_hash=task["proposal_hash"], user_message_ref="test:shared-access",
            approved_stage_ids=["draft", "work"])
        result = fixture.controller.check(task["task_id"])
        self.assertEqual(len(result["results"]), 1)
        self.assertEqual(result["results"][0]["exit_code"], 0)
        self.assertEqual(result["results"][0]["access"]["target"], fixture.target)

    def test_authenticated_same_recipe_reapproval_after_observation_holds_effect(self):
        fixture = self.fixture
        task = fixture.create(fixture.contract("credential-continuity", action=True))
        actual_observe, changed = fixture.controller._observe_action, []
        def observe_then_replace(*args, **kwargs):
            observation = actual_observe(*args, **kwargs)
            if not changed:
                AuditAuthorityTests.replace_approval(fixture, task["task_id"], resources=[fixture.target])
                changed.append(True)
            return observation
        error = None
        with patch.object(fixture.controller, "_observe_action", side_effect=observe_then_replace):
            try:
                fixture.controller.execute_actions(task["task_id"], task["contract"]["stages"][0])
            except ValueError as ex:
                error = str(ex)
        self.assertEqual(changed, [True])
        self.assertFalse((fixture.repo / "resource.counter").exists(), "Credential action adopted replacement scope")
        self.assertIsNotNone(error)

    def test_access_api_preserves_authority_from_before_declaration_lookup(self):
        from ase.access import CredentialAccess
        fixture = self.fixture
        for operation in ("ensure", "request"):
            with self.subTest(operation=operation):
                task = fixture.create(fixture.contract("access-continuity-" + operation))
                actual_declared = fixture.controller._declared_access
                entered_access = []
                def declared_then_replace(*args, **kwargs):
                    spec = actual_declared(*args, **kwargs)
                    AuditAuthorityTests.replace_approval(fixture, task["task_id"], resources=[fixture.target])
                    return spec
                # A rejected request must not reach credential resolution or
                # HTTP at all. Real approval/recipe lookup executes beforehand.
                def unexpected_access(*args, **kwargs):
                    entered_access.append(True)
                    raise AssertionError("Replacement authority reached credential/network work")
                error = None
                with patch.object(fixture.controller, "_declared_access", side_effect=declared_then_replace), patch.object(CredentialAccess, operation, side_effect=unexpected_access):
                    try:
                        method = fixture.controller.ensure_access if operation == "ensure" else fixture.controller.request_access
                        method(task["task_id"], "work", fixture.access)
                    except ValueError as ex:
                        error = str(ex)
                self.assertEqual(entered_access, [])
                self.assertIsNotNone(error)


if __name__ == "__main__":
    unittest.main()
