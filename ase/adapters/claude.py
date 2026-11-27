"""Claude safe-mode JSON envelope protocol. Live support requires a local probe."""
from . import probe_cli, invocation
from ..protocol import canonical, normalized, packet_prompt


class ClaudeAdapter:
    name = "claude"

    def probe(self, config, *, deadline=None, cancel_requested=None):
        # --safe-mode preserves normal auth; --bare would require API credentials.
        return probe_cli(self.name, config, ["--safe-mode", "--tools", "--strict-mcp-config", "--json-schema", "--output-format"],
                         deadline=deadline, cancel_requested=cancel_requested)

    def build_invocation(self, request, capabilities):
        # Print mode reads the prompt from stdin when no prompt argument is given, so packets
        # beyond the Windows command-line limit reach the backend whole instead of failing.
        call = invocation(capabilities, ["--safe-mode", "--tools", "", "--strict-mcp-config", "--mcp-config",
            '{"mcpServers":{}}', "--no-session-persistence", "--output-format", "json", "--json-schema",
            canonical(request["schema"]).decode("utf-8"), "-p"])
        call["stdin"] = packet_prompt(request)
        return call

    def parse_result(self, raw_events, final_payload):
        if len(raw_events) != 1 or raw_events[0].get("type") != "result":
            return normalized(blocker="TERMINAL_RESULT_MISSING")
        envelope = raw_events[0]
        session_id, denials = envelope.get("session_id"), envelope.get("permission_denials", [])
        if not isinstance(denials, list):
            return normalized(blocker="SESSION_PROTOCOL_INVALID")
        common = {"session_id": session_id, "terminal": envelope, "permission_denials": denials}
        if not isinstance(session_id, str) or not session_id:
            return normalized(blocker="SESSION_PROTOCOL_INVALID", **common)
        if denials: return normalized(blocker="PERMISSION_DENIED", **common)
        if envelope.get("is_error") is not False or envelope.get("subtype") != "success":
            return normalized(blocker="BACKEND_ERROR", **common)
        if "structured_output" not in envelope:
            return normalized(blocker="STRUCTURED_OUTPUT_MISSING", **common)
        return normalized(envelope["structured_output"], **common)
