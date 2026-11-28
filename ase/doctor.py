"""Self-check after a framework update: is the installed framework complete and consistent?

An update counts as complete only when every check passes. Each check is PASS, FAIL or
UNAVAILABLE (it could not run here, for example without the framework source); any FAIL makes
the update INCOMPLETE and an UNAVAILABLE check leaves it UNVERIFIED, never a success.
"""
import hashlib
import json
import os
import sqlite3
from pathlib import Path
import subprocess
import sys

from .framework import UNKNOWN, open_tasks_with_stale_verdicts, policy_file_sha256, short, source_commit, stamp
from .protocol import sha256

INSTALLATION = Path('autonomous-se/installation.json')
SKILL_MIRRORS = ('.agents/skills', '.claude/skills', '.github/skills')
POLICY = Path('ase/data/runtime_policy.json')
# The deterministic review-quality gate: answer-key fixtures run against both policy variants.
FIXTURE_TESTS = 'test_production_reasoning_scenarios.py'
CHECK_TIMEOUT_SECONDS = 1800
# Passed source checks per exact source tree, inside the ignored outputs/ folder of the checkout:
# every install from an unchanged source would otherwise rerun minutes of identical fixture tests.
SOURCE_CHECK_CACHE = Path('outputs/doctor-checks')
GIT_TIMEOUT_SECONDS = 60
PASS, FAIL, UNAVAILABLE = 'PASS', 'FAIL', 'UNAVAILABLE'
MAX_DETAILS = 20


def _check(name, status, details=()):
    details = list(details)
    shown = details[:MAX_DETAILS] + ([f'{len(details) - MAX_DETAILS} more'] if len(details) > MAX_DETAILS else [])
    return {'name': name, 'status': status, 'details': shown}


def _sha(path):
    return sha256(path.read_bytes())


def installation(home):
    path = Path(home) / INSTALLATION
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None


def installed_commit(home):
    record = installation(home) if home else None
    return (record or {}).get('framework_commit') or UNKNOWN


def check_installed_files(home):
    record = installation(home)
    if record is None:
        return _check('installed files match the install manifest', UNAVAILABLE,
                      ['no installation record at ' + str(Path(home) / INSTALLATION)])
    # Framework-owned copies only: instruction files and local config are the owner's to edit later.
    problems = []
    try:
        roots = [Path(record['runtime']), *(Path(path) for path in record.get('skill_destinations', {}).values() if path)]
        for item in record.get('owned_files', []):
            path = Path(item['path'])
            if not any(path.is_relative_to(root) for root in roots):
                continue
            if not path.is_file():
                problems.append('missing: ' + str(path))
            elif _sha(path) != item['sha256']:
                problems.append('differs from the manifest: ' + str(path))
    except (KeyError, TypeError, AttributeError, OSError) as error:
        problems.append(f'installation record could not be checked: {type(error).__name__}: {error}')
    return _check('installed files match the install manifest', FAIL if problems else PASS, problems)


def _tree(root):
    return {path.relative_to(root).as_posix(): path for path in root.rglob('*')
            if path.is_file() and '__pycache__' not in path.parts and path.suffix != '.pyc'}


def compare_skills(canonical, mirror):
    """Differences between the framework's skills and one mirror; other skills in the mirror are ignored."""
    problems = []
    for skill in sorted(path for path in canonical.iterdir() if path.is_dir()):
        source, copy = _tree(skill), _tree(mirror / skill.name) if (mirror / skill.name).is_dir() else {}
        for name in sorted(set(source) | set(copy)):
            if name not in copy:
                problems.append(f'{mirror / skill.name / name}: missing')
            elif name not in source:
                problems.append(f'{mirror / skill.name / name}: not in the framework skill')
            elif source[name].read_bytes() != copy[name].read_bytes():
                problems.append(f'{mirror / skill.name / name}: differs')
    return problems


def check_mirrors(canonical, mirrors):
    present = [mirror for mirror in mirrors if mirror.is_dir()]
    if not canonical.is_dir():
        return _check('skill mirrors are identical', UNAVAILABLE, ['no framework skills at ' + str(canonical)])
    problems = [problem for mirror in present for problem in compare_skills(canonical, mirror)]
    return _check('skill mirrors are identical', FAIL if problems else PASS,
                  problems or ['compared: ' + ', '.join(str(mirror) for mirror in present) if present else 'no mirrors present'])


def check_policy(package_root):
    from .protocol import canonical
    from .runtime_policy import build_bundle
    path = package_root / POLICY
    try:
        expected = canonical(build_bundle(package_root)) + b'\n'
        actual = path.read_bytes()
    except (OSError, ValueError) as error:
        return _check('runtime policy matches the skill text', FAIL, [str(error)])
    if actual != expected:
        return _check('runtime policy matches the skill text', FAIL,
                      [f'{path} is stale: regenerate it with scripts/build_assets.py'])
    return _check('runtime policy matches the skill text', PASS)


def _run(name, command, cwd):
    try:
        result = subprocess.run(command, cwd=cwd, capture_output=True, timeout=CHECK_TIMEOUT_SECONDS, check=False)
    except (OSError, subprocess.SubprocessError) as error:
        return _check(name, FAIL, [str(error)])
    output = (result.stdout + result.stderr).decode('utf-8', 'replace').strip().splitlines()
    return _check(name, PASS if result.returncode == 0 else FAIL, output[-8:] if result.returncode else output[-1:])


def source_tree_key(source_root):
    """A hash of the source checkout exactly as it is now: HEAD, tracked edits and untracked files.

    Read-only (no index or object writes); None when it cannot be computed, so nothing is reused."""
    def git(*args):
        return subprocess.run(['git', '-c', 'core.quotepath=false', *args], cwd=source_root, capture_output=True,
                              timeout=GIT_TIMEOUT_SECONDS, check=True).stdout
    try:
        digest = hashlib.sha256(git('rev-parse', 'HEAD'))
        digest.update(git('diff', '--binary', '--no-ext-diff', '--no-textconv', 'HEAD'))
        for name in sorted(git('ls-files', '--others', '--exclude-standard', '-z').split(b'\0')):
            if name:
                digest.update(b'\0' + name + b'\0' + (source_root / os.fsdecode(name)).read_bytes())
    except (OSError, subprocess.SubprocessError):
        return None
    return digest.hexdigest()


def vouching_source(candidate, installed_commit, installed_dirty=False):
    """(source, note): the checkout whose tests may vouch for the installed files, else None and why.

    Only a checkout at the installed commit with no uncommitted or untracked changes describes the
    installed files; commit equality alone would test edits that were never installed."""
    if installed_dirty is None:
        return None, 'not recorded whether the installation was copied from a clean commit'
    if installed_dirty is not False:
        return None, 'installed from uncommitted changes: no source checkout describes these files'
    if candidate is None or not (Path(candidate) / '.git').exists():
        return None, 'framework source checkout is not available'
    if source_commit(candidate) != installed_commit:
        return None, f'framework source is no longer at the installed commit {short(installed_commit)}'
    try:
        status = subprocess.run(['git', '-C', str(candidate), 'status', '--porcelain'], capture_output=True,
                                timeout=GIT_TIMEOUT_SECONDS, check=False)
    except (OSError, subprocess.SubprocessError) as error:
        return None, 'framework source state could not be read: ' + str(error)
    if status.returncode or status.stdout.strip():
        return None, 'framework source has uncommitted changes: its tests do not describe the installed files'
    return Path(candidate), None


def failed_report(previous_commit, new_commit, error):
    """The report when the self-check itself could not finish: incomplete, never a success."""
    name = 'self-check completed'
    return {'status': 'INCOMPLETE', 'old_commit': previous_commit or UNKNOWN, 'new_commit': new_commit or UNKNOWN,
            'policy_file_sha256': UNKNOWN, 'checks': [_check(name, FAIL, [f'{type(error).__name__}: {error}'])],
            'failing': [name], 'unavailable': [], 'stale_tasks': [], 'memory_reverify': []}


def cached_source_checks(source_root, checks):
    """Run the source checks, reusing ones that already passed on this exact source tree."""
    key = source_tree_key(source_root) if source_root is not None and (source_root / '.git').exists() else None
    path = source_root / SOURCE_CHECK_CACHE / (key + '.json') if key else None
    try:
        passed = json.loads(path.read_text(encoding='utf-8')) if path else {}
    except (OSError, ValueError):
        passed = {}
    results = []
    for name, check in checks:
        if passed.get(name, {}).get('status') == PASS:
            results.append({**passed[name], 'details': [*passed[name]['details'],
                                                        f'reused: passed on this exact source tree ({short(key)})']})
            continue
        result = check(source_root)
        results.append(result)
        if result['status'] == PASS and path is not None:
            passed[name] = result
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                temporary = path.with_suffix(f'.{os.getpid()}.tmp')
                temporary.write_text(json.dumps(passed), encoding='utf-8')
                os.replace(temporary, path)
            except OSError:
                pass
    return results


def check_fixtures(source_root):
    if source_root is None or not (source_root / 'tests').is_dir():
        return _check('review-quality fixture tests pass', UNAVAILABLE, ['framework source with tests is not available'])
    return _run('review-quality fixture tests pass',
                [sys.executable, '-B', '-m', 'unittest', 'discover', '-s', 'tests', '-p', FIXTURE_TESTS], source_root)


def check_public_content(source_root):
    script = source_root / 'scripts/check_public_content.py' if source_root else None
    if script is None or not script.is_file() or not (source_root / '.git').exists():
        return _check('public-content generic checks pass', UNAVAILABLE, ['framework source checkout is not available'])
    return _run('public-content generic checks pass',
                [sys.executable, '-B', str(script), '--repo', str(source_root), '--no-denylist'], source_root)


def open_project_store(project, state_home=None):
    """(store, note): the project's existing state, never registering a project or creating state.

    The store is None when there is nothing to open, and the note says why; a note starting with
    "unavailable" means state exists but could not be opened."""
    from .projects import ProjectRegistry, resolve_state_home
    from .store import Store
    path = Path(project).expanduser()
    root = path.resolve() if path.is_dir() else None
    try:
        home = resolve_state_home(state_home, project=root)
        if not (home / 'registry.sqlite3').is_file():
            return None, 'no project state yet: nothing to list'
        registry = ProjectRegistry(home, project=root)
        try:
            context = registry.resolve(str(root) if root else str(project))
        finally:
            registry.close()
        return Store(context), None
    except ValueError as error:
        if str(error).startswith('Unknown project or workspace'):
            return None, 'project has no recorded state: nothing to list'
        return None, 'unavailable: ' + str(error)
    except (OSError, sqlite3.Error) as error:
        return None, 'unavailable: ' + str(error)


def registered_projects(state_home=None):
    """(project ids, ids whose directory is gone, note) of the active registrations in a state home.

    Read without registering anything. A registration whose workspace directory no longer exists
    has no project left to update, so it is counted, not opened."""
    from .projects import ProjectRegistry, resolve_state_home
    try:
        home = resolve_state_home(state_home)
        if not (home / 'registry.sqlite3').is_file():
            return [], [], None
        registry = ProjectRegistry(home)
        try:
            rows = registry.db.execute(
                "SELECT p.project_id, w.root FROM projects p JOIN workspaces w "
                "ON w.workspace_id = p.current_workspace_id WHERE p.status='ACTIVE' ORDER BY p.project_id").fetchall()
        finally:
            registry.close()
    except (ValueError, OSError, sqlite3.Error) as error:
        return [], [], 'unavailable: ' + str(error)
    present = [row['project_id'] for row in rows if Path(row['root']).is_dir()]
    return present, [row['project_id'] for row in rows if row['project_id'] not in present], None


def check_project_state(project, state_home, current, package_root):
    """The open tasks with stale verdicts and the notes to re-verify, as a check plus its two lists.

    With no project (a shared runtime update), every project registered in the state home is read."""
    from .__main__ import parser
    from .memory.reverify import inventory, reverify
    name = 'stale verdicts and notes to re-verify listed'
    selected, gone, problem = ([project], [], None) if project else registered_projects(state_home)
    try:
        framework = inventory(package_root, parser())
    except (OSError, ValueError, sqlite3.Error) as error:
        return _check(name, UNAVAILABLE, ['framework inventory could not be read: ' + str(error)]), [], []
    stale, notes, details = [], [], [problem] if problem else []
    if gone:
        details.append(f'{len(gone)} registered project(s) skipped: their directory no longer exists')
    for selector in selected:
        try:
            store, note = open_project_store(selector, state_home)
        except Exception as error:  # A locked or corrupt project database must not hide the others.
            store, note = None, f'unavailable: {type(error).__name__}: {error}'
        if store is None:
            details.append(f'{selector}: {note}')
            problem = problem or (note if note.startswith('unavailable') else None)
            continue
        try:
            label = {'project': str(store.repo)} if not project else {}
            found = [{**row, **label} for row in open_tasks_with_stale_verdicts(store, current=current)]
            listed = [{**row, **label} for row in reverify(store, framework, current_commit=current['framework_commit'])]
        except Exception as error:  # Unreadable records leave this project unlisted, and say so.
            note = f'unavailable: {type(error).__name__}: {error}'
            details.append(f'{selector}: {note}')
            problem = problem or note
            continue
        finally:
            store.close()
        stale.extend(found)
        notes.extend(listed)
    details.append(f'{len(selected)} project(s) read: {len(stale)} open task(s) with stale verdicts, '
                   f'{len(notes)} note(s) to re-verify')
    return _check(name, UNAVAILABLE if problem else PASS, details), stale, notes


def run(*, home=None, package_root=None, source_root=None, project=None, previous_commit=None, run_tests=True,
        state_home=None, source_note=None):
    """Check one installation (home) or a framework source checkout and report the update status."""
    package_root = Path(package_root or Path(__file__).resolve().parents[1])
    record = installation(home) if home else None
    current = stamp(commit=(record or {}).get('framework_commit') or source_commit(),
                    policy_path=package_root / POLICY)
    checks = []
    if home is not None:
        checks.append(check_installed_files(home))
        if record is not None and record.get('framework_dirty') is not False:
            # Copied from uncommitted changes (or not recorded): no commit is known to describe these files.
            checks.append(_check('installed from a clean framework commit', UNAVAILABLE, [
                f'installed from uncommitted changes on top of {short(current["framework_commit"])}'
                if record.get('framework_dirty') else 'not recorded: installed before this check existed']))
    mirrors = [Path(path) for path in (record or {}).get('skill_destinations', {}).values() if path]
    if source_root is not None and package_root == source_root:
        mirrors.extend(source_root / mirror for mirror in SKILL_MIRRORS)
    checks.append(check_mirrors(package_root / 'skills', mirrors))
    checks.append(check_policy(package_root))
    if source_root is not None and package_root != source_root:
        # The installed policy must be the one the source builds, not only self-consistent.
        if policy_file_sha256(source_root / POLICY) != current['policy_file_sha256']:
            checks.append(_check('installed policy is the source policy', FAIL,
                                 ['installed runtime_policy.json differs from the framework source']))
    if run_tests and source_root is None and source_note:
        checks.extend(_check(name, UNAVAILABLE, [source_note])
                      for name in ('review-quality fixture tests pass', 'public-content generic checks pass'))
    elif run_tests:
        checks.extend(cached_source_checks(source_root, [('fixtures', check_fixtures),
                                                         ('public content', check_public_content)]))
    project_check, stale, notes = check_project_state(project or (record or {}).get('project'), state_home, current,
                                                      package_root)
    checks.append(project_check)
    failing = [row['name'] for row in checks if row['status'] == FAIL]
    unavailable = [row['name'] for row in checks if row['status'] == UNAVAILABLE]
    report = {'status': 'INCOMPLETE' if failing else 'UNVERIFIED' if unavailable else 'COMPLETE',
              'old_commit': previous_commit or UNKNOWN, 'new_commit': current['framework_commit'],
              'policy_file_sha256': current['policy_file_sha256'], 'checks': checks,
              'failing': failing, 'unavailable': unavailable, 'stale_tasks': stale, 'memory_reverify': notes}
    return report


def text(report):
    lines = [f"Framework update {short(report['old_commit'])} -> {short(report['new_commit'])}: {report['status']}"]
    for row in report['checks']:
        lines.append(f"  [{row['status']}] {row['name']}")
        lines.extend('      ' + detail for detail in (row['details'] if row['status'] != PASS else []))
    if report['status'] != 'COMPLETE':
        lines.append('  The update is not complete: ' + ', '.join(report['failing'] + report['unavailable']))
    for task in report['stale_tasks']:
        lines.append(f"  Open task {task['task_id']} ({task['state']}) has stale verdicts:")
        lines.extend('      ' + row['reason'] for row in task['stale'])
    for note in report['memory_reverify']:
        lines.append(f"  Re-verify {note['note']} (written under {note['written_under']}): "
                     + ', '.join(note['references']) + ' -- ' + note['action'])
    return '\n'.join(lines)
