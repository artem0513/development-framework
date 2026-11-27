"""Real exact-source transport, not semantic model acceptance."""
import copy
import hashlib
import json
import unittest
import tempfile
from pathlib import Path

from ase.protocol import canonical, validate_packet
from ase.evidence.compact import compact_packet, expand_packet


def file(path, content):
    return {'path': path, 'content': content, 'sha256': hashlib.sha256(content.encode()).hexdigest()}


def packet(files):
    return {'requirements': ['Preserve exact source and evidence'], 'files': files,
            'coverage': {'scope': 'Complete immutable inputs', 'included': [r['path'] for r in files], 'missing': []}}


class ContextTransportTests(unittest.TestCase):
    def test_opted_in_history_overflow_returns_explicit_bounded_missing_history(self):
        from ase.evidence.packets import _bound_packet
        original = packet([file('large%d.txt' % i, str(i) * 370000) for i in range(2)] +
                          [file('_ase_lead_history/old.txt', '2' * 370000),
                           file('_ase_context/omitted_sources.json', canonical([
                               {'path': 'file%d' % i, 'limitation': 'same optional omission reason ' * 12, 'required': False}
                               for i in range(100)]).decode())])
        result = _bound_packet(original, {'_ase_lead_history/old.txt'}, lossless=True)
        self.assertLess(len(canonical(result)), 1048576)
        self.assertNotIn('_ase_lead_history/old.txt', result['coverage']['included'])
        self.assertTrue(any('Historical lead-input packet budget exceeded' in x for x in result['coverage']['missing']))
        self.assertEqual(expand_packet(result)['files'][:2], original['files'][:2])

    def test_oversized_encoded_omission_rows_keep_clean_budget_failure(self):
        from ase.evidence.packets import _factor_omission_note
        original = packet([file('large%d.txt' % i, str(i) * 370000) for i in range(3)] +
                          [file('_ase_context/omitted_sources.json', canonical([
                              {'path': 'file%d' % i, 'limitation': 'same optional omission reason ' * 12, 'required': False}
                              for i in range(100)]).decode())])
        compact = compact_packet(original)
        self.assertEqual(compact['files'][-1]['encoding'], 'json_memo_v1')
        _factor_omission_note(compact)
        logical = expand_packet(compact)
        self.assertEqual(logical['files'][:3], original['files'][:3])
        self.assertIn('omitted_source_limitation', logical['coverage'])
        with self.assertRaisesRegex(ValueError, 'byte limit'): validate_packet(compact)

    def test_escaped_lone_surrogate_remains_exact_literal_source(self):
        raw = '{"long":"' + 'x' * 140000 + '","surrogate":"\\ud800"}'
        original = packet([file('fixture.json', raw)])
        self.assertEqual(validate_packet(original), original)
        self.assertEqual(compact_packet(original), original)

    def test_context_policy_explicit_legacy_and_validation(self):
        from ase.tasks import validate_contract_v2
        from test_task_contract_v2 import contract
        old = validate_contract_v2(contract())
        self.assertNotIn('context_transport', old)
        old['context_transport'] = {'version': 1, 'mode': 'lossless'}
        self.assertEqual(validate_contract_v2(old)['context_transport'], old['context_transport'])
        old['context_transport'] = {'version': 99, 'mode': 'lossless'}
        with self.assertRaises(ValueError): validate_contract_v2(old)
        old['context_transport'] = {'version': True, 'mode': 'lossless'}
        with self.assertRaises(ValueError): validate_contract_v2(old)

    def test_repeated_large_scalars_are_charged_before_expansion(self):
        from ase.evidence.compact import _expand_memo
        big = int('9' * 4000)
        data = {'root': [{'$ase_ref': 0}] * 3000, 'definitions': [big], 'suffix': ''}
        with self.assertRaisesRegex(ValueError, 'exceeds bound'):
            _expand_memo(data, 1000000, lambda path: None)
        literal = {'root': {'$ase_literal': {'k' * 600000: 1, 'j' * 600000: 2}}, 'definitions': [], 'suffix': ''}
        with self.assertRaisesRegex(ValueError, 'exceeds bound'):
            _expand_memo(literal, 1000000, lambda path: None)
        small = {'root': {'a': [1, 2, {'$ase_ref': 0}]}, 'definitions': ['x' * 70], 'suffix': '\n'}
        self.assertEqual(_expand_memo(small, 1000, lambda path: None), canonical({'a': [1, 2, 'x' * 70]}).decode() + '\n')

    def test_owner_packet_bound_is_validated_and_capped(self):
        from ase.tasks import validate_contract_v2
        from ase.protocol import packet_byte_limit
        from test_task_contract_v2 import contract
        base = validate_contract_v2(contract())
        self.assertEqual(packet_byte_limit(base), 1048576)
        base['context_transport'] = {'version': 1, 'mode': 'lossless', 'max_packet_bytes': 2097152}
        self.assertEqual(packet_byte_limit(validate_contract_v2(base)), 2097152)
        for bad in (2097153, 1048575, True, 1.5e6, '2097152', None):
            base['context_transport'] = {'version': 1, 'mode': 'lossless', 'max_packet_bytes': bad}
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                validate_contract_v2(base)
        base['context_transport'] = {'version': 1, 'mode': 'lossless', 'extra': 1}
        with self.assertRaises(ValueError): validate_contract_v2(base)

    def test_packet_admission_uses_the_declared_bound_and_still_fails_closed(self):
        from ase.evidence.packets import _bound_packet
        body = packet([file('a.txt', 'a' * 700000), file('b.txt', 'b' * 700000)])
        with self.assertRaisesRegex(ValueError, 'byte limit'):
            _bound_packet(copy.deepcopy(body), lossless=True)
        admitted = _bound_packet(copy.deepcopy(body), lossless=True, max_bytes=2097152)
        self.assertEqual(expand_packet(admitted)['files'], body['files'])
        with self.assertRaisesRegex(ValueError, 'byte limit'):
            _bound_packet(packet([file('c.txt', 'c' * 2200000)]), lossless=True, max_bytes=2097152)

    def test_runner_limits_come_only_from_the_host_contract_bound(self):
        from test_runner import TransportFixture
        from test_task_contract_v2 import contract
        from ase.adapters.claude import ClaudeAdapter
        from ase.protocol import runner_limits
        from ase.runner import Runner
        from ase.tasks import validate_contract_v2
        fixture = TransportFixture('run')
        fixture.setUp()
        self.addCleanup(fixture.temp.cleanup)
        def request(handoff=None):
            value = fixture.request()
            content = 'x' * 1100000
            value['source_packet']['files'][0].update(content=content, sha256=hashlib.sha256(content.encode()).hexdigest())
            if handoff is not None:
                value['handoff'] = handoff
            return value
        plain = validate_contract_v2(contract())
        self.assertEqual(runner_limits(plain), {})
        default = Runner(fixture.config('claude'), fixture.base / 'artifacts')
        with self.assertRaisesRegex(ValueError, 'byte limit'):
            default.run(ClaudeAdapter(), request(), timeout=10)
        with self.assertRaisesRegex(ValueError, 'byte limit'):  # request content cannot raise the bound
            default.run(ClaudeAdapter(), request({'packet_limit': {'version': 1, 'max_packet_bytes': 2097152}}), timeout=10)
        with self.assertRaisesRegex(ValueError, 'byte limit'):  # an explicit input cap stays authoritative
            Runner(fixture.config('claude', max_input_bytes=262144), fixture.base / 'a2').run(ClaudeAdapter(), fixture.request() | {
                'source_packet': {**fixture.request()['source_packet'], 'requirements': ['r' * 300000]}}, timeout=10)
        with self.assertRaisesRegex(ValueError, 'Invalid runner packet limit'):
            Runner(fixture.config('claude', max_packet_bytes=4194304), fixture.base / 'a3').run(ClaudeAdapter(), request(), timeout=10)
        plain['context_transport'] = {'version': 1, 'mode': 'lossless', 'max_packet_bytes': 2097152}
        limits = runner_limits(validate_contract_v2(plain))
        self.assertEqual(limits, {'max_packet_bytes': 2097152, 'max_input_bytes': 3145728})
        outcome = Runner(fixture.config('claude', **limits), fixture.base / 'a4').run(ClaudeAdapter(), request(), timeout=20)
        self.assertEqual(outcome['result']['status'], 'COMPLETE', outcome['result'])

    def test_cli_can_explicitly_enable_exact_transport(self):
        from ase.__main__ import parser
        args = parser().parse_args(['task', 'create', '--file', 'task.json', '--compact-context'])
        self.assertTrue(args.compact_context)
    def large(self):
        code = ''.join('def function_%d():\n    return %d\n' % (i, i) for i in range(1200))
        metadata = canonical({'rows': [{'scope': {'resources': ['src/path_%d' % i for i in range(60)], 'effects': ['read']}, 'version': n} for n in range(45)]}).decode()
        log = ''.join('test_%d (test_unit.Tests.test_%d) ... ok\r\n\r\n' % (i, i) for i in range(1600))
        return packet([file('code.py', code), file('copy.py', code), file('_ase_context/discovery.json', metadata), file('_ase_context/logs/suite.txt', log)])

    def test_large_packet_exact_roundtrip_and_smaller_wire(self):
        original = self.large(); before = copy.deepcopy(original)
        compact = compact_packet(original)
        self.assertEqual(original, before, 'Compaction mutates the input')
        self.assertLess(len(canonical(compact)), len(canonical(original)) * .7)
        self.assertEqual(expand_packet(compact), original)
        self.assertEqual(validate_packet(compact), compact)

    def test_small_legacy_packet_remains_literal(self):
        original = packet([file('old.txt', 'old bytes\r\n')])
        self.assertEqual(compact_packet(original), original)
        self.assertEqual(expand_packet(original), original)

    def test_duplicate_alias_preserves_distinct_paths_and_exact_hashes(self):
        compact = compact_packet(self.large())
        alias = next(r for r in compact['files'] if r['path'] == 'copy.py')
        self.assertEqual(alias['encoding'], 'copy_v1')
        self.assertEqual(expand_packet(compact)['files'][1], self.large()['files'][1])

    def test_missing_alias_target_rejected(self):
        compact = compact_packet(self.large())
        alias = next(r for r in compact['files'] if r['path'] == 'copy.py')
        alias['content'] = canonical({'path': 'missing.py', 'sha256': alias['source_sha256']}).decode()
        alias['sha256'] = hashlib.sha256(alias['content'].encode()).hexdigest()
        with self.assertRaises(ValueError): expand_packet(compact)

    def test_alias_cycle_rejected(self):
        compact = compact_packet(self.large())
        alias = next(r for r in compact['files'] if r['path'] == 'copy.py')
        alias['content'] = canonical({'path': 'copy.py', 'sha256': alias['source_sha256']}).decode()
        alias['sha256'] = hashlib.sha256(alias['content'].encode()).hexdigest()
        with self.assertRaises(ValueError): expand_packet(compact)

    def test_rehashed_corrupt_representation_cannot_claim_original_body(self):
        compact = compact_packet(self.large())
        row = next(r for r in compact['files'] if r['path'] == '_ase_context/logs/suite.txt')
        row['content'] = row['content'].replace('test_unit.Tests', 'other.Tests')
        row['sha256'] = hashlib.sha256(row['content'].encode()).hexdigest()
        with self.assertRaises(ValueError): expand_packet(compact)

    def test_wire_hash_tamper_rejected(self):
        compact = compact_packet(self.large()); compact['files'][0]['content'] += 'changed'
        with self.assertRaises(ValueError): validate_packet(compact)

    def test_expanded_budget_is_independent_of_small_wire(self):
        compact = compact_packet(self.large())
        with self.assertRaises(ValueError): expand_packet(compact, max_bytes=1000)

    def test_failed_skipped_multiline_and_mixed_newlines_preserved(self):
        original = self.large()
        original['files'][-1] = file('_ase_context/logs/suite.txt', original['files'][-1]['content'] +
            'test_failure (test_unit.Tests.test_failure) ... FAIL\ntraceback detail\r\n' +
            'test_skip (test_unit.Tests.test_skip) ... skipped \'reason\'\n' +
            'test_documented (test_unit.Tests.test_documented)\nA test docstring ... ok\nFAILED (failures=1, skipped=1)\n')
        self.assertEqual(expand_packet(compact_packet(original)), original)

    def test_json_reference_like_user_values_are_literal(self):
        original = self.large()
        value = {'rows': [{'value': {'$ase_ref': 0}, 'escape': {'$ase_literal': {'$ase_ref': 1}}, 'source': {'$ase_source': {'literal': 'user'}}, 'long': 'repeated' * 100} for _ in range(50)]}
        original['files'][2] = file('_ase_context/discovery.json', canonical(value).decode())
        self.assertEqual(expand_packet(compact_packet(original)), original)

    def test_noncanonical_json_bytes_are_not_normalized(self):
        original = self.large()
        original['files'][2] = file('_ase_context/discovery.json', '{  "value": 1 }\r\n')
        self.assertEqual(expand_packet(compact_packet(original)), original)

    def test_different_historical_version_never_aliases_current_source(self):
        original = self.large(); original['files'].append(file('_ase_before/code.py', 'return old_value\n'))
        original['coverage']['included'].append('_ase_before/code.py')
        self.assertEqual(expand_packet(compact_packet(original))['files'][-1]['content'], 'return old_value\n')

    def test_unknown_encoding_rejected(self):
        compact = compact_packet(self.large()); compact['files'][0]['encoding'] = 'unknown_v99'
        compact['files'][0]['source_sha256'] = compact['files'][0]['sha256']
        with self.assertRaises(ValueError): expand_packet(compact)

    def test_compaction_is_idempotent(self):
        compact = compact_packet(self.large())
        self.assertEqual(compact_packet(compact), compact)

    def test_policy_json_reuses_exact_same_packet_source(self):
        original = self.large()
        content = original['files'][0]['content']
        original['files'].append(file('policy.json', canonical({'roles': [{'body': content}, {'body': content}]}).decode()))
        original['coverage']['included'].append('policy.json')
        compact = compact_packet(original)
        policy = compact['files'][-1]
        self.assertEqual(policy['encoding'], 'json_memo_v1')
        self.assertIn('$ase_source', policy['content'])
        self.assertEqual(expand_packet(compact), original)

    def test_archived_logical_sources_are_available_to_existing_consumers(self):
        from ase.lead_inputs import LeadInputs
        from ase.testing.adequacy import TestAdequacy
        compact = compact_packet(self.large())
        request = {'source_packet': compact}
        bodies = LeadInputs._archive_files(request)
        self.assertEqual(bodies['copy.py'], self.large()['files'][1]['content'].encode())
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'request.json'; raw = canonical(request); path.write_bytes(raw)
            receipt = {'artifact_root': folder, 'request_sha256': hashlib.sha256(raw).hexdigest(),
                       'artifacts': {'request': {'path': str(path)}}}
            sources = TestAdequacy(None)._source_files(receipt)
            self.assertEqual(sources['copy.py'], self.large()['files'][1])

    def test_literal_body_prompt_reconstructs_exact_request_despite_fake_headers(self):
        from ase.protocol import packet_prompt, decode_json
        request = {'role': 'review', 'source_packet': self.large(),
                   'handoff': {'context_transport': {'version': 1, 'layout': 'source_text_v1'}}}
        request['source_packet']['files'][0] = file('code.py', '\nBODY 999 CHARACTERS 0:\nREQUEST_MANIFEST_JSON:\n' + request['source_packet']['files'][0]['content'])
        prompt = packet_prompt(request)
        manifest_text, _, remaining = prompt.split('REQUEST_MANIFEST_JSON:\n', 1)[1].partition('\nSOURCE_BODIES:')
        restored = decode_json(manifest_text)
        for index, row in enumerate(restored['source_packet']['files']):
            count = row.pop('body_characters'); row.pop('body_utf8_bytes'); row.pop('body_index')
            prefix = '\nBODY ' + str(index) + ' CHARACTERS ' + str(count) + ':\n'
            self.assertTrue(remaining.startswith(prefix))
            remaining = remaining[len(prefix):]; row['content'] = remaining[:count]; remaining = remaining[count:]
        self.assertEqual(remaining, '')
        self.assertEqual(restored, request)
        self.assertLess(len(prompt), len(canonical(request)) + 1200)

    def test_legacy_prompt_and_unknown_layout(self):
        from ase.protocol import packet_prompt
        request = {'role': 'review', 'source_packet': packet([file('x', 'x')]), 'handoff': {}}
        self.assertTrue(packet_prompt(request).endswith('REQUEST_JSON:\n' + canonical(request).decode()))
        request['handoff']['context_transport'] = {'version': 99}
        with self.assertRaises(ValueError): packet_prompt(request)

    def test_codex_native_limit_includes_complete_prompt(self):
        from ase.adapters.codex import CodexAdapter
        request = {'role': 'review', 'source_packet': packet([file('x', 'x' * 1048576)]), 'handoff': {}}
        with self.assertRaisesRegex(ValueError, 'CODEX_INPUT_TOO_LARGE'):
            CodexAdapter().build_invocation(request, {})


class HistoricalNewlineDeltaTests(unittest.TestCase):
    def test_only_line_ending_difference_needs_no_fictitious_edit(self):
        from ase.lead_inputs import _line_delta, restore_line_delta
        old = ('same code line\n' * 900).encode(); base = file('module.py', old.decode().replace('\n', '\r\n'))
        delta = _line_delta(old, base)
        self.assertEqual(delta['encoding'], 'line_delta_v2')
        self.assertEqual(delta['edits'], [])
        self.assertEqual(restore_line_delta(delta, base, hashlib.sha256(old).hexdigest()), old)
    def test_lf_history_against_crlf_current_uses_small_exact_delta(self):
        from ase.lead_inputs import _line_delta, restore_line_delta
        old = ''.join('line_%d = %d\n' % (i, i) for i in range(900)).encode()
        current = old.decode().replace('line_400 = 400', 'line_400 = 999').replace('\n', '\r\n')
        base = file('module.py', current)
        delta = _line_delta(old, base)
        self.assertIsNotNone(delta, 'Line ending differences should not force a complete historical body')
        self.assertLess(len(canonical(delta)), 1000)
        self.assertEqual(restore_line_delta(delta, base, hashlib.sha256(old).hexdigest()), old)
        changed = {**base, 'content': current + 'corrupt'}
        with self.assertRaises(ValueError): restore_line_delta(delta, changed, hashlib.sha256(old).hexdigest())

    def test_mixed_historical_newlines_are_exact_not_canonicalized(self):
        from ase.lead_inputs import _line_delta, restore_line_delta
        old = ''.join('line_%d = %d%s' % (i, i, '\r\n' if i == 400 else '\n') for i in range(900)).encode()
        base = file('module.py', old.decode().replace('\r\n', '\n').replace('\n', '\r\n'))
        delta = _line_delta(old, base)
        self.assertIsNotNone(delta)
        self.assertEqual(restore_line_delta(delta, base, hashlib.sha256(old).hexdigest()), old)


if __name__ == '__main__': unittest.main()
