"""Quick lane: one command sets up a small, low-risk software task for the owner to approve.

The full path (create, discover, propose, approve, adequacy plan, ...) is right for real
features. For a LOW-risk, single-stage change it is mostly ceremony, and ceremony that agents
skip leaves no evidence at all. The quick lane keeps what matters (a scope approved by the owner
after seeing the plan, executed tests, separate independent AC and review, one lead pass, the gate
and `finish`) and drops the separate test-adequacy session. `finish` holds a quick task to a small
diff budget, so a change that grows is moved to the full path instead of slipping through.
"""
import os
import shlex

from .state import text

QUICK_MAX_LINES = 300
QUICK_MAX_FILES = 15


def _parser(argv):
    """Pick the count parser from the executable and the `-m` module, not from substrings."""
    def base(word):
        name = os.path.basename(word).lower()
        for suffix in ('.exe', '.cmd', '.bat', '.ps1'):
            name = name.removesuffix(suffix)
        return name
    words = [base(w) for w in argv[:6]]
    module = argv[argv.index('-m') + 1].lower() if '-m' in argv[:-1] else ''
    # `uv run pytest`, `poetry run pytest`, `py.test`: look at the first few tokens, not substrings.
    if module == 'pytest' or {'pytest', 'py.test'} & set(words):
        return {'parser': 'pytest'}
    if module == 'unittest':
        return {'parser': 'unittest'}
    if words[0] == 'dotnet':
        return {'parser': 'dotnet'}
    if words[0] in {'npm', 'npx', 'node', 'jest', 'vitest', 'pnpm', 'yarn'}:
        return {'parser': 'node'}
    return {}


def _argv(command):
    """POSIX splitting would eat Windows backslashes; there, split and strip surrounding quotes."""
    if os.name != 'nt':
        return shlex.split(command, posix=True)
    return [part[1:-1] if len(part) > 1 and part[0] == part[-1] and part[0] in '"\'' else part
            for part in shlex.split(command, posix=False)]


def validate_lane(contract):
    if contract.get('lane') != 'quick':
        raise ValueError('lane must be "quick"')
    if contract.get('risk') != 'LOW' or len(contract.get('stages', [])) != 1 or contract.get('profile') != 'software':
        raise ValueError('The quick lane is only for LOW-risk single-stage software tasks; use the full path')
    if (contract.get('test_adequacy') or {}).get('required') is True:
        raise ValueError('A task with required test adequacy is on the full path; remove lane')


def contract(*, task_id, request, resources, test_command, source, backend):
    """A v2 software contract for one LOW-risk implementation stage."""
    from .lead_review import policy_for_risk
    from .review.blind import POLICY as BLIND_REVIEW
    argv = _argv(test_command)
    if not argv:
        raise ValueError('Quick lane needs a test command')
    return {'id': task_id, 'schema_version': 2, 'profile': 'software', 'lane': 'quick',
            'request': request, 'risk': 'LOW',
            'scope': {'resources': list(resources), 'effects': ['file_write', 'command']},
            'sources': [{'id': 'REQUEST', 'locator': source}],
            'acceptance': [{'id': 'AC1', 'expected': request, 'mandatory': True, 'evidence_ids': ['tests']}],
            'evidence': [{'id': 'tests', 'kind': 'test', **_parser(argv), 'argv': argv, 'required': True}],
            'stages': [{'id': 'implement', 'acceptance_ids': ['AC1'], 'depends_on': []}],
            'unknowns': [], 'assumptions': [],
            'lead_review': policy_for_risk('LOW'), 'review_format': {'version': 2}, 'blind_review': dict(BLIND_REVIEW),
            'execution_policy': {'backend': backend, 'recovery_mode': 'progress', 'max_task_cycles': 6, 'max_diagnostic_passes': 3},
            'reporting': {'timezone': 'local'}}


def start(store, *, task_id, request, resources, test_command, summary, user_message_ref, backend='claude', base=None):
    """Create, discover and propose in one step. Nothing is approved here: the owner sees the plan
    first, and `ase task approve` then cites the owner's reply (not the request message)."""
    from .discovery import discover_capabilities, discovery_refresh_scope, record_discovery
    from .prompts import resolve
    from .tasks import TaskService
    text(request, 'request', limit=4000)
    text(summary, 'summary', limit=4000)
    text(user_message_ref, 'user_message_ref', limit=2000)
    captured = resolve(store, user_message_ref)
    if base:
        from . import ci_check
        from .repositories import repository_path, task_repository
        repo = repository_path(store.repo, task_repository(store.repo, {'scope': {'resources': list(resources or [])}})['path'])
        size = ci_check.run(repo, base)
        if size['files'] > QUICK_MAX_FILES or size['lines'] > QUICK_MAX_LINES:
            raise ValueError(f"The branch already differs from {base} by {size['files']} files / {size['lines']} lines; "
                             'the quick lane cannot finish here, use the full path')
    if not resources:
        raise ValueError('Quick lane needs at least one --resource')
    # AC judges against the request; with a captured prompt the owner's own words travel with it.
    source = user_message_ref
    if captured:
        from .core import redact
        words = redact(captured['text'])
        cut = len(words) > 1500 or captured.get('truncated')
        source += (' (owner wrote' + (', truncated' if cut else '') + ', sha256 ' + captured['sha256'][:16] + ': '
                   + words[:1500] + ')')
    tasks = TaskService(store)
    task = tasks.create(contract(task_id=task_id, request=request, resources=resources, test_command=test_command,
                                 source=source, backend=backend))
    task_id = task['task_id']
    try:
        record_discovery(store, task_id, discover_capabilities(store.project, scope=discovery_refresh_scope(store, task_id),
                                                               backend_config={}), expected_revision=0)
        task = tasks.propose(task_id, {'summary': summary, 'stage_ids': ['implement']}, expected_revision=task['revision'])
    except Exception as error:
        raise ValueError(f'Quick task {task_id} was created but setup stopped: {error}. Continue it with '
                         '`ase task discover/propose/approve` instead of creating it again') from error
    return {'task_id': task_id, 'state': task['state'], 'lane': 'quick', 'proposal_hash': task['proposal_hash'],
            'next': ['show the owner the short implementation plan and wait for their reply',
                     f'ase task approve {task_id} --proposal-hash {task["proposal_hash"]} --stage implement '
                     '--user-message-ref <the owner reply that approves the plan>',
                     *steps_after_approval(task_id, resources, backend)],
            'limits': f'Quick lane: LOW risk, one stage, at most {QUICK_MAX_FILES} files / {QUICK_MAX_LINES} lines; '
                      'larger or riskier work uses the full path.'}


def steps_after_approval(task_id, resources, backend):
    """Each entry is an `ase` command (parseable as written, placeholders aside), then `  # ` and why."""
    return [
        f'ase events bind-local {task_id} --backend <codex|claude|copilot> --user-message-ref <the owner reply> '
        '--expected-revision 0  # binds this chat (its own backend) as the original lead once per task; use '
        '`ase events bind --file` instead when the host exposes the native chat id. Keep payload.destination.thread_id',
        f'ase task run {task_id}  # the only command that records the implementation capture. Make the edits '
        f'within {", ".join(resources)} either natively after approval or by letting this run: the {backend} '
        'implementer session proposes the edits against the current tree (none if the change is already there), '
        'then the tests and independent AC/review run. Run it after the change exists so it captures it; native '
        'edits alone are not a capture, so `lead-review prepare` before this run fails. It ends BLOCKED awaiting '
        'the lead pass',
        f'ase lead-review status {task_id}  # record.revision is the --expected-revision for prepare',
        f'ase lead-review prepare {task_id} --thread-id <thread_id> --expected-revision <revision>  '
        '# then perform the critique in handoff.prompt and write handoff.capture_template filled in',
        f'ase lead-review record {task_id} --attempt <handoff.attempt_id> --file <assessment.json> '
        '--thread-id <thread_id> --expected-revision <revision returned by prepare>',
        f'ase task verify {task_id}  # final independent AC/review against the lead pass. After any later fix, '
        'run `ase task run` again before verify: verify alone blocks on stale evidence',
        f'ase task finish {task_id} --base <default branch> --body-file <file>']
