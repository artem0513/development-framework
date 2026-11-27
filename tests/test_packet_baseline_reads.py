"""Packet builds read baseline bytes in one batch and validate the repository once, with unchanged output.

The reference path is the per-file read the packet used before: a fresh repository validation and
one `cat-file blob` per file. Equality is asserted on the canonical packet bytes; Git process
counts, not timings, show that the work no longer grows with the number of changed files.
"""
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from ase import repositories
from ase.evidence import impact
from ase.evidence.packets import _BaselineBlobs
from ase.inspection import _run, _validate, _validate_batch, safe_blob, safe_blobs
from ase.protocol import canonical
from ase.store import Store
from ase.tasks import TaskService, record_baseline
from ase.workflow import WorkflowController

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_nested_repositories import contract  # noqa: E402


def git(repo, *args):
    done = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    if done.returncode:
        raise AssertionError(done.stdout + done.stderr)
    return done.stdout.strip()


def per_file_reads():
    """The pre-batch behaviour: every file validates the repository again and reads its blob alone."""
    return (patch.object(_BaselineBlobs, "prefetch", lambda self, paths: None),
            patch.object(_BaselineBlobs, "blob", lambda self, path, expected, blob=_BaselineBlobs.blob:
                         blob(_BaselineBlobs(self.root, self.baseline), path, expected)))


class GitCount:
    """Counts host Git processes the safe inspection adapter starts."""

    def __init__(self):
        self.commands = []
        self.original = subprocess.run

    def __enter__(self):
        def counting(args, *a, **k):
            if args and str(args[0]).lower().endswith(("git", "git.exe")):
                self.commands.append([str(arg) for arg in args])
            return self.original(args, *a, **k)
        self.patch = patch("ase.inspection.subprocess.run", side_effect=counting)
        self.patch.start()
        return self

    def __exit__(self, *exc):
        self.patch.stop()

    def subcommands(self, name):
        return [command for command in self.commands if name in command]


@unittest.skipUnless(shutil.which("git"), "git required")
class PacketBaselineReadTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="ase-baseline-reads-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()

    def workspace(self, name, changed):
        """Workspace with nested clone src/orders on a branch that changes `changed` files against origin/master."""
        root = self.base / name
        root.mkdir()
        git(root, "init", "-q", "-b", "main")
        for repo in (root,):
            git(repo, "config", "user.name", "Fixture"), git(repo, "config", "user.email", "fixture@example.invalid")
        (root / ".gitignore").write_text("src/\n", encoding="utf-8")
        (root / ".meta").write_text(json.dumps({"projects": {"src/orders": "git@example.invalid:team/orders.git"}}), encoding="utf-8")
        git(root, "add", "-A"), git(root, "commit", "-q", "-m", "Workspace")
        repo = root / "src" / "orders"
        repo.mkdir(parents=True)
        git(repo, "init", "-q", "-b", "master")
        git(repo, "config", "user.name", "Fixture"), git(repo, "config", "user.email", "fixture@example.invalid")
        git(repo, "config", "core.autocrlf", "false")
        for index in range(changed):
            path = repo / "app" / f"module{index:02d}.py"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"from app.module{(index + 1) % changed:02d} import run\n\ndef run(x):\n    return x + {index}\n",
                            encoding="utf-8", newline="\n")
        (repo / "app" / "big.py").write_text("# big\n" + "x = 1\n" * 100000, encoding="utf-8", newline="\n")
        git(repo, "add", "-A"), git(repo, "commit", "-q", "-m", "Initial")
        remote = self.base / (name + "-orders.git")
        git(self.base, "init", "-q", "--bare", str(remote))
        git(repo, "remote", "add", "origin", str(remote)), git(repo, "push", "-q", "origin", "master"), git(repo, "fetch", "-q", "origin")
        git(repo, "checkout", "-q", "-b", "feature/x")
        for index in range(changed):
            path = repo / "app" / f"module{index:02d}.py"
            path.write_text(path.read_text(encoding="utf-8").replace("x + ", "x * "), encoding="utf-8", newline="\n")
        (repo / "app" / "big.py").write_text("# big, edited\n" + "x = 1\n" * 100000, encoding="utf-8", newline="\n")
        git(repo, "commit", "-q", "-am", "Change")
        store = Store(root, self.base / (name + "-state"))
        self.addCleanup(store.close)
        tasks = TaskService(store)
        task = tasks.create(contract("orders", ["src/orders"], {"path": "src/orders", "base": "origin/master"}))
        task = tasks.propose("orders", {"summary": "Change", "stage_ids": ["change"]}, expected_revision=task["revision"])
        task = tasks.approve("orders", proposal_hash=task["proposal_hash"], user_message_ref="test:owner-approved",
                             approved_stage_ids=["change"])
        record_baseline(store, "orders", task["contract"])
        return store, repo

    def packet(self, store):
        with patch("ase.model_limits.packet_budget", return_value=16 * 1048576), GitCount() as count:
            packet = WorkflowController(store)._packet("orders")
        return packet, count

    def test_packet_is_byte_identical_to_per_file_reads_and_git_does_not_scale_with_changed_files(self):
        store, _ = self.workspace("many", 24)
        batched, count = self.packet(store)
        first, second = per_file_reads()
        with first, second:
            reference, reference_count = self.packet(store)
        self.assertEqual(canonical(batched), canonical(reference))
        supplied = {item["path"]: item["content"] for item in batched["files"]}
        self.assertEqual(supplied["_ase_before/src/orders/app/module03.py"], "from app.module04 import run\n\ndef run(x):\n    return x + 3\n")
        self.assertIn("Base version unavailable for src/orders/app/big.py: the baseline file exceeds the packet file limit",
                      batched["coverage"]["missing"])
        # Two batch reads replace one `cat-file blob` per file, and the nested clone is validated once.
        self.assertEqual([c[c.index("cat-file"):] for c in count.subcommands("cat-file")],
                         [["cat-file", "--batch-check"], ["cat-file", "--batch"]])
        self.assertEqual(len(reference_count.subcommands("cat-file")), 25)
        self.assertGreater(len(reference_count.commands) - len(count.commands), 5 * 24)

        few, _ = self.workspace("few", 3)
        _, few_count = self.packet(few)
        self.assertEqual(len(few_count.commands), len(count.commands))

    def capture_first_run(self, store):
        """A first implementer capture taken after every changed file was edited (see test_workflow_v2)."""
        controller = WorkflowController(store)
        with patch("ase.model_limits.packet_budget", return_value=16 * 1048576):
            captured = controller._packet("orders")
        session = store.root / "tasks" / "orders" / "sessions" / "first"
        session.mkdir(parents=True)
        (session / "request.json").write_text(json.dumps({"source_packet": captured}), encoding="utf-8")
        controller._save("baseline_implementation", "orders", {"artifact_root": str(session),
                         "artifacts": {"request": {"path": str(session / "request.json")}}})

    def test_historical_capture_reads_edited_files_in_one_batch_with_identical_packet(self):
        counts = {}
        for name, changed in (("history-many", 24), ("history-few", 3)):
            store, _ = self.workspace(name, changed)
            self.capture_first_run(store)
            with patch("ase.evidence.packets.verify_receipt"):
                batched, count = self.packet(store)
                first, second = per_file_reads()
                with first, second:
                    reference, reference_count = self.packet(store)
            self.assertEqual(canonical(batched), canonical(reference))
            supplied = {item["path"]: item["content"] for item in batched["files"]}
            self.assertEqual(supplied["_ase_before/src/orders/app/module01.py"],
                             "from app.module02 import run\n\ndef run(x):\n    return x + 1\n")
            self.assertFalse([gap for gap in batched["coverage"]["missing"] if "module" in gap], batched["coverage"]["missing"])
            # The capture loop reads every edited file in one batch; only big.py (oversized) is left to the base pass.
            self.assertEqual([c[c.index("cat-file"):] for c in count.subcommands("cat-file")],
                             [["cat-file", "--batch-check"], ["cat-file", "--batch"], ["cat-file", "--batch-check"]])
            self.assertEqual(len(reference_count.subcommands("cat-file")), changed + 1)
            counts[name] = len(count.commands)
        self.assertEqual(counts["history-many"], counts["history-few"])

    def test_repository_validation_failure_is_reported_for_every_file_after_one_check(self):
        root = self.base / "plain"
        root.mkdir()
        reads = _BaselineBlobs(root, {"commit": "a" * 40, "repository": "src/missing"})
        with patch("ase.repositories.declared_repositories", wraps=repositories.declared_repositories) as declared:
            reads.prefetch(["src/missing/a.py", "src/missing/b.py"])
            errors = []
            for path in ("src/missing/a.py", "src/missing/b.py"):
                with self.assertRaisesRegex(ValueError, "not declared") as raised:
                    reads.blob(path, "0" * 64)
                errors.append(str(raised.exception))
        self.assertEqual(declared.call_count, 1)
        self.assertEqual(errors[0], errors[1])
        with self.assertRaisesRegex(ValueError, "outside the task repository"):
            reads.blob("src/other/a.py", "0" * 64)
        with self.assertRaisesRegex(ValueError, "records no Git commit"):
            _BaselineBlobs(root, {"repository": "src/missing"}).blob("src/missing/a.py", "0" * 64)

    def test_batch_returns_exactly_the_blobs_a_single_read_returns(self):
        _, repo = self.workspace("blobs", 2)
        (repo / "with space.txt").write_text("spaced\n", encoding="utf-8", newline="\n")
        git(repo, "add", "-A"), git(repo, "commit", "-q", "-m", "Spaced")
        commit = git(repo, "rev-parse", "HEAD")
        paths = ["app/module00.py", "with space.txt", "app/big.py", "app", "absent.py", "../escape.py", ".git/config", "a\nb"]
        blobs, oversized = safe_blobs(repo, commit, paths, 4096)
        self.assertEqual(blobs, {path: safe_blob(repo, commit, path) for path in ("app/module00.py", "with space.txt")})
        self.assertEqual(oversized, {"app/big.py"})
        for path in ("app", "absent.py"):
            with self.subTest(path=path), self.assertRaisesRegex(ValueError, "cat-file failed"):
                safe_blob(repo, commit, path)
        self.assertEqual(safe_blobs(repo, "HEAD", ["app/module00.py"], 4096), ({}, set()))

    def test_batch_forms_take_no_operands(self):
        _validate(["cat-file", "--batch"])
        _validate(["cat-file", "--batch-check"])
        for args in (["cat-file", "--batch", "x"], ["cat-file", "--batch-command"], ["cat-file", "--batch-all-objects"],
                     ["cat-file", "--batch=%(rest)"], ["cat-file", "--batch-check", "--follow-symlinks"]):
            with self.subTest(args=args), self.assertRaisesRegex(ValueError, "Unsupported"):
                _validate(args)
        commit = "a" * 40
        _validate_batch((commit + "\n" + commit + ":src/a b.py\n").encode())
        for names in (None, b"", commit.encode(), b"HEAD:x\n", (commit + ":../x\n").encode(), (commit + ":.git/config\n").encode(),
                      (commit + ":a\rb\n").encode(), b"--batch-all-objects\n", (commit + ":a\n\n").encode()):
            with self.subTest(names=names), self.assertRaisesRegex(ValueError, "Unsupported"):
                _validate_batch(names)
        # _run applies it before any process starts, whatever repository is named.
        with patch("ase.inspection.subprocess.run") as started, self.assertRaisesRegex(ValueError, "Unsupported"):
            _run(self.base, ["cat-file", "--batch"], input=b"HEAD:x\n")
        started.assert_not_called()


def reference_scan(repo, snapshot_paths, changed):
    """The per-pattern scan used before terms were matched by one tokenization per file."""
    patterns = {path: re.compile(r'(?<![\w-])(' + '|'.join(map(re.escape, impact._terms(path))) + r')(?![\w-])')
                for path in changed if impact._terms(path)}
    symbols = {path: impact._symbols(repo, path) for path in patterns}
    rows = {path: [] for path in changed}
    candidates = [p for p in sorted(snapshot_paths) if Path(p).suffix in impact.SOURCE_SUFFIXES]
    scanned = 0
    for candidate in candidates[:impact.MAX_SCAN_FILES]:
        target = repo / candidate
        try:
            if target.is_symlink() or target.stat().st_size > impact.MAX_FILE_BYTES:
                continue
            content = target.read_text(encoding='utf-8')
        except (OSError, UnicodeError):
            continue
        scanned += 1
        for path, pattern in patterns.items():
            if candidate != path and len(rows[path]) < impact.MAX_CALLERS and pattern.search(content):
                rows[path].append((candidate, impact._excerpt(content, pattern, symbols[path])))
    return rows, scanned, len(candidates) > impact.MAX_SCAN_FILES


class ScanEquivalenceTests(unittest.TestCase):
    def test_token_matching_finds_the_same_callers_as_per_pattern_search(self):
        repo = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, repo, ignore_errors=True)
        changed = ['app/rates.py', 'app/rates.v2.py', 'app/my-mod.py', 'app/größe.py', 'app/abc.py', 'app/abc.d.ts',
                   'pkg/billing/__init__.py', 'app/x+y.py', 'app/rates_old.py']
        callers = {
            'use/a.py': 'import rates\nrates.v2 = 1\n',
            'use/b.py': 'x = my-mod\ny = my_mod\n',
            'use/c.py': 'from größe import f\n',
            'use/d.ts': 'import abc.d\nabc.d.run()\n',
            'use/e.py': 'separates = 1\nrates_old\n-rates\nrates-\n',
            'use/f.py': 'from pkg import billing\nx+y\n',
            'use/g.js': 'abcd abc_ abcé éabc abc\n',
            'use/h.py': 'nothing here\n',
        }
        for rel in changed:
            (repo / rel).parent.mkdir(parents=True, exist_ok=True)
            (repo / rel).write_text('def handler():\n    return 1\n', encoding='utf-8')
        for rel, body in callers.items():
            (repo / rel).parent.mkdir(parents=True, exist_ok=True)
            (repo / rel).write_text(body, encoding='utf-8')
        (repo / 'use/binary.py').write_bytes(b'rates\xff\xfe')
        paths = {rel: 'h' for rel in [*changed, *callers, 'use/binary.py']}
        for limit in (impact.MAX_CALLERS, 1):
            with self.subTest(limit=limit), patch.object(impact, 'MAX_CALLERS', limit):
                # The fourth value counts all matches per change; the reference predates it.
                self.assertEqual(impact._scan(repo, paths, changed)[:3], reference_scan(repo, paths, changed))
        rows, _, _, _ = impact._scan(repo, paths, changed)
        self.assertEqual([caller for caller, _ in rows['app/rates.py']], ['use/a.py'])
        self.assertEqual([caller for caller, _ in rows['app/rates_old.py']], ['use/e.py'])
        self.assertEqual([caller for caller, _ in rows['app/x+y.py']], ['use/f.py'])


if __name__ == "__main__":
    unittest.main()
