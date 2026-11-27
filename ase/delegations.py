"""Durable native work registry. Bots spawn workers; this service never does."""
import copy
import json
from pathlib import Path, PureWindowsPath
import uuid

from .delegation_artifacts import attempt_root, load_result, seal_result
from .gates import source_snapshot, verification_context_hash
from .native_paths import logical_path
from .operations import OperationLedger
from .state import append_event, encode, hash_value, identifier, read_record, text, transaction, write_record
from .store import now
from .tasks import TaskService

STATES = {'PENDING', 'DISPATCHING', 'RUNNING', 'RESULT_READY', 'ACCEPTED', 'UNKNOWN', 'CANCELLED'}


class DelegationRegistry:
    def __init__(self, store):
        self.store = store
        self.tasks = TaskService(store)

    def root_task_id(self, task_id):
        return OperationLedger(self.store).root_task_id(task_id)

    def _key(self, task_id, work_id):
        return self.root_task_id(task_id) + ':' + identifier(work_id, 'delegated work ID')

    def get(self, task_id, work_id):
        record = read_record(self.store.db, 'delegation', self._key(task_id, work_id))
        value = record['payload']
        if (not isinstance(value, dict) or value.get('version') != 1 or value.get('state') not in STATES
                or value.get('task_id') != task_id or value.get('id') != work_id
                or value.get('project_id') != self.store.project_id or value.get('workspace_id') != self.store.workspace_id):
            raise ValueError('Unknown or foreign delegated assignment')
        return record

    def _entries(self, task_id):
        root = self.root_task_id(task_id)
        result = []
        for row in self.store.db.execute("SELECT key FROM runtime_records WHERE kind='delegation' ORDER BY key"):
            if row['key'].startswith(root + ':'):
                record = read_record(self.store.db, 'delegation', row['key'])
                value = record['payload']
                if not isinstance(value, dict) or value.get('root_task_id') != root:
                    raise ValueError('Malformed delegated registry record')
                result.append(self.get(value['task_id'], value['id']))
        return result

    def _save(self, record, value):
        key = self._key(value['task_id'], value['id'])
        result = write_record(self.store.db, 'delegation', key, value, expected_revision=record['revision'])
        append_event(self.store.db, 'delegation:' + key + ':' + str(result['revision']), 'delegation_' + value['state'].lower(), value['task_id'],
            {'work_id': value['id'], 'state': value['state'], 'attempt_id': (value.get('attempt') or {}).get('id'),
             'result_sha256': (value.get('result') or {}).get('sha256')})
        self._notify(value)
        return result

    def _notify(self, value):
        if value.get('result'):
            from .coordinator import CoordinatorEvents
            CoordinatorEvents(self.store).enqueue(value['task_id'], source_kind='delegation',
                source_id='delegation:' + value['attempt']['id'],
                status=value['result_summary']['status'], summary=value['result_summary']['summary'],
                evidence=[{'ref': value['result']['path'], 'sha256': value['result']['sha256']}])

    @staticmethod
    def _revision(record, expected_revision):
        if type(expected_revision) is not int or record['revision'] != expected_revision:
            raise ValueError('Stale delegation revision; reload and reconcile')

    def _controller(self):
        from .workflow import WorkflowController
        return WorkflowController(self.store)

    def _binding(self, task_id, stage_id, *, allow_completed=False):
        task = self.tasks.require_approval(task_id, stage_id, allow_terminal=allow_completed)
        stage = next(stage for stage in task['contract']['stages'] if stage['id'] == stage_id)
        controller = self._controller()
        if not allow_completed:
            controller._available(task_id, 1)
        if not self.tasks.readiness(task_id, stage_id=stage_id)['discovery_present']:
            raise ValueError('Scoped discovery is required before native delegation')
        prerequisites = []
        for owner, dependency in controller._prerequisite_stages(task, stage):
            proof_record = read_record(self.store.db, 'stage', owner['task_id'] + ':' + dependency)
            proof = proof_record['payload'] or {}
            if (proof.get('state') != 'COMPLETED' or proof.get('contract_hash') != owner['contract_hash']
                    or proof.get('proposal_hash') != owner['proposal_hash']):
                raise ValueError('Managed stage prerequisite is not complete: ' + owner['task_id'] + ':' + dependency)
            from .gates import GateService
            verdict = GateService(self.store).gate(owner['task_id'], stage_id=dependency)
            if verdict['status'] != 'APPROVED':
                raise ValueError('Managed stage prerequisite needs current proof: ' + owner['task_id'] + ':' + dependency)
            prerequisites.append({'task_id': owner['task_id'], 'stage_id': dependency,
                'stage_revision': proof_record['revision'], 'fingerprint': verdict['fingerprint'],
                'verification_context_sha256': verdict['verification_context_sha256'],
                'contract_hash': owner['contract_hash'], 'proposal_hash': owner['proposal_hash'],
                'plan_hash': self.tasks.execution_plan(owner['task_id'])['plan_hash'],
                'authority': controller._authority_binding(owner['task_id'], dependency)})
        source = source_snapshot(self.store, task['contract'], task_id=task_id)
        authority = controller._authority_binding(task_id, stage_id) if allow_completed else controller._stage_authority(task, stage)
        binding = {'authority': authority, 'source_fingerprint': source['fingerprint'],
            'verification_context_sha256': verification_context_hash(self.store, task['contract'], task_id, stage_id),
            'contract_hash': task['contract_hash'], 'proposal_hash': task['proposal_hash'],
            'plan_hash': self.tasks.execution_plan(task_id)['plan_hash']}
        if prerequisites:
            binding['prerequisites'] = prerequisites
        return binding

    def _spec(self, task_id, spec):
        fields = {'id', 'stage_id', 'goal', 'expected_result', 'mode', 'resources', 'depends_on'}
        if not isinstance(spec, dict) or set(spec) - fields:
            raise ValueError('Unknown delegated assignment fields')
        value = copy.deepcopy(spec)
        for field in ('id', 'stage_id'):
            identifier(value.get(field), 'delegation ' + field)
        text(value.get('goal'), 'delegation goal', limit=4000)
        text(value.get('expected_result'), 'delegation expected result', limit=4000)
        value.setdefault('mode', 'read_only')
        if value['mode'] not in {'read_only', 'proposal'}:
            raise ValueError('Native delegation returns analysis or proposals; it grants no target mutation')
        value.setdefault('depends_on', [])
        if (not isinstance(value['depends_on'], list) or len(value['depends_on']) > 100
                or len(set(value['depends_on'])) != len(value['depends_on'])):
            raise ValueError('Invalid delegation dependencies')
        for dependency in value['depends_on']:
            identifier(dependency, 'delegation dependency')
            if dependency == value['id']:
                raise ValueError('Delegation dependency cycle')
        resources = value.get('resources')
        if not isinstance(resources, list) or not resources or len(resources) > 100:
            raise ValueError('Delegation requires a bounded resource footprint')
        task = self.tasks.require_approval(task_id, value['stage_id'])
        stage = next(stage for stage in task['contract']['stages'] if stage['id'] == value['stage_id'])
        from .workflow import _target
        normalized = []
        for resource in resources:
            text(resource, 'delegation resource', limit=2048)
            if resource in stage['scope']['resources']:
                normalized.append(resource)
                continue
            if '://' in resource or PureWindowsPath(resource).drive or Path(resource).is_absolute():
                raise ValueError('Delegation resource exceeds approved stage')
            target = _target(self.store.repo, resource)
            boundaries = [_target(self.store.repo, item) for item in stage['scope']['resources']
                if '://' not in item and not PureWindowsPath(item).drive]
            if not any(boundary.is_dir() and target.is_relative_to(boundary) for boundary in boundaries):
                raise ValueError('Delegation resource exceeds approved stage')
            normalized.append(target.relative_to(self.store.repo).as_posix())
        value['resources'] = list(dict.fromkeys(normalized))
        return value

    def register(self, task_id, spec, expected_revision=0):
        with transaction(self.store.db):
            spec = self._spec(task_id, spec)
            key = self._key(task_id, spec['id'])
            old = read_record(self.store.db, 'delegation', key)
            if old['payload'] is not None:
                if old['payload'].get('task_id') == task_id and old['payload'].get('spec') == spec:
                    return old
                raise ValueError('Delegated work ID already binds a different immutable assignment')
            self._revision(old, expected_revision)
            entries = self._entries(task_id)
            if len(entries) >= 1000:
                raise ValueError('Managed root delegation inventory exceeds limit')
            known = {item['payload']['id'] for item in entries}
            if not set(spec['depends_on']).issubset(known):
                raise ValueError('Delegation dependencies must already exist in this managed root')
            value = {'version': 1, 'id': spec['id'], 'task_id': task_id, 'stage_id': spec['stage_id'],
                'root_task_id': self.root_task_id(task_id), 'project_id': self.store.project_id,
                'workspace_id': self.store.workspace_id, 'spec': spec, 'spec_sha256': hash_value(spec),
                'binding': self._binding(task_id, spec['stage_id']), 'state': 'PENDING',
                'attempt': None, 'result': None, 'acceptance': None, 'created_at': now()}
            return self._save(old, value)

    def _dependencies(self, value):
        root = self.root_task_id(value['task_id'])
        records, bindings, verified, visiting = {}, {}, set(), set()

        def reference(work):
            record = records[work]
            return {'revision': record['revision'], 'result_sha256': record['payload']['result']['sha256']}

        # Iterative postorder keeps deep DAGs off Python's call stack and checks
        # each shared prerequisite once per traversal, including its provenance.
        pending = [(work, False) for work in value['spec']['depends_on']]
        while pending:
            work, expanded = pending.pop()
            if work in verified:
                continue
            if expanded:
                parent = records[work]['payload']
                current = {dependency: reference(dependency) for dependency in parent['spec']['depends_on']}
                if current != parent['attempt']['dependencies']:
                    raise ValueError('Delegation prerequisite bindings changed: ' + work)
                visiting.remove(work)
                verified.add(work)
                continue
            if work in visiting:
                raise ValueError('Delegation dependency cycle')
            record = read_record(self.store.db, 'delegation', root + ':' + work)
            parent = record['payload']
            if not parent or parent['state'] != 'ACCEPTED':
                raise ValueError('Delegation dependency is not accepted: ' + work)
            record = self.get(parent['task_id'], work)
            context = (parent['task_id'], parent['stage_id'])
            if context not in bindings:
                bindings[context] = self._binding(*context, allow_completed=True)
            if bindings[context] != parent['binding']:
                raise ValueError('Delegation dependency context is stale: ' + work)
            load_result(self.store, parent)
            records[work] = record
            visiting.add(work)
            pending.append((work, True))
            pending.extend((dependency, False) for dependency in parent['spec']['depends_on'])
        return {work: reference(work) for work in value['spec']['depends_on']}

    def _guard(self, value):
        if self._binding(value['task_id'], value['stage_id']) != value['binding']:
            raise ValueError('Delegation original authority or source context changed')
        return self._dependencies(value)

    @staticmethod
    def _intent(spec):
        return {**{key: item for key, item in spec.items() if key not in {'id', 'resources', 'depends_on'}},
            'resources': sorted(spec['resources']), 'depends_on': sorted(spec['depends_on'])}

    def _dispatch_dependencies(self, value):
        dependencies = self._guard(value)
        # A ready-wave observation and the transactional dispatch must agree.
        # Known distinct RUNNING analyses may still share read-only inputs.
        for item in self._entries(value['task_id']):
            other = item['payload']
            if (other['state'] in {'DISPATCHING', 'RUNNING', 'RESULT_READY', 'UNKNOWN'}
                    and self._intent(other['spec']) == self._intent(value['spec'])):
                raise ValueError('Another unsettled assignment has the same delegated intent')
            if other['state'] in {'DISPATCHING', 'UNKNOWN'} and OperationLedger._scopes_overlap(
                    OperationLedger._scope({'resources': value['spec']['resources']}),
                    OperationLedger._scope({'resources': other['spec']['resources']})):
                raise ValueError('Overlapping native delegation has an unresolved launch')
        return dependencies

    def dispatch(self, task_id, work_id, *, expected_revision, coordinator, backend):
        if self.store.db.in_transaction:
            raise ValueError('Native dispatch must own its transaction; commit the caller transaction before requesting a launch ticket')
        text(coordinator, 'delegation coordinator', limit=300)
        if backend not in {'codex', 'claude', 'copilot'}:
            raise ValueError('Unknown native delegation backend')
        with transaction(self.store.db):
            record = self.get(task_id, work_id)
            self._revision(record, expected_revision)
            value = copy.deepcopy(record['payload'])
            if value['state'] != 'PENDING' or value['attempt'] is not None:
                raise ValueError('Native delegation already dispatched; reconcile its original attempt')
            dependencies = self._dispatch_dependencies(value)
            context = {'project': {'project_id': self.store.project_id, 'workspace_id': self.store.workspace_id,
                'root': str(self.store.repo)}, 'spec_sha256': value['spec_sha256'], 'binding': value['binding'], 'dependencies': dependencies}
            value['attempt'] = {'id': uuid.uuid4().hex, 'backend': backend, 'coordinator': coordinator,
                'context_sha256': hash_value(context), 'dependencies': dependencies, 'native': None, 'prepared_at': now()}
            value['state'] = 'DISPATCHING'
            directory = attempt_root(self.store, value) / 'output'
            directory.mkdir(parents=True, exist_ok=True)
            result = self._save(record, value)
            return {**result, 'handoff': {**context, 'task_id': task_id, 'work_id': work_id,
                'stage_id': value['stage_id'], 'attempt_id': value['attempt']['id'], 'spec': value['spec'],
                'artifact_dir': str(logical_path(directory)),
                'instruction': 'Return only bounded analysis or a proposed change in this private output directory. Preserve the exact attempt and original context. Do not mutate target resources, spawn replacement workers, or claim original acceptance.'}}

    @staticmethod
    def _attempt(value, attempt_id):
        if not isinstance(value.get('attempt'), dict) or value['attempt'].get('id') != attempt_id:
            raise ValueError('Native delegation attempt differs from the reserved attempt')

    def _native_available(self, value, native):
        if not isinstance(native, dict) or set(native) != {'backend', 'agent_id'}:
            raise ValueError('Native handle requires an exact backend and agent ID')
        text(native['agent_id'], 'native agent ID', limit=300)
        if native['backend'] != value['attempt']['backend']:
            raise ValueError('Native backend differs from reserved backend')
        if value['state'] == 'ACCEPTED' and value['attempt']['native'] == native:
            return
        for record in self._entries(value['task_id']):
            other = record['payload']
            if (other['id'] != value['id'] and other['state'] not in {'ACCEPTED', 'CANCELLED'}
                    and (other.get('attempt') or {}).get('native') == native):
                raise ValueError('Native handle already belongs to another unsettled assignment')

    def attach(self, task_id, work_id, attempt_id, native, *, expected_revision):
        if not isinstance(native, dict) or set(native) != {'backend', 'agent_id'}:
            raise ValueError('Native handle requires an exact backend and agent ID')
        text(native['agent_id'], 'native agent ID', limit=300)
        with transaction(self.store.db):
            record = self.get(task_id, work_id)
            value = copy.deepcopy(record['payload'])
            self._attempt(value, attempt_id)
            self._native_available(value, native)
            if native['backend'] != value['attempt']['backend']:
                raise ValueError('Native backend differs from reserved backend')
            previous = value['attempt']['native']
            if previous is not None and previous != native:
                raise ValueError('Native delegation handle is immutable')
            if previous == native and value['state'] in {'RUNNING', 'RESULT_READY', 'ACCEPTED'}:
                return record
            self._revision(record, expected_revision)
            if value['state'] not in {'DISPATCHING', 'UNKNOWN', 'RUNNING'}:
                raise ValueError('Native delegation cannot attach in this state')
            value['attempt']['native'] = copy.deepcopy(native)
            value['state'] = 'RUNNING'
            return self._save(record, value)

    def result(self, task_id, work_id, attempt_id, payload, *, expected_revision):
        with transaction(self.store.db):
            record = self.get(task_id, work_id)
            value = copy.deepcopy(record['payload'])
            self._attempt(value, attempt_id)
            self._native_available(value, {'backend': value['attempt']['backend'],
                'agent_id': payload.get('agent_id') if isinstance(payload, dict) else None})
            if value['state'] not in {'DISPATCHING', 'RUNNING', 'UNKNOWN', 'RESULT_READY', 'ACCEPTED'}:
                raise ValueError('Native delegation cannot receive a result in this state')
            if value['state'] not in {'RESULT_READY', 'ACCEPTED'}:
                self._revision(record, expected_revision)
            reference = seal_result(self.store, value, payload)
            if value.get('result') == reference and value['state'] in {'RESULT_READY', 'ACCEPTED'}:
                load_result(self.store, value)
                self._notify(value)
                return record
            self._revision(record, expected_revision)
            value['result'] = reference
            value['result_summary'] = {'status': payload['status'], 'summary': payload['summary']}
            value['attempt']['native'] = {'backend': value['attempt']['backend'], 'agent_id': payload['agent_id']}
            value['state'] = 'RESULT_READY'
            return self._save(record, value)

    def accept(self, task_id, work_id, attempt_id, *, expected_revision, coordinator, summary):
        text(summary, 'delegation acceptance summary', limit=4000)
        with transaction(self.store.db):
            record = self.get(task_id, work_id)
            value = copy.deepcopy(record['payload'])
            self._attempt(value, attempt_id)
            if value['attempt']['coordinator'] != coordinator:
                raise ValueError('Only the original delegation coordinator accepts this result')
            if value['state'] != 'RESULT_READY':
                if value['state'] == 'ACCEPTED' and value['acceptance']['summary'] == summary:
                    load_result(self.store, value)
                    return record
                raise ValueError('Native result must be recorded before coordinator acceptance')
            self._revision(record, expected_revision)
            dependencies = self._guard(value)
            if dependencies != value['attempt']['dependencies']:
                raise ValueError('Delegation dependency results changed during the native attempt')
            sealed = load_result(self.store, value)
            if sealed['status'] != 'COMPLETE':
                raise ValueError('A failed or blocked native result is not acceptable completion')
            value['state'] = 'ACCEPTED'
            value['acceptance'] = {'coordinator': coordinator, 'summary': summary, 'at_utc': now(),
                'kind': 'advisory_only', 'result_sha256': value['result']['sha256']}
            return self._save(record, value)

    def cancel(self, task_id, work_id, *, expected_revision, reason):
        text(reason, 'delegation cancellation reason', limit=4000)
        with transaction(self.store.db):
            record = self.get(task_id, work_id)
            self._revision(record, expected_revision)
            value = copy.deepcopy(record['payload'])
            if value['state'] == 'RESULT_READY':
                load_result(self.store, value)
            elif value['state'] != 'PENDING' or value['attempt'] is not None:
                raise ValueError('Active or unknown native work needs terminal reconciliation before cancellation')
            value.update(state='CANCELLED', cancellation={'reason': reason, 'at_utc': now()})
            return self._save(record, value)

    def recover(self, task_id, work_id=None):
        selected = [self.get(task_id, work_id)] if work_id is not None else [item for item in self._entries(task_id)
            if item['payload']['task_id'] == task_id or self._belongs(item['payload']['task_id'], task_id, None)]
        recovered = []
        for original in selected:
            owner, work = original['payload']['task_id'], original['payload']['id']
            with transaction(self.store.db):
                record = self.get(owner, work)
                value = copy.deepcopy(record['payload'])
                if value['state'] in {'DISPATCHING', 'RUNNING', 'UNKNOWN'}:
                    try:
                        sealed = load_result(self.store, value)
                        self._native_available(value, {'backend': sealed['backend'], 'agent_id': sealed['agent_id']})
                    except (ValueError, OSError, TypeError, KeyError):
                        if value['state'] != 'UNKNOWN':
                            value.update(state='UNKNOWN', recovery_reason='Observe the original native attempt; no complete sealed result is available. Do not spawn a replacement from timeout or absence.')
                            record = self._save(record, value)
                    else:
                        path = attempt_root(self.store, value) / 'result.json'
                        from .protocol import sha256
                        from .runner import _read
                        value['result'] = {'path': str(logical_path(path)), 'sha256': sha256(_read(path, 65536))}
                        value['result_summary'] = {'status': sealed['status'], 'summary': sealed['summary']}
                        value['attempt']['native'] = {'backend': sealed['backend'], 'agent_id': sealed['agent_id']}
                        value['state'] = 'RESULT_READY'
                        record = self._save(record, value)
                self._notify(value)
                recovered.append(record)
        return {'task_id': task_id, 'work': recovered, 'authority': 'Recovery observations only; no native launch, target effect or semantic acceptance'}

    def _belongs(self, owner, task_id, stage_id):
        if owner == task_id:
            return stage_id is None
        seen = set()
        while owner not in seen:
            seen.add(owner)
            task = self.tasks.snapshot(owner)
            parent = task['contract'].get('parent_task_id')
            if parent == task_id:
                return stage_id is None or task['contract'].get('parent_stage_id') == stage_id
            if not parent:
                return False
            owner = parent
        raise ValueError('Delegation task ancestry cycle')

    def closure_blockers(self, task_id, stage_id=None):
        blockers = []
        for item in self._entries(task_id):
            value = item['payload']
            relevant = (value['task_id'] == task_id and (stage_id is None or value['stage_id'] == stage_id)) or self._belongs(value['task_id'], task_id, stage_id)
            if not relevant:
                continue
            if value['state'] not in {'ACCEPTED', 'CANCELLED'}:
                blockers.append('Delegated work is unsettled: ' + value['id'] + ' (' + value['state'] + ')')
            elif value.get('result'):
                try:
                    load_result(self.store, value)
                except (ValueError, OSError, TypeError, KeyError):
                    blockers.append('Delegated result integrity requires reconciliation: ' + value['id'])
        return blockers

    def closure_binding(self, task_id):
        root = self.root_task_id(task_id)
        blockers = self.closure_blockers(root)
        if blockers:
            raise ValueError('; '.join(blockers))
        return hash_value([{'task_id': item['payload']['task_id'], 'id': item['payload']['id'],
            'revision': item['revision']} for item in self._entries(root)])

    def packet_evidence(self, task_id, stage_id=None):
        """Host-capture operational history without treating it as provider AC."""
        selected = [item for item in self._entries(task_id) if (
            item['payload']['task_id'] == task_id and (stage_id is None or item['payload']['stage_id'] == stage_id))
            or self._belongs(item['payload']['task_id'], task_id, stage_id)]
        identities = {(item['payload']['task_id'], item['payload']['id']) for item in selected}
        results = {item['payload']['id']: load_result(self.store, item['payload']) for item in selected if item['payload'].get('result')}
        events = []
        for row in self.store.db.execute("SELECT sequence,kind,task_id,payload,created FROM runtime_events WHERE kind LIKE 'delegation_%' ORDER BY sequence"):
            payload = json.loads(row['payload'])
            if (row['task_id'], payload.get('work_id')) in identities:
                events.append({'sequence': row['sequence'], 'kind': row['kind'], 'task_id': row['task_id'],
                    'payload': payload, 'created_at_utc': row['created']})
        evidence = {'task_id': task_id, 'work': selected, 'results': results, 'events': events,
            'authority': 'Host-captured advisory registry/history and hash-verified result manifests. Native handles are coordinator observations, not machine-authenticated provider execution or independent semantic AC.'}
        # Preserve the existing small-history shape. Large histories use a
        # complete identity index, leaving room for actual required source.
        # Every result above is still loaded and hash-verified before either
        # representation is selected; compaction never repairs invalid proof.
        if len(encode(evidence).encode('utf-8')) <= 32768:
            return evidence
        return self._compact_packet_evidence(evidence)

    def _compact_packet_evidence(self, evidence):
        from .core import redact
        contexts, index, states = {}, [], {}
        for record in evidence['work']:
            value = record['payload']
            context = (value['task_id'], value['stage_id'])
            if context not in contexts:
                try:
                    contexts[context] = {'sha256': hash_value(self._binding(*context, allow_completed=True))}
                except (ValueError, OSError, KeyError, TypeError) as error:
                    # This comparison is advisory identity provenance, never a
                    # replacement for current authority or closure blockers.
                    contexts[context] = {'sha256': None, 'unavailable_reason': redact(error)}
            original = hash_value(value['binding'])
            current = contexts[context]['sha256']
            binding = {'original_sha256': original, 'current_sha256': current,
                'status': 'UNAVAILABLE' if current is None else 'CURRENT' if current == original else 'CHANGED'}
            if current is None:
                binding['unavailable_reason'] = contexts[context]['unavailable_reason']
            attempt = value.get('attempt')
            sealed = evidence['results'].get(value['id'])
            acceptance = value.get('acceptance')
            index.append({'task_id': value['task_id'], 'work_id': value['id'],
                'stage_id': value['stage_id'], 'revision': record['revision'], 'state': value['state'],
                'record_sha256': hash_value(record), 'spec_sha256': value['spec_sha256'], 'binding': binding,
                'attempt': None if attempt is None else {
                    **{key: attempt[key] for key in ('id', 'backend', 'coordinator', 'context_sha256', 'native')},
                    'dependencies_sha256': hash_value(attempt['dependencies'])},
                'result': None if sealed is None else {'reference': value['result'],
                    'manifest_sha256': hash_value(sealed), 'status': sealed['status'],
                    'artifact_count': len(sealed['artifacts']), 'artifacts_sha256': hash_value(sealed['artifacts'])},
                'acceptance': None if acceptance is None else {'sha256': hash_value(acceptance),
                    **{key: acceptance[key] for key in ('kind', 'coordinator', 'result_sha256')}}})
            states[value['state']] = states.get(value['state'], 0) + 1
        return {'format': 'compact_advisory_index_v1', 'task_id': evidence['task_id'],
            'project_id': self.store.project_id, 'workspace_id': self.store.workspace_id,
            'root_task_id': self.root_task_id(evidence['task_id']), 'work_index': index,
            'counts': {'work': len(index), 'results': len(evidence['results']), 'events': len(evidence['events']),
                'unsettled': sum(count for state, count in states.items() if state not in {'ACCEPTED', 'CANCELLED'}), 'states': states},
            'history_sha256': hash_value(evidence), 'history_bytes': len(encode(evidence).encode('utf-8')),
            'omitted': {'specs_results_events': 'NOT_SUPPLIED_NOT_REVIEWED',
                'details': 'Full specifications, result/acceptance summary text, artifact bodies and event history are omitted from this advisory transport. All selected identities are indexed; referenced manifests and artifact hashes were revalidated, not semantically reviewed. Full history is bound by history_sha256.'},
            'authority': evidence['authority'] + ' Binding CURRENT means matching identity inputs only, not current approval. Unsettled work and result-integrity failures retain their independent closure guards.'}

    def snapshot(self, task_id):
        entries = self._entries(task_id)
        ready, blocked = [], {}
        for item in entries:
            value = item['payload']
            if value['state'] == 'PENDING':
                try:
                    self._dispatch_dependencies(value)
                except (ValueError, OSError, TypeError, KeyError) as ex:
                    from .core import redact
                    blocked[value['id']] = [redact(ex)]
                else:
                    ready.append(value['id'])
            elif value['state'] not in {'ACCEPTED', 'CANCELLED'}:
                blocked[value['id']] = ['Observe and settle the exact native attempt; state=' + value['state']]
        return {'task_id': task_id, 'root_task_id': self.root_task_id(task_id), 'work': entries,
            'ready': ready, 'blocked': blocked, 'closure_blockers': self.closure_blockers(task_id),
            'authority': 'Operational advisory registry; native spawning belongs to the bot and original acceptance remains independently gated'}
