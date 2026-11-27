"""Portable snapshots retain history but grant no authority in a new workspace."""
from contextlib import closing
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
import warnings
import zipfile

from ase.projects import ProjectRegistry
from ase.state import read_record, write_record
from ase.store import Store, now
from ase.tasks import TaskService
from test_task_contract_v2 import contract

if importlib.util.find_spec("ase.transfer"):
    from ase import transfer
else:
    transfer = None


class TransferTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(transfer, "project transfer implementation is required")
        temporary = tempfile.TemporaryDirectory(prefix="ase-transfer-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.root = self.base / "source"
        self.root.mkdir()
        self.home = self.base / "state"
        with closing(ProjectRegistry(self.home)) as registry:
            self.project = registry.register(self.root, kind="document", timezone="Europe/Bucharest")
        self.store = Store(self.project)
        self.addCleanup(self.store.close)
        self.archive = self.base / "snapshot.zip"

    def seed(self):
        tasks = TaskService(self.store)
        task = tasks.create(contract("portable"))
        task = tasks.propose("portable", {"summary": "Draft the guide", "stage_ids": ["draft"]}, expected_revision=task["revision"])
        tasks.approve("portable", proposal_hash=task["proposal_hash"], user_message_ref="chat:approved", approved_stage_ids=["draft"])
        receipt = {"artifact_root": str(self.store.root / "tasks" / "portable" / "runs" / "old"), "status": "COMPLETE"}
        with self.store.db:
            write_record(self.store.db, "verification", "portable:ac", receipt, expected_revision=0)
            write_record(self.store.db, "operation", "external", {"state": "PREPARED", "task_id": "portable", "action_id": "external"}, expected_revision=0)
            self.store.db.execute("INSERT INTO knowledge_claims(topic,statement,observed_at,source_refs,created) VALUES(?,?,?,?,?)", ("policy", "Review local requirements", now(), "[]", now()))
            write_record(self.store.db, "knowledge_validity", "1", {"validity": "CURRENT"}, expected_revision=0)
            self.store.db.execute("INSERT INTO sessions(task_id,started,summary) VALUES(?,?,?)", ("portable", now(), "Retain this summary"))
        folder = self.store.root / "tasks" / "portable"
        folder.mkdir(parents=True)
        (folder / "check.json").write_text(json.dumps(receipt), encoding="utf-8")
        (folder / "command.log").write_text("Executed 3 checks", encoding="utf-8")
        for name in ("ac.session.log", "stdout.jsonl", "stdin.txt", "ac.raw.json", "request.json", "auth.json"):
            (folder / name).write_text("raw conversation must not travel", encoding="utf-8")
        (self.store.root / "publishing.json").write_text('{"enabled":true}', encoding="utf-8")
        return receipt

    def target(self, name="target"):
        result = self.base / name
        result.mkdir()
        return result

    def test_export_is_consistent_during_wal_writes(self):
        self.seed()
        with self.store.db:
            self.store.db.execute("CREATE TABLE snapshot_pairs(id INTEGER PRIMARY KEY,left_value INTEGER,right_value INTEGER)")
            self.store.db.execute("INSERT INTO snapshot_pairs VALUES(1,0,0)")
        stopped = threading.Event()
        started = threading.Event()
        failures = []
        def writer():
            try:
                with closing(sqlite3.connect(self.store.root / "memory.sqlite3")) as db:
                    started.set()
                    for value in range(1, 10000):
                        if stopped.is_set():
                            break
                        with db:
                            db.execute("UPDATE snapshot_pairs SET left_value=?,right_value=?", (value, value))
            except BaseException as error:
                failures.append(error)
        thread = threading.Thread(target=writer)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(stopped.set)
        self.assertTrue(started.wait(3))
        try:
            manifest = transfer.export_project(self.project, self.archive)
        finally:
            stopped.set()
            thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(failures, [])
        with zipfile.ZipFile(self.archive) as archive:
            persisted = json.loads(archive.read("manifest.json"))
            self.assertEqual(manifest["project"], persisted["project"])
            for item in persisted["files"]:
                data = archive.read(item["path"])
                self.assertEqual(len(data), item["bytes"])
                self.assertEqual(hashlib.sha256(data).hexdigest(), item["sha256"])
            snapshot = self.base / "read-snapshot.sqlite3"
            snapshot.write_bytes(archive.read("memory.sqlite3"))
        with closing(sqlite3.connect(snapshot)) as db:
            self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            left, right = db.execute("SELECT left_value,right_value FROM snapshot_pairs").fetchone()
            self.assertEqual(left, right)
            operation = json.loads(db.execute("SELECT payload FROM runtime_records WHERE kind='operation'").fetchone()[0])
            self.assertEqual(operation["state"], "UNKNOWN")

    def test_export_excludes_auth_raw_and_verbose_by_default(self):
        self.seed()
        transfer.export_project(self.project, self.archive)
        with zipfile.ZipFile(self.archive) as archive:
            names = archive.namelist()
            self.assertIn("tasks/portable/check.json", names)
            self.assertFalse(any(name.endswith((".log", ".jsonl", ".raw.json", "auth.json", "publishing.json", "stdin.txt", "request.json")) for name in names))
        verbose = self.base / "verbose.zip"
        transfer.export_project(self.project, verbose, include_verbose=True)
        with zipfile.ZipFile(verbose) as archive:
            self.assertIn("tasks/portable/command.log", archive.namelist())
            self.assertNotIn("tasks/portable/ac.session.log", archive.namelist())

    def test_copy_does_not_revive_approval(self):
        receipt = self.seed()
        transfer.export_project(self.project, self.archive)
        imported = transfer.import_project(self.archive, self.home, root=self.target())
        self.assertNotEqual(imported.project_id, self.project.project_id)
        self.assertNotEqual(imported.workspace_id, self.project.workspace_id)
        with closing(Store(imported)) as store:
            origin = read_record(store.db, "import_origin", "origin")["payload"]
            self.assertEqual(origin["origin_project_id"], self.project.project_id)
            self.assertEqual(read_record(store.db, "historical_verification", "portable:ac")["payload"], receipt)
            self.assertIsNone(read_record(store.db, "approval", "portable")["payload"])
            task = TaskService(store).snapshot("portable")
            self.assertEqual(task["state"], "AWAITING_APPROVAL")
            self.assertEqual(task["approved_stage_ids"], [])
            self.assertIsNone(task["proposal_hash"])
            with self.assertRaisesRegex(ValueError, "approval"):
                TaskService(store).require_approval("portable", "draft")
            with self.assertRaisesRegex(ValueError, "hash"):
                TaskService(store).approve("portable", proposal_hash="old", user_message_ref="old", approved_stage_ids=["draft"])
            self.assertEqual(read_record(store.db, "operation", "external")["payload"]["state"], "UNKNOWN")
            self.assertEqual(read_record(store.db, "knowledge_validity", "1")["payload"]["validity"], "UNKNOWN")
            self.assertEqual(store.db.execute("SELECT summary FROM sessions").fetchone()[0], "Retain this summary")
            self.assertFalse((store.root / "tasks").exists())
            historical = store.root / "historical" / "tasks" / "portable" / "check.json"
            self.assertEqual(json.loads(historical.read_text()), receipt)

    def test_restore_retains_uuid_only_without_collision(self):
        self.seed()
        transfer.export_project(self.project, self.archive)
        with self.assertRaisesRegex(ValueError, "CONFLICT"):
            transfer.import_project(self.archive, self.home, root=self.target("collision"), mode="restore")
        restored = transfer.import_project(self.archive, self.base / "restored-state", root=self.target("restored"), mode="restore")
        self.assertEqual(restored.project_id, self.project.project_id)
        self.assertNotEqual(restored.workspace_id, self.project.workspace_id)
        with closing(Store(restored)) as store:
            self.assertEqual(store.db.execute("SELECT count(*) FROM sessions").fetchone()[0], 1)

    def rewrite(self, mutate):
        with zipfile.ZipFile(self.archive) as source:
            entries = [(entry, source.read(entry)) for entry in source.infolist()]
        changed = self.base / "changed.zip"
        with zipfile.ZipFile(changed, "w") as target:
            mutate(entries)
            for entry, data in entries:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", UserWarning)
                    target.writestr(entry, data)
        return changed

    def assert_unregistered(self, root, home=None):
        with closing(ProjectRegistry(home or self.home)) as registry:
            with self.assertRaisesRegex(ValueError, "Unknown"):
                registry.resolve(str(root))

    def test_import_rejects_escape_and_divergent_restore(self):
        self.seed()
        transfer.export_project(self.project, self.archive)
        for name in ("../escaped", "/absolute", "C:/outside", "tasks/../escape", "tasks\\escape", "tasks/CON/file", "tasks/portable/check.json:stream"):
            with self.subTest(name=name):
                def inject(entries):
                    entries.append((zipfile.ZipInfo(name), b"escape"))
                root = self.target("target-" + str(len(list(self.base.glob("target-*")))))
                with self.assertRaises(ValueError):
                    transfer.import_project(self.rewrite(inject), self.home, root=root)
                self.assert_unregistered(root)
        self.assertFalse((self.base / "escaped").exists())
        before = (self.store.root / "memory.sqlite3").read_bytes()
        with self.assertRaisesRegex(ValueError, "CONFLICT"):
            transfer.import_project(self.archive, self.home, root=self.root)
        self.assertEqual((self.store.root / "memory.sqlite3").read_bytes(), before)

    def test_import_rejects_hash_tamper_links_and_duplicate_members(self):
        self.seed()
        transfer.export_project(self.project, self.archive)
        def tamper(entries):
            entries[:] = [(entry, b"tampered" if entry.filename.endswith("check.json") else data) for entry, data in entries]
        def symlink(entries):
            link = zipfile.ZipInfo("tasks/portable/link.json")
            link.create_system = 3
            link.external_attr = (stat.S_IFLNK | 0o777) << 16
            entries.append((link, b"/outside"))
        def hardlink(entries):
            link = zipfile.ZipInfo("tasks/portable/link.json")
            link.create_system = 3
            link.external_attr = (stat.S_IFREG | 0o600) << 16
            link.extra = b"\x0d\x00\x08\x00outside!"
            entries.append((link, b""))
        def duplicate(entries):
            entries.append(entries[0])
        for mutation in (tamper, symlink, hardlink, duplicate):
            with self.subTest(mutation=mutation.__name__):
                root = self.target(mutation.__name__)
                with self.assertRaises(ValueError):
                    transfer.import_project(self.rewrite(mutation), self.home, root=root)
                self.assert_unregistered(root)

    def test_activation_failure_rolls_back_only_new_managed_state(self):
        self.seed()
        transfer.export_project(self.project, self.archive)
        target = self.target()
        marker = target / "existing-user-file.txt"
        marker.write_text("keep", encoding="utf-8")
        original = set((self.home / "projects").iterdir())
        with patch.object(transfer, "_activate_import", side_effect=RuntimeError("activation interrupted")):
            with self.assertRaisesRegex(RuntimeError, "activation interrupted"):
                transfer.import_project(self.archive, self.home, root=target)
        self.assert_unregistered(target)
        self.assertEqual(set((self.home / "projects").iterdir()), original)
        self.assertEqual(marker.read_text(), "keep")
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM sessions").fetchone()[0], 1)
        imported = transfer.import_project(self.archive, self.home, root=target)
        with closing(Store(imported)) as store:
            self.assertEqual(store.db.execute("SELECT count(*) FROM sessions").fetchone()[0], 1)

    def test_export_rejects_existing_destination_and_linked_artifacts(self):
        self.seed()
        self.archive.write_bytes(b"keep")
        with self.assertRaisesRegex(ValueError, "CONFLICT"):
            transfer.export_project(self.project, self.archive)
        self.assertEqual(self.archive.read_bytes(), b"keep")
        self.archive.unlink()
        path = self.store.root / "tasks" / "portable" / "check.json"
        os.link(path, self.base / "hardlinked.json")
        with self.assertRaises(ValueError):
            transfer.export_project(self.project, self.archive)
        self.assertFalse(self.archive.exists())

    def test_export_rejects_credentials_without_rewriting_history(self):
        self.seed()
        fixture_value = "=".join(("password", "private-value-must-stay-local"))
        with self.store.db:
            self.store.db.execute("UPDATE sessions SET summary=?", (fixture_value,))
        with self.assertRaisesRegex(ValueError, "credential"):
            transfer.export_project(self.project, self.archive)
        self.assertFalse(self.archive.exists())
        self.assertEqual(self.store.db.execute("SELECT summary FROM sessions").fetchone()[0], fixture_value)

    def test_import_rejects_excess_bounds_and_database_identity_tamper(self):
        self.seed()
        transfer.export_project(self.project, self.archive)
        target = self.target()
        with patch.object(transfer, "MAX_TOTAL_BYTES", 10):
            with self.assertRaisesRegex(ValueError, "bound"):
                transfer.import_project(self.archive, self.home, root=target)
        def tamper_identity(entries):
            manifest = json.loads(next(data for entry, data in entries if entry.filename == "manifest.json"))
            manifest["project"]["project_id"] = "00000000-0000-4000-8000-000000000001"
            entries[:] = [(entry, json.dumps(manifest).encode() if entry.filename == "manifest.json" else data) for entry, data in entries]
        with self.assertRaisesRegex(ValueError, "identity"):
            transfer.import_project(self.rewrite(tamper_identity), self.home, root=target)
        self.assert_unregistered(target)

    def test_export_snapshot_preserves_original_unresolved_operation_state(self):
        self.seed()
        transfer.export_project(self.project, self.archive)
        with zipfile.ZipFile(self.archive) as archive:
            database = self.base / "operation-snapshot.sqlite3"
            database.write_bytes(archive.read("memory.sqlite3"))
        with closing(sqlite3.connect(database)) as db:
            historical = read_record(db, "historical_operation", "external")["payload"]
            self.assertIsNotNone(historical)
            self.assertEqual(historical["state"], "PREPARED")
        self.assertEqual(read_record(self.store.db, "operation", "external")["payload"]["state"], "PREPARED")

    def test_invalid_archive_does_not_create_state_home(self):
        invalid = self.base / "invalid.zip"
        invalid.write_bytes(b"not a zip")
        home = self.base / "unused-state"
        with self.assertRaises(ValueError):
            transfer.import_project(invalid, home, root=self.target())
        self.assertFalse(home.exists())

    def test_export_cannot_capture_its_own_state_destination(self):
        self.seed()
        with self.assertRaisesRegex(ValueError, "project state"):
            transfer.export_project(self.project, self.store.root / "nested.zip")

    def test_copy_can_be_exported_again_without_losing_older_artifacts(self):
        receipt = self.seed()
        transfer.export_project(self.project, self.archive)
        first = transfer.import_project(self.archive, self.home, root=self.target("first-copy"))
        second_archive = self.base / "second.zip"
        transfer.export_project(first, second_archive)
        with zipfile.ZipFile(second_archive) as archive:
            self.assertIn("historical/tasks/portable/check.json", archive.namelist())
            self.assertIn("historical/memory.sqlite3", archive.namelist())
        second = transfer.import_project(second_archive, self.home, root=self.target("second-copy"))
        with closing(Store(second)) as store:
            self.assertEqual(store.db.execute("SELECT summary FROM sessions").fetchone()[0], "Retain this summary")
            self.assertEqual(json.loads((store.root / "historical" / "historical" / "tasks" / "portable" / "check.json").read_text()), receipt)
            with self.assertRaisesRegex(ValueError, "approval"):
                TaskService(store).require_approval("portable", "draft")

    def test_process_crash_preserves_history_and_retry_identifies_orphan(self):
        self.seed()
        transfer.export_project(self.project, self.archive)
        target = self.target()
        program = ("import os\nfrom pathlib import Path\nfrom ase import transfer\n"
                   "transfer._activate_import=lambda *args: os._exit(86)\n"
                   f"transfer.import_project(Path({str(self.archive)!r}),Path({str(self.home)!r}),root=Path({str(target)!r}))\n")
        crashed = subprocess.run([sys.executable, "-c", program], capture_output=True, text=True, timeout=15)
        self.assertEqual(crashed.returncode, 86, crashed.stderr)
        self.assert_unregistered(target)
        orphans = [folder for folder in (self.home / "projects").iterdir() if folder.name != self.project.storage_key and not folder.name.startswith(".import-")]
        self.assertEqual(len(orphans), 1)
        with closing(sqlite3.connect(orphans[0] / "memory.sqlite3")) as db:
            self.assertEqual(db.execute("SELECT summary FROM sessions").fetchone()[0], "Retain this summary")
        with self.assertRaisesRegex(ValueError, "CONFLICT") as rejected:
            transfer.import_project(self.archive, self.home, root=target)
        self.assertIn(str(orphans[0]), str(rejected.exception))
        self.assertTrue((orphans[0] / "historical" / "memory.sqlite3").is_file())


if __name__ == "__main__":
    unittest.main()


class TransferLifecycleBoundaryTests(unittest.TestCase):
    setUp = TransferTests.setUp
    seed = TransferTests.seed
    target = TransferTests.target
    rewrite = TransferTests.rewrite

    def test_unknown_timezone_is_rejected_before_target_activation(self):
        self.seed()
        transfer.export_project(self.project,self.archive)
        def change(entries):
            for index,(entry,data) in enumerate(entries):
                if entry.filename=='manifest.json':
                    value=json.loads(data)
                    value['project']['timezone']='Unknown/Audit_Zone'
                    entries[index]=(entry,json.dumps(value).encode())
        archive=self.rewrite(change)
        home=self.base/'fresh-state'
        root=self.target('unknown-zone')
        with self.assertRaisesRegex(ValueError,'TZ_DATA_UNAVAILABLE'):
            transfer.import_project(archive,home,root=root)
        self.assertFalse(home.exists(),'Invalid timezone must not reserve any identity')
        with closing(ProjectRegistry(home)) as registry:
            current=registry.register(root,kind='document',timezone='UTC')
            self.assertEqual(current.timezone,'UTC')

    def test_uncertain_push_history_remains_fenced_after_import(self):
        self.seed()
        states=('UNKNOWN','CONFIRMED','APPLIED','ABSENT')
        originals={}
        with self.store.db:
            for number,state in enumerate(states):
                key=format(number+1,'064x')
                value={'version':1,'state':state,'action_id':key,'attempt_number':1,
                    'intent':{'project_id':self.project.project_id,'workspace_id':self.project.workspace_id,
                              'task_id':'portable','url':'https://example.invalid/repository.git',
                              'ref':'refs/heads/feature/audit','commit':'a'*40}}
                if state=='CONFIRMED':value['confirmation']={'historical':'cached pre-import observation'}
                originals[key]=value
                write_record(self.store.db,'publication_push',key,value,expected_revision=0)
        transfer.export_project(self.project,self.archive)
        context=transfer.import_project(self.archive,self.base/'push-state',root=self.target('push-target'))
        with closing(Store(context)) as store:
            for key,old in originals.items():
                current=read_record(store.db,'publication_push',key)['payload']
                self.assertIsNotNone(current,'An uncertain push must not disappear into passive history')
                self.assertEqual(current['state'],old['state'] if old['state'] in {'APPLIED','ABSENT'} else 'UNKNOWN')
                self.assertEqual(current['intent'],old['intent'],'Retain original target/owner, never rebind authority')
                self.assertNotIn('confirmation',current)
                self.assertEqual(read_record(store.db,'historical_publication_push',key)['payload'],old)
            self.assertIsNone(read_record(store.db,'approval','portable')['payload'])

    def test_supported_reporting_timezone_survives_import(self):
        from ase.journal import Journal
        self.seed()
        transfer.export_project(self.project,self.archive)
        context=transfer.import_project(self.archive,self.base/'known-state',root=self.target('known-zone'))
        self.assertEqual(context.timezone,'Europe/Bucharest')
        with closing(Store(context)) as store:
            report=Journal(store).meeting_report('2026-10-25','2026-10-26',timezone=store.project.timezone)
            self.assertEqual(report['timezone'],'Europe/Bucharest')

    def test_import_preserves_event_counter_before_new_closure_and_proposal(self):
        from ase.continuity import Continuity
        from ase.journal import Journal
        tasks=TaskService(self.store)
        task=tasks.create(contract('portable'))
        task=tasks.transition('portable','BLOCKED',expected_revision=task['revision'],reason='Original required decision')
        old_revision=task['revision']
        history=[tuple(row) for row in self.store.db.execute('SELECT event_key,kind,task_id,payload,created FROM runtime_events ORDER BY sequence')]
        transfer.export_project(self.project,self.archive)
        archive=self.archive
        for number in (1,2):
            context=transfer.import_project(archive,self.base/('state-'+str(number)),root=self.target('target-'+str(number)))
            with closing(Store(context)) as store:
                service=TaskService(store)
                task=service.snapshot('portable')
                self.assertEqual(task['state'],'AWAITING_APPROVAL')
                self.assertIsNone(read_record(store.db,'approval','portable')['payload'])
                self.assertIsNone(read_record(store.db,'verification','portable:ac')['payload'])
                journal=Journal(store)
                journal.start_session('portable','imported-'+str(number))
                closure=journal.finish_session('portable','imported-'+str(number),{
                    'finish_reason':'blocked','stage':'imported','summary':'Imported history awaits owner approval',
                    'next_action':'Review original requirements and approve a current proposal','context':{'summary':'Imported history requires fresh approval'},'blockers':[]},
                    closure_key='portable:imported-closure-'+str(number),
                    expected_revision=Continuity(store).snapshot('portable')['revision'],expected_context_revision=journal.context()['revision'])
                self.assertIsNotNone(closure)
                task=service.snapshot('portable')
                self.assertGreater(task['revision'],old_revision,'New closure must continue beyond immutable historical events')
                task=service.propose('portable',{'summary':'Current imported proposal '+str(number),'stage_ids':['draft']},expected_revision=task['revision'])
                self.assertEqual(task['approved_stage_ids'],[])
                current=[tuple(row) for row in store.db.execute('SELECT event_key,kind,task_id,payload,created FROM runtime_events ORDER BY sequence')]
                self.assertEqual(current[:len(history)],history,'Immutable original history must be preserved')
                self.assertEqual(len({row[0] for row in current}),len(current))
                old_revision=task['revision']
                archive=self.base/('repeat-'+str(number)+'.zip')
                transfer.export_project(context,archive)


class ImportRevisionHistoryTests(unittest.TestCase):
    """Fresh authorization must append beyond passive imported history."""
    setUp = TransferTests.setUp
    seed = TransferTests.seed
    target = TransferTests.target

    @staticmethod
    def history(store):
        return [tuple(row) for row in store.db.execute(
            'SELECT event_key,kind,task_id,payload,created FROM runtime_events ORDER BY sequence')]

    def close_and_approve(self, store, label, task_high, approval_high):
        from ase.continuity import Continuity
        from ase.journal import Journal
        service = TaskService(store)
        task = service.snapshot('portable')
        self.assertEqual(task['state'], 'AWAITING_APPROVAL')
        self.assertEqual(task['approved_stage_ids'], [])
        self.assertIsNone(task['proposal_hash'])
        self.assertIsNone(read_record(store.db, 'approval', 'portable')['payload'])
        self.assertFalse(store.db.execute(
            "SELECT 1 FROM runtime_records WHERE kind='approval_version'").fetchone())
        with self.assertRaisesRegex(ValueError, 'approval'):
            service.require_approval('portable', 'draft')
        retained = self.history(store)
        imported_task_revision = task['revision']
        imported_approval_revision = read_record(store.db, 'approval', 'portable')['revision']
        try:
            journal = Journal(store)
            journal.start_session('portable', label)
            journal.finish_session('portable', label, {
                'finish_reason': 'blocked', 'summary': 'Review imported obligations',
                'next_action': 'Approve the current draft proposal',
                'context': {'summary': 'Imported authorization is historical'}, 'blockers': []},
                closure_key='portable:' + label,
                expected_revision=Continuity(store).snapshot('portable')['revision'],
                expected_context_revision=journal.context()['revision'])
            task = service.snapshot('portable')
            task = service.propose('portable', {'summary': 'Fresh imported draft ' + label,
                'stage_ids': ['draft']}, expected_revision=task['revision'])
            task = service.approve('portable', proposal_hash=task['proposal_hash'],
                user_message_ref='chat:fresh-import-' + label, approved_stage_ids=['draft'])
        except ValueError as error:
            self.fail('Fresh imported closure/proposal/approval must append without collision: ' + str(error))
        self.assertGreater(imported_task_revision, task_high)
        self.assertGreaterEqual(imported_approval_revision, approval_high)
        self.assertGreater(task['revision'], task_high)
        approval = read_record(store.db, 'approval', 'portable')
        self.assertGreater(approval['revision'], approval_high)
        self.assertEqual(approval['payload']['user_message_ref'], 'chat:fresh-import-' + label)
        self.assertEqual(service.require_approval('portable', 'draft')['state'], 'READY')
        current = self.history(store)
        self.assertEqual(current[:len(retained)], retained)
        self.assertEqual(len({row[0] for row in current}), len(current))
        return task['revision'], approval['revision']

    def test_approved_task_can_close_and_gain_fresh_approval_after_copy_restore_and_recopy(self):
        self.seed()
        task_high = TaskService(self.store).snapshot('portable')['revision']
        approval_high = read_record(self.store.db, 'approval', 'portable')['revision']
        original = self.history(self.store)
        transfer.export_project(self.project, self.archive)
        archive = self.archive
        for number, mode in enumerate(('copy', 'restore', 'copy'), 1):
            with self.subTest(mode=mode, number=number):
                context = transfer.import_project(archive, self.base / ('fresh-state-' + str(number)),
                    root=self.target('fresh-target-' + str(number)), mode=mode)
                with closing(Store(context)) as store:
                    self.assertEqual(self.history(store)[:len(original)], original)
                    task_high, approval_high = self.close_and_approve(
                        store, 'fresh-' + str(number), task_high, approval_high)
                    archive = self.base / ('fresh-copy-' + str(number) + '.zip')
                    transfer.export_project(context, archive)

    def test_invalidated_approval_tombstone_retains_its_counter_without_its_grant(self):
        self.seed()
        service = TaskService(self.store)
        task = service.snapshot('portable')
        task = service.propose('portable', {'summary': 'Changed source proposal', 'stage_ids': ['draft']},
            expected_revision=task['revision'])
        tombstone = read_record(self.store.db, 'approval', 'portable')
        self.assertIsNone(tombstone['payload'])
        transfer.export_project(self.project, self.archive)
        context = transfer.import_project(self.archive, self.base / 'tombstone-state',
            root=self.target('tombstone-target'))
        with closing(Store(context)) as store:
            self.close_and_approve(store, 'invalidated', task['revision'], tombstone['revision'])

    def legacy_reset(self):
        """Retain real source records/events behind the counters old imports reset."""
        self.seed()
        service = TaskService(self.store)
        for number in (1, 2):
            task = service.snapshot('portable')
            task = service.propose('portable', {'summary': 'Original revision ' + str(number),
                'stage_ids': ['draft']}, expected_revision=task['revision'])
            service.approve('portable', proposal_hash=task['proposal_hash'],
                user_message_ref='chat:original-' + str(number), approved_stage_ids=['draft'])
        task = service.snapshot('portable')
        task = service.propose('portable', {'summary': 'Original invalidated final proposal',
            'stage_ids': ['draft']}, expected_revision=task['revision'])
        high = (task['revision'], read_record(self.store.db, 'approval', 'portable')['revision'])
        original = self.history(self.store)
        transfer.export_project(self.project, self.archive)
        context = transfer.import_project(self.archive, self.base / 'legacy-state',
            root=self.target('legacy-target'))
        return context, high, original

    def test_legacy_reset_counters_recover_from_all_historical_record_tiers(self):
        context, high, original = self.legacy_reset()
        with closing(Store(context)) as store:
            with store.db:
                # Old copies retained the true high-water only as passive history.
                for kind in ('task', 'approval'):
                    row = store.db.execute('SELECT key,revision,payload,updated FROM runtime_records '
                        'WHERE kind=? AND key=?', ('historical_' + kind, 'portable')).fetchone()
                    store.db.execute('INSERT INTO runtime_records VALUES(?,?,?,?,?)',
                        ('historical_historical_' + kind, *tuple(row)))
                    store.db.execute('UPDATE runtime_records SET revision=1 WHERE kind=? AND key=?',
                        ('historical_' + kind, 'portable'))
                store.db.execute("UPDATE runtime_records SET revision=1 WHERE kind='task' AND key='portable'")
                store.db.execute("DELETE FROM runtime_records WHERE kind='approval' AND key='portable'")
            before = [tuple(row) for row in store.db.execute("SELECT * FROM runtime_records WHERE kind LIKE 'historical_%' ORDER BY kind,key")]
            archive = self.base / 'legacy-reset.zip'
            transfer.export_project(context, archive)
        current = transfer.import_project(archive, self.base / 'legacy-recovered-state',
            root=self.target('legacy-recovered-target'))
        with closing(Store(current)) as store:
            self.assertEqual(self.history(store)[:len(original)], original)
            for row in before:
                self.assertEqual(tuple(store.db.execute('SELECT * FROM runtime_records WHERE kind=? AND key=?', row[:2]).fetchone()), row)
            self.close_and_approve(store, 'legacy-restored', *high)

    def test_legacy_reset_counters_recover_from_canonical_events_when_record_counters_lag(self):
        context, high, original = self.legacy_reset()
        # Preserved canonical events are independently sufficient when old record
        # snapshots lag behind the immutable event history.
        with closing(Store(context)) as store:
            with store.db:
                for row in store.db.execute('SELECT kind,key FROM runtime_records').fetchall():
                    kind = row[0]
                    while kind.startswith('historical_'):
                        kind = kind[11:]
                    if kind in {'task', 'approval'}:
                        store.db.execute('UPDATE runtime_records SET revision=1 WHERE kind=? AND key=?', tuple(row))
                    elif kind in {'task_version', 'approval_version'}:
                        store.db.execute('DELETE FROM runtime_records WHERE kind=? AND key=?', tuple(row))
                store.db.execute("DELETE FROM runtime_records WHERE kind='approval' AND key='portable'")
                from ase.state import append_event
                append_event(store.db, 'portable:transition:21', 'transition', 'portable',
                    {'state': 'BLOCKED', 'reason': 'Actual preserved legacy transition'})
                append_event(store.db, 'portable:approval:23', 'approval', 'portable',
                    {'user_message_ref': 'chat:preserved-legacy-approval'})
            events = self.history(store)
            archive = self.base / 'legacy-events.zip'
            transfer.export_project(context, archive)
        current = transfer.import_project(archive, self.base / 'event-recovered-state',
            root=self.target('event-recovered-target'))
        with closing(Store(current)) as store:
            self.assertEqual(self.history(store), events)
            self.close_and_approve(store, 'event-restored', 21, 23)
