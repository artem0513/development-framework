"""Framework identity on verdicts, and the rule that makes an older verdict stale.

Every independent receipt and every gate decision records the framework source commit and the
hash of ase/data/runtime_policy.json it ran under. A required verdict of an open task whose
policy hash differs from the installed one is stale: it must be re-run before the task can be
verified. Closed tasks are never re-opened or re-reviewed because of an update.
"""
import json
from pathlib import Path
import subprocess

from .protocol import sha256
from .runtime_policy import BUNDLE_PATH

# Written by scripts/install.py into the installed package; a source checkout asks Git instead.
# A fixed path relative to the package root, so a root reached through a link still finds it.
SOURCE_RELATIVE = Path('ase/data/framework_source.json')
SOURCE_FILE = Path(__file__).with_name('data') / SOURCE_RELATIVE.name
STAMP_FIELD = 'framework'
STAMP_VERSION = 1
UNKNOWN = 'unknown'
# Git abbreviates commits to this many hex digits by default; reports use the same width.
SHORT_COMMIT = 7
# Policy hashes are shown a little longer: two policies of one commit range must stay distinguishable.
SHORT_HASH = 12
GIT_TIMEOUT_SECONDS = 30


def package_root():
    return Path(__file__).resolve().parents[1]


def recorded_source(source_file=SOURCE_FILE):
    """The installer's record of the commit an installed package was copied from, or None."""
    try:
        value = json.loads(Path(source_file).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    if isinstance(value, dict) and isinstance(value.get('commit'), str) and value['commit']:
        return {'commit': value['commit'], 'dirty': value.get('dirty') is not False}
    return None


def source_commit(root=None):
    """The framework commit of a checkout (root) or of this package.

    Only a folder that is itself a Git checkout answers from Git: an installed runtime inside a project's
    tree would otherwise report the project's HEAD. An installed package answers from the installer's record.
    """
    root = Path(root) if root is not None else package_root()
    if not (root / '.git').exists():
        recorded = recorded_source(root / SOURCE_RELATIVE)
        return recorded['commit'] if recorded else UNKNOWN
    try:
        result = subprocess.run(['git', '-C', str(root), 'rev-parse', '--verify', '-q', 'HEAD'],
                                capture_output=True, timeout=GIT_TIMEOUT_SECONDS, check=False)
    except (OSError, subprocess.SubprocessError):
        return UNKNOWN
    commit = result.stdout.decode('ascii', 'replace').strip()
    return commit if result.returncode == 0 and commit else UNKNOWN


def policy_file_sha256(path=BUNDLE_PATH):
    try:
        return sha256(Path(path).read_bytes())
    except OSError:
        return UNKNOWN


def stamp(*, commit=None, policy_path=BUNDLE_PATH):
    return {'version': STAMP_VERSION, 'framework_commit': commit or source_commit(),
            'policy_file_sha256': policy_file_sha256(policy_path)}


def valid_stamp(value):
    return (isinstance(value, dict) and value.get('version') == STAMP_VERSION
            and all(isinstance(value.get(name), str) and value[name] for name in ('framework_commit', 'policy_file_sha256')))


def short(value, width=SHORT_COMMIT):
    return value if value == UNKNOWN else value[:width]


def stale_verdict(label, receipt, current):
    """None when the receipt ran under the current policy, else a description of why it is stale."""
    if not isinstance(receipt, dict):
        return None
    recorded = receipt.get(STAMP_FIELD)
    if not valid_stamp(recorded):
        return {'verdict': label, 'old_commit': None, 'new_commit': current['framework_commit'],
                'old_policy': None, 'new_policy': current['policy_file_sha256'],
                'reason': (f'Stale verdict: {label} has no framework stamp (recorded before stamping) -> current '
                           f'framework {short(current["framework_commit"])}; re-run it under the current policy')}
    if recorded['policy_file_sha256'] == current['policy_file_sha256']:
        return None
    return {'verdict': label, 'old_commit': recorded['framework_commit'], 'new_commit': current['framework_commit'],
            'old_policy': recorded['policy_file_sha256'], 'new_policy': current['policy_file_sha256'],
            'reason': (f'Stale verdict: {label} ran under framework {short(recorded["framework_commit"])} -> current '
                       f'{short(current["framework_commit"])} (runtime policy {short(recorded["policy_file_sha256"], SHORT_HASH)} -> '
                       f'{short(current["policy_file_sha256"], SHORT_HASH)}); re-run it under the current policy')}


def required_verdicts(store, contract, task_id, stage_id=None):
    """(label, receipt) for each recorded independent verdict this gate relies on."""
    from .evidence.representation import part_key, parts
    from .review import blind
    from .state import read_record
    from .testing.adequacy import TestAdequacy, required
    rows = [('Acceptance', read_record(store.db, 'verification', task_id + ':ac')['payload']),
            ('Review', read_record(store.db, 'verification', task_id + ':review')['payload'])]
    if required(contract, stage_id):
        rows.append(('Test adequacy', TestAdequacy(store).current_receipt(task_id, stage_id)))
    if blind.enabled(contract):
        rows.append(('Blind review', read_record(store.db, 'verification_blind', blind.key(task_id, stage_id))['payload']))
    rows.extend(('Review part ' + part['id'],
                 read_record(store.db, 'verification_part', part_key(task_id, stage_id, part['id']))['payload'])
                for part in parts(contract))
    return [(label, receipt) for label, receipt in rows if receipt]


def stale_verdicts(store, task, stage_id=None, *, current=None):
    """Stale required verdicts of an open task; a closed task is never re-judged after an update."""
    from .tasks import TERMINAL_STATES
    if task.get('state') in TERMINAL_STATES:
        return []
    current = current or stamp()
    found = (stale_verdict(label, receipt, current)
             for label, receipt in required_verdicts(store, task['contract'], task['task_id'], stage_id))
    return [row for row in found if row]


def open_tasks_with_stale_verdicts(store, *, current=None):
    from .tasks import TaskService
    current = current or stamp()
    service = TaskService(store)
    result = []
    for row in store.db.execute('SELECT id FROM tasks ORDER BY id').fetchall():
        try:
            task = service.snapshot(row['id'])
            if task.get('legacy_v1'):
                continue
            stale = stale_verdicts(store, task, current=current)
        except (ValueError, KeyError, TypeError):
            continue
        if stale:
            result.append({'task_id': task['task_id'], 'state': task['state'], 'stale': stale})
    return result
