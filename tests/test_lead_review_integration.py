"""CLI/gate/host boundaries; policy seams here are not independent model AC."""
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

import test_lead_review as support
from ase.gates import GateService, verification_context_hash
from ase.state import read_record
from ase.workflow import AC_SCHEMA


class LeadReviewIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.case = support.LeadReviewTests(); self.case.setUp()
        self.addCleanup(self.case.doCleanups)

    def test_new_cli_tasks_scale_lead_passes_with_risk_and_expose_local_commands(self):
        c = self.case
        argv = [sys.executable, '-B', '-m', 'ase', '--repo', str(c.root), '--home', str(c.home)]
        for task_id, risk, passes in (('new-cli', c.contract.get('risk', 'MEDIUM'), 1), ('new-high', 'HIGH', 3)):
            data = dict(c.contract); data.pop('lead_review'); data['id'] = task_id; data['risk'] = risk
            file = c.base / (task_id + '.json'); file.write_text(json.dumps(data), encoding='utf-8')
            result = subprocess.run([*argv, 'task', 'create', '--file', str(file)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(json.loads(result.stdout)['contract'].get('lead_review'), {**support.POLICY, 'additional_passes': passes})
        result = subprocess.run([*argv, 'lead-review', 'status', 'new-cli'], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('required_passes', result.stdout)

    def test_gate_exposes_missing_whole_root_passes_but_not_intermediate_stages(self):
        c = self.case
        whole = GateService(c.store).gate('lead')
        self.assertEqual(whole['lead_review']['completed_passes'], 0)
        self.assertEqual(whole['lead_review']['status'], 'BLOCKED')
        self.assertEqual(GateService(c.store).gate('lead', stage_id='finish')['lead_review']['status'], 'NOT_REQUIRED')

    def test_whole_verifier_context_changes_without_self_invalidating_pass_inputs(self):
        c = self.case; task = c.tasks.snapshot('lead')
        old = verification_context_hash(c.store, task['contract'], 'lead')
        stage = verification_context_hash(c.store, task['contract'], 'lead', 'finish')
        before = c.controller._request('lead', 'ac', AC_SCHEMA, {'stage_id': None})['bindings']
        ticket = c.prepare(); c.record(ticket)
        after = c.controller._request('lead', 'ac', AC_SCHEMA, {'stage_id': None})['bindings']
        self.assertNotEqual(before.get('lead_review_sha256'), after.get('lead_review_sha256'))
        self.assertEqual(old, verification_context_hash(c.store, task['contract'], 'lead'))
        self.assertEqual(stage, verification_context_hash(c.store, task['contract'], 'lead', 'finish'))
        self.assertEqual(c.service.gate('lead')['completed_passes'], 1)

    def test_evidence_packet_supplies_original_intent_and_real_lead_assessments(self):
        c = self.case
        c.service.classify('lead', {'kind': 'nontrivial',
            'reason': 'The guide has a substantive approved business constraint',
            'evidence_ids': ['ready'], 'source_refs': ['guide.txt']},
            thread_id=support.OWNER, expected_revision=0)
        c.finish_three()
        authority = c.service.gate('lead')
        self.assertEqual(authority['status'], 'READY')
        packet = c.controller._packet('lead')
        files = {row['path']: row['content'] for row in packet['files']}
        report = json.loads(files['_ase_context/lead_review.json'])
        self.assertIn(c.contract['request'], json.dumps(report))
        self.assertIn('native-turn-', json.dumps(report))
        self.assertNotIn('source_paths', json.dumps(report))
        self.assertNotIn('source_hashes', json.dumps(report))
        self.assertEqual(report['gate']['classification'], report['classification'])
        self.assertIn('guide.txt', authority['classification']['base']['source_paths'])
        self.assertEqual(c.service.gate('lead'), authority)

    def test_packet_references_only_an_identical_approved_current_contract(self):
        import copy
        from ase.protocol import canonical, sha256
        c = self.case; previous = c.service.requirements('lead')['approved_intents'][-1]
        task = c.tasks.snapshot('lead'); contract = copy.deepcopy(task['contract'])
        contract['request'] += '; preserve the clarified title'
        c.tasks.revise('lead', contract, expected_revision=task['revision'])
        c.approve('Approved title clarification'); c.controller.check('lead'); c.finish_three()
        originals = c.service.requirements('lead')['approved_intents']
        packet = c.controller._packet('lead')
        files = {row['path']: row for row in packet['files']}
        report = json.loads(files['_ase_context/lead_review.json']['content'])
        intents = report['requirements']['approved_intents']
        self.assertEqual(intents[0], previous, 'A different old contract remains complete')
        ref = intents[-1]['contract']
        self.assertEqual(ref.get('$ref'), '_ase_context/contract.json')
        supplied = files[ref['$ref']]
        self.assertEqual(ref['sha256'], sha256(supplied['content'].encode()))
        restored = copy.deepcopy(intents[-1]); restored['contract'] = json.loads(supplied['content'])
        self.assertEqual(restored, originals[-1], 'Restore every approved byte-equivalent field and intent hash')
        self.assertEqual(sha256(canonical({k: v for k, v in restored.items() if k != 'sha256'})), restored['sha256'])
        self.assertEqual(c.service.requirements('lead')['approved_intents'], originals)

    def test_numerically_equal_json_contracts_keep_distinct_approved_hashes(self):
        import copy
        from ase.protocol import canonical, sha256
        c = self.case
        for timeout in (10, 10.0):
            task = c.tasks.snapshot('lead'); contract = copy.deepcopy(task['contract'])
            contract['evidence'][0]['timeout'] = timeout
            c.tasks.revise('lead', contract, expected_revision=task['revision'])
            c.approve('Explicit timeout representation ' + str(timeout))
        c.controller.check('lead'); c.finish_three()
        originals = c.service.requirements('lead')['approved_intents']
        packet = c.controller._packet('lead'); files = {r['path']: r for r in packet['files']}
        report = json.loads(files['_ase_context/lead_review.json']['content'])
        intents = report['requirements']['approved_intents']
        self.assertNotIn('$ref', intents[-2]['contract'], 'Python numeric equality is not exact JSON identity')
        self.assertIs(type(intents[-2]['contract']['evidence'][0]['timeout']), int)
        self.assertIn('$ref', intents[-1]['contract'])
        for intent in intents:
            restored = copy.deepcopy(intent); ref = restored['contract']
            if '$ref' in ref: restored['contract'] = json.loads(files[ref['$ref']]['content'])
            self.assertEqual(sha256(canonical(restored['contract'])), restored['approval']['contract_hash'])
            self.assertEqual(sha256(canonical({k: v for k, v in restored.items() if k != 'sha256'})), restored['sha256'])
        self.assertEqual(c.service.requirements('lead')['approved_intents'], originals)

    def test_whole_acceptance_explicitly_challenges_classification_and_pass_evidence(self):
        c = self.case; requests = []
        class CaptureRequest:
            def run(self, adapter, request, **kwargs):
                requests.append(request)
                raise RuntimeError('request captured without executing a model')
        c.controller.runner = CaptureRequest()
        with self.assertRaisesRegex(RuntimeError, 'request captured'):
            c.controller.verify('lead')
        schema = requests[0]['schema']
        self.assertIn('lead_review', schema['properties'])
        self.assertIn('classification', requests[0]['handoff']['instruction'])

    def test_stage_verification_before_the_lead_pass_reads_as_pending_final_verification(self):
        c = self.case
        controller = c.verifying_controller(); c.implementation()
        with c.independent_boundary():
            controller.check('lead', 'finish')
            verdict = controller.verify('lead', 'finish')
            self.assertEqual(verdict['status'], 'APPROVED', verdict)
            controller._capture_stage('lead', 'finish', verdict)
            whole = GateService(c.store).gate('lead')
        self.assertEqual(whole['status'], 'BLOCKED')
        self.assertNotIn('stale', json.dumps(whole['blocked_reasons']))
        self.assertFalse(any(r.startswith('Independent ') for r in whole['blocked_reasons']), whole['blocked_reasons'])
        self.assertIn('Final independent AC, review and test adequacy of the whole task run after the lead review pass', whole['blocked_reasons'])
        self.assertEqual([r for r in whole['reasons'] if r.startswith('Independent ')], [])
        self.assertTrue(any(r.startswith('Lead review: ') for r in whole['blocked_reasons']))
        # Once the lead passes are READY the same stage receipts are reported exactly as before.
        from ase.lead_review import LeadReview
        with c.independent_boundary(), patch.object(LeadReview, 'gate',
                return_value={**whole['lead_review'], 'status': 'READY', 'blocked_reasons': []}):
            ready = GateService(c.store).gate('lead')
        self.assertEqual(ready['status'], 'BLOCKED')
        self.assertNotIn('Final independent AC, review and test adequacy of the whole task run after the lead review pass', ready['blocked_reasons'])
        self.assertTrue(any(r.startswith('Independent ') for r in ready['blocked_reasons']), ready['blocked_reasons'])

    def test_pending_lead_pass_folds_only_the_expected_missing_whole_task_adequacy_receipt(self):
        from ase.lead_review import LeadReview
        from ase.testing.adequacy import TestAdequacy
        c = self.case; c.implementation()
        missing = 'Independent test adequacy receipt missing'
        note = 'Final independent AC, review and test adequacy of the whole task run after the lead review pass'
        def adequacy(reasons):
            return patch.object(TestAdequacy, 'gate', return_value={'configured': True, 'status': 'BLOCKED',
                                'blocked_reasons': reasons, 'rejected_reasons': []})
        with c.independent_boundary(), adequacy([missing, 'Test adequacy plan is stale']):
            whole = GateService(c.store).gate('lead')
            stage = GateService(c.store).gate('lead', stage_id='finish')
        self.assertEqual(whole['status'], 'BLOCKED')
        self.assertNotIn(missing, whole['blocked_reasons'])
        self.assertIn(note, whole['blocked_reasons'])
        self.assertIn('Test adequacy plan is stale', whole['blocked_reasons'])
        self.assertIn(missing, stage['blocked_reasons'])
        with c.independent_boundary(), adequacy([missing]):
            only = GateService(c.store).gate('lead')
        self.assertEqual(only['status'], 'BLOCKED')
        self.assertTrue(any(r.startswith('Lead review: ') for r in only['blocked_reasons']), only['blocked_reasons'])
        with c.independent_boundary(), adequacy([missing]), patch.object(LeadReview, 'gate',
                return_value={**whole['lead_review'], 'status': 'READY', 'blocked_reasons': []}):
            ready = GateService(c.store).gate('lead')
        self.assertEqual(ready['status'], 'BLOCKED')
        self.assertIn(missing, ready['blocked_reasons'])

    def test_internal_lead_handoff_does_not_repeat_checks_or_independent_calls(self):
        c = self.case
        self.assertTrue(hasattr(c.controller, '_final_review'), 'Missing internal original-lead handoff')
        with patch.object(c.controller, 'check') as checks, patch.object(c.controller, 'verify') as verify:
            value = c.controller._final_review('lead')
            self.assertTrue(value['awaiting_lead_review'])
            self.assertFalse(value.get('awaiting_approval', False))
            checks.assert_not_called(); verify.assert_not_called()

    def test_lead_evidence_race_recheck_also_runs_for_document_roots(self):
        c = self.case; digest = ['first']
        def evaluated(*args):
            digest[0] = 'changed-during-gate'
            return {'status': 'APPROVED', 'blocked_reasons': [], 'rejected_reasons': [], 'reasons': [],
                'profile_evidence': {'status': 'PASS'}, 'contract_hash': c.tasks.snapshot('lead')['contract_hash']}
        # A deterministic policy seam isolates the final transactional race;
        # it does not create actual independent approval or native pass proof.
        with patch('ase.gates.evaluate_gate', side_effect=evaluated), \
                patch.object(c.module.LeadReview, 'binding', side_effect=lambda *args, **kwargs: digest[0]), \
                patch.object(c.module.LeadReview, 'gate', return_value={'configured': True, 'status': 'READY', 'blocked_reasons': [], 'rejected_reasons': []}):
            value = GateService(c.store).gate('lead')
        self.assertEqual(value['status'], 'BLOCKED')
        self.assertTrue(any('changed during gate' in reason for reason in value['reasons']))

    def test_final_gate_rejects_stale_lead_receipts_and_negative_dispositions(self):
        import copy
        from ase.gates import evaluate_gate
        from test_gate_regression import GateRegressionTests
        # Isolate the actual final consumer's policy, not native authentication
        # or semantic judgment. Runner authentication has separate real captures.
        fixture = GateRegressionTests(); fixture.setUp()
        fixture.current.update(lead_review_required=True, lead_review_sha256='current-lead')
        bindings = {'fingerprint': 'source', 'contract_sha256': fixture.current['contract_hash'],
            'proposal_sha256': 'proposal', 'plan_sha256': 'plan', 'check_sha256': 'check',
            'verification_context_sha256': 'missing', 'lead_review_sha256': 'current-lead'}
        receipts = [fixture.checks, {**fixture.ac, 'bindings': dict(bindings)},
            {**fixture.review, 'bindings': dict(bindings)}]
        decision = {'classification_status': 'AGREE', 'pass_assessment': 'SUFFICIENT',
            'reason': 'Checked current classification and all original-main critiques', 'evidence': ['Current bound critiques']}
        def verified(receipt, *, expected_bindings):
            if receipt['bindings'] != expected_bindings:
                raise ValueError('Stale final lead receipt binding')
            value = fixture.judgment(receipt)
            if receipt['role'] == 'ac' and disposition[0] is not None:
                value['result']['payload']['lead_review'] = copy.deepcopy(disposition[0])
            return value
        disposition = [decision]
        with patch('ase.gates.verify_receipt', side_effect=verified):
            self.assertEqual(evaluate_gate(fixture.contract, fixture.current, receipts, [])['status'], 'APPROVED')
            for role in ('ac', 'review'):
                with self.subTest(stale_role=role):
                    stale = copy.deepcopy(receipts)
                    next(row for row in stale if row.get('role') == role)['bindings']['lead_review_sha256'] = 'old-lead'
                    result = evaluate_gate(fixture.contract, fixture.current, stale, [])
                    self.assertEqual(result['status'], 'BLOCKED')
                    self.assertTrue(any('Stale final lead receipt binding' in r for r in result['reasons']))
            for name, invalid in [('missing', None), ('disagree', {**decision, 'classification_status': 'DISAGREE'}),
                    ('insufficient', {**decision, 'pass_assessment': 'INSUFFICIENT'}),
                    ('unverified', {**decision, 'pass_assessment': 'UNVERIFIED'}),
                    ('no-evidence', {**decision, 'evidence': []})]:
                with self.subTest(disposition=name):
                    disposition[0] = invalid
                    result = evaluate_gate(fixture.contract, fixture.current, receipts, [])
                    self.assertEqual(result['status'], 'BLOCKED')
                    self.assertTrue(any('Independent AC' in r for r in result['reasons']))

    def test_pending_lead_continuation_persists_in_session_and_meeting_report(self):
        from datetime import datetime, timedelta
        from ase.journal import Journal
        c = self.case; c.implementation()
        # Exercise real run closure and reports after the already tested stage
        # scheduler reaches its real final-review continuation boundary.
        with patch.object(c.controller, '_run', side_effect=lambda *a, **k: c.controller._final_review('lead')), \
                patch.object(c.controller, 'check') as checks, patch.object(c.controller, 'verify') as verify:
            result = c.controller.run('lead')
        checks.assert_not_called(); verify.assert_not_called()
        self.assertTrue(result['awaiting_lead_review']); self.assertFalse(result['awaiting_approval'])
        self.assertEqual(result['status'], 'BLOCKED')
        self.assertEqual(result['closure']['finish_reason'], 'awaiting_lead_review')
        self.assertEqual(result['closure']['state'], 'REVIEWING')
        self.assertEqual(c.tasks.snapshot('lead')['state'], 'REVIEWING')
        journal = Journal(c.store)
        self.assertEqual(journal.session('lead', result['session_id'])['payload']['finish_reason'], 'awaiting_lead_review')
        today = datetime.now().date()
        report = journal.meeting_report(today.isoformat(), (today + timedelta(days=1)).isoformat(), timezone='local')
        self.assertFalse(any(row['task_id'] == 'lead' for row in report['done']))
        continuation = next(row for row in report['next_steps'] if row['task_id'] == 'lead')
        self.assertEqual(continuation['next_action'], result['next_action'])
        self.assertFalse(any(row['task_id'] == 'lead' for row in report['blockers']))
        self.assertEqual(c.service.gate('lead')['completed_passes'], 0)


if __name__ == '__main__':
    unittest.main()
