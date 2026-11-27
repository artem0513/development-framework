"""Owner dispositions of review findings: P0/P1 cannot be waived, contract changes need a new decision."""
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

from ase.findings import Findings, finding_id
from ase.gates import evaluate_gate
from ase.state import hash_value

P1 = {"severity": "P1", "path": "app.py", "line": 3, "message": "Null dereference"}
P2 = {"severity": "P2", "path": "app.py", "line": 9, "message": "Style-only duplication"}


class _Store:
    def __init__(self):
        self.db = sqlite3.connect(':memory:', isolation_level=None)
        self.db.execute('CREATE TABLE runtime_records(kind TEXT, key TEXT, revision INT, payload TEXT, updated TEXT, PRIMARY KEY(kind,key))')


class FindingServiceTests(unittest.TestCase):
    def setUp(self):
        self.store = _Store()
        self.contract_hash = 'c1'
        review = patch('ase.runner.verify_receipt', return_value={'result': {'payload': {'findings': [P1, P2], 'summary': 's'}}})
        snapshot = patch('ase.tasks.TaskService.snapshot', side_effect=lambda task_id: {
            'task_id': task_id, 'contract_hash': self.contract_hash, 'proposal_hash': 'proposal'})
        review.start(); snapshot.start()
        self.addCleanup(review.stop); self.addCleanup(snapshot.stop)
        from ase.state import write_record
        write_record(self.store.db, 'verification', 't:review', {'role': 'review', 'ended_at_utc': '2026-11-21T10:00:00+00:00'},
                     expected_revision=0)
        self.service = Findings(self.store)

    def owner_prompt(self, text='Это ложное срабатывание, отклони', captured='2026-11-21T11:00:00+00:00'):
        from ase import prompts
        with patch('ase.prompts.now', return_value=captured):
            return prompts.capture(self.store, {'prompt': text, 'session_id': 's'})['id']

    def test_p1_cannot_be_waived_but_p2_can(self):
        with self.assertRaisesRegex(ValueError, 'cannot be waived'):
            self.service.dispose('t', finding_id(P1), 'waive', reason='later', user_message_ref='chat:1')
        with self.assertRaisesRegex(ValueError, 'owner-quote'):
            self.service.dispose('t', finding_id(P2), 'waive', reason='accepted debt', user_message_ref='chat:2')
        waived = self.service.dispose('t', finding_id(P2), 'waive', reason='accepted debt', user_message_ref='chat:2',
                                      owner_quote='P2 оставляем, это техдолг')
        self.assertFalse(waived['verified'])
        rows = {row['id']: row for row in self.service.list('t')['findings']}
        self.assertTrue(rows[finding_id(P1)]['blocking'])
        self.assertFalse(rows[finding_id(P2)]['blocking'])

    def test_disprove_needs_owner_reference_and_known_finding(self):
        with self.assertRaises(ValueError):
            self.service.dispose('t', finding_id(P1), 'disprove', reason='guarded at caller', user_message_ref='')
        with self.assertRaisesRegex(ValueError, 'Unknown finding'):
            self.service.dispose('t', 'deadbeefdeadbeef', 'disprove', reason='x', user_message_ref='chat:3')
        with self.assertRaisesRegex(ValueError, 'waive or disprove'):
            self.service.dispose('t', finding_id(P1), 'ignore', reason='x', user_message_ref='chat:3')
        with self.assertRaisesRegex(ValueError, 'owner prompt'):
            self.service.dispose('t', finding_id(P1), 'disprove', reason='guarded at caller', user_message_ref='chat:3')
        early = self.owner_prompt(captured='2026-11-21T09:00:00+00:00')
        with self.assertRaisesRegex(ValueError, 'predates this review'):
            self.service.dispose('t', finding_id(P1), 'disprove', reason='guarded at caller', user_message_ref=early)
        entry = self.service.dispose('t', finding_id(P1), 'disprove', reason='guarded at caller', user_message_ref=self.owner_prompt())
        self.assertTrue(entry['verified'])
        self.assertIn(finding_id(P1), self.service.current('t'))
        self.assertFalse([row for row in self.service.list('t')['findings'] if row['id'] == finding_id(P1)][0]['blocking'])

    def test_any_cited_prompt_must_postdate_the_review_and_its_text_is_kept(self):
        early = self.owner_prompt('ok', captured='2026-11-21T09:00:00+00:00')
        with self.assertRaisesRegex(ValueError, 'predates this review'):
            self.service.dispose('t', finding_id(P2), 'waive', reason='debt', user_message_ref=early)
        entry = self.service.dispose('t', finding_id(P2), 'waive', reason='debt', user_message_ref=self.owner_prompt('Оставь P2 как есть'))
        self.assertEqual((entry['verified'], entry['prompt_excerpt']), (True, 'Оставь P2 как есть'))

    def test_prompt_excerpt_is_redacted_and_keeps_the_end_of_long_prompts(self):
        secret = 'ghp_' + 'a' * 30
        entry = self.service.dispose('t', finding_id(P1), 'disprove', reason='guarded',
                                     user_message_ref=self.owner_prompt('x' * 400 + ' token ' + secret + ' — отклони P1'))
        self.assertNotIn(secret, entry['prompt_excerpt'])
        self.assertTrue(entry['prompt_excerpt'].endswith('отклони P1'))

    def test_without_prompt_capture_the_owner_quote_allows_an_unverified_disproof(self):
        with self.assertRaisesRegex(ValueError, 'owner_quote'):
            self.service.dispose('t', finding_id(P1), 'disprove', reason='guarded', user_message_ref='chat:9', owner_quote='  ')
        entry = self.service.dispose('t', finding_id(P1), 'disprove', reason='guarded at caller',
                                     user_message_ref='chat:9', owner_quote='Это не баг, отклони')
        self.assertEqual((entry['verified'], entry['owner_quote']), (False, 'Это не баг, отклони'))
        self.assertTrue(entry['owner_asserted'])
        self.assertFalse([r for r in self.service.list('t')['findings'] if r['id'] == finding_id(P1)][0]['blocking'])

    def test_with_prompt_capture_in_use_p0_p1_disproof_needs_the_captured_prompt(self):
        from ase.state import write_record
        for source in ('request', 'approval'):
            with self.subTest(source=source):
                store = _Store()
                write_record(store.db, 'verification', 't:review', {'ended_at_utc': '2026-11-21T10:00:00+00:00'}, expected_revision=0)
                binding = {'contract_hash': self.contract_hash, 'proposal_hash': 'proposal'}
                if source == 'approval':
                    first = {**binding, 'approved_stage_ids': ['draft'], 'user_message_ref': 'prompt:abc'}
                    write_record(store.db, 'approval_version', 't:1', first, expected_revision=0)
                    write_record(store.db, 'approval', 't', first, expected_revision=0)
                    write_record(store.db, 'approval', 't', {**binding, 'approved_stage_ids': ['draft', 'publish'],
                        'earlier_stage_grants': {'draft': 1}, 'user_message_ref': 'chat:publish'}, expected_revision=1)
                task = {**binding, 'task_id': 't', 'contract': {'sources': [
                    {'id': 'REQUEST', 'locator': 'prompt:abc' if source == 'request' else 'chat:1'}]}}
                with patch('ase.tasks.TaskService.snapshot', side_effect=lambda task_id: task):
                    service = Findings(store)
                    with self.assertRaisesRegex(ValueError, 'uses prompt capture'):
                        service.dispose('t', finding_id(P1), 'disprove', reason='guarded', user_message_ref='chat:9',
                                        owner_quote='Это не баг')
                    waived = service.dispose('t', finding_id(P2), 'waive', reason='debt', user_message_ref='chat:9',
                                             owner_quote='P2 оставляем')
                self.assertTrue(waived['owner_asserted'])

    def test_contract_revision_drops_earlier_dispositions(self):
        self.service.dispose('t', finding_id(P1), 'disprove', reason='guarded', user_message_ref=self.owner_prompt())
        self.contract_hash = 'c2'
        self.assertEqual(self.service.current('t'), {})


class ReviewJournalTests(unittest.TestCase):
    def test_an_omission_keeps_the_finding_open_and_explicit_proof_closes_it(self):
        from ase.findings import reconcile_review

        reply = {'findings': [P1]}
        rows = reconcile_review({}, reply, 'initial', 'source-1', {}, {'app.py'}, None)
        key = finding_id(P1)
        same = reconcile_review(rows, {'findings': []}, 'omitted', 'source-2', rows, {'app.py'}, None)
        self.assertEqual(same[key]['status'], 'OPEN')
        close = {'findings': [], 'finding_followup': [{'id': key, 'outcome': 'FIXED',
                 'source': 'app.py', 'evidence': 'The missing value now exits before the dereference.'}]}
        with self.assertRaisesRegex(ValueError, 'current packet'):
            reconcile_review(same, close, 'no-source', 'source-2', same, {'_ase_before/app.py'}, None)
        with self.assertRaisesRegex(ValueError, 'different source'):
            reconcile_review(same, close, 'same-source', 'source-1', same, {'app.py'}, None)
        closed = reconcile_review(same, close, 'checked', 'source-2', same, {'app.py'}, None)
        self.assertEqual(closed[key]['status'], 'RESOLVED')
        self.assertEqual(closed[key]['resolution']['receipt_sha256'], 'checked')
        later = reconcile_review(closed, {'findings': []}, 'later', 'source-3', closed, {'app.py'}, None)
        self.assertEqual(later, closed)
        regression = {**P1, 'line': 43, 'message': 'Missing value again reaches the dereference', 'same_finding_id': key}
        reopened = reconcile_review(later, {'findings': [regression]}, 'regression', 'source-4',
                                    later, {'app.py'}, None)
        self.assertEqual(set(reopened), {key})
        self.assertEqual(reopened[key]['status'], 'OPEN')
        self.assertEqual(reopened[key]['resolution']['receipt_sha256'], 'checked')
        self.assertEqual(reopened[key]['finding']['line'], 43)
        repeated = {**regression, 'same_finding_id': ''}
        rows = reconcile_review(reopened, {'findings': [repeated]}, 'repeat', 'source-4',
                                reopened, {'app.py'}, None)
        self.assertEqual(set(rows), {key})

    def test_whole_disposition_cannot_clear_an_identical_part_finding(self):
        from ase.findings import JOURNAL, reconcile_review
        from ase.state import write_record

        store = _Store()
        self.addCleanup(store.db.close)
        rows = reconcile_review({}, {'findings': [P2]}, 'whole', 'source', {}, {'app.py'}, None)
        rows = reconcile_review(rows, {'findings': [P2]}, 'part', 'source', {}, {'app.py'}, 'beta')
        self.assertEqual(len(rows), 2)
        write_record(store.db, JOURNAL, 't:edit', rows, expected_revision=0)
        task = {'task_id': 't', 'contract': {'stages': [{'id': 'edit'}]}}
        waiver = {finding_id(P2): {'disposition': 'waive', 'user_message_ref': 'test:whole-waiver'}}
        opened, disposed = Findings(store).review_obligations(task, 'edit', waiver)
        self.assertEqual([row['part_id'] for row in opened], ['beta'])
        self.assertEqual([row['id'] for row in disposed], [finding_id(P2)])

    def test_new_split_routes_an_existing_finding_to_its_part(self):
        from ase.findings import JOURNAL, reconcile_review
        from ase.state import write_record

        store = _Store()
        self.addCleanup(store.db.close)
        rows = reconcile_review({}, {'findings': [P2]}, 'whole', 'source', {}, {'app.py'}, None)
        write_record(store.db, JOURNAL, 't:edit', rows, expected_revision=0)
        task = {'task_id': 't', 'contract': {'review_packet': {'parts': [
            {'id': 'app', 'resources': ['app.py']}, {'id': 'tests', 'resources': ['tests']}]}}}
        with patch.object(Findings, 'current', return_value={}):
            journal = Findings(store)
            self.assertEqual(journal.for_review(task, 'edit'), {})
            self.assertEqual(journal.for_review(task, 'edit', 'app'), rows)
            active = reconcile_review(rows, {'findings': [{**P2, 'same_finding_id': finding_id(P2)}]},
                                      'part', 'source', rows, {'app.py'}, 'app')
        self.assertEqual(active[finding_id(P2)]['part_id'], 'app')

    def test_review_evidence_detects_private_edits_and_only_proves_named_deletions(self):
        from ase.evidence.check_inputs import MANIFEST_PATH, PACKET_PREFIX
        from ase.findings import SOURCE_CHANGES, _review_evidence, reconcile_review

        private_path = '.devframework/local/checks/probe.py'
        checks = {'path': MANIFEST_PATH, 'content': json.dumps({'inputs': [
            {'path': private_path, 'sha256': 'a' * 64}]})}
        before = [{'path': 'app.py', 'sha256': 'b' * 64}, checks]
        checked = {**checks, 'content': checks['content'].replace('a' * 64, 'c' * 64)}
        private_after = [before[0], checked, {'path': PACKET_PREFIX + private_path, 'sha256': 'd' * 64}]
        before_basis, _, _ = _review_evidence(before, 'actions')
        after_basis, private_paths, _ = _review_evidence(private_after, 'actions')
        self.assertNotEqual(before_basis, after_basis)
        self.assertIn(PACKET_PREFIX + private_path, private_paths)
        after = [{'path': SOURCE_CHANGES, 'content': json.dumps({'changes': [
            {'path': 'app.py', 'before_sha256': 'b' * 64, 'after_sha256': None}]})}]
        current_basis, paths, deleted = _review_evidence(after, 'actions')
        opened = reconcile_review({}, {'findings': [P1]}, 'open', before_basis, {}, {'app.py'}, None)
        close = {'findings': [], 'finding_followup': [{'id': finding_id(P1), 'outcome': 'FIXED',
                 'source': SOURCE_CHANGES, 'evidence': 'The offending file was removed.'}]}
        with self.assertRaisesRegex(ValueError, 'current packet'):
            reconcile_review(opened, close, 'wrong', current_basis, opened, paths, None, deleted={'another.py'})
        resolved = reconcile_review(opened, close, 'verified', current_basis, opened, paths, None, deleted=deleted)
        self.assertEqual(resolved[finding_id(P1)]['status'], 'RESOLVED')


class ReviewJournalIntegrationTests(unittest.TestCase):
    def test_independent_followup_preserves_the_finding_across_approval_revisions(self):
        from ase.adapters.codex import CodexAdapter
        from ase.runner import Runner
        from ase.state import read_record
        from ase.store import Store
        from ase.tasks import TaskService
        from ase.workflow import WorkflowController
        from test_runner import FIXTURE
        from test_task_contract_v2 import contract

        directory = tempfile.TemporaryDirectory(prefix='ase-finding-journal-')
        self.addCleanup(directory.cleanup)
        base = Path(directory.name)
        repo, trusted = base / 'repo', base / 'trusted'
        repo.mkdir()
        trusted.mkdir()
        guide = repo / 'guide.md'
        guide.write_text('The original operating guide.', encoding='utf-8')
        store = Store(repo, base / 'state')
        self.addCleanup(store.close)
        tasks = TaskService(store)
        spec = contract('journal')
        tasks.create(spec)

        def approve(ref):
            task = tasks.snapshot('journal')
            proposal = tasks.propose('journal', {'summary': 'Draft the guide', 'stage_ids': ['draft']},
                                     expected_revision=task['revision'])
            return tasks.approve('journal', proposal_hash=proposal['proposal_hash'],
                                 user_message_ref=ref, approved_stage_ids=['draft'])

        approve('test:fixture-owner-1')
        ac = {'criteria': [{'id': 'AC1', 'expected': spec['acceptance'][0]['expected'], 'status': 'PASS',
              'evidence': ['guide.md'], 'method': 'Fixture', 'limitations': [], 'missing_coverage': []}]}
        fixture = trusted / 'provider.py'
        fixture.write_text(FIXTURE.replace("sid = str(uuid.uuid4())",
            "sid = 'fixture-duplicate' if pathlib.Path('duplicate').exists() else str(uuid.uuid4())").replace("payload = {'verdict':'PASS'}",
            "payload = " + repr(ac) + "\nif '\"role\":\"review\"' in input_text: "
            "payload = json.loads(pathlib.Path('review.json').read_text())"), encoding='utf-8')
        controller = WorkflowController(store, runner=Runner({'executable': sys.executable,
            'executable_args': [str(fixture), 'success', 'codex'], 'protocol_only': True,
            'trusted_cwd': str(trusted)}, store.root / 'tasks/journal/sessions'))
        real_probe = CodexAdapter.probe

        def independent_fixture(adapter, *args, **kwargs):
            # Only a protocol seam. No independent model or semantic judgment runs here.
            return {**real_probe(adapter, *args, **kwargs), 'protocol_only': False}

        real_call = controller._call

        def review(payload, *, drift=None, run_checks=True):
            (trusted / 'review.json').write_text(json.dumps({'summary': 'Protocol fixture', **payload}))
            if run_checks:
                controller.check('journal', 'draft')

            def call_then_change(*args, **kwargs):
                captured = real_call(*args, **kwargs)
                if drift:
                    drift()
                return captured

            with patch.object(CodexAdapter, 'probe', new=independent_fixture), \
                    patch.object(controller, '_call', side_effect=call_then_change):
                return controller.verify('journal', 'draft')

        issue = {**P1, 'path': 'guide.md'}
        key = finding_id(issue)
        self.assertEqual(review({'findings': [issue]})['status'], 'REJECTED')
        task = tasks.snapshot('journal')
        tasks.revise('journal', {**spec, 'request': spec['request'] + ' Include the revised wording.'},
                     expected_revision=task['revision'])
        approve('test:fixture-owner-2')
        omitted = review({'findings': [], 'finding_followup': []})
        self.assertEqual(omitted['status'], 'BLOCKED', omitted)
        self.assertEqual(omitted['open_review_findings'], [key])
        self.assertIn('ase task check journal --stage draft', omitted['next_action'])
        self.assertIn('ase task verify journal --stage draft', omitted['next_action'])
        approval = read_record(store.db, 'approval', 'journal')
        guide.write_text('The clarified operating guide with the corrected workflow.', encoding='utf-8')
        fixed = {'id': key, 'outcome': 'FIXED', 'source': 'guide.md', 'evidence': 'The correct workflow is now present.'}
        stale = review({'findings': [], 'finding_followup': [fixed]},
                       drift=lambda: guide.write_text('Actual content changed during review.', encoding='utf-8'))
        self.assertIn(key, stale['open_review_findings'])
        self.assertEqual(review({'findings': [], 'finding_followup': []})['status'], 'BLOCKED')
        malformed = review({'summary': '   ', 'findings': [], 'finding_followup': [fixed]})
        self.assertIn(key, malformed['open_review_findings'])
        plan = tasks.execution_plan('journal')
        tasks.update_execution_plan('journal', {'checks': [*plan['checks'],
            {'id': 'extra', 'kind': 'artifact', 'path': 'guide.md', 'required': True}]},
            expected_revision=plan['revision'])
        old_checks = review({'findings': [], 'finding_followup': [fixed]}, run_checks=False)
        self.assertIn(key, old_checks['open_review_findings'])
        (trusted / 'duplicate').touch()
        new_issue = {**issue, 'message': 'This reply reused the acceptance session', 'same_finding_id': ''}
        self.assertEqual(review({'findings': [new_issue], 'finding_followup': []})['status'], 'REJECTED')
        (trusted / 'duplicate').unlink()
        self.assertEqual(review({'findings': [], 'finding_followup': []})['open_review_findings'], [key])
        guide.write_text('The clarified operating guide with the corrected workflow.', encoding='utf-8')
        self.assertEqual(review({'findings': [], 'finding_followup': [fixed]})['status'], 'APPROVED')
        self.assertEqual(review({'findings': []})['status'], 'APPROVED')
        self.assertEqual(read_record(store.db, 'approval', 'journal'), approval)
        self.assertEqual(Findings(store).list('journal')['journal'][0]['status'], 'RESOLVED')
        regression = {**issue, 'line': 12, 'same_finding_id': key}
        self.assertEqual(review({'findings': [regression]})['status'], 'REJECTED')
        journal = Findings(store).list('journal')['journal']
        self.assertEqual([(row['id'], row['status']) for row in journal], [(key, 'OPEN')])
        self.assertEqual(review({'findings': [], 'finding_followup': []})['status'], 'BLOCKED')
        Findings(store).dispose('journal', key, 'disprove', reason='Explicit owner observation',
                                user_message_ref='test:owner-finding-decision', owner_quote='This is an invalid finding.')
        final = review({'findings': []})
        self.assertEqual(final['status'], 'APPROVED')
        self.assertEqual([(row['severity'], row['verified']) for row in final['disposed_findings']], [('P1', False)])
        self.assertEqual(Findings(store).list('journal')['journal'][0]['status'], 'OWNER_DISPOSED')


class GateDispositionTests(unittest.TestCase):
    def setUp(self):
        self.contract = {"profile": "document", "acceptance": [
            {"id": "AC1", "expected": "A confirmed result", "mandatory": True, "evidence_ids": ["manual"]}],
            "evidence": [{"id": "manual", "kind": "manual", "required": True}], "unknowns": []}
        self.current = {"contract_hash": hash_value(self.contract), "proposal_hash": "proposal", "plan_hash": "plan",
                        "fingerprint": "source", "check_hash": "check", "checks": self.contract["evidence"]}
        self.checks = {"kind": "checks", "fingerprint": "source", "contract_hash": self.current["contract_hash"],
                       "plan_hash": "plan", "results": [{"id": "manual", "kind": "manual", "confirmed": True,
                                                           "user_message_ref": "chat:user:manual-proof"}]}
        self.receipts = [self.checks, {"role": "ac", "execution_id": "a"}, {"role": "review", "execution_id": "r"}]

    def gate(self, findings, dispositions):
        def judgment(receipt, **kwargs):
            payload = ({"criteria": [{"id": "AC1", "expected": "A confirmed result", "status": "PASS",
                                       "evidence": ["manual"], "method": "compared", "limitations": [], "missing_coverage": []}]}
                       if receipt["role"] == "ac" else {"findings": findings, "summary": "reviewed"})
            return {"valid": True, "evidence_kind": "independent_cli",
                    "result": {"payload": payload, "session_id": receipt["role"] + "-session"}}
        with patch("ase.gates.verify_receipt", side_effect=judgment):
            return evaluate_gate(self.contract, {**self.current, "finding_dispositions": dispositions}, self.receipts, [])

    def test_disposed_findings_unblock_and_are_reported(self):
        disprove = {"disposition": "disprove", "user_message_ref": "prompt:5", "verified": True}
        waive = {"disposition": "waive", "user_message_ref": "chat:6"}
        self.assertEqual(self.gate([P1, P2], {})["status"], "REJECTED")
        result = self.gate([P1, P2], {finding_id(P1): disprove, finding_id(P2): waive})
        self.assertEqual(result["status"], "APPROVED", result)
        self.assertEqual({row["id"] for row in result["disposed_findings"]}, {finding_id(P1), finding_id(P2)})

    def test_disproof_needs_a_captured_prompt_or_the_owner_quote(self):
        bare = {"disposition": "disprove", "user_message_ref": "chat:x"}
        self.assertEqual(self.gate([P1], {finding_id(P1): bare})["status"], "REJECTED")
        quoted = {**bare, "verified": False, "owner_quote": "Это не баг, guard стоит в вызывающем коде"}
        result = self.gate([P1], {finding_id(P1): quoted})
        self.assertEqual(result["status"], "APPROVED", result)
        self.assertFalse(result["disposed_findings"][0]["verified"])

    def test_waiver_never_covers_p1_even_if_recorded(self):
        result = self.gate([P1], {finding_id(P1): {"disposition": "waive", "user_message_ref": "chat:7"}})
        self.assertEqual(result["status"], "REJECTED")
        self.assertNotIn("disposed_findings", result)

    def test_changed_finding_text_is_a_new_finding(self):
        changed = {**P1, "message": "Null dereference in a new branch"}
        result = self.gate([changed], {finding_id(P1): {"disposition": "disprove", "user_message_ref": "prompt:8", "verified": True}})
        self.assertEqual(result["status"], "REJECTED")


if __name__ == '__main__':
    unittest.main()
