"""PreToolUse guard decisions, project-only hook installation and captured prompt references."""
import importlib.util
import io
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


guard = _load('pretooluse_guard', ROOT / 'templates/hooks/pretooluse_guard.py')
installer = _load('install_agent_hooks', ROOT / 'scripts/install_agent_hooks.py')


class GuardTests(unittest.TestCase):
    def decide(self, command, branch='feature/x', ci_base=None):
        with patch.object(guard, '_current_branch', return_value=branch):
            return guard.check(command, '.', ci_base)

    def test_denies_default_branch_force_and_bypass(self):
        for command in ('git push origin main', 'git push origin HEAD:refs/heads/main', 'cd repo && git push',
                        'git push -f origin feature/x', 'git push origin +feature/x', 'git push --force-with-lease',
                        'git commit --no-verify -m x', 'gh pr merge 12 --squash', 'git -C other push origin master',
                        'gh api -X DELETE repos/o/r/branches/main/protection', 'git --git-dir=.git push origin main',
                        '(git push origin main)', 'bash -c "git push origin main"', 'git push --all origin',
                        'bash -c "cd r && git push origin main"', 'echo $(git push origin main)',
                        'sleep 1 & git push origin main'):
            with self.subTest(command=command):
                decision = self.decide(command, branch='main' if command == 'cd repo && git push' else 'feature/x')
                self.assertEqual(decision[0] if decision else None, 'deny')

    def test_allows_feature_pushes_and_ordinary_commands(self):
        for command in ('git push origin feature/x', 'git push -u origin HEAD', 'git status', 'gh pr view 3',
                        'python -m unittest', 'echo "git push origin main is blocked"',
                        'gh pr create --body "Docs: never run \\`git push --force\\`"',
                        "git commit -m 'explain `gh pr merge` and git push origin main; then stop'",
                        'git commit -m "notes: a && b; c | d"'):
            with self.subTest(command=command):
                self.assertIsNone(self.decide(command))

    def test_ci_base_runs_ci_check_before_push_and_pr(self):
        with patch.object(guard, '_ci', return_value=('deny', 'ci failed')) as ci:
            self.assertEqual(self.decide('gh pr create --fill', ci_base='origin/main'), ('deny', 'ci failed'))
            self.assertEqual(self.decide('git push origin feature/x', ci_base='origin/main'), ('deny', 'ci failed'))
            self.assertIsNone(self.decide('git status', ci_base='origin/main'))
        self.assertEqual(ci.call_count, 2)

    def test_only_fail_findings_deny_and_review_items_are_reported(self):
        def run(findings, stdout=None):
            body = stdout if stdout is not None else json.dumps({'status': 'FAIL', 'findings': findings}).encode()
            done = subprocess.CompletedProcess([], 2, stdout=body, stderr=b'')
            with patch.object(guard.subprocess, 'run', return_value=done):
                return guard._ci('.', 'origin/main', None)
        review = {'level': 'REVIEW', 'check': 'protected_paths', 'message': 'owner review', 'paths': ['AGENTS.md']}
        fail = {'level': 'FAIL', 'check': 'test_skip_added', 'message': 'skip added', 'paths': ['tests/t.py']}
        self.assertEqual(run([review])[0], 'warn')
        self.assertEqual(run([review, fail])[0], 'deny')
        self.assertIsNone(run([{'level': 'INFO', 'check': 'x', 'message': 'm', 'paths': []}]))
        self.assertEqual(run(None, stdout=b'{"status": "BLOCKED", "error": "bad ref"}')[0], 'deny')
        self.assertEqual(run(None, stdout='\u043e'.encode('cp1251'))[0], 'deny')
        with patch.object(guard.subprocess, 'run', side_effect=subprocess.TimeoutExpired('ase', 180)):
            self.assertEqual(guard._ci('.', 'origin/main', None)[0], 'deny')

    def test_hook_protocol_emits_deny_json(self):
        def stdin(value):
            # A cp1252 text layer must not matter: the hook decodes the raw bytes as UTF-8.
            return io.TextIOWrapper(io.BytesIO(json.dumps(value, ensure_ascii=False).encode('utf-8')), encoding='cp1252')
        event = {'tool_name': 'Bash', 'tool_input': {'command': 'git push origin main  # пуш'}, 'cwd': '.'}
        out = io.StringIO()
        with patch('sys.stdin', stdin(event)), redirect_stdout(out):
            self.assertEqual(guard.main([]), 0)
        self.assertEqual(json.loads(out.getvalue())['hookSpecificOutput']['permissionDecision'], 'deny')
        out = io.StringIO()
        with patch('sys.stdin', stdin({'tool_name': 'Read', 'tool_input': {}})), redirect_stdout(out):
            guard.main([])
        self.assertEqual(out.getvalue(), '')


@unittest.skipUnless(shutil.which('git'), 'git required')
class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.repo = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.repo, ignore_errors=True)
        subprocess.run(['git', 'init', '-q', str(self.repo)], check=True)

    def test_installs_into_project_settings_preserving_existing_hooks_once(self):
        settings = self.repo / '.claude' / 'settings.json'
        settings.parent.mkdir()
        existing = {'permissions': {'allow': ['Bash(ls)']},
                    'hooks': {'PreToolUse': [{'matcher': 'Edit', 'hooks': [{'type': 'command', 'command': 'lint'}]}]}}
        settings.write_text(json.dumps(existing), encoding='utf-8')
        first = installer.install(self.repo, ci_base='origin/main')
        second = installer.install(self.repo, ci_base='origin/main')
        self.assertEqual((first['added'], second['added']), (['PreToolUse', 'UserPromptSubmit'], []))
        data = json.loads(settings.read_text(encoding='utf-8'))
        self.assertEqual(data['permissions'], existing['permissions'])
        self.assertEqual(len(data['hooks']['PreToolUse']), 2)
        self.assertIn('--ci-base', data['hooks']['PreToolUse'][1]['hooks'][0]['command'])
        capture = data['hooks']['UserPromptSubmit'][0]['hooks'][0]['command']
        self.assertIn('prompt_capture.py', capture)
        self.assertNotIn('&&', capture)

    def test_prompt_capture_script_is_silent_even_on_failure(self):
        capture = _load('prompt_capture', ROOT / 'templates/hooks/prompt_capture.py')
        out = io.StringIO()
        with patch('sys.stdin', io.TextIOWrapper(io.BytesIO(b'not json'))), redirect_stdout(out):
            self.assertEqual(capture.main(['--repo', str(self.repo)]), 0)
        self.assertEqual(out.getvalue(), '')


class PromptReferenceTests(unittest.TestCase):
    def setUp(self):
        import sqlite3
        self.store = type('S', (), {})()
        self.store.db = sqlite3.connect(':memory:', isolation_level=None)
        self.store.db.execute('CREATE TABLE runtime_records(kind TEXT, key TEXT, revision INT, payload TEXT, updated TEXT, PRIMARY KEY(kind,key))')

    def test_each_submission_is_its_own_record_and_references_resolve(self):
        from ase import prompts
        first = prompts.capture(self.store, {'prompt': 'Да, пушь', 'session_id': 's1'})
        again = prompts.capture(self.store, {'prompt': 'Да, пушь', 'session_id': 's2'})
        self.assertNotEqual(first['id'], again['id'])
        self.assertEqual(first['sha256'], again['sha256'])
        self.assertEqual(prompts.resolve(self.store, first['id'])['text'], 'Да, пушь')
        self.assertIsNone(prompts.resolve(self.store, 'chat:free-text'))
        with self.assertRaisesRegex(ValueError, 'Unknown captured prompt'):
            prompts.resolve(self.store, 'prompt:0000000000000000')
        with self.assertRaises(ValueError):
            prompts.capture(self.store, {'prompt': '  '})
        self.assertEqual(prompts.recent(self.store)[0]['excerpt'], 'Да, пушь')


if __name__ == '__main__':
    unittest.main()
