"""Bun summaries must prove executed tests without hiding failures or skips."""
import unittest

from ase.core import test_counts


def bun_summary(passed, failed=0, skipped=0, todo=0):
    counts = [f" {passed} pass"]
    if skipped:
        counts.append(f" {skipped} skip")
    if todo:
        counts.append(f" {todo} todo")
    counts.extend([f" {failed} fail", " 12 expect() calls"])
    total = passed + failed + skipped + todo
    return "bun test v1.3.10 (abc123)\n\ntest/example.test.ts:\n(pass) example\n\n" + "\n".join(counts) + f"\nRan {total} tests across 2 files. [12.00ms]\n"


class BunCountsTests(unittest.TestCase):
    def test_unconfigured_parser_recognizes_complete_bun_summary(self):
        self.assertEqual(test_counts(bun_summary(437), None),
                         {"total": 437, "skipped": 0, "failed": 0})

    def test_explicit_node_parser_does_not_autodetect_bun(self):
        self.assertEqual(test_counts(bun_summary(7), "node"),
                         {"total": 0, "skipped": 0, "failed": 0})

    def test_explicit_node_parser_keeps_failures_in_mixed_output(self):
        output = "Tests: 1 failed, 2 passed, 3 skipped\n" + bun_summary(7)
        self.assertEqual(test_counts(output, "node"),
                         {"total": 2, "skipped": 3, "failed": 1})

    def test_autodetection_rejects_mixed_runner_summaries(self):
        summaries = {
            "jest_failure": "Tests: 1 failed, 2 passed\n",
            "vitest_skip": "Tests 1 skipped\n",
            "jasmine_failure": "TOTAL: 1 FAILED\n",
            "node_failure": "# tests 2\n# fail 1\n",
            "unittest_failure": "Ran 2 tests in 0.01s\nFAILED (failures=1)\n",
            "pytest_skip": "=== 2 passed, 1 skipped in 0.01s ===\n",
            "pytest_quiet_failure": "1 failed, 2 passed in 0.31s\n",
            "pytest_quiet_skip": "2 passed, 1 skipped in 0.31s\n",
            "dotnet_failure": "Failed! - Failed: 1, Passed: 2, Total: 3\n",
        }
        for name, summary in summaries.items():
            with self.subTest(case=name):
                self.assertEqual(test_counts(summary + bun_summary(7), None),
                                 {"total": 0, "skipped": 0, "failed": 0})

    def test_multiple_bun_suites_are_summed(self):
        output = "$ bun run frontend && bun run backend && bun run scripts\n"
        output += bun_summary(474) + bun_summary(176) + bun_summary(261)
        self.assertEqual(test_counts(output, None),
                         {"total": 911, "skipped": 0, "failed": 0})

    def test_zero_count_foreign_summaries_block_bun_autodetection(self):
        summaries = {
            "node": "# tests 0\n# fail 0\n",
            "unittest": "Ran 0 tests in 0.000s\nOK\n",
            "pytest": "=== no tests ran in 0.01s ===\n",
            "pytest_quiet": "no tests ran in 0.01s\n",
            "pytest_counters": "0 passed, 0 failed, 0 skipped in 0.01s\n",
            "dotnet": "Passed! - Failed: 0, Passed: 0, Skipped: 0, Total: 0\n",
            "jest": "Tests: 0 total\n",
            "vitest": "Tests 0 passed (0)\n",
            "jasmine": "TOTAL: 0 SUCCESS\n",
        }
        for runner, summary in summaries.items():
            for placement, output in {
                "before": summary + bun_summary(4),
                "after": bun_summary(4) + summary,
                "between": bun_summary(4) + summary + bun_summary(3),
            }.items():
                with self.subTest(runner=runner, placement=placement):
                    self.assertEqual(test_counts(output, None),
                                     {"total": 0, "skipped": 0, "failed": 0})

    def test_foreign_expected_failure_and_empty_run_markers_are_rejected(self):
        for summary in ("1 xfailed in 0.01s\n", "1 xpassed in 0.01s\n",
                        "collected 0 items\n", "no tests collected in 0.01s\n",
                        "No tests found, exiting with code 0\n",
                        "No test files found, exiting with code 0\n",
                        "No test is available in empty.dll\n"):
            for output in (summary + bun_summary(4), bun_summary(4) + summary,
                           bun_summary(4) + summary + bun_summary(3)):
                with self.subTest(summary=summary, output=output):
                    self.assertEqual(test_counts(output, None),
                                     {"total": 0, "skipped": 0, "failed": 0})

    def test_truncated_bun_headers_cannot_hide_an_incomplete_run(self):
        for header in ("bun test v", "bun test", "bun test ", " bun test v",
                       "bun\ttest v", "\x1b[31mbun test v\x1b[0m"):
            for output in (header + "\n" + bun_summary(4),
                           bun_summary(4) + header,
                           bun_summary(4) + header + "\n" + bun_summary(3)):
                with self.subTest(header=header, output=output):
                    self.assertEqual(test_counts(output, None),
                                     {"total": 0, "skipped": 0, "failed": 0})

    def test_ansi_sequences_do_not_obscure_failure(self):
        output = bun_summary(2, failed=1).replace("1 fail", "\x1b[31m1 fail\x1b[0m")
        self.assertEqual(test_counts(output, None),
                         {"total": 3, "skipped": 0, "failed": 1})

    def test_skipped_and_todo_tests_are_not_executed(self):
        self.assertEqual(test_counts(bun_summary(2, skipped=3, todo=4), None),
                         {"total": 2, "skipped": 7, "failed": 0})

    def test_skipped_only_suite_has_zero_executed_tests(self):
        self.assertEqual(test_counts(bun_summary(0, skipped=3), None),
                         {"total": 0, "skipped": 3, "failed": 0})

    def test_empty_suite_does_not_prove_executed_tests(self):
        self.assertEqual(test_counts(bun_summary(0), None),
                         {"total": 0, "skipped": 0, "failed": 0})

    def test_failure_in_later_suite_is_not_hidden(self):
        self.assertEqual(test_counts(bun_summary(4) + bun_summary(1, failed=2), None),
                         {"total": 7, "skipped": 0, "failed": 2})

    def test_singular_footer_and_windows_newlines(self):
        output = bun_summary(1).replace("1 tests across 2 files", "1 test across 1 file")
        self.assertEqual(test_counts(output.replace("\n", "\r\n"), None),
                         {"total": 1, "skipped": 0, "failed": 0})

    def test_expect_counter_is_optional(self):
        self.assertEqual(test_counts(bun_summary(1).replace(" 12 expect() calls\n", ""), None),
                         {"total": 1, "skipped": 0, "failed": 0})

    def test_unframed_counts_are_not_bun_evidence(self):
        output = bun_summary(40).split("\n", 1)[1]
        self.assertEqual(test_counts(output, None),
                         {"total": 0, "skipped": 0, "failed": 0})

    def test_inconsistent_footer_is_not_accepted(self):
        output = bun_summary(4).replace("Ran 4 tests", "Ran 99 tests")
        self.assertEqual(test_counts(output, None),
                         {"total": 0, "skipped": 0, "failed": 0})

    def test_incomplete_later_suite_blocks_combined_count(self):
        output = bun_summary(4) + "bun test v1.3.10 (abc123)\n 5 pass\n"
        self.assertEqual(test_counts(output, None),
                         {"total": 0, "skipped": 0, "failed": 0})

    def test_duplicate_summary_counters_are_not_accepted(self):
        output = bun_summary(4).replace(" 4 pass", " 2 pass\n 2 pass")
        self.assertEqual(test_counts(output, None),
                         {"total": 0, "skipped": 0, "failed": 0})

    def test_missing_failure_counter_is_not_accepted(self):
        self.assertEqual(test_counts(bun_summary(4).replace(" 0 fail\n", ""), None),
                         {"total": 0, "skipped": 0, "failed": 0})

    def test_unmatched_counters_before_complete_summary_are_rejected(self):
        for counter in ("1 fail", "3 skip", "2 todo", "9 pass"):
            with self.subTest(counter=counter):
                output = bun_summary(4).replace("test/example.test.ts:",
                                               f" {counter}\n\ntest/example.test.ts:")
                self.assertEqual(test_counts(output, None),
                                 {"total": 0, "skipped": 0, "failed": 0})

    def test_truncated_summary_before_first_header_is_rejected(self):
        for prefix in (" 1 fail\nRan 1 test across 1 file. [1ms]\n",
                       " 3 skip\n", " 2 todo\n", " 9 pass\n",
                       " 7 expect() calls\n", "Ran 9 tests across 1 file.\n"):
            with self.subTest(prefix=prefix):
                self.assertEqual(test_counts(prefix + bun_summary(4), None),
                                 {"total": 0, "skipped": 0, "failed": 0})

    def test_unmatched_summary_lines_after_complete_summary_are_rejected(self):
        for line in ("1 fail", "3 skip", "2 todo", "9 pass",
                     "7 expect() calls", "Ran 99 tests across 1 file."):
            with self.subTest(line=line):
                self.assertEqual(test_counts(bun_summary(4) + f"\n {line}\n", None),
                                 {"total": 0, "skipped": 0, "failed": 0})

    def test_other_explicit_parsers_keep_their_own_format(self):
        self.assertEqual(test_counts(bun_summary(4), "unittest"),
                         {"total": 0, "skipped": 0, "failed": 0})
        self.assertEqual(test_counts("# tests 5\n# fail 1\n# skipped 2\n", "node"),
                         {"total": 5, "skipped": 2, "failed": 1})


if __name__ == "__main__":
    unittest.main()
