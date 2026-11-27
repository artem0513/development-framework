"""Quick lane: one step to a proposed LOW-risk task the owner approves; misuse and growth go to the full path."""
import copy
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from ase.quick import contract, start
from ase.store import Store
from ase.tasks import TaskService, validate_contract_v2


class QuickLaneTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='ase-quick-')
        root = Path(self.temp.name) / 'repo'
        (root / 'src').mkdir(parents=True)
        (root / 'src' / 'app.py').write_text('X = 1\n', encoding='utf-8')
        subprocess.run(['git', 'init', '-q', str(root)], check=True)
        self.store = Store(root, Path(self.temp.name) / 'state')
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self.store.close)

    def test_one_step_proposes_and_the_owner_approves_the_plan_with_a_reply(self):
        result = start(self.store, task_id='fix-x', request='X must be 2', resources=['src'],
                       test_command=f'"{sys.executable}" -m unittest discover -s tests', summary='Set X to 2',
                       user_message_ref='chat:owner-1')
        self.assertEqual(result['lane'], 'quick')
        self.assertNotEqual(result['state'], 'READY', 'nothing is approved before the owner sees the plan')
        tasks = TaskService(self.store)
        self.assertEqual(tasks.snapshot('fix-x')['approved_stage_ids'], [])
        with self.assertRaisesRegex(ValueError, 'Show the owner the implementation plan first'):
            tasks.approve('fix-x', proposal_hash=result['proposal_hash'], user_message_ref='chat:owner-1',
                          approved_stage_ids=['implement'])
        approved = tasks.approve('fix-x', proposal_hash=result['proposal_hash'], user_message_ref='chat:owner-1-plan-ok',
                                 approved_stage_ids=['implement'])
        self.assertEqual(approved['state'], 'READY')
        task = TaskService(self.store).snapshot('fix-x')
        c = task['contract']
        self.assertEqual((c['risk'], c['lead_review']['additional_passes'], c['review_format']), ('LOW', 1, {'version': 2}))
        self.assertNotIn('test_adequacy', c)
        self.assertEqual(c['evidence'][0]['parser'], 'unittest')
        self.assertEqual(c['execution_policy']['recovery_mode'], 'progress')
        self.assertTrue(TaskService(self.store).readiness('fix-x')['discovery_present'])

    def test_next_steps_are_parseable_commands_in_a_working_order(self):
        import re
        import shlex
        from ase.__main__ import parser
        result = start(self.store, task_id='n', request='X must be 2', resources=['src'], test_command='pytest',
                       summary='Set X to 2', user_message_ref='chat:owner-1')
        commands = [step.split('  # ')[0] for step in result['next'] if step.startswith('ase ')]
        order = [' '.join(shlex.split(c)[1:3]) for c in commands]
        self.assertEqual(order, ['task approve', 'events bind-local', 'task run', 'lead-review status',
                                 'lead-review prepare', 'lead-review record', 'task verify', 'task finish'])
        self.assertIn('implementation capture', result['next'][3])
        for command in commands:
            # A placeholder becomes its first listed choice, or a value valid for any argument type.
            argv = shlex.split(re.sub(r'<([^>]*)>', lambda m: m[1].split('|')[0] if '|' in m[1] else '1', command))
            with self.subTest(command=command):
                parser().parse_args(argv[1:])

    def test_approval_guard_variants_preapproval_and_captured_prompts(self):
        from ase.prompts import capture
        tasks = TaskService(self.store)
        result = start(self.store, task_id='g', request='X must be 2', resources=['src'], test_command='pytest',
                       summary='Set X to 2', user_message_ref='chat:owner-7')
        approve = lambda ref, **kw: tasks.approve('g', proposal_hash=result['proposal_hash'], user_message_ref=ref,
                                                  approved_stage_ids=['implement'], **kw)
        with self.assertRaisesRegex(ValueError, 'plan first'):
            approve(' Chat:Owner-7 ')
        # The owner said in the request to follow the recommended plan without asking.
        with self.assertRaisesRegex(ValueError, 'boolean'):
            approve('chat:owner-7', owner_preapproved='yes')
        done = approve('chat:owner-7', owner_preapproved=True)
        self.assertEqual(done['state'], 'READY')
        from ase.state import read_record
        self.assertIs(read_record(self.store.db, 'approval', 'g')['payload']['owner_preapproved'], True)
        # A captured request needs a captured reply, not free text.
        request = capture(self.store, {'prompt': 'X must be 3', 'session_id': 's'})
        captured = start(self.store, task_id='p', request='X must be 3', resources=['src'], test_command='pytest',
                         summary='Set X to 3', user_message_ref=request['id'])
        approve_p = lambda ref: tasks.approve('p', proposal_hash=captured['proposal_hash'], user_message_ref=ref,
                                              approved_stage_ids=['implement'])
        earlier = capture(self.store, {'prompt': 'an older message', 'session_id': 'old'})
        from ase.state import read_record, write_record
        old = read_record(self.store.db, 'user_prompt', earlier['id'][len('prompt:'):])
        write_record(self.store.db, 'user_prompt', earlier['id'][len('prompt:'):],
                     {**old['payload'], 'captured': '2000-01-01T00:00:00+00:00'}, expected_revision=old['revision'])
        self.store.db.commit()
        for ref, message in ((request['id'], 'plan first'), ('chat:looks-fine', 'captured reply'),
                             (earlier['id'], 'given after it')):
            with self.subTest(ref=ref), self.assertRaisesRegex(ValueError, message):
                approve_p(ref)
        reply = capture(self.store, {'prompt': 'ok, go with the plan', 'session_id': 's'})
        self.assertEqual(approve_p(reply['id'])['state'], 'READY')
        self.assertEqual(read_record(self.store.db, 'approval', 'p')['payload']['provenance'], 'captured_prompt')
        self.assertEqual(read_record(self.store.db, 'approval', 'g')['payload']['provenance'], 'owner_asserted')

    def test_approval_records_provenance_and_preapproval_cannot_cite_an_older_prompt(self):
        from ase.prompts import capture
        from ase.state import read_record, write_record
        tasks = TaskService(self.store)

        def backdate(prompt):
            key = prompt['id'][len('prompt:'):]
            old = read_record(self.store.db, 'user_prompt', key)
            write_record(self.store.db, 'user_prompt', key, {**old['payload'], 'captured': '2000-01-01T00:00:00+00:00'},
                         expected_revision=old['revision'])
            self.store.db.commit()
            return prompt['id']

        older = backdate(capture(self.store, {'prompt': 'an older message', 'session_id': 'old'}))
        request = capture(self.store, {'prompt': 'X must be 3, go ahead without asking', 'session_id': 's'})
        started = start(self.store, task_id='pre', request='X must be 3', resources=['src'], test_command='pytest',
                        summary='Set X to 3', user_message_ref=request['id'])
        approve = lambda ref, **kw: tasks.approve('pre', proposal_hash=started['proposal_hash'], user_message_ref=ref,
                                                  approved_stage_ids=['implement'], **kw)
        with self.assertRaisesRegex(ValueError, 'predates the task request'):
            approve(older, owner_preapproved=True)
        approve(request['id'], owner_preapproved=True)
        self.assertEqual(read_record(self.store.db, 'approval', 'pre')['payload']['provenance'], 'captured_prompt')
        # Full path: a captured prompt older than the proposal is recorded as asserted, not as a verified reply.
        start(self.store, task_id='full', request='X must be 2', resources=['src'], test_command='pytest',
              summary='Set X to 2', user_message_ref='chat:owner-9')
        task = tasks.snapshot('full')
        grown = copy.deepcopy(task['contract']); grown['test_adequacy'] = {'version': 1, 'required': True}
        task = tasks.revise('full', grown, expected_revision=task['revision'])
        task = tasks.propose('full', {'summary': 'Set X to 2', 'stage_ids': ['implement']}, expected_revision=task['revision'])
        tasks.approve('full', proposal_hash=task['proposal_hash'], user_message_ref=older, approved_stage_ids=['implement'])
        self.assertEqual(read_record(self.store.db, 'approval', 'full')['payload']['provenance'], 'owner_asserted')

    def test_full_path_task_keeps_its_own_approval_rules(self):
        tasks = TaskService(self.store)
        start(self.store, task_id='f', request='X must be 2', resources=['src'], test_command='pytest',
              summary='Set X to 2', user_message_ref='chat:owner-8')
        task = tasks.snapshot('f')
        grown = copy.deepcopy(task['contract']); grown['test_adequacy'] = {'version': 1, 'required': True}
        task = tasks.revise('f', grown, expected_revision=task['revision'])
        task = tasks.propose('f', {'summary': 'Set X to 2', 'stage_ids': ['implement']}, expected_revision=task['revision'])
        approved = tasks.approve('f', proposal_hash=task['proposal_hash'], user_message_ref='chat:owner-8',
                                 approved_stage_ids=['implement'])
        self.assertEqual(approved['state'], 'READY')

    def test_adding_test_adequacy_moves_the_task_to_the_full_path(self):
        start(self.store, task_id='grow', request='X must be 2', resources=['src'], test_command='pytest',
              summary='Set X to 2', user_message_ref='chat:owner-2')
        tasks = TaskService(self.store)
        task = tasks.snapshot('grow')
        kept = tasks.revise('grow', {**copy.deepcopy(task['contract']), 'request': 'X must be 2 everywhere'},
                            expected_revision=task['revision'])
        self.assertEqual(kept['contract']['lane'], 'quick')
        grown = copy.deepcopy(kept['contract']); grown['test_adequacy'] = {'version': 1, 'required': True}  # lane left in
        moved = tasks.revise('grow', grown, expected_revision=kept['revision'])
        self.assertNotIn('lane', moved['contract'])

    def test_parser_comes_from_executable_and_module_not_substrings(self):
        from ase.quick import _parser
        self.assertEqual(_parser(['python', '-m', 'unittest', 'tests.test_node_bridge']), {'parser': 'unittest'})
        self.assertEqual(_parser(['C:/Py/python.exe', '-m', 'pytest', '-q']), {'parser': 'pytest'})
        self.assertEqual(_parser(['npm.cmd', 'test']), {'parser': 'node'})
        self.assertEqual(_parser(['make', 'check']), {})
        self.assertEqual(_parser(['uv', 'run', 'pytest']), {'parser': 'pytest'})
        self.assertEqual(_parser(['yarn.ps1', 'test']), {'parser': 'node'})

    def test_quick_lane_refused_when_the_branch_is_already_too_big(self):
        import subprocess
        repo = self.store.repo
        for args in (('config', 'user.email', 'q@example.invalid'), ('config', 'user.name', 'q')):
            subprocess.run(['git', '-C', str(repo), *args], check=True)
        subprocess.run(['git', '-C', str(repo), 'add', '-A'], check=True)
        subprocess.run(['git', '-C', str(repo), 'commit', '-qm', 'base'], check=True)
        subprocess.run(['git', '-C', str(repo), 'branch', 'basebr'], check=True)
        (repo / 'src' / 'big.py').write_text(''.join(f'v{i} = {i}\n' for i in range(400)), encoding='utf-8')
        subprocess.run(['git', '-C', str(repo), 'add', '-A'], check=True)
        subprocess.run(['git', '-C', str(repo), 'commit', '-qm', 'big'], check=True)
        with self.assertRaisesRegex(ValueError, 'quick lane cannot finish here'):
            start(self.store, task_id='late', request='r', resources=['src'], test_command='pytest', summary='s',
                  user_message_ref='chat:1', base='basebr')

    def test_required_adequacy_and_lane_cannot_coexist(self):
        base = contract(task_id='t', request='r', resources=['src'], test_command='pytest', source='chat:1', backend='claude')
        with self.assertRaisesRegex(ValueError, 'remove lane'):
            validate_contract_v2({**base, 'test_adequacy': {'version': 1, 'required': True}})

    def test_windows_paths_survive_argv_splitting(self):
        from unittest.mock import patch
        from ase import quick
        with patch.object(quick.os, 'name', 'nt'):
            self.assertEqual(quick._argv(r'"C:\Program Files\Python\python.exe" -m pytest tests'),
                             [r'C:\Program Files\Python\python.exe', '-m', 'pytest', 'tests'])
            self.assertEqual(quick._argv(r'C:\Py\python.exe -m pytest'), [r'C:\Py\python.exe', '-m', 'pytest'])

    def test_lane_is_refused_for_riskier_or_multi_stage_work(self):
        base = contract(task_id='t', request='r', resources=['src'], test_command='pytest', source='chat:1', backend='claude')
        validate_contract_v2(copy.deepcopy(base))
        for change in ({'risk': 'MEDIUM'}, {'stages': base['stages'] + [{'id': 'docs', 'acceptance_ids': ['AC1'], 'depends_on': []}]},
                       {'lane': 'fast'}):
            broken = {**copy.deepcopy(base), **change}
            with self.subTest(change=list(change)), self.assertRaises(ValueError):
                validate_contract_v2(broken)

    def test_unknown_prompt_reference_and_missing_resource_are_refused(self):
        with self.assertRaisesRegex(ValueError, 'Unknown captured prompt'):
            start(self.store, task_id='a', request='r', resources=['src'], test_command='pytest', summary='s',
                  user_message_ref='prompt:0000000000000000')
        with self.assertRaisesRegex(ValueError, 'resource'):
            start(self.store, task_id='b', request='r', resources=[], test_command='pytest', summary='s',
                  user_message_ref='chat:1')


if __name__ == '__main__':
    unittest.main()
