"""Deterministic CI merge checks against a real temporary Git repository."""
import io
import json
import os
import re
import shutil
import sys
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from ase import ci_check
from ase.__main__ import main


def working_bash():
    """Git for Windows bash first, then PATH bash; each must actually run a script."""
    candidates = []
    git = shutil.which('git')
    if git and os.name == 'nt':
        candidates.append(Path(git).resolve().parents[1] / 'bin' / 'bash.exe')
    if shutil.which('bash'):
        candidates.append(Path(shutil.which('bash')))
    for bash in candidates:
        try:
            done = subprocess.run([str(bash), '-c', 'echo ok'], capture_output=True, timeout=30)
        except (OSError, subprocess.SubprocessError):
            continue
        if done.returncode == 0 and done.stdout.strip() == b'ok':
            return str(bash)
    return None


@unittest.skipUnless(shutil.which('git'), 'git required')
class CiCheckTests(unittest.TestCase):
    def setUp(self):
        self.repo = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.repo, ignore_errors=True)
        self.git('init', '-q', '-b', 'main')
        self.git('config', 'user.email', 'ci@example.invalid')
        self.git('config', 'user.name', 'ci')
        self.write('app.py', 'def add(a, b):\n    return a + b\n')
        self.write('tests/test_app.py', 'import unittest\nclass T(unittest.TestCase):\n    def test_add(self):\n        self.assertEqual(1, 1)\n        self.assertTrue(True)\n')
        self.write('tests/test_other.py', 'def test_x():\n    assert True\n')
        self.write('shared/jobs.py', 'JOBS = []\n')
        self.write('.devframework/protected-paths.txt', 'shared/\n')
        self.commit('base')
        self.git('checkout', '-q', '-b', 'feature')

    def git(self, *args):
        return subprocess.run(['git', '-C', str(self.repo), *args], check=True, capture_output=True).stdout.decode()

    def write(self, rel, content):
        path = self.repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding='utf-8', newline='\n')

    def commit(self, message):
        self.git('add', '-A')
        self.git('commit', '-q', '-m', message)

    def checks(self, result):
        return {row['check']: row for row in result['findings']}

    def test_clean_change_with_test_passes(self):
        self.write('app.py', 'def add(a, b):\n    return a + b\n\n\ndef sub(a, b):\n    return a - b\n')
        self.write('tests/test_app.py', (self.repo / 'tests/test_app.py').read_text() + '    def test_sub(self):\n        self.assertEqual(0, 0)\n')
        self.commit('feature')
        result = ci_check.run(self.repo, 'main')
        self.assertEqual(result['status'], 'PASS', result)

    def test_deleted_tests_and_added_skips_fail(self):
        (self.repo / 'tests/test_other.py').unlink()
        self.write('tests/test_app.py', 'import unittest\nclass T(unittest.TestCase):\n    @unittest.skip("flaky")\n    def test_add(self):\n        self.assertEqual(1, 1)\n        self.assertTrue(True)\n')
        self.commit('tamper')
        result = ci_check.run(self.repo, 'main')
        found = self.checks(result)
        self.assertEqual(result['status'], 'FAIL')
        self.assertEqual(found['test_deleted']['paths'], ['tests/test_other.py'])
        self.assertEqual(found['test_deleted']['level'], 'REVIEW')
        self.assertEqual(found['test_skip_added']['level'], 'REVIEW')
        overridden = self.checks(ci_check.run(self.repo, 'main', allow_test_skip=True, allow_test_removal=True))
        self.assertEqual((overridden['test_skip_added']['level'], overridden['test_deleted']['level']), ('INFO', 'INFO'))
        self.assertEqual(self.checks(ci_check.run(self.repo, 'main', allow_test_removal=True))['test_deleted']['level'], 'INFO')
        self.assertEqual(found['test_skip_added']['paths'], ['tests/test_app.py'])

    def test_moved_tests_are_not_deleted_and_skiptest_is_a_skip(self):
        (self.repo / 'tests/unit').mkdir()
        self.git('mv', 'tests/test_other.py', 'tests/unit/test_other.py')
        self.write('tests/test_app.py', (self.repo / 'tests/test_app.py').read_text()
                   + '    def test_later(self):\n        self.skipTest("later")\n')
        self.commit('move')
        found = self.checks(ci_check.run(self.repo, 'main'))
        self.assertNotIn('test_deleted', found)
        self.assertEqual(found['test_skip_added']['paths'], ['tests/test_app.py'])

    def test_moved_test_keeps_its_existing_skips_but_lost_assertions_count(self):
        self.write('tests/test_skipped.py', 'import unittest\nclass S(unittest.TestCase):\n    def test_a(self):\n'
                   '        self.skipTest("platform")\n        self.assertTrue(1)\n        self.assertTrue(2)\n'
                   '        self.assertTrue(3)\n        self.assertTrue(4)\n')
        self.commit('skipped test on base')
        self.git('branch', '-f', 'main', 'HEAD')
        (self.repo / 'tests/unit').mkdir()
        self.git('mv', 'tests/test_skipped.py', 'tests/unit/test_skipped.py')
        self.commit('move only')
        found = self.checks(ci_check.run(self.repo, 'main'))
        self.assertNotIn('test_skip_added', found)
        self.assertNotIn('test_deleted', found)
        moved = self.repo / 'tests/unit/test_skipped.py'
        moved.write_text(moved.read_text().replace('        self.assertTrue(4)\n', ''), encoding='utf-8', newline='\n')
        self.commit('move and weaken')
        self.assertEqual(self.checks(ci_check.run(self.repo, 'main'))['assertions_removed']['paths'], ['tests/unit/test_skipped.py'])

    def test_template_step_fails_the_job_when_ci_check_fails(self):
        bash = working_bash()
        if not bash:
            self.skipTest('a working bash is required (the WindowsApps WSL stub without a distribution does not count)')
        root = Path(__file__).resolve().parents[1]
        template = (root / 'templates/ci/ase-merge-check.yml').read_text(encoding='utf-8')
        self.assertIn('shell: bash', template)
        script = re.search(r'name: Deterministic merge checks.*?run: \|\n(.*?)\n      - name:', template, re.S).group(1)
        script = (script.replace('${{ inputs.max_lines }}', '2000').replace('${{ inputs.max_files }}', '60')
                  .replace('$GITHUB_WORKSPACE/framework', root.as_posix())
                  .replace('$GITHUB_WORKSPACE/project', self.repo.as_posix())
                  .replace('"origin/$BASE_REF"', '"$BASE_REF"')
                  .replace('python -m ase', '"' + Path(sys.executable).as_posix() + '" -m ase'))
        self.write('tests/test_other.py', 'import pytest\n@pytest.mark.skip\ndef test_x():\n    assert True\n')
        self.commit('skip')
        summary = self.repo.parent / (self.repo.name + '-summary.md')
        self.addCleanup(lambda: summary.unlink(missing_ok=True))
        env = {**os.environ, 'BASE_REF': 'main', 'ALLOW_PROTECTED': 'false', 'ALLOW_LARGE': 'false',
               'ALLOW_TEST_REMOVAL': 'false', 'ALLOW_TEST_SKIP': 'false', 'GITHUB_STEP_SUMMARY': str(summary)}
        done = subprocess.run([bash, '-e', '-c', script], env=env, capture_output=True)
        self.assertNotEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn(b'FAIL', done.stdout)

    def test_removed_assertions_need_review(self):
        self.write('tests/test_app.py', 'import unittest\nclass T(unittest.TestCase):\n    def test_add(self):\n        pass\n')
        self.commit('weaken')
        found = self.checks(ci_check.run(self.repo, 'main'))
        self.assertEqual(found['assertions_removed']['level'], 'REVIEW')

    def test_protected_rules_come_from_base_so_branch_cannot_unprotect(self):
        self.write('.devframework/protected-paths.txt', '# nothing protected now\n')
        self.write('shared/jobs.py', 'JOBS = ["new"]\n')
        self.commit('unprotect')
        result = ci_check.run(self.repo, 'main')
        paths = self.checks(result)['protected_paths']['paths']
        self.assertIn('shared/jobs.py', paths)
        self.assertIn('.devframework/protected-paths.txt', paths)
        self.assertEqual(result['status'], 'FAIL')
        overridden = ci_check.run(self.repo, 'main', allow_protected=True)
        self.assertEqual(self.checks(overridden)['protected_paths']['level'], 'INFO')

    def test_diff_budget_and_dependency_report(self):
        self.write('requirements.txt', 'requests==2.32.3\n')
        self.write('big.py', ''.join(f'x{i} = {i}\n' for i in range(30)))
        self.commit('big')
        result = ci_check.run(self.repo, 'main', max_lines=20)
        found = self.checks(result)
        self.assertEqual(found['diff_budget']['level'], 'REVIEW')
        self.assertEqual(result['dependency_lines'], [{'manifest': 'requirements.txt', 'line': 'requests==2.32.3'}])
        self.assertEqual(self.checks(ci_check.run(self.repo, 'main', max_lines=20, allow_large=True))['diff_budget']['level'], 'INFO')

    def test_cli_exit_codes(self):
        self.write('tests/test_other.py', 'import pytest\n@pytest.mark.skip\ndef test_x():\n    assert True\n')
        self.commit('skip')
        out = io.StringIO()
        with redirect_stdout(out):
            code = main(['--repo', str(self.repo), 'ci-check', '--base', 'main'])
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(out.getvalue())['status'], 'FAIL')
        with redirect_stdout(io.StringIO()):
            self.assertEqual(main(['--repo', str(self.repo), 'ci-check', '--base', 'missing-ref']), 3)


if __name__ == '__main__':
    unittest.main()
