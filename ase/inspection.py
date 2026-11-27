"""Read-only Git inspection for local CLI workflow services.

Git's apparently read-only commands can invoke repository-configured fsmonitor,
clean filters and diff helpers. This adapter disables those execution paths and
accepts only the inspection forms used by the local runtime. Publishing
continues to use the ordinary CLI inside its existing authorization boundary.
"""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess


def _trusted_git():
    """Resolve once from absolute host PATH entries, never the current directory."""
    executable = "git.exe" if os.name == "nt" else "git"
    current = Path.cwd().resolve()
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        if not entry:
            continue
        directory = Path(os.path.expandvars(entry.strip('"')))
        if not directory.is_absolute():
            continue
        directory = directory.resolve()
        if directory == current:
            continue
        candidate = directory / executable
        if candidate.is_file() and (os.name == "nt" or os.access(candidate, os.X_OK)):
            return str(candidate.resolve(strict=True))
    raise ValueError("Trusted Git executable unavailable in absolute host PATH")


try:
    TRUSTED_GIT = _trusted_git()
except ValueError:
    # Generic project memory, documents and reporting do not require Git.
    # Resolve/raise only when an actual software inspection requests it.
    TRUSTED_GIT = None


def trusted_git():
    return TRUSTED_GIT or _trusted_git()


def _environment():
    env = {key: value for key, value in os.environ.items() if not key.upper().startswith("GIT_")}
    env.update({"GIT_CONFIG_SYSTEM": os.devnull, "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_CONFIG_NOSYSTEM": "1", "GIT_ALLOW_PROTOCOL": "",
                "GIT_NO_LAZY_FETCH": "1", "GIT_TERMINAL_PROMPT": "0"})
    return env


def _base(repo):
    return [trusted_git(), "--no-pager", "--no-optional-locks", "-C", str(repo),
            # Trust only the already selected canonical checkout for this invocation.
            # Windows tool sandboxes run as a different user; helpers remain disabled.
            "-c", "safe.directory=" + str(repo), "-c", "core.fsmonitor=false", "-c", "core.hooksPath=" + os.devnull,
            "-c", "core.worktree=" + str(repo), "-c", "core.attributesFile=" + os.devnull,
            "-c", "diff.external=", "-c", "submodule.recurse=false"]


OBJECT_ID = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")


def _validate_batch(names):
    """Stdin of a cat-file batch: newline-terminated object ids or names valid for the single-blob form."""
    if not isinstance(names, bytes) or not names.endswith(b"\n"):
        raise ValueError("Unsupported host Git batch input; newline-terminated object names are required")
    for name in names.decode("utf-8").split("\n")[:-1]:
        if "\r" in name:
            raise ValueError("Unsupported host Git batch object name")
        if not OBJECT_ID.fullmatch(name):
            _validate(["cat-file", "blob", name])


def _validate(args):
    if not args or not all(isinstance(arg, str) and "\0" not in arg for arg in args):
        raise ValueError("Host Git requires a supported read-only argument list")
    command, *options = args
    if command == "rev-parse" and options in (["HEAD"], ["--show-toplevel"], ["--git-path", "info/exclude"]):
        return
    if (command == "rev-parse" and len(options) == 3 and options[:2] == ["--verify", "--end-of-options"]
            and options[2].endswith("^{commit}") and is_ref_name(options[2][:-len("^{commit}")])):
        return
    if (command == "ls-tree" and len(options) == 4 and options[:3] == ["-r", "-z", "--full-tree"]
            and re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", options[3])):
        return
    if command == "branch" and options == ["--show-current"]:
        return
    if command == "status" and options in (["--porcelain"], ["--porcelain=v1"], ["--porcelain=v2"], SOURCE_STATUS):
        return
    if command == "ls-files" and set(options).issubset({"--cached", "--others", "--exclude-standard", "-z", "--stage"}):
        return
    if command == "check-ignore" and options == ["-z", "--no-index", "--stdin"]:
        return
    if command == "cat-file" and len(options) == 2 and options[0] == "blob":
        revision, _, path = options[1].partition(":")
        if (re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", revision) and path and "\\" not in path
                and all(part not in {"", ".", ".."} and part.lower() != ".git" for part in path.split("/"))):
            return
    if command == "cat-file" and options in (["--batch-check"], ["--batch"]):
        return  # _run checks every stdin object name with _validate_batch.
    if command == "check-ignore" and options == ["--no-index", "--quiet", "--", ".devframework/local/.ase-privacy-probe"]:
        return
    if command == "check-ignore" and options == ["--no-index", "--quiet", "--", ".devframework/local/"]:
        return
    if command == "diff":
        flags = {"--cached", "--check", "--stat", "--name-only", "--no-ext-diff", "--no-textconv", "-z", "--no-renames"}
        revisions = 0
        for index, option in enumerate(options):
            if option == "--":
                if index != len(options) - 1:
                    raise ValueError("Host Git diff does not accept path operands")
            elif option in flags:
                pass
            elif option == "HEAD" or re.fullmatch(r"[0-9a-fA-F]{40,64}", option):
                revisions += 1
            else:
                raise ValueError("Unsupported host Git diff option")
        if revisions <= 1:
            return
    raise ValueError("Unsupported host Git inspection; mutations and arbitrary options are forbidden")


def _run(repo, args, check=True, input=None):
    _validate(args)
    if args[0] == "cat-file" and args[1] in ("--batch-check", "--batch"):
        _validate_batch(input)
    repo = Path(repo).resolve(strict=True)
    env = _environment()
    base = _base(repo)
    # Config inspection itself does not invoke filters. NUL framing preserves
    # unusual subsection names without shell interpolation or line ambiguity.
    filters = subprocess.run([*base, "config", "--null", "--name-only", "--get-regexp", r"^filter\."],
                             stdin=subprocess.DEVNULL, capture_output=True, timeout=60, env=env, cwd=Path(__file__).resolve().parent)
    if filters.returncode not in {0, 1}:
        raise ValueError("Could not safely inspect repository filter configuration")
    prefixes = set()
    for key in filters.stdout.decode("utf-8", "strict").split("\0"):
        if key.startswith("filter.") and "." in key[7:]:
            prefix, suffix = key.rsplit(".", 1)
            if suffix.lower() in {"clean", "smudge", "process", "required"}:
                prefixes.add(prefix)
    for prefix in sorted(prefixes):
        for suffix in ("clean", "smudge", "process"):
            base += ["-c", prefix + "." + suffix + "="]
        base += ["-c", prefix + ".required=false"]
    command = list(args)
    if command[0] == "diff":
        command = ["diff", "--no-ext-diff", "--no-textconv", *command[1:]]
    feed = {'stdin': subprocess.DEVNULL} if input is None else {'input': input}
    result = subprocess.run([*base, *command], **feed, capture_output=True, timeout=60, env=env,
                            cwd=Path(__file__).resolve().parent)
    if check and result.returncode:
        # Do not echo arbitrary repository-configured strings or credentials to
        # the host API. A failed safe inspection blocks verification.
        raise ValueError("Safe Git " + command[0] + " failed with exit code " + str(result.returncode))
    return result.stdout


def is_ref_name(value):
    """A plain branch/remote-tracking ref or commit name, never an option or a range."""
    return (isinstance(value, str) and 0 < len(value) <= 200 and bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*", value))
            and ".." not in value and "//" not in value and not value.endswith(("/", ".lock", ".")))


def safe_git(repo, *args, check=True):
    return _run(repo, args, check).decode("utf-8", "replace").strip()


def safe_git_bytes(repo, *args):
    return _run(repo, args)


def safe_blob(repo, commit, path):
    """Raw committed bytes of one repository-relative path; cat-file applies no filters."""
    return _run(repo, ["cat-file", "blob", commit + ":" + path])


def safe_blobs(repo, commit, paths, max_bytes):
    """Committed bytes of many repository-relative paths in two Git reads, whatever the path count.

    Returns ({path: bytes}, {paths whose blob exceeds max_bytes; body not read}). Each object
    name passes the same validation as safe_blob. A path that is missing, not a blob or not
    expressible on one batch line is in neither, so the caller's safe_blob for it fails
    exactly as a single read does.
    """
    names = []
    for path in dict.fromkeys(paths):
        try:
            _validate(["cat-file", "blob", commit + ":" + path])
            (commit + ":" + path).encode("utf-8")
        except (ValueError, TypeError, UnicodeError):
            continue
        if "\n" not in path and "\r" not in path:
            names.append(path)
    if not names:
        return {}, set()
    listed = _run(repo, ["cat-file", "--batch-check"],
                  input="".join(commit + ":" + path + "\n" for path in names).encode("utf-8"))
    rows = [line.split(b" ") for line in listed.split(b"\n")[:-1]]
    if len(rows) != len(names):
        raise ValueError("Safe Git cat-file batch output is malformed")
    wanted, oversized = {}, set()
    for path, row in zip(names, rows):
        # "<name> missing" / "ambiguous" rows are skipped: the single read reports them.
        if len(row) == 3 and row[1] == b"blob" and row[2].isdigit() and OBJECT_ID.fullmatch(row[0].decode("ascii", "replace")):
            if int(row[2]) > max_bytes:
                oversized.add(path)
            else:
                wanted[path] = (row[0], int(row[2]))
    if not wanted:
        return {}, oversized
    output = _run(repo, ["cat-file", "--batch"], input=b"".join(oid + b"\n" for oid, _ in wanted.values()))
    blobs, position = {}, 0
    for path, (oid, size) in wanted.items():
        end = output.find(b"\n", position)
        if (end < 0 or output[position:end] != oid + b" blob " + str(size).encode()
                or output[end + 1 + size:end + 2 + size] != b"\n"):
            raise ValueError("Safe Git cat-file batch output is malformed")
        blobs[path] = output[end + 1:end + 1 + size]
        position = end + 2 + size
    return blobs, oversized


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def is_bytecode_cache(relative):
    """Interpreter-written PEP 3147 cache file; callers exclude it only when untracked."""
    path = Path(relative)
    return path.parent.name.lower() == "__pycache__" and path.suffix.lower() in {".pyc", ".pyo"}


SOURCE_STATUS = ["--porcelain=v1", "-z", "--untracked-files=all"]


def source_status(raw):
    """Dirty-worktree entries of `git status` + SOURCE_STATUS, minus untracked bytecode like safe_snapshot."""
    return "\0".join(entry for entry in raw.split("\0")
                     if entry and not (entry.startswith("?? ") and is_bytecode_cache(entry[3:])))


def safe_snapshot(repo, *, _excluded_files=(), nested=()):
    """Source-content fingerprint: tracked and nonignored untracked files, content and mode.

    Untracked __pycache__ bytecode is test-run output, not source. Declared nested
    repositories (`nested`, repository-relative) are observed through their own Git.
    """
    repo = Path(repo).resolve(strict=True)
    raw = _run(repo, ["ls-files", "--cached", "--others", "--exclude-standard", "-z"])
    paths = sorted(set(raw.decode("utf-8").split("\0")) - {""})
    from .project_local import is_private_path
    if any(is_private_path(path) for path in paths):
        raise ValueError("Private project-local state cannot be source or publication input")
    files = {}
    tracked = None
    for relative in paths:
        if relative in _excluded_files:
            continue
        if nested and relative.endswith("/") and relative.rstrip("/") in nested:
            continue
        p = repo / relative
        if p.is_file() and p.stat().st_nlink > 1:
            raise ValueError("Hardlinked source cannot be fingerprinted or included in packets")
        if p.resolve().is_relative_to(repo) and is_private_path(p.resolve().relative_to(repo)):
            raise ValueError("Private project-local state cannot be source through an alias")
        if not p.resolve().is_relative_to(repo):
            raise ValueError("Source escapes repository: " + relative)
        if is_bytecode_cache(relative) and (p.is_file() or p.is_symlink()):
            if tracked is None:
                tracked = set(_run(repo, ["ls-files", "--cached", "-z"]).decode("utf-8").split("\0"))
            if relative not in tracked:
                continue
        if p.is_symlink():
            if not p.is_file():
                raise ValueError("Directory/broken symlink needs explicit verification support: " + relative)
            files[relative] = "link:" + os.readlink(p) + ":" + hashlib.sha256(p.read_bytes()).hexdigest()
        elif p.is_file():
            files[relative] = hashlib.sha256(p.read_bytes()).hexdigest() + (":x" if os.name != "nt" and p.stat().st_mode & 0o111 else "")
        elif p.is_dir():
            raise ValueError("Git submodule/directory source requires separate verified project: " + relative)
    return {"fingerprint": hashlib.sha256(_canonical(files)).hexdigest(), "files": files,
            "commit": safe_git(repo, "rev-parse", "HEAD", check=False) or None,
            "branch": safe_git(repo, "branch", "--show-current"),
            "timestamp": datetime.now(timezone.utc).isoformat()}
