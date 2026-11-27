"""Local document lifecycle demonstration; no synthetic semantic approvals.

Without an observed approval reference, prepares discovery/proposal and closes
awaiting approval. An approved draft produces an actual local artifact/check,
but the parent stays blocked on publication and independent review. Optional
--live-codex runs genuine read-only AC/review processes through Runner.
"""
import argparse
from contextlib import closing
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sys
import tempfile

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ase.discovery import discover_capabilities, record_discovery, validate_intake
from ase.gates import GateService
from ase.journal import Journal
from ase.runner import Runner
from ase.store import Store, now
from ase.tasks import TaskService
from ase.workflow import WorkflowController, apply_edits


def draft_contract():
    return {'id': 'document-demo', 'schema_version': 2, 'profile': 'document',
            'request': 'Prepare a provisional guide from brief.md; hold publication until its destination is confirmed.',
            'scope': {'resources': ['guide.md'], 'effects': ['file_write']},
            'acceptance': [{'id': 'AC1', 'expected': 'The provisional guide contains the named brief and identifies pending publication.',
                            'mandatory': True, 'evidence_ids': ['guide']}],
            'evidence': [{'id': 'guide', 'kind': 'artifact', 'path': 'guide.md', 'required': True}],
            'stages': [{'id': 'draft', 'acceptance_ids': ['AC1']},
                       {'id': 'publish', 'scope': {'resources': ['guide.md'], 'effects': []},
                        'acceptance_ids': [], 'depends_on': ['draft'], 'unknown_ids': ['destination']}],
            'unknowns': [{'id': 'destination', 'statement': 'Publication destination is not confirmed.',
                          'blocking_stage_ids': ['publish'], 'status': 'UNRESOLVED'}],
            'sources': [{'id': 'brief', 'reference': 'local:brief.md'}],
            'reporting': {'timezone': 'Europe/Bucharest'}}


def run_demo(workspace, state_home, *, approval_user_message_ref=None, live_codex=None):
    """Run only in a selected workspace with an existing brief.md source."""
    workspace = Path(workspace).resolve(strict=True)
    state_home = Path(state_home).resolve()
    if not workspace.is_dir():
        raise ValueError('Existing demonstration workspace required')
    source = workspace / 'brief.md'
    if not source.is_file() or source.is_symlink() or not source.resolve().is_relative_to(workspace):
        raise ValueError('Selected workspace requires a regular local brief.md source')
    if (workspace / 'guide.md').exists():
        raise ValueError('Demo will not overwrite an existing guide.md; select a fresh disposable workspace')
    if approval_user_message_ref is not None and not str(approval_user_message_ref).strip():
        raise ValueError('Approval reference must identify the observed user authorization')
    if live_codex is not None:
        if not approval_user_message_ref:
            raise ValueError('--live-codex requires --approval-user-message-ref for the exact draft/review scope')
        if not Path(live_codex).is_absolute():
            raise ValueError('--live-codex must identify an absolute executable')
    with closing(Store(workspace, state_home)) as store:
        tasks = TaskService(store)
        task = tasks.create(draft_contract())
        intake = validate_intake({
            'source_refs': [{'id': 'brief', 'kind': 'document', 'reference': 'local:brief.md', 'observed_at': now()}],
            'outcomes': [{'id': 'guide', 'text': 'Prepare a provisional operating guide', 'source_refs': ['brief']}],
            'decisions': [], 'proposals': [], 'contradictions': [],
            'gaps': [{'id': 'destination', 'text': 'Publication destination needs a user decision', 'required': True, 'source_refs': ['brief']}]})
        discovery = discover_capabilities(store.project,
            scope={'resources': ['guide.md'], 'dependencies': [{'path': 'brief.md', 'from': 'guide.md', 'reason': 'Explicit source brief for the requested guide'}]},
            backend_config={})
        recorded = record_discovery(store, task['task_id'], {**discovery, 'intake': intake}, expected_revision=0)
        task = tasks.propose(task['task_id'], {
            'summary': 'Prepare a provisional guide; publication remains held.', 'stage_ids': ['draft', 'publish'],
            'business': 'Reuse the supplied brief to produce a reviewable draft while the publication decision remains open.',
            'technical': 'Local file artifact, profile evidence and isolated SQLite memory; optional real Codex review.',
            'steps': ['Inspect only the supplied brief', 'Draft the guide after approval', 'Check the actual artifact and record blockers'],
            'verification': 'Artifact hash plus independent acceptance/review when available; parent stays incomplete.',
            'limitations': ['Publication destination unresolved', 'No actual external send is authorized']}, expected_revision=task['revision'])
        proposal = {'proposal_hash': task['proposal_hash'], 'proposal': task['proposal']}
        journal = Journal(store)
        journal.start_session(task['task_id'], 'demo-session')
        checks, receipts, stage_verdict = None, [], None
        blockers = [{'id': 'destination', 'reason': 'Publication destination is not confirmed', 'next_probe': 'Obtain the explicit user destination decision'}]
        artifacts = []
        if approval_user_message_ref:
            task = tasks.approve(task['task_id'], proposal_hash=task['proposal_hash'],
                                 user_message_ref=approval_user_message_ref, approved_stage_ids=['draft'])
            tasks.require_approval(task['task_id'], 'draft')
            content = ('# Provisional operating guide\n\n' + source.read_text(encoding='utf-8')
                       + '\nPending: publication destination approval.\n'
                       + '\nNext action: confirm the publication destination with the task owner; keep this guide unpublished until approval.\n')
            apply_edits(workspace, task['contract']['scope'], [{'path': 'guide.md', 'operation': 'write', 'before_sha256': '', 'content': content}])
            controller = WorkflowController(store)
            checks = controller.check(task['task_id'], 'draft')
            artifacts = [{'path': 'guide.md', 'sha256': hashlib.sha256((workspace / 'guide.md').read_bytes()).hexdigest()}]
            if live_codex:
                trusted = state_home / 'demo-runner'
                trusted.mkdir(parents=True, exist_ok=True)
                runner = Runner({'executable': str(live_codex), 'trusted_cwd': str(trusted)},
                                store.root / 'tasks' / task['task_id'] / 'sessions')
                controller = WorkflowController(store, runner=runner)
                stage_verdict = controller.verify(task['task_id'], 'draft')
                from ase.state import read_record
                receipts = [read_record(store.db, 'verification', task['task_id'] + ':' + role)['payload'] for role in ('ac', 'review')]
            else:
                blockers.append({'id': 'independent-review', 'reason': 'Independent acceptance/review was not executed',
                                 'next_probe': 'Use an authorized available independent backend and fresh actual evidence'})
            reason = 'blocked'
            summary = 'Actual provisional guide and artifact check saved; parent publication obligations remain unresolved'
        else:
            reason, summary = 'awaiting_approval', 'Scoped discovery and proposal saved; no deliverable changed'
            blockers.append({'id': 'approval', 'reason': 'Exact draft/review scope needs observed user approval', 'next_probe': 'Review the proposal hash and approve draft only'})
        gate = GateService(store).gate(task['task_id'])
        closure = journal.finish_session(task['task_id'], 'demo-session', {
            'finish_reason': reason, 'summary': summary,
            'next_action': 'Review the proposal and confirm only the intended draft/review scope' if reason == 'awaiting_approval' else 'Independently review the provisional guide and resolve the publication destination',
            'context': {'summary': summary, 'source_refs': ['local:brief.md'], 'limitations': ['Illustrative local document; no actual publication or infrastructure provisioning']},
            'blockers': blockers, 'artifacts': artifacts}, closure_key='demo-closure', expected_revision=0, expected_context_revision=journal.context()['revision'])
        today = datetime.now(timezone.utc).date()
        meeting = journal.meeting_report((today - timedelta(days=1)).isoformat(), (today + timedelta(days=2)).isoformat(), timezone='Europe/Bucharest')
        return {'project_id': store.project_id, 'task': tasks.snapshot(task['task_id']), 'proposal': proposal,
                'discovery': recorded, 'checks': checks, 'gate': gate, 'closure': closure, 'meeting_report': meeting,
                'live_stage_verdict': stage_verdict, 'live_receipts': receipts,
                'limitations': ['This example never publishes, sends, provisions a VM or marks the parent completed.',
                                'Without --live-codex no independent model judgment is executed.']}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workspace', type=Path, help='Existing disposable workspace containing brief.md; default creates a temporary fixture')
    parser.add_argument('--state-home', type=Path, help='Local state destination outside the workspace')
    parser.add_argument('--approval-user-message-ref', help='Observed user authorization reference for draft and optional review; never inferred')
    parser.add_argument('--live-codex', type=Path, help='Optional absolute Codex executable for actual independent AC/review')
    args = parser.parse_args(argv)
    if args.live_codex and not args.approval_user_message_ref:
        parser.error('--live-codex requires --approval-user-message-ref')
    if (args.workspace or args.live_codex) and not args.state_home:
        parser.error('--workspace and --live-codex require explicit --state-home outside the workspace so evidence remains available')
    with tempfile.TemporaryDirectory(prefix='ase-universal-demo-') as temp:
        base = Path(temp)
        workspace = args.workspace
        if workspace is None:
            workspace = base / 'workspace'
            workspace.mkdir()
            (workspace / 'brief.md').write_text('Review the supplied source, retain open decisions and record the next action.\n', encoding='utf-8')
        result = run_demo(workspace, args.state_home or base / 'state',
                          approval_user_message_ref=args.approval_user_message_ref, live_codex=args.live_codex)
        result['fixture_is_temporary'] = args.workspace is None
        result['state_is_temporary'] = args.state_home is None
        print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
