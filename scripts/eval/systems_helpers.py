"""Helper fixture: a domain conversion with a magic number where the repository has a helper class for that domain.

One Git repository, `ledger`: an internal entries service reading an external accounting system ("the ledger").
`app/ledger/values.py` already holds `LedgerValues`, the conversions of ledger values (amounts with a trailing
sign, zero-padded account numbers, yyyymmdd dates). The ledger keys entries by an inverted date
(99999999 - yyyymmdd, newest first) and entry rows carry no other date; no helper exists for that key yet,
only docs/ledger.md describes it.

* L1  - ticket that needs the inverted-date key both ways. GOOD adds a method and a named constant to
        LedgerValues and uses them; BAD inlines 99999999 in production code outside LedgerValues.
* L1R - review of a PR that inlines the conversion and its inverse (results are correct; everything else
        already uses LedgerValues). GOOD names it as a blocking finding with the fix in LedgerValues; BAD
        approves without pointing the conversion to LedgerValues.
* L1Q - the owner asks what the number in that code is and whether there is a helper. GOOD answers with
        the fact (LedgerValues has no such method; proposes adding it); BAD only offers to check.
grade_answer decides BAD deterministically; everything else is CANDIDATE_GOOD or GRADER for the rubric
grader (scripts/eval/rubric.md, Round 6).
"""
import re
from types import SimpleNamespace

from systems_scope import _diff

README = """# Entries service

Internal HTTP service over the company ledger (an external accounting system). Plain Python 3.11.

* `app/ledger/client.py` - read access to ledger tables.
* `app/ledger/values.py` - `LedgerValues`: conversions of ledger values. See docs/ledger.md.
* `app/handlers/` - request handlers, routed by `app/router.py`.

Run the tests: `python -m unittest discover -s tests`
"""

LEDGER_DOC = """# Ledger conventions

The ledger stores values in its own encodings. Convert them with `LedgerValues` (app/ledger/values.py).

* Amounts are integer cents with a trailing sign: `1250-` is -12.50, `300+` is 3.00.
* Account numbers are zero-padded to 10 digits.
* Dates are `yyyymmdd` strings.
* The `entries` table is keyed by an inverted posting date, `99999999 - yyyymmdd` as an 8-digit string,
  so a key scan returns the newest entries first. Several entries share a key (one per day).
"""

VALUES = '''"""Conversions of ledger values; see docs/ledger.md."""
from datetime import date
from decimal import Decimal

ACCOUNT_DIGITS = 10


class LedgerValues:
    @staticmethod
    def parse_amount(raw):
        # Integer cents with a trailing sign.
        sign = -1 if raw.endswith("-") else 1
        return sign * Decimal(raw.rstrip("+-")).scaleb(-2)

    @staticmethod
    def format_account(number):
        return str(number).zfill(ACCOUNT_DIGITS)

    @staticmethod
    def parse_date(raw):
        return date(int(raw[:4]), int(raw[4:6]), int(raw[6:8]))
'''

CLIENT = '''"""In-memory stand-in for the ledger's table API: rows are dicts with a "key", scanned in key order."""
TABLES = {"entries": []}


def scan(table, start_key="", limit=100):
    rows = sorted((row for row in TABLES[table] if row["key"] >= start_key), key=lambda row: row["key"])
    return rows[:limit]
'''

HTTP = """from dataclasses import dataclass, field


@dataclass
class Request:
    method: str
    path: str
    params: dict = field(default_factory=dict)


@dataclass
class Response:
    status: int
    body: object = None
"""

ENTRIES = '''from app.http import Response
from app.ledger import client
from app.ledger.values import LedgerValues


def list_account_entries(request):
    account = LedgerValues.format_account(int(request.params["account"]))
    rows = [row for row in client.scan("entries", limit=1000) if row["account"] == account]
    return Response(200, [{"account": row["account"], "amount": str(LedgerValues.parse_amount(row["amount"]))}
                          for row in rows])
'''

ROUTER = """from app.handlers.entries import list_account_entries
from app.http import Response

ROUTES = {("GET", "/entries"): list_account_entries}


def handle(request):
    handler = ROUTES.get((request.method, request.path))
    return handler(request) if handler else Response(404, {"error": "no route"})
"""

TEST_ENTRIES = '''import unittest

from app import router
from app.http import Request
from app.ledger import client


class EntriesTests(unittest.TestCase):
    def setUp(self):
        client.TABLES["entries"] = [
            {"key": "79739878", "account": "0000004711", "amount": "1250-"},
            {"key": "79738888", "account": "0000004711", "amount": "300+"},
        ]

    def test_entries_of_an_account(self):
        response = router.handle(Request("GET", "/entries", {"account": "4711"}))
        self.assertEqual(response.status, 200)
        self.assertEqual([row["amount"] for row in response.body], ["3.00", "-12.50"])
'''

TEST_VALUES = '''import unittest
from datetime import date
from decimal import Decimal

from app.ledger.values import LedgerValues


class LedgerValuesTests(unittest.TestCase):
    def test_amount_with_trailing_sign(self):
        self.assertEqual(LedgerValues.parse_amount("1250-"), Decimal("-12.50"))

    def test_account_is_zero_padded(self):
        self.assertEqual(LedgerValues.format_account(4711), "0000004711")

    def test_date(self):
        self.assertEqual(LedgerValues.parse_date("20261111"), date(2026, 11, 11))
'''

A, B = "Lena Brandt", "Oskar Lind"


def _history():
    return [
        ("Entries service: ledger client, account entries endpoint", A, "2026-06-02T09:00:00",
         {"README.md": README, "docs/ledger.md": LEDGER_DOC, "app/__init__.py": "", "app/http.py": HTTP,
          "app/ledger/__init__.py": "", "app/ledger/client.py": CLIENT, "app/ledger/values.py": VALUES,
          "app/handlers/__init__.py": "", "app/handlers/entries.py": ENTRIES, "app/router.py": ROUTER,
          "tests/test_entries.py": TEST_ENTRIES, "tests/test_values.py": TEST_VALUES}),
    ]


T_RECENT = """# LED-41 Recent entries endpoint

Component: entries · Reporter: finance operations

Add `GET /entries/recent?since=YYYY-MM-DD`: all ledger entries posted on or after `since`, newest first,
each with account, amount and the posting date as ISO date.

AC1: entries posted on or after `since` are returned, newest first.
AC2: entries posted before `since` are not returned.
"""

PR_DIFF = '''--- a/app/handlers/entries.py
+++ b/app/handlers/entries.py
@@ -1,3 +1,5 @@
+from datetime import datetime
+
 from app.http import Response
 from app.ledger import client
 from app.ledger.values import LedgerValues
@@ -8,3 +10,16 @@ def list_account_entries(request):
     rows = [row for row in client.scan("entries", limit=1000) if row["account"] == account]
     return Response(200, [{"account": row["account"], "amount": str(LedgerValues.parse_amount(row["amount"]))}
                           for row in rows])
+
+
+def list_recent_entries(request):
+    since = datetime.strptime(request.params["since"], "%Y-%m-%d").date()
+    newest_key = 99999999 - int(since.strftime("%Y%m%d"))
+    result = []
+    for row in client.scan("entries", limit=1000):
+        if int(row["key"]) > newest_key:
+            break
+        posted = LedgerValues.parse_date(str(99999999 - int(row["key"])).zfill(8))
+        result.append({"account": row["account"], "amount": str(LedgerValues.parse_amount(row["amount"])),
+                       "posted": posted.isoformat()})
+    return Response(200, result)
--- a/app/router.py
+++ b/app/router.py
@@ -1,7 +1,7 @@
-from app.handlers.entries import list_account_entries
+from app.handlers.entries import list_account_entries, list_recent_entries
 from app.http import Response
 
-ROUTES = {("GET", "/entries"): list_account_entries}
+ROUTES = {("GET", "/entries"): list_account_entries, ("GET", "/entries/recent"): list_recent_entries}
 
 
 def handle(request):
--- /dev/null
+++ b/tests/test_recent.py
@@ -0,0 +1,18 @@
+import unittest
+
+from app import router
+from app.http import Request
+from app.ledger import client
+
+
+class RecentTests(unittest.TestCase):
+    def setUp(self):
+        client.TABLES["entries"] = [
+            {"key": "79739878", "account": "0000004711", "amount": "1250-"},
+            {"key": "79738888", "account": "0000004711", "amount": "300+"},
+        ]
+
+    def test_recent_entries_newest_first(self):
+        response = router.handle(Request("GET", "/entries/recent", {"since": "2026-11-01"}))
+        self.assertEqual(response.status, 200)
+        self.assertEqual([row["posted"] for row in response.body], ["2026-11-11"])
+        self.assertEqual(len(router.handle(Request("GET", "/entries/recent", {"since": "2026-01-01"})).body), 2)
'''

QUESTION = """# Question from the owner on the LED-41 pull request (pr/LED-41.diff)

In `list_recent_entries` I see `99999999 - int(since.strftime("%Y%m%d"))` and the same number again a few
lines below. What is this, and don't we have a helper for it?
"""

PR_PATH = "pr/LED-41.diff"

VARIANTS = {
    "ledger": SimpleNamespace(TICKETS={"L1": T_RECENT, "L1Q": QUESTION}, history=_history, TITLE="Entries",
                              HELPER="app/ledger/values.py", CLASS="LedgerValues", MAGIC="99999999"),
}
# Review scenario: (pull request path, its diff, ticket id); the PR file is added to the repository.
REVIEWS = {"L1R": (PR_PATH, PR_DIFF, "L1")}
# The question scenario reads the PR from the repository as well.
EXTRA_FILES = {"L1Q": {PR_PATH: PR_DIFF}}

# ---------------------------------------------------------------- grading
_DEFER = re.compile(r"\b(?:need(?:s)? to|should|will|I'll|let me|we (?:could|can|should))\s+(?:check|look|search|verify|see)\b"
                    r"[^.\n]{0,60}\b(?:helper|exists?|already)", re.I)
# A stated result of the search: "LedgerValues has no ... method", "there is no helper for ...".
_NO_HELPER = re.compile(r"(?:LedgerValues|values\.py|helper)[^.\n]{0,80}?\b(?:has no|doesn't have|does not have|lacks|"
                        r"contains no|has nothing)\b|\b(?:there is|there's|we have) no\b[^.\n]{0,40}\b(?:helper|method)\b"
                        r"|\bno (?:existing )?(?:helper|method)\b[^.\n]{0,60}(?:LedgerValues|values\.py|exists)", re.I)
_CONSTANT = re.compile(r"^\s*[A-Z][A-Z0-9_]*\s*(?::[^=]+)?=\s*99999999\b")
_NAMES_CONVERSION = re.compile(r"99999999|invert|magic", re.I)
_BLOCKING = re.compile(r"request(?:ing)?\s+changes|changes requested|\bP[0-2]\b", re.I)


def grade_answer(text, scenario):
    """BAD is final; otherwise CANDIDATE_GOOD (no notes) or GRADER (notes)."""
    spec = VARIANTS["ledger"]
    bad, notes = [], []
    if scenario == "L1":
        _, changed = _diff(text)
        changed = [(path, line.replace("_", "") if spec.MAGIC in line.replace("_", "") else line) for path, line in changed]
        production = [(path, line) for path, line in changed
                      if path.startswith("app/") and path.endswith(".py") and path != spec.HELPER
                      and spec.MAGIC in line and not line.lstrip().startswith(("#", '"', "'"))]
        inline = sorted({path for path, line in production if not _CONSTANT.match(line)})
        if inline:
            bad.append("inlines the inverted-date conversion outside " + spec.HELPER + ": " + ", ".join(inline))
        elif production:
            notes.append("named constant for " + spec.MAGIC + " outside " + spec.CLASS)
        helper_lines = [line for path, line in changed if path == spec.HELPER]
        if not any(_CONSTANT.match(line) for line in helper_lines):
            notes.append("no named constant for " + spec.MAGIC + " in " + spec.HELPER)
        if not any(line.lstrip().startswith("def ") for line in helper_lines):
            notes.append("no new " + spec.CLASS + " method")
        if not changed:
            notes.append("no diff")
    else:
        names_helper = spec.CLASS in text or spec.HELPER in text or "values.py" in text
        if not (names_helper and _NAMES_CONVERSION.search(text)):
            bad.append("does not point the inverted-date conversion to " + spec.CLASS)
        if scenario == "L1R" and not _BLOCKING.search(text):
            notes.append("no blocking verdict or P0-P2 severity")
        if scenario == "L1Q":
            if _DEFER.search(text) and not _NO_HELPER.search(text):
                bad.append("offers to check for a helper instead of answering with the fact")
            elif not _NO_HELPER.search(text):
                notes.append("does not state whether a helper exists")
    verdict = "BAD" if bad else "GRADER" if notes else "CANDIDATE_GOOD"
    return {"verdict": verdict, "reasons": bad or notes}
