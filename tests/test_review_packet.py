"""Owner-approved compact review packets, review parts and declared private check inputs.

Git, baselines, packets, the Runner and the gate run for real. The review provider is the
protocol fixture: the gate sees it as independent only through the explicit test seam below,
so these tests prove packet/coverage/binding protocol, never semantic review quality.
"""
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from ase.evidence.representation import (DIFF_PREFIX, MANIFEST_PATH, owners, part_gate, part_key, propose_parts,
                                         validate_review_packet)
from ase.evidence.check_inputs import CHECK_INPUT_DIRECTORY, validate_check_inputs
from ase.gates import GateService, source_snapshot
from ase.protocol import DEFAULT_PACKET_BYTES, canonical
from ase.runner import Runner, verify_receipt
from ase.state import read_record
from ase.store import Store
from ase.tasks import TaskService, record_baseline, validate_contract_v2
from ase.workflow import WorkflowController
from test_runner import FIXTURE

FOLDERS = ("alpha", "beta", "gamma")

PROVIDER = """
request = json.loads(input_text.split('REQUEST_JSON:\\n',1)[1])
role = request['role']
if role == 'ac':
 payload = {'criteria':[{'id':'AC1','expected':'Every call uses new_helper.','status':'PASS','evidence':['Protocol fixture'],
  'method':'Deterministic boundary fixture','limitations':['No semantic AC'],'missing_coverage':[]}]}
else:
 part = (request['handoff'].get('review_part') or {}).get('id', 'whole')
 findings = ([{'severity':'P1','path':'src/svc/beta/m001.py','line':1,'message':'injected part defect'}]
             if part == 'beta' and pathlib.Path('p1-beta').exists() else [])
 payload = {'findings':findings,'summary':'Protocol-only boundary fixture for ' + part + ', not semantic review'}
"""


def contract(task_id, review_packet=None, check_inputs=None):
    scope = {"resources": ["src/svc"], "effects": ["file_write"]}
    value = {"id": task_id, "schema_version": 2, "profile": "software", "risk": "LOW",
             "request": "Rename old_helper to new_helper in every module.", "scope": scope,
             "repository": {"path": "src/svc", "base": "origin/master"},
             "acceptance": [{"id": "AC1", "expected": "Every call uses new_helper.", "evidence_ids": ["lint"]}],
             "evidence": [{"id": "lint", "kind": "command", "argv": [sys.executable, "-c", "print('ok')"]}],
             "stages": [{"id": "change", "scope": scope, "acceptance_ids": ["AC1"], "depends_on": []}], "unknowns": []}
    if review_packet:
        value["review_packet"] = review_packet
    if check_inputs:
        value["check_inputs"] = check_inputs
    return value


def module(index, helper, lines_per_file):
    lines = ["# module %d: generated fixture line %03d of a mechanical rename\n" % (index, n) for n in range(lines_per_file)]
    # Each module names the next one, so changed modules are callers of other changed modules.
    lines[0] = "# module %d uses m%03d\n" % (index, index + 1)
    lines[lines_per_file // 2] = "value = %s(%d)\n" % (helper, index)
    return "".join(lines)


@unittest.skipUnless(shutil.which("git"), "git required")
class PacketFixture(unittest.TestCase):
    """A nested clone whose feature branch changes one line in every module of three folders."""
    FILES_PER_FOLDER = 20
    LINES_PER_FILE = 400
    UNCHANGED_FILES = 0  # unchanged modules naming the changed ones, so the caller index grows as in real services

    @classmethod
    def setUpClass(cls):
        # One large nested clone shared by the tests (copied per test): building it is the slow part.
        cls.template = tempfile.TemporaryDirectory(prefix="ase-review-packet-")
        base = Path(cls.template.name)
        root = base / "workspace"
        root.mkdir()
        cls.run_git(root, "init", "-q", "-b", "main")
        cls.identity(root)
        (root / ".gitignore").write_text("src/\n.devframework/\n", encoding="utf-8")
        (root / ".meta").write_text(json.dumps({"projects": {"src/svc": "git@example.invalid:team/svc.git"}}), encoding="utf-8")
        cls.run_git(root, "add", "-A")
        cls.run_git(root, "commit", "-q", "-m", "Workspace")
        repo = root / "src" / "svc"
        repo.mkdir(parents=True)
        cls.run_git(repo, "init", "-q", "-b", "master")
        cls.identity(repo)
        for folder in FOLDERS:
            (repo / folder).mkdir()
            for index in range(cls.FILES_PER_FOLDER):
                (repo / folder / ("m%03d.py" % index)).write_text(module(index, "old_helper", cls.LINES_PER_FILE), encoding="utf-8", newline="\n")
        if cls.UNCHANGED_FILES:
            (repo / "lib").mkdir()
            names = " ".join("m%03d" % index for index in range(cls.FILES_PER_FOLDER))
            for index in range(cls.UNCHANGED_FILES):
                (repo / "lib" / ("u%04d.py" % index)).write_text("# unchanged consumer %d of %s\nvalue = %d\n" % (index, names, index),
                                                                encoding="utf-8", newline="\n")
        cls.run_git(repo, "add", "-A")
        cls.run_git(repo, "commit", "-q", "-m", "Initial")
        remote = base / "svc.git"
        cls.run_git(base, "init", "-q", "--bare", str(remote))
        cls.run_git(repo, "remote", "add", "origin", str(remote))
        cls.run_git(repo, "push", "-q", "origin", "master")
        cls.run_git(repo, "fetch", "-q", "origin")
        cls.run_git(repo, "checkout", "-q", "-b", "feature/rename")
        for folder in FOLDERS:
            for index in range(cls.FILES_PER_FOLDER):
                (repo / folder / ("m%03d.py" % index)).write_text(module(index, "new_helper", cls.LINES_PER_FILE), encoding="utf-8", newline="\n")
        cls.run_git(repo, "commit", "-q", "-am", "Rename the helper")

    @classmethod
    def tearDownClass(cls):
        cls.template.cleanup()

    @staticmethod
    def run_git(repo, *args):
        done = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
        if done.returncode:
            raise AssertionError(done.stdout + done.stderr)
        return done.stdout.strip()

    @classmethod
    def identity(cls, repo):
        cls.run_git(repo, "config", "user.name", "Review Packet Fixture")
        cls.run_git(repo, "config", "user.email", "packet@example.invalid")
        cls.run_git(repo, "config", "core.autocrlf", "false")

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="ase-review-packet-case-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        shutil.copytree(Path(self.template.name) / "workspace", self.base / "workspace")
        remote = Path(self.template.name) / "svc.git"
        shutil.copytree(remote, self.base / "svc.git")
        self.root = self.base / "workspace"
        self.repo = self.root / "src" / "svc"
        self.run_git(self.repo, "remote", "set-url", "origin", str(self.base / "svc.git"))
        self.store = Store(self.root, self.base / "state")
        self.addCleanup(self.store.close)
        self.tasks = TaskService(self.store)

    def approve(self, task_id, **fields):
        task = self.tasks.create(contract(task_id, **fields))
        task = self.tasks.propose(task_id, {"summary": "Rename", "stage_ids": ["change"]}, expected_revision=task["revision"])
        task = self.tasks.approve(task_id, proposal_hash=task["proposal_hash"], user_message_ref="test:owner-approved",
                                  approved_stage_ids=["change"])
        record_baseline(self.store, task_id, task["contract"])
        return task

    def revise(self, task_id, **fields):
        task = self.tasks.snapshot(task_id)
        task = self.tasks.revise(task_id, contract(task_id, **fields), expected_revision=task["revision"])
        task = self.tasks.propose(task_id, {"summary": "Rename", "stage_ids": ["change"]}, expected_revision=task["revision"])
        return self.tasks.approve(task_id, proposal_hash=task["proposal_hash"], user_message_ref="test:owner-approved-2",
                                  approved_stage_ids=["change"])

    def changed(self, task_id):
        baseline = read_record(self.store.db, "baseline", task_id)["payload"]
        current = source_snapshot(self.store, self.tasks.snapshot(task_id)["contract"], task_id=task_id)["files"]
        return sorted(p for p in set(baseline["files"]) | set(current) if baseline["files"].get(p) != current.get(p))

    def controller(self, task_id):
        trusted = self.base / "provider"
        trusted.mkdir(exist_ok=True)
        provider = trusted / "provider.py"
        provider.write_text(FIXTURE.replace("payload = {'verdict':'PASS'}", PROVIDER), encoding="utf-8")
        runner = Runner({"executable": sys.executable, "executable_args": [str(provider), "success", "codex"],
                         "trusted_cwd": str(trusted), "protocol_only": True}, self.store.root / "tasks" / task_id / "sessions")
        return WorkflowController(self.store, runner=runner)

    def gate_at_protocol_boundary(self, task_id):
        """The fixture's receipts are protocol-only; this seam lets the gate judge coverage and bindings."""
        def seam(receipt, **kwargs):
            verified = verify_receipt(receipt, **kwargs)
            self.assertEqual(verified["evidence_kind"], "protocol_only")
            return {**verified, "evidence_kind": "independent_cli"}
        with patch("ase.gates.verify_receipt", side_effect=seam), patch("ase.runner.verify_receipt", side_effect=seam):
            return GateService(self.store).gate(task_id)



@unittest.skipUnless(shutil.which("git"), "git required")
class LargeMechanicalChangeTests(PacketFixture):
    """The reported case: >400 changed files, >4 MB, git diff <1 MB. Building its packets takes minutes."""
    FILES_PER_FOLDER = 150
    LINES_PER_FILE = 200

    def test_large_mechanical_change_fits_in_the_approved_diff_mode_and_independent_sessions_start(self):
        self.approve("large", review_packet={"version": 1, "changed_files": "diff_context", "context_lines": 5})
        changed = self.changed("large")
        self.assertEqual(len(changed), len(FOLDERS) * self.FILES_PER_FOLDER)
        self.assertGreater(sum((self.root / path).stat().st_size for path in changed), 4 * 1024 * 1024)
        diff = subprocess.run(["git", "-C", str(self.repo), "diff", "origin/master"], capture_output=True).stdout
        self.assertLess(len(diff), 1024 * 1024)
        self.controller("large").verify("large")
        for role in ("ac", "review"):
            receipt = read_record(self.store.db, "verification", "large:" + role)["payload"]
            self.assertEqual(receipt["status"], "COMPLETE", role)
            request = json.loads(Path(receipt["artifacts"]["request"]["path"]).read_text(encoding="utf-8"))
            packet = request["source_packet"]
            self.assertLessEqual(len(canonical(packet)), DEFAULT_PACKET_BYTES)
            files = {row["path"]: row["content"] for row in packet["files"]}
            rows = json.loads(files[MANIFEST_PATH])["files"]
            self.assertEqual(sorted(row["path"] for row in rows), changed)
            self.assertEqual({row["representation"] for row in rows}, {"DIFF_CONTEXT"})
            self.assertTrue(all(DIFF_PREFIX + path in files for path in changed))


@unittest.skipUnless(shutil.which("git"), "git required")
class ContextOverheadTests(PacketFixture):
    """The reported service: 444 of 1318 files changed, every changed module named by hundreds of others."""
    FILES_PER_FOLDER = 148
    LINES_PER_FILE = 120
    UNCHANGED_FILES = 874
    BOUND = {"version": 1, "mode": "lossless", "max_packet_bytes": 2097152}

    def approve_bound(self, task_id, **fields):
        value = contract(task_id, **fields)
        value["context_transport"] = self.BOUND
        task = self.tasks.create(value)
        task = self.tasks.propose(task_id, {"summary": "Rename", "stage_ids": ["change"]}, expected_revision=task["revision"])
        task = self.tasks.approve(task_id, proposal_hash=task["proposal_hash"], user_message_ref="test:owner-approved",
                                  approved_stage_ids=["change"])
        record_baseline(self.store, task_id, task["contract"])
        return task

    @staticmethod
    def sizes(packet):
        files = {row["path"]: len(row["content"].encode()) for row in packet["files"]}
        context = {path: size for path, size in files.items() if path.startswith("_ase_context/")}
        changed = sum(size for path, size in files.items() if path.startswith(DIFF_PREFIX))
        return context, changed

    def test_context_files_stay_smaller_than_the_change_and_the_packet_fits(self):
        self.approve_bound("overhead", review_packet={"version": 1, "changed_files": "diff_context", "context_lines": 20})
        self.assertEqual(len(self.changed("overhead")), len(FOLDERS) * self.FILES_PER_FOLDER)
        from ase.evidence.compact import expand_packet
        # Measured before transport compaction: a literal packet must fit too.
        logical = expand_packet(self.controller("overhead")._packet("overhead", role="review"))
        context, changed = self.sizes(logical)
        self.assertLessEqual(len(canonical(logical)), 2097152)
        self.assertLess(sum(context.values()), changed)
        files = {row["path"]: row["content"] for row in logical["files"]}
        impact = json.loads(files["_ase_context/change_impact.json"])
        self.assertEqual(len(impact["changes"]), len(FOLDERS) * self.FILES_PER_FOLDER)
        self.assertEqual(len(impact["caller_paths"]), len(set(impact["caller_paths"])))
        # Every lib module names every changed module; each changed m001+ is also named by the m000 of all folders' predecessors.
        self.assertTrue(all(row["callers_not_listed"] + len(row["callers"])
                            in (self.UNCHANGED_FILES, self.UNCHANGED_FILES + len(FOLDERS)) for row in impact["changes"]))
        source = json.loads(files["_ase_context/source_changes.json"])
        self.assertEqual((len(source["unchanged_paths"]), source["unchanged_path_count"], source["unchanged_paths_truncated"]),
                         (self.UNCHANGED_FILES, self.UNCHANGED_FILES, False))
        self.assertTrue(set(impact["caller_paths"][number] for number in impact["caller_in_packet"]) <= set(files))
        self.assertTrue(all("before_sha256" not in row for row in json.loads(files[MANIFEST_PATH])["files"]))

    def test_unchanged_list_is_a_sorted_prefix_within_its_budget(self):
        self.approve_bound("budget", review_packet={"version": 1, "changed_files": "diff_context", "context_lines": 20})
        from ase.evidence.compact import expand_packet
        with patch("ase.evidence.packets.UNCHANGED_LIST_BYTES", 230):
            logical = expand_packet(self.controller("budget")._packet("budget", role="review"))
        source = json.loads({row["path"]: row["content"] for row in logical["files"]}["_ase_context/source_changes.json"])
        expected = ["src/svc/lib/u%04d.py" % index for index in range(10)]
        self.assertEqual(source["unchanged_paths"], expected)
        self.assertEqual((source["unchanged_paths_truncated"], source["unchanged_path_count"]), (True, self.UNCHANGED_FILES))

    def test_each_part_carries_only_its_own_context(self):
        parts = [{"id": folder, "resources": ["src/svc/" + folder]} for folder in FOLDERS]
        self.approve_bound("parts", review_packet={"version": 1, "changed_files": "diff_context", "context_lines": 20,
                                                   "parts": parts})
        from ase.evidence.compact import expand_packet
        controller = self.controller("parts")
        whole = expand_packet(controller._packet("parts", role="review"))
        for folder in FOLDERS:
            with self.subTest(part=folder):
                packet = expand_packet(controller._packet("parts", part_id=folder, role="review"))
                files = {row["path"]: row["content"] for row in packet["files"]}
                impact = json.loads(files["_ase_context/change_impact.json"])
                self.assertEqual({row["path"].split("/")[2] for row in impact["changes"]}, {folder})
                in_packet = {impact["caller_paths"][number] for number in impact["caller_in_packet"]}
                self.assertTrue(in_packet <= set(files))
                # Changed callers are diffs or in other parts here: never "in the packet", so they get excerpts or a reason.
                changed_callers = {path for path in impact["caller_paths"] if not path.startswith("src/svc/lib/")}
                self.assertTrue(changed_callers)
                self.assertFalse(changed_callers & in_packet)
                manifest = json.loads(files[MANIFEST_PATH])
                self.assertEqual({row["path"].split("/")[2] for row in manifest["files"]}, {folder})
                self.assertEqual(manifest["in_other_parts"], {other: self.FILES_PER_FOLDER for other in FOLDERS if other != folder})
                self.assertLess(len(canonical(packet)) * 2, 2097152)
        # The whole-task packet keeps a paths-only index of the changes its parts review.
        impact = json.loads({row["path"]: row["content"] for row in whole["files"]}["_ase_context/change_impact.json"])
        self.assertEqual(len(impact["changes"]), len(FOLDERS) * self.FILES_PER_FOLDER)
        self.assertTrue(all("excerpts" not in row and set(row["excerpt_omitted"]) == {"CHANGE_NOT_CARRIED"}
                            for row in impact["changes"]))


@unittest.skipUnless(shutil.which("git"), "git required")
class BranchDiffCoverageTests(PacketFixture):
    FILES_PER_FOLDER = 3
    LINES_PER_FILE = 14

    def test_only_packets_requiring_the_whole_branch_patch_report_its_size(self):
        parts = [{"id": folder, "resources": ["src/svc/" + folder]} for folder in FOLDERS]
        self.approve("compact", review_packet={"version": 1, "changed_files": "diff_context"})
        self.approve("split", review_packet={"version": 1, "changed_files": "full_bodies", "parts": parts})
        self.approve("plain")
        from ase.evidence.compact import expand_packet
        controller = WorkflowController(self.store)
        with patch("ase.source_coverage.PACKET_FILE_LIMIT", 2876):
            for task, role, part in (("compact", "implementation", None), ("compact", "ac", None),
                                     ("split", "review", None), ("split", "review", "alpha")):
                with self.subTest(task=task, role=role, part=part):
                    packet = expand_packet(controller._packet(task, stage_id="change", part_id=part, role=role))
                    paths = {item["path"] for item in packet["files"]}
                    self.assertNotIn("_ase_context/source_diff.patch", paths)
                    self.assertIn(MANIFEST_PATH, paths)
                    self.assertEqual(packet["coverage"]["missing"], [])
            plain = controller._packet("plain", stage_id="change")
            self.assertTrue(any("Branch diff against" in reason for reason in plain["coverage"]["missing"]), plain["coverage"])

    def test_compact_policy_skips_the_whole_patch_on_a_clean_baseline(self):
        self.approve("compact", review_packet={"version": 1, "changed_files": "diff_context"})
        self.run_git(self.repo, "checkout", "-q", "master")
        with patch("ase.evidence.packets._branch_diff", side_effect=AssertionError("Whole patch is redundant")):
            packet = WorkflowController(self.store)._packet("compact", role="implementation")
        self.assertEqual(packet["coverage"]["missing"], [])


class RequiredReviewContextTests(PacketFixture):
    FILES_PER_FOLDER = 3
    LINES_PER_FILE = 14
    UNCHANGED_FILES = 1

    def record_context(self, task_id, required, optional=(), stage="change"):
        from ase.discovery import discover_capabilities, record_discovery
        entries = [{"path": path, "from": "src/svc/alpha/m000.py", "required": is_required,
                    "blocking_stage_ids": [stage], "reason": "Inspect the complete calling or dependency logic"}
                   for is_required, paths in ((True, required), (False, optional)) for path in paths]
        discovery = discover_capabilities(self.store.project, scope={"resources": ["src/svc"], "dependencies": entries},
                                          backend_config={})
        record_discovery(self.store, task_id, discovery, expected_revision=0)

    def test_compact_packet_keeps_required_changed_file_before_and_after(self):
        path = "src/svc/beta/m000.py"
        optional = "src/svc/gamma/m000.py"
        self.approve("context", review_packet={"version": 1, "changed_files": "diff_context", "full_body_max_bytes": 0})
        self.record_context("context", [path], [optional])
        packet = WorkflowController(self.store)._packet("context", stage_id="change", role="review")
        files = {item["path"]: item["content"] for item in packet["files"]}
        self.assertEqual(files.get(path), module(0, "new_helper", self.LINES_PER_FILE))
        self.assertEqual(files.get("_ase_before/" + path), module(0, "old_helper", self.LINES_PER_FILE))
        self.assertNotIn(DIFF_PREFIX + path, files)
        self.assertNotIn(optional, files)
        self.assertEqual(packet["coverage"]["missing"], [])

    def test_split_packets_keep_required_caller_and_cross_part_dependency_as_context(self):
        caller = "src/svc/lib/u0000.py"
        dependency = "src/svc/beta/m000.py"
        optional = "src/svc/gamma/m000.py"
        parts = [{"id": name, "resources": ["src/svc/" + name]} for name in FOLDERS]
        self.approve("context", review_packet={"version": 1, "changed_files": "diff_context",
                                             "full_body_max_bytes": 0, "parts": parts})
        self.record_context("context", [caller, dependency], [optional])
        controller = WorkflowController(self.store)
        for role, part in (("review", "alpha"), ("review", "beta"), ("ac", None), ("review", None)):
            with self.subTest(role=role, part=part):
                packet = controller._packet("context", stage_id="change", part_id=part, role=role)
                files = {item["path"]: item["content"] for item in packet["files"]}
                self.assertEqual(files.get(caller), (self.root / caller).read_text(encoding="utf-8"))
                self.assertEqual(files.get(dependency), module(0, "new_helper", self.LINES_PER_FILE))
                self.assertNotIn(optional, files)
                rows = {row["path"]: row for row in json.loads(files[MANIFEST_PATH])["files"]}
                self.assertEqual(rows[dependency]["part"], "beta")
                self.assertEqual(rows[dependency]["representation"], "FULL_BODY")
                self.assertEqual(rows[dependency].get("context_only", False), part != "beta")
                if part == "alpha":
                    impact = json.loads(files["_ase_context/change_impact.json"])
                    self.assertNotIn(dependency, {row["path"] for row in impact["changes"]})

    def test_required_context_uses_stage_mapping_and_does_not_silently_shrink(self):
        path = "src/svc/beta/m000.py"
        value = contract("context", review_packet={"version": 1, "changed_files": "diff_context",
                                                   "full_body_max_bytes": 0})
        value["stages"].append({"id": "followup", "scope": value["scope"],
                                "acceptance_ids": ["AC1"], "depends_on": ["change"]})
        task = self.tasks.create(value)
        task = self.tasks.propose("context", {"summary": "Rename", "stage_ids": ["change", "followup"]},
                                  expected_revision=task["revision"])
        task = self.tasks.approve("context", proposal_hash=task["proposal_hash"],
                                  user_message_ref="test:owner-approved", approved_stage_ids=["change", "followup"])
        record_baseline(self.store, "context", task["contract"])
        self.record_context("context", [path], stage="followup")
        controller = WorkflowController(self.store)
        early = controller._packet("context", stage_id="change", role="review")
        self.assertNotIn(path, early["coverage"]["included"])
        later = controller._packet("context", stage_id="followup", role="review")
        self.assertIn(path, later["coverage"]["included"])
        with patch("ase.model_limits.packet_budget", return_value=500):
            with self.assertRaisesRegex(ValueError, "Required whole-file review context .*" + path):
                controller._packet("context", stage_id="followup", role="review")

    def test_required_context_recovers_verified_head_baseline_without_a_base_ref(self):
        path = "src/svc/beta/m000.py"
        self.run_git(self.repo, "checkout", "-q", "master")
        value = contract("context", review_packet={"version": 1, "changed_files": "diff_context",
                                                   "full_body_max_bytes": 0})
        del value["repository"]["base"]
        task = self.tasks.create(value)
        task = self.tasks.propose("context", {"summary": "Rename", "stage_ids": ["change"]},
                                  expected_revision=task["revision"])
        task = self.tasks.approve("context", proposal_hash=task["proposal_hash"],
                                  user_message_ref="test:owner-approved", approved_stage_ids=["change"])
        record_baseline(self.store, "context", task["contract"])
        self.record_context("context", [path])
        self.run_git(self.repo, "checkout", "-q", "feature/rename")

        packet = WorkflowController(self.store)._packet("context", stage_id="change", role="review")
        files = {item["path"]: item["content"] for item in packet["files"]}
        self.assertEqual(files.get("_ase_before/" + path), module(0, "old_helper", self.LINES_PER_FILE))
        self.assertEqual(packet["coverage"]["missing"], [])

    def test_split_advice_accounts_for_required_context_present_in_every_part(self):
        from ase.evidence.representation import overflow_advice

        policy = contract("context", review_packet={"version": 1, "changed_files": "diff_context"})
        path = "src/svc/shared.py"
        files = [{"path": path, "content": "x" * 52},
                 {"path": "_ase_before/" + path, "content": "x" * 50},
                 {"path": DIFF_PREFIX + "src/svc/alpha/m000.py", "content": "y" * 20}]
        sizes = {path: 102, "src/svc/alpha/m000.py": 64}
        advice = overflow_advice(policy, files, sizes, 100, 150, required_context={path})
        self.assertIn("context files alone", advice)
        self.assertNotIn('"parts"', advice)
        policy["context_transport"] = {"version": 1, "mode": "lossless"}
        advice = overflow_advice(policy, files, sizes, 100, 150, required_context={path})
        self.assertIn("uncompressed estimate", advice)
        self.assertIn("measure each proposed part", advice)


class ReviewPacketTests(PacketFixture):
    # --- C: compact representation ------------------------------------------------------------

    def test_small_ordinary_task_packet_is_unchanged_by_the_new_code(self):
        self.approve("plain")
        self.run_git(self.repo, "checkout", "-q", "master")
        (self.repo / "alpha" / "m000.py").write_text(module(0, "new_helper", self.LINES_PER_FILE), encoding="utf-8", newline="\n")
        packet = WorkflowController(self.store)._packet("plain")
        paths = {row["path"] for row in packet["files"]}
        self.assertIn("src/svc/alpha/m000.py", paths)
        self.assertIn("_ase_before/src/svc/alpha/m000.py", paths)
        self.assertIn("_ase_context/source_diff.patch", paths)
        self.assertFalse([path for path in paths if path.startswith(DIFF_PREFIX)])
        self.assertNotIn(MANIFEST_PATH, paths)
        self.assertNotIn("changed_file_mode", packet["coverage"])

    def test_full_copies_overflow_and_the_advice_asks_the_owner_for_the_compact_mode(self):
        self.approve("plain")
        with self.assertRaisesRegex(ValueError, "exceeds byte limit.*60 changed files.*diff_context.*never chosen by size"):
            WorkflowController(self.store)._packet("plain")

    def test_owner_approved_diff_mode_fits_and_names_every_changed_file(self):
        self.approve("compact", review_packet={"version": 1, "changed_files": "diff_context", "context_lines": 5})
        packet = WorkflowController(self.store)._packet("compact")
        self.assertLessEqual(len(canonical(packet)), DEFAULT_PACKET_BYTES)
        files = {row["path"]: row["content"] for row in packet["files"]}
        manifest = json.loads(files[MANIFEST_PATH])
        self.assertEqual(manifest["mode"], "diff_context")
        self.assertEqual(packet["coverage"]["changed_file_mode"], "diff_context")
        changed = self.changed("compact")
        self.assertEqual(sorted(row["path"] for row in manifest["files"]), changed)
        self.assertEqual({row["representation"] for row in manifest["files"]}, {"DIFF_CONTEXT"})
        sample = "src/svc/beta/m007.py"
        self.assertNotIn(sample, files)
        self.assertNotIn("_ase_before/" + sample, files)
        self.assertNotIn("_ase_context/source_diff.patch", files)
        diff = files[DIFF_PREFIX + sample]
        self.assertIn("-value = old_helper(7)", diff)
        self.assertIn("+value = new_helper(7)", diff)
        self.assertEqual(diff.count("generated fixture line"), 10)  # five context lines on each side

    def test_small_new_and_artifact_files_keep_full_bodies(self):
        self.approve("compact", review_packet={"version": 1, "changed_files": "diff_context", "context_lines": 5,
                                               "full_body_max_bytes": 0})
        (self.repo / "added.py").write_text("NEW = 1\n", encoding="utf-8", newline="\n")
        packet = WorkflowController(self.store)._packet("compact")
        files = {row["path"]: row["content"] for row in packet["files"]}
        rows = {row["path"]: row for row in json.loads(files[MANIFEST_PATH])["files"]}
        self.assertEqual(rows["src/svc/added.py"]["representation"], "FULL_BODY")
        self.assertEqual(rows["src/svc/added.py"]["reason"], "new file")
        self.assertEqual(files["src/svc/added.py"], "NEW = 1\n")

    def test_mode_is_a_validated_contract_field_bound_by_the_contract_hash(self):
        plain = validate_contract_v2(contract("a"))
        compact = validate_contract_v2(contract("a", review_packet={"version": 1, "changed_files": "diff_context"}))
        self.assertEqual(compact["review_packet"], {"version": 1, "changed_files": "diff_context",
                                                     "context_lines": 20, "full_body_max_bytes": 4096})
        from ase.state import hash_value
        self.assertNotEqual(hash_value(plain), hash_value(compact))
        for bad in ({"version": 2, "changed_files": "diff_context"}, {"version": 1, "changed_files": "auto"},
                    {"version": 1, "changed_files": "diff_context", "context_lines": 1},
                    {"version": 1, "changed_files": "full_bodies", "context_lines": 5},
                    {"version": 1, "changed_files": "full_bodies", "parts": [{"id": "a", "resources": ["x"]}]},
                    {"version": 1, "changed_files": "full_bodies", "parts": [{"id": "a", "resources": ["../x"]},
                                                                              {"id": "b", "resources": ["y"]}]}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                validate_review_packet(bad)

    # --- C: review parts ----------------------------------------------------------------------

    def test_overflowing_compact_packet_proposes_a_split_by_folder(self):
        self.approve("compact", review_packet={"version": 1, "changed_files": "diff_context", "context_lines": 200})
        with self.assertRaises(ValueError) as raised:
            WorkflowController(self.store)._packet("compact")
        message = str(raised.exception)
        self.assertIn("review_packet.parts", message)
        proposal, _ = json.JSONDecoder().raw_decode(message[message.index("[{"):])
        self.assertIn("Size breakdown", message)
        changed = self.changed("compact")
        self.assertTrue(all(len([p for p in proposal if any(r == "." or path == r or path.startswith(r + "/")
                                                            for r in p["resources"])]) == 1 for path in changed))

    def test_proposed_parts_cover_each_path_once_and_respect_the_budget(self):
        sizes = {"a/x/1.py": 40, "a/x/2.py": 40, "a/y/3.py": 40, "b/4.py": 30, "c.py": 10}
        proposal = propose_parts(sizes, 90)
        for path in sizes:
            self.assertEqual(len([p for p in proposal if any(path == r or path.startswith(r + "/") for r in p["resources"])]), 1)
        self.assertTrue(len(proposal) >= 2)

    def test_split_review_runs_one_session_per_part_and_the_gate_requires_every_file_covered_once(self):
        parts = [{"id": folder, "resources": ["src/svc/" + folder]} for folder in FOLDERS]
        self.approve("split", review_packet={"version": 1, "changed_files": "diff_context", "context_lines": 40, "parts": parts})
        controller = self.controller("split")
        whole = controller._packet("split")
        whole_rows = json.loads({r["path"]: r["content"] for r in whole["files"]}[MANIFEST_PATH])["files"]
        self.assertEqual({row["representation"] for row in whole_rows}, {"REVIEWED_IN_PART"})
        for folder in FOLDERS:
            packet = controller._packet("split", part_id=folder)
            self.assertLessEqual(len(canonical(packet)), DEFAULT_PACKET_BYTES)
            rows = json.loads({r["path"]: r["content"] for r in packet["files"]}[MANIFEST_PATH])["files"]
            mine = [row["path"] for row in rows if row["representation"] == "DIFF_CONTEXT"]
            self.assertEqual(len(mine), self.FILES_PER_FOLDER)
            self.assertTrue(all(path.startswith("src/svc/" + folder + "/") for path in mine))
        controller.verify("split")
        receipts = {part["id"]: read_record(self.store.db, "verification_part", part_key("split", None, part["id"]))["payload"]
                    for part in parts}
        self.assertTrue(all(receipt and receipt["status"] == "COMPLETE" for receipt in receipts.values()))
        self.assertEqual(len({receipt["execution_id"] for receipt in receipts.values()}), len(parts))
        verdict = self.gate_at_protocol_boundary("split")
        self.assertFalse([r for r in verdict["reasons"] if "part" in r.lower()], verdict["reasons"])
        # A part session never stands in for another part (its binding names the part).
        with patch("ase.evidence.representation.part_hash", return_value="0" * 64):
            verdict = self.gate_at_protocol_boundary("split")
        self.assertTrue(any("Review part alpha: receipt invalid" in r for r in verdict["blocked_reasons"]))
        # Only code review is split: acceptance is not told to ignore the bodies it lacks.
        for role in ("ac", "review"):
            receipt = read_record(self.store.db, "verification", "split:" + role)["payload"]
            request = json.loads(Path(receipt["artifacts"]["request"]["path"]).read_text(encoding="utf-8"))
            self.assertEqual("REVIEWED_IN_PART" in request["handoff"]["instruction"], role == "review", role)
        # The whole-task review receipt cannot stand in for a part.
        from ase.state import transaction, write_record
        main = read_record(self.store.db, "verification", "split:review")["payload"]
        with transaction(self.store.db) as db:
            old = read_record(db, "verification_part", part_key("split", None, "beta"))
            write_record(db, "verification_part", part_key("split", None, "beta"), main, expected_revision=old["revision"])
        verdict = self.gate_at_protocol_boundary("split")
        self.assertTrue(any(r.startswith("Review part beta:") for r in verdict["reasons"]), verdict["reasons"])
        self.assertNotEqual(verdict["status"], "APPROVED")
        # A blocking finding in one part rejects the whole gate; stage verification keys part receipts by stage.
        (self.base / "provider" / "p1-beta").write_text("", encoding="utf-8")
        controller.verify("split", "change")
        self.assertIsNotNone(read_record(self.store.db, "verification_part", part_key("split", "change", "beta"))["payload"])
        def seam(receipt, **kwargs):
            return {**verify_receipt(receipt, **kwargs), "evidence_kind": "independent_cli"}
        with patch("ase.gates.verify_receipt", side_effect=seam), patch("ase.runner.verify_receipt", side_effect=seam):
            verdict = GateService(self.store).gate("split", "change")
        self.assertEqual(verdict["status"], "REJECTED")
        self.assertTrue(any("Review part beta: P1: injected part defect" in r for r in verdict["rejected_reasons"]))
        # Removing a folder from coverage: the gate refuses, even with every remaining part reviewed.
        self.revise("split", review_packet={"version": 1, "changed_files": "diff_context", "context_lines": 40,
                                            "parts": parts[:2]})
        verdict = self.gate_at_protocol_boundary("split")
        self.assertNotEqual(verdict["status"], "APPROVED")
        self.assertTrue(any("not covered by exactly one independently reviewed part: src/svc/gamma/" in r
                            for r in verdict["blocked_reasons"]))

    def test_whole_task_packets_of_a_split_keep_changed_tests_and_say_other_bodies_were_not_supplied(self):
        parts = [{"id": folder, "resources": ["src/svc/" + folder]} for folder in FOLDERS]
        self.approve("split", review_packet={"version": 1, "changed_files": "diff_context", "context_lines": 5, "parts": parts})
        (self.repo / "alpha" / "tests").mkdir()
        (self.repo / "alpha" / "tests" / "test_rename.py").write_text("def test_rename():\n    assert True\n",
                                                                       encoding="utf-8", newline="\n")
        packet = WorkflowController(self.store)._packet("split")
        files = {row["path"]: row["content"] for row in packet["files"]}
        manifest = json.loads(files[MANIFEST_PATH])
        rows = {row["path"]: row for row in manifest["files"]}
        self.assertEqual(rows["src/svc/alpha/tests/test_rename.py"]["representation"], "FULL_BODY")
        self.assertIn("src/svc/alpha/tests/test_rename.py", files)
        self.assertEqual(rows["src/svc/alpha/m001.py"]["representation"], "REVIEWED_IN_PART")
        self.assertNotIn("src/svc/alpha/m001.py", files)
        self.assertIn("NOT supplied to this session", manifest["note"])
        self.assertIn("UNVERIFIED and blocks", manifest["note"])

    def test_overlapping_or_missing_parts_and_failed_or_missing_part_reviews_never_pass(self):
        declared = validate_contract_v2(contract("x", review_packet={"version": 1, "changed_files": "full_bodies", "parts": [
            {"id": "one", "resources": ["src/svc/alpha", "src/svc/beta/m001.py"]}, {"id": "two", "resources": ["src/svc/beta"]}]}))
        self.assertEqual(owners(declared, "src/svc/beta/m001.py"), ["one", "two"])
        self.assertEqual(owners(declared, "src/svc/gamma/m001.py"), [])
        blocked, rejected = part_gate(self.store, declared, "x", None, {}, ["src/svc/beta/m001.py", "src/svc/gamma/m001.py"])
        self.assertTrue(any("src/svc/beta/m001.py" in reason for reason in blocked))
        self.assertTrue(any("src/svc/gamma/m001.py" in reason for reason in blocked))
        self.assertIn("Review part one: independent review session missing", blocked)
        self.assertIn("Review part two: independent review session missing", blocked)
        self.assertEqual(part_gate(self.store, validate_contract_v2(contract("y")), "y", None, {}, ["a"]), ([], []))

    # --- B: declared private check inputs ------------------------------------------------------

    def write_check(self, text):
        folder = self.root.joinpath(*CHECK_INPUT_DIRECTORY.split("/"))
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "rename_check.py").write_text(text, encoding="utf-8", newline="\n")
        return CHECK_INPUT_DIRECTORY + "/rename_check.py"

    def test_check_inputs_are_restricted_to_the_dedicated_private_folder(self):
        good = [{"path": CHECK_INPUT_DIRECTORY + "/rule.ps1", "evidence_ids": ["lint"]}]
        self.assertEqual(validate_check_inputs(good, ["lint"]), good)
        for path in (".devframework/local/state.db", ".devframework/local/credentials/x.json", ".devframework/local/checks",
                     ".devframework/local/checks/../state.db", "scripts/check.ps1", "C:/x/.devframework/local/checks/a.ps1",
                     ".devframework\\local\\checks\\a.ps1"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                validate_check_inputs([{"path": path, "evidence_ids": ["lint"]}], ["lint"])
        with self.assertRaisesRegex(ValueError, "unknown evidence"):
            validate_check_inputs([{"path": CHECK_INPUT_DIRECTORY + "/a.ps1", "evidence_ids": ["other"]}], ["lint"])

    def test_declared_check_script_is_visible_redacted_and_bound_and_an_edit_makes_evidence_stale(self):
        path = self.write_check("RULE = 'no old_helper'\nTOKEN = 'ghp_" + "a" * 36 + "'\nprint('rule: no old_helper; threshold 0; violations', 0)\n")
        self.run_git(self.repo, "checkout", "-q", "master")
        (self.repo / "alpha" / "m000.py").write_text(module(0, "new_helper", self.LINES_PER_FILE), encoding="utf-8", newline="\n")
        task = self.approve("checked", check_inputs=[{"path": path, "evidence_ids": ["lint"]}])
        packet = WorkflowController(self.store)._packet("checked")
        files = {row["path"]: row["content"] for row in packet["files"]}
        body = files["_ase_check_inputs/" + path]
        self.assertIn("RULE = 'no old_helper'", body)
        self.assertNotIn("ghp_" + "a" * 36, body)
        manifest = json.loads(files["_ase_context/check_inputs.json"])
        self.assertEqual(manifest["inputs"][0]["evidence_ids"], ["lint"])
        self.assertEqual(manifest["inputs"][0]["body_status"], "SUPPLIED_REDACTED")
        self.assertIn("injected break", manifest["recommendation"])
        # The script is not project source: it is no changed file and never part of the change.
        changes = json.loads(files["_ase_context/source_changes.json"])
        self.assertEqual([entry["path"] for entry in changes["changes"]], ["src/svc/alpha/m000.py"])
        before = source_snapshot(self.store, task["contract"], task_id="checked")
        controller = WorkflowController(self.store)
        controller.check("checked")
        self.assertNotIn("Stale check evidence: fingerprint", GateService(self.store).gate("checked")["reasons"])
        self.write_check("RULE = 'weakened'\n")
        after = source_snapshot(self.store, task["contract"], task_id="checked")
        self.assertNotEqual(before["fingerprint"], after["fingerprint"])
        self.assertEqual(before["files"], after["files"])
        self.assertIn("Stale check evidence: fingerprint", GateService(self.store).gate("checked")["reasons"])
        files = {row["path"]: row["content"] for row in WorkflowController(self.store)._packet("checked")["files"]}
        row = json.loads(files["_ase_context/check_inputs.json"])["inputs"][0]
        self.assertTrue(row["changed_since_approval"])
        self.assertNotEqual(row["approved_sha256"], row["sha256"])

    def test_separate_approval_keeps_earlier_check_input_baseline(self):
        path = self.write_check("RULE = 'strict'\n")
        self.run_git(self.repo, "checkout", "-q", "master")
        value = contract("checked", check_inputs=[{"path": path, "evidence_ids": ["lint"]}])
        value["stages"].append({**value["stages"][0], "id": "followup", "depends_on": ["change"]})
        task = self.tasks.create(value)
        task = self.tasks.propose("checked", {"summary": "Rename in stages", "stage_ids": ["change", "followup"]},
                                  expected_revision=task["revision"])
        self.tasks.approve("checked", proposal_hash=task["proposal_hash"], user_message_ref="test:change",
                           approved_stage_ids=["change"])
        self.write_check("RULE = 'weakened'\n")
        self.tasks.approve("checked", proposal_hash=task["proposal_hash"], user_message_ref="test:followup",
                           approved_stage_ids=["followup"])
        controller = WorkflowController(self.store)
        for stage_id, changed in (("change", True), ("followup", False), (None, True)):
            with self.subTest(stage_id=stage_id):
                files = {row["path"]: row["content"] for row in controller._packet("checked", stage_id)["files"]}
                row = json.loads(files["_ase_context/check_inputs.json"])["inputs"][0]
                self.assertEqual(row["changed_since_approval"], changed)

    def test_unreadable_linked_or_oversized_check_inputs_are_refused_and_block_the_gate(self):
        import os
        from ase.evidence.check_inputs import CHECK_INPUT_MAX_BYTES, read_check_input
        path = self.write_check("print('ok')\n")
        folder = self.root.joinpath(*CHECK_INPUT_DIRECTORY.split("/"))
        self.assertEqual(read_check_input(self.root, path), b"print('ok')\n")
        os.link(folder / "rename_check.py", self.base / "outside-link.py")
        with self.assertRaisesRegex(ValueError, "hardlinked"):
            read_check_input(self.root, path)
        (self.base / "outside-link.py").unlink()
        (folder / "big.py").write_bytes(b"#" * (CHECK_INPUT_MAX_BYTES + 1))
        with self.assertRaisesRegex(ValueError, "exceeds"):
            read_check_input(self.root, CHECK_INPUT_DIRECTORY + "/big.py")
        try:
            os.symlink(folder / "rename_check.py", folder / "linked.py")
        except OSError:
            pass  # creating symlinks needs a Windows privilege; the hardlink case above still runs
        else:
            with self.assertRaisesRegex(ValueError, "link"):
                read_check_input(self.root, CHECK_INPUT_DIRECTORY + "/linked.py")
        self.run_git(self.repo, "checkout", "-q", "master")
        self.approve("checked", check_inputs=[{"path": path, "evidence_ids": ["lint"]}])
        (folder / "rename_check.py").unlink()
        reasons = GateService(self.store).gate("checked")["blocked_reasons"]
        self.assertTrue(any(r.startswith("Declared check input unavailable: " + path) for r in reasons), reasons)

    def test_declared_check_script_never_reaches_the_commit_pr_body_or_ci_check(self):
        from ase import ci_check
        from ase.finish import finish
        path = self.write_check("print('rule: no old_helper; threshold 0; violations', 0)\n")
        self.run_git(self.repo, "checkout", "-q", "master")
        self.run_git(self.repo, "checkout", "-q", "-b", "feature/one")
        (self.repo / "alpha" / "m000.py").write_text(module(0, "new_helper", self.LINES_PER_FILE), encoding="utf-8", newline="\n")
        self.approve("checked", check_inputs=[{"path": path, "evidence_ids": ["lint"]}])
        self.run_git(self.repo, "commit", "-q", "-am", "Rename the helper in alpha")
        body = self.base / "body.md"
        with patch("ase.finish.GateService.gate", return_value={"status": "APPROVED", "reasons": []}):
            ready = finish(self.store, "checked", "origin/master", body_file=str(body))
        self.assertEqual(ready["status"], "READY", ready["reasons"])
        text = ready["pr_body"]
        self.assertIn("Rename old_helper", text)
        self.assertNotIn("rename_check", text)
        self.assertNotIn(".devframework", text)
        branch = self.run_git(self.repo, "diff", "--name-only", "origin/master...HEAD")
        self.assertEqual(branch, "alpha/m000.py")
        checked = ci_check.run(self.repo, "origin/master")
        self.assertEqual(checked["status"], "PASS", checked)
        self.assertNotIn("rename_check", json.dumps(checked))
        self.assertNotIn("rename_check", json.dumps(ready))

class SizeBreakdownTests(unittest.TestCase):
    def test_overflow_names_whether_changes_or_context_fill_the_packet(self):
        from ase.evidence.representation import size_breakdown
        files = [{"path": "a.py", "content": "x" * 10}, {"path": DIFF_PREFIX + "b.py", "content": "y" * 20},
                 {"path": "_ase_context/change_impact.json", "content": "z" * 300},
                 {"path": "_ase_context/contract.json", "content": "c" * 5}, {"path": "other.txt", "content": "o" * 7}]
        text = size_breakdown(files, ["a.py", "b.py"], 100)
        self.assertIn("changed files 30, context files 305 (largest: change_impact.json 300, contract.json 5), other 7", text)


if __name__ == "__main__":
    unittest.main()

