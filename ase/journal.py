"""Append-only UTC work history and transactional session closure.

Journal events are concise structured observations. They do not confer evidence
approval, infer human time, execute actions or send reports to external systems.
"""
import copy
from contextlib import contextmanager
from datetime import datetime, timezone as utc_timezone
import hashlib
from pathlib import Path

from .state import append_event, encode, hash_value, identifier, read_record, text, transaction, write_record
from .store import now
from .memory.repository import JournalRepository
from .memory.reports import ReportService
from .tasks import TaskService
from .timezones import resolve_timezone


FINISH_STATES = {"completed": "COMPLETED", "awaiting_approval": "AWAITING_APPROVAL",
                 "awaiting_lead_review": "REVIEWING",
                 "blocked": "BLOCKED", "paused": "PAUSED", "cancelled": "CANCELLED", "interrupted": "PAUSED",
                 "superseded": None}
EVENT_KINDS = {"work_started", "work_completed", "stage_result", "check", "decision", "blocker_opened",
               "blocker_resolved", "pause", "session_finished", "correction", "committed", "published", "deployed", "merged"}
PROVEN_KINDS = {"check", "committed", "published", "deployed", "merged"}


@contextmanager
def _finish_transaction(db):
    """A failed closure must also roll back inside a caller-owned transaction."""
    if not db.in_transaction:
        with transaction(db):
            yield db
        return
    db.execute("SAVEPOINT ase_journal_finish")
    try:
        yield db
    except BaseException:
        db.execute("ROLLBACK TO SAVEPOINT ase_journal_finish")
        db.execute("RELEASE SAVEPOINT ase_journal_finish")
        raise
    else:
        db.execute("RELEASE SAVEPOINT ase_journal_finish")


def _utc(value, name="occurred_at_utc"):
    text(value, name, limit=100)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(name + " requires an aware timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(name + " requires an aware timestamp")
    return parsed.astimezone(utc_timezone.utc).isoformat()


def _strings(value, name):
    if not isinstance(value, list) or len(value) > 1000:
        raise ValueError(name + " must be a bounded string list")
    return [text(item, name, limit=2000) for item in value]


def _blocker(value):
    allowed = {"id", "reason", "dependent_stage_ids", "owner", "expected_response", "next_probe"}
    if not isinstance(value, dict) or set(value) - allowed:
        raise ValueError("Invalid blocker fields")
    result = copy.deepcopy(value)
    identifier(result.get("id"), "blocker id")
    for key in ("reason", "next_probe"):
        text(result.get(key), "blocker " + key, limit=4000)
    result["dependent_stage_ids"] = _strings(result.get("dependent_stage_ids", []), "dependent_stage_ids")
    for key in ("owner", "expected_response"):
        if key in result:
            text(result[key], "blocker " + key, limit=2000)
    return result


def _context(value):
    if not isinstance(value, dict) or set(value) - {"summary", "decisions", "source_refs", "limitations"}:
        raise ValueError("Unknown project context fields")
    text(value.get("summary"), "context summary", limit=4000)
    result = copy.deepcopy(value)
    for key in ("decisions", "source_refs", "limitations"):
        if key in result:
            result[key] = _strings(result[key], key)
    return result


class Journal:
    def __init__(self, store, *, gate_provider=None):
        self.store = store
        self.tasks = TaskService(store)
        self.gate_provider = gate_provider
        self.repository = JournalRepository(store)

    def _event_row(self, event_key):
        return self.repository.event(event_key)

    def _validate_event(self, event):
        allowed = {"kind", "task_id", "session_id", "summary", "occurred_at_utc", "source_refs", "evidence_refs",
                   "blocker", "blocker_id", "corrects", "next_action", "finish_reason", "workflow_outcome", "status"}
        if not isinstance(event, dict) or set(event) - allowed:
            raise ValueError("Unknown journal event fields; retain concise structured observations only")
        value = copy.deepcopy(event)
        if value.get("kind") not in EVENT_KINDS:
            raise ValueError("Unknown journal event kind")
        self.store.task(identifier(value.get("task_id"), "task id"))
        text(value.get("summary"), "event summary", limit=4000)
        if "session_id" in value:
            identifier(value["session_id"], "session id")
        for key in ("source_refs", "evidence_refs"):
            if key in value:
                value[key] = _strings(value[key], key)
        if value["kind"] in PROVEN_KINDS and not value.get("evidence_refs"):
            raise ValueError(value["kind"] + " requires evidence references")
        if value["kind"] in {"check", "stage_result"}:
            statuses = {"PASS", "FAIL", "BLOCKED", "UNKNOWN"}
            if value["kind"] == "stage_result":
                statuses |= {"COMPLETED", "PAUSED"}
            if value.get("status") not in statuses:
                raise ValueError(value["kind"] + " requires an explicit result status")
        elif "status" in value:
            raise ValueError("Only checks and stage results carry result status")
        if value["kind"] == "blocker_opened":
            value["blocker"] = _blocker(value.get("blocker"))
        elif "blocker" in value:
            raise ValueError("Only blocker_opened carries a blocker")
        if value["kind"] == "blocker_resolved":
            identifier(value.get("blocker_id"), "blocker id")
        if value["kind"] == "correction":
            text(value.get("corrects"), "correction event key", limit=300)
            original = self._event_row(value["corrects"])
            if original is None or original["task_id"] != value["task_id"]:
                raise ValueError("A correction requires an existing event in the same task")
        elif "corrects" in value:
            raise ValueError("Only a correction may reference an original event")
        for key in ("next_action", "workflow_outcome"):
            if key in value:
                text(value[key], key, limit=4000)
        if "finish_reason" in value and value["finish_reason"] not in FINISH_STATES:
            raise ValueError("Invalid finish_reason")
        if "occurred_at_utc" in value:
            value["occurred_at_utc"] = _utc(value["occurred_at_utc"])
        text(encode(value), "event", limit=50000)
        return value

    def append(self, event, *, event_key):
        text(event_key, "event_key", limit=300)
        value = self._validate_event(event)
        request_hash = hash_value(value)
        with transaction(self.store.db) as db:
            previous = self._event_row(event_key)
            if previous is not None:
                if previous["request_hash"] != request_hash:
                    raise ValueError("CONFLICT: event key already describes another event")
                return previous
            stamp = now()
            value.setdefault("occurred_at_utc", stamp)
            value.update(recorded_at_utc=stamp, request_hash=request_hash)
            append_event(db, event_key, "journal", value["task_id"], value)
            return self._event_row(event_key)

    def context(self):
        from .memory.context import ContextService
        return ContextService(self.store).snapshot()

    def session(self, task_id, session_id):
        self.store.task(identifier(task_id, "task id"))
        identifier(session_id, "session id")
        return read_record(self.store.db, "work_session", task_id + ":" + session_id)

    def start_session(self, task_id, session_id):
        """Explicitly register a running session; reading state never repairs it."""
        with transaction(self.store.db) as db:
            current = self.session(task_id, session_id)
            if current["payload"] is not None:
                return current
            return write_record(db, "work_session", task_id + ":" + session_id,
                                {"task_id": task_id, "session_id": session_id, "status": "RUNNING", "started_at_utc": now()}, expected_revision=0)

    def _validate_finish(self, payload):
        allowed = {"finish_reason", "summary", "next_action", "context", "stage", "blockers", "source_refs",
                   "tests", "decisions", "remaining", "changed_paths", "events", "artifacts", "workflow_outcome"}
        if not isinstance(payload, dict) or set(payload) - allowed:
            raise ValueError("Unknown finish fields; gate approval is recomputed, never supplied")
        value = copy.deepcopy(payload)
        if value.get("finish_reason") not in FINISH_STATES:
            raise ValueError("Invalid finish_reason")
        for key in ("summary", "next_action"):
            text(value.get(key), key, limit=4000)
        value["context"] = _context(value.get("context"))
        for key in ("source_refs", "tests", "decisions", "remaining", "changed_paths"):
            if key in value:
                value[key] = _strings(value[key], key)
        for key in ("stage", "workflow_outcome"):
            if key in value:
                text(value[key], key, limit=4000)
        blockers = value.get("blockers", [])
        if not isinstance(blockers, list) or len(blockers) > 1000:
            raise ValueError("Invalid finish blockers")
        value["blockers"] = [_blocker(item) for item in blockers]
        if len({b["id"] for b in value["blockers"]}) != len(value["blockers"]):
            raise ValueError("Finish blockers require unique IDs")
        for key in ("events", "artifacts"):
            if not isinstance(value.get(key, []), list) or len(value.get(key, [])) > 1000:
                raise ValueError(key + " must be a bounded list")
        text(encode(value), "finish", limit=500000)
        return value

    def _verify_artifacts(self, artifacts):
        for artifact in artifacts:
            if not isinstance(artifact, dict) or set(artifact) != {"path", "sha256"}:
                raise ValueError("Artifact requires path and sha256")
            text(artifact["path"], "artifact path", limit=4000)
            path = Path(artifact["path"])
            if not path.is_absolute():
                path = self.store.repo / path
            try:
                if path.is_symlink() or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != artifact["sha256"]:
                    raise ValueError("Artifact missing or altered: " + artifact["path"])
            except OSError as error:
                raise ValueError("Artifact unavailable: " + artifact["path"]) from error

    def _current_gate(self, task_id):
        if self.gate_provider is not None:
            return self.gate_provider(task_id)
        from .gates import GateService
        return GateService(self.store).gate(task_id)

    def finish_session(self, task_id, session_id, payload, *, closure_key, expected_revision, expected_context_revision):
        identifier(task_id, "task id")
        identifier(session_id, "session id")
        text(closure_key, "closure_key", limit=200)
        for value in (expected_revision, expected_context_revision):
            if type(value) is not int or value < 0:
                raise ValueError("Expected revisions must be nonnegative integers")
        value = self._validate_finish(payload)
        request_hash = hash_value({"task_id": task_id, "session_id": session_id, "payload": value})
        db = self.store.db
        with _finish_transaction(db):
            previous = read_record(db, "session_closure", closure_key)
            if previous["payload"] is not None:
                if previous["payload"]["request_hash"] != request_hash:
                    raise ValueError("CONFLICT: closure key already describes another payload")
                return previous["payload"]["result"]
            task = self.tasks.snapshot(task_id)
            checkpoint_revision = db.execute("SELECT COALESCE(MAX(revision),0) FROM checkpoints WHERE task_id=?", (task_id,)).fetchone()[0]
            if checkpoint_revision != expected_revision:
                raise ValueError("Stale checkpoint revision; reload and reconcile")
            context = self.context()
            if context["revision"] != expected_context_revision:
                raise ValueError("Stale project context revision; reload and reconcile")
            session = self.session(task_id, session_id)
            if session["payload"] and session["payload"]["status"] != "RUNNING":
                raise ValueError("CONFLICT: session already closed under another closure key")
            self._verify_artifacts(value.get("artifacts", []))
            reason = value["finish_reason"]
            plan = self.tasks.execution_plan(task_id)
            if reason == "completed":
                gate = self._current_gate(task_id)
                if (not gate or gate.get("status") != "APPROVED" or gate.get("contract_hash") != task["contract_hash"]
                        or gate.get("plan_hash") != plan["plan_hash"]):
                    raise ValueError("COMPLETED requires a current APPROVED gate")
            else:
                gate = {"status": "BLOCKED", "current_verification": "NOT_RECOMPUTED",
                        "contract_hash": task["contract_hash"], "proposal_hash": task["proposal_hash"],
                        "plan_hash": plan["plan_hash"], "historical_gate": read_record(db, "gate", task_id)["payload"],
                        "blocked_reasons": ["Current verification was not recomputed during " + reason + " closure"]}
            # Transition validates current obligations as well as the gate. No
            # external operation records are altered when a session stops.
            target = FINISH_STATES[reason]
            if target is None:
                target = task["state"]
            else:
                if task["state"] in {"COMPLETED", "CANCELLED"} and reason != "completed":
                    target = task["state"]
                self.tasks.transition(task_id, target, expected_revision=task["revision"], reason=value["summary"])
            stamp = now()
            checkpoint = {"stage": value.get("stage", task["state"].lower()), "summary": value["summary"],
                          "next_action": value["next_action"], "blockers": [b["reason"] for b in value["blockers"]]}
            checkpoint.update({k: value[k] for k in ("source_refs", "tests", "decisions", "remaining", "changed_paths") if k in value})
            if (self.store.repo / ".git").exists():
                from .continuity import _observation
                observation = _observation(self.store.repo)
            else:
                # A non-Git project does not trigger an unscoped content scan.
                observation = {"repository": str(self.store.repo), "project_id": self.store.project_id,
                               "kind": "filesystem", "branch": None, "commit": None,
                               "fingerprint": None, "worktree": None,
                               "limitation": "Root identity only; scoped content is observed by the evidence gate"}
            db.execute("INSERT INTO checkpoints VALUES(?,?,?,?,?)", (task_id, expected_revision + 1, encode(checkpoint), encode(observation), stamp))
            next_context = {**value["context"], "workspace_id": self.store.workspace_id, "task_id": task_id, "next_action": value["next_action"], "updated_at_utc": stamp}
            write_record(db, "project_context", self.store.project_id, next_context, expected_revision=expected_context_revision)
            write_record(db, "project_context_version", self.store.project_id + ":" + str(expected_context_revision + 1), next_context, expected_revision=0)
            events = []
            for index, event in enumerate(value.get("events", [])):
                if not isinstance(event, dict) or event.get("task_id", task_id) != task_id or event.get("session_id", session_id) != session_id:
                    raise ValueError("Finish events must belong to the closing task and session")
                events.append(self.append({**event, "task_id": task_id, "session_id": session_id}, event_key="closure:" + closure_key + ":event:" + str(index)))
            for blocker in value["blockers"]:
                events.append(self.append({"kind": "blocker_opened", "task_id": task_id, "session_id": session_id,
                                           "summary": blocker["reason"], "blocker": blocker, "occurred_at_utc": stamp},
                                          event_key="closure:" + closure_key + ":blocker:" + blocker["id"]))
            if reason == "completed" and task["state"] != "COMPLETED":
                events.append(self.append({"kind": "work_completed", "task_id": task_id, "session_id": session_id,
                    "summary": value["summary"], "source_refs": value.get("source_refs", []), "occurred_at_utc": stamp},
                    event_key="closure:" + closure_key + ":completed"))
            events.append(self.append({"kind": "session_finished", "task_id": task_id, "session_id": session_id,
                                       "summary": value["summary"], "next_action": value["next_action"], "finish_reason": reason,
                                       "occurred_at_utc": stamp, "workflow_outcome": value.get("workflow_outcome", target)},
                                      event_key="closure:" + closure_key + ":summary"))
            from .memory.context import ContextService
            ContextService(self.store).record_task(task_id, next_context, summary_event=events[-1], finish_reason=reason)
            started = session["payload"]["started_at_utc"] if session["payload"] else stamp
            legacy = db.execute("INSERT INTO sessions(task_id,started,ended,summary,important_decisions,unresolved_questions) VALUES(?,?,?,?,?,?)",
                                (task_id, started, stamp, value["summary"], "\n".join(value.get("decisions", [])), "\n".join(b["reason"] for b in value["blockers"])))
            write_record(db, "work_session", task_id + ":" + session_id,
                         {"task_id": task_id, "session_id": session_id, "status": "INTERRUPTED" if reason == "interrupted" else "CLOSED",
                          "started_at_utc": started, "ended_at_utc": stamp, "finish_reason": reason, "closure_key": closure_key},
                         expected_revision=session["revision"])
            result = {"task_id": task_id, "session_id": session_id, "closure_key": closure_key, "finish_reason": reason,
                      "state": target, "summary": value["summary"], "next_action": value["next_action"],
                      "checkpoint_revision": expected_revision + 1, "context_revision": expected_context_revision + 1,
                      "gate": gate, "gate_recomputed": reason == "completed",
                      "contract_hash": task["contract_hash"], "proposal_hash": task["proposal_hash"], "plan_hash": plan["plan_hash"],
                      "workflow_outcome": value.get("workflow_outcome", target),
                      "journal_ids": [e["sequence"] for e in events], "legacy_session_id": legacy.lastrowid,
                      "closed_at_utc": stamp, "artifacts": value.get("artifacts", [])}
            write_record(db, "session_closure", closure_key, {"request_hash": request_hash, "result": result}, expected_revision=0)
            return result

    def _events(self):
        return self.repository.events()

    @staticmethod
    def _active_blockers(events):
        return ReportService._active_blockers(events)

    def _reports(self):
        # Preserve the Journal clock/timezone seams used by existing callers.
        return ReportService(self.store, self.repository, clock=now, timezone_resolver=resolve_timezone)

    def worklog(self, start_date, until_date, *, timezone):
        return self._reports().worklog(start_date, until_date, timezone=timezone)

    def active_blockers(self):
        return self._reports().current_blockers()

    def meeting_report(self, start_date, until_date, *, timezone, as_of_end=False):
        return self._reports().meeting_report(start_date, until_date, timezone=timezone, as_of_end=as_of_end)
