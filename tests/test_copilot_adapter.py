"""Conservative Copilot envelope fixtures, not a claim of verified live format."""
import os
import subprocess
import unittest

from test_runner import TransportFixture
from ase.adapters import WINDOWS_COMMAND_LINE_BUDGET
from ase.adapters.copilot import CopilotAdapter
from ase.protocol import packet_prompt
from ase.runner import Runner
from ase.runtime_policy import bind_policy


class CopilotAdapterTests(TransportFixture):
    def command_line_length(self, request):
        config = self.config("copilot")
        caps = {"available": True, "argv_prefix": [os.path.abspath(config["executable"]), *config["executable_args"]], "trusted_cwd": ""}
        return len(subprocess.list2cmdline(CopilotAdapter().build_invocation(bind_policy(request), caps)["argv"]))

    def request_with_command_line_length(self, characters):
        base = self.command_line_length(self.request_with_prompt_length(len(packet_prompt(bind_policy(self.request())))))
        request = self.request_with_prompt_length(len(packet_prompt(bind_policy(self.request()))) + characters - base)
        self.assertEqual(self.command_line_length(request), characters)
        return request

    @unittest.skipUnless(os.name == "nt", "Windows command-line limit")
    def test_copilot_prompt_over_command_line_budget_blocks_before_any_artifact(self):
        artifacts = self.base / "artifacts"
        request = self.request_with_command_line_length(WINDOWS_COMMAND_LINE_BUDGET + 1)
        with self.assertRaisesRegex(ValueError, "^PROMPT_ARGUMENT_TOO_LARGE"):
            Runner(self.config("copilot"), artifacts).run(CopilotAdapter(), request, timeout=10)
        self.assert_no_execution_artifacts(artifacts)
        self.assertFalse((self.trusted / "invocations.jsonl").exists())

    @unittest.skipUnless(os.name == "nt", "Windows command-line limit")
    def test_copilot_prompt_at_command_line_budget_proceeds(self):
        request = self.request_with_command_line_length(WINDOWS_COMMAND_LINE_BUDGET)
        outcome = Runner(self.config("copilot"), self.base / "artifacts").run(CopilotAdapter(), request, timeout=10)
        self.assertEqual(outcome["result"]["status"], "COMPLETE", outcome["result"])

    def test_copilot_requires_terminal_result_and_valid_schema(self):
        for case in ("truncated", "unknown", "malformed", "schema"):
            with self.subTest(case=case):
                result = self.run_backend(CopilotAdapter(), "copilot", case)
                self.assertEqual(result["result"]["status"], "BLOCKED", result)

    def test_copilot_final_envelope_is_only_protocol_evidence(self):
        result = self.run_backend(CopilotAdapter(), "copilot")
        self.assertEqual(result["result"]["status"], "COMPLETE", result)
        self.assertEqual(result["receipt"]["evidence_kind"], "protocol_only")

    def test_copilot_live_execution_blocks_without_clean_start_evidence(self):
        result = self.run_backend(CopilotAdapter(), 'copilot', protocol_only=False)
        self.assertEqual(result['result']['status'], 'BLOCKED')
        self.assertEqual(result['result']['blocker'], 'COPILOT_CLEAN_START_UNVERIFIED')
        self.assertIsNone(result['receipt']['pid'])
