"""Experimental parser only: live prompt JSONL/clean-start not verified here."""
import os
import subprocess

from . import probe_cli, invocation, WINDOWS_COMMAND_LINE_BUDGET
from ..protocol import decode_json, normalized, packet_prompt


class CopilotAdapter:
    name = "copilot"

    def check_prompt(self, request, argv_prefix):
        # The prompt is an argv value, so it shares the remaining Windows command-line budget.
        if os.name != "nt": return
        argv = self.build_invocation(request, {"available": True, "argv_prefix": argv_prefix, "trusted_cwd": ""})["argv"]
        if len(subprocess.list2cmdline(argv)) > WINDOWS_COMMAND_LINE_BUDGET:
            raise ValueError("PROMPT_ARGUMENT_TOO_LARGE: backend stdin protocol unverified")

    def probe(self, config, *, deadline=None, cancel_requested=None):
        caps = probe_cli(self.name, config, ["--output-format", "--no-custom-instructions", "--disable-builtin-mcps", "--no-auto-update"],
                         deadline=deadline, cancel_requested=cancel_requested)
        if not caps["protocol_only"] and caps["available"]:
            caps.update(available=False, blocker="COPILOT_CLEAN_START_UNVERIFIED", clean_start=False)
        caps["live_format_verified"] = False
        return caps

    def build_invocation(self, request, capabilities):
        return invocation(capabilities, ["--output-format", "json", "--no-custom-instructions", "--disable-builtin-mcps",
            "--no-auto-update", "--no-ask-user", "--no-experimental", "--no-bash-env", "--deny-tool",
            "shell,write,read,url,memory", "-p", packet_prompt(request)])

    def parse_result(self, raw_events, final_payload):
        # Deliberately do not accept SDK session.idle or intermediate assistant text.
        if not raw_events or raw_events[-1].get("type") != "result":
            return normalized(blocker="TERMINAL_RESULT_MISSING")
        terminal = raw_events[-1]
        session_id = terminal.get("session_id")
        if len([e for e in raw_events if e.get("type") == "result"]) != 1 or not isinstance(session_id, str) or not session_id:
            return normalized(blocker="SESSION_PROTOCOL_INVALID", terminal=terminal)
        if terminal.get("subtype") != "success" or terminal.get("is_error") is not False:
            return normalized(blocker="BACKEND_ERROR", session_id=session_id, terminal=terminal)
        try:
            payload = decode_json(terminal["result"])
        except (ValueError, TypeError, KeyError):
            return normalized(blocker="STRUCTURED_OUTPUT_INVALID", session_id=session_id, terminal=terminal)
        return normalized(payload, session_id=session_id, terminal=terminal)
