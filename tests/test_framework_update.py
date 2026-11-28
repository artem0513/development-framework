"""Framework updates: policy stamps on verdicts, stale verdicts, the post-update doctor and memory notes.

The provider is the protocol fixture, so these tests prove stamping, gating and reporting, never
review quality.
"""
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from ase import doctor, framework
from ase.core import LocalRecords
from ase.knowledge import Knowledge
from ase.__main__ import parser
from ase.memory.reverify import inventory, reverify
from ase.state import read_record, write_record
from ase.store import Store
from test_review_packet import PacketFixture

ROOT = Path(__file__).resolve().parents[1]
OLD = {'version': 1, 'framework_commit': 'a' * 40, 'policy_file_sha256': 'b' * 64}


@unittest.skipUnless(shutil.which("git"), "git required")
class StaleVerdictTests(PacketFixture):
    FILES_PER_FOLDER = 2
    LINES_PER_FILE = 20

    def verified(self, task_id):
        self.approve(task_id)
        self.controller(task_id).verify(task_id)

    def test_receipts_and_gate_decisions_carry_the_framework_stamp(self):
        self.verified("stamped")
        current = framework.stamp()
        for role in ("ac", "review"):
            receipt = read_record(self.store.db, "verification", "stamped:" + role)["payload"]
            self.assertEqual(receipt[framework.STAMP_FIELD], current)
        verdict = self.gate_at_protocol_boundary("stamped")
        self.assertEqual(verdict[framework.STAMP_FIELD], current)
        self.assertNotIn("stale_verdicts", verdict)
        self.assertEqual(read_record(self.store.db, "gate", "stamped")["payload"][framework.STAMP_FIELD], current)

    def test_a_policy_change_makes_open_task_verdicts_stale_and_blocks(self):
        self.verified("stale")
        recorded = framework.stamp()
        newer = {**recorded, 'framework_commit': 'c' * 40, 'policy_file_sha256': 'd' * 64}
        with patch.object(framework, "stamp", return_value=newer):
            verdict = self.gate_at_protocol_boundary("stale")
        self.assertNotEqual(verdict["status"], "APPROVED")
        labels = [row["verdict"] for row in verdict["stale_verdicts"]]
        self.assertEqual(labels[:2], ["Acceptance", "Review"])
        reason = verdict["stale_verdicts"][1]["reason"]
        self.assertIn(f"Review ran under framework {recorded['framework_commit'][:7]} -> current ccccccc", reason)
        self.assertIn(reason, verdict["blocked_reasons"])
        task = self.tasks.snapshot("stale")
        listed = framework.open_tasks_with_stale_verdicts(self.store, current=newer)
        self.assertEqual([row["task_id"] for row in listed], ["stale"])
        # A shared-runtime update (no project selected) reads every registered project of the state home.
        check, stale, _ = doctor.check_project_state(None, self.base / "state", newer, ROOT)
        self.assertEqual(check["status"], "PASS", check)
        self.assertEqual([(row["task_id"], row["project"]) for row in stale], [("stale", str(self.store.repo))])
        # A registration whose directory was removed has nothing left to update: counted, never "unavailable".
        removed = self.base / "removed"
        removed.mkdir()
        subprocess.run(["git", "init", "-b", "main", str(removed)], check=True, capture_output=True)
        Store(removed, self.base / "state").close()
        shutil.rmtree(removed)
        check, stale, _ = doctor.check_project_state(None, self.base / "state", newer, ROOT)
        self.assertEqual(check["status"], "PASS", check)
        self.assertIn("1 registered project(s) skipped: their directory no longer exists", check["details"])
        self.assertEqual([row["task_id"] for row in stale], ["stale"])
        # A closed task is never re-judged after an update.
        self.assertEqual(framework.stale_verdicts(self.store, {**task, "state": "COMPLETED"}, current=newer), [])

    def test_unstamped_and_matching_receipts(self):
        current = framework.stamp()
        self.assertIsNone(framework.stale_verdict("Review", {"framework": current}, current))
        unstamped = framework.stale_verdict("Review", {"status": "COMPLETE"}, current)
        self.assertIn("Review has no framework stamp", unstamped["reason"])
        self.assertIsNone(unstamped["old_commit"])
        same_policy = {**current, "framework_commit": "e" * 40}
        self.assertIsNone(framework.stale_verdict("Review", {"framework": same_policy}, current))


class DoctorTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="ase-doctor-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.package = self.base / "runtime"
        for name in ("skills", "ase/data"):
            shutil.copytree(ROOT / name, self.package / name, ignore=shutil.ignore_patterns("__pycache__"))

    def names(self, report, status):
        return [row["name"] for row in report["checks"] if row["status"] == status]

    def test_consistent_package_passes_and_a_stale_policy_file_fails(self):
        report = doctor.run(package_root=self.package, run_tests=False, project=self.base, state_home=self.base / "state")
        self.assertEqual(report["status"], "COMPLETE", report)
        self.assertTrue(report["checks"][-1]["details"][0].endswith("no project state yet: nothing to list"))
        # Without a project every project registered in the state home is read (none here).
        shared = doctor.run(package_root=self.package, run_tests=False, state_home=self.base / "state")
        self.assertEqual(shared["status"], "COMPLETE", shared)
        self.assertIn("0 project(s) read", shared["checks"][-1]["details"][-1])
        policy = self.package / doctor.POLICY
        policy.write_bytes(policy.read_bytes().replace(b"Order", b"Ordre", 1))
        report = doctor.run(package_root=self.package, previous_commit="f" * 40, run_tests=False, project=self.base,
                            state_home=self.base / "state")
        self.assertEqual(report["status"], "INCOMPLETE")
        self.assertEqual(report["failing"], ["runtime policy matches the skill text"])
        self.assertIn("Framework update fffffff ->", doctor.text(report))
        self.assertIn("not complete", doctor.text(report))

    def test_mirrors_installed_files_and_unavailable_source(self):
        mirror = self.base / "project/.agents/skills"
        shutil.copytree(self.package / "skills", mirror)
        (mirror / "own-project-skill").mkdir()  # The project's own skill is not compared.
        (mirror / "own-project-skill/SKILL.md").write_text("mine\n", encoding="utf-8")
        installed = self.package / "skills/quality-gate/SKILL.md"
        home = self.base / "home"
        (home / doctor.INSTALLATION).parent.mkdir(parents=True)
        record = {"runtime": str(self.package), "skill_destinations": {"codex": str(mirror)}, "project": str(self.base),
                  "framework_commit": "1" * 40, "framework_dirty": False,
                  "owned_files": [{"path": str(installed), "sha256": doctor._sha(installed)},
                                  {"path": str(home / "AGENTS.md"), "sha256": "0" * 64}]}
        (home / doctor.INSTALLATION).write_text(json.dumps(record), encoding="utf-8")
        report = doctor.run(home=home, package_root=self.package, state_home=self.base / "state", run_tests=True)
        # Owner-editable files outside the framework copies are not judged; tests need the source.
        self.assertEqual(report["status"], "UNVERIFIED", report)
        self.assertEqual(report["new_commit"], "1" * 40)
        self.assertEqual(sorted(report["unavailable"]), ["public-content generic checks pass",
                                                         "review-quality fixture tests pass"])
        (mirror / "quality-gate/SKILL.md").write_text("edited\n", encoding="utf-8")
        installed.write_text("partial\n", encoding="utf-8")
        report = doctor.run(home=home, package_root=self.package, state_home=self.base / "state", run_tests=False)
        self.assertEqual(report["status"], "INCOMPLETE")
        record["framework_dirty"] = True
        (home / doctor.INSTALLATION).write_text(json.dumps(record), encoding="utf-8")
        self.assertIn("installed from a clean framework commit",
                      doctor.run(home=home, package_root=self.package, state_home=self.base / "state", run_tests=False)["unavailable"])
        self.assertEqual(sorted(report["failing"]), ["installed files match the install manifest",
                                                     "skill mirrors are identical"])


    def test_installed_cli_finds_its_installation_and_only_a_clean_source_at_the_installed_commit(self):
        from argparse import Namespace
        from ase.__main__ import _doctor_inputs
        source = self.base / "source"
        source.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main", str(source)], check=True)
        (source / "a.txt").write_text("one", encoding="utf-8")
        subprocess.run(["git", "-C", str(source), "add", "."], check=True)
        subprocess.run(["git", "-C", str(source), "-c", "user.name=t", "-c", "user.email=t@example.invalid",
                        "commit", "-q", "-m", "base"], check=True)
        home = self.base / "home"
        package = home / "autonomous-se/runtime"
        package.mkdir(parents=True)
        record = {"runtime": str(package), "framework_commit": framework.source_commit(source),
                  "framework_dirty": False, "framework_source_root": str(source), "project": None}

        def inputs(explicit=None):
            (home / doctor.INSTALLATION).write_text(json.dumps(record), encoding="utf-8")
            return _doctor_inputs(Namespace(install_home=None, source=explicit, previous_commit=None, project=None,
                                            repo=".", home=None))
        with patch("ase.framework.package_root", return_value=package):
            found = inputs()
            self.assertEqual((found["home"], found["package_root"], found["source_root"]),
                             (home, package, source.resolve()))
            # Uncommitted edits at the same commit were never installed: the source cannot vouch.
            (source / "a.txt").write_text("edited", encoding="utf-8")
            edited = inputs()
            self.assertIsNone(edited["source_root"])
            self.assertIn("uncommitted changes", edited["source_note"])
            # A checkout named on the command line is held to the same rule.
            self.assertIsNone(inputs(explicit=source)["source_root"])
            report = doctor.run(home=home, package_root=self.package, run_tests=True, source_root=None,
                                source_note=edited["source_note"], state_home=self.base / "state")
            self.assertEqual(report["status"], "UNVERIFIED")  # never COMPLETE without the source checks
            self.assertIn("review-quality fixture tests pass", report["unavailable"])
            (source / "a.txt").write_text("one", encoding="utf-8")
            record["framework_dirty"] = True  # Installed from uncommitted changes: no checkout describes it.
            self.assertIsNone(inputs()["source_root"])
            record["framework_dirty"] = None  # An install recorded before this field existed.
            self.assertIn("not recorded", inputs()["source_note"])
            self.assertEqual(inputs(explicit=source)["source_root"], None)
            record["framework_dirty"] = False
            self.assertEqual(inputs(explicit=source)["source_root"], source.resolve())
            record["framework_dirty"], record["framework_commit"] = False, "0" * 40  # The source moved on.
            self.assertIn("no longer at the installed commit", inputs()["source_note"])

    def test_a_self_check_that_cannot_finish_reports_incomplete_and_the_install_result(self):
        from scripts import install as installer
        manifest = {"runtime_home": str(self.base / "home"), "runtime": str(self.package),
                    "framework_commit": "1" * 40, "framework_dirty": False}
        with patch("ase.doctor.run", side_effect=RuntimeError("database is locked")):
            report = installer.post_update_check(manifest, "2" * 40)
        self.assertEqual(report["status"], "INCOMPLETE")
        self.assertEqual(report["failing"], ["self-check completed"])
        self.assertIn("database is locked", doctor.text(report))
        # One unreadable project is listed as unavailable; it never hides the rest or crashes the check.
        with patch("ase.doctor.registered_projects", return_value=(["broken"], [], None)),                 patch("ase.doctor.open_project_store", side_effect=RuntimeError("database disk image is malformed")):
            check, stale, notes = doctor.check_project_state(None, self.base / "state", framework.stamp(), ROOT)
        self.assertEqual(check["status"], doctor.UNAVAILABLE)
        self.assertIn("malformed", " ".join(check["details"]))
        # A corrupt registry database is an unavailable listing, not a traceback.
        (self.base / "state").mkdir(exist_ok=True)
        (self.base / "state" / "registry.sqlite3").write_bytes(b"not a database" * 100)
        with patch("ase.projects.resolve_state_home", return_value=self.base / "state"):
            present, gone, note = doctor.registered_projects(self.base / "state")
        self.assertEqual((present, gone), ([], []))
        self.assertTrue(note.startswith("unavailable"), note)
        # A malformed installation record is a failed check, not a traceback.
        (self.base / "home" / doctor.INSTALLATION).parent.mkdir(parents=True, exist_ok=True)
        (self.base / "home" / doctor.INSTALLATION).write_text(json.dumps({"owned_files": [{}]}), encoding="utf-8")
        self.assertEqual(doctor.check_installed_files(self.base / "home")["status"], doctor.FAIL)


@unittest.skipUnless(shutil.which("git"), "git required")
class MemoryReverifyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="ase-reverify-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.repo = self.base / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-b", "main", str(self.repo)], check=True, capture_output=True)
        self.store = Store(self.repo, self.base / "state")
        self.addCleanup(self.store.close)
        self.framework = inventory(ROOT, parser())
        self.claim = {"topic": "workflow", "observed_at": "2026-01-01T12:00:00+00:00", "source_refs": ["local:notes.txt"]}

    def test_only_notes_with_removed_framework_references_are_listed_and_nothing_changes(self):
        knowledge = Knowledge(self.store)
        with patch("ase.memory.reverify.source_commit", return_value="1" * 40):
            stale = knowledge.record({**self.claim, "statement": "Run `ase task frobnicate T1` and read skills/gone-skill/SKILL.md"})
            current = knowledge.record({**self.claim, "statement": "Run `ase task verify T1 --no-such-flag`"})
            fine = knowledge.record({**self.claim, "statement": "Run `ase task verify T1` per skills/quality-gate/SKILL.md"})
        (self.repo / "docs").mkdir()
        (self.repo / "docs/runbook.md").write_text("project file\n", encoding="utf-8")
        own = knowledge.record({**self.claim, "statement": "The project runbook is docs/runbook.md; `ase status T1`"})
        old = knowledge.record({**self.claim, "statement": "Run `ase task frobnicate` first"})
        knowledge.record({**self.claim, "statement": "Run `ase task verify T1`", "supersedes": old["id"]})
        records = LocalRecords(self.store)
        records.decision("release", "Always run `ase gate-all` before merging", "owner preference")
        self.store.db.execute("INSERT INTO sessions(task_id,started,ended,summary) VALUES(NULL,'t','t',?)",
                              ("Next: `ase status --verbose-old`",))
        write_record(self.store.db, "task_context", "w:t1", {"task_id": "t1", "next_action": "`ase task frobnicate t1`"},
                     expected_revision=0)
        self.store.db.commit()
        before = self.store.db.execute("SELECT statement FROM knowledge_claims ORDER BY id").fetchall()
        with patch("ase.memory.reverify.source_commit", return_value="2" * 40):
            listed = reverify(self.store, self.framework)
        notes = {row["note"]: row for row in listed}
        self.assertEqual(set(notes), {f"knowledge claim {stale['id']}", f"knowledge claim {current['id']}",
                                      "owner decision 1", "session summary 1", "next action of task t1"})
        self.assertNotIn(f"knowledge claim {fine['id']}", notes)
        self.assertNotIn(f"knowledge claim {own['id']}", notes)
        self.assertNotIn(f"knowledge claim {old['id']}", notes)  # Superseded history is not re-listed.
        self.assertEqual(notes[f"knowledge claim {stale['id']}"]["references"],
                         ["command `ase task frobnicate`", "file `skills/gone-skill/SKILL.md`"])
        self.assertEqual(notes[f"knowledge claim {stale['id']}"]["written_under"], "1111111")
        self.assertEqual(notes[f"knowledge claim {current['id']}"]["references"], ["flag `--no-such-flag` of `ase task verify`"])
        self.assertTrue(notes["owner decision 1"]["owner"])
        self.assertIn("kept as recorded", notes["owner decision 1"]["action"])
        self.assertEqual(notes["session summary 1"]["written_under"], framework.UNKNOWN)
        # Notes written under the current framework are not re-listed, and nothing is modified.
        with patch("ase.memory.reverify.source_commit", return_value="1" * 40):
            self.assertNotIn(f"knowledge claim {stale['id']}", {row["note"] for row in reverify(self.store, self.framework)})
        self.assertEqual(self.store.db.execute("SELECT statement FROM knowledge_claims ORDER BY id").fetchall(), before)


@unittest.skipUnless(shutil.which("git"), "git required")
class SourceCheckCacheTests(unittest.TestCase):
    def test_a_passed_source_check_is_reused_only_on_the_identical_source_tree(self):
        with tempfile.TemporaryDirectory(prefix="ase-doctor-cache-") as temporary:
            source = Path(temporary)
            subprocess.run(["git", "init", "-q", "-b", "main", str(source)], check=True)
            (source / ".gitignore").write_text("outputs/\n", encoding="utf-8")
            (source / "a.txt").write_text("one", encoding="utf-8")
            subprocess.run(["git", "-C", str(source), "add", "."], check=True)
            subprocess.run(["git", "-C", str(source), "-c", "user.name=t", "-c", "user.email=t@example.invalid",
                            "commit", "-q", "-m", "base"], check=True)
            runs = []

            def passing(root):
                runs.append("pass")
                return doctor._check("passing", doctor.PASS)

            def failing(root):
                runs.append("fail")
                return doctor._check("failing", doctor.FAIL)

            checks = [("passing", passing), ("failing", failing)]
            first = doctor.cached_source_checks(source, checks)
            again = doctor.cached_source_checks(source, checks)
            self.assertEqual(runs, ["pass", "fail", "fail"])  # a failure is never reused
            self.assertEqual([row["status"] for row in again], [doctor.PASS, doctor.FAIL])
            self.assertTrue(again[0]["details"][-1].startswith("reused: passed on this exact source tree"))
            self.assertEqual(first[0]["details"], [])
            (source / "b.txt").write_text("untracked", encoding="utf-8")  # any change reruns the check
            doctor.cached_source_checks(source, checks[:1])
            (source / "a.txt").write_text("edited", encoding="utf-8")
            doctor.cached_source_checks(source, checks[:1])
            self.assertEqual(runs, ["pass", "fail", "fail", "pass", "pass"])


class LinkedPackageTests(unittest.TestCase):
    def test_a_runtime_reached_through_a_link_still_finds_its_recorded_commit(self):
        with tempfile.TemporaryDirectory(prefix="ase-linked-") as temporary:
            real = Path(temporary) / "real"
            (real / framework.SOURCE_RELATIVE).parent.mkdir(parents=True)
            (real / framework.SOURCE_RELATIVE).write_text(json.dumps({"commit": "8" * 40, "dirty": False}),
                                                          encoding="utf-8")
            link = Path(temporary) / "link"
            try:
                link.symlink_to(real, target_is_directory=True)
            except OSError:
                if not shutil.which("cmd"):
                    self.skipTest("no directory links on this system")
                subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(real)], check=True, capture_output=True)
            self.assertEqual(framework.source_commit(link), "8" * 40)


class InstallerSourceTests(unittest.TestCase):
    def test_installed_runtime_reinstall_keeps_the_recorded_commit_and_untracked_files_are_dirty(self):
        import scripts.install as installer
        with tempfile.TemporaryDirectory(prefix="ase-source-") as temporary:
            runtime = Path(temporary) / "project/.devframework/autonomous-se/runtime"
            (runtime / installer.FRAMEWORK_SOURCE).parent.mkdir(parents=True)
            (runtime / installer.FRAMEWORK_SOURCE).write_text(json.dumps({"commit": "9" * 40, "dirty": False}),
                                                              encoding="utf-8")
            with patch.object(installer, "ROOT", runtime):
                self.assertEqual(installer.framework_source(), {"commit": "9" * 40, "dirty": False})
            checkout = Path(temporary) / "checkout"
            (checkout / "ase").mkdir(parents=True)
            subprocess.run(["git", "init", "-q", str(checkout)], check=True, capture_output=True)
            (checkout / "ase/new_module.py").write_text("VALUE = 1\n", encoding="utf-8")
            with patch.object(installer, "ROOT", checkout):
                self.assertTrue(installer.framework_source()["dirty"])


if __name__ == "__main__":
    unittest.main()
