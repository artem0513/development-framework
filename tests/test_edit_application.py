"""Real filesystem behavior for resumable, metadata-bound local edit batches."""
import copy
import hashlib
import importlib.util
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch

for parent in Path(__file__).resolve().parents:
    if (parent / "ase" / "workflow.py").is_file():
        sys.path.insert(0, str(parent))
        break

from ase.workflow import _target

if Path(__file__).with_name("edit_application.py").is_file():
    spec = importlib.util.spec_from_file_location("edit_application_under_test", Path(__file__).with_name("edit_application.py"))
    application = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(application)
else:
    from ase import edit_application as application

APPLICATION_ID = "a123456789abcdef0123456789abcdef"


def digest(value):
    return hashlib.sha256(value).hexdigest()


class EditApplicationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="ase-edit-application-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.scope = {"resources": ["."], "effects": ["file_write"]}

    def write(self, name, value):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(value)
        return path

    def edit(self, name, content, *, operation="write"):
        path = self.root / name
        return {"path": name, "operation": operation, "before_sha256": digest(path.read_bytes()) if path.is_file() else "", "content": content}

    def plan(self, edits, scope=None):
        return application.plan_edits(self.root, scope or self.scope, edits, APPLICATION_ID, _target)

    def apply(self, edits, plan, scope=None):
        return application.apply_planned_edits(self.root, scope or self.scope, edits, plan, _target)

    def test_plan_is_metadata_only_and_names_only_its_own_temporary(self):
        self.write("folder/a.txt", b"before")
        edits = [self.edit("folder/./a.txt", "café\r\n")]
        plan = self.plan(edits)
        self.assertEqual(plan, {"version": 1, "application_id": APPLICATION_ID, "edits": [{
            "path": "folder/a.txt", "operation": "write", "before_sha256": digest(b"before"),
            "after_sha256": digest("café\r\n".encode("utf-8")),
            "temporary_path": "folder/a.txt.ase-" + APPLICATION_ID + "-0.tmp"}]})
        self.assertEqual((self.root / "folder/a.txt").read_bytes(), b"before")
        self.assertEqual(sorted(p.name for p in (self.root / "folder").iterdir()), ["a.txt"])

    def test_plan_validates_every_preimage_before_any_write(self):
        self.write("a", b"one")
        self.write("b", b"two")
        edits = [self.edit("a", "changed"), self.edit("b", "changed")]
        edits[1]["before_sha256"] = digest(b"stale")
        with self.assertRaises(ValueError):
            self.plan(edits)
        self.assertEqual((self.root / "a").read_bytes(), b"one")
        self.assertEqual((self.root / "b").read_bytes(), b"two")
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ["a", "b"])

    @unittest.skipUnless(os.name == "nt", "Windows path normalization")
    def test_windows_normalizing_path_components_rejected_before_mutation(self):
        for name in ("trailing.", "trailing-space ", "folder./file.txt", "folder /file.txt"):
            with self.subTest(name=name):
                edits = [{"path": name, "operation": "write", "before_sha256": "", "content": "new"}]
                with self.assertRaisesRegex(ValueError, "normaliz"):
                    self.plan(edits)
                self.assertEqual(list(self.root.iterdir()), [])

    def test_plan_rejects_out_of_scope_duplicate_missing_effect_and_invalid_operation(self):
        self.write("a", b"one")
        edit = self.edit("a", "changed")
        cases = [([edit], {"resources": ["b"], "effects": ["file_write"]}),
            ([edit, dict(edit, path="./a")], self.scope),
            ([edit], {"resources": ["a"], "effects": []}),
            ([dict(edit, operation="move")], self.scope),
            ([dict(edit, content=None)], self.scope),
            ([dict(edit, path="../escape")], self.scope),
            ([dict(edit, path=".git/config")], self.scope),
            ([dict(edit, path=".devframework/local/state")], self.scope)]
        for edits, scope in cases:
            with self.subTest(edits=edits, scope=scope), self.assertRaises(ValueError):
                self.plan(edits, scope)
        self.assertEqual((self.root / "a").read_bytes(), b"one")

    def test_application_id_requires_exactly_thirty_two_lowercase_hex_characters(self):
        for value in ("", "a" * 31, "a" * 33, "g" * 32, "../outside", None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                application.plan_edits(self.root, self.scope, [], value, _target)

    def test_resume_completes_partial_utf8_temp_and_preserves_completed_file(self):
        first = self.write("a", b"before-a")
        self.write("b", b"before-b")
        edits = [self.edit("a", "after-a"), self.edit("b", "café\r\n")]
        plan = self.plan(edits)
        first.write_bytes(b"after-a")
        before_stat = first.stat()
        staged = self.write(plan["edits"][1]["temporary_path"], b"caf\xc3")
        staged_inode = staged.stat().st_ino
        self.assertEqual(self.apply(edits, plan), ["a", "b"])
        self.assertEqual(first.read_bytes(), b"after-a")
        self.assertEqual(first.stat().st_mtime_ns, before_stat.st_mtime_ns)
        self.assertEqual(first.stat().st_ino, before_stat.st_ino)
        self.assertEqual((self.root / "b").read_bytes(), "café\r\n".encode("utf-8"))
        self.assertEqual((self.root / "b").stat().st_ino, staged_inode)
        self.assertFalse(staged.exists())

    def test_third_state_on_later_target_prevents_all_pending_writes(self):
        self.write("a", b"before-a")
        self.write("b", b"before-b")
        edits = [self.edit("a", "after-a"), self.edit("b", "after-b")]
        plan = self.plan(edits)
        (self.root / "b").write_bytes(b"external edit")
        with self.assertRaisesRegex(ValueError, "preimage|drift|state"):
            self.apply(edits, plan)
        self.assertEqual((self.root / "a").read_bytes(), b"before-a")
        self.assertEqual((self.root / "b").read_bytes(), b"external edit")
        self.assertFalse((self.root / plan["edits"][0]["temporary_path"]).exists())

    def test_drift_on_previously_completed_target_blocks_remaining_target(self):
        self.write("a", b"before-a")
        self.write("b", b"before-b")
        edits = [self.edit("a", "after-a"), self.edit("b", "after-b")]
        plan = self.plan(edits)
        (self.root / "a").write_bytes(b"external edit after previous application")
        with self.assertRaises(ValueError):
            self.apply(edits, plan)
        self.assertEqual((self.root / "b").read_bytes(), b"before-b")

    def test_plan_tampering_or_edit_replacement_causes_no_writes(self):
        self.write("a", b"before")
        edits = [self.edit("a", "after")]
        original = self.plan(edits)
        changes = [("after_sha256", digest(b"other")), ("path", "other"),
            ("temporary_path", "foreign.tmp"), ("before_sha256", None), ("operation", "delete")]
        for key, value in changes:
            plan = copy.deepcopy(original)
            plan["edits"][0][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.apply(edits, plan)
        changed = [dict(edits[0], content="replacement proposal")]
        with self.assertRaises(ValueError):
            self.apply(changed, original)
        self.assertEqual((self.root / "a").read_bytes(), b"before")

    def test_unknown_temp_content_blocks_entire_batch_and_is_preserved(self):
        self.write("a", b"before-a")
        self.write("b", b"before-b")
        edits = [self.edit("a", "after-a"), self.edit("b", "after-b")]
        plan = self.plan(edits)
        owned = self.write(plan["edits"][1]["temporary_path"], b"foreign contents")
        with self.assertRaisesRegex(ValueError, "temp|Temp"):
            self.apply(edits, plan)
        self.assertEqual(owned.read_bytes(), b"foreign contents")
        self.assertEqual((self.root / "a").read_bytes(), b"before-a")
        self.assertEqual((self.root / "b").read_bytes(), b"before-b")

    def test_cleanup_leaves_foreign_temp_and_removes_known_temp_for_completed_target(self):
        self.write("a", b"before")
        edits = [self.edit("a", "after")]
        plan = self.plan(edits)
        owned = self.write(plan["edits"][0]["temporary_path"], b"af")
        foreign = self.write("a.ase-foreign.tmp", b"foreign bytes")
        (self.root / "a").write_bytes(b"after")
        self.assertEqual(self.apply(edits, plan), ["a"])
        self.assertFalse(owned.exists())
        self.assertEqual(foreign.read_bytes(), b"foreign bytes")

    def test_mixed_batch_delete_create_empty_and_no_op_are_idempotent(self):
        original = self.write("stable", b"unchanged")
        self.write("obsolete", b"remove")
        self.write("empty", b"")
        edits = [self.edit("stable", "unchanged"), self.edit("obsolete", "", operation="delete"),
            self.edit("new/path", "new\n"), self.edit("empty", ""), self.edit("absent", "", operation="delete")]
        plan = self.plan(edits)
        self.assertIsNone(plan["edits"][1]["after_sha256"])
        self.assertIsNone(plan["edits"][1]["temporary_path"])
        stable_stat = original.stat()
        expected = ["stable", "obsolete", "new/path", "empty", "absent"]
        self.assertEqual(self.apply(edits, plan), expected)
        self.assertFalse((self.root / "obsolete").exists())
        self.assertFalse((self.root / "absent").exists())
        self.assertEqual((self.root / "new/path").read_bytes(), b"new\n")
        self.assertEqual((self.root / "empty").read_bytes(), b"")
        new_stat = (self.root / "new/path").stat()
        self.assertEqual(self.apply(edits, plan), expected)
        self.assertEqual(original.stat().st_mtime_ns, stable_stat.st_mtime_ns)
        self.assertEqual(original.stat().st_ino, stable_stat.st_ino)
        self.assertEqual((self.root / "new/path").stat().st_mtime_ns, new_stat.st_mtime_ns)
        self.assertFalse(list(self.root.rglob("*.tmp")))

    def test_full_staged_file_is_replaced_once_and_repeat_resume_does_nothing(self):
        self.write("a", b"before")
        edits = [self.edit("a", "after")]
        plan = self.plan(edits)
        staged = self.write(plan["edits"][0]["temporary_path"], b"after")
        identity = staged.stat().st_ino
        self.apply(edits, plan)
        self.assertEqual((self.root / "a").read_bytes(), b"after")
        self.assertEqual((self.root / "a").stat().st_ino, identity)
        self.assertFalse(staged.exists())
        with patch.object(application.os, "replace", side_effect=AssertionError("Completed target must not be replaced")):
            self.apply(edits, plan)

    def test_known_temp_hardlink_is_preserved_without_mutating_other_targets(self):
        self.write("a", b"before-a")
        self.write("b", b"before-b")
        edits = [self.edit("a", "after-a"), self.edit("b", "after-b")]
        plan = self.plan(edits)
        foreign = self.write("foreign", b"af")
        temp = self.root / plan["edits"][1]["temporary_path"]
        try:
            os.link(foreign, temp)
        except OSError as error:
            self.skipTest("Host cannot create hardlinks: " + str(error))
        with self.assertRaises(ValueError):
            self.apply(edits, plan)
        self.assertTrue(temp.exists())
        self.assertEqual(foreign.read_bytes(), b"af")
        self.assertEqual((self.root / "a").read_bytes(), b"before-a")

    def test_known_temp_symlink_or_simulated_link_metadata_is_preserved(self):
        self.write("a", b"before")
        edits = [self.edit("a", "after")]
        plan = self.plan(edits)
        foreign = self.write("foreign", b"af")
        temp = self.root / plan["edits"][0]["temporary_path"]
        try:
            temp.symlink_to(foreign)
        except OSError as error:
            if not isinstance(error, PermissionError) and getattr(error, "winerror", None) != 1314:
                raise
            # The host denied real link creation. Exercise the actual regular-file
            # metadata check with one path's lstat mode changed to a symlink.
            temp.write_bytes(b"af")
            lstat = Path.lstat
            def symlink_metadata(path, *args, **kwargs):
                details = lstat(path, *args, **kwargs)
                if path == temp:
                    values = list(details)
                    values[0] = stat.S_IFLNK | stat.S_IMODE(details.st_mode)
                    return os.stat_result(values)
                return details
            with self.subTest(coverage="simulated symlink metadata; host denied real creation"):
                with patch.object(Path, "lstat", symlink_metadata), self.assertRaises(ValueError):
                    self.apply(edits, plan)
            self.assertFalse(temp.is_symlink())
            self.assertEqual(temp.read_bytes(), b"af")
        else:
            with self.subTest(coverage="actual filesystem symlink"), self.assertRaises(ValueError):
                self.apply(edits, plan)
            self.assertTrue(temp.is_symlink())
        self.assertEqual(foreign.read_bytes(), b"af")
        self.assertEqual((self.root / "a").read_bytes(), b"before")

    def test_target_changed_to_hardlink_or_directory_is_rejected(self):
        self.write("a", b"before")
        edits = [self.edit("a", "after")]
        plan = self.plan(edits)
        (self.root / "a").unlink()
        (self.root / "a").mkdir()
        with self.assertRaises(ValueError):
            self.apply(edits, plan)
        (self.root / "a").rmdir()
        foreign = self.write("foreign", b"before")
        try:
            os.link(foreign, self.root / "a")
        except OSError as error:
            self.skipTest("Host cannot create hardlinks: " + str(error))
        with self.assertRaises(ValueError):
            self.apply(edits, plan)
        self.assertEqual(foreign.read_bytes(), b"before")

    def test_rechecks_preimage_after_staging_before_replace(self):
        self.write("a", b"before")
        edits = [self.edit("a", "after")]
        plan = self.plan(edits)
        original_fsync = application.os.fsync
        def drift_after_flush(descriptor):
            original_fsync(descriptor)
            (self.root / "a").write_bytes(b"external write")
        with patch.object(application.os, "fsync", side_effect=drift_after_flush), self.assertRaises(ValueError):
            self.apply(edits, plan)
        self.assertEqual((self.root / "a").read_bytes(), b"external write")

    def test_interruption_after_first_replace_resumes_without_rewriting_first_target(self):
        self.write("a", b"before-a")
        self.write("b", b"before-b")
        edits = [self.edit("a", "after-a"), self.edit("b", "after-b")]
        plan = self.plan(edits)
        replace = application.os.replace
        def interrupt_after_replace(source, target):
            replace(source, target)
            raise KeyboardInterrupt()
        with patch.object(application.os, "replace", side_effect=interrupt_after_replace), self.assertRaises(KeyboardInterrupt):
            self.apply(edits, plan)
        self.assertEqual((self.root / "a").read_bytes(), b"after-a")
        self.assertEqual((self.root / "b").read_bytes(), b"before-b")
        first_stat = (self.root / "a").stat()
        self.apply(edits, plan)
        self.assertEqual((self.root / "a").stat().st_mtime_ns, first_stat.st_mtime_ns)
        self.assertEqual((self.root / "a").stat().st_ino, first_stat.st_ino)
        self.assertEqual((self.root / "b").read_bytes(), b"after-b")

    def test_fsync_failure_preserves_staged_bytes_for_later_resume(self):
        self.write("a", b"before")
        edits = [self.edit("a", "after")]
        plan = self.plan(edits)
        with patch.object(application.os, "fsync", side_effect=OSError("controlled fsync failure")), self.assertRaises(OSError):
            self.apply(edits, plan)
        self.assertEqual((self.root / "a").read_bytes(), b"before")
        temporary = self.root / plan["edits"][0]["temporary_path"]
        self.assertEqual(temporary.read_bytes(), b"after")
        self.apply(edits, plan)
        self.assertEqual((self.root / "a").read_bytes(), b"after")
        self.assertFalse(temporary.exists())

    def test_deletion_rechecks_target_after_earlier_write(self):
        self.write("a", b"before-a")
        self.write("b", b"before-b")
        edits = [self.edit("a", "after-a"), self.edit("b", "", operation="delete")]
        plan = self.plan(edits)
        replace = application.os.replace
        def drift_after_replace(source, target):
            replace(source, target)
            (self.root / "b").write_bytes(b"external edit")
        with patch.object(application.os, "replace", side_effect=drift_after_replace), self.assertRaises(ValueError):
            self.apply(edits, plan)
        self.assertEqual((self.root / "b").read_bytes(), b"external edit")

    def test_target_cannot_overlap_another_edits_journal_temporary(self):
        self.write("a", b"before")
        owned_name = "a.ase-" + APPLICATION_ID + "-0.tmp"
        edits = [self.edit("a", "after"), self.edit(owned_name, "independent edit")]
        with self.assertRaises(ValueError):
            self.plan(edits)
        self.assertEqual((self.root / "a").read_bytes(), b"before")
        self.assertFalse((self.root / owned_name).exists())

    def test_target_ancestor_conflict_rejects_batch_before_any_creation(self):
        edits = [self.edit("a", "parent file"), self.edit("a/b", "child file")]
        error = None
        try:
            self.apply(edits, self.plan(edits))
        except ValueError as observed:
            error = observed
        self.assertEqual(list(self.root.iterdir()), [],
            "An ancestor conflict must create neither source files nor staging paths")
        self.assertIsInstance(error, ValueError, "The conflicting batch must be rejected")

    def test_target_temporary_ancestor_conflict_rejects_before_any_creation(self):
        temporary = "a.ase-" + APPLICATION_ID + "-0.tmp"
        edits = [self.edit("a", "source file"), self.edit(temporary + "/child", "nested source")]
        error = None
        try:
            self.apply(edits, self.plan(edits))
        except ValueError as observed:
            error = observed
        self.assertEqual(list(self.root.iterdir()), [],
            "A target below a journal temporary must not create source or temporary paths")
        self.assertIsInstance(error, ValueError, "The temporary/target ancestor conflict must be rejected")

    def test_non_directory_parent_blocks_batch_before_an_earlier_write(self):
        self.write("a", b"before")
        self.write("blocked", b"regular parent")
        edits = [self.edit("a", "after"), {"path":"blocked/child", "operation":"write", "before_sha256":"", "content":"after"}]
        with self.assertRaises(ValueError):
            self.plan(edits)
        self.assertEqual((self.root / "a").read_bytes(), b"before")

    def test_unhashable_operation_is_rejected_before_writes(self):
        self.write("a", b"before")
        edits = [dict(self.edit("a", "after"), operation=[])]
        with self.assertRaises(ValueError):
            self.plan(edits)
        self.assertEqual((self.root / "a").read_bytes(), b"before")


if __name__ == "__main__":
    unittest.main()
