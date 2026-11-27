"""Disposable source classification and real Git publication ownership checks.

Candidate credential-shaped strings are assembled only in temporary files.
History transport tests use an explicit protocol gate double, real Git commits,
hooks and local bare remotes; they do not claim independent semantic acceptance.
"""
import hashlib
import copy
from contextlib import closing, contextmanager
import os
import shlex
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from ase.publishing import Publisher
from ase.gates import source_snapshot
from ase.discovery import discover_capabilities, record_discovery
from ase.state import read_record
from ase.core import SECRET
from ase.store import Store
from ase.tasks import TaskService
from ase.workflow import WorkflowController
import ase.publishing as publishing_module
import test_publishing_v2 as publishing_fixtures


class PythonPublicationScanTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="ase-publication-source-")
        self.addCleanup(temporary.cleanup)
        self.repo = Path(temporary.name)
        self.publisher = Publisher(SimpleNamespace(repo=self.repo))

    @staticmethod
    def assignment(value):
        return "to" + "ken = " + value + "\n"

    def inspect(self, content, *, name="candidate.py", before=None):
        path = self.repo / name
        path.write_text(content, encoding="utf-8")
        baseline = {"files": {} if before is None else {name: hashlib.sha256(before.encode()).hexdigest()}}
        current = {"files": {name: hashlib.sha256(path.read_bytes()).hexdigest()}}
        return self.publisher._secret_check(baseline, current)

    def test_runtime_lookup_and_encoding_expressions_do_not_embed_credentials(self):
        expressions = ["vault_value", "provider.current_credential", "vault['long_lookup_field_name']",
            "provider.fetch_credential()", "os.getenv('LONG_CREDENTIAL_FIELD_NAME')",
            "base64.b64encode((profile['secrets']['username'] + ':' + profile['secrets']['password']).encode()).decode()"]
        for expression in expressions:
            with self.subTest(expression=expression):
                self.assertIsNone(self.inspect(self.assignment(expression)))

    def test_condition_colon_is_not_a_literal_binding(self):
        code = "if values and sec" + "ret:\n    values.update(runtime_values)\n"
        self.assertIsNone(self.inspect(code))

    def test_condition_match_does_not_hide_a_following_literal_binding(self):
        code = "if sec" + "ret:\n    " + self.assignment(repr("disposable-opaque-credential-value"))
        self.assertIsNotNone(self.inspect(code))

    def test_lookup_key_metadata_does_not_exempt_literals_inside_unknown_calls(self):
        expression = "vault[transform(" + repr("disposable-opaque-credential-value") + ")]"
        self.assertIsNotNone(self.inspect(self.assignment(expression)))

    def test_unicode_columns_preserve_binding_classification(self):
        self.assertIsNone(self.inspect("métadonnée = 1; " + self.assignment("runtime_credential")))
        self.assertIsNotNone(self.inspect("métadonnée = 1; " + self.assignment(repr("disposable-opaque-credential-value"))))

    def test_current_detector_definitions_remain_publishable_python(self):
        root = Path(__file__).resolve().parents[1]
        for name in ("ase/access.py", "ase/core.py", "ase/discovery.py"):
            with self.subTest(path=name):
                self.assertIsNone(self.inspect((root / name).read_text(encoding="utf-8")))

    def test_literal_values_and_literals_wrapped_in_unknown_calls_are_rejected(self):
        credential = "disposable-opaque-credential-value"
        expressions = [repr(credential), "str(" + repr(credential) + ")",
            "transform(runtime_value, " + repr(credential) + ")", "1234567890123",
            "re.compile(" + repr(credential) + ")", "re.compile(" + repr(credential + "[0-9]+") + ")"]
        for expression in expressions:
            with self.subTest(expression_type=expression.split("(", 1)[0]):
                self.assertIsNotNone(self.inspect(self.assignment(expression)))

    def test_constant_built_payloads_do_not_gain_dynamic_expression_exemption(self):
        expressions = ["''.join(" + repr(["gh" + "p_"] + ["xxxx"] * 5) + ")",
            "''.join(" + repr(["abcd", "efgh", "ijkl", "mnop"]) + ")",
            "bytes(" + repr(list(range(97, 109))) + ").decode()",
            "assemble(" + ", ".join(map(repr, ["abcd", "efgh", "ijkl", "mnop"])) + ")"]
        for index, expression in enumerate(expressions):
            content = self.assignment(expression)
            with self.subTest(example=index):
                self.assertIsNotNone(SECRET.search(content), "Regression must preserve a raw-positive candidate")
                self.assertIsNotNone(self.inspect(content))

    def test_literal_binding_is_not_exempt_when_baseline_line_was_identical(self):
        before = self.assignment(repr("disposable-opaque-credential-value"))
        self.assertIsNotNone(self.inspect(before + "unrelated = 1\n", before=before))
        self.assertIsNotNone(self.inspect(before + before, before=before))

    def test_comments_strings_and_known_prefixes_remain_rejected(self):
        prefix_value = "gh" + "p_" + "x" * 40
        assignments = self.assignment(repr("disposable-opaque-credential-value"))
        examples = ["# " + assignments, "text = " + repr(assignments) + "\n",
            "text = " + repr(prefix_value) + "\n", "# " + prefix_value + "\n",
            self.assignment("lookup(" + repr(prefix_value) + ")")]
        for index, content in enumerate(examples):
            with self.subTest(example=index):
                self.assertIsNotNone(self.inspect(content))

    def test_python_parse_failure_keeps_raw_detection(self):
        self.assertIsNotNone(self.inspect(self.assignment("runtime_credential(")))
        self.assertIsNotNone(self.inspect(self.assignment("'''unterminated")))

    def test_non_python_raw_detection_and_private_file_guards_remain(self):
        self.assertIsNotNone(self.inspect(self.assignment("runtime_credential()"), name="config.txt"))
        for name in (".env", "id_rsa", "id_ed25519", "client.pem", "client.key", "client.p12", "client.pfx"):
            with self.subTest(path=name):
                self.assertIn("Potential credential file", self.inspect("ordinary text\n", name=name))

    def test_hardlinked_source_is_still_rejected(self):
        original = self.repo / "original.py"
        original.write_text("value = 1\n")
        linked = self.repo / "linked.py"
        os.link(original, linked)
        self.assertIn("redirected or shared", self.publisher._secret_check({"files": {}}, {"files": {"linked.py": "changed"}}))

    def test_unchanged_link_target_allows_reviewed_target_content_change(self):
        # Windows without link privilege uses only a filesystem metadata seam;
        # secret classification and before/current fingerprint logic stay real.
        link = self.repo / "helper-link"
        link.write_text("value = 2\n", encoding="utf-8")
        before = {"files": {"helper-link": "link:app.py:" + "a" * 64}}
        current = {"files": {"helper-link": "link:app.py:" + "b" * 64}}
        with patch.object(Path, "is_symlink", lambda path: path == link), patch("ase.publishing.os.readlink", return_value="app.py"):
            self.assertIsNone(self.publisher._secret_check(before, current))

    def test_new_or_retargeted_links_remain_rejected(self):
        link = self.repo / "helper-link"
        link.write_text("value = 2\n", encoding="utf-8")
        current = {"files": {"helper-link": "link:app.py:" + "b" * 64}}
        for prior in (None, "link:other.py:" + "b" * 64, "ordinary-file-hash"):
            before = {"files": {} if prior is None else {"helper-link": prior}}
            with self.subTest(prior=prior), patch.object(Path, "is_symlink", lambda path: path == link), patch("ase.publishing.os.readlink", return_value="app.py"):
                self.assertIn("redirected or shared", self.publisher._secret_check(before, current))

    def test_link_changed_after_snapshot_remains_rejected(self):
        link = self.repo / "helper-link"
        link.write_text("value = 2\n", encoding="utf-8")
        before = {"files": {"helper-link": "link:app.py:" + "a" * 64}}
        current = {"files": {"helper-link": "link:app.py:" + "b" * 64}}
        with patch.object(Path, "is_symlink", lambda path: path == link), patch("ase.publishing.os.readlink", return_value="other.py"):
            self.assertIn("redirected or shared", self.publisher._secret_check(before, current))

    def test_this_test_module_has_no_embedded_candidate_payload(self):
        self.assertIsNone(self.inspect(Path(__file__).read_text(encoding="utf-8")))


class ObservedInputPublicationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = publishing_fixtures.PublishingV2Tests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def test_ignored_observation_changes_do_not_become_publication_paths(self):
        fixture = self.fixture
        (fixture.repo / ".gitignore").write_text("observations/\n")
        fixture.git("add", ".gitignore")
        fixture.git("commit", "-m", "Ignore read-only fixture observations")
        fixture.refresh_baseline()
        task = fixture.tasks.snapshot("publish")
        baseline = read_record(fixture.store.db, "baseline", "publish")["payload"]
        observation = fixture.repo / "observations/current.txt"
        observation.parent.mkdir()
        observation.write_text("gh" + "p_" + "x" * 40)
        (fixture.repo / "app.py").write_text("value = 2\n")
        discovery = discover_capabilities(fixture.store.project, scope={"resources": ["app.py"],
            "dependencies": [{"path": "observations/current.txt", "reason": "Read-only fixture observation"}]}, backend_config={})
        record_discovery(fixture.store, "publish", discovery, expected_revision=0)
        current = source_snapshot(fixture.store, task["contract"], task_id="publish")
        self.assertIn("observations/current.txt", current["files"])
        paths = fixture.publisher._publication_paths(task, baseline, current)
        self.assertEqual(paths, ["app.py"])
        self.assertIsNone(fixture.publisher._secret_check(baseline, current))
        self.assertEqual(fixture.git("diff", "--cached", "--name-only"), "")

    def test_forced_index_entry_is_checked_even_when_git_ignored(self):
        fixture = self.fixture
        (fixture.repo / ".gitignore").write_text("observations/\n")
        fixture.git("add", ".gitignore")
        fixture.git("commit", "-m", "Ignore fixture observations")
        fixture.refresh_baseline()
        observation = fixture.repo / "observations/private.txt"
        observation.parent.mkdir()
        observation.write_text("Must not publish outside scope\n")
        fixture.git("add", "-f", "observations/private.txt")
        task = fixture.tasks.snapshot("publish")
        baseline = read_record(fixture.store.db, "baseline", "publish")["payload"]
        with self.assertRaisesRegex(ValueError, "outside approved publication scope"):
            fixture.publisher._publication_paths(task, baseline, source_snapshot(fixture.store, task["contract"]))

    def test_rename_checks_both_deleted_source_and_new_destination(self):
        fixture = self.fixture
        fixture.git("mv", "app.py", "outside.py")
        task = fixture.tasks.snapshot("publish")
        baseline = read_record(fixture.store.db, "baseline", "publish")["payload"]
        with self.assertRaisesRegex(ValueError, "outside approved publication scope"):
            fixture.publisher._publication_paths(task, baseline, source_snapshot(fixture.store, task["contract"]))


class PublishedHistorySecretTests(unittest.TestCase):
    def setUp(self):
        self.fixture = publishing_fixtures.PublishingV2Tests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.publisher.authorize()

    @staticmethod
    def canary():
        return PythonPublicationScanTests.assignment(repr("gh" + "p_" + "x" * 40))

    def commit(self, content, name="app.py"):
        fixture = self.fixture
        path = fixture.repo / name
        if content is None:
            path.unlink()
        else:
            path.write_text(content, encoding="utf-8")
        fixture.git("add", "--", name)
        fixture.git("commit", "-m", "Disposable history inspection fixture")
        return fixture.git("rev-parse", "HEAD")

    def publish(self, **kwargs):
        fixture = self.fixture
        # Real Git publication boundary only; no semantic approval is inferred.
        with patch("ase.publishing.GateService.gate", side_effect=fixture.protocol_gate):
            return fixture.publisher.publish("publish", execute_push=True, **kwargs)

    def assert_rejected_before_push(self, **kwargs):
        result = self.publish(**kwargs)
        self.assertEqual(result["status"], "REJECTED", result)
        self.assertFalse(result["published"])
        self.assertFalse(self.fixture.remote_commit())
        self.assertNotIn("gh" + "p_" + "x" * 40, str(result))
        self.assertIn("history", result["reasons"][0].lower())
        return result

    def test_literal_introduced_then_restored_to_baseline_is_rejected_before_push(self):
        self.commit(self.canary())
        self.commit("value = 1\n")
        self.assert_rejected_before_push()

    def test_literal_introduced_then_file_deleted_is_rejected_before_push(self):
        self.fixture.revise_scope(["app.py", "temporary.py"], ["file_write"])
        self.commit(self.canary(), "temporary.py")
        self.commit(None, "temporary.py")
        self.assert_rejected_before_push()

    def test_merged_side_branch_transient_literal_is_rejected_before_push(self):
        fixture = self.fixture
        fixture.git("checkout", "-b", "feature/disposable-side")
        self.commit(self.canary())
        self.commit("value = 1\n")
        fixture.git("checkout", "feature/publishing")
        self.commit("value = 2\n")
        fixture.git("merge", "--no-ff", "feature/disposable-side", "-m", "Merge clean final side tree")
        self.assert_rejected_before_push()

    def test_literal_only_in_merge_resolution_is_rejected_after_later_removal(self):
        fixture = self.fixture
        fixture.git("checkout", "-b", "feature/disposable-side")
        self.commit("value = 3\n")
        fixture.git("checkout", "feature/publishing")
        self.commit("value = 2\n")
        fixture.git("merge", "--no-ff", "feature/disposable-side", check=False)
        merge = self.commit(self.canary())
        self.assertEqual(len(fixture.git("rev-list", "--parents", "-n", "1", merge).split()), 3)
        self.commit("value = 4\n")
        self.assert_rejected_before_push()

    def test_clean_and_dynamic_python_metadata_history_is_publishable(self):
        self.commit(PythonPublicationScanTests.assignment("vault['long_lookup_field_name']"))
        self.commit(PythonPublicationScanTests.assignment("base64.b64encode((profile['secrets']['username'] + ':' + profile['secrets']['password']).encode()).decode()"))
        self.commit("value = 2\n")
        result = self.publish()
        self.assertTrue(result["published"], result)
        self.assertEqual(self.fixture.remote_commit(), self.fixture.git("rev-parse", "HEAD"))

    def test_legacy_credential_before_baseline_is_not_retroactively_scanned(self):
        self.commit(self.canary(), "legacy.py")
        self.fixture.refresh_baseline()
        self.commit("value = 2\n")
        result = self.publish()
        self.assertTrue(result["published"], result)

    def test_copy_of_baseline_blob_to_new_deleted_path_is_still_introduced(self):
        self.commit(self.canary(), "legacy.py")
        self.fixture.refresh_baseline()
        self.fixture.revise_scope(["app.py", "copy.py"], ["file_write"])
        self.commit(self.canary(), "copy.py")
        self.commit(None, "copy.py")
        self.assert_rejected_before_push()

    def test_transient_private_credential_path_is_rejected(self):
        self.fixture.revise_scope(["app.py", ".env"], ["file_write"])
        self.commit("ordinary disposable text\n", ".env")
        self.commit(None, ".env")
        result = self.assert_rejected_before_push()
        self.assertIn("credential file", result["reasons"][0])

    def test_large_transient_blob_is_rejected_without_silent_scan_omission(self):
        self.commit("x" * 2_000_001)
        self.commit("value = 1\n")
        result = self.assert_rejected_before_push()
        self.assertIn("bound", result["reasons"][0].lower())

    def test_binary_transient_blob_is_rejected_without_silent_scan_omission(self):
        self.commit("disposable binary\0payload")
        self.commit("value = 1\n")
        result = self.assert_rejected_before_push()
        self.assertIn("binary", result["reasons"][0].lower())

    def test_post_commit_hook_transient_history_is_checked_before_push(self):
        fixture = self.fixture
        quoted = self.canary().replace("'", "'\"'\"'")
        fixture.hook("post-commit", """test -f .git/history-hook-ran && exit 0
touch .git/history-hook-ran
cp app.py .git/reviewed-app
printf '%s' '""" + quoted + """' > app.py
git add app.py
git commit -m 'Transient local hook content'
cp .git/reviewed-app app.py
git add app.py
git commit -m 'Restore reviewed hook tree'""")
        (fixture.repo / "app.py").write_text("value = 2\n", encoding="utf-8")
        self.assert_rejected_before_push(commit_message="Reviewed disposable change")


class PublicationAuthorityTests(unittest.TestCase):
    """Actual Git/processes and ordinary concurrent API writes; gate seam only."""
    def setUp(self):
        self.fixture = publishing_fixtures.PublishingV2Tests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.publisher.authorize()
        self.before = self.fixture.git("rev-parse", "HEAD")
        self.task_id = "publish"
        (self.fixture.repo / "app.py").write_text("value = 2\n", encoding="utf-8")

    def protocol_gate(self, task_id, *_args, **_kwargs):
        task = self.fixture.tasks.snapshot(task_id)
        return {"status": "APPROVED", "reasons": [], "blocked_reasons": [], "rejected_reasons": [],
            "fingerprint": source_snapshot(self.fixture.store, task["contract"], task_id=task_id)["fingerprint"],
            "contract_hash": task["contract_hash"], "proposal_hash": task["proposal_hash"],
            "plan_hash": self.fixture.tasks.execution_plan(task_id)["plan_hash"]}

    def concurrent_change(self, kind, actor="publish"):
        errors = []
        def change():
            try:
                with closing(Store(self.fixture.repo, self.fixture.base / "state")) as store:
                    tasks = TaskService(store)
                    task = tasks.snapshot(actor)
                    if kind == "cancel":
                        tasks.transition(actor, "CANCELLED", expected_revision=task["revision"], reason="Disposable ordinary cancellation")
                    elif kind in {"stop", "stop_resume"}:
                        controller = WorkflowController(store)
                        controller.request_stop(actor, reason="Disposable ordinary stop")
                        if kind == "stop_resume":
                            # No discovery exists, so public resume clears the
                            # stop and blocks before any backend call.
                            self.assertEqual(controller.run(actor, resume=True)["status"], "BLOCKED")
                            self.assertIsNone(read_record(store.db, "stop", actor)["payload"])
                    elif kind == "reapprove":
                        task = tasks.propose(actor, copy.deepcopy(task["proposal"]), expected_revision=task["revision"])
                        tasks.approve(actor, proposal_hash=task["proposal_hash"], user_message_ref="test:replacement-grant",
                            approved_stage_ids=task["proposal"]["stage_ids"])
                    elif kind == "remote_regrant":
                        publisher = Publisher(store)
                        publisher.authorize(disable=True)
                        publisher.authorize()
                    else:
                        raise AssertionError("Unknown test change")
            except BaseException as error:
                errors.append(error)
        worker = threading.Thread(target=change)
        worker.start()
        worker.join(25)
        self.assertFalse(worker.is_alive(), "Ordinary authority write must not wait for a Git process")
        if errors:
            raise errors[0]

    def publish(self):
        with patch("ase.publishing.GateService.gate", side_effect=self.protocol_gate):
            return self.fixture.publisher.publish(self.task_id, execute_push=True, commit_message="Reviewed authority fixture")

    def assert_held(self, result, *, committed=False):
        self.assertFalse(result["published"], result)
        self.assertIn(result["status"], {"BLOCKED", "REJECTED"})
        self.assertFalse(self.fixture.remote_commit())
        self.assertEqual(self.fixture.git("rev-parse", "HEAD") == self.before, not committed)

    def at_command_launch(self, command, change, *, actor="publish"):
        actual = publishing_module.execute
        called = []
        def execute(argv, *args, **kwargs):
            if argv[5] == command and not called:
                called.append(command)
                original_guard = kwargs.get("launch_guard")
                @contextmanager
                def boundary():
                    self.concurrent_change(change, actor)
                    if original_guard is None:
                        yield
                    else:
                        with original_guard():
                            yield
                kwargs["launch_guard"] = boundary
            return actual(argv, *args, **kwargs)
        with patch("ase.publishing.execute", side_effect=execute):
            result = self.publish()
        self.assertEqual(called, [command], result)
        return result

    def at_lock_entry(self, change, *, actor="publish"):
        actual = publishing_module._exclusive
        @contextmanager
        def boundary(path):
            # _exclusive is nonblocking; this models the actual gap between
            # the entry snapshot and acquiring the real execution lock.
            self.concurrent_change(change, actor)
            with actual(path):
                yield
        with patch("ase.publishing._exclusive", side_effect=boundary):
            return self.publish()

    def test_cancel_between_entry_snapshot_and_lock_blocks_commit_and_push(self):
        self.assert_held(self.at_lock_entry("cancel"))

    def test_transient_stop_and_public_resume_cannot_rebind_waiting_publication(self):
        self.assert_held(self.at_lock_entry("stop_resume"))

    def test_stop_at_add_launch_blocks_index_mutation(self):
        self.assert_held(self.at_command_launch("add", "stop"))
        self.assertEqual(self.fixture.git("diff", "--cached", "--name-only"), "")

    def test_cancel_at_commit_launch_preserves_head_and_blocks_push(self):
        self.assert_held(self.at_command_launch("commit", "cancel"))

    def test_stop_at_push_launch_preserves_actual_local_commit_without_push(self):
        self.assert_held(self.at_command_launch("push", "stop"), committed=True)

    def test_same_hash_reapproval_does_not_rebind_original_publication(self):
        self.assert_held(self.at_command_launch("push", "reapprove"), committed=True)

    def test_exact_url_remote_authorization_regrant_does_not_rebind_old_batch(self):
        self.assert_held(self.at_command_launch("push", "remote_regrant"), committed=True)

    def test_ancestor_stop_at_child_push_launch_is_enforced(self):
        parent = self.fixture.tasks.snapshot("publish")
        child = copy.deepcopy(parent["contract"])
        child.update(id="delegate", parent_task_id="publish", parent_stage_id="change")
        self.fixture.tasks.create(child)
        WorkflowController(self.fixture.store)._save("baseline", "delegate", read_record(self.fixture.store.db, "baseline", "publish")["payload"])
        self.task_id = "delegate"
        self.assert_held(self.at_command_launch("push", "stop", actor="publish"), committed=True)

    def test_stop_after_actual_push_keeps_readonly_remote_confirmation_truthful(self):
        actual = publishing_module.execute
        def execute(argv, *args, **kwargs):
            receipt = actual(argv, *args, **kwargs)
            if argv[5] == "push":
                self.assertEqual(receipt["exit_code"], 0)
                self.concurrent_change("stop")
            return receipt
        with patch("ase.publishing.execute", side_effect=execute):
            result = self.publish()
        self.assertTrue(result["published"], result)
        self.assertEqual(result["remote_commit"], self.fixture.remote_commit())
        self.assertEqual(result["status"], "BLOCKED")

    def test_ordinary_stop_cancels_real_git_during_precommit_hook(self):
        fixture = self.fixture
        started, release = fixture.repo / ".git/hook-started", fixture.repo / ".git/hook-release"
        fixture.hook("pre-commit", "touch .git/hook-started\nwhile test ! -f .git/hook-release; do sleep .05; done")
        cancellation_seen = threading.Event()
        errors = []
        def stop_when_hook_runs():
            try:
                for _ in range(500):
                    if started.exists():
                        break
                    if cancellation_seen.wait(.05):
                        break
                self.assertTrue(started.exists(), "Actual pre-commit hook must have started")
                self.concurrent_change("stop")
            except BaseException as error:
                errors.append(error)
            finally:
                # Old code has no cancellation callback: release its real hook
                # after the control fails so RED never hangs for five minutes.
                if not cancellation_seen.wait(3):
                    release.touch()
        worker = threading.Thread(target=stop_when_hook_runs)
        worker.start()
        actual = publishing_module.execute
        def execute(argv, *args, **kwargs):
            cancel = kwargs.get("cancel_requested")
            def observed_cancel():
                value = cancel() if cancel else False
                if value:
                    cancellation_seen.set()
                return value
            kwargs["cancel_requested"] = observed_cancel
            return actual(argv, *args, **kwargs)
        with patch("ase.publishing.execute", side_effect=execute):
            result = self.publish()
        worker.join(30)
        self.assertFalse(worker.is_alive())
        if errors:
            raise errors[0]
        self.assertTrue(started.exists())
        self.assert_held(result)


class PinnedPublicationInputTests(unittest.TestCase):
    """Real disposable Git; gate seam proves transport, not semantic approval."""
    def setUp(self):
        self.fixture = publishing_fixtures.PublishingV2Tests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.publisher.authorize()
        self.before = self.fixture.git("rev-parse", "HEAD")
        (self.fixture.repo / "app.py").write_bytes(b"value = 2\n")

    def publish(self):
        with patch("ase.publishing.GateService.gate", side_effect=self.fixture.protocol_gate):
            return self.fixture.publisher.publish("publish", execute_push=True, commit_message="Reviewed pinned inputs")

    def at_launch(self, command, change):
        actual = publishing_module.execute
        called = []
        def execute(argv, *args, **kwargs):
            if argv[5] == command and not called:
                called.append(command)
                guard = kwargs["launch_guard"]
                @contextmanager
                def boundary():
                    change()
                    with guard():
                        yield
                kwargs["launch_guard"] = boundary
            return actual(argv, *args, **kwargs)
        with patch("ase.publishing.execute", side_effect=execute):
            result = self.publish()
        self.assertEqual(called, [command])
        return result

    def other_remote(self):
        other = self.fixture.base / "other.git"
        self.fixture.git("init", "--bare", str(other))
        return other

    def assert_only_authorized(self, result, other):
        self.assertTrue(result["published"], result)
        self.assertEqual(result["remote_commit"], self.fixture.remote_commit())
        self.assertFalse(self.fixture.remote_commit(other))

    def test_late_pushurl_change_cannot_redirect_actual_transport(self):
        other = self.other_remote()
        result = self.at_launch("push", lambda: self.fixture.git("remote", "set-url", "--push", "origin", str(other)))
        self.assert_only_authorized(result, other)

    def test_late_second_pushurl_cannot_fan_out_actual_transport(self):
        other = self.other_remote()
        result = self.at_launch("push", lambda: self.fixture.git("remote", "set-url", "--add", "--push", "origin", str(other)))
        self.assert_only_authorized(result, other)

    def test_late_url_rewrites_cannot_redirect_push_or_confirmation(self):
        other = self.other_remote()
        def change():
            self.fixture.git("config", "url." + str(other) + ".insteadOf", str(self.fixture.remote))
            self.fixture.git("config", "url." + str(other) + ".pushInsteadOf", str(self.fixture.remote))
        result = self.at_launch("push", change)
        self.assert_only_authorized(result, other)

    def test_resolved_url_is_not_rewritten_a_second_time(self):
        other = self.other_remote()
        self.fixture.git("remote", "set-url", "origin", "audit-pin:")
        self.fixture.git("config", "url." + str(self.fixture.remote) + ".insteadOf", "audit-pin:")
        self.fixture.git("config", "url." + str(other) + ".insteadOf", str(self.fixture.remote))
        self.assertEqual(self.fixture.publisher._urls("origin"), [str(self.fixture.remote)])
        self.fixture.publisher.authorize()
        self.assert_only_authorized(self.publish(), other)

    def test_clean_filter_mismatch_is_rejected_before_commit(self):
        self.fixture.git("config", "filter.audit.clean", "sed 's/value = 2/value = 3/'")
        (self.fixture.repo / ".git/info/attributes").write_text("app.py filter=audit\n")
        result = self.publish()
        self.assertFalse(result["published"], result)
        self.assertIn(result["status"], {"REJECTED", "BLOCKED"})
        self.assertEqual(self.fixture.git("rev-parse", "HEAD"), self.before)
        self.assertFalse(self.fixture.remote_commit())

    def test_explicit_text_eol_normalization_remains_publishable(self):
        (self.fixture.repo / ".git/info/attributes").write_text("app.py text eol=lf\n")
        (self.fixture.repo / "app.py").write_bytes(b"value = 2\r\n")
        result = self.publish()
        self.assertTrue(result["published"], result)
        self.assertEqual(self.fixture.git("show", "HEAD:app.py"), "value = 2")
        self.assertEqual((self.fixture.repo / "app.py").read_bytes(), b"value = 2\r\n")

    def test_autocrlf_text_normalization_remains_publishable(self):
        self.fixture.git("config", "core.autocrlf", "true")
        (self.fixture.repo / "app.py").write_bytes(b"value = 2\r\n")
        self.assertTrue(self.publish()["published"])

    def test_non_text_filter_eol_transformation_is_rejected(self):
        self.fixture.git("config", "core.autocrlf", "false")
        self.fixture.git("config", "filter.audit.clean", "sed 's/\r$//'")
        (self.fixture.repo / ".git/info/attributes").write_text("app.py -text filter=audit\n")
        (self.fixture.repo / "app.py").write_bytes(b"value = 2\r\n")
        result = self.publish()
        self.assertFalse(result["published"], result)
        self.assertEqual(self.fixture.git("rev-parse", "HEAD"), self.before)

    def test_post_commit_filtered_tree_drift_cannot_publish(self):
        self.fixture.hook("post-commit", chr(10).join([
            "rm .git/hooks/post-commit",
            "git config filter.audit.clean \"sed 's/value = 2/value = 3/'\"",
            "echo 'app.py filter=audit' > .git/info/attributes",
            "git add --renormalize app.py",
            "git commit --amend --no-edit"]))
        result = self.publish()
        self.assertEqual(self.fixture.git("show", "HEAD:app.py"), "value = 3")
        self.assertFalse(result["published"], result)
        self.assertFalse(self.fixture.remote_commit())

    def test_source_drift_at_commit_launch_prevents_commit(self):
        result = self.at_launch("commit", lambda: (self.fixture.repo / "app.py").write_bytes(b"value = 4\n"))
        self.assertFalse(result["published"], result)
        self.assertEqual(self.fixture.git("rev-parse", "HEAD"), self.before)

    def test_owned_deletion_preserves_empty_tree(self):
        (self.fixture.repo / "app.py").unlink()
        result = self.publish()
        self.assertTrue(result["published"], result)
        self.assertEqual(self.fixture.git("ls-tree", "-r", "--name-only", "HEAD"), "")

    def test_executable_mode_is_preserved(self):
        if os.name != "nt":
            (self.fixture.repo / "app.py").chmod(0o755)
        self.fixture.git("update-index", "--chmod=+x", "app.py")
        result = self.publish()
        self.assertTrue(result["published"], result)
        self.assertTrue(self.fixture.git("ls-tree", "HEAD", "app.py").startswith("100755 "))

    def test_unchanged_baseline_link_representation_is_preserved(self):
        (self.fixture.repo / "app.py").write_bytes(bytes([118,97,108,117,101,32,61,32,49,10]))
        link = self.fixture.repo / "helper-link"
        if os.name == "nt":
            self.fixture.git("config", "core.symlinks", "false")
            link.write_bytes(b"app.py")
        else:
            link.symlink_to("app.py")
        blob_input = self.fixture.base / "link-target-object"
        blob_input.write_bytes(b"app.py")
        oid = self.fixture.git("hash-object", "-w", "--no-filters", str(blob_input))
        self.fixture.git("update-index", "--add", "--cacheinfo", "120000," + oid + ",helper-link")
        self.fixture.git("commit", "-m", "Existing baseline link")
        self.fixture.refresh_baseline()
        (self.fixture.repo / "app.py").write_bytes(bytes([118,97,108,117,101,32,61,32,50,10]))
        result = self.publish()
        self.assertTrue(result["published"], result)
        self.assertTrue(self.fixture.git("ls-tree", "HEAD", "helper-link").startswith("120000 "))

    def test_transport_keeps_native_remote_hook_and_credential_configuration(self):
        self.fixture.git("config", "credential.helper", "fixture-preserved")
        destination = shlex.quote(str(self.fixture.remote))
        self.fixture.hook("pre-push", chr(10).join([
            'test "$1" = origin || exit 17',
            'test "$2" = ' + destination + ' || exit 18',
            'test "$(git config --get credential.helper)" = fixture-preserved || exit 19']))
        self.assertTrue(self.publish()["published"])

    def test_unavailable_confirmation_preserves_unknown_effect(self):
        actual = publishing_module.execute
        pushes = []
        def execute(argv, *args, **kwargs):
            receipt = actual(argv, *args, **kwargs)
            if argv[5] == "push":
                pushes.append(receipt)
            if argv[5] == "ls-remote":
                receipt = {**receipt, "exit_code": 1, "error": "Disposable unavailable confirmation"}
            return receipt
        with patch("ase.publishing.execute", side_effect=execute):
            result = self.publish()
        self.assertFalse(result["published"], result)
        self.assertEqual(result["publication_state"], "UNKNOWN")
        self.assertEqual(len(pushes), 1)
        self.assertEqual(self.fixture.remote_commit(), result["commit"])

    def test_confirmed_push_error_retains_actual_publication(self):
        actual = publishing_module.execute
        def execute(argv, *args, **kwargs):
            receipt = actual(argv, *args, **kwargs)
            if argv[5] == "push":
                self.assertEqual(receipt["exit_code"], 0)
                receipt = {**receipt, "exit_code": 1, "error": "Disposable lost process confirmation"}
            return receipt
        with patch("ase.publishing.execute", side_effect=execute):
            result = self.publish()
        self.assertTrue(result["published"], result)
        self.assertTrue(result["reconciled_push_error"])
        self.assertEqual(result["remote_commit"], self.fixture.remote_commit())


class DurablePublicationRecoveryTests(unittest.TestCase):
    """Actual Git/hook/crash behavior; the evidence gate remains a protocol seam."""
    def setUp(self):
        self.fixture = publishing_fixtures.PublishingV2Tests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.publisher.authorize()
        self.marker = self.fixture.base / 'push-markers.txt'
        self.fixture.hook('pre-push', 'printf "push\\n" >> ' + shlex.quote(str(self.marker).replace('\\', '/')))
        self.gate_patch = patch('ase.publishing.GateService.gate', autospec=True, side_effect=self.protocol_gate)
        self.gate_patch.start()
        self.addCleanup(self.gate_patch.stop)

    @staticmethod
    def protocol_gate(service, task_id, **kwargs):
        tasks = TaskService(service.store)
        task = tasks.snapshot(task_id)
        return {'status': 'APPROVED', 'reasons': [], 'blocked_reasons': [], 'rejected_reasons': [],
                'fingerprint': source_snapshot(service.store, task['contract'], task_id=task_id)['fingerprint'],
                'contract_hash': task['contract_hash'], 'proposal_hash': task['proposal_hash'],
                'plan_hash': tasks.execution_plan(task_id)['plan_hash']}

    def markers(self):
        return self.marker.read_text().splitlines() if self.marker.exists() else []

    def publish(self, *, publisher=None, task_id='publish', remote='origin'):
        return (publisher or self.fixture.publisher).publish(task_id, execute_push=True, remote=remote)

    def lose_confirmation(self):
        actual = publishing_module.execute
        def execute(argv, *args, **kwargs):
            receipt = actual(argv, *args, **kwargs)
            if argv[5] == 'ls-remote':
                return {**receipt, 'exit_code': 1, 'error': 'Disposable lost confirmation'}
            return receipt
        with patch('ase.publishing.execute', side_effect=execute):
            return self.publish()

    def other_task(self):
        fixture = self.fixture
        task = fixture.tasks.create({**fixture.tasks.snapshot('publish')['contract'], 'id': 'other-root'})
        task = fixture.tasks.propose('other-root', {'summary': 'Separate approved root', 'stage_ids': ['change']}, expected_revision=task['revision'])
        fixture.tasks.approve('other-root', proposal_hash=task['proposal_hash'], user_message_ref='test:other-root', approved_stage_ids=['change'])
        from ase.state import transaction, write_record
        with transaction(fixture.store.db):
            baseline = read_record(fixture.store.db, 'baseline', 'publish')['payload']
            write_record(fixture.store.db, 'baseline', 'other-root', baseline, expected_revision=0)
        return task

    def crash(self, point):
        import subprocess
        import sys
        script = self.fixture.base / 'crash-publisher.py'
        script.write_text("""
import os, sys, importlib.util
from pathlib import Path
from unittest.mock import patch
candidate=os.environ.get('ASE_PUBLICATION_PROPOSAL_FILE')
if candidate:
    import ase
    spec=importlib.util.spec_from_file_location('ase.publishing', candidate)
    module=importlib.util.module_from_spec(spec); sys.modules['ase.publishing']=module; spec.loader.exec_module(module); ase.publishing=module
from ase.store import Store
from ase.tasks import TaskService
from ase.gates import source_snapshot
from ase.journal import Journal
import ase.publishing as publication
repo, home, point = sys.argv[1:]
store=Store(Path(repo),Path(home))
def gate(service,task_id,**kwargs):
    tasks=TaskService(service.store); task=tasks.snapshot(task_id)
    return {'status':'APPROVED','reasons':[],'fingerprint':source_snapshot(service.store,task['contract'],task_id=task_id)['fingerprint'],'contract_hash':task['contract_hash'],'proposal_hash':task['proposal_hash'],'plan_hash':tasks.execution_plan(task_id)['plan_hash']}
actual=publication.execute
proof=publication.Publisher._proof
dump=publication.json.dump
def write_proof(payload,stream,*args,**kwargs):
    if point=='partial-proof' and 'confirmed_at_utc' in payload:
        stream.write('{"commit":'); stream.flush(); os._exit(77)
    return dump(payload,stream,*args,**kwargs)
append=Journal.append
def journal_append(self,event,*,event_key):
    if event['kind']=='published' and point=='after-proof': os._exit(76)
    return append(self,event,event_key=event_key)
def execute(argv,*args,**kwargs):
    if argv[5]=='push' and point=='before-push': os._exit(73)
    receipt=actual(argv,*args,**kwargs)
    if argv[5]=='push' and point=='after-push': os._exit(74)
    return receipt
def acknowledge(self,folder,kind,task_id,payload):
    if kind=='published' and point=='before-journal': os._exit(75)
    return proof(self,folder,kind,task_id,payload)
with patch('ase.publishing.GateService.gate',autospec=True,side_effect=gate), patch('ase.publishing.execute',side_effect=execute), patch.object(publication.Publisher,'_proof',acknowledge), patch.object(Journal,'append',journal_append), patch.object(publication.json,'dump',write_proof):
    publication.Publisher(store).publish('publish',execute_push=True)
raise SystemExit(99)
""", encoding='utf-8')
        environment = dict(os.environ)
        environment['PYTHONPATH'] = str(Path(publishing_fixtures.__file__).resolve().parents[1])
        process = subprocess.run([sys.executable, '-B', str(script), str(self.fixture.repo), str(self.fixture.base/'state'), point],
                                 env=environment, capture_output=True, text=True, timeout=30)
        self.assertEqual(process.returncode, {'before-push':73,'after-push':74,'before-journal':75,'after-proof':76,'partial-proof':77}[point], process.stdout+process.stderr)
        # A dead fixture process no longer owns its crash-retained lease.
        (self.fixture.store.root/'tasks'/'publish'/'execution.lock').unlink(missing_ok=True)

    def test_caller_transaction_is_rejected_without_losing_unrelated_writes(self):
        from ase.state import transaction, write_record
        fixture = self.fixture
        with transaction(fixture.store.db):
            write_record(fixture.store.db,'outer','kept',{'kept':True},expected_revision=0)
            with self.assertRaisesRegex(ValueError, 'transaction|write-ahead'):
                self.publish()
            self.assertTrue(fixture.store.db.in_transaction)
            self.assertEqual(read_record(fixture.store.db,'outer','kept')['payload'],{'kept':True})
        self.assertEqual(self.markers(), [])
        self.assertFalse(fixture.remote_commit())

    def test_lost_confirmation_reconciles_once_without_repeating_push_hook(self):
        first = self.lose_confirmation()
        self.assertEqual(first['publication_state'], 'UNKNOWN')
        recovered = self.publish()
        again = self.publish()
        self.assertTrue(recovered['published'], recovered)
        self.assertTrue(again['published'], again)
        self.assertEqual(self.markers(), ['push'])
        self.assertEqual(len(self.fixture.journal('published')), 1)

    def test_absence_and_task_remote_aliases_cannot_renew_uncertain_push(self):
        self.fixture.hook('pre-push','printf "push\\n" >> '+shlex.quote(str(self.marker).replace('\\','/'))+'\nexit 42')
        first = self.lose_confirmation()
        self.assertEqual(first['publication_state'], 'UNKNOWN')
        self.other_task()
        self.fixture.git('remote','add','alias',str(self.fixture.remote))
        self.fixture.publisher.authorize('alias')
        second = self.publish(task_id='other-root', remote='alias')
        self.assertFalse(second['published'], second)
        self.assertEqual(second.get('publication_state'), 'UNKNOWN')
        self.assertEqual(self.markers(), ['push'])
        self.assertFalse(self.fixture.remote_commit())

    def test_different_roots_racing_for_one_target_cannot_both_launch(self):
        self.other_task()
        entered, release = threading.Event(), threading.Event()
        actual = publishing_module.execute
        failures, outcomes = [], []
        def execute(argv,*args,**kwargs):
            if argv[5]=='push' and not entered.is_set():
                entered.set()
                if not release.wait(15): raise RuntimeError('Fixture launch boundary timed out')
            return actual(argv,*args,**kwargs)
        def owner():
            try:
                with closing(Store(self.fixture.repo,self.fixture.base/'state')) as store:
                    outcomes.append(self.publish(publisher=Publisher(store)))
            except BaseException as error: failures.append(error)
        with patch('ase.publishing.execute',side_effect=execute):
            thread=threading.Thread(target=owner); thread.start()
            try:
                self.assertTrue(entered.wait(15))
                other=self.publish(task_id='other-root')
            finally:
                release.set(); thread.join(20)
        self.assertFalse(thread.is_alive())
        self.assertEqual(failures, [])
        self.assertFalse(other['published'], other)
        self.assertTrue(outcomes[0]['published'], outcomes)
        self.assertEqual(self.markers(), ['push'])

    def test_authority_denied_before_launch_does_not_poison_retry(self):
        actual = publishing_module.execute
        def denied(argv,*args,**kwargs):
            if argv[5]=='push':
                @contextmanager
                def guard():
                    raise publishing_module._PublicationHeld('Fixture authority held before Popen')
                    yield
                kwargs['launch_guard']=guard
            return actual(argv,*args,**kwargs)
        with patch('ase.publishing.execute',side_effect=denied):
            first=self.publish()
        self.assertFalse(first['published'])
        self.assertEqual(self.markers(), [])
        self.assertTrue(self.publish()['published'])
        self.assertEqual(self.markers(), ['push'])

    def test_crash_after_writeahead_before_push_stays_unknown_on_absence(self):
        self.crash('before-push')
        recovered=self.publish()
        self.assertFalse(recovered['published'],recovered)
        self.assertEqual(recovered.get('publication_state'),'UNKNOWN')
        self.assertEqual(self.markers(), [])
        self.assertFalse(self.fixture.remote_commit())

    def test_crash_after_actual_push_reconciles_without_a_second_hook(self):
        self.crash('after-push')
        recovered=self.publish()
        self.assertTrue(recovered['published'],recovered)
        self.assertEqual(self.markers(), ['push'])
        self.assertEqual(len(self.fixture.journal('published')),1)

    def test_crash_before_journal_ack_recovers_once_without_a_second_hook(self):
        self.crash('before-journal')
        recovered=self.publish()
        again=self.publish()
        self.assertTrue(recovered['published'],recovered)
        self.assertTrue(again['published'],again)
        self.assertEqual(self.markers(), ['push'])
        self.assertEqual(len(self.fixture.journal('published')),1)

    def test_remote_confirmation_keeps_truth_when_current_gate_is_blocked(self):
        self.lose_confirmation()
        with patch('ase.publishing.GateService.gate',return_value={'status':'BLOCKED','reasons':['Evidence no longer current']}):
            recovered=self.publish()
        self.assertTrue(recovered['published'],recovered)
        self.assertEqual(recovered['status'],'BLOCKED')
        self.assertEqual(self.markers(), ['push'])
        self.assertEqual(len(self.fixture.journal('published')),1)


    def test_file_url_alias_reconciles_the_same_local_destination(self):
        first=self.lose_confirmation()
        self.assertEqual(first['publication_state'],'UNKNOWN')
        self.fixture.git('remote','set-url','origin',self.fixture.remote.as_uri())
        self.fixture.publisher.authorize()
        recovered=self.publish()
        self.assertTrue(recovered['published'],recovered)
        self.assertEqual(self.markers(),['push'])
        self.assertEqual(len(self.fixture.journal('published')),1)

    def test_simultaneous_fresh_preflights_share_one_target_cas(self):
        self.other_task()
        barrier=threading.Barrier(2)
        counts={}
        outcomes, failures=[],[]
        def gate(service,task_id,**kwargs):
            result=self.protocol_gate(service,task_id,**kwargs)
            counts[task_id]=counts.get(task_id,0)+1
            if counts[task_id]==2: barrier.wait(15)
            return result
        def publish(task_id):
            try:
                with closing(Store(self.fixture.repo,self.fixture.base/'state')) as store:
                    outcomes.append(self.publish(publisher=Publisher(store),task_id=task_id))
            except BaseException as error: failures.append(error)
        with patch('ase.publishing.GateService.gate',new=gate):
            threads=[threading.Thread(target=publish,args=(task_id,)) for task_id in ('publish','other-root')]
            for thread in threads: thread.start()
            for thread in threads: thread.join(25)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(failures,[])
        self.assertEqual(sum(bool(result['published']) for result in outcomes),1,outcomes)
        self.assertEqual(self.markers(),['push'])

    def test_completed_target_allows_a_freshly_reviewed_new_commit(self):
        self.assertTrue(self.publish()['published'])
        (self.fixture.repo/'app.py').write_text('value = 2\n',encoding='utf-8')
        self.fixture.git('add','app.py');self.fixture.git('commit','-m','New approved app version')
        second=self.publish()
        self.assertTrue(second['published'],second)
        self.assertEqual(self.markers(),['push','push'])
        self.assertEqual(len(self.fixture.journal('published')),2)

    def test_crash_after_proof_file_recovers_the_same_journal_once(self):
        self.crash('after-proof')
        recovered=self.publish()
        self.assertTrue(recovered['published'],recovered)
        self.assertEqual(self.markers(),['push'])
        self.assertEqual(len(self.fixture.journal('published')),1)

    def test_scp_target_identity_does_not_change_with_workspace_root(self):
        other=Publisher(SimpleNamespace(repo=self.fixture.base/'another-workspace'))
        self.assertEqual(self.fixture.publisher._push_key('git@example.invalid:team/repo.git','refs/heads/feature/publishing'),
                         other._push_key('git@example.invalid:team/repo.git','refs/heads/feature/publishing'))

    def test_imported_uncertainty_settles_old_owner_then_allows_fresh_new_work(self):
        from ase.transfer import export_project, import_project
        from ase.state import transaction, write_record
        from ase.journal import Journal
        first=self.lose_confirmation()
        original_files=sorted(path.name for path in Path(first['evidence_root']).iterdir())
        archive=self.fixture.base/'uncertain.zip'
        export_project(self.fixture.store.project,archive)
        target=self.fixture.base/'imported-repo'
        self.fixture.git('clone',str(self.fixture.repo),str(target))
        self.fixture.git('-C',str(target),'config','user.name','DisposableImportPublishing')
        self.fixture.git('-C',str(target),'config','user.email','fixture@example.invalid')
        self.fixture.git('-C',str(target),'remote','set-url','origin',str(self.fixture.remote))
        project=import_project(archive,self.fixture.base/'imported-state',root=target)
        with closing(Store(project)) as store:
            publisher=Publisher(store)
            recovered=self.publish(publisher=publisher)
            self.assertFalse(recovered['published'],recovered)
            self.assertTrue(recovered.get('historical_publication_confirmed'),recovered)
            self.assertEqual(recovered['status'],'BLOCKED')
            self.assertNotEqual(recovered['workspace_id'],store.workspace_id)
            tasks=TaskService(store)
            task=tasks.create({**tasks.snapshot('publish')['contract'],'id':'fresh-import-work'})
            task=tasks.propose('fresh-import-work',{'summary':'Review new workspace work','stage_ids':['change']},expected_revision=task['revision'])
            tasks.approve('fresh-import-work',proposal_hash=task['proposal_hash'],user_message_ref='test:current-import-approval',approved_stage_ids=['change'])
            baseline={**source_snapshot(store,tasks.snapshot('fresh-import-work')['contract']),'dirty':''}
            with transaction(store.db): write_record(store.db,'baseline','fresh-import-work',baseline,expected_revision=0)
            (target/'app.py').write_text('value = 2\n',encoding='utf-8')
            self.fixture.git('-C',str(target),'add','app.py')
            self.fixture.git('-C',str(target),'commit','-m','Approved current workspace change')
            publisher.authorize()
            pushed=self.publish(publisher=publisher,task_id='fresh-import-work')
            self.assertTrue(pushed['published'],pushed)
            self.assertEqual(pushed['workspace_id'],store.workspace_id)
        self.assertEqual(self.markers(),['push'])  # A clone has its own native hooks.
        self.assertEqual(self.fixture.journal('published'),[])
        self.assertEqual(sorted(path.name for path in Path(first['evidence_root']).iterdir()),original_files)


    def test_lost_push_receipt_is_retained_when_confirmation_is_unavailable(self):
        actual=publishing_module.execute
        def lost(argv,*args,**kwargs):
            receipt=actual(argv,*args,**kwargs)
            if argv[5] in {'push','ls-remote'}:
                return {**receipt,'exit_code':1,'error':'Disposable lost acknowledgement'}
            return receipt
        with patch('ase.publishing.execute',side_effect=lost): first=self.publish()
        self.assertEqual(first['publication_state'],'UNKNOWN')
        recovered=self.publish()
        self.assertTrue(recovered['published'],recovered)
        self.assertTrue(recovered['reconciled_push_error'],recovered)
        self.assertEqual(self.markers(),['push'])

    def test_earlier_owner_retains_confirmation_after_target_advances(self):
        self.other_task()
        observed,release=threading.Event(),threading.Event()
        actual=publishing_module.execute
        failures,outcomes=[],[]
        owner_name='publication-original-owner'
        def execute(argv,*args,**kwargs):
            receipt=actual(argv,*args,**kwargs)
            if argv[5]=='ls-remote' and threading.current_thread().name==owner_name:
                observed.set()
                if not release.wait(20): raise RuntimeError('Fixture owner confirmation timed out')
            return receipt
        def owner():
            try:
                with closing(Store(self.fixture.repo,self.fixture.base/'state')) as store:
                    outcomes.append(self.publish(publisher=Publisher(store)))
            except BaseException as error: failures.append(error)
        first_commit=self.fixture.git('rev-parse','HEAD')
        with patch('ase.publishing.execute',side_effect=execute):
            thread=threading.Thread(target=owner,name=owner_name);thread.start()
            try:
                self.assertTrue(observed.wait(15))
                settled=self.publish(task_id='other-root')
                self.assertTrue(settled.get('historical_publication_confirmed'),settled)
                (self.fixture.repo/'app.py').write_text('value = 2\n',encoding='utf-8')
                self.fixture.git('add','app.py');self.fixture.git('commit','-m','Next approved publication')
                advanced=self.publish(task_id='other-root')
                self.assertTrue(advanced['published'],advanced)
            finally:
                release.set();thread.join(25)
        self.assertFalse(thread.is_alive())
        self.assertEqual(failures,[])
        self.assertTrue(outcomes[0]['published'],outcomes)
        self.assertEqual(outcomes[0]['commit'],first_commit)
        self.assertEqual(outcomes[0]['status'],'BLOCKED')
        self.assertEqual(self.markers(),['push','push'])
        self.assertEqual(len(self.fixture.journal('published')),2)

    def test_partial_proof_write_failure_can_acknowledge_without_replay(self):
        original=publishing_module.json.dump
        def interrupted(payload,stream,*args,**kwargs):
            if 'confirmed_at_utc' in payload:
                stream.write('{"commit":')
                raise OSError('Disposable interrupted proof write')
            return original(payload,stream,*args,**kwargs)
        with patch('ase.publishing.json.dump',side_effect=interrupted), self.assertRaises(OSError):
            self.publish()
        recovered=self.publish()
        self.assertTrue(recovered['published'],recovered)
        self.assertEqual(self.markers(),['push'])
        self.assertEqual(len(self.fixture.journal('published')),1)

    def test_crash_during_proof_json_can_acknowledge_without_replay(self):
        self.crash('partial-proof')
        recovered=self.publish()
        self.assertTrue(recovered['published'],recovered)
        self.assertEqual(self.markers(),['push'])
        self.assertEqual(len(self.fixture.journal('published')),1)

    def test_prior_application_and_current_remote_absence_are_distinguished(self):
        first=self.publish()
        self.assertTrue(first['published'],first)
        self.fixture.git('--git-dir',str(self.fixture.remote),'update-ref','-d','refs/heads/feature/publishing')
        recovered=self.publish()
        self.assertTrue(recovered['published'],recovered)
        self.assertTrue(recovered['historical_publication_confirmed'])
        self.assertEqual(recovered['status'],'BLOCKED')
        self.assertIsNone(recovered['remote_commit'])
        self.assertEqual(recovered['confirmed_commit'],first['commit'])
        self.assertEqual(self.markers(),['push'])


if __name__ == "__main__":
    unittest.main()
