"""Behavioral UTC history, atomic closure and local reporting tests."""
import copy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

from ase.continuity import Continuity
from ase.journal import Journal
from ase.state import read_record, transaction, write_record
from ase.store import Store
from ase.tasks import TaskService


def contract(task_id):
    return {"id": task_id, "schema_version": 2, "profile": "document", "request": "Write an operating guide",
            "scope": {"resources": ["guide.txt"], "effects": ["file_write"]},
            "acceptance": [{"id": "AC1", "expected": "Guide exists", "evidence_ids": ["guide"]}],
            "evidence": [{"id": "guide", "kind": "artifact", "path": "guide.txt", "required": True}],
            "stages": [{"id": "draft", "acceptance_ids": ["AC1"], "depends_on": []}]}


class JournalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ase-journal-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "project"
        self.repo.mkdir()
        self.store = Store(self.repo, self.root / "state")
        self.addCleanup(self.store.close)
        self.tasks = TaskService(self.store)
        for task_id in ("one", "two"):
            self.tasks.create(contract(task_id))
        self.journal = Journal(self.store)
        self.payload = {"finish_reason": "paused", "summary": "Saved the draft", "next_action": "Review the draft",
                        "context": {"summary": "Guide awaits review"}, "blockers": []}

    def counts(self):
        return tuple(self.store.db.execute("SELECT COUNT(*) FROM " + table).fetchone()[0]
                     for table in ("checkpoints", "sessions", "runtime_events", "runtime_records"))

    def finish(self, task="one", session="session", key="closure", **kwargs):
        return self.journal.finish_session(task, session, kwargs.pop("payload", self.payload),
                                          closure_key=key, expected_revision=kwargs.pop("revision", 0),
                                          expected_context_revision=kwargs.pop("context_revision", 0), **kwargs)

    def test_finish_is_atomic_idempotent_and_not_completion(self):
        result = self.finish()
        counts = self.counts()
        self.assertEqual(result, self.finish())
        self.assertEqual(self.counts(), counts)
        self.assertEqual(result["checkpoint_revision"], 1)
        self.assertEqual(result["context_revision"], 1)
        self.assertEqual(self.tasks.snapshot("one")["state"], "PAUSED")
        self.assertEqual(Continuity(self.store).snapshot("one")["payload"]["next_action"], "Review the draft")
        with self.assertRaisesRegex(ValueError, "CONFLICT"):
            self.finish(payload={**self.payload, "summary": "Different"})
        with self.assertRaisesRegex(ValueError, "Stale"):
            self.finish(key="stale-checkpoint", context_revision=1)
        with self.assertRaisesRegex(ValueError, "Stale"):
            self.finish(key="stale-context", revision=1)
        self.assertEqual(self.counts(), counts)
        self.finish(task="two", key="other", context_revision=1)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM checkpoints").fetchone()[0], 2)

    def test_finish_rolls_back_every_write_on_database_failure(self):
        before = self.counts()
        self.store.db.execute("CREATE TRIGGER reject_session BEFORE INSERT ON sessions BEGIN SELECT RAISE(ABORT,'simulated failure'); END")
        self.store.db.commit()
        with self.assertRaisesRegex(Exception, "simulated failure"):
            self.finish()
        self.assertEqual(self.counts(), before)
        self.assertEqual(self.tasks.snapshot("one")["state"], "DRAFT")

    def test_failed_finish_cannot_be_committed_by_a_callers_outer_transaction(self):
        before = self.counts()
        self.store.db.execute("CREATE TRIGGER reject_session BEFORE INSERT ON sessions BEGIN SELECT RAISE(ABORT,'simulated failure'); END")
        self.store.db.commit()
        with transaction(self.store.db) as db:
            with self.assertRaisesRegex(Exception, "simulated failure"):
                self.finish()
            write_record(db, "outer_work", "unrelated", {"summary": "Outer transaction continues"}, expected_revision=0)
        after = self.counts()
        self.assertEqual(after[:3], before[:3])
        self.assertEqual(after[3], before[3] + 1)
        self.assertEqual(self.tasks.snapshot("one")["state"], "DRAFT")

    def test_noncomplete_reasons_close_sessions_with_honest_task_state(self):
        states = {"awaiting_approval": "AWAITING_APPROVAL", "blocked": "BLOCKED", "paused": "PAUSED",
                  "cancelled": "CANCELLED", "interrupted": "PAUSED"}
        for index, (reason, state) in enumerate(states.items()):
            task = "reason" + str(index)
            self.tasks.create(contract(task))
            result = self.finish(task=task, key=task, payload={**self.payload, "finish_reason": reason}, context_revision=index)
            self.assertEqual(self.tasks.snapshot(task)["state"], state)
            self.assertEqual(result["finish_reason"], reason)
        self.assertTrue(all(row[0] for row in self.store.db.execute("SELECT ended FROM sessions")))

    def test_noncomplete_finish_labels_cached_approval_as_historical(self):
        task, plan = self.tasks.snapshot("one"), self.tasks.execution_plan("one")
        historical = {"status": "APPROVED", "contract_hash": "historical-contract", "plan_hash": "historical-plan"}
        with transaction(self.store.db) as db:
            write_record(db, "gate", "one", historical, expected_revision=0)
        result = self.finish()
        self.assertEqual(result["gate"]["status"], "BLOCKED")
        self.assertEqual(result["gate"]["current_verification"], "NOT_RECOMPUTED")
        self.assertEqual(result["gate"]["historical_gate"], historical)
        self.assertEqual(result["gate"]["contract_hash"], task["contract_hash"])
        self.assertEqual(result["gate"]["plan_hash"], plan["plan_hash"])
        self.assertEqual(read_record(self.store.db, "gate", "one")["payload"], historical)

    def test_completed_does_not_trust_payload_or_cached_gate(self):
        task, plan = self.tasks.snapshot("one"), self.tasks.execution_plan("one")
        with transaction(self.store.db) as db:
            write_record(db, "gate", "one", {"status": "APPROVED", "contract_hash": task["contract_hash"],
                                             "plan_hash": plan["plan_hash"]}, expected_revision=0)
        with self.assertRaisesRegex(ValueError, "gate|APPROVED"):
            self.finish(payload={**self.payload, "finish_reason": "completed"})
        with self.assertRaisesRegex(ValueError, "Unknown|gate"):
            self.finish(payload={**self.payload, "gate": {"status": "APPROVED"}})
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM checkpoints").fetchone()[0], 0)

    def test_unresolved_obligations_prevent_completion_even_with_protocol_gate(self):
        pending = contract("pending")
        pending["unknowns"] = [{"id": "access", "statement": "Clarify destination", "status": "UNRESOLVED"}]
        self.tasks.create(pending)
        def protocol_gate(task_id):
            task, plan = self.tasks.snapshot(task_id), self.tasks.execution_plan(task_id)
            gate = {"status": "APPROVED", "contract_hash": task["contract_hash"], "plan_hash": plan["plan_hash"]}
            with transaction(self.store.db) as db:
                write_record(db, "gate", task_id, gate, expected_revision=0)
            return gate
        self.journal = Journal(self.store, gate_provider=protocol_gate)
        with self.assertRaisesRegex(ValueError, "obligation"):
            self.finish(task="pending", payload={**self.payload, "finish_reason": "completed"})
        self.assertIsNone(read_record(self.store.db, "gate", "pending")["payload"])
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0], 0)

    def test_completed_requires_current_gate_and_all_obligations(self):
        # This injected provider tests closure protocol only. It is not a semantic evidence judgment.
        def protocol_gate(task_id):
            task, plan = self.tasks.snapshot(task_id), self.tasks.execution_plan(task_id)
            gate = {"status": "APPROVED", "contract_hash": task["contract_hash"], "plan_hash": plan["plan_hash"]}
            with transaction(self.store.db) as db:
                current = read_record(db, "gate", task_id)
                write_record(db, "gate", task_id, gate, expected_revision=current["revision"])
            return gate
        self.journal = Journal(self.store, gate_provider=protocol_gate)
        # Isolate closure transaction behavior at the current-gate boundary;
        # the integration/live suites exercise the real evidence evaluator.
        with patch("ase.gates.GateService.gate", lambda service, task_id: protocol_gate(task_id)):
            result = self.finish(payload={**self.payload, "finish_reason": "completed"})
        self.assertEqual(result["gate"]["status"], "APPROVED")
        self.assertEqual(self.tasks.snapshot("one")["state"], "COMPLETED")
        self.assertEqual(self.finish(payload={**self.payload, "finish_reason": "completed"}), result)

    def test_prepared_artifacts_are_verified_before_closure(self):
        path = self.repo / "guide.txt"
        path.write_text("guide", encoding="utf-8")
        artifact = {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
        path.write_text("changed", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Artifact|artifact"):
            self.finish(payload={**self.payload, "artifacts": [artifact]})
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0], 0)

    def test_prepared_artifact_reference_and_idempotency_survive_later_expiry(self):
        path = self.repo / "guide.txt"
        path.write_text("prepared", encoding="utf-8")
        artifact = {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
        payload = {**self.payload, "artifacts": [artifact]}
        result = self.finish(payload=payload)
        path.unlink()
        self.assertEqual(result["artifacts"], [artifact])
        self.assertEqual(self.finish(payload=payload), result)

    def test_changed_artifact_cannot_complete_using_an_old_cached_gate(self):
        from ase.gates import GateService, source_snapshot
        path = self.repo / "guide.txt"
        path.write_text("prepared", encoding="utf-8")
        task, plan = self.tasks.snapshot("one"), self.tasks.execution_plan("one")
        source = source_snapshot(self.store, task["contract"])
        with transaction(self.store.db) as db:
            write_record(db, "checks", "one", {"kind": "checks", "fingerprint": source["fingerprint"],
                         "contract_hash": task["contract_hash"], "plan_hash": plan["plan_hash"],
                         "results": [{"id": "guide", "path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}]},
                         expected_revision=0)
            write_record(db, "gate", "one", {"status": "APPROVED", "contract_hash": task["contract_hash"],
                                             "plan_hash": plan["plan_hash"]}, expected_revision=0)
        path.write_text("changed after checks", encoding="utf-8")
        gate = GateService(self.store).gate("one")
        self.assertEqual(gate["status"], "REJECTED")
        self.assertTrue(any("Stale check" in reason for reason in gate["rejected_reasons"]))
        self.assertTrue(any("Artifact" in reason for reason in gate["rejected_reasons"]))
        with self.assertRaisesRegex(ValueError, "APPROVED"):
            self.finish(payload={**self.payload, "finish_reason": "completed"})
        self.assertEqual(self.tasks.snapshot("one")["state"], "DRAFT")

    def test_two_concurrent_closures_share_one_checkpoint_and_context_cas(self):
        self.store.close()
        barrier, results = threading.Barrier(2), []
        def writer(number):
            store = Store(self.repo, self.root / "state")
            try:
                barrier.wait()
                results.append(Journal(store).finish_session("one", "s" + str(number), self.payload,
                               closure_key="k" + str(number), expected_revision=0, expected_context_revision=0))
            except ValueError as error:
                results.append(str(error))
            finally:
                store.close()
        workers = [threading.Thread(target=writer, args=(i,)) for i in range(2)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(20)
            self.assertFalse(worker.is_alive())
        self.store = Store(self.repo, self.root / "state")
        self.addCleanup(self.store.close)
        self.assertEqual(sum(isinstance(r, dict) for r in results), 1)
        self.assertEqual(sum(isinstance(r, str) and "Stale" in r for r in results), 1)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM checkpoints").fetchone()[0], 1)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0], 1)

    def test_pausing_preserves_unknown_operations_and_history(self):
        with transaction(self.store.db) as db:
            write_record(db, "operation", "external", {"status": "UNKNOWN", "next_action": "Probe remote receipt"}, expected_revision=0)
        self.finish()
        before = read_record(self.store.db, "operation", "external")
        self.finish(session="resumed", key="second", revision=1, context_revision=1)
        self.assertEqual(before, read_record(self.store.db, "operation", "external"))
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM checkpoints").fetchone()[0], 2)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM runtime_records WHERE kind='project_context_version'").fetchone()[0], 2)

    def test_two_tasks_cannot_overwrite_the_same_context_revision(self):
        barrier, results = threading.Barrier(2), []
        def writer(task_id):
            store = Store(self.repo, self.root / "state")
            try:
                barrier.wait()
                results.append(Journal(store).finish_session(task_id, "session", self.payload,
                               closure_key="finish-" + task_id, expected_revision=0, expected_context_revision=0))
            except ValueError as error:
                results.append(str(error))
            finally:
                store.close()
        workers = [threading.Thread(target=writer, args=(task_id,)) for task_id in ("one", "two")]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(20)
            self.assertFalse(worker.is_alive())
        self.assertEqual(sum(isinstance(r, dict) for r in results), 1)
        self.assertEqual(sum(isinstance(r, str) and "Stale project context" in r for r in results), 1)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM checkpoints").fetchone()[0], 1)
        self.assertEqual(self.journal.context()["revision"], 1)

    def test_interrupted_session_recovers_the_last_committed_checkpoint(self):
        self.finish()
        running = self.journal.start_session("one", "running")
        self.assertEqual(running["payload"]["status"], "RUNNING")
        self.store.close()
        self.store = Store(self.repo, self.root / "state")
        self.addCleanup(self.store.close)
        self.journal = Journal(self.store)
        self.tasks = TaskService(self.store)
        self.assertEqual(Continuity(self.store).snapshot("one")["revision"], 1)
        self.assertEqual(self.journal.session("one", "running")["payload"]["status"], "RUNNING")
        result = self.finish(session="running", key="interrupted", revision=1, context_revision=1,
                             payload={**self.payload, "finish_reason": "interrupted", "summary": "Runner ended without a result"})
        self.assertEqual(result["finish_reason"], "interrupted")
        self.assertEqual(self.journal.session("one", "running")["payload"]["status"], "INTERRUPTED")
        self.assertEqual(self.tasks.snapshot("one")["state"], "PAUSED")
        self.assertEqual(len(self.store.db.execute("SELECT * FROM checkpoints WHERE task_id='one'").fetchall()), 2)

    def test_git_session_finish_preserves_continuity_observation(self):
        subprocess.run(["git", "init", "-b", "feature/session", str(self.repo)], check=True, capture_output=True)
        (self.repo / "guide.txt").write_text("draft", encoding="utf-8")
        self.finish()
        restored = Continuity(self.store).restore("one")
        self.assertFalse(restored["drift"])
        self.assertEqual(restored["checkpoint"]["observation"]["branch"], "feature/session")
        (self.repo / "guide.txt").write_text("changed", encoding="utf-8")
        self.assertIn("fingerprint", Continuity(self.store).restore("one")["drift"])

    def test_session_cannot_close_again_under_another_key(self):
        self.finish()
        before = self.counts()
        with self.assertRaisesRegex(ValueError, "CONFLICT"):
            self.finish(key="different", revision=1, context_revision=1)
        self.assertEqual(self.counts(), before)

    def test_carried_blocker_resolved_during_period_and_task_isolation(self):
        blocker = {"id": "access", "reason": "Access pending", "next_probe": "Ask for access"}
        self.event("blocker_opened", "2026-11-10T12:00:00Z", "open-one", blocker=blocker)
        self.event("blocker_opened", "2026-11-10T12:00:00Z", "open-two", task="two", blocker=blocker)
        self.event("blocker_resolved", "2026-11-12T10:00:00Z", "resolve-one", blocker_id="access")
        with patch("ase.journal.now", return_value="2026-11-14T12:00:00+00:00"):
            report = self.journal.worklog("2026-11-12", "2026-11-13", timezone="Europe/Bucharest")
        self.assertEqual({b["task_id"] for b in report["carried_blockers"]}, {"one", "two"})
        self.assertEqual([b["task_id"] for b in report["blockers_as_of_end"]], ["two"])
        self.assertEqual([b["task_id"] for b in report["current_blockers"]], ["two"])
        self.assertEqual([e["event_key"] for e in report["period_blockers"]], ["resolve-one"])

    def test_report_uses_half_open_local_midnight_and_rejects_bad_ranges(self):
        self.event("decision", "2026-11-11T21:59:59Z", "before")
        self.event("decision", "2026-11-11T22:00:00Z", "at-start")
        self.event("decision", "2026-11-12T22:00:00Z", "at-end")
        report = self.journal.worklog("2026-11-12", "2026-11-13", timezone="Europe/Bucharest")
        self.assertEqual([e["event_key"] for e in report["events"]], ["at-start"])
        for start, end in (("2026-11-12", "2026-11-12"), ("2026-11-13", "2026-11-12"), ("20261112", "2026-11-13")):
            with self.assertRaises(ValueError):
                self.journal.worklog(start, end, timezone="UTC")

    def event(self, kind, stamp, key, task="one", **extra):
        return self.journal.append({"kind": kind, "task_id": task, "occurred_at_utc": stamp,
                                    "summary": key, **extra}, event_key=key)

    def test_worklog_reports_events_and_carried_blockers(self):
        blocker = {"id": "access", "reason": "Access pending", "dependent_stage_ids": ["draft"], "next_probe": "Ask for access"}
        self.event("blocker_opened", "2026-11-10T12:00:00Z", "open", blocker=blocker)
        late = self.event("work_completed", "2026-11-11T22:30:00Z", "draft")
        self.event("check", "2026-11-12T10:00:00Z", "tested", evidence_refs=["receipt:1"], status="PASS")
        self.event("published", "2026-11-12T11:00:00Z", "published", evidence_refs=["receipt:publish"])
        correction = self.event("correction", "2026-11-12T12:00:00Z", "corrected", corrects="draft")
        self.event("blocker_resolved", "2026-11-13T10:00:00Z", "closed", blocker_id="access")
        with patch("ase.journal.now", return_value="2026-11-14T12:00:00+00:00"):
            report = self.journal.worklog("2026-11-12", "2026-11-13", timezone="Europe/Bucharest")
        self.assertEqual([e["event_key"] for e in report["events"]], ["draft", "tested", "published", "corrected"])
        self.assertEqual(report["events"][0]["local_date"], "2026-11-12")
        self.assertNotEqual(late["occurred_at_utc"], late["recorded_at_utc"])
        self.assertEqual(correction["corrects"], "draft")
        self.assertFalse(report["current_blockers"])
        self.assertEqual(report["blockers_as_of_end"][0]["id"], "access")
        self.assertEqual(report["carried_blockers"][0]["id"], "access")
        meeting = self.journal.meeting_report("2026-11-12", "2026-11-13", timezone="Europe/Bucharest", as_of_end=True)
        self.assertEqual(meeting["done"], [])
        self.assertEqual([e["event_key"] for e in meeting["corrections"]], ["corrected"])
        self.assertEqual([e["event_key"] for e in meeting["verified"]], ["tested"])
        self.assertEqual([e["event_key"] for e in meeting["published"]], ["published"])
        self.assertEqual(meeting["blockers"][0]["id"], "access")
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM runtime_events WHERE kind='journal'").fetchone()[0], 6)

    def test_event_idempotency_correction_scope_and_sensitive_fields(self):
        event = {"task_id": "one", "kind": "decision", "summary": "Keep the draft local"}
        first = self.journal.append(event, event_key="choice")
        self.assertEqual(self.journal.append(event, event_key="choice"), first)
        with self.assertRaisesRegex(ValueError, "CONFLICT"):
            self.journal.append({**event, "summary": "Publish now"}, event_key="choice")
        with self.assertRaisesRegex(ValueError, "task|correction"):
            self.event("correction", "2026-11-12T11:00:00Z", "wrong-task", task="two", corrects="choice")
        for extra in ({"transcript": "raw"}, {"summary": "token sk-" + "a" * 40}):
            with self.assertRaises(ValueError):
                self.journal.append({**event, **extra}, event_key="private")
        with self.assertRaisesRegex(ValueError, "evidence"):
            self.event("published", "2026-11-12T10:00:00Z", "unproven")

    def test_missing_local_rules_does_not_prevent_utc_events(self):
        from ase.timezones import TimezoneDataUnavailable
        with patch("ase.journal.resolve_timezone", side_effect=TimezoneDataUnavailable("missing")):
            self.journal.append({"task_id": "one", "kind": "decision", "summary": "Saved in UTC"}, event_key="utc")
            with self.assertRaisesRegex(TimezoneDataUnavailable, "TZ_DATA_UNAVAILABLE"):
                self.journal.worklog("2026-11-12", "2026-11-13", timezone="Europe/Bucharest")

    def test_failed_checks_and_stage_results_do_not_claim_verified_work(self):
        self.event("stage_result", "2026-11-12T09:00:00Z", "draft-blocked", status="BLOCKED")
        self.event("check", "2026-11-12T10:00:00Z", "failed-test", evidence_refs=["receipt:failed"], status="FAIL")
        self.event("check", "2026-11-12T11:00:00Z", "passed-test", evidence_refs=["receipt:passed"], status="PASS")
        report = self.journal.meeting_report("2026-11-12", "2026-11-13", timezone="Europe/Bucharest")
        self.assertFalse(report["done"])
        self.assertEqual([e["event_key"] for e in report["verified"]], ["passed-test"])
        self.assertEqual([e["event_key"] for e in report["checked"]], ["failed-test", "passed-test"])
        self.assertEqual(report["stage_results"][0]["status"], "BLOCKED")
        with self.assertRaisesRegex(ValueError, "status"):
            self.event("check", "2026-11-12T12:00:00Z", "ambiguous", evidence_refs=["receipt:unknown"])
