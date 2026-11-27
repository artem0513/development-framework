"""One pre-push answer: is this HEAD shippable for this task?

Combines the existing pieces instead of adding new rules: the gate (current independent
evidence for the working tree), a clean working tree (so the gated tree is HEAD), the
deterministic `ci-check` against the base, and the PR body. It is a local reliability step;
it enforces nothing on the server.
"""
import subprocess

from .gates import GateService
from .inspection import SOURCE_STATUS, source_status


def _git(repo, *args):
    done = subprocess.run(['git', '-C', str(repo), *args], capture_output=True, timeout=120)
    if done.returncode != 0:
        raise ValueError('git ' + args[0] + ' failed: ' + done.stderr.decode('utf-8', 'replace').strip()[:300])
    return done.stdout.decode('utf-8', 'replace')


# Fallback when a private installation's exclude block is missing or was edited.
KNOWN_PRIVATE = ('/AGENTS.override.md', '/CLAUDE.local.md', '/.devframework/', '/.github/instructions/devframework.instructions.md',
                 '/.agents/skills/task-orchestrator/', '/.claude/skills/task-orchestrator/', '/.github/skills/task-orchestrator/',
                 '/.claude/agents/ase-implementer.md', '/.github/agents/ase-implementer.md', '/.codex/agents/ase-implementer.toml',
                 '/.codex/autonomous-se-backups/')
MANAGED_MARKERS = ('autonomous-se managed begin', 'autonomous-se private files begin')
# Whole tokens only, so "autonomous sensing" or a team's own "base" never matches.
FRAMEWORK_TERMS = (r'developmentframework', r'development framework', r'autonomous[- ]se', r'\.devframework',
                   r'autonomous-se managed begin', r'autonomous-se private files begin')
WORKFLOW_TERMS = (r'ase', r'quick lane', r'evidence gate', r'lead review', r'test adequacy')


def private_repository(repo):
    """Every repository except the framework's own is private: the team must not see the framework."""
    from .project_local import is_framework_source
    from pathlib import Path
    return not is_framework_source(Path(_git(repo, 'rev-parse', '--show-toplevel').strip()))


def _mentions(text, terms=FRAMEWORK_TERMS + WORKFLOW_TERMS):
    import re
    lowered = text.lower()
    return [term.replace('\\', '').replace('[- ]', '-') for term in terms
            if re.search(r'(?<![\w.-])' + term + r'(?![\w-])', lowered)]


def private_leaks(repo, base, body):
    """Ways the branch or the PR body would show the framework to the team; no override exists.

    Code and added lines are checked only for the framework's own names, so a team's `ase`
    package passes; commit messages and the PR body, written by the agent, also for its workflow terms.
    """
    from pathlib import Path
    from .project_local import matches_private, private_patterns
    top = Path(_git(repo, 'rev-parse', '--show-toplevel').strip())
    # Color, external diff drivers and textconv would hide added lines from the scan.
    plain = ('-c', 'color.ui=never', '-c', 'diff.external=', '-c', 'core.quotePath=false')
    patterns = private_patterns(top) or list(KNOWN_PRIVATE)
    reasons = []
    paths = [p for p in _git(top, *plain, 'diff', '--no-ext-diff', '--no-textconv', '--name-only', '-z', base + '...HEAD').split('\0') if p]
    exposed = [p for p in paths if any(matches_private(pattern, p) for pattern in patterns)]
    if exposed:
        reasons.append('Framework files are in this branch; remove them from its commits: ' + ', '.join(exposed[:10]))
    added = '\n'.join(line[1:] for line in _git(top, *plain, 'diff', '--no-ext-diff', '--no-textconv', '--no-color', '-U0',
                                                 base + '...HEAD').splitlines()
                      if line.startswith('+') and not line.startswith('+++'))
    said = _mentions(added, FRAMEWORK_TERMS)
    if said:
        reasons.append('Added lines mention the framework (' + ', '.join(said) + '); remove those mentions')
    said = _mentions(_git(top, *plain, 'log', '--no-color', '--format=%B', base + '..HEAD')
                     + '\n' + _git(top, 'rev-parse', '--abbrev-ref', 'HEAD'))
    if said:
        reasons.append('Commit messages or the branch name mention the framework (' + ', '.join(said) + '); reword them')
    said = _mentions(body)
    if said:
        reasons.append('The PR description would mention the framework (' + ', '.join(said) + '); write your own '
                       'description to the --body-file path (outside the repository) and run finish again')
    return reasons


def plain_body(task, gate, notes=()):
    """A PR description about the change only: no framework, gate, lane or evidence terms."""
    contract, proposal = task['contract'], task.get('proposal') or {}
    lines = [str(contract.get('request') or '').strip(), '']
    if proposal.get('summary'):
        lines += ['## Approach', '', proposal['summary'].strip(), '']
    for title, key in (('Non-goals', 'non_goals'), ('Unchanged behaviour', 'unchanged_behavior')):
        if proposal.get(key):
            lines += ['## ' + title, '', *('- ' + str(item) for item in proposal[key]), '']
    if proposal.get('rollback'):
        lines += ['**Rollback:** ' + str(proposal['rollback']), '']
    tests = [row for row in ((gate.get('profile_evidence') or {}).get('by_id') or {}).values()
             if isinstance(row.get('counts'), dict)]
    if tests:
        total = sum(row['counts'].get('total', 0) for row in tests)
        failed = sum(row['counts'].get('failed', 0) for row in tests)
        lines += ['## Testing', '', f'{total} tests run, {failed} failed.', '']
    return '\n'.join(lines + list(notes)).rstrip() + '\n'


def _merge_base(repo, base, head, dirty=()):
    """The fork point the change is measured from; a base that hides the change is refused."""
    if not base or base.startswith('-'):
        raise ValueError('Base must be a branch name, e.g. origin/main')
    if subprocess.run(['git', '-C', str(repo), 'rev-parse', '--verify', '--quiet', base + '^{commit}'],
                      capture_output=True, timeout=60).returncode != 0:
        raise ValueError('Base ' + base + ' does not resolve to a commit; fetch it or pass e.g. origin/main')
    # A revision expression (HEAD~1, a SHA) could hide this branch's earlier commits; the merge target is a branch.
    full = subprocess.run(['git', '-C', str(repo), 'rev-parse', '--symbolic-full-name', base],
                          capture_output=True, timeout=60).stdout.decode('utf-8', 'replace').strip()
    if not full.startswith(('refs/heads/', 'refs/remotes/')):
        raise ValueError('Base ' + base + ' is not a branch; pass the branch this change will merge into, e.g. origin/main')
    done = subprocess.run(['git', '-C', str(repo), 'merge-base', base, head], capture_output=True, timeout=60)
    if done.returncode != 0:
        raise ValueError('Base ' + base + ' shares no history with HEAD; pass the branch this change will merge into')
    fork = done.stdout.decode('utf-8', 'replace').strip()
    if fork == head and dirty:
        raise ValueError('Base ' + base + ' already contains HEAD because the change is not committed yet ('
                         + ', '.join(dirty[:10]) + '); commit it on the feature branch and run finish again')
    if fork == head:
        raise ValueError('Base ' + base + ' already contains HEAD, so the change against it is empty; '
                         'pass the branch this change will merge into'
                         + ('' if base == 'origin/main' else ', e.g. origin/main'))
    return fork


def _override_reference(store, task_id, ref, quote):
    """Same rule as a finding disposition: a captured prompt after the review, or a chat ref plus the owner's words."""
    from .findings import _excerpt, capture_in_use
    from .prompts import resolve
    from .state import read_record, text
    usage = ('ci-check overrides need the owner decision: --override-ref prompt:<id> from `ase prompt list` or, '
             'without prompt capture, --override-ref <chat ref> with --override-quote "<owner\'s exact words>"')
    if not isinstance(ref, str) or not ref.strip():
        raise ValueError(usage)
    text(ref, 'override_ref', limit=2000)
    if quote is not None:
        text(quote, 'override_quote', limit=2000)
    captured = resolve(store, ref.strip())
    if captured is None and not (quote or '').strip():
        raise ValueError(usage)
    if captured is None and capture_in_use(store, task_id):
        raise ValueError('This task uses prompt capture: --override-ref must be the owner prompt:<id> from `ase prompt list`')
    review = read_record(store.db, 'verification', task_id + ':review')['payload'] or {}
    if captured is not None and captured['captured'] < (review.get('ended_at_utc') or ''):
        raise ValueError('The cited owner prompt predates the review; cite the owner decision made after it')
    return {'user_message_ref': ref.strip(), 'verified': captured is not None,
            'owner_words': _excerpt(captured['text'] if captured else quote)}


def _owner(override_owner):
    return (override_owner['user_message_ref'] + ', ' + _basis(override_owner['verified'])
            + ': "' + override_owner['owner_words'] + '"')


def _basis(verified):
    return 'captured owner prompt' if verified else 'owner-asserted, not verified'


def finish(store, task_id, base, *, allow_protected=False, allow_large=False, allow_test_removal=False,
           allow_test_skip=False, body_file=None, override_ref=None, override_quote=None):
    from . import ci_check
    from .report_md import render
    from .tasks import TaskService, current_approval_decisions
    from .usage import task_usage
    from .repositories import repository_path, task_repository
    # The repository this task pushes: a declared nested clone or the workspace root.
    repo = repository_path(store.repo, task_repository(store.repo, TaskService(store).snapshot(task_id)['contract'])['path'])
    reasons = []

    from pathlib import Path
    body = Path(body_file).resolve() if body_file else None
    top = Path(_git(repo, 'rev-parse', '--show-toplevel').strip()).resolve()
    own = body.relative_to(top).as_posix() if body and body.is_relative_to(top) else None
    private = private_repository(repo)
    # In a private repository an existing body file is the agent's own description: it is scanned, not replaced.
    supplied = body.read_text(encoding='utf-8') if private and body and own is None and body.is_file() else None

    def tree_state():
        entries = iter(filter(None, source_status(_git(repo, 'status', *SOURCE_STATUS)).split('\0')))
        dirty = []
        for entry in entries:
            if 'R' in entry[:2] or 'C' in entry[:2]:
                next(entries, None)  # -z puts the rename/copy source in its own entry
            if entry[3:] != own:
                dirty.append(entry[3:])
        return _git(repo, 'rev-parse', 'HEAD').strip(), dirty

    head, dirty = tree_state()
    fork = _merge_base(repo, base, head, dirty)
    overrides = [name for name, used in (('allow-protected', allow_protected), ('allow-large', allow_large),
                                         ('allow-test-removal', allow_test_removal), ('allow-test-skip', allow_test_skip)) if used]
    override_owner = _override_reference(store, task_id, override_ref, override_quote) if overrides else None
    gate = GateService(store).gate(task_id)
    if tree_state() != (head, dirty):
        reasons.append('HEAD or the working tree changed while the gate ran; run finish again')
    if gate.get('status') != 'APPROVED':
        reasons.append('Gate is ' + str(gate.get('status')) + ': ' + '; '.join(gate.get('reasons', [])[:5]))
    # The gate judged the working tree; only a clean tree makes that judgment apply to HEAD.
    if dirty:
        reasons.append('Uncommitted changes, so the gated tree is not HEAD: ' + ', '.join(dirty[:10]))
    from .quick import QUICK_MAX_FILES, QUICK_MAX_LINES
    quick = TaskService(store).snapshot(task_id)['contract'].get('lane') == 'quick'
    check = ci_check.run(repo, base, max_lines=QUICK_MAX_LINES if quick else ci_check.DEFAULT_MAX_LINES,
                         max_files=QUICK_MAX_FILES if quick else ci_check.DEFAULT_MAX_FILES,
                         allow_protected=allow_protected, allow_large=allow_large, allow_test_removal=allow_test_removal,
                         allow_test_skip=allow_test_skip)
    if check['status'] != 'PASS':
        reasons.extend(f"ci-check {f['level']} {f['check']}: {f['message']}" for f in check['findings']
                       if f['level'] in {'FAIL', 'REVIEW'})
    if quick and (check['files'] > QUICK_MAX_FILES or check['lines'] > QUICK_MAX_LINES):
        # The size is what justified skipping the test-adequacy session, so no override waives it.
        reasons.append(f"Quick lane exceeded ({check['files']} files / {check['lines']} lines > {QUICK_MAX_FILES} / "
                       f"{QUICK_MAX_LINES}): revise the task with test_adequacy {{version: 1, required: true}} "
                       "(lane is then removed) to move it to the full path")
    if quick and any(f['check'] == 'no_test_changes' for f in check['findings']):
        # Without a test-adequacy session, a changed test is the only proof the tests exercise this change.
        reasons.append(f'Quick lane changed source without adding or changing a test file against {base}: '
                       'add a test for the change, or move the task to the full path with test_adequacy '
                       '{version: 1, required: true}')
        # Report the finding at the level that blocks here; ci_check itself keeps INFO for other lanes.
        check = {**check, 'status': 'FAIL', 'findings': [
            {**f, 'level': 'FAIL', 'message': f['message'] + ' Quick lane: blocks finish.'}
            if f['check'] == 'no_test_changes' else f for f in check['findings']]}
    task = TaskService(store).snapshot(task_id)
    approvals = current_approval_decisions(store.db, task)
    # Owner decisions that only the agent's record vouches for: listed for the owner, never silently READY.
    review = [f"{row['severity']} finding disproved by owner ({row['user_message_ref']}, {_basis(False)}): "
              f"{row.get('message')}" for row in gate.get('disposed_findings') or []
              if row.get('severity') in {'P0', 'P1'} and not row.get('verified')]
    asserted = []
    if override_owner:
        asserted.append('Overrides ' + ', '.join(overrides) + ' by owner (' + _owner(override_owner) + ')')
        if not override_owner['verified']:
            review.append('Overrides ' + ', '.join(overrides) + ' rest on owner words the agent supplied ('
                          + override_owner['user_message_ref'] + '); confirm them')
    unverified = [approval['user_message_ref'] for approval in approvals if approval.get('provenance') != 'captured_prompt']
    asserted.extend('Plan approval by owner (' + ref + ', ' + _basis(False) + ')' for ref in unverified)
    report = {'task': task, 'quality_gate': gate, 'usage': task_usage(store, task_id)}
    body = render(report)
    ci_line = f"ci-check against {base} (merge-base {fork[:12]}): {check['status']}"
    if overrides:
        ci_line += ' with owner overrides: ' + ', '.join(overrides) + ' (' + _owner(override_owner) + ')'
    noted = [f"{f['check']}" for f in check['findings'] if f['level'] == 'INFO' and f['check'] in
             {'protected_paths', 'diff_budget', 'test_deleted', 'test_skip_added', 'assertions_removed'}]
    if noted:
        ci_line += ' (overridden checks: ' + ', '.join(noted) + ')'
    if quick:
        ci_line += '\n\nLane: quick (LOW risk, one stage, no separate test-adequacy session).'
    if review:
        ci_line += '\n\n**Needs owner review**\n' + ''.join('\n- ' + item for item in review)
    if asserted:
        ci_line += '\n\n**Owner decisions**\n' + ''.join('\n- ' + item for item in asserted)
    body = body.replace('### Enforcement', '### Deterministic checks\n\n' + ci_line + '\n\n### Enforcement', 1)
    write_body = True
    if private:
        # The full evidence stays in this answer and the local store; the team sees only the change.
        # Owner-review items and local prompt references stay in the result for the owner, never in the body.
        notes = []
        if overrides and '## Owner exceptions' not in (supplied or ''):
            notes += ['## Owner exceptions', '', *('- ' + name + ': approved by the owner' for name in overrides), '']
        if supplied is not None:
            body = supplied.rstrip() + '\n\n' + '\n'.join(notes).rstrip() + '\n' if notes else supplied
        else:
            body = plain_body(report['task'], gate, notes)
        reasons.extend(private_leaks(repo, base, body))
        if own is not None:
            write_body = False
            reasons.append('Write the PR body file outside the repository, so it cannot be committed: ' + own)
    blocked = gate.get('status') == 'BLOCKED'
    owner_review = None
    preapproved = [approval['user_message_ref'] for approval in approvals if approval.get('owner_preapproved')]
    if preapproved:
        # Never in the PR body: this is for the owner, who did not see the plan before the work.
        owner_review = ('Approved in advance (' + ', '.join(preapproved) + ') without a plan review: '
                        'report every HIGH, MEDIUM and LOW decision taken, with its option and score, first.')
    status = 'BLOCKED' if reasons and blocked else 'NOT_READY' if reasons else 'REVIEW' if review else 'READY'
    from .coordinator import CoordinatorEvents
    # The original lead processes its inbox before the final report; acknowledgement is not a completion condition.
    inbox = CoordinatorEvents(store).pending(task_id)
    events_hint = None
    if inbox['events']:
        events_hint = (f"{len(inbox['events'])} coordinator event(s) are not processed by the original lead (does not change "
                       f"{status}). Run each next_command: a PENDING event is claimed first, then classify the result against "
                       "the current evidence and rerun `ase task finish` to get the ack command for the claimed event.")
        if not inbox['bound']:
            from .lead_review import bind_local_action
            from .state import read_record
            revision = read_record(store.db, 'coordinator', CoordinatorEvents(store)._root(task_id))['revision']
            events_hint = bind_local_action(task_id, revision) + ' Then use its thread_id. ' + events_hint
    return {'owner_review': owner_review, 'status': status,
            'pending_events': inbox['events'], 'pending_events_hint': events_hint,
            'head': head, 'base': base, 'merge_base': fork, 'repository': str(repo), 'gate_status': gate.get('status'), 'overrides': overrides,
            'override_owner': override_owner, 'needs_owner_review': review, 'owner_asserted': asserted,
            'approval_provenance': 'owner_asserted' if unverified else 'captured_prompt' if approvals else None,
            'ci_check': {'status': check['status'], 'findings': check['findings']}, 'dirty': dirty,
            'reasons': reasons, 'pr_body': body, 'write_body_file': write_body,
            'enforcement': 'Local check only; no server-side enforcement is assumed.'}
