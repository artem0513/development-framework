"""Bounded sealed native-result artifacts; content integrity, not semantic AC."""
import os
from pathlib import Path, PureWindowsPath
import re
import stat

from .native_paths import logical_path, state_io
from .projects import _safe_state
from .protocol import canonical, decode_json, sha256
from .runner import _read
from .state import identifier, text

MAX_RESULT = 65536
MAX_ARTIFACT = 1048576


def attempt_root(store, entry):
    task, work = identifier(entry['task_id']), identifier(entry['id'])
    attempt = entry['attempt']['id']
    if not isinstance(attempt, str) or not re.fullmatch('[0-9a-f]{32}', attempt):
        raise ValueError('Invalid native delegation attempt')
    path = logical_path(store.root / 'tasks' / task / 'delegations' / work / attempt)
    _safe_state(path)
    return state_io(path)


def _envelope(store, entry, payload):
    if not isinstance(payload, dict) or set(payload) != {'attempt_id', 'agent_id', 'status', 'summary', 'artifacts'}:
        raise ValueError('Invalid native delegation result fields')
    attempt = entry['attempt']
    if payload['attempt_id'] != attempt['id'] or payload['status'] not in {'COMPLETE', 'FAILED', 'BLOCKED'}:
        raise ValueError('Native result differs from its exact attempt')
    text(payload['agent_id'], 'native agent ID', limit=300)
    text(payload['summary'], 'native result summary', limit=4000)
    native = attempt.get('native')
    if native is not None and native != {'backend': attempt['backend'], 'agent_id': payload['agent_id']}:
        raise ValueError('Native result belongs to a different worker')
    refs = payload['artifacts']
    if not isinstance(refs, list) or len(refs) > 20:
        raise ValueError('Native result artifacts must be a bounded list')
    root = attempt_root(store, entry) / 'output'
    artifacts, seen, total = [], set(), 0
    for item in refs:
        if not isinstance(item, dict) or set(item) != {'path', 'sha256'}:
            raise ValueError('Invalid native output reference')
        name = text(item['path'], 'native output path', limit=2048)
        relative = Path(name)
        if (relative.is_absolute() or PureWindowsPath(name).drive or '\\' in name
                or any(part == '..' or (part not in {'.'} and part.endswith(('.', ' '))) for part in relative.parts)):
            raise ValueError('Native output path escapes its attempt or normalizes ambiguously')
        name = relative.as_posix()
        if name in seen:
            raise ValueError('Duplicate native output reference')
        seen.add(name)
        data = _read(root / relative, MAX_ARTIFACT)
        if sha256(data) != item['sha256']:
            raise ValueError('Native output artifact differs from its captured SHA')
        total += len(data)
        if total > 8 * MAX_ARTIFACT:
            raise ValueError('Native output artifact batch exceeds byte limit')
        artifacts.append({'path': name, 'sha256': item['sha256'], 'bytes': len(data)})
    return {'version': 1, 'project_id': store.project_id, 'workspace_id': store.workspace_id,
        'task_id': entry['task_id'], 'work_id': entry['id'], 'attempt_id': attempt['id'],
        'context_sha256': attempt['context_sha256'], 'backend': attempt['backend'],
        'agent_id': payload['agent_id'], 'status': payload['status'], 'summary': payload['summary'], 'artifacts': artifacts}


def seal_result(store, entry, payload):
    envelope = _envelope(store, entry, payload)
    data = canonical(envelope)
    if len(data) > MAX_RESULT:
        raise ValueError('Native result manifest exceeds byte limit')
    root = attempt_root(store, entry)
    root.mkdir(parents=True, exist_ok=True)
    target, pending = root / 'result.json', root / 'result.json.pending'
    if target.exists() or target.is_symlink():
        if _read(target, MAX_RESULT) != data:
            raise ValueError('Conflicting native result is already sealed')
    else:
        prefix = _read(pending, MAX_RESULT) if pending.exists() or pending.is_symlink() else b''
        if not data.startswith(prefix):
            raise ValueError('Unknown native result staging content; preserved')
        flags = os.O_WRONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0)
        flags |= os.O_APPEND if pending.exists() else os.O_CREAT | os.O_EXCL
        descriptor = os.open(pending, flags, 0o600)
        with os.fdopen(descriptor, 'wb') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError('Unsafe native result staging file')
            stream.write(data[len(prefix):])
            stream.flush()
            os.fsync(stream.fileno())
        if _read(pending, MAX_RESULT) != data:
            raise ValueError('Native result staging changed before sealing')
        if target.exists() or target.is_symlink():
            raise ValueError('Native result target changed before sealing')
        os.replace(pending, target)
    return {'path': str(logical_path(target)), 'sha256': sha256(data)}


def load_result(store, entry):
    path = attempt_root(store, entry) / 'result.json'
    raw = _read(path, MAX_RESULT)
    reference = entry.get('result')
    if reference is not None and (not isinstance(reference, dict) or set(reference) != {'path', 'sha256'}
            or reference != {'path': str(logical_path(path)), 'sha256': sha256(raw)}):
        raise ValueError('Native result reference differs from its exact attempt')
    value = decode_json(raw)
    try:
        if not isinstance(value, dict) or not isinstance(value.get('artifacts'), list):
            raise ValueError('Malformed sealed native result')
        payload = {key: value[key] for key in ('attempt_id', 'agent_id', 'status', 'summary')}
        payload['artifacts'] = [{key: item[key] for key in ('path', 'sha256')} for item in value['artifacts']]
        if canonical(_envelope(store, entry, payload)) != canonical(value):
            raise ValueError('Sealed native result identity differs from its reservation')
    except (AttributeError, TypeError, KeyError) as ex:
        raise ValueError('Malformed sealed native result fields') from ex
    return value
