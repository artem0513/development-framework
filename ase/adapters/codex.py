"""Codex exec JSONL and output-schema adapter, no MCP coordinator required."""
from . import probe_cli, invocation
from ..protocol import canonical, decode_json, normalized, packet_prompt


class CodexAdapter:
    name = "codex"
    max_prompt_characters = 1048576

    def check_prompt(self, request, argv_prefix):
        if len(packet_prompt(request)) > self.max_prompt_characters:
            raise ValueError('CODEX_INPUT_TOO_LARGE: complete prompt exceeds native character limit')

    def probe(self, config, *, deadline=None, cancel_requested=None):
        caps = probe_cli(self.name, config, ["--ignore-user-config", "--ephemeral", "--sandbox", "--output-schema", "--output-last-message", "--json"],
                         deadline=deadline, cancel_requested=cancel_requested)
        caps["os_sandbox"] = "read-only"
        return caps

    def build_invocation(self, request, capabilities):
        prompt = packet_prompt(request)
        if len(prompt) > self.max_prompt_characters:
            raise ValueError('CODEX_INPUT_TOO_LARGE: complete prompt exceeds native character limit')
        paths = capabilities["artifact_paths"]
        call = invocation(capabilities, ["exec", "--ignore-user-config", "--ephemeral", "--sandbox", "read-only",
            "--skip-git-repo-check", "--cd", capabilities["trusted_cwd"], "--output-schema", paths["schema"],
            "--output-last-message", paths["final"], "--json", "-"], os_sandbox="read-only")
        call['stdin'] = prompt
        return call

    def parse_result(self, raw_events, final_payload):
        if any(not isinstance(e, dict) or not isinstance(e.get("type"), str) or not e["type"] or
               (e["type"].startswith("item.") and (not isinstance(e.get("item"), dict) or
                not isinstance(e["item"].get("type"), str))) for e in raw_events):
            return normalized(blocker="SESSION_PROTOCOL_INVALID")
        if not raw_events or raw_events[-1].get("type") != "turn.completed":
            return normalized(blocker="TERMINAL_RESULT_MISSING")
        starts = [e for e in raw_events if e.get("type") == "thread.started"]
        terminals = [e for e in raw_events if e.get("type") == "turn.completed"]
        if len(starts) != 1 or not isinstance(starts[0].get("thread_id"), str) or not starts[0]["thread_id"] or len(terminals) != 1:
            return normalized(blocker="SESSION_PROTOCOL_INVALID")
        session_id = starts[0]["thread_id"]
        terminal = raw_events[-1]
        if any(e.get("type") in {"error", "turn.failed"} for e in raw_events):
            return normalized(blocker="BACKEND_ERROR", session_id=session_id, terminal=terminal)
        if any(e.get("type", "").startswith("item.") and e.get("item", {}).get("type") not in {"agent_message", "reasoning"} for e in raw_events):
            return normalized(blocker="UNAUTHORIZED_TOOL_EVENT", session_id=session_id, terminal=terminal)
        messages = [e.get("item", {}).get("text") for e in raw_events if e.get("type") == "item.completed" and
                    e.get("item", {}).get("type") == "agent_message"]
        if not messages or final_payload is None:
            return normalized(blocker="STRUCTURED_OUTPUT_MISSING", session_id=session_id, terminal=terminal)
        try:
            message_payload = decode_json(messages[-1])
        except (ValueError, TypeError):
            return normalized(blocker="STRUCTURED_OUTPUT_INVALID", session_id=session_id, terminal=terminal)
        if canonical(message_payload) != canonical(final_payload):
            return normalized(blocker="OUTPUT_MISMATCH", session_id=session_id, terminal=terminal)
        return normalized(final_payload, session_id=session_id, terminal=terminal)
