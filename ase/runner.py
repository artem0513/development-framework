"""Host-captured CLI receipts, bounded input/output, and integrity verification.

The host owns config and artifact_root. Target-controlled requests cannot select
executables, environment, working directory, or permissions. Files and hashes
detect mistakes; they do not constrain another hostile process of the same user.
"""
from datetime import datetime, timezone
from contextlib import nullcontext
import functools
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import tempfile
import threading
import time
import uuid

from .adapters import WINDOWS_COMMAND_LINE_BUDGET
from .protocol import canonical, decode_json, normalized, sha256, validate_packet, validate_payload, DEFAULT_PACKET_BYTES, MAX_DECLARED_PACKET_BYTES


def _long_path(path):
    """Expand Windows 8.3 short components (C:\\PROGRA~1) of an existing path; no link resolution."""
    if os.name != "nt" or "~" not in str(path) or not path.exists():
        return path
    import ctypes
    size = ctypes.windll.kernel32.GetLongPathNameW(str(path), None, 0)
    if not size:
        return path
    buffer = ctypes.create_unicode_buffer(size)
    if not ctypes.windll.kernel32.GetLongPathNameW(str(path), buffer, size):
        return path
    return Path(buffer.value)


def checked_path(value, *, directory):
    path = Path(value)
    if not path.is_absolute(): raise ValueError("Host path must be absolute")
    absolute = Path(os.path.abspath(path))
    if os.path.normcase(str(path)) != os.path.normcase(str(absolute)):
        raise ValueError("Host path traversal is not permitted")
    # Short and long names are the same directory entry; links are still checked component by component below.
    absolute = _long_path(absolute)
    for ancestor in [*reversed(absolute.parents), absolute]:
        if ancestor.exists() or ancestor.is_symlink():
            info = ancestor.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 1024):
                raise ValueError("Reparse points are not permitted in host paths")
            if stat.S_ISREG(info.st_mode) and info.st_nlink != 1:
                raise ValueError("Hardlinked host artifacts are not permitted")
    if os.path.normcase(str(absolute.resolve())) != os.path.normcase(str(absolute)):
        raise ValueError("Host path resolves outside its lexical location")
    if directory and not absolute.is_dir(): raise ValueError("Existing trusted directory required")
    if not directory and not absolute.is_file(): raise ValueError("Existing executable or artifact required")
    return absolute


@functools.cache
def _job_api():
    """Load once, before launch: only syscalls may sit between Popen and job assignment."""
    import ctypes
    from ctypes import wintypes
    class Basic(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong), ("PerJobUserTimeLimit", ctypes.c_longlong),
                    ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD)]
    class IO(ctypes.Structure):
        _fields_ = [(name, ctypes.c_ulonglong) for name in ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount", "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]
    class Extended(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", Basic), ("IoInfo", IO), ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    kernel.SetInformationJobObject.restype = wintypes.BOOL
    kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    return ctypes, wintypes, kernel, Extended


class _WindowsJob:
    """Close-to-kill job, so descendants cannot outlive a captured Windows run."""
    def __init__(self, process):
        self.handle = None
        if os.name != "nt": return
        ctypes, wintypes, kernel, Extended = _job_api()
        handle = kernel.CreateJobObjectW(None, None)
        if not handle: raise OSError(ctypes.get_last_error(), "CreateJobObject failed")
        info = Extended()
        info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel.SetInformationJobObject(handle, 9, ctypes.byref(info), ctypes.sizeof(info)) or not kernel.AssignProcessToJobObject(handle, wintypes.HANDLE(int(process._handle))):
            error = ctypes.get_last_error()
            kernel.CloseHandle(handle)
            raise OSError(error, "Unable to contain subprocess tree")
        self.kernel, self.handle, self.info = kernel, handle, info

    def close(self, *, terminate=True):
        if self.handle:
            if not terminate:
                import ctypes
                self.info.BasicLimitInformation.LimitFlags = 0
                if not self.kernel.SetInformationJobObject(self.handle, 9, ctypes.byref(self.info), ctypes.sizeof(self.info)):
                    raise OSError(ctypes.get_last_error(), "Unable to release completed process job")
            self.kernel.CloseHandle(self.handle)
            self.handle = None


def capture_process(argv, cwd, *, timeout, max_output_bytes, input_bytes=None, extra_output_paths=(), cancel_requested=None, env=None, launch_guard=None, deadline=None):
    """Run shell=False with an owned input channel; never inherit chat stdin."""
    if type(timeout) not in (int, float) or timeout <= 0 or type(max_output_bytes) is not int or max_output_bytes < 1024:
        raise ValueError("Positive timeout and output cap required")
    started = datetime.now(timezone.utc).isoformat()
    began = time.monotonic()
    process, job, writer = None, None, None
    delivered = []  # set only after the complete prompt was written and flushed
    code, error, timed_out, limited, cancelled = None, None, False, False, False
    if deadline is not None: timeout = min(timeout, deadline - began)
    cancelled = cancel_requested is not None and cancel_requested()
    if cancelled or timeout <= 0:
        return {"pid": None, "started_at_utc": started, "ended_at_utc": datetime.now(timezone.utc).isoformat(),
                "duration_seconds": time.monotonic() - began, "exit_code": None, "error": None,
                "timed_out": not cancelled, "output_limited": False, "cancelled": cancelled, "stdout": b"", "stderr": b""}
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr, \
            tempfile.TemporaryDirectory(prefix="ase-pycache-", ignore_cleanup_errors=True) as pycache:
        try:
            if os.name == "nt": _job_api()
            with launch_guard() if launch_guard is not None else nullcontext():
                process = subprocess.Popen(argv, cwd=cwd, stdin=subprocess.PIPE if input_bytes is not None else subprocess.DEVNULL,
                    stdout=stdout, stderr=stderr, shell=False, start_new_session=os.name != "nt",
                    # Untracked __pycache__ is outside the source fingerprint; don't load it (env-honouring Python only).
                    env={**(os.environ if env is None else env), "PYTHONDONTWRITEBYTECODE": "1", "PYTHONPYCACHEPREFIX": pycache})
            job = _WindowsJob(process)
            if input_bytes is not None:
                def send_input():
                    try:
                        process.stdin.write(input_bytes)
                        process.stdin.flush()
                        delivered.append(True)
                    except (OSError, ValueError):
                        pass
                    finally:
                        try: process.stdin.close()
                        except OSError: pass
                writer = threading.Thread(target=send_input, daemon=True)
                writer.start()
            while process.poll() is None:
                if cancel_requested is not None and cancel_requested():
                    cancelled = True
                    break
                sizes = os.fstat(stdout.fileno()).st_size + os.fstat(stderr.fileno()).st_size
                for path in extra_output_paths:
                    if Path(path).exists(): sizes += Path(path).lstat().st_size
                if sizes > max_output_bytes:
                    limited = True
                    break
                if time.monotonic() - began >= timeout:
                    timed_out = True
                    break
                time.sleep(0.01)
            if cancel_requested is not None and cancel_requested():
                cancelled = True
        except KeyboardInterrupt:
            cancelled = True
        except (OSError, ValueError) as failure:
            error = str(failure)
        finally:
            if process is not None:
                if os.name != "nt":
                    try: os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError: pass
                if job: job.close()
                if process.poll() is None: process.kill()
                process.wait(timeout=10)
                code = process.returncode
                if writer: writer.join(timeout=2)
                if input_bytes is not None and not delivered and error is None:
                    # A child that closed stdin early never saw the sealed prompt; never credit its output.
                    error = "Prompt input was not fully delivered"
            size = os.fstat(stdout.fileno()).st_size + os.fstat(stderr.fileno()).st_size
            limited = limited or size > max_output_bytes
        stdout.seek(0)
        out = stdout.read(max_output_bytes)
        stderr.seek(0)
        err = stderr.read(max(0, max_output_bytes - len(out)))
    return {"pid": process.pid if process else None, "started_at_utc": started,
            "ended_at_utc": datetime.now(timezone.utc).isoformat(), "duration_seconds": time.monotonic() - began,
            "exit_code": code, "error": error, "timed_out": timed_out, "output_limited": limited,
            "cancelled": cancelled, "stdout": out, "stderr": err}


def _write(path, data):
    checked_path(path.parent, directory=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    descriptor = os.open(path, flags, 0o600)
    with os.fdopen(descriptor, "wb") as stream: stream.write(data)
    return {"path": str(path), "sha256": sha256(data), "bytes": len(data)}


def _read(path, limit):
    path = checked_path(path, directory=False)
    if path.stat().st_size > limit: raise ValueError("Artifact exceeds byte limit")
    return path.read_bytes()


def _events(data, backend):
    text = data.decode("utf-8", "strict")
    if backend == "claude": values = [decode_json(text)]
    else: values = [decode_json(line) for line in text.splitlines() if line.strip()]
    if not all(isinstance(value, dict) for value in values): raise ValueError("Event must be an object")
    return values


def _parse(adapter, request, stdout, final_data):
    try:
        events = _events(stdout, adapter.name)
        final = decode_json(final_data) if final_data is not None else None
        result = adapter.parse_result(events, final)
    except (ValueError, TypeError, UnicodeError):
        return normalized(blocker="MALFORMED_BACKEND_OUTPUT")
    payload = result.get('payload')
    if isinstance(payload, dict):
        declarations = [payload.get('missing_coverage', [])]
        if isinstance(payload.get('criteria'), list):
            declarations.extend(item.get('missing_coverage', []) for item in payload['criteria'] if isinstance(item, dict))
        result['missing_coverage'] = list(dict.fromkeys(gap for declaration in declarations if isinstance(declaration, list)
                                                      for gap in declaration if isinstance(gap, str)))
    if result["status"] == "COMPLETE":
        validation = validate_payload(result["payload"], request["schema"])
        if validation["status"] != "VALID":
            result.update(status="BLOCKED", blocker="UNSUPPORTED_SCHEMA" if validation["status"] == "BLOCKED" else "SCHEMA_INVALID", validation=validation)
        coverage = request["source_packet"]["coverage"]["missing"]
        if coverage:
            result.update(status="BLOCKED", blocker="SOURCE_COVERAGE_MISSING",
                          missing_coverage=list(dict.fromkeys([*result['missing_coverage'], *coverage])))
    return result


class Runner:
    def __init__(self, config, artifact_root):
        self.config = decode_json(canonical(config))
        self.artifact_root = Path(artifact_root)
        if not self.artifact_root.is_absolute(): raise ValueError("Absolute artifact root required")

    def run(self, adapter, request, *, timeout=300, cancel_requested=None, execution_id=None, launch_guard=None):
        if type(timeout) not in (int, float) or timeout <= 0:
            raise ValueError("Positive timeout required")
        deadline = time.monotonic() + timeout
        if execution_id is not None and (not isinstance(execution_id, str) or not re.fullmatch('[0-9a-f]{32}', execution_id)):
            raise ValueError('Invalid host execution ID')
        allowed = {"role", "project", "bindings", "schema", "source_packet", "handoff"}
        if not isinstance(request, dict) or set(request) - allowed:
            raise ValueError("Only transport-neutral request fields are accepted")
        if request.get('role') not in {'implementation', 'ac', 'review', 'diagnostic'}:
            raise ValueError('Unknown role')
        from .runtime_policy import bind_policy
        request = bind_policy(request)
        request_data = canonical(request)
        # Limits come only from the host-built config, never from request content: a fixed ceiling, while
        # the host's packet builder sizes each packet for its role's model (ase/model_limits.py).
        packet_limit = self.config.get("max_packet_bytes", DEFAULT_PACKET_BYTES)
        if type(packet_limit) is not int or not 0 < packet_limit <= MAX_DECLARED_PACKET_BYTES:
            raise ValueError("Invalid runner packet limit")
        input_limit = self.config.get("max_input_bytes", 2097152)
        if len(request_data) > input_limit: raise ValueError("Request exceeds byte limit")
        request = decode_json(request_data)  # immutable snapshot, no caller-owned nested references
        from .runtime_policy import validate_policy
        validate_policy(request)  # Validate the exact frozen bytes before probe/spawn.
        if request.get("role") not in {"implementation", "ac", "review", "diagnostic"}: raise ValueError("Unknown role")
        bindings = request.get("bindings")
        required_bindings = {"fingerprint", "contract_sha256", "proposal_sha256", "plan_sha256", "check_sha256"}
        if not isinstance(bindings, dict) or not required_bindings <= bindings.keys() or not all(isinstance(v, str) and v for v in bindings.values()):
            raise ValueError("Current source/contract/proposal/plan/check bindings required")
        validate_packet(request.get("source_packet"), max_bytes=packet_limit)
        if validate_payload(None, request.get("schema"))["status"] == "BLOCKED": raise ValueError("Unsupported result schema")
        cwd = checked_path(self.config["trusted_cwd"], directory=True)
        project = request.get("project")
        if not isinstance(project, dict) or not isinstance(project.get("root"), str): raise ValueError("Explicit target project root required")
        target = Path(project["root"]).resolve()
        if cwd == target or cwd.is_relative_to(target): raise ValueError("Trusted cwd must be outside target project")
        check_prompt = getattr(adapter, "check_prompt", None)
        if check_prompt is not None:
            # Native prompt limits fail before an execution folder or request.json exists.
            prefix = self.config.get("executable_args", [])
            check_prompt(request, [os.path.abspath(str(self.config["executable"])), *(prefix if isinstance(prefix, list) else [])])
        # Check every existing ancestor before creating anything; UUID run folders
        # and exclusive writes prevent concurrent role runs sharing artifacts.
        ancestor = self.artifact_root
        while not ancestor.exists() and not ancestor.is_symlink(): ancestor = ancestor.parent
        checked_path(ancestor, directory=True)
        self.artifact_root.mkdir(parents=True, exist_ok=True)
        checked_path(self.artifact_root, directory=True)
        execution_id = execution_id if execution_id is not None else uuid.uuid4().hex
        root = self.artifact_root / execution_id
        root.mkdir(mode=0o700)
        artifacts = {"request": _write(root / "request.json", request_data),
                     "schema": _write(root / "schema.json", canonical(request["schema"]))}
        caps = adapter.probe(self.config, deadline=deadline, cancel_requested=cancel_requested)
        caps["artifact_paths"] = {"schema": str(root / "schema.json"), "final": str(root / "final.json")}
        receipt = {"receipt_version": 2, "execution_id": execution_id, "backend": adapter.name, "role": request["role"],
                   "bindings": bindings, "request_sha256": sha256(request_data), "artifact_root": str(root),
                   "evidence_kind": "protocol_only" if caps.get("protocol_only") else "independent_cli",
                   "executable": caps["executable"], "executable_sha256": caps["executable_sha256"],
                   "version": caps["version"], "capabilities": caps, "artifacts": artifacts}
        if not caps.get("available"):
            result = normalized(blocker=caps.get("blocker") or "BACKEND_UNAVAILABLE")
            receipt.update(pid=None, exit_code=None, status="BLOCKED", error=result["blocker"],
                           timed_out=caps.get("timed_out", False), output_limited=caps.get("output_limited", False), cancelled=caps.get("cancelled", False),
                           terminal=None, session_id=None, argv=[], cwd=str(cwd), permission_description={}, duration_seconds=0)
        else:
            call = adapter.build_invocation(request, caps)
            if call["cwd"] != str(cwd) or call["argv"][:len(caps["argv_prefix"])] != caps["argv_prefix"]:
                raise ValueError("Adapter changed the pinned executable or trusted cwd")
            prompt = call.get("stdin")
            input_bytes = prompt.encode("utf-8") if prompt is not None else None
            if input_bytes is not None and len(input_bytes) > input_limit: raise ValueError("Prompt exceeds byte limit")
            if os.name == 'nt' and len(subprocess.list2cmdline(call["argv"])) > WINDOWS_COMMAND_LINE_BUDGET:
                # Stdin delivery keeps the prompt off argv, but schema and flags still count.
                raise ValueError("PROMPT_ARGUMENT_TOO_LARGE: backend stdin protocol unverified" if input_bytes is None
                                 else "ARGUMENT_TOO_LARGE: command line exceeds the Windows limit")
            artifacts["stdin"] = _write(root / "stdin.bin", input_bytes or b"")
            cap = self.config.get("max_output_bytes", 1048576)
            process = capture_process(call["argv"], cwd, timeout=timeout, max_output_bytes=cap, input_bytes=input_bytes,
                                      extra_output_paths=[root / 'final.json'], cancel_requested=cancel_requested, deadline=deadline,
                                      **({'launch_guard': launch_guard} if launch_guard is not None else {}))
            stdout, stderr = process.pop("stdout"), process.pop("stderr")
            artifacts.update(stdout=_write(root / "stdout.jsonl", stdout), stderr=_write(root / "stderr.log", stderr))
            final_data = None
            if (root / 'final.json').exists():
                try:
                    final_path = checked_path(root / 'final.json', directory=False)
                    remaining = max(0, cap - len(stdout) - len(stderr))
                    truncated = final_path.stat().st_size > remaining
                    if truncated:
                        # Preserve only a bounded prefix after the process tree
                        # has stopped. Truncated output can never validate.
                        with final_path.open('r+b') as stream: stream.truncate(remaining)
                        process.update(output_limited=True, error='FINAL_OUTPUT_LIMIT')
                    final_data = _read(final_path, cap)
                    artifacts['final'] = {"path": str(root / 'final.json'), "sha256": sha256(final_data), "bytes": len(final_data)}
                    if truncated: artifacts['final']['truncated'] = True
                except (ValueError, OSError):
                    process.update(output_limited=True, error="FINAL_ARTIFACT_INVALID")
            result = _parse(adapter, request, stdout, final_data)
            failure = ("PROCESS_TIMEOUT" if process["timed_out"] else "OUTPUT_LIMIT" if process["output_limited"] else
                       "PROCESS_CANCELLED" if process["cancelled"] else "PROCESS_FAILED" if process["exit_code"] != 0 or process["error"] else None)
            if failure: result.update(status="BLOCKED", blocker=failure)
            receipt.update(process, argv=call["argv"], cwd=call["cwd"], permission_description=call["permission_description"],
                           stdin_sha256=sha256(input_bytes or b""), status=result["status"], terminal=result["terminal"], session_id=result["session_id"])
        artifacts["result"] = _write(root / "result.json", canonical(result))
        _write(root / "receipt.json", canonical(receipt))
        return {"receipt": receipt, "result": result}


def verify_receipt(receipt, *, expected_bindings=None, require_current_policy=True):
    """Revalidate persisted bytes, terminal/schema and caller-supplied current inputs.

    A valid protocol_only receipt is transport evidence, never semantic approval.
    Legacy v1 receipts are left untouched and require their existing validator.
    """
    if not isinstance(receipt, dict) or receipt.get("receipt_version") != 2 or receipt.get("status") != "COMPLETE":
        raise ValueError("No completed v2 subprocess receipt")
    if type(receipt.get("pid")) is not int or receipt["pid"] <= 0 or type(receipt.get("exit_code")) is not int or receipt["exit_code"] != 0 or any(receipt.get(k) for k in ("error", "timed_out", "output_limited", "cancelled")):
        raise ValueError("Subprocess did not finish successfully")
    return _verify_capture(receipt, expected_bindings=expected_bindings,
        require_current_policy=require_current_policy, require_success=True)


def verify_capture(receipt, *, expected_bindings=None, require_current_policy=True):
    """Validate terminal captured facts, including failures. Never grants acceptance."""
    if (not isinstance(receipt, dict) or receipt.get('receipt_version') != 2
            or receipt.get('status') not in {'COMPLETE', 'BLOCKED'}):
        raise ValueError('No terminal v2 captured attempt')
    return {**_verify_capture(receipt, expected_bindings=expected_bindings,
        require_current_policy=require_current_policy, require_success=False), 'capture_only': True}


def _verify_capture(receipt, *, expected_bindings, require_current_policy, require_success):
    if expected_bindings is not None and receipt.get("bindings") != expected_bindings:
        raise ValueError("Receipt inputs are stale")
    root = checked_path(receipt["artifact_root"], directory=True)
    if root.name != receipt["execution_id"] or len(root.name) != 32: raise ValueError("Execution artifact root mismatch")
    stored = decode_json(_read(root / "receipt.json", 8388608))
    if canonical(stored) != canonical(receipt): raise ValueError("Receipt metadata differs from captured receipt")
    data = {}
    for name, artifact in receipt.get("artifacts", {}).items():
        path = Path(artifact["path"])
        if path.parent != root: raise ValueError("Artifact escapes execution root")
        value = _read(path, 8388608)
        if sha256(value) != artifact["sha256"] or len(value) != artifact["bytes"]: raise ValueError("Artifact hash mismatch")
        data[name] = value
    required = ('request', 'schema', 'result')
    if receipt.get('capabilities', {}).get('available'):
        required += ('stdin', 'stdout', 'stderr')
    for name in required:
        if name not in data: raise ValueError("Required captured artifact missing")
    request = decode_json(data["request"])
    if sha256(data["request"]) != receipt["request_sha256"] or request["bindings"] != receipt["bindings"] or request["role"] != receipt["role"]:
        raise ValueError("Captured request binding mismatch")
    if (canonical(decode_json(data["schema"])) != canonical(request["schema"])
            or ('stdin' in data and sha256(data['stdin']) != receipt.get('stdin_sha256'))):
        raise ValueError("Schema or input mismatch")
    from .runtime_policy import validate_policy
    from .protocol import packet_prompt
    policy = validate_policy(request, require_current=require_current_policy)
    prompt = packet_prompt(request)
    if 'stdin' in data:
        argv = receipt.get('argv', [])
        argument_delivery = not data['stdin'] and argv[-2:] == ['-p', prompt]
        if receipt['backend'] == 'codex':
            delivered = data['stdin'] == prompt.encode('utf-8')
        elif receipt['backend'] == 'claude':
            # Current Claude captures read the prompt from stdin after a bare -p; older receipts
            # delivered it as the -p argument and keep verifying under that original view.
            delivered = (data['stdin'] == prompt.encode('utf-8') and argv[-1:] == ['-p']) or argument_delivery
        else:
            delivered = argument_delivery
        if not delivered:
            raise ValueError('Captured policy/request differs from the delivered prompt')
    elif receipt.get('pid') is not None or receipt.get('argv') or not receipt.get('error'):
        raise ValueError('Missing model input is not an established unavailable-backend attempt')
    validate_packet(request["source_packet"], max_bytes=8388608)
    from .adapters import get_adapter
    if 'stdin' in data:
        result = _parse(get_adapter(receipt['backend']), request, data['stdout'], data.get('final'))
        failure = ('PROCESS_TIMEOUT' if receipt.get('timed_out') else 'OUTPUT_LIMIT' if receipt.get('output_limited') else
            'PROCESS_CANCELLED' if receipt.get('cancelled') else 'PROCESS_FAILED' if receipt.get('exit_code') != 0 or receipt.get('error') else None)
        if failure:
            result.update(status='BLOCKED', blocker=failure)
    else:
        result = normalized(blocker=receipt['error'])
    if ((require_success and result['status'] != 'COMPLETE') or result['status'] != receipt['status']
            or canonical(result) != canonical(decode_json(data["result"]))
            or canonical(result["terminal"]) != canonical(receipt["terminal"]) or result["session_id"] != receipt["session_id"]):
        raise ValueError("Terminal or normalized result mismatch")
    handoff = request.get('handoff', {})
    identity = {'role': request['role'], 'purpose': handoff.get('purpose'),
        'stage_id': handoff.get('stage_id'), 'schema_sha256': sha256(canonical(request['schema'])),
        'project_id': request['project'].get('project_id'), 'test_plan_sha256': handoff.get('test_plan_sha256')}
    return {"valid": True, "evidence_kind": receipt["evidence_kind"], "execution_id": receipt["execution_id"],
        "result": result, 'request_identity': identity, **policy}
