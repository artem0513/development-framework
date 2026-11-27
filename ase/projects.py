"""Local project UUIDs and explicit workspace aliases, without Git dependency."""
from dataclasses import dataclass
from datetime import datetime, timezone as utc_timezone
import hashlib
import os
from pathlib import Path
import re
import sqlite3
import uuid

from .project_local import find_local_config, validate_local_state
from .native_paths import state_io
from .timezones import resolve_timezone


def _now():
    return datetime.now(utc_timezone.utc).isoformat()


def local_config(project=None):
    """Find selected path configuration without treating a UUID as cwd."""
    if project is not None:
        path = Path(project).expanduser()
        if not path.is_dir():
            return None
        return find_local_config(path)
    return find_local_config()


def resolve_state_home(home=None, project=None):
    if home is not None:
        selected = Path(home).expanduser()
    else:
        config = local_config(project)
        if config is not None:
            selected = config["root"] / ".devframework" / "local"
        else:
            configured = os.environ.get("ASE_HOME")
            base = Path(os.environ.get("LOCALAPPDATA", Path.home() / ".local" / "share"))
            selected = Path(configured).expanduser() if configured else base / "AutonomousSE"
    # Validate local aliases before canonicalization; shared-home aliases retain
    # the legacy canonical-home behavior.
    validate_local_state(selected)
    return selected.resolve()


def _canonical(root):
    return os.path.normcase(str(Path(root).expanduser().resolve()))


def _safe_state(path):
    """Reject redirected directories and shared file inodes below canonical home."""
    path = Path(path)
    if not path.is_absolute():
        raise ValueError("State files must remain inside their project directory; reparse points are not supported")
    state_io(path)


def _outside(home, root):
    if validate_local_state(home, project_root=root):
        return True
    if home == root or home.is_relative_to(root):
        raise ValueError("State home must be outside the repository or workspace")


@dataclass(frozen=True)
class ProjectContext:
    project_id: str
    workspace_id: str
    root: Path
    storage_key: str
    kind: str
    state_home: Path
    timezone: str
    revision: int = 1


class ProjectRegistry:
    def __init__(self, state_home=None, *, project=None):
        self.state_home = resolve_state_home(state_home, project=project)
        if project is not None and Path(project).expanduser().is_dir():
            config = local_config(project)
            _outside(self.state_home, config["root"] if config else Path(project).expanduser().resolve())
        state_io(self.state_home).mkdir(parents=True, exist_ok=True)
        for name in ("registry.sqlite3", "registry.sqlite3-wal", "registry.sqlite3-shm", "projects"):
            _safe_state(self.state_home / name)
        self.db = sqlite3.connect(state_io(self.state_home / "registry.sqlite3"), timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        try:
            self.db.execute("BEGIN IMMEDIATE")
            version = self.db.execute("PRAGMA user_version").fetchone()[0]
            if version > 1:
                raise ValueError("Project registry newer than this application")
            self.db.execute("""CREATE TABLE IF NOT EXISTS projects(
                project_id TEXT PRIMARY KEY,storage_key TEXT NOT NULL UNIQUE,
                kind TEXT NOT NULL,timezone TEXT NOT NULL,status TEXT NOT NULL,
                revision INTEGER NOT NULL,idempotency_key TEXT NOT NULL UNIQUE,
                current_workspace_id TEXT NOT NULL,created TEXT NOT NULL)""")
            self.db.execute("""CREATE TABLE IF NOT EXISTS workspaces(
                workspace_id TEXT PRIMARY KEY,project_id TEXT NOT NULL REFERENCES projects(project_id),
                root TEXT NOT NULL,canonical_root TEXT NOT NULL UNIQUE,created TEXT NOT NULL)""")
            self.db.execute("""CREATE TABLE IF NOT EXISTS aliases(
                alias TEXT PRIMARY KEY,workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id))""")
            self.db.execute("PRAGMA user_version=1")
            self.db.commit()
        except BaseException:
            self.db.rollback()
            self.db.close()
            raise

    def _lookup(self, selector):
        row = self.db.execute("""SELECT p.*,w.workspace_id,w.root FROM projects p
            JOIN workspaces w ON w.workspace_id=p.current_workspace_id
            WHERE p.project_id=?""", (str(selector),)).fetchone()
        if row is None:
            row = self.db.execute("""SELECT p.*,w.workspace_id,w.root FROM aliases a
                JOIN workspaces w ON w.workspace_id=a.workspace_id
                JOIN projects p ON p.project_id=w.project_id WHERE a.alias=?""",
                                  (_canonical(selector),)).fetchone()
        return row

    def _context(self, row):
        return ProjectContext(row["project_id"], row["workspace_id"], Path(row["root"]),
                              row["storage_key"], row["kind"], self.state_home,
                              row["timezone"], row["revision"])

    def resolve(self, selector):
        row = self._lookup(selector)
        if row is None:
            raise ValueError("Unknown project or workspace: " + str(selector))
        if row["status"] != "ACTIVE":
            raise ValueError("Inactive project registration: " + row["status"])
        _outside(self.state_home, Path(row["root"]))
        return self._context(row)

    def validate(self, context):
        actual = self.resolve(str(context.root))
        if (actual.project_id, actual.workspace_id, actual.storage_key, actual.kind,
                actual.state_home, actual.timezone) != (
                context.project_id, context.workspace_id, context.storage_key, context.kind,
                context.state_home, context.timezone):
            raise ValueError("Project context identity differs from registry")
        return actual

    def register(self, root, *, kind, timezone=None):
        return self._register(root, kind=kind, timezone=timezone)

    def for_root(self, root):
        """Compatibility locator: reuse existing metadata, register new paths only."""
        return self._register(root, kind=None, timezone=None)

    def _register(self, root, *, kind, timezone):
        root = Path(root).expanduser().resolve(strict=True)
        if not root.is_dir():
            raise ValueError("Project root must be a directory")
        valid_local_home = _outside(self.state_home, root)
        if kind is not None and (not isinstance(kind, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", kind)):
            raise ValueError("Project kind must be a concise identifier")
        if timezone is not None and (not isinstance(timezone, str) or not re.fullmatch(r"[A-Za-z0-9_+./-]{1,128}", timezone)):
            raise ValueError("Project timezone must be local or an IANA timezone identifier")
        self.db.execute("BEGIN IMMEDIATE")
        try:
            row = self._lookup(str(root))
            if row is not None:
                if ((kind is not None and kind != row["kind"])
                        or (timezone is not None and timezone != row["timezone"])):
                    raise ValueError("Conflict: workspace already registered with different metadata")
                project_id = row["project_id"]
            else:
                if valid_local_home and self.db.execute("SELECT 1 FROM projects LIMIT 1").fetchone():
                    raise ValueError("Existing project-local identity requires explicit project attach after relocation")
                config = local_config(root)
                default_timezone = config["timezone"] if config else "local"
                selected_timezone = timezone or default_timezone
                resolve_timezone(selected_timezone)
                project_id, workspace_id = str(uuid.uuid4()), str(uuid.uuid4())
                legacy_key = hashlib.sha256(_canonical(root).encode()).hexdigest()[:24]
                legacy = self.state_home / "projects" / legacy_key / "memory.sqlite3"
                _safe_state(legacy)
                storage_key = legacy_key if state_io(legacy).is_file() else project_id
                self.db.execute("INSERT INTO projects VALUES(?,?,?,?,?,?,?,?,?)", (
                    project_id, storage_key, kind or ("software" if (root / ".git").exists() else "workspace"),
                    selected_timezone, "PREPARING", 1,
                    hashlib.sha256(_canonical(root).encode()).hexdigest(), workspace_id, _now()))
                self.db.execute("INSERT INTO workspaces VALUES(?,?,?,?,?)", (
                    workspace_id, project_id, str(root), _canonical(root), _now()))
                self.db.execute("INSERT INTO aliases VALUES(?,?)", (_canonical(root), workspace_id))
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise
        self._prepare(project_id)
        return self.resolve(str(root))

    def _prepare(self, project_id):
        # PREPARING was committed first. This second transaction serializes
        # recovery across processes; it does not pretend to span both databases.
        from .store import Store
        self.db.execute("BEGIN IMMEDIATE")
        try:
            row = self._lookup(project_id)
            if row is None:
                raise ValueError("Unknown project: " + project_id)
            if row["status"] == "PREPARING":
                context = self._context(row)
                store = Store.__new__(Store)
                store._initialize(context, preparing=True)
                store.close()
                self._activate(project_id)
            elif row["status"] != "ACTIVE":
                raise ValueError("Inactive project registration: " + row["status"])
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise

    def _activate(self, project_id):
        self.db.execute("UPDATE projects SET status='ACTIVE' WHERE project_id=? AND status='PREPARING'", (project_id,))

    def recover(self):
        ids = [row[0] for row in self.db.execute("SELECT project_id FROM projects WHERE status='PREPARING' ORDER BY created")]
        recovered = []
        for project_id in ids:
            self._prepare(project_id)
            recovered.append(self.resolve(project_id))
        return recovered

    def attach(self, project_id, root, *, expected_revision):
        if type(expected_revision) is not int or expected_revision < 0:
            raise ValueError("Expected revision must be a nonnegative integer")
        root = Path(root).expanduser().resolve(strict=True)
        if not root.is_dir():
            raise ValueError("Project root must be a directory")
        _outside(self.state_home, root)
        self.db.execute("BEGIN IMMEDIATE")
        try:
            row = self._lookup(project_id)
            if row is None or row["project_id"] != project_id:
                raise ValueError("Unknown project: " + str(project_id))
            if row["status"] != "ACTIVE":
                raise ValueError("Inactive project registration: " + row["status"])
            if row["revision"] != expected_revision:
                raise ValueError("Stale project revision")
            existing = self._lookup(str(root))
            if existing is not None and existing["project_id"] != project_id:
                raise ValueError("Conflict: root belongs to another project")
            if existing is None:
                workspace_id = str(uuid.uuid4())
                self.db.execute("INSERT INTO workspaces VALUES(?,?,?,?,?)", (
                    workspace_id, project_id, str(root), _canonical(root), _now()))
                self.db.execute("INSERT INTO aliases VALUES(?,?)", (_canonical(root), workspace_id))
            else:
                workspace_id = existing["workspace_id"]
            self.db.execute("UPDATE projects SET current_workspace_id=?,revision=revision+1 WHERE project_id=?", (workspace_id, project_id))
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise
        return self.resolve(project_id)

    def close(self):
        self.db.close()
