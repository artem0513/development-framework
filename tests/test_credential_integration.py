"""Real vault and controlled subprocess coverage of approved credential integration."""
import base64
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from ase.credentials import CredentialVault
from ase.gates import GateService
from ase.operations import OperationLedger
from ase.state import hash_value, read_record
from ase.store import Store
from ase.tasks import TaskService
from ase.transfer import export_project
from ase.workflow import WorkflowController


@unittest.skipUnless(os.name == 'nt', 'requires actual Windows DPAPI')
class CredentialIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='ase-credential-integration-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / 'work'; self.repo.mkdir()
        self.home = self.root / 'home'
        self.store = Store(self.repo, self.home)
        self.addCleanup(self.store.close)
        self.vault = CredentialVault(self.home)
        self.secret = 'fixture-only-credential-canary-3267'
        self.target = 'https://lab.example'
        self.access = {'credential_ref':'lab', 'target':self.target}
        self.profile = {'id':'lab', 'target':self.target, 'kind':'environment',
                        'secrets':{'token':self.secret}, 'env':{'ASE_FIXTURE_TOKEN_3267':'token'}}
        source = self.root / 'fake-import.json'
        source.write_text(json.dumps({'schema_version':1,'credentials':[self.profile]}), encoding='utf-8')
        self.vault.import_file(source)
        self.tasks = TaskService(self.store)
        self.controller = WorkflowController(self.store)

    def contract(self, task_id='access-check', *, action=False):
        check = {'id':'observe','kind':'command', 'argv':[sys.executable,'-c',
            'import os,base64; s=os.environ["ASE_FIXTURE_TOKEN_3267"]; print(s); print(base64.b64encode(s.encode()).decode())'],
            'access':self.access}
        scope = {'resources':[self.target, 'resource.counter'], 'effects':['command']}
        stage = {'id':'work','scope':scope,'acceptance_ids':['AC1'], 'depends_on':[]}
        if action:
            stage['actions'] = [{'id':'create-counter','access':self.access,
                'argv':[sys.executable,'-c','import os; from pathlib import Path; assert os.environ["ASE_FIXTURE_TOKEN_3267"]; p=Path("resource.counter"); p.write_text(str(int(p.read_text())+1) if p.exists() else "1")'],
                'observation_argv':[sys.executable,'-c','import os,json; from pathlib import Path; assert os.environ["ASE_FIXTURE_TOKEN_3267"]; p=Path("resource.counter"); print(json.dumps({"exists":p.exists(),"resource_id":"counter" if p.exists() else None}))']}]
        return {'id':task_id,'schema_version':2,'profile':'infrastructure','request':'Observe a disposable controlled resource',
                'scope':scope, 'acceptance':[{'id':'AC1','expected':'Controlled observation runs','evidence_ids':['observe']}],
                'evidence':[check], 'stages':[stage], 'unknowns':[]}

    def create(self, contract, approve=True):
        task = self.tasks.create(contract)
        task = self.tasks.propose(task['task_id'], {'summary':'Inspect the disposable resource','stage_ids':['work']}, expected_revision=task['revision'])
        if approve:
            self.tasks.approve(task['task_id'], proposal_hash=task['proposal_hash'], user_message_ref='test:approved', approved_stage_ids=['work'])
        return self.tasks.snapshot(task['task_id'])

    def test_check_requires_current_approval_and_scope_includes_exact_target(self):
        task = self.create(self.contract(), approve=False)
        with self.assertRaisesRegex(ValueError, 'approval'):
            self.controller.check(task['task_id'], 'work')
        invalid = self.contract('wrong-scope')
        invalid['stages'][0]['scope'] = {'resources':['https://lab.example:444','resource.counter'],'effects':['command']}
        invalid['scope'] = copy.deepcopy(invalid['stages'][0]['scope'])
        with self.assertRaises(ValueError):
            self.tasks.create(invalid)

    def test_check_needs_target_in_the_approved_stage_not_another_stage(self):
        contract = self.contract('other-stage')
        contract['stages'].append({'id':'other', 'scope':{'resources':['resource.counter'], 'effects':['command']},
                                   'acceptance_ids':['AC1'], 'depends_on':[]})
        with self.assertRaises(ValueError):
            task = self.tasks.create(contract)
            task = self.tasks.propose(task['task_id'], {'summary':'Approve only the unrelated scope','stage_ids':['other']}, expected_revision=task['revision'])
            self.tasks.approve(task['task_id'], proposal_hash=task['proposal_hash'], user_message_ref='test:approved', approved_stage_ids=['other'])
            self.controller.check(task['task_id'], 'other')

    def test_infrastructure_action_needs_stage_approval(self):
        task = self.create(self.contract('action-unapproved', action=True), approve=False)
        with self.assertRaisesRegex(ValueError, 'approval'):
            self.controller.execute_actions(task['task_id'], task['contract']['stages'][0])
        self.assertFalse((self.repo / 'resource.counter').exists())

    def test_check_injects_child_only_and_saves_safe_revision_receipt(self):
        task = self.create(self.contract())
        parent = os.environ.get('ASE_FIXTURE_TOKEN_3267')
        check = self.controller.check(task['task_id'], 'work')
        result = check['results'][0]
        self.assertEqual(result['exit_code'], 0)
        self.assertEqual(result['access'], {'credential_ref':'lab','target':self.target,'revision':1})
        self.assertEqual(os.environ.get('ASE_FIXTURE_TOKEN_3267'), parent)
        text = Path(result['log']).read_text(encoding='utf-8')
        self.assertNotIn(self.secret, text)
        self.assertNotIn(base64.b64encode(self.secret.encode()).decode(), text)
        self.assertIn('[REDACTED]', text)
        saved = self.tasks.snapshot(task['task_id'])['contract']['evidence'][0]['access']
        self.assertEqual(saved, self.access)

    def test_gate_reports_rotated_check_credentials_as_stale(self):
        task = self.create(self.contract())
        self.controller.check(task['task_id'], 'work')
        before = GateService(self.store).gate(task['task_id'], 'work')
        reason = 'Credential evidence is missing or stale: observe'
        self.assertNotIn(reason, before['reasons'])
        self.vault.update_secrets('lab',self.target,{'token':'rotated-fixture-secret'},1)
        verdict = GateService(self.store).gate(task['task_id'], 'work')
        self.assertNotEqual(verdict['status'], 'APPROVED')
        self.assertIn(reason, verdict['reasons'])

    def test_action_uses_credentials_for_probe_and_effect_and_reconciles_rotation(self):
        task = self.create(self.contract('action-task', action=True))
        stage = task['contract']['stages'][0]
        observation = self.controller.execute_actions(task['task_id'], stage)[0]
        self.assertTrue(observation['exists'])
        self.assertEqual(observation['access'], {'credential_ref':'lab','target':self.target,'revision':1})
        self.assertEqual((self.repo / 'resource.counter').read_text(), '1')
        key = hash_value({'task':task['task_id'],'stage':'work','action':'create-counter'})
        with self.assertRaisesRegex(ValueError, 'replayed'):
            OperationLedger(self.store).started(key)
        self.vault.update_secrets('lab',self.target,{'token':'rotated-fixture-secret'},1)
        result = self.controller.execute_actions(task['task_id'], stage)[0]
        self.assertEqual(result['access']['revision'], 2)
        self.assertEqual((self.repo / 'resource.counter').read_text(), '1')
        self.assertEqual(read_record(self.store.db,'operation',key)['payload']['state'], 'APPLIED')

    def test_rotation_reconciles_a_real_lost_ack_without_replaying_effect(self):
        task=self.create(self.contract('lost-ack-rotation',action=True));stage=task['contract']['stages'][0]
        real_observe=self.controller._observe_action
        calls=[]
        def lost_postcondition(*args,**kwargs):
            calls.append(1)
            if len(calls)==2:
                raise ValueError('Fixture lost postcondition acknowledgement')
            return real_observe(*args,**kwargs)
        with patch.object(self.controller,'_observe_action',side_effect=lost_postcondition):
            with self.assertRaisesRegex(ValueError,'acknowledgement'):
                self.controller.execute_actions(task['task_id'],stage)
        key=hash_value({'task':task['task_id'],'stage':'work','action':'create-counter'})
        self.assertEqual(read_record(self.store.db,'operation',key)['payload']['state'],'UNKNOWN')
        self.assertEqual((self.repo/'resource.counter').read_text(),'1')
        self.vault.update_secrets('lab',self.target,{'token':'rotated-fixture-secret'},1)
        observation=self.controller.execute_actions(task['task_id'],stage)[0]
        self.assertEqual(observation['access']['revision'],2)
        self.assertEqual((self.repo/'resource.counter').read_text(),'1')
        self.assertEqual(read_record(self.store.db,'operation',key)['payload']['state'],'APPLIED')

    def test_rejects_forged_stage_action_before_effect(self):
        task = self.create(self.contract('forged-task', action=True))
        stage = copy.deepcopy(task['contract']['stages'][0])
        stage['actions'][0]['argv'] = [sys.executable, '-c', 'from pathlib import Path; Path("forged").write_text("bad")']
        with self.assertRaises(ValueError):
            self.controller.execute_actions(task['task_id'], stage)
        self.assertFalse((self.repo / 'forged').exists())

    def test_gate_reports_stale_action_observation_after_rotation(self):
        task = self.create(self.contract('action-stale', action=True))
        self.controller.execute_actions(task['task_id'], task['contract']['stages'][0])
        self.vault.update_secrets('lab',self.target,{'token':'rotated-fixture-secret'},1)
        verdict = GateService(self.store).gate(task['task_id'], 'work')
        self.assertNotEqual(verdict['status'], 'APPROVED')
        self.assertTrue(any('credential' in reason.lower() and 'create-counter' in reason for reason in verdict['reasons']), verdict['reasons'])

    def test_check_secrets_absent_from_packet_database_and_export(self):
        task = self.create(self.contract('storage-test'))
        check = self.controller.check(task['task_id'], 'work')
        self.assertEqual(check['results'][0]['exit_code'], 0)
        packet = json.dumps(self.controller._packet(task['task_id']), ensure_ascii=False).encode()
        self.store.db.commit()
        archive = self.root / 'export.zip'
        export_project(self.store.project, archive, include_verbose=True)
        with zipfile.ZipFile(archive) as bundle:
            exported = b''.join(bundle.read(name) for name in bundle.namelist() if not name.endswith('/'))
        blobs = [packet, exported]
        blobs.extend(path.read_bytes() for path in self.store.root.rglob('*') if path.is_file())
        for canary in (self.secret.encode(), base64.b64encode(self.secret.encode())):
            for blob in blobs:
                self.assertNotIn(canary, blob)


if __name__ == '__main__':
    unittest.main()
