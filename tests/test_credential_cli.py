import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


@unittest.skipUnless(os.name == 'nt', 'requires Windows DPAPI')
class CredentialCLITests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name); self.home = self.root / 'home'
        self.path = self.root / 'import.json'
        self.entry = {'id': 'lab', 'target': 'https://example.com', 'kind': 'http-bearer',
                      'secrets': {'token': 'cli-token-canary'}, 'env': {'LAB_TOKEN': 'token'}}
        self.path.write_text(json.dumps({'schema_version': 1, 'credentials': [self.entry]}), encoding='utf-8')

    def cli(self, *args):
        return subprocess.run([sys.executable, '-m', 'ase', '--home', str(self.home), *args], capture_output=True, text=True)

    def test_import_list_and_configure_without_creating_project(self):
        before = self.path.read_bytes()
        result = self.cli('credentials', 'import', '--file', str(self.path))
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertNotIn('cli-token-canary', result.stdout + result.stderr)
        self.assertEqual(self.path.read_bytes(), before)
        result = self.cli('credentials', 'list')
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(json.loads(result.stdout)['credentials'][0]['revision'], 1)
        config = self.root / 'config.json'; config.write_text('{"env": {}}', encoding='utf-8')
        result = self.cli('credentials', 'configure', 'lab', '--target', 'https://example.com', '--file', str(config))
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(json.loads(result.stdout)['revision'], 2)
        self.assertFalse((self.home / 'registry.sqlite3').exists())
        self.assertFalse((self.home / 'projects').exists())

    def test_errors_never_echo_input_or_path(self):
        bad = self.root / 'path-canary.json'; bad.write_text('content-canary', encoding='utf-8')
        result = self.cli('credentials', 'import', '--file', str(bad))
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('canary', result.stdout + result.stderr)
        result = self.cli('credentials', 'configure', 'lab', '--target', 'secret-canary', '--file', str(bad))
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('canary', result.stdout + result.stderr)
        result = self.cli('access', 'request', 'task1', '--stage', 's', '--credential', 'lab', '--target', 'https://example.com', '--method', 'method-canary')
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('canary', result.stdout + result.stderr)

    def test_access_routes_exact_references_to_workflow_controller(self):
        import contextlib
        import io
        from unittest.mock import MagicMock, patch
        from ase.__main__ import main
        store = MagicMock(); controller = MagicMock()
        controller.ensure_access.return_value = {'status': 'READY'}
        controller.request_access.return_value = {'status': 'READY'}
        with patch('ase.__main__.Store', return_value=store), patch('ase.__main__.WorkflowController', return_value=controller), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(['--home', str(self.home), 'access', 'ensure', 'task1', '--stage', 's1', '--credential', 'lab', '--target', 'https://example.com']), 0)
            self.assertEqual(main(['--home', str(self.home), 'access', 'request', 'task1', '--stage', 's1', '--credential', 'lab', '--target', 'https://example.com', '--path', '/health', '--method', 'HEAD']), 0)
        access = {'credential_ref': 'lab', 'target': 'https://example.com'}
        controller.ensure_access.assert_called_once_with('task1', 's1', access)
        controller.request_access.assert_called_once_with('task1', 's1', access, path='/health', method='HEAD')

    def test_import_rejects_file_inside_non_git_workspace(self):
        repo = self.root / 'workspace'; repo.mkdir()
        path = repo / 'sensitive.json'
        path.write_text(json.dumps({'schema_version': 1, 'credentials': [self.entry]}), encoding='utf-8')
        result = self.cli('--repo', str(repo), 'credentials', 'import', '--file', str(path))
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('cli-token-canary', result.stdout + result.stderr)
        self.assertFalse((self.home / 'credentials' / 'vault.json').exists())

    def test_non_git_workspace_cannot_be_vault_home(self):
        repo = self.root / 'workspace'; repo.mkdir()
        command = [sys.executable, '-m', 'ase', '--repo', str(repo), '--home', str(repo / 'state'), 'credentials', 'import', '--file', str(self.path)]
        result = subprocess.run(command, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((repo / 'state').exists())

    def test_public_access_approval_and_child_environment(self):
        repo = self.root / 'work'; repo.mkdir()
        subprocess.run(['git', 'init', str(repo)], check=True, capture_output=True)
        (repo / 'readme.txt').write_text('disposable fixture', encoding='utf-8')
        subprocess.run(['git', '-C', str(repo), 'add', 'readme.txt'], check=True, capture_output=True)
        subprocess.run(['git', '-C', str(repo), '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.test', 'commit', '-m', 'fixture'], check=True, capture_output=True)
        self.entry['kind'] = 'environment'
        self.path.write_text(json.dumps({'schema_version': 1, 'credentials': [self.entry]}), encoding='utf-8')
        def run(*args):
            result = self.cli('--repo', str(repo), *args)
            self.assertNotIn('cli-token-canary', result.stdout + result.stderr)
            return result
        self.assertEqual(run('credentials', 'import', '--file', str(self.path)).returncode, 0)
        access = {'credential_ref': 'lab', 'target': 'https://example.com'}
        scope = {'resources': ['https://example.com'], 'effects': ['command']}
        contract = {'id': 'access-task', 'schema_version': 2, 'profile': 'software',
                    'request': 'Check a disposable service environment.', 'scope': scope,
                    'acceptance': [{'id': 'AC1', 'expected': 'Check succeeds', 'mandatory': True, 'evidence_ids': ['env-check']}],
                    'evidence': [{'id': 'env-check', 'kind': 'command', 'required': True,
                                  'argv': [sys.executable, '-c', "import os; print(os.environ['LAB_TOKEN'])"], 'access': access}],
                    'stages': [{'id': 'observe', 'scope': scope, 'acceptance_ids': ['AC1'], 'depends_on': []}], 'unknowns': []}
        source = self.root / 'task.json'; source.write_text(json.dumps(contract), encoding='utf-8')
        created = run('task', 'create', '--file', str(source))
        self.assertEqual(created.returncode, 0, created.stdout)
        task = json.loads(created.stdout)
        args = ('access', 'ensure', 'access-task', '--stage', 'observe', '--credential', 'lab', '--target', 'https://example.com')
        self.assertNotEqual(run(*args).returncode, 0)
        proposal = self.root / 'proposal.json'; proposal.write_text(json.dumps({'summary': 'Run the disposable environment check.', 'stage_ids': ['observe']}), encoding='utf-8')
        proposed = run('task', 'propose', 'access-task', '--file', str(proposal), '--expected-revision', str(task['revision']))
        self.assertEqual(proposed.returncode, 0, proposed.stdout)
        task = json.loads(proposed.stdout)
        approved = run('task', 'approve', 'access-task', '--proposal-hash', task['proposal_hash'], '--stage', 'observe', '--user-message-ref', 'test:approval')
        self.assertEqual(approved.returncode, 0, approved.stdout)
        ensured = run(*args)
        self.assertEqual(ensured.returncode, 0, ensured.stdout)
        self.assertEqual(json.loads(ensured.stdout)['status'], 'prepared')
        denied = run('access', 'ensure', 'access-task', '--stage', 'observe', '--credential', 'lab', '--target', 'https://other.example')
        self.assertNotEqual(denied.returncode, 0)
        checked = run('check', 'access-task')
        self.assertEqual(checked.returncode, 0, checked.stdout)
        self.assertNotIn('cli-token-canary', checked.stdout)
        result = json.loads(checked.stdout)['results'][0]
        self.assertEqual(result['id'], 'env-check')
        self.assertEqual(result['exit_code'], 0)
        self.assertEqual(result['access']['revision'], 1)
        for path in self.home.rglob('*'):
            if path.is_file():
                self.assertNotIn(b'cli-token-canary', path.read_bytes(), str(path))
