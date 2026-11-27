"""Run independent review on paired, executable candidate patches in disposable repositories."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
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


def commit_baseline(target):
    git = ['git', '-c', 'core.autocrlf=false', '-c', 'user.name=Fixture Owner',
           '-c', 'user.email=fixture@example.invalid', '-c', 'core.hooksPath=' + str(target / '.git/no-hooks')]
    for args in (['add', '--', '.'], ['commit', '--quiet', '--no-gpg-sign', '-m', 'Synthetic baseline']):
        subprocess.run([*git, *args], cwd=target, stdin=subprocess.DEVNULL,
                       capture_output=True, check=True, timeout=30)


def prepare_candidate(output, number, case, variant):
    folder = output / 'cases' / f'{number:02d}'
    target = initialize(folder, case)
    commit_baseline(target)
    stage = case['contract']['stages'][0]['id']
    with closing(Store(target, output / f's{number:02d}')) as store:
        contract = contract_for(case)
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
        return controller._request(task_id, 'review', REVIEW_SCHEMA_V2, {
            'stage_id': stage, 'instruction': 'Independently review actual source for actionable defects P0-P3. '
            'P0/P1/P2 block. Report material missing evidence in unverified_material.',
            'evaluation_observations': {
                'before_check': {'fingerprint': before['fingerprint'], 'exit_code': before['results'][0]['exit_code'],
                                 'log': before_log},
                'repository': 'One local baseline commit and this working-tree change. '
                              'The harness creates no remote, candidate branch or pull request.'}})


def review_candidate(output, executable, backend, number, case, variant):
    request = prepare_candidate(output, number, case, variant)
    folder = output / 'cases' / f'{number:02d}'
    trusted = folder / 'trusted'
    trusted.mkdir()
    run = Runner({'executable': str(executable), 'trusted_cwd': str(trusted)}, folder / 'runs').run(
        get_adapter(backend), request, timeout=240)
    receipt = run['receipt']
    verified = verify_receipt(receipt, expected_bindings=request['bindings'])['valid']
    return {'number': number, 'case': case['id'], 'verified': verified,
            'usage': session_usage(receipt.get('terminal')) if verified else None, 'result': run['result']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True, help='Fresh disposable output folder')
    parser.add_argument('--executable', type=Path, required=True, help='Existing local reviewer CLI executable')
    parser.add_argument('--backend', choices=('codex', 'claude'), default='codex', help='CLI protocol of --executable')
    parser.add_argument('--case', action='append', default=[], help='Review only this case ID (repeatable)')
    args = parser.parse_args()
    cases = [case for case in decode_json(CASES.read_bytes())['cases'] if not args.case or case['id'] in args.case]
    if args.case and {case['id'] for case in cases} != set(args.case):
        raise SystemExit('Unknown case ID')
    jobs = list(enumerate(((case, variant) for case in cases for variant in case['variants']), 1))
    args.output.mkdir(parents=True)
    # Keep the variant identities and expectations outside every reviewed workspace and packet.
    save(args.output / 'rubric.json', {str(number): variant['expectation'] for number, (_, variant) in jobs})
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(review_candidate, args.output, args.executable, args.backend, number, case, variant)
                   for number, (case, variant) in jobs]
        results = [future.result() for future in futures]
    save(args.output / 'results.json', results)
    return 0 if all(row['verified'] for row in results) else 3


if __name__ == '__main__':
    raise SystemExit(main())
