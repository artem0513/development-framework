"""Historical proof transport: real local checks; model replies are fixtures."""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

import test_lead_review as support
from ase.native_paths import logical_path
from ase.protocol import canonical, sha256
from ase.state import hash_value, read_record, transaction, write_record
from ase.store import Store


class LeadInputTests(unittest.TestCase):
    def setUp(self):
        self.case = support.LeadReviewTests(); self.case.setUp()
        self.addCleanup(self.case.doCleanups)

    def report(self):
        c = self.case
        packet = c.controller._packet('lead')
        files = {r['path']: r for r in packet['files']}
        report = json.loads(files['_ase_context/lead_review.json']['content'])
        return report, files

    def observe_log(self, ticket):
        c = self.case; value = c.payload(ticket)
        proof = {'check_id': 'ready', 'source': ticket['handoff']['base']['check_sources']['ready'],
            'assertion': 'The executed current ready probe is supplied in full', 'kind': 'check'}
        value['assessment']['risks'][0]['evidence'].append(proof)
        return value

    def test_referenced_source_and_log_survive_fix_and_original_log_removal(self):
        c = self.case; first = c.prepare(); log = Path(read_record(c.store.db, 'checks', 'lead')['payload']['results'][0]['log'])
        old_source = (c.root/'guide.txt').read_bytes(); old_log = log.read_bytes()
        c.record(first, self.observe_log(first))
        (c.root/'guide.txt').write_text('A subsequently corrected guide'); log.unlink()
        c.controller.check('lead')
        report, files = self.report()
        inputs = report['passes'][0].get('inputs', {})
        self.assertEqual(inputs.get('status'), 'AVAILABLE', 'Old critiques need preserved inputs, not only hashes')
        for source, raw in [('guide.txt', old_source), ('_ase_context/logs/ready.txt', old_log)]:
            ref = inputs['sources'][source]
            self.assertEqual(files[ref['packet_path']]['content'].encode(), raw)
            self.assertEqual(ref['sha256'], sha256(raw))
        self.assertEqual(files['guide.txt']['content'], 'A subsequently corrected guide')

    def test_equal_historical_bodies_are_deduplicated_against_current_packet(self):
        c = self.case; c.finish_three(); report, files = self.report()
        for item in report['passes']:
            self.assertEqual(item.get('inputs', {}).get('status'), 'AVAILABLE')
            ref = item['inputs']['sources']['guide.txt']
            self.assertEqual(ref['packet_path'], 'guide.txt')
        hashes = [row['sha256'] for path, row in files.items() if path.startswith('_ase_lead_history/')]
        self.assertEqual(len(hashes), len(set(hashes)))

    def test_input_sidecar_is_bound_and_tampering_blocks_readiness(self):
        c = self.case; c.finish_three()
        reference = c.service.snapshot('lead')['payload']['passes'][0]
        self.assertIn('inputs', reference, 'Input manifest must join the committed pass reference')
        before = c.service.binding('lead')
        manifest = Path(reference['inputs']['path'])
        value = json.loads(manifest.read_bytes())
        blob = Path(next(iter(value['sources'].values()))['path'])
        blob.write_bytes(b'not the captured input')
        self.assertEqual(c.service.gate('lead')['status'], 'BLOCKED')
        with self.assertRaises(ValueError): c.service.binding('lead')
        self.assertIsInstance(before, str)

    def test_input_manifest_cannot_point_outside_original_attempt(self):
        c = self.case; c.finish_three(); old = c.service.snapshot('lead'); value = copy.deepcopy(old['payload'])
        self.assertIn('inputs', value['passes'][0])
        original = Path(value['passes'][0]['inputs']['path']); outside = c.base/'outside-inputs.json'
        outside.write_bytes(original.read_bytes()); value['passes'][0]['inputs']['path'] = str(outside)
        with transaction(c.store.db): write_record(c.store.db, 'lead_review', 'lead', value, expected_revision=old['revision'])
        self.assertEqual(c.service.gate('lead')['status'], 'BLOCKED')

    def test_log_race_preserves_model_capture_and_recovers_without_native_replay(self):
        c = self.case; ticket = c.prepare(); data = self.observe_log(ticket)
        result = read_record(c.store.db, 'checks', 'lead')['payload']['results'][0]
        log = Path(result['log']); raw = log.read_bytes(); log.write_bytes(b'{"ready":false}')
        with patch.object(c.service, '_base', return_value=ticket['handoff']['base']):
            with self.assertRaises(ValueError): c.record(ticket, data)
        self.assertTrue(c.service._path('lead', ticket['handoff']['attempt_id']).exists())
        self.assertIsNotNone(c.service.snapshot('lead')['payload']['inflight'])
        log.write_bytes(raw); c.service.recover('lead')
        self.assertEqual(c.service.gate('lead')['completed_passes'], 1)
        before = c.service.snapshot('lead'); self.assertEqual(c.service.recover('lead'), before)

    def test_source_hash_race_cannot_be_archived_as_the_inspected_input(self):
        c = self.case; ticket = c.prepare(); data = c.payload(ticket)
        original = (c.root/'guide.txt').read_bytes(); (c.root/'guide.txt').write_bytes(b'changed during capture')
        with patch.object(c.service, '_base', return_value=ticket['handoff']['base']):
            with self.assertRaises(ValueError): c.record(ticket, data)
        self.assertTrue(c.service._path('lead', ticket['handoff']['attempt_id']).exists())
        (c.root/'guide.txt').write_bytes(original); c.service.recover('lead')
        report, files = self.report(); ref = report['passes'][0]['inputs']['sources']['guide.txt']
        self.assertEqual(files[ref['packet_path']]['content'].encode(), original)

    def legacy_reference(self):
        c = self.case; ticket = c.prepare(); c.record(ticket)
        old = c.service.snapshot('lead'); value = copy.deepcopy(old['payload']); reference = value['passes'][0]
        path = Path(reference['path']); capture = json.loads(path.read_bytes())
        capture['ticket'].pop('input_basis', None); capture['ticket']['base'].pop('source_hashes', None)
        path.write_bytes(canonical(capture)); reference.pop('inputs', None); reference['sha256'] = hash_value(capture)
        with transaction(c.store.db):
            write_record(c.store.db, 'lead_review', 'lead', value, expected_revision=old['revision'])
            key = 'lead:' + capture['ticket']['id']
            projected = read_record(c.store.db, 'lead_review_capture', key)
            write_record(c.store.db, 'lead_review_capture', key, {**reference, 'credited': True}, expected_revision=projected['revision'])
        return capture

    def test_legacy_input_can_be_correlated_only_to_exact_authenticated_runner_request(self):
        c = self.case; capture = self.legacy_reference()
        (c.root/'guide.txt').write_text('Current data must not be substituted for old data'); c.controller.check('lead')
        report, files = self.report(); inputs = report['passes'][0].get('inputs', {})
        self.assertEqual(inputs.get('status'), 'AVAILABLE')
        self.assertEqual(inputs['provenance']['kind'], 'archived_runner_request')
        ref = inputs['sources']['guide.txt']
        self.assertEqual(files[ref['packet_path']]['content'], 'The approved guide')
        self.assertEqual(inputs['provenance']['check_sha256'], capture['ticket']['base']['check_hash'])

    def test_missing_legacy_input_is_explicit_not_a_copy_of_current_source(self):
        c = self.case; self.legacy_reference()
        receipt = read_record(c.store.db, 'implementation', 'lead')['payload']
        Path(receipt['artifacts']['request']['path']).unlink()
        (c.root/'guide.txt').write_text('Only current source remains'); c.controller.check('lead')
        report, _ = self.report(); inputs = report['passes'][0].get('inputs', {})
        self.assertEqual(inputs.get('status'), 'MISSING')
        self.assertTrue(inputs['missing'])

    def test_secret_source_is_not_copied_into_new_input_archive(self):
        c = self.case; sample_value = 'password=' + 'syntheticOnlyValue-0123456789'
        (c.root/'guide.txt').write_text(sample_value); c.controller.check('lead'); ticket = c.prepare()
        with self.assertRaisesRegex(ValueError, 'credential|secret'):
            c.record(ticket)
        root = logical_path(c.service._path('lead', ticket['handoff']['attempt_id'])).parent
        for path in root.rglob('*.txt'):
            self.assertNotIn(sample_value.encode(), path.read_bytes())
        self.assertTrue(c.service._path('lead', ticket['handoff']['attempt_id']).exists())

    def test_process_death_after_response_before_input_seal_recovers_exact_response(self):
        c = self.case; ticket = c.prepare(); data = c.payload(ticket)
        source = c.base/'input.json'; source.write_text(json.dumps({'ticket': ticket, 'data': data}))
        code = """import json,os,sys
from ase.store import Store
from ase.lead_review import LeadReview
from ase.lead_inputs import LeadInputs
x=json.load(open(sys.argv[3]));s=Store(sys.argv[1],sys.argv[2]);r=LeadReview(s)
LeadInputs.seal=lambda *args:os._exit(87)
r.record('lead',x['ticket']['handoff']['attempt_id'],x['data'],thread_id='original-main',expected_revision=x['ticket']['revision'])
"""
        p = subprocess.run([sys.executable, '-B', '-c', code, str(c.root), str(c.home), str(source)],
            cwd=Path(__file__).resolve().parents[1], capture_output=True, timeout=20)
        self.assertEqual(p.returncode, 87, p.stderr.decode(errors='replace'))
        c.store.close(); c.store = Store(c.root, c.home); c.service = c.module.LeadReview(c.store)
        c.service.recover('lead'); report = c.service.assessment('lead')
        self.assertEqual(report['passes'][0]['inputs']['status'], 'AVAILABLE')
        self.assertEqual(c.service.gate('lead')['completed_passes'], 1)

    def test_structured_secret_body_rejected_before_any_blob_copy(self):
        c = self.case; sample_value = 'syntheticOnlyValue-' + '0123456789'
        (c.root/'guide.txt').write_text(json.dumps({'connection': {'password': sample_value}}))
        c.controller.check('lead'); ticket = c.prepare()
        with self.assertRaisesRegex(ValueError, 'credential|secret'): c.record(ticket)
        directory = c.service._path('lead', ticket['handoff']['attempt_id']).parent
        self.assertTrue(c.service._path('lead', ticket['handoff']['attempt_id']).exists())
        self.assertEqual(list(directory.glob('input-*.txt')), [])

    def test_structured_secret_in_prepared_checks_rejected_before_basis_copy(self):
        c = self.case
        old = read_record(c.store.db, 'checks', 'lead'); checks = copy.deepcopy(old['payload'])
        checks['observation'] = {'connection': {'api_key': 'syntheticValue-' + '0123456789'}}
        with transaction(c.store.db): write_record(c.store.db, 'checks', 'lead', checks, expected_revision=old['revision'])
        with self.assertRaisesRegex(ValueError, 'credential|secret'): c.prepare()
        directory = c.store.root/'tasks/lead/lead-review'
        self.assertEqual(list(directory.rglob('basis.json')), [])

    def test_plain_empty_and_credential_reference_json_bodies_remain_valid(self):
        from ase.lead_inputs import _body
        for raw in (b'', b' \r\n', b'ordinary source', b'{"credential_ref":"local:service"}', b'{"password":null}'):
            self.assertEqual(_body(raw).encode(), raw)
        # Validate duplicate pairs before JSON's last-key-wins conversion.
        raw = ('{"password":"' + 'syntheticValue-0123456789' + '","password":null}').encode()
        with self.assertRaisesRegex(ValueError, 'credential|secret'): _body(raw)

    def test_json_shaped_prefix_in_plain_code_does_not_create_false_blocker(self):
        import ast
        c = self.case
        raw = ('{"password":"' + 'example-only-value' + '"}\nprint("Example documentation")\n').encode()
        ast.parse(raw)
        with self.assertRaises(json.JSONDecodeError): json.loads(raw)
        (c.root/'guide.txt').write_bytes(raw); c.controller.check('lead')
        ticket = c.prepare(); c.record(ticket)
        report, files = self.report()
        ref = report['passes'][0]['inputs']['sources']['guide.txt']
        self.assertEqual(files[ref['packet_path']]['content'].encode(), raw)
        self.assertEqual(c.service.gate('lead')['completed_passes'], 1)

    def test_excessive_json_nesting_has_explicit_bounded_failure(self):
        from ase.lead_inputs import _body
        depth = sys.getrecursionlimit() + 1000
        raw = b'[' * depth + b'null' + b']' * depth
        with self.assertRaisesRegex(ValueError, 'nest|bound'): _body(raw)

    def test_schema_and_boolean_metadata_preserve_exact_source_proof(self):
        c = self.case
        raw = canonical({'properties': {'password': {'type': 'string'}}, 'secret': False, 'token': 7})
        (c.root/'guide.txt').write_bytes(raw); c.controller.check('lead')
        c.record(c.prepare()); report, files = self.report()
        ref = report['passes'][0]['inputs']['sources']['guide.txt']
        self.assertEqual(files[ref['packet_path']]['content'].encode(), raw)
        self.assertEqual(c.service.gate('lead')['completed_passes'], 1)

    def test_boolean_check_metadata_does_not_block_prepared_basis(self):
        c = self.case; old = read_record(c.store.db, 'checks', 'lead'); checks = copy.deepcopy(old['payload'])
        checks['observation'] = {'secret': False, 'token': 7}
        with transaction(c.store.db): write_record(c.store.db, 'checks', 'lead', checks, expected_revision=old['revision'])
        c.record(c.prepare())
        self.assertEqual(c.service.gate('lead')['completed_passes'], 1)

    def test_secret_strings_inside_nested_arrays_are_still_rejected(self):
        from ase.lead_inputs import _body
        for key in ('password', 'api_key', 'private_key'):
            raw = canonical({key: [[None, False, 'syntheticValue-' + '0123456789']]})
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, 'credential|secret'): _body(raw)

    def test_process_death_after_input_seal_before_sql_uses_exact_existing_blobs(self):
        c = self.case; ticket = c.prepare(); data = c.payload(ticket)
        source = c.base/'input.json'; source.write_text(json.dumps({'ticket': ticket, 'data': data}))
        code = """import json,os,sys
from ase.store import Store
from ase.lead_review import LeadReview
from ase.lead_inputs import LeadInputs
x=json.load(open(sys.argv[3]));s=Store(sys.argv[1],sys.argv[2]);r=LeadReview(s)
original=LeadInputs.seal
def interrupt(self,*args):
 original(self,*args);os._exit(88)
LeadInputs.seal=interrupt
r.record('lead',x['ticket']['handoff']['attempt_id'],x['data'],thread_id='original-main',expected_revision=x['ticket']['revision'])
"""
        p = subprocess.run([sys.executable, '-B', '-c', code, str(c.root), str(c.home), str(source)],
            cwd=Path(__file__).resolve().parents[1], capture_output=True, timeout=20)
        self.assertEqual(p.returncode, 88, p.stderr.decode(errors='replace'))
        directory = c.service._path('lead', ticket['handoff']['attempt_id']).parent
        sealed = {str(path): sha256(path.read_bytes()) for path in directory.glob('*')}
        c.store.close(); c.store = Store(c.root, c.home); c.service = c.module.LeadReview(c.store)
        c.service.recover('lead'); before = c.service.snapshot('lead')
        self.assertEqual(c.service.recover('lead'), before)
        self.assertEqual({str(path): sha256(path.read_bytes()) for path in directory.glob('*')}, sealed)
        self.assertEqual(c.service.gate('lead')['completed_passes'], 1)

    def test_exact_pending_input_prefix_recovers_and_conflicting_prefix_is_preserved(self):
        from ase.lead_inputs import _seal, _read
        path = self.case.base/'blob.txt'; pending = path.with_name('blob.txt.pending')
        pending.write_bytes(b'original')
        ref = _seal(path, b'original body', 100)
        self.assertEqual(_read(path, 100), b'original body')
        self.assertEqual(ref['sha256'], sha256(b'original body'))
        bad = self.case.base/'other.txt'; staging = bad.with_name('other.txt.pending')
        staging.write_bytes(b'unknown')
        with self.assertRaises(ValueError): _seal(bad, b'original body', 100)
        self.assertEqual(staging.read_bytes(), b'unknown'); self.assertFalse(bad.exists())

    def test_hardlinked_input_is_rejected_without_overwriting_other_file(self):
        from ase.lead_inputs import _seal, _read
        original = self.case.base/'original.txt'; original.write_bytes(b'original')
        alias = self.case.base/'linked.txt'; os.link(original, alias)
        with self.assertRaises(ValueError): _read(alias, 100)
        with self.assertRaises(ValueError): _seal(alias, b'original', 100)
        self.assertEqual(original.read_bytes(), b'original')

    def test_legacy_tampered_request_never_becomes_current_proof(self):
        c = self.case; self.legacy_reference()
        receipt = read_record(c.store.db, 'implementation', 'lead')['payload']
        path = Path(receipt['artifacts']['request']['path'])
        path.write_bytes(path.read_bytes() + b' ')
        report, _ = self.report()
        self.assertEqual(report['passes'][0]['inputs']['status'], 'MISSING')

    def test_missing_root_history_does_not_block_intermediate_stage_packet(self):
        c = self.case; self.legacy_reference()
        receipt = read_record(c.store.db, 'implementation', 'lead')['payload']
        Path(receipt['artifacts']['request']['path']).unlink()
        root = c.controller._packet('lead')
        self.assertTrue(any('Original-lead pass' in row for row in root['coverage']['missing']))
        stage = c.controller._packet('lead', 'finish')
        self.assertFalse(any('Original-lead' in row for row in stage['coverage']['missing']))
        self.assertNotIn('_ase_context/lead_review.json', {row['path'] for row in stage['files']})

    def test_source_size_bound_keeps_exact_response_uncredited_and_pending(self):
        from ase.discovery import record_discovery
        from ase.lead_inputs import MAX_SOURCE, _body
        from ase.source_coverage import PACKET_FILE_LIMIT
        c = self.case
        self.assertEqual(MAX_SOURCE, 512000)
        self.assertEqual(PACKET_FILE_LIMIT, MAX_SOURCE)
        # Exercise the historical-body guard itself at both exact edges. The
        # integrated path also enforces source_bytes' equal upstream bound.
        self.assertEqual(_body(b'x' * MAX_SOURCE), 'x' * MAX_SOURCE)
        with self.assertRaisesRegex(ValueError, 'per-source bound'):
            _body(b'x' * (MAX_SOURCE + 1))
        raw = b'x' * (MAX_SOURCE + 1)
        (c.root/'oversize.txt').write_bytes(raw)
        dependency = {'path': 'oversize.txt', 'from': 'guide.txt',
            'reason': 'Optional reference explicitly inspected by this critique', 'required': False}
        record_discovery(c.store, 'lead', {'scope': {'resources': ['guide.txt'], 'dependencies': [dependency]},
            'resources': [{'path': 'guide.txt', 'status': 'INSPECTED'},
                          {'path': 'oversize.txt', 'status': 'INSPECTED'}],
            'dependencies': [dependency], 'candidate_checks': [],
            'selected_tools': [], 'selected_skills': [], 'limitations': []}, expected_revision=0)
        c.controller.check('lead'); ticket = c.prepare(); data = c.payload(ticket)
        self.assertIn('oversize.txt', ticket['handoff']['base']['source_paths'])
        data['assessment']['risks'][0]['evidence'].append({'check_id': 'ready', 'source': 'oversize.txt',
            'assertion': 'This critique also refers to this complete optional reference', 'kind': 'check'})
        before = c.service.snapshot('lead'); attempt = ticket['handoff']['attempt_id']
        with self.assertRaisesRegex(ValueError, 'per-file packet bound|per-source bound'):
            c.record(ticket, data)
        path = c.service._path('lead', attempt); sealed = path.read_bytes()
        self.assertEqual(json.loads(sealed), {'ticket': before['payload']['inflight'], 'result': data})
        self.assertEqual(c.service.snapshot('lead'), before)
        self.assertIsNone(read_record(c.store.db, 'lead_review_capture', 'lead:' + attempt)['payload'])
        self.assertEqual(c.service.gate('lead')['completed_passes'], 0)
        self.assertEqual(c.service.gate('lead')['status'], 'BLOCKED')
        self.assertFalse(c.store.db.in_transaction)
        self.assertEqual(list(path.parent.glob('input-*.txt')), [])
        with self.assertRaisesRegex(ValueError, 'Existing unsettled|in.flight|reconcile'):
            c.prepare()
        # A permanent actual cap stays blocked. Recovery must not discard this
        # response, award credit, or silently replace the original native turn.
        with self.assertRaisesRegex(ValueError, 'per-file packet bound|per-source bound'):
            c.service.recover('lead')
        self.assertEqual(path.read_bytes(), sealed)
        self.assertEqual(c.service.snapshot('lead'), before)
        self.assertIsNone(read_record(c.store.db, 'lead_review_capture', 'lead:' + attempt)['payload'])

    def test_total_snapshot_bound_keeps_exact_response_uncredited_and_pending(self):
        from ase.discovery import record_discovery
        from ase.lead_inputs import MAX_SOURCE, MAX_TOTAL
        c = self.case
        self.assertEqual(MAX_SOURCE, 512000)
        self.assertEqual(MAX_TOTAL, 8 * 1024 * 1024)
        paths, hashes, dependencies = [], set(), []
        for index in range(17):
            path = 'reference-' + str(index) + '.txt'
            prefix = ('Distinct optional historical reference ' + str(index) + '\n').encode()
            raw = prefix + b'x' * (500000 - len(prefix))
            self.assertLess(len(raw), MAX_SOURCE)
            (c.root/path).write_bytes(raw); paths.append(path); hashes.add(sha256(raw))
            dependencies.append({'path': path, 'from': 'guide.txt',
                'reason': 'Optional reference explicitly inspected by this critique', 'required': False})
        self.assertEqual(len(hashes), 17, 'Unique bodies must not collapse under blob deduplication')
        self.assertGreater(17 * 500000, MAX_TOTAL)
        record_discovery(c.store, 'lead', {'scope': {'resources': ['guide.txt'], 'dependencies': dependencies},
            'resources': [{'path': path, 'status': 'INSPECTED'} for path in ['guide.txt', *paths]],
            'dependencies': dependencies, 'candidate_checks': [],
            'selected_tools': [], 'selected_skills': [], 'limitations': []}, expected_revision=0)
        c.controller.check('lead'); ticket = c.prepare(); data = c.payload(ticket)
        self.assertTrue(set(paths).issubset(ticket['handoff']['base']['source_paths']))
        # Optional inputs belong to the prepared source inventory; the initial
        # protocol implementation packet need not copy all 8.5 MB of bodies.
        receipt = read_record(c.store.db, 'implementation', 'lead')['payload']
        request = json.loads(Path(receipt['artifacts']['request']['path']).read_bytes())
        self.assertFalse(set(paths) & {row['path'] for row in request['source_packet']['files']})
        for path in paths:
            data['assessment']['risks'][0]['evidence'].append({'check_id': 'ready', 'source': path,
                'assertion': 'This critique refers to this distinct complete optional reference', 'kind': 'check'})
        before = c.service.snapshot('lead'); attempt = ticket['handoff']['attempt_id']
        with self.assertRaisesRegex(ValueError, 'Historical input set exceeds total bound'):
            c.record(ticket, data)
        path = c.service._path('lead', attempt); sealed = path.read_bytes()
        self.assertEqual(json.loads(sealed), {'ticket': before['payload']['inflight'], 'result': data})
        self.assertEqual(c.service.snapshot('lead'), before)
        self.assertIsNone(read_record(c.store.db, 'lead_review_capture', 'lead:' + attempt)['payload'])
        self.assertEqual(c.service.gate('lead')['completed_passes'], 0)
        self.assertEqual(c.service.gate('lead')['status'], 'BLOCKED')
        self.assertFalse(c.store.db.in_transaction)
        self.assertEqual(list(path.parent.glob('input-*.txt')), [])
        self.assertFalse((path.parent/'inputs.json').exists())
        with self.assertRaisesRegex(ValueError, 'Existing unsettled|in.flight|reconcile'):
            c.prepare()
        with self.assertRaisesRegex(ValueError, 'Historical input set exceeds total bound'):
            c.service.recover('lead')
        self.assertEqual(path.read_bytes(), sealed)
        self.assertEqual(c.service.snapshot('lead'), before)
        self.assertIsNone(read_record(c.store.db, 'lead_review_capture', 'lead:' + attempt)['payload'])

    def test_history_packet_overflow_is_explicit_and_blocks_runner_complete(self):
        from ase.adapters.codex import CodexAdapter
        from ase.discovery import record_discovery
        from ase.runner import Runner, verify_capture, verify_receipt
        import test_runner as transport
        c = self.case
        old = b'o' * 400000; current = b'c' * 400000; pad = b'p' * 300000
        self.assertGreater(len(old) + len(current) + len(pad), 1048576)
        (c.root/'guide.txt').write_bytes(old); c.controller.check('lead')
        ticket = c.prepare(); anchor = ticket['handoff']['base']['anchor_sha256']; c.record(ticket)
        reference = c.service.snapshot('lead')['payload']['passes'][0]
        directory = c.service._path('lead', ticket['handoff']['attempt_id']).parent
        sealed = {str(path): sha256(path.read_bytes()) for path in directory.iterdir() if path.is_file()}
        (c.root/'guide.txt').write_bytes(current); (c.root/'required-pad.txt').write_bytes(pad)
        dependency = {'path': 'required-pad.txt', 'from': 'guide.txt',
            'reason': 'Required current supporting source for this same approved guide', 'required': True}
        record_discovery(c.store, 'lead', {'scope': {'resources': ['guide.txt'], 'dependencies': [dependency]},
            'resources': [{'path': path, 'status': 'INSPECTED'} for path in ('guide.txt', 'required-pad.txt')],
            'dependencies': [dependency], 'candidate_checks': [],
            'selected_tools': [], 'selected_skills': [], 'limitations': []}, expected_revision=0)
        c.controller.check('lead')
        self.assertEqual(c.service.requirements('lead')['anchor_sha256'], anchor)
        # Stage packet is the same complete current inputs without root history.
        current_packet = c.controller._packet('lead', 'finish')
        self.assertLess(len(canonical(current_packet)), 1048576)
        root = c.controller._packet('lead'); files = {row['path']: row for row in root['files']}
        self.assertLessEqual(len(canonical(root)), 1048576)
        self.assertEqual(files['guide.txt']['content'].encode(), current)
        self.assertEqual(files['required-pad.txt']['content'].encode(), pad)
        self.assertFalse(any(path.startswith('_ase_lead_history/') for path in files))
        self.assertTrue(any('Historical lead-input packet budget exceeded' in reason for reason in root['coverage']['missing']))
        self.assertTrue({row['path'] for row in current_packet['files']}.issubset(files),
            'Budget handling must retain every supplied current input')
        for row in current_packet['files']:
            # Context metadata can identify root vs stage consumers; raw
            # current source and check bodies must remain byte-for-byte equal.
            if (not row['path'].startswith('_ase_context/')
                    or row['path'].startswith('_ase_context/logs/')
                    or row['path'] == '_ase_context/actual_checks.json'):
                self.assertEqual(files[row['path']], row, 'A complete current input was altered by history budgeting: ' + row['path'])
        lead = json.loads(files['_ase_context/lead_review.json']['content'])
        historical_ref = lead['passes'][0]['inputs']['sources']['guide.txt']
        self.assertEqual(historical_ref['sha256'], sha256(old))
        self.assertNotIn(historical_ref['packet_path'], files)
        self.assertEqual({str(path): sha256(path.read_bytes()) for path in directory.iterdir() if path.is_file()}, sealed)
        self.assertEqual(c.service.snapshot('lead')['payload']['passes'][0], reference)
        # A real fixture subprocess supplies a successful model terminal and
        # PASS payload. The real Runner normalizer must still block that reply
        # because this bounded consumer request lacks required history bytes.
        request = c.controller._request('lead', 'ac', transport.SCHEMA)
        self.assertEqual(request['source_packet']['coverage']['missing'], root['coverage']['missing'])
        runner = Runner({'executable': sys.executable,
            'executable_args': [str(c.base/'fixture.py'), 'success', 'codex'],
            'trusted_cwd': str(c.base/'trusted'), 'protocol_only': True}, c.store.root/'tasks/lead/sessions')
        outcome = runner.run(CodexAdapter(), request, timeout=10)
        result, receipt = outcome['result'], outcome['receipt']
        self.assertGreater(receipt['pid'], 0); self.assertEqual(receipt['exit_code'], 0)
        self.assertEqual(receipt['evidence_kind'], 'protocol_only')
        self.assertEqual(result['payload'], {'verdict': 'PASS'})
        events = [json.loads(line) for line in Path(receipt['artifacts']['stdout']['path']).read_text().splitlines()]
        self.assertTrue(any(event.get('type') == 'turn.completed' for event in events))
        self.assertEqual(result['status'], 'BLOCKED')
        self.assertEqual(result['blocker'], 'SOURCE_COVERAGE_MISSING')
        self.assertIn(next(reason for reason in root['coverage']['missing']
            if 'Historical lead-input packet budget exceeded' in reason), result['missing_coverage'])
        self.assertTrue(verify_capture(receipt)['valid'])
        with self.assertRaises(ValueError): verify_receipt(receipt)


    def test_shared_historical_scope_references_restore_original_intent_hashes(self):
        c = self.case
        paths = ['guide.txt'] + ['scope-' + str(i) + '.txt' for i in range(200)]
        for path in paths[1:]: (c.root/path).write_text('')
        task = c.tasks.snapshot('lead'); contract = copy.deepcopy(task['contract'])
        contract['scope']['resources'] = paths
        contract['stages'][0]['scope'] = copy.deepcopy(contract['scope'])
        c.tasks.revise('lead', contract, expected_revision=task['revision'])
        c.approve('Approved large source scope')
        task = c.tasks.snapshot('lead'); contract = copy.deepcopy(task['contract'])
        contract['acceptance'][0]['expected'] += '; retain the clarified title'
        c.tasks.revise('lead', contract, expected_revision=task['revision'])
        c.approve('Approved title clarification without source scope change')
        c.controller.check('lead'); c.finish_three()
        originals = c.service.requirements('lead')['approved_intents']
        report, files = self.report()
        intents = report['requirements']['approved_intents']
        historical = intents[-2]['contract']
        self.assertIn('/scope', report['contract_reference'])
        self.assertEqual(historical['scope'].get('pointer'), '/scope')
        self.assertEqual(historical['stages'][0]['scope'].get('pointer'), '/scope')
        self.assertNotIn('$ref', historical, 'Different approved contract must not become the current contract')
        def expand(value):
            if isinstance(value, dict) and '$ref' in value:
                source = files[value['$ref']]
                self.assertEqual(source['sha256'], value['sha256'])
                body = json.loads(source['content'])
                if 'pointer' in value:
                    self.assertEqual(value['pointer'], '/scope'); body = body['scope']
                    self.assertEqual(hash_value(body), value['value_sha256'])
                return copy.deepcopy(body)
            if isinstance(value, dict): return {key: expand(child) for key, child in value.items()}
            if isinstance(value, list): return [expand(child) for child in value]
            return value
        self.assertEqual(expand(intents), originals)
        for intent in expand(intents):
            self.assertEqual(hash_value(intent['contract']), intent['approval']['contract_hash'])
            self.assertEqual(hash_value({k: v for k, v in intent.items() if k != 'sha256'}), intent['sha256'])
        self.assertEqual(c.service.requirements('lead')['approved_intents'], originals)

    def test_shared_omission_note_retains_every_policy_and_required_gap(self):
        from ase.evidence.packets import _factor_omission_note
        from ase.protocol import validate_packet
        from ase.state import encode
        note = 'These optional bodies were not supplied or reviewed; exact hashes and stage impact remain visible.'
        rows = [{'path': 'optional-' + str(i) + '.txt', 'required': False,
            'relevant': True, 'reason': 'Known duplicate', 'limitation': note} for i in range(100)]
        content = encode(rows)
        metadata = {'path': '_ase_context/omitted_sources.json', 'content': content, 'sha256': sha256(content.encode())}
        source = {'path': 'guide.txt', 'content': 'Required guide', 'sha256': sha256(b'Required guide')}
        packet = {'requirements': ['Retain required source and explicit historical gap'],
            'files': [copy.deepcopy(source), metadata], 'coverage': {'scope': 'Explicit selected sources',
                'included': [source['path'], metadata['path']], 'missing': ['Required historical proof is absent']}}
        before = len(canonical(packet)); _factor_omission_note(packet)
        self.assertLess(len(canonical(packet)), before)
        self.assertEqual(packet['files'][0], source)
        self.assertEqual(packet['coverage']['missing'], ['Required historical proof is absent'])
        restored = [{**row, 'limitation': packet['coverage']['omitted_source_limitation']}
            for row in json.loads(metadata['content'])]
        self.assertEqual(restored, rows)
        self.assertEqual(metadata['sha256'], sha256(metadata['content'].encode()))
        validate_packet(packet)
        frozen = canonical(packet); _factor_omission_note(packet)
        self.assertEqual(canonical(packet), frozen)

    def test_scope_reference_restores_an_older_ordered_resource_subset(self):
        c = self.case; paths = ['guide.txt'] + ['old-scope-' + str(i) + '.txt' for i in range(200)]
        for path in paths[1:]: (c.root/path).write_text('')
        task = c.tasks.snapshot('lead'); contract = copy.deepcopy(task['contract'])
        contract['scope']['resources'] = paths; contract['stages'][0]['scope'] = copy.deepcopy(contract['scope'])
        c.tasks.revise('lead', contract, expected_revision=task['revision']); c.approve('Approved original large scope')
        previous = c.service.requirements('lead')['approved_intents'][-1]
        task = c.tasks.snapshot('lead'); contract = copy.deepcopy(task['contract'])
        (c.root/'new-scope.txt').write_text('')
        contract['scope']['resources'].append('new-scope.txt')
        contract['stages'][0]['scope'] = copy.deepcopy(contract['scope'])
        c.tasks.revise('lead', contract, expected_revision=task['revision']); c.approve('Approved added scope resource')
        c.controller.check('lead'); c.finish_three(); report, files = self.report()
        historical = copy.deepcopy(report['requirements']['approved_intents'][-2])
        for owner in [historical['contract'], *historical['contract']['stages']]:
            ref = owner['scope']; self.assertEqual(ref.get('exclude_resources'), ['new-scope.txt'])
            source = files[ref['$ref']]; self.assertEqual(source['sha256'], ref['sha256'])
            scope = json.loads(source['content'])['scope']
            scope['resources'] = [path for path in scope['resources'] if path not in ref['exclude_resources']]
            self.assertEqual(hash_value(scope), ref['value_sha256']); owner['scope'] = scope
        self.assertEqual(historical, previous)
        self.assertEqual(hash_value(historical['contract']), historical['approval']['contract_hash'])
        self.assertEqual(hash_value({k: v for k, v in historical.items() if k != 'sha256'}), historical['sha256'])
        self.assertEqual(c.service.requirements('lead')['approved_intents'][-2], previous)

    def test_line_delta_restores_unicode_crlf_insertions_deletions_and_final_line(self):
        from ase.lead_inputs import _line_delta, restore_line_delta
        lines = ['line ' + str(i) + ': проверка café\r\n' for i in range(400)]
        base = ''.join(lines).encode()
        old = ''.join([*lines[:20], 'earlier rule\r\n', *lines[21:150], *lines[152:300], 'inserted\r\n', *lines[300:-1], 'last without newline']).encode()
        row = {'path': 'guide.txt', 'content': base.decode(), 'sha256': sha256(base)}
        delta = _line_delta(old, row)
        self.assertIsNotNone(delta)
        self.assertLess(len(canonical(delta)), len(old))
        self.assertEqual(restore_line_delta(delta, row, sha256(old)), old)

    def test_large_similar_history_fits_packet_without_losing_raw_archives(self):
        from ase.lead_inputs import restore_line_delta
        from ase.discovery import record_discovery
        c = self.case
        lines = ['rule ' + str(i) + ': ' + 'x' * 380 + '\n' for i in range(1000)]
        old = ''.join(lines).encode(); (c.root/'guide.txt').write_bytes(old)
        c.controller.check('lead'); c.finish_three()
        references = copy.deepcopy(c.service.snapshot('lead')['payload']['passes'])
        manifests = {row['inputs']['path']: Path(row['inputs']['path']).read_bytes() for row in references}
        lines[500] = 'corrected material rule\n'; current = ''.join(lines).encode()
        (c.root/'guide.txt').write_bytes(current); (c.root/'required-pad.txt').write_bytes(b'p' * 300000)
        dependency = {'path': 'required-pad.txt', 'from': 'guide.txt', 'reason': 'Required current supporting source', 'required': True}
        record_discovery(c.store, 'lead', {'scope': {'resources': ['guide.txt'], 'dependencies': [dependency]},
            'resources': [{'path': p, 'status': 'INSPECTED'} for p in ('guide.txt', 'required-pad.txt')],
            'dependencies': [dependency], 'candidate_checks': [], 'selected_tools': [], 'selected_skills': [], 'limitations': []}, expected_revision=0)
        c.controller.check('lead'); packet = c.controller._packet('lead')
        self.assertLessEqual(len(canonical(packet)), 1048576)
        self.assertEqual(packet['coverage']['missing'], [])
        files = {row['path']: row for row in packet['files']}
        report = json.loads(files['_ase_context/lead_review.json']['content'])
        restored_refs = []
        for item in report['passes']:
            ref = item['inputs']['sources']['guide.txt']; restored_refs.append(ref)
            self.assertEqual(ref['encoding'], 'line_delta_v1')
            recipe = files[ref['packet_path']]
            self.assertEqual(recipe['sha256'], ref['representation_sha256'])
            delta = json.loads(recipe['content'])
            self.assertEqual(restore_line_delta(delta, files[delta['base_packet_path']], ref['sha256']), old)
        self.assertTrue(all(ref == restored_refs[0] for ref in restored_refs))
        self.assertEqual(files['guide.txt']['content'].encode(), current)
        self.assertEqual(files['required-pad.txt']['content'].encode(), b'p' * 300000)
        self.assertEqual(c.service.snapshot('lead')['payload']['passes'], references)
        for path, raw in manifests.items(): self.assertEqual(Path(path).read_bytes(), raw)
        for capture, ref in zip(c.service._captures(c.service.snapshot('lead')['payload']), references):
            self.assertEqual(c.service.inputs.load('lead', capture, ref['inputs'])['_bodies']['guide.txt'], old)

    def test_line_delta_rejects_changed_base_recipe_digest_and_unsafe_ranges(self):
        from ase.lead_inputs import _line_delta, restore_line_delta
        base = ''.join('line ' + str(i) + ': ' + 'x' * 40 + '\n' for i in range(300)).encode()
        old = base.replace(b'line 100:', b'prior 100:')
        row = {'path': 'guide.txt', 'content': base.decode(), 'sha256': sha256(base)}
        delta = _line_delta(old, row); self.assertIsNotNone(delta)
        with self.assertRaises(ValueError): restore_line_delta(delta, {**row, 'content': row['content'] + 'changed'}, sha256(old))
        with self.assertRaises(ValueError): restore_line_delta(delta, row, sha256(b'other'))
        broken = copy.deepcopy(delta); broken['edits'][0]['content'] += 'tampered'
        with self.assertRaises(ValueError): restore_line_delta(broken, row, sha256(old))
        for edit in ({'start': True, 'until': 1, 'content': ''}, {'start': -1, 'until': 1, 'content': ''},
                     {'start': 0, 'until': 999999, 'content': ''}):
            with self.subTest(edit=edit):
                broken = copy.deepcopy(delta); broken['edits'] = [edit]
                with self.assertRaises(ValueError): restore_line_delta(broken, row, sha256(old))
        broken = copy.deepcopy(delta); broken['edits'] = [broken['edits'][0], broken['edits'][0]]
        with self.assertRaises(ValueError): restore_line_delta(broken, row, sha256(old))

    def test_line_delta_keeps_unhelpful_and_excessive_line_inputs_unencoded(self):
        from ase.lead_inputs import _line_delta
        row = {'path': 'guide.txt', 'content': 'c' * 10000, 'sha256': sha256(b'c' * 10000)}
        self.assertIsNone(_line_delta(b'o' * 10000, row))
        raw = b'a\n' * 9000
        self.assertIsNone(_line_delta(raw + b'old\n', {'path': 'guide.txt', 'content': raw.decode(), 'sha256': sha256(raw)}))

    def test_line_delta_rejects_oversized_reconstruction_and_unknown_fields(self):
        from ase.lead_inputs import _line_delta, restore_line_delta, MAX_SOURCE
        base = ('prefix\n' + 'x' * 450000).encode()
        row = {'path': 'guide.txt', 'content': base.decode(), 'sha256': sha256(base)}
        old = base.replace(b'prefix', b'oldfix'); delta = _line_delta(old, row)
        self.assertIsNotNone(delta)
        delta['edits'] = [{'start': 0, 'until': 0, 'content': 'y' * 100000}]
        with self.assertRaises(ValueError): restore_line_delta(delta, row, sha256(old))
        delta = _line_delta(old, row); delta['surprise'] = True
        with self.assertRaises(ValueError): restore_line_delta(delta, row, sha256(old))
        with self.assertRaises(ValueError): restore_line_delta(_line_delta(old, row), {**row, 'content': 'z' * (MAX_SOURCE + 1)}, sha256(old))


if __name__ == '__main__': unittest.main()
