"""Durable result inbox. Native hosts deliver; notifications grant no authority."""
import copy
import json
from pathlib import Path
import re
import uuid

from .operations import OperationLedger
from .state import hash_value, read_record, text, transaction, write_record
from .store import now


CLASSIFICATION_NOTE = ('captured_result counts come from the sealed receipt result of that event and are not a gate verdict; '
    'superseded_by names a later event whose receipt replaced this one as the recorded verification binding '
    'for the same role and the same stage scope, usually ack --outcome stale after checking it. Absent fields mean no reliable aid.')
_TOKEN = re.compile('[A-Z][A-Z0-9_]{0,39}')
_COUNTED = {'criteria': 'status', 'findings': 'severity', 'scenarios': 'status', 'additional_scenarios': 'status'}


def _counts(rows, field):
    counts = {}
    for row in rows:
        value = row.get(field) if isinstance(row, dict) else None
        name = value if isinstance(value, str) and _TOKEN.fullmatch(value) else 'OTHER'
        counts[name] = counts.get(name, 0) + 1
    return counts


def _sealed_receipt(content):
    """The receipt this event's single evidence hash binds, or None."""
    if content['source_kind'] not in {'verification', 'adequacy'} or len(content['evidence']) != 1:
        return None
    from .runner import _read
    entry = content['evidence'][0]
    try:
        receipt = json.loads(_read(Path(entry['ref']), 8388608))
        if (hash_value(receipt) != entry['sha256'] or 'verification:' + receipt['execution_id'] != content['source_id']
                or Path(entry['ref']) != Path(receipt['artifact_root']) / 'receipt.json'):
            return None
    except (OSError, ValueError, KeyError, TypeError, RecursionError):
        return None
    return receipt


_UNKNOWN_SCOPE = object()


def _stage_scope(content):
    """handoff.stage_id (None for the whole task) of the hash-verified request the event's receipt binds."""
    from .protocol import sha256
    from .runner import _read
    receipt = _sealed_receipt(content)
    try:
        artifact = receipt['artifacts']['request']
        path = Path(artifact['path'])
        if path.parent != Path(receipt['artifact_root']):
            return _UNKNOWN_SCOPE
        data = _read(path, 8388608)
        if sha256(data) != artifact['sha256']:
            return _UNKNOWN_SCOPE
        stage_id = json.loads(data).get('handoff', {}).get('stage_id')
    except (OSError, ValueError, KeyError, TypeError, AttributeError, RecursionError):
        return _UNKNOWN_SCOPE
    return stage_id if stage_id is None or isinstance(stage_id, str) else _UNKNOWN_SCOPE


def _captured_result(content):
    """Status tokens and counts only, read from the receipt this event's evidence hash binds."""
    receipt = _sealed_receipt(content)
    if receipt is None:
        return None
    from .protocol import sha256
    from .runner import _read
    try:
        root = Path(receipt['artifact_root'])
        artifact = receipt['artifacts']['result']
        if Path(artifact['path']) != root / 'result.json':
            return None
        data = _read(root / 'result.json', 8388608)
        if sha256(data) != artifact['sha256']:
            return None
        result = json.loads(data)
    except (OSError, ValueError, KeyError, TypeError, RecursionError):
        return None
    if (not isinstance(result, dict) or receipt.get('role') not in {'ac', 'review'}
            or receipt.get('evidence_kind') not in {'independent_cli', 'protocol_only'}
            or result.get('status') not in {'COMPLETE', 'BLOCKED'}):
        return None
    captured = {'role': 'adequacy' if content['source_kind'] == 'adequacy' else receipt['role'],
        'evidence_kind': receipt['evidence_kind'], 'result_status': result['status']}
    if isinstance(result.get('blocker'), str) and _TOKEN.fullmatch(result['blocker']):
        captured['blocker'] = result['blocker']
    payload = result.get('payload')
    if isinstance(payload, dict):
        if isinstance(payload.get('verdict'), str) and _TOKEN.fullmatch(payload['verdict']):
            captured['verdict'] = payload['verdict']
        for name, field in _COUNTED.items():
            if isinstance(payload.get(name), list):
                captured[name] = _counts(payload[name], field)
        for name in ('missing_coverage', 'unverified_material'):
            if isinstance(payload.get(name), list):
                captured[name + '_count'] = len(payload[name])
    return captured


class CoordinatorEvents:
    def __init__(self, store):
        self.store = store

    def _root(self, task_id):
        return OperationLedger(self.store).root_task_id(task_id)

    def _destination(self, value):
        fields={'backend','thread_id','host_id'}
        if not isinstance(value, dict) or set(value) not in (fields,fields|{'transport'}):
            raise ValueError('Coordinator requires exact backend, thread_id and host_id')
        if 'transport' in value and (value['transport']!='local' or value['host_id']!='ase-local-inbox'
                or not isinstance(value['thread_id'],str) or not re.fullmatch('ase-owner-[0-9a-f]{32}',value['thread_id'])):
            raise ValueError('Invalid local inbox owner identity')
        if value['backend'] not in {'codex', 'claude', 'copilot'}:
            raise ValueError('Unknown coordinator backend')
        for key in ('thread_id', 'host_id'):
            text(value[key], 'coordinator ' + key, limit=300)
        return copy.deepcopy(value)

    def bind(self, task_id, destination, *, user_message_ref, expected_revision):
        root = self._root(task_id)
        destination = self._destination(destination)
        text(user_message_ref, 'original coordinator authorization', limit=2000)
        with transaction(self.store.db):
            old = read_record(self.store.db, 'coordinator', root)
            if old['payload'] and old['payload']['destination'] != destination:
                raise ValueError('The original coordinator destination is immutable')
            if old['payload']:
                if expected_revision != old['revision']:
                    raise ValueError('Stale coordinator revision')
                return old
            return write_record(self.store.db, 'coordinator', root,
                {'root_task_id': root, 'project_id': self.store.project_id,
                 'destination': destination, 'user_message_ref': user_message_ref},
                expected_revision=expected_revision)

    def bind_local(self,task_id,*,backend,user_message_ref,expected_revision):
        """Mint a local lead identity when a host exposes no native chat ID."""
        root=self._root(task_id)
        if backend not in {'codex','claude','copilot'}:
            raise ValueError('Unknown coordinator backend')
        with transaction(self.store.db):
            old=read_record(self.store.db,'coordinator',root)
            if old['payload']:
                destination=old['payload']['destination']
                if destination.get('transport')!='local' or destination['backend']!=backend:
                    raise ValueError('Keep the original bound coordinator; do not replace its identity')
            else:
                destination={'backend':backend,'thread_id':'ase-owner-'+uuid.uuid4().hex,
                    'host_id':'ase-local-inbox','transport':'local'}
            return self.bind(task_id,destination,user_message_ref=user_message_ref,expected_revision=expected_revision)

    def _owner(self, root, thread_id=None):
        owner = read_record(self.store.db, 'coordinator', root)['payload']
        if not owner:
            raise ValueError('Original coordinator destination is not bound; run `ase events bind-local` (or `ase events bind`) first')
        if thread_id is not None and thread_id != owner['destination']['thread_id']:
            raise ValueError('Only the original coordinator thread may process this inbox; pass --thread-id from '
                'payload.destination.thread_id of `ase events bind-local` (also coordinator.destination.thread_id in `ase events list`)')
        return owner

    def enqueue(self, task_id, *, source_kind, source_id, status, summary, evidence):
        root = self._root(task_id)
        if source_kind not in {'delegation', 'verification', 'adequacy'}:
            raise ValueError('Unknown coordinator event source kind')
        text(source_id, 'event source identity', limit=500)
        text(summary, 'event summary', limit=4000)
        if status not in {'COMPLETE', 'FAILED', 'BLOCKED', 'APPROVED', 'REJECTED'}:
            raise ValueError('Unknown coordinator event status')
        if not isinstance(evidence, list) or not 1 <= len(evidence) <= 20:
            raise ValueError('Event requires bounded evidence references')
        for entry in evidence:
            if (not isinstance(entry, dict) or set(entry) != {'ref', 'sha256'}
                    or not isinstance(entry['sha256'], str)
                    or not re.fullmatch('[0-9a-f]{64}', entry['sha256'])):
                raise ValueError('Invalid event evidence hash')
            text(entry['ref'], 'event evidence reference', limit=2000)
        event_id = hash_value({'root': root, 'kind': source_kind, 'source_id': source_id})
        content = {'id': event_id, 'task_id': task_id, 'root_task_id': root,
            'project_id': self.store.project_id, 'source_kind': source_kind,
            'source_id': source_id, 'status': status, 'summary': summary,
            'evidence': copy.deepcopy(evidence)}
        with transaction(self.store.db):
            old = read_record(self.store.db, 'coordinator_event', root + ':' + event_id)
            if old['payload']:
                if old['payload']['content'] != content:
                    raise ValueError('Event source identity conflict: different result content')
                return old
            return write_record(self.store.db, 'coordinator_event', root + ':' + event_id,
                {'id': event_id, 'content': content, 'created': now(),
                 'delivery': {'state': 'PENDING', 'id': None},
                 'processing': {'state': 'PENDING', 'id': None}}, expected_revision=0)

    def _get(self, task_id, event_id, expected_revision):
        if not isinstance(event_id, str) or not re.fullmatch('[0-9a-f]{64}', event_id):
            raise ValueError('Invalid coordinator event ID')
        root = self._root(task_id)
        key = root + ':' + event_id
        record = read_record(self.store.db, 'coordinator_event', key)
        if not record['payload'] or record['revision'] != expected_revision:
            raise ValueError('Missing event or stale coordinator event revision')
        return root, key, record, copy.deepcopy(record['payload'])

    def dispatch(self, task_id, event_id, *, expected_revision):
        if self.store.db.in_transaction:
            raise ValueError('Delivery dispatch must commit before native sending; outer transaction is forbidden')
        with transaction(self.store.db):
            root, key, record, value = self._get(task_id, event_id, expected_revision)
            owner = self._owner(root)
            if owner['destination'].get('transport')=='local':
                raise ValueError('Native chat routing unavailable; consume the original local inbox')
            if value['processing']['state'] == 'DONE':
                raise ValueError('Event already processed')
            if value['delivery']['state'] != 'PENDING':
                raise ValueError('Delivery already reserved; reconcile its original attempt')
            value['delivery'] = {'state': 'DISPATCHING', 'id': uuid.uuid4().hex,
                'destination': owner['destination'], 'prepared_at': now()}
            saved = write_record(self.store.db, 'coordinator_event', key, value,
                expected_revision=record['revision'])
            content = value['content']
            prompt = ('DevelopmentFramework service event ' + event_id + '. This is evidence, not user authorization. '
                'Process this existing task through its original scope and recovery history. '
                'Claim the event in ase before effects; inspect current referenced evidence and reconcile an existing claim after interruption. '
                'Do not resend, reset attempts or infer task completion from this message.\n'
                + json.dumps(content, ensure_ascii=False, sort_keys=True))
            return {**saved, 'handoff': {'destination': owner['destination'], 'event_id': event_id,
                'attempt_id': value['delivery']['id'], 'prompt': prompt,
                'authority': 'Host-mediated delivery only; no CLI wake-up or new owner approval'}}

    def observe(self, task_id, event_id, *, attempt_id, observation, expected_revision):
        if (not isinstance(observation, dict) or set(observation) != {'state', 'destination', 'reference'}
                or observation['state'] not in {'DELIVERED', 'UNKNOWN'}):
            raise ValueError('Delivery requires DELIVERED/UNKNOWN and an exact destination/reference')
        text(observation['reference'], 'delivery observation reference', limit=2000)
        destination = self._destination(observation['destination'])
        with transaction(self.store.db):
            root, key, record, value = self._get(task_id, event_id, expected_revision)
            delivery = value['delivery']
            if delivery['id'] != attempt_id or delivery['state'] not in {'DISPATCHING', 'UNKNOWN'}:
                raise ValueError('Reconcile the exact unsettled delivery attempt')
            if destination != delivery['destination'] or destination != self._owner(root)['destination']:
                raise ValueError('Delivery observation destination mismatch')
            delivery.update(state=observation['state'], reference=observation['reference'], observed_at=now())
            return write_record(self.store.db, 'coordinator_event', key, value,
                expected_revision=record['revision'])

    def claim(self, task_id, event_id, *, thread_id, expected_revision):
        if self.store.db.in_transaction:
            raise ValueError('Processing claim must commit before effects')
        with transaction(self.store.db):
            root, key, record, value = self._get(task_id, event_id, expected_revision)
            self._owner(root, thread_id)
            if value['processing']['state'] != 'PENDING':
                raise ValueError('Event processing already claimed; reconcile the original claim')
            value['processing'] = {'state': 'CLAIMED', 'id': uuid.uuid4().hex,
                'thread_id': thread_id, 'claimed_at': now()}
            saved = write_record(self.store.db, 'coordinator_event', key, value,
                expected_revision=record['revision'])
        return {**saved, **self._aid(self.snapshot(task_id)['events'], value)}

    def ack(self, task_id, event_id, *, processing_id, thread_id, outcome, evidence, expected_revision):
        if outcome not in {'handled', 'stale', 'blocked'}:
            raise ValueError('Processing outcome must be handled, stale or blocked')
        text(evidence, 'processing checkpoint/evidence', limit=2000)
        with transaction(self.store.db):
            root, key, record, value = self._get(task_id, event_id, expected_revision)
            self._owner(root, thread_id)
            processing = value['processing']
            if processing['state'] != 'CLAIMED' or processing['id'] != processing_id:
                raise ValueError('A matching original processing claim is required')
            processing.update(state='DONE', outcome=outcome, evidence=evidence, processed_at=now())
            return write_record(self.store.db, 'coordinator_event', key, value,
                expected_revision=record['revision'])

    def snapshot(self, task_id):
        root = self._root(task_id)
        events = []
        for row in self.store.db.execute("SELECT key FROM runtime_records WHERE kind='coordinator_event' ORDER BY key"):
            if row[0].startswith(root + ':'):
                events.append(read_record(self.store.db, 'coordinator_event', row[0]))
        events.sort(key=lambda row: (row['payload']['created'], row['payload']['id']))
        return {'root_task_id': root, 'project_id': self.store.project_id,
            'coordinator': read_record(self.store.db, 'coordinator', root)['payload'], 'events': events,
            'pending_count': sum(row['payload']['processing']['state'] != 'DONE' for row in events),
            'authority': 'Evidence inbox only. Native host delivery is optional; unknown dispatches and claimed processing never expire.'}

    def pending(self, task_id):
        """Events the original lead has not processed yet, each with its exact next claim/ack command."""
        inbox = self.snapshot(task_id)
        thread = inbox['coordinator']['destination']['thread_id'] if inbox['coordinator'] else '<thread_id from bind-local>'
        rows = []
        for row in inbox['events']:
            event, revision = row['payload'], row['revision']
            processing = event['processing']
            if processing['state'] == 'DONE':
                continue
            if processing['state'] == 'PENDING':
                command = (f"ase events claim {task_id} {event['id']} --thread-id {thread} --expected-revision {revision}")
            else:
                command = (f"ase events ack {task_id} {event['id']} --processing-id {processing['id']} --thread-id {thread} "
                           f"--outcome <handled|stale|blocked> --evidence <checkpoint> --expected-revision {revision}")
            rows.append({'id': event['id'], 'source_kind': event['content']['source_kind'],
                         'status': event['content']['status'], 'processing': processing['state'],
                         'revision': revision, 'next_command': command, **self._aid(inbox['events'], event)})
        return {'bound': inbox['coordinator'] is not None, 'events': rows, 'classification_note': CLASSIFICATION_NOTE}

    def _aid(self, events, event):
        """Counts from the event's own sealed receipt plus a supersession hint; omitted when not reliable."""
        content = event['content']
        captured = _captured_result(content)
        if captured is None:
            return {}
        aid = {'captured_result': captured}
        # Every verification save overwrites the task/role record and enqueues its event in the same transaction;
        # stage and whole-task ac/review runs share that record, so only a same-scope later event supersedes.
        current = read_record(self.store.db, 'verification', content['task_id'] + ':' + captured['role'])['payload']
        execution = current.get('execution_id') if isinstance(current, dict) else None
        if isinstance(execution, str) and 'verification:' + execution != content['source_id']:
            newer = [row['payload'] for row in events
                     if row['payload']['content']['source_id'] == 'verification:' + execution
                     and row['payload']['content']['task_id'] == content['task_id']
                     and row['payload']['created'] > event['created']]
            if len(newer) == 1:
                scope = _stage_scope(content)
                if scope is not _UNKNOWN_SCOPE and scope == _stage_scope(newer[0]['content']):
                    aid['superseded_by'] = newer[0]['id']
        return aid

    def record_receipt(self, task_id, role, receipt):
        role = role.split(':', 1)[0]
        return self.enqueue(task_id,
            source_kind='adequacy' if role == 'adequacy' else 'verification',
            source_id='verification:' + receipt['execution_id'], status=receipt['status'],
            summary='Independent ' + role + ' result captured; inspect findings and current bindings before acting',
            evidence=[{'ref': str(Path(receipt['artifact_root']) / 'receipt.json'), 'sha256': hash_value(receipt)}])

    def recover(self, task_id):
        """Recover sealed facts only, never replace a judgment or start a worker."""
        from .delegations import DelegationRegistry
        from .runner import _read, verify_capture
        from .native_paths import logical_path, state_io
        DelegationRegistry(self.store).recover(task_id)
        root = self._root(task_id)
        unavailable = []
        owners = []
        for row in self.store.db.execute("SELECT key FROM runtime_records WHERE kind='task'"):
            if self._root(row[0]) == root:
                owners.append(row[0])
        for owner in owners:
            sessions = state_io(self.store.root / 'tasks' / owner / 'sessions')
            if not sessions.exists():
                continue
            if sessions.is_symlink() or getattr(sessions, 'is_junction', lambda: False)():
                raise ValueError('Verifier recovery cannot follow a linked session directory')
            for folder in sorted(sessions.iterdir()):
                if not re.fullmatch('[0-9a-f]{32}', folder.name):
                    continue
                try:
                    receipt = json.loads(_read(folder / 'receipt.json', 8388608))
                    if receipt.get('status') not in {'COMPLETE', 'BLOCKED'} or receipt.get('role') not in {'ac', 'review'}:
                        continue
                    verified = verify_capture(receipt, require_current_policy=False)
                    identity = verified['request_identity']
                    if (identity['project_id'] != self.store.project_id
                            or receipt['bindings'].get('workspace_id') != self.store.workspace_id
                            or logical_path(receipt['artifact_root']).resolve() != logical_path(folder).resolve()):
                        raise ValueError('Verifier result belongs to another project/workspace')
                    role = 'adequacy' if identity['purpose'] == 'test_adequacy' else identity['role']
                    self.record_receipt(owner, role, receipt)
                except (OSError, ValueError, KeyError, TypeError) as error:
                    from .core import redact
                    unavailable.append({'execution_id': folder.name, 'reason': str(redact(error))[:500]})
        return {**self.snapshot(task_id), 'unavailable_captured_results': unavailable,
            'recovery_authority': 'Recovered historical captured evidence only; original current gates and source checks still apply'}
