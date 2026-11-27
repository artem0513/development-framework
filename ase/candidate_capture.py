"""Exact host-reserved implementation capture; never select a latest response."""
import re
from datetime import datetime

from .native_paths import logical_path, state_io
from .protocol import canonical, decode_json, sha256
from .runner import _read, verify_receipt
from .runtime_policy import bind_policy


FIELDS = {'version', 'task_id', 'stage_id', 'candidate_id', 'execution_id', 'request_sha256'}


def reference(task_id, stage_id, candidate_id, execution_id, request):
    frozen = bind_policy(request)
    value = {'version': 1, 'task_id': task_id, 'stage_id': stage_id,
        'candidate_id': candidate_id, 'execution_id': execution_id,
        'request_sha256': sha256(canonical(frozen))}
    _validate(value)
    return value, frozen


def _validate(value):
    if (not isinstance(value, dict) or set(value) != FIELDS
            or type(value.get('version')) is not int or value['version'] != 1
            or any(not isinstance(value.get(key), str) or not re.fullmatch('[0-9a-f]{32}', value[key])
                for key in ('candidate_id', 'execution_id'))
            or not isinstance(value.get('request_sha256'), str)
            or not re.fullmatch('[0-9a-f]{64}', value['request_sha256'])):
        raise ValueError('Candidate capture reference is invalid')
    from .state import identifier
    identifier(value['task_id'], 'capture task')
    identifier(value['stage_id'], 'capture stage')


def completed(controller, value, *, require_current_policy=True):
    """Validate the one reserved capture, independently of mutable latest rows."""
    _validate(value)
    store = controller.store
    root = logical_path(store.root / 'tasks' / value['task_id'] / 'sessions' / value['execution_id'])
    receipt = decode_json(_read(state_io(root / 'receipt.json'), 8388608))
    if (not isinstance(receipt, dict) or receipt.get('execution_id') != value['execution_id']
            or logical_path(receipt.get('artifact_root', '')) != root
            or receipt.get('request_sha256') != value['request_sha256']
            or receipt.get('role') != 'implementation'):
        raise ValueError('Candidate capture receipt differs from the reserved execution')
    try:
        verified = verify_receipt(receipt, require_current_policy=require_current_policy)
    except (AttributeError, KeyError, TypeError) as ex:
        raise ValueError('Candidate capture artifact metadata is malformed') from ex
    raw = _read(state_io(receipt['artifacts']['request']['path']), 8388608)
    request = decode_json(raw)
    handoff, project = request.get('handoff', {}), request.get('project', {})
    if (sha256(raw) != value['request_sha256'] or request.get('role') != 'implementation'
            or handoff.get('candidate_id') != value['candidate_id']
            or handoff.get('stage', {}).get('id') != value['stage_id']
            or project.get('project_id') != store.project_id
            or logical_path(project.get('root', '')) != logical_path(store.repo)
            or request.get('bindings', {}).get('workspace_id') != store.workspace_id):
        raise ValueError('Candidate capture request identity differs from its reservation')
    return {'receipt': receipt, 'result': verified['result']}


def completed_interval(controller, active, root_id):
    """Authenticate an interrupted call's completed interval, never its downtime."""
    value = active['implementation_capture']
    if value['task_id'] != active.get('task_id') or controller._root_task(value['task_id']) != root_id:
        raise ValueError('Interrupted capture belongs to a different active task')
    receipt = completed(controller, value, require_current_policy=False)['receipt']
    started = datetime.fromisoformat(active['started'])
    launched = datetime.fromisoformat(receipt['started_at_utc'])
    ended = datetime.fromisoformat(receipt['ended_at_utc'])
    if any(value.tzinfo is None for value in (started, launched, ended)) or not started <= launched <= ended:
        raise ValueError('Completed capture timing differs from the active call')
    return (ended - started).total_seconds()
