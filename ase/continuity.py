"""Canonical SQLite task handoffs and optional source coverage, never authority."""
import json
import re
from pathlib import Path

from .core import redact
from .inspection import safe_git, safe_snapshot
from .store import now


def _text(value, name, required=True):
    if not isinstance(value, str) or (required and not value.strip()) or len(value) > 20000:
        raise ValueError(name + " must be a nonempty bounded string")
    if redact(value) != value:
        raise ValueError(name + " must not contain credentials")
    return value


def _strings(value, name):
    if not isinstance(value, list) or len(value) > 1000:
        raise ValueError(name + " must be a bounded string list")
    return [_text(item, name) for item in value]


def _observation(repo):
    if not (Path(repo) / ".git").exists():
        return {"repository": str(Path(repo).resolve()), "branch": None, "commit": None,
                "fingerprint": None, "worktree": None, "kind": "filesystem",
                "limitation": "Root identity only; v2 scoped source observations establish content drift"}
    observed = safe_snapshot(repo)
    # rev-parse prints literal HEAD for an unborn branch even on failure.
    if not re.fullmatch(r"[a-fA-F0-9]{40,64}", observed["commit"] or ""):
        observed["commit"] = None
    observed.pop("files")
    observed["repository"] = str(repo)
    observed["worktree"] = safe_git(repo, "status", "--porcelain=v1")
    return observed


class Continuity:
    def __init__(self, store):
        self.store = store

    def snapshot(self, task_id):
        self.store.task(task_id)
        row = self.store.db.execute("SELECT * FROM checkpoints WHERE task_id=? ORDER BY revision DESC LIMIT 1", (task_id,)).fetchone()
        if row is None:
            return {"task_id": task_id, "revision": 0, "payload": None, "observation": None}
        result = dict(row)
        for key in ("payload", "observation"):
            result[key] = json.loads(result[key])
        return result

    def checkpoint(self, task_id, expected_revision, payload):
        self.store.task(task_id)
        if type(expected_revision) is not int or expected_revision < 0:
            raise ValueError("expected_revision must be a nonnegative integer")
        allowed = {"stage", "summary", "next_action", "blockers", "source_refs", "task_url", "changed_paths", "tests", "decisions", "remaining"}
        if not isinstance(payload, dict) or set(payload) - allowed:
            raise ValueError("Unknown checkpoint fields")
        for key in ("stage", "summary", "next_action"):
            _text(payload.get(key), key)
        for key in ("blockers", "source_refs", "changed_paths", "tests", "decisions", "remaining"):
            if key in payload:
                _strings(payload[key], key)
        if "task_url" in payload:
            _text(payload["task_url"], "task_url")
        observation = _observation(self.store.repo)
        db = self.store.db
        db.execute("BEGIN IMMEDIATE")
        try:
            if self.snapshot(task_id)["revision"] != expected_revision:
                raise ValueError("Stale checkpoint revision; reload and reconcile before saving")
            if "task_url" in payload:
                old = db.execute("SELECT locator FROM task_identities WHERE task_id=?", (task_id,)).fetchone()
                if old and old[0] != payload["task_url"]:
                    raise ValueError("Task identity cannot change within a logical task")
                db.execute("INSERT OR IGNORE INTO task_identities VALUES(?,?)", (task_id, payload["task_url"]))
                bound = db.execute("SELECT task_id FROM task_identities WHERE locator=?", (payload["task_url"],)).fetchone()
                if not bound or bound[0] != task_id:
                    raise ValueError("Task identity already belongs to another task")
            db.execute("INSERT INTO checkpoints VALUES(?,?,?,?,?)", (task_id, expected_revision + 1,
                       json.dumps(payload, ensure_ascii=False), json.dumps(observation), now()))
            db.commit()
        except BaseException:
            db.rollback()
            raise
        return self.snapshot(task_id)

    def restore(self, task_id):
        from .delegations import DelegationRegistry
        from .coordinator import CoordinatorEvents
        checkpoint = self.snapshot(task_id)
        current = _observation(self.store.repo)
        previous = checkpoint["observation"]
        drift = [key for key in ("repository", "branch", "commit", "fingerprint", "worktree")
                 if previous and previous.get(key) != current.get(key)]
        identity = self.store.db.execute("SELECT locator FROM task_identities WHERE task_id=?", (task_id,)).fetchone()
        return {"task_id": task_id, "task": self.store.task(task_id), "task_url": identity[0] if identity else None,
                "decisions": [dict(r) for r in self.store.db.execute("SELECT * FROM decisions WHERE task_id=? ORDER BY id DESC LIMIT 20", (task_id,))],
                "sessions": [dict(r) for r in self.store.db.execute("SELECT * FROM sessions WHERE task_id=? ORDER BY id DESC LIMIT 10", (task_id,))],
                "checkpoint": checkpoint, "current": current, "drift": drift,
                "coverage": self.coverage(task_id), "delegations": DelegationRegistry(self.store).snapshot(task_id),
                "coordinator_events": CoordinatorEvents(self.store).snapshot(task_id),
                "authority": "Observations only; no execution or publication authorization"}

    def source_record(self, task_id, payload, expected_sequence):
        self.store.task(task_id)
        if type(expected_sequence) is not int or expected_sequence < 0:
            raise ValueError("expected_sequence must be a nonnegative integer")
        allowed = {"id", "locator", "revision", "extent", "unit", "ranges", "gaps", "status", "analysis", "blocker"}
        if not isinstance(payload, dict) or set(payload) - allowed:
            raise ValueError("Unknown source fields")
        for key in ("id", "locator", "revision"):
            _text(payload.get(key), key)
        if type(payload.get("extent")) is not int or payload["extent"] < 1:
            raise ValueError("extent must be a positive integer; use consistent line or cue units")
        if payload.get("unit") not in {"lines", "cues", "bytes"}:
            raise ValueError("unit must be lines, cues or bytes")
        status = payload.get("status")
        if status not in {"discovered", "acquired", "fully_read", "analyzed", "blocked"}:
            raise ValueError("Invalid source status")
        ranges = payload.get("ranges", [])
        if not isinstance(ranges, list) or len(ranges) > 10000:
            raise ValueError("ranges must be a bounded list")
        for item in ranges:
            if not isinstance(item, list) or len(item) != 2 or any(type(n) is not int for n in item) or not 1 <= item[0] <= item[1] <= payload["extent"]:
                raise ValueError("Invalid inclusive source range")
        merged = []
        for start, end in sorted(ranges):
            if merged and start <= merged[-1][1] + 1:
                merged[-1][1] = max(end, merged[-1][1])
            else:
                merged.append([start, end])
        value = dict(payload, ranges=merged, gaps=_strings(payload.get("gaps", []), "gaps"))
        for key in ("analysis", "blocker"):
            if key in value:
                _text(value[key], key)
        full = merged == [[1, value["extent"]]]
        if status in {"fully_read", "analyzed"} and not full:
            raise ValueError("Source status requires complete range coverage")
        if status == "analyzed" and (value["gaps"] or not value.get("analysis")):
            raise ValueError("Analyzed requires analysis reference and no unresolved gaps")
        if status == "blocked" and not value.get("blocker"):
            raise ValueError("Blocked source requires a concrete blocker")
        # Each record replaces all ranges for that revision. Old revisions are
        # retained as history; nothing is inherited when a source changes.
        db = self.store.db
        db.execute("BEGIN IMMEDIATE")
        try:
            actual = db.execute("SELECT COALESCE(MAX(sequence),0) FROM source_history WHERE task_id=? AND source_id=?", (task_id, value["id"])).fetchone()[0]
            if actual != expected_sequence:
                raise ValueError("Stale source sequence; reload and reconcile before saving")
            db.execute("INSERT INTO source_history(task_id,source_id,payload,created) VALUES(?,?,?,?)",
                                  (task_id, value["id"], json.dumps(value, ensure_ascii=False), now()))
            db.commit()
        except BaseException:
            db.rollback()
            raise
        return self.coverage(task_id)

    def coverage(self, task_id):
        self.store.task(task_id)
        rows = self.store.db.execute("SELECT sequence,payload FROM source_history WHERE sequence IN (SELECT MAX(sequence) FROM source_history WHERE task_id=? GROUP BY source_id) ORDER BY source_id", (task_id,)).fetchall()
        sources = []
        for row in rows:
            source = json.loads(row["payload"])
            source["sequence"] = row["sequence"]
            unread, cursor = [], 1
            for start, end in source["ranges"]:
                if cursor < start:
                    unread.append([cursor, start - 1])
                cursor = end + 1
            if cursor <= source["extent"]:
                unread.append([cursor, source["extent"]])
            source["unread_ranges"] = unread
            sources.append(source)
        complete = bool(sources) and all(s["status"] == "analyzed" and not s["gaps"] and not s["unread_ranges"] for s in sources)
        status = "UNCONFIGURED" if not sources else "COMPLETE" if complete else "BLOCKED" if any(s["status"] == "blocked" for s in sources) else "PARTIAL"
        return {"task_id": task_id, "status": status, "complete": complete, "sources": sources,
                "limitation": "Declared source inventory and ranges; not proof of semantic analysis or independent gate approval"}
