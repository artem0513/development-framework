"""Install opt-in Claude Code hooks into ONE project's .claude/settings.json.

- PreToolUse(Bash): pretooluse_guard.py denies default-branch/force pushes, --no-verify and PR
  merges; with --ci-base it runs `ase ci-check` before push/PR creation.
- UserPromptSubmit: prompt_capture.py (`ase prompt capture`) records each owner prompt so approvals can cite
  `prompt:<id>`.

User-level settings are never touched. Existing project hooks are preserved; an entry that
already runs the same command is not added twice. Hooks are an accident barrier: server-side
branch protection remains the enforcement boundary.
"""
import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GUARD = ROOT / 'templates' / 'hooks' / 'pretooluse_guard.py'
CAPTURE = ROOT / 'templates' / 'hooks' / 'prompt_capture.py'


def _quote(value):
    value = str(value)
    if any(ch in value for ch in '"$`%'):
        raise SystemExit('Paths with quotes, $, backticks or % are not supported in hook commands: ' + value)
    return '"' + value + '"'


def commands(repo, ci_base=None, python=sys.executable):
    guard = [_quote(python), _quote(GUARD), '--framework', _quote(ROOT)]
    if ci_base:
        guard += ['--ci-base', _quote(ci_base)]
    # Interpreter + script + quoted args only: no cd/&&/env syntax for the hook shell to interpret.
    capture = [_quote(python), _quote(CAPTURE), '--repo', _quote(repo)]
    return ' '.join(guard), ' '.join(capture)


def _add(hooks, event, matcher, command):
    entries = hooks.setdefault(event, [])
    if any(h.get('command') == command for entry in entries for h in entry.get('hooks', [])):
        return False
    entry = {'hooks': [{'type': 'command', 'command': command, 'timeout': 200}]}
    if matcher:
        entry['matcher'] = matcher
    entries.append(entry)
    return True


def install(repo, *, ci_base=None, guard=True, prompts=True, python=sys.executable):
    repo = Path(repo).resolve(strict=True)
    subprocess.run(['git', '-C', str(repo), 'rev-parse', '--show-toplevel'], check=True, capture_output=True)
    path = repo / '.claude' / 'settings.json'
    settings = json.loads(path.read_text(encoding='utf-8-sig')) if path.exists() else {}
    if not isinstance(settings, dict) or not isinstance(settings.get('hooks', {}), dict):
        raise SystemExit('Unexpected .claude/settings.json shape; integrate hooks manually.')
    hooks = settings.setdefault('hooks', {})
    guard_command, capture_command = commands(repo, ci_base, python)
    added = []
    if guard and _add(hooks, 'PreToolUse', 'Bash', guard_command):
        added.append('PreToolUse')
    if prompts and _add(hooks, 'UserPromptSubmit', None, capture_command):
        added.append('UserPromptSubmit')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(settings, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return {'settings': str(path), 'added': added}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--repo', type=Path, required=True)
    parser.add_argument('--ci-base', help='Run ase ci-check against this base before push/PR, e.g. origin/main')
    parser.add_argument('--no-guard', action='store_true')
    parser.add_argument('--no-prompt-capture', action='store_true')
    args = parser.parse_args(argv)
    print(json.dumps(install(args.repo, ci_base=args.ci_base, guard=not args.no_guard,
                             prompts=not args.no_prompt_capture), indent=2))


if __name__ == '__main__':
    main()
