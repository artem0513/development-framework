"""Canonical action history retains fresh, recipe-bound gate evidence."""
import copy
from pathlib import Path
import sys
import tempfile
import unittest

from ase.gates import GateService
from ase.state import hash_value, read_record, transaction, write_record
from ase.store import Store
from ase.tasks import TaskService
from ase.workflow import WorkflowController


class OperationAliasGateTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory(prefix='ase-operation-gate-');self.addCleanup(temp.cleanup)
        base=Path(temp.name);self.repo=base/'repo';self.repo.mkdir()
        self.store=Store(self.repo,base/'state');self.addCleanup(self.store.close)
        self.tasks=TaskService(self.store);self.controller=WorkflowController(self.store)
        observer=[sys.executable,'-c',"from pathlib import Path;import json;p=Path('counter');print(json.dumps({'exists':p.exists(),'resource_id':'counter'}))"]
        action={'id':'create','argv':[sys.executable,'-c',"from pathlib import Path;p=Path('counter');p.write_text(str(int(p.read_text())+1 if p.exists() else 1))"],'observation_argv':observer}
        value={'id':'aliases','schema_version':2,'profile':'infrastructure','risk':'LOW','request':'Create one local controlled counter','scope':{'resources':['counter'],'effects':['command']},
            'acceptance':[{'id':'COUNT','expected':'The counter exists once','evidence_ids':['observe']}],
            'evidence':[{'id':'observe','kind':'probe','argv':observer,'predicate':{'path':'exists','op':'eq','value':True}}],
            'stages':[{'id':'provision','acceptance_ids':['COUNT'],'actions':[action]}],'unknowns':[],'assumptions':[],'sources':[]}
        self.task=self.authorize(self.tasks.create(value))

    def authorize(self, task):
        task=self.tasks.propose(task['task_id'],{'summary':'Create the approved counter once','stage_ids':[task['contract']['stages'][0]['id']]},expected_revision=task['revision'])
        return self.tasks.approve(task['task_id'],proposal_hash=task['proposal_hash'],
            approved_stage_ids=task['proposal']['stage_ids'],user_message_ref=f"user:one-counter:{task['revision']}")

    def execute(self):
        self.controller.execute_actions('aliases',self.task['contract']['stages'][0])

    def revise(self, *, changed_observer=False):
        value=copy.deepcopy(self.task['contract']);value['stages'][0]['id']='renamed'
        if changed_observer:
            value['stages'][0]['actions'][0]['observation_argv'][-1]+='; # revised read-only observation'
        self.task=self.authorize(self.tasks.revise('aliases',value,expected_revision=self.task['revision']))

    def action_reasons(self):
        return [reason for reason in GateService(self.store).gate('aliases')['reasons'] if reason.startswith('Action ')]

    def test_renamed_stage_reuses_application_and_fresh_gate_evidence(self):
        self.execute();self.revise();self.execute()
        self.assertEqual((self.repo/'counter').read_text(),'1')
        self.assertEqual(self.action_reasons(),[])

    def test_changed_observer_needs_fresh_observation_on_canonical_operation(self):
        self.execute();self.revise(changed_observer=True)
        self.assertTrue(any('missing or stale' in reason for reason in self.action_reasons()))
        self.execute()
        self.assertEqual((self.repo/'counter').read_text(),'1')
        self.assertEqual(self.action_reasons(),[])

    def test_unauthenticated_action_still_requires_intact_actual_observation_log(self):
        self.execute()
        key=hash_value({'task':'aliases','stage':'provision','action':'create'})
        operation=read_record(self.store.db,'operation',key)['payload']
        Path(operation['observation']['proof_ref'].rsplit('#sha256=',1)[0]).write_text('altered proof')
        self.assertIn('Action observation proof is missing or invalid: create',self.action_reasons())

    def test_stamped_observation_from_other_workspace_cannot_use_legacy_fallback(self):
        self.execute()
        key=hash_value({'task':'aliases','stage':'provision','action':'create'})
        record=read_record(self.store.db,'operation',key)
        value=copy.deepcopy(record['payload']);value['observation']['observing_workspace_id']='other-workspace'
        write_record(self.store.db,'operation',key,value,expected_revision=record['revision'])
        self.assertIn('Action evidence is missing or stale: create',self.action_reasons())

    def test_legacy_unstamped_completed_action_retains_valid_proof_after_scope_upgrade(self):
        self.execute()
        key=hash_value({'task':'aliases','stage':'provision','action':'create'})
        record=read_record(self.store.db,'operation',key);value=copy.deepcopy(record['payload'])
        value.pop('effect_scope',None);value['intent'].pop('effect_scope',None)
        value['intent_hash']=hash_value(value['intent'])
        for field in ['action_hash','observing_task_id','observing_stage_id','observing_workspace_id']:
            value['observation'].pop(field,None)
        with transaction(self.store.db) as db:
            write_record(db,'operation',key,value,expected_revision=record['revision'])
            task=self.tasks.snapshot('aliases');payload={k:v for k,v in task.items() if k not in {'revision','legacy_v1'}}
            payload['state']='COMPLETED';self.tasks._save(payload,task['revision'])
        self.assertEqual(self.action_reasons(),[])
        self.controller.check('aliases')
        self.assertEqual(self.action_reasons(),[])
        self.assertEqual((self.repo/'counter').read_text(),'1')
        with self.assertRaisesRegex(ValueError,'terminal'):
            self.execute()
        self.assertEqual((self.repo/'counter').read_text(),'1')


if __name__=='__main__':unittest.main()
