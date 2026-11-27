"""Local-bare publication safeguards; mocked gate is a protocol boundary only.

Positive transport tests do not claim semantic acceptance. Their sole test
double is GateService.gate; Git, hooks, source bytes, authorization and journal
persistence are real. Rejection of unavailable evidence uses the real gate.
"""
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from ase.discovery import discover_capabilities, record_discovery
from ase.gates import source_snapshot
import ase.publishing as publishing_module
from ase.publishing import Publisher
from ase.state import read_record, transaction, write_record
from ase.store import Store, now
from ase.tasks import TaskService


class PublishingV2Tests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="ase-publish-v2-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.repo = self.base / "repo"
        self.repo.mkdir()
        self.git("init", "-b", "feature/publishing")
        self.git("config", "user.name", "DisposablePublishing")
        self.git("config", "user.email", "fixture@example.invalid")
        (self.repo / "app.py").write_text("value = 1\n", encoding="utf-8")
        self.git("add", "app.py")
        self.git("commit", "-m", "Seed fixture")
        self.remote = self.base / "remote.git"
        self.git("init", "--bare", str(self.remote))
        self.git("remote", "add", "origin", str(self.remote))
        self.store = Store(self.repo, self.base / "state")
        self.addCleanup(self.store.close)
        self.tasks = TaskService(self.store)
        scope = {"resources": ["app.py"], "effects": ["file_write"]}
        task = self.tasks.create({"id": "publish", "schema_version": 2, "profile": "software", "risk": "LOW",
            "request": "Change the disposable app value and publish only to an explicitly authorized remote.",
            "scope": scope, "acceptance": [{"id": "AC1", "expected": "The disposable app has the requested value.", "evidence_ids": ["tests"]}],
            "evidence": [{"id": "tests", "kind": "test", "parser": "unittest", "argv": [sys.executable, "-m", "unittest"]}],
            "stages": [{"id": "change", "scope": scope, "acceptance_ids": ["AC1"], "depends_on": []}], "unknowns": []})
        task = self.tasks.propose("publish", {"summary": "Apply the approved disposable source change", "stage_ids": ["change"]}, expected_revision=task["revision"])
        self.tasks.approve("publish", proposal_hash=task["proposal_hash"], user_message_ref="test:approved-disposable-fixture", approved_stage_ids=["change"])
        baseline = {**source_snapshot(self.store, self.tasks.snapshot("publish")["contract"]), "dirty": ""}
        with transaction(self.store.db) as db:
            write_record(db, "baseline", "publish", baseline, expected_revision=0)
        self.publisher = Publisher(self.store)

    def git(self, *args, check=True):
        run = subprocess.run(["git", *args], cwd=self.repo, capture_output=True, text=True)
        if check:
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        return run.stdout.strip()

    def protocol_gate(self, *_args, **_kwargs):
        task = self.tasks.snapshot("publish")
        return {"status": "APPROVED", "reasons": [], "blocked_reasons": [], "rejected_reasons": [],
            "fingerprint": source_snapshot(self.store, task["contract"], task_id="publish")["fingerprint"],
            "contract_hash": task["contract_hash"], "proposal_hash": task["proposal_hash"],
            "plan_hash": self.tasks.execution_plan("publish")["plan_hash"]}

    def complete_fixture(self):
        with patch("ase.publishing.GateService.gate", side_effect=self.protocol_gate):
            task = self.tasks.snapshot("publish")
            self.tasks.transition("publish", "COMPLETED", expected_revision=task["revision"], reason="Protocol boundary fixture; not semantic acceptance")

    def remote_commit(self, remote=None):
        return self.git("--git-dir", str(remote or self.remote), "rev-parse", "--verify", "refs/heads/feature/publishing", check=False)

    def hook(self, name, body):
        path = self.repo / ".git/hooks" / name
        path.write_text("#!/bin/sh\n" + body + "\n", encoding="utf-8", newline="")
        path.chmod(0o755)

    def journal(self, kind):
        rows = self.store.db.execute("SELECT payload FROM runtime_events WHERE kind='journal'").fetchall()
        return [json.loads(row[0]) for row in rows if json.loads(row[0])["kind"] == kind]

    def refresh_baseline(self):
        previous = read_record(self.store.db, "baseline", "publish")
        baseline = {**source_snapshot(self.store, self.tasks.snapshot("publish")["contract"]), "dirty": ""}
        with transaction(self.store.db) as db:
            write_record(db, "baseline", "publish", baseline, expected_revision=previous["revision"])

    def revise_scope(self, resources, effects):
        task = self.tasks.snapshot("publish")
        contract = dict(task["contract"], scope={"resources": resources, "effects": effects})
        contract["stages"] = [{**contract["stages"][0], "scope": contract["scope"]}]
        task = self.tasks.revise("publish", contract, expected_revision=task["revision"])
        task = self.tasks.propose("publish", {"summary": "Scoped disposable publishing fixture", "stage_ids": ["change"]}, expected_revision=task["revision"])
        self.tasks.approve("publish", proposal_hash=task["proposal_hash"], user_message_ref="test:approved-disposable-scope", approved_stage_ids=["change"])

    def test_dry_run_does_not_commit_stage_or_push(self):
        before = self.git("rev-parse", "HEAD")
        (self.repo / "app.py").write_text("value = 2\n", encoding="utf-8")
        with patch("ase.publishing.GateService.gate", side_effect=self.protocol_gate):
            result = self.publisher.publish("publish", commit_message="Reviewed app change")
        self.assertEqual(result["status"], "APPROVED")
        self.assertTrue(result["dry_run"])
        self.assertFalse(result["published"])
        self.assertFalse(result["authorized"])
        self.assertEqual(self.git("rev-parse", "HEAD"), before)
        self.assertEqual(self.git("diff", "--cached", "--name-only"), "")
        self.assertFalse(self.remote_commit())
        self.assertFalse(self.journal("published"))

    def test_authorized_local_push_preserves_hooks_completed_state_and_real_proof(self):
        self.complete_fixture()
        self.publisher.authorize()
        markers = self.base / "hook-markers.txt"
        marker = shlex.quote(str(markers).replace("\\", "/"))
        self.hook("pre-commit", "printf 'commit\\n' >> " + marker)
        self.hook("pre-push", "printf 'push\\n' >> " + marker)
        (self.repo / "app.py").write_text("value = 2\n", encoding="utf-8")
        with patch("ase.publishing.GateService.gate", side_effect=self.protocol_gate):
            result = self.publisher.publish("publish", execute_push=True, commit_message="Reviewed app change")
        self.assertTrue(result["published"])
        self.assertEqual(result["status"], "APPROVED")
        self.assertEqual(result["post_push"]["status"], "PASS")
        self.assertEqual(result["post_push"]["head"], self.git("rev-parse", "HEAD"))
        self.assertEqual(result["post_push"]["branch"], "feature/publishing")
        self.assertEqual(result["post_push"]["dirty"], "")
        self.assertEqual(self.remote_commit(), self.git("rev-parse", "HEAD"))
        self.assertEqual(markers.read_text(encoding="utf-8").splitlines(), ["commit", "push"])
        self.assertEqual(self.tasks.snapshot("publish")["state"], "COMPLETED")
        self.assertEqual(len(self.journal("committed")), 1)
        event = self.journal("published")[0]
        self.assertTrue(any(self.remote_commit() in ref for ref in event["evidence_refs"]))
        proof_ref = next(ref for ref in event["evidence_refs"] if "#sha256=" in ref)
        path, digest = proof_ref.rsplit("#sha256=", 1)
        self.assertEqual(hashlib.sha256(Path(path).read_bytes()).hexdigest(), digest)
        proof = json.loads(Path(path).read_bytes())
        self.assertEqual(proof["commit"], self.remote_commit())
        self.assertEqual(proof["remote_commit"], proof["commit"])

    def test_unavailable_real_gate_legacy_and_nonsoftware_tasks_cannot_publish(self):
        self.publisher.authorize()
        result = self.publisher.publish("publish", execute_push=True)
        self.assertEqual(result["status"], "BLOCKED")
        self.assertFalse(result["published"])
        contract = self.tasks.snapshot("publish")["contract"]
        self.tasks.create({**contract, "id": "document", "profile": "document"})
        with self.store.db:
            self.store.db.execute("INSERT INTO tasks VALUES(?,?,?,?,?,?)", ("legacy", "Old task", "APPROVED", "{}", now(), now()))
        for task_id in ("document", "legacy"):
            with self.subTest(task_id=task_id):
                self.assertFalse(self.publisher.publish(task_id, execute_push=True)["published"])
        self.assertFalse(self.remote_commit())

    def test_main_other_branches_and_detached_head_are_rejected(self):
        for branch in ("main", "release/test"):
            self.git("checkout", "-B", branch)
            with self.subTest(branch=branch), patch("ase.publishing.GateService.gate", side_effect=self.protocol_gate):
                self.assertEqual(self.publisher.publish("publish")["status"], "REJECTED")
        self.git("checkout", "--detach")
        with patch("ase.publishing.GateService.gate", side_effect=self.protocol_gate):
            self.assertEqual(self.publisher.publish("publish")["status"], "REJECTED")
        self.assertFalse(self.remote_commit())

    def test_exact_single_url_authorization_can_be_revoked_and_cannot_follow_redirect(self):
        before = self.git("rev-parse", "HEAD")
        with patch("ase.publishing.GateService.gate", side_effect=self.protocol_gate):
            self.assertEqual(self.publisher.publish("publish", execute_push=True)["status"], "BLOCKED")
            self.publisher.authorize()
            self.publisher.authorize(disable=True)
            self.assertEqual(self.publisher.publish("publish", execute_push=True)["status"], "BLOCKED")
            self.publisher.authorize()
            other = self.base / "other.git"
            self.git("init", "--bare", str(other))
            self.git("remote", "set-url", "origin", str(other))
            self.assertEqual(self.publisher.publish("publish", execute_push=True)["status"], "BLOCKED")
            self.git("config", "--add", "remote.origin.pushurl", str(self.remote))
            self.git("config", "--add", "remote.origin.pushurl", str(other))
            with self.assertRaises(ValueError):
                self.publisher.authorize()
            (self.repo / "app.py").write_text("value = 2\n", encoding="utf-8")
            self.assertEqual(self.publisher.publish("publish", execute_push=True, commit_message="Reject invalid destination")["status"], "REJECTED")
        self.assertEqual(self.git("rev-parse", "HEAD"), before)
        self.assertEqual(self.git("diff", "--cached", "--name-only"), "")
        self.assertFalse(self.journal("committed"))
        self.assertFalse(self.journal("published"))
        self.assertFalse(self.remote_commit())
        self.assertFalse(self.remote_commit(other))

    def test_preexisting_or_unknown_dirty_baseline_never_stages_work(self):
        self.publisher.authorize()
        (self.repo / "app.py").write_text("value = 2\n", encoding="utf-8")
        for dirty in (" M app.py", None):
            record = read_record(self.store.db, "baseline", "publish")
            baseline = dict(record["payload"])
            if dirty is None:
                baseline.pop("dirty", None)
            else:
                baseline["dirty"] = dirty
            with transaction(self.store.db) as db:
                write_record(db, "baseline", "publish", baseline, expected_revision=record["revision"])
            with self.subTest(dirty=dirty), patch("ase.publishing.GateService.gate", side_effect=self.protocol_gate):
                self.assertEqual(self.publisher.publish("publish", execute_push=True, commit_message="Do not stage foreign work")["status"], "REJECTED")
            self.assertEqual(self.git("diff", "--cached", "--name-only"), "")
        self.assertFalse(self.remote_commit())

    def test_unrelated_untracked_file_is_not_staged_committed_or_published(self):
        self.publisher.authorize()
        before = self.git("rev-parse", "HEAD")
        (self.repo / "app.py").write_text("value = 2\n", encoding="utf-8")
        (self.repo / "unrelated-private.txt").write_text("Outside approved task scope\n", encoding="utf-8")
        with patch("ase.publishing.GateService.gate", side_effect=self.protocol_gate):
            result = self.publisher.publish("publish", execute_push=True, commit_message="Only the approved app change")
        self.assertEqual(result["status"], "REJECTED")
        self.assertFalse(result["published"])
        self.assertEqual(self.git("rev-parse", "HEAD"), before)
        self.assertEqual(self.git("diff", "--cached", "--name-only"), "")
        self.assertFalse(self.remote_commit())

    def test_untracked_bytecode_cache_is_neither_staged_nor_dirty(self):
        self.complete_fixture()
        self.publisher.authorize()
        cache = self.repo / "__pycache__" / "app.cpython-311.pyc"
        cache.parent.mkdir()
        cache.write_bytes(b"\x00bytecode")
        (self.repo / "app.py").write_text("value = 2\n", encoding="utf-8")
        with patch("ase.publishing.GateService.gate", side_effect=self.protocol_gate):
            result = self.publisher.publish("publish", execute_push=True, commit_message="Reviewed app change")
        self.assertTrue(result["published"], result)
        self.assertEqual(result["post_push"]["status"], "PASS")
        self.assertEqual(self.git("ls-tree", "-r", "--name-only", "HEAD").splitlines(), ["app.py"])
        self.assertTrue(cache.is_file())

    def test_outside_scope_deleted_file_is_preserved_as_uncommitted_work(self):
        self.publisher.authorize()
        outside = self.repo / "outside.txt"
        outside.write_text("Original unrelated file\n", encoding="utf-8")
        self.git("add", "outside.txt")
        self.git("commit", "-m", "Baseline unrelated fixture file")
        self.refresh_baseline()
        before = self.git("rev-parse", "HEAD")
        outside.unlink()
        (self.repo / "app.py").write_text("value = 2\n", encoding="utf-8")
        with patch("ase.publishing.GateService.gate", side_effect=self.protocol_gate):
            result = self.publisher.publish("publish", execute_push=True, commit_message="Approved app only")
        self.assertEqual(result["status"], "REJECTED")
        self.assertEqual(self.git("rev-parse", "HEAD"), before)
        self.assertEqual(self.git("diff", "--cached", "--name-only"), "")
        self.assertFalse(self.remote_commit())

    def test_outside_scope_index_change_is_not_discarded_when_worktree_matches_baseline(self):
        self.publisher.authorize()
        outside = self.repo / "outside.txt"
        outside.write_text("Original unrelated file\n", encoding="utf-8")
        self.git("add", "outside.txt")
        self.git("commit", "-m", "Baseline unrelated fixture file")
        self.refresh_baseline()
        before = self.git("rev-parse", "HEAD")
        outside.write_text("Staged user work\n", encoding="utf-8")
        self.git("add", "outside.txt")
        staged_before = self.git("diff", "--cached", "--binary")
        outside.write_text("Original unrelated file\n", encoding="utf-8")
        (self.repo / "app.py").write_text("value = 2\n", encoding="utf-8")
        with patch("ase.publishing.GateService.gate", side_effect=self.protocol_gate):
            result = self.publisher.publish("publish", execute_push=True, commit_message="Approved app only")
        self.assertEqual(result["status"], "REJECTED")
        self.assertEqual(self.git("diff", "--cached", "--binary"), staged_before)
        self.assertEqual(self.git("rev-parse", "HEAD"), before)
        self.assertFalse(self.remote_commit())

    def test_outside_scope_committed_history_cannot_leak_even_after_revert(self):
        self.publisher.authorize()
        outside = self.repo / "unrelated-private.txt"
        outside.write_text("Outside scope history must stay local\n", encoding="utf-8")
        self.git("add", "unrelated-private.txt")
        self.git("commit", "-m", "Unrelated local change")
        outside.unlink()
        self.git("add", "-A")
        self.git("commit", "-m", "Restore baseline tree")
        self.assertEqual(self.git("status", "--porcelain"), "")
        with patch("ase.publishing.GateService.gate", side_effect=self.protocol_gate):
            result = self.publisher.publish("publish", execute_push=True)
        self.assertEqual(result["status"], "REJECTED")
        self.assertFalse(self.remote_commit())

    def test_rewritten_head_without_baseline_ancestry_is_not_published(self):
        self.publisher.authorize()
        self.git("checkout", "--orphan", "feature/unrelated-history")
        self.git("commit", "-m", "Different history with identical source bytes")
        with patch("ase.publishing.GateService.gate", side_effect=self.protocol_gate):
            result = self.publisher.publish("publish", execute_push=True)
        self.assertEqual(result["status"], "REJECTED")
        self.assertFalse(result["published"])
        self.assertEqual(self.git("--git-dir", str(self.remote), "for-each-ref"), "")

    def test_source_changes_require_approved_file_write_effect(self):
        self.publisher.authorize()
        self.revise_scope(["app.py"], [])
        before = self.git("rev-parse", "HEAD")
        (self.repo / "app.py").write_text("value = 2\n", encoding="utf-8")
        with patch("ase.publishing.GateService.gate", side_effect=self.protocol_gate):
            result = self.publisher.publish("publish", execute_push=True, commit_message="Effect is not approved")
        self.assertEqual(result["status"], "REJECTED")
        self.assertEqual(self.git("rev-parse", "HEAD"), before)
        self.assertFalse(self.remote_commit())

    def test_approved_directory_alias_allows_only_its_descendants(self):
        self.publisher.authorize()
        (self.repo / "src").mkdir()
        (self.repo / "src/module.py").write_text("value = 1\n", encoding="utf-8")
        self.git("add", "src/module.py")
        self.git("commit", "-m", "Seed directory fixture")
        self.revise_scope(["./src/."], ["file_write"])
        self.refresh_baseline()
        (self.repo / "src/module.py").write_text("value = 2\n", encoding="utf-8")
        (self.repo / "src/helper.py").write_text("helper = True\n", encoding="utf-8")
        with patch("ase.publishing.GateService.gate", side_effect=self.protocol_gate):
            result = self.publisher.publish("publish", execute_push=True, commit_message="Approved directory changes")
        self.assertTrue(result["published"])
        self.assertIn("src/helper.py", self.git("--git-dir", str(self.remote), "ls-tree", "-r", "--name-only", "refs/heads/feature/publishing"))

    def test_ambient_git_environment_cannot_select_or_configure_another_repository(self):
        other = self.base / "foreign-repo"
        other.mkdir()
        self.git("-C", str(other), "init", "-b", "feature/foreign")
        self.git("-C", str(other), "config", "user.name", "ForeignFixture")
        self.git("-C", str(other), "config", "user.email", "foreign@example.invalid")
        (other / "private.txt").write_text("Foreign repository content\n", encoding="utf-8")
        self.git("-C", str(other), "add", "private.txt")
        self.git("-C", str(other), "commit", "-m", "Foreign fixture")
        other_remote = self.base / "foreign-remote.git"
        self.git("init", "--bare", str(other_remote))
        self.git("-C", str(other), "remote", "add", "origin", str(other_remote))
        expected = self.git("rev-parse", "HEAD")
        hostile = {"GIT_DIR": str(other / ".git"), "GIT_WORK_TREE": str(other),
                   "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "remote.origin.pushurl",
                   "GIT_CONFIG_VALUE_0": str(other_remote)}
        with patch.dict(os.environ, hostile), patch("ase.publishing.GateService.gate", side_effect=self.protocol_gate):
            self.publisher.authorize()
            result = self.publisher.publish("publish", execute_push=True)
        self.assertTrue(result["published"])
        self.assertEqual(self.remote_commit(), expected)
        self.assertEqual(self.git("--git-dir", str(other_remote), "for-each-ref"), "")
        authorization = read_record(self.store.db, "publishing_authorization", self.store.workspace_id + ":origin")["payload"]
        self.assertEqual(authorization["url"], str(self.remote))

    def test_follow_tags_configuration_cannot_publish_unapproved_annotated_tag(self):
        self.publisher.authorize()
        self.git("tag", "-a", "private-only", "-m", "Local tag outside the approved feature branch")
        self.git("config", "push.followTags", "true")
        with patch("ase.publishing.GateService.gate", side_effect=self.protocol_gate):
            result = self.publisher.publish("publish", execute_push=True)
        self.assertTrue(result["published"])
        self.assertTrue(self.remote_commit())
        self.assertEqual(self.git("--git-dir", str(self.remote), "show-ref", "--tags", check=False), "")

    def test_changed_secret_files_and_content_are_rejected_before_commit(self):
        self.publisher.authorize()
        before = self.git("rev-parse", "HEAD")
        for name, content in ((".env", "harmless fixture value"), ("app.py", "token=ghp_" + "x" * 40)):
            path = self.repo / name
            original = path.read_bytes() if path.exists() else None
            path.write_text(content, encoding="utf-8")
            with self.subTest(name=name), patch("ase.publishing.GateService.gate", side_effect=self.protocol_gate):
                self.assertEqual(self.publisher.publish("publish", execute_push=True, commit_message="Review source")["status"], "REJECTED")
            if original is None:
                path.unlink()
            else:
                path.write_bytes(original)
        self.assertEqual(self.git("rev-parse", "HEAD"), before)
        self.assertFalse(self.remote_commit())

    def test_commit_hook_source_drift_blocks_push(self):
        self.publisher.authorize()
        (self.repo / "app.py").write_text("value = 2\n", encoding="utf-8")
        self.hook("post-commit", "printf 'value = 99\\n' > app.py")
        with patch("ase.publishing.GateService.gate", side_effect=self.protocol_gate):
            result = self.publisher.publish("publish", execute_push=True, commit_message="Reviewed app change")
        self.assertEqual(result["status"], "REJECTED")
        self.assertFalse(result["published"])
        self.assertFalse(self.remote_commit())
        self.assertEqual(len(self.journal("committed")), 1)

    def test_commit_hook_remote_or_branch_change_blocks_push(self):
        self.publisher.authorize()
        other = self.base / "hook-redirect.git"
        self.git("init", "--bare", str(other))
        (self.repo / "app.py").write_text("value = 2\n", encoding="utf-8")
        self.hook("post-commit", "git remote set-url origin " + shlex.quote(str(other).replace("\\", "/")))
        with patch("ase.publishing.GateService.gate", side_effect=self.protocol_gate):
            result = self.publisher.publish("publish", execute_push=True, commit_message="Reviewed app change")
        self.assertFalse(result["published"])
        self.assertFalse(self.remote_commit())
        self.assertFalse(self.remote_commit(other))
        self.git("remote", "set-url", "origin", str(self.remote))
        self.hook("post-commit", "git checkout -b bugfix/hook-switch")
        (self.repo / "app.py").write_text("value = 3\n", encoding="utf-8")
        with patch("ase.publishing.GateService.gate", side_effect=self.protocol_gate):
            result = self.publisher.publish("publish", execute_push=True, commit_message="Another reviewed change")
        self.assertFalse(result["published"])
        self.assertFalse(self.remote_commit())

    def test_fresh_gate_and_existing_pre_push_hook_each_block_actual_push(self):
        self.publisher.authorize()
        calls = []
        def gate(*args, **kwargs):
            calls.append(1)
            if len(calls) > 1:
                return {"status": "BLOCKED", "reasons": ["Required current verification unavailable"], "blocked_reasons": ["Required current verification unavailable"]}
            return self.protocol_gate()
        with patch("ase.publishing.GateService.gate", side_effect=gate):
            result = self.publisher.publish("publish", execute_push=True)
        self.assertFalse(result["published"])
        self.assertGreaterEqual(len(calls), 2)
        self.hook("pre-push", "exit 42")
        with patch("ase.publishing.GateService.gate", side_effect=self.protocol_gate):
            result = self.publisher.publish("publish", execute_push=True)
        self.assertFalse(result["published"])
        self.assertFalse(self.remote_commit())
        self.assertFalse(self.journal("published"))

    def assert_confirmed_push_with_unmet_postconditions(self, result):
        self.assertTrue(result["published"], result)
        self.assertEqual(result["remote_commit"], self.remote_commit())
        self.assertEqual(result["commit"], result["remote_commit"])
        self.assertEqual(result["status"], "BLOCKED", result)
        self.assertTrue(result["authority_current"])
        self.assertEqual(result["post_push"]["status"], "BLOCKED")
        self.assertTrue(result["post_push"]["reasons"])
        proof = Path(result["proof"]["path"])
        self.assertEqual(hashlib.sha256(proof.read_bytes()).hexdigest(), result["proof"]["sha256"])
        saved = json.loads(proof.read_bytes())
        self.assertEqual(saved["post_push"], result["post_push"])
        self.assertEqual(saved["remote_commit"], self.remote_commit())
        self.assertEqual(len(self.journal("published")), 1)

    def test_pre_push_hook_committed_drift_preserves_publication_but_blocks_completion(self):
        self.publisher.authorize()
        (self.repo / "app.py").write_text("value = 2\n", encoding="utf-8")
        self.hook("pre-push", "printf 'value = 3\\n' > app.py\ngit add app.py\ngit commit -m 'Hook generated change'")
        with patch("ase.publishing.GateService.gate", side_effect=self.protocol_gate):
            result = self.publisher.publish("publish", execute_push=True, commit_message="Reviewed source")
        self.assertNotEqual(self.git("rev-parse", "HEAD"), result["remote_commit"])
        self.assert_confirmed_push_with_unmet_postconditions(result)
        self.assertEqual(result["post_push"]["head"], self.git("rev-parse", "HEAD"))
        self.assertEqual((self.repo / "app.py").read_text(), "value = 3\n")

    def test_pre_push_hook_branch_switch_is_recorded_after_confirmed_push(self):
        self.publisher.authorize()
        self.hook("pre-push", "git checkout -b bugfix/hook-local")
        with patch("ase.publishing.GateService.gate", side_effect=self.protocol_gate):
            result = self.publisher.publish("publish", execute_push=True)
        self.assert_confirmed_push_with_unmet_postconditions(result)
        self.assertEqual(result["post_push"]["branch"], "bugfix/hook-local")
        self.assertEqual(result["post_push"]["head"], result["remote_commit"])

    def test_pre_push_hook_dirty_source_is_recorded_after_confirmed_push(self):
        self.publisher.authorize()
        self.hook("pre-push", "printf 'value = 3\\n' > app.py")
        with patch("ase.publishing.GateService.gate", side_effect=self.protocol_gate):
            result = self.publisher.publish("publish", execute_push=True)
        self.assert_confirmed_push_with_unmet_postconditions(result)
        self.assertTrue(result["post_push"]["dirty"])
        self.assertEqual(result["post_push"]["head"], result["remote_commit"])

    def test_pre_push_hook_ignored_input_drift_is_not_mistaken_for_current_approval(self):
        self.publisher.authorize()
        (self.repo / ".gitignore").write_text("observations/\n", encoding="utf-8")
        self.git("add", ".gitignore")
        self.git("commit", "-m", "Ignore read-only observations")
        self.refresh_baseline()
        dependency = self.repo / "observations/current.txt"
        dependency.parent.mkdir()
        dependency.write_text("Original input\n", encoding="utf-8")
        discovery = discover_capabilities(self.store.project, scope={"resources": ["app.py"],
            "dependencies": [{"path": "observations/current.txt", "reason": "Required read-only input"}]}, backend_config={})
        record_discovery(self.store, "publish", discovery, expected_revision=0)
        before = source_snapshot(self.store, self.tasks.snapshot("publish")["contract"], task_id="publish")["fingerprint"]
        self.hook("pre-push", "printf 'Changed input\\n' > observations/current.txt")
        with patch("ase.publishing.GateService.gate", side_effect=self.protocol_gate):
            result = self.publisher.publish("publish", execute_push=True)
        self.assert_confirmed_push_with_unmet_postconditions(result)
        self.assertEqual(result["post_push"]["dirty"], "")
        self.assertEqual(result["post_push"]["head"], result["remote_commit"])
        self.assertNotEqual(result["post_push"]["fingerprint"], before)

    def test_unavailable_local_postcondition_does_not_erase_confirmed_publication(self):
        self.publisher.authorize()
        actual_execute, actual_git = publishing_module.execute, self.publisher._git
        confirmed = []
        def execute(argv, *args, **kwargs):
            result = actual_execute(argv, *args, **kwargs)
            if argv[5] == "ls-remote" and result["exit_code"] == 0:
                confirmed.append(True)
            return result
        def git(*args, **kwargs):
            if confirmed and args == ("rev-parse", "HEAD"):
                raise ValueError("Local HEAD observation unavailable after push")
            return actual_git(*args, **kwargs)
        with patch("ase.publishing.GateService.gate", side_effect=self.protocol_gate), \
                patch("ase.publishing.execute", side_effect=execute), patch.object(self.publisher, "_git", side_effect=git):
            result = self.publisher.publish("publish", execute_push=True)
        self.assert_confirmed_push_with_unmet_postconditions(result)
        self.assertIsNone(result["post_push"]["head"])
        self.assertTrue(any("HEAD" in reason for reason in result["post_push"]["reasons"]))


if __name__ == "__main__":
    unittest.main()
