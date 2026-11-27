"""Durable admission of one exact local candidate, not a new execution grant."""
from contextlib import contextmanager
import json
import hashlib
import re

from .edit_application import plan_edits, inspect_plan
from .gates import source_snapshot
from .native_paths import logical_path, state_io
from .runner import verify_receipt
from .state import append_event, hash_value, read_record, transaction, write_record
from .protocol import decode_json, sha256
from .store import now


KIND = 'local_edit_application'


def _identity(controller, task, stage, progress, authority):
    return {'task_id': task['task_id'], 'stage_id': stage['id'],
        'candidate_id': progress.get('candidate_id'), 'project_id': controller.store.project_id,
        'workspace_id': controller.store.workspace_id, 'contract_hash': task['contract_hash'],
        'proposal_hash': task['proposal_hash'], 'plan_hash': controller.tasks.execution_plan(task['task_id'])['plan_hash'],
        'authority': authority}


def _capture(controller, task, stage, progress, receipt, schema):
    root = logical_path(receipt['artifact_root'])
    expected_root = logical_path(controller.store.root / 'tasks' / task['task_id'] / 'sessions')
    if not root.is_relative_to(expected_root):
        raise ValueError('Local edit receipt is outside this task sessions')
    expected = {'contract_sha256': task['contract_hash'], 'proposal_sha256': task['proposal_hash'],
        'plan_sha256': controller.tasks.execution_plan(task['task_id'])['plan_hash'],
        'workspace_id': controller.store.workspace_id}
    verified = verify_receipt(receipt)
    if any(receipt['bindings'].get(key) != value for key, value in expected.items()):
        raise ValueError('Local edit receipt task/proposal/plan/workspace identity is stale')
    captured = receipt['artifacts']['request']
    raw = state_io(captured['path']).read_bytes()
    if hashlib.sha256(raw).hexdigest() != captured['sha256']:
        raise ValueError('Local edit request changed after verification')
    request = json.loads(raw)
    handoff = request.get('handoff', {})
    if (request.get('role') != 'implementation' or receipt.get('role') != 'implementation'
            or request.get('schema') != schema or handoff.get('stage') != stage
            or handoff.get('candidate_id') != progress.get('candidate_id')
            or request['project'].get('project_id') != controller.store.project_id
            or logical_path(request['project']['root']) != logical_path(controller.store.repo)
            or verified['result'].get('status') != 'COMPLETE'):
        raise ValueError('Local edit capture differs from admitted candidate/stage/project')
    return verified['result']['payload']['edits']


def _guard(controller, task, plan):
    owned_temporaries = {item['temporary_path'] for item in plan['edits'] if item['temporary_path']}
    source = source_snapshot(controller.store, task['contract'], task_id=task['task_id'], _excluded_files=owned_temporaries)
    own = {item['path'] for item in plan['edits']}
    own.update(item['temporary_path'] for item in plan['edits'] if item['temporary_path'])
    return {'unrelated_files_sha256': hash_value({key: value for key, value in source['files'].items() if key not in own}),
        'unavailable_sha256': hash_value(source.get('unavailable', {})),
        'runtime_policy_sha256': source.get('runtime_policy_sha256'),
        'discovery_policy_sha256': source.get('discovery_policy_sha256')}


def _receipt_reference(controller, task, receipt):
    path = logical_path(receipt['artifact_root']) / 'receipt.json'
    root = logical_path(controller.store.root / 'tasks' / task['task_id'] / 'sessions')
    if not path.is_relative_to(root):
        raise ValueError('Local edit receipt reference escapes this task')
    raw = state_io(path).read_bytes()
    if hash_value(decode_json(raw)) != hash_value(receipt):
        raise ValueError('Local edit receipt capture differs from its durable artifact')
    return {'path': str(path), 'sha256': sha256(raw)}


def _read_receipt(controller, task, reference):
    if not isinstance(reference, dict) or set(reference) != {'path', 'sha256'}:
        raise ValueError('Local edit receipt reference is invalid')
    path = logical_path(reference['path'])
    root = logical_path(controller.store.root / 'tasks' / task['task_id'] / 'sessions')
    if not path.is_relative_to(root) or path.name != 'receipt.json':
        raise ValueError('Local edit receipt reference escapes this task')
    raw = state_io(path).read_bytes()
    if sha256(raw) != reference['sha256']:
        raise ValueError('Local edit receipt artifact changed')
    return decode_json(raw)


def _authority(controller, task, stage, authority, prerequisite_binding):
    if controller._stage_authority(task, stage) != authority:
        raise ValueError('Candidate authority changed before local file mutation')
    controller._available(task['task_id'], 1)
    controller._require_prerequisite_binding(task['task_id'], stage['id'], prerequisite_binding)


def prepare_local_edits(controller, task, stage, progress, receipt, authority, prerequisite_binding, resolve_target, schema):
    """Commit intent and APPLYING pointer before any filesystem side effect."""
    store = controller.store
    key = progress['candidate_id']
    with transaction(store.db):
        _authority(controller, task, stage, authority, prerequisite_binding)
        if progress.get('candidate_authority') != authority:
            raise ValueError('Candidate original authority is unavailable')
        edits = _capture(controller, task, stage, progress, receipt, schema)
        source = source_snapshot(store, task['contract'], task_id=task['task_id'])
        if source['fingerprint'] != receipt['bindings']['fingerprint']:
            raise ValueError('Candidate inputs changed before local edit admission')
        plan = plan_edits(store.repo, stage['scope'], edits, key, resolve_target)
        from .evidence.guarded_additions import candidate_blockers
        blockers, targets = candidate_blockers(controller.tasks, task, stage['id'], plan, edits)
        if blockers:
            from .tasks import proposal_sequence
            sequence = proposal_sequence(store.db, task['task_id'])
            detail = '; '.join(blockers[:3])
            resume = '`ase task resume ' + task['task_id'] + '`'
            action = (f'{detail}. The candidate was held before any write. If these additions are needed, explain why '
                      f'and get the named additions approved in the stage proposal. Otherwise use {resume} for a '
                      'candidate within the current approval.' if targets else
                      f'{detail}. Restore comparable source or correct the candidate, then {resume}.')
            verdict = {'status': 'BLOCKED', 'blocked_reasons': blockers, 'next_action': action,
                       'awaiting_approval': bool(targets), 'needs_owner_input': bool(targets)}
            if targets:
                verdict['addition_approval'] = {'targets': targets, 'proposal_sequence': sequence}
            progress = {**progress, 'phase': 'CANDIDATE_BLOCKED', 'candidate_blocker': verdict,
                        'candidate_proposal_sequence': sequence}
            controller._save('cycle', task['task_id'] + ':' + stage['id'], progress)
            return progress
        value = {'version': 1, 'identity': _identity(controller, task, stage, progress, authority),
            'receipt_ref': _receipt_reference(controller, task, receipt), 'receipt_sha256': hash_value(receipt), 'file_plan': plan,
            'source_guard': _guard(controller, task, plan), 'prepared_at': now()}
        value['intent_sha256'] = hash_value(value)
        value['state'] = 'PREPARED'
        old = read_record(store.db, KIND, key)
        if old['payload'] is not None:
            raise ValueError('Local edit intent already exists; reconcile the existing reservation')
        write_record(store.db, KIND, key, value, expected_revision=old['revision'])
        progress = {**progress, 'phase': 'APPLYING', 'local_edit_application_id': key}
        controller._save('cycle', task['task_id'] + ':' + stage['id'], progress)
        append_event(store.db, key + ':local-edit-prepared', 'local_edit_prepared', task['task_id'],
            {'candidate_id': key, 'intent_sha256': value['intent_sha256']})
    return progress


def _validated_intent(controller, task, stage, progress, resolve_target, schema, *, require_applied=False):
    """Authenticate persisted identity, receipt, exact plan and staging state."""
    store = controller.store
    key = progress.get('local_edit_application_id')
    if not isinstance(key, str) or not re.fullmatch('[0-9a-f]{32}', key):
        raise ValueError('Local edit application ID is invalid')
    record = read_record(store.db, KIND, key)
    value = record['payload']
    if (not isinstance(value, dict) or value.get('state') not in {'PREPARED', 'APPLIED'}
            or type(value.get('version')) is not int or value['version'] != 1):
        raise ValueError('Local edit intent is missing or invalid')
    if require_applied and value['state'] != 'APPLIED':
        raise ValueError('Only a completed local intent can resume pending stage actions')
    immutable = {name: item for name, item in value.items() if name not in {'state', 'applied_at', 'intent_sha256'}}
    if (hash_value(immutable) != value.get('intent_sha256')
            or key != progress.get('candidate_id') or value['file_plan']['application_id'] != key
            or value['identity'] != _identity(controller, task, stage, progress, progress.get('candidate_authority'))):
        raise ValueError('Local edit intent identity or captured proof differs')
    receipt = _read_receipt(controller, task, value['receipt_ref'])
    if hash_value(receipt) != value['receipt_sha256']:
        raise ValueError('Local edit receipt identity differs')
    edits = _capture(controller, task, stage, progress, receipt, schema)
    inspect_plan(store.repo, stage['scope'], edits, value['file_plan'], resolve_target)
    return record, value, edits


@contextmanager
def recovery_inventory(controller, task, stage, progress, resolve_target, schema):
    """Authenticate an APPLYING intent before excluding its owned crash staging.

    All participating task observations use the same inventory. Each sample
    revalidates the intent, staging and current authority; final mutation also
    retains its normal prerequisite binding and unrelated-source guards.
    """
    def authenticate():
        with transaction(controller.store.db):
            saved = read_record(controller.store.db, 'cycle', task['task_id'] + ':' + stage['id'])['payload']
            if saved != progress:
                raise ValueError('Saved local edit progress changed before recovery inventory')
            _, value, _ = _validated_intent(controller, task, stage, progress, resolve_target, schema)
            if controller._stage_authority(task, stage) != value['identity']['authority']:
                raise ValueError('Candidate authority changed before local file mutation')
            controller._available(task['task_id'], 1)
            return {item['temporary_path'] for item in value['file_plan']['edits'] if item['temporary_path']}

    if progress.get('phase') != 'APPLYING':
        raise ValueError('Recovery inventory requires a saved APPLYING intent')
    authenticate()
    # The existing guard excludes only this now-authenticated plan, and still
    # detects unrelated source/policy changes before any prerequisite refresh.
    with transaction(controller.store.db):
        _, value, _ = _validated_intent(controller, task, stage, progress, resolve_target, schema)
        if _guard(controller, task, value['file_plan']) != value['source_guard']:
            raise ValueError('Unrelated source or runtime changed during local edit recovery')
    owners = {task['task_id'], *(owner['task_id'] for owner, _ in controller._prerequisite_stages(task, stage))}
    from .gates import _source_inventory
    with _source_inventory(controller.store, owners, authenticate):
        yield


def finish_local_edits(controller, task, stage, progress, prerequisite_binding, resolve_target, apply_function, schema, *, require_applied=False):
    """Complete that committed intent; never reserve or regenerate a candidate."""
    store = controller.store
    key = progress.get('local_edit_application_id')
    with transaction(store.db):
        record, value, edits = _validated_intent(controller, task, stage, progress, resolve_target,
            schema, require_applied=require_applied)
        _authority(controller, task, stage, value['identity']['authority'], prerequisite_binding)
        if _guard(controller, task, value['file_plan']) != value['source_guard']:
            raise ValueError('Unrelated source or runtime changed during local edit recovery')
        if value['state'] != 'APPLIED':
            apply_function(store.repo, stage['scope'], edits, application=value['file_plan'])
        from .edit_application import _file_state
        for entry in value['file_plan']['edits']:
            path = resolve_target(store.repo.resolve(), entry['path'])
            if _file_state(path) != entry['after_sha256']:
                raise ValueError('Local edit postimage differs; replay or overwrite is not authorized')
        if value['state'] != 'APPLIED':
            value = {**value, 'state': 'APPLIED', 'applied_at': now()}
            write_record(store.db, KIND, key, value, expected_revision=record['revision'])
            append_event(store.db, key + ':local-edit-applied', 'local_edit_applied', task['task_id'],
                {'candidate_id': key, 'intent_sha256': value['intent_sha256']})
        progress = {**progress, 'phase': 'CHECKING',
            'fingerprint': source_snapshot(store, task['contract'], task_id=task['task_id'])['fingerprint']}
        progress.pop('read_only_recovery', None)
        controller._save('cycle', task['task_id'] + ':' + stage['id'], progress)
    return progress
