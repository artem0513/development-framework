"""New CLI software work opts into adequacy; saved contracts keep their identity."""
from contextlib import closing
import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from ase.tasks import validate_contract_v2


def software_contract():
    return {'id': 'adequacy-policy', 'schema_version': 2, 'profile': 'software',
        'request': 'Implement a durable result queue',
        'scope': {'resources': ['queue.py'], 'effects': ['file_write']},
        'acceptance': [{'id': 'QUEUE', 'expected': 'Results survive a restart', 'evidence_ids': ['tests']}],
        'evidence': [{'id': 'tests', 'kind': 'test', 'parser': 'unittest',
            'argv': [sys.executable, '-B', '-m', 'unittest', 'discover', '-v']}],
        'stages': [{'id': 'implement', 'acceptance_ids': ['QUEUE']}]}


class AdequacyPolicyTests(unittest.TestCase):
    def test_explicit_current_software_policy_is_valid(self):
        contract = software_contract()
        contract['test_adequacy'] = {'version': 1, 'required': True}
        self.assertEqual(validate_contract_v2(contract)['test_adequacy'], {'version': 1, 'required': True})

    def test_false_malformed_and_nonsoftware_adequacy_policies_are_rejected(self):
        for policy in ({'version': True, 'required': True}, {'version': 1, 'required': False},
                {'version': 2, 'required': True}, {'version': 1, 'required': True, 'skip': True}):
            contract = software_contract()
            contract['test_adequacy'] = policy
            with self.subTest(policy=policy), self.assertRaises(ValueError):
                validate_contract_v2(contract)

    def test_legacy_validation_does_not_insert_new_policy(self):
        self.assertNotIn('test_adequacy', validate_contract_v2(software_contract()))

    def test_new_software_cli_task_enables_adequacy_before_approval(self):
        with tempfile.TemporaryDirectory(prefix='ase-adequacy-cli-') as temp:
            base = Path(temp)
            repo = base / 'work'
            repo.mkdir()
            file = base / 'contract.json'
            file.write_text(json.dumps(software_contract()), encoding='utf-8')
            result = subprocess.run([sys.executable, '-B', '-m', 'ase', '--repo', str(repo),
                '--home', str(base / 'state'), 'task', 'create', '--file', str(file)],
                capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(json.loads(result.stdout)['contract'].get('test_adequacy'), {'version': 1, 'required': True})

    def test_child_inherits_required_adequacy_and_revision_preserves_it(self):
        from ase.store import Store
        from ase.tasks import TaskService
        with tempfile.TemporaryDirectory(prefix='ase-adequacy-inherit-') as temp:
            root = Path(temp) / 'repo'
            root.mkdir()
            with closing(Store(root, Path(temp) / 'state')) as store:
                tasks = TaskService(store)
                parent = software_contract()
                parent['test_adequacy'] = {'version': 1, 'required': True}
                task = tasks.create(parent)
                task = tasks.propose(task['task_id'], {'summary': 'Original software work', 'stage_ids': ['implement']}, expected_revision=task['revision'])
                task = tasks.approve(task['task_id'], proposal_hash=task['proposal_hash'], user_message_ref='test:owner', approved_stage_ids=['implement'])
                child = software_contract()
                child.update(id='child', parent_task_id=task['task_id'], parent_stage_id='implement')
                self.assertEqual(tasks.create(child)['contract'].get('test_adequacy'), parent['test_adequacy'])
                revised = tasks.revise(task['task_id'], software_contract(), expected_revision=task['revision'])
                self.assertEqual(revised['contract']['test_adequacy'], parent['test_adequacy'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
