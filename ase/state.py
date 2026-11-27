"""Transactional JSON records shared by the local workflow services."""
from contextlib import contextmanager
import hashlib
import json
import re
import uuid

from .store import now


def encode(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def hash_value(value):
    return hashlib.sha256(encode(value).encode("utf-8")).hexdigest()


def text(value, name, *, limit=20000):
    if not isinstance(value, str) or not value.strip() or len(value) > limit or "\0" in value:
        raise ValueError(name + " must be a nonempty bounded string")
    # Keep the same credential policy as legacy records without a module cycle.
    from .core import redact
    if redact(value) != value:
        raise ValueError(name + " must not contain credentials")
    return value


def identifier(value, name="identifier"):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", value):
        raise ValueError("Invalid " + name)
    return value


@contextmanager
def transaction(db, *, immediate=True):
    owner = not db.in_transaction
    savepoint = "ase_" + uuid.uuid4().hex
    if owner:
        db.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    else:
        db.execute("SAVEPOINT " + savepoint)
    try:
        yield db
    except BaseException:
        if owner:
            db.rollback()
        else:
            db.execute("ROLLBACK TO SAVEPOINT " + savepoint)
            db.execute("RELEASE SAVEPOINT " + savepoint)
        raise
    else:
        if owner:
            db.commit()
        else:
            db.execute("RELEASE SAVEPOINT " + savepoint)


def read_record(db, kind, key):
    row = db.execute("SELECT revision,payload,updated FROM runtime_records WHERE kind=? AND key=?", (kind, key)).fetchone()
    return {"revision": row[0], "payload": json.loads(row[1]), "updated": row[2]} if row else {"revision": 0, "payload": None, "updated": None}


def write_record(db, kind, key, payload, *, expected_revision):
    if type(expected_revision) is not int or expected_revision < 0:
        raise ValueError("expected_revision must be a nonnegative integer")
    encoded = encode(payload)
    current = read_record(db, kind, key)
    if current["revision"] != expected_revision:
        raise ValueError("Stale " + kind + " revision; reload and reconcile")
    db.execute("INSERT INTO runtime_records VALUES(?,?,?,?,?) ON CONFLICT(kind,key) DO UPDATE SET revision=excluded.revision,payload=excluded.payload,updated=excluded.updated",
               (kind, key, expected_revision + 1, encoded, now()))
    return read_record(db, kind, key)


def append_event(db, event_key, kind, task_id, payload):
    text(event_key, "event_key", limit=300)
    encoded = encode(payload)
    previous = db.execute("SELECT sequence,kind,task_id,payload FROM runtime_events WHERE event_key=?", (event_key,)).fetchone()
    if previous:
        if (previous[1], previous[2], previous[3]) != (kind, task_id, encoded):
            raise ValueError("CONFLICT: event key already describes another event")
        return previous[0]
    cursor = db.execute("INSERT INTO runtime_events(event_key,kind,task_id,payload,created) VALUES(?,?,?,?,?)",
                        (event_key, kind, task_id, encoded, now()))
    return cursor.lastrowid
