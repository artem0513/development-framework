"""Explicit v2 software publication with current evidence and preserved hooks."""
import ast
from contextlib import contextmanager
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import tokenize
from urllib.parse import parse_qsl, unquote, urlsplit, urlunsplit
import uuid

from .core import SECRET, execute, redact
from .gates import GateService, source_snapshot
from .inspection import SOURCE_STATUS, is_bytecode_cache, safe_git, source_status, trusted_git
from .journal import Journal
from .native_paths import state_io
from .projects import _safe_state
from .runner import capture_process
from .state import hash_value, identifier, read_record, text, transaction, write_record
from .operations import _EffectLaunchWitness
from .store import now
from .tasks import TaskService
from .workflow import WorkflowController, _exclusive


class _PublicationHeld(ValueError):
    """Authority rejected before a publication process was launched."""


def _python_has_secret(source):
    """Disambiguate executable bindings while retaining raw literal detection.

    No source is executed. Unknown syntax and payload-bearing expressions keep
    the conservative raw result; lookup keys and regular-expression definitions
    are metadata rather than embedded credential values.
    """
    raw = SECRET.search(source)
    if raw is None:
        return False
    try:
        tree = ast.parse(source)
        tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
    except (SyntaxError, ValueError, tokenize.TokenError, RecursionError):
        return True
    nodes = list(ast.walk(tree))
    for node in nodes:
        if isinstance(node, ast.Constant) and isinstance(node.value, (str, bytes)):
            value = node.value.decode("utf-8", "replace") if isinstance(node.value, bytes) else node.value
            if SECRET.search(value):
                return True
    if any(item.type == tokenize.COMMENT and SECRET.search(item.string) for item in tokens):
        return True
    lines = source.splitlines(keepends=True)
    starts, total = [], 0
    for line in lines:
        starts.append(total)
        total += len(line)

    def position(location):
        line, column = location
        return starts[line - 1] + column

    def ast_start(node):
        # AST columns count UTF-8 bytes; tokenizer columns count characters.
        prefix = lines[node.lineno - 1].encode("utf-8")[:node.col_offset].decode("utf-8")
        return starts[node.lineno - 1] + len(prefix)

    bindings, conditions = {}, set()
    for node in nodes:
        if isinstance(node, (ast.Assign, ast.AnnAssign, ast.NamedExpr)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name):
                    bindings[ast_start(target)] = node.value
        elif isinstance(node, (ast.If, ast.While)):
            conditions.update(ast_start(item) for item in ast.walk(node.test)
                              if isinstance(item, ast.Name) and isinstance(item.ctx, ast.Load))

    def embedded_payload(value):
        if value is None:
            return False
        if isinstance(value, ast.Constant):
            return value.value is not None and type(value.value) is not bool

        def regex_metadata(pattern):
            try:
                re.compile(pattern)
            except (re.error, OverflowError, RecursionError):
                return False
            if not any(marker in pattern for marker in ("[", "\\", "(?")):
                return False
            # Permit pattern syntax and credential-field labels, not an opaque
            # payload merely wrapped in a regex constructor.
            literals = re.sub(r"\[(?:\\.|[^\]\\])*\]", "", pattern)
            literals = re.sub(r"\\.", "", literals)
            fields = {"password", "secret", "bearer", "authorization", "signature", "credential", "username", "api_key", "apikey"}
            return all(part.lower() in fields for part in re.findall(r"[A-Za-z0-9_.-]{6,}", literals))

        metadata = set()
        for item in ast.walk(value):
            if isinstance(item, ast.Subscript) and isinstance(item.slice, ast.Constant):
                metadata.add(item.slice)
            elif isinstance(item, ast.BinOp) and isinstance(item.op, ast.Add):
                for part, other in ((item.left, item.right), (item.right, item.left)):
                    if (isinstance(part, ast.Constant) and isinstance(part.value, str)
                            and len(part.value) <= 2 and not any(character.isalnum() for character in part.value)
                            and any(isinstance(node, (ast.Name, ast.Attribute, ast.Subscript)) for node in ast.walk(other))):
                        metadata.add(part)
            elif isinstance(item, ast.Call) and isinstance(item.func, ast.Attribute):
                owner = item.func.value
                regex = (isinstance(owner, ast.Name) and owner.id == "re" and item.func.attr == "compile"
                         and len(item.args) == 1 and not item.keywords
                         and isinstance(item.args[0], ast.Constant) and isinstance(item.args[0].value, str)
                         and regex_metadata(item.args[0].value))
                environment = ((isinstance(owner, ast.Name) and owner.id == "os" and item.func.attr == "getenv")
                    or (isinstance(owner, ast.Attribute) and isinstance(owner.value, ast.Name)
                        and owner.value.id == "os" and owner.attr == "environ" and item.func.attr == "get"))
                if item.args and (regex or environment):
                    metadata.add(item.args[0])
        for item in ast.walk(value):
            if isinstance(item, ast.Constant) and item not in metadata:
                return True
            if isinstance(item, (ast.List, ast.Tuple, ast.Set, ast.Dict)):
                return True
            if (isinstance(item, ast.Call) and isinstance(item.func, ast.Name)
                    and item.func.id in {"str", "bytes", "bytearray", "list", "tuple", "dict", "set", "int", "float", "bool"}
                    and not item.args and not item.keywords):
                return True
        return False

    labels = re.compile(r"(?i)(?:password|api[_-]?key|token|secret)\s*[=:]")
    while raw is not None:
        if labels.match(source, raw.start()) is None:
            return True
        item = next((item for item in tokens if item.type == tokenize.NAME
                     and position(item.start) <= raw.start() < position(item.end)), None)
        if item is None:
            return True
        start = position(item.start)
        if start not in conditions and (start not in bindings or embedded_payload(bindings[start])):
            return True
        # A benign condition may overlap the next literal binding. Never skip
        # that second candidate merely because the first regex consumed it.
        raw = SECRET.search(source, raw.start() + 1)
    return False


class Publisher:
    def __init__(self, store):
        self.store = store
        self.tasks = TaskService(store)
        # The Git repository this publisher acts in: the workspace root or one declared nested clone.
        self.repo, self.repository, self.prefix = store.repo, None, ""

    def _select(self, repository):
        from .repositories import repository_path
        self.repo = repository_path(self.store.repo, repository)
        self.repository, self.prefix = repository, (repository + "/" if repository else "")

    def _select_task(self, contract):
        from .repositories import task_repository
        self._select(task_repository(self.store.repo, contract)["path"])

    def _local(self, snapshot):
        """Snapshot/baseline files keyed by paths in the selected repository."""
        if not self.prefix:
            return snapshot
        return {**snapshot, "files": {name[len(self.prefix):]: value for name, value in snapshot["files"].items()
                                      if name.startswith(self.prefix)}}

    @staticmethod
    def _remote(remote):
        if not isinstance(remote, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}", remote):
            raise ValueError("Invalid publishing remote name")
        return remote

    def _repository(self):
        top = Path(safe_git(self.repo, "rev-parse", "--show-toplevel")).resolve()
        if top != self.repo.resolve():
            raise ValueError("Publishing requires the selected Git repository root")

    def _argv(self, *args):
        # Pin Git, but preserve the repository's actual hooks and credential
        # helper policy. Never add --no-verify or override core.hooksPath.
        return [trusted_git(), "-C", str(self.repo), "-c", "safe.directory=" + str(self.repo), *args]

    @staticmethod
    def _environment():
        # Ambient selection/configuration can override -C and redirect Git to
        # another repository. On-disk user/repository configuration, hooks and
        # credential helpers remain active; process-supplied GIT_* overrides do
        # not belong to this selected workspace's publishing authorization.
        return {key: value for key, value in os.environ.items() if not key.upper().startswith("GIT_")}

    def _git(self, *args, check=True, strip=True, env=None):
        result = subprocess.run(self._argv(*args), cwd=self.repo, stdin=subprocess.DEVNULL,
                                capture_output=True, timeout=60, env=self._environment() if env is None else env)
        if check and result.returncode:
            raise ValueError("Git " + args[0] + " failed: " + redact(result.stderr.decode("utf-8", "replace").strip()))
        output = result.stdout.decode("utf-8", "replace")
        return output.strip() if strip else output

    def _dirty(self):
        return source_status(self._git("status", *SOURCE_STATUS, strip=False))

    def _urls(self, remote):
        urls = self._git("remote", "get-url", "--push", "--all", remote, check=False).splitlines()
        if len(urls) != 1:
            raise ValueError("Publishing supports exactly one push URL; inspect all configured destinations")
        url = text(urls[0], "push URL", limit=2048)
        parsed = urlsplit(url)
        sensitive = re.compile(r"(?i)(?:^|[-_])(?:token|secret|password|credential|authorization|signature|api[-_]?key)(?:$|[-_])|^sig$")
        if url.startswith("-") or parsed.username is not None or any(sensitive.search(key) for key, _ in parse_qsl(parsed.query, keep_blank_values=True)):
            raise ValueError("Push URL must be credential-free")
        return urls

    def _transport_environment(self, remote, url):
        """Pin one resolved destination without reapplying its URL rewrites.

        Git rewrites each configured URL once. A fresh exact alias therefore
        maps straight to the already authorized URL; explicit pushurl suppresses
        pushInsteadOf. Empty values reset multi-valued URL lists. Keep the named
        remote, hooks, credential helpers and all unrelated native configuration.
        The read-only probe fails closed when Git lacks these config semantics.
        """
        transport_alias = "ase-publication-" + uuid.uuid4().hex + ":"
        entries = [("remote." + remote + ".url", ""),
                   ("remote." + remote + ".url", transport_alias),
                   ("remote." + remote + ".pushurl", ""),
                   ("remote." + remote + ".pushurl", transport_alias),
                   ("url." + url + ".insteadOf", transport_alias)]
        environment = self._environment()
        environment["GIT_CONFIG_COUNT"] = str(len(entries))
        for index, (key, value) in enumerate(entries):
            environment["GIT_CONFIG_KEY_" + str(index)] = key
            environment["GIT_CONFIG_VALUE_" + str(index)] = value
        for flags in (("--push", "--all"), ("--all",)):
            if self._git("remote", "get-url", *flags, remote, env=environment).splitlines() != [url]:
                raise ValueError("Git cannot bind the exact approved publication destination")
        return environment

    def _authorization(self, remote):
        scope = self.store.workspace_id + ":" + (self.repository + ":" if self.repository else "")
        return read_record(self.store.db, "publishing_authorization", scope + remote)

    def _authorized(self, remote, url):
        value = self._authorization(remote)["payload"] or {}
        return (value.get("url") == url and value.get("project_id") == self.store.project_id
                and value.get("workspace_id") == self.store.workspace_id
                and value.get("repository") == self.repository)

    def authorize(self, remote="origin", disable=False, repository=None):
        remote = self._remote(remote)
        if type(disable) is not bool:
            raise ValueError("disable must be boolean")
        if repository is not None:
            from .repositories import _relative
            self._select(_relative(repository, "--repository"))
        self._repository()
        url = None if disable else self._urls(remote)[0]
        with transaction(self.store.db) as db:
            old = self._authorization(remote)
            value = None if disable else {"url": url, "project_id": self.store.project_id,
                "workspace_id": self.store.workspace_id, "authorized_at_utc": now(),
                **({"repository": self.repository} if self.repository else {})}
            write_record(db, "publishing_authorization", self.store.workspace_id + ":"
                         + (self.repository + ":" if self.repository else "") + remote,
                         value, expected_revision=old["revision"])
        return {"remote": remote, "enabled": not disable, **({"repository": self.repository} if self.repository else {}),
                "note": "Explicit workspace-scoped exact push URL authorization; publication remains dry-run unless executed"}

    @staticmethod
    def _failure(status, reason, **extra):
        return {"status": status, "published": False, "reasons": [reason], **extra}

    def _history_git(self, *args, max_bytes=2_000_000):
        # Read immutable objects without textconv/filters or lazy network fetch.
        # Spooling bounds captured bytes even for a malformed/huge inventory.
        result = capture_process(self._argv(*args), self.repo, timeout=60,
            max_output_bytes=max_bytes, env={**self._environment(), "GIT_NO_LAZY_FETCH": "1"})
        if result.get("output_limited"):
            raise ValueError("Git history inspection exceeds its output bound")
        if result.get("exit_code") != 0 or any(result.get(key) for key in ("error", "timed_out", "cancelled")):
            # Never include cat-file output: it may itself be a credential.
            raise ValueError("Git history inspection failed: " + args[0])
        return result["stdout"]

    def _history_secret_check(self, baseline, head=None):
        """Inspect introduced blobs of every unpublished task commit.

        Net tree differences omit reverted/deleted content. Compare every
        commit with each parent (including merge resolutions), then read the
        immutable new blob. Do not exclude blobs merely present at another
        baseline path: copying legacy content introduces it at a new path.
        Earlier baseline history retains its existing task-boundary semantics.
        """
        base = baseline.get("commit")
        if not base:
            return None
        try:
            head = head or self._git("rev-parse", "HEAD")
            if any(not isinstance(value, str) or not re.fullmatch(r"[a-fA-F0-9]{40}|[a-fA-F0-9]{64}", value) for value in (base, head)):
                raise ValueError("Full commit identities are required for history inspection")
            self._history_git("merge-base", "--is-ancestor", base, head)
            commits = self._history_git("rev-list", "--max-count=1001", base + ".." + head, max_bytes=80_000).decode("ascii").splitlines()
            if len(commits) > 1000:
                raise ValueError("Publication history exceeds its commit bound")
            blobs, inspected = {}, set()
            total_bytes = total_entries = metadata_bytes = 0
            for commit in commits:
                if not re.fullmatch(r"[a-fA-F0-9]{40}|[a-fA-F0-9]{64}", commit):
                    raise ValueError("Malformed publication commit identity")
                raw = self._history_git("diff-tree", "--root", "--no-commit-id", "--raw", "--no-abbrev",
                    "--no-renames", "--no-ext-diff", "--no-textconv", "-r", "-m", "-z", commit)
                metadata_bytes += len(raw)
                if metadata_bytes > 8_000_000:
                    raise ValueError("Publication history exceeds its metadata bound")
                rows = raw.split(b"\0")
                if rows.pop() != b"" and raw:
                    raise ValueError("Malformed publication tree inventory")
                if len(rows) % 2:
                    raise ValueError("Malformed publication tree inventory")
                for index in range(0, len(rows), 2):
                    total_entries += 1
                    if total_entries > 10_000:
                        raise ValueError("Publication history exceeds its entry bound")
                    fields = rows[index].split()
                    if (len(fields) != 5 or not fields[0].startswith(b":")
                            or fields[4] not in {b"A", b"M", b"D", b"T"}):
                        raise ValueError("Malformed publication tree entry")
                    mode, oid = fields[1].decode("ascii"), fields[3].decode("ascii")
                    name = rows[index + 1].decode("utf-8", "strict")
                    if not name or not re.fullmatch(r"[a-fA-F0-9]{40}|[a-fA-F0-9]{64}", oid):
                        raise ValueError("Malformed publication blob identity")
                    if fields[4] == b"D":
                        continue  # Its introduced version is inspected in its earlier commit.
                    path = Path(name)
                    if path.name.lower() in {".env", "id_rsa", "id_ed25519"} or path.suffix.lower() in {".pfx", ".p12", ".pem", ".key"}:
                        return "Potential credential file in published history: " + redact(name)
                    if mode not in {"100644", "100755"}:
                        raise ValueError("Unsupported published Git entry mode: " + redact(name))
                    python = path.suffix.lower() in {".py", ".pyw"}
                    if (oid, python) in inspected:
                        continue
                    if oid not in blobs:
                        size = int(self._history_git("cat-file", "-s", oid, max_bytes=1024).decode("ascii"))
                        if size < 0 or size >= 2_000_000 or total_bytes + size > 16_000_000:
                            raise ValueError("Published blob inspection exceeds its byte bound: " + redact(name))
                        data = self._history_git("cat-file", "blob", oid, max_bytes=max(1024, size + 1))
                        if len(data) != size:
                            raise ValueError("Published blob size differs from immutable object metadata")
                        identity = hashlib.new("sha1" if len(oid) == 40 else "sha256",
                            b"blob " + str(size).encode("ascii") + b"\0" + data).hexdigest()
                        if identity != oid.lower():
                            raise ValueError("Published blob content differs from its Git object identity")
                        blobs[oid] = data
                        total_bytes += size
                    data = blobs[oid]
                    if b"\0" in data:
                        raise ValueError("Published binary blob cannot be inspected for secrets: " + redact(name))
                    content = data.decode("utf-8", "strict")
                    detected = _python_has_secret(content) if python else SECRET.search(content)
                    if detected:
                        return "Potential secret in published history: " + redact(name)
                    inspected.add((oid, python))
        except (ValueError, OSError, UnicodeError) as error:
            return "Publication history secret inspection failed: " + redact(error)
        return None

    def _secret_check(self, baseline, current, *, head=None):
        baseline, current = self._local(baseline), self._local(current)
        history = self._history_secret_check(baseline, head)
        if history:
            return history
        publishable = set(current["files"])
        if baseline.get("commit"):
            publishable = {path for path in self._git("ls-files", "--cached", "--others", "--exclude-standard", "-z", strip=False).split("\0") if path}
        for name, version in current["files"].items():
            if name not in publishable or baseline["files"].get(name) == version:
                continue
            path = self.repo / name
            if path.is_file() and (path.name.lower() in {".env", "id_rsa", "id_ed25519"}
                                  or path.suffix.lower() in {".pfx", ".p12", ".pem", ".key"}):
                return "Potential credential file: " + name
            if path.is_symlink():
                # The fingerprint also binds dereferenced bytes. A reviewed
                # target change does not change an existing Git link blob.
                before = baseline["files"].get(name, "")
                target = "link:" + os.readlink(path)
                if (version.startswith("link:") and before.startswith("link:")
                        and before.rsplit(":", 1)[0] == version.rsplit(":", 1)[0] == target):
                    continue
                return "Changed source is a redirected or shared file: " + name
            if path.is_file() and path.stat().st_nlink > 1:
                return "Changed source is a redirected or shared file: " + name
            if path.is_file() and path.stat().st_size < 2_000_000:
                content = path.read_text(encoding="utf-8", errors="replace")
                detected = _python_has_secret(content) if path.suffix.lower() in {".py", ".pyw"} else SECRET.search(content)
                if detected:
                    return "Potential secret in changed file: " + name
        return None

    def _publication_paths(self, task, baseline, current):
        """Account for worktree, index and every new commit, not just net bytes.

        Remote authorization names a destination; it does not widen the task's
        approved resources or authorize publishing unrelated local history.
        """
        baseline, current = self._local(baseline), self._local(current)
        base = baseline.get("commit")
        if not isinstance(base, str) or not re.fullmatch(r"[a-fA-F0-9]{40}|[a-fA-F0-9]{64}", base):
            raise ValueError("A full baseline commit is required to establish publication ownership")
        head = self._git("rev-parse", "HEAD")
        try:
            self._git("merge-base", "--is-ancestor", base, head)
        except ValueError:
            raise ValueError("Current HEAD diverged from the task baseline; publication ownership is unknown") from None

        def paths(*args):
            return {path for path in self._git(*args, strip=False).split("\0") if path}

        staged = paths("diff", "--no-ext-diff", "--no-textconv", "--cached", "--name-only", "--no-renames", "-z")
        stage_paths = staged | paths("diff", "--no-ext-diff", "--no-textconv", "--name-only", "--no-renames", "-z", "HEAD")
        stage_paths |= {path for path in paths("ls-files", "--others", "--exclude-standard", "-z") if not is_bytecode_cache(path)}
        # The fingerprint also observes ignored read-only dependencies. They
        # remain gate inputs, but Git will not stage or publish those files.
        changed = set(stage_paths)
        commits = self._git("rev-list", base + ".." + head).splitlines()
        if len(commits) > 1000:
            raise ValueError("Publication history exceeds the bounded task review scope")
        for commit in commits:
            changed |= paths("diff-tree", "--no-commit-id", "--name-only", "--no-renames", "-r", "-m", "-z", commit)
        if changed and "file_write" not in task["contract"]["scope"]["effects"]:
            raise ValueError("Source publication changes require the approved file_write effect")
        root = self.repo.resolve()

        def canonical(path):
            relative = Path(path)
            if relative.is_absolute() or ".." in relative.parts:
                raise ValueError("Publishing scope paths must stay relative to the selected repository")
            resolved = (root / relative).resolve()
            if not resolved.is_relative_to(root) or any(part.lower() == ".git" for part in resolved.relative_to(root).parts):
                raise ValueError("Publishing scope escapes the selected repository")
            return resolved

        known = {canonical(path) for path in set(baseline["files"]) | set(current["files"]) | changed}
        baseline_files = {canonical(path) for path in baseline["files"]}
        boundaries = []
        for resource in task["contract"]["scope"]["resources"]:
            if self.prefix:
                resource = resource.replace("\\", "/").rstrip("/")
                # Scope resources are workspace-relative; the task repository root itself is ".".
                resource = "." if resource + "/" == self.prefix else resource[len(self.prefix):] if resource.startswith(self.prefix) else resource
            boundary = canonical(resource)
            directory = boundary not in baseline_files and (boundary.is_dir() or any(path != boundary and path.is_relative_to(boundary) for path in known))
            boundaries.append((boundary, directory))
        outside = [path for path in sorted(changed) if not any(canonical(path) == boundary or
                   (directory and canonical(path).is_relative_to(boundary)) for boundary, directory in boundaries)]
        if outside:
            raise ValueError("Changes outside approved publication scope: " + ", ".join(outside))
        return sorted(stage_paths)

    def _text_eol_allowed(self, name, commit):
        source = "--source=" + commit if commit else "--cached"
        raw = self._git("check-attr", "-z", source, "text", "eol", "working-tree-encoding", "--", name, strip=False)
        fields = raw.split("\0")
        if fields.pop() != "" or len(fields) != 9:
            raise ValueError("Malformed publication text attributes")
        attrs = {}
        for index in range(0, len(fields), 3):
            if fields[index] != name:
                raise ValueError("Publication text attributes name another source")
            attrs[fields[index + 1]] = fields[index + 2]
        if attrs.get("text") == "unset" or attrs.get("working-tree-encoding") not in {"unspecified", "unset"}:
            return False
        if attrs.get("text") in {"set", "auto"} or attrs.get("eol") in {"lf", "crlf"}:
            return True
        return (attrs.get("text") == "unspecified" and
                self._git("config", "--get", "core.autocrlf", check=False).lower() in {"true", "input"})

    def _publication_tree(self, current, *, commit=None):
        """Bind actual index/commit blobs to reviewed bytes, without filters.

        Only Git's explicitly selected text CRLF-to-LF normalization is admitted.
        A custom filter/encoding transform needs verification of that artifact;
        clean status alone is not evidence about the immutable source tree.
        """
        current = self._local(current)
        args = (("ls-tree", "-r", "-z", "--full-tree", commit) if commit else
                ("ls-files", "--stage", "-z"))
        rows = self._history_git(*args).split(b"\0")
        if rows.pop() != b"" or len(rows) > 10_000:
            raise ValueError("Publication tree inventory is malformed or exceeds its bound")
        entries = {}
        for row in rows:
            metadata, separator, raw_name = row.partition(b"\t")
            fields = metadata.decode("ascii").split()
            name = raw_name.decode("utf-8", "strict")
            if not separator or len(fields) != 3 or name in entries:
                raise ValueError("Malformed publication tree entry")
            mode, oid = (fields[0], fields[2]) if commit else (fields[0], fields[1])
            if ((fields[1] != "blob" if commit else fields[2] != "0")
                    or mode not in {"100644", "100755", "120000"}
                    or not re.fullmatch(r"[a-fA-F0-9]{40}|[a-fA-F0-9]{64}", oid)):
                raise ValueError("Unsupported publication tree entry: " + redact(name))
            entries[name] = (mode, oid.lower())
        publishable = {name for name in self._git("ls-files", "--cached", "--others", "--exclude-standard", "-z", strip=False).split("\0") if name}
        if set(entries) != set(current["files"]) & publishable:
            raise ValueError("Publication tree paths differ from reviewed source")
        for name, (mode, oid) in entries.items():
            path = self.repo / name
            if (not path.resolve().is_relative_to(self.repo.resolve())
                    or any(part.lower() == ".git" for part in Path(name).parts)):
                raise ValueError("Publication tree source escapes the repository")
            if path.is_symlink():
                target = os.readlink(path)
                version = "link:" + target + ":" + hashlib.sha256(path.read_bytes()).hexdigest()
                data = os.fsencode(target)
                if mode != "120000":
                    raise ValueError("Publication tree link mode differs from reviewed source: " + redact(name))
            else:
                # Git for Windows represents existing links as target-text files
                # when core.symlinks=false. Preserve that exact blob/mode too;
                # new link history retains the existing unsupported-mode guard.
                link_stub = (mode == "120000" and os.name == "nt" and
                    self._git("config", "--get", "core.symlinks", check=False).lower() == "false")
                if not path.is_file() or path.stat().st_nlink != 1 or (mode == "120000" and not link_stub):
                    raise ValueError("Publication tree requires an unshared reviewed regular file: " + redact(name))
                data = path.read_bytes()
                executable = os.name != "nt" and bool(path.stat().st_mode & 0o111)
                version = hashlib.sha256(data).hexdigest() + (":x" if executable else "")
                if os.name != "nt" and (mode == "100755") != executable:
                    raise ValueError("Publication tree executable mode differs from reviewed source: " + redact(name))
            if current["files"].get(name) != version:
                raise ValueError("Source changed while binding publication tree: " + redact(name))

            def blob_identity(content):
                return hashlib.new("sha1" if len(oid) == 40 else "sha256",
                    b"blob " + str(len(content)).encode("ascii") + b"\0" + content).hexdigest()

            if blob_identity(data) == oid:
                continue
            normalized = data.replace(b"\r\n", b"\n")
            if (mode != "120000" and b"\0" not in data and b"\r" not in normalized
                    and normalized != data and blob_identity(normalized) == oid
                    and self._text_eol_allowed(name, commit)):
                continue
            raise ValueError("Publication tree bytes differ from reviewed source: " + redact(name))
        return entries

    def _proof(self, folder, kind, task_id, payload):
        path = folder / (kind + ".json")
        # Published proof writers hold the acknowledgement DB transaction;
        # committed proof owns its unique folder under the root execution lease.
        # Stage complete bytes so a crash cannot reserve a partial final JSON.
        if path.exists():
            if json.loads(state_io(path).read_text(encoding="utf-8")) != payload:
                raise ValueError("Publication proof conflicts with the confirmed attempt")
        else:
            staged = folder / ("." + kind + "-" + uuid.uuid4().hex + ".tmp")
            try:
                with state_io(staged).open("x", encoding="utf-8", newline="") as stream:
                    json.dump(payload, stream, ensure_ascii=False, sort_keys=True, indent=2)
                    stream.write("\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                if path.exists():
                    if json.loads(state_io(path).read_text(encoding="utf-8")) != payload:
                        raise ValueError("Publication proof conflicts with the confirmed attempt")
                else:
                    os.replace(state_io(staged), state_io(path))
            finally:
                state_io(staged).unlink(missing_ok=True)
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        Journal(self.store).append({"kind": kind, "task_id": task_id,
            "summary": ("Published" if kind == "published" else "Created local") + " commit " + payload["commit"],
            "evidence_refs": [str(path) + "#sha256=" + digest, "git:commit:" + payload["commit"]]},
            event_key="publication:" + folder.name + ":" + kind)
        return {"path": str(path), "sha256": digest}

    def _publication_authority(self, controller, task_id, remote):
        with transaction(self.store.db):
            task = self.tasks.snapshot(task_id)
            if task["legacy_v1"] or task["contract"]["profile"] != "software":
                raise ValueError("Current software task approval is required for publishing")
            for stage in task["contract"]["stages"]:
                self.tasks.require_approval(task_id, stage["id"], allow_terminal=True)
            if controller._stop_requested(task_id):
                raise ValueError("Task or ancestor is stopped or cancelled")
            from .delegations import DelegationRegistry
            delegations = DelegationRegistry(self.store).closure_binding(task_id)
            authority = controller._authority_binding(task_id)
            return {"workflow": authority, 'delegations_sha256': delegations,
                "stop_revisions": {item["task_id"]: read_record(self.store.db, "stop", item["task_id"])["revision"]
                    for item in authority["ancestry"]},
                "publishing_authorization_revision": self._authorization(remote)["revision"]}

    def _publication_control(self, controller, task_id, remote, authority):
        def cancelled():
            try:
                return self._publication_authority(controller, task_id, remote) != authority
            except ValueError:
                return True

        @contextmanager
        def launch_guard():
            # Match the workflow's short check/Popen fence. Never keep a DB
            # writer while Git, its hooks or a credential helper is running.
            with transaction(self.store.db):
                if cancelled():
                    raise _PublicationHeld("Original publication authority changed before launch")
                yield
        return cancelled, launch_guard

    def _push_key(self, url, ref):
        # Footprint identity does not grant transport authority. Local path and
        # file-URL spellings share one fence; transport keeps the exact URL.
        parsed = urlsplit(url)
        if parsed.scheme.lower() == "file":
            path = unquote(parsed.path)
            if parsed.netloc and parsed.netloc.lower() != "localhost":
                path = "//" + parsed.netloc + path
            elif os.name == "nt" and re.match(r"^/[A-Za-z]:/", path):
                path = path[1:]
            identity = "file:" + os.path.normcase(str(Path(path).resolve()))
        elif not parsed.scheme and re.match(r"^[^/\\:]+:.+", url):
            host, path = url.split(":", 1)
            identity = "scp:" + host.lower() + ":" + path
        elif not parsed.scheme or re.match(r"^[A-Za-z]:[\\/]", url):
            path = Path(url)
            identity = "file:" + os.path.normcase(str((path if path.is_absolute() else self.repo / path).resolve()))
        else:
            identity = urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path, parsed.query, parsed.fragment))
        return hash_value({"url_identity": identity, "ref": ref})

    def _push_folder(self, value):
        intent = value["intent"]
        folder = (self.store.root / "tasks" / identifier(intent["task_id"], "publication task")
                  / "publications" / identifier(value["host_attempt_id"], "publication attempt"))
        _safe_state(folder)
        state_io(folder).mkdir(parents=True, exist_ok=True)
        return folder

    def _reserve_push(self, task_id, remote, url, branch, commit, fingerprint, authority, gate, proof, attempt):
        key = self._push_key(url, "refs/heads/" + branch)
        intent = {"project_id": self.store.project_id, "workspace_id": self.store.workspace_id,
            "root_task_id": authority["workflow"]["ancestry"][-1]["task_id"], "task_id": task_id,
            "remote": remote, "url": url, "ref": "refs/heads/" + branch, "branch": branch,
            "commit": commit, "fingerprint": fingerprint, "authority": authority,
            "gate_binding": {name: gate.get(name) for name in ("fingerprint", "contract_hash", "proposal_hash", "plan_hash")}}
        with transaction(self.store.db):
            old = read_record(self.store.db, "publication_push", key)
            previous = old["payload"] or {}
            if (previous and (previous["state"] not in {"ABSENT", "APPLIED"}
                    or (previous["state"] == "APPLIED" and previous["intent"]["commit"] == commit))):
                raise _PublicationHeld("Publication target has a prior attempt; reconcile it before another push")
            value = {"version": 1, "action_id": key, "state": "UNKNOWN", "intent": intent,
                "intent_hash": hash_value(intent), "attempt_number": previous.get("attempt_number", 0) + 1,
                "host_attempt_id": attempt, "proof": proof}
            write_record(self.store.db, "publication_push", key, value, expected_revision=old["revision"])
        # This owned transaction commits before entering the possible-Popen scope.
        return value

    def _push_nonlaunch(self, value, witness):
        if witness.phase != "NOT_LAUNCHED" or not witness.proof:
            return
        with transaction(self.store.db):
            old = read_record(self.store.db, "publication_push", value["action_id"])
            current = old["payload"]
            if current["host_attempt_id"] == value["host_attempt_id"] and current["state"] == "UNKNOWN":
                write_record(self.store.db, "publication_push", value["action_id"],
                    {**current, "state": "ABSENT", "host_nonlaunch_proof": witness.proof}, expected_revision=old["revision"])

    def _retain_push_proof(self, value, proof):
        """Keep actual receipts without changing a newer/acknowledged attempt."""
        with transaction(self.store.db):
            old = read_record(self.store.db, "publication_push", value["action_id"])
            current = old["payload"]
            if current["host_attempt_id"] == value["host_attempt_id"] and current["state"] == "UNKNOWN":
                write_record(self.store.db, "publication_push", value["action_id"],
                    {**current, "proof": proof}, expected_revision=old["revision"])

    def _prior_push_receipt(self, value):
        """A newer target CAS cannot erase an already acknowledged old effect."""
        path = self._push_folder(value) / "published.json"
        try:
            data = state_io(path).read_bytes()
            proof = json.loads(data)
            artifact = {"path": str(path), "sha256": hashlib.sha256(data).hexdigest()}
            event = Journal(self.store).repository.event("publication:" + value["host_attempt_id"] + ":published")
            intent = value["intent"]
            if (not event or event["task_id"] != intent["task_id"]
                    or str(path) + "#sha256=" + artifact["sha256"] not in event.get("evidence_refs", [])
                    or any(proof.get(name) != intent[name] for name in ("commit", "project_id", "workspace_id", "url", "branch"))):
                raise ValueError("Prior publication receipt does not bind this attempt")
        except (OSError, ValueError, TypeError) as error:
            raise _PublicationHeld("Prior publication acknowledgement unavailable: " + redact(error)) from error
        return {**value, "state": "APPLIED", "confirmation": proof, "artifact": artifact, "superseded": True}

    def _push_assessment(self, intent, *, recovery=False):
        post = {"head": None, "branch": None, "dirty": None, "fingerprint": None,
                "observed_at_utc": now(), "reasons": []}
        def fingerprint():
            task = self.tasks.snapshot(intent["task_id"])
            return source_snapshot(self.store, task["contract"], task_id=intent["task_id"])["fingerprint"]
        observations = {"head": lambda: self._git("rev-parse", "HEAD"),
            "branch": lambda: self._git("branch", "--show-current"),
            "dirty": self._dirty, "fingerprint": fingerprint}
        expected = {"head": intent["commit"], "branch": intent["branch"], "dirty": "", "fingerprint": intent["fingerprint"]}
        for name, observe in observations.items():
            try:
                post[name] = observe()
                if post[name] != expected[name]:
                    post["reasons"].append("Publication confirmed; local " + name + " differs from the reviewed publication inputs")
            except (ValueError, OSError, RuntimeError, subprocess.SubprocessError) as error:
                post["reasons"].append("Publication confirmed; local " + name + " observation unavailable: " + redact(error))
        post["status"] = "BLOCKED" if post["reasons"] else "PASS"
        try:
            authority_current = (intent["project_id"] == self.store.project_id
                and intent["workspace_id"] == self.store.workspace_id
                and self._publication_authority(WorkflowController(self.store), intent["task_id"], intent["remote"]) == intent["authority"])
        except ValueError:
            authority_current = False
        reasons = [*post["reasons"], *([] if authority_current else ["Publication confirmed; original authority changed after launch"])]
        if recovery:
            try:
                gate = GateService(self.store).gate(intent["task_id"])
                if gate.get("status") != "APPROVED" or any(gate.get(name) != expected for name, expected in intent["gate_binding"].items()):
                    reasons.append("Publication confirmed; current evidence gate no longer approves the original inputs")
            except (ValueError, OSError, RuntimeError, subprocess.SubprocessError) as error:
                reasons.append("Publication confirmed; current evidence gate unavailable: " + redact(error))
        return post, authority_current, reasons

    def _confirm_push(self, value, proof, *, recovery=False):
        post, authority_current, reasons = self._push_assessment(value["intent"], recovery=recovery)
        proof = {**proof, "remote_commit": value["intent"]["commit"], "confirmed_at_utc": now(),
            "post_push": post, "authority_current": authority_current, "reasons": reasons}
        with transaction(self.store.db):
            old = read_record(self.store.db, "publication_push", value["action_id"])
            current = old["payload"]
            if current["host_attempt_id"] != value["host_attempt_id"]:
                return self._prior_push_receipt(value)
            if current["state"] == "UNKNOWN":
                current = {**current, "state": "CONFIRMED", "confirmation": proof}
                write_record(self.store.db, "publication_push", value["action_id"], current, expected_revision=old["revision"])
        # Freeze positive confirmation before filesystem/journal acknowledgement.
        return current

    def _ack_push(self, value):
        with transaction(self.store.db):
            old = read_record(self.store.db, "publication_push", value["action_id"])
            current = old["payload"]
            if current["host_attempt_id"] != value["host_attempt_id"]:
                return self._prior_push_receipt(value)
            if current["state"] not in {"CONFIRMED", "APPLIED"}:
                raise _PublicationHeld("Publication confirmation changed before acknowledgement")
            if current["state"] == "CONFIRMED":
                artifact = self._proof(self._push_folder(current), "published", current["intent"]["task_id"], current["confirmation"])
                current = {**current, "state": "APPLIED", "artifact": artifact}
                write_record(self.store.db, "publication_push", value["action_id"], current, expected_revision=old["revision"])
        return current

    def _push_result(self, value, *, requested_task_id=None, recovery=False, observed_commit=None):
        intent, proof = value["intent"], value["confirmation"]
        if recovery:
            post, authority_current, reasons = self._push_assessment(intent, recovery=True)
            if observed_commit != intent["commit"]:
                reasons.append("Publication was confirmed earlier; the current remote ref does not confirm that commit")
        else:
            post, authority_current, reasons = proof["post_push"], proof["authority_current"], proof["reasons"]
        current_owner = (intent["project_id"] == self.store.project_id and intent["workspace_id"] == self.store.workspace_id
                         and requested_task_id in {None, intent["task_id"]})
        if not current_owner:
            reasons = [*reasons, "Confirmed publication belongs to the original task/workspace; it grants no current task approval"]
        return {"status": "BLOCKED" if reasons else "APPROVED", "published": current_owner,
            "historical_publication_confirmed": True, "publication_state": "APPLIED", "dry_run": False,
            "task_id": intent["task_id"], "project_id": intent["project_id"], "workspace_id": intent["workspace_id"],
            "remote": intent["remote"], "branch": intent["branch"], "commit": intent["commit"],
            "confirmed_commit": intent["commit"], "remote_commit": observed_commit if recovery else intent["commit"],
            "remote_confirmation": "CURRENT_EXACT" if not recovery or observed_commit == intent["commit"] else "HISTORICAL_ONLY",
            "authority_current": authority_current, "post_push": post,
            "reconciled_push_error": proof.get("reconciled_push_error", False), "proof": value["artifact"],
            **({"reasons": reasons} if reasons else {}), **({"reconciled": True} if recovery else {})}

    def _recover_push(self, task_id, remote):
        branch = self._git("branch", "--show-current")
        urls = self._urls(remote)
        key = self._push_key(urls[0], "refs/heads/" + branch)
        value = read_record(self.store.db, "publication_push", key)["payload"]
        if not value or value["state"] == "ABSENT":
            return None
        if value["state"] == "APPLIED" and value["intent"]["commit"] != self._git("rev-parse", "HEAD"):
            return None  # A new commit still needs all ordinary approval/gate/URL checks.
        folder = self._push_folder(value)
        env = self._transport_environment(remote, value["intent"]["url"])
        observed = execute(self._argv("ls-remote", "--heads", remote, value["intent"]["ref"]), self.repo,
            folder / ("reconcile-" + uuid.uuid4().hex + ".log"), 300, env=env)
        remote_commit = self._observed_push_commit(observed, value["intent"]["ref"])
        if value["state"] == "UNKNOWN":
            if remote_commit != value["intent"]["commit"]:
                return self._failure("BLOCKED", "Prior push remains UNKNOWN; absence or a different ref value does not authorize replay",
                    publication_state="UNKNOWN", commit=value["intent"]["commit"], evidence_root=str(folder))
            proof = {**value["proof"], "commands": [*value["proof"]["commands"], observed]}
            value = self._confirm_push(value, proof, recovery=True)
        value = self._ack_push(value)
        return self._push_result(value, requested_task_id=task_id, recovery=True, observed_commit=remote_commit)

    @staticmethod
    def _observed_push_commit(observed, ref):
        if observed.get("exit_code") == 0 and not any(observed.get(key) for key in ("error", "timed_out", "cancelled")):
            for line in state_io(observed["log"]).read_text(encoding="utf-8").splitlines():
                fields = line.split()
                if len(fields) == 2 and fields[1] == ref and re.fullmatch(r"[a-fA-F0-9]{40}|[a-fA-F0-9]{64}", fields[0]):
                    return fields[0].lower()
        return None

    def publish(self, task_id, execute_push=False, remote="origin", commit_message=None):
        remote = self._remote(remote)
        if type(execute_push) is not bool:
            raise ValueError("execute_push must be boolean")
        if execute_push and self.store.db.in_transaction:
            raise ValueError("Publication requires a committed write-ahead boundary; caller transaction is active")
        if commit_message is not None:
            text(commit_message, "commit message", limit=2000)
        task = self.tasks.snapshot(task_id)
        if task["legacy_v1"]:
            return self._failure("BLOCKED", "Legacy tasks have no v2 publishing authority")
        if task["contract"]["profile"] != "software":
            return self._failure("BLOCKED", "Publishing requires the software acceptance profile")
        try:
            self._select_task(task["contract"])
        except ValueError as error:
            return self._failure("BLOCKED", redact(error))
        self._repository()
        if execute_push:
            try:
                self._urls(remote)
            except ValueError as error:
                return self._failure("REJECTED", redact(error))
            try:
                recovered = self._recover_push(task_id, remote)
            except (ValueError, OSError) as error:
                return self._failure("BLOCKED", redact(error))
            if recovered is not None:
                return recovered
        if task["state"] == "CANCELLED":
            return self._failure("BLOCKED", "Cancelled task cannot publish")
        controller = WorkflowController(self.store)
        try:
            # Capture once before the ownership boundary; a later replacement
            # approval or a stop followed by resume cannot rebind this batch.
            authority = self._publication_authority(controller, task_id, remote)
        except ValueError as error:
            return self._failure("BLOCKED", redact(error))
        root_id = authority["workflow"]["ancestry"][-1]["task_id"]
        with _exclusive(self.store.root / "tasks" / root_id / "execution.lock"):
            cancelled, launch_guard = self._publication_control(controller, task_id, remote, authority)
            if cancelled():
                return self._failure("BLOCKED", "Original publication authority changed before ownership")
            return self._publish(task_id, execute_push, remote, commit_message, cancelled, launch_guard, authority)

    def _publish(self, task_id, execute_push, remote, commit_message, cancelled, launch_guard, authority):
        task = self.tasks.snapshot(task_id)
        gate = GateService(self.store).gate(task_id)
        if gate["status"] != "APPROVED":
            return {**gate, "published": False}
        branch = self._git("branch", "--show-current")
        if not re.fullmatch(r"(?:feature|bugfix)/[A-Za-z0-9][A-Za-z0-9._/-]*", branch):
            return self._failure("REJECTED", "Publishing requires the feature/* or bugfix/* branch allowlist")
        dirty = self._dirty()
        if dirty and not commit_message:
            return self._failure("BLOCKED", "Working tree dirty; supply a reviewed commit message or commit intended changes first")
        baseline = read_record(self.store.db, "baseline", task_id)["payload"]
        if not isinstance(baseline, dict) or not isinstance(baseline.get("files"), dict):
            return self._failure("BLOCKED", "Task source baseline is unavailable")
        if dirty and (not isinstance(baseline.get("dirty"), str) or baseline["dirty"]):
            return self._failure("REJECTED", "Preexisting or unknown baseline changes prevent automatic staging ownership")
        current = source_snapshot(self.store, task["contract"], task_id=task_id)
        try:
            stage_paths = self._publication_paths(task, baseline, current)
            if dirty and not stage_paths:
                raise ValueError("Dirty work has no exact approved staging paths")
        except ValueError as ex:
            return self._failure("REJECTED", str(ex))
        try:
            urls = self._urls(remote)
        except ValueError as ex:
            return self._failure("REJECTED", str(ex))
        authorized = self._authorized(remote, urls[0])
        stage_arguments = [":(literal)" + path for path in stage_paths]
        commands = ([["git", "add", "-A", "--", *stage_arguments], ["git", "commit", "-m", commit_message]] if dirty else [])
        commands.append(["git", "push", "--porcelain", "--no-follow-tags", remote, "HEAD:refs/heads/" + branch])
        plan = {"status": "APPROVED", "published": False, "dry_run": not execute_push,
                "remote": remote, "branch": branch, "authorized": authorized, "commands": commands}
        if not execute_push:
            return plan
        if not authorized:
            return {**plan, "status": "BLOCKED", "reasons": ["Explicit exact push URL authorization missing or changed"]}
        secret = self._secret_check(baseline, current)
        if secret:
            return self._failure("REJECTED", secret)
        if current["fingerprint"] != gate.get("fingerprint"):
            return self._failure("REJECTED", "Source changed before publishing")
        folder = self.store.root / "tasks" / task_id / "publications" / uuid.uuid4().hex
        _safe_state(folder)
        folder.mkdir(parents=True)
        proof = {"task_id": task_id, "project_id": self.store.project_id, "workspace_id": self.store.workspace_id,
                 "remote": remote, "url": urls[0], "branch": branch, "fingerprint": current["fingerprint"], "commands": []}

        index_tree = None
        transport_env = None
        push_guard = launch_guard

        @contextmanager
        def commit_guard():
            nonlocal index_tree
            # Scan outside the authority transaction so stop/approval writers
            # never wait for a Git scan or hook. The original launch fence then
            # validates authority and owns only the short Popen boundary.
            try:
                if source_snapshot(self.store, task["contract"], task_id=task_id)["fingerprint"] != gate["fingerprint"]:
                    raise ValueError("Source changed before commit launch")
                index_tree = self._publication_tree(current)
            except (ValueError, OSError, UnicodeError) as error:
                raise _PublicationHeld(redact(error)) from error
            with launch_guard():
                yield

        def run(*args):
            guard = commit_guard if args[0] == "commit" else push_guard if args[0] == "push" else launch_guard
            controls = {"cancel_requested": cancelled, "launch_guard": guard} if args[0] in {"add", "commit", "push"} else {}
            result = execute(self._argv(*args), self.repo, folder / (str(len(proof["commands"])) + ".log"), 300,
                             env=transport_env if args[0] in {"push", "ls-remote"} else self._environment(), **controls)
            proof["commands"].append(result)
            return result

        if dirty:
            for args in (("add", "-A", "--", *stage_arguments), ("diff", "--cached", "--check"), ("commit", "-m", commit_message)):
                try:
                    result = run(*args)
                except _PublicationHeld as error:
                    return self._failure("BLOCKED", str(error), evidence_root=str(folder),
                        command_receipts=proof["commands"], observed_head=self._git("rev-parse", "HEAD"))
                if result.get("exit_code") != 0 or result.get("error") or result.get("timed_out"):
                    return self._failure("BLOCKED", "Git " + args[0] + " failed; inspect actual command logs", evidence_root=str(folder),
                        command_receipts=proof["commands"], observed_head=self._git("rev-parse", "HEAD"))
            proof["commit"] = self._git("rev-parse", "HEAD")
            self._proof(folder, "committed", task_id, proof)
        commit = self._git("rev-parse", "HEAD")
        proof["commit"] = commit
        if self._dirty() or source_snapshot(self.store, task["contract"], task_id=task_id)["fingerprint"] != gate["fingerprint"]:
            return self._failure("REJECTED", "Commit/hooks changed tested source or left dirty worktree", commit=commit)
        try:
            self._publication_paths(task, baseline, source_snapshot(self.store, task["contract"], task_id=task_id))
        except ValueError as ex:
            return self._failure("REJECTED", str(ex), commit=commit)
        try:
            committed_tree = self._publication_tree(current, commit=commit)
            if index_tree is not None and committed_tree != index_tree:
                raise ValueError("Commit/hooks changed the reviewed index tree")
        except (ValueError, OSError, UnicodeError) as error:
            return self._failure("REJECTED", redact(error), commit=commit)
        # Recompute through the real gate after commit hooks and immediately
        # before push; historical APPROVED JSON is never sufficient.
        fresh = GateService(self.store).gate(task_id)
        if fresh["status"] != "APPROVED":
            return {**fresh, "published": False, "commit": commit}
        if any(fresh.get(key) != gate.get(key) for key in ("fingerprint", "contract_hash", "proposal_hash", "plan_hash")):
            return self._failure("REJECTED", "Reviewed inputs changed before publishing", commit=commit)
        try:
            final_urls = self._urls(remote)
        except ValueError as ex:
            return self._failure("REJECTED", str(ex), commit=commit)
        if final_urls != urls or not self._authorized(remote, urls[0]):
            return self._failure("REJECTED", "Push destination or authorization changed during commit/hooks", commit=commit)
        if self._git("branch", "--show-current") != branch or self._git("rev-parse", "HEAD") != commit:
            return self._failure("REJECTED", "Branch or commit changed during commit/hooks", commit=commit)
        if self._dirty() or source_snapshot(self.store, task["contract"], task_id=task_id)["fingerprint"] != fresh["fingerprint"]:
            return self._failure("REJECTED", "Source changed immediately before push", commit=commit)
        # Hooks may introduce and then remove content while preserving the
        # tested worktree. Inspect the exact commit about to be pushed again.
        secret = self._secret_check(baseline, current, head=commit)
        if secret:
            return self._failure("REJECTED", secret, commit=commit)
        try:
            transport_env = self._transport_environment(remote, urls[0])
        except (ValueError, OSError) as error:
            return self._failure("REJECTED", redact(error), commit=commit)
        # Commit uncertainty before any possible push Popen. The target CAS is
        # independent of root-task leases, task names and named-remote aliases.
        try:
            attempt = self._reserve_push(task_id, remote, urls[0], branch, commit,
                current["fingerprint"], authority, fresh, proof, folder.name)
        except _PublicationHeld as error:
            return self._failure("BLOCKED", str(error), commit=commit, publication_state="UNKNOWN", evidence_root=str(folder))
        witness = _EffectLaunchWitness(attempt)
        push_guard = witness.guard(launch_guard)
        try:
            with witness:
                pushed = run("push", "--porcelain", "--no-follow-tags", remote, commit + ":refs/heads/" + branch)
        except _PublicationHeld as error:
            return self._failure("BLOCKED", str(error), commit=commit, push_state="NOT_LAUNCHED",
                evidence_root=str(folder), command_receipts=proof["commands"])
        finally:
            self._push_nonlaunch(attempt, witness)
        proof["reconciled_push_error"] = pushed.get("exit_code") != 0
        self._retain_push_proof(attempt, proof)
        # Observation remains permitted after cancellation; launch may have
        # reached the remote. A negative/missing observation cannot renew it.
        observed = run("ls-remote", "--heads", remote, "refs/heads/" + branch)
        remote_commit = self._observed_push_commit(observed, "refs/heads/" + branch)
        self._retain_push_proof(attempt, proof)
        if remote_commit != commit:
            return self._failure("BLOCKED", "Push not confirmed at the authorized destination; reconcile before retrying",
                commit=commit, publication_state="UNKNOWN", evidence_root=str(folder))
        confirmed = self._confirm_push(attempt, proof)
        acknowledged = self._ack_push(confirmed)
        return self._push_result(acknowledged, recovery=acknowledged.get("superseded", False))
