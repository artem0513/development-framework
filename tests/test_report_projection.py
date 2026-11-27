"""Effective report facts are projections; immutable journal rows remain audit history."""
from pathlib import Path
from itertools import permutations
import tempfile
import unittest
from unittest.mock import patch

from ase.journal import Journal
from ase.memory.reports import ReportService
from ase.store import Store
from ase.tasks import TaskService
from test_journal import contract


class ReportProjectionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="ase-report-projection-")
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        repo = root / "project"
        repo.mkdir()
        self.store = Store(repo, root / "state")
        self.addCleanup(self.store.close)
        self.tasks = TaskService(self.store)
        for task_id in ("one", "two"):
            self.tasks.create(contract(task_id))
        self.journal = Journal(self.store)
        clock = patch("ase.journal.now", return_value="2026-11-15T12:00:00+00:00")
        clock.start()
        self.addCleanup(clock.stop)

    def event(self, kind, key, stamp="2026-11-12T10:00:00Z", task="one", **extra):
        return self.journal.append({"kind": kind, "task_id": task, "summary": key,
                                    "occurred_at_utc": stamp, **extra}, event_key=key)

    def report(self, *, as_of_end=False, start="2026-11-12", until="2026-11-13"):
        return self.journal.meeting_report(start, until, timezone="UTC", as_of_end=as_of_end)

    def test_legacy_completed_session_is_done_with_inference_provenance(self):
        self.event("session_finished", "closed", session_id="implementation", finish_reason="completed")
        done = self.report()["done"]
        self.assertEqual([row["event_key"] for row in done], ["closed"])
        self.assertEqual(done[0]["completion_provenance"], "inferred_session_finished")
        self.assertEqual(done[0]["kind"], "session_finished")

    def test_explicit_completion_keeps_its_provenance_and_deduplicates_summary(self):
        self.event("work_completed", "completed", session_id="implementation", source_refs=["receipt:gate"])
        self.event("session_finished", "closed", session_id="implementation", finish_reason="completed")
        done = self.report()["done"]
        self.assertEqual([row["event_key"] for row in done], ["completed"])
        self.assertIn("completion_provenance", done[0])
        self.assertEqual(done[0]["completion_provenance"], "explicit_work_completed")
        self.assertEqual(done[0]["source_refs"], ["receipt:gate"])

    def test_publication_followup_does_not_repeat_task_completion(self):
        self.event("session_finished", "implementation", "2026-11-11T10:00:00Z",
                   session_id="implementation", finish_reason="completed")
        self.event("work_completed", "published-completion", session_id="publication")
        self.event("session_finished", "published-summary", session_id="publication", finish_reason="completed")
        self.assertEqual(self.report()["done"], [])

    def test_standalone_completion_and_later_publication_summary_count_once(self):
        self.event("work_completed", "legacy-complete")
        self.event("session_finished", "publication", "2026-11-12T11:00:00Z",
                   session_id="publish", finish_reason="completed")
        self.assertEqual([row["event_key"] for row in self.report()["done"]], ["legacy-complete"])

    def test_old_standalone_completion_does_not_repeat_on_publication_day(self):
        self.event("work_completed", "legacy-complete", "2026-11-11T10:00:00Z")
        self.event("work_completed", "publication-marker", session_id="publish")
        self.event("session_finished", "publication", "2026-11-12T11:00:00Z",
                   session_id="publish", finish_reason="completed")
        self.assertEqual(self.report()["done"], [])
        all_days = self.report(start="2026-11-11")
        self.assertEqual([row["event_key"] for row in all_days["done"]], ["legacy-complete"])

    def test_retracted_standalone_completion_cannot_promote_publication(self):
        self.event("work_completed", "legacy-complete")
        self.event("session_finished", "publication", "2026-11-12T11:00:00Z",
                   session_id="publish", finish_reason="completed")
        self.event("correction", "withdrawn", "2026-11-14T10:00:00Z", corrects="legacy-complete")
        self.assertEqual(self.report()["done"], [])

    def test_retracting_withdrawal_restores_only_the_standalone_completion(self):
        self.event("work_completed", "legacy-complete")
        self.event("session_finished", "publication", "2026-11-12T11:00:00Z",
                   session_id="publish", finish_reason="completed")
        self.event("correction", "withdrawn", "2026-11-14T10:00:00Z", corrects="legacy-complete")
        self.event("correction", "restored", "2026-11-14T11:00:00Z", corrects="withdrawn")
        self.assertEqual([row["event_key"] for row in self.report()["done"]], ["legacy-complete"])

    def test_later_summary_alias_cannot_resurrect_an_already_retracted_completion(self):
        self.event("work_completed", "completed", session_id="implementation")
        self.event("correction", "withdrawn", corrects="completed")
        self.event("session_finished", "later-summary", session_id="implementation", finish_reason="completed")
        self.assertEqual(self.report()["done"], [])

    def test_completion_alias_retraction_is_independent_of_legal_append_order(self):
        templates = {
            "done": {"kind": "work_completed", "session_id": "implementation"},
            "summary": {"kind": "session_finished", "session_id": "implementation", "finish_reason": "completed"},
            "withdraw": {"kind": "correction", "corrects": "done"},
            "restore": {"kind": "correction", "corrects": "withdraw"},
            "withdraw2": {"kind": "correction", "corrects": "done"},
            "done2": {"kind": "work_completed", "session_id": "implementation"},
        }
        scenarios = [(('done', 'summary', 'withdraw'), 0),
                     (('done', 'summary', 'withdraw', 'restore'), 1),
                     (('done', 'summary', 'withdraw', 'restore', 'withdraw2'), 0),
                     (('done', 'done2', 'withdraw'), 0),
                     (('done', 'done2', 'withdraw', 'restore'), 1)]
        checked = 0
        for names, expected in scenarios:
            for target in ('done', 'summary'):
                if target not in names:
                    continue
                templates['withdraw']['corrects'] = target
                templates['withdraw2']['corrects'] = target
                for order in permutations(names):
                    if any(templates[name].get('corrects') in order and
                           order.index(templates[name]['corrects']) > order.index(name)
                           for name in order):
                        continue
                    events = [{**templates[name], 'task_id': 'one', 'event_key': name, 'summary': name,
                               'sequence': index + 1, 'occurred_at_utc': '2026-11-12T10:00:00+00:00'}
                              for index, name in enumerate(order)]
                    with self.subTest(order=order, target=target):
                        effective = ReportService._effective(events)
                        self.assertEqual(len(ReportService._completions(events, effective)), expected)
                    checked += 1
        self.assertGreater(checked, 40)

    def test_correcting_a_paused_summary_does_not_retract_completed_facts(self):
        self.event('session_finished', 'paused-summary', session_id='implementation', finish_reason='paused')
        self.event('work_completed', 'done', session_id='implementation')
        self.event('session_finished', 'completed-summary', session_id='implementation', finish_reason='completed')
        self.event('correction', 'correct-pause', corrects='paused-summary')
        self.assertEqual([e['event_key'] for e in self.report()['done']], ['done'])

    def test_current_blocker_query_applies_retracted_resolution_and_ignores_future_events(self):
        self.event("blocker_opened", "opened", blocker={"id": "execution-blocker", "reason": "Await fix", "next_probe": "Verify"})
        self.event("blocker_resolved", "wrong-resolution", blocker_id="execution-blocker")
        self.event("correction", "withdraw-resolution", corrects="wrong-resolution")
        self.event("blocker_opened", "future", "2026-11-16T10:00:00Z",
                   blocker={"id": "future", "reason": "Future observation", "next_probe": "Wait"})
        self.assertEqual([b['id'] for b in self.journal.active_blockers()], ['execution-blocker'])

    def test_mixed_legacy_completions_are_deduplicated_per_task(self):
        for task in ("one", "two"):
            self.event("work_completed", task + "-complete", task=task)
            self.event("session_finished", task + "-publication", "2026-11-12T11:00:00Z",
                       task=task, session_id="publish", finish_reason="completed")
        done = self.report()["done"]
        self.assertEqual(len(done), 2)
        self.assertEqual({row["task_id"] for row in done}, {"one", "two"})

    def test_retracting_first_completion_does_not_promote_publication_followup(self):
        self.event("session_finished", "implementation", "2026-11-11T10:00:00Z",
                   session_id="implementation", finish_reason="completed")
        self.event("session_finished", "published-summary", session_id="publication", finish_reason="completed")
        self.event("correction", "withdrawn", "2026-11-14T10:00:00Z", corrects="implementation")
        self.assertEqual(self.report()["done"], [])

    def test_terminal_task_state_removes_current_next_action(self):
        self.event("pause", "pending", next_action="Review guide")
        with patch("ase.state.now", return_value="2026-11-14T12:00:00+00:00"):
            self.tasks.transition("one", "CANCELLED", expected_revision=1, reason="No longer needed")
        self.assertEqual(self.report()["next_steps"], [])
        self.assertEqual(self.report(as_of_end=True)["next_steps"][0]["next_action"], "Review guide")

    def test_completed_task_state_removes_current_next_action(self):
        self.event("pause", "pending", next_action="Publish guide")
        snapshot, plan = self.tasks.snapshot("one"), self.tasks.execution_plan("one")
        # Isolate the already-covered completion gate; this tests the report's real state query.
        gate = {"status": "APPROVED", "contract_hash": snapshot["contract_hash"], "plan_hash": plan["plan_hash"]}
        with patch("ase.gates.GateService.gate", return_value=gate):
            self.tasks.transition("one", "COMPLETED", expected_revision=1, reason="Guide complete")
        self.assertEqual(self.report()["next_steps"], [])

    def test_terminal_closure_does_not_resurrect_older_next_action(self):
        self.event("pause", "pending", next_action="Review guide")
        self.event("session_finished", "closed", "2026-11-12T11:00:00Z", finish_reason="cancelled",
                   next_action="No task work remains")
        self.assertEqual(self.report(as_of_end=True)["next_steps"], [])

    def test_late_correction_retracts_current_done_but_preserves_historical_cutoff(self):
        self.event("work_completed", "done", source_refs=["guide.txt"])
        self.event("correction", "withdrawn", "2026-11-14T10:00:00Z", corrects="done")
        self.assertEqual(self.report()["done"], [])
        self.assertEqual([row["event_key"] for row in self.report(as_of_end=True)["done"]], ["done"])
        self.assertEqual([row["event_key"] for row in self.report()["corrections"]], ["withdrawn"])

    def test_late_correction_retracts_verification_and_next_action(self):
        self.event("check", "passed", status="PASS", evidence_refs=["receipt:pass"], next_action="Publish")
        self.event("correction", "invalid-receipt", "2026-11-14T10:00:00Z", corrects="passed")
        report = self.report()
        self.assertEqual(report["verified"], [])
        self.assertEqual(report["checked"], [])
        self.assertEqual(report["next_steps"], [])

    def test_retracted_latest_next_action_does_not_resurrect_an_older_action(self):
        self.event("pause", "old-action", "2026-11-11T10:00:00Z", next_action="Use superseded destination")
        self.event("pause", "latest-action", next_action="Use final destination")
        self.event("correction", "withdrawn", "2026-11-14T10:00:00Z", corrects="latest-action")
        self.assertEqual(self.report()["next_steps"], [])

    def test_corrected_blocker_opening_is_retracted_only_in_current_view(self):
        self.event("blocker_opened", "blocked", blocker={"id": "access", "reason": "Missing access", "next_probe": "Check"})
        self.event("correction", "wrong-blocker", "2026-11-14T10:00:00Z", corrects="blocked")
        self.assertEqual(self.report()["blockers"], [])
        self.assertEqual([row["id"] for row in self.report(as_of_end=True)["blockers"]], ["access"])

    def test_corrected_resolution_reopens_carried_blocker(self):
        self.event("blocker_opened", "blocked", "2026-11-11T10:00:00Z",
                   blocker={"id": "access", "reason": "Missing access", "next_probe": "Check"})
        self.event("blocker_resolved", "resolved", blocker_id="access")
        self.event("correction", "still-blocked", "2026-11-14T10:00:00Z", corrects="resolved")
        self.assertEqual([row["id"] for row in self.report()["blockers"]], ["access"])
        self.assertEqual(self.report(as_of_end=True)["blockers"], [])

    def test_late_correction_amends_carried_blocker_in_current_projection(self):
        self.event("blocker_opened", "blocked", "2026-11-11T10:00:00Z",
                   blocker={"id": "access", "reason": "Missing access", "next_probe": "Check"})
        self.event("correction", "withdrawn", "2026-11-14T10:00:00Z", corrects="blocked")
        self.assertEqual(self.report()["carried_blockers"], [])
        self.assertEqual([row["id"] for row in self.report(as_of_end=True)["carried_blockers"]], ["access"])

    def test_report_service_accepts_store_and_repository_without_coordinator(self):
        from ase.memory.repository import JournalRepository
        from ase.memory.reports import ReportService
        self.event("decision", "choice")
        repository = JournalRepository(self.store)
        service = ReportService(self.store, repository, clock=lambda: "2026-11-15T12:00:00+00:00")
        report = service.meeting_report("2026-11-12", "2026-11-13", timezone="UTC")
        self.assertEqual([row["summary"] for row in report["decisions"]], ["choice"])
        first_read = repository.events()
        first_read[0]["summary"] = "Modified caller copy"
        self.assertEqual(repository.events()[0]["summary"], "choice")

    def test_correction_of_correction_restores_fact(self):
        self.event("decision", "choice")
        self.event("correction", "retract", "2026-11-13T10:00:00Z", corrects="choice")
        self.event("correction", "undo-retract", "2026-11-14T10:00:00Z", corrects="retract")
        self.assertEqual([row["event_key"] for row in self.report()["decisions"]], ["choice"])

    def test_multiple_amendments_require_all_active_retractions_to_be_retracted(self):
        self.event("decision", "choice")
        self.event("correction", "retract-one", "2026-11-13T10:00:00Z", corrects="choice")
        self.event("correction", "retract-two", "2026-11-13T11:00:00Z", corrects="choice")
        self.event("correction", "undo-one", "2026-11-14T10:00:00Z", corrects="retract-one")
        self.assertEqual(self.report()["decisions"], [])

    def test_completed_summary_alias_correction_retracts_explicit_fact(self):
        self.event("work_completed", "completed", session_id="implementation")
        self.event("session_finished", "closed", session_id="implementation", finish_reason="completed")
        self.event("correction", "withdrawn", "2026-11-14T10:00:00Z", corrects="closed")
        self.assertEqual(self.report()["done"], [])

    def test_explicit_completion_correction_does_not_fall_back_to_summary_alias(self):
        self.event("work_completed", "completed", session_id="implementation")
        self.event("session_finished", "closed", session_id="implementation", finish_reason="completed")
        self.event("correction", "withdrawn", "2026-11-14T10:00:00Z", corrects="completed")
        self.assertEqual(self.report()["done"], [])

    def test_retracted_completion_summary_correction_restores_explicit_alias(self):
        self.event("work_completed", "completed", session_id="implementation")
        self.event("session_finished", "closed", session_id="implementation", finish_reason="completed")
        self.event("correction", "withdrawn", "2026-11-13T10:00:00Z", corrects="closed")
        self.event("correction", "restored", "2026-11-14T10:00:00Z", corrects="withdrawn")
        self.assertEqual([row["event_key"] for row in self.report()["done"]], ["completed"])

    def test_backdated_correction_chain_follows_append_dependencies(self):
        self.event("decision", "choice")
        self.event("correction", "withdrawn", "2026-11-14T10:00:00Z", corrects="choice")
        self.event("correction", "restored", "2026-11-13T10:00:00Z", corrects="withdrawn")
        self.assertEqual([row["event_key"] for row in self.report()["decisions"]], ["choice"])

    def test_late_correction_of_out_of_period_alias_is_linked_to_period_fact(self):
        self.event("work_completed", "completed", session_id="implementation")
        self.event("session_finished", "closed", "2026-11-13T10:00:00Z", session_id="implementation", finish_reason="completed")
        self.event("correction", "withdrawn", "2026-11-14T10:00:00Z", corrects="closed")
        self.assertEqual(self.report()["done"], [])
        self.assertEqual([row["event_key"] for row in self.report()["corrections"]], ["withdrawn"])

    def test_correction_at_period_end_is_excluded_from_historical_view(self):
        self.event("work_completed", "completed")
        self.event("correction", "withdrawn", "2026-11-13T00:00:00Z", corrects="completed")
        self.assertEqual([row["event_key"] for row in self.report(as_of_end=True)["done"]], ["completed"])
        self.assertEqual(self.report()["done"], [])

    def test_future_amendment_does_not_affect_current_view(self):
        self.event("work_completed", "completed")
        self.event("correction", "future", "2026-11-16T10:00:00Z", corrects="completed")
        self.assertEqual([row["event_key"] for row in self.report()["done"]], ["completed"])
        self.assertEqual(self.report()["corrections"], [])

    def test_reports_preserve_raw_rows_and_link_late_amendment_to_original(self):
        self.event("work_completed", "completed")
        self.event("correction", "withdrawn", "2026-11-14T10:00:00Z", corrects="completed")
        before = [tuple(row) for row in self.store.db.execute("SELECT * FROM runtime_events ORDER BY sequence")]
        report = self.report()
        self.assertEqual([row["event_key"] for row in report["events"]], ["completed"])
        self.assertIn("effective_events", report)
        self.assertEqual(report["effective_events"], [])
        self.assertEqual(report["amendments"][0]["corrects"], "completed")
        self.assertEqual(before, [tuple(row) for row in self.store.db.execute("SELECT * FROM runtime_events ORDER BY sequence")])


if __name__ == "__main__":
    unittest.main()
