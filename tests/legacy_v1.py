"""Write a v1 task record the way the retired v1 engine stored it, for history-readability tests.

The v1 engine (start/check/verify/run/gate/publish) is retired; existing installations still hold
such rows and task folders, so tests recreate that stored shape instead of calling removed code.
"""
import json

from ase.core import save
from ase.store import now


def make_legacy_task(store, contract, *, status="PLANNED", files=None):
    """Insert a v1 task row, its PLANNED transition and the task folder files; return the folder."""
    task_id = contract["id"]
    stored = {key: value for key, value in contract.items() if key != "id"}
    stored.setdefault("risk", "MEDIUM")
    stored.setdefault("protected_paths", [])
    stored.setdefault("max_attempts", 2)
    with store.db:
        store.db.execute("INSERT INTO tasks VALUES(?,?,?,?,?,?)",
                         (task_id, stored["request"], status, json.dumps(stored), now(), now()))
    store.transition(task_id, status, "Confirmed task contract recorded")
    folder = store.root / "tasks" / task_id
    folder.mkdir(parents=True)
    save(folder / "contract.json", stored)
    save(folder / "baseline.json", {"fingerprint": "legacy", "files": {}, "commit": None, "dirty": ""})
    for name, value in (files or {}).items():
        save(folder / name, value)
    return folder
