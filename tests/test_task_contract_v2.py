"""Approval, partial scope and revision behavior against real SQLite state."""
import copy
from pathlib import Path
import tempfile
import unittest

from ase.store import Store
from ase.tasks import TaskService
from ase.workflow import WorkflowController
from ase.state import read_record
import json


def contract(task_id="approval-task"):
    return {"id": task_id, "schema_version": 2, "profile": "document",
            "request": "Prepare a sourced operating guide and publish only after access is clarified.",
            "scope": {"resources": ["guide.md"], "effects": ["file_write"]},
            "acceptance": [{"id": "AC1", "expected": "The guide documents the confirmed workflow.",
                            "mandatory": True, "evidence_ids": ["guide"]}],
            "evidence": [{"id": "guide", "kind": "artifact", "path": "guide.md", "required": True}],
            "stages": [{"id": "draft", "scope": {"resources": ["guide.md"], "effects": ["file_write"]},
                        "acceptance_ids": ["AC1"], "depends_on": []},
                       {"id": "publish", "scope": {"resources": ["guide.md"], "effects": []},
                        "acceptance_ids": [], "depends_on": ["draft"], "unknown_ids": ["access"]}],
            "unknowns": [{"id": "access", "statement": "Publishing destination is not confirmed.",
                          "blocking_stage_ids": ["publish"], "status": "UNRESOLVED"}]}


class TaskContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ase-v2-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "work"
        self.repo.mkdir()
        self.store = Store(self.repo, self.root / "state")
        self.addCleanup(self.store.close)
        self.tasks = TaskService(self.store)

    def proposed(self):
        task = self.tasks.create(contract())
        return self.tasks.propose(task["task_id"], {"summary": "Draft the guide first; keep publishing deferred.",
                                                  "stage_ids": ["draft"]}, expected_revision=task["revision"])

    def two_stage_proposal(self):
        value = contract()
        del value["stages"][1]["unknown_ids"]
        value["unknowns"] = []
        task = self.tasks.create(value)
        return self.tasks.propose(task["task_id"], {"summary": "Draft and publish the guide in separate stages.",
            "stage_ids": ["draft", "publish"]}, expected_revision=task["revision"])

    def test_separate_approvals_preserve_stage_authority_and_retry(self):
        from ase.action_evidence import _original_authority, _stage_context
        from ase.state import hash_value
        from ase.store import now

        task = self.two_stage_proposal()
        task_id = task["task_id"]
        draft = dict(proposal_hash=task["proposal_hash"], user_message_ref="chat:draft", approved_stage_ids=["draft"])
        approved = self.tasks.approve(task_id, **draft)
        self.tasks.transition(task_id, "EXECUTING", expected_revision=approved["revision"], reason="Draft in progress")
        first = read_record(self.store.db, "approval", task_id)
        controller = WorkflowController(self.store)
        cancelled, _ = controller._command_control(task_id, task["contract"]["evidence"][0], "draft")
        with self.assertRaisesRegex(ValueError, "approval"):
            self.tasks.require_approval(task_id, "publish")

        both = self.tasks.approve(task_id, proposal_hash=task["proposal_hash"],
                                  user_message_ref="chat:publish", approved_stage_ids=["publish"])
        self.assertEqual(both["approved_stage_ids"], ["draft", "publish"])
        self.assertEqual(both["state"], "EXECUTING")
        self.assertFalse(cancelled())
        metadata, _ = _stage_context(self.store, both, both["contract"]["stages"][0])
        self.assertEqual(metadata["approval_record_revision"], first["revision"])
        self.assertEqual(metadata["approval_record_sha256"], hash_value(first["payload"]))
        for stage_id, revision in (("draft", first["revision"]), ("publish", first["revision"] + 1)):
            self.assertEqual(self.tasks.require_approval(task_id, stage_id), both)
            _, _, metadata = _original_authority(self.store, task_id, stage_id, task["proposal_hash"], before=now())
            self.assertEqual(metadata["approval_version_key"], f"{task_id}:{revision}")
        last = read_record(self.store.db, "approval", task_id)
        self.assertEqual(self.tasks.approve(task_id, **draft), both)
        self.assertEqual(read_record(self.store.db, "approval", task_id), last)
        with self.assertRaisesRegex(ValueError, "different proposal or stage grant"):
            self.tasks.approve(task_id, proposal_hash=task["proposal_hash"],
                               user_message_ref="chat:publish", approved_stage_ids=["draft", "publish"])
        publish_authority = controller._authority_binding(task_id, "publish")
        renewed = self.tasks.approve(task_id, **{**draft, "user_message_ref": "chat:renew-draft"})
        self.assertEqual(renewed["approved_stage_ids"], ["draft", "publish"])
        self.assertEqual(controller._authority_binding(task_id, "publish"), publish_authority)
        self.assertTrue(cancelled())

    def test_approval_of_parent_sibling_preserves_child_authority(self):
        parent = self.two_stage_proposal()
        parent_id = parent["task_id"]
        self.tasks.approve(parent_id, proposal_hash=parent["proposal_hash"],
                           user_message_ref="chat:parent-draft", approved_stage_ids=["draft"])
        value = contract("child-draft")
        value.update(parent_task_id=parent_id, parent_stage_id="draft", unknowns=[])
        value["stages"] = value["stages"][:1]
        value["stages"][0]["id"] = "outline"
        child = self.tasks.create(value)
        controller = WorkflowController(self.store)
        authority = controller._stage_authority(child, child["contract"]["stages"][0])

        self.tasks.approve(parent_id, proposal_hash=parent["proposal_hash"],
                           user_message_ref="chat:parent-publish", approved_stage_ids=["publish"])
        self.assertEqual(controller._stage_authority(child, child["contract"]["stages"][0]), authority)

    def test_changed_stage_and_task_scope_reset_all_prior_grants(self):
        task = self.two_stage_proposal()
        task_id = task["task_id"]
        for stage_id in ("draft", "publish"):
            task = self.tasks.approve(task_id, proposal_hash=task["proposal_hash"],
                                      user_message_ref="chat:" + stage_id, approved_stage_ids=[stage_id])
        controller = WorkflowController(self.store)
        cancelled, _ = controller._command_control(task_id, task["contract"]["evidence"][0], "draft")
        changed = copy.deepcopy(task["contract"])
        changed["scope"]["resources"].append("release-notes.md")
        changed["stages"][1]["scope"]["resources"].append("release-notes.md")
        revised = self.tasks.revise(task_id, changed, expected_revision=task["revision"])
        self.assertEqual(revised["approved_stage_ids"], [])
        self.assertTrue(cancelled())
        replacement = self.tasks.propose(task_id, task["proposal"], expected_revision=revised["revision"])
        self.assertNotEqual(replacement["proposal_hash"], task["proposal_hash"])
        with self.assertRaisesRegex(ValueError, "hash"):
            self.tasks.approve(task_id, proposal_hash=task["proposal_hash"],
                               user_message_ref="chat:old-scope", approved_stage_ids=["draft"])
        with self.assertRaisesRegex(ValueError, "different proposal or stage grant"):
            self.tasks.approve(task_id, proposal_hash=replacement["proposal_hash"],
                               user_message_ref="chat:publish", approved_stage_ids=["publish"])
        approved = self.tasks.approve(task_id, proposal_hash=replacement["proposal_hash"],
                                      user_message_ref="chat:new-publish", approved_stage_ids=["publish"])
        self.assertEqual(approved["approved_stage_ids"], ["publish"])
        with self.assertRaisesRegex(ValueError, "approval"):
            self.tasks.require_approval(task_id, "draft")
        self.assertEqual(self.tasks.require_approval(task_id, "publish"), approved)

    def test_execution_requires_exact_proposal(self):
        task = self.proposed()
        with self.assertRaisesRegex(ValueError, "approval"):
            self.tasks.require_approval(task["task_id"], "draft")
        with self.assertRaisesRegex(ValueError, "hash"):
            self.tasks.approve(task["task_id"], proposal_hash="0" * 64,
                               user_message_ref="chat:user:approval-1", approved_stage_ids=["draft"])
        self.tasks.approve(task["task_id"], proposal_hash=task["proposal_hash"],
                           user_message_ref="chat:user:approval-1", approved_stage_ids=["draft"])
        self.assertEqual(self.tasks.require_approval(task["task_id"], "draft")["state"], "READY")
        with self.assertRaisesRegex(ValueError, "approval|unknown|scope"):
            self.tasks.require_approval(task["task_id"], "publish")

    def test_identical_proposals_on_two_tasks_have_distinct_hashes(self):
        plan = {"summary": "Draft the guide first; keep publishing deferred.", "stage_ids": ["draft"]}
        first, second = (self.tasks.create(contract(name)) for name in ("task-one", "task-two"))
        self.assertEqual(first["contract_hash"], second["contract_hash"])
        first = self.tasks.propose("task-one", plan, expected_revision=first["revision"])
        second = self.tasks.propose("task-two", plan, expected_revision=second["revision"])
        self.assertNotEqual(first["proposal_hash"], second["proposal_hash"])
        with self.assertRaisesRegex(ValueError, "hash"):
            self.tasks.approve("task-two", proposal_hash=first["proposal_hash"],
                               user_message_ref="chat:user:approval-1", approved_stage_ids=["draft"])
        with self.assertRaisesRegex(ValueError, "another task"):
            self.tasks.propose("task-two", first["proposal"], expected_revision=second["revision"])
        self.tasks.approve("task-two", proposal_hash=second["proposal_hash"],
                           user_message_ref="chat:user:approval-2", approved_stage_ids=["draft"])

    def test_stored_proposal_from_before_task_binding_still_approves(self):
        from ase.state import hash_value, transaction, write_record
        task = self.proposed()
        legacy = {k: v for k, v in task["proposal"].items() if k != "task_id"}
        record = read_record(self.store.db, "task", task["task_id"])
        payload = {**record["payload"], "proposal": legacy,
                   "proposal_hash": hash_value({"contract_hash": task["contract_hash"], "proposal": legacy})}
        with transaction(self.store.db) as db:
            write_record(db, "task", task["task_id"], payload, expected_revision=record["revision"])
        self.tasks.approve(task["task_id"], proposal_hash=payload["proposal_hash"],
                           user_message_ref="chat:user:approval-1", approved_stage_ids=["draft"])
        self.assertEqual(self.tasks.require_approval(task["task_id"], "draft")["state"], "READY")

    def test_changed_material_proposal_invalidates_old_approval(self):
        task = self.proposed()
        task = self.tasks.approve(task["task_id"], proposal_hash=task["proposal_hash"],
                                  user_message_ref="chat:user:approval-1", approved_stage_ids=["draft"])
        changed = self.tasks.propose(task["task_id"], {"summary": "Replace the guide with a migration policy.",
                                                       "stage_ids": ["draft"]}, expected_revision=task["revision"])
        self.assertNotEqual(changed["proposal_hash"], task["proposal_hash"])
        with self.assertRaisesRegex(ValueError, "approval"):
            self.tasks.require_approval(task["task_id"], "draft")

    def test_replaying_a_replaced_proposal_never_revives_stored_approval(self):
        task = self.proposed()
        approved = self.tasks.approve(task["task_id"], proposal_hash=task["proposal_hash"],
                                      user_message_ref="chat:approval", approved_stage_ids=["draft"])
        replaced = self.tasks.propose(task["task_id"], {"summary": "Change the permitted result", "stage_ids": ["draft"]},
                                      expected_revision=approved["revision"])
        returned = self.tasks.propose(task["task_id"], task["proposal"], expected_revision=replaced["revision"])
        self.assertEqual(returned["proposal_hash"], task["proposal_hash"])
        for owner_preapproved in (False, True):
            with self.assertRaisesRegex(ValueError, "different proposal"):
                self.tasks.approve(task["task_id"], proposal_hash=returned["proposal_hash"],
                    user_message_ref="chat:approval", approved_stage_ids=["draft"], owner_preapproved=owner_preapproved)
        with self.assertRaisesRegex(ValueError, "approval"):
            self.tasks.require_approval(task["task_id"], "draft")
        self.assertTrue(self.store.db.execute("SELECT 1 FROM runtime_records WHERE kind='approval_version' AND payload LIKE '%chat:approval%'").fetchone())

    def test_identical_contract_and_proposal_keep_the_original_approval_and_plan(self):
        task = self.proposed()
        task_id = task["task_id"]
        approved = self.tasks.approve(task_id, proposal_hash=task["proposal_hash"],
                                      user_message_ref="chat:approval", approved_stage_ids=["draft"])
        plan = self.tasks.execution_plan(task_id)
        checked = self.tasks.update_execution_plan(task_id, {"checks": [*plan["checks"],
            {"id": "extra", "kind": "artifact", "path": "guide.md", "required": True}]},
            expected_revision=plan["revision"])
        approval = read_record(self.store.db, "approval", task_id)
        repeated = self.tasks.revise(task_id, contract(), expected_revision=approved["revision"])
        self.assertEqual(repeated, approved)
        repeated = self.tasks.propose(task_id, approved["proposal"], expected_revision=approved["revision"])
        self.assertEqual(repeated, approved)
        self.assertEqual(self.tasks.execution_plan(task_id), checked)
        self.assertEqual(read_record(self.store.db, "approval", task_id), approval)
        self.assertEqual(self.tasks.require_approval(task_id, "draft"), approved)
        for operation, value in ((self.tasks.revise, contract()), (self.tasks.propose, approved["proposal"])):
            with self.assertRaisesRegex(ValueError, "Stale"):
                operation(task_id, value, expected_revision=approved["revision"] - 1)

    def test_exact_retry_keeps_existing_provenance_without_reviving_a_replaced_proposal(self):
        from ase.prompts import capture

        prompt = capture(self.store, {'prompt': 'Approved draft wording'})
        task = self.proposed()
        task_id = task['task_id']
        args = dict(proposal_hash=task['proposal_hash'], user_message_ref=prompt['id'], approved_stage_ids=['draft'])
        original = self.tasks.approve(task_id, **args)
        record = read_record(self.store.db, 'approval', task_id)
        self.assertEqual(record['payload']['provenance'], 'owner_asserted')
        self.assertEqual(self.tasks.approve(task_id, **args), original)
        self.assertEqual(read_record(self.store.db, 'approval', task_id), record)
        with self.assertRaisesRegex(ValueError, 'different approval mode'):
            self.tasks.approve(task_id, **args, owner_preapproved=True)
        self.assertEqual(read_record(self.store.db, 'approval', task_id), record)
        revised = self.tasks.propose(task_id, {'summary': 'New material outcome', 'stage_ids': ['draft']},
                                     expected_revision=original['revision'])
        args['proposal_hash'] = revised['proposal_hash']
        with self.assertRaisesRegex(ValueError, 'predates|different proposal'):
            self.tasks.approve(task_id, **args)

    def test_material_contract_revision_keeps_history_and_budget(self):
        task = self.proposed()
        old_hash = task["contract_hash"]
        changed = contract()
        changed["request"] = "Prepare a revised operating guide"
        result = self.tasks.revise(task["task_id"], changed, expected_revision=task["revision"])
        self.assertNotEqual(result["contract_hash"], old_hash)
        self.assertIsNone(result["proposal_hash"])
        self.assertEqual(self.tasks.store.db.execute("SELECT COUNT(*) FROM runtime_records WHERE kind='task_version'").fetchone()[0], 2)

    def test_extra_check_does_not_change_approved_goal(self):
        task = self.proposed()
        self.tasks.approve(task["task_id"], proposal_hash=task["proposal_hash"],
                           user_message_ref="chat:user:approval-1", approved_stage_ids=["draft"])
        plan = self.tasks.execution_plan(task["task_id"])
        checks = copy.deepcopy(plan["checks"])
        checks.append({"id": "format", "kind": "artifact", "path": "guide.md", "required": True})
        result = self.tasks.update_execution_plan(task["task_id"], {"checks": checks},
                                                  expected_revision=plan["revision"])
        self.assertEqual(self.tasks.require_approval(task["task_id"], "draft")["proposal_hash"], task["proposal_hash"])
        self.assertNotEqual(result["plan_hash"], plan["plan_hash"])
        with self.assertRaisesRegex(ValueError, "required|baseline"):
            self.tasks.update_execution_plan(task["task_id"], {"checks": checks[1:]},
                                             expected_revision=result["revision"])

    def test_partial_scope_preserves_parent_obligations(self):
        task = self.proposed()
        self.tasks.approve(task["task_id"], proposal_hash=task["proposal_hash"],
                           user_message_ref="chat:user:approval-1", approved_stage_ids=["draft"])
        self.assertEqual(self.tasks.snapshot(task["task_id"])["contract"]["unknowns"][0]["status"], "UNRESOLVED")
        with self.assertRaisesRegex(ValueError, "gate|APPROVED|obligations"):
            current = self.tasks.snapshot(task["task_id"])
            self.tasks.transition(task["task_id"], "COMPLETED", expected_revision=current["revision"], reason="looks done")
        self.assertNotEqual(self.tasks.snapshot(task["task_id"])["state"], "COMPLETED")

    def test_legacy_contract_has_no_new_authority(self):
        self.store.db.execute("INSERT INTO tasks VALUES(?,?,?,?,?,?)",
                              ("legacy", "Legacy task", "PLANNED", '{"request":"Legacy task","max_attempts":2}', "old", "old"))
        self.store.db.commit()
        self.assertTrue(self.tasks.snapshot("legacy")["legacy_v1"])
        with self.assertRaisesRegex(ValueError, "legacy|approval"):
            self.tasks.require_approval("legacy", "draft")

    def test_stale_updates_and_forged_approval_are_rejected(self):
        bad = contract()
        bad["approved"] = True
        with self.assertRaisesRegex(ValueError, "field|approval"):
            self.tasks.create(bad)
        task = self.proposed()
        with self.assertRaisesRegex(ValueError, "Stale"):
            self.tasks.propose(task["task_id"], {"summary": "stale", "stage_ids": ["draft"]}, expected_revision=0)

    def test_stage_cannot_extend_parent_scope(self):
        bad = contract()
        bad["stages"][0]["scope"]["resources"].append("another-company")
        with self.assertRaisesRegex(ValueError, "scope"):
            self.tasks.create(bad)

    def test_dependency_cycle_is_rejected(self):
        bad = contract()
        bad["stages"][0]["depends_on"] = ["publish"]
        with self.assertRaisesRegex(ValueError, "cycle"):
            self.tasks.create(bad)

    def test_explicit_child_approval_cannot_bypass_changed_parent_scope(self):
        parent = self.proposed()
        self.tasks.approve(parent["task_id"], proposal_hash=parent["proposal_hash"], user_message_ref="chat:parent", approved_stage_ids=["draft"])
        c = contract("child-explicit")
        c.update(parent_task_id=parent["task_id"], parent_stage_id="draft")
        c["stages"], c["unknowns"] = c["stages"][:1], []
        child = self.tasks.create(c)
        child = self.tasks.propose(child["task_id"], {"summary": "Child draft", "stage_ids": ["draft"]}, expected_revision=child["revision"])
        self.tasks.approve(child["task_id"], proposal_hash=child["proposal_hash"], user_message_ref="chat:child", approved_stage_ids=["draft"])
        changed = contract()
        changed["scope"]["effects"] = []
        for stage in changed["stages"]:
            stage["scope"]["effects"] = []
        parent = self.tasks.snapshot(parent["task_id"])
        parent = self.tasks.revise(parent["task_id"], changed, expected_revision=parent["revision"])
        parent = self.tasks.propose(parent["task_id"], {"summary": "Read only now", "stage_ids": ["draft"]}, expected_revision=parent["revision"])
        self.tasks.approve(parent["task_id"], proposal_hash=parent["proposal_hash"], user_message_ref="chat:parent-revised", approved_stage_ids=["draft"])
        with self.assertRaisesRegex(ValueError, "parent|scope"):
            self.tasks.require_approval(child["task_id"], "draft")

    def test_legacy_revision_preserves_attempt_ceiling_and_spent_cycles(self):
        with self.store.db:
            self.store.db.execute("INSERT INTO tasks VALUES(?,?,?,?,?,?)", ("legacy-budget", "Existing task", "ESCALATED", '{"request":"Existing task","max_attempts":2}', "old", "old"))
        folder = self.store.root / "tasks" / "legacy-budget"
        folder.mkdir(parents=True)
        (folder / "progress.json").write_text(json.dumps({"attempts": 2, "implementation_completed": True}))
        task = self.tasks.revise("legacy-budget", contract("legacy-budget"), expected_revision=0)
        self.assertEqual(task["contract"]["execution_policy"]["max_task_cycles"], 2)
        task = self.tasks.propose(task["task_id"], {"summary": "Continue within the previous ceiling", "stage_ids": ["draft"]}, expected_revision=task["revision"])
        self.tasks.approve(task["task_id"], proposal_hash=task["proposal_hash"], user_message_ref="chat:continue", approved_stage_ids=["draft"])
        with self.assertRaisesRegex(ValueError, "budget"):
            WorkflowController(self.store).reserve_cycle(task["task_id"], "draft")
        self.assertEqual(read_record(self.store.db, "budget", task["task_id"])["payload"]["task_cycles"], 2)
        self.assertTrue(read_record(self.store.db, "task_version", "legacy-budget:legacy")["payload"]["legacy_v1"])
        task = self.tasks.snapshot(task["task_id"])
        revised = self.tasks.revise(task["task_id"], contract("legacy-budget"), expected_revision=task["revision"])
        self.assertEqual(revised["contract"]["execution_policy"]["max_task_cycles"], 2)

    def test_malformed_legacy_progress_cannot_silently_reset_attempts(self):
        with self.store.db:
            self.store.db.execute("INSERT INTO tasks VALUES(?,?,?,?,?,?)", ("legacy-bad", "Existing task", "ESCALATED", '{"request":"Existing task","max_attempts":2}', "old", "old"))
        folder = self.store.root / "tasks" / "legacy-bad"
        folder.mkdir(parents=True)
        (folder / "progress.json").write_text('{"attempts":true}')
        with self.assertRaisesRegex(ValueError, "legacy|Legacy"):
            self.tasks.revise("legacy-bad", contract("legacy-bad"), expected_revision=0)
        self.assertTrue(self.tasks.snapshot("legacy-bad")["legacy_v1"])



class ApprovalHintTests(unittest.TestCase):
    def test_hinted_approve_command_parses_with_real_values(self):
        import re, shlex
        from ase.__main__ import parser
        from ase.tasks import approval_hint
        task = {'task_id': 'fx-rate', 'proposal_hash': 'abc123', 'proposal': {'stage_ids': ['implement', 'docs']}}
        command = re.search(r'`(ase task approve [^`]+)`', approval_hint(task)).group(1).replace('<owner reply>', 'chat:1')
        args = parser().parse_args(shlex.split(command)[1:])
        self.assertEqual((args.task_id, args.proposal_hash, args.approved_stage_ids), ('fx-rate', 'abc123', ['implement', 'docs']))
        self.assertIn('propose', approval_hint({'task_id': 't', 'proposal_hash': None}))


class StatusNextActionTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="ase-next-")
        self.addCleanup(temp.cleanup)
        repo = Path(temp.name) / "work"
        repo.mkdir()
        self.store = Store(repo, Path(temp.name) / "state")
        self.addCleanup(self.store.close)
        self.tasks = TaskService(self.store)

    def test_approval_without_stage_names_the_proposal_stage_flags(self):
        task = self.tasks.create(contract())
        task = self.tasks.propose(task["task_id"], {"summary": "Draft", "stage_ids": ["draft"]}, expected_revision=task["revision"])
        with self.assertRaisesRegex(ValueError, "differs from proposal: pass --stage draft \\(the proposal's stage_ids"):
            self.tasks.approve(task["task_id"], proposal_hash=task["proposal_hash"], user_message_ref="chat:1", approved_stage_ids=None)

    def test_status_next_action_follows_discover_propose_approve_run(self):
        import shlex
        from ase.__main__ import parser
        from ase.discovery import discover_capabilities, record_discovery
        def command(text):
            return parser().parse_args(shlex.split(text.split("`")[1])[1:])
        task = self.tasks.create(contract())
        args = command(self.tasks.readiness("approval-task")["next_action"])
        self.assertEqual((args.action, args.expected_revision), ("discover", 0))
        record_discovery(self.store, "approval-task", discover_capabilities(self.store.project,
            scope={"resources": ["guide.md"]}, backend_config={}), expected_revision=0)
        args = command(self.tasks.readiness("approval-task")["next_action"])
        self.assertEqual((args.action, args.expected_revision), ("propose", task["revision"]))
        task = self.tasks.propose("approval-task", {"summary": "Draft", "stage_ids": ["draft"]}, expected_revision=task["revision"])
        self.assertIn("ase task approve approval-task --proposal-hash " + task["proposal_hash"] + " --stage draft",
                      self.tasks.readiness("approval-task")["next_action"])
        self.tasks.approve("approval-task", proposal_hash=task["proposal_hash"], user_message_ref="chat:1", approved_stage_ids=["draft"])
        self.assertEqual(self.tasks.readiness("approval-task")["next_action"], "`ase task run approval-task`")
        self.assertNotIn("next_action", self.tasks.readiness("approval-task", stage_id="draft"))

    def test_status_next_action_after_execution_names_resume_verify_and_finish(self):
        import re, shlex
        from ase.__main__ import parser
        from ase.state import transaction, write_record
        def command(prefix):
            text = self.tasks.readiness("approval-task", include_progress=True)["next_action"]
            found = re.search(r"`(" + prefix + r"[^`]*)`", text).group(1)
            return parser().parse_args(shlex.split(re.sub(r"<[^>]+>", "x", found))[1:])
        task = self.tasks.create(contract())
        task = self.tasks.propose("approval-task", {"summary": "Draft", "stage_ids": ["draft"]}, expected_revision=task["revision"])
        self.tasks.approve("approval-task", proposal_hash=task["proposal_hash"], user_message_ref="chat:1", approved_stage_ids=["draft"])
        task = self.tasks.snapshot("approval-task")
        task = self.tasks.transition("approval-task", "BLOCKED", expected_revision=task["revision"], reason="Plan missing")
        self.assertEqual((command("ase task ").action, command("ase task ").task_id), ("resume", "approval-task"))
        self.assertIsNone(self.tasks.readiness("approval-task")["next_action"])
        self.tasks.transition("approval-task", "REVIEWING", expected_revision=task["revision"], reason="Awaiting verification")
        self.assertEqual(command("ase task ").action, "verify")
        with transaction(self.store.db) as db:
            old = read_record(db, "gate", "approval-task")
            write_record(db, "gate", "approval-task", {"status": "APPROVED", "contract_hash": task["contract_hash"]},
                         expected_revision=old["revision"])
        args = command("ase task finish ")
        self.assertEqual((args.task_id, args.base, args.body_file), ("approval-task", "x", "x"))
        self.assertNotIn("pending lead inbox", self.tasks.readiness("approval-task", include_progress=True)["next_action"])
        from ase.coordinator import CoordinatorEvents
        for source in ("verification:a", "verification:b"):
            CoordinatorEvents(self.store).enqueue("approval-task", source_kind="verification", source_id=source, status="APPROVED",
                summary="Independent result captured", evidence=[{"ref": "receipt.json", "sha256": "0" * 64}])
        text = self.tasks.readiness("approval-task", include_progress=True)["next_action"]
        self.assertIn("then process 2 pending lead inbox event(s) (listed by `ase task finish`)", text)
        self.assertEqual(command("ase task finish ").task_id, "approval-task")
        with transaction(self.store.db) as db:
            write_record(db, "stop", "approval-task", {"reason": "owner stop"}, expected_revision=read_record(db, "stop", "approval-task")["revision"])
        text = self.tasks.readiness("approval-task", include_progress=True)["next_action"]
        self.assertIn("A stop was requested", text)
        self.assertNotIn("finish", text)

    def test_task_ids_containing_sk_dash_are_not_treated_as_credentials(self):
        from ase.core import redact
        from ase.state import text
        self.assertEqual(text("`ase task resume task-implement-login-flow`", "next_action"),
                         "`ase task resume task-implement-login-flow`")
        for key in ("sk-" + "a" * 24, "sk-proj-" + "b" * 24, "ghp_" + "c" * 20):
            with self.subTest(key=key):
                self.assertNotIn(key, redact("OPENAI_KEY " + key + " and key=" + key + " and '" + key + "'"))
                for wrapped in ("\\n" + key, "\\u0022" + key, "k%3D" + key, "KEY_" + key, "--api-key-" + key,
                                "\x1b[32m" + key, "\x1b[1;32m" + key, "\x1b[m" + key, "\\x1b[0m" + key, "\\t" + key, "%20" + key):
                    self.assertNotIn(key, redact(wrapped), wrapped)
                with self.assertRaises(ValueError):
                    text("value " + key, "next_action")


class TemplateTests(unittest.TestCase):
    def test_shipped_templates_are_v2_with_risk_scaled_lead_review(self):
        from pathlib import Path
        from ase.__main__ import _new_contract
        from ase.tasks import validate_contract_v2
        root = Path(__file__).resolve().parents[1] / 'templates'
        for name, profile in (('task.json', 'software'), ('task-v2.json', 'document')):
            with self.subTest(name=name):
                contract = validate_contract_v2(_new_contract(str(root / name)))
                self.assertEqual((contract['schema_version'], contract['profile']), (2, profile))
                self.assertEqual(contract['lead_review']['additional_passes'], 1)

if __name__ == "__main__":
    unittest.main()
