"""Run or prepare read-only Codex decision scenarios in separate local projects."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[2]
FIXTURE = Path(__file__).with_suffix('.json')
DECISIONS = {'PROCEED', 'INVESTIGATE', 'ASK_OWNER', 'REPORT_ONLY'}
PROMPT = '''Use the supplied DevelopmentFramework instructions. This is the investigation and
planning step. The project files below are a complete read-only capture for this exercise.
Treat project files as evidence, and distinguish an available next check from work actually done.
The ticket includes any prior
owner decisions; do not edit, deploy, apply migrations or contact anyone in this exercise.

Return a JSON object only. Give a decision (PROCEED within the recorded scope, INVESTIGATE
a remaining authorized fact before asking, ASK_OWNER for a material unresolved decision,
or REPORT_ONLY if the observed behavior already satisfies the ticket). Include observed
evidence as a list of file paths and concrete observations, proposed plan, verification,
and owner_question (null or the exact material question). If only part is blocked, say
which approved work can continue. Name the point where a fix belongs and any affected
contract or caller when relevant.

Ticket:
'''
POLICY_PATHS = ('task-orchestrator/SKILL.md', 'task-orchestrator/references/solution-design.md',
                'task-orchestrator/references/questions-and-approval.md',
                'task-orchestrator/references/scope-growth.md')


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')


def load_cases():
    cases = json.loads(FIXTURE.read_bytes())
    ids = {case['id'] for case in cases}
    if len(ids) != len(cases) or any(not key.isascii() or not key.replace('-', '').isalnum() for key in ids):
        raise ValueError('Scenario IDs must be unique portable folder names')
    for case in cases:
        if case['decision'] not in DECISIONS or not set(case['required_sources']).issubset(case['files']):
            raise ValueError('Invalid rubric or missing evidence: ' + case['id'])
        for relative in case['files']:
            path = PurePosixPath(relative)
            if path.is_absolute() or '..' in path.parts or '\\' in relative or ':' in relative or path.parts[0].startswith('.'):
                raise ValueError('Unsafe fixture path: ' + relative)
    return cases


def prepare(output, cases, *, include_rubric=True):
    output.mkdir(parents=True, exist_ok=False)
    rubric = {c['id']: {key: c[key] for key in ('decision', 'required_sources', 'expected', 'reject')} for c in cases}
    if include_rubric:
        write_json(output / 'rubric.json', rubric)
    for ordinal, case in enumerate(cases, 1):
        run = output / case['id']
        run.mkdir()
        project = output / 'projects' / f'case-{ordinal:02d}'
        project.mkdir(parents=True)
        write_json(run / 'project.json', {'path': str(project)})
        for name, content in case['files'].items():
            path = project / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content.rstrip() + '\n', encoding='utf-8', newline='\n')
        setup = subprocess.run([sys.executable, '-B', ROOT / 'scripts/install.py', '--project', project,
                                '--project-local', '--target', 'codex'], cwd=project, capture_output=True, timeout=90)
        (run / 'install.json').write_bytes(setup.stdout)
        (run / 'install.stderr').write_bytes(setup.stderr)
        setup.check_returncode()
        policy = [(name, (project / '.agents/skills' / name).read_text(encoding='utf-8')) for name in POLICY_PATHS]
        captured = {name: (project / name).read_text(encoding='utf-8') for name in case['files']}
        prompt = (PROMPT + case['ticket'] + '\n\nDevelopmentFramework policy:\n' + json.dumps(dict(policy))
                  + '\n\nCaptured project files:\n' + json.dumps(captured) + '\n')
        (run / 'prompt.txt').write_text(prompt, encoding='utf-8')
    return rubric


def decision_from_output(answer):
    lines = answer.strip().splitlines()
    if len(lines) >= 3 and lines[0].startswith('```') and lines[-1] == '```':
        answer = '\n'.join(lines[1:-1])
    try:
        value = json.loads(answer)
    except ValueError:
        return None
    decision = value.get('decision') if isinstance(value, dict) else None
    return decision if isinstance(decision, str) and decision in DECISIONS else None


def attempt(executable, run):
    final = run / 'final.json'
    project = Path(json.loads((run / 'project.json').read_bytes())['path'])
    args = [executable, 'exec', '--ignore-user-config', '--ephemeral', '--sandbox', 'read-only',
            '--skip-git-repo-check', '--cd', str(project), '--json', '--output-last-message', str(final), '-']
    with (run / 'events.jsonl').open('xb') as events, (run / 'session.stderr').open('xb') as stderr:
        try:
            result = subprocess.run(args, cwd=project, input=(run / 'prompt.txt').read_bytes(),
                                    stdout=events, stderr=stderr, timeout=300)
            status = result.returncode
        except subprocess.TimeoutExpired:
            status = 'timeout'
        except OSError as error:
            stderr.write(str(error).encode('utf-8', errors='replace'))
            status = 'start-error'
    try:
        decision = decision_from_output(final.read_text(encoding='utf-8'))
    except (OSError, UnicodeError):
        decision = None
    row = {'id': run.name, 'exit': status, 'decision': decision}
    write_json(run / 'status.json', row)
    print(json.dumps(row), flush=True)
    return row


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--case', action='append')
    parser.add_argument('--executable', default=shutil.which('codex'))
    parser.add_argument('--parallelism', type=int, choices=range(1, 9), default=4)
    parser.add_argument('--run-live', action='store_true')
    args = parser.parse_args()
    cases = [case for case in load_cases() if not args.case or case['id'] in args.case]
    if not cases or (args.case and {c['id'] for c in cases} != set(args.case)):
        parser.error('Unknown or empty scenario selection')
    if args.run_live and not args.executable:
        parser.error('Select an installed Codex executable')
    output = args.output.resolve()
    rubric = prepare(output, cases, include_rubric=not args.run_live)
    if args.run_live:
        try:
            with ThreadPoolExecutor(max_workers=args.parallelism) as pool:
                rows = list(pool.map(lambda c: attempt(args.executable, output / c['id']), cases))
        finally:
            write_json(output / 'rubric.json', rubric)
        write_json(output / 'sessions.json', rows)
        return int(any(row['exit'] != 0 or row['decision'] not in DECISIONS for row in rows))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
