"""Partial recovery credits require original immutable tests and actual execution."""
import copy
import hashlib
from pathlib import Path
import sys
import tempfile
import unittest

from ase.gates import source_snapshot
from ase.state import read_record
from ase.store import Store
from ase.tasks import TaskService
from ase.workflow import WorkflowController


class PartialTestProgress(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="ase-partial-tests-")
        self.addCleanup(temporary.cleanup)
        base = Path(temporary.name)
        self.work = base / "work"
        self.work.mkdir()
        (self.work / "tests").mkdir()
        (self.work / "tests/__init__.py").write_text("")
        (self.work / "tests/support.py").write_text("def expected(number): return number\n")
        self.test_source = ("import unittest\nfrom pathlib import Path\nfrom tests.support import expected\n"
            "class Cases(unittest.TestCase):\n" + "".join(
                " def test_case_" + str(number) + "(self): self.assertGreaterEqual(int(Path('product.txt').read_text()),expected(" + str(number) + "))\n"
                for number in range(1, 7)))
        (self.work / "tests/test_sample.py").write_text(self.test_source)
        (self.work / "pyproject.toml").write_text("[tool.example]\nvalue=1\n")
        (self.work / "product.txt").write_text("0")
        self.store = Store(self.work, base / "state")
        self.addCleanup(self.store.close)
        spec = {"id": "partial", "schema_version": 2, "profile": "document", "request": "Satisfy all six unchanged original cases",
            "scope": {"resources": ["product.txt", "tests", "pyproject.toml"], "effects": ["file_write"]},
            "acceptance": [{"id": "AC1", "expected": "All six original cases pass", "evidence_ids": ["unit"]}],
            "evidence": [{"id": "unit", "kind": "test", "parser": "unittest", "required": True,
                "argv": [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-t", ".", "-v"],
                "progress_test_sources": ["tests", "pyproject.toml"]}],
            "stages": [{"id": "implement", "scope": {"resources": ["product.txt", "tests", "pyproject.toml"], "effects": ["file_write"]},
                "acceptance_ids": ["AC1"], "depends_on": []}], "unknowns": [], "execution_policy": {"recovery_mode": "progress"}}
        self.spec = spec
        self.controller = WorkflowController(self.store)
        self.configure("partial")

    def configure(self, task_id, *, argv=None, sources=None):
        spec = copy.deepcopy(self.spec)
        spec["id"] = task_id
        if argv is not None:
            spec["evidence"][0]["argv"] = [sys.executable, "-m", "unittest", *argv]
        if sources is False:
            spec["evidence"][0].pop("progress_test_sources")
        elif sources is not None:
            spec["evidence"][0]["progress_test_sources"] = sources
        tasks = TaskService(self.store)
        task = tasks.create(spec)
        task = tasks.propose(task_id, {"summary": "Satisfy original tests", "stage_ids": ["implement"]}, expected_revision=task["revision"])
        tasks.approve(task_id, proposal_hash=task["proposal_hash"], user_message_ref="test:authorized", approved_stage_ids=["implement"])
        self.controller._save("baseline", task_id, source_snapshot(self.store, task["contract"], task_id=task_id))
        self.task_id = task_id
        self.observe(0)

    def observe(self, value, *, mutate_log=None, mutate_result=None):
        (self.work / "product.txt").write_text(str(value))
        self.controller.reserve_cycle(self.task_id, "implement")
        checks = self.controller.check(self.task_id, "implement")
        if mutate_log is not None:
            result = checks["results"][0]
            log = Path(result["log"])
            log.write_text(mutate_log(log.read_text()))
            result["log_sha256"] = hashlib.sha256(log.read_bytes()).hexdigest()
        if mutate_result is not None:
            mutate_result(checks["results"][0])
        if mutate_log is not None or mutate_result is not None:
            self.controller._save("checks", self.task_id, checks)
        return self.controller.record_failure(self.task_id, "implement", ["Mandatory AC failed: AC1"])

    def test_six_original_cases_make_partial_progress_beyond_three_total_cycles(self):
        for number in range(1, 6):
            result = self.observe(number)
            self.assertTrue(result["newly_verified"], "A newly passing unchanged original case must count once")
            self.assertEqual(result["stagnation"], 0)
            self.assertFalse(result["diagnostic_required"])
        budget = read_record(self.store.db, "budget", "partial")["payload"]
        self.assertEqual(budget["task_cycles"], 6)
        self.assertEqual(self.controller.check("partial", "implement")["results"][0]["exit_code"], 1)

    def test_changed_test_definition_does_not_earn_partial_credit(self):
        path = self.work / "tests/test_sample.py"
        path.write_text(self.test_source.replace("expected(1)", "expected(0)"))
        self.assertEqual(self.observe(1)["newly_verified"], [])

    def test_changed_helper_does_not_earn_partial_credit(self):
        (self.work / "tests/support.py").write_text("def expected(number): return 0\n")
        self.assertEqual(self.observe(1)["newly_verified"], [])

    def test_changed_configuration_does_not_earn_partial_credit(self):
        (self.work / "pyproject.toml").write_text("[tool.example]\nvalue=2\n")
        self.assertEqual(self.observe(1)["newly_verified"], [])

    def test_new_test_case_does_not_earn_partial_credit(self):
        (self.work / "tests/test_extra.py").write_text("import unittest\nclass Extra(unittest.TestCase):\n def test_new(self): pass\n")
        self.assertEqual(self.observe(1)["newly_verified"], [])

    def test_relabelled_case_does_not_earn_partial_credit(self):
        (self.work / "tests/test_sample.py").write_text(self.test_source.replace("test_case_1", "test_renamed"))
        self.assertEqual(self.observe(1)["newly_verified"], [])

    def test_deleted_case_does_not_earn_partial_credit(self):
        (self.work / "tests/test_sample.py").write_text("\n".join(line for line in self.test_source.splitlines() if "def test_case_1" not in line))
        self.assertEqual(self.observe(1)["newly_verified"], [])

    def test_skip_does_not_earn_partial_credit(self):
        (self.work / "tests/test_sample.py").write_text(self.test_source.replace(" def test_case_1", " @unittest.skip('unverified')\n def test_case_1"))
        self.assertEqual(self.observe(1)["newly_verified"], [])

    def test_unfinished_log_does_not_earn_partial_credit(self):
        self.assertEqual(self.observe(1, mutate_log=lambda value: value.split("Ran ")[0])["newly_verified"], [])

    def test_mismatched_manifest_count_does_not_earn_partial_credit(self):
        self.assertEqual(self.observe(1, mutate_log=lambda value: value.replace("Ran 6 tests", "Ran 7 tests"))["newly_verified"], [])

    def test_malformed_case_log_does_not_earn_partial_credit(self):
        self.assertEqual(self.observe(1, mutate_log=lambda value: value.replace(" ... ok", " ... UNKNOWN"))["newly_verified"], [])

    def test_malformed_terminal_does_not_earn_partial_credit(self):
        self.assertEqual(self.observe(1, mutate_log=lambda value: value.replace("FAILED (failures=5)", "FAILED (failures=5, )"))["newly_verified"], [])

    def test_duplicate_case_manifest_does_not_earn_partial_credit(self):
        def duplicate(value):
            lines = value.splitlines()
            cases = [index for index, line in enumerate(lines) if " ... " in line]
            lines[cases[1]] = lines[cases[0]]
            return "\n".join(lines) + "\n"
        self.assertEqual(self.observe(1, mutate_log=duplicate)["newly_verified"], [])

    def test_missing_baseline_does_not_earn_partial_credit(self):
        self.controller._save("baseline", "partial", None)
        self.assertEqual(self.observe(1)["newly_verified"], [])

    def test_changed_manifest_case_ids_do_not_earn_partial_credit(self):
        self.assertEqual(self.observe(1, mutate_log=lambda value: value.replace("test_case_1", "test_unknown"))["newly_verified"], [])

    def test_resume_keeps_original_case_high_water(self):
        self.assertTrue(self.observe(1)["newly_verified"])
        self.controller = WorkflowController(self.store)
        self.assertEqual(self.observe(1)["newly_verified"], [])

    def test_default_discovery_top_level_resolves_original_cases(self):
        self.configure("default-top", argv=["discover", "-s", "tests", "-v"])
        self.assertTrue(self.observe(1)["newly_verified"])

    def test_long_discovery_flags_resolve_original_cases(self):
        self.configure("long-flags", argv=["discover", "--start-directory=tests", "--top-level-directory=tests", "--verbose"])
        self.assertTrue(self.observe(1)["newly_verified"])

    def test_separate_long_discovery_flags_resolve_original_cases(self):
        self.configure("split-long-flags", argv=["discover", "--start-directory", "tests", "--top-level-directory", "tests", "--verbose"])
        self.assertTrue(self.observe(1)["newly_verified"])

    def test_unknown_immutable_test_boundary_does_not_earn_partial_credit(self):
        self.configure("unknown-boundary", sources=False)
        self.assertEqual(self.observe(1)["newly_verified"], [])

    def test_wrong_command_receipt_does_not_earn_partial_credit(self):
        self.assertEqual(self.observe(1, mutate_result=lambda result: result.update(configured_argv=["different"]))["newly_verified"], [])

    def test_wrong_log_hash_does_not_earn_partial_credit(self):
        self.assertEqual(self.observe(1, mutate_result=lambda result: result.update(log_sha256="0" * 64))["newly_verified"], [])

    def test_wrong_working_directory_does_not_earn_partial_credit(self):
        self.assertEqual(self.observe(1, mutate_result=lambda result: result.update(cwd=str(self.store.state_home)))["newly_verified"], [])

    def test_cancelled_command_does_not_earn_partial_credit(self):
        self.assertEqual(self.observe(1, mutate_result=lambda result: result.update(cancelled=True))["newly_verified"], [])

    def test_verified_error_elimination_earns_partial_credit(self):
        path = self.work / "tests/test_sample.py"
        path.write_text(self.test_source.replace("self.assertGreaterEqual(int(Path('product.txt').read_text()),expected(1))",
            "self.assertTrue(1 / int(Path('product.txt').read_text()))"))
        self.configure("initial-error")
        self.assertTrue(self.observe(1)["newly_verified"])

    def test_ambiguous_case_module_mapping_does_not_earn_partial_credit(self):
        (self.work / "tests.py").write_text("# Ambiguous module versus package name\n")
        self.spec["scope"]["resources"].append("tests.py")
        self.spec["stages"][0]["scope"]["resources"].append("tests.py")
        self.configure("ambiguous-modules", sources=["tests", "tests.py", "pyproject.toml"])
        self.assertEqual(self.observe(1)["newly_verified"], [])

    def test_repeated_pass_after_regression_is_not_new_progress(self):
        self.assertTrue(self.observe(1)["newly_verified"])
        self.assertEqual(self.observe(0)["newly_verified"], [])
        self.assertEqual(self.observe(1)["newly_verified"], [])


if __name__ == "__main__":
    unittest.main()
