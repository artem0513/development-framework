"""Project-isolated transactional SQLite memory, without raw chat retention."""
import json
import os
from pathlib import Path
import sqlite3
import time
from datetime import datetime, timezone

from .projects import ProjectContext, ProjectRegistry, _canonical, _outside, _safe_state, local_config, resolve_state_home
from .native_paths import state_io


def now():
    return datetime.now(timezone.utc).isoformat()


def default_home():
    return resolve_state_home()


class Store:
    def __init__(self, repo, home=None):
        if isinstance(repo, ProjectContext):
            if home is not None and resolve_state_home(home) != repo.state_home:
                raise ValueError("Project context state home conflicts with explicit home")
            _outside(repo.state_home, repo.root)
            registry = ProjectRegistry(repo.state_home, project=repo.root)
            try:
                project = registry.validate(repo)
            finally:
                registry.close()
        else:
            config = local_config(repo)
            root = config["root"] if config else Path(repo).expanduser().resolve(strict=True)
            selected_home = resolve_state_home(home, project=root)
            _outside(selected_home, root)
            registry = ProjectRegistry(selected_home, project=root)
            try:
                project = registry.for_root(root)
            finally:
                registry.close()
        self._initialize(project)

    def _initialize(self, project, *, preparing=False):
        config = local_config(project.root)
        default_retention = config["log_retention_days"] if config else 30
        try:
            self.log_retention_days = int(default_retention if config else os.environ.get("ASE_LOG_RETENTION_DAYS", "30"))
        except ValueError:
            raise ValueError("ASE_LOG_RETENTION_DAYS must be an integer from 1 to 365") from None
        if not 1 <= self.log_retention_days <= 365:
            raise ValueError("ASE_LOG_RETENTION_DAYS must be an integer from 1 to 365")
        self.project = project
        self.repo = project.root.resolve(strict=True)
        self.project_id = project.project_id
        self.workspace_id = project.workspace_id
        self.state_home = project.state_home
        valid_local_home = _outside(self.state_home, self.repo)
        if (not project.storage_key or Path(project.storage_key).name != project.storage_key
                or project.storage_key in {".", ".."}):
            raise ValueError("Invalid project storage key")
        self.root = self.state_home / "projects" / project.storage_key
        _safe_state(self.root)
        for name in ("memory.sqlite3", "memory.sqlite3-wal", "memory.sqlite3-shm", "backups"):
            path = self.root / name
            _safe_state(path)
        if not valid_local_home and (self.root == self.repo or self.root.is_relative_to(self.repo)):
            raise ValueError("State home must be outside the repository")
        state_io(self.root).mkdir(parents=True, exist_ok=True)
        database = state_io(self.root / "memory.sqlite3")
        if not preparing and not database.is_file():
            raise ValueError("Missing project memory database; explicit recovery required")
        self.db = sqlite3.connect(database, timeout=30)
        try:
            self.db.row_factory = sqlite3.Row
            self.db.execute("PRAGMA foreign_keys=ON")
            self.db.execute("PRAGMA journal_mode=WAL")
            version = self.db.execute("PRAGMA user_version").fetchone()[0]
            if preparing and 0 < version <= 5:
                legacy = self.db.execute("SELECT repo FROM projects WHERE id IN (?,?)",
                                         (self.project_id, project.storage_key)).fetchall()
                pending_empty = (project.storage_key == self.project_id
                                 and not self.db.execute("SELECT 1 FROM projects LIMIT 1").fetchone())
                if not pending_empty and not any(_canonical(row[0]) == _canonical(self.repo) for row in legacy):
                    raise ValueError("Legacy project database identity does not match selected workspace")
            self.migrate()
            with self.db:
                identity = self.db.execute("SELECT payload FROM runtime_records WHERE kind='project_identity' AND key='identity'").fetchone()
                expected = {"project_id": self.project_id, "storage_key": project.storage_key}
                if identity is not None and json.loads(identity[0]) != expected:
                    raise ValueError("Project database identity conflicts with registry")
                if identity is None:
                    if not preparing:
                        raise ValueError("Missing project database identity; explicit recovery required")
                    self.db.execute("INSERT INTO runtime_records VALUES(?,?,?,?,?)", (
                        "project_identity", "identity", 1, json.dumps(expected), now()))
                self.db.execute("INSERT OR IGNORE INTO projects VALUES(?,?,?,?)", (
                    self.project_id, str(self.repo), "{}", now()))
            if not preparing:
                self.prune_evidence()
        except BaseException:
            self.db.close()
            raise

    def prune_evidence(self):
        """Remove expired known verbose evidence, preserving structured memory.

        A lock is treated conservatively as active until explicit recovery. No
        operation locks, receipts, summaries, source or arbitrary files are deleted.
        Missing old logs make their old gates invalid, requiring fresh verification.
        """
        tasks = self.root / "tasks"
        if not tasks.is_dir() or tasks.is_symlink() or tasks.resolve().parent != self.root.resolve():
            return []
        cutoff = time.time() - self.log_retention_days * 86400
        removed = []
        for task in tasks.iterdir():
            if not task.is_dir() or task.is_symlink() or not task.resolve().is_relative_to(tasks.resolve()):
                continue
            if (task / "operation.lock").exists():
                continue
            candidates = [task / "handoff.json", task / "ac.session.log", task / "review.session.log"]
            candidates += [task / ("implementation-" + str(attempt) + ".log") for attempt in range(1, 6)]
            runs = task / "runs"
            if runs.is_dir() and not runs.is_symlink() and runs.resolve().is_relative_to(task.resolve()):
                for run in runs.iterdir():
                    if run.is_dir() and not run.is_symlink() and run.resolve().is_relative_to(task.resolve()):
                        candidates.extend(run.glob("*.log"))
            cycles = task / "cycles"
            if (cycles.is_dir() and not cycles.is_symlink()
                    and not getattr(cycles, "is_junction", lambda: False)()
                    and cycles.resolve().parent == task.resolve()):
                for cycle in cycles.iterdir():
                    if (cycle.is_dir() and not cycle.is_symlink()
                            and not getattr(cycle, "is_junction", lambda: False)()
                            and cycle.resolve().parent == cycles.resolve()):
                        candidates.extend(cycle / name for name in (
                            "handoff.json", "ac.session.log", "review.session.log"))
                        candidates.extend(cycle / ("implementation-" + str(attempt) + ".log")
                                          for attempt in range(1, 6))
            for path in candidates:
                try:
                    if (path.is_symlink() or not path.is_file()
                            or not path.resolve().is_relative_to(task.resolve())
                            or path.stat().st_nlink > 1):
                        continue
                    if path.stat().st_mtime < cutoff:
                        path.unlink()
                        removed.append(str(path))
                except FileNotFoundError:
                    # Another read-only CLI invocation may prune the same file.
                    pass
        return removed

    def migrate(self):
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version > 5:
            raise ValueError("Memory schema newer than this application")
        if version == 0:
            self.db.executescript("""
            BEGIN IMMEDIATE;
            CREATE TABLE projects(id TEXT PRIMARY KEY,repo TEXT NOT NULL,configuration TEXT NOT NULL,created TEXT NOT NULL);
            CREATE TABLE tasks(id TEXT PRIMARY KEY,request TEXT NOT NULL,status TEXT NOT NULL,contract TEXT NOT NULL,created TEXT NOT NULL,updated TEXT NOT NULL);
            CREATE TABLE sessions(id INTEGER PRIMARY KEY,task_id TEXT REFERENCES tasks(id),started TEXT NOT NULL,ended TEXT,summary TEXT NOT NULL);
            CREATE TABLE decisions(id INTEGER PRIMARY KEY,task_id TEXT REFERENCES tasks(id),context TEXT NOT NULL,decision TEXT NOT NULL,rationale TEXT NOT NULL,created TEXT NOT NULL);
            CREATE TABLE verification_runs(id INTEGER PRIMARY KEY,task_id TEXT REFERENCES tasks(id),fingerprint TEXT NOT NULL,outcome TEXT NOT NULL,artifact TEXT NOT NULL,created TEXT NOT NULL);
            CREATE TABLE time_entries(id INTEGER PRIMARY KEY,task_id TEXT REFERENCES tasks(id),activity TEXT NOT NULL,started TEXT NOT NULL,ended TEXT NOT NULL,seconds REAL NOT NULL,source TEXT NOT NULL,confirmed INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE reports(id INTEGER PRIMARY KEY,task_id TEXT REFERENCES tasks(id),type TEXT NOT NULL,content TEXT NOT NULL,created TEXT NOT NULL);
            CREATE TABLE transitions(id INTEGER PRIMARY KEY,task_id TEXT REFERENCES tasks(id),status TEXT NOT NULL,detail TEXT NOT NULL,created TEXT NOT NULL);
            PRAGMA user_version=1;
            COMMIT;
            """)

        if version < 2:
            if version == 1:
                self.backup()
            self.db.executescript("""
            BEGIN IMMEDIATE;
            CREATE TABLE time_entry_audit(id INTEGER PRIMARY KEY,entry_id INTEGER NOT NULL REFERENCES time_entries(id),before_json TEXT NOT NULL,after_json TEXT NOT NULL,reason TEXT NOT NULL,created TEXT NOT NULL);
            ALTER TABLE sessions ADD COLUMN important_decisions TEXT NOT NULL DEFAULT '';
            ALTER TABLE sessions ADD COLUMN unresolved_questions TEXT NOT NULL DEFAULT '';
            PRAGMA user_version=2;
            COMMIT;
            """)

        if version < 3:
            if version == 2:
                self.backup()
            self.db.executescript("""
            BEGIN IMMEDIATE;
            CREATE TABLE IF NOT EXISTS task_identities(task_id TEXT PRIMARY KEY REFERENCES tasks(id),locator TEXT NOT NULL UNIQUE);
            CREATE TABLE IF NOT EXISTS checkpoints(task_id TEXT NOT NULL REFERENCES tasks(id),revision INTEGER NOT NULL,payload TEXT NOT NULL,observation TEXT NOT NULL,created TEXT NOT NULL,PRIMARY KEY(task_id,revision));
            CREATE TABLE IF NOT EXISTS source_history(sequence INTEGER PRIMARY KEY,task_id TEXT NOT NULL REFERENCES tasks(id),source_id TEXT NOT NULL,payload TEXT NOT NULL,created TEXT NOT NULL);
            PRAGMA user_version=3;
            COMMIT;
            """)

        if version < 4:
            if version == 3:
                self.backup()
            self.db.executescript("""
            BEGIN IMMEDIATE;
            CREATE TABLE IF NOT EXISTS knowledge_claims(id INTEGER PRIMARY KEY,topic TEXT NOT NULL,statement TEXT NOT NULL,observed_at TEXT NOT NULL,source_refs TEXT NOT NULL,task_id TEXT REFERENCES tasks(id),repository TEXT,repository_revision TEXT,supersedes INTEGER UNIQUE REFERENCES knowledge_claims(id),created TEXT NOT NULL);
            PRAGMA user_version=4;
            COMMIT;
            """)

        if version < 5:
            if version == 4:
                self.backup()
            self.db.execute("BEGIN IMMEDIATE")
            try:
                self.db.execute("""CREATE TABLE IF NOT EXISTS runtime_records(
                    kind TEXT NOT NULL,key TEXT NOT NULL,revision INTEGER NOT NULL,
                    payload TEXT NOT NULL,updated TEXT NOT NULL,PRIMARY KEY(kind,key))""")
                self.db.execute("""CREATE TABLE IF NOT EXISTS runtime_events(
                    sequence INTEGER PRIMARY KEY,event_key TEXT NOT NULL UNIQUE,
                    kind TEXT NOT NULL,task_id TEXT,payload TEXT NOT NULL,created TEXT NOT NULL)""")
                self.db.execute("""CREATE TABLE IF NOT EXISTS task_discoveries(
                    task_id TEXT NOT NULL REFERENCES tasks(id),revision INTEGER NOT NULL,
                    payload TEXT NOT NULL,created TEXT NOT NULL,PRIMARY KEY(task_id,revision))""")
                self.db.execute("""INSERT OR IGNORE INTO runtime_records(kind,key,revision,payload,updated)
                    SELECT 'knowledge_validity',CAST(id AS TEXT),1,?,? FROM knowledge_claims""",
                                (json.dumps({"validity": "UNKNOWN"}), now()))
                self.db.execute("PRAGMA user_version=5")
                self.db.commit()
            except BaseException:
                self.db.rollback()
                raise

    def task(self, task_id):
        row = self.db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row is None:
            raise ValueError("Unknown task in this project: " + task_id)
        result = dict(row)
        result["contract"] = json.loads(result["contract"])
        return result

    def transition(self, task_id, status, detail=""):
        from .state import transaction
        with transaction(self.db):
            self.db.execute("UPDATE tasks SET status=?,updated=? WHERE id=?", (status, now(), task_id))
            self.db.execute("INSERT INTO transitions(task_id,status,detail,created) VALUES(?,?,?,?)", (task_id, status, detail, now()))

    def backup(self):
        folder = self.root / "backups"
        state_io(folder).mkdir(exist_ok=True)
        target = folder / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f") + ".sqlite3")
        out = sqlite3.connect(state_io(target))
        try:
            self.db.backup(out)
        finally:
            out.close()
        return str(target)

    def close(self):
        self.db.close()
