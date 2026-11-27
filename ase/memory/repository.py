"""Read journal history and task state through the caller's SQLite connection."""
import json


class JournalRepository:
    def __init__(self, store):
        self.store = store

    @staticmethod
    def _event(row):
        return {**json.loads(row["payload"]), "sequence": row["sequence"], "event_key": row["event_key"]}

    def event(self, event_key):
        row = self.store.db.execute(
            "SELECT sequence,event_key,payload FROM runtime_events WHERE event_key=? AND kind='journal'",
            (event_key,)).fetchone()
        return self._event(row) if row is not None else None

    def events(self):
        return [self._event(row) for row in self.store.db.execute(
            "SELECT sequence,event_key,payload FROM runtime_events WHERE kind='journal' ORDER BY sequence")]

    def task_states(self, *, before=None):
        if before is None:
            return {row["id"]: row["status"] for row in self.store.db.execute("SELECT id,status FROM tasks")}
        # Do not apply today's terminal status to an earlier report. Both v1
        # and v2 transitions retain their own UTC occurrence/creation boundary.
        states = {}
        for row in self.store.db.execute(
                "SELECT task_id,status FROM transitions WHERE created < ? ORDER BY created,id", (before,)):
            states[row["task_id"]] = row["status"]
        for row in self.store.db.execute(
                "SELECT task_id,payload FROM runtime_events WHERE kind='transition' AND created < ? ORDER BY created,sequence",
                (before,)):
            states[row["task_id"]] = json.loads(row["payload"])["state"]
        return states
