"""Blind review pass: an independent session that sees the change, not its author's intent.

The ordinary review receives the approved proposal, contract, stage history and lead
critiques, and tends to read the code through that intent. The blind pass gets only the
changed source, its base versions, the change index and actual check logs. Its blocking
findings block the gate like a review part's: they are fixed, or a later independent pass
no longer reports them. Opt in with `blind_review: {"version": 1}`; new CLI tasks set it.
"""
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


def blind_gate(store, contract, task_id, stage_id, current):
    """(blocked, rejected) for the blind pass of this gate."""
    if not enabled(contract):
        return [], []
    from ..evidence.representation import known_sessions, part_key, parts, review_session_verdict
    from ..state import read_record
    executions, sessions = known_sessions(store, task_id, stage_id, current, extra=[
        ('verification_part', part_key(task_id, stage_id, part['id'])) for part in parts(contract)])
    receipt = read_record(store.db, 'verification_blind', key(task_id, stage_id))['payload']
    return review_session_verdict(store, contract, current, receipt, 'Blind review', executions, sessions, {})
