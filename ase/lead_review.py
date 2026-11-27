"""Original-lead critiques: native bots think; local CAS/captures retain evidence.

Native origins are cooperative host attestations, not independent verification
or isolation against another process running as the same OS user.
"""
import copy
from contextlib import contextmanager
import json
import os
from pathlib import Path
import uuid

from .native_paths import logical_path, state_io
from .profiles import evaluate_contract_profiles
from .projects import _safe_state
from .protocol import canonical, decode_json, sha256, validate_payload
from .runner import _read, verify_receipt
from .state import encode, hash_value, identifier, read_record, text, transaction, write_record
from .store import now

POLICY = {'version': 1, 'required': True, 'additional_passes': 3}
# Review depth scales with risk: one whole-solution pass for LOW/MEDIUM, three focused passes for HIGH.
PASSES_BY_RISK = {'LOW': 1, 'MEDIUM': 1, 'HIGH': 3}


class _StaleLeadChecks(ValueError):
    """Fresh execution is required before a lead critique can start."""


def policy_for_risk(risk):
    return {**POLICY, 'additional_passes': PASSES_BY_RISK.get(risk or 'MEDIUM', 3)}


def _template_placeholder(value):
    """First unreplaced capture_template '<...>' string, so an unedited template never records."""
    if isinstance(value, str):
        stripped = value.strip()
        return stripped if (len(stripped) > 2 and stripped[0] == '<' and stripped[-1] == '>'
                            and '\n' not in stripped and '<' not in stripped[1:-1] and '>' not in stripped[1:-1]) else None
    children = value.values() if isinstance(value, dict) else value if isinstance(value, list) else ()
    return next((found for found in map(_template_placeholder, children) if found), None)


def required_passes(contract):
    """Configured depth, never below the risk minimum (also for contracts stored before the minimum)."""
    configured = validate_policy(contract['lead_review'])['additional_passes']
    return max(configured, PASSES_BY_RISK.get(contract.get('risk') or 'MEDIUM', 3))


def bind_local_action(task_id, revision):
    """The host step that binds the original lead chat before native critiques."""
    return ('Bind this original lead chat as the task coordinator (a host step, not an owner question): '
            f'`ase events bind-local {task_id} --backend <this chat\'s backend: codex|claude|copilot> '
            f'--user-message-ref <the owner reply that approved the plan> --expected-revision {revision}`; '
            'use `ase events bind ... --file <destination.json>` instead when the host exposes the native chat id.')

AREAS = ('business', 'failure', 'performance', 'security', 'tests')
FOCI = ('requirements-business', 'failure-performance-security', 'whole-solution-requirements')
MAX_CAPTURE = 1048576
MAX_LINEAGE = 1000


def _requirements_value(original, intents):
    """Keep the original anchor representation identical for every approved prefix."""
    latest = intents[-1]
    rows = [{'id': 'original-request', 'expected': original['request']},
            {'id': 'approved-request', 'expected': latest['contract']['request']},
            {'id': 'approved-clarifications', 'expected': encode(latest['proposal'])}]
    rows.extend({'id': 'AC:' + ac['id'], 'expected': ac['expected']} for ac in latest['contract']['acceptance'] if ac.get('mandatory', True))
    for row in rows:
        row['expected_sha256'] = hash_value(row['expected'])
    return {'original_contract': original, 'approved_intents': intents, 'requirements': rows,
        'interpretation': 'Reconcile original intent with explicit approved supersessions. Current implementation and tests cannot redefine expected behavior.'}


def validate_intent_text(value, name='approved intent', *, limit=MAX_CAPTURE):
    """Reject credential-bearing structured fields as well as inline secrets."""
    secret_keys = {'password', 'passwd', 'pwd', 'apikey', 'token', 'secret',
        'accesstoken', 'refreshtoken', 'clientsecret', 'privatekey'}
    def inspect(node):
        if isinstance(node, dict):
            for key, child in node.items():
                normalized = str(key).lower().replace('_', '').replace('-', '')
                if normalized in secret_keys and child is not None and child != '':
                    raise ValueError(name + ' must use credential references, not secret fields')
                inspect(child)
        elif isinstance(node, list):
            for child in node:
                inspect(child)
    inspect(value)
    text(encode(value), name, limit=limit)


def validate_policy(value):
    if (not isinstance(value, dict) or set(value) != set(POLICY)
            or type(value['version']) is not int or value['version'] != 1
            or type(value['additional_passes']) is not int or value['additional_passes'] not in (1, 2, 3)
            or value['required'] is not True):
        raise ValueError('lead_review requires version 1, required true and 1-3 additional passes')
    return copy.deepcopy(value)


def archive_approved_intent(store, task):
    """Retain exact still-available approved text; never invent a past proposal."""
    approval = read_record(store.db, 'approval', task['task_id'])['payload']
    if (not approval or not task.get('proposal')
            or approval['contract_hash'] != task['contract_hash']
            or approval['proposal_hash'] != task.get('proposal_hash')):
        return
    if hash_value({'contract_hash': task['contract_hash'], 'proposal': task['proposal']}) != approval['proposal_hash']:
        raise ValueError('Approved proposal text does not match its authenticated identity')
    versions = [(key, json.loads(payload)) for key, payload in store.db.execute(
        "SELECT key,payload FROM runtime_records WHERE kind='approval_version' AND key GLOB ?", (task['task_id'] + ':*',))]
    matches = [key for key, value in versions if value == approval]
    if len(matches) != 1:
        raise ValueError('Approved intent lacks an exact immutable approval version')
    value = {'version': 1, 'project_id': store.project_id, 'workspace_id': store.workspace_id,
        'task_id': task['task_id'], 'approval_version_key': matches[0],
        'contract': task['contract'], 'proposal': task['proposal'], 'approval': approval}
    value['sha256'] = hash_value(value)
    validate_intent_text(value, limit=800000)
    old = read_record(store.db, 'approved_intent', matches[0])
    if old['payload'] is not None:
        if old['payload'] != value:
            raise ValueError('Immutable approved intent conflicts with recorded text')
        return
    write_record(store.db, 'approved_intent', matches[0], value, expected_revision=0)


def _object(properties):
    return {'type': 'object', 'properties': properties, 'required': list(properties), 'additionalProperties': False}


_STR = {'type': 'string', 'minLength': 1, 'maxLength': 8000}
_LIST = {'type': 'array', 'maxItems': 100, 'items': _STR}
_PROOF = _object({'check_id': _STR, 'source': {**_STR, 'description': 'Exact source_paths or matching check_sources path from prepared base; no :line suffix. Put line references in assertion.'}, 'assertion': _STR,
    'kind': {'type': 'string', 'enum': ['check', 'measurement']}})
_PROOFS = {'type': 'array', 'maxItems': 100, 'items': _PROOF}
LEGACY_ASSESSMENT_SCHEMA = _object({
    'requirements': {'type': 'array', 'maxItems': 1000, 'items': _object({
        'id': _STR, 'expected_sha256': _STR, 'status': {'type': 'string', 'enum': ['PASS', 'FAIL', 'UNVERIFIED']},
        'evidence': _PROOFS, 'reason': _STR})},
    'risks': {'type': 'array', 'minItems': len(AREAS), 'maxItems': len(AREAS),
        'description': 'Exactly one row for EVERY area (business, failure, performance, security, tests): CHECKED with evidence, or NOT_APPLICABLE/GAP with a reason.',
        'items': _object({
        'area': {'type': 'string', 'enum': list(AREAS)},
        'status': {'type': 'string', 'enum': ['CHECKED', 'NOT_APPLICABLE', 'GAP']}, 'reason': _STR, 'evidence': _PROOFS})},
    'findings': {'type': 'array', 'maxItems': 100, 'items': _object({
        'id': _STR, 'requirement_id': _STR, 'priority': {'type': 'string', 'enum': ['P0', 'P1', 'P2', 'P3']},
        'description': _STR, 'evidence': _PROOFS})},
    'resolutions': {'type': 'array', 'maxItems': 100, 'items': _object({
        'id': _STR, 'outcome': {'type': 'string', 'enum': ['fixed', 'not_a_bug']}, 'reason': _STR, 'evidence': _PROOFS})},
    'remaining_gaps': {**_LIST, 'description': 'Only material unmet original/approved solution requirements or proof required at this critique. Pending recording of this response and subsequent independent AC/review/adequacy or publication are later workflow obligations, not current gaps unless an original criterion explicitly requires them now. Mention nonblocking later steps in summary.'}, 'summary': _STR})

ASSESSMENT_SCHEMA = copy.deepcopy(LEGACY_ASSESSMENT_SCHEMA)
_resolution = ASSESSMENT_SCHEMA['properties']['resolutions']['items']
_resolution['properties']['origin_anchor_sha256'] = {'type': 'string', 'maxLength': 64,
    'description': 'Empty for a finding in this approved series; otherwise the exact inherited finding origin anchor from finding_ledger.'}
_resolution['required'].append('origin_anchor_sha256')

DISPOSITION_SCHEMA = _object({
    'classification_status': {'type': 'string', 'enum': ['AGREE', 'DISAGREE', 'UNVERIFIED']},
    'pass_assessment': {'type': 'string', 'enum': ['SUFFICIENT', 'INSUFFICIENT', 'UNVERIFIED']},
    'reason': _STR, 'evidence': _LIST})


def acceptance_schema(schema):
    value = copy.deepcopy(schema)
    value['properties']['lead_review'] = DISPOSITION_SCHEMA
    value['required'].append('lead_review')
    return value


def disposition_errors(payload):
    decision = payload.get('lead_review') if isinstance(payload, dict) else None
    if validate_payload(decision, DISPOSITION_SCHEMA)['status'] != 'VALID':
        return ['Independent AC lead classification/pass assessment is missing or malformed']
    if (decision['classification_status'] != 'AGREE' or decision['pass_assessment'] != 'SUFFICIENT'
            or not decision['evidence']):
        return ['Independent AC has not confirmed lead classification and sufficient critique evidence: ' + decision['reason']]
    return []


class LeadReview:
    def __init__(self, store):
        from .tasks import TaskService
        from .lead_inputs import LeadInputs
        self.store, self.tasks = store, TaskService(store)
        self.inputs = LeadInputs(store)

    def configured(self, task_id):
        task = self.tasks.snapshot(task_id)
        if 'lead_review' not in task['contract']:
            return False
        validate_policy(task['contract']['lead_review'])
        return not task['contract'].get('parent_task_id')

    def requirements(self, task_id):
        """Read authenticated approved text even while an unapproved draft exists."""
        task = self.tasks.snapshot(task_id)
        versions = [(key, json.loads(raw)) for key, raw in self.store.db.execute(
            "SELECT key,payload FROM runtime_records WHERE kind='task_version' AND key GLOB ?", (task_id + ':*',))]
        versions.sort(key=lambda item: int(item[0].rsplit(':', 1)[1]) if item[0].rsplit(':', 1)[1].isdigit() else -1)
        if not versions:
            raise ValueError('Original requirements snapshot is unavailable')
        original = versions[0][1]['contract']
        intents = []
        approval_versions = list(self.store.db.execute(
            "SELECT key,payload FROM runtime_records WHERE kind='approval_version' AND key GLOB ?", (task_id + ':*',)))
        approval_versions.sort(key=lambda row: int(row[0].rsplit(':', 1)[1]))
        for key, raw in approval_versions:
            snapshot = read_record(self.store.db, 'approved_intent', key)['payload']
            if snapshot is None:
                raise ValueError('Approved clarification text unavailable for ' + key)
            value = {k: v for k, v in snapshot.items() if k != 'sha256'}
            approval = json.loads(raw)
            if (hash_value(value) != snapshot.get('sha256') or snapshot['approval'] != approval
                    or snapshot['project_id'] != self.store.project_id or snapshot['workspace_id'] != self.store.workspace_id
                    or snapshot['task_id'] != task_id or snapshot['approval_version_key'] != key
                    or hash_value(snapshot['contract']) != approval['contract_hash']
                    or hash_value({'contract_hash': approval['contract_hash'], 'proposal': snapshot['proposal']}) != approval['proposal_hash']):
                raise ValueError('Approved requirements provenance differs from its immutable approval')
            intents.append(snapshot)
        if not intents:
            raise ValueError('Current approved requirements are unavailable')
        latest = intents[-1]
        value = _requirements_value(original, intents)
        validate_intent_text(value, 'approved requirements export')
        return {**value, 'anchor_sha256': hash_value(value),
            'eligible': task.get('proposal_hash') == latest['approval']['proposal_hash']
                and task['contract_hash'] == latest['approval']['contract_hash']
                and read_record(self.store.db, 'approval', task_id)['payload'] == latest['approval']}

    def _owner(self, task_id, thread_id=None):
        from .coordinator import CoordinatorEvents
        owner = CoordinatorEvents(self.store)._owner(task_id, thread_id)
        if owner['project_id'] != self.store.project_id or owner['root_task_id'] != task_id:
            raise ValueError('Original lead belongs to another project/root')
        return copy.deepcopy(owner['destination'])

    def _lineage(self, task_id, value=None):
        """Authenticate prior series retrospectively, including pre-carry databases."""
        requirements = self.requirements(task_id)
        anchors = {}
        for length in range(1, len(requirements['approved_intents']) + 1):
            prefix = _requirements_value(requirements['original_contract'], requirements['approved_intents'][:length])
            anchor = hash_value(prefix)
            anchors[anchor] = {'requirements': {**prefix, 'anchor_sha256': anchor, 'eligible': True},
                'approval_version_key': prefix['approved_intents'][-1]['approval_version_key'], 'order': length}
        rows = list(self.store.db.execute(
            "SELECT key,payload FROM runtime_records WHERE kind='lead_review_history' AND key GLOB ? LIMIT ?",
            (task_id + ':*', MAX_LINEAGE + 1)))
        if len(rows) > MAX_LINEAGE or sum(len(raw.encode('utf-8')) for _, raw in rows) > 8 * MAX_CAPTURE:
            raise ValueError('Prior lead lineage exceeds its bounded inventory')
        records = {}
        for key, raw in rows:
            record = decode_json(raw.encode('utf-8'))
            anchor = record['anchor_sha256']
            if key != task_id + ':' + anchor:
                raise ValueError('Historical lead series has a foreign anchor key')
            records[anchor] = record
        value = self.snapshot(task_id)['payload'] if value is None else value
        if value:
            anchor = value['anchor_sha256']
            if anchor in records and encode(records[anchor]) != encode(value):
                raise ValueError('Conflicting historical/current lead series')
            records[anchor] = value
        series, references, turns = [], {}, set()
        for anchor, record in records.items():
            if record['task_id'] != task_id or anchor not in anchors:
                raise ValueError('Historical lead series lacks an authenticated approved prefix')
            if not isinstance(record['passes'], list) or len(record['passes']) > MAX_LINEAGE - len(references):
                raise ValueError('Credited lead lineage exceeds its bounded capture inventory')
            captures = self._captures(record)
            for capture, reference in zip(captures, record['passes']):
                if encode(capture['ticket']['requirements']) != encode(anchors[anchor]['requirements']):
                    raise ValueError('Historical lead requirements differ from the approved prefix')
                attempt = capture['ticket']['id']; turn = capture['result']['origin']['turn_ref']
                if attempt in references or turn in turns:
                    raise ValueError('Duplicated credited lead attempt/turn in lineage')
                references[attempt] = reference; turns.add(turn)
            series.append({'anchor_sha256': anchor, **anchors[anchor], 'record': record, 'captures': captures})
        if len(references) > MAX_LINEAGE:
            raise ValueError('Credited lead lineage exceeds its bounded capture inventory')
        projected = list(self.store.db.execute(
            "SELECT key,payload FROM runtime_records WHERE kind='lead_review_capture' AND key GLOB ? LIMIT ?",
            (task_id + ':*', MAX_LINEAGE + 1)))
        if len(projected) > MAX_LINEAGE:
            raise ValueError('Credited lead projection exceeds its bounded inventory')
        credited = {}
        for key, raw in projected:
            reference = decode_json(raw.encode('utf-8'))
            if reference.get('credited') is True:
                credited[key.removeprefix(task_id + ':')] = {k: v for k, v in reference.items() if k != 'credited'}
        if encode(credited) != encode(references):
            raise ValueError('Credited lead captures are missing or conflict with authenticated history')
        series.sort(key=lambda item: item['order'])
        return requirements, series

    def _context(self, task_id, value=None, candidate=None):
        requirements, series = self._lineage(task_id, value)
        # Validate an already-observed candidate under its prepared approved
        # epoch, even if a later approval arrived. Currentness separately decides
        # credit; stale output must still seal verbatim and settle its ticket.
        anchor = candidate['ticket']['base']['anchor_sha256'] if candidate else requirements['anchor_sha256']
        boundary = next((item['order'] for item in series if item['anchor_sha256'] == anchor), None)
        if candidate and boundary is None:
            raise ValueError('Candidate lacks its original authenticated lead series')
        prior = [item for item in series if item['anchor_sha256'] != anchor
                 and (boundary is None or item['order'] < boundary)]
        active = next((item for item in series if item['anchor_sha256'] == anchor), None)
        captures = list(active['captures']) if active else []
        if candidate is not None:
            captures.append(candidate)
        ledger = self._ledger(captures, prior=prior, current_anchor=anchor)
        # Only open prior obligations need their original bodies in the current
        # model packet. All lineage identities remain authenticated and bound.
        prior_ledger = self._ledger([], prior=prior, current_anchor=anchor)
        needed = {row['origin_anchor_sha256'] for row in prior_ledger.values() if row['state'] == 'OPEN'}
        evidence = [item for item in prior if item['anchor_sha256'] in needed]
        return {'requirements': requirements, 'series': series, 'prior': prior, 'active': active,
            'captures': captures, 'ledger': ledger, 'evidence': evidence,
            'prior_sha256': hash_value([{'anchor_sha256': item['anchor_sha256'], 'record': item['record']} for item in prior])}

    def _base(self, task_id):
        from .gates import source_snapshot, verification_context_hash
        task = self.tasks.snapshot(task_id)
        anchor = self.requirements(task_id)
        if not anchor['eligible']:
            raise ValueError('Current approved requirement anchor is not eligible')
        self.tasks.require_approval(task_id, task['approved_stage_ids'][0], allow_terminal=task['state'] == 'COMPLETED')
        plan = self.tasks.execution_plan(task_id)
        source = source_snapshot(self.store, task['contract'], task_id=task_id)
        from .evidence.check_inputs import gate_blockers
        blockers = gate_blockers(source.get('check_inputs', {}))
        if blockers:
            raise ValueError('; '.join(blockers))
        checks = read_record(self.store.db, 'checks', task_id)['payload']
        if (not checks or checks.get('contract_hash') != task['contract_hash']
                or checks.get('plan_hash') != plan['plan_hash'] or checks.get('workspace_id') != self.store.workspace_id
                or checks.get('fingerprint') != source['fingerprint'] or checks.get('source_changed_during_checks')):
            raise _StaleLeadChecks('Current executed source/check evidence is missing or stale')
        profile = evaluate_contract_profiles(task['contract'], plan['checks'], checks['results'])
        if profile['status'] != 'PASS':
            raise ValueError('Required checks are not passing')
        check_sources = {r['id']: '_ase_context/logs/' + r['id'] + '.txt' if r.get('log')
            else '_ase_context/actual_checks.json' for r in checks['results']
            if profile['by_id'].get(r['id'], {}).get('status') in {'PASS', 'FAIL'}}
        # Execution timestamps/paths/durations change on an unchanged recheck.
        # Only source/plan/action or parsed observed outcomes can support a fix;
        # independent AC still judges whether that change actually fixes it.
        correction_sha256 = hash_value({'fingerprint': source['fingerprint'], 'plan_hash': plan['plan_hash'],
            'actions': verification_context_hash(self.store, task['contract'], task_id, _include_lead=False),
            'observations': {k: {f: v[f] for f in ('status', 'counts', 'observation', 'sha256') if f in v}
                for k, v in profile['by_id'].items()}})
        return {'project_id': self.store.project_id, 'workspace_id': self.store.workspace_id,
            'root': str(self.store.repo), 'task_id': task_id, 'owner': self._owner(task_id),
            'anchor_sha256': anchor['anchor_sha256'], 'contract_hash': task['contract_hash'], 'proposal_hash': task['proposal_hash'],
            'plan_hash': plan['plan_hash'], 'fingerprint': source['fingerprint'], 'check_hash': hash_value(checks),
            'action_context_sha256': verification_context_hash(self.store, task['contract'], task_id, _include_lead=False),
            'source_hashes': dict(source['files']),
            'source_paths': sorted(set(source['files']) | set(check_sources.values())),
            'check_sources': check_sources, 'correction_sha256': correction_sha256,
            'check_ids': sorted(key for key, value in profile['by_id'].items() if value['status'] in {'PASS', 'FAIL'}),
            'passing_check_ids': sorted(key for key, value in profile['by_id'].items() if value['status'] == 'PASS'),
            'prior_lineage_sha256': self._context(task_id)['prior_sha256']}

    def snapshot(self, task_id):
        self.tasks.snapshot(task_id)
        return read_record(self.store.db, 'lead_review', task_id)

    def _fresh(self, task_id, old):
        anchor = self.requirements(task_id)['anchor_sha256']
        value = copy.deepcopy(old['payload'])
        seen_turns = list(value['seen_turns']) if value else []
        if value and value['anchor_sha256'] != anchor:
            if value['inflight']:
                raise ValueError('Reconcile the existing native critique before a new approved series')
            key = task_id + ':' + value['anchor_sha256']
            previous = read_record(self.store.db, 'lead_review_history', key)
            if previous['payload'] is None:
                write_record(self.store.db, 'lead_review_history', key, value, expected_revision=0)
            elif encode(previous['payload']) != encode(value):
                raise ValueError('Conflicting immutable historical lead series')
            value = None
        return value or {'version': 1, 'task_id': task_id, 'anchor_sha256': anchor,
            'classification': None, 'passes': [], 'inflight': None, 'seen_turns': seen_turns, 'last_stale': None}

    def _eligible_trivial(self, task_id):
        task = self.tasks.snapshot(task_id); c = task['contract']
        plan = self.tasks.execution_plan(task_id)
        if (c['risk'] != 'LOW' or c['profile'] == 'mixed' or len(c['stages']) != 1
                or any(s.get('actions') or s.get('required_child_task_id') for s in c['stages'])
                or any('access' in spec for spec in plan['checks'])
                or self.tasks.readiness(task_id)['required_source_gaps']
                or any(u.get('status', 'UNRESOLVED') == 'UNRESOLVED' for u in c.get('unknowns', []))):
            raise ValueError('Trivial exemption requires current LOW mechanical work without actions/access/gaps/children')

    def classify(self, task_id, payload, *, thread_id, expected_revision):
        self._owner(task_id, thread_id)
        if not self.configured(task_id):
            raise ValueError('Only configured root tasks have a lead classification')
        if not isinstance(payload, dict) or set(payload) != {'kind', 'reason', 'evidence_ids', 'source_refs'} or payload['kind'] not in {'trivial', 'nontrivial'}:
            raise ValueError('Classification requires a supported kind, concrete reason and current evidence')
        text(payload['reason'], 'classification reason', limit=8000)
        for field in ('evidence_ids', 'source_refs'):
            if (not isinstance(payload[field], list) or not 1 <= len(payload[field]) <= 100
                    or any(not isinstance(item, str) or not item for item in payload[field])):
                raise ValueError('Classification requires bounded evidence/source lists')
        with transaction(self.store.db):
            base = self._base(task_id)
            if payload['kind'] == 'trivial':
                self._eligible_trivial(task_id)
            if (not payload['evidence_ids'] or not set(payload['evidence_ids']).issubset(base['passing_check_ids'])
                    or not payload['source_refs'] or not set(payload['source_refs']).issubset(base['source_paths'])):
                raise ValueError('Classification lacks current check/source evidence')
            old = self.snapshot(task_id)
            if old['revision'] != expected_revision:
                raise ValueError('Stale lead review revision')
            value = self._fresh(task_id, old)
            if value['inflight'] or value['passes']:
                raise ValueError('Classification cannot erase existing critique work/findings')
            value['classification'] = {**copy.deepcopy(payload), 'base': base}
            return write_record(self.store.db, 'lead_review', task_id, value, expected_revision=expected_revision)

    def _require_initial_capture(self, task_id, base, receipt):
        from .evidence.check_inputs import captured_hashes
        from .evidence.compact import expand_packet
        from .gates import fingerprint_with_check_inputs, source_snapshot

        if not receipt:
            raise ValueError('Initial implementation capture is required before additional critiques')
        actual = verify_receipt(receipt, require_current_policy=False)
        raw_request = _read(receipt['artifacts']['request']['path'], 8388608)
        if sha256(raw_request) != receipt['request_sha256']:
            raise ValueError('Initial implementation request changed')
        request = decode_json(raw_request)
        if (actual['request_identity']['project_id'] != self.store.project_id
                or logical_path(request['project']['root']).resolve() != logical_path(self.store.repo).resolve()
                or not logical_path(receipt['artifact_root']).resolve().is_relative_to(logical_path(self.store.root).resolve())):
            raise ValueError('Initial implementation belongs to another project/root')
        task = self.tasks.snapshot(task_id)
        captured_stage = request.get('handoff', {}).get('stage', {}).get('id')
        if actual['request_identity']['role'] != 'implementation' or captured_stage not in task['approved_stage_ids']:
            raise ValueError('Initial implementation capture must have the implementation role and name an approved stage')
        bindings = receipt['bindings']
        identity = {'contract_sha256': base['contract_hash'], 'proposal_sha256': base['proposal_hash'],
                    'plan_sha256': base['plan_hash'], 'workspace_id': base['workspace_id']}
        if actual['result']['status'] != 'COMPLETE' or any(bindings.get(k) != v for k, v in identity.items()):
            raise ValueError('Initial implementation capture is stale')
        if actual['policy_current'] and bindings == {**identity, 'fingerprint': base['fingerprint'],
                                                     'check_sha256': base['check_hash']}:
            return

        source = source_snapshot(self.store, task['contract'], task_id=task_id) if task['contract'].get('check_inputs') else {}
        if source and source['fingerprint'] != base['fingerprint']:
            raise ValueError('Current source changed while validating the initial implementation capture')
        # Check script edits invalidate execution and review. Fresh checks are
        # required by _base; the sealed manifest proves the implementation's
        # source and discovery inputs, after restoring only the old script hashes.
        if source and actual['policy_current']:
            previous = captured_hashes(expand_packet(request['source_packet'])['files'])
            if previous != source['check_inputs'] and fingerprint_with_check_inputs(source, previous) == bindings['fingerprint']:
                return

        # Normal run can capture BEFORE applying edits. Only independently
        # authenticated proof for that exact completed stage can bridge those
        # edits or superseded runtime policy.
        from .workflow import WorkflowController
        history, _ = WorkflowController(self.store)._stage_history(task)
        receipt_hash = hash_value(receipt)
        for entry in history['stages']:
            checks = entry.get('checks', {})
            if (entry['stage_id'] != captured_stage or not entry.get('semantic_approval')
                    or not entry.get('policy_current') or not entry.get('current_identity')
                    or entry.get('contract_hash') != base['contract_hash']
                    or entry.get('proposal_hash') != base['proposal_hash']
                    or checks.get('contract_hash') != base['contract_hash']
                    or checks.get('plan_hash') != base['plan_hash']
                    or checks.get('workspace_id') != base['workspace_id']
                    or checks.get('source_changed_during_checks')
                    or entry['roles']['implementation'].get('receipt_sha256') != receipt_hash):
                continue
            historical = fingerprint_with_check_inputs(source, entry.get('check_inputs'))
            if checks['fingerprint'] == base['fingerprint'] or checks['fingerprint'] == historical:
                return
        raise ValueError('Initial implementation capture is stale')

    def prepare(self, task_id, *, thread_id, expected_revision):
        if self.store.db.in_transaction:
            raise ValueError('Lead prompt intent must commit before native invocation')
        self._owner(task_id, thread_id)
        if not self.configured(task_id):
            raise ValueError('Only configured original root lead can prepare additional critiques')
        with transaction(self.store.db):
            old = self.snapshot(task_id)
            if old['revision'] != expected_revision:
                raise ValueError('Stale lead review revision')
            value = self._fresh(task_id, old)
            if value['inflight']:
                raise ValueError('Existing unsettled lead critique; reconcile its original ticket, never resend')
            base = self._base(task_id)
            if not value['passes']:
                receipt = read_record(self.store.db, 'implementation', task_id)['payload']
                self._require_initial_capture(task_id, base, receipt)
            number = len(value['passes']) + 1
            required = required_passes(self.tasks.snapshot(task_id)['contract'])
            # The last required pass always looks at the whole solution, whatever the depth.
            focus = ('whole-solution-requirements' if number == required
                     else FOCI[number - 1] if number < required else 'corrective-whole-solution')
            ticket = {'id': uuid.uuid4().hex, 'number': number, 'focus': focus,
                'base': base, 'requirements': self.requirements(task_id), 'assessment_version': 2,
                'previous_sha256': value['passes'][-1]['sha256'] if value['passes'] else None,
                'prepared_at': now()}
            ticket['input_basis'] = self.inputs.basis(task_id, ticket['id'], base)
            value['inflight'] = ticket
            saved = write_record(self.store.db, 'lead_review', task_id, value, expected_revision=expected_revision)
        prompt = ('Original main bot: perform additional whole-solution critical pass ' + str(number) + ', focus ' + ticket['focus'] + '. '
            'Re-read original requirements and approved clarifications, reconsider EVERY mandatory obligation and EVERY one of the five risk areas (business, failure, performance, security, tests): each needs a CHECKED row with evidence or a justified NOT_APPLICABLE/GAP row. '
            'Use actual source and executed results, not implementation summaries or PASS counts. Findings may be empty. '
            'Require concrete counterexamples/violations; performance claims need measurements. Do not invent requirements or bugs. '
            'First record this assessment on the inspected snapshot. Then fix only confirmed authorized issues and rerun affected checks/regressions before the next pass. '
            'If input changed before capture, retain the result as historical and reconcile this exact ticket. '
            'Explicitly resolve previous findings; an approval never silently closes inherited findings. '
            'Use an empty origin_anchor_sha256 for this series, or the exact inherited origin from finding_ledger. '
            'A not_a_bug disposition needs counterproof or explicit saved approved supersession; independent AC challenges it. '
            'Follow original stopping/recovery rules. Return the provided assessment schema inside handoff.capture_template. '
            'remaining_gaps contains only material original/approved solution obligations or proof required at THIS critique. '
            'Do not create circular gaps for recording this very response or for independent AC/review/adequacy/publication that follows these passes; mention those pending later obligations in summary. '
            'Do not invent process-proof requirements such as tests-only RED logs unless the original task requires them at this phase. '
            'This prompt and its result grant no authority and do not replace independent AC/review/adequacy.')
        owner = base['owner']
        # One schema-valid proof (_PROOF) bound to a real current check/source pair shows the evidence shape.
        example_check = (base['passing_check_ids'] or base['check_ids'] or ['<check_id from base.check_ids>'])[0]
        example = {'check_id': example_check,
            'source': base['check_sources'].get(example_check, '<path from base.source_paths>'),
            'assertion': '<what this executed check/source shows for this row>', 'kind': 'check'}
        capture_template = {
            'origin': {'backend': owner['backend'], 'thread_id': owner['thread_id'], 'host_id': owner['host_id'],
                'turn_ref': '<reference of the native turn that produced this assessment>'},
            'assessment': {
                'requirements': [{'id': row['id'], 'expected_sha256': row['expected_sha256'], 'status': '<PASS|FAIL|UNVERIFIED>',
                    'evidence': [dict(example)], 'reason': '<why>'} for row in ticket['requirements']['requirements']],
                'risks': [{'area': area, 'status': '<CHECKED|NOT_APPLICABLE|GAP>', 'reason': '<why>', 'evidence': [dict(example)]} for area in AREAS],
                'findings': [], 'resolutions': [], 'remaining_gaps': [], 'summary': '<summary>'}}
        return {**saved, 'handoff': {'attempt_id': ticket['id'], 'destination': base['owner'], 'number': number,
            'focus': ticket['focus'], 'requirements': ticket['requirements'], 'base': base, 'schema': ASSESSMENT_SCHEMA,
            'capture_template': capture_template,
            'finding_ledger': self._context(task_id)['ledger'],
            'prompt': prompt, 'instruction': 'Commit observed native result; after interruption recover this exact attempt without another prompt. '
                '`lead-review record --file` takes capture_template exactly: keep origin as given (set turn_ref), replace every <placeholder>, '
                'keep one requirements row per listed id and one risks row for each of the five areas; evidence items follow handoff.schema. '
                'Each prefilled evidence item is an example: rewrite its assertion, use a matching base.check_ids/check_sources or base.source_paths pair, '
                'add measurement items for performance claims, or drop it where the row needs none.'}}

    def _path(self, task_id, attempt_id):
        if len(attempt_id) != 32 or any(c not in '0123456789abcdef' for c in attempt_id):
            raise ValueError('Invalid lead attempt identity')
        path = logical_path(self.store.root / 'tasks' / identifier(task_id) / 'lead-review' / attempt_id / 'capture.json')
        _safe_state(path)
        return state_io(path)

    def _proofs(self, proofs, base, *, required=True, passing=False):
        if required and not proofs:
            raise ValueError('Requirement/finding needs actual check/source evidence')
        for proof in proofs:
            if proof['check_id'] not in base['check_ids'] or proof['source'] not in base['source_paths']:
                raise ValueError('Evidence references an unknown check or unsupplied source')
            if proof['source'].startswith('_ase_context/') and base.get('check_sources', {}).get(proof['check_id']) != proof['source']:
                raise ValueError('Evidence source belongs to another executed check')
            if passing and proof['check_id'] not in base['passing_check_ids']:
                raise ValueError('PASS requirement needs passing current check evidence')

    def _validate(self, payload, ticket):
        if not isinstance(payload, dict) or set(payload) != {'origin', 'assessment'}:
            raise ValueError('Lead capture requires exact native origin and assessment')
        origin = payload['origin']; owner = ticket['base']['owner']
        if not isinstance(origin, dict) or set(origin) != {'backend', 'thread_id', 'host_id', 'turn_ref'}:
            raise ValueError('Invalid original native lead origin')
        if any(origin[key] != owner[key] for key in ('backend', 'thread_id', 'host_id')):
            raise ValueError('Native assessment belongs to another original lead')
        text(origin['turn_ref'], 'observed native turn reference', limit=2000)
        version = ticket.get('assessment_version', 1)
        if type(version) is not int or version not in {1, 2}:
            raise ValueError('Unsupported lead assessment version')
        schema = ASSESSMENT_SCHEMA if version == 2 else LEGACY_ASSESSMENT_SCHEMA
        assessment = payload['assessment']; validation = validate_payload(assessment, schema)
        if validation['status'] != 'VALID':
            raise ValueError('Invalid closed lead assessment schema: ' + '; '.join(validation['errors'][:5]))
        text(encode(payload), 'lead assessment', limit=300000)
        expected = {r['id']: r['expected_sha256'] for r in ticket['requirements']['requirements']}
        rows = assessment['requirements']
        if len(rows) != len(expected) or {r['id']: r['expected_sha256'] for r in rows} != expected:
            raise ValueError('Assessment must reconcile every exact original/approved requirement')
        for row in rows:
            self._proofs(row['evidence'], ticket['base'], required=row['status'] == 'PASS', passing=row['status'] == 'PASS')
        risks = assessment['risks']
        if len(risks) != len(AREAS) or {r['area'] for r in risks} != set(AREAS):
            raise ValueError('Every one of the five risk areas (business, failure, performance, security, tests) needs exactly one checked or justified applicability row')
        for risk in risks:
            self._proofs(risk['evidence'], ticket['base'], required=risk['status'] == 'CHECKED')
            if risk['area'] == 'performance' and risk['status'] == 'CHECKED' and not any(p['kind'] == 'measurement' for p in risk['evidence']):
                raise ValueError('Performance claims require measurement evidence')
        for field in ('findings', 'resolutions'):
            ids = [(row.get('origin_anchor_sha256', ''), identifier(row['id'], 'lead finding ID')) for row in assessment[field]]
            if len(ids) != len(set(ids)):
                raise ValueError('Duplicate lead finding/resolution IDs')
            for row in assessment[field]:
                self._proofs(row['evidence'], ticket['base'])
                origin = row.get('origin_anchor_sha256', '')
                if origin and (len(origin) != 64 or any(c not in '0123456789abcdef' for c in origin)):
                    raise ValueError('Resolution needs an exact original finding anchor')
                if field == 'findings' and row['requirement_id'] not in expected:
                    raise ValueError('Finding must identify an original approved requirement')

    def _captures(self, value):
        captures = []
        for reference in value['passes']:
            path = logical_path(reference['path'])
            if path != logical_path(self._path(value['task_id'], path.parent.name)):
                raise ValueError('Lead assessment path escapes its original project/task attempt')
            raw = _read(reference['path'], MAX_CAPTURE)
            capture = decode_json(raw)
            if hash_value(capture) != reference['sha256']:
                raise ValueError('Lead assessment capture changed after commitment')
            self._validate(capture['result'], capture['ticket'])
            ticket = capture['ticket']; base = ticket['base']
            if (ticket['id'] != path.parent.name or ticket['number'] != len(captures) + 1
                    or base['project_id'] != self.store.project_id or base['workspace_id'] != self.store.workspace_id
                    or base['task_id'] != value['task_id'] or base['owner'] != self._owner(value['task_id'])
                    or logical_path(base['root']).resolve() != logical_path(self.store.repo).resolve()
                    or base['anchor_sha256'] != value['anchor_sha256']
                    or ticket['requirements']['anchor_sha256'] != value['anchor_sha256']
                    or capture['result']['origin']['turn_ref'] in {c['result']['origin']['turn_ref'] for c in captures}):
                raise ValueError('Lead assessment has a different project/root/anchor/turn boundary')
            if capture['ticket']['previous_sha256'] != (value['passes'][len(captures)-1]['sha256'] if captures else None):
                raise ValueError('Lead assessment sequence differs from its predecessor')
            if reference.get('inputs') is not None:
                self.inputs.load(value['task_id'], capture, reference['inputs'])
            captures.append(capture)
        return captures

    @staticmethod
    def _ledger(captures, *, prior=(), current_anchor=None):
        ledger = {}
        if current_anchor is None and captures:
            current_anchor = captures[0]['ticket']['base']['anchor_sha256']
        ordered = [(item['anchor_sha256'], capture) for item in prior for capture in item['captures']]
        ordered.extend((current_anchor, capture) for capture in captures)
        for anchor, capture in ordered:
            prior_open = {key for key, row in ledger.items() if row['state'] == 'OPEN'}
            assessment = capture['result']['assessment']
            for row in assessment['findings']:
                key = (anchor, row['id'])
                if key in ledger:
                    raise ValueError('Existing finding must be explicitly resolved, not replaced')
                ledger[key] = {**row, 'origin_anchor_sha256': anchor, 'state': 'OPEN',
                    'opened_capture_sha256': hash_value(capture), 'opened_base_sha256': hash_value(capture['ticket']['base']),
                    'opened_identity': {field: capture['ticket']['base'].get(field)
                        for field in ('fingerprint', 'plan_hash', 'action_context_sha256')},
                    'opened_correction_sha256': capture['ticket']['base'].get('correction_sha256')}
            for row in assessment['resolutions']:
                # Immutable v1 responses always mean their own series. A v2
                # response may explicitly select one exact inherited origin.
                origin = row.get('origin_anchor_sha256', '') if capture['ticket'].get('assessment_version', 1) == 2 else ''
                key = (origin or anchor, row['id'])
                if key not in prior_open or ledger[key]['state'] != 'OPEN':
                    raise ValueError('Resolution requires an earlier open finding, not one introduced in this capture')
                base = capture['ticket']['base']; opened = ledger[key]['opened_identity']
                correction = ledger[key]['opened_correction_sha256']
                changed = (base.get('correction_sha256') != correction if correction else
                    any(base.get(field) != opened.get(field) for field in ('fingerprint', 'plan_hash', 'action_context_sha256')))
                if capture['ticket'].get('assessment_version', 1) == 1 and correction is None:
                    # Preserve already-credited legacy local dispositions. New
                    # v2 dispositions cannot treat absent old proof as a change.
                    changed = bool(base.get('correction_sha256'))
                if row['outcome'] == 'fixed' and (not base.get('correction_sha256') or not changed):
                    raise ValueError('A fixed finding requires changed source/check/action evidence, not another claim')
                ledger[key].update(state='RESOLVED', resolution=row, resolution_capture_sha256=hash_value(capture))
        return {(identifier if origin == current_anchor else origin + ':' + identifier): row
                for (origin, identifier), row in ledger.items()}

    @contextmanager
    def _capture_writer(self):
        with transaction(self.store.db):
            # A caller-owned deferred BEGIN makes transaction() use only a
            # SAVEPOINT. Acquire SQLite's writer reservation before any staging
            # file access, without modifying a row or committing caller state.
            self.store.db.execute('UPDATE runtime_records SET revision=revision WHERE 0')
            yield

    def record(self, task_id, attempt_id, payload, *, thread_id, expected_revision):
        # Serialize the response's staging-file writer as well as its SQL
        # projection. Otherwise two deliveries can append the same empty
        # prefix and both destroy recoverability. The response is still fsynced
        # before any credit is committed; a process death rolls back SQL only.
        with self._capture_writer():
            return self._record(task_id, attempt_id, payload,
                thread_id=thread_id, expected_revision=expected_revision)

    def _record(self, task_id, attempt_id, payload, *, thread_id, expected_revision):
        self._owner(task_id, thread_id)
        path = self._path(task_id, attempt_id)
        old = self.snapshot(task_id); value = old['payload']
        if path.exists():
            capture = decode_json(_read(path, MAX_CAPTURE))
            if capture['result'] != payload:
                raise ValueError('Conflicting native result is already sealed')
            projected = read_record(self.store.db, 'lead_review_capture', task_id + ':' + attempt_id)['payload']
            if projected:
                if projected['sha256'] != hash_value(capture):
                    raise ValueError('Conflicting projected lead result')
                if projected.get('inputs') is not None:
                    self.inputs.load(task_id, capture, projected['inputs'])
                return old
        else:
            if old['revision'] != expected_revision or not value or not value['inflight'] or value['inflight']['id'] != attempt_id:
                raise ValueError('Missing/stale original lead ticket; reconcile before record')
            ticket = value['inflight']
            # New input only: captures sealed before this rule are still read back unchanged.
            placeholder = _template_placeholder(payload)
            if placeholder:
                raise ValueError('Replace the capture_template placeholder ' + placeholder + ' with the observed value')
            self._validate(payload, ticket)
            if payload['origin']['turn_ref'] in value['seen_turns']:
                raise ValueError('Native turn was reused for another lead pass')
            # Deterministic bytes let the same already-observed native response
            # finish a partial write. No new timestamp forces a different seal.
            candidate = {'ticket': ticket, 'result': copy.deepcopy(payload)}
            self._context(task_id, value, candidate)
            raw = canonical(candidate)
            if len(raw) > MAX_CAPTURE:
                raise ValueError('Lead capture exceeds bounded private artifact')
            path.parent.mkdir(parents=True, exist_ok=True)
            pending = path.with_suffix('.json.pending'); _safe_state(logical_path(pending))
            prefix = _read(pending, MAX_CAPTURE) if pending.exists() else b''
            if not raw.startswith(prefix):
                raise ValueError('Unknown lead capture staging content; preserved')
            flags = os.O_WRONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0)
            flags |= os.O_APPEND if pending.exists() else os.O_CREAT | os.O_EXCL
            with os.fdopen(os.open(pending, flags, 0o600), 'wb') as stream:
                info = os.fstat(stream.fileno())
                if info.st_nlink != 1:
                    raise ValueError('Unsafe linked lead capture')
                stream.write(raw[len(prefix):]); stream.flush(); os.fsync(stream.fileno())
            if _read(pending, MAX_CAPTURE) != raw or path.exists():
                raise ValueError('Lead capture changed before seal')
            os.replace(pending, path)
            capture = candidate
        return self._commit_capture(task_id, capture, path)

    def _commit_capture(self, task_id, capture, path):
        with transaction(self.store.db):
            old = self.snapshot(task_id); value = copy.deepcopy(old['payload'])
            ticket = capture['ticket']
            projected = read_record(self.store.db, 'lead_review_capture', task_id + ':' + ticket['id'])
            if projected['payload']:
                if projected['payload']['sha256'] != hash_value(capture):
                    raise ValueError('Conflicting projected lead capture')
                return old
            if not value or not value['inflight'] or value['inflight'] != ticket:
                raise ValueError('Capture differs from its original in-flight ticket')
            self._validate(capture['result'], ticket)
            reference = {'path': str(logical_path(path)), 'sha256': hash_value(capture)}
            try:
                current = self._base(task_id)
                if ticket.get('assessment_version', 1) == 1:
                    current = {key: value for key, value in current.items() if key != 'prior_lineage_sha256'}
                credited = current == ticket['base']
            except (ValueError, OSError, KeyError, TypeError):
                credited = False
            value['seen_turns'].append(capture['result']['origin']['turn_ref'])
            if credited:
                self._context(task_id, value, capture)
                reference['inputs'] = self.inputs.seal(task_id, capture)
                value['passes'].append(reference)
            else:
                value['last_stale'] = reference
            value['inflight'] = None
            write_record(self.store.db, 'lead_review_capture', task_id + ':' + ticket['id'],
                {**reference, 'credited': credited}, expected_revision=0)
            return write_record(self.store.db, 'lead_review', task_id, value, expected_revision=old['revision'])

    def recover(self, task_id):
        # Recovery must not rename a pending capture while record is writing it.
        with self._capture_writer():
            return self._recover(task_id)

    def _recover(self, task_id):
        row = self.snapshot(task_id); value = row['payload']
        if value and value['inflight']:
            path = self._path(task_id, value['inflight']['id'])
            pending = path.with_suffix('.json.pending')
            if not path.exists() and pending.exists():
                try:
                    capture = decode_json(_read(pending, MAX_CAPTURE))
                except (ValueError, UnicodeError):
                    return row  # Incomplete original bytes stay; never relaunch.
                if capture.get('ticket') != value['inflight']:
                    raise ValueError('Pending lead capture differs from its original ticket')
                self._validate(capture['result'], capture['ticket'])
                os.replace(pending, path)
            if path.exists():
                return self._commit_capture(task_id, decode_json(_read(path, MAX_CAPTURE)), path)
        return row

    def binding(self, task_id, stage_id=None):
        if stage_id is not None or not self.configured(task_id):
            return None
        row = self.snapshot(task_id); context = self._context(task_id)
        selected = [*context['evidence'], *([context['active']] if context['active'] else [])]
        captures = [capture for item in selected for capture in item['captures']]
        references = [reference for item in selected for reference in item['record']['passes']]
        inputs = self.inputs.binding(task_id, captures, references)
        return hash_value({'anchor': context['requirements']['anchor_sha256'], 'record': row['payload'],
            'prior_lineage_sha256': context['prior_sha256'], 'inputs': inputs})

    def assessment(self, task_id, *, current_files=()):
        """Bounded semantic input; omit repeated source inventories, retain provenance."""
        if not self.configured(task_id):
            return {'configured': False, 'authority': 'No final-root lead policy applies to this saved task/child'}
        row = self.snapshot(task_id); value = row['payload']; context = self._context(task_id)
        selected = [*context['evidence'], *([context['active']] if context['active'] else [])]
        captures = [capture for item in selected for capture in item['captures']]
        references = [reference for item in selected for reference in item['record']['passes']]
        reports, input_files, input_missing = self.inputs.packet(task_id, captures, references, current_files=current_files)
        summaries, inherited = [], []
        for capture, input_report in zip(captures, reports):
            ticket = capture['ticket']
            summary = {'number': ticket['number'], 'focus': ticket['focus'],
                'origin_anchor_sha256': ticket['base']['anchor_sha256'],
                'base': {k: v for k, v in ticket['base'].items() if k not in {'source_paths', 'check_ids', 'source_hashes'}},
                'origin': capture['result']['origin'], 'assessment': capture['result']['assessment'],
                'capture_sha256': hash_value(capture), 'inputs': input_report}
            (summaries if ticket['base']['anchor_sha256'] == context['requirements']['anchor_sha256'] else inherited).append(summary)
        classification = copy.deepcopy(context['active']['record']['classification']) if context['active'] else None
        prior_classification = None
        if (classification and classification['kind'] == 'trivial'
                and (value['passes'] or value['inflight'])):
            prior_classification, classification = classification, None
        if classification:
            classification['base'] = {k: v for k, v in classification['base'].items() if k not in {'source_paths', 'check_ids', 'source_hashes'}}
        if prior_classification:
            prior_classification['base'] = {k: v for k, v in prior_classification['base'].items() if k not in {'source_paths', 'check_ids', 'source_hashes'}}
        gate = self.gate(task_id)
        if gate.get('classification', {}).get('base'):
            gate['classification'] = copy.deepcopy(gate['classification'])
            gate['classification']['base'] = {k: v for k, v in gate['classification']['base'].items()
                if k not in {'source_paths', 'check_ids', 'source_hashes'}}
        return {'configured': True, 'requirements': self.requirements(task_id),
            'classification': classification or {'kind': 'nontrivial', 'reason': 'Default when complexity is not mechanically justified'},
            'prior_classification': prior_classification,
            'passes': summaries, 'inherited_passes': inherited, 'finding_ledger': context['ledger'], 'gate': gate,
            'prior_lineage_sha256': context['prior_sha256'],
            'input_files': input_files, 'input_coverage_missing': input_missing,
            'binding_sha256': self.binding(task_id),
            'authority': 'Original native lead critiques/host attestations, not independent semantic approval. All current source/check obligations and independent roles remain required.'}

    def gate(self, task_id, *, stage_id=None):
        task = self.tasks.snapshot(task_id)
        result = {'configured': 'lead_review' in task['contract'], 'status': 'BLOCKED',
            'completed_passes': 0, 'required_passes': 3, 'blocked_reasons': [], 'rejected_reasons': [],
            'base_current': False,
            'next_action': 'Resolve the reported source or check blocker before preparing the next native critique'}
        if 'lead_review' not in task['contract']:
            return {**result, 'status': 'NOT_CONFIGURED', 'next_action': None}
        required = required_passes(task['contract'])
        result['required_passes'] = required
        if stage_id is not None or task['contract'].get('parent_task_id'):
            return {**result, 'status': 'NOT_REQUIRED', 'next_action': None}
        coordinator = read_record(self.store.db, 'coordinator', task_id)
        if coordinator['payload'] is None:
            return {**result, 'needs_owner_binding': True,
                'blocked_reasons': ['Original main chat must be bound before native critiques'],
                'next_action': bind_local_action(task_id, coordinator['revision'])}
        try:
            base = self._base(task_id); value = self.snapshot(task_id)['payload']
            result['base_current'] = True
            if not value or value['anchor_sha256'] != base['anchor_sha256']:
                raise ValueError(f'{required} additional original-lead critique(s) pending for the approved requirements')
            context = self._context(task_id); captures = context['captures']; ledger = context['ledger']
            result['completed_passes'] = len(captures)
            result['finding_ledger'] = ledger
            if value['inflight']:
                raise ValueError('Reconcile the existing native lead critique; never resend its prompt')
            open_findings = [row for row in ledger.values() if row['state'] == 'OPEN' and row['priority'] in {'P0', 'P1', 'P2'}]
            if open_findings:
                raise ValueError('Unresolved material original-lead findings: ' + ', '.join(row['id'] for row in open_findings))
            classification = value['classification']
            if classification and classification['kind'] == 'trivial' and classification['base'] == base and not captures:
                self._eligible_trivial(task_id)
                return {**result, 'status': 'READY', 'classification': classification,
                    'requires_independent_disposition': True, 'next_action': 'Independent AC must challenge the mechanical exemption'}
            if len(captures) < required:
                raise ValueError(f'{required} additional original-lead critique(s) have not completed')
            if captures[-1]['ticket']['base'] != base:
                raise ValueError('Final lead critique is stale; keep earlier depth and perform a current follow-up')
            selected = [*context['evidence'], context['active']]
            proof_captures = [capture for item in selected for capture in item['captures']]
            proof_references = [reference for item in selected for reference in item['record']['passes']]
            _, _, input_missing = self.inputs.packet(task_id, proof_captures, proof_references)
            if input_missing:
                raise ValueError('Historical lead inputs are incomplete: ' + '; '.join(input_missing[:3]))
            assessment = captures[-1]['result']['assessment']
            if (any(r['status'] != 'PASS' for r in assessment['requirements']) or assessment['remaining_gaps']
                    or any(r['status'] == 'GAP' for r in assessment['risks'])):
                raise ValueError('Final lead critique retains required requirement/evidence gaps')
            effective_classification = (classification if classification and classification['kind'] != 'trivial'
                else {'kind': 'nontrivial', 'reason': 'Started substantive critiques supersede an earlier mechanical exemption'})
            binding = self.binding(task_id)
            verified = all(((read_record(self.store.db, 'verification', task_id + ':' + role)['payload'] or {})
                .get('bindings') or {}).get('lead_review_sha256') == binding for role in ('ac', 'review'))
            from .tasks import finish_command
            return {**result, 'status': 'READY', 'classification': effective_classification,
                'finding_ledger': ledger, 'requires_independent_disposition': True, 'verification_bound': verified,
                'next_action': ('None for the lead review: independent AC/review are bound to this original-lead evidence; '
                    'once the task gate is APPROVED: ' + finish_command(task_id)) if verified
                    else 'Refresh independent verification against the captured original-lead evidence: `ase task verify '
                        + task_id + '`'}
        except (ValueError, OSError, KeyError, TypeError) as error:
            result['blocked_reasons'].append(str(error))
            receipt = read_record(self.store.db, 'implementation', task_id)['payload']
            if isinstance(error, _StaleLeadChecks) and receipt:
                result['next_action'] = ('Refresh current execution evidence with `ase task check ' + task_id
                    + '`, then inspect `ase lead-review status ' + task_id + '`.')
            if result['base_current'] or not receipt:
                try:
                    result['next_action'] = self._pending_action(task, coordinator['payload'], receipt,
                        base if result['base_current'] else None)
                except (ValueError, OSError, KeyError, TypeError) as capture_error:
                    result['blocked_reasons'].append(str(capture_error))
                    result['initial_capture_blocked'] = True
                    result['next_action'] = ('Restore valid implementation or completed-stage evidence for the current source '
                        'before starting lead review. Record a framework blocker if the current approved workflow cannot supply it.')
            return result

    def _pending_action(self, task, coordinator, receipt, base):
        """The concrete command that advances a pending lead review of a bound root task."""
        from .tasks import run_command
        task_id = task['task_id']
        if not receipt:
            return (run_command(self.store, task) + ' first: it records the implementation receipt and current checks '
                    'that original-lead critiques start from; no owner question is needed')
        record = self.snapshot(task_id)
        value = record['payload'] or {}
        flags = (' --thread-id ' + coordinator['destination']['thread_id']
                 + ' --expected-revision ' + str(record['revision']))
        inflight = value.get('inflight')
        if inflight:
            return ('Original lead records the native result of the open critique ticket (never resend its prompt): '
                    '`ase lead-review record ' + task_id + flags + ' --attempt ' + inflight['id']
                    + ' --file <capture.json>`, or `ase lead-review recover ' + task_id
                    + '` after an interruption; no owner question is needed')
        if value.get('anchor_sha256') != base['anchor_sha256'] or not value.get('passes'):
            self._require_initial_capture(task_id, base, receipt)
        return ('Original lead prepares the next native critique: `ase lead-review prepare ' + task_id + flags
                + '`, then records its result with `ase lead-review record`; no owner question is needed')
