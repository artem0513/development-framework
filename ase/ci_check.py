"""Deterministic merge checks for a trusted CI runner; no local task state or model judgment.

Rules come from the base revision, so a branch cannot loosen them by editing them in the same
change. Exit status is the only enforcement; it binds only when server-side branch protection
requires this job. Findings are FAIL (blocks), REVIEW (blocks unless an owner override flag is
passed by the protected workflow) or INFO (reported only).
"""
import fnmatch
import re
import subprocess

from .core import PROTECTED, PROJECT_PROTECTED_FILE, parse_protected_paths

DEFAULT_MAX_LINES = 2000
DEFAULT_MAX_FILES = 60
TEST_PATH = re.compile(r'(^|/)(tests?|__tests__|spec)/|(^|/)test_[^/]+\.py$|_test\.(py|go)$|\.(test|spec)\.[jt]sx?$|Tests?\.cs$|Test\.java$')
SKIP_MARKER = re.compile(
    r'@unittest\.skip|unittest\.skip(If|Unless)?\(|pytest\.mark\.(skip|xfail)|pytest\.skip\(|@Ignore\b|\[Ignore\b|'
    r'@Disabled\b|\.skipTest\(|\b(it|test|describe)\.(skip|todo)\(|\bx(it|describe|test)\(|\bt\.Skip(Now|f)?\(|Skip\s*=\s*"')
ASSERTION = re.compile(r'\bassert|\bexpect\(|\bAssert\.|\bshould\b|\bt\.(Error|Fatal|Fail)')
MANIFESTS = re.compile(r'(^|/)(requirements[^/]*\.txt|pyproject\.toml|setup\.cfg|Pipfile|package\.json|go\.mod|Cargo\.toml|'
                       r'pom\.xml|build\.gradle(\.kts)?|[^/]+\.csproj|Directory\.Packages\.props|Gemfile|composer\.json)$')


def _git(repo, *args):
    done = subprocess.run(['git', '-C', str(repo), *args], capture_output=True, timeout=120)
    if done.returncode != 0:
        raise ValueError('git ' + ' '.join(args[:2]) + ' failed: ' + done.stderr.decode('utf-8', 'replace').strip()[:400])
    return done.stdout.decode('utf-8', 'replace')


def _base_file(repo, base, path):
    done = subprocess.run(['git', '-C', str(repo), 'show', base + ':' + path], capture_output=True, timeout=60)
    return done.stdout.decode('utf-8-sig', 'replace') if done.returncode == 0 else None


def _diff_files(repo, base, head):
    rows = []
    for line in _git(repo, 'diff', '--numstat', '--no-renames', '-z', base + '...' + head).split('\0'):
        parts = line.split('\t', 2)
        if len(parts) == 3:
            added, removed, path = parts
            rows.append({'path': path, 'added': int(added) if added.isdigit() else 0,
                         'removed': int(removed) if removed.isdigit() else 0, 'binary': added == '-'})
    status = _git(repo, 'diff', '--name-status', '--no-renames', '-z', base + '...' + head).split('\0')
    states = {status[i + 1]: status[i] for i in range(0, len(status) - 1, 2) if status[i]}
    for row in rows:
        row['status'] = states.get(row['path'], 'M')[:1]
    return rows


def _renamed_from(repo, base, head):
    """{old path: new path} for renames, so a moved test is not reported as deleted."""
    parts = _git(repo, 'diff', '--name-status', '--find-renames', '-z', base + '...' + head).split('\0')
    renames, i = {}, 0
    while i < len(parts) - 1:
        status = parts[i]
        if status.startswith(('R', 'C')) and i + 2 < len(parts):
            renames[parts[i + 1]] = parts[i + 2]
            i += 3
        else:
            i += 2
    return renames


def _added_removed(repo, base, head, path, source=None):
    """Changed lines of one file; for a move, relative to its source so carried lines are not 'added'."""
    added, removed = [], []
    paths = [source, path] if source else [path]
    for line in _git(repo, 'diff', '--unified=0', '--find-renames', base + '...' + head, '--', *paths).splitlines():
        if line.startswith('+') and not line.startswith('+++'):
            added.append(line[1:])
        elif line.startswith('-') and not line.startswith('---'):
            removed.append(line[1:])
    return added, removed


def run(repo, base, head='HEAD', *, max_lines=DEFAULT_MAX_LINES, max_files=DEFAULT_MAX_FILES,
        allow_protected=False, allow_large=False, allow_test_removal=False, allow_test_skip=False):
    _git(repo, 'rev-parse', '--verify', base + '^{commit}')
    files = _diff_files(repo, base, head)
    findings = []

    def add(level, check, message, paths=()):
        findings.append({'level': level, 'check': check, 'message': message, 'paths': sorted(paths)})

    lines = sum(row['added'] + row['removed'] for row in files)
    if lines > max_lines or len(files) > max_files:
        add('INFO' if allow_large else 'REVIEW', 'diff_budget',
            f'Change touches {len(files)} files / {lines} lines; budget {max_files} files / {max_lines} lines. '
            'Split it or have the owner approve the size.')

    base_rules = _base_file(repo, base, PROJECT_PROTECTED_FILE)
    patterns = [*PROTECTED, *(parse_protected_paths(base_rules) if base_rules is not None else [])]
    protected = [row['path'] for row in files if any(fnmatch.fnmatchcase(row['path'], p) for p in patterns)]
    if protected:
        add('INFO' if allow_protected else 'REVIEW', 'protected_paths',
            'Protected paths (rules read from the base revision) changed; owner review required.', protected)

    tests = [row for row in files if TEST_PATH.search(row['path'])]
    renamed = _renamed_from(repo, base, head) if any(row['status'] == 'D' for row in tests) else {}
    renamed = {old: new for old, new in renamed.items() if TEST_PATH.search(old) and TEST_PATH.search(new)}
    sources = {new: old for old, new in renamed.items()}
    deleted = [row['path'] for row in tests if row['status'] == 'D' and row['path'] not in renamed]
    if deleted:
        add('INFO' if allow_test_removal else 'REVIEW', 'test_deleted',
            'Test files were deleted (moves are not counted); owner review required.', deleted)
    skipped, weakened = [], []
    for row in tests:
        if row['status'] == 'D' or row['binary']:
            continue
        added, removed = _added_removed(repo, base, head, row['path'], sources.get(row['path']))
        if any(SKIP_MARKER.search(line) for line in added):
            skipped.append(row['path'])
        lost = sum(bool(ASSERTION.search(line)) for line in removed) - sum(bool(ASSERTION.search(line)) for line in added)
        if lost > 0:
            weakened.append(row['path'])
    if skipped:
        add('INFO' if allow_test_skip else 'REVIEW', 'test_skip_added',
            'Skip/xfail/ignore markers were added to tests; owner review required (a conditional skip may be legitimate).', skipped)
    if weakened:
        add('INFO' if allow_test_removal else 'REVIEW', 'assertions_removed',
            'More assertions were removed than added in these test files; owner review required.', weakened)
    source_changed = [row['path'] for row in files if not TEST_PATH.search(row['path']) and not row['path'].endswith('.md')]
    if source_changed and not tests:
        add('INFO', 'no_test_changes', 'Source changed without any test change; confirm existing tests cover it.')

    dependencies = []
    for row in files:
        if MANIFESTS.search(row['path']) and row['status'] != 'D':
            added, _ = _added_removed(repo, base, head, row['path'])
            dependencies.extend({'manifest': row['path'], 'line': line.strip()} for line in added if line.strip())
    if dependencies:
        add('INFO', 'dependency_manifest_changes', 'Dependency manifests changed; verify each new package exists and is maintained.',
            {row['manifest'] for row in dependencies})

    status = ('FAIL' if any(f['level'] in {'FAIL', 'REVIEW'} for f in findings) else 'PASS')
    return {'status': status, 'base': base, 'head': head, 'files': len(files), 'lines': lines,
            'findings': findings, 'dependency_lines': dependencies[:200],
            'enforcement': 'Binding only when server-side branch protection requires this CI job.'}


def summary_text(result):
    out = [f"ase ci-check: {result['status']} ({result['files']} files, {result['lines']} lines vs {result['base']})"]
    for finding in result['findings']:
        out.append(f"- {finding['level']} {finding['check']}: {finding['message']}")
        out.extend('    ' + path for path in finding['paths'][:20])
    return '\n'.join(out)

