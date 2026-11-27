"""Codex protocol coverage, not live semantic acceptance."""
from test_runner import TransportFixture


class CodexAdapterTests(TransportFixture):
    def test_codex_uses_clean_read_only_packet_invocation(self):
        result = self.run_backend()
        self.assertEqual(result["result"]["status"], "COMPLETE", result)
        receipt = result["receipt"]
        self.assertEqual(receipt["cwd"], str(self.trusted))
        self.assertEqual(receipt["permission_description"]["os_sandbox"], "read-only")
        self.assertIn('--ignore-user-config', receipt["argv"])
        self.assertEqual(receipt["terminal"]["type"], 'turn.completed')
