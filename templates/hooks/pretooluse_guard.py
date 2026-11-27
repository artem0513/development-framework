"""Claude Code PreToolUse guard for Bash: a local accident barrier, not a security boundary.

Denies pushes to the default branch, force pushes, --no-verify, `gh pr merge` and disabling
branch protection. With --ci-base REF it also runs `ase ci-check` before `git push` and
`gh pr create`: FAIL findings deny, owner-review findings are reported. An agent with shell access
can still bypass any local hook; server-side branch protection is the real enforcement.
"""
import argparse
import json
import os
import re
import shlex
import subprocess
import sys

PROTECTED_BRANCHES = {'main', 'master'}


def _emit(decision):
    kind, message = decision
    if kind == 'deny':
        print(json.dumps({'hookSpecificOutput': {'hookEventName': 'PreToolUse',
                                                 'permissionDecision': 'deny', 'permissionDecisionReason': message}}))
    else:
        # Owner-review items are reported, not blocked: the owner decides them on the PR.
        print(json.dumps({'systemMessage': message}))
    return 0


def _split(command):
    """Split on && || ; | newline and at $( or ` outside single quotes and escapes, as the shell would.

    Quoted text such as a PR body mentioning `git push --force` stays inside its word; a real
    command substitution inside double quotes still starts a command.
    """
    parts, current, quote, i = [], [], None, 0
    while i < len(command):
        ch = command[i]
        if quote == "'":
            current.append(ch)
            quote = None if ch == "'" else quote
        elif ch == '\\':
            current.append(command[i:i + 2])
            i += 1
        elif ch in '"\'' and quote is None:
            quote = ch
            current.append(ch)
        elif ch == '"' and quote == '"':
            quote = None
            current.append(ch)
        elif ch == '`' or command.startswith('$(', i):
            parts.append(''.join(current))
            current = []
            i += 1 if ch == '$' else 0
        elif quote is None and ch in ';|&\n':
            parts.append(''.join(current))
            current = []
            i += 1 if command.startswith(('&&', '||'), i) else 0  # a lone & (background) also separates
        else:
            current.append(ch)
        i += 1
    parts.append(''.join(current))
    return parts


def _segments(command):
    """Shell-like segments so `cd x && git push` and `$(git push ...)` are still seen."""
    for part in _split(command):
        try:
            words = shlex.split(part, posix=True)
        except ValueError:
            words = part.split()
        # Words may keep quote or subshell residue from the fallback split; strip it.
        words = [w.lstrip('$').strip('()"\'`') for w in words]
        words = [w for w in words if w]
        if words:
            yield words


def _current_branch(cwd):
    done = subprocess.run(['git', 'symbolic-ref', '--quiet', '--short', 'HEAD'], cwd=cwd, capture_output=True, text=True)
    return done.stdout.strip() if done.returncode == 0 else ''


def _git_words(words):
    """Drop git's global options (`-C dir`, `-c k=v`, `--git-dir=x`, `--no-pager`) before the verb."""
    i = 1
    while i < len(words) and words[i].startswith('-'):
        i += 2 if words[i] in {'-C', '-c', '--git-dir', '--work-tree', '--namespace'} else 1
    return ['git', *words[i:]]


def check(command, cwd, ci_base=None, framework=None, depth=0):
    """Return ('deny'|'warn', message) or None."""
    for words in _segments(command):
        while words and re.fullmatch(r'\w+=.*', words[0]):
            words = words[1:]
        if len(words) >= 3 and words[0] in {'bash', 'sh', 'zsh', 'pwsh', 'powershell'} and words[1] in {'-c', '-lc', '-Command'} and depth < 3:
            nested = check(' '.join(words[2:]), cwd, ci_base, framework, depth + 1)
            if nested:
                return nested
            continue
        if words[:1] == ['git']:
            words = _git_words(words)
        if len(words) < 2:
            continue
        tool, verb, rest = words[0], words[1], words[2:]
        if tool == 'git' and '--no-verify' in words:
            return 'deny', 'Hooks may not be bypassed with --no-verify.'
        if tool == 'git' and verb == 'push':
            if any(w in {'-f', '--force', '--force-with-lease', '--mirror', '--delete', '-d'} or w.startswith('+')
                   or w.startswith('--force') for w in rest):
                return 'deny', 'Force/mirror/delete pushes need the owner; ask first.'
            if '--all' in rest or '--branches' in rest:
                return 'deny', 'Pushing all branches includes the default branch; push the feature branch by name.'
            positional = [w for w in rest if not w.startswith('-')]
            refs = {w.split(':')[-1].removeprefix('refs/heads/') for w in positional[1:]}
            if not refs or 'HEAD' in refs:
                refs.add(_current_branch(cwd))
            if refs & PROTECTED_BRANCHES:
                return 'deny', 'Pushing to the default branch needs explicit owner approval.'
            if ci_base:
                return _ci(cwd, ci_base, framework)
        if tool == 'gh' and verb == 'pr' and rest[:1] == ['merge']:
            return 'deny', 'Merging a PR needs explicit owner approval.'
        if tool == 'gh' and verb == 'pr' and rest[:1] == ['create'] and ci_base:
            return _ci(cwd, ci_base, framework)
        if (tool == 'gh' and verb == 'api' and any('protection' in w or 'rulesets' in w for w in rest)
                and any(w in {'-X', '--method'} for w in rest)):
            return 'deny', 'Changing branch protection is an owner action.'
    return None


def _ci(cwd, base, framework):
    """Deny on FAIL findings and on a check that cannot run or evaluate; report owner-review items."""
    env = {**os.environ, 'PYTHONUTF8': '1', 'PYTHONIOENCODING': 'utf-8'}  # child JSON must be UTF-8
    if framework:
        env['PYTHONPATH'] = framework + os.pathsep + env.get('PYTHONPATH', '')
    try:
        done = subprocess.run([sys.executable, '-m', 'ase', '--repo', cwd, 'ci-check', '--base', base],
                              cwd=cwd, capture_output=True, env=env, timeout=180)
    except (OSError, subprocess.SubprocessError) as error:
        return 'deny', 'ase ci-check did not finish (' + type(error).__name__ + '); fix the check setup or ask the owner.'
    try:
        result = json.loads(done.stdout.decode('utf-8'))
    except ValueError:
        result = {}
    findings = result.get('findings') if isinstance(result, dict) else None
    if not isinstance(findings, list):
        # An unreadable or BLOCKED result is not a pass: the configured check must run.
        return 'deny', ('ase ci-check could not evaluate this change: '
                        + (done.stdout + done.stderr).decode('utf-8', 'replace')[-1500:])
    summary = '\n'.join(f"- {f['level']} {f['check']}: {f['message']} {', '.join(f['paths'][:10])}" for f in findings)
    if any(f['level'] == 'FAIL' for f in findings):
        return 'deny', 'ase ci-check failed; fix these first:\n' + summary
    if any(f['level'] == 'REVIEW' for f in findings):
        return 'warn', 'ase ci-check: owner review needed on the PR:\n' + summary
    return None


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--ci-base', help='Base ref for ase ci-check before push/PR, e.g. origin/main')
    parser.add_argument('--framework', help='Path to the DevelopmentFramework checkout providing `ase`')
    args = parser.parse_args(argv)
    try:
        # Claude Code sends UTF-8; the Windows console code page must not decode it.
        event = json.loads(sys.stdin.buffer.read().decode('utf-8-sig'))
    except (ValueError, AttributeError):
        return 0
    if not isinstance(event, dict) or event.get('tool_name') != 'Bash':
        return 0
    command = (event.get('tool_input') or {}).get('command') or ''
    decision = check(command, event.get('cwd') or '.', args.ci_base, args.framework)
    return _emit(decision) if decision else 0


if __name__ == '__main__':
    sys.exit(main())
