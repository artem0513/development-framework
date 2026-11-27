"""Independent nested Git clones inside one adopted workspace.

A workspace root may itself be a Git repository that ignores a folder of
separate clones (not submodules), each with its own .git and remote. They are
declared in the root `.meta` manifest (`{"projects": {"src/a": "<url>"}}`) and/or
the project-local config (`repositories = ["src/a"]`). A task changes exactly one
repository: the workspace root or one declared nested clone. Its source
observation, base, finish and publication then use that repository's Git.
"""
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import stat

MANIFEST = ".meta"
MANIFEST_LIMIT = 262144


def _relative(value, origin):
    if not isinstance(value, str) or not value or len(value) > 500:
        raise ValueError(origin + " lists an invalid repository path")
    path = value.replace("\\", "/").strip("/")
    pure = PurePosixPath(path)
    if (not path or PureWindowsPath(value).drive or value.startswith(("/", "\\")) or path == "."
            or any(part in {"", ".", ".."} or part.lower() == ".git" for part in pure.parts)):
        raise ValueError(origin + " repository paths must be workspace-relative: " + value)
    from .project_local import is_private_path
    if is_private_path(path) or pure.parts[0].lower() == ".devframework":
        raise ValueError(origin + " cannot name framework state as a repository: " + value)
    return pure.as_posix()


def _redirected(root, relative):
    item = root
    for part in (*PurePosixPath(relative).parts, ".git"):
        item = item / part
        try:
            info = item.lstat()
        except FileNotFoundError:
            return False
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            return True
    return False


def _manifest_projects(manifest):
    """Paths of a meta-style manifest; a `.meta` file of another tool declares nothing."""
    if not manifest.is_file() or manifest.is_symlink() or manifest.stat().st_size > MANIFEST_LIMIT:
        return []
    try:
        value = json.loads(manifest.read_bytes().decode("utf-8-sig"))
    except (UnicodeError, json.JSONDecodeError):
        return []
    projects = value.get("projects") if isinstance(value, dict) else None
    return [_relative(path, MANIFEST) for path in projects] if isinstance(projects, dict) else []


def declared_repositories(root):
    """Sorted workspace-relative paths of declared nested repositories; existence is checked on use."""
    root = Path(root)
    declared = _manifest_projects(root / MANIFEST)
    from .project_local import find_local_config
    config = find_local_config(root)
    if config and Path(config["root"]).resolve() == root.resolve():
        declared += [_relative(path, "Project-local config") for path in config.get("repositories", [])]
    declared = sorted(set(declared))
    for first in declared:
        if any(other != first and PurePosixPath(first).is_relative_to(other) for other in declared):
            raise ValueError("Nested repositories cannot contain each other: " + first)
    return declared


def repository_path(root, relative):
    """Validated absolute path of one declared nested clone, or the root for None."""
    root = Path(root).resolve()
    if relative is None:
        return root
    if relative not in declared_repositories(root):
        raise ValueError("Repository is not declared in " + MANIFEST + " or the project-local config: " + relative)
    if _redirected(root, relative):
        raise ValueError("Nested repository path or its .git is a symlink or junction: " + relative)
    path = root / relative
    if not path.is_dir() or not (path / ".git").exists():
        raise ValueError("Declared nested repository is not a Git clone: " + relative)
    if os.path.normcase(str(path.resolve())) != os.path.normcase(str(path)):
        raise ValueError("Nested repository resolves outside its declared location: " + relative)
    from .inspection import safe_git
    top = Path(safe_git(path, "rev-parse", "--show-toplevel"))
    if os.path.normcase(str(top.resolve())) != os.path.normcase(str(path)):
        raise ValueError("Declared nested repository is not its own Git top level: " + relative)
    _require_ignored(root, [relative])
    return path


def _require_ignored(root, declared):
    """The root's own Git must ignore declared clones, or its status, staging and finish would see them."""
    if not declared or not (root / ".git").exists():
        return
    from .inspection import _run
    listed = b"".join(name.encode("utf-8") + b"/\0" for name in declared)
    ignored = {name.rstrip("/") for name in _run(root, ["check-ignore", "-z", "--no-index", "--stdin"], False, listed)
               .decode("utf-8", "replace").split("\0") if name}
    missing = [name for name in declared if name not in ignored]
    if missing:
        raise ValueError("The workspace root's .gitignore must ignore its nested repositories: " + ", ".join(missing))


def _fold(value):
    return value.casefold() if os.name == "nt" else value


def owner(declared, resource):
    """The declared repository containing a workspace-relative resource, or None for the root."""
    path = PurePosixPath(_fold(resource.replace("\\", "/")))
    for relative in declared:
        if path == PurePosixPath(_fold(relative)) or path.is_relative_to(_fold(relative)):
            return relative
    return None


def _resources(contract):
    items = list(contract.get("scope", {}).get("resources", []))
    for stage in contract.get("stages", []):
        items += stage.get("scope", {}).get("resources", [])
    return list(dict.fromkeys(items))


def task_repository(root, contract):
    """{'path': nested relative path or None (root), 'base': ref or None} for one task.

    An explicit `repository` contract field names the path and optional base ref;
    otherwise the path is inferred from the scope resources. Resources in two
    repositories are rejected: one task changes one repository.
    """
    root = Path(root).resolve()
    explicit = contract.get("repository")
    declared = declared_repositories(root)
    owners = set()
    for resource in _resources(contract):
        if re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", resource):
            continue
        path = Path(resource)
        if path.is_absolute():
            if not path.is_relative_to(root):
                continue
            resource = path.relative_to(root).as_posix()
        if resource in {"", "."} and declared:
            owners.add(None)
            continue
        owners.add(owner(declared, resource))
    if len(owners) > 1:
        names = sorted("workspace root" if item is None else item for item in owners)
        raise ValueError("A task changes one repository, but its scope spans " + ", ".join(names)
                         + "; create one task per repository")
    path = explicit["path"] if explicit else (owners.pop() if owners else None)
    if explicit:
        path = None if path == "." else next((item for item in declared if _fold(item) == _fold(path)), path)
        if owners and owners != {path}:
            raise ValueError("Task scope resources must lie inside its declared repository " + explicit["path"])
    return {"path": path, "base": explicit.get("base") if explicit else None}


def validate_field(value):
    """Shape of the optional contract field {'path': '<nested path or .>', 'base': '<ref>'}."""
    from .inspection import is_ref_name
    if not isinstance(value, dict) or not set(value) <= {"path", "base"} or "path" not in value:
        raise ValueError("repository requires path and an optional base ref")
    if value["path"] != ".":
        _relative(value["path"], "repository")
    if "base" in value and not is_ref_name(value["base"]):
        raise ValueError("repository base must be a plain ref such as origin/master")
    return {"path": value["path"] if value["path"] == "." else _relative(value["path"], "repository"),
            **({"base": value["base"]} if "base" in value else {})}


def snapshot(root, relative, *, _excluded_files=()):
    """Root or nested source snapshot with workspace-relative file keys."""
    from .inspection import safe_snapshot
    root = Path(root).resolve()
    path = repository_path(root, relative)
    if relative is None:
        nested = set(declared_repositories(root))
        _require_ignored(root, sorted(nested))
        return (safe_snapshot(root, _excluded_files=_excluded_files, nested=nested)
                if _excluded_files or nested else safe_snapshot(root))
    prefix = relative + "/"
    local_excluded = {name[len(prefix):] for name in _excluded_files if name.startswith(prefix)}
    observed = safe_snapshot(path, _excluded_files=local_excluded) if local_excluded else safe_snapshot(path)
    files = {prefix + name: value for name, value in observed["files"].items()}
    from .state import hash_value
    return {**observed, "files": files, "repository": relative,
            "fingerprint": hash_value({"repository": relative, "files": files})}


def _tree(path, commit):
    from .inspection import safe_git_bytes
    entries = {}
    for row in safe_git_bytes(path, "ls-tree", "-r", "-z", "--full-tree", commit).split(b"\0"):
        if not row:
            continue
        metadata, _, name = row.partition(b"\t")
        mode, kind, oid = metadata.decode("ascii").split()
        if kind == "blob":
            entries[name.decode("utf-8", "strict")] = (mode, oid)
    return entries


def base_baseline(root, relative, ref, current):
    """Baseline at a base ref: base-commit hashes for paths that differ from it, current hashes otherwise.

    Paths Git reports as unchanged against the base carry identical content, so the
    current observation is their base state; this keeps checkout line endings exact.
    """
    import hashlib
    from datetime import datetime, timezone
    from .inspection import safe_blob, safe_git, safe_git_bytes
    from .state import hash_value
    path = repository_path(root, relative)
    commit = safe_git(path, "rev-parse", "--verify", "--end-of-options", ref + "^{commit}")
    if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", commit):
        raise ValueError("Task base ref does not name a commit: " + ref)
    prefix = "" if relative is None else relative + "/"
    changed = {name for name in safe_git_bytes(path, "diff", "--name-only", "-z", "--no-renames", commit)
               .decode("utf-8", "strict").split("\0") if name}
    if len(changed) > 5000:
        raise ValueError("Task branch differs from its base in more files than a bounded review covers")
    # Declared dependencies outside this repository are not part of its history.
    files = {key: value for key, value in current.items() if prefix and not key.startswith(prefix)}
    tree = _tree(path, commit)
    # Files present before the task and not committed (untracked, ignored dependencies) keep their state;
    # files the branch committed after the base are additions.
    indexed = {name for name in safe_git_bytes(path, "ls-files", "--cached", "-z").decode("utf-8", "strict").split("\0") if name}
    for key, value in current.items():
        if key.startswith(prefix) and key[len(prefix):] not in tree and key[len(prefix):] not in indexed:
            files[key] = value
    for name, (mode, oid) in tree.items():
        key = prefix + name
        if name not in changed and key in current:
            files[key] = current[key]
            continue
        data = safe_blob(path, commit, name)
        if mode == "120000":
            files[key] = "link:" + data.decode("utf-8", "replace") + ":base"
        else:
            files[key] = hashlib.sha256(data).hexdigest() + (":x" if mode == "100755" and os.name != "nt" else "")
    return {"fingerprint": hash_value({"repository": relative, "base": commit, "files": files}), "files": files,
            "commit": commit, "base_ref": ref, "repository": relative,
            "branch": safe_git(path, "branch", "--show-current"),
            "timestamp": datetime.now(timezone.utc).isoformat()}
