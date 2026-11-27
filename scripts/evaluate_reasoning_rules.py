"""Capture, and optionally live-run, advisory workflow decision/artifact cases.

Example (PowerShell; replace the executable path with the installed Codex path):
  python -B scripts/evaluate_reasoning_rules.py --executable C:/path/codex.exe `
    --cases tests/fixtures/evidence-decisions-cases.json `
    --output outputs/reasoning-evaluation/my-fresh-run --run-live

Without --run-live only immutable inputs are captured; no backend is probed.
This is a packet-only simulation, not task acceptance or infrastructure execution.
Review the captured outputs independently against rubric.json. The script never
assigns a behavioral PASS, writes a task database, installs skills, or mutates a
service. Existing Runner receipts establish transport and input integrity only.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from ase.adapters.codex import CodexAdapter
from ase.protocol import canonical, packet_prompt, sha256
from ase.runner import Runner, verify_receipt

DEFAULT_SKILLS = (
    'task-intake', 'task-discovery', 'task-orchestrator', 'project-context-manager',
    'communication-assistant', 'independent-code-reviewer', 'independent-ac-verifier', 'quality-gate',
)


def stamp():
    return datetime.now(timezone.utc).isoformat()


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('xb') as stream:
        stream.write(canonical(value))


def record(path, content):
    return {'path': path, 'content': content, 'sha256': sha256(content.encode('utf-8'))}


def object_schema(properties):
    return {'type': 'object', 'properties': properties, 'required': list(properties), 'additionalProperties': False}


def result_schema(count):
    string = {'type': 'string'}
    strings = {'type': 'array', 'items': string}
    item = object_schema({
        'id': string, 'decision': string, 'next_operation': string, 'artifact': string,
        'owner_message': string, 'evidence': strings, 'limits': strings,
    })
    return object_schema({'items': {'type': 'array', 'minItems': count, 'maxItems': count, 'items': item}})


def capture(args):
    if args.output.exists():
        raise ValueError('Select a fresh output directory; captured runs are never overwritten')
    fixture_bytes = args.cases.read_bytes()
    fixture = json.loads(fixture_bytes)
    cases, rubric = fixture['cases'], fixture['rubric']
    ids = [item['id'] for item in cases]
    if not cases or len(set(ids)) != len(ids) or set(ids) != set(rubric):
        raise ValueError('Cases and separately withheld rubric must have matching unique IDs')
    files, sources = [], []
    guide_paths = args.guide or [ROOT / 'skills' / name for name in DEFAULT_SKILLS]
    paths = []
    for selected in guide_paths:
        if selected.is_dir():
            paths.extend(selected.rglob('*.md'))
        elif selected.is_file():
            paths.append(selected)
        else:
            raise ValueError('Missing guide source: ' + str(selected))
    if not paths:
        raise ValueError('At least one actual guide body is required')
    for index, path in enumerate(sorted(set(path.resolve() for path in paths))):
        raw = path.read_bytes()
        content = raw.decode('utf-8-sig').replace('\r\n', '\n')
        try:
            packet_path = path.relative_to(ROOT).as_posix()
        except ValueError:
            packet_path = 'guidance/' + str(index).zfill(3) + '/' + path.name
        row = record(packet_path, content)
        files.append(row)
        sources.append({'source_path': str(path), 'packet_path': packet_path,
                        'source_bytes_sha256': sha256(raw), 'packet_text_sha256': row['sha256']})
    case_record = record('evaluation/work-items.json', canonical(cases).decode('utf-8'))
    files.append(case_record)
    requirements = [
        'You are performing the assigned task-agent work items in evaluation/work-items.json. Apply the supplied existing workflow skills and references as the active policy. Each item is independent; give every id exactly once in its supplied order.',
        'The observations and source snippets are stipulated state/evidence for a compact offline exercise. Decide what to do next, and prepare a concrete useful artifact: exact candidate code/SQL, handoff record, incident note, review response or internal draft as appropriate. Do not merely recite a checklist. Artifacts are proposed local deliverables, not claimed file writes or executed checks.',
        'Choose ordinary implementation details yourself under existing authorization. If a real owner decision is needed now, provide the concise owner message; otherwise owner_message is empty. Clearly distinguish proposed next operations from supplied executed evidence. Do not evaluate the skills or guess a grading rubric.',
        'No actual commands, service calls, messages or mutations are authorized in this packet exercise. Use only supplied evidence; any necessary next investigation is an intended operation, never a claim that you performed it.',
    ]
    packet = {'requirements': requirements, 'files': files, 'coverage': {
        'scope': 'Full selected workflow guide bodies and stipulated small work-item fixtures. Simulated decision/artifact behavior, not real infrastructure execution or main-task acceptance.',
        'included': [row['path'] for row in files], 'missing': [],
    }}
    digest = sha256(canonical(packet))
    bindings = {key: digest for key in ('fingerprint', 'contract_sha256', 'proposal_sha256', 'plan_sha256', 'check_sha256')}
    bindings['source_packet_sha256'] = digest
    request = {'role': 'diagnostic', 'project': {'root': str(ROOT)}, 'bindings': bindings,
               'schema': result_schema(len(cases)), 'source_packet': packet,
               'handoff': {'purpose': 'workflow behavioral exercise; advisory only',
                           'session_scope': 'fresh independent packet-only scenario batch'}}
    args.output.mkdir(parents=True)
    save(args.output / 'request-template.json', request)
    save(args.output / 'cases.json', cases)
    save(args.output / 'rubric.json', {'captured_at_utc': stamp(), 'criteria': rubric,
        'grading': 'Independent reading of actual outputs is required. No automatic behavioral verdict is generated.'})
    manifest = {'captured_at_utc': stamp(), 'case_source': str(args.cases),
                'case_file_sha256': sha256(fixture_bytes), 'case_content_sha256': case_record['sha256'],
                'rubric_sha256': sha256(canonical(rubric)), 'packet_sha256': digest,
                'request_sha256': sha256(canonical(request)),
                'prompt_sha256': sha256(packet_prompt(request).encode('utf-8')), 'guide_sources': sources,
                'intended_repetitions': args.repetitions,
                'scope': 'Raw Runner advisory exercise; no Workflow/task DB or main semantic approval.'}
    save(args.output / 'input-manifest.json', manifest)
    return request


def launch(args, request):
    with tempfile.TemporaryDirectory(prefix='ase-reasoning-behavior-trusted-') as trusted_name:
        return _launch_in_trusted(args, request, Path(trusted_name))


def _launch_in_trusted(args, request, trusted):
    # Use only the supplied installed executable and existing adapter flags.
    # No model, provider, reasoning effort or user config values are overridden.
    config = {'executable': str(args.executable), 'trusted_cwd': str(trusted),
              'max_input_bytes': 2000000, 'max_packet_bytes': 1000000, 'max_output_bytes': 2000000}
    save(args.output / 'launch.json', {'started_at_utc': stamp(), 'executable': str(args.executable),
        'trusted_cwd': str(trusted), 'timeout_seconds': args.timeout, 'repetitions': args.repetitions,
        'parallelism': args.parallelism, 'model_override': None, 'configuration_overrides': None,
        'adapter_flags': 'Existing CodexAdapter clean ephemeral packet-only read-only flags.',
        'input_manifest_sha256': sha256((args.output / 'input-manifest.json').read_bytes())})

    def one(index):
        folder = args.output / ('rep-' + str(index).zfill(2))
        value = Runner(config, folder / 'sessions').run(CodexAdapter(), request, timeout=args.timeout)
        valid = False
        if value['result']['status'] == 'COMPLETE':
            verify_receipt(value['receipt'], expected_bindings=request['bindings'])
            valid = True
        receipt = value['receipt']
        row = {'index': index, 'status': value['result']['status'], 'blocker': value['result'].get('blocker'),
               'receipt_integrity_verified': valid, 'execution_id': receipt['execution_id'],
               'session_id': receipt['session_id'], 'artifact_root': receipt['artifact_root'],
               'receipt_sha256': sha256((Path(receipt['artifact_root']) / 'receipt.json').read_bytes())}
        save(folder / 'capture.json', {**row, 'result': value['result']})
        print(json.dumps(row), flush=True)
        return row

    rows = []
    with ThreadPoolExecutor(max_workers=args.parallelism) as pool:
        for future in as_completed([pool.submit(one, index) for index in range(1, args.repetitions + 1)]):
            rows.append(future.result())
    session_ids = [row['session_id'] for row in rows if row['session_id']]
    if len(set(session_ids)) != len(session_ids):
        raise ValueError('Fresh session identities are not unique')
    save(args.output / 'sessions-summary.json', {'ended_at_utc': stamp(),
        'sessions': sorted(rows, key=lambda row: row['index']),
        'notice': 'Transport completion/integrity is not a behavioral verdict; independently grade raw outputs.'})
    return 0 if all(row['receipt_integrity_verified'] for row in rows) else 3


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--executable', type=Path, required=True, help='Absolute installed Codex executable; no model/config overrides')
    parser.add_argument('--cases', type=Path, required=True, help='Case and withheld rubric JSON file')
    parser.add_argument('--output', type=Path, required=True, help='Fresh output directory; never overwritten')
    parser.add_argument('--guide', type=Path, action='append', help='Explicit guide file/directory; repeatable; defaults to relevant canonical skills including references/assets')
    parser.add_argument('--repetitions', type=int, default=5)
    parser.add_argument('--parallelism', type=int, choices=(1, 2), default=2)
    parser.add_argument('--timeout', type=int, default=900)
    parser.add_argument('--run-live', action='store_true', help='Explicitly opt in to real installed-Codex sessions')
    args = parser.parse_args()
    if not args.executable.is_absolute() or not args.executable.is_file():
        parser.error('--executable must be an absolute existing installed executable')
    if not 1 <= args.repetitions <= 20 or not 1 <= args.timeout <= 3600:
        parser.error('repetitions must be 1..20; timeout must be 1..3600 seconds')
    args.cases = args.cases.resolve(strict=True)
    args.output = args.output.resolve()
    request = capture(args)
    print(json.dumps({'phase': 'captured', 'timestamp': stamp(), 'output': str(args.output),
                      'request_sha256': sha256(canonical(request)), 'live': args.run_live}), flush=True)
    return launch(args, request) if args.run_live else 0


if __name__ == '__main__':
    raise SystemExit(main())
