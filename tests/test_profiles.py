"""Domain proof comes from actual logs/artifacts, not model PASS text."""
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

from ase.core import execute
from ase.profiles import evaluate_profile


class ProfileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ase-profile-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def command(self, output, exit_code=0):
        argv = [sys.executable, "-c", "import sys; print(" + repr(output) + "); sys.exit(" + str(exit_code) + ")"]
        receipt = execute(argv, self.root, self.root / "command.log")
        receipt.update(id="probe", kind="command", configured_argv=argv, parser=None)
        return {"id": "probe", "kind": "command", "argv": argv, "required": True}, receipt

    def test_unknown_scope_effect_lists_allowed_values(self):
        from ase.profiles import validate_scope_effects
        self.assertEqual(validate_scope_effects(["file_write", "command"]), ["file_write", "command"])
        self.assertEqual(validate_scope_effects(["read_only"]), ["read_only"])
        self.assertEqual(validate_scope_effects([]), [])
        with self.assertRaisesRegex(ValueError, r"Unknown scope effect 'write'; allowed: command, file_write, read_only"):
            validate_scope_effects(["file_write", "write"])

    def test_software_requires_positive_executed_tests(self):
        spec, receipt = self.command("Ran 0 tests in 0.001s\nOK")
        spec.update(kind="test", parser="unittest")
        receipt.update(kind="test", parser="unittest")
        result = evaluate_profile("software", [spec], [receipt])
        self.assertEqual(result["status"], "BLOCKED")
        self.assertTrue(any("test" in reason.lower() for reason in result["blocked_reasons"]))

    def test_skipped_required_tests_and_altered_logs_cannot_pass(self):
        spec, receipt = self.command("Ran 2 tests in 0.001s\nOK (skipped=1)")
        spec.update(kind="test", parser="unittest")
        receipt.update(kind="test", parser="unittest")
        self.assertEqual(evaluate_profile("software", [spec], [receipt])["status"], "REJECTED")
        Path(receipt["log"]).write_text("Ran 2 tests in 0.1s\nOK")
        self.assertEqual(evaluate_profile("software", [spec], [receipt])["status"], "REJECTED")

    def test_infrastructure_requires_actual_probe_predicate(self):
        spec, receipt = self.command(json.dumps({"running": False, "resource_id": "vm-1"}))
        spec.update(kind="probe", predicate={"path": "running", "op": "eq", "value": True})
        receipt["kind"] = "probe"
        result = evaluate_profile("infrastructure", [spec], [receipt])
        self.assertEqual(result["status"], "REJECTED")
        receipt["status"] = "PASS"
        self.assertEqual(evaluate_profile("infrastructure", [spec], [receipt])["status"], "REJECTED")

    def test_artifact_change_invalidates_document_proof(self):
        path = self.root / "guide.md"
        path.write_text("Confirmed guide", encoding="utf-8")
        receipt = {"id": "guide", "kind": "artifact", "path": str(path),
                   "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
        spec = {"id": "guide", "kind": "artifact", "path": "guide.md", "required": True}
        self.assertEqual(evaluate_profile("document", [spec], [receipt])["status"], "PASS")
        path.write_text("Changed guide", encoding="utf-8")
        self.assertEqual(evaluate_profile("document", [spec], [receipt])["status"], "REJECTED")

    def test_unknown_evaluator_and_absent_evidence_block(self):
        for spec in [{"id": "x", "kind": "mystery"}, {"id": "x", "kind": "probe"}]:
            self.assertEqual(evaluate_profile("research", [spec], [])["status"], "BLOCKED")

    def test_failed_optional_command_is_not_accepted_as_ac_evidence(self):
        spec, receipt = self.command("failure", 1)
        spec["required"] = False
        result = evaluate_profile("infrastructure", [spec], [receipt])
        self.assertEqual(result["by_id"]["probe"]["status"], "FAIL")


if __name__ == "__main__":
    unittest.main()
