"""v2 protected-file authorization: exact paths, bound to the current source and contract.

Replaces the v1 receipt tests that went with the retired v1 engine. The authorization is recorded
through the public CLI (`ase task authorize-protected`); the gate is evaluated in process.
"""
import io
import json
from contextlib import redirect_stdout
from pathlib import Path
import subprocess
import tempfile
import unittest

from ase.__main__ import main
from ase.gates import GateService, source_snapshot
from ase.state import read_record
from ase.store import Store
from ase.tasks import TaskService
from ase.workflow import WorkflowController
from test_task_contract_v2 import contract


class ProtectedAuthorizationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="ase-protected-")
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.repo, self.home = root / "work", root / "state"
        self.repo.mkdir()
        (self.repo / "guide.md").write_text("old content")
        for args in (("init", "-b", "feature/protected"), ("config", "user.email", "test@example.invalid"),
                     ("config", "user.name", "Protected test"), ("add", "."), ("commit", "-m", "baseline")):
            subprocess.run(["git", "-C", str(self.repo), *args], check=True, capture_output=True)
        self.store = Store(self.repo, self.home)
        self.addCleanup(self.store.close)
        c = contract("guarded")
        c["profile"] = "software"
        TaskService(self.store).create(c)
        task = TaskService(self.store).snapshot("guarded")
        WorkflowController(self.store, runner=None)._save("baseline", "guarded", source_snapshot(self.store, task["contract"], task_id="guarded"))

    def change(self, path="AGENTS.md", text="User-authorized instruction\n"):
        target = self.repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)

    def cli(self, *args):
        out = io.StringIO()
        with redirect_stdout(out):
            code = main(["--repo", str(self.repo), "--home", str(self.home), *args])
        return code, json.loads(out.getvalue())

    def authorize(self, *paths):
        code, result = self.cli("task", "authorize-protected", "guarded", "--reason", "User explicitly requested these files",
                                "--user-message-ref", "chat:protected", *[v for p in paths for v in ("--path", p)])
        self.assertEqual(code, 0, result)
        return result

    def protected_reasons(self):
        return [r for r in GateService(self.store).gate("guarded")["reasons"] if r.startswith("Unauthorized protected file changes")]

    def test_exact_authorization_clears_only_the_named_file(self):
        self.change()
        self.assertEqual(self.protected_reasons(), ["Unauthorized protected file changes: AGENTS.md"])
        receipt = self.authorize("AGENTS.md")
        self.assertEqual(receipt["paths"], ["AGENTS.md"])
        self.assertEqual(read_record(self.store.db, "protected_approval", "guarded")["payload"]["paths"], ["AGENTS.md"])
        self.assertEqual(self.protected_reasons(), [])

    def test_source_change_after_authorization_stales_it(self):
        self.change()
        self.authorize("AGENTS.md")
        self.change("guide.md", "new source revision")
        self.assertEqual(self.protected_reasons(), ["Unauthorized protected file changes: AGENTS.md"])

    def test_exact_scope_leaves_other_protections(self):
        self.change()
        self.change(".github/workflows/new.yml", "on: push\n")
        self.authorize("AGENTS.md")
        reasons = self.protected_reasons()
        self.assertEqual(len(reasons), 1, reasons)
        self.assertIn(".github/workflows/new.yml", reasons[0])
        self.assertNotIn("AGENTS.md", reasons[0])

    def test_paths_outside_the_repository_are_refused(self):
        self.change()
        for path in ("../AGENTS.md", "/AGENTS.md", "C:/AGENTS.md"):
            with self.subTest(path=path):
                code, result = self.cli("task", "authorize-protected", "guarded", "--path", path,
                                        "--reason", "explicit user authorization", "--user-message-ref", "chat:protected")
                self.assertEqual((code, result["status"]), (3, "BLOCKED"), result)
        self.assertIsNone(read_record(self.store.db, "protected_approval", "guarded")["payload"])


if __name__ == "__main__":
    unittest.main()
