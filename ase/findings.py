"""Owner dispositions of independent review findings.

A blocking finding normally needs a fix and a fresh review. The owner may instead waive a P2
or disprove any finding. A disproof cites either a captured `prompt:<id>` recorded after the
review ran (verified) or, where prompt-capture hooks are not allowed (for example a workplace
that forbids agent hooks), the chat reference plus the owner's exact words (owner-asserted, not
verified); a P2 waiver needs the same. The gate JSON keeps the redacted excerpt or quote; the PR
body names the reference and whether it is verified. Neither is a separate human identity. A
captured prompt must postdate the review, so a decision made before a re-review is restated.
Dispositions match by finding content and the current contract, so an identical finding that a
later review repeats stays disposed, while a changed finding or contract needs a new decision.
"""
import hashlib
import copy
from pathlib import Path

from .protocol import canonical, decode_json, sha256
from .state import append_event, hash_value, read_record, write_record, transaction, text

KIND = 'finding_disposition'
DISPOSITIONS = {'waive', 'disprove'}
WAIVABLE = {'P2', 'P3'}
MATERIAL = {'P0', 'P1', 'P2'}
JOURNAL = 'review_findings'
SOURCE_CHANGES = '_ase_context/source_changes.json'


def finding_id(finding):
    body = {name: finding.get(name) for name in ('severity', 'path', 'line', 'message')}
    return hashlib.sha256(canonical(body)).hexdigest()[:16]


def substantive_review(payload):
    return (isinstance(payload, dict) and isinstance(payload.get('findings'), list)
            and isinstance(payload.get('summary'), str) and bool(payload['summary'].strip()))


def reconcile_review(rows, payload, receipt_sha256, source_sha256, presented, source_paths, part_id, *, deleted=()):
    """Project an independent result while retaining older findings and their closure evidence."""
    current = copy.deepcopy(rows)
    reported = set()
    by_content = {}
    for key, row in presented.items():
        by_content.setdefault(finding_id(row['finding']), []).append(key)
    for finding in payload['findings']:
        if finding['severity'] not in MATERIAL:
            continue
        related = finding.get('same_finding_id')
        if related and (related not in presented or related not in current):
            raise ValueError('A repeated finding must name a finding presented to this reviewer')
        issue = {name: value for name, value in finding.items() if name != 'same_finding_id'}
        content_id = finding_id(issue)
        exact = by_content.get(content_id, ())
        key = related or (exact[0] if len(exact) == 1 else None)
        if key is None:
            key = content_id if part_id is None else hash_value({'part_id': part_id, 'finding_id': content_id})[:16]
        if key in reported:
            raise ValueError('Review reported the same finding more than once')
        reported.add(key)
        row = current.setdefault(key, {'id': key, 'part_id': part_id, 'opened_in': receipt_sha256})
        if part_id is not None:
            row['part_id'] = part_id
        if row.get('status') != 'OPEN':
            row['open_source_sha256'] = source_sha256
        row.update(status='OPEN', finding=issue, last_reported_in=receipt_sha256)

    seen = set()
    for resolution in payload.get('finding_followup', []):
        key, outcome = resolution['id'], resolution['outcome']
        prior = presented.get(key) or {}
        if key in seen or prior.get('status') != 'OPEN':
            raise ValueError('Follow-up requires a distinct earlier open finding from this review scope')
        seen.add(key)
        row = current[key]
        if outcome in ('STILL_OPEN', 'UNVERIFIED'):
            continue
        if key in reported:
            raise ValueError('A review cannot report and close the same finding')
        if row['last_reported_in'] != prior['last_reported_in'] or row['status'] != 'OPEN':
            continue  # A sibling re-reported or resolved it while these reviews were in flight.
        removal = resolution['source'] == SOURCE_CHANGES and outcome == 'FIXED' and row['finding']['path'] in deleted
        if resolution['source'] not in source_paths and not removal:
            raise ValueError('Finding closure requires a source or check log from the current packet')
        text(resolution['evidence'], 'finding closure evidence', limit=4000)
        if outcome not in ('FIXED', 'DISPROVED'):
            raise ValueError('Invalid finding follow-up outcome')
        if outcome == 'FIXED' and row.get('open_source_sha256') == source_sha256:
            raise ValueError('A fixed finding requires different source evidence or an explicit disproof')
        row.update(status='RESOLVED', resolution={**resolution, 'receipt_sha256': receipt_sha256,
                                                   'source_sha256': source_sha256})
    return current


def review_followup_schema(schema, history):
    """Add explicit follow-up only when the host supplies finding history."""
    if not history:
        return schema
    schema = copy.deepcopy(schema)
    item = schema['properties']['findings']['items']
    item['properties']['same_finding_id'] = {'type': 'string', 'enum': ['', *history]}
    item['required'].append('same_finding_id')
    opened = [key for key, row in history.items() if row['status'] == 'OPEN']
    if opened:
        fields = {'id': {'type': 'string', 'enum': opened},
                  'outcome': {'type': 'string', 'enum': ['FIXED', 'DISPROVED', 'STILL_OPEN', 'UNVERIFIED']},
                  'source': {'type': 'string'},
                  'evidence': {'type': 'string', 'minLength': 1, 'maxLength': 4000}}
        schema['properties']['finding_followup'] = {'type': 'array', 'maxItems': len(opened),
            'items': {'type': 'object', 'properties': fields, 'required': list(fields), 'additionalProperties': False}}
        schema['required'].append('finding_followup')
    return schema


def _excerpt(text, limit=300):
    """Credential-redacted tail of the owner prompt (decisions tend to come last); local JSON only."""
    from .core import redact
    text = redact(text)
    return text if len(text) <= limit else '…' + text[-limit:]


def applies(finding, disposition):
    """A waiver never covers P0/P1; a disproof covers any severity with a captured prompt or the owner's quote."""
    if not disposition:
        return False
    if disposition['disposition'] == 'disprove':
        return disposition.get('verified') is True or bool(disposition.get('owner_quote'))
    return finding.get('severity') in WAIVABLE  # waivers recorded before quotes were required stay valid


def disposition_result(finding, disposition):
    return {'id': finding_id(finding), 'severity': finding['severity'], 'message': finding.get('message'),
            'disposition': disposition['disposition'], 'user_message_ref': disposition['user_message_ref'],
            'verified': bool(disposition.get('verified')), 'prompt_excerpt': disposition.get('prompt_excerpt'),
            'owner_quote': disposition.get('owner_quote')}


def _effective_row(row, dispositions):
    disposition = dispositions.get(finding_id(row['finding']))
    if row['part_id'] is None and row['status'] == 'OPEN' and applies(row['finding'], disposition):
        return {**row, 'status': 'OWNER_DISPOSED', 'disposition': disposition['disposition']}
    return row


def _review_evidence(files, action_context):
    from .evidence.check_inputs import captured_hashes

    sources = {file['path']: file['sha256'] for file in files if not file['path'].startswith('_ase_')}
    paths = {*sources, *(file['path'] for file in files
                         if file['path'].startswith(('_ase_diff/', '_ase_context/logs/', '_ase_check_inputs/')))}
    manifest = next((decode_json(file['content']) for file in files if file['path'] == SOURCE_CHANGES), None)
    deleted = set()
    for change in (manifest or {}).get('changes', []):
        path, after = change['path'], change['after_sha256']
        sources[path] = after
        if after is None and change.get('before_sha256'):
            deleted.add(path)
    basis = hash_value({'files': sources, 'actions': action_context, 'check_inputs': captured_hashes(files)})
    return basis, paths, deleted


def capture_in_use(store, task_id):
    """True when the task's request or plan approval cites a captured prompt, so capture works for this task."""
    from .tasks import TaskService, current_approval_decisions
    task = TaskService(store).snapshot(task_id)
    contract = task.get('contract') or {}
    request = next((s for s in contract.get('sources', []) if isinstance(s, dict) and s.get('id') == 'REQUEST'), {})
    refs = [request.get('locator', ''), *(decision['user_message_ref'] for decision in current_approval_decisions(store.db, task))]
    return any(str(ref).strip().startswith('prompt:') for ref in refs)


class Findings:
    def __init__(self, store):
        self.store = store

    def _receipt(self, task_id):
        record = read_record(self.store.db, 'verification', task_id + ':review')['payload']
        if not record:
            raise ValueError('No independent review receipt for this task')
        return record

    def _review(self, task_id, receipt=None):
        from .runner import verify_receipt
        payload = verify_receipt(receipt or self._receipt(task_id), require_current_policy=False)['result']['payload']
        return [row for row in payload.get('findings', []) if isinstance(row, dict)]

    def current(self, task_id):
        """Dispositions bound to the current contract, keyed by finding id."""
        from .tasks import TaskService
        contract_hash = TaskService(self.store).snapshot(task_id)['contract_hash']
        payload = read_record(self.store.db, KIND, task_id)['payload'] or {}
        return {key: value for key, value in payload.items() if value.get('contract_hash') == contract_hash}

    def list(self, task_id):
        dispositions = self.current(task_id)
        history = [_effective_row(row, dispositions) for row in self.journal(task_id)]
        rows = []
        receipt = read_record(self.store.db, 'verification', task_id + ':review')['payload']
        latest_error = {}
        try:
            latest = self._review(task_id, receipt) if receipt else []
        except (ValueError, OSError, KeyError, TypeError) as error:
            from .core import redact
            latest, latest_error = [], {'latest_review_error': redact(str(error))[:1000]}
        for finding in latest:
            key = finding_id(finding)
            disposition = dispositions.get(key)
            rows.append({'id': key, **finding, 'blocking': finding.get('severity') in {'P0', 'P1', 'P2'}
                         and not applies(finding, disposition), 'disposition': disposition})
        return {'task_id': task_id, 'findings': rows, 'journal': history, **latest_error}

    @staticmethod
    def _key(task_id, stage_id):
        return task_id + ':' + (stage_id or '')

    def journal(self, task_id):
        prefix = task_id + ':'
        records = self.store.db.execute(
            "SELECT key,payload FROM runtime_records WHERE kind=? AND substr(key,1,?)=? ORDER BY key",
            (JOURNAL, len(prefix), prefix))
        return [{**row, 'stage_id': key[len(prefix):] or None}
                for key, raw in records for row in decode_json(raw).values()]

    def review_obligations(self, task, stage_id, dispositions):
        stages = {stage_id} if stage_id is not None else {None, *(stage['id'] for stage in task['contract']['stages'])}
        opened, disposed = [], []
        for row in self.journal(task['task_id']):
            if row['stage_id'] not in stages or row['status'] != 'OPEN':
                continue
            finding = row['finding']
            decision = dispositions.get(finding_id(finding))
            if row['part_id'] is None and applies(finding, decision):
                disposed.append(disposition_result(finding, decision))
            else:
                opened.append(row)
        return opened, disposed

    def for_review(self, task, stage_id, part_id=None):
        from .evidence.representation import owners

        stored = read_record(self.store.db, JOURNAL, self._key(task['task_id'], stage_id))['payload'] or {}
        dispositions = self.current(task['task_id'])
        selected = {}
        for key, row in stored.items():
            covering = owners(task['contract'], row['finding']['path'])
            if part_id == (covering[0] if len(covering) == 1 else None):
                selected[key] = _effective_row(row, dispositions)
        closed = [key for key, row in selected.items() if row['status'] != 'OPEN']
        return {key: row for key, row in selected.items() if row['status'] == 'OPEN' or key in closed[-40:]}

    def seed_current_review(self, task, stage_id, part_id):
        """Keep the last independent finding from older installations when it is still available."""
        from .evidence.representation import part_key
        task_id = task['task_id']
        kind, key = (('verification', task_id + ':review') if part_id is None
                     else ('verification_part', part_key(task_id, stage_id, part_id)))
        previous = read_record(self.store.db, kind, key)['payload']
        if previous:
            try:
                self.record_review(task, stage_id, part_id, previous)
            except (ValueError, OSError, KeyError, TypeError):
                # A legacy latest receipt may belong to another stage, lack
                # authentic independent proof, or be unavailable.
                pass

    def record_review(self, task, stage_id, part_id, receipt, *, presented=None, expected_request=None, check_current=None):
        """Accept only a sealed independent review in the selected task and stage boundary."""
        if receipt.get('status') != 'COMPLETE' or receipt.get('evidence_kind') != 'independent_cli':
            return
        from .runner import _read, verify_receipt
        from .evidence.compact import expand_packet
        from .evidence.representation import part_hash, parts
        from .native_paths import logical_path

        task_id = task['task_id']
        event = self._event_key(task_id, receipt)
        if self.store.db.execute('SELECT 1 FROM runtime_events WHERE event_key=?', (event,)).fetchone():
            return
        root = logical_path(receipt['artifact_root']).resolve()
        if not root.is_relative_to(logical_path(self.store.root / 'tasks' / task_id / 'sessions').resolve()):
            raise ValueError('Review journal receipt belongs to another task')
        actual = verify_receipt(receipt, expected_bindings=expected_request['bindings'] if expected_request else None,
                                require_current_policy=expected_request is not None)
        identity = actual['request_identity']
        if (identity['role'] != 'review' or identity['purpose'] is not None or identity['stage_id'] != stage_id
                or identity['project_id'] != self.store.project_id
                or receipt['bindings'].get('workspace_id') != self.store.workspace_id):
            raise ValueError('Review journal receipt belongs to another review scope')
        part = next((part for part in parts(task['contract']) if part['id'] == part_id), None)
        if receipt['bindings'].get('review_part_sha256') != (part_hash(part) if part else None):
            raise ValueError('Review journal receipt belongs to another review part')
        raw = _read(receipt['artifacts']['request']['path'], 8388608)
        if sha256(raw) != receipt['request_sha256']:
            raise ValueError('Review journal request changed after verification')
        request = decode_json(raw)
        if logical_path(request['project']['root']).resolve() != logical_path(self.store.repo).resolve():
            raise ValueError('Review journal request belongs to another project root')
        if expected_request:
            supplied = request['handoff'].get('finding_history', [])
            def identities(rows):
                return [(row['id'], row['status'], row['last_reported_in']) for row in rows]
            if (actual['request_identity']['schema_sha256'] != sha256(canonical(expected_request['schema']))
                    or identities(supplied) != identities((presented or {}).values())):
                raise ValueError('Sealed review was prompted with a different finding history or schema')
        payload = actual['result']['payload']
        if not actual['result'].get('session_id') or not substantive_review(payload):
            raise ValueError('Review journal requires a substantive independent result')
        if not payload['findings'] and not payload.get('finding_followup'):
            return
        files = expand_packet(request['source_packet'])['files']
        review_basis, paths, deleted = _review_evidence(files, receipt['bindings'].get('verification_context_sha256'))
        key = self._key(task_id, stage_id)
        receipt_hash = hash_value(receipt)
        with transaction(self.store.db) as db:
            if db.execute('SELECT 1 FROM runtime_events WHERE event_key=?', (event,)).fetchone():
                return
            if check_current is not None and not check_current():
                raise ValueError('Review source, approval or executed checks changed while the review was in flight')
            old = read_record(db, JOURNAL, key)
            updated = reconcile_review(old['payload'] or {}, payload, receipt_hash, review_basis,
                                       presented or {}, paths, part_id, deleted=deleted)
            write_record(db, JOURNAL, key, updated, expected_revision=old['revision'])
            append_event(db, event, 'review_findings', task_id, {'stage_id': stage_id, 'part_id': part_id,
                         'receipt_sha256': receipt_hash, 'artifact_root': str(root)})

    @staticmethod
    def _event_key(task_id, receipt):
        return 'review-findings:' + task_id + ':' + receipt['execution_id']

    def withhold(self, task_id, stage_id, part_id, receipt, reason):
        """Keep a captured result visible without allowing it to seed the journal on the next pass."""
        append_event(self.store.db, self._event_key(task_id, receipt), 'review_findings', task_id,
                     {'stage_id': stage_id, 'part_id': part_id, 'receipt_sha256': hash_value(receipt), 'withheld': reason})

    def projection_error(self, task_id, stage_id, receipt):
        if not receipt or not receipt.get('execution_id'):
            return None
        result = self.store.db.execute("SELECT payload FROM runtime_events WHERE event_key=?",
                                      (self._event_key(task_id, receipt),)).fetchone()
        if result:
            saved = decode_json(result[0])
            if stage_id is not None and saved.get('stage_id') not in (None, stage_id):
                return None
            if saved.get('receipt_sha256') == hash_value(receipt):
                return saved.get('withheld')

    def _disposition_target(self, task_id, key):
        matches = [row for row in self.journal(task_id) if row['status'] == 'OPEN'
                   and (key == row['id'] or key == finding_id(row['finding']))]
        eligible = [row for row in matches if row['part_id'] is None]
        if eligible:
            findings = {finding_id(row['finding']) for row in eligible}
            if len(findings) != 1:
                raise ValueError('Finding ID is ambiguous across stages; use the current finding content ID')
            from .runner import _read
            events = self.store.db.execute(
                "SELECT payload FROM runtime_events WHERE task_id=? AND kind='review_findings' "
                "AND json_extract(payload,'$.receipt_sha256') IN (" + ','.join('?' for _ in eligible) + ")",
                (task_id, *(row['last_reported_in'] for row in eligible)))
            receipts = []
            for (raw,) in events:
                event = decode_json(raw)
                receipt = decode_json(_read(Path(event['artifact_root']) / 'receipt.json', 8388608))
                if hash_value(receipt) != event['receipt_sha256'] or not any(
                        finding_id(finding) in findings for finding in self._review(task_id, receipt)):
                    raise ValueError('Journal finding no longer matches its sealed review')
                receipts.append(receipt)
            if not receipts:
                raise ValueError('Journal finding has no authenticated last review')
            return eligible[0]['finding'], max(receipts, key=lambda row: row.get('ended_at_utc') or ''), eligible
        if matches:
            raise ValueError('Split review findings require an independent correction or disproof')
        receipt = self._receipt(task_id)
        finding = next((row for row in self._review(task_id, receipt) if finding_id(row) == key), None)
        if finding is None:
            raise ValueError('Unknown finding id for the current review receipt')
        return finding, receipt, []

    def dispose(self, task_id, key, disposition, *, reason, user_message_ref, owner_quote=None):
        if disposition not in DISPOSITIONS:
            raise ValueError('Disposition must be waive or disprove')
        text(reason, 'reason', limit=4000)
        text(user_message_ref, 'user_message_ref', limit=2000)
        from .tasks import TaskService
        from .state import now
        tasks = TaskService(self.store)
        contract_hash = tasks.snapshot(task_id)['contract_hash']
        finding, receipt, journal_rows = self._disposition_target(task_id, key)
        if disposition == 'waive' and finding.get('severity') not in WAIVABLE:
            raise ValueError('P0/P1 findings cannot be waived; fix them or disprove them with evidence')
        from .prompts import resolve
        captured = resolve(self.store, user_message_ref)
        if owner_quote is not None:
            text(owner_quote, 'owner_quote', limit=2000)
        if captured is None and not (owner_quote or '').strip():
            raise ValueError('An owner disposition needs the owner prompt:<id> from `ase prompt list` '
                             'or, without prompt capture, --owner-quote with the owner\'s exact words')
        if captured is None and finding.get('severity') in {'P0', 'P1'} and capture_in_use(self.store, task_id):
            raise ValueError('This task uses prompt capture: a P0/P1 disproof needs the owner prompt:<id> from `ase prompt list`, '
                             'not a quote')
        if captured is not None and captured['captured'] < (receipt.get('ended_at_utc') or ''):
            raise ValueError('The cited owner prompt predates this review; cite the owner decision made after it')
        entry = {'disposition': disposition, 'severity': finding.get('severity'), 'message': finding.get('message'),
                 'reason': reason, 'user_message_ref': user_message_ref, 'verified': captured is not None,
                 'owner_asserted': captured is None,
                 # Capture proves when the owner wrote, not what it covers; the excerpt lets readers check.
                 'prompt_excerpt': _excerpt(captured['text']) if captured else None,
                 'owner_quote': _excerpt(owner_quote) if owner_quote and captured is None else None,
                 'contract_hash': contract_hash, 'recorded': now()}
        with transaction(self.store.db) as db:
            latest = self.journal(task_id) if journal_rows else []
            if (tasks.snapshot(task_id)['contract_hash'] != contract_hash
                    or (journal_rows and any(row not in latest for row in journal_rows))
                    or (not journal_rows and self._receipt(task_id) != receipt)):
                raise ValueError('Finding or contract changed before recording the owner decision')
            old = read_record(db, KIND, task_id)
            payload = dict(old['payload'] or {})
            payload[finding_id(finding)] = entry
            write_record(db, KIND, task_id, payload, expected_revision=old['revision'])
        return {'id': key, **entry}
