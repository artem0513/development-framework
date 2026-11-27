"""Bugfix policy and real adequacy-plan integration; fixtures grant no approval."""
import copy
from contextlib import closing
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from ase.testing import adequacy
from ase.testing import bugfix
from ase.tasks import TaskService, validate_contract_v2
from ase.store import Store
from ase.state import read_record, write_record, transaction, hash_value
from test_adequacy_policy import software_contract
import test_test_adequacy as existing


def dossier(stage='code', scenario='concurrent'):
    return {'stage_id':stage, 'symptom':'Consumer loses the returned value',
        'expected':'Consumer uses the contractually complete response',
        'first_divergence':'Caller returns the request after the callee returns the response',
        'cause_status':'CONFIRMED',
        'trace':[{'path':'tests/test_claim.py','locator':'test_claim_once',
            'observation':'Concrete caller/consumer observation in the captured source'}],
        'causal_evidence':'Observed input, response and consumed value differ at the caller',
        'fix_rationale':'Use returned response at the existing boundary',
        'alternatives':'An extra merger would mask the caller error and drop legitimate fields',
        'regression_scenario_ids':[scenario]}


class BugfixContractTests(unittest.TestCase):
    def contract(self):
        value=software_contract()
        value['test_adequacy']={'version':1,'required':True}
        value['bugfix_evidence']={'version':1,'required':True}
        return value

    def test_explicit_policy_requires_software_and_adequacy(self):
        self.assertTrue(bugfix.required(validate_contract_v2(self.contract())))
        for field, value in [('test_adequacy',None),('bugfix_evidence',{'version':True,'required':True}),
                ('bugfix_evidence',{'version':1,'required':False})]:
            contract=self.contract()
            if value is None:contract.pop(field)
            else:contract[field]=value
            with self.subTest(field=field,value=value),self.assertRaises(ValueError):
                validate_contract_v2(contract)

    def test_quiet_unittest_check_fails_at_create_with_runtime_reason(self):
        contract=self.contract()
        contract['evidence'][0]['argv'].remove('-v')
        with self.assertRaisesRegex(ValueError,'implement check tests.*'+adequacy.CASE_PROOF_REQUIRED+'.*-v'):
            bugfix.validate_contract(contract)
        with self.assertRaisesRegex(ValueError,'implement check tests'):
            validate_contract_v2(contract)
        contract['evidence'][0]['argv'].append('-v')
        bugfix.validate_contract(contract)
        validate_contract_v2(contract)
        bugfix.validate_contract(software_contract())

    def test_unknown_scope_effect_fails_at_create(self):
        contract=software_contract()
        contract['scope']['effects']=['write']
        with self.assertRaisesRegex(ValueError,'Unknown scope effect'):
            validate_contract_v2(contract)

    def test_quiet_suite_beside_a_verbose_regression_check_is_accepted(self):
        contract=self.contract()
        contract['evidence'].append({'id':'suite','kind':'test','parser':'unittest',
            'argv':[sys.executable,'-B','-m','unittest','discover','-q']})
        contract['acceptance'][0]['evidence_ids'].append('suite')
        validate_contract_v2(contract)
        contract['evidence'][0]['argv'].remove('-v')
        with self.assertRaisesRegex(ValueError,'cannot show the regression test'):
            validate_contract_v2(contract)

    def test_lint_command_beside_a_quiet_unittest_still_fails_at_create(self):
        contract=self.contract()
        contract['evidence'][0]['argv'].remove('-v')
        contract['evidence'].append({'id':'lint','kind':'command','argv':[sys.executable,'-m','compileall','-q','.']})
        contract['acceptance'][0]['evidence_ids'].append('lint')
        with self.assertRaisesRegex(ValueError,'or add a separate verbose unittest/pytest check'):
            validate_contract_v2(contract)
        for argv,runner in (([sys.executable,'-m','pytest'],True),(['C:\\tools\\pytest.exe','-q'],True),
                ([sys.executable,'-mpytest'],True),(['ruff','check','tests/pytest'],False),
                (['mypy','tests/unittest'],False),(['npm','test'],False)):
            with self.subTest(argv=argv):
                self.assertEqual(bugfix._per_case_runner(argv),runner)

    def test_read_only_effect_cannot_be_combined(self):
        contract=software_contract()
        contract['scope']['effects']=['read_only','file_write']
        with self.assertRaisesRegex(ValueError,'read_only cannot be combined'):
            validate_contract_v2(contract)

    def test_quiet_unittest_predicate(self):
        python=sys.executable
        for argv,quiet in (([python,'-B','-m','unittest','discover'],True),
                ([python,'-m','unittest','discover','-s','tests','-v'],False),
                ([python,'-m','unittest','--verbose','tests.test_x'],False),
                ([python,'-m','unittest','-v','-q'],True),
                ([python,'-m','unittest','-fv'],False),
                ([python,'-m','unittest','-bfv','tests'],False),
                ([python,'-m','unittest','-f'],True),
                (['py','-3','-m','unittest'],True),
                ([python,'-m','pytest','-q'],False),
                ([python,'tests/run.py','-m','unittest'],False),
                (['npm','-m','unittest'],False)):
            with self.subTest(argv=argv):
                self.assertEqual(adequacy.quiet_unittest(argv),quiet)

    def test_list_alternatives_name_expected_string_type(self):
        row=dossier();row['alternatives']=[{'option':'merger','why_not':'masks the defect'}]
        with self.assertRaisesRegex(ValueError,'bugfix alternatives must be one string.*got list'):
            bugfix.validate_dossiers({'bugfixes':[row]})
        self.assertEqual(bugfix.validate_dossiers({'bugfixes':[dossier()]})[0]['alternatives'],dossier()['alternatives'])
    def test_legacy_contract_unchanged_and_mixed_only_software(self):
        legacy=software_contract()
        self.assertNotIn('bugfix_evidence',validate_contract_v2(legacy))
        self.assertFalse(bugfix.required(legacy))
        mixed=self.contract();mixed['profile']='mixed'
        mixed['stages']=[{'id':'code','profile':'software','acceptance_ids':['QUEUE']}, {'id':'notes','profile':'document'}]
        self.assertTrue(bugfix.required(mixed,'code'))
        self.assertFalse(bugfix.required(mixed,'notes'))
        with self.assertRaises(ValueError):bugfix.required(mixed,'missing')

    def test_cli_explicit_bugfix_and_restart_preserve_policy(self):
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp);repo=base/'repo';repo.mkdir()
            file=base/'task.json';file.write_text(json.dumps(software_contract()))
            result=subprocess.run([sys.executable,'-B','-m','ase','--repo',str(repo),
                '--home',str(base/'state'),'task','create','--file',str(file),'--bugfix'],
                text=True,capture_output=True)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
            with closing(Store(repo,base/'state')) as store:
                task=TaskService(store).snapshot('adequacy-policy')
                self.assertEqual(task['contract']['bugfix_evidence'],{'version':1,'required':True})
                revised=TaskService(store).revise('adequacy-policy',software_contract(),expected_revision=task['revision'])
                self.assertEqual(revised['contract']['bugfix_evidence'],task['contract']['bugfix_evidence'])

    def test_child_inherits_bugfix_policy_from_approved_parent(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo=Path(tmp)/'repo';repo.mkdir()
            with closing(Store(repo,Path(tmp)/'state')) as store:
                tasks=TaskService(store);task=tasks.create(self.contract())
                task=tasks.propose(task['task_id'],{'summary':'Repair confirmed defect','stage_ids':['implement']},expected_revision=task['revision'])
                tasks.approve(task['task_id'],proposal_hash=task['proposal_hash'],user_message_ref='test:owner',approved_stage_ids=['implement'])
                child=software_contract();child.update(id='child',parent_task_id=task['task_id'],parent_stage_id='implement')
                saved=tasks.create(child)
                self.assertTrue(bugfix.required(saved['contract']))
                self.assertEqual(saved['contract']['bugfix_evidence'],self.contract()['bugfix_evidence'])

    def test_inherited_policy_is_validated_before_child_persistence(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo=Path(tmp)/'repo';repo.mkdir()
            with closing(Store(repo,Path(tmp)/'state')) as store:
                tasks=TaskService(store);parent=self.contract()
                parent['stages']=[{'id':'edit','acceptance_ids':[]},
                    {'id':'implement','acceptance_ids':['QUEUE'],'depends_on':['edit']}]
                task=tasks.create(parent)
                task=tasks.propose(task['task_id'],{'summary':'Edit then validate','stage_ids':['edit','implement']},expected_revision=task['revision'])
                tasks.approve(task['task_id'],proposal_hash=task['proposal_hash'],user_message_ref='test:owner',approved_stage_ids=['edit','implement'])
                child=software_contract();child.update(id='unmapped-child',parent_task_id=task['task_id'],parent_stage_id='edit')
                child['stages'][0]['acceptance_ids']=[]
                with self.assertRaises(ValueError):tasks.create(child)
                self.assertIsNone(store.db.execute('SELECT id FROM tasks WHERE id=?',('unmapped-child',)).fetchone())


class BugfixPlanTests(existing.AdequacyFixture):
    def setUp(self):
        super().setUp()
        task=self.tasks.snapshot('adequacy');contract=task['contract']
        contract['bugfix_evidence']={'version':1,'required':True}
        task=self.tasks.revise('adequacy',contract,expected_revision=task['revision'])
        task=self.tasks.propose('adequacy',{'summary':'Repair a confirmed consumer failure','stage_ids':['code']},expected_revision=task['revision'])
        self.tasks.approve('adequacy',proposal_hash=task['proposal_hash'],user_message_ref='test:owner',approved_stage_ids=['code'])
        self.plan_payload['bugfixes']=[dossier()]

    def test_plan_survives_restart_without_rewriting_old_state(self):
        saved=self.planned()
        saved.pop('next_action',None)
        with closing(Store(self.repo,self.base/'state')) as other:
            self.assertEqual(adequacy.TestAdequacy(other).snapshot('adequacy'),saved)

    def test_missing_duplicate_and_cross_stage_or_optional_regression_rejected(self):
        for mutation in ('missing','duplicate','wrong-stage','unknown-scenario','optional','escape','empty-trace'):
            value=copy.deepcopy(self.plan_payload)
            if mutation=='missing':value.pop('bugfixes')
            elif mutation=='duplicate':value['bugfixes']*=2
            elif mutation=='wrong-stage':value['bugfixes'][0]['stage_id']='other'
            elif mutation=='unknown-scenario':value['bugfixes'][0]['regression_scenario_ids']=['missing']
            elif mutation=='optional':value['scenarios'][0]['required']=False
            elif mutation=='escape':value['bugfixes'][0]['trace'][0]['path']='../outside.py'
            else:value['bugfixes'][0]['trace']=[]
            with self.subTest(mutation=mutation),self.assertRaises(ValueError):
                self.service.plan('adequacy',value,expected_revision=0)

    def test_hypothesis_and_workaround_can_be_saved_but_not_misreported(self):
        for status in ('HYPOTHESIS','WORKAROUND'):
            value=copy.deepcopy(self.plan_payload);value['bugfixes'][0]['cause_status']=status
            saved=self.service.plan('adequacy',value,expected_revision=self.service.snapshot('adequacy')['revision'])
            self.assertEqual(saved['payload']['bugfixes'][0]['cause_status'],status)

    def test_changing_causal_evidence_invalidates_identity(self):
        saved=self.planned();value=copy.deepcopy(self.plan_payload)
        value['bugfixes'][0]['causal_evidence']='A newer observed boundary contradicts the first theory'
        changed=self.service.plan('adequacy',value,expected_revision=saved['revision'])
        self.assertNotEqual(saved['payload']['test_plan_sha256'],changed['payload']['test_plan_sha256'])

    def _trace(self,locator,version=None):
        value=copy.deepcopy(self.plan_payload)
        ref=value['bugfixes'][0]['trace'][0];ref['locator']=locator
        if version:ref['version']=version
        return value

    def _baseline(self,*,commit=False):
        from ase.tasks import record_baseline
        git=['git','-C',str(self.repo),'-c','user.name=t','-c','user.email=t@example.invalid','-c','core.autocrlf=false']
        subprocess.run([*git,'init','-q'],check=True,capture_output=True)
        if commit:
            subprocess.run([*git,'add','tests/test_claim.py'],check=True,capture_output=True)
            subprocess.run([*git,'commit','-q','-m','baseline'],check=True,capture_output=True)
        self.assertIsNotNone(record_baseline(self.store,'adequacy',self.tasks.snapshot('adequacy')['contract'],kind='approval_baseline'))

    def test_non_literal_trace_locator_rejected_at_plan_time(self):
        self._baseline()
        for version in (None,'before'):
            label='before' if version else 'current'
            with self.subTest(version=label),self.assertRaisesRegex(ValueError,
                    r'locator must be literal text present in tests/test_claim\.py \('+label+r' version\).*ClaimTests\.test_claim_once'):
                self.service.plan('adequacy',self._trace('ClaimTests.test_claim_once',version),expected_revision=0)
        self.assertIsNone(self.service.snapshot('adequacy')['payload'])
        file=self.base/'non-literal-plan.json';file.write_text(json.dumps(self._trace('ClaimTests.test_claim_once','before')),encoding='utf-8')
        result=subprocess.run([sys.executable,'-B','-m','ase','--repo',str(self.repo),'--home',str(self.store.state_home),
            'adequacy','plan','adequacy','--file',str(file),'--expected-revision','0'],capture_output=True,text=True)
        self.assertNotEqual(result.returncode,0)
        self.assertIn('must be literal text present in tests/test_claim.py (before version)',result.stdout+result.stderr)

    def test_literal_trace_locator_accepted_for_current_and_before(self):
        self._baseline()
        saved=self.service.plan('adequacy',self._trace('def test_claim_once(self):'),expected_revision=0)
        saved=self.service.plan('adequacy',self._trace('class ClaimTests(','before'),expected_revision=saved['revision'])
        self.assertEqual(saved['payload']['bugfixes'][0]['trace'][0]['version'],'before')

    def test_before_locator_uses_baseline_bytes_not_edited_working_file(self):
        self._baseline(commit=True)
        file=self.repo/'tests/test_claim.py'
        file.write_bytes(self.test_source.replace('def claim():','def claim_fixed():').encode())
        saved=self.service.plan('adequacy',self._trace('def claim():','before'),expected_revision=0)
        self.assertEqual(saved['payload']['bugfixes'][0]['trace'][0]['locator'],'def claim():')
        for locator,version in (('def claim_fixed():','before'),('def claim():',None)):
            with self.subTest(version=version),self.assertRaisesRegex(ValueError,
                    r'present in tests/test_claim\.py \('+(version or 'current')+r' version\)'):
                self.service.plan('adequacy',self._trace(locator,version),expected_revision=saved['revision'])

    def test_unresolvable_before_bytes_never_reject(self):
        # No recorded baseline (here: not a Git repository) means verification bytes are unknown.
        saved=self.service.plan('adequacy',self._trace('ClaimTests.test_claim_once','before'),expected_revision=0)
        self.assertEqual(saved['payload']['bugfixes'][0]['trace'][0]['version'],'before')

    def test_verification_message_names_file_version_and_literal_rule(self):
        contract=self.tasks.snapshot('adequacy')['contract'];row=dossier()
        row['trace']=[{'path':'tests/test_claim.py','locator':'ClaimTests.test_claim_once','observation':'o','version':'before'},
            {'path':'tests/test_claim.py','locator':'test_claim_once','observation':'o'}]
        judged={field:{} for field in ('cause','necessity','regression')}
        reasons=[]
        bugfix.assess(contract,{'bugfixes':[row]},{'bugfixes':[{'stage_id':'code',**judged}]},'code',
            {'_ase_before/tests/test_claim.py':{'content':self.test_source}},{'concurrent':{'evidence_ids':['suite']}},
            None,'software',lambda *args:None,reasons,[])
        self.assertEqual(len(reasons),2,reasons)
        self.assertIn('must be literal text present in tests/test_claim.py (before version)',reasons[0])
        self.assertIn("'ClaimTests.test_claim_once'",reasons[0])
        self.assertIn('tests/test_claim.py (current version) is not in the captured source packet',reasons[1])

    def test_empty_acceptance_delivery_stage_does_not_require_fictitious_regression(self):
        task=self.tasks.snapshot('adequacy');contract=task['contract']
        contract['stages'].append({'id':'delivery','acceptance_ids':[],'depends_on':['code']})
        task=self.tasks.revise('adequacy',contract,expected_revision=task['revision'])
        self.tasks.propose('adequacy',{'summary':'Repair then coordinate delivery','stage_ids':['code']},expected_revision=task['revision'])
        saved=self.planned()
        self.assertEqual([row['stage_id'] for row in saved['payload']['bugfixes']],['code'])
        self.assertFalse(bugfix.required(contract,'delivery'))

    def test_fresh_repair_requires_current_confirmed_plan_before_model_or_reservation(self):
        from ase.workflow import WorkflowController
        from ase.discovery import discover_capabilities,record_discovery
        record_discovery(self.store,'adequacy',discover_capabilities(self.store.project,
            scope={'resources':['tests']},backend_config={}),expected_revision=0)
        controller=WorkflowController(self.store)
        subprocess.run(['git','init','-q',str(self.repo)],check=True,capture_output=True)
        task=self.tasks.snapshot('adequacy');stage=task['contract']['stages'][0]
        for mode in ('missing','hypothesis','stale'):
            if mode!='missing':
                value=copy.deepcopy(self.plan_payload);value['bugfixes'][0]['cause_status']='HYPOTHESIS' if mode=='hypothesis' else 'CONFIRMED'
                self.service.plan('adequacy',value,expected_revision=self.service.snapshot('adequacy')['revision'])
            if mode=='stale':
                self.tasks.update_execution_plan('adequacy',{'checks':[self.spec,{**self.spec,'id':'extra'}]},expected_revision=self.tasks.execution_plan('adequacy')['revision'])
            with self.subTest(mode=mode),patch.object(controller,'_request',side_effect=AssertionError('Must not launch implementation')):
                result=controller._run_stage(task,stage,None,None,resume=False)
                self.assertEqual(result['status'],'BLOCKED')
                self.assertFalse(result['needs_owner_input'])
                self.assertIn('investigat',result['next_action'].lower())
                self.assertIsNone(read_record(self.store.db,'cycle','adequacy:code')['payload'])

    def test_confirmed_current_plan_can_reach_implementation(self):
        from ase.workflow import WorkflowController
        from ase.discovery import discover_capabilities,record_discovery
        record_discovery(self.store,'adequacy',discover_capabilities(self.store.project,
            scope={'resources':['tests']},backend_config={}),expected_revision=0)
        self.planned();task=self.tasks.snapshot('adequacy');controller=WorkflowController(self.store)
        subprocess.run(['git','init','-q',str(self.repo)],check=True,capture_output=True)
        class ReachedImplementation(Exception):pass
        with patch.object(controller,'_request',side_effect=ReachedImplementation),self.assertRaises(ReachedImplementation):
            controller._run_stage(task,task['contract']['stages'][0],None,None,resume=False)

    def _repair_execution(self):
        from ase.workflow import WorkflowController
        from ase.runner import Runner
        from ase.adapters import get_adapter
        from ase.discovery import discover_capabilities,record_discovery
        from test_runner import FIXTURE
        record_discovery(self.store,'adequacy',discover_capabilities(self.store.project,
            scope={'resources':['tests']},backend_config={}),expected_revision=0)
        self.planned()
        subprocess.run(['git','init','-q',str(self.repo)],check=True,capture_output=True)
        trusted=self.base/'trusted';trusted.mkdir()
        fixture=self.base/'repair-fixture.py'
        fixture.write_text(FIXTURE.replace("payload = {'verdict':'PASS'}",
            "payload = {'edits':[],'summary':'Protocol-only empty proposal'}"),encoding='utf-8')
        runner=Runner({'executable':sys.executable,'executable_args':[str(fixture),'success','codex'],
            'trusted_cwd':str(trusted),'protocol_only':True},self.store.root/'tasks'/'adequacy'/'sessions')
        return self.tasks.snapshot('adequacy'),WorkflowController(self.store),runner,get_adapter('codex'),trusted

    def _replace_repair_plan(self,status):
        value=copy.deepcopy(self.plan_payload)
        value['bugfixes'][0]['cause_status']=status
        value['bugfixes'][0]['symptom']='Updated investigation at the launch boundary'
        return self.service.plan('adequacy',value,expected_revision=self.service.snapshot('adequacy')['revision'])

    def _candidate_invocations(self,trusted):
        log=trusted/'invocations.jsonl'
        rows=[json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
        return [args for args in rows if args[:1]==['exec'] and args!=['exec','--help']]

    def test_plan_withdrawn_during_reservation_cannot_spend_or_prepare_candidate(self):
        task,controller,runner,adapter,_=self._repair_execution()
        reserve=controller.reserve_cycle
        def replace_then_reserve(*args,**kwargs):
            self._replace_repair_plan('HYPOTHESIS')
            return reserve(*args,**kwargs)
        with patch.object(controller,'reserve_cycle',side_effect=replace_then_reserve),patch.object(
                controller,'_request',side_effect=AssertionError('Withdrawn repair prepared a candidate')):
            result=controller._run_stage(task,task['contract']['stages'][0],runner,adapter,resume=False)
        self.assertEqual(result['status'],'BLOCKED')
        self.assertFalse(result['needs_owner_input'])
        self.assertIn('investigat',result['next_action'].lower())
        self.assertIsNone(read_record(self.store.db,'budget','adequacy')['payload'])
        self.assertIsNone(read_record(self.store.db,'cycle','adequacy:code')['payload'])
        self.assertEqual(self.service.snapshot('adequacy')['payload']['bugfixes'][0]['cause_status'],'HYPOTHESIS')

    def _assert_changed_plan_denies_actual_launch(self,status):
        from ase.runner import verify_capture
        task,controller,runner,adapter,trusted=self._repair_execution()
        pinned=self.service.snapshot('adequacy')['payload']['test_plan_sha256']
        build=adapter.build_invocation
        def replace_before_spawn(*args,**kwargs):
            # Preparation/probes must not hold SQLite's writer across backend waits.
            self.assertFalse(self.store.db.in_transaction)
            self._replace_repair_plan(status)
            return build(*args,**kwargs)
        with patch.object(adapter,'build_invocation',side_effect=replace_before_spawn),patch(
                'ase.local_edit_intent.prepare_local_edits',side_effect=AssertionError('Withdrawn repair reached edits')):
            result=controller._run_stage(task,task['contract']['stages'][0],runner,adapter,resume=False)
        self.assertEqual(result['status'],'BLOCKED')
        self.assertFalse(result['needs_owner_input'])
        self.assertIn('investigat',result['next_action'].lower())
        self.assertEqual(self._candidate_invocations(trusted),[])
        receipt=read_record(self.store.db,'stage_implementation','adequacy:code')['payload']
        self.assertIsNone(receipt['pid'])
        self.assertEqual(receipt['status'],'BLOCKED')
        self.assertTrue(verify_capture(receipt)['capture_only'])
        progress=read_record(self.store.db,'cycle','adequacy:code')['payload']
        self.assertEqual(progress['repair_test_plan_sha256'],pinned)
        self.assertEqual(progress['implementation_capture']['execution_id'],receipt['execution_id'])
        self.assertEqual(read_record(self.store.db,'budget','adequacy')['payload']['task_cycles'],1)
        self.assertIsNone(read_record(self.store.db,'active_call','adequacy')['payload'])
        self.assertFalse(self.store.db.in_transaction)
        # A denied durable capture remains a reconciliation obligation on resume.
        with patch.object(controller,'reserve_cycle',side_effect=AssertionError('Denied capture was resubmitted')):
            resumed=controller._run_stage(task,task['contract']['stages'][0],runner,adapter,resume=True)
        self.assertEqual(resumed['status'],'BLOCKED')
        self.assertIn('reconciliation',resumed['blocked_reasons'][0])
        self.assertEqual(self._candidate_invocations(trusted),[])

    def test_workaround_saved_just_before_actual_spawn_prevents_model_invocation(self):
        self._assert_changed_plan_denies_actual_launch('WORKAROUND')

    def test_changed_confirmed_plan_just_before_spawn_cannot_use_old_admission(self):
        self._assert_changed_plan_denies_actual_launch('CONFIRMED')

    def test_unchanged_confirmed_plan_launches_under_short_transaction(self):
        from ase.runner import verify_receipt
        task,controller,runner,adapter,trusted=self._repair_execution()
        pinned=self.service.snapshot('adequacy')['payload']['test_plan_sha256']
        popen=subprocess.Popen;launch_transactions=[]
        def observed_spawn(argv,*args,**kwargs):
            if 'success' in argv and argv[argv.index('success')+2:][:1]==['exec'] and argv[-1]=='-':
                launch_transactions.append(self.store.db.in_transaction)
            return popen(argv,*args,**kwargs)
        class ReachedLocalEdits(Exception):pass
        with patch('ase.runner.subprocess.Popen',side_effect=observed_spawn),patch(
                'ase.local_edit_intent.prepare_local_edits',side_effect=ReachedLocalEdits),self.assertRaises(ReachedLocalEdits):
            controller._run_stage(task,task['contract']['stages'][0],runner,adapter,resume=False)
        self.assertEqual(launch_transactions,[True])
        self.assertEqual(len(self._candidate_invocations(trusted)),1)
        receipt=read_record(self.store.db,'stage_implementation','adequacy:code')['payload']
        self.assertTrue(verify_receipt(receipt)['valid'])
        request=json.loads(Path(receipt['artifacts']['request']['path']).read_text())
        self.assertEqual(request['handoff']['test_plan_sha256'],pinned)
        self.assertFalse(self.store.db.in_transaction)

    def test_split_edit_then_acceptance_cannot_bypass_causal_preflight(self):
        from ase.workflow import WorkflowController
        from ase.discovery import discover_capabilities,record_discovery
        task=self.tasks.snapshot('adequacy');contract=task['contract']
        contract['stages']=[{'id':'edit','acceptance_ids':[]},
            {'id':'code','acceptance_ids':['AC1'],'depends_on':['edit']}]
        task=self.tasks.revise('adequacy',contract,expected_revision=task['revision'])
        task=self.tasks.propose('adequacy',{'summary':'Edit then validate the same repair','stage_ids':['edit','code']},expected_revision=task['revision'])
        task=self.tasks.approve('adequacy',proposal_hash=task['proposal_hash'],user_message_ref='test:split-plan',approved_stage_ids=['edit','code'])
        record_discovery(self.store,'adequacy',discover_capabilities(self.store.project,scope={'resources':['tests']},backend_config={}),expected_revision=0)
        subprocess.run(['git','init','-q',str(self.repo)],check=True,capture_output=True)
        controller=WorkflowController(self.store)
        with patch.object(controller,'_request',side_effect=AssertionError('No implementation before investigation')):
            result=controller._run_stage(task,task['contract']['stages'][0],None,None,resume=False)
        self.assertEqual(result['status'],'BLOCKED')
        self.assertFalse(result['needs_owner_input'])
        self.assertIsNone(read_record(self.store.db,'cycle','adequacy:edit')['payload'])
        value=copy.deepcopy(self.plan_payload);value['bugfixes'][0]['cause_status']='HYPOTHESIS'
        self.service.plan('adequacy',value,expected_revision=0)
        self.assertFalse(bugfix.repair_readiness(self.store,'adequacy','edit')['ready'])
        value['bugfixes'][0]['cause_status']='CONFIRMED'
        self.service.plan('adequacy',value,expected_revision=1)
        self.assertTrue(bugfix.repair_readiness(self.store,'adequacy','edit')['ready'])

    def test_dependency_mapping_uses_related_boundaries_and_skips_nonmutating_coordination(self):
        contract=self.tasks.snapshot('adequacy')['contract']
        contract['stages']=[{'id':'edit','scope':{'effects':['file_write']},'acceptance_ids':[],'depends_on':[]},
            {'id':'verify','acceptance_ids':['AC1'],'depends_on':['edit']},
            {'id':'other','acceptance_ids':['AC2'],'depends_on':[]},
            {'id':'deliver','scope':{'effects':['file_write']},'acceptance_ids':[],'depends_on':['verify']},
            {'id':'coordinate','scope':{'effects':[]},'acceptance_ids':[],'depends_on':[]}]
        self.assertEqual(bugfix.repair_targets(contract,'edit'),{'verify'})
        self.assertEqual(bugfix.repair_targets(contract,'deliver'),{'verify'})
        self.assertEqual(bugfix.repair_targets(contract,'coordinate'),set())
        contract['stages'][1]['depends_on']=[]
        with self.assertRaises(ValueError):bugfix.repair_targets(contract,'edit')

    def test_resume_reconciliation_is_not_blocked_by_fresh_repair_preflight(self):
        from ase.workflow import WorkflowController
        from ase.discovery import discover_capabilities,record_discovery
        record_discovery(self.store,'adequacy',discover_capabilities(self.store.project,
            scope={'resources':['tests']},backend_config={}),expected_revision=0)
        task=self.tasks.snapshot('adequacy');controller=WorkflowController(self.store)
        subprocess.run(['git','init','-q',str(self.repo)],check=True,capture_output=True)
        with transaction(self.store.db):
            write_record(self.store.db,'cycle','adequacy:code',{'phase':'VERIFYING','contract_hash':task['contract_hash'],
                'proposal_hash':task['proposal_hash']},expected_revision=0)
        class ReachedVerification(Exception):pass
        with patch.object(controller,'verify',side_effect=ReachedVerification),patch.object(controller,'check'),self.assertRaises(ReachedVerification):
            controller._run_stage(task,task['contract']['stages'][0],None,None,resume=True)

    def test_verifier_scope_distinguishes_empty_edit_gate_from_future_acceptance(self):
        from ase.workflow import WorkflowController,AC_SCHEMA,REVIEW_SCHEMA
        from ase.review.service import VerificationService
        from ase.discovery import discover_capabilities,record_discovery
        task=self.tasks.snapshot('adequacy');contract=task['contract']
        contract['stages']=[{'id':'edit','acceptance_ids':[]},
            {'id':'code','acceptance_ids':['AC1'],'depends_on':['edit']}]
        task=self.tasks.revise('adequacy',contract,expected_revision=task['revision'])
        task=self.tasks.propose('adequacy',{'summary':'Edit then validate','stage_ids':['edit','code']},expected_revision=task['revision'])
        self.tasks.approve('adequacy',proposal_hash=task['proposal_hash'],user_message_ref='test:split-plan',approved_stage_ids=['edit','code'])
        execution=self.tasks.execution_plan('adequacy')
        self.tasks.update_execution_plan('adequacy',{'checks':[self.spec,{**self.spec,'id':'edit-smoke'}]},expected_revision=execution['revision'])
        self.planned()
        record_discovery(self.store,'adequacy',discover_capabilities(self.store.project,scope={'resources':['tests']},backend_config={}),expected_revision=0)
        subprocess.run(['git','init','-q',str(self.repo)],check=True,capture_output=True)
        controller=WorkflowController(self.store);requests=[]
        class Captured(Exception):pass
        class CaptureRunner:
            def run(_,adapter,request,**kwargs):
                requests.append(request);raise Captured()
        for stage,expected_ac,expected_checks,expected_scenarios,expected_bugfix in [
                ('edit',[],['edit-smoke'],[],[]),
                (None,['AC1'],['suite','edit-smoke'],['concurrent'],['code'])]:
            with patch.object(controller,'_runner',return_value=CaptureRunner()),self.assertRaises(Captured):
                VerificationService(controller,AC_SCHEMA,REVIEW_SCHEMA).verify('adequacy',stage)
            scope=requests[-1]['handoff']['verification_scope']
            self.assertEqual(scope,{'stage_id':stage,'acceptance_ids':expected_ac,'check_ids':expected_checks,
                'scenario_ids':expected_scenarios,'bugfix_stage_ids':expected_bugfix,
                'scenario_check_ids':{name:['suite'] for name in expected_scenarios},
                'bugfix_check_ids':{name:['suite'] for name in expected_bugfix}})
        # Every listed gap blocks, so the assessor is told to stay in scope and keep notes in summary.
        adequacy=[r for r in requests if r['handoff'].get('purpose')=='test_adequacy']
        self.assertTrue(adequacy)
        self.assertIn('only material gaps of handoff.verification_scope',adequacy[-1]['handoff']['instruction'])
        self.assertNotIn("this stage's scenarios",adequacy[-1]['handoff']['instruction'],'whole-task scope covers every stage')
        self.assertIn('Blocking gaps only',adequacy[-1]['schema']['properties']['missing_coverage']['description'])
        from ase.protocol import validate_payload
        from ase.testing.adequacy import ADEQUACY_SCHEMA
        self.assertEqual(validate_payload({'scenarios':[],'additional_scenarios':[],'missing_coverage':[],
            'summary':'nothing to add'},ADEQUACY_SCHEMA)['status'],'VALID')


class BugfixAssessmentTests(unittest.TestCase):
    def setUp(self):
        self.contract={'profile':'software','stages':[{'id':'code','acceptance_ids':['AC1']}],
            'bugfix_evidence':{'version':1,'required':True}}
        self.plan={'bugfixes':[dossier()]}
        self.proof={'check_id':'suite','test_source':'tests/test_claim.py','test_id':'test_claim_once',
            'assertion':'Real caller consumes the returned fields; injected old caller fails'}
        check={'status':'PASS','reason':'Observed discriminating chain','evidence':[self.proof],'missing_coverage':[]}
        self.result={'bugfixes':[{'stage_id':'code','cause':copy.deepcopy(check),
            'necessity':copy.deepcopy(check),'regression':copy.deepcopy(check)}]}
        self.files={'tests/test_claim.py':{'content':'def test_claim_once(): pass'}}

    def assess(self):
        reasons=[];rejected=[];calls=[]
        def assessment(row,label,allowed,*args):
            calls.append((row,label,allowed))
            if row['status']=='FAIL':rejected.append(label)
            elif row['status']!='PASS':reasons.append(label)
            if row['status']=='PASS' and not row['evidence']:reasons.append('missing proof')
        bugfix.assess(self.contract,self.plan,self.result,None,self.files,{'concurrent':{'evidence_ids':['suite']}},
            [],{},assessment,reasons,rejected)
        return reasons,rejected,calls

    def test_schema_requires_three_separate_judgments_only_for_bugfixes(self):
        schema=bugfix.assessment_schema(adequacy.ADEQUACY_SCHEMA,self.contract)
        self.assertIn('bugfixes',schema['required'])
        self.assertNotIn('bugfixes',adequacy.ADEQUACY_SCHEMA['properties'])
        legacy={'profile':'software','stages':[{'id':'code'}]}
        self.assertEqual(bugfix.assessment_schema(adequacy.ADEQUACY_SCHEMA,legacy),adequacy.ADEQUACY_SCHEMA)

    def test_bugfix_schema_rejects_unmapped_current_check_before_gate(self):
        from ase.protocol import validate_payload
        plan={**self.plan,'scenarios':[{'id':'concurrent','evidence_ids':['suite']}]}
        schema=bugfix.assessment_schema(adequacy.ADEQUACY_SCHEMA,self.contract,plan=plan)
        result={**copy.deepcopy(self.result),'scenarios':[],'additional_scenarios':[],
            'missing_coverage':[],'summary':'Protocol/schema test only'}
        self.assertEqual(validate_payload(result,schema)['status'],'VALID')
        result['bugfixes'][0]['regression']['evidence'][0]['check_id']='other-current-check'
        self.assertEqual(validate_payload(result,schema)['status'],'INVALID')

    def test_confirmed_cause_requires_three_evidenced_judgments(self):
        reasons,rejected,calls=self.assess()
        self.assertEqual((reasons,rejected),([],[]))
        self.assertEqual(len(calls),3)
        self.assertTrue(all(call[2]=={'suite'} for call in calls))

    def test_green_tests_do_not_override_unknown_cause_or_missing_necessity(self):
        for status in ('HYPOTHESIS','WORKAROUND'):
            self.plan['bugfixes'][0]['cause_status']=status
            self.assertTrue(self.assess()[0])
        self.plan['bugfixes'][0]['cause_status']='CONFIRMED'
        self.result['bugfixes'][0]['necessity']['status']='UNVERIFIED'
        self.assertTrue(self.assess()[0])

    def test_unknown_duplicate_missing_and_missing_actual_trace_block(self):
        for value in ([],self.result['bugfixes']*2,[{**self.result['bugfixes'][0],'stage_id':'other'}]):
            original=self.result;self.result={'bugfixes':value}
            self.assertTrue(self.assess()[0])
            self.result=original
        self.files['tests/test_claim.py']['content']='unrelated'
        self.assertTrue(self.assess()[0])

    def test_confirmed_semantic_failure_remains_rejection(self):
        self.result['bugfixes'][0]['regression']['status']='FAIL'
        self.assertTrue(self.assess()[1])

    def test_whole_judgment_covers_one_stage_without_cross_stage_proof(self):
        self.contract['stages'].append({'id':'other','acceptance_ids':['AC1']})
        self.plan['bugfixes'].append(dossier('other'))
        self.result['bugfixes'].append({**copy.deepcopy(self.result['bugfixes'][0]),'stage_id':'other'})
        reasons=[];calls=[]
        bugfix.assess(self.contract,self.plan,self.result,'code',self.files,
            {'concurrent':{'evidence_ids':['suite']}},[],{},
            lambda *args:calls.append(args),reasons,[],captured_stage=None)
        self.assertEqual(reasons,[])
        self.assertEqual(len(calls),3)

    def test_deleted_divergence_uses_explicit_authenticated_before_trace(self):
        self.plan['bugfixes'][0]['trace'][0]['version']='before'
        self.files={'_ase_before/tests/test_claim.py':{'content':'def test_claim_once(): pass'}}
        self.assertEqual(self.assess()[0],[])
        self.files={}
        self.assertTrue(self.assess()[0])

    def test_before_trace_never_falls_back_to_current_or_allows_untrusted_prefix(self):
        self.plan['bugfixes'][0]['trace'][0]['version']='before'
        self.assertTrue(self.assess()[0])
        self.plan['bugfixes'][0]['trace'][0]['version']='unknown'
        with self.assertRaises(ValueError):bugfix.validate_dossiers(self.plan)
        self.plan['bugfixes'][0]['trace'][0]['version']='before'
        self.plan['bugfixes'][0]['trace'][0]['path']='_ase_before/tests/test_claim.py'
        with self.assertRaises(ValueError):bugfix.validate_dossiers(self.plan)


class BugfixReceiptTests(unittest.TestCase):
    """Actual Runner artifacts/logs; semantic seam is explicit, never approval."""
    def setUp(self):
        self.case=case=existing.AdequacyGateTests()
        case.setUp();self.addCleanup(case.doCleanups)
        task=case.tasks.snapshot('adequacy');contract=task['contract']
        contract['bugfix_evidence']={'version':1,'required':True}
        task=case.tasks.revise('adequacy',contract,expected_revision=task['revision'])
        task=case.tasks.propose('adequacy',{'summary':'Require causal consumer proof','stage_ids':['code']},expected_revision=task['revision'])
        task=case.tasks.approve('adequacy',proposal_hash=task['proposal_hash'],user_message_ref='test:owner',approved_stage_ids=['code'])
        case.plan_payload['bugfixes']=[dossier()]
        case.saved=case.service.plan('adequacy',case.plan_payload,expected_revision=case.saved['revision'])
        with transaction(case.store.db):
            record=read_record(case.store.db,'checks','adequacy');bundle=record['payload']
            bundle['contract_hash']=task['contract_hash']
            write_record(case.store.db,'checks','adequacy',bundle,expected_revision=record['revision'])
        case.current.update(contract_hash=task['contract_hash'],proposal_hash=task['proposal_hash'],check_hash=hash_value(bundle))
        check=copy.deepcopy(case.payload['scenarios'][0]);check.pop('id')
        case.payload['bugfixes']=[{'stage_id':'code',**{field:copy.deepcopy(check) for field in ('cause','necessity','regression')}}]
        self.schema=bugfix.assessment_schema(adequacy.ADEQUACY_SCHEMA,task['contract'],plan=case.saved['payload'])

    def test_actual_receipt_policy_requires_causal_schema_and_independent_provider(self):
        case=self.case;case.receipt(schema=self.schema)
        self.assertEqual(case.gate()['status'],'PASS')


        self.assertEqual(case.gate(semantic_seam=False)['status'],'BLOCKED')
        self.assertEqual(case.gate(identity_changes={'schema_sha256':hash_value(adequacy.ADEQUACY_SCHEMA)})['status'],'BLOCKED')

    def test_green_scenario_without_necessary_fix_evidence_cannot_pass(self):
        case=self.case
        case.payload['bugfixes'][0]['necessity']['evidence']=[]
        case.receipt(schema=self.schema)
        self.assertEqual(case.gate()['status'],'BLOCKED')
        case.payload['bugfixes'][0]['necessity']['status']='FAIL'
        case.receipt(schema=self.schema)
        self.assertEqual(case.gate()['status'],'FAIL')

    def test_saved_workaround_and_changed_dossier_do_not_reuse_old_pass(self):
        case=self.case;case.receipt(schema=self.schema)
        case.plan_payload['bugfixes'][0]['cause_status']='WORKAROUND'
        case.saved=case.service.plan('adequacy',case.plan_payload,expected_revision=case.saved['revision'])
        self.assertEqual(case.gate()['status'],'BLOCKED')
        case.receipt(schema=self.schema)
        result=case.gate()
        self.assertEqual(result['status'],'BLOCKED')
        self.assertTrue(any('not confirmed' in reason for reason in result['blocked_reasons']),result)

    def test_unavailable_causal_reviewer_does_not_complete(self):
        self.assertEqual(self.case.gate()['status'],'BLOCKED')

    def test_whole_receipt_can_cover_selected_stage(self):
        self.case.receipt(stage=None,schema=self.schema)
        self.assertEqual(self.case.gate()['status'],'PASS')

    def test_actual_captured_before_trace_is_required_without_current_fallback(self):
        import hashlib
        case=self.case
        case.plan_payload['bugfixes'][0]['trace'][0]['version']='before'
        case.saved=case.service.plan('adequacy',case.plan_payload,expected_revision=case.saved['revision'])
        case.receipt(schema=self.schema)
        self.assertEqual(case.gate()['status'],'BLOCKED')
        files=[{'path':'tests/test_claim.py','content':case.test_source,'sha256':hashlib.sha256(case.test_source.encode()).hexdigest()},
            {'path':'_ase_before/tests/test_claim.py','content':'def test_claim_once(): pass','sha256':hashlib.sha256(b'def test_claim_once(): pass').hexdigest()}]
        case.receipt(schema=self.schema,files=files)
        self.assertEqual(case.gate()['status'],'PASS')


class VerifierSchemaTests(unittest.TestCase):
    def test_opt_in_schema_prevents_scenario_or_stage_ids_and_empty_stage_claims(self):
        from ase.workflow import AC_SCHEMA
        from ase.review.service import _scoped_ac_schema
        from ase.protocol import validate_payload
        criterion={'id':'QUEUE','status':'UNVERIFIED','expected':'Results survive a restart',
            'method':'Schema-only protocol test','evidence':[],'limitations':[],'missing_coverage':['Not executed']}
        schema=_scoped_ac_schema(AC_SCHEMA,['QUEUE'],enabled=True)
        self.assertEqual(validate_payload({'criteria':[criterion]},schema)['status'],'VALID')
        for wrong in ('consumer-chain','repair'):
            self.assertEqual(validate_payload({'criteria':[{**criterion,'id':wrong}]},schema)['status'],'INVALID')
        self.assertEqual(validate_payload({'criteria':[]},schema)['status'],'INVALID')
        empty=_scoped_ac_schema(AC_SCHEMA,[],enabled=True)
        self.assertEqual(validate_payload({'criteria':[]},empty)['status'],'VALID')
        self.assertEqual(validate_payload({'criteria':[criterion]},empty)['status'],'INVALID')
        self.assertNotIn('enum',AC_SCHEMA['properties']['criteria']['items']['properties']['id'])

    def test_legacy_verification_preserves_existing_wire_schema(self):
        from ase.workflow import WorkflowController,AC_SCHEMA,REVIEW_SCHEMA
        from ase.review.service import VerificationService
        from ase.discovery import discover_capabilities,record_discovery
        from ase.protocol import validate_payload
        with tempfile.TemporaryDirectory() as tmp:
            repo=Path(tmp)/'repo';repo.mkdir();(repo/'queue.py').write_text('value=1\n')
            subprocess.run(['git','init','-q',str(repo)],check=True,capture_output=True)
            with closing(Store(repo,Path(tmp)/'state')) as store:
                tasks=TaskService(store);contract=software_contract()
                contract['stages']=[{'id':'edit','acceptance_ids':[]},
                    {'id':'implement','acceptance_ids':['QUEUE'],'depends_on':['edit']}]
                task=tasks.create(contract)
                task=tasks.propose(task['task_id'],{'summary':'Legacy unconfigured task; inspect schema only','stage_ids':['edit','implement']},expected_revision=task['revision'])
                tasks.approve(task['task_id'],proposal_hash=task['proposal_hash'],user_message_ref='test:owner',approved_stage_ids=['edit','implement'])
                record_discovery(store,task['task_id'],discover_capabilities(store.project,scope={'resources':['queue.py']},backend_config={}),expected_revision=0)
                controller=WorkflowController(store);requests=[]
                class Captured(Exception):pass
                class CaptureRunner:
                    def run(_,adapter,request,**kwargs):requests.append(request);raise Captured()
                criterion={'id':'QUEUE','status':'UNVERIFIED','expected':'Results survive a restart',
                    'method':'Schema-only protocol test','evidence':[],'limitations':[],'missing_coverage':['Not executed']}
                for stage in ('implement','edit'):
                    with patch.object(controller,'_runner',return_value=CaptureRunner()),self.assertRaises(Captured):
                        VerificationService(controller,AC_SCHEMA,REVIEW_SCHEMA).verify(task['task_id'],stage)
                    # Sessions run concurrently, so select the AC request by role, not by position.
                    schema=[r for r in requests if r['role']=='ac'][-1]['schema']
                    self.assertEqual(schema,AC_SCHEMA)
                    self.assertEqual(validate_payload({'criteria':[]},schema)['status'],'VALID')
                    self.assertEqual(validate_payload({'criteria':[criterion]},schema)['status'],'VALID')
                self.assertNotIn('enum',AC_SCHEMA['properties']['criteria']['items']['properties']['id'])


if __name__=='__main__':unittest.main()
