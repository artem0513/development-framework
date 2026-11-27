"""Bounded manual project snapshots; imported evidence is historical only.

ZIP hashes detect corruption, not a hostile process using the same OS identity.
Workspace source files and agent configuration are never packaged or replaced.
"""
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sqlite3
import stat
import tempfile
import uuid
import zipfile

from .projects import ProjectContext, ProjectRegistry, _canonical, _outside, resolve_state_home
from .state import encode, read_record, write_record
from .store import now


FORMAT = "autonomous-se-project"
VERSION = 1
MAX_FILES = 4096
MAX_MANIFEST_BYTES = 2 * 1024 * 1024
MAX_FILE_BYTES = 16 * 1024 * 1024
MAX_DATABASE_BYTES = 256 * 1024 * 1024
MAX_TOTAL_BYTES = 512 * 1024 * 1024
_STRUCTURED = {"baseline.json", "contract.json", "check.json", "verify.json", "gate.json",
               "ac.json", "review.json", "report.json", "progress.json", "cycle.json",
               "receipt.json", "result.json", "schema.json"}
_RAW = re.compile(r"(?i)(?:raw|transcript|recording|conversation|chat|credential|secret|auth|token|password|stdin|stdout|stderr|session)")
_RESERVED = re.compile(r"(?i)^(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?$")


def _checked_path(value):
    """Check every lexical ancestor before resolving, including Windows junctions."""
    path = Path(os.path.abspath(Path(value).expanduser()))
    for candidate in (*reversed(path.parents), path):
        if candidate.exists() or candidate.is_symlink():
            info = candidate.lstat()
            if (stat.S_ISLNK(info.st_mode)
                    or getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 1024)
                    or (stat.S_ISREG(info.st_mode) and info.st_nlink != 1)):
                raise ValueError("Transfer paths cannot contain symlinks, reparse points or hardlinks")
    if os.path.normcase(str(path.resolve())) != os.path.normcase(str(path)):
        raise ValueError("Transfer path escapes its lexical location")
    return path


def _archive_path(value):
    if (not isinstance(value, str) or not value or len(value) > 240
            or "\\" in value or "\0" in value or value.startswith("/")):
        raise ValueError("Invalid archive path")
    parts = value.split("/")
    if len(parts) > 12 or any(not part or part in {".", ".."} or len(part) > 100
            or part[-1:] in {".", " "} or _RESERVED.fullmatch(part)
            or any(ord(char) < 32 or char in '<>:"|?*' for char in part) for part in parts):
        raise ValueError("Unsafe archive path")
    return PurePosixPath(value)


def _artifact_allowed(path, include_verbose):
    parts = list(PurePosixPath(path).parts)
    historical = False
    while parts and parts[0] == "historical":
        parts.pop(0)
        historical = True
    if historical and parts in (["memory.sqlite3"], ["manifest.json"]):
        return True
    if len(parts) < 3 or parts[0] != "tasks" or any(_RAW.search(part) for part in parts):
        return False
    name = parts[-1]
    return (name in _STRUCTURED or bool(re.fullmatch(r"implementation-[1-5]\.json", name))
            or (include_verbose and name.endswith(".log")))


def _json(data):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate JSON key")
            result[key] = value
        return result
    try:
        return json.loads(data, object_pairs_hook=unique,
                          parse_constant=lambda value: (_ for _ in ()).throw(ValueError("Non-finite JSON")))
    except (UnicodeError, json.JSONDecodeError, RecursionError) as error:
        raise ValueError("Invalid bounded JSON") from error


def _contains_private(value):
    from .core import redact
    if isinstance(value, str):
        return redact(value) != value
    if isinstance(value, list):
        return any(_contains_private(item) for item in value)
    if isinstance(value, dict):
        private_keys = {"password", "token", "secret", "api_key", "apikey", "authorization",
                        "access_token", "refresh_token", "credential", "credentials", "raw_transcript", "raw_chat"}
        return any((key.casefold() in private_keys and item not in (None, "", [], {}))
                   or _contains_private(item) for key, item in value.items())
    return False


def _check_database(db, project):
    if db.execute("PRAGMA user_version").fetchone()[0] != 5:
        raise ValueError("Transfer requires memory schema v5")
    objects = db.execute("SELECT type,name,sql FROM sqlite_master WHERE sql IS NOT NULL").fetchall()
    if any(kind in {"trigger", "view"} or "CREATE VIRTUAL TABLE" in sql.upper() for kind, _, sql in objects):
        raise ValueError("Executable SQLite objects are not supported in transfer snapshots")
    if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok" or db.execute("PRAGMA foreign_key_check").fetchone():
        raise ValueError("Corrupt project database snapshot")
    try:
        identity = read_record(db, "project_identity", "identity")["payload"]
        if identity != {"project_id": project["project_id"], "storage_key": project["storage_key"]}:
            raise ValueError("Snapshot project identity differs from manifest")
        if not db.execute("SELECT 1 FROM projects WHERE id=?", (project["project_id"],)).fetchone():
            raise ValueError("Snapshot project row is missing")
        # Reject known credential patterns instead of rewriting evidence bytes.
        for kind, name, _ in objects:
            if kind != "table":
                continue
            if _RAW.search(name) and name != "sessions":
                raise ValueError("Private/raw database tables cannot be exported")
            quoted = '"' + name.replace('"', '""') + '"'
            for row in db.execute("SELECT * FROM " + quoted):
                for value in row:
                    if isinstance(value, str):
                        if value.lstrip()[:1] in {"{", "["}:
                            try:
                                value = _json(value)
                            except ValueError:
                                pass
                        if _contains_private(value):
                            raise ValueError("Snapshot contains credentials or private raw records; remove them before transfer")
    except sqlite3.DatabaseError as error:
        raise ValueError("Invalid project database schema") from error


def _unknown_operations(db):
    for key, revision, data in db.execute("SELECT key,revision,payload FROM runtime_records WHERE kind='operation'").fetchall():
        value = _json(data)
        if value.get("state") not in {"APPLIED", "ABSENT"}:
            historical = "historical_operation"
            while db.execute("SELECT 1 FROM runtime_records WHERE kind=? AND key=?", (historical, key)).fetchone():
                historical = "historical_" + historical
            db.execute("INSERT INTO runtime_records SELECT ?,key,revision,payload,updated FROM runtime_records WHERE kind='operation' AND key=?", (historical, key))
            write_record(db, "operation", key, {**value, "state": "UNKNOWN"}, expected_revision=revision)


def _file_entry(path, name, kind):
    data = path.read_bytes()
    return {"path": name, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(), "kind": kind}


def export_project(project: ProjectContext, destination: Path, *, include_verbose: bool = False) -> dict:
    """Create a new local ZIP using SQLite's backup API; never send it anywhere."""
    if not isinstance(project, ProjectContext) or type(include_verbose) is not bool:
        raise ValueError("Export requires a ProjectContext and boolean include_verbose")
    destination = _checked_path(destination)
    if destination.exists():
        raise ValueError("CONFLICT: export destination is occupied")
    if not destination.parent.is_dir():
        raise ValueError("Export destination parent must exist")
    with closing(ProjectRegistry(project.state_home)) as registry:
        current = registry.validate(project)
    source_root = _checked_path(current.state_home / "projects" / current.storage_key)
    if destination.is_relative_to(source_root):
        raise ValueError("Export destination cannot be inside project state")
    database = _checked_path(source_root / "memory.sqlite3")
    if not database.is_file():
        raise ValueError("Missing project database")
    for name in ("memory.sqlite3-wal", "memory.sqlite3-shm"):
        _checked_path(source_root / name)
    metadata = {"project_id": current.project_id, "workspace_id": current.workspace_id,
                "storage_key": current.storage_key, "kind": current.kind, "timezone": current.timezone,
                "root": str(current.root), "revision": current.revision}
    installed = False
    with tempfile.TemporaryDirectory(prefix="ase-export-", dir=destination.parent) as temporary:
        temporary = Path(temporary)
        snapshot = temporary / "memory.sqlite3"
        with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as source, closing(sqlite3.connect(snapshot)) as target:
            source.backup(target, pages=128, sleep=0.01)
            _check_database(target, metadata)
            with target:
                _unknown_operations(target)
            target.execute("PRAGMA journal_mode=DELETE")
            # Remove deleted/free pages that might contain former private data.
            target.execute("VACUUM")
        files = [("memory.sqlite3", snapshot, "database")]
        for folder in ("tasks", "historical"):
            tasks = source_root / folder
            if not tasks.exists():
                continue
            _checked_path(tasks)
            visited = 0
            for directory, directories, names in os.walk(tasks, followlinks=False):
                visited += len(directories) + len(names)
                if visited > MAX_FILES * 4:
                    raise ValueError("Artifact inventory exceeds transfer limit")
                for name in directories:
                    _checked_path(Path(directory) / name)
                for name in names:
                    path = Path(directory) / name
                    relative = path.relative_to(source_root).as_posix()
                    if not _artifact_allowed(relative, include_verbose):
                        continue
                    _archive_path(relative)
                    path = _checked_path(path)
                    maximum = MAX_DATABASE_BYTES if path.name == "memory.sqlite3" else MAX_FILE_BYTES
                    if not path.is_file() or path.stat().st_size > maximum:
                        raise ValueError("Artifact is not a bounded regular file")
                    data = path.read_bytes()
                    if path.name == "memory.sqlite3":
                        historical_manifest = _json(_checked_path(path.with_name("manifest.json")).read_bytes())
                        with closing(sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True)) as historical_db:
                            _check_database(historical_db, historical_manifest["project"])
                    elif _contains_private(_json(data) if path.suffix == ".json" else data.decode("utf-8")):
                        raise ValueError("Artifact contains credentials; review local evidence before transfer")
                    captured = temporary / "artifacts" / relative
                    captured.parent.mkdir(parents=True, exist_ok=True)
                    captured.write_bytes(data)
                    files.append((relative, captured, "historical" if folder == "historical" else "artifact"))
        entries = [_file_entry(path, name, kind) for name, path, kind in sorted(files)]
        if (len(entries) > MAX_FILES or snapshot.stat().st_size > MAX_DATABASE_BYTES
                or sum(entry["bytes"] for entry in entries) > MAX_TOTAL_BYTES):
            raise ValueError("Snapshot exceeds transfer bounds")
        manifest = {"format": FORMAT, "archive_version": VERSION, "memory_schema": 5,
                    "created_at_utc": now(), "project": metadata, "include_verbose": include_verbose,
                    "files": entries, "relationships": [{"artifact": name, "database": "memory.sqlite3",
                        "task_id": PurePosixPath(name).parts[1], "source_path": str(source_root / name),
                        "historical_only": True} for name, _, kind in sorted(files) if kind != "database"],
                    "review_notice": "Review structured content for business-sensitive information before external sharing"}
        manifest_bytes = encode(manifest).encode("utf-8")
        if len(manifest_bytes) > MAX_MANIFEST_BYTES:
            raise ValueError("Manifest exceeds transfer bounds")
        packed = temporary / "snapshot.zip"
        with zipfile.ZipFile(packed, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name, data in [("manifest.json", manifest_bytes), *((name, path.read_bytes()) for name, path, _ in sorted(files))]:
                info = zipfile.ZipInfo(name)
                info.create_system = 3
                info.external_attr = (stat.S_IFREG | 0o600) << 16
                info.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(info, data)
        try:
            # Exclusive destination creation makes concurrent exports fail closed.
            with destination.open("xb") as out, packed.open("rb") as source:
                installed = True
                shutil.copyfileobj(source, out)
                out.flush()
                os.fsync(out.fileno())
        except FileExistsError as error:
            raise ValueError("CONFLICT: export destination is occupied") from error
        except BaseException:
            if installed:
                destination.unlink(missing_ok=True)
            raise
    return manifest


def _validate_archive(archive, temporary):
    infos = archive.infolist()
    if len(infos) > MAX_FILES + 1:
        raise ValueError("Archive member count exceeds transfer bounds")
    seen, total = {}, 0
    for info in infos:
        _archive_path(info.orig_filename)
        mode = info.external_attr >> 16
        if (info.filename.casefold() in seen or info.is_dir() or info.extra
                or info.flag_bits & 1 or stat.S_IFMT(mode) not in {0, stat.S_IFREG}
                or info.external_attr & 0x10):
            raise ValueError("Archive contains duplicate, linked, encrypted or unsupported members")
        maximum = MAX_DATABASE_BYTES if PurePosixPath(info.filename).name == "memory.sqlite3" else MAX_FILE_BYTES
        if info.file_size > maximum:
            raise ValueError("Archive member exceeds transfer bounds")
        total += info.file_size
        seen[info.filename.casefold()] = info
    if (total > MAX_TOTAL_BYTES or "manifest.json" not in seen
            or seen["manifest.json"].filename != "manifest.json" or seen["manifest.json"].file_size > MAX_MANIFEST_BYTES):
        raise ValueError("Archive manifest missing or transfer bounds exceeded")
    manifest = _json(archive.read(seen["manifest.json"]))
    if (not isinstance(manifest, dict) or manifest.get("format") != FORMAT or manifest.get("archive_version") != VERSION
            or manifest.get("memory_schema") != 5 or type(manifest.get("include_verbose")) is not bool):
        raise ValueError("Unsupported project snapshot manifest")
    project = manifest.get("project")
    try:
        if not isinstance(project, dict):
            raise ValueError("Invalid project metadata")
        for key in ("project_id", "workspace_id"):
            if str(uuid.UUID(project[key])) != project[key]:
                raise ValueError("Invalid UUID")
        _archive_path(project["storage_key"])
        if "/" in project["storage_key"] or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", project["kind"]):
            raise ValueError("Invalid project metadata")
        if not re.fullmatch(r"[A-Za-z0-9_+./-]{1,128}", project["timezone"]):
            raise ValueError("Invalid timezone")
        from .timezones import resolve_timezone
        resolve_timezone(project["timezone"])
    except (KeyError, TypeError, AttributeError) as error:
        raise ValueError("Invalid project metadata") from error
    entries = manifest.get("files")
    if not isinstance(entries, list) or not 1 <= len(entries) <= MAX_FILES:
        raise ValueError("Invalid file manifest")
    expected = {"manifest.json"}
    for item in entries:
        if not isinstance(item, dict) or set(item) != {"path", "bytes", "sha256", "kind"}:
            raise ValueError("Invalid manifest entry")
        name = item["path"]
        _archive_path(name)
        if (name.casefold() in expected or name.casefold() not in seen
                or type(item["bytes"]) is not int or item["bytes"] < 0
                or not isinstance(item["sha256"], str) or not re.fullmatch(r"[a-f0-9]{64}", item["sha256"])
                or (name != "memory.sqlite3" and not _artifact_allowed(name, manifest["include_verbose"]))
                or item["kind"] != ("database" if name == "memory.sqlite3" else "historical" if name.startswith("historical/") else "artifact")):
            raise ValueError("Manifest contains unexpected or duplicate file")
        expected.add(name.casefold())
        info = seen[name.casefold()]
        if info.filename != name or info.file_size != item["bytes"]:
            raise ValueError("Manifest file size/path mismatch")
        path = temporary / name
        path.parent.mkdir(parents=True, exist_ok=True)
        digest, count = hashlib.sha256(), 0
        with archive.open(info) as source, path.open("xb") as target:
            while block := source.read(1024 * 1024):
                count += len(block)
                if count > item["bytes"]:
                    raise ValueError("Archive member exceeds declared bounds")
                digest.update(block)
                target.write(block)
        if count != item["bytes"] or digest.hexdigest() != item["sha256"]:
            raise ValueError("Archive file hash mismatch")
    if set(seen) != expected or "memory.sqlite3" not in expected:
        raise ValueError("Archive members differ from manifest")
    return manifest


def _import_revision_highwaters(db, records):
    """Retain counters independently of the historical grants they once held.

    Earlier imports reset current rows while preserving older record tiers and
    immutable events. All of those observations can advance the next revision;
    none supplies a current approval payload.
    """
    counters = {"task": {}, "approval": {}}
    for kind, key, revision, _, _ in records:
        while kind.startswith("historical_"):
            kind = kind[11:]
        if kind in counters:
            counters[kind][key] = max(counters[kind].get(key, 0), revision)
        elif kind in {"task_version", "approval_version"}:
            owner, separator, suffix = key.rpartition(":")
            if separator and re.fullmatch(r"[1-9][0-9]*", suffix):
                counter = counters[kind[:-8]]
                counter[owner] = max(counter.get(owner, 0), int(suffix))
    for key, kind, task_id in db.execute(
            "SELECT event_key,kind,task_id FROM runtime_events WHERE kind IN ('proposal','transition','approval')"):
        prefix = str(task_id) + ":" + kind + ":"
        suffix = key[len(prefix):] if key.startswith(prefix) else ""
        if re.fullmatch(r"[1-9][0-9]*", suffix):
            counter = counters["approval" if kind == "approval" else "task"]
            counter[task_id] = max(counter.get(task_id, 0), int(suffix))
    return counters


def _historical_import(db, manifest, context, mode):
    with db:
        records = db.execute("SELECT kind,key,revision,payload,updated FROM runtime_records").fetchall()
        highwaters = _import_revision_highwaters(db, records)
        for kind, key, revision, payload, updated in records:
            if not kind.startswith("historical_"):
                historical = "historical_" + kind
                # Every import adds one namespace level instead of overwriting older history.
                while db.execute("SELECT 1 FROM runtime_records WHERE kind=? AND key=?", (historical, key)).fetchone():
                    historical = "historical_" + historical
                db.execute("INSERT INTO runtime_records VALUES(?,?,?,?,?)", (historical, key, revision, payload, updated))
        # Keep original records under historical_*; rebuild only passive current records.
        db.execute("DELETE FROM runtime_records WHERE substr(kind,1,11) != 'historical_'")
        for kind, key, revision, data, _ in records:
            if kind == "task":
                task = _json(data)
                task.update(state="AWAITING_APPROVAL", proposal_hash=None, approved_stage_ids=[],
                            last_reason="Imported history requires a new reviewed proposal and approval")
                # Preserve both counters above current, historical and event
                # observations. A tombstone retains no old approval authority.
                db.execute("INSERT INTO runtime_records VALUES(?,?,?,?,?)",
                           (kind, key, highwaters["task"][key] + 1, encode(task), now()))
                approval_revision = highwaters["approval"].get(key, 0)
                if approval_revision:
                    db.execute("INSERT INTO runtime_records VALUES(?,?,?,?,?)",
                               ("approval", key, approval_revision, encode(None), now()))
            elif kind == "operation":
                operation = _json(data)
                if operation.get("state") not in {"APPLIED", "ABSENT"}:
                    operation["state"] = "UNKNOWN"
                write_record(db, kind, key, operation, expected_revision=0)
            elif kind == "publication_push":
                # A copied workspace must not lose a remote push's uncertainty.
                # Known applied/nonlaunch history grants no current approval;
                # pending confirmation must be reobserved at the actual target.
                publication = _json(data)
                if publication.get("state") not in {"APPLIED", "ABSENT"}:
                    publication = {**publication, "state": "UNKNOWN"}
                    publication.pop("confirmation", None)
                write_record(db, kind, key, publication, expected_revision=0)
            elif kind in {"execution_plan", "budget", "knowledge_dependencies"}:
                write_record(db, kind, key, _json(data), expected_revision=0)
        db.execute("UPDATE tasks SET status='AWAITING_APPROVAL',updated=?", (now(),))
        for (claim_id,) in db.execute("SELECT id FROM knowledge_claims").fetchall():
            write_record(db, "knowledge_validity", str(claim_id), {"validity": "UNKNOWN", "reasons": ["Imported history has no current workspace observations"]}, expected_revision=0)
        write_record(db, "import_origin", "origin", {"origin_project_id": manifest["project"]["project_id"],
                     "origin_workspace_id": manifest["project"]["workspace_id"], "origin_root": manifest["project"].get("root"),
                     "mode": mode, "imported_at_utc": now(), "historical_only": True}, expected_revision=0)
        write_record(db, "project_identity", "identity", {"project_id": context.project_id, "storage_key": context.storage_key}, expected_revision=0)
        # The old projects row is already preserved in the immutable original snapshot.
        db.execute("DELETE FROM projects")
        db.execute("INSERT INTO projects VALUES(?,?,?,?)", (context.project_id, str(context.root), "{}", now()))


def _activate_import(registry, context):
    """Called inside the final registry transaction, after installing complete state."""
    registry.db.execute("INSERT INTO projects VALUES(?,?,?,?,?,?,?,?,?)", (
        context.project_id, context.storage_key, context.kind, context.timezone, "ACTIVE", 1,
        hashlib.sha256(_canonical(context.root).encode()).hexdigest(), context.workspace_id, now()))
    registry.db.execute("INSERT INTO workspaces VALUES(?,?,?,?,?)", (
        context.workspace_id, context.project_id, str(context.root), _canonical(context.root), now()))
    registry.db.execute("INSERT INTO aliases VALUES(?,?)", (_canonical(context.root), context.workspace_id))


def _reject_pending_import(registry, projects, root):
    """An interrupted copy must not be silently bypassed by allocating another UUID."""
    candidates = list(projects.glob("*/import-recovery.json"))
    if len(candidates) > MAX_FILES:
        raise ValueError("Import recovery inventory exceeds transfer bounds")
    for marker in candidates:
        marker = _checked_path(marker)
        if marker.stat().st_size > MAX_MANIFEST_BYTES:
            raise ValueError("Invalid import recovery marker: " + str(marker))
        pending = _json(marker.read_bytes())
        if not isinstance(pending, dict):
            raise ValueError("Invalid import recovery marker: " + str(marker))
        if pending.get("canonical_root") == _canonical(root):
            row = registry.db.execute("SELECT status FROM projects WHERE project_id=?", (pending.get("project_id"),)).fetchone()
            if row is None or row[0] != "ACTIVE":
                raise ValueError("CONFLICT: interrupted import retained at " + str(marker.parent)
                                 + "; inspect import-recovery.json and historical/ before manual recovery; no files were deleted")


def import_project(archive: Path, state_home: Path, *, root: Path, mode: str = "copy") -> ProjectContext:
    """Validate a snapshot completely, then activate new managed state without merging."""
    if mode not in {"copy", "restore"}:
        raise ValueError("Import mode must be copy or restore; merge/overwrite is unsupported")
    archive, root = _checked_path(archive), _checked_path(root)
    home = resolve_state_home(_checked_path(state_home))
    if not root.is_dir() or not archive.is_file():
        raise ValueError("Import requires an existing workspace directory and archive")
    _outside(home, root)
    if archive.stat().st_size > MAX_TOTAL_BYTES:
        raise ValueError("Archive exceeds transfer bounds")
    # An invalid archive never creates a registry or touches a selected workspace.
    with tempfile.TemporaryDirectory(prefix="ase-import-") as temporary:
        temporary = Path(temporary)
        try:
            with zipfile.ZipFile(archive) as packed:
                manifest = _validate_archive(packed, temporary)
            snapshot = temporary / "memory.sqlite3"
            with closing(sqlite3.connect(snapshot)) as db:
                _check_database(db, manifest["project"])
            for item in manifest["files"]:
                if item["kind"] == "historical" and PurePosixPath(item["path"]).name == "memory.sqlite3":
                    historical = temporary / item["path"]
                    historical_manifest = _json(historical.with_name("manifest.json").read_bytes())
                    with closing(sqlite3.connect(historical)) as db:
                        _check_database(db, historical_manifest["project"])
        except (zipfile.BadZipFile, sqlite3.DatabaseError, UnicodeError, OSError, RuntimeError) as error:
            raise ValueError("Invalid project snapshot: " + str(error)) from error
        identity = manifest["project"]["project_id"] if mode == "restore" else str(uuid.uuid4())
        context = ProjectContext(identity, str(uuid.uuid4()), root, identity, manifest["project"]["kind"], home, manifest["project"]["timezone"])
        home.mkdir(parents=True, exist_ok=True)
        projects = _checked_path(home / "projects")
        projects.mkdir(exist_ok=True)
        destination = _checked_path(projects / identity)
        with closing(ProjectRegistry(home)) as registry:
            registry.db.execute("BEGIN IMMEDIATE")
            installed = False
            try:
                _reject_pending_import(registry, projects, root)
                if (destination.exists() or registry._lookup(str(root)) is not None
                        or registry.db.execute("SELECT 1 FROM workspaces WHERE canonical_root=?", (_canonical(root),)).fetchone()
                        or registry.db.execute("SELECT 1 FROM projects WHERE project_id=? OR storage_key=?", (identity, identity)).fetchone()):
                    raise ValueError("CONFLICT: project identity, workspace or managed destination is occupied: " + str(destination))
                with tempfile.TemporaryDirectory(prefix=".import-", dir=projects) as staging:
                    staging = Path(staging)
                    historical = staging / "historical"
                    historical.mkdir()
                    shutil.copyfile(snapshot, historical / "memory.sqlite3")
                    (historical / "manifest.json").write_text(encode(manifest), encoding="utf-8")
                    for item in manifest["files"]:
                        if item["kind"] != "database":
                            target = historical / item["path"]
                            target.parent.mkdir(parents=True, exist_ok=True)
                            shutil.copyfile(temporary / item["path"], target)
                    shutil.copyfile(snapshot, staging / "memory.sqlite3")
                    with closing(sqlite3.connect(staging / "memory.sqlite3")) as db:
                        _historical_import(db, manifest, context, mode)
                        if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok" or db.execute("PRAGMA foreign_key_check").fetchone():
                            raise ValueError("Imported state failed integrity validation")
                    (staging / "import-recovery.json").write_text(encode({
                        "project_id": identity, "workspace_id": context.workspace_id,
                        "root": str(root), "canonical_root": _canonical(root),
                        "managed_directory": str(destination), "archive": str(archive),
                        "manifest_sha256": hashlib.sha256(encode(manifest).encode()).hexdigest(),
                        "recovery": "Registry commit is activation; preserve historical/ and inspect before manual recovery"}), encoding="utf-8")
                    # Exclusive directory creation never replaces another destination,
                    # including on POSIX where rename could replace an empty directory.
                    # Registry activation is the visibility boundary. A hard crash leaves
                    # unregistered state for inspection; retry fails CONFLICT, not merge.
                    destination.mkdir()
                    installed = True
                    for child in staging.iterdir():
                        child.rename(destination / child.name)
                    _activate_import(registry, context)
                    registry.db.commit()
            except BaseException:
                registry.db.rollback()
                active = registry.db.execute("SELECT 1 FROM projects WHERE project_id=? AND status='ACTIVE'", (identity,)).fetchone()
                if installed and not active:
                    _checked_path(destination)
                    shutil.rmtree(destination)
                raise
            return registry.resolve(identity)
