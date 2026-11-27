"""Captured owner prompts so approvals can cite a verifiable `prompt:<id>` reference.

A UserPromptSubmit hook pipes each submitted prompt here. Records live in the local state
database, outside the repository. This is local reliability: a same-user process could also
write records, so it proves "this text was captured", not a separate human identity.
"""
import hashlib
import json

from .state import read_record, write_record, transaction, now

KIND = 'user_prompt'
PREFIX = 'prompt:'
MAX_TEXT = 20000


def capture(store, event):
    prompt = event.get('prompt') if isinstance(event, dict) else None
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError('Hook event has no prompt text')
    digest = hashlib.sha256(prompt.encode('utf-8')).hexdigest()
    session, captured = str(event.get('session_id') or ''), now()
    # Each submission is its own record: a repeated "yes" later in another session is a new decision.
    key = hashlib.sha256('|'.join((session, captured, digest)).encode('utf-8')).hexdigest()[:16]
    payload = {'id': PREFIX + key, 'sha256': digest, 'session_id': session,
               'captured': captured, 'text': prompt[:MAX_TEXT], 'truncated': len(prompt) > MAX_TEXT}
    with transaction(store.db) as db:
        old = read_record(db, KIND, key)
        if old['payload']:
            return old['payload']
        write_record(db, KIND, key, payload, expected_revision=0)
    return payload


def resolve(store, ref):
    """None for a free-text reference; the captured record for `prompt:<id>`; error if unknown."""
    if not isinstance(ref, str) or not ref.startswith(PREFIX):
        return None
    record = read_record(store.db, KIND, ref[len(PREFIX):])['payload']
    if not record:
        raise ValueError('Unknown captured prompt reference ' + ref + '; run `ase prompt list`')
    return record


def recent(store, limit=20):
    rows = store.db.execute("SELECT payload FROM runtime_records WHERE kind=? ORDER BY updated DESC LIMIT ?", (KIND, limit))
    out = []
    for (payload,) in rows:
        record = json.loads(payload)
        out.append({key: record[key] for key in ('id', 'captured', 'session_id')} | {'excerpt': record['text'][:200]})
    return out
