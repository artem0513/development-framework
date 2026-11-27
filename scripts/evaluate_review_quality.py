"""Run independent review on paired, executable candidate patches in disposable repositories.

Each case's variants carry an expectation the reviewer never sees (it stays in the output
folder's rubric, outside every reviewed workspace and packet): `block` (a blocking finding at
the named path), `note` (any finding there) or `pass` (no blocking finding). With
`cite_precedent` a finding only counts when it cites a local precedent with counts ("4 of 4").
A case may name a `project` folder under --projects-root: a read-only copy of that project
becomes the workspace before the case files are written over it.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
import re
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ase.adapters import get_adapter
from ase.core import test_counts
from ase.profiles import evaluate_contract_profiles
from ase.protocol import decode_json
from ase.review.schema import REVIEW_SCHEMA_V2
from ase.runner import Runner, verify_receipt
from ase.store import Store
from ase.usage import session_usage
from ase.workflow import WorkflowController
from scripts.evaluate_production_reasoning import authorize, contract_for, initialize, save


CASES = ROOT / 'tests/fixtures/review-quality-cases.json'
BLOCKING = {'P0', 'P1', 'P2'}
# "154 of 191 handlers", "10 из 10 вызовов": a local precedent stated with counts.
PRECEDENT = re.compile(r'\b\d+\s+(?:of|out of|из)\s+\d+\b', re.IGNORECASE)
# Copying a project never brings its history, private framework state, build output or other agents' files.
PROJECT_EXCLUDED = ('.git', '.devframework', '.agents', '.claude', '.codex', '.github', 'bin', 'obj',
                    'node_modules', '__pycache__', '.vs', 'evidence', 'AGENTS.override.md')
REVIEW_INSTRUCTION = ('Independently review actual source for actionable defects P0-P3. '
                      'P0/P1/P2 block. Report material missing evidence in unverified_material.')


def commit_baseline(target):
    git = ['git', '-c', 'core.autocrlf=false', '-c', 'user.name=Fixture Owner',
           '-c', 'user.email=fixture@example.invalid', '-c', 'core.hooksPath=' + str(target / '.git/no-hooks')]
    for args in (['add', '--', '.'], ['commit', '--quiet', '--no-gpg-sign', '-m', 'Synthetic baseline']):
        subprocess.run([*git, *args], cwd=target, stdin=subprocess.DEVNULL,
                       capture_output=True, check=True, timeout=30)


def initialize_case(folder, case, projects_root=None):
    if not case.get('project'):
        return initialize(folder, case)
    if projects_root is None:
        raise ValueError(f'{case["id"]}: --projects-root is required for a project case')
    source = (Path(projects_root) / case['project']).resolve()
    if not source.is_dir() or not source.is_relative_to(Path(projects_root).resolve()):
        raise ValueError(f'{case["id"]}: unknown project folder')
    target = folder / 'target'
    shutil.copytree(source, target, ignore=shutil.ignore_patterns(*PROJECT_EXCLUDED))
    for relative, content in case['files'].items():
        path = target / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding='utf-8', newline='')
    if subprocess.run(['git', 'init', '--quiet', str(target)], stdin=subprocess.DEVNULL,
                      capture_output=True, timeout=30).returncode:
        raise ValueError('Could not initialize disposable local Git metadata')
    return target


def prepare_candidate(output, number, case, variant, *, projects_root=None, blind=False):
    """The review request for one candidate; with `blind`, (scope request, blind request)."""
    folder = output / 'cases' / f'{number:02d}'
    target = initialize_case(folder, case, projects_root)
    commit_baseline(target)
    stage = case['contract']['stages'][0]['id']
    with closing(Store(target, output / f's{number:02d}')) as store:
        contract = contract_for(case)
        if blind:
            contract['blind_review'] = {'version': 1}
        task_id = authorize(store, contract, dependencies=case['files'])
        controller = WorkflowController(store)
        before = controller.check(task_id, stage)
        save(folder / 'baseline-checks.json', before)
        for name, content in variant['changes'].items():
            path = target / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding='utf-8', newline='')
        after = controller.check(task_id, stage)
        save(folder / 'candidate-checks.json', after)
        if ([result['exit_code'] for result in before['results']] != [1]
                or before['source_changed_during_checks'] or after['source_changed_during_checks']):
            raise ValueError(f'{case["id"]}: expected a failing baseline and green candidate')
        before_log = Path(before['results'][0]['log']).read_text(encoding='utf-8')
        profile = evaluate_contract_profiles(contract, contract['evidence'], after['results'], stage_id=stage)
        if not test_counts(before_log, 'unittest')['failed'] or profile['status'] != 'PASS':
            raise ValueError(f'{case["id"]}: expected a failing baseline and green candidate')
        scope = controller._request(task_id, 'review', REVIEW_SCHEMA_V2, {
            'stage_id': stage, 'instruction': REVIEW_INSTRUCTION,
            'evaluation_observations': {
                'before_check': {'fingerprint': before['fingerprint'], 'exit_code': before['results'][0]['exit_code'],
                                 'log': before_log},
                'repository': 'One local baseline commit and this working-tree change. '
                              'The harness creates no remote, candidate branch or pull request.'}})
        if not blind:
            return scope
        from ase.review import blind as blind_review
        return scope, controller._request(task_id, 'review', REVIEW_SCHEMA_V2, {
            'stage_id': stage, 'purpose': blind_review.PURPOSE, 'instruction': blind_review.INSTRUCTION})


def score(payloads, expectation):
    """found (None for a pass variant) and false positives, over the merged findings of one candidate's passes."""
    findings = [finding for payload in payloads if isinstance(payload, dict)
                for finding in payload.get('findings') or [] if isinstance(finding, dict)]
    unverified = [item for payload in payloads if isinstance(payload, dict) for item in payload.get('unverified_material') or []]
    expected_path = expectation.get('path')

    def located(finding):
        path = str(finding.get('path') or '').replace('\\', '/').removeprefix('./')
        return expected_path is None or path == expected_path or path.endswith('/' + expected_path)

    def cited(finding):
        text = ' '.join(str(finding.get(name) or '') for name in ('message', 'evidence', 'consequence', 'fix'))
        return not expectation.get('cite_precedent') or PRECEDENT.search(text) is not None

    blocking = [finding for finding in findings if finding.get('severity') in BLOCKING]
    disposition = expectation['disposition']
    if disposition == 'pass':
        return {'found': None, 'false_positives': len(blocking) + len(unverified)}
    candidates = blocking if disposition == 'block' else findings
    found = any(located(finding) and cited(finding) for finding in candidates)
    return {'found': found, 'false_positives': len([finding for finding in blocking if not located(finding)]) + len(unverified)}


def summarize(rows, rubric):
    """Per case: found and missed runs on finding variants, false positives on every variant."""
    summary = {}
    for row in rows:
        entry = summary.setdefault(row['case'], {'found': 0, 'missed': 0, 'false_positives': 0, 'runs': 0,
                                                 'unverified_runs': 0})
        entry['runs'] += 1
        if not row['verified']:
            entry['unverified_runs'] += 1
            continue
        result = score(row['payloads'], rubric[str(row['number'])])
        entry['false_positives'] += result['false_positives']
        if result['found'] is True:
            entry['found'] += 1
        elif result['found'] is False:
            entry['missed'] += 1
    return summary


def run_request(folder, executable, backend, request, label):
    trusted = folder / ('trusted-' + label)
    trusted.mkdir()
    run = Runner({'executable': str(executable), 'trusted_cwd': str(trusted)}, folder / ('runs-' + label)).run(
        get_adapter(backend), request, timeout=360)
    receipt = run['receipt']
    verified = verify_receipt(receipt, expected_bindings=request['bindings'])['valid']
    return verified, (session_usage(receipt.get('terminal')) if verified else None), run['result']


def reconciled_scope_request(output, number, case, request, blind_findings, blind_sha256):
    """The scope request as the runtime builds it after the blind pass: with that pass's blocking findings."""
    from ase.review import blind as blind_review
    reserved = {'runtime_policy', 'task_proposal'}
    handoff = {key: value for key, value in request['handoff'].items() if key not in reserved}
    handoff.update(blind_findings=blind_findings, blind_receipt_sha256=blind_sha256,
                   instruction=handoff['instruction'] + blind_review.CONFLICT_INSTRUCTION)
    schema = blind_review.conflict_schema(REVIEW_SCHEMA_V2, [row['finding_id'] for row in blind_findings])
    target = output / 'cases' / f'{number:02d}' / 'target'
    with closing(Store(target, output / f's{number:02d}')) as store:
        return WorkflowController(store)._request(case['id'], 'review', schema, handoff)


def without_reconciled(case, blind_payload, scope_payload):
    """The blind payload minus findings the scope pass reconciled with a verbatim requirement quote, as the gate does."""
    from ase.findings import finding_id
    from ase.review import blind as blind_review
    text = blind_review.requirement_text(contract_for(case))
    quoted = {row.get('finding_id') for row in (scope_payload or {}).get(blind_review.CONFLICT_FIELD) or []
              if isinstance(row, dict) and isinstance(row.get('requirement_quote'), str)
              and ' '.join(row['requirement_quote'].split()).casefold() in text}
    if not isinstance(blind_payload, dict):
        return blind_payload, 0
    kept = [item for item in blind_payload.get('findings') or [] if not (
        isinstance(item, dict) and item.get('severity') in BLOCKING and finding_id(item) in quoted)]
    return {**blind_payload, 'findings': kept}, len(blind_payload.get('findings') or []) - len(kept)


def review_candidate(output, executable, backend, number, case, variant, *, projects_root=None, blind=False):
    requests = prepare_candidate(output, number, case, variant, projects_root=projects_root, blind=blind)
    folder = output / 'cases' / f'{number:02d}'
    if not blind:
        verified, usage, result = run_request(folder, executable, backend, requests, 'scope')
        return {'number': number, 'case': case['id'], 'verified': verified, 'usage': {'scope': usage},
                'results': {'scope': result}, 'payloads': [result.get('payload')]}
    scope_request, blind_request = requests
    # The runtime order: the blind pass first, then the scope pass with its blocking findings.
    passes = {'blind': run_request(folder, executable, backend, blind_request, 'blind')}
    from ase.review import blind as blind_review
    from ase.state import hash_value
    blind_findings = blind_review.blocking_findings(passes['blind'][2].get('payload'))
    if blind_findings:
        blind_receipt = decode_json((folder / 'runs-blind').glob('*/receipt.json').__next__().read_bytes())
        scope_request = reconciled_scope_request(output, number, case, scope_request, blind_findings, hash_value(blind_receipt))
    passes['scope'] = run_request(folder, executable, backend, scope_request, 'scope')
    blind_payload, reconciled = without_reconciled(case, passes['blind'][2].get('payload'), passes['scope'][2].get('payload'))
    return {'number': number, 'case': case['id'], 'verified': all(verified for verified, _, _ in passes.values()),
            'usage': {label: usage for label, (_, usage, _) in passes.items()},
            'results': {label: result for label, (_, _, result) in passes.items()},
            'payloads': [passes['scope'][2].get('payload'), blind_payload], 'reconciled': reconciled}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True, help='Fresh disposable output folder')
    parser.add_argument('--executable', type=Path, required=True, help='Existing local reviewer CLI executable')
    parser.add_argument('--backend', choices=('codex', 'claude'), default='codex', help='CLI protocol of --executable')
    parser.add_argument('--cases', type=Path, default=CASES, help='Case file (keep answer keys outside reviewed projects)')
    parser.add_argument('--projects-root', type=Path, help='Folder holding the projects that cases name (copied read-only)')
    parser.add_argument('--case', action='append', default=[], help='Review only this case ID (repeatable)')
    parser.add_argument('--repeat', type=int, default=1, help='Review every candidate this many times to see variance')
    parser.add_argument('--blind', action='store_true', help='Also run the blind pass and merge its findings')
    parser.add_argument('--workers', type=int, default=2)
    args = parser.parse_args()
    cases = [case for case in decode_json(args.cases.read_bytes())['cases'] if not args.case or case['id'] in args.case]
    if args.case and {case['id'] for case in cases} != set(args.case):
        raise SystemExit('Unknown case ID')
    if args.repeat < 1:
        raise SystemExit('--repeat must be at least 1')
    variants = [(case, variant) for case in cases for variant in case['variants']]
    jobs = list(enumerate((pair for _ in range(args.repeat) for pair in variants), 1))
    args.output.mkdir(parents=True)
    # Keep the variant identities and expectations outside every reviewed workspace and packet.
    rubric = {str(number): variant['expectation'] for number, (_, variant) in jobs}
    save(args.output / 'rubric.json', rubric)
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(review_candidate, args.output, args.executable, args.backend, number, case, variant,
                               projects_root=args.projects_root, blind=args.blind)
                   for number, (case, variant) in jobs]
        results = [future.result() for future in futures]
    save(args.output / 'results.json', results)
    summary = summarize(results, rubric)
    save(args.output / 'summary.json', summary)
    for case_id, entry in summary.items():
        print(f"{case_id}: found {entry['found']}, missed {entry['missed']}, false positives {entry['false_positives']}, "
              f"runs {entry['runs']}" + (f", unverified {entry['unverified_runs']}" if entry['unverified_runs'] else ''))
    return 0 if all(row['verified'] for row in results) else 3


if __name__ == '__main__':
    raise SystemExit(main())
