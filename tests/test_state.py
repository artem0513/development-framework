from pathlib import Path
import tempfile
import unittest

from ase.state import read_record, transaction, write_record
from ase.store import Store


class TransactionTests(unittest.TestCase):
    def test_nested_failure_rolls_back_without_discarding_outer_write(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "work").mkdir()
            store = Store(root / "work", root / "state")
            try:
                with transaction(store.db):
                    write_record(store.db, "test", "outer", {"kept": True}, expected_revision=0)
                    try:
                        with transaction(store.db):
                            write_record(store.db, "test", "inner", {"partial": True}, expected_revision=0)
                            raise ValueError("interrupted atomic service")
                    except ValueError:
                        pass
                self.assertEqual(read_record(store.db, "test", "outer")["payload"], {"kept": True})
                self.assertIsNone(read_record(store.db, "test", "inner")["payload"])
            finally:
                store.close()

    def test_store_transition_waits_for_caller_rollback(self):
        from ase.store import now
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "work").mkdir()
            store = Store(root / "work", root / "state")
            try:
                with store.db:
                    store.db.execute("INSERT INTO tasks VALUES(?,?,?,?,?,?)",
                        ("legacy", "Request", "PLANNED", "{}", now(), now()))
                with self.assertRaisesRegex(RuntimeError, "later failure"):
                    with transaction(store.db):
                        write_record(store.db, "test", "outer", {"uncommitted": True}, expected_revision=0)
                        store.transition("legacy", "TESTING", "Caller-owned operation")
                        raise RuntimeError("later failure")
                self.assertEqual(store.task("legacy")["status"], "PLANNED")
                self.assertIsNone(read_record(store.db, "test", "outer")["payload"])
                self.assertEqual(store.db.execute("SELECT count(*) FROM transitions").fetchone()[0], 0)
            finally:
                store.close()

    def test_failed_store_transition_preserves_caller_write(self):
        import sqlite3
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "work").mkdir()
            store = Store(root / "work", root / "state")
            try:
                with transaction(store.db):
                    write_record(store.db, "test", "outer", {"kept": True}, expected_revision=0)
                    with self.assertRaises(sqlite3.IntegrityError):
                        store.transition("missing", "TESTING")
                    self.assertTrue(store.db.in_transaction)
                    write_record(store.db, "test", "later", {"kept": True}, expected_revision=0)
                self.assertEqual(read_record(store.db, "test", "outer")["payload"], {"kept": True})
                self.assertEqual(read_record(store.db, "test", "later")["payload"], {"kept": True})
                self.assertEqual(store.db.execute("SELECT count(*) FROM transitions").fetchone()[0], 0)
            finally:
                store.close()

    def test_standalone_store_transition_commits_status_and_history(self):
        from ase.store import now
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "work").mkdir()
            store = Store(root / "work", root / "state")
            try:
                with store.db:
                    store.db.execute("INSERT INTO tasks VALUES(?,?,?,?,?,?)",
                        ("legacy", "Request", "PLANNED", "{}", now(), now()))
                store.transition("legacy", "TESTING", "Standalone operation")
                self.assertFalse(store.db.in_transaction)
            finally:
                store.close()
            store = Store(root / "work", root / "state")
            try:
                self.assertEqual(store.task("legacy")["status"], "TESTING")
                self.assertEqual(tuple(store.db.execute("SELECT status,detail FROM transitions").fetchone()),
                                 ("TESTING", "Standalone operation"))
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
