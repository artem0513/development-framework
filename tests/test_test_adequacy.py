"""Real SQLite/check/Runner protocol tests; no fixture grants semantic approval."""
import copy
import hashlib
import importlib
import json
from pathlib import Path
import shutil
import sys
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from ase.adapters import get_adapter
from ase.core import execute
from ase.protocol import canonical
from ase.runner import Runner, verify_receipt
from ase.state import encode, hash_value, read_record, transaction, write_record
from ase.store import Store
from ase.tasks import TaskService
from test_runner import FIXTURE


def api(case):
    try:
        return importlib.import_module('ase.testing.adequacy')
    except ModuleNotFoundError:
        case.fail('Independent test adequacy service is not implemented')


class AdequacyPolicyTests(unittest.TestCase):
    def test_legacy_contract_is_not_rewritten_or_silently_enabled(self):
        module=api(self)
        contract={'profile':'software','stages':[{'id':'code'}]}
        original=copy.deepcopy(contract)
        self.assertFalse(module.required(contract))
        self.assertEqual(contract,original)

    def test_policy_applies_to_software_stages_only(self):
        module=api(self)
        contract={'profile':'mixed','test_adequacy':{'version':1,'required':True},
            'stages':[{'id':'code','profile':'software'},{'id':'report','profile':'document'}]}
        self.assertTrue(module.required(contract))
        self.assertTrue(module.required(contract,'code'))
        self.assertFalse(module.required(contract,'report'))
        contract['test_adequacy']['required']=False
        self.assertFalse(module.required(contract,'code'))

    def test_malformed_policy_and_unknown_stage_fail_closed(self):
        module=api(self)
        for policy in (None,{'version':True,'required':True},{'version':1,'required':'yes'},
                       {'version':1,'required':True,'skip':True}):
            with self.subTest(policy=policy),self.assertRaises(ValueError):
                module.required({'profile':'software','test_adequacy':policy,'stages':[{'id':'code'}]})
        with self.assertRaises(ValueError):
            module.required({'profile':'software','test_adequacy':{'version':1,'required':True},'stages':[{'id':'code'}]},'unknown')


class AdequacyFixture(unittest.TestCase):
    def setUp(self):
        self.api=api(self)
        temporary=tempfile.TemporaryDirectory(prefix='ase-adequacy-')
        self.addCleanup(temporary.cleanup)
        self.base=Path(temporary.name).resolve()
        self.repo=self.base/'repo'; self.repo.mkdir()
        (self.repo/'tests').mkdir()
        self.test_source="""import concurrent.futures, queue, threading, unittest
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
"""
        (self.repo/'tests/test_claim.py').write_text(self.test_source)
        self.store=Store(self.repo,self.base/'state'); self.addCleanup(self.store.close)
        self.tasks=TaskService(self.store)
        scope={'resources':['tests'],'effects':['file_write','command']}
        self.spec={'id':'suite','kind':'test','parser':'unittest','required':True,
            'argv':[sys.executable,'-B','-m','unittest','discover','-s','tests','-v'],'timeout':10}
        contract={'id':'adequacy','schema_version':2,'profile':'software','request':'One claim survives simultaneous consumers.',
            'scope':scope,'acceptance':[{'id':'AC1','expected':'Concurrent claim is exclusive','evidence_ids':['suite']}],
            'evidence':[self.spec],'stages':[{'id':'code','acceptance_ids':['AC1']}], 'risk':'LOW'}
        task=self.tasks.create(contract)
        # TaskService policy normalization is a separate root-owned integration.
        self._set_policy({'version':1,'required':True})
        task=self.tasks.snapshot('adequacy')
        task=self.tasks.propose('adequacy',{'summary':'Prove concurrent consumer behavior','stage_ids':['code']},expected_revision=task['revision'])
        self.tasks.approve('adequacy',proposal_hash=task['proposal_hash'],user_message_ref='test:disposable-approval',approved_stage_ids=['code'])
        self.service=self.api.TestAdequacy(self.store)
        self.plan_payload={'scenarios':[{'id':'concurrent','acceptance_ids':['AC1'],'risk':'Two consumers claim one event',
            'boundary':'Independent consumers of durable state','expected':'One winner and one loser',
            'evidence_ids':['suite'],'required':True}], 'not_applicable':[]}

    def _set_policy(self,policy):
        with transaction(self.store.db):
            row=read_record(self.store.db,'task','adequacy'); value=row['payload']
            if policy is None:value['contract'].pop('test_adequacy',None)
            else:value['contract']['test_adequacy']=policy
            value['contract_hash']=hash_value(value['contract'])
            write_record(self.store.db,'task','adequacy',value,expected_revision=row['revision'])
            self.store.db.execute('UPDATE tasks SET contract=? WHERE id=?',(encode(value['contract']),'adequacy'))

    def planned(self):
        return self.service.plan('adequacy',copy.deepcopy(self.plan_payload),expected_revision=0)


class AdequacyStateTests(AdequacyFixture):
    def test_cli_saves_and_reads_the_actual_versioned_scenario_plan(self):
        file = self.base / 'scenario-plan.json'
        file.write_text(json.dumps(self.plan_payload), encoding='utf-8')
        current = self.service.snapshot('adequacy')
        argv = [sys.executable, '-B', '-m', 'ase', '--repo', str(self.repo), '--home', str(self.store.state_home), 'adequacy']
        result = subprocess.run([*argv, 'plan', 'adequacy', '--file', str(file), '--expected-revision', str(current['revision'])], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        saved = json.loads(result.stdout)
        import re, shlex
        from ase.__main__ import parser
        quoted = re.search(r'`(ase task [^`]+)`', saved.pop('next_action')).group(1)
        args = parser().parse_args(shlex.split(quoted)[1:])
        self.assertEqual((args.action, args.task_id), ('run', 'adequacy'))
        result = subprocess.run([*argv, 'status', 'adequacy'], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout), saved)
        self.assertEqual(saved['payload']['scenarios'][0]['id'], 'concurrent')

    def test_plan_is_durable_cas_and_does_not_bind_source(self):
        saved=self.planned()
        self.assertEqual(saved['revision'],1)
        self.assertEqual(saved['payload']['scenarios'][0]['id'],'concurrent')
        self.assertEqual(len(saved['payload']['test_plan_sha256']),64)
        (self.repo/'new-source.py').write_text('changed after design\n')
        second=Store(self.repo,self.base/'state')
        try:self.assertEqual(self.api.TestAdequacy(second).snapshot('adequacy')['payload'],saved['payload'])
        finally:second.close()
        with self.assertRaisesRegex(ValueError,'Stale|revision'):
            self.service.plan('adequacy',self.plan_payload,expected_revision=0)

    def test_plan_must_cover_mandatory_acceptance_with_required_scenario(self):
        for change in ('none','optional'):
            payload=copy.deepcopy(self.plan_payload)
            if change=='none':payload['scenarios']=[]
            else:payload['scenarios'][0]['required']=False
            with self.subTest(change=change),self.assertRaises(ValueError):
                self.service.plan('adequacy',payload,expected_revision=0)

    def test_unknown_duplicate_and_cross_stage_plan_references_are_rejected(self):
        for field,value in [('acceptance_ids',['missing']),('evidence_ids',['missing']),('acceptance_ids',['AC1','AC1']),('required','true')]:
            payload=copy.deepcopy(self.plan_payload); payload['scenarios'][0][field]=value
            with self.subTest(field=field,value=value),self.assertRaises(ValueError):
                self.service.plan('adequacy',payload,expected_revision=0)
        payload=copy.deepcopy(self.plan_payload); payload['scenarios']*=2
        with self.assertRaises(ValueError):self.service.plan('adequacy',payload,expected_revision=0)

    def test_shared_acceptance_needs_compatible_proof_in_every_software_stage(self):
        task=self.tasks.snapshot('adequacy');contract=task['contract']
        special={**self.spec,'id':'special'}
        contract['profile']='mixed';contract['evidence'].append(special)
        contract['stages']=[{'id':'code','profile':'software','acceptance_ids':['AC1'],'evidence_ids':['suite','special'],'depends_on':[]},
            {'id':'other','profile':'software','acceptance_ids':['AC1'],'evidence_ids':['suite'],'depends_on':[]}]
        task['contract_hash']=hash_value(contract)
        with transaction(self.store.db):
            row=read_record(self.store.db,'task','adequacy')
            value={key:item for key,item in task.items() if key not in {'revision','legacy_v1'}}
            write_record(self.store.db,'task','adequacy',value,expected_revision=row['revision'])
            row=read_record(self.store.db,'execution_plan','adequacy')
            checks=[self.spec,special]
            write_record(self.store.db,'execution_plan','adequacy',{'checks':checks,'plan_hash':hash_value(checks)},expected_revision=row['revision'])
        payload=copy.deepcopy(self.plan_payload);payload['scenarios'][0]['evidence_ids']=['special']
        with self.assertRaises(ValueError):self.service.plan('adequacy',payload,expected_revision=0)

    def test_a_plan_changed_after_review_has_a_new_identity(self):
        first=self.planned()
        payload=copy.deepcopy(self.plan_payload); payload['scenarios'][0]['boundary']='Two SQLite connections'
        second=self.service.plan('adequacy',payload,expected_revision=1)
        self.assertNotEqual(first['payload']['test_plan_sha256'],second['payload']['test_plan_sha256'])

    def test_snapshot_does_not_accept_a_tampered_plan_hash(self):
        self.planned()
        with transaction(self.store.db):
            row=read_record(self.store.db,'test_adequacy_plan','adequacy'); value=row['payload']
            value['scenarios'][0]['expected']='Two winners are acceptable'
            write_record(self.store.db,'test_adequacy_plan','adequacy',value,expected_revision=row['revision'])
        with self.assertRaises(ValueError):self.service.snapshot('adequacy')


class AdequacyGateTests(AdequacyFixture):
    @unittest.skipUnless(shutil.which('git'), 'git required')
    def test_verification_request_lists_actual_test_sources_for_mapped_checks(self):
        from ase.workflow import WorkflowController

        subprocess.run(['git', 'init', '-q', str(self.repo)], check=True)
        controller = WorkflowController(self.store)
        controller.check('adequacy', 'code')
        with patch.object(controller, '_runner'), patch('ase.review.service._run_sessions', return_value=({}, None)) as sessions:
            controller.verify('adequacy', 'code')
        _, _, planned, _ = sessions.call_args.args
        request = next(request for label, _, _, request in planned if label == 'adequacy')
        handoff = request['handoff']
        guide = handoff['test_source_candidates']
        self.assertEqual(guide['repository_test_files'], ['tests/test_claim.py'])
        self.assertEqual(guide['checks'], {'suite': {'declared_scripts': [], 'configured_test_files': []}})
        self.assertIn('test_source_candidates', handoff['instruction'])
        for label in ('ac', 'review'):
            peer = next(request for selected, _, _, request in planned if selected == label)
            self.assertIn('Do not wait for another verification receipt', peer['handoff']['instruction'])

    def test_candidate_inventory_only_exposes_supplied_current_sources(self):
        from ase.evidence.check_inputs import PACKET_PREFIX
        from ase.evidence.compact import compact_packet

        private = '.devframework/local/checks/claim.py'
        paths = [PACKET_PREFIX + private, 'tests/test_claim.py', 'tests/claim_fixture.py', 'cases/order_flow.py',
                 '_ase_context/logs/suite.txt', '_ase_before/tests/test_claim.py',
                 '_ase_diff/tests/test_other.py']
        files = [{'path': path, 'content': self.test_source,
                  'sha256': hashlib.sha256(self.test_source.encode()).hexdigest()} for path in paths]
        packet = compact_packet({'files': files, 'coverage': {'included': paths, 'missing': [], 'scope': 'fixture'}})
        contract = {'check_inputs': [{'path': private, 'evidence_ids': ['probe']}]}
        specs = [{'id': 'probe', 'kind': 'probe'}, {'id': 'suite', 'kind': 'test',
                  'progress_test_sources': ['tests/claim_fixture.py', 'tests/test_other.py', 'cases/']},
                 {'id': 'later', 'kind': 'test'}]
        result = self.api.test_source_candidates(packet, contract, specs, {'check_ids': ['probe', 'suite']})
        self.assertEqual(result['repository_test_files'], ['tests/claim_fixture.py', 'tests/test_claim.py'])
        self.assertEqual(result['checks'], {
            'probe': {'declared_scripts': [PACKET_PREFIX + private], 'configured_test_files': []},
            'suite': {'declared_scripts': [], 'configured_test_files': ['cases/order_flow.py', 'tests/claim_fixture.py']},
        })

    def test_repeated_configured_paths_keep_the_source_guide_bounded(self):
        from ase.evidence.check_inputs import PACKET_PREFIX
        from ase.evidence.compact import compact_packet

        private = '.devframework/local/checks/claim.py'
        paths = [f'tests/{"boundary_" * 10}/test_flow_{index}.py' for index in range(100)]
        paths.append(PACKET_PREFIX + private)
        digest = hashlib.sha256(self.test_source.encode()).hexdigest()
        files = [{'path': path, 'content': self.test_source, 'sha256': digest} for path in paths]
        packet = compact_packet({'files': files, 'coverage': {'included': paths, 'missing': [], 'scope': 'fixture'}})
        specs = [{'id': f'suite_{index}', 'kind': 'test', 'progress_test_sources': ['tests']}
                 for index in range(100)]
        specs.append({'id': 'probe', 'kind': 'probe'})
        contract = {'check_inputs': [{'path': private, 'evidence_ids': ['probe']}]}
        guide = self.api.test_source_candidates(packet, contract, specs, {'check_ids': [spec['id'] for spec in specs]})

        self.assertLessEqual(len(canonical(guide)), self.api.MAX_TEST_SOURCE_GUIDE_BYTES)
        self.assertEqual(guide['checks']['probe']['declared_scripts'], [PACKET_PREFIX + private])

    def test_real_gate_requires_configured_adequacy_even_with_other_missing_proof(self):
        from ase.gates import GateService
        subprocess.run(['git', 'init', '-q', str(self.repo)], check=True, capture_output=True)
        verdict = GateService(self.store).gate('adequacy', stage_id='code')
        self.assertEqual(verdict.get('test_adequacy', {}).get('status'), 'BLOCKED', verdict)
        self.assertTrue(any('adequacy receipt missing' in reason for reason in verdict['blocked_reasons']), verdict)

    def test_gate_detects_source_change_during_adequacy_observation(self):
        from ase.gates import GateService
        subprocess.run(['git', 'init', '-q', str(self.repo)], check=True, capture_output=True)
        def changed(*args, **kwargs):
            file = self.repo / 'tests/test_claim.py'
            file.write_text(file.read_text() + '\n# concurrent source change\n')
            return {'configured': True, 'status': 'PASS', 'blocked_reasons': [], 'rejected_reasons': []}
        with patch.object(self.api.TestAdequacy, 'gate', side_effect=changed):
            verdict = GateService(self.store).gate('adequacy', stage_id='code')
        self.assertTrue(any('changed during gate' in reason for reason in verdict['blocked_reasons']), verdict)

    def test_gate_detects_scenario_plan_change_during_adequacy_observation(self):
        from ase.gates import GateService
        subprocess.run(['git', 'init', '-q', str(self.repo)], check=True, capture_output=True)
        def changed(*args, **kwargs):
            payload = copy.deepcopy(self.plan_payload)
            payload['scenarios'][0]['boundary'] += '; changed after assessment'
            self.service.plan('adequacy', payload, expected_revision=self.saved['revision'])
            return {'configured': True, 'status': 'PASS', 'blocked_reasons': [], 'rejected_reasons': []}
        with patch.object(self.api.TestAdequacy, 'gate', side_effect=changed):
            verdict = GateService(self.store).gate('adequacy', stage_id='code')
        self.assertTrue(any('changed during gate' in reason for reason in verdict['blocked_reasons']), verdict)

    def test_gate_detects_selected_adequacy_receipt_change_during_observation(self):
        from ase.gates import GateService
        subprocess.run(['git', 'init', '-q', str(self.repo)], check=True, capture_output=True)
        def changed(*args, **kwargs):
            with transaction(self.store.db):
                write_record(self.store.db, 'verification', 'adequacy:adequacy:code',
                    {'execution_id': 'different-captured-result'}, expected_revision=0)
            return {'configured': True, 'status': 'PASS', 'blocked_reasons': [], 'rejected_reasons': []}
        with patch.object(self.api.TestAdequacy, 'gate', side_effect=changed):
            verdict = GateService(self.store).gate('adequacy', stage_id='code')
        self.assertTrue(any('changed during gate' in reason for reason in verdict['blocked_reasons']), verdict)

    def test_prerequisite_inputs_change_when_its_scenario_plan_changes(self):
        from ase.workflow import WorkflowController
        from ase.gates import source_snapshot
        subprocess.run(['git', 'init', '-q', str(self.repo)], check=True, capture_output=True)
        task = self.tasks.snapshot('adequacy')
        contract=copy.deepcopy(task['contract'])
        contract['stages'].append({'id':'dependent','acceptance_ids':[],'depends_on':['code']})
        task=self.tasks.revise('adequacy',contract,expected_revision=task['revision'])
        task=self.tasks.propose('adequacy',{'summary':'Preserve proof before dependent mutation',
            'stage_ids':['code','dependent']},expected_revision=task['revision'])
        task=self.tasks.approve('adequacy',proposal_hash=task['proposal_hash'],
            user_message_ref='test:original-scope',approved_stage_ids=['code','dependent'])
        saved=self.service.plan('adequacy',self.plan_payload,expected_revision=self.saved['revision'])
        controller = WorkflowController(self.store)
        controller._save('stage', 'adequacy:code', {'state': 'COMPLETED',
            'contract_hash': task['contract_hash'], 'proposal_hash': task['proposal_hash']})
        before = controller._prerequisite_current_inputs(task, 'code')
        binding={'task_id':'adequacy','stage_id':'dependent','workspace_id':self.store.workspace_id,
            'contract_hash':task['contract_hash'],'proposal_hash':task['proposal_hash'],
            'plan_hash':self.tasks.execution_plan('adequacy')['plan_hash'],
            'fingerprint':source_snapshot(self.store,task['contract'],task_id='adequacy')['fingerprint'],
            'prerequisites':[before]}
        controller._require_prerequisite_binding('adequacy','dependent',binding)
        payload = copy.deepcopy(self.plan_payload)
        payload['scenarios'][0]['boundary'] += '; requires new proof'
        self.service.plan('adequacy', payload, expected_revision=saved['revision'])
        after = controller._prerequisite_current_inputs(task, 'code')
        self.assertNotEqual(before.get('test_adequacy_sha256'), after.get('test_adequacy_sha256'))
        with self.assertRaisesRegex(ValueError,'Prerequisite evidence changed'):
            controller._require_prerequisite_binding('adequacy','dependent',binding)

    def test_confirmed_in_scope_inadequacy_is_repairable_rejection_without_false_blocker(self):
        payload = copy.deepcopy(self.payload)
        payload['scenarios'][0].update(status='FAIL', reason='Confirmed regression in the tested claim behavior')
        self.receipt(payload)
        verdict = self.gate()
        self.assertEqual(verdict['status'], 'FAIL')
        self.assertTrue(verdict['rejected_reasons'])
        self.assertEqual(verdict['blocked_reasons'], [], verdict)

    def test_later_independent_roles_can_read_the_captured_adequacy_assessment(self):
        self.receipt()
        captured = self.service.assessment('adequacy', stage_id='code')
        self.assertEqual(captured['state'], 'CAPTURED')
        self.assertEqual(captured['payload']['scenarios'][0]['id'], 'concurrent')
        self.assertEqual(captured['request_identity']['purpose'], 'test_adequacy')
        self.assertEqual(captured['evidence_kind'], 'protocol_only')
        self.assertNotIn('approved', captured)

    def setUp(self):
        super().setUp(); self.saved=self.planned()
        task=self.tasks.snapshot('adequacy'); plan=self.tasks.execution_plan('adequacy')
        log=self.store.root/'tasks/adequacy/checks/actual/suite.log'
        result=execute(self.spec['argv'],self.repo,log,timeout=10)
        self.assertEqual(result['exit_code'],0)
        result.update(id='suite',kind='test',parser='unittest',configured_argv=self.spec['argv'])
        bundle={'kind':'checks','task_id':'adequacy','fingerprint':hash_value(self.test_source),
            'contract_hash':task['contract_hash'],'plan_hash':plan['plan_hash'],'results':[result]}
        with transaction(self.store.db):write_record(self.store.db,'checks','adequacy',bundle,expected_revision=0)
        self.current={'contract_hash':task['contract_hash'],'proposal_hash':task['proposal_hash'],
            'plan_hash':plan['plan_hash'],'fingerprint':bundle['fingerprint'],'check_hash':hash_value(bundle),
            'workspace_id':self.store.workspace_id,'verification_context_sha256s':('stage-context','whole-context'),
            'checks':plan['checks'],'implementation_execution_id':'implementation-execution'}
        self.proof={'check_id':'suite','test_source':'tests/test_claim.py','test_id':'ClaimTests.test_claim_once',
            'assertion':'Two simultaneously released consumers produce one winner and one loser'}
        self.payload={'scenarios':[{'id':'concurrent','status':'PASS','evidence':[self.proof],
            'missing_coverage':[],'reason':'The two actual workers overlap before independent claims'}],
            'additional_scenarios':[],'missing_coverage':[],'summary':'Bounded concurrent-consumer proof'}
        self.trusted=self.base/'provider';self.trusted.mkdir()
        self.fixture=self.trusted/'provider.py'
        self.requests={}

    def receipt(self,payload=None,*,stage='code',purpose='test_adequacy',schema=None,bindings=None,plan_hash=None,project_id=None,files=None):
        payload=copy.deepcopy(self.payload if payload is None else payload)
        body="payload = json.loads("+repr(json.dumps(payload))+")"
        self.fixture.write_text(FIXTURE.replace("payload = {'verdict':'PASS'}",body))
        selected={key:self.current[key] for key in ('fingerprint','workspace_id')}
        selected.update(contract_sha256=self.current['contract_hash'],proposal_sha256=self.current['proposal_hash'],
            plan_sha256=self.current['plan_hash'],check_sha256=self.current['check_hash'],verification_context_sha256='stage-context' if stage else 'whole-context')
        selected.update(bindings or {})
        request={'role':'review','project':{'root':str(self.repo),'project_id':project_id or self.store.project_id},
            'bindings':selected,'schema':schema or self.api.ADEQUACY_SCHEMA,
            'handoff':{'purpose':purpose,'stage_id':stage,'test_plan_sha256':plan_hash or self.saved['payload']['test_plan_sha256']},
            'source_packet':{'requirements':['Only one concurrent claim'],
                'files':files if files is not None else [{'path':'tests/test_claim.py','content':self.test_source,'sha256':hashlib.sha256(self.test_source.encode()).hexdigest()}],
                'coverage':{'scope':'Disposable concrete test source','included':[row['path'] for row in files] if files is not None else ['tests/test_claim.py'],'missing':[]}}}
        runner=Runner({'executable':sys.executable,'executable_args':[str(self.fixture),'success','codex'],
            'trusted_cwd':str(self.trusted),'protocol_only':True},self.store.root/'tasks/adequacy/sessions')
        receipt=runner.run(get_adapter('codex'),request,timeout=10)['receipt']
        self.assertEqual(receipt['status'],'COMPLETE')
        self.requests[receipt['execution_id']]=copy.deepcopy(request)
        key='adequacy:adequacy'+(':'+stage if stage else '')
        with transaction(self.store.db):
            old=read_record(self.store.db,'verification',key)
            write_record(self.store.db,'verification',key,receipt,expected_revision=old['revision'])
        return receipt

    def gate(self,*,semantic_seam=True,stage='code',identity_changes=None):
        def actual_receipt_validation(receipt,**kwargs):
            verified=verify_receipt(receipt,**kwargs)
            captured=self.requests[receipt['execution_id']]
            identity={'role':captured['role'],'purpose':captured['handoff']['purpose'],
                'stage_id':captured['handoff']['stage_id'],'schema_sha256':hash_value(captured['schema']),
                'project_id':captured['project']['project_id'],'test_plan_sha256':captured['handoff']['test_plan_sha256']}
            identity.update(identity_changes or {})
            for field,value in (identity_changes or {}).items():
                if value=='__omit__':identity.pop(field,None)
            # This seam tests deterministic policy only; never semantic approval.
            return {**verified,'evidence_kind':'independent_cli' if semantic_seam else verified['evidence_kind'],
                'request_identity':identity}
        with patch.object(self.api,'verify_receipt',side_effect=actual_receipt_validation):
            return self.service.gate('adequacy',current=self.current,stage_id=stage)

    def test_sufficient_executed_scenario_passes_only_at_protocol_policy_seam(self):
        self.receipt()
        self.assertEqual(self.gate()['status'],'PASS')
        self.assertEqual(self.gate(semantic_seam=False)['status'],'BLOCKED')

    def test_missing_receipt_and_high_count_without_scenarios_do_not_pass(self):
        self.assertEqual(self.gate()['status'],'BLOCKED')
        self.receipt({**self.payload,'scenarios':[]})
        self.assertEqual(self.gate()['status'],'BLOCKED')

    def test_missing_required_risk_or_proof_blocks_even_when_status_passes(self):
        for edit in ('proof','coverage','unknown-case','duplicate-case'):
            payload=copy.deepcopy(self.payload)
            if edit=='proof':payload['scenarios'][0]['evidence']=[]
            elif edit=='coverage':payload['scenarios'][0]['missing_coverage']=['No simultaneous I/O observed']
            elif edit=='unknown-case':payload['scenarios'][0]['id']='invented'
            else:payload['scenarios']*=2
            with self.subTest(edit=edit):
                self.receipt(payload);self.assertEqual(self.gate()['status'],'BLOCKED')

    def test_confirmed_inadequacy_is_fail_and_additional_unproved_risk_blocks(self):
        payload=copy.deepcopy(self.payload);payload['scenarios'][0]['status']='FAIL'
        self.receipt(payload);self.assertEqual(self.gate()['status'],'FAIL')
        payload=copy.deepcopy(self.payload)
        payload['additional_scenarios']=[{'risk':'Tied timestamps','boundary':'Actual event selection',
            'expected':'No equal-time event is lost','status':'UNVERIFIED','evidence':[],
            'missing_coverage':['No frozen tied-time proof'],'reason':'Sequential unique dates miss this risk'}]
        self.receipt(payload);self.assertEqual(self.gate()['status'],'BLOCKED')

    def test_wrong_consumer_test_locator_or_unexecuted_check_cannot_pass(self):
        for field,value in [('check_id','not-run'),('test_source','tests/absent.py'),('test_id','ClaimTests.test_not_present'),
                            ('assertion',' '),('test_source','../secrets.py')]:
            payload=copy.deepcopy(self.payload);payload['scenarios'][0]['evidence'][0][field]=value
            with self.subTest(field=field,value=value):
                # Whitespace passes JSON minLength, and must fail semantic validation.
                self.receipt(payload);self.assertEqual(self.gate()['status'],'BLOCKED')

    def test_changed_checks_and_each_binding_stale_the_assessment(self):
        self.receipt()
        for key in ('fingerprint','contract_hash','proposal_hash','plan_hash','check_hash','workspace_id'):
            old=self.current[key];self.current[key]='changed'
            with self.subTest(key=key):self.assertEqual(self.gate()['status'],'BLOCKED')
            self.current[key]=old
        self.current['verification_context_sha256s']=('new-context',)
        self.assertEqual(self.gate()['status'],'BLOCKED')

    def test_changed_plan_makes_old_receipt_unusable(self):
        self.receipt()
        payload=copy.deepcopy(self.plan_payload);payload['scenarios'][0]['boundary']='Distinct worker processes'
        self.service.plan('adequacy',payload,expected_revision=1)
        self.assertEqual(self.gate()['status'],'BLOCKED')

    def test_exact_request_purpose_schema_project_and_scope_are_required(self):
        for changes in ({'purpose':'ordinary_review'},{'schema_sha256':'wrong-schema'},
                        {'project_id':'other-project'},{'stage_id':'other-stage'},
                        {'test_plan_sha256':'old-plan'},{'role':'ac'}):
            self.receipt()
            with self.subTest(changes=changes):self.assertEqual(self.gate(identity_changes=changes)['status'],'BLOCKED')

    def test_independent_execution_and_provider_session_cannot_be_reused(self):
        receipt=self.receipt()
        for role in ('ac','review'):
            with self.subTest(role=role):
                with transaction(self.store.db):
                    write_record(self.store.db,'verification','adequacy:'+role,
                        {'execution_id':'another-execution','session_id':receipt['session_id']},expected_revision=0)
                self.assertEqual(self.gate()['status'],'BLOCKED')
        self.current['implementation_execution_id']=receipt['execution_id']
        self.assertEqual(self.gate()['status'],'BLOCKED')

    def test_whole_receipt_covers_stage_but_stage_does_not_cover_whole(self):
        self.receipt()
        self.assertEqual(self.gate(stage=None)['status'],'BLOCKED')
        self.receipt(stage=None)
        self.assertEqual(self.gate(stage=None)['status'],'PASS')
        self.assertEqual(self.gate()['status'],'PASS')

    def test_stale_stage_receipt_does_not_shadow_current_whole_receipt(self):
        self.receipt(bindings={'check_sha256': 'f' * 64})
        self.assertEqual(self.gate()['status'], 'BLOCKED')
        stale_binding = self.service.binding('adequacy', 'code')
        self.receipt(stage=None)
        self.assertNotEqual(self.service.binding('adequacy', 'code'), stale_binding)
        self.assertEqual(self.gate()['status'], 'PASS')

        failure = copy.deepcopy(self.payload)
        failure['scenarios'][0].update(status='FAIL', reason='Independent stage finding')
        self.receipt(failure)
        self.assertEqual(self.gate()['status'], 'FAIL')

    def test_malformed_stage_receipt_blocks_without_crashing_selection(self):
        self.receipt(stage=None)
        key = 'adequacy:adequacy:code'
        for bindings in (None, {}, {'check_sha256': 42}, {'check_sha256': 'invalid'}):
            with self.subTest(bindings=bindings):
                with transaction(self.store.db):
                    previous = read_record(self.store.db, 'verification', key)
                    write_record(self.store.db, 'verification', key, {'bindings': bindings},
                                 expected_revision=previous['revision'])
                self.assertIsInstance(self.service.binding('adequacy', 'code'), str)
                self.assertEqual(self.service.assessment('adequacy', 'code')['state'], 'UNAVAILABLE')
                outcome = self.gate()
                self.assertEqual(outcome['status'], 'BLOCKED')
                self.assertIn('Invalid adequacy receipt bindings', outcome['blocked_reasons'][0])

    def test_missing_stage_identity_cannot_authorize_whole_task(self):
        self.receipt(stage=None)
        self.assertEqual(self.gate(stage=None,identity_changes={'stage_id':'__omit__'})['status'],'BLOCKED')

    def test_modified_captured_request_or_test_log_blocks(self):
        receipt=self.receipt()
        log=Path(read_record(self.store.db,'checks','adequacy')['payload']['results'][0]['log'])
        log.write_text('Ran 999 tests\nOK\n')
        self.assertEqual(self.gate()['status'],'BLOCKED')
        request=Path(receipt['artifacts']['request']['path'])
        request.write_text('{}')
        self.assertEqual(self.gate()['status'],'BLOCKED')

    def test_unconfigured_and_disabled_contracts_are_transparent(self):
        self._set_policy(None)
        result=self.service.gate('adequacy',current=self.current,stage_id='code')
        self.assertEqual((result['configured'],result['status']),(False,'NOT_CONFIGURED'))
        self._set_policy({'version':1,'required':False})
        self.assertEqual(self.service.gate('adequacy',current=self.current,stage_id='code')['status'],'NOT_REQUIRED')


if __name__=='__main__':unittest.main()
