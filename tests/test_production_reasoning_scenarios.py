"""Offline integrity and executable-test sensitivity, never model behavior scores."""
import argparse
from contextlib import closing
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from ase.knowledge import Knowledge
from ase.store import Store
from ase.workflow import WorkflowController, EDIT_SCHEMA
from scripts import evaluate_production_reasoning as evaluation
from scripts import evaluate_review_quality as reviewer_evaluation


class ProductionReasoningScenariosTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='ase-production-scenario-test-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.fixture = evaluation.load_cases(evaluation.DEFAULT_CASES)
        self.cases = {case['id']: case for case in self.fixture['cases']}

    def run_artifact_tests(self, target):
        return subprocess.run([sys.executable, '-B', '-m', 'unittest', '-v', 'test_artifact'],
                              cwd=target, capture_output=True, timeout=30)

    def call_path_fixture(self, case_id):
        fixture = evaluation.load_cases(evaluation.DEFAULT_CASES.with_name('call-path-reasoning-cases.json'))
        case = next(case for case in fixture['cases'] if case['id'] == case_id)
        return evaluation.initialize(self.root / case_id, case)

    def test_dispatcher_fixture_distinguishes_invocation_from_persistence(self):
        target = self.call_path_fixture('due-handler-result')
        before = self.run_artifact_tests(target)
        self.assertIn(b"TypeError: close_due() missing 1 required positional argument: 'day'", before.stderr)

        worker = target / 'worker.py'
        source = worker.read_text(encoding='utf-8')
        worker.write_text(source.replace('close_due(entry, day)', 'close_due(entry)'), encoding='utf-8')
        signature_only = self.run_artifact_tests(target)
        self.assertEqual(signature_only.returncode, 1, signature_only.stderr.decode())
        self.assertIn(b'AssertionError', signature_only.stderr)
        self.assertIn(b"('due', 'draft', None)", signature_only.stderr)

        worker.write_text('from dispatch import run_due\n\n\ndef run(day, ledger):\n'
                          '    def close_due(entry):\n'
                          '        ledger.close(entry["id"], "Draft inactivity")\n\n'
                          '    run_due(ledger.open_entries(), {"expense": close_due}, day)\n', encoding='utf-8')
        fixed = self.run_artifact_tests(target)
        self.assertEqual(fixed.returncode, 0, fixed.stderr.decode())
        self.assertIn(b'Ran 1 test', fixed.stderr)

    def test_voucher_fixture_detects_reuse_that_breaks_historical_inputs(self):
        target = self.call_path_fixture('legacy-voucher-site')
        before = self.run_artifact_tests(target)
        self.assertEqual(before.returncode, 1, before.stderr.decode())
        self.assertRegex(before.stderr, rb'(?m)^test_overnight_export_matches_register_at_the_two_sites .* \.\.\. FAIL\r?$')
        self.assertRegex(before.stderr, rb'(?m)^test_existing_archived_rows_keep_their_recorded_day .* \.\.\. ok\r?$')

        report = target / 'voucher_report.py'
        for guard, expected_error in (('', b"KeyError: 'site'"),
                                      ('entry["archived_day"] if "site" not in entry else ', b"KeyError: 'OLD'")):
            with self.subTest(expected_error=expected_error):
                report.write_text('from register import trading_date\n\n\ndef export(entries):\n'
                                  f'    return [(entry["id"], {guard}trading_date(entry)) for entry in entries]\n',
                                  encoding='utf-8')
                broken = self.run_artifact_tests(target)
                self.assertEqual(broken.returncode, 1, broken.stderr.decode())
                self.assertIn(expected_error, broken.stderr)

        report.write_text('from register import trading_date\n\n\ndef export(entries):\n'
                          '    return [(entry["id"], entry["archived_day"] if "archived_day" in entry\n'
                          '             else trading_date(entry)) for entry in entries]\n', encoding='utf-8')
        fixed = self.run_artifact_tests(target)
        self.assertEqual(fixed.returncode, 0, fixed.stderr.decode())
        self.assertIn(b'Ran 2 tests', fixed.stderr)

    def test_review_pair_delivers_real_check_results_and_withholds_the_rubric(self):
        case = json.loads(reviewer_evaluation.CASES.read_text(encoding='utf-8'))['cases'][0]
        for number, variant in enumerate(case['variants'], 1):
            with self.subTest(number=number):
                request = reviewer_evaluation.prepare_candidate(self.root, number, case, variant)
                packet = json.dumps(request, ensure_ascii=False)
                before = request['handoff']['evaluation_observations']['before_check']
                self.assertEqual(before['exit_code'], 1)
                self.assertIn('unsupported operand type', before['log'])
                self.assertNotIn(variant['expectation']['reason'], packet)
                self.assertNotIn('"expectation"', packet)
                capture = self.root / 'cases' / f'{number:02d}' / 'candidate-checks.json'
                self.assertEqual(json.loads(capture.read_text())['results'][0]['exit_code'], 0)

    def test_every_review_quality_variant_has_a_failing_baseline_and_green_candidate(self):
        cases = json.loads(reviewer_evaluation.CASES.read_text(encoding='utf-8'))['cases']
        jobs = [(case, variant) for case in cases for variant in case['variants']]
        for number, (case, variant) in enumerate(jobs, 1):
            with self.subTest(case=case['id'], disposition=variant['expectation']['disposition']):
                request = reviewer_evaluation.prepare_candidate(self.root, number, case, variant)
                self.assertNotIn('"expectation"', json.dumps(request, ensure_ascii=False))

    def test_review_pair_refuses_a_candidate_with_only_skipped_checks(self):
        case = json.loads(reviewer_evaluation.CASES.read_text(encoding='utf-8'))['cases'][0]
        candidate = case['variants'][0]
        candidate['changes']['test_artifact.py'] = ('import unittest\n\n'
            'class TestPayment(unittest.TestCase):\n'
            '    @unittest.skip("not executed")\n'
            '    def test_payment(self):\n'
            '        pass\n')
        with self.assertRaisesRegex(ValueError, 'green candidate'):
            reviewer_evaluation.prepare_candidate(self.root, 1, case, candidate)

    def test_capture_never_creates_target_database_or_calls_backend(self):
        args = argparse.Namespace(output=self.root / 'capture', cases=evaluation.DEFAULT_CASES,
                                  repetitions=1, case=[])
        with patch.object(evaluation, 'WorkflowController') as controller, patch.object(evaluation, 'Store') as store:
            selected = evaluation.capture(args)
        controller.assert_not_called()
        store.assert_not_called()
        self.assertEqual(len(selected), 4)
        self.assertFalse(list(args.output.rglob('*.sqlite3')))
        self.assertFalse((args.output / 'launch.json').exists())
        before = {path.name: path.read_bytes() for path in args.output.iterdir()}
        with self.assertRaisesRegex(ValueError, 'fresh output'):
            evaluation.capture(args)
        self.assertEqual(before, {path.name: path.read_bytes() for path in args.output.iterdir()})

    def test_sqlite_tests_detect_actual_callee_defect_and_gratuitous_false_fix(self):
        case = self.cases['actual-callee-pair']
        target = evaluation.initialize(self.root / 'sql', case)
        original = self.run_artifact_tests(target)
        self.assertNotEqual(original.returncode, 0)
        self.assertIn(b'module=\'true_store\'', original.stderr)
        path = target / 'true_store.py'
        path.write_text(path.read_text().replace('        yield connection\n',
                        '        yield connection\n        connection.commit()\n'), encoding='utf-8')
        fixed = self.run_artifact_tests(target)
        self.assertEqual(fixed.returncode, 0, fixed.stderr.decode())
        self.assertIn(b'Ran 3 tests', fixed.stderr)
        path = target / 'false_store.py'
        path.write_text(path.read_text() + '\n# An unnecessary reviewer-requested repair\n', encoding='utf-8')
        gratuitous = self.run_artifact_tests(target)
        self.assertNotEqual(gratuitous.returncode, 0)
        self.assertIn(b'test_false_allegation', gratuitous.stderr)

    def test_executed_bugfix_baseline_failure_survives_current_checks_in_review(self):
        case = self.cases['actual-callee-pair']
        folder = self.root / 'baseline-proof'
        target = evaluation.initialize(folder, case)
        contract = evaluation.contract_for(case)
        contract['test_adequacy'] = {'version': 1, 'required': True}
        contract['bugfix_evidence'] = {'version': 1, 'required': True}
        contract['evidence'][0]['progress_test_sources'] = ['test_artifact.py']
        with closing(Store(target, folder / 'private-state')) as store:
            task = evaluation.authorize(store, contract, dependencies=case['files'])
            controller = WorkflowController(store)
            test_source = target / 'test_artifact.py'
            test_source.write_text(test_source.read_text() + '\n# Reviewed test-only change\n', encoding='utf-8')
            baseline = controller.check(task, 'implement')
            self.assertEqual(baseline['results'][0]['exit_code'], 1)
            original = baseline['results'][0]
            source = target / 'true_store.py'
            before_source = source.read_bytes()
            source.write_text(source.read_text().replace('        yield connection\n',
                              '        yield connection\n        connection.commit()\n'), encoding='utf-8')
            after_source = source.read_bytes()
            plan = controller.tasks.execution_plan(task)
            controller.tasks.update_execution_plan(task, {'checks': [*plan['checks'],
                {'id': 'source-file', 'kind': 'artifact', 'path': 'true_store.py'}]},
                expected_revision=plan['revision'])
            current = controller.check(task, 'implement')
            self.assertEqual(current['results'][0]['exit_code'], 0)

            def files():
                packet = controller._packet(task, stage_id='implement', role='review')
                return {row['path']: row for row in packet['files']}

            captured = files()
            history = json.loads(captured['_ase_context/actual_checks.json']['content'])['baseline_failures']
            self.assertEqual(history['results'][0]['log_sha256'], original['log_sha256'])
            self.assertIn('test_artifact.py', history['results'][0]['test_source_changes'])
            old = captured['_ase_context/baseline_logs/unit.txt']
            self.assertIn('FAIL:', old['content'])
            self.assertIn(' ... ok', captured['_ase_context/logs/unit.txt']['content'])

            verified_tests = test_source.read_bytes()
            test_source.write_bytes(verified_tests + b'\n# Another test revision\n')
            self.assertNotIn('_ase_context/baseline_logs/unit.txt', files())
            source.write_bytes(before_source)
            self.assertEqual(controller.check(task, 'implement')['results'][0]['exit_code'], 1)
            test_source.write_bytes(verified_tests)
            source.write_bytes(after_source)
            self.assertIn('_ase_context/baseline_logs/unit.txt', files())

            Path(original['log']).write_text('Substituted text\n', encoding='utf-8')
            self.assertNotIn('_ase_context/baseline_logs/unit.txt', files())
            source.write_bytes(before_source)
            retry = controller.check(task, 'implement')['results'][0]
            self.assertEqual(retry['exit_code'], 1)
            source.write_bytes(after_source)
            controller.check(task, 'implement')
            self.assertEqual(files()['_ase_context/baseline_logs/unit.txt']['sha256'], retry['log_sha256'])
            current_task = controller.tasks.snapshot(task)
            controller.tasks.propose(task, {'summary': 'Different approved intent', 'stage_ids': ['implement']},
                                     expected_revision=current_task['revision'])
            metadata = json.loads(files()['_ase_context/actual_checks.json']['content'])
            self.assertNotIn('baseline_failures', metadata)

    def test_collection_errors_do_not_displace_a_real_baseline_failure(self):
        from ase.testing.adequacy import executed_failure_cases

        self.assertFalse(executed_failure_cases(
            'ERROR: test_service (unittest.loader._FailedTest.test_service)\n', 'unittest'))
        self.assertFalse(executed_failure_cases(
            'ERROR: setUpClass (test_service.ServiceTests)\nRan 0 tests in 0.0s\n', 'unittest'))
        self.assertFalse(executed_failure_cases('ERROR collecting tests/test_service.py\n1 error\n', 'pytest'))
        self.assertEqual(executed_failure_cases('tests/test_service.py::test_http FAILED [100%]\n', 'pytest'),
                         ['FAIL tests/test_service.py::test_http'])

    def test_progress_sources_cannot_hide_edits_to_the_executed_regression_test(self):
        folder = self.root / 'different-declared-test'
        case = self.cases['actual-callee-pair']
        target = evaluation.initialize(folder, case)
        (target / 'test_neighbor.py').write_text('# A different progress test\n', encoding='utf-8')
        tests = target / 'regression.py'
        tests.write_bytes((target / 'test_artifact.py').read_bytes())
        private_input = target / '.devframework/local/checks/runner.txt'
        private_input.parent.mkdir(parents=True)
        private_input.write_text('Approved runner configuration\n', encoding='utf-8')
        with (target / '.git/info/exclude').open('a', encoding='utf-8') as exclude:
            exclude.write('\n/.devframework/\n')
        contract = evaluation.contract_for(case)
        contract['test_adequacy'] = {'version': 1, 'required': True}
        contract['bugfix_evidence'] = {'version': 1, 'required': True}
        contract['evidence'][0]['progress_test_sources'] = ['test_neighbor.py']
        contract['evidence'][0]['argv'][-1] = 'regression'
        contract['check_inputs'] = [{'path': private_input.relative_to(target).as_posix(), 'evidence_ids': ['unit']}]
        with closing(Store(target, folder / 'private-state')) as store:
            task = evaluation.authorize(store, contract, dependencies=case['files'])
            controller = WorkflowController(store)
            self.assertEqual(controller.check(task, 'implement')['results'][0]['exit_code'], 1)
            source = target / 'true_store.py'
            source.write_text(source.read_text().replace('        yield connection\n',
                              '        yield connection\n        connection.commit()\n'), encoding='utf-8')
            original = tests.read_bytes()
            tests.write_bytes(original + b'\n# Regression test changed after its original failure\n')
            self.assertEqual(controller.check(task, 'implement')['results'][0]['exit_code'], 0)
            packet = controller._packet(task, stage_id='implement', role='review')
            self.assertFalse(any(row['path'] == '_ase_context/baseline_logs/unit.txt' for row in packet['files']))
            tests.write_bytes(original)
            packet = controller._packet(task, stage_id='implement', role='review')
            self.assertTrue(any(row['path'] == '_ase_context/baseline_logs/unit.txt' for row in packet['files']))
            private_input.write_text('Changed runner configuration\n', encoding='utf-8')
            packet = controller._packet(task, stage_id='implement', role='review')
            self.assertFalse(any(row['path'] == '_ase_context/baseline_logs/unit.txt' for row in packet['files']))

    def test_a_later_original_failure_can_add_the_actual_consumer_case(self):
        folder = self.root / 'two-failures'
        case = self.cases['actual-callee-pair']
        target = evaluation.initialize(folder, case)
        marker = folder / 'consumer-ready'
        provider = folder / 'provider-ready'
        tests = target / 'test_artifact.py'
        content = tests.read_text(encoding='utf-8')
        tests.write_text(content.replace(
            '    def test_both_actual_callees_commit_success(self):\n',
            f'    def test_other_failure(self):\n        self.assertTrue(Path({str(marker)!r}).exists())\n\n'
            '    def test_both_actual_callees_commit_success(self):\n'
            f'        if not Path({str(marker)!r}).exists():\n            self.skipTest("consumer unavailable")\n'
            f'        if not Path({str(provider)!r}).exists():\n            raise OSError("disposable provider unavailable")\n'),
            encoding='utf-8')
        contract = evaluation.contract_for(case)
        contract['test_adequacy'] = {'version': 1, 'required': True}
        contract['bugfix_evidence'] = {'version': 1, 'required': True}
        with closing(Store(target, folder / 'private-state')) as store:
            task = evaluation.authorize(store, contract, dependencies=case['files'])
            controller = WorkflowController(store)
            first = controller.check(task, 'implement')['results'][0]
            self.assertIn('FAIL: test_other_failure', Path(first['log']).read_text())
            marker.touch()
            error = controller.check(task, 'implement')['results'][0]
            self.assertIn('ERROR: test_both_actual_callees_commit_success', Path(error['log']).read_text())
            provider.touch()
            second = controller.check(task, 'implement')['results'][0]
            self.assertIn('FAIL: test_both_actual_callees_commit_success', Path(second['log']).read_text())
            # Running the same failing consumer again adds no new case.
            controller.check(task, 'implement')
            source = target / 'true_store.py'
            source.write_text(source.read_text().replace('        yield connection\n',
                              '        yield connection\n        connection.commit()\n'), encoding='utf-8')
            self.assertEqual(controller.check(task, 'implement')['results'][0]['exit_code'], 0)
            packet = controller._packet(task, stage_id='implement', role='review')
            files = {row['path']: row['content'] for row in packet['files']}
            failures = json.loads(files['_ase_context/actual_checks.json'])['baseline_failures']['results']
            self.assertEqual({row['log_sha256'] for row in failures},
                             {row['log_sha256'] for row in (first, error, second)})
            self.assertEqual(len({row['log_packet_path'] for row in failures}), 3)
            self.assertTrue(any('FAIL: test_both_actual_callees_commit_success' in files[row['log_packet_path']]
                                for row in failures))

    def test_rewrite_tests_require_real_refactor_and_detect_semantic_drift(self):
        target = evaluation.initialize(self.root / 'rewrite', self.cases['equivalent-and-control'])
        self.assertNotEqual(self.run_artifact_tests(target).returncode, 0)
        (target / 'equivalent.py').write_text((target / 'equivalent-candidate.txt').read_text(), encoding='utf-8')
        (target / 'decision-note.json').write_text(json.dumps({
            'equivalent': {'action': 'APPLIED', 'owner_message': '', 'reason': 'Equivalent under existing authorization'},
            'control': {'action': 'HELD', 'owner_message': 'Authorize a changed None result?', 'reason': 'None result changes'},
        }), encoding='utf-8')
        equivalent = self.run_artifact_tests(target)
        self.assertEqual(equivalent.returncode, 0, equivalent.stderr.decode())
        self.assertIn(b'Ran 5 tests', equivalent.stderr)
        (target / 'controlled.py').write_text((target / 'controlled-candidate.txt').read_text(), encoding='utf-8')
        self.assertNotEqual(self.run_artifact_tests(target).returncode, 0)

    def test_real_source_and_owner_drift_invalidates_cached_claims_and_checks(self):
        case = self.cases['draft-resume-drift']
        folder = self.root / 'drift'
        target = evaluation.initialize(folder, case)
        with closing(Store(target, folder / 'private-state')) as store:
            task_id = evaluation.authorize(store, evaluation.contract_for(case), dependencies=case['files'])
            controller = WorkflowController(store)
            current = evaluation.cache_sources(store, task_id, case)
            self.assertEqual({claim['validity'] for claim in current['claims']}, {'CURRENT'})
            guide = 'Internal draft\n' + ''.join((target / name).read_text() for name in ('brief.md', 'owner-decision.md'))
            (target / 'guide.md').write_text(guide, encoding='utf-8')
            before = controller.check(task_id, 'draft')
            self.assertEqual(self.run_artifact_tests(target).returncode, 0)
            for relative, content in case['drift']['files'].items():
                (target / relative).write_text(content, encoding='utf-8')
            stale = Knowledge(store).retrieve(scope={}, refresh=True)
            self.assertEqual({claim['validity'] for claim in stale['claims']}, {'STALE'})
            self.assertNotEqual(self.run_artifact_tests(target).returncode, 0)
            after = controller.check(task_id, 'draft')
            self.assertNotEqual(before['fingerprint'], after['fingerprint'])

    def test_normal_request_does_not_receive_withheld_rubric_or_manual_guides(self):
        case = self.cases['draft-resume-drift']
        folder = self.root / 'packet'
        target = evaluation.initialize(folder, case)
        with closing(Store(target, folder / 'private-state')) as store:
            task_id = evaluation.authorize(store, evaluation.contract_for(case))
            request = WorkflowController(store)._request(task_id, 'implementation', EDIT_SCHEMA, {'stage_id': 'draft'})
        encoded = json.dumps(request)
        self.assertNotIn('rubric', encoded.lower())
        self.assertNotIn(self.fixture['rubric']['draft-resume-drift']['criteria'][0], encoded)
        contract = json.loads(next(row['content'] for row in request['source_packet']['files']
                                   if row['path'] == '_ase_context/contract.json'))
        self.assertEqual([item['id'] for item in contract['acceptance']], ['DRAFT', 'PUSH'])
        self.assertEqual(contract['stages'][1]['depends_on'], ['draft'])

    def test_owner_answer_enters_normal_packet_with_exact_revised_authority(self):
        case = self.cases['draft-resume-drift']
        folder = self.root / 'owner-answer'
        target = evaluation.initialize(folder, case)
        with closing(Store(target, folder / 'private-state')) as store:
            task_id = evaluation.authorize(store, evaluation.contract_for(case), dependencies=case['files'])
            controller = WorkflowController(store)
            original = controller.tasks.snapshot(task_id)
            controller.reserve_cycle(task_id, 'draft')
            for relative, content in case['drift']['files'].items():
                (target / relative).write_text(content, encoding='utf-8')
            evaluation.authorize(store, evaluation.contract_for(case, drift=True), revise=True,
                                 dependencies=case['files'], user_message_ref=case['drift']['owner_message_ref'])
            current = controller.tasks.snapshot(task_id)
            request = controller._request(task_id, 'implementation', EDIT_SCHEMA, {'stage_id': 'draft'})
            supplied = json.loads(next(row['content'] for row in request['source_packet']['files']
                                      if row['path'] == '_ase_context/contract.json'))
            self.assertIn(case['drift']['owner_instruction'], supplied['request'])
            self.assertIn(case['drift']['owner_message_ref'], supplied['request'])
            self.assertNotEqual(original['contract_hash'], current['contract_hash'])
            self.assertNotEqual(original['proposal_hash'], current['proposal_hash'])
            self.assertEqual(original['contract']['scope'], current['contract']['scope'])
            self.assertEqual(request['bindings']['contract_sha256'], current['contract_hash'])
            self.assertEqual(request['bindings']['proposal_sha256'], current['proposal_hash'])
            grant = evaluation.read_record(store.db, 'approval', task_id)['payload']
            self.assertEqual(grant['user_message_ref'], case['drift']['owner_message_ref'])
            # Exact reauthorization does not reset the earlier cycle or its cap.
            self.assertEqual(controller.reserve_cycle(task_id, 'draft')['stage_cycles'], 2)
            with self.assertRaisesRegex(ValueError, 'budget'):
                controller.reserve_cycle(task_id, 'draft')


if __name__ == '__main__':
    unittest.main()
