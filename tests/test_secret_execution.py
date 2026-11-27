"""Secret-aware process transport uses fake canaries and real child processes."""
import base64
import os
from pathlib import Path
import sys
import tempfile
import unittest
from urllib.parse import quote

from ase.core import execute
from ase.runner import capture_process


class SecretExecutionTests(unittest.TestCase):
    def test_raw_encoded_and_error_output_are_redacted_before_log_persistence(self):
        secret = "LocalFixture-Only+Private/Value-193"
        variants = [secret, quote(secret, safe=""), base64.b64encode(secret.encode()).decode()]
        with tempfile.TemporaryDirectory(prefix="ase-secret-process-") as temporary:
            folder = Path(temporary)
            result = execute([sys.executable, "-c", "import os,base64,urllib.parse,sys; s=os.environ['ASE_FIXTURE_SECRET']; print(s); print(urllib.parse.quote(s,safe='')); print(base64.b64encode(s.encode()).decode(),file=sys.stderr)"],
                folder, folder / "command.log", env={**os.environ, "ASE_FIXTURE_SECRET": secret}, sensitive_values=[secret])
            self.assertEqual(result["exit_code"], 0)
            output = (folder / "command.log").read_text(encoding="utf-8")
            self.assertIn("[REDACTED]", output)
            for variant in variants:
                self.assertNotIn(variant, output)
                self.assertNotIn(variant, str(result))
            self.assertNotIn("ASE_FIXTURE_SECRET", os.environ)

    def test_private_capture_accepts_child_only_environment(self):
        secret = "ChildOnly-Fixture-381"
        with tempfile.TemporaryDirectory(prefix="ase-secret-capture-") as temporary:
            result = capture_process([sys.executable, "-c", "import os; print(os.environ['ASE_PRIVATE_CAPTURE'])"],
                temporary, timeout=5, max_output_bytes=4096, env={**os.environ, "ASE_PRIVATE_CAPTURE": secret})
            self.assertEqual(result["exit_code"], 0)
            self.assertEqual(result["stdout"].decode().strip(), secret)
            self.assertNotIn("ASE_PRIVATE_CAPTURE", os.environ)

    def test_authenticated_output_is_bounded_and_cannot_pass_on_output_limit(self):
        with tempfile.TemporaryDirectory(prefix="ase-secret-limit-") as temporary:
            folder = Path(temporary)
            result = execute([sys.executable, "-c", "print('x'*100000)"], folder,
                folder / "command.log", timeout=5, max_output_bytes=4096)
            self.assertTrue(result["output_limited"])
            self.assertEqual(result["error"], "PROCESS_OUTPUT_LIMIT")
            self.assertLessEqual((folder / "command.log").stat().st_size, 4096)


if __name__ == "__main__":
    unittest.main()
