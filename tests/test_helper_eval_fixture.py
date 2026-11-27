import contextlib
import importlib
import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
EVAL = ROOT / "scripts" / "eval"

GOOD_DIFF = """```diff
--- a/app/ledger/values.py
+++ b/app/ledger/values.py
@@ -4,1 +4,3 @@
 ACCOUNT_DIGITS = 10
+# Entry keys are 99999999 minus yyyymmdd, so a key scan returns the newest entries first.
+INVERTED_DATE_BASE = 99999999
@@ -20,0 +23,7 @@
+    @staticmethod
+    def entry_key(day):
+        return str(INVERTED_DATE_BASE - int(day.strftime("%Y%m%d"))).zfill(8)
+
+    @staticmethod
+    def date_from_entry_key(key):
+        return LedgerValues.parse_date(str(INVERTED_DATE_BASE - int(key)).zfill(8))
--- a/app/handlers/entries.py
+++ b/app/handlers/entries.py
@@ -12,0 +13,3 @@
+def list_recent_entries(request):
+    newest = LedgerValues.entry_key(date.fromisoformat(request.params["since"]))
+    return Response(200, [row for row in client.scan("entries") if row["key"] <= newest])
```
Decision: the inverted-date key had no helper; added `LedgerValues.entry_key`/`date_from_entry_key` with a
named constant next to the other ledger conversions.
"""

INLINE_DIFF = """```diff
--- a/app/handlers/entries.py
+++ b/app/handlers/entries.py
@@ -12,0 +13,3 @@
+def list_recent_entries(request):
+    newest = 99999999 - int(request.params["since"].replace("-", ""))
+    return Response(200, [row for row in client.scan("entries") if int(row["key"]) <= newest])
```
"""


class HelperFixtureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, str(EVAL))
        cls.helpers = importlib.import_module("systems_helpers")
        cls.harness = importlib.import_module("harness")
        cls.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        cls.systems = Path(cls.tmp.name) / "systems"
        with mock.patch.object(cls.harness, "SYSTEMS", cls.systems), contextlib.redirect_stdout(io.StringIO()):
            cls.harness.build(["ledger"])
        cls.env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()
        sys.path.remove(str(EVAL))

    def run_in(self, repo, *args):
        return subprocess.run(args, cwd=repo, env=self.env, capture_output=True, text=True, timeout=120)

    def test_fixture_tests_pass_and_no_helper_exists_for_the_inverted_date(self):
        repo = self.systems / "ledger"
        result = self.run_in(repo, sys.executable, "-B", "-m", "unittest", "discover", "-s", "tests")
        self.assertEqual(result.returncode, 0, result.stderr)
        values = (repo / "app/ledger/values.py").read_text(encoding="utf-8")
        self.assertIn("class LedgerValues", values)
        self.assertNotIn("99999999", values)
        self.assertIn("99999999 - yyyymmdd", (repo / "docs/ledger.md").read_text(encoding="utf-8"))

    def test_pull_request_applies_and_its_tests_pass(self):
        repo = Path(self.tmp.name) / "pr"
        subprocess.run(["git", "clone", "-q", str(self.systems / "ledger"), str(repo)], check=True, capture_output=True)
        (repo / "change.diff").write_text(self.helpers.PR_DIFF, encoding="utf-8", newline="\n")
        applied = self.run_in(repo, "git", "apply", "change.diff")
        self.assertEqual(applied.returncode, 0, applied.stderr)
        result = self.run_in(repo, sys.executable, "-B", "-m", "unittest", "discover", "-s", "tests")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.helpers.PR_DIFF.count("99999999"), 2)

    def test_tickets_do_not_hint_at_the_helper(self):
        for leak in ("LedgerValues", "values.py", "helper", "99999999", "invert"):
            self.assertNotIn(leak, self.helpers.T_RECENT)

    def test_harness_registers_the_scenarios(self):
        self.assertEqual({sid: self.harness.SCENARIOS[sid] for sid in ("L1", "L1Q", "L1R")},
                         {"L1": "ledger", "L1Q": "ledger", "L1R": "ledger"})
        self.assertEqual(self.harness.EXTRA_FILES["L1Q"], {"pr/LED-41.diff": self.helpers.PR_DIFF})

    def test_grade_answer(self):
        grade = self.helpers.grade_answer
        self.assertEqual(grade(GOOD_DIFF, "L1"), {"verdict": "CANDIDATE_GOOD", "reasons": []})
        self.assertEqual(grade(INLINE_DIFF, "L1")["verdict"], "BAD")
        self.assertEqual(grade(GOOD_DIFF + INLINE_DIFF, "L1")["verdict"], "BAD")
        self.assertEqual(grade("Approve. Clean change, tests cover both ACs.", "L1R")["verdict"], "BAD")
        review = "Request changes. P2: the inverted-date conversion is inlined twice; add it to LedgerValues with a named constant."
        self.assertEqual(grade(review, "L1R")["verdict"], "CANDIDATE_GOOD")
        self.assertEqual(grade("It is an inverted date. I need to check whether we already have a helper for it.", "L1Q")["verdict"], "BAD")
        answer = ("It is the ledger's inverted date (99999999 - yyyymmdd, newest first). LedgerValues in "
                  "app/ledger/values.py has no method for it; I propose LedgerValues.entry_key with a named constant.")
        self.assertEqual(grade(answer, "L1Q")["verdict"], "CANDIDATE_GOOD")

    def test_grader_edge_cases(self):
        grade = self.helpers.grade_answer
        underscored = INLINE_DIFF.replace("99999999", "99_999_999")
        self.assertEqual(grade(underscored, "L1")["verdict"], "BAD")
        constant_in_handler = INLINE_DIFF.replace(
            "@@ -12,0 +13,3 @@\n", "@@ -12,0 +13,4 @@\n+NEWEST = 99999999\n").replace("99999999 - int", "NEWEST - int")
        result = grade(constant_in_handler, "L1")
        self.assertEqual(result["verdict"], "GRADER")
        self.assertIn("named constant for 99999999 outside LedgerValues", result["reasons"])
        test_only = GOOD_DIFF.replace("```\nDecision", "--- a/tests/test_recent.py\n+++ b/tests/test_recent.py\n"
                                      "@@ -0,0 +1,1 @@\n+EXPECTED = str(99999999 - 20261101)\n```\nDecision")
        self.assertEqual(grade(test_only, "L1")["verdict"], "CANDIDATE_GOOD")
        self.assertEqual(grade("Approve. Reuses LedgerValues.parse_amount; P3 nit: rename result.", "L1R")["verdict"], "BAD")
        nit = grade("Approve. P3: the inverted-date literal could move to LedgerValues.", "L1R")
        self.assertEqual(nit["verdict"], "GRADER")
        self.assertEqual(grade("It is an inverted date. I'm not sure, I need to check whether a helper exists in LedgerValues.",
                               "L1Q")["verdict"], "BAD")


if __name__ == "__main__":
    unittest.main()
