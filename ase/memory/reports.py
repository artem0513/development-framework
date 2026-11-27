"""Read-only effective facts derived from append-only journal history.

A plain correction retracts its target; its prose never becomes an inferred
replacement. Retraction of a correction restores its target unless another
active correction also retracts that target. Historical views use occurrence
cutoffs, rather than pretending to reconstruct what was recorded at the time.
"""
from datetime import date, datetime, time, timezone as utc_timezone

from ..store import now
from ..timezones import resolve_timezone
from .repository import JournalRepository


def _order(event):
    return event["occurred_at_utc"], event["sequence"]


def _completion_group(event):
    identity = ("session", event["session_id"]) if "session_id" in event else ("event", event["event_key"])
    return event["task_id"], identity


def _is_completion(event):
    return (event["kind"] == "work_completed" or
            event["kind"] == "session_finished" and event.get("finish_reason") == "completed")


class ReportService:
    def __init__(self, store, repository=None, *, clock=None, timezone_resolver=None):
        self.store = store
        self.repository = repository if repository is not None else JournalRepository(store)
        self.clock = clock if clock is not None else now
        self.timezone_resolver = timezone_resolver if timezone_resolver is not None else resolve_timezone

    @staticmethod
    def _effective(events):
        # append() requires an existing target, so reverse sequence traversal
        # resolves a correction DAG even when occurrence dates are backdated.
        aliases = {}
        for event in events:
            if _is_completion(event):
                group = _completion_group(event)
                aliases.setdefault(group, set()).add(event["event_key"])
        by_key = {event["event_key"]: event for event in events}
        retracted, effective = set(), []
        for event in sorted(events, key=lambda e: e["sequence"], reverse=True):
            if event["event_key"] in retracted:
                continue
            effective.append(event)
            if event["kind"] == "correction":
                retracted.add(event["corrects"])
                target = by_key.get(event["corrects"])
                if target is not None and _is_completion(target):
                    retracted.update(aliases.get(_completion_group(target), ()))
        # An alias may have been appended after the correction and therefore
        # visited earlier. Apply the final retraction set to those rows too.
        return sorted((e for e in effective if e["event_key"] not in retracted), key=_order)

    @staticmethod
    def _active_blockers(events):
        active = {}
        for event in sorted(events, key=_order):
            if event["kind"] == "blocker_opened":
                active[(event["task_id"], event["blocker"]["id"])] = {
                    **event["blocker"], "task_id": event["task_id"],
                    "opened_at_utc": event["occurred_at_utc"], "event_key": event["event_key"]}
            elif event["kind"] == "blocker_resolved":
                active.pop((event["task_id"], event["blocker_id"]), None)
        return list(active.values())

    @staticmethod
    def _local(event, zone):
        local = datetime.fromisoformat(event["occurred_at_utc"]).astimezone(zone)
        return {**event, "local_date": local.date().isoformat(), "local_time": local.isoformat()}

    def _worklog(self, start_date, until_date, *, timezone, as_of_end=False):
        zone = self.timezone_resolver(timezone)
        try:
            start, until = date.fromisoformat(start_date), date.fromisoformat(until_date)
            if start.isoformat() != start_date or until.isoformat() != until_date or start >= until:
                raise ValueError()
        except (ValueError, TypeError):
            raise ValueError("Dates require an ascending half-open YYYY-MM-DD range") from None
        start_utc = datetime.combine(start, time.min, zone).astimezone(utc_timezone.utc).isoformat()
        until_utc = datetime.combine(until, time.min, zone).astimezone(utc_timezone.utc).isoformat()
        stamp = self.clock()
        all_events = sorted(self.repository.events(), key=_order)
        historical = [e for e in all_events if e["occurred_at_utc"] < until_utc]
        current = [e for e in all_events if e["occurred_at_utc"] <= stamp]
        visible = historical if as_of_end else current
        effective = self._effective(visible)
        period = [self._local(e, zone) for e in all_events if start_utc <= e["occurred_at_utc"] < until_utc]
        facts = [self._local(e, zone) for e in effective
                 if start_utc <= e["occurred_at_utc"] < until_utc and e["kind"] != "correction"]
        # Include late amendments to period facts, carried blockers and prior
        # next actions, including corrections of corrections in the same chain.
        related = {e["event_key"] for e in period}
        related.update(e["event_key"] for e in visible if e["occurred_at_utc"] < until_utc
                       and (e["kind"] in {"blocker_opened", "blocker_resolved"} or e.get("next_action")))
        completion_aliases = [e for e in visible if e["kind"] == "work_completed" or
                              e["kind"] == "session_finished" and e.get("finish_reason") == "completed"]
        related_groups = {_completion_group(e) for e in completion_aliases if e["event_key"] in related}
        related.update(e["event_key"] for e in completion_aliases if _completion_group(e) in related_groups)
        active_keys = {e["event_key"] for e in effective}
        amendments = []
        for event in sorted(visible, key=lambda e: e["sequence"]):
            if event["kind"] == "correction" and (event["event_key"] in related or event["corrects"] in related):
                related.add(event["event_key"])
                amendments.append({**self._local(event, zone), "amendment_active": event["event_key"] in active_keys})
        report = {
            "project_id": self.store.project_id, "start_date": start_date, "until_date": until_date, "timezone": timezone,
            "start_at_utc": start_utc, "until_at_utc": until_utc, "events": period, "effective_events": facts,
            "amendments": amendments, "effective_at_utc": until_utc if as_of_end else stamp,
            "effective_view": "as_of_end" if as_of_end else "current",
            "current_blockers": self._active_blockers(self._effective(current)),
            "blockers_as_of_end": self._active_blockers(self._effective(historical)),
            "period_blockers": [e for e in period if e["kind"] in {"blocker_opened", "blocker_resolved"}],
            "carried_blockers": self._active_blockers([e for e in effective if e["occurred_at_utc"] < start_utc]),
            "note": "Occurrence dates, not recording dates. Raw events are history; effective facts apply explicit retractions. No human time inferred."}
        return report, visible, effective, zone

    def worklog(self, start_date, until_date, *, timezone):
        return self._worklog(start_date, until_date, timezone=timezone)[0]

    def current_blockers(self):
        stamp = self.clock()
        visible = [e for e in self.repository.events() if e["occurred_at_utc"] <= stamp]
        return self._active_blockers(self._effective(visible))

    @staticmethod
    def _completions(visible, effective):
        # Fix the first completion's identity before applying corrections, so a
        # withdrawn completion cannot promote a later publication-only closure.
        first = {}
        for event in sorted(visible, key=_order):
            if _is_completion(event):
                group = _completion_group(event)
                first.setdefault(event["task_id"], group)
        done, explicit = [], set()
        for event in effective:
            if event["kind"] != "work_completed":
                continue
            group = _completion_group(event)
            if group != first[event["task_id"]] or event["task_id"] in explicit:
                continue
            explicit.add(event["task_id"])
            done.append({**event, "completion_provenance": "explicit_work_completed"})
        for event in effective:
            if event["kind"] == "session_finished" and event.get("finish_reason") == "completed":
                group = _completion_group(event)
                if group == first[event["task_id"]] and event["task_id"] not in explicit:
                    done.append({**event, "completion_provenance": "inferred_session_finished"})
                    explicit.add(event["task_id"])
        return sorted(done, key=_order)

    def meeting_report(self, start_date, until_date, *, timezone, as_of_end=False):
        if type(as_of_end) is not bool:
            raise ValueError("as_of_end must be boolean")
        report, visible, effective, zone = self._worklog(start_date, until_date, timezone=timezone, as_of_end=as_of_end)
        events = report["effective_events"]
        report.update(
            done=[self._local(e, zone) for e in self._completions(visible, effective)
                  if report["start_at_utc"] <= e["occurred_at_utc"] < report["until_at_utc"]],
            checked=[e for e in events if e["kind"] == "check"],
            verified=[e for e in events if e["kind"] == "check" and e["status"] == "PASS"],
            stage_results=[e for e in events if e["kind"] == "stage_result"],
            decisions=[e for e in events if e["kind"] == "decision"], corrections=report["amendments"],
            blockers=report["blockers_as_of_end"] if as_of_end else report["current_blockers"], as_of_end=as_of_end)
        for kind in ("committed", "published", "deployed", "merged"):
            report[kind] = [e for e in events if e["kind"] == kind]
        states = self.repository.task_states(before=report["until_at_utc"] if as_of_end else None)
        terminal = {task_id for task_id, state in states.items() if state in {"COMPLETED", "CANCELLED"}}
        terminal.update(e["task_id"] for e in effective
                        if e["kind"] == "session_finished" and e.get("finish_reason") in {"completed", "cancelled"})
        latest = {}
        for event in visible:
            if event["task_id"] not in terminal and event.get("next_action") and event["kind"] != "correction":
                latest[event["task_id"]] = {"task_id": event["task_id"], "next_action": event["next_action"], "event_key": event["event_key"]}
        active_keys = {e["event_key"] for e in effective}
        # Retraction withdraws the latest action, rather than reviving an older
        # superseded action. A new next action requires another explicit event.
        report["next_steps"] = [row for row in latest.values() if row["event_key"] in active_keys]
        report["detail_refs"] = list(dict.fromkeys("journal:" + str(e["sequence"]) for e in report["events"] + report["amendments"]))
        return report
