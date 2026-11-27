"""Opt-in real-Codex reasoning scenarios through the production workflow.

Capture only (the default) never probes a backend or creates a project database.
--run-live creates disposable local targets and private state under --output,
then uses WorkflowController.run/_request/_verify and the existing CodexAdapter.
The adapter proposes edits; the host applies them and executes actual unittest
checks. No cloud resources, external SQL, messages or publishing are performed.

Mechanical checks and live independent AC/review are recorded separately. A
scenario's semantic choices still require independent reading against rubric.json;
this program never manufactures a behavioral PASS from fixture expectations.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import copy
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ase.discovery import discover_capabilities, record_discovery
from ase.gates import GateService, source_snapshot
from ase.knowledge import Knowledge
from ase.protocol import canonical, sha256
from ase.runner import verify_receipt
from ase.state import read_record
from ase.store import Store, now
from ase.tasks import TaskService
from ase.workflow import WorkflowController, EDIT_SCHEMA


DEFAULT_CASES = ROOT / 'tests/fixtures/production-reasoning-cases.json'


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('xb') as stream:
        stream.write(canonical(value))


def host_source_snapshot():
    paths = [*ROOT.joinpath('ase').rglob('*.py'), ROOT / 'ase/data/runtime_policy.json',
             Path(__file__).resolve(), ROOT / 'pyproject.toml', ROOT / 'scripts/build_assets.py']
    files = {path.relative_to(ROOT).as_posix(): sha256(path.read_bytes()) for path in sorted(set(paths))}
    return {'observed_at_utc': now(), 'files': files, 'fingerprint': sha256(canonical(files)),
            'scope': 'Actual host runtime Python, packaged runtime policy, evaluator and packaging sources'}


def load_cases(path):
    value = json.loads(path.read_bytes())
    cases = value['cases']
    ids = [case['id'] for case in cases]
    if not cases or len(ids) != len(set(ids)) or set(ids) != set(value['rubric']):
        raise ValueError('Cases and withheld rubric require matching unique IDs')
    for case in cases:
        for relative in case['files']:
            path = Path(relative)
            if path.is_absolute() or '..' in path.parts or ':' in relative or relative.startswith(('/', '\\')):
                raise ValueError('Fixture files must stay inside the disposable target')
    return value


def capture(args):
    if args.output.exists():
        raise ValueError('Select a fresh output directory; captures are never overwritten')
    fixture = load_cases(args.cases)
    selected = [case for case in fixture['cases'] if not args.case or case['id'] in args.case]
    if not selected or (args.case and set(args.case) - {case['id'] for case in selected}):
        raise ValueError('Unknown or empty scenario selection')
    args.output.mkdir(parents=True)
    save(args.output / 'host-source-before.json', host_source_snapshot())
    save(args.output / 'cases.json', selected)
    save(args.output / 'rubric.json', {case['id']: fixture['rubric'][case['id']] for case in selected})
    save(args.output / 'input-manifest.json', {
        'captured_at_utc': now(), 'fixture_path': str(args.cases),
        'fixture_sha256': sha256(args.cases.read_bytes()), 'cases_sha256': sha256(canonical(selected)),
        'rubric_sha256': sha256(canonical({case['id']: fixture['rubric'][case['id']] for case in selected})),
        'repetitions': args.repetitions, 'selected_ids': [case['id'] for case in selected],
        'drift_mode': getattr(args, 'drift_mode', 'interrupted'),
        'policy_source': 'Unmodified WorkflowController._request production policy path; no guide-body injection',
        'scope': 'Disposable local Python/SQLite artifacts; actual workflow sessions and checks only with --run-live',
        'semantic_grading': 'Read actual choices independently against the withheld rubric; no automatic behavioral verdict',
    })
    return selected


def contract_for(case, *, drift=False):
    selected = copy.deepcopy(case['contract'])
    selected['id'] = case['id']
    selected['schema_version'] = 2
    selected['execution_policy'] = {
        'backend': 'codex', 'recovery_mode': 'fixed', 'max_stage_cycles': 2,
        'max_task_cycles': 4, 'max_diagnostic_passes': 1,
        'active_time_budget_seconds': 3600,
    }
    for evidence in selected['evidence']:
        if evidence.get('kind') == 'test':
            evidence.update(argv=[sys.executable, '-B', '-m', 'unittest', '-v', 'test_artifact'],
                            parser='unittest', timeout=30)
    if drift:
        selected['request'] += '\n' + case['drift']['owner_instruction']
    return selected


def authorize(store, contract, *, revise=False, dependencies=(), user_message_ref=None):
    tasks = TaskService(store)
    task_id = contract['id']
    task = (tasks.revise(task_id, contract, expected_revision=tasks.snapshot(task_id)['revision'])
            if revise else tasks.create(contract))
    discovery = discover_capabilities(store.project, scope={
        'resources': contract['scope']['resources'],
        'dependencies': [{'path': path, 'reason': 'Actual source/test/owner input required by this scenario'}
                         for path in sorted(set(dependencies) - set(contract['scope']['resources']))],
    }, backend_config={})
    revision = store.db.execute('SELECT COALESCE(MAX(revision),0) FROM task_discoveries WHERE task_id=?', (task_id,)).fetchone()[0]
    record_discovery(store, task_id, discovery, expected_revision=revision)
    stages = [contract['stages'][0]['id']]
    task = tasks.propose(task_id, {'summary': contract['request'], 'stage_ids': stages}, expected_revision=task['revision'])
    tasks.approve(task_id, proposal_hash=task['proposal_hash'],
                  user_message_ref=(user_message_ref or 'fixture:explicit-local-scenario-authorization'),
                  approved_stage_ids=stages)
    return task_id


def initialize(folder, case):
    target = folder / 'target'
    target.mkdir(parents=True)
    for relative, content in case['files'].items():
        path = target / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding='utf-8', newline='')
    # Local metadata only; no remote is configured and no commit or push occurs.
    process = subprocess.run(['git', 'init', '--quiet', str(target)], stdin=subprocess.DEVNULL,
                             capture_output=True, timeout=30)
    if process.returncode:
        raise ValueError('Could not initialize disposable local Git metadata')
    return target


def configure(controller, args):
    controller._save('backend_config', 'codex', {
        'executable': str(args.executable), 'max_input_bytes': 2097152,
        'max_packet_bytes': 1048576, 'max_output_bytes': 2097152,
    })


def observe(folder, controller, task_id, label):
    store = controller.store
    checks = read_record(store.db, 'checks', task_id)['payload']
    stages = {stage['id']: read_record(store.db, 'stage', task_id + ':' + stage['id'])['payload']
              for stage in controller.tasks.snapshot(task_id)['contract']['stages']}
    value = {'observed_at_utc': now(), 'task': controller.tasks.snapshot(task_id),
             'source': source_snapshot(store, controller.tasks.snapshot(task_id)['contract'], task_id),
             'checks': checks, 'stages': stages, 'gate': GateService(store).gate(task_id)}
    save(folder / (label + '.json'), value)
    return value


def cache_sources(store, task_id, case):
    knowledge = Knowledge(store)
    for path in case.get('drift', {}).get('files', {}):
        claim = knowledge.record({'topic': 'scenario-current-input', 'statement': 'Current input at ' + path,
                                  'observed_at': now(), 'source_refs': ['local:' + path],
                                  'scope': {'paths': [path]}, 'task_id': task_id})
        version = 'sha256:' + sha256((store.repo / path).read_bytes())
        knowledge.record_observations([
            {'source_id': path.replace('.', '-'), 'locator': 'local:' + path,
             'version': version, 'observed_at_utc': now(), 'availability': 'AVAILABLE', 'kind': 'file'}],
            [{'claim_id': claim['id'], 'source_id': path.replace('.', '-'), 'observed_version': version}],
            expected_revision=knowledge.retrieve(scope={}, refresh=False)['revision'])
    return knowledge.retrieve(scope={}, refresh=True)


def receipt_inventory(store, task_id):
    return inventory_at(store.root / 'tasks' / task_id / 'sessions')


def inventory_at(session_root):
    rows = []
    for path in sorted(session_root.glob('*/receipt.json')):
        receipt = json.loads(path.read_bytes())
        verified, error, current, current_error = False, None, False, None
        try:
            verify_receipt(receipt, require_current_policy=False)
            verified = True
        except ValueError as ex:
            error = str(ex)
        if verified:
            try:
                verify_receipt(receipt)
                current = True
            except ValueError as ex:
                current_error = str(ex)
        request = json.loads(Path(receipt['artifacts']['request']['path']).read_bytes())
        rows.append({'role': receipt['role'], 'session_id': receipt['session_id'],
                     'execution_id': receipt['execution_id'], 'receipt_path': str(path),
                     'receipt_sha256': sha256(path.read_bytes()), 'receipt_integrity_verified': verified,
                     'current_policy_verified': current, 'current_policy_error': current_error,
                     'transport_error': error, 'status': receipt['status'],
                     'bindings': request['bindings'],
                     'policy_transport': {key: request.get('handoff', {}).get('runtime_policy', {}).get(key)
                                          for key in ('version', 'prompt_version', 'role', 'policy_id', 'sha256')},
                     'request_sha256': sha256(canonical(request))})
    return rows


def run_one(args, case, repetition):
    folder = args.output / ('rep-' + str(repetition).zfill(2)) / case['id']
    target = initialize(folder, case)
    # Runner artifact filenames add two UUIDs and the task ID. Keep state near
    # the capture root so ordinary Windows installations stay below MAX_PATH.
    state = args.output / 's' / (str(repetition) + '-' + sha256(case['id'].encode())[:6])
    save(folder / 'state-location.json', {'state_home': str(state), 'target': str(target)})
    store = Store(target, state)
    try:
        selected = contract_for(case)
        # Dependencies are discovery inputs, never scope edit authority.
        task_id = authorize(store, selected, dependencies=case['files'])
        controller = WorkflowController(store)
        configure(controller, args)
        baseline = controller.check(task_id, selected['stages'][0]['id'])
        save(folder / 'baseline-checks.json', baseline)
        save(folder / 'preflight-request.json', controller._request(task_id, 'implementation',
             EDIT_SCHEMA,
             {'instruction': 'Propose minimal approved-scope edits. Empty before_sha256 means a new file. Do not change requirements or proof state.',
              'stage': controller.tasks.snapshot(task_id)['contract']['stages'][0]}))
        before_cache = cache_sources(store, task_id, case)
        save(folder / 'cache-before.json', before_cache)
        if case.get('drift') and args.drift_mode == 'interrupted':
            # Interrupt only after the actual first candidate was applied and
            # checked. No initial semantic verdict is stubbed or accepted.
            def interrupt_before_verification(selected_task, stage_id=None):
                controller.request_stop(selected_task,
                    reason='Controlled scenario interruption after actual initial draft and checks')
                raise ValueError('Controlled interruption before initial independent verification')
            controller.verify = interrupt_before_verification
        first = controller.run(task_id)
        save(folder / 'first-run.json', first)
        observe(folder, controller, task_id, 'first-observation')
        if case.get('drift'):
            # The draft exists before actual independent input and owner changes.
            if not (target / case['drift']['artifact']).is_file():
                raise ValueError('Draft was not actually created before the drift step')
            for relative, content in case['drift']['files'].items():
                (target / relative).write_text(content, encoding='utf-8', newline='')
            save(folder / 'drift-gate-before-refresh.json', GateService(store).gate(task_id, selected['stages'][0]['id']))
            save(folder / 'cache-after-source-drift.json', Knowledge(store).retrieve(scope={}, refresh=True))
            save(folder / 'drift-manifest.json', {'at_utc': now(), 'owner_instruction': case['drift']['owner_instruction'],
                 'owner_message_ref': case['drift']['owner_message_ref'],
                 'actual_files': {path: sha256((target / path).read_bytes()) for path in case['drift']['files']}})
            store.close()
            store = Store(target, state)
            controller = WorkflowController(store)
            configure(controller, args)
            # The fixture owner supplied a real answer, so transport it through
            # current approved authority in both interruption and revision modes.
            authorize(store, contract_for(case, drift=True), revise=True, dependencies=case['files'],
                      user_message_ref=case['drift']['owner_message_ref'])
            save(folder / 'resume-run.json', controller.run(task_id, resume=True))
            observe(folder, controller, task_id, 'resumed-observation')
        # Re-execute artifact tests after final workflow result, without creating
        # new proof bindings that would stale the just-captured independent AC.
        actual = subprocess.run([sys.executable, '-B', '-m', 'unittest', '-v', 'test_artifact'], cwd=target,
                                stdin=subprocess.DEVNULL, capture_output=True, timeout=30)
        with (folder / 'artifact-tests.log').open('xb') as stream:
            stream.write(actual.stdout + actual.stderr)
        rows = receipt_inventory(store, task_id)
        save(folder / 'receipts.json', rows)
        result = {'case_id': task_id, 'repetition': repetition,
                  'artifact_test_exit_code': actual.returncode,
                  'artifact_test_log_sha256': sha256((folder / 'artifact-tests.log').read_bytes()),
                  'sessions': len(rows), 'verified_receipts': sum(row['receipt_integrity_verified'] for row in rows),
                  'current_policy_receipts': sum(row['current_policy_verified'] for row in rows),
                  'semantic_grade': 'UNASSIGNED: independently read outputs, artifact effects and rubric',
                  'output': str(folder)}
        save(folder / 'capture.json', result)
        print(json.dumps(result), flush=True)
        return result
    finally:
        store.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--cases', type=Path, default=DEFAULT_CASES)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--case', action='append', help='Select a fixture ID; repeatable')
    parser.add_argument('--repetitions', type=int, default=1)
    parser.add_argument('--drift-mode', choices=('interrupted', 'revision'), default='interrupted',
                        help='Resume after controlled post-check interruption, or completed-stage owner revision')
    parser.add_argument('--parallelism', type=int, choices=(1, 2), default=1,
                        help='Concurrent independent disposable targets; each owns a separate Store')
    parser.add_argument('--executable', type=Path, help='Installed Codex executable; no model/provider/config override')
    parser.add_argument('--run-live', action='store_true')
    args = parser.parse_args()
    if not 1 <= args.repetitions <= 5:
        parser.error('--repetitions must be 1..5')
    if args.run_live and (args.executable is None or not args.executable.is_absolute() or not args.executable.is_file()):
        parser.error('--run-live requires an absolute installed --executable')
    args.cases = args.cases.resolve(strict=True)
    args.output = args.output.resolve()
    cases = capture(args)
    if not args.run_live:
        print(json.dumps({'captured': str(args.output), 'live': False}), flush=True)
        return 0
    save(args.output / 'launch.json', {'started_at_utc': now(), 'executable': str(args.executable),
         'model_override': None, 'provider_override': None, 'user_config_override': None,
         'parallelism': args.parallelism,
         'workflow': 'Production WorkflowController.run/resume with its normal _request and _verify',
         'mutations': 'Disposable target files, local SQLite tests and local private-state only'})
    def one(item):
        repetition, case = item
        try:
            return run_one(args, case, repetition)
        except (ValueError, OSError, KeyError, subprocess.TimeoutExpired) as ex:
            folder = args.output / ('rep-' + str(repetition).zfill(2)) / case['id']
            partial = []
            location = folder / 'state-location.json'
            if location.is_file():
                state = Path(json.loads(location.read_bytes())['state_home'])
                for sessions in state.glob('projects/*/tasks/' + case['id'] + '/sessions'):
                    partial.extend(inventory_at(sessions))
            save(folder / 'partial-receipts.json', partial)
            row = {'case_id': case['id'], 'repetition': repetition,
                   'capture_error': type(ex).__name__ + ': ' + str(ex),
                   'artifact_test_exit_code': None, 'sessions': len(partial),
                   'verified_receipts': sum(item['receipt_integrity_verified'] for item in partial),
                   'current_policy_receipts': sum(item['current_policy_verified'] for item in partial),
                   'semantic_grade': 'UNASSIGNED: scenario did not finish; inspect preserved partial artifacts',
                   'output': str(folder)}
            save(folder / 'capture-error.json', row)
            print(json.dumps(row), flush=True)
            return row

    with ThreadPoolExecutor(max_workers=args.parallelism) as pool:
        rows = list(pool.map(one, [(repetition, case) for repetition in range(1, args.repetitions + 1) for case in cases]))
    sessions = [row for path in args.output.glob('rep-*/*/*receipts.json') for row in json.loads(path.read_bytes())]
    ids = [row['session_id'] for row in sessions if row['session_id']]
    if len(ids) != len(set(ids)):
        raise ValueError('Fresh independent sessions reused an identity')
    before = json.loads((args.output / 'host-source-before.json').read_bytes())
    after = host_source_snapshot()
    save(args.output / 'host-source-after.json', after)
    unchanged = before['files'] == after['files']
    save(args.output / 'summary.json', {'ended_at_utc': now(), 'scenarios': rows,
         'session_count': len(sessions), 'unique_session_count': len(set(ids)),
         'host_source_unchanged': unchanged,
         'host_source_fingerprint_before': before['fingerprint'],
         'host_source_fingerprint_after': after['fingerprint'],
         'semantic_grade': 'UNASSIGNED; transport and executable artifact tests are not a behavioral verdict'})
    return 0 if unchanged and all(row['artifact_test_exit_code'] == 0 and row['verified_receipts'] == row['sessions']
                    and row['current_policy_receipts'] == row['sessions'] and row['sessions']
                    for row in rows) else 3


if __name__ == '__main__':
    raise SystemExit(main())
