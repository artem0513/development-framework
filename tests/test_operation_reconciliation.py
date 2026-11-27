"""Reconciliation must establish nonapplication before permitting a replay."""
import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

from ase.operations import OperationLedger
from ase.state import hash_value, read_record, transaction, write_record
from ase.store import Store
from ase.tasks import TaskService
from ase.workflow import WorkflowController
from test_task_contract_v2 import contract


class OperationReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ase-reconciliation-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "work"
        self.repo.mkdir()
        self._open()
        TaskService(self.store).create(contract())
        self.intent = {"create": "controlled-resource"}
        self.ops.prepare("approval-task", "draft", "create", self.intent)

    def _open(self):
        self.store = Store(self.repo, self.root / "state")
        self.addCleanup(self.store.close)
        self.ops = OperationLedger(self.store)

    def _reopen(self):
        self.store.close()
        self._open()

    def _record(self):
        return read_record(self.store.db, "operation", "create")

    def _submission(self, provider_attempt_id, *, name="submission.json", exit_code=0):
        log = self.root / name
        log.write_text(json.dumps({"provider_attempt_id": provider_attempt_id}), encoding="utf-8")
        return {"log": str(log), "log_sha256": hashlib.sha256(log.read_bytes()).hexdigest(),
                "exit_code": exit_code, "error": None, "timed_out": False, "cancelled": False}

    def _action_intent(self, action_id="submit", *, target="resource-one", observer="probe-one"):
        return {"project_id": self.store.project_id, "workspace_id": self.store.workspace_id,
                "effect_scope": {"resources": [target], "effects": ["command"]},
                "action": {"id": action_id, "argv": ["provider", "create", target],
                           "observation_argv": ["provider", observer, target], "timeout": 30}}

    def test_stage_rename_and_changed_observer_reuse_unknown_canonical_operation(self):
        intent = self._action_intent()
        self.ops.prepare("approval-task", "draft", "original", intent)
        self.ops.started("original")
        self._reopen()
        renamed = self.ops.prepare("approval-task", "renamed", "renamed-operation",
                                  self._action_intent(observer="improved-probe"))
        self.assertEqual(renamed["action_id"], "original")
        self.assertEqual(renamed["state"], "UNKNOWN")
        self.assertFalse(renamed["new"])

    def test_relabelled_action_cannot_duplicate_an_unknown_same_recipe(self):
        self.ops.prepare("approval-task", "draft", "original", self._action_intent())
        self.ops.started("original")
        self._reopen()
        with self.assertRaisesRegex(ValueError, "CONFLICT|UNKNOWN|[Uu]nresolved"):
            self.ops.prepare("approval-task", "renamed", "renamed-operation",
                             self._action_intent(action_id="renamed-action"))
        self.assertIsNone(read_record(self.store.db, "operation", "renamed-operation")["payload"])

    def test_same_effect_in_unrelated_root_task_stays_independent(self):
        self.ops.prepare("approval-task", "draft", "original", self._action_intent())
        self.ops.started("original")
        TaskService(self.store).create(contract("unrelated"))
        separate = self.ops.prepare("unrelated", "draft", "separate", self._action_intent())
        self.assertTrue(separate["new"])
        self.assertEqual(separate["action_id"], "separate")

    def test_delegate_cannot_reset_parent_unknown_operation_identity(self):
        tasks = TaskService(self.store)
        task = tasks.snapshot("approval-task")
        task = tasks.propose("approval-task", {"summary": "Approved scoped work", "stage_ids": ["draft"]},
                             expected_revision=task["revision"])
        tasks.approve("approval-task", proposal_hash=task["proposal_hash"],
                      user_message_ref="chat:authorized", approved_stage_ids=["draft"])
        child = contract("delegate")
        child.update(parent_task_id="approval-task", parent_stage_id="draft", unknowns=[])
        child["stages"] = child["stages"][:1]
        tasks.create(child)
        self.ops.prepare("approval-task", "draft", "original", self._action_intent())
        self.ops.started("original")
        self._reopen()
        inherited = self.ops.prepare("delegate", "draft", "child-operation", self._action_intent())
        self.assertEqual(inherited["action_id"], "original")
        self.assertEqual(inherited["state"], "UNKNOWN")

    def test_distinct_logical_actions_can_repeat_recipe_after_first_applied(self):
        self.ops.prepare("approval-task", "draft", "original", self._action_intent())
        self.ops.started("original")
        self.ops.reconcile("original", {"exists": True, "resource_id": "resource-one", "proof_ref": "actual.json"})
        second = self.ops.prepare("approval-task", "draft", "second",
                                  self._action_intent(action_id="create-second-resource"))
        self.assertTrue(second["new"])
        self.assertEqual(second["action_id"], "second")

    def test_different_effect_recipe_can_continue_while_other_is_unknown(self):
        self.ops.prepare("approval-task", "draft", "original", self._action_intent())
        self.ops.started("original")
        separate = self.ops.prepare("approval-task", "draft", "separate",
                                    self._action_intent(action_id="other", target="resource-two"))
        self.assertTrue(separate["new"])

    def test_canonical_lookup_is_read_only(self):
        self.ops.prepare("approval-task", "draft", "original", self._action_intent())
        before = self.store.db.total_changes
        found = self.ops.lookup("approval-task", "renamed", "renamed-operation", self._action_intent())
        self.assertEqual(found["action_id"], "original")
        self.assertEqual(self.store.db.total_changes, before)

    def test_same_recipe_with_disjoint_declared_scope_is_a_distinct_operation(self):
        intent = self._action_intent()
        self.ops.prepare("approval-task", "draft", "original", intent)
        self.ops.started("original")
        self._reopen()
        changed = copy.deepcopy(intent)
        changed["effect_scope"]["resources"] = ["resource-two"]
        separate = self.ops.prepare("approval-task", "other-stage", "separate", changed)
        self.assertEqual(separate["action_id"], "separate")
        self.assertEqual(separate["state"], "PREPARED")
        self.assertTrue(separate["new"])
        self.assertEqual(read_record(self.store.db, "operation", "original")["payload"]["state"], "UNKNOWN")

    def test_legacy_unknown_cannot_adopt_new_scope_through_same_recipe_alias(self):
        legacy = self._action_intent()
        legacy.pop("effect_scope")
        self.ops.prepare("approval-task", "draft", "original", legacy)
        self.ops.started("original")
        self._reopen()
        with self.assertRaisesRegex(ValueError, "CONFLICT"):
            self.ops.prepare("approval-task", "other-stage", "separate", self._action_intent())

    def test_legacy_known_application_can_refresh_without_gaining_scope_or_replay(self):
        legacy = self._action_intent()
        legacy.pop("effect_scope")
        self.ops.prepare("approval-task", "draft", "original", legacy)
        self.ops.started("original")
        self.ops.reconcile("original", {"exists": True, "resource_id": "resource-one", "proof_ref": "actual.json"})
        self._reopen()
        for exists in (True, False):
            before = self.store.db.total_changes
            exact = self.ops.lookup("approval-task", "draft", "original", self._action_intent())
            alias = self.ops.prepare("approval-task", "renamed", "renamed-operation", self._action_intent())
            self.assertEqual(exact["action_id"], "original")
            self.assertEqual(alias["action_id"], "original")
            self.assertFalse(alias["new"])
            self.assertEqual(self.store.db.total_changes, before)
            observation = {"exists": exists, "proof_ref": "fresh-observation.json"}
            if exists:
                observation["resource_id"] = "resource-one"
            self.ops.reconcile("original", observation)
            self._reopen()
            stored = read_record(self.store.db, "operation", "original")["payload"]
            self.assertTrue(stored["ever_applied"])
            self.assertNotIn("effect_scope", stored)
            self.assertNotIn("effect_scope", stored["intent"])
            with self.assertRaises(ValueError):
                self.ops.started("original")
        refreshed = self.ops.lookup("approval-task", "draft", "original", self._action_intent())
        self.assertEqual(refreshed["state"], "UNKNOWN")
        self.assertTrue(refreshed["ever_applied"])

    def test_old_style_lookup_can_observe_known_scoped_application_read_only(self):
        self.ops.prepare("approval-task", "draft", "original", self._action_intent())
        self.ops.started("original")
        self.ops.reconcile("original", {"exists": True, "resource_id": "resource-one", "proof_ref": "actual.json"})
        self._reopen()
        old_style = self._action_intent()
        old_style.pop("effect_scope")
        before = self.store.db.total_changes
        found = self.ops.lookup("approval-task", "renamed", "alias", old_style)
        self.assertIsNotNone(found)
        self.assertEqual(found["action_id"], "original")
        self.assertEqual(self.store.db.total_changes, before)
        self.assertEqual(found["effect_scope"], {"resources": ["resource-one"]})
        with self.assertRaises(ValueError):
            self.ops.started("original")

    def test_same_operation_id_cannot_change_declared_scope(self):
        intent = self._action_intent()
        self.ops.prepare("approval-task", "draft", "original", intent)
        changed = copy.deepcopy(intent)
        changed["effect_scope"]["resources"] = ["resource-two"]
        with self.assertRaisesRegex(ValueError, "CONFLICT"):
            self.ops.prepare("approval-task", "draft", "original", changed)

    def test_uri_and_opaque_scope_case_changes_cannot_reconcile_unknown_identity(self):
        cases = [("https://local.invalid/Foo", "https://local.invalid/foo"),
                 ("OpaqueResource", "opaqueresource"), ("namespace/Foo", "namespace/foo"),
                 ("https://local.invalid/parent/../Foo", "https://local.invalid/Foo")]
        for index, (original_resource, changed_resource) in enumerate(cases):
            with self.subTest(original=original_resource, current=changed_resource):
                task_id = "case-scope-" + str(index)
                TaskService(self.store).create(contract(task_id))
                intent = self._action_intent()
                intent["effect_scope"]["resources"] = [original_resource]
                original = self.ops.prepare(task_id, "draft", "case-original-" + str(index), intent)
                self.assertEqual(original["effect_scope"], {"resources": [original_resource]})
                self.ops.started(original["action_id"])
                self._reopen()
                changed = copy.deepcopy(intent)
                changed["effect_scope"]["resources"] = [changed_resource]
                before = self.store.db.total_changes
                with self.assertRaisesRegex(ValueError, "CONFLICT"):
                    self.ops.lookup(task_id, "renamed", "case-next-" + str(index), changed)
                self.assertEqual(self.store.db.total_changes, before)
                self.assertEqual(read_record(self.store.db, "operation", original["action_id"])["payload"]["state"], "UNKNOWN")

    def test_explicit_windows_path_case_and_separator_alias_reuses_canonical_operation(self):
        intent = self._action_intent()
        intent["effect_scope"]["resources"] = ["C:\\approved\\Resource"]
        self.ops.prepare("approval-task", "draft", "original", intent)
        self.ops.started("original")
        self._reopen()
        changed = copy.deepcopy(intent)
        changed["effect_scope"]["resources"] = ["c:/approved/resource"]
        found = self.ops.prepare("approval-task", "renamed", "alias", changed)
        self.assertEqual(found["action_id"], "original")
        self.assertEqual(found["state"], "UNKNOWN")
        self.assertFalse(found["new"])

    def test_distinct_uri_paths_remain_independent_with_identical_recipe(self):
        intent = self._action_intent()
        intent["effect_scope"]["resources"] = ["https://local.invalid/resource-one"]
        self.ops.prepare("approval-task", "draft", "original", intent)
        self.ops.started("original")
        self._reopen()
        changed = copy.deepcopy(intent)
        changed["effect_scope"]["resources"] = ["https://local.invalid/resource-two"]
        separate = self.ops.prepare("approval-task", "renamed", "separate", changed)
        self.assertTrue(separate["new"])
        self.assertEqual(separate["action_id"], "separate")

    def test_windows_aliases_parent_resources_and_ambiguous_scopes_stay_conservative(self):
        cases = [("C:\\approved\\Resource", "c:/approved/resource"),
                 ("resources", "resources/child"), (".", "resource-two"),
                 ("resource-*", "resource-two"), ("resources/../resource-one", "resource-one"),
                 ("resource-one.", "resource-one"), ("resource-one ", "resource-one"),
                 ("C:resource-one", "C:/approved/resource-one"),
                 ("//./C:/approved/resource-one", "C:/approved/resource-one"),
                 ("C:/approved/resource-one", "resource-one")]
        for index, (original_resource, next_resource) in enumerate(cases):
            with self.subTest(original=original_resource, current=next_resource):
                task_id = "scope-" + str(index)
                TaskService(self.store).create(contract(task_id))
                intent = self._action_intent()
                intent["effect_scope"]["resources"] = [original_resource]
                self.ops.prepare(task_id, "draft", "original-" + str(index), intent)
                self.ops.started("original-" + str(index))
                changed = copy.deepcopy(intent)
                changed["action"]["argv"].append("harmless-spelling-change")
                changed["effect_scope"]["resources"] = [next_resource]
                before = self.store.db.total_changes
                with self.assertRaisesRegex(ValueError, "CONFLICT"):
                    self.ops.lookup(task_id, "other-stage", "separate-" + str(index), changed)
                self.assertEqual(self.store.db.total_changes, before)

    def test_terminal_observer_claim_without_submission_binding_remains_unknown(self):
        self.ops.started("create")
        for provider in (None, "observer-only-attempt"):
            observation = {"exists": False, "absence_confirmed": True, "proof_ref": "fresh-proof.json"}
            if provider is not None:
                observation["provider_attempt_id"] = provider
            self.assertEqual(self.ops.reconcile("create", observation)["state"], "UNKNOWN")
            with self.assertRaises(ValueError):
                self.ops.started("create")

    def test_new_log_path_cannot_make_old_provider_attempt_current_after_restart(self):
        self.ops.started("create")
        self.ops.record_submission("create", self._submission("request-one"))
        first = {"exists": False, "absence_confirmed": True,
                 "provider_attempt_id": "request-one", "proof_ref": "uuid-one.log#same-provider-bytes"}
        self.assertEqual(self.ops.reconcile("create", first)["state"], "ABSENT")
        self.ops.started("create")
        self.ops.record_submission("create", self._submission("request-two", name="second.json"))
        for proof in ("uuid-two.log#same-provider-bytes", "uuid-three.log#same-provider-bytes"):
            self._reopen()
            stale = dict(first, proof_ref=proof)
            self.assertEqual(self.ops.reconcile("create", stale)["state"], "UNKNOWN")
            with self.assertRaises(ValueError):
                self.ops.started("create")

    def test_current_execution_binding_allows_only_current_terminal_absence(self):
        self.ops.started("create")
        self.ops.record_submission("create", self._submission("request-one", exit_code=9))
        self._reopen()
        result = self.ops.reconcile("create", {"exists": False, "absence_confirmed": True,
            "provider_attempt_id": "request-one", "proof_ref": "terminal.json"})
        self.assertEqual(result["state"], "ABSENT")
        self.assertEqual(self.ops.started("create")["state"], "UNKNOWN")

    def test_submission_receipt_bytes_must_match_recorded_hash(self):
        self.ops.started("create")
        receipt = self._submission("request-one")
        Path(receipt["log"]).write_text('{"provider_attempt_id":"other"}', encoding="utf-8")
        before = self._record()
        with self.assertRaises(ValueError):
            self.ops.record_submission("create", receipt)
        self.assertEqual(self._record(), before)

    def test_submission_cannot_replace_a_current_provider_attempt_binding(self):
        self.ops.started("create")
        receipt = self._submission("request-one")
        self.ops.record_submission("create", receipt)
        self.ops.record_submission("create", receipt)
        before = self._record()
        with self.assertRaises(ValueError):
            self.ops.record_submission("create", self._submission("request-two", name="conflict.json"))
        self.assertEqual(self._record(), before)

    def test_provider_attempt_identity_cannot_be_reused_for_a_later_submission(self):
        self.ops.started("create")
        self.ops.record_submission("create", self._submission("request-one"))
        self.ops.reconcile("create", {"exists": False, "absence_confirmed": True,
            "provider_attempt_id": "request-one", "proof_ref": "terminal.json"})
        self.ops.started("create")
        self._reopen()
        with self.assertRaises(ValueError):
            self.ops.record_submission("create", self._submission("request-one", name="new-local-log.json"))
        self.assertEqual(self._record()["payload"]["state"], "UNKNOWN")

    def test_unstructured_submission_output_can_still_reconcile_actual_application(self):
        self.ops.started("create")
        receipt = self._submission("unused")
        Path(receipt["log"]).write_text("Provisioning finished", encoding="utf-8")
        receipt["log_sha256"] = hashlib.sha256(Path(receipt["log"]).read_bytes()).hexdigest()
        self.assertEqual(self.ops.record_submission("create", receipt)["state"], "UNKNOWN")
        result = self.ops.reconcile("create", {"exists": True, "resource_id": "actual-resource",
                                               "proof_ref": "actual-visible-state.json"})
        self.assertEqual(result["state"], "APPLIED")

    def test_legacy_path_only_terminal_proof_cannot_authorize_another_attempt(self):
        record = self._record()
        previous = dict(record["payload"], state="ABSENT", ever_attempted=True,
                        nonapplication_proof_ref="old-uuid-terminal.log#hash")
        with transaction(self.store.db) as db:
            write_record(db, "operation", "create", previous, expected_revision=record["revision"])
        self._reopen()
        with self.assertRaises(ValueError):
            self.ops.started("create")
        result = self.ops.reconcile("create", {"exists": False,
            "proof_ref": "fresh-uuid-terminal.log#hash"})
        self.assertEqual(result["state"], "UNKNOWN")

    def test_provider_attempt_identity_must_be_actual_nonsecret_text(self):
        self.ops.started("create")
        for provider in ("", " ", 1, True, {}, "bad\0id", "[REDACTED]"):
            with self.subTest(provider=repr(provider)):
                before = self._record()
                with self.assertRaises(ValueError):
                    self.ops.record_submission("create", self._submission(provider))
                self.assertEqual(self._record(), before)

    def test_initial_absence_allows_the_first_attempt(self):
        result = self.ops.reconcile("create", {"exists": False, "proof_ref": "initial-probe.json"})
        self.assertEqual(result["state"], "ABSENT")
        self.assertEqual(self.ops.started("create")["state"], "UNKNOWN")

    def test_delayed_visibility_after_start_remains_unknown(self):
        self.ops.started("create")
        for probe in ("delayed-probe.json", "still-delayed-probe.json"):
            result = self.ops.reconcile("create", {"exists": False, "proof_ref": probe})
            self.assertEqual(result["state"], "UNKNOWN")

    def test_started_refuses_unresolved_replay_without_changing_the_record(self):
        self.ops.started("create")
        before = self._record()
        with self.assertRaisesRegex(ValueError, "UNKNOWN|[Uu]nresolved"):
            self.ops.started("create")
        self.assertEqual(self._record(), before)

    def test_terminal_nonapplication_allows_one_new_attempt(self):
        self.ops.started("create")
        self.ops.record_submission("create", self._submission("request-one"))
        result = self.ops.reconcile("create", {"exists": False, "absence_confirmed": True,
                                               "provider_attempt_id": "request-one",
                                               "proof_ref": "provider-terminal-rejection.json"})
        self.assertEqual(result["state"], "ABSENT")
        self._reopen()
        result = self.ops.reconcile("create", {"exists": False, "proof_ref": "pre-retry-probe.json"})
        self.assertEqual(result["state"], "ABSENT")
        self.ops.started("create")
        result = self.ops.reconcile("create", {"exists": False, "proof_ref": "retry-not-visible.json"})
        self.assertEqual(result["state"], "UNKNOWN")
        with self.assertRaises(ValueError):
            self.ops.started("create")

    def test_consumed_terminal_proof_cannot_resolve_a_later_attempt(self):
        self.ops.started("create")
        self.ops.record_submission("create", self._submission("request-one"))
        rejected = {"exists": False, "absence_confirmed": True, "provider_attempt_id": "request-one",
                    "proof_ref": "first-terminal-rejection.json"}
        self.ops.reconcile("create", rejected)
        self.ops.reconcile("create", rejected)
        self.ops.started("create")
        self.ops.record_submission("create", self._submission("request-two", name="second.json"))
        self._reopen()
        self.assertEqual(self.ops.reconcile("create", rejected)["state"], "UNKNOWN")
        self.assertEqual(self._record()["payload"]["attempt_number"], 2)
        with self.assertRaises(ValueError):
            self.ops.started("create")
        self.ops.reconcile("create", {"exists": False, "absence_confirmed": True,
                                      "provider_attempt_id": "request-two",
                                      "proof_ref": "second-terminal-rejection.json"})
        self.ops.started("create")
        self._reopen()
        self.assertEqual(self.ops.reconcile("create", rejected)["state"], "UNKNOWN")

    def test_all_terminal_proofs_for_an_old_attempt_are_consumed(self):
        self.ops.started("create")
        self.ops.record_submission("create", self._submission("request-one"))
        first = {"exists": False, "absence_confirmed": True, "provider_attempt_id": "request-one",
                 "proof_ref": "terminal-status.json"}
        self.ops.reconcile("create", first)
        self.ops.reconcile("create", {"exists": False, "absence_confirmed": True,
                                      "provider_attempt_id": "request-one",
                                      "proof_ref": "second-read-of-terminal-status.json"})
        self.ops.started("create")
        self._reopen()
        self.assertEqual(self.ops.reconcile("create", first)["state"], "UNKNOWN")
        with self.assertRaises(ValueError):
            self.ops.started("create")

    def test_restart_keeps_delayed_visibility_unresolved_then_finds_the_effect(self):
        self.ops.started("create")
        self._reopen()
        prepared = self.ops.prepare("approval-task", "draft", "create", self.intent)
        self.assertFalse(prepared["new"])
        result = self.ops.reconcile("create", {"exists": False, "proof_ref": "lagging-read.json"})
        self.assertEqual(result["state"], "UNKNOWN")
        with self.assertRaises(ValueError):
            self.ops.started("create")
        applied = self.ops.reconcile("create", {"exists": True, "resource_id": "resource-1",
                                               "proof_ref": "visible-resource.json"})
        self.assertEqual(applied["state"], "APPLIED")
        self.assertEqual(applied["observation"]["resource_id"], "resource-1")
        self._reopen()
        with self.assertRaises(ValueError):
            self.ops.started("create")

    def test_known_application_refuses_duplicate_start(self):
        self.ops.reconcile("create", {"exists": True, "resource_id": "resource-1", "proof_ref": "found.json"})
        before = self._record()
        with self.assertRaisesRegex(ValueError, "APPLIED|[Aa]pplied"):
            self.ops.started("create")
        self.assertEqual(self._record(), before)

    def test_later_disappearance_cannot_authorize_recreation(self):
        self.ops.started("create")
        self.ops.reconcile("create", {"exists": True, "resource_id": "resource-1", "proof_ref": "created.json"})
        missing = self.ops.reconcile("create", {"exists": False, "proof_ref": "later-missing.json"})
        self.assertEqual(missing["state"], "UNKNOWN")
        self._reopen()
        before = self._record()
        with self.assertRaises(ValueError):
            self.ops.reconcile("create", {"exists": False, "absence_confirmed": True,
                                          "proof_ref": "contradictory-nonapplication.json"})
        self.assertEqual(self._record(), before)
        with self.assertRaises(ValueError):
            self.ops.started("create")

    def test_incompatible_presence_and_terminal_nonapplication_are_rejected(self):
        self.ops.started("create")
        before = self._record()
        with self.assertRaises(ValueError):
            self.ops.reconcile("create", {"exists": True, "resource_id": "resource-1",
                                          "absence_confirmed": True, "proof_ref": "contradiction.json"})
        self.assertEqual(self._record(), before)

    def test_confirmation_flag_requires_an_actual_boolean(self):
        self.ops.started("create")
        before = self._record()
        for flag in (None, 0, 1, "true", [], {}):
            with self.subTest(flag=flag):
                with self.assertRaises(ValueError):
                    self.ops.reconcile("create", {"exists": False, "absence_confirmed": flag,
                                                  "proof_ref": "untyped-claim.json"})
                self.assertEqual(self._record(), before)

    def test_explicit_false_confirmation_does_not_resolve_uncertainty(self):
        self.ops.started("create")
        result = self.ops.reconcile("create", {"exists": False, "absence_confirmed": False,
                                               "proof_ref": "nonterminal-status.json"})
        self.assertEqual(result["state"], "UNKNOWN")

    def test_nonempty_valid_resource_id_is_required_for_application(self):
        self.ops.started("create")
        before = self._record()
        for resource in ("", " ", 1, True, {}, "bad\0id", "[REDACTED]", "x" * 20001):
            with self.subTest(resource=repr(resource)[:80]):
                with self.assertRaises(ValueError):
                    self.ops.reconcile("create", {"exists": True, "resource_id": resource,
                                                  "proof_ref": "bad-resource.json"})
                self.assertEqual(self._record(), before)

    def test_terminal_nonapplication_requires_a_valid_proof_reference(self):
        self.ops.started("create")
        before = self._record()
        for proof in (None, "", " ", 1, "bad\0ref", "x" * 20001):
            with self.subTest(proof=repr(proof)[:80]):
                with self.assertRaises(ValueError):
                    self.ops.reconcile("create", {"exists": False, "absence_confirmed": True,
                                                  "proof_ref": proof})
                self.assertEqual(self._record(), before)

    def test_failed_observation_does_not_clear_an_attempt_on_restart(self):
        self.ops.started("create")
        with self.assertRaises(ValueError):
            self.ops.reconcile("create", {"exists": False})
        self._reopen()
        with self.assertRaises(ValueError):
            self.ops.started("create")

    def test_legacy_unknown_record_does_not_gain_replay_permission(self):
        record = self._record()
        with transaction(self.store.db) as db:
            write_record(db, "operation", "create", dict(record["payload"], state="UNKNOWN"),
                         expected_revision=record["revision"])
        self._reopen()
        result = self.ops.reconcile("create", {"exists": False, "proof_ref": "legacy-lagging-read.json"})
        self.assertEqual(result["state"], "UNKNOWN")
        with self.assertRaises(ValueError):
            self.ops.started("create")

    def test_legacy_applied_record_retains_application_history(self):
        record = self._record()
        with transaction(self.store.db) as db:
            write_record(db, "operation", "create", dict(record["payload"], state="APPLIED",
                         observation={"exists": True, "resource_id": "resource-1", "proof_ref": "old-receipt.json"}),
                         expected_revision=record["revision"])
        missing = self.ops.reconcile("create", {"exists": False, "proof_ref": "legacy-missing.json"})
        self.assertEqual(missing["state"], "UNKNOWN")
        self._reopen()
        with self.assertRaises(ValueError):
            self.ops.reconcile("create", {"exists": False, "absence_confirmed": True,
                                          "proof_ref": "impossible-terminal-claim.json"})

    def test_legacy_absence_without_attempt_binding_stays_unknown(self):
        record = self._record()
        value = {key: item for key, item in record["payload"].items()
                 if key not in {"ever_attempted", "ever_applied"}}
        value.update(state="ABSENT", observation={"exists": False, "proof_ref": "old-missing.json"})
        with transaction(self.store.db) as db:
            write_record(db, "operation", "create", value, expected_revision=record["revision"])
        self._reopen()
        with self.assertRaises(ValueError):
            self.ops.started("create")
        result = self.ops.reconcile("create", {"exists": False, "proof_ref": "fresh-missing.json"})
        self.assertEqual(result["state"], "UNKNOWN")
        result = self.ops.reconcile("create", {"exists": False, "absence_confirmed": True,
            "provider_attempt_id": "observer-only-attempt", "proof_ref": "terminal-rejection.json"})
        self.assertEqual(result["state"], "UNKNOWN")
        with self.assertRaises(ValueError):
            self.ops.started("create")


class OperationEffectScopeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ase-effect-scope-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.repo = self.base / "repo"
        self.repo.mkdir()
        self._open()
        stages = []
        for stage_id, target in (("first", "resource-one"), ("second", "resource-two")):
            stages.append({"id": stage_id, "acceptance_ids": ["RESOURCE"],
                "scope": {"resources": [target], "effects": ["command"]},
                "actions": [{"id": "create", "argv": [sys.executable, "-c",
                    "from pathlib import Path;p=Path(" + repr(target) + ");"
                    "p.write_text((p.read_text() if p.exists() else '')+'x')"],
                    "observation_argv": [sys.executable, "-c",
                        "import json;print(json.dumps({'exists':False}))"]}]})
        value = {"id": "effects", "schema_version": 2, "profile": "infrastructure", "risk": "LOW",
            "request": "Create independent approved disposable resources once",
            "scope": {"resources": ["resource-one", "resource-two"], "effects": ["command"]},
            "acceptance": [{"id": "RESOURCE", "expected": "The resource is created once", "evidence_ids": ["resource"]}],
            "evidence": [{"id": "resource", "kind": "artifact", "path": "resource-one"}],
            "stages": stages, "unknowns": [], "assumptions": [], "sources": []}
        self._approve(self.tasks.create(value))

    def _open(self):
        self.store = Store(self.repo, self.base / "state")
        self.addCleanup(self.store.close)
        self.tasks = TaskService(self.store)
        self.controller = WorkflowController(self.store)

    def _reopen(self):
        self.store.close()
        self._open()

    def _approve(self, task):
        task = self.tasks.propose("effects", {"summary": "Create each approved resource once",
            "stage_ids": [stage["id"] for stage in task["contract"]["stages"]]}, expected_revision=task["revision"])
        self.tasks.approve("effects", proposal_hash=task["proposal_hash"],
            approved_stage_ids=task["proposal"]["stage_ids"],
            user_message_ref=f"user:controlled-resources:{task['revision']}")

    def _execute(self, index=0):
        stage = self.tasks.snapshot("effects")["contract"]["stages"][index]
        return self.controller.execute_actions("effects", stage)

    def _first_unknown(self):
        with self.assertRaisesRegex(ValueError, "postcondition"):
            self._execute()
        self.assertEqual((self.repo / "resource-one").read_text(), "x")
        self.assertEqual(self._operation()["state"], "UNKNOWN")
        self._reopen()

    def _operation(self):
        key = hash_value({"task": "effects", "stage": "first", "action": "create"})
        return read_record(self.store.db, "operation", key)["payload"]

    def _revise_recipe(self, *, action_id="create"):
        task = self.tasks.snapshot("effects")
        value = copy.deepcopy(task["contract"])
        value["stages"][0]["id"] = "renamed"
        action = value["stages"][0]["actions"][0]
        action["id"] = action_id
        action["argv"][-1] += "; # harmless recipe spelling change"
        self._approve(self.tasks.revise("effects", value, expected_revision=task["revision"]))

    def test_stage_rename_and_comment_cannot_replay_unknown_effect_after_reopen(self):
        self._first_unknown()
        self._revise_recipe()
        with self.assertRaisesRegex(ValueError, "CONFLICT"):
            self._execute()
        self.assertEqual((self.repo / "resource-one").read_text(), "x")
        self.assertEqual(self._operation()["state"], "UNKNOWN")
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM runtime_records WHERE kind='operation'").fetchone()[0], 1)

    def test_uri_path_case_change_cannot_falsely_apply_then_replay_original_target(self):
        upper, lower = "https://local.invalid/Foo", "https://local.invalid/foo"
        counts = self.repo / "counts.json"
        counts.write_text(json.dumps({"Foo": 0, "foo": 1}), encoding="utf-8")
        target = self.repo / "target.txt"
        target.write_text(upper, encoding="utf-8")
        task = self.tasks.snapshot("effects")
        spec = copy.deepcopy(task["contract"])
        spec["scope"] = {"resources": [upper, lower], "effects": ["command"]}
        stage = spec["stages"][0]
        stage["scope"] = {"resources": [upper], "effects": ["command"]}
        stage["actions"][0]["argv"] = [sys.executable, "-c",
            "from pathlib import Path;import json;p=Path('counts.json');counts=json.loads(p.read_text());"
            "key=Path('target.txt').read_text().rsplit('/',1)[-1];counts[key]+=1;p.write_text(json.dumps(counts))"]
        stage["actions"][0]["observation_argv"] = [sys.executable, "-c",
            "from pathlib import Path;import json;target=Path('target.txt').read_text();"
            "key=target.rsplit('/',1)[-1];counts=json.loads(Path('counts.json').read_text());"
            "print(json.dumps({'exists':key=='foo' and counts[key]>0,'resource_id':target}))"]
        spec["stages"] = [stage]
        spec["evidence"][0]["path"] = "counts.json"
        self._approve(self.tasks.revise("effects", spec, expected_revision=task["revision"]))
        with self.assertRaisesRegex(ValueError, "postcondition"):
            self._execute()
        self.assertEqual(json.loads(counts.read_text()), {"Foo": 1, "foo": 1})
        self.assertEqual(self._operation()["state"], "UNKNOWN")
        self._reopen()

        def change_scope(stage_id, resource, *, comment=False):
            task = self.tasks.snapshot("effects")
            spec = copy.deepcopy(task["contract"])
            spec["stages"][0]["id"] = stage_id
            spec["stages"][0]["scope"]["resources"] = [resource]
            if comment:
                spec["stages"][0]["actions"][0]["argv"][-1] += "; # equivalent recipe"
            self._approve(self.tasks.revise("effects", spec, expected_revision=task["revision"]))
            target.write_text(resource, encoding="utf-8")

        change_scope("lower-case", lower)
        second_error = None
        try:
            self._execute()
        except ValueError as error:
            second_error = str(error)
        self._reopen()
        change_scope("upper-again", upper, comment=True)
        third_error = None
        try:
            self._execute()
        except ValueError as error:
            third_error = str(error)
        self.assertEqual(json.loads(counts.read_text()), {"Foo": 1, "foo": 1})
        self.assertEqual(self._operation()["state"], "UNKNOWN")
        self.assertRegex(second_error or "", "CONFLICT")
        self.assertRegex(third_error or "", "CONFLICT")
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM runtime_records WHERE kind='operation'").fetchone()[0], 1)

    def test_action_relabel_and_comment_cannot_replay_unknown_same_target(self):
        self._first_unknown()
        self._revise_recipe(action_id="renamed-action")
        with self.assertRaisesRegex(ValueError, "CONFLICT"):
            self._execute()
        self.assertEqual((self.repo / "resource-one").read_text(), "x")
        self.assertEqual(self._operation()["state"], "UNKNOWN")

    def test_independently_approved_disjoint_target_can_execute_while_first_unknown(self):
        self._first_unknown()
        with self.assertRaisesRegex(ValueError, "postcondition"):
            self._execute(1)
        self.assertEqual((self.repo / "resource-one").read_text(), "x")
        self.assertEqual((self.repo / "resource-two").read_text(), "x")
        self.assertEqual(self._operation()["state"], "UNKNOWN")

    def test_legacy_unknown_without_durable_scope_cannot_claim_disjoint_target(self):
        self._first_unknown()
        key = hash_value({"task": "effects", "stage": "first", "action": "create"})
        record = read_record(self.store.db, "operation", key)
        value = copy.deepcopy(record["payload"])
        value.pop("effect_scope", None)
        value["intent"].pop("effect_scope", None)
        with transaction(self.store.db) as db:
            write_record(db, "operation", key, value, expected_revision=record["revision"])
        self._reopen()
        with self.assertRaisesRegex(ValueError, "CONFLICT"):
            self._execute(1)
        self.assertFalse((self.repo / "resource-two").exists())
        self.assertEqual((self.repo / "resource-one").read_text(), "x")

    def test_distinct_known_applied_action_can_intentionally_repeat_target(self):
        self._first_unknown()
        OperationLedger(self.store).reconcile(self._operation()["action_id"],
            {"exists": True, "resource_id": "resource-one", "proof_ref": str(self.repo / "resource-one")})
        self._reopen()
        self._revise_recipe(action_id="intentional-second-action")
        with self.assertRaisesRegex(ValueError, "postcondition"):
            self._execute()
        self.assertEqual((self.repo / "resource-one").read_text(), "xx")
        self.assertEqual(self._operation()["state"], "APPLIED")

    def test_applied_logical_action_cannot_reset_history_with_stage_and_recipe_alias(self):
        task = self.tasks.snapshot("effects")
        spec = copy.deepcopy(task["contract"])
        spec["stages"][0]["actions"][0]["observation_argv"] = [sys.executable, "-c",
            "from pathlib import Path;import json;p=Path('resource-one');"
            "print(json.dumps({'exists':p.exists(),'resource_id':'resource-one'}))"]
        self._approve(self.tasks.revise("effects", spec, expected_revision=task["revision"]))
        self._execute()
        self.assertEqual((self.repo / "resource-one").read_text(), "x")
        self.assertEqual(self._operation()["state"], "APPLIED")
        self._reopen()
        task = self.tasks.snapshot("effects")
        spec = copy.deepcopy(task["contract"])
        spec["stages"][0]["id"] = "renamed"
        action = spec["stages"][0]["actions"][0]
        action["argv"][-1] += "; # harmless recipe spelling change"
        action["observation_argv"] = [sys.executable, "-c", "import json;print(json.dumps({'exists':False}))"]
        self._approve(self.tasks.revise("effects", spec, expected_revision=task["revision"]))
        error = None
        try:
            self._execute()
        except ValueError as failure:
            error = str(failure)
        self.assertEqual((self.repo / "resource-one").read_text(), "x")
        self.assertRegex(error or "", "CONFLICT")
        self.assertEqual(self._operation()["state"], "APPLIED")
        self.assertTrue(self._operation()["ever_applied"])
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM runtime_records WHERE kind='operation'").fetchone()[0], 1)

    def test_applied_logical_action_cannot_reset_history_with_overlapping_scope(self):
        for resource, comment in (("context", True), ("context", False), ("RESOURCE-ONE", True), (".", True)):
            with self.subTest(added_resource=resource, changed_recipe=comment):
                # Independent disposable roots keep both concrete cases isolated.
                case = OperationEffectScopeTests()
                case.setUp()
                self.addCleanup(case.doCleanups)
                task = case.tasks.snapshot("effects")
                spec = copy.deepcopy(task["contract"])
                spec["scope"]["resources"].append(resource)
                spec["stages"][0]["actions"][0]["observation_argv"] = [sys.executable, "-c",
                    "from pathlib import Path;import json;p=Path('resource-one');"
                    "print(json.dumps({'exists':p.exists(),'resource_id':'resource-one'}))"]
                case._approve(case.tasks.revise("effects", spec, expected_revision=task["revision"]))
                case._execute()
                self.assertEqual((case.repo / "resource-one").read_text(), "x")
                self.assertEqual(case._operation()["state"], "APPLIED")
                case._reopen()
                task = case.tasks.snapshot("effects")
                spec = copy.deepcopy(task["contract"])
                spec["stages"][0]["id"] = "renamed"
                spec["stages"][0]["scope"]["resources"] = (["resource-one", resource]
                    if resource == "context" else [resource])
                action = spec["stages"][0]["actions"][0]
                if comment:
                    action["argv"][-1] += "; # harmless recipe spelling change"
                action["observation_argv"] = [sys.executable, "-c", "import json;print(json.dumps({'exists':False}))"]
                case._approve(case.tasks.revise("effects", spec, expected_revision=task["revision"]))
                error = None
                try:
                    case._execute()
                except ValueError as failure:
                    error = str(failure)
                self.assertEqual((case.repo / "resource-one").read_text(), "x")
                self.assertRegex(error or "", "CONFLICT")
                self.assertEqual(case._operation()["state"], "APPLIED")
                self.assertTrue(case._operation()["ever_applied"])
                self.assertEqual(case.store.db.execute("SELECT COUNT(*) FROM runtime_records WHERE kind='operation'").fetchone()[0], 1)



if __name__ == "__main__":
    unittest.main()
