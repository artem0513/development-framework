"""Blind review pass: an independent session that sees the change, not its author's intent.

The ordinary review receives the approved proposal, contract, stage history and lead
critiques, and tends to read the code through that intent. The blind pass gets only the
changed source, its base versions, the change index and actual check logs. Its blocking
findings block the gate like a review part's: they are fixed, or a later independent pass
no longer reports them. Opt in with `blind_review: {"version": 1}`; new CLI tasks set it.
"""
import copy
import json
from pathlib import Path

from ..profiles import has_software

POLICY = {'version': 1}
PURPOSE = 'blind_review'
LABEL = 'review_blind'
# Context the blind pass keeps: what changed and what actually ran. Everything else in
# _ase_context (contract, proposal, plan, stage history, discovery, lead critiques,
# adequacy prose) carries the author's intent or earlier verdicts.
KEPT_CONTEXT = ('_ase_context/source_changes.json', '_ase_context/changed_files.json',
                '_ase_context/change_impact.json', '_ase_context/omitted_sources.json',
                '_ase_context/actual_checks.json', '_ase_context/review_examples.md')
KEPT_PREFIXES = ('_ase_context/logs/', '_ase_context/baseline_logs/')
WITHHELD = ('Blind pass: the request, approved proposal, implementation notes and earlier verdicts are withheld. '
            'Judge the change against the base versions and the surrounding project code only.')
INSTRUCTION = ('Blind review pass. You get the changed source, its base versions (_ase_before/ or _ase_diff/), the change '
               'index and actual check logs, but not the request, proposal, notes or earlier verdicts. Run the mechanism '
               'inventory and the reader pass on every changed method and new mechanism, comparing each with the base '
               'version and the local precedent around it. Report actionable defects P0-P3; P0/P1/P2 block. Necessity and '
               'scope are judged by the separate scope pass: do not report them, and leave unverified_material empty '
               'unless correctness itself cannot be established from the supplied code.')
CONFLICT_FIELD = 'blind_requirement_conflicts'
QUOTE_LIMIT = 1000  # A quoted requirement sentence, not a document.
CONFLICT_INSTRUCTION = (' handoff.blind_findings lists blocking findings of the independent blind pass, which did not see '
    'the request. For each one whose flagged behaviour the request, an acceptance criterion or a recorded owner decision '
    'explicitly requires, add an entry to ' + CONFLICT_FIELD + ' with its finding_id and the exact requirement text '
    'quoted verbatim. Do not list a finding you merely disagree with or find minor; it stays blocking.')


def validate(value):
    if value != POLICY:
        raise ValueError('blind_review must be {"version": 1}')
    return value


def enabled(contract):
    return contract.get('blind_review') == POLICY and has_software(contract)


def key(task_id, stage_id):
    return task_id + ':' + LABEL + ((':' + stage_id) if stage_id else '')


def blind_packet(packet):
    """The same bounded packet without intent-bearing context."""
    files = [item for item in packet['files'] if not item['path'].startswith('_ase_')
             or item['path'].startswith(('_ase_before/', '_ase_diff/'))
             or item['path'] in KEPT_CONTEXT or item['path'].startswith(KEPT_PREFIXES)]
    coverage = {**packet['coverage'], 'included': [item['path'] for item in files], 'blind': WITHHELD}
    return {**packet, 'requirements': [WITHHELD], 'files': files, 'coverage': coverage}


def blocking_findings(payload):
    """The blind pass's P0-P2 findings with their content IDs, as the scope pass receives them."""
    from ..findings import finding_id
    findings = payload.get('findings') if isinstance(payload, dict) else None
    return [{'finding_id': finding_id(item), **item} for item in findings or []
            if isinstance(item, dict) and item.get('severity') in {'P0', 'P1', 'P2'}]


def conflict_schema(schema, finding_ids):
    """The scope review schema plus the reconciliation of the listed blind findings."""
    schema = copy.deepcopy(schema)
    fields = {'finding_id': {'type': 'string', 'enum': list(finding_ids)},
              'requirement_quote': {'type': 'string', 'minLength': 1, 'maxLength': QUOTE_LIMIT},
              'reason': {'type': 'string', 'minLength': 1, 'maxLength': QUOTE_LIMIT}}
    schema['properties'][CONFLICT_FIELD] = {'type': 'array', 'maxItems': len(finding_ids), 'items': {
        'type': 'object', 'properties': fields, 'required': list(fields), 'additionalProperties': False}}
    schema['required'].append(CONFLICT_FIELD)
    return schema


def _normal(text):
    return ' '.join(str(text).split()).casefold()


def requirement_text(contract):
    """What a quote may come from: the request, acceptance criteria and recorded owner decisions."""
    parts = [contract.get('request', ''), *(row.get('expected', '') for row in contract.get('acceptance', []))]
    parts += [row.get('resolution', '') for row in contract.get('unknowns', [])
              if row.get('status') in {'RESOLVED', 'CONFIRMED'}]
    return _normal('\n'.join(part for part in parts if isinstance(part, str)))


def dismissed(store, contract, task_id, blind_receipt):
    """{finding_id: quote} of blind findings that the current scope review shows the requirement explicitly asks for."""
    from ..runner import verify_receipt
    from ..state import hash_value, read_record
    record = read_record(store.db, 'verification', task_id + ':review')['payload']
    if not record or not blind_receipt:
        return {}
    try:
        payload = verify_receipt(record)['result'].get('payload') or {}
        request = json.loads(Path(record['artifacts']['request']['path']).read_text(encoding='utf-8'))
    except (ValueError, OSError, KeyError, TypeError):
        return {}
    # The reconciliation answers exactly this blind receipt; a newer blind pass needs a newer scope pass.
    if request.get('handoff', {}).get('blind_receipt_sha256') != hash_value(blind_receipt):
        return {}
    text = requirement_text(contract)
    return {row['finding_id']: row['requirement_quote'] for row in payload.get(CONFLICT_FIELD) or []
            if isinstance(row, dict) and isinstance(row.get('requirement_quote'), str)
            and _normal(row['requirement_quote']) and _normal(row['requirement_quote']) in text}


def blind_gate(store, contract, task_id, stage_id, current):
    """(blocked, rejected, dismissed notes) for the blind pass of this gate."""
    if not enabled(contract):
        return [], [], []
    from ..evidence.representation import known_sessions, part_key, parts, review_session_verdict
    from ..state import read_record
    executions, sessions = known_sessions(store, task_id, stage_id, current, extra=[
        ('verification_part', part_key(task_id, stage_id, part['id'])) for part in parts(contract)])
    receipt = read_record(store.db, 'verification_blind', key(task_id, stage_id))['payload']
    quotes = dismissed(store, contract, task_id, receipt)
    blocked, rejected = review_session_verdict(store, contract, current, receipt, 'Blind review', executions, sessions,
                                               {}, dismissed=frozenset(quotes))
    notes = [f'Blind review finding {finding} contradicts the requirement "{quote[:300]}"' for finding, quote in quotes.items()]
    return blocked, rejected, notes
