"""Case evidence through real selective commands and the adequacy policy seam.

Runner protocol fixtures below supply judgment only; they never establish an
independent semantic PASS. Unittest commands really execute the cited source.
"""
import copy
import hashlib
from pathlib import Path
import sys
import unittest

from ase.core import execute
from ase.state import hash_value, read_record, transaction, write_record
import test_test_adequacy as existing


SOURCE = '''import concurrent.futures, queue, threading, unittest
class ClaimTests(unittest.TestCase):
 def test_claim_once(self):
  q=queue.Queue(); q.put('one'); start=threading.Barrier(2)
  def claim():
   start.wait(timeout=5)
   try:return q.get_nowait()
   except queue.Empty:return 'empty'
  with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
   futures=[pool.submit(claim) for _ in range(2)]
   self.assertEqual(sorted(f.result(timeout=5) for f in futures),['empty','one'])
 def test_other(self):
  self.assertEqual(2 + 2, 4)
'''


class ExecutionEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.case = existing.AdequacyGateTests()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)

    def run_case(self, source=SOURCE, *, selected='ClaimTests.test_other', verbose=True,
                 parser='unittest', argv=None, test_source='tests/test_claim.py', check_inputs=None):
        case = self.case
        case.test_source = source
        target = case.repo / test_source
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(source, encoding='utf-8')
        case.spec['parser'] = parser
        case.spec['argv'] = argv or [sys.executable, '-B', '-m', 'unittest',
            'discover', '-s', 'tests', *(['-v'] if verbose else []), '-k', selected]
        task = case.tasks.snapshot('adequacy')
        contract = copy.deepcopy(task['contract'])
        contract['evidence'] = [case.spec]
        if check_inputs is not None:
            contract['check_inputs'] = check_inputs
        task = case.tasks.revise('adequacy', contract, expected_revision=task['revision'])
        task = case.tasks.propose('adequacy', {'summary': 'Check actual selected case',
            'stage_ids': ['code']}, expected_revision=task['revision'])
        if not task['approved_stage_ids']:
            case.tasks.approve('adequacy', proposal_hash=task['proposal_hash'],
                user_message_ref=f"test:disposable-scope:{task['revision']}", approved_stage_ids=['code'])
        plan = case.tasks.execution_plan('adequacy')
        case.saved = case.service.plan('adequacy', case.plan_payload,
            expected_revision=case.saved['revision'])
        result = execute(case.spec['argv'], case.repo,
            case.store.root / 'tasks/adequacy/checks/selected/suite.log', timeout=10)
        result.update(id='suite', kind='test', parser=parser, configured_argv=case.spec['argv'])
        bundle = {'kind': 'checks', 'task_id': 'adequacy', 'fingerprint': hash_value(source),
            'contract_hash': task['contract_hash'], 'plan_hash': plan['plan_hash'], 'results': [result]}
        with transaction(case.store.db):
            old = read_record(case.store.db, 'checks', 'adequacy')
            write_record(case.store.db, 'checks', 'adequacy', bundle, expected_revision=old['revision'])
        case.current.update(contract_hash=task['contract_hash'], proposal_hash=task['proposal_hash'],
            plan_hash=plan['plan_hash'], fingerprint=bundle['fingerprint'],
            check_hash=hash_value(bundle), checks=plan['checks'])
        return result

    def verdict(self, *, test_id=None, test_source=None, files=None):
        case = self.case
        payload = copy.deepcopy(case.payload)
        if test_id is not None:
            payload['scenarios'][0]['evidence'][0]['test_id'] = test_id
        if test_source is not None:
            payload['scenarios'][0]['evidence'][0]['test_source'] = test_source
        case.receipt(payload, files=files)
        return case.gate()

    def test_selective_success_of_other_case_cannot_credit_cited_source_case(self):
        result = self.run_case()
        self.assertEqual(result['exit_code'], 0)
        output = Path(result['log']).read_text(encoding='utf-8')
        self.assertIn('test_other', output)
        self.assertNotIn('test_claim_once', output)
        self.assertEqual(self.verdict()['status'], 'BLOCKED')

    def test_actual_selected_success_is_accepted(self):
        result = self.run_case(selected='ClaimTests.test_claim_once')
        self.assertEqual(result['exit_code'], 0)
        self.assertEqual(self.verdict()['status'], 'PASS')

    def test_unsupported_runner_uses_source_locator_and_successful_check(self):
        # .NET/JS/Go output has no per-case adapter; outside bugfix stages the cited case must exist in
        # the captured source and the check must succeed, as before case-level proof was added.
        source = 'public class ClaimTests {\n  [Fact] public void ClaimOnce() { Assert.True(true); }\n}\n'
        summary = "print('Passed!  - Failed:     0, Passed:     1, Skipped:     0, Total:     1')"
        self.run_case(source, parser='dotnet', argv=[sys.executable, '-c', summary], test_source='tests/ClaimTests.cs')
        files = [{'path': 'tests/ClaimTests.cs', 'content': source, 'sha256': hashlib.sha256(source.encode()).hexdigest()}]
        self.assertEqual(self.verdict(test_id='ClaimTests.ClaimOnce', test_source='tests/ClaimTests.cs', files=files)['status'], 'PASS')
        self.assertEqual(self.verdict(test_id='ClaimTests.Missing', test_source='tests/ClaimTests.cs', files=files)['status'], 'BLOCKED')

    def test_declared_private_check_script_is_a_test_source_only_for_the_checks_that_run_it(self):
        script = '.devframework/local/checks/nullable.ps1'
        body = "Write-Output 'PASS rule_line_length: 0 lines over 140'\n"
        (self.case.repo / script).parent.mkdir(parents=True, exist_ok=True)
        (self.case.repo / script).write_text(body, encoding='utf-8')
        summary = "print('Passed!  - Failed:     0, Passed:     1, Skipped:     0, Total:     1')"
        packet = '_ase_check_inputs/' + script
        files = [{'path': packet, 'content': body, 'sha256': hashlib.sha256(body.encode()).hexdigest()}]
        self.run_case(body, parser='dotnet', argv=[sys.executable, '-c', summary], test_source='tests/unused.txt',
                      check_inputs=[{'path': script, 'evidence_ids': ['suite']}])
        self.assertEqual(self.verdict(test_id='rule_line_length', test_source=packet, files=files)['status'], 'PASS')
        # Any other _ase_ path, or a script cited for a check it is not declared for, stays unproved.
        other = [{'path': '_ase_context/notes.ps1', 'content': body, 'sha256': files[0]['sha256']}]
        self.assertEqual(self.verdict(test_id='rule_line_length', test_source='_ase_context/notes.ps1',
                                      files=other)['status'], 'BLOCKED')

    def test_declared_python_check_script_with_unittest_parser_uses_the_locator(self):
        script = '.devframework/local/checks/rules.py'
        body = "def rule_line_length():\n    print('PASS rule_line_length')\nrule_line_length()\n"
        (self.case.repo / script).parent.mkdir(parents=True, exist_ok=True)
        (self.case.repo / script).write_text(body, encoding='utf-8')
        packet = '_ase_check_inputs/' + script
        files = [{'path': packet, 'content': body, 'sha256': hashlib.sha256(body.encode()).hexdigest()}]
        # The script reports like a unittest run; per-case log proof cannot exist for a check script.
        summary = r"import sys; sys.stderr.write('rule_line_length ... ok\n\nRan 1 test in 0.001s\n\nOK\n')"
        self.run_case(body, parser='unittest', argv=[sys.executable, '-c', summary],
                      test_source='tests/unused.txt', check_inputs=[{'path': script, 'evidence_ids': ['suite']}])
        self.assertEqual(self.verdict(test_id='rule_line_length', test_source=packet, files=files)['status'], 'PASS')

    def test_undeclared_private_check_script_is_not_a_test_source(self):
        body = "Write-Output 'PASS rule_line_length'\n"
        summary = "print('Passed!  - Failed:     0, Passed:     1, Skipped:     0, Total:     1')"
        packet = '_ase_check_inputs/.devframework/local/checks/nullable.ps1'
        files = [{'path': packet, 'content': body, 'sha256': hashlib.sha256(body.encode()).hexdigest()}]
        self.run_case(body, parser='dotnet', argv=[sys.executable, '-c', summary], test_source='tests/unused.txt')
        self.assertEqual(self.verdict(test_id='rule_line_length', test_source=packet, files=files)['status'], 'BLOCKED')

    def test_failed_cited_case_is_rejected_instead_of_missing_proof(self):
        self.run_case(SOURCE.replace("['empty','one']", "['one','one']"), selected='ClaimTests.test_claim_once')
        verdict = self.verdict()
        self.assertEqual(verdict['status'], 'FAIL', verdict)
        self.assertTrue(verdict['rejected_reasons'])

    def test_skipped_cited_case_is_unproved(self):
        self.run_case(SOURCE.replace(' def test_claim_once', " @unittest.skip('not executed')\n def test_claim_once"),
            selected='ClaimTests.test_claim_once')
        self.assertEqual(self.verdict()['status'], 'BLOCKED')

    def test_expected_failure_cited_case_is_unproved(self):
        source = SOURCE.replace(' def test_claim_once', ' @unittest.expectedFailure\n def test_claim_once')
        self.run_case(source.replace("['empty','one']", "['one','one']"), selected='ClaimTests.test_claim_once')
        self.assertEqual(self.verdict()['status'], 'BLOCKED')

    def test_aggregate_only_success_does_not_prove_a_case(self):
        self.run_case(selected='ClaimTests.test_claim_once', verbose=False)
        self.assertEqual(self.verdict()['status'], 'BLOCKED')

    def test_qualified_case_does_not_match_another_class_with_same_leaf(self):
        source = SOURCE + '''class OtherTests(unittest.TestCase):
 def test_claim_once(self):
  self.assertTrue(True)
'''
        self.run_case(source, selected='OtherTests.test_claim_once')
        self.assertEqual(self.verdict()['status'], 'BLOCKED')
        self.assertEqual(self.verdict(test_id='OtherTests.test_claim_once')['status'], 'PASS')

    def test_unqualified_leaf_is_accepted_only_when_unique_in_source(self):
        self.run_case(selected='ClaimTests.test_claim_once')
        self.assertEqual(self.verdict(test_id='test_claim_once')['status'], 'PASS')
        source = SOURCE + '''class OtherTests(unittest.TestCase):
 def test_claim_once(self):
  self.assertTrue(True)
'''
        self.run_case(source, selected='ClaimTests.test_claim_once')
        self.assertEqual(self.verdict(test_id='test_claim_once')['status'], 'BLOCKED')

    def test_qualified_module_cannot_be_replaced_by_same_leaf(self):
        self.run_case(selected='ClaimTests.test_claim_once')
        self.assertEqual(self.verdict(test_id='test_claim.ClaimTests.test_claim_once')['status'], 'PASS')
        self.assertEqual(self.verdict(test_id='wrong_module.ClaimTests.test_claim_once')['status'], 'BLOCKED')

    def test_same_basename_source_cannot_match_another_discovery_or_module_root(self):
        self.run_case(selected='ClaimTests.test_claim_once')
        other = self.case.repo / 'other/test_claim.py'
        other.parent.mkdir()
        other.write_text(SOURCE, encoding='utf-8')
        files = [{'path': name, 'content': SOURCE,
            'sha256': hashlib.sha256(SOURCE.encode()).hexdigest()}
            for name in ('tests/test_claim.py', 'other/test_claim.py')]
        self.assertEqual(self.verdict(test_source='other/test_claim.py', files=files)['status'], 'BLOCKED')
        (self.case.repo / 'tests/__init__.py').write_text('', encoding='utf-8')
        self.run_case(argv=[sys.executable, '-B', '-m', 'unittest',
            'tests.test_claim.ClaimTests.test_claim_once', '-v'])
        self.assertEqual(self.verdict(test_source='other/test_claim.py', files=files)['status'], 'BLOCKED')
        self.assertEqual(self.verdict()['status'], 'PASS')

    def test_locator_in_comment_does_not_define_an_executed_source_case(self):
        self.run_case(selected='ClaimTests.test_claim_once')
        content = '# ClaimTests.test_claim_once\n'
        files = [{'path': 'tests/test_claim.py', 'content': content,
            'sha256': hashlib.sha256(content.encode()).hexdigest()}]
        self.assertEqual(self.verdict(files=files)['status'], 'BLOCKED')

    def test_main_module_requires_exact_direct_script_binding(self):
        source = SOURCE + "\nif __name__ == '__main__': unittest.main(verbosity=2)\n"
        self.run_case(source, argv=[sys.executable, '-B', 'tests/test_claim.py', 'ClaimTests.test_claim_once'])
        self.assertEqual(self.verdict()['status'], 'PASS')
        self.assertEqual(self.verdict(test_id='__main__.ClaimTests.test_claim_once')['status'], 'PASS')
        self.run_case(source, argv=[sys.executable, '-B', 'tests/test_other.py'], test_source='tests/test_other.py')
        files = [{'path': 'tests/test_claim.py', 'content': source,
            'sha256': hashlib.sha256(source.encode()).hexdigest()}]
        self.assertEqual(self.verdict(files=files)['status'], 'BLOCKED')

    def test_direct_script_identity_requires_the_executed_argv_binding(self):
        source = SOURCE + "\nif __name__ == '__main__': unittest.main(verbosity=2)\n"
        self.run_case(source, argv=[sys.executable, '-B', 'tests/test_claim.py'])
        # Corrupt the actual launch field while retaining the configured recipe.
        with transaction(self.case.store.db):
            old = read_record(self.case.store.db, 'checks', 'adequacy')
            bundle = old['payload']
            bundle['results'][0]['argv'] = [sys.executable, '-B', 'tests/test_other.py']
            write_record(self.case.store.db, 'checks', 'adequacy', bundle,
                expected_revision=old['revision'])
        self.case.current['check_hash'] = hash_value(bundle)
        self.assertEqual(self.verdict()['status'], 'BLOCKED')

    def test_main_named_source_cannot_bypass_direct_script_requirement(self):
        output = 'test_claim_once (__main__.ClaimTests.test_claim_once) ... ok\nRan 1 test in 0.001s\nOK\n'
        self.run_case(test_source='__main__.py',
            argv=[sys.executable, '-B', '-c', 'print(' + repr(output) + ')'])
        files = [{'path': '__main__.py', 'content': SOURCE,
            'sha256': hashlib.sha256(SOURCE.encode()).hexdigest()}]
        self.assertEqual(self.verdict(test_source='__main__.py', files=files)['status'], 'BLOCKED')

    def test_verbose_docstring_case_still_proves_success(self):
        source = SOURCE.replace(' def test_claim_once(self):',
            ' def test_claim_once(self):\n  """Actual claim boundary."""')
        self.run_case(source, selected='ClaimTests.test_claim_once')
        self.assertEqual(self.verdict()['status'], 'PASS')

    def test_explicit_discovery_top_level_preserves_source_module_binding(self):
        (self.case.repo / 'tests/__init__.py').write_text('', encoding='utf-8')
        self.run_case(argv=[sys.executable, '-B', '-m', 'unittest', 'discover',
            '-s', 'tests', '-t', '.', '-v', '-k', 'ClaimTests.test_claim_once'])
        self.assertEqual(self.verdict(test_id='tests.test_claim.ClaimTests.test_claim_once')['status'], 'PASS')
        self.assertEqual(self.verdict(test_id='test_claim.ClaimTests.test_claim_once')['status'], 'BLOCKED')

    def test_failed_subtest_is_a_failed_cited_case(self):
        source = SOURCE.replace("   self.assertEqual(sorted(f.result(timeout=5) for f in futures),['empty','one'])",
            '   with self.subTest(value=1):\n    self.assertEqual(1, 2)')
        self.run_case(source, selected='ClaimTests.test_claim_once')
        self.assertEqual(self.verdict()['status'], 'FAIL')

    def test_unittest_error_is_a_failed_cited_case(self):
        self.run_case(SOURCE.replace("   self.assertEqual(sorted(f.result(timeout=5) for f in futures),['empty','one'])",
            "   raise RuntimeError('broken claim')"), selected='ClaimTests.test_claim_once')
        self.assertEqual(self.verdict()['status'], 'FAIL')

    def test_older_unittest_verbose_identity_is_supported(self):
        # Known Python 3.11 verbose grammar; this is a parser protocol fixture.
        output = 'test_claim_once (tests.test_claim.ClaimTests) ... ok\n\nRan 1 test in 0.001s\n\nOK\n'
        self.run_case(argv=[sys.executable, '-B', '-c', 'print(' + repr(output) + ')'])
        self.assertEqual(self.verdict()['status'], 'PASS')

    def pytest_output(self, line):
        # Known verbose pytest protocol grammar, not a claimed pytest execution.
        program = 'print(' + repr(line + '\n================ 1 passed in 0.01s ================') + ')'
        return self.run_case(parser='pytest', argv=[sys.executable, '-B', '-c', program])

    def test_pytest_node_identity_and_success_are_required(self):
        self.pytest_output('tests/test_claim.py::ClaimTests::test_other PASSED [100%]')
        self.assertEqual(self.verdict()['status'], 'BLOCKED')
        self.pytest_output('tests/test_claim.py::ClaimTests::test_claim_once PASSED [100%]')
        self.assertEqual(self.verdict()['status'], 'PASS')
        self.assertEqual(self.verdict(test_id='tests/test_claim.py::ClaimTests::test_claim_once')['status'], 'PASS')
        self.assertEqual(self.verdict(test_id='tests/test_wrong.py::ClaimTests::test_claim_once')['status'], 'BLOCKED')

    def test_pytest_failed_and_skipped_case_outcomes_are_distinct(self):
        self.pytest_output('tests/test_claim.py::ClaimTests::test_claim_once FAILED [100%]')
        self.assertEqual(self.verdict()['status'], 'FAIL')
        self.pytest_output('tests/test_claim.py::ClaimTests::test_claim_once SKIPPED [100%]')
        self.assertEqual(self.verdict()['status'], 'BLOCKED')

    def test_pytest_parameter_identity_must_match_exactly(self):
        self.pytest_output('tests/test_claim.py::ClaimTests::test_claim_once[a.b] PASSED [100%]')
        self.assertEqual(self.verdict(test_id='ClaimTests.test_claim_once[a.b]')['status'], 'PASS')
        self.assertEqual(self.verdict(test_id='ClaimTests.test_claim_once[other]')['status'], 'BLOCKED')
        self.assertEqual(self.verdict()['status'], 'BLOCKED')

    def test_pytest_same_leaf_in_other_source_does_not_prove_cited_case(self):
        self.pytest_output('tests/test_other.py::ClaimTests::test_claim_once PASSED [100%]')
        self.assertEqual(self.verdict()['status'], 'BLOCKED')


if __name__ == '__main__':
    unittest.main()
