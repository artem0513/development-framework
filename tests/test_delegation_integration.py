"""Cross-service delegation closure and native handoff invariants."""
from contextlib import closing
from pathlib import Path
import unittest
from unittest.mock import patch

from ase.continuity import Continuity
from ase.delegations import DelegationRegistry
from ase.gates import GateService
from ase.discovery import discover_capabilities, record_discovery
from ase.delegation_artifacts import seal_result
from ase.workflow import WorkflowController
from ase.workflow import AC_SCHEMA
import json
import test_delegations as registry_fixture
import test_publishing_v2 as publishing_fixture


class DelegationIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = registry_fixture.DelegationTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.store = self.fixture.store
        self.registry = DelegationRegistry(self.store)

    def test_handoff_carries_explicit_project_workspace_and_target_root(self):
        dispatched = self.fixture.start()
        self.assertEqual(dispatched['handoff']['project'], {'project_id': self.store.project_id,
            'workspace_id': self.store.workspace_id, 'root': str(self.store.repo)})

    def test_one_native_handle_cannot_serve_two_unsettled_assignments(self):
        first = self.fixture.start('first')
        native = {'backend': 'codex', 'agent_id': 'same-native-worker'}
        self.registry.attach('delegated', 'first', first['payload']['attempt']['id'], native,
            expected_revision=first['revision'])
        second = self.fixture.start('second')
        with self.assertRaises(ValueError):
            self.registry.attach('delegated', 'second', second['payload']['attempt']['id'], native,
                expected_revision=second['revision'])

    def test_result_cannot_auto_attach_another_unsettled_assignments_native_handle(self):
        first = self.fixture.start('first')
        native = {'backend': 'codex', 'agent_id': 'same-native-worker'}
        self.registry.attach('delegated', 'first', first['payload']['attempt']['id'], native,
            expected_revision=first['revision'])
        second = self.fixture.start('second')
        payload = {'attempt_id': second['payload']['attempt']['id'], 'agent_id': native['agent_id'],
            'status': 'COMPLETE', 'summary': 'Conflicting worker identity', 'artifacts': []}
        with self.assertRaises(ValueError):
            self.registry.result('delegated', 'second', payload['attempt_id'], payload, expected_revision=second['revision'])
        self.assertEqual(self.registry.get('delegated', 'second')['payload']['state'], 'DISPATCHING')

    def test_orphan_recovery_cannot_share_an_unsettled_native_handle(self):
        first = self.fixture.start('first')
        native = {'backend': 'codex', 'agent_id': 'same-native-worker'}
        self.registry.attach('delegated', 'first', first['payload']['attempt']['id'], native,
            expected_revision=first['revision'])
        second = self.fixture.start('second')
        payload = {'attempt_id': second['payload']['attempt']['id'], 'agent_id': native['agent_id'],
            'status': 'COMPLETE', 'summary': 'Conflicting worker identity', 'artifacts': []}
        seal_result(self.store, second['payload'], payload)
        self.registry.recover('delegated', 'second')
        self.assertEqual(self.registry.get('delegated', 'second')['payload']['state'], 'UNKNOWN')

    def test_restore_exposes_unknown_attempt_and_gate_reports_unsettled_work(self):
        self.fixture.start()
        self.registry.recover('delegated', 'research')
        restored = Continuity(self.store).restore('delegated')
        self.assertEqual(restored['delegations']['work'][0]['payload']['state'], 'UNKNOWN')
        gate = GateService(self.store).gate('delegated', stage_id='draft')
        self.assertTrue(any('Delegated work' in reason for reason in gate['blocked_reasons']))

    def test_verifier_packet_includes_registry_history_and_verified_result_manifest(self):
        accepted, output = self.fixture.accepted()
        request = WorkflowController(self.store)._request('delegated', 'ac', AC_SCHEMA, {'stage_id': 'draft'})
        files = {item['path']: item for item in request['source_packet']['files']}
        self.assertIn('_ase_context/delegations.json', files)
        evidence = json.loads(files['_ase_context/delegations.json']['content'])
        self.assertEqual(evidence['work'][0]['payload']['state'], 'ACCEPTED')
        self.assertEqual(evidence['results']['research']['attempt_id'], accepted['payload']['attempt']['id'])
        self.assertTrue(any(event['kind'] == 'delegation_accepted' for event in evidence['events']))
        self.assertIn('advisory', evidence['authority'].lower())

    def test_tampered_registry_result_is_reported_as_missing_packet_coverage(self):
        accepted, output = self.fixture.accepted()
        output.write_text('Changed after coordinator acceptance', encoding='utf-8')
        request = WorkflowController(self.store)._request('delegated', 'ac', AC_SCHEMA, {'stage_id': 'draft'})
        self.assertTrue(any('delegation' in value.lower() for value in request['source_packet']['coverage']['missing']))

    def test_exact_settled_reply_can_repeat_after_native_handle_is_reused(self):
        first = self.fixture.start('first')
        result, payload, _ = self.fixture.result(first, 'first', agent_id='shared-native')
        accepted = self.registry.accept('delegated', 'first', first['payload']['attempt']['id'],
            expected_revision=result['revision'], coordinator='lead', summary='Accepted original output')
        second = self.fixture.start('second')
        self.registry.attach('delegated', 'second', second['payload']['attempt']['id'],
            {'backend': 'codex', 'agent_id': 'shared-native'}, expected_revision=second['revision'])
        self.assertEqual(self.registry.attach('delegated', 'first', first['payload']['attempt']['id'],
            {'backend': 'codex', 'agent_id': 'shared-native'}, expected_revision=first['revision']), accepted)
        self.assertEqual(self.registry.result('delegated', 'first', first['payload']['attempt']['id'], payload,
            expected_revision=result['revision']), accepted)

    def test_child_closure_does_not_wait_for_parent_or_sibling_assignments(self):
        self.registry.register('delegated', registry_fixture.spec('parent-work'))
        tasks = self.fixture.tasks
        parent = tasks.snapshot('delegated')
        child = dict(parent['contract'], id='child', parent_task_id='delegated', parent_stage_id='draft')
        tasks.create(child)
        self.assertEqual(self.registry.closure_blockers('child'), [])
        self.assertTrue(self.registry.closure_blockers('delegated'))

    def test_child_recovery_does_not_change_its_parents_native_attempt(self):
        parent_work = self.fixture.start('parent-work')
        parent = self.fixture.tasks.snapshot('delegated')
        self.fixture.tasks.create(dict(parent['contract'], id='child', parent_task_id='delegated', parent_stage_id='draft'))
        recovered = self.registry.recover('child')
        self.assertEqual(recovered['work'], [])
        self.assertEqual(self.registry.get('delegated', 'parent-work'),
            {key: value for key, value in parent_work.items() if key != 'handoff'})

    def test_accepted_child_facts_remain_dependencies_after_child_completion(self):
        tasks = self.fixture.tasks
        parent = tasks.snapshot('delegated')
        child = tasks.create(dict(parent['contract'], id='child', parent_task_id='delegated', parent_stage_id='draft'))
        record_discovery(self.store, 'child', discover_capabilities(self.store.project,
            scope={'resources': ['inputs', 'guide.md']}, backend_config={}), expected_revision=0)
        value = self.registry.register('child', registry_fixture.spec('child-facts'))
        dispatched = self.registry.dispatch('child', 'child-facts', expected_revision=value['revision'], coordinator='lead', backend='codex')
        attempt = dispatched['payload']['attempt']['id']
        value = self.registry.result('child', 'child-facts', attempt,
            {'attempt_id': attempt, 'agent_id': 'child-native', 'status': 'COMPLETE', 'summary': 'Original child facts', 'artifacts': []},
            expected_revision=dispatched['revision'])
        self.registry.accept('child', 'child-facts', attempt, expected_revision=value['revision'], coordinator='lead', summary='Checked child facts')
        # A protocol boundary double isolates terminal-task dependency lookup;
        # it is not independent semantic approval or live evidence.
        child = tasks.snapshot('child')
        with patch.object(GateService, 'gate', return_value={'status': 'APPROVED', 'contract_hash': child['contract_hash'],
                'plan_hash': tasks.execution_plan('child')['plan_hash']}):
            tasks.transition('child', 'COMPLETED', expected_revision=child['revision'], reason='Protocol-only completed-child boundary')
        value = self.registry.register('delegated', registry_fixture.spec('consumer', depends_on=['child-facts']))
        dispatched = self.registry.dispatch('delegated', 'consumer', expected_revision=value['revision'], coordinator='lead', backend='codex')
        self.assertEqual(dispatched['payload']['state'], 'DISPATCHING')


class DelegationPublicationFenceTests(unittest.TestCase):
    def setUp(self):
        self.fixture = publishing_fixture.PublishingV2Tests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        record_discovery(self.fixture.store, 'publish', discover_capabilities(self.fixture.store.project,
            scope={'resources': ['app.py']}, backend_config={}), expected_revision=0)

    def test_late_registry_work_holds_original_publication_launch(self):
        fixture = self.fixture
        controller = WorkflowController(fixture.store)
        authority = fixture.publisher._publication_authority(controller, 'publish', 'origin')
        cancelled, guard = fixture.publisher._publication_control(controller, 'publish', 'origin', authority)
        self.assertFalse(cancelled())
        registry = DelegationRegistry(fixture.store)
        registry.register('publish', {'id': 'late-review', 'stage_id': 'change', 'goal': 'Check final proposal',
            'expected_result': 'Return an advisory review', 'resources': ['app.py']})
        self.assertTrue(cancelled())
        launched = False
        with self.assertRaises(ValueError):
            with guard():
                launched = True
        self.assertFalse(launched)

    def test_registry_change_then_cancellation_still_invalidates_waiting_publication(self):
        fixture = self.fixture
        controller = WorkflowController(fixture.store)
        authority = fixture.publisher._publication_authority(controller, 'publish', 'origin')
        registry = DelegationRegistry(fixture.store)
        value = registry.register('publish', {'id': 'late-review', 'stage_id': 'change', 'goal': 'Check final proposal',
            'expected_result': 'Return an advisory review', 'resources': ['app.py']})
        registry.cancel('publish', 'late-review', expected_revision=value['revision'], reason='No native launch occurred')
        cancelled, guard = fixture.publisher._publication_control(controller, 'publish', 'origin', authority)
        self.assertTrue(cancelled())
        with self.assertRaises(ValueError):
            with guard():
                self.fail('Changed publication authority reached process launch')


class DelegationActionContextTests(unittest.TestCase):
    """Real observer/registry calls; semantic seams are explicit protocol only."""
    def setUp(self):
        import test_gate_regression as context_fixture
        self.fixture = context_fixture.VerificationContextTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.store = self.fixture.store
        self.registry = DelegationRegistry(self.store)

    @staticmethod
    def spec(name, *, depends_on=(), stage_id="b"):
        return {"id": name, "stage_id": stage_id,
            "goal": "Inspect the captured required resource identity",
            "expected_result": "Identity-specific bounded advisory findings", "mode": "read_only",
            "resources": ["proof.txt"], "depends_on": list(depends_on)}

    def returned(self, name, *, task_id="context", stage_id="b"):
        record = self.registry.register(task_id, self.spec(name, stage_id=stage_id))
        ticket = self.registry.dispatch(task_id, name, expected_revision=record["revision"], coordinator="lead", backend="codex")
        attempt = ticket["payload"]["attempt"]["id"]
        payload = {"attempt_id": attempt, "agent_id": "native-" + name, "status": "COMPLETE",
            "summary": "Observed required-resource", "artifacts": []}
        recorded = self.registry.result(task_id, name, attempt, payload, expected_revision=ticket["revision"])
        return recorded, payload

    def accepted(self, name, *, task_id="context", stage_id="b"):
        recorded, payload = self.returned(name, task_id=task_id, stage_id=stage_id)
        accepted = self.registry.accept(task_id, name, payload["attempt_id"], expected_revision=recorded["revision"],
            coordinator="lead", summary="Compared with captured resource identity")
        return accepted, payload

    def change_observation(self):
        self.fixture.remote.write_text("different-resource")
        self.fixture.controller.execute_actions("context", self.fixture.stage_b)

    @staticmethod
    def semantic_kind_seam(receipt, **kwargs):
        from ase.runner import verify_receipt
        verified = verify_receipt(receipt, **kwargs)
        if verified["evidence_kind"] != "protocol_only":
            raise AssertionError("Expected explicitly protocol-only fixture")
        return {**verified, "evidence_kind": "independent_cli"}

    def chain_child(self):
        """Parent action stage b is an inherited prerequisite of child draft."""
        import copy
        from ase.adapters import get_adapter
        from ase.runner import Runner
        from ase.tasks import TaskService
        from ase.workflow import EDIT_SCHEMA
        tasks = TaskService(self.store)
        original = copy.deepcopy(self.fixture.task["contract"])
        b = original["stages"][1]
        c = {"id": "c", "scope": original["scope"], "acceptance_ids": ["A"], "depends_on": ["b"]}
        original.update(id="chain", stages=[b, c])
        task = tasks.create(original)
        task = tasks.propose("chain", {"summary": "Establish action proof before child analysis", "stage_ids": ["b", "c"]}, expected_revision=task["revision"])
        task = tasks.approve("chain", proposal_hash=task["proposal_hash"], user_message_ref="test:chain", approved_stage_ids=["b", "c"])
        record_discovery(self.store, "chain", discover_capabilities(self.store.project,
            scope={"resources": ["proof.txt"]}, backend_config={}), expected_revision=0)
        runner = Runner(self.fixture.runner.config, self.store.root / "tasks/chain/sessions")
        controller = WorkflowController(self.store, runner=runner)
        controller.execute_actions("chain", b)
        controller.check("chain")
        for stage in task["contract"]["stages"]:
            receipt = runner.run(get_adapter("codex"), controller._request("chain", "implementation", EDIT_SCHEMA,
                {"stage": stage}), timeout=5)["receipt"]
            self.assertEqual(receipt["status"], "COMPLETE")
            controller._save("stage_implementation", "chain:" + stage["id"], receipt)
            verdict = controller.verify("chain", stage["id"])
            self.assertEqual(verdict["status"], "APPROVED", verdict)
            controller._capture_stage("chain", stage["id"], verdict)
        verdict = controller.verify("chain")
        self.assertEqual(verdict["status"], "APPROVED", verdict)
        child = {"id": "child", "schema_version": 2, "profile": "document", "request": "Analyze after parent prerequisite",
            "scope": original["scope"], "parent_task_id": "chain", "parent_stage_id": "c",
            "acceptance": [original["acceptance"][0]], "evidence": original["evidence"],
            "stages": [{"id": "draft", "acceptance_ids": ["A"]}]}
        tasks.create(child)
        record_discovery(self.store, "child", discover_capabilities(self.store.project,
            scope={"resources": ["proof.txt"]}, backend_config={}), expected_revision=0)
        return controller, b

    def test_changed_own_action_context_blocks_pending_dispatch(self):
        pending = self.registry.register("context", self.spec("pending"))
        self.change_observation()
        with self.assertRaisesRegex(ValueError, "context changed"):
            self.registry.dispatch("context", "pending", expected_revision=pending["revision"], coordinator="lead", backend="codex")

    def test_changed_own_action_context_blocks_returned_acceptance(self):
        recorded, payload = self.returned("returned")
        self.change_observation()
        with self.assertRaisesRegex(ValueError, "context changed"):
            self.registry.accept("context", "returned", payload["attempt_id"], expected_revision=recorded["revision"],
                coordinator="lead", summary="Changed identity cannot refresh original result")
        recovered = self.registry.recover("context", "returned")["work"][0]
        self.assertEqual(recovered, recorded)

    def test_changed_action_context_blocks_dependency_but_preserves_history(self):
        accepted, payload = self.accepted("facts")
        self.change_observation()
        consumer = self.registry.register("context", self.spec("consumer", depends_on=["facts"]))
        with self.assertRaisesRegex(ValueError, "dependency context is stale"):
            self.registry.dispatch("context", "consumer", expected_revision=consumer["revision"], coordinator="lead", backend="codex")
        self.assertEqual(self.registry.result("context", "facts", payload["attempt_id"], payload,
            expected_revision=accepted["revision"]), accepted)
        self.registry.cancel("context", "consumer", expected_revision=consumer["revision"], reason="Stale dependency requires fresh work")
        self.assertEqual(self.registry.closure_blockers("context"), [])

    def test_current_whole_judgments_allow_inherited_prerequisite_delegation(self):
        with patch("ase.gates.verify_receipt", side_effect=self.semantic_kind_seam):
            self.chain_child()
            accepted, _ = self.accepted("child-facts", task_id="child", stage_id="draft")
            consumer = self.registry.register("child", self.spec("child-consumer", depends_on=["child-facts"], stage_id="draft"))
            dispatched = self.registry.dispatch("child", "child-consumer", expected_revision=consumer["revision"], coordinator="lead", backend="codex")
        self.assertEqual(dispatched["payload"]["state"], "DISPATCHING")
        self.assertEqual(dispatched["handoff"]["dependencies"]["child-facts"]["revision"], accepted["revision"])

    def test_changed_inherited_action_context_blocks_accepted_dependency(self):
        from ase.state import read_record
        with patch("ase.gates.verify_receipt", side_effect=self.semantic_kind_seam):
            controller, b = self.chain_child()
            self.accepted("inherited-facts", task_id="child", stage_id="draft")
            historical_stage = read_record(self.store.db, "stage", "chain:b")
            self.fixture.remote.write_text("different-resource")
            controller.execute_actions("chain", b)
            self.assertEqual(controller.verify("chain", "b")["status"], "APPROVED")
            self.assertEqual(read_record(self.store.db, "stage", "chain:b"), historical_stage)
            consumer = self.registry.register("child", self.spec("inherited-consumer", depends_on=["inherited-facts"], stage_id="draft"))
            with self.assertRaisesRegex(ValueError, "dependency context is stale"):
                self.registry.dispatch("child", "inherited-consumer", expected_revision=consumer["revision"], coordinator="lead", backend="codex")


class CompactAdvisoryPacketTests(unittest.TestCase):
    """Actual registry transitions and immutable source bytes, never model AC."""
    def setUp(self):
        self.fixture = registry_fixture.DelegationTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.store = self.fixture.store
        self.registry = DelegationRegistry(self.store)
        self.controller = WorkflowController(self.store)

    def _accepted(self, count=16):
        import hashlib
        returned = []
        for number in range(count):
            name = 'module-' + str(number)
            ticket = self.fixture.start(name)
            attempt = ticket['payload']['attempt']['id']
            native = {'backend':'codex','agent_id':'native-'+name}
            record = self.registry.attach('delegated',name,attempt,native,expected_revision=ticket['revision'])
            output = Path(ticket['handoff']['artifact_dir'])/'finding.txt'
            output.write_bytes(b'Isolated module finding\n')
            summary = ('Observed module '+str(number)+' sources, callers, tests and isolated boundary probes. ')*40
            record = self.registry.result('delegated',name,attempt,{'attempt_id':attempt,'agent_id':native['agent_id'],
                'status':'COMPLETE','summary':summary,'artifacts':[{'path':'finding.txt',
                'sha256':hashlib.sha256(output.read_bytes()).hexdigest()}]},expected_revision=record['revision'])
            record = self.registry.accept('delegated',name,attempt,expected_revision=record['revision'],
                coordinator='lead',summary='Compared module finding with its current approved source')
            returned.append((record,output))
        return returned

    def _large_sources(self):
        bodies = {'inputs/a.txt':b'a'*440000,'inputs/b.txt':b'b'*440000}
        for path,data in bodies.items(): (self.store.repo/path).write_bytes(data)
        return bodies

    def _compact(self):
        evidence = self.registry.packet_evidence('delegated',stage_id='draft')
        self.assertEqual(evidence.get('format'),'compact_advisory_index_v1',
            'Large advisory history must use an explicit complete compact index')
        return evidence

    def test_sixteen_advisories_fit_without_omitting_large_source_bodies(self):
        from ase.protocol import canonical
        bodies = self._large_sources()
        base = self.controller._packet('delegated','draft')
        self.assertLess(len(canonical(base)),1048576)
        self._accepted()
        try:
            packet = self.controller._packet('delegated','draft')
        except ValueError as error:
            self.fail('Redundant advisory metadata exhausted a previously fitting packet: '+str(error))
        self.assertLessEqual(len(canonical(packet)),1048576)
        actual = {f['path']:f for f in packet['files']}
        for path,body in bodies.items():
            self.assertEqual(actual[path]['content'].encode('utf-8'),body)
        evidence = json.loads(actual['_ase_context/delegations.json']['content'])
        self.assertEqual(evidence.get('format'),'compact_advisory_index_v1')
        self.assertEqual(len(evidence['work_index']),16)
        self.assertEqual(packet['coverage']['missing'],[])

    def test_compact_index_preserves_all_identities_and_integrity_references(self):
        from ase.state import hash_value
        records = self._accepted()
        evidence = self._compact()
        self.assertEqual(evidence['counts'],{'work':16,'results':16,'events':80,'unsettled':0,'states':{'ACCEPTED':16}})
        self.assertEqual(len(evidence['history_sha256']),64)
        self.assertEqual(evidence['omitted']['specs_results_events'],'NOT_SUPPLIED_NOT_REVIEWED')
        indexed = {item['work_id']:item for item in evidence['work_index']}
        for record,_ in records:
            value = record['payload']
            item = indexed[value['id']]
            self.assertEqual((item['task_id'],item['stage_id'],item['revision'],item['state']),
                ('delegated','draft',record['revision'],'ACCEPTED'))
            self.assertEqual(item['record_sha256'],hash_value(record))
            self.assertEqual(item['attempt']['id'],value['attempt']['id'])
            self.assertEqual(item['attempt']['native'],value['attempt']['native'])
            self.assertEqual(item['result']['reference'],value['result'])
            self.assertEqual(item['acceptance']['sha256'],hash_value(value['acceptance']))
            self.assertEqual(item['binding']['status'],'CURRENT')
        original = indexed['module-0']['binding']['original_sha256']
        (self.store.repo/'guide.md').write_bytes(b'Legitimate integrated source change\n')
        changed = {item['work_id']:item for item in self._compact()['work_index']}['module-0']
        self.assertEqual(changed['binding']['status'],'CHANGED')
        self.assertEqual(changed['binding']['original_sha256'],original)
        self.assertNotEqual(changed['binding']['current_sha256'],original)

    def test_compact_index_preserves_unsettled_deep_descendant(self):
        from ase.discovery import discover_capabilities,record_discovery
        self._accepted()
        tasks = self.fixture.tasks
        parent = 'delegated'
        for number in range(8):
            name = 'child-'+str(number)
            tasks.create(dict(tasks.snapshot(parent)['contract'],id=name,parent_task_id=parent,parent_stage_id='draft'))
            parent = name
        record_discovery(self.store,parent,discover_capabilities(self.store.project,
            scope={'resources':['inputs','guide.md']},backend_config={}),expected_revision=0)
        record = self.registry.register(parent,registry_fixture.spec('deep-work'))
        ticket = self.registry.dispatch(parent,'deep-work',expected_revision=record['revision'],coordinator='lead',backend='codex')
        record = self.registry.attach(parent,'deep-work',ticket['payload']['attempt']['id'],
            {'backend':'codex','agent_id':'deep-native'},expected_revision=ticket['revision'])
        self.registry.recover(parent,'deep-work')
        evidence = self._compact()
        item = next(item for item in evidence['work_index'] if item['work_id']=='deep-work')
        self.assertEqual((item['task_id'],item['state']),(parent,'UNKNOWN'))
        self.assertEqual(item['attempt']['native'],{'backend':'codex','agent_id':'deep-native'})
        self.assertEqual(evidence['counts']['unsettled'],1)
        self.assertTrue(any('deep-work' in reason for reason in self.registry.closure_blockers('delegated','draft')))
        self.assertTrue(any('deep-work' in reason for reason in GateService(self.store).gate('delegated')['blocked_reasons']))

    def test_compaction_cannot_hide_last_corrupt_result_artifact(self):
        records = self._accepted()
        self._compact()
        records[-1][1].write_bytes(b'Tampered after advisory acceptance\n')
        with self.assertRaises(ValueError):
            self.registry.packet_evidence('delegated',stage_id='draft')
        self.assertTrue(any('integrity' in reason for reason in self.registry.closure_blockers('delegated')))
        packet = self.controller._packet('delegated','draft')
        self.assertTrue(any('delegation' in reason.lower() for reason in packet['coverage']['missing']))

    def test_essential_source_overflow_still_fails_closed(self):
        self._large_sources()
        (self.store.repo/'inputs/c.txt').write_bytes(b'c'*440000)
        with self.assertRaisesRegex(ValueError,'byte limit'):
            self.controller._packet('delegated','draft')

    def test_large_inventory_keeps_every_index_entry_and_original_packet_limit(self):
        self._large_sources()
        # Seed a valid persisted inventory from one API-created assignment;
        # registration throughput is outside this packet/index regression.
        import copy
        from ase.state import hash_value,transaction,write_record
        original = self.registry.register('delegated',registry_fixture.spec('pending-0'))
        with transaction(self.store.db) as db:
            for number in range(1,1000):
                value = copy.deepcopy(original['payload'])
                value['id'] = 'pending-'+str(number)
                value['spec'] = registry_fixture.spec(value['id'])
                value['spec_sha256'] = hash_value(value['spec'])
                write_record(db,'delegation','delegated:'+value['id'],value,expected_revision=0)
        evidence = self._compact()
        self.assertEqual(evidence['counts']['work'],1000)
        self.assertEqual(evidence['counts']['unsettled'],1000)
        self.assertEqual({item['work_id'] for item in evidence['work_index']},{'pending-'+str(i) for i in range(1000)})
        with self.assertRaisesRegex(ValueError,'byte limit'):
            self.controller._packet('delegated','draft')
