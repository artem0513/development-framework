"""Explicit CLI backends. Native-agent callbacks are advisory, not receipts."""
import hashlib
import os
from pathlib import Path
from typing import Protocol

from ..protocol import normalized

# Below the Windows 32,767-character CreateProcess limit, leaving room for quoting differences.
WINDOWS_COMMAND_LINE_BUDGET = 28000


class BackendAdapter(Protocol):
    name: str
    def probe(self, config: dict, *, deadline=None, cancel_requested=None) -> dict: ...
    def build_invocation(self, request: dict, capabilities: dict) -> dict: ...
    def parse_result(self, raw_events: list[dict], final_payload: object) -> dict: ...


def installed_executable(backend):
    """Fallback when the CLI is not on PATH: Claude's native installer puts it in ~/.local/bin."""
    checked = ['PATH']
    if backend == 'claude':
        local = Path.home() / '.local' / 'bin' / ('claude.exe' if os.name == 'nt' else 'claude')
        if local.is_file():
            return str(local)
        checked.append(str(local))
    raise ValueError(f'CLI_NOT_INSTALLED: {backend} (checked {", ".join(checked)}). Put it on PATH or set the absolute '
                     f'"executable" in this project\'s backend_config state record for {backend}')


def probe_cli(name, config, required_flags, *, deadline=None, cancel_requested=None):
    # Imports lazily to avoid a runner/adapter import cycle.
    from ..runner import capture_process, checked_path
    cwd = checked_path(config["trusted_cwd"], directory=True)
    executable = Path(config["executable"])
    if not executable.is_absolute(): raise ValueError("Absolute host-configured executable required")
    if not executable.exists():
        # Validate existing ancestors without requiring the missing executable.
        ancestor = executable.parent
        while not ancestor.exists(): ancestor = ancestor.parent
        checked_path(ancestor, directory=True)
        return {"backend": name, "installed": False, "available": False, "blocker": "CLI_NOT_INSTALLED",
                "version": None, "executable": str(executable), "executable_sha256": None,
                "argv_prefix": [str(executable)], "trusted_cwd": str(cwd),
                "auth_availability": "not-probed", "protocol_only": config.get("protocol_only") is True,
                "structured_output": False, "clean_start": False}
    executable = checked_path(executable, directory=False)
    prefix = config.get("executable_args", [])
    if not isinstance(prefix, list) or not all(isinstance(x, str) and "\0" not in x for x in prefix):
        raise ValueError("Invalid host-configured executable prefix")
    argv = [str(executable), *prefix]
    help_args = ["exec", "--help"] if name == "codex" else ["--help"]
    results, blocker = [], None
    for args in (["--version"], help_args):
        result = capture_process([*argv, *args], cwd, timeout=10, max_output_bytes=65536,
                                 deadline=deadline, cancel_requested=cancel_requested)
        results.append(result)
        blocker = ("PROCESS_CANCELLED" if result["cancelled"] else "PROCESS_TIMEOUT" if result["timed_out"] else
                   "OUTPUT_LIMIT" if result["output_limited"] else "CLEAN_START_UNAVAILABLE" if result["error"] or result["exit_code"] != 0 else None)
        if blocker: break
    version = results[0]
    help_result = results[1] if len(results) == 2 else {"stdout": b"", "exit_code": None}
    help_text = help_result["stdout"].decode("utf-8", "replace")
    supported = all(flag in help_text for flag in required_flags)
    available = blocker is None and version["exit_code"] == 0 and help_result["exit_code"] == 0 and supported
    return {"backend": name, "installed": version["exit_code"] == 0,
            "version": version["stdout"].decode("utf-8", "replace").strip(),
            "executable": str(executable), "executable_sha256": hashlib.sha256(executable.read_bytes()).hexdigest(),
            "argv_prefix": argv, "trusted_cwd": str(cwd), "available": available,
            "blocker": None if available else blocker or "CLEAN_START_UNAVAILABLE",
            "timed_out": any(result["timed_out"] for result in results),
            "cancelled": any(result["cancelled"] for result in results),
            "output_limited": any(result["output_limited"] for result in results),
            "help_sha256": hashlib.sha256(help_result["stdout"]).hexdigest(),
            "auth_availability": "not-probed", "protocol_only": config.get("protocol_only") is True,
            "structured_output": True, "clean_start": supported,
            "permission_policy": "packet-only; no target tools authorized", "os_sandbox": "not-established"}


def invocation(capabilities, args, *, os_sandbox="not-established"):
    if not capabilities.get("available"):
        raise ValueError(capabilities.get("blocker") or "BACKEND_UNAVAILABLE")
    return {"argv": [*capabilities["argv_prefix"], *args], "cwd": capabilities["trusted_cwd"], "stdin": None,
            "permission_description": {"policy": "packet-only; no target tools authorized", "os_sandbox": os_sandbox},
            "artifact_paths": capabilities.get("artifact_paths", {})}


def get_adapter(name):
    from .codex import CodexAdapter
    from .claude import ClaudeAdapter
    from .copilot import CopilotAdapter
    from .deepseek import DeepSeekAdapter
    backends = {"codex": CodexAdapter, "claude": ClaudeAdapter, "copilot": CopilotAdapter, "deepseek": DeepSeekAdapter}
    if name not in backends: raise ValueError("Unknown explicitly selected backend")
    return backends[name]()
