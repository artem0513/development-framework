"""Immutable historical inputs for original-lead critiques.

These bytes establish local provenance/transport. Neither native assertions nor
archived protocol fixtures constitute independent semantic approval.
"""
import copy
import difflib
import json
import os
import re
import stat

from .native_paths import logical_path, state_io
from .protocol import canonical, decode_json, sha256
from .runner import verify_receipt
from .source_coverage import source_bytes
from .state import hash_value, identifier, read_record, text

MAX_SOURCE = 512000
MAX_TOTAL = 8 * 1024 * 1024
MAX_MANIFEST = 1024 * 1024
MAX_REFS = 1000
MAX_JSON_DEPTH = 128
MAX_DELTA_LINES = 8192


def restore_line_delta(value, base, expected_sha256):
    """Restore one bounded, same-packet source; never follow another delta."""
    keys = {'encoding', 'base_packet_path', 'base_sha256', 'source_sha256', 'edits'}
    normalized = isinstance(value, dict) and value.get('encoding') == 'line_delta_v2'
    if normalized:
        keys.add('base_newlines')
    if (not isinstance(base, dict) or not isinstance(base.get('path'), str)
            or not isinstance(base.get('content'), str) or not isinstance(base.get('sha256'), str)
            or not isinstance(value, dict) or set(value) != keys or value['encoding'] not in {'line_delta_v1', 'line_delta_v2'}
            or (normalized and value['base_newlines'] != 'crlf_to_lf')
            or len(canonical(value)) > MAX_SOURCE or value['base_packet_path'] != base['path']):
        raise ValueError('Invalid historical line delta')
    raw = base['content'].encode('utf-8')
    if (len(raw) > MAX_SOURCE or sha256(raw) != base['sha256'] or base['sha256'] != value['base_sha256']
            or _digest(value['source_sha256']) != _digest(expected_sha256)):
        raise ValueError('Historical delta source identity differs')
    content = base['content'].replace('\r\n', '\n') if normalized else base['content']
    lines = content.splitlines(keepends=True)
    edits = value['edits']
    if len(lines) > MAX_DELTA_LINES or not isinstance(edits, list) or not (0 if normalized else 1) <= len(edits) <= MAX_REFS:
        raise ValueError('Historical delta exceeds bounded line edits')
    pieces, cursor = [], 0
    for edit in edits:
        if (not isinstance(edit, dict) or set(edit) != {'start', 'until', 'content'}
                or type(edit['start']) is not int or type(edit['until']) is not int
                or not cursor <= edit['start'] <= edit['until'] <= len(lines)
                or not isinstance(edit['content'], str)):
            raise ValueError('Unsafe historical delta range')
        pieces.extend(lines[cursor:edit['start']]); pieces.append(edit['content']); cursor = edit['until']
    pieces.extend(lines[cursor:])
    restored = ''.join(pieces).encode('utf-8')
    if len(restored) > MAX_SOURCE or sha256(restored) != expected_sha256:
        raise ValueError('Historical delta does not restore exact bounded bytes')
    return restored


def _line_delta(raw, base):
    if len(raw) < 4096 or len(raw) > MAX_SOURCE:
        return None
    before = base['content'].splitlines(keepends=True)
    after = raw.decode('utf-8').splitlines(keepends=True)
    if len(before) > MAX_DELTA_LINES or len(after) > MAX_DELTA_LINES:
        return None
    def candidate(lines, normalized):
        edits = [{'start': i, 'until': end, 'content': ''.join(after[j:stop])}
                 for kind, i, end, j, stop in difflib.SequenceMatcher(None, lines, after).get_opcodes() if kind != 'equal']
        value = {'encoding': 'line_delta_v2' if normalized else 'line_delta_v1', 'base_packet_path': base['path'], 'base_sha256': base['sha256'],
                 'source_sha256': sha256(raw), 'edits': edits}
        if normalized: value['base_newlines'] = 'crlf_to_lf'
        return value
    value = candidate(before, False)
    if '\r\n' in base['content']:
        other = candidate(base['content'].replace('\r\n', '\n').splitlines(keepends=True), True)
        if len(canonical(other)) < len(canonical(value)): value = other
    edits = value['edits']
    encoded = canonical(value)
    if (not edits and value['encoding'] == 'line_delta_v1') or len(edits) > MAX_REFS or len(encoded) > MAX_SOURCE or len(encoded) * 4 >= len(raw) * 3:
        return None
    if restore_line_delta(value, base, sha256(raw)) != raw:
        raise ValueError('Historical delta reconstruction differs')
    return value


class _ObjectPairs(list):
    """Preserve duplicate keys without running guards during JSON parsing."""


def _digest(value):
    if not isinstance(value, str) or not re.fullmatch('[0-9a-f]{64}', value):
        raise ValueError('Invalid historical input hash')
    return value


def _body(raw):
    if len(raw) > MAX_SOURCE:
        raise ValueError('Historical input exceeds per-source bound')
    content = raw.decode('utf-8', errors='strict')
    # text rejects empty strings, which are legitimate whole input bodies.
    # Prefixing keeps its credential/NUL guard without changing archived bytes.
    text('historical input\n' + content, 'historical input', limit=MAX_SOURCE + 20)
    # Establish whole-body JSON syntax before judging structured fields: an
    # object prefix in ordinary code/documentation is still plain source.
    # Keep duplicate pairs so a later null cannot hide an earlier secret.
    from .lead_review import validate_intent_text
    try:
        parsed = json.loads(content, object_pairs_hook=_ObjectPairs)
    except json.JSONDecodeError:
        return content  # Plain/empty/whitespace bodies are legitimate inputs.
    except RecursionError:
        raise ValueError('Historical input JSON nesting exceeds validation bound') from None
    pending = [(parsed, 0, None)]
    try:
        while pending:
            node, depth, field = pending.pop()
            if depth > MAX_JSON_DEPTH:
                raise ValueError('Historical input JSON nesting exceeds validation bound')
            if isinstance(node, _ObjectPairs):
                for key, child in node:
                    pending.append((child, depth + 1, key))
            elif isinstance(node, list):
                pending.extend((child, depth + 1, field) for child in node)
            elif isinstance(node, str):
                # Credential values are text, including arrays of text. A
                # schema declaration/container or a boolean flag is not one;
                # still traverse its descendants instead of exempting a tree.
                validate_intent_text({field: node} if field is not None else node,
                    'historical input', limit=MAX_TOTAL)
    except RecursionError:
        raise ValueError('Historical input JSON nesting exceeds validation bound') from None
    return content


def _read(path, limit):
    physical = state_io(path)
    flags = os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0)
    with os.fdopen(os.open(physical, flags), 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > limit:
            raise ValueError('Unsafe or oversized historical input artifact')
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise ValueError('Historical input artifact exceeds bound')
    return raw


def _seal(path, raw, limit):
    """Resume only an exact deterministic prefix; never overwrite a sealed file."""
    path = logical_path(path)
    physical = state_io(path)
    if len(raw) > limit:
        raise ValueError('Historical input artifact exceeds bound')
    if physical.exists():
        if _read(path, limit) != raw:
            raise ValueError('Conflicting immutable historical input artifact')
        return {'path': str(path), 'sha256': sha256(raw), 'bytes': len(raw)}
    state_io(path.parent).mkdir(parents=True, exist_ok=True)
    pending = path.with_name(path.name + '.pending')
    staging = state_io(pending)
    prefix = _read(pending, limit) if staging.exists() else b''
    if not raw.startswith(prefix):
        raise ValueError('Unknown historical input staging bytes; preserved')
    flags = os.O_WRONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0)
    flags |= os.O_APPEND if staging.exists() else os.O_CREAT | os.O_EXCL
    with os.fdopen(os.open(staging, flags, 0o600), 'wb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError('Unsafe linked historical input staging file')
        stream.write(raw[len(prefix):]); stream.flush(); os.fsync(stream.fileno())
    if _read(pending, limit) != raw or state_io(path).exists():
        raise ValueError('Historical input changed before seal')
    # Publish the deterministic staging bytes atomically, matching lead captures.
    # The local same-user trust boundary does not claim hostile-writer isolation.
    os.replace(staging, physical)
    if os.name != 'nt':
        descriptor = os.open(path.parent, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
        try: os.fsync(descriptor)
        finally: os.close(descriptor)
    return {'path': str(path), 'sha256': sha256(raw), 'bytes': len(raw)}


class LeadInputs:
    def __init__(self, store):
        self.store = store

    def _directory(self, task_id, attempt_id):
        identifier(task_id)
        if not isinstance(attempt_id, str) or not re.fullmatch('[0-9a-f]{32}', attempt_id):
            raise ValueError('Invalid lead input attempt identity')
        path = logical_path(self.store.root / 'tasks' / task_id / 'lead-review' / attempt_id)
        state_io(path)
        return path

    def _identity(self, task_id, attempt_id, base):
        if (base.get('project_id') != self.store.project_id
                or base.get('workspace_id') != self.store.workspace_id
                or base.get('task_id') != task_id
                or logical_path(base['root']) != logical_path(self.store.repo)):
            raise ValueError('Historical input has a foreign project/workspace/task/root')
        if not isinstance(base.get('owner'), dict) or not base['owner']:
            raise ValueError('Historical input lacks original lead ownership')
        return {'project_id': self.store.project_id, 'workspace_id': self.store.workspace_id,
                'root': str(logical_path(self.store.repo)), 'task_id': task_id,
                'attempt_id': attempt_id, 'owner': copy.deepcopy(base['owner']),
                'base_sha256': hash_value(base)}

    @staticmethod
    def _checks(base, checks):
        if not isinstance(checks, dict) or hash_value(checks) != base['check_hash']:
            raise ValueError('Historical check receipts differ from prepared checks')
        _body(canonical(checks))  # Also protect the earlier prepared-basis copy.
        if (checks.get('task_id') != base['task_id']
                or checks.get('workspace_id') != base['workspace_id']
                or checks.get('fingerprint') != base['fingerprint']
                or checks.get('contract_hash') != base['contract_hash']
                or checks.get('plan_hash') != base['plan_hash']):
            raise ValueError('Historical check receipt bindings differ from prepared base')
        rows = checks.get('results', [])
        if not isinstance(rows, list) or len(rows) > MAX_REFS:
            raise ValueError('Historical check receipts exceed bound')
        by_id = {row['id']: row for row in rows}
        if len(by_id) != len(rows) or not set(base['check_ids']).issubset(by_id):
            raise ValueError('Missing or duplicate prepared historical checks')
        if set(base.get('check_sources', {})) != set(base['check_ids']):
            raise ValueError('Historical check source inventory differs from prepared checks')
        for check_id, path in base['check_sources'].items():
            row = by_id[check_id]
            expected = '_ase_context/logs/' + check_id + '.txt' if row.get('log') else '_ase_context/actual_checks.json'
            if path != expected or path not in base['source_paths']:
                raise ValueError('Historical check source mapping is invalid')
            if row.get('log'):
                _digest(row.get('log_sha256'))
        return by_id

    def basis(self, task_id, attempt_id, base):
        directory = self._directory(task_id, attempt_id)
        identity = self._identity(task_id, attempt_id, base)
        hashes = base.get('source_hashes')
        if not isinstance(hashes, dict) or len(hashes) > MAX_REFS:
            raise ValueError('Prepared source hashes are missing or unbounded')
        for path, digest in hashes.items():
            if not isinstance(path, str) or path not in base['source_paths']:
                raise ValueError('Prepared source hash has an unknown source')
            _digest(digest.removesuffix(':x') if isinstance(digest, str) else digest)
        checks = read_record(self.store.db, 'checks', task_id)['payload']
        self._checks(base, checks)
        value = {'version': 1, **identity, 'source_hashes': copy.deepcopy(hashes),
                 'checks': copy.deepcopy(checks)}
        reference = _seal(directory / 'basis.json', canonical(value), MAX_MANIFEST)
        return {key: reference[key] for key in ('path', 'sha256')}

    def _owned_json(self, reference, expected):
        if not isinstance(reference, dict) or set(reference) - {'path', 'sha256', 'bytes'} or not {'path', 'sha256'} <= reference.keys():
            raise ValueError('Malformed historical input reference')
        if logical_path(reference['path']) != logical_path(expected):
            raise ValueError('Historical input reference escapes its original attempt')
        raw = _read(expected, MAX_MANIFEST)
        if sha256(raw) != _digest(reference['sha256']):
            raise ValueError('Historical input manifest changed after commitment')
        if 'bytes' in reference and reference['bytes'] != len(raw):
            raise ValueError('Historical input manifest length differs')
        value = decode_json(raw)
        if not isinstance(value, dict):
            raise ValueError('Historical input manifest must be an object')
        return value

    def _basis(self, task_id, capture):
        ticket = capture['ticket']; base = ticket['base']
        directory = self._directory(task_id, ticket['id'])
        expected = self._identity(task_id, ticket['id'], base)
        value = self._owned_json(ticket['input_basis'], directory / 'basis.json')
        if value.get('version') != 1 or any(value.get(key) != item for key, item in expected.items()):
            raise ValueError('Historical input basis differs from original capture')
        if value.get('source_hashes') != base.get('source_hashes'):
            raise ValueError('Historical source hashes differ from prepared basis')
        self._checks(base, value['checks'])
        return value

    def _required(self, capture, checks):
        base = capture['ticket']['base']; by_id = self._checks(base, checks)
        sources, check_ids, pairs = set(), set(), set()
        assessment = capture['result']['assessment']
        for field in ('requirements', 'risks', 'findings', 'resolutions'):
            for row in assessment[field]:
                for proof in row['evidence']:
                    source, check_id = proof['source'], proof['check_id']
                    pairs.add((source, check_id))
                    if len(pairs) > MAX_REFS:
                        raise ValueError('Historical input references exceed bound')
                    if source not in base['source_paths'] or check_id not in base['check_ids']:
                        raise ValueError('Historical proof refers to an unprepared input')
                    if source.startswith('_ase_context/') and base['check_sources'].get(check_id) != source:
                        raise ValueError('Historical check source belongs to another check')
                    sources.add(source); check_ids.add(check_id)
        # A production source and check ID pair also needs the actual check log,
        # even if the model did not separately cite the virtual log file.
        sources.update(base['check_sources'][check_id] for check_id in check_ids)
        if len(sources) > MAX_REFS:
            raise ValueError('Historical source inventory exceeds bound')
        return sorted(sources), sorted(check_ids), by_id

    def _blob(self, directory, raw):
        _body(raw)
        return _seal(directory / ('input-' + sha256(raw) + '.txt'), raw, MAX_SOURCE)

    def seal(self, task_id, capture):
        ticket = capture['ticket']; base = ticket['base']
        directory = self._directory(task_id, ticket['id'])
        basis = self._basis(task_id, capture)
        path = directory / 'inputs.json'
        if state_io(path).exists():
            raw = _read(path, MAX_MANIFEST)
            reference = {'path': str(path), 'sha256': sha256(raw)}
            self.load(task_id, capture, reference)
            return reference
        checks = basis['checks']; wanted, check_ids, by_id = self._required(capture, checks)
        checks_raw = canonical(checks)
        if len(checks_raw) > MAX_SOURCE:
            raise ValueError('Full historical checks exceed per-source bound')
        bodies = {'_ase_context/actual_checks.json': checks_raw}
        task_root = logical_path(self.store.root / 'tasks' / identifier(task_id))
        for source in wanted:
            if source == '_ase_context/actual_checks.json':
                continue
            if source.startswith('_ase_context/logs/'):
                check_id = next((key for key in check_ids if base['check_sources'][key] == source), None)
                if check_id is None:
                    raise ValueError('Historical log has no prepared check')
                receipt = by_id[check_id]; log = logical_path(receipt['log'])
                if not log.is_relative_to(task_root):
                    raise ValueError('Historical log escapes its original task state')
                raw = _read(log, MAX_SOURCE)
                if sha256(raw) != receipt['log_sha256']:
                    raise ValueError('Historical log changed after preparation')
            else:
                expected = basis['source_hashes'].get(source)
                if expected is None:
                    raise ValueError('Historical source lacks a prepared raw hash')
                raw = source_bytes(self.store.repo, source)
                if sha256(raw) != expected.removesuffix(':x'):
                    raise ValueError('Historical source changed after preparation')
            bodies[source] = raw
        if sum(len(raw) for raw in {sha256(raw): raw for raw in bodies.values()}.values()) > MAX_TOTAL:
            raise ValueError('Historical input set exceeds total bound')
        # Validate ALL bodies before copying any, to avoid partial secret copies.
        for raw in bodies.values(): _body(raw)
        refs = {source: self._blob(directory, raw) for source, raw in sorted(bodies.items())}
        value = {'version': 1, **self._identity(task_id, ticket['id'], base),
                 'capture_sha256': hash_value(capture), 'basis': copy.deepcopy(ticket['input_basis']),
                 'sources': {source: refs[source] for source in wanted},
                 'checks': refs['_ase_context/actual_checks.json'], 'check_ids': check_ids,
                 'provenance': {'kind': 'snapshot', 'check_sha256': base['check_hash']}}
        reference = _seal(path, canonical(value), MAX_MANIFEST)
        result = {key: reference[key] for key in ('path', 'sha256')}
        self.load(task_id, capture, result)
        return result

    def _load_blob(self, directory, reference):
        if not isinstance(reference, dict) or set(reference) != {'path', 'sha256', 'bytes'}:
            raise ValueError('Malformed historical input blob reference')
        digest = _digest(reference['sha256'])
        path = directory / ('input-' + digest + '.txt')
        if logical_path(reference['path']) != path:
            raise ValueError('Historical input blob escapes its original attempt')
        raw = _read(path, MAX_SOURCE)
        if sha256(raw) != digest or type(reference['bytes']) is not int or len(raw) != reference['bytes']:
            raise ValueError('Historical input blob differs from its committed bytes')
        _body(raw)
        return raw

    def load(self, task_id, capture, reference_or_none):
        ticket = capture['ticket']; base = ticket['base']
        directory = self._directory(task_id, ticket['id'])
        identity = self._identity(task_id, ticket['id'], base)
        if reference_or_none is None:
            if ticket.get('input_basis'):
                self._basis(task_id, capture)
                missing = self._missing('Historical input snapshot reference is absent')
                missing['provenance'] = {'kind': 'snapshot', 'check_sha256': base['check_hash']}
                return missing
            return self._legacy(task_id, capture)
        basis = self._basis(task_id, capture)
        value = self._owned_json(reference_or_none, directory / 'inputs.json')
        if (value.get('version') != 1 or any(value.get(key) != item for key, item in identity.items())
                or value.get('capture_sha256') != hash_value(capture)
                or value.get('basis') != ticket['input_basis']
                or value.get('provenance') != {'kind': 'snapshot', 'check_sha256': base['check_hash']}):
            raise ValueError('Historical input snapshot has a foreign capture/basis boundary')
        wanted, check_ids, by_id = self._required(capture, basis['checks'])
        if not isinstance(value.get('sources'), dict) or set(value['sources']) != set(wanted) or value.get('check_ids') != check_ids:
            raise ValueError('Historical snapshot drops or adds original proof inputs')
        checks_raw = self._load_blob(directory, value['checks'])
        if checks_raw != canonical(basis['checks']):
            raise ValueError('Historical checks differ from prepared full receipts')
        bodies = {}
        for source, reference in value['sources'].items():
            raw = self._load_blob(directory, reference)
            if source == '_ase_context/actual_checks.json':
                if raw != checks_raw: raise ValueError('Historical virtual checks differ')
            elif source.startswith('_ase_context/logs/'):
                expected = {by_id[key]['log_sha256'] for key in check_ids if base['check_sources'][key] == source}
                if expected != {sha256(raw)}: raise ValueError('Historical log differs from prepared receipt')
            elif basis['source_hashes'].get(source, '').removesuffix(':x') != sha256(raw):
                raise ValueError('Historical source differs from prepared raw hash')
            bodies[source] = raw
        if sum(len(raw) for raw in {sha256(raw): raw for raw in [*bodies.values(), checks_raw]}.values()) > MAX_TOTAL:
            raise ValueError('Historical input set exceeds total bound')
        return {'status': 'AVAILABLE', 'sources': copy.deepcopy(value['sources']),
                'checks': copy.deepcopy(value['checks']), 'provenance': copy.deepcopy(value['provenance']),
                'manifest': copy.deepcopy(reference_or_none), 'missing': [],
                '_bodies': bodies, '_checks_body': checks_raw}

    @staticmethod
    def _missing(reason):
        return {'status': 'MISSING', 'sources': {}, 'checks': None,
                'provenance': {'kind': 'archived_runner_request'}, 'missing': [reason],
                '_bodies': {}, '_checks_body': None}

    @staticmethod
    def _archive_files(request):
        from .evidence.compact import expand_packet
        files = {}
        for row in expand_packet(request['source_packet'])['files']:
            raw = row['content'].encode('utf-8')
            if sha256(raw) != row['sha256'] or row['path'] in files:
                raise ValueError('Archived source packet has duplicate or changed bodies')
            files[row['path']] = raw
        # CompactRunner can deliver an exact logical body once in policy.files.
        # Recover raw bytes only when its sealed mapping supplies the raw hash;
        # LF/CRLF are candidate encodings, never inferred authority.
        policy = {row['path']: row for row in request.get('handoff', {}).get('runtime_policy', {}).get('files', [])}
        mappings = request['source_packet'].get('coverage', {}).get('source_bodies_in_runtime_policy', [])
        if not isinstance(mappings, list) or len(mappings) > MAX_REFS:
            raise ValueError('Archived source mapping exceeds bound')
        for row in mappings:
            path = row['path']; original = policy[path]
            if path in files or sha256(original['content'].encode()) != row['policy_sha256'] or original['sha256'] != row['policy_sha256']:
                raise ValueError('Archived source mapping differs from sealed policy body')
            digest = _digest(row['source_sha256'])
            content = original['content']
            candidates = [content.encode(), content.replace('\r\n', '\n').replace('\n', '\r\n').encode()]
            raw = next((candidate for candidate in candidates if sha256(candidate) == digest), None)
            if raw is None:
                raise ValueError('Archived source raw bytes cannot be reconstructed exactly')
            files[path] = raw
        return files

    def _legacy(self, task_id, capture):
        base = capture['ticket']['base']
        expected = {'fingerprint': base['fingerprint'], 'check_sha256': base['check_hash'],
                    'contract_sha256': base['contract_hash'], 'proposal_sha256': base['proposal_hash'],
                    'plan_sha256': base['plan_hash'], 'workspace_id': base['workspace_id']}
        sessions = logical_path(self.store.root / 'tasks' / identifier(task_id) / 'sessions')
        physical = state_io(sessions)
        if not physical.is_dir():
            return self._missing('No exact authenticated archived Runner inputs are available')
        # Bound names, receipt bytes inspected, and candidate requests verified.
        entries = []
        with os.scandir(physical) as stream:
            for entry in stream:
                if len(entries) >= MAX_REFS:
                    return self._missing('Archived Runner lookup exceeds bounded session inventory')
                entries.append(entry.name)
        scanned, candidates = 0, 0
        for name in sorted(entries):
            if not re.fullmatch('[0-9a-f]{32}', name): continue
            root = sessions / name
            try:
                raw = _read(root / 'receipt.json', MAX_MANIFEST)
                scanned += len(raw)
                if scanned > MAX_TOTAL:
                    return self._missing('Archived Runner lookup exceeds bounded receipt inventory')
                receipt = decode_json(raw)
                bindings = receipt.get('bindings', {})
                if any(bindings.get(key) != item for key, item in expected.items()): continue
                candidates += 1
                if candidates > 32:
                    return self._missing('Archived Runner lookup exceeds bounded exact candidates')
                if logical_path(receipt['artifact_root']) != root or receipt['execution_id'] != name:
                    continue
                actual = verify_receipt(receipt, require_current_policy=False)
                request_ref = receipt['artifacts']['request']
                if logical_path(request_ref['path']) != root / 'request.json': continue
                request_raw = _read(request_ref['path'], MAX_TOTAL)
                if sha256(request_raw) != receipt['request_sha256']: continue
                request = decode_json(request_raw); project = request['project']
                if (project.get('project_id') != self.store.project_id
                        or logical_path(project['root']) != logical_path(self.store.repo)
                        or any(request['bindings'].get(key) != item for key, item in expected.items())):
                    continue
                files = self._archive_files(request)
                checks_raw = files['_ase_context/actual_checks.json']
                checks = decode_json(checks_raw)
                wanted, check_ids, by_id = self._required(capture, checks)
                bodies = {source: files[source] for source in wanted}
                for source, body in bodies.items():
                    _body(body)
                    if source.startswith('_ase_context/logs/'):
                        hashes = {by_id[key]['log_sha256'] for key in check_ids if base['check_sources'][key] == source}
                        if hashes != {sha256(body)}:
                            raise ValueError('Archived check log differs from exact original receipts')
                    elif source == '_ase_context/actual_checks.json':
                        if body != checks_raw: raise ValueError('Archived full checks differ')
                    elif base.get('source_hashes') and base['source_hashes'].get(source, '').removesuffix(':x') != sha256(body):
                        raise ValueError('Archived source differs from prepared source hash')
                _body(checks_raw)
                if sum(len(body) for body in {sha256(body): body for body in [*bodies.values(), checks_raw]}.values()) > MAX_TOTAL:
                    raise ValueError('Archived historical input set exceeds total bound')
                # A request reference points to transport containing these bytes;
                # packet export uses the verified bodies, never today's files.
                sources = {source: {'path': request_ref['path'], 'sha256': sha256(body), 'bytes': len(body)}
                           for source, body in bodies.items()}
                result = actual['result']
                verdict = {key: result[key] for key in ('status', 'blocker') if key in result}
                payload = result.get('payload')
                if isinstance(payload, dict):
                    verdict['payload_verdict'] = {key: payload[key] for key in ('status', 'verdict')
                                                  if key in payload and isinstance(payload[key], (str, bool, type(None)))}
                provenance = {'kind': 'archived_runner_request', 'check_sha256': base['check_hash'],
                              'execution_id': name, 'request_sha256': receipt['request_sha256'],
                              'receipt_sha256': sha256(raw), 'evidence_kind': actual['evidence_kind'],
                              'original_result': verdict}
                return {'status': 'AVAILABLE', 'sources': sources,
                        'checks': {'path': request_ref['path'], 'sha256': sha256(checks_raw), 'bytes': len(checks_raw)},
                        'provenance': provenance, 'missing': [], '_bodies': bodies, '_checks_body': checks_raw}
            except (OSError, ValueError, KeyError, TypeError, UnicodeError):
                # Missing/invalid legacy archives are explicit coverage gaps.
                # A PRESENT snapshot reference above is never swallowed here.
                continue
        return self._missing('No exact authenticated archived Runner inputs are available')

    def packet(self, task_id, captures, references, current_files=()):
        if len(captures) != len(references):
            raise ValueError('Historical capture/reference sequence differs')
        by_hash, exported, reports, missing = {}, {}, [], []
        current_files = list(current_files)
        current_by_path = {row['path']: row for row in current_files}
        for row in current_files:
            raw = row['content'].encode('utf-8')
            digest = sha256(raw)
            if digest != row['sha256']:
                raise ValueError('Current packet source content differs from declared hash')
            by_hash.setdefault(digest, {'sha256': digest, 'packet_path': row['path']})

        def packet_ref(reference, raw, source=None):
            digest = sha256(raw)
            if digest != reference['sha256']:
                raise ValueError('Historical packet body differs from verified reference')
            if digest not in by_hash:
                delta = _line_delta(raw, current_by_path[source]) if source in current_by_path else None
                if delta is not None:
                    content = canonical(delta).decode('utf-8'); representation_hash = sha256(content.encode())
                    path = '_ase_lead_history/' + digest + '.delta.json'
                    by_hash[digest] = {'sha256': digest, 'packet_path': path,
                        'encoding': delta['encoding'], 'representation_sha256': representation_hash}
                    exported[digest] = {'path': path, 'content': content, 'sha256': representation_hash}
                else:
                    path = '_ase_lead_history/' + digest + '.txt'
                    by_hash[digest] = {'sha256': digest, 'packet_path': path}
                    exported[digest] = {'path': path, 'content': raw.decode('utf-8'), 'sha256': digest}
            return copy.deepcopy(by_hash[digest])

        for capture, reference in zip(captures, references):
            loaded = self.load(task_id, capture, reference.get('inputs'))
            report = {key: copy.deepcopy(loaded[key]) for key in ('status', 'provenance', 'missing')}
            report['sources'] = {source: packet_ref(ref, loaded['_bodies'][source], source)
                                 for source, ref in loaded['sources'].items()}
            report['checks'] = packet_ref(loaded['checks'], loaded['_checks_body']) if loaded['checks'] else None
            reports.append(report)
            missing.extend('Original-lead pass ' + str(capture['ticket']['number']) + ': ' + reason for reason in loaded['missing'])
        return reports, list(exported.values()), missing

    def binding(self, task_id, captures, references):
        if len(captures) != len(references):
            raise ValueError('Historical capture/reference sequence differs')
        result = []
        for capture, reference in zip(captures, references):
            loaded = self.load(task_id, capture, reference.get('inputs'))
            result.append({'capture_sha256': hash_value(capture),
                           'status': loaded['status'], 'provenance': loaded['provenance'],
                           'manifest': loaded.get('manifest'), 'missing': loaded['missing'],
                           'sources': {source: ref['sha256'] for source, ref in loaded['sources'].items()},
                           'checks_sha256': loaded['checks']['sha256'] if loaded['checks'] else None})
        return result
