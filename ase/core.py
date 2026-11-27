"""Shared runtime helpers: command execution receipts, redaction, protected paths, test counts.

The local user owns this program and its state. This is an error-detection boundary,
not protection from a malicious process running with that user's credentials.
"""
from contextlib import nullcontext
from datetime import datetime, date as calendar_date, timedelta
import hashlib
import base64
import json
import os
from pathlib import Path, PureWindowsPath
import re
import shutil
import subprocess
import tempfile
import time
import uuid
from urllib.parse import quote, quote_plus

from .store import now
from .native_paths import logical_path, state_io

PROTECTED = [".github/workflows/*", ".codex/*", ".agents/*", "AGENTS.md", ".gitmodules", ".githooks/*"]
PROTECTED += [".devframework/config.toml", ".devframework/.gitignore", ".devframework/bin/*", ".devframework/autonomous-se/*"]
# A project lists its shared, cross-cutting code (job runners, DI setup, DB context, middleware...)
# here; feature tasks may not change it without recorded authorization. The list protects itself.
PROJECT_PROTECTED_FILE = ".devframework/protected-paths.txt"
PROTECTED += [PROJECT_PROTECTED_FILE]


def project_protected_paths(repo):
    """Glob patterns from the project's protected-paths list; comments and blank lines are ignored."""
    path = Path(repo) / PROJECT_PROTECTED_FILE
    try:
        if path.is_symlink() or path.stat().st_size > 65536:
            raise ValueError("Unsafe or oversized " + PROJECT_PROTECTED_FILE)
        content = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return []
    except UnicodeError:
        raise ValueError(PROJECT_PROTECTED_FILE + " must be UTF-8 text") from None
    return parse_protected_paths(content)


def parse_protected_paths(content):
    """Parse protected-paths text; shared by the local gate and the base-revision CI check."""
    patterns = []
    for line in content.splitlines():
        # Windows separators and "./" prefixes are accepted; "dir/" means everything below dir.
        pattern = line.strip().replace("\\", "/")
        if not pattern or pattern.startswith("#"):
            continue
        pattern = pattern[2:] if pattern.startswith("./") else pattern
        patterns.append(pattern + "*" if pattern.endswith("/") else pattern)
    if any(p.startswith("/") or ":" in p or ".." in p.split("/") for p in patterns):
        raise ValueError(PROJECT_PROTECTED_FILE + " requires repository-relative glob patterns")
    return patterns
# A key prefix inside a word ("task-...") is not a key; after escapes (\n, \u0022, %3D), ANSI colour codes or _/- it still is.
# One literal pattern, like discovery._SECRET, so the publication scanner treats it as regex metadata.
SECRET = re.compile(r"(?i)(?:(?:(?<![a-z0-9])|(?<=\\[nrt])|(?<=\\u[0-9a-f]{4})|(?<=%[0-9a-f]{2})|(?<=\[m)|(?<=[\[;][0-9]m)|(?<=[\[;][0-9]{2}m)|(?<=[\[;][0-9]{3}m))(?:sk-[a-z0-9_-]{16,}|gh[pousr]_[a-z0-9_]{16,})|Bearer\s+[a-z0-9._-]{12,}|(?:password|api[_-]?key|token|secret)\s*[=:]\s*[^\s,;]{8,})")


def redact(value, sensitive_values=()):
    value = str(value)
    variants = set()
    for secret in sensitive_values:
        if isinstance(secret, str) and secret:
            variants.update((secret, quote(secret, safe=""), quote_plus(secret),
                base64.b64encode(secret.encode()).decode(), json.dumps(secret, ensure_ascii=True)[1:-1]))
    for secret in sorted(variants, key=len, reverse=True):
        value = value.replace(secret, "[REDACTED]")
    value = re.sub(r"(?i)(https?://)[^/@\s]+:[^/@\s]+@", r"\1[REDACTED]@", value)
    value = re.sub(r"(?i)([?&](?:token|api[_-]?key|secret|password)=)[^&\s]+", r"\1[REDACTED]", value)
    return SECRET.sub("[REDACTED]", value)


def digest(value):
    return hashlib.sha256(value).hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def load(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    temp.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(temp, path)


def test_counts(output, parser):
    output = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", output)
    total, skipped, failed = 0, 0, 0
    # Bun is also used behind package scripts with no explicit parser. Require
    # its runner header and complete, internally consistent summary per suite;
    # isolated "N pass" lines are not evidence of an executed test run.
    bun_runs = re.split(r"(?m)^bun test v\d[^\r\n]*\r?$", output)
    if parser is None and len(bun_runs) > 1:
        # Autodetection must not approve just the Bun portion of a mixed-runner
        # command while losing failures, skips or an empty foreign test run.
        # Detect summary syntax independently of whether its counters are zero.
        foreign_summary = re.compile(
            r"\bRan \d+ tests? in\b|\b(?:Total|Failed|Skipped):\s*\d+\b|"
            r"\b\d+ (?:passed|failed|errors?|skipped|deselected|xfailed|xpassed)\b|"
            r"\bcollected \d+ items?\b|"
            r"(?i:\bno tests? (?:ran|collected|found)\b|"
            r"\bno test files found\b|\bno test is available\b)|"
            r"# (?:tests|fail|skipped|todo)\s+\d+\b|"
            r"Tests\s*:|Tests\s+\d|TOTAL:"
        )
        if foreign_summary.search(output):
            return {"total": 0, "skipped": 0, "failed": 0}
        summary = re.compile(
            r"(?m)(?P<counts>(?:^[ \t]*\d+ (?:pass|skip|todo|fail)[ \t]*\r?\n)+)"
            r"(?:^[ \t]*\d+ expect\(\) calls?[ \t]*\r?\n)?"
            r"^[ \t]*Ran (?P<ran>\d+) tests? across \d+ files?\."
            r"(?: \[[^\r\n]+\])?[ \t]*\r?$"
        )
        summary_line = re.compile(
            r"(?m)^[ \t]*(?:\d+ (?:pass|skip|todo|fail)\b|"
            r"\d+ expect\(\) calls?\b|Ran \d+ tests? across\b|bun[ \t]+test\b)"
        )
        if summary_line.search(bun_runs[0]):
            return {"total": 0, "skipped": 0, "failed": 0}
        for run in bun_runs[1:]:
            matches = list(summary.finditer(run))
            if len(matches) != 1:
                return {"total": 0, "skipped": 0, "failed": 0}
            match = matches[0]
            # A complete suffix must not hide an earlier/later partial summary.
            unmatched = run[:match.start()] + run[match.end():]
            if summary_line.search(unmatched):
                return {"total": 0, "skipped": 0, "failed": 0}
            entries = re.findall(r"(\d+) (pass|skip|todo|fail)", match["counts"])
            counts = {kind: int(number) for number, kind in entries}
            if (len(counts) != len(entries) or not {"pass", "fail"} <= counts.keys()
                    or sum(counts.values()) != int(match["ran"])):
                return {"total": 0, "skipped": 0, "failed": 0}
            total += counts["pass"] + counts["fail"]
            skipped += counts.get("skip", 0) + counts.get("todo", 0)
            failed += counts["fail"]
        return {"total": total, "skipped": skipped, "failed": failed}
    if parser == "unittest":
        match = re.search(r"Ran (\d+) tests? in", output)
        total = int(match[1]) if match else 0
        skipped = sum(map(int, re.findall(r"skipped=(\d+)", output)))
        failed = sum(map(int, re.findall(r"(?:failures|errors)=(\d+)", output)))
    elif parser == "pytest":
        counts = {key: sum(map(int, re.findall(r"(\d+) " + key, output))) for key in ("passed", "failed", "error", "skipped", "deselected")}
        total = counts["passed"] + counts["failed"] + counts["error"]
        skipped = counts["skipped"] + counts["deselected"]
        failed = counts["failed"] + counts["error"]
    elif parser == "dotnet":
        total = sum(map(int, re.findall(r"Total:\s*(\d+)", output)))
        skipped = sum(map(int, re.findall(r"Skipped:\s*(\d+)", output)))
        failed = sum(map(int, re.findall(r"Failed:\s*(\d+)", output)))
    elif parser == "node":
        # Node test runner, Jest/Vitest, and Jasmine/Angular summaries.
        match = re.search(r"# tests (\d+)", output)
        if match:
            total = int(match[1])
            skipped = sum(map(int, re.findall(r"# (?:skipped|todo) (\d+)", output)))
            failed = sum(map(int, re.findall(r"# fail (\d+)", output)))
        else:
            lines = [line for line in output.splitlines() if re.search(r"Tests\s*:|Tests\s+\d|TOTAL:", line)]
            for line in lines:
                total += sum(map(int, re.findall(r"(\d+) (?:passed|SUCCESS)", line)))
                skipped += sum(map(int, re.findall(r"(\d+) (?:skipped|todo|pending)", line)))
                failed += sum(map(int, re.findall(r"(\d+) (?:failed|FAILED)", line)))
    return {"total": total, "skipped": skipped, "failed": failed}


_MANAGER_SCRIPTS = {"npm": "node_modules/npm/bin/npm-cli.js", "pnpm": "node_modules/pnpm/bin/pnpm.cjs",
                    "yarn": "node_modules/yarn/bin/yarn.js"}


def _command_hash(value):
    return digest(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode())


def _launch_file(path):
    path = Path(path).resolve(strict=True)
    observed = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1048576), b""):
            observed.update(chunk)
    return {"path": str(path), "sha256": observed.hexdigest()}


def validate_command_argv(receipt, configured):
    """Validate captured command identity, without resolving today's PATH/files."""
    expected = [redact(argument) for argument in configured]
    actual, proof = receipt.get("argv"), receipt.get("launch_normalization")
    if receipt.get("configured_argv", expected) != expected:
        raise ValueError("Configured argv differs from approved recipe")
    if proof is None:
        if actual != expected:
            raise ValueError("Execution argv differs from approved recipe without launch proof")
        return
    fields = {"version", "kind", "manager", "wrapper", "runtime", "entrypoint", "configured_sha256", "executed_sha256", "sha256"}
    if (not isinstance(proof, dict) or set(proof) != fields or type(proof.get("version")) is not int or proof.get("version") != 1
            or proof.get("kind") != "windows_package_manager" or not expected or expected[0] not in _MANAGER_SCRIPTS
            or proof.get("manager") != expected[0] or receipt.get("configured_argv") != expected
            or proof["sha256"] != _command_hash({key: value for key, value in proof.items() if key != "sha256"})
            or proof["configured_sha256"] != _command_hash(expected) or proof["executed_sha256"] != _command_hash(actual)):
        raise ValueError("Invalid captured launch normalization binding")
    for key in ("wrapper", "runtime", "entrypoint"):
        identity = proof[key]
        if (not isinstance(identity, dict) or set(identity) != {"path", "sha256"}
                or not isinstance(identity["path"], str) or not PureWindowsPath(identity["path"]).is_absolute()
                or str(PureWindowsPath(identity["path"])).startswith(("\\\\?\\", "\\\\.\\", "\\??\\"))
                or ".." in PureWindowsPath(identity["path"]).parts
                or not isinstance(identity["sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", identity["sha256"])):
            raise ValueError("Invalid captured launch file identity")
    wrapper, runtime, entry = (PureWindowsPath(proof[key]["path"]) for key in ("wrapper", "runtime", "entrypoint"))
    if (wrapper.name.lower() not in {expected[0], expected[0] + ".cmd", expected[0] + ".bat", expected[0] + ".exe"}
            or runtime.name.lower() != "node.exe"
            or entry != wrapper.parent / _MANAGER_SCRIPTS[expected[0]]
            or actual != [proof["runtime"]["path"], proof["entrypoint"]["path"], *expected[1:]]):
        raise ValueError("Unsupported captured package-manager launch shape or argv tail")


def execute(argv, cwd, log, timeout=300, *, env=None, cancel_requested=None, sensitive_values=(), max_output_bytes=None, launch_guard=None):
    """shell=False; keep only redacted stdout/stderr; record failures to start."""
    started = now()
    began = time.monotonic()
    log = logical_path(log)
    state_io(log.parent).mkdir(parents=True, exist_ok=True)
    # Open without truncating: old callers may deliberately reuse one log.
    # Fail before Popen if the eventual capture cannot be written at all.
    with state_io(log).open("ab"):
        pass
    code, error, timed_out, cancelled, limited = None, None, False, False, False
    if max_output_bytes is not None and (type(max_output_bytes) is not int or max_output_bytes < 1024):
        raise ValueError("Output limit must be at least 1024 bytes")
    argv = list(argv)
    configured_argv = [redact(argument, sensitive_values) for argument in argv]
    normalization = None
    # Windows npm/pnpm/yarn are batch wrappers. Run the CLI's JS entry point
    # directly rather than cmd.exe interpolation of repository-supplied args.
    if os.name == "nt" and argv[0] in {"npm", "pnpm", "yarn"}:
        executable = shutil.which(argv[0])
        if executable:
            manager = argv[0]
            script = Path(executable).resolve().parent / _MANAGER_SCRIPTS[manager]
            node = shutil.which("node")
            if script.is_file() and node:
                try:
                    normalization = {"version": 1, "kind": "windows_package_manager", "manager": manager,
                                     "wrapper": _launch_file(executable), "runtime": _launch_file(node), "entrypoint": _launch_file(script)}
                    argv = [normalization["runtime"]["path"], normalization["entrypoint"]["path"], *argv[1:]]
                    for identity in (normalization[key] for key in ("wrapper", "runtime", "entrypoint")):
                        identity["path"] = redact(identity["path"], sensitive_values)
                    normalization.update(configured_sha256=_command_hash(configured_argv),
                                         executed_sha256=_command_hash([redact(argument, sensitive_values) for argument in argv]))
                    normalization["sha256"] = _command_hash(normalization)
                except OSError:
                    error = "Package manager launch identity unavailable"
                    normalization = None
            else:
                error = "Package manager JS entry point unavailable; configure explicit node argv"
    output = ""
    if not error:
        # Loaded before launch: a slow first import after Popen let fast children exit before job assignment.
        from .runner import _WindowsJob, _job_api
        try:
            if os.name == "nt":
                _job_api()
            # Codex can start long-lived MCP children that inherit stdout. A
            # PIPE/communicate waits for those children's EOF even after Codex
            # exits. Spool anonymously and wait for the actual command process.
            # The raw temporary output is removed on close; only redacted logs
            # are retained as evidence.
            with tempfile.TemporaryFile(mode="w+b") as spool, \
                    tempfile.TemporaryDirectory(prefix="ase-pycache-", ignore_cleanup_errors=True) as pycache:
                with launch_guard() if launch_guard is not None else nullcontext():
                    proc = subprocess.Popen(argv, cwd=cwd, stdin=subprocess.DEVNULL, stdout=spool, stderr=subprocess.STDOUT,
                                            shell=False, start_new_session=os.name != "nt",
                                            # Untracked __pycache__ is outside the source fingerprint; don't load it (env-honouring Python only).
                                            env={**(os.environ if env is None else env), "PYTHONDONTWRITEBYTECODE": "1",
                                                 "PYTHONPYCACHEPREFIX": pycache})
                job, settled = None, False
                try:
                    if os.name == "nt":
                        job = _WindowsJob(proc)
                    while proc.poll() is None:
                        if cancel_requested is not None and cancel_requested():
                            cancelled = True
                            break
                        if max_output_bytes is not None and os.fstat(spool.fileno()).st_size > max_output_bytes:
                            limited = True
                            break
                        remaining = timeout - (time.monotonic() - began)
                        if remaining <= 0:
                            timed_out = True
                            break
                        try:
                            proc.wait(timeout=min(0.1, remaining))
                        except subprocess.TimeoutExpired:
                            pass
                    if cancel_requested is not None and cancel_requested():
                        cancelled = True
                    if cancelled:
                        error = "PROCESS_CANCELLED"
                    limited = limited or (max_output_bytes is not None and os.fstat(spool.fileno()).st_size > max_output_bytes)
                    if limited:
                        error = "PROCESS_OUTPUT_LIMIT"
                    settled = not (timed_out or cancelled or limited)
                finally:
                    # A completed command may deliberately start a persistent
                    # resource. Cancellation must still own its descendants
                    # when the command parent has already exited.
                    if job:
                        try:
                            job.close(terminate=not settled)
                        finally:
                            if job.handle:
                                job.close()
                    if not settled:
                        if os.name == "nt" and job is None and proc.poll() is None:
                            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True)
                        elif os.name != "nt":
                            import signal
                            try:
                                os.killpg(proc.pid, signal.SIGKILL)
                            except ProcessLookupError:
                                pass
                        if proc.poll() is None:
                            proc.kill()
                    proc.wait(timeout=30)
                code = proc.returncode
                spool.seek(0)
                output = spool.read(max_output_bytes if max_output_bytes is not None else -1).decode("utf-8", "replace")
        except OSError as ex:
            error = redact(ex, sensitive_values)
    # A capped stream may end midway through a secret, so literal redaction
    # cannot safely recognize its truncated suffix/prefix.
    output = "[OUTPUT OMITTED: PROCESS_OUTPUT_LIMIT]\n" if limited else redact(output, sensitive_values)
    state_io(log).write_text(output, encoding="utf-8")
    result = {"argv": [redact(a, sensitive_values) for a in argv], "configured_argv": configured_argv, "cwd": str(cwd), "started": started, "ended": now(),
            "duration_seconds": time.monotonic() - began, "exit_code": code, "error": error,
            "timed_out": timed_out, "cancelled": cancelled, "log": str(log), "log_sha256": digest(state_io(log).read_bytes())}
    if normalization is not None:
        result["launch_normalization"] = normalization
    if max_output_bytes is not None:
        result["output_limited"] = limited
    return result


class LocalRecords:
    """Time entries, decisions, session summaries and read-only v1 task history.

    The v1 execution engine (start/check/verify/run/gate/publish) was retired; v2 task
    commands own execution. Historical v1 rows and files stay readable and are never rewritten.
    """

    def __init__(self, store):
        self.store = store

    def folder(self, task_id):
        self.store.task(task_id)
        folder = self.store.root / "tasks" / task_id
        if folder.resolve() != folder or not folder.is_relative_to(self.store.root):
            raise ValueError("Task state reparse points are not supported")
        if folder.is_dir():
            for child in folder.iterdir():
                if child.resolve() != child or (child.is_file() and child.stat().st_nlink > 1):
                    raise ValueError("Task evidence reparse points are not supported")
        return folder

    def decision(self, context, decision, rationale, task_id=None):
        if task_id:
            self.store.task(task_id)
        with self.store.db:
            row = self.store.db.execute("INSERT INTO decisions(task_id,context,decision,rationale,created) VALUES(?,?,?,?,?)", (task_id, redact(context), redact(decision), redact(rationale), now()))
        return {"decision_id": row.lastrowid}

    def record_time(self, task_id, activity, started, ended, source="agent", confirmed=False):
        self.store.task(task_id)
        if source not in {"agent", "human"}:
            raise ValueError("Time source must be agent or human")
        a, b = datetime.fromisoformat(started), datetime.fromisoformat(ended)
        if a.tzinfo is None or b.tzinfo is None or b < a:
            raise ValueError("Time range requires timezone-aware ascending timestamps")
        with self.store.db:
            row = self.store.db.execute("INSERT INTO time_entries(task_id,activity,started,ended,seconds,source,confirmed) VALUES(?,?,?,?,?,?,?)", (task_id, redact(activity), started, ended, (b-a).total_seconds(), source, int(confirmed)))
        return {"entry_id": row.lastrowid, "source": source, "confirmed": confirmed}

    def correct_time(self, entry_id, activity=None, started=None, ended=None, source=None, confirmed=None, reason="Explicit CLI correction"):
        row = self.store.db.execute("SELECT * FROM time_entries WHERE id=?", (entry_id,)).fetchone()
        if row is None:
            raise ValueError("Unknown time entry in this project")
        before = dict(row)
        if all(x is None for x in (activity, started, ended, source, confirmed)):
            raise ValueError("Correction requires at least one changed field")
        after = dict(before)
        for key, value in (("activity", activity), ("started", started), ("ended", ended), ("source", source)):
            if value is not None:
                after[key] = redact(value)
        if after["source"] not in {"human", "agent"}:
            raise ValueError("Time source must be agent or human")
        a, b = datetime.fromisoformat(after["started"]), datetime.fromisoformat(after["ended"])
        if a.tzinfo is None or b.tzinfo is None or b < a:
            raise ValueError("Time range requires timezone-aware ascending timestamps")
        after["seconds"] = (b-a).total_seconds()
        # Changed time/source/activity invalidates previous confirmation. The
        # caller must explicitly confirm this corrected entry afresh.
        after["confirmed"] = int(confirmed is True)
        with self.store.db:
            updated = self.store.db.execute("UPDATE time_entries SET activity=?,started=?,ended=?,seconds=?,source=?,confirmed=? WHERE id=? AND activity=? AND started=? AND ended=? AND seconds=? AND source=? AND confirmed=?", (
                after["activity"], after["started"], after["ended"], after["seconds"], after["source"], after["confirmed"], entry_id,
                before["activity"], before["started"], before["ended"], before["seconds"], before["source"], before["confirmed"]))
            if updated.rowcount != 1:
                raise ValueError("Entry changed concurrently; inspect and retry correction")
            audit = self.store.db.execute("INSERT INTO time_entry_audit(entry_id,before_json,after_json,reason,created) VALUES(?,?,?,?,?)", (
                entry_id, json.dumps(before), json.dumps(after), redact(reason), now()))
        return {"entry": after, "audit_id": audit.lastrowid, "note": "Correction audited; human confirmation applies only when explicitly supplied."}

    def session_summary(self, task_id, summary, decision="", unresolved=""):
        self.store.task(task_id)
        if not summary.strip() or any(len(x) > 4000 for x in (summary, decision, unresolved)):
            raise ValueError("Use a nonempty concise summary; each session field is limited to 4000 characters")
        stamp = now()
        with self.store.db:
            row = self.store.db.execute("INSERT INTO sessions(task_id,started,ended,summary,important_decisions,unresolved_questions) VALUES(?,?,?,?,?,?)", (
                task_id, stamp, stamp, redact(summary), redact(decision), redact(unresolved)))
        return {"session_id": row.lastrowid, "task_id": task_id, "summary": redact(summary), "important_decisions": redact(decision), "unresolved_questions": redact(unresolved)}

    def daily(self, date=None):
        date = date or now()[:10]
        if calendar_date.fromisoformat(date).isoformat() != date:
            raise ValueError("Date must be YYYY-MM-DD")
        entries = [dict(r) for r in self.store.db.execute("SELECT * FROM time_entries WHERE substr(started,1,10)=? ORDER BY started", (date,))]
        return {"date": date, "timezone": "UTC offsets retained in entries; date groups by stored start-date", "entries": entries,
                "agent_seconds": sum(r["seconds"] for r in entries if r["source"] == "agent"),
                "confirmed_human_seconds": sum(r["seconds"] for r in entries if r["source"] == "human" and r["confirmed"]),
                "draft_human_seconds": sum(r["seconds"] for r in entries if r["source"] == "human" and not r["confirmed"]),
                "note": "Draft only. Agent runtime is never human billable time."}

    def weekly(self, date=None):
        end = calendar_date.fromisoformat(date or now()[:10])
        if date is not None and end.isoformat() != date:
            raise ValueError("Date must be YYYY-MM-DD")
        start = end - timedelta(days=6)
        days = [self.daily((start + timedelta(days=offset)).isoformat()) for offset in range(7)]
        return {"start_date": start.isoformat(), "end_date": end.isoformat(), "days": days,
                "agent_seconds": sum(d["agent_seconds"] for d in days),
                "confirmed_human_seconds": sum(d["confirmed_human_seconds"] for d in days),
                "draft_human_seconds": sum(d["draft_human_seconds"] for d in days),
                "timezone": "Groups by stored start-date with original UTC offset; no timezone conversion or midnight splitting.",
                "note": "Seven days ending on --date, inclusive. Draft only; runtime is never inferred as human work."}

    def legacy_report(self, task_id):
        """Show a v1 task's recorded evidence as it was; nothing is re-evaluated or written."""
        task = self.store.task(task_id)
        folder = self.folder(task_id)

        def recorded(name):
            path = folder / name
            if not path.is_file():
                return None
            try:
                return load(path)
            except (OSError, ValueError) as ex:
                return {"unreadable": redact(ex)}

        return {"status": "LEGACY_READ_ONLY", "task_id": task_id, "legacy_v1": True,
                "original_task": task["request"], "recorded_status": task["status"], "contract": task["contract"],
                "acceptance": recorded("ac.json"), "review": recorded("review.json"), "checks": recorded("check.json"),
                "recorded_gate": recorded("gate.json"), "recorded_report": recorded("report.json"),
                "note": ("Historical v1 record shown as stored. The v1 engine was retired: this gate was not "
                         "re-evaluated and grants no authority. Revise the task to a v2 contract to continue it.")}
