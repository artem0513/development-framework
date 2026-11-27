"""Newly discovered blockers hold only mapped stages and their dependents."""
import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ase.discovery import discover_capabilities, record_discovery, validate_intake
from ase.gates import GateService
from ase.store import Store
from ase.tasks import TaskService, validate_contract_v2
from test_task_contract_v2 import contract


class ScopedReadinessTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory(prefix='ase-scoped-readiness-')
        self.addCleanup(temp.cleanup)
        root=Path(temp.name);work=root/'work';work.mkdir()
        self.store=Store(work,root/'state');self.addCleanup(self.store.close)
        self.tasks=TaskService(self.store)
        value=contract('scope-task');value['unknowns']=[]
        value['stages'][1]['unknown_ids']=[]
        value['stages'].append({'id':'follow-up','acceptance_ids':[], 'depends_on':['publish']})
        task=self.tasks.create(value)
        task=self.tasks.propose(task['task_id'],{'summary':'Prepare then publish the agreed guide','stage_ids':['draft','publish','follow-up']},expected_revision=task['revision'])
        self.approved=self.tasks.approve(task['task_id'],proposal_hash=task['proposal_hash'],approved_stage_ids=['draft','publish','follow-up'],user_message_ref='user:do-the-clear-task')

    def discovery(self, stages=None):
        gap={'id':'destination','text':'Publication destination is unavailable','source_refs':['task'],'required':True}
        if stages is not None:gap['blocking_stage_ids']=stages
        intake={'source_refs':[{'id':'task','kind':'user_message','reference':'chat:task','observed_at':'2026-11-13T10:00:00Z'}],
                'outcomes':[{'id':'guide','text':'Prepare and publish the guide','source_refs':['task']}],'gaps':[gap]}
        payload=discover_capabilities(self.store.project,scope={'resources':['guide.md']},backend_config={})
        payload['intake']=intake
        return payload

    def test_scoped_gap_keeps_independent_existing_authorization(self):
        record_discovery(self.store,'scope-task',self.discovery(['publish']),expected_revision=0)
        self.assertEqual(self.tasks.require_approval('scope-task','draft')['proposal_hash'],self.approved['proposal_hash'])
        self.assertEqual(self.tasks.readiness('scope-task',stage_id='draft')['required_source_gaps'],[])
        for stage in ['publish','follow-up']:
            with self.assertRaisesRegex(ValueError,'destination'):
                self.tasks.require_approval('scope-task',stage)

    def test_ambiguous_gap_is_conservative_and_empty_mapping_is_invalid(self):
        record_discovery(self.store,'scope-task',self.discovery(),expected_revision=0)
        with self.assertRaisesRegex(ValueError,'destination'):
            self.tasks.require_approval('scope-task','draft')
        with self.assertRaises(ValueError):validate_intake(self.discovery([])['intake'])

    def test_unknown_mapping_cannot_hide_a_gap(self):
        with self.assertRaisesRegex(ValueError,'stage'):
            record_discovery(self.store,'scope-task',self.discovery(['not-a-stage']),expected_revision=0)

    def test_conflicting_contract_mapping_is_rejected(self):
        original=self.tasks.snapshot('scope-task')['contract']
        changed=copy.deepcopy(original)
        changed['unknowns']=[{'id':'destination','statement':'Missing destination','status':'UNRESOLVED','blocking_stage_ids':['draft']}]
        task=self.tasks.revise('scope-task',changed,expected_revision=self.approved['revision'])
        with self.assertRaisesRegex(ValueError,'stage|mapping'):
            record_discovery(self.store,'scope-task',self.discovery(['publish']),expected_revision=0)

    def test_full_gate_still_reports_unresolved_scoped_gap(self):
        record_discovery(self.store,'scope-task',self.discovery(['publish']),expected_revision=0)
        gate=GateService(self.store).gate('scope-task')
        self.assertEqual(gate['status'],'BLOCKED')
        self.assertTrue(any('destination' in reason for reason in gate['blocked_reasons']))

    def test_capability_refresh_carries_existing_unresolved_intake(self):
        record_discovery(self.store,'scope-task',self.discovery(['publish']),expected_revision=0)
        refresh=discover_capabilities(self.store.project,scope={'resources':['guide.md']},backend_config={})
        record_discovery(self.store,'scope-task',refresh,expected_revision=1)
        with self.assertRaisesRegex(ValueError,'destination'):
            self.tasks.require_approval('scope-task','publish')

    def test_explicit_new_snapshot_cannot_drop_an_open_required_gap(self):
        record_discovery(self.store,'scope-task',self.discovery(['publish']),expected_revision=0)
        refresh=self.discovery(['publish']);refresh['intake']['gaps']=[]
        with self.assertRaisesRegex(ValueError,'gap'):
            record_discovery(self.store,'scope-task',refresh,expected_revision=1)

    def test_stale_stage_mapping_after_rename_is_conservative(self):
        record_discovery(self.store,'scope-task',self.discovery(['publish']),expected_revision=0)
        revised=copy.deepcopy(self.approved['contract'])
        revised['stages'][1]['id']='release'
        revised['stages'][2]['depends_on']=['release']
        self.tasks.revise('scope-task',revised,expected_revision=self.approved['revision'])
        for stage in ['draft','release','follow-up']:
            self.assertEqual([g['id'] for g in self.tasks.readiness('scope-task',stage_id=stage)['required_source_gaps']],['destination'])

    def test_unresolved_gap_cannot_be_narrowed_without_new_evidence(self):
        record_discovery(self.store,'scope-task',self.discovery(),expected_revision=0)
        with self.assertRaisesRegex(ValueError,'evidence|gap'):
            record_discovery(self.store,'scope-task',self.discovery(['publish']),expected_revision=1)

    def test_new_impact_evidence_can_scope_gap_without_another_approval(self):
        record_discovery(self.store,'scope-task',self.discovery(),expected_revision=0)
        update=self.discovery(['publish'])
        update['intake']['source_refs'].append({'id':'scope-inspection','kind':'local','reference':'local:stage-impact#version=2','observed_at':'2026-11-13T10:01:00Z'})
        update['intake']['gaps'][0]['source_refs'].append('scope-inspection')
        record_discovery(self.store,'scope-task',update,expected_revision=1)
        self.assertEqual(self.tasks.require_approval('scope-task','draft')['proposal_hash'],self.approved['proposal_hash'])

    def test_required_gap_resolution_needs_new_attributed_evidence(self):
        record_discovery(self.store,'scope-task',self.discovery(['publish']),expected_revision=0)
        update=self.discovery(['publish']);update['intake']['gaps'][0]['status']='RESOLVED'
        with self.assertRaisesRegex(ValueError,'evidence|gap'):
            record_discovery(self.store,'scope-task',update,expected_revision=1)
        update['intake']['source_refs'].append({'id':'answer','kind':'user_message','reference':'chat:confirmed-destination','observed_at':'2026-11-13T10:01:00Z'})
        update['intake']['gaps'][0]['source_refs'].append('answer')
        record_discovery(self.store,'scope-task',update,expected_revision=1)
        self.tasks.require_approval('scope-task','publish')

    def test_malformed_revision_does_not_coerce_policy_or_mutate_task(self):
        for value in [None, [], {**self.approved['contract'], 'execution_policy': [('backend','codex')]}]:
            with self.subTest(value=value),self.assertRaises(ValueError):
                self.tasks.revise('scope-task',value,expected_revision=self.approved['revision'])
        self.assertEqual(self.tasks.snapshot('scope-task')['revision'],self.approved['revision'])

    def tracked_gap_task(self, *, legacy_approval=False):
        value=copy.deepcopy(self.approved['contract'])
        value['unknowns']=[{'id':'destination','statement':'Missing destination','status':'UNRESOLVED','blocking_stage_ids':['publish']}]
        task=self.tasks.revise('scope-task',value,expected_revision=self.approved['revision'])
        task=self.tasks.propose('scope-task',{'summary':'Prepare then publish','stage_ids':['draft','publish','follow-up']},expected_revision=task['revision'])
        if legacy_approval:
            # Model an approval already persisted by the previous direct-only check.
            with patch.object(self.tasks,'_check_unknowns',return_value=None):
                self.tasks.approve('scope-task',proposal_hash=task['proposal_hash'],approved_stage_ids=['draft','publish','follow-up'],user_message_ref='user:previously-approved')
        return task

    def test_contract_unknown_blocks_dependent_approval_without_discovery(self):
        task=self.tracked_gap_task()
        with self.assertRaisesRegex(ValueError,'destination'):
            self.tasks.approve('scope-task',proposal_hash=task['proposal_hash'],approved_stage_ids=['follow-up'],user_message_ref='user:approve')
        self.tasks.approve('scope-task',proposal_hash=task['proposal_hash'],approved_stage_ids=['draft'],user_message_ref='user:approve-draft')
        self.tasks.require_approval('scope-task','draft')

    def test_saved_authority_does_not_bypass_propagated_contract_unknown(self):
        self.tracked_gap_task(legacy_approval=True)
        self.tasks.require_approval('scope-task','draft')
        with self.assertRaisesRegex(ValueError,'destination'):
            self.tasks.require_approval('scope-task','follow-up')

    def test_tracked_source_gap_blocks_dependents_under_saved_authority(self):
        self.tracked_gap_task(legacy_approval=True)
        record_discovery(self.store,'scope-task',self.discovery(['publish']),expected_revision=0)
        self.assertEqual(self.tasks.readiness('scope-task',stage_id='follow-up')['clarity'],'BLOCKED')
        self.tasks.require_approval('scope-task','draft')
        with self.assertRaisesRegex(ValueError,'destination'):
            self.tasks.require_approval('scope-task','follow-up')

    def ambiguous_unknown_task(self, mapping, *, legacy_approval=False, resolved=False):
        value=copy.deepcopy(self.approved['contract'])
        unknown={'id':'ambiguous','statement':'Impact is not established','status':'RESOLVED' if resolved else 'UNRESOLVED'}
        if mapping is not None:unknown['blocking_stage_ids']=mapping
        value['unknowns']=[unknown]
        task=self.tasks.revise('scope-task',value,expected_revision=self.approved['revision'])
        task=self.tasks.propose('scope-task',{'summary':'Prepare the guide','stage_ids':['draft','publish','follow-up']},expected_revision=task['revision'])
        if legacy_approval:
            with patch.object(self.tasks,'_check_unknowns',return_value=None):
                task=self.tasks.approve('scope-task',proposal_hash=task['proposal_hash'],approved_stage_ids=['draft','publish','follow-up'],user_message_ref='user:previously-approved')
        return task

    def test_unmapped_unknown_denies_approval_for_every_stage(self):
        task=self.ambiguous_unknown_task(None)
        for stage in ['draft','publish','follow-up']:
            with self.subTest(stage=stage),self.assertRaisesRegex(ValueError,'ambiguous'):
                self.tasks.approve('scope-task',proposal_hash=task['proposal_hash'],approved_stage_ids=[stage],user_message_ref='user:approve')

    def test_empty_unknown_mapping_denies_execution_under_saved_authority(self):
        self.ambiguous_unknown_task([],legacy_approval=True)
        from ase.workflow import WorkflowController
        for stage in ['draft','publish','follow-up']:
            with self.subTest(stage=stage),self.assertRaisesRegex(ValueError,'ambiguous'):
                WorkflowController(self.store).reserve_cycle('scope-task',stage)

    def test_resolved_unmapped_unknown_does_not_hold_work(self):
        task=self.ambiguous_unknown_task(None,resolved=True)
        self.tasks.approve('scope-task',proposal_hash=task['proposal_hash'],approved_stage_ids=['draft','publish','follow-up'],user_message_ref='user:confirmed')
        for stage in ['draft','publish','follow-up']:self.tasks.require_approval('scope-task',stage)


class ProgressPolicyContractTests(unittest.TestCase):
    def test_new_default_reassesses_stagnation_without_fixed_total_cap(self):
        policy=validate_contract_v2(contract())['execution_policy']
        self.assertEqual(policy['recovery_mode'],'progress')
        self.assertEqual(policy['stagnation_limit'],3)
        self.assertIsNone(policy['max_stage_cycles']);self.assertIsNone(policy['max_task_cycles'])

    def test_explicit_fixed_policy_and_limits_remain_supported(self):
        value=contract();value['execution_policy']={'recovery_mode':'fixed','max_stage_cycles':2,'max_task_cycles':5,'max_diagnostic_passes':1}
        policy=validate_contract_v2(value)['execution_policy']
        self.assertEqual(policy['recovery_mode'],'fixed');self.assertEqual(policy['max_stage_cycles'],2)

    def test_old_style_explicit_hard_caps_keep_fixed_policy(self):
        value=contract();value['execution_policy']={'max_stage_cycles':2,'max_task_cycles':5,'max_diagnostic_passes':1}
        self.assertEqual(validate_contract_v2(value)['execution_policy']['recovery_mode'],'fixed')

    def test_invalid_modes_thresholds_and_unbounded_fixed_policy_fail(self):
        for policy in [{'recovery_mode':'guess'},{'stagnation_limit':True},{'stagnation_limit':0},
                       {'recovery_mode':'fixed','max_stage_cycles':None}]:
            value=contract();value['execution_policy']=policy
            with self.subTest(policy=policy),self.assertRaises(ValueError):validate_contract_v2(value)


if __name__=='__main__':unittest.main()
