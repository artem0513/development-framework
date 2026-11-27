"""Check current immutable native bugfix evidence, without relaunching models.

Run from the public checkout with explicit captured evidence directories:
  python -B scripts/check_native_bugfix_evidence.py --consumer outputs/bugfix-evidence-20261119/live-consumer-execution-bound --cases outputs/bugfix-evidence-20261119/current-cases

The consumer is the executed disposable local fixture, not a remote service.
The six advisory cases are stipulated packet-only exercises. Their complete raw
outputs and capture identities are printed for independent semantic assessment;
no keyword matching or aggregate PASS determines whether those decisions are
correct. The unittest results below establish evidence integrity/currentness.
Existing executions are reused only while all these checks remain satisfied.
Local hashes/receipts are error detection, not signatures against a hostile user.
"""
from __future__ import annotations

import argparse
import ast
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from ase.gates import GateService
from ase.projects import ProjectRegistry
from ase.protocol import canonical, decode_json, packet_prompt, sha256
from ase.runner import checked_path, verify_receipt
from ase.runtime_policy import bind_policy
from ase.state import read_record
from ase.store import Store
from ase.tasks import TaskService
from scripts.evaluate_reasoning_rules import DEFAULT_SKILLS, result_schema

CASE_IDS = ('symptom-only-merger', 'caller-ignores-return', 'partial-response-contract',
            'serialization-loss', 'unknown-useful-probe', 'missing-contract')
GATE_BINDINGS = ('contract_hash', 'proposal_hash', 'plan_hash', 'fingerprint',
                 'verification_context_sha256', 'test_adequacy_sha256', 'lead_review_sha256')
TASK = 'consumer-repair'
REQUIREMENTS = [
    'You are performing the assigned task-agent work items in evaluation/work-items.json. Apply the supplied existing workflow skills and references as the active policy. Each item is independent; give every id exactly once in its supplied order.',
    'The observations and source snippets are stipulated state/evidence for a compact offline exercise. Decide what to do next, and prepare a concrete useful artifact: exact candidate code/SQL, handoff record, incident note, review response or internal draft as appropriate. Do not merely recite a checklist. Artifacts are proposed local deliverables, not claimed file writes or executed checks.',
    'Choose ordinary implementation details yourself under existing authorization. If a real owner decision is needed now, provide the concise owner message; otherwise owner_message is empty. Clearly distinguish proposed next operations from supplied executed evidence. Do not evaluate the skills or guess a grading rubric.',
    'No actual commands, service calls, messages or mutations are authorized in this packet exercise. Use only supplied evidence; any necessary next investigation is an intended operation, never a claim that you performed it.',
]
CONSUMER_TEST = '''import json, unittest
from client import consumer
class ConsumerTests(unittest.TestCase):
 def test_complete_response_reaches_consumer(self):
  original={'Number':'M7','OriginGroup':'A','Discount':0}
  observed=[]
  def transport(raw):
   observed.append(json.loads(raw))
   return json.dumps({'Number':'M7','OriginGroup':'B','Discount':17})
  result=consumer(original,transport)
  self.assertEqual(observed,[original])
  self.assertEqual(result,{'Number':'M7','OriginGroup':'B','Discount':17})
  self.assertEqual(original,{'Number':'M7','OriginGroup':'A','Discount':0})
'''


def require(value, message):
    if not value:
        raise ValueError(message)


def load(path):
    return decode_json(checked_path(Path(path), directory=False).read_bytes())


def same(actual, expected, message):
    require(canonical(actual) == canonical(expected), message)


def record(path, content):
    return {'path': path, 'content': content, 'sha256': sha256(content.encode('utf-8'))}


def packet_files(request):
    from ase.evidence.compact import expand_packet
    rows = expand_packet(request['source_packet'])['files']
    result = {row['path']: row for row in rows}
    require(len(result) == len(rows), 'Duplicate source packet paths')
    return result


def validate_identities(rows):
    require(bool(rows) and all(isinstance(a, str) and a and isinstance(b, str) and b
                              for a, b in rows), 'Missing execution/provider session identity')
    require(len({a for a, _ in rows}) == len(rows), 'Duplicate execution identities')
    require(len({b for _, b in rows}) == len(rows), 'Duplicate provider session identities')


def independent(receipt, verified):
    require(verified.get('valid') is True and verified.get('evidence_kind') == 'independent_cli'
            and receipt.get('evidence_kind') == 'independent_cli', 'Actual independent_cli capture required')
    require(receipt.get('backend') == 'codex', 'Expected genuine installed Codex CLI capture')
    require(verified.get('execution_id') == receipt.get('execution_id'), 'Verified execution identity mismatch')
    require(verified['result']['status'] == 'COMPLETE'
            and verified['result']['session_id'] == receipt.get('session_id'), 'Incomplete/mismatched native result')
    validate_identities([(receipt['execution_id'], receipt['session_id'])])


def validate_cases(folder, *, source_root=ROOT, verify=verify_receipt):
    """Rebuild the evaluator inputs; injected verifiers are for unit tests only."""
    folder = checked_path(Path(folder).resolve(), directory=True)
    source_root = Path(source_root).resolve()
    fixture_path = source_root / 'tests' / 'fixtures' / 'bugfix-evidence-cases.json'
    fixture_bytes = checked_path(fixture_path, directory=False).read_bytes()
    fixture = decode_json(fixture_bytes)
    cases, rubric = fixture['cases'], fixture['rubric']
    same([row['id'] for row in cases], list(CASE_IDS), 'Original fixture case IDs differ')
    same(sorted(rubric), sorted(CASE_IDS), 'Original fixture rubric IDs differ')
    manifest = load(folder / 'input-manifest.json')
    same(Path(manifest['case_source']).resolve().as_posix(), fixture_path.as_posix(), 'Wrong original case source')
    require(manifest['case_file_sha256'] == sha256(fixture_bytes), 'Original case fixture bytes changed')
    same(load(folder / 'cases.json'), cases, 'Captured cases differ from original fixture')
    same(load(folder / 'rubric.json')['criteria'], rubric, 'Captured rubric differs from original fixture')
    require(manifest['rubric_sha256'] == sha256(canonical(rubric)), 'Rubric hash mismatch')
    require(manifest['intended_repetitions'] == 1, 'Exactly one current advisory capture is required')
    files, sources = [], []
    guides = sorted({path.resolve() for name in DEFAULT_SKILLS
                     for path in (source_root / 'skills' / name).rglob('*.md')})
    require(all((source_root / 'skills' / name).is_dir() for name in DEFAULT_SKILLS)
            and bool(guides), 'Current default guide set is missing')
    for path in guides:
        raw = checked_path(path, directory=False).read_bytes()
        row = record(path.relative_to(source_root).as_posix(), raw.decode('utf-8-sig').replace('\r\n', '\n'))
        files.append(row)
        sources.append({'source_path': str(path), 'packet_path': row['path'],
                        'source_bytes_sha256': sha256(raw), 'packet_text_sha256': row['sha256']})
    same(manifest['guide_sources'], sources, 'Captured guide set/bytes are stale or incomplete')
    case_row = record('evaluation/work-items.json', canonical(cases).decode('utf-8'))
    files.append(case_row)
    require(manifest['case_content_sha256'] == case_row['sha256'], 'Case content hash mismatch')
    packet = {'requirements': REQUIREMENTS, 'files': files, 'coverage': {
        'scope': 'Full selected workflow guide bodies and stipulated small work-item fixtures. Simulated decision/artifact behavior, not real infrastructure execution or main-task acceptance.',
        'included': [row['path'] for row in files], 'missing': []}}
    digest = sha256(canonical(packet))
    bindings = {key: digest for key in ('fingerprint', 'contract_sha256', 'proposal_sha256',
                                      'plan_sha256', 'check_sha256', 'source_packet_sha256')}
    template = {'role': 'diagnostic', 'project': {'root': str(source_root)}, 'bindings': bindings,
        'schema': result_schema(len(cases)), 'source_packet': packet,
        'handoff': {'purpose': 'workflow behavioral exercise; advisory only',
                    'session_scope': 'fresh independent packet-only scenario batch'}}
    same(load(folder / 'request-template.json'), template, 'Request template differs from current original inputs')
    require(manifest['packet_sha256'] == digest and manifest['request_sha256'] == sha256(canonical(template))
            and manifest['prompt_sha256'] == sha256(packet_prompt(template).encode('utf-8')), 'Input manifest request/prompt hash mismatch')
    launch = load(folder / 'launch.json')
    require(launch['input_manifest_sha256'] == sha256((folder / 'input-manifest.json').read_bytes())
            and launch['repetitions'] == 1 and launch['model_override'] is None
            and launch['configuration_overrides'] is None, 'Launch manifest mismatch or unauthorized overrides')
    require(sorted(path.name for path in folder.glob('rep-*')) == ['rep-01'], 'Missing or duplicate advisory captures')
    capture = load(folder / 'rep-01' / 'capture.json')
    require(capture['index'] == 1 and capture['status'] == 'COMPLETE' and capture['blocker'] is None
            and capture['receipt_integrity_verified'] is True, 'Capture is not complete; saved integrity flag alone is insufficient')
    execution = capture['execution_id']
    run = folder / 'rep-01' / 'sessions' / execution
    require(Path(capture['artifact_root']) == run, 'Capture artifact root escapes selected capture')
    require(sorted(path.name for path in run.parent.iterdir()) == [execution], 'Duplicated advisory execution captures')
    receipt_path = run / 'receipt.json'
    receipt = load(receipt_path)
    require(capture['receipt_sha256'] == sha256(receipt_path.read_bytes()), 'Captured receipt hash mismatch')
    require(receipt['artifact_root'] == str(run) and receipt['execution_id'] == execution
            and receipt['session_id'] == capture['session_id'] and receipt['role'] == 'diagnostic', 'Capture/receipt identities differ')
    require(receipt['executable'] == launch['executable'] and receipt['cwd'] == launch['trusted_cwd'], 'Launch executable/cwd differs from receipt')
    verified = verify(receipt, expected_bindings=bindings, require_current_policy=True)
    independent(receipt, verified)
    require(verified.get('policy_current') is True, 'Current runtime policy required')
    same(load(run / 'request.json'), bind_policy(template), 'Delivered advisory inputs differ from current inputs/policy')
    same(capture['result'], verified['result'], 'Capture result differs from verified actual model result')
    same(load(folder / 'sessions-summary.json')['sessions'],
         [{key: value for key, value in capture.items() if key != 'result'}], 'Session summary differs from capture')
    items = verified['result']['payload']['items']
    same([row['id'] for row in items], list(CASE_IDS), 'Actual output case IDs are missing, duplicated or reordered')
    require(fixture_path.read_bytes() == fixture_bytes
            and all(sha256(Path(row['source_path']).read_bytes()) == row['source_bytes_sha256'] for row in sources),
            'Original fixture/current guides changed while validating captures')
    return {'scope': 'Stipulated packet-only outputs; independent semantic adjudication is required.',
        'capture': {'artifact_root': str(run), 'receipt_sha256': capture['receipt_sha256'],
                    'execution_id': execution, 'session_id': receipt['session_id'],
                    'request_sha256': sha256(canonical(bind_policy(template)))},
        'identities': [(execution, receipt['session_id'])], 'items': items}


def sealed_database(path):
    path = checked_path(path, directory=False)
    require(not any(Path(str(path) + suffix).exists() for suffix in ('-wal', '-shm', '-journal')),
            'Consumer databases must be sealed: close the capture writer before validation')
    return sha256(path.read_bytes())


@contextmanager
def consumer_store(folder):
    """Use original paths and an in-memory DB backup; gate writes stay private.

    Store/ProjectRegistry constructors perform maintenance. Avoid them here: use
    their existing read methods with a read-only registry and copied database.
    The source/check/receipt bytes remain at the captured absolute paths.
    """
    repo, state = folder / 'repo', folder / 'state'
    registry = object.__new__(ProjectRegistry)
    registry.state_home = checked_path(state, directory=True)
    registry_path = checked_path(state / 'registry.sqlite3', directory=False)
    registry_hash = sealed_database(registry_path)
    # immutable=1 avoids SQLite creating WAL/SHM files next to captured evidence.
    # Reject active journals first; otherwise immutable mode could ignore them.
    registry.db = sqlite3.connect(registry_path.as_uri() + '?mode=ro&immutable=1', uri=True)
    registry.db.row_factory = sqlite3.Row
    memory, captured = None, None
    try:
        project = registry.resolve(str(checked_path(repo, directory=True)))
        store = object.__new__(Store)
        store.project, store.repo, store.state_home = project, project.root, state
        store.project_id, store.workspace_id = project.project_id, project.workspace_id
        store.root, store.log_retention_days = state / 'projects' / project.storage_key, 30
        path = checked_path(store.root / 'memory.sqlite3', directory=False)
        captured_hash = sealed_database(path)
        memory = sqlite3.connect(':memory:')
        memory.row_factory = sqlite3.Row
        captured = sqlite3.connect(path.as_uri() + '?mode=ro&immutable=1', uri=True)
        captured.backup(memory)
        store.db = memory
        same(read_record(memory, 'project_identity', 'identity')['payload'],
             {'project_id': project.project_id, 'storage_key': project.storage_key}, 'Consumer database identity mismatch')
        yield store
        require(sealed_database(path) == captured_hash and sealed_database(registry_path) == registry_hash,
                'Captured consumer state changed during validation')
    finally:
        if captured is not None:
            captured.close()
        if memory is not None:
            memory.close()
        registry.db.close()


def validate_saved_gate(saved, current):
    require(saved.get('status') == current.get('status') == 'APPROVED', 'Saved/current consumer gate is not APPROVED')
    for key in GATE_BINDINGS:
        require(key in saved and key in current and saved[key] == current[key], 'Consumer result is stale: ' + key)
    for key, value in current.items():
        if key != 'timestamp':
            require(key in saved, 'Saved consumer result field missing: ' + key)
            same(saved[key], value, 'Saved consumer result differs from current gate: ' + key)


def validate_judgment(row, receipt, verified):
    same(row, {'kind': verified['evidence_kind'], 'session': verified['result']['session_id'],
               'payload': verified['result']['payload'], 'receipt': receipt['artifact_root']},
         'Saved judgment differs from actual verified receipt')


def validate_consumer_assertions(source):
    """Protect the known immutable full consumer regression, not the fix shape."""
    same(ast.dump(ast.parse(source), include_attributes=False),
         ast.dump(ast.parse(CONSUMER_TEST), include_attributes=False),
         'Full-response consumer assertion was weakened or replaced')


def validate_consumer(folder, *, verify=verify_receipt):
    folder = checked_path(Path(folder).resolve(), directory=True)
    with consumer_store(folder) as store:
        # Recompute with current public runtime policy, source and actual checks.
        current = GateService(store).gate(TASK)
        validate_saved_gate(load(folder / 'result.json'), current)
        task = TaskService(store).snapshot(TASK)
        plan = TaskService(store).execution_plan(TASK)
        require(task['state'] == 'COMPLETED' and task['contract'].get('bugfix_evidence') == {'version': 1, 'required': True},
                'Expected completed bugfix-evidence consumer task')
        baseline = read_record(store.db, 'baseline', TASK)['payload']
        before = read_record(store.db, 'baseline_implementation', TASK)['payload']
        require(before and Path(before['artifact_root']).is_relative_to(store.root / 'tasks' / TASK / 'sessions'),
                'Authenticated consumer before-source receipt missing')
        # Historical source authentication is the only legacy-policy exception.
        # It never grants current acceptance or replaces a failed command run.
        original = verify(before, expected_bindings=before['bindings'], require_current_policy=False)
        independent(before, original)
        require(before['role'] == 'implementation' and before['bindings']['fingerprint'] == baseline['fingerprint'],
                'Before-source receipt does not bind the baseline')
        for field, key in (('contract_sha256', 'contract_hash'), ('proposal_sha256', 'proposal_hash'), ('plan_sha256', 'plan_hash')):
            require(before['bindings'][field] == current[key], 'Before-source task inputs differ: ' + field)
        original_files = packet_files(load(Path(before['artifact_root']) / 'request.json'))
        for path in ('client.py', 'tests/test_consumer.py', 'API_CONTRACT.md', 'baseline.txt'):
            require(path in original_files and original_files[path]['sha256'] == baseline['files'][path].removesuffix(':x'),
                    'Before-source packet/baseline hash mismatch: ' + path)
        sources = {name: (store.repo / name).read_bytes().decode('utf-8')
                   for name in ('client.py', 'tests/test_consumer.py', 'API_CONTRACT.md', 'baseline.txt')}
        for name in ('tests/test_consumer.py', 'API_CONTRACT.md', 'baseline.txt'):
            require(sha256((store.repo / name).read_bytes()) == baseline['files'][name].removesuffix(':x'),
                    'Authoritative consumer fixture/test/baseline changed: ' + name)
        # The known fixture's entire consumer assertion and observed wire payload
        # must remain meaningful. Production fix shape is assessed independently.
        validate_consumer_assertions(sources['tests/test_consumer.py'])
        require('FAIL: test_complete_response_reaches_consumer' in sources['baseline.txt']
                and 'FAILED (failures=1)' in sources['baseline.txt'], 'Recorded original consumer failure is missing')
        dossier = read_record(store.db, 'test_adequacy_plan', TASK)['payload']
        refs = [ref for bug in dossier['bugfixes'] for ref in bug['trace'] if ref.get('version') == 'before']
        require(any(ref['path'] == 'client.py' for ref in refs), 'Explicit authenticated before trace missing')
        for ref in refs:
            require(ref['path'] in original_files and ref['locator'] in original_files[ref['path']]['content'],
                    'Before trace locator is not in authenticated original source')
        bundle = read_record(store.db, 'checks', TASK)['payload']
        specs = {row['id']: row for row in plan['checks']}
        checks = {row['id']: row for row in bundle['results']}
        require('consumer' in checks and specs['consumer']['kind'] == 'test' and specs['consumer']['parser'] == 'unittest',
                'Actual consumer unittest check missing')
        check = checks['consumer']
        require(check['argv'] == check['configured_argv'] == specs['consumer']['argv']
                and check['argv'][1:] == ['-B', '-m', 'unittest', 'discover', '-s', 'tests', '-v'],
                'Consumer command differs from the actual approved fixture check')
        identities = [(before['execution_id'], before['session_id'])]
        judgments = load(folder / 'judgments.json')
        same(sorted(judgments), ['ac', 'adequacy', 'review'], 'Consumer judgment roles differ')
        raw_judgments = {}
        for role in ('adequacy', 'ac', 'review'):
            receipt = read_record(store.db, 'verification', TASK + ':' + role)['payload']
            require(receipt and Path(receipt['artifact_root']).is_relative_to(store.root / 'tasks' / TASK / 'sessions'),
                    'Consumer role receipt missing/outside task: ' + role)
            verified = verify(receipt, require_current_policy=True)
            independent(receipt, verified)
            require(verified.get('policy_current') is True, 'Current consumer role runtime policy required')
            validate_judgment(judgments[role], receipt, verified)
            files = packet_files(load(Path(receipt['artifact_root']) / 'request.json'))
            require(files['_ase_before/client.py']['content'] == original_files['client.py']['content'],
                    'Judgment before-source differs from authenticated original')
            for name, content in sources.items():
                require(files[name]['content'] == content, 'Judgment current fixture source differs: ' + name)
            identities.append((receipt['execution_id'], receipt['session_id']))
            raw_judgments[role] = judgments[role]
        implementation = read_record(store.db, 'implementation', TASK)['payload']
        require(implementation and implementation['role'] == 'implementation'
                and Path(implementation['artifact_root']).is_relative_to(store.root / 'tasks' / TASK / 'sessions'),
                'Native implementation capture missing/outside task')
        implemented = verify(implementation, require_current_policy=True)
        independent(implementation, implemented)
        require(implemented.get('policy_current') is True, 'Current implementation runtime policy required')
        for field, key in (('contract_sha256', 'contract_hash'), ('proposal_sha256', 'proposal_hash'),
                           ('plan_sha256', 'plan_hash'), ('fingerprint', 'fingerprint')):
            require(implementation['bindings'][field] == current[key], 'Current consumer implementation inputs differ: ' + field)
        if implementation['execution_id'] != before['execution_id']:
            identities.append((implementation['execution_id'], implementation['session_id']))
        validate_identities(identities)
        return {'scope': 'Executed disposable local consumer fixture; no remote service claim.',
            'identities': identities, 'current_gate': current,
            'before_source': original_files['client.py']['content'], 'current_sources': sources,
            'actual_checks': bundle, 'judgments': raw_judgments}


class NativeEvidenceIntegrityChecks(unittest.TestCase):
    """Static case identities bind verbose output to this source file."""

    def __init__(self, methodName='runTest', *, consumer=None, cases=None, cache=None):
        super().__init__(methodName)
        self._consumer = consumer
        self._cases = cases
        self._cache = cache if cache is not None else {}

    def _evidence(self, kind):
        if kind not in self._cache:
            value = validate_consumer(self._consumer) if kind == 'consumer' else validate_cases(self._cases)
            self._cache[kind] = value
        return self._cache[kind]

    def _advisory_case(self, name):
        require(sum(row['id'] == name for row in self._evidence('advisory')['items']) == 1,
                'Missing unique raw advisory output: ' + name)

    def test_executed_current_consumer(self):
        self._evidence('consumer')

    def test_distinct_native_execution_and_provider_sessions(self):
        validate_identities(self._evidence('consumer')['identities'] + self._evidence('advisory')['identities'])

    def test_advisory_capture__symptom_only_merger(self):
        """Capture integrity only; independent reading required: symptom-only-merger"""
        self._advisory_case('symptom-only-merger')

    def test_advisory_capture__caller_ignores_return(self):
        """Capture integrity only; independent reading required: caller-ignores-return"""
        self._advisory_case('caller-ignores-return')

    def test_advisory_capture__partial_response_contract(self):
        """Capture integrity only; independent reading required: partial-response-contract"""
        self._advisory_case('partial-response-contract')

    def test_advisory_capture__serialization_loss(self):
        """Capture integrity only; independent reading required: serialization-loss"""
        self._advisory_case('serialization-loss')

    def test_advisory_capture__unknown_useful_probe(self):
        """Capture integrity only; independent reading required: unknown-useful-probe"""
        self._advisory_case('unknown-useful-probe')

    def test_advisory_capture__missing_contract(self):
        """Capture integrity only; independent reading required: missing-contract"""
        self._advisory_case('missing-contract')


def build_suite(consumer, cases):
    cache = {}
    class IntegritySuite(unittest.TestSuite):
        def run(self, result, debug=False):
            outcome = super().run(result, debug)
            # Complete every unittest status line before emitting data on stdout.
            # Real command capture merges streams; data must not split a case's
            # identity from its success marker and invalidate case-level proof.
            for kind, value in cache.items():
                print(json.dumps({'evidence_integrity_only': True, kind: value}, ensure_ascii=False, separators=(',', ':')), flush=True)
            return outcome
    return IntegritySuite(NativeEvidenceIntegrityChecks(name, consumer=consumer, cases=cases, cache=cache)
        for name in unittest.defaultTestLoader.getTestCaseNames(NativeEvidenceIntegrityChecks))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--consumer', type=Path, required=True, help='Existing executed consumer tree with repo/, state/, result.json and judgments.json')
    parser.add_argument('--cases', type=Path, required=True, help='Existing one-repetition live advisory capture tree')
    args = parser.parse_args(argv)
    print('Evidence integrity/currentness checks only. Advisory correctness requires independent semantic adjudication.', flush=True)
    return 0 if unittest.TextTestRunner(stream=sys.stdout, verbosity=2).run(build_suite(args.consumer, args.cases)).wasSuccessful() else 1


if __name__ == '__main__':
    raise SystemExit(main())
