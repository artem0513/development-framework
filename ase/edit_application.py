"""Preflight and idempotent application of an authenticated local file intent.

The caller owns durable intent storage, receipt integrity, authority and locking.
This module only recognizes the recorded preimage or intended postimage. A
process may die after replacement and before its caller records completion;
postimage comparison makes that interruption safe to resume without rewriting.
"""
import hashlib
import os
from pathlib import Path
import re
import stat


def _digest(data):
    return hashlib.sha256(data).hexdigest()


def _resolve(root, value, resolve_target):
    if os.name == "nt" and isinstance(value, str) and any(
            part not in {".", ".."} and part.endswith((".", " ")) for part in Path(value).parts):
        raise ValueError("Windows-normalizing edit path is unsupported")
    path = Path(resolve_target(root, value)).resolve()
    if not path.is_relative_to(root):
        raise ValueError("Edit path escapes approved root")
    if any(parent.exists() and not parent.is_dir() for parent in path.parents if parent.is_relative_to(root)):
        raise ValueError("Edit target parent is not a directory")
    return path


def _file_state(path):
    try:
        details = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(details.st_mode) or details.st_nlink != 1:
        raise ValueError("Edit target must be a regular single-link file")
    return _digest(path.read_bytes())


def _entries(root, scope, edits, application_id, resolve_target):
    if not isinstance(application_id, str) or not re.fullmatch(r"[0-9a-f]{32}", application_id):
        raise ValueError("Invalid edit application ID")
    if (not isinstance(scope, dict) or not isinstance(scope.get("resources"), list)
            or not isinstance(scope.get("effects"), list) or not isinstance(edits, list)):
        raise ValueError("Invalid edit scope or batch")
    if edits and "file_write" not in scope["effects"]:
        raise ValueError("File mutation exceeds approved scope effects")
    boundaries = [_resolve(root, resource, resolve_target) for resource in scope["resources"]]
    result, seen = [], set()
    for index, edit in enumerate(edits):
        if not isinstance(edit, dict):
            raise ValueError("Invalid edit operation")
        path = _resolve(root, edit.get("path"), resolve_target)
        if path in seen or not any(path == boundary or (boundary.is_dir() and path.is_relative_to(boundary)) for boundary in boundaries):
            raise ValueError("Edit exceeds approved scope or duplicates a path")
        seen.add(path)
        operation, content = edit.get("operation", "write"), edit.get("content")
        before = edit.get("before_sha256")
        if (not isinstance(operation, str) or operation not in {"write", "delete"} or not isinstance(content, str)
                or (before is not None and (not isinstance(before, str)
                    or (before and not re.fullmatch(r"[0-9a-f]{64}", before))))):
            raise ValueError("Invalid edit operation or preimage")
        data = content.encode("utf-8")
        name = path.relative_to(root).as_posix()
        temporary = path.with_name(path.name + ".ase-" + application_id + "-" + str(index) + ".tmp") if operation == "write" else None
        metadata = {"path": name, "operation": operation, "before_sha256": before or None,
            "after_sha256": _digest(data) if operation == "write" else None,
            "temporary_path": temporary.relative_to(root).as_posix() if temporary else None}
        result.append((path, metadata, data))
    if any(root / metadata["temporary_path"] in seen for _, metadata, _ in result if metadata["temporary_path"]):
        raise ValueError("Edit target overlaps an application temporary path")
    endpoints = [path for path, _, _ in result]
    endpoints += [root / metadata['temporary_path'] for _, metadata, _ in result if metadata['temporary_path']]
    if any(left != right and (left.is_relative_to(right) or right.is_relative_to(left))
            for index, left in enumerate(endpoints) for right in endpoints[index + 1:]):
        raise ValueError('Edit targets or staging paths have incompatible ancestry')
    return result


def plan_edits(root, scope, edits, application_id, resolve_target):
    """Validate a fresh batch without writing source, and return metadata only."""
    root = Path(root).resolve()
    entries = _entries(root, scope, edits, application_id, resolve_target)
    for path, metadata, _ in entries:
        if _file_state(path) != metadata["before_sha256"]:
            raise ValueError("Edit preimage is stale")
        if metadata['temporary_path']:
            stage = root / metadata['temporary_path']
            if stage.exists() or stage.is_symlink():
                raise ValueError('Application staging path already exists before admission')
    return {"version": 1, "application_id": application_id, "edits": [metadata for _, metadata, _ in entries]}


def _current_path(root, metadata, resolve_target):
    path = _resolve(root, metadata["path"], resolve_target)
    if path.relative_to(root).as_posix() != metadata["path"]:
        raise ValueError("Edit path changed since application planning")
    return path


def _temporary(root, metadata, resolve_target):
    name = metadata["temporary_path"]
    path = _resolve(root, name, resolve_target)
    if path.relative_to(root).as_posix() != name:
        raise ValueError("Application temporary path changed")
    return path


def _temporary_state(path, data):
    try:
        details = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(details.st_mode) or details.st_nlink != 1:
        raise ValueError("Application temporary must be a regular single-link file")
    with path.open("rb") as stream:
        prefix = stream.read(len(data) + 1)
    if not data.startswith(prefix):
        raise ValueError("Application temporary contains unknown content; preserved")
    return details


def _stage_bytes(root, metadata, data, resolve_target):
    temporary = _temporary(root, metadata, resolve_target)
    temporary.parent.mkdir(parents=True, exist_ok=True)
    details = _temporary_state(temporary, data)
    flags = os.O_RDWR | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    if details is None:
        flags |= os.O_CREAT | os.O_EXCL
    descriptor = os.open(temporary, flags, 0o600)
    with os.fdopen(descriptor, "r+b") as stream:
        opened = os.fstat(stream.fileno())
        if (not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1
                or (details is not None and (opened.st_dev, opened.st_ino) != (details.st_dev, details.st_ino))):
            raise ValueError("Application temporary changed during staging")
        _temporary(root, metadata, resolve_target)
        current = temporary.lstat()
        if (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino):
            raise ValueError("Application temporary changed during staging")
        prefix = stream.read(len(data) + 1)
        if not data.startswith(prefix):
            raise ValueError("Application temporary contains unknown content; preserved")
        stream.seek(len(prefix))
        stream.write(data[len(prefix):])
        stream.flush()
        os.fsync(stream.fileno())
    return temporary


def _cleanup_temporary(root, metadata, data, resolve_target):
    if metadata["temporary_path"] is None:
        return
    temporary = _temporary(root, metadata, resolve_target)
    details = _temporary_state(temporary, data)
    if details is not None:
        temporary.unlink()


def validate_plan(root, scope, edits, plan, resolve_target):
    """Verify the admitted metadata still describes exactly the captured edits."""
    root = Path(root).resolve()
    if (not isinstance(plan, dict) or type(plan.get("version")) is not int
            or plan["version"] != 1 or set(plan) != {"version", "application_id", "edits"}):
        raise ValueError("Invalid edit application plan")
    entries = _entries(root, scope, edits, plan["application_id"], resolve_target)
    expected = {"version": 1, "application_id": plan["application_id"], "edits": [metadata for _, metadata, _ in entries]}
    if plan != expected:
        raise ValueError("Edit application plan differs from captured edits")
    return entries


def inspect_plan(root, scope, edits, plan, resolve_target):
    """Validate all target and owned staging states without modifying either."""
    root = Path(root).resolve()
    entries = validate_plan(root, scope, edits, plan, resolve_target)
    pending = []
    for _, metadata, data in entries:
        path = _current_path(root, metadata, resolve_target)
        current = _file_state(path)
        if current == metadata["after_sha256"]:
            needed = False
        elif current == metadata["before_sha256"]:
            needed = True
        else:
            raise ValueError("Edit preimage drift requires reconciliation")
        if metadata["temporary_path"] is not None:
            _temporary_state(_temporary(root, metadata, resolve_target), data)
        pending.append(needed)
    return entries, pending


def apply_planned_edits(root, scope, edits, plan, resolve_target):
    """Apply pending preimages once; reject drift anywhere before any mutation."""
    root = Path(root).resolve()
    entries, pending = inspect_plan(root, scope, edits, plan, resolve_target)
    for (_, metadata, data), needed in zip(entries, pending):
        path = _current_path(root, metadata, resolve_target)
        current = _file_state(path)
        if current == metadata["after_sha256"]:
            _cleanup_temporary(root, metadata, data, resolve_target)
            continue
        if not needed or current != metadata["before_sha256"]:
            raise ValueError("Edit preimage drift requires reconciliation")
        if metadata["operation"] == "delete":
            # A missing file would already match the intended postimage.
            path.unlink()
        else:
            temporary = _stage_bytes(root, metadata, data, resolve_target)
            path = _current_path(root, metadata, resolve_target)
            current = _file_state(path)
            if current == metadata["after_sha256"]:
                _cleanup_temporary(root, metadata, data, resolve_target)
                continue
            if current != metadata["before_sha256"]:
                raise ValueError("Edit preimage drift requires reconciliation")
            _temporary_state(_temporary(root, metadata, resolve_target), data)
            if _file_state(temporary) != metadata["after_sha256"]:
                raise ValueError("Application temporary changed before replacement")
            os.replace(temporary, path)
    return [metadata["path"] for _, metadata, _ in entries]
