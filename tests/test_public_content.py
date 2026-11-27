"""The framework's tracked files carry no trace of real machines, mailboxes or run outputs."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import check_public_content as check  # noqa: E402
import install_hooks  # noqa: E402

# Fixture values are assembled at run time so this file itself stays clean.
WINDOWS_HOME = 'C:' + '\\Users\\' + 'someone' + '\\repo'
GIT_BASH_HOME = '/c/' + 'Users/' + 'someone/repo'
LINUX_HOME = 'cd /' + 'home/' + 'someone/repo'
MAC_HOME = 'open /' + 'Users/' + 'someone/repo'
REAL_EMAIL = 'someone' + '@' + 'mail.org'


class RepositoryTests(unittest.TestCase):
    def test_tracked_files_pass_generic_checks(self):
        hits = check.scan(ROOT)
        self.assertEqual([], hits, 'public-content leaks:\n' + '\n'.join(f'{p}:{n}: {c}: {m}' for p, n, c, m in hits))


class DetectorTests(unittest.TestCase):
    def test_home_paths(self):
        for line in (WINDOWS_HOME, GIT_BASH_HOME, LINUX_HOME, MAC_HOME, WINDOWS_HOME.replace('\\', '/')):
            with self.subTest(line=line):
                self.assertEqual(['home-path'], [c for c, _ in check.line_hits(line)])

    def test_placeholders_and_generic_paths_pass(self):
        for line in ('C:\\Users\\<name>\\repo', '~/.config/x', 'C:\\Work\\MyProject', '$HOME/x',
                     'https://example.com/home/page', 'docs/Users/guide.md', '%USERPROFILE%\\x', 'C:\\Users\\Public'):
            with self.subTest(line=line):
                self.assertEqual([], check.line_hits(line))

    def test_emails(self):
        self.assertEqual([('email', REAL_EMAIL)], check.line_hits('mail ' + REAL_EMAIL))
        for line in ('a@example.com', 'b@example.org', '1+x@users.noreply.github.com', 'c@example.invalid',
                     'git@github.com:owner/repo.git', "'import pytest\\n@pytest.mark.skip'", '@types/node'):
            with self.subTest(line=line):
                self.assertEqual([], check.line_hits(line))

    def test_allow_marker_exempts_one_line(self):
        self.assertEqual([], check.line_hits(REAL_EMAIL + '  # ' + check.ALLOW_MARKER))

    def test_vendored_upstream_credits_skip_only_email(self):
        self.assertEqual([], check.line_hits(REAL_EMAIL, vendored=True))
        self.assertEqual(['home-path'], [c for c, _ in check.line_hits(LINUX_HOME, vendored=True)])


class ScanTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        self.git('init', '-q')
        (self.repo / 'outputs').mkdir()
        (self.repo / 'outputs' / 'run.txt').write_text('transcript\n', encoding='utf-8')
        (self.repo / 'notes.md').write_text('one\nsee ' + LINUX_HOME + '\nAcmeWidget rollout\n', encoding='utf-8')
        self.git('add', '-A')
        self.git('-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-q', '-m', 'f')

    def tearDown(self):
        self.tmp.cleanup()

    def git(self, *args):
        subprocess.run(['git', '-C', str(self.repo), *args], check=True, capture_output=True)

    def run_check(self, *args, denylist=None):
        env = dict(os.environ)
        env.pop(check.DENYLIST_ENV, None)
        if denylist is not None:
            env[check.DENYLIST_ENV] = str(denylist)
        return subprocess.run([sys.executable, '-B', str(ROOT / 'scripts' / 'check_public_content.py'),
                               '--repo', str(self.repo), *args], capture_output=True, text=True, env=env)

    def test_commit_index_and_work_tree_report_file_line_and_text(self):
        for mode in ((), ('--index',), ('--commit', 'HEAD')):
            with self.subTest(mode=mode):
                hits = check.scan(self.repo, *((mode[1],) if mode and mode[0] == '--commit' else ()),
                                  index=mode == ('--index',))
                self.assertIn(('outputs/run.txt', 0, 'tracked-outputs', 'outputs/run.txt'), hits)
                self.assertIn(('notes.md', 2, 'home-path', '/' + 'home/someone'), hits)

    def test_own_checkout_path_is_reported_in_any_spelling(self):
        location = str(self.repo.resolve())
        escaped = location.replace('\\', '\\\\')
        (self.repo / 'log.json').write_text('{"p": "' + escaped + '\\\\out.txt"}\n' + location.replace('\\', '/') + '\n',
                                            encoding='utf-8')
        self.git('add', 'log.json')
        checks = [(n, c) for p, n, c, _ in check.scan(self.repo, index=True) if p == 'log.json' and c == 'checkout-path']
        self.assertEqual([(1, 'checkout-path'), (2, 'checkout-path')], checks)

    def test_private_denylist_hits_never_print_the_pattern(self):
        denylist = self.repo.parent / (self.repo.name + '-deny.txt')
        denylist.write_text('# comment\nacme(?:widget)\n', encoding='utf-8')
        self.addCleanup(denylist.unlink)
        done = self.run_check('--commit', 'HEAD', denylist=denylist)
        self.assertEqual(1, done.returncode)
        self.assertIn('notes.md:3: private-denylist: AcmeWidget', done.stdout)
        self.assertNotIn('(?:', done.stdout + done.stderr)
        self.assertNotIn('AcmeWidget', self.run_check('--no-denylist').stdout)

    def test_missing_or_invalid_denylist_is_a_configuration_error(self):
        missing = self.repo.parent / (self.repo.name + '-absent.txt')
        self.assertEqual(2, self.run_check(denylist=missing).returncode)
        broken = self.repo.parent / (self.repo.name + '-broken.txt')
        broken.write_text('secret(\n', encoding='utf-8')
        self.addCleanup(broken.unlink)
        done = self.run_check(denylist=broken)
        self.assertEqual(2, done.returncode)
        self.assertIn('line 1', done.stderr)
        self.assertNotIn('secret', done.stderr)

    def test_clean_repository_exits_zero(self):
        self.git('rm', '-q', '-r', '--cached', 'outputs')
        (self.repo / 'notes.md').write_text('clean\n', encoding='utf-8')
        self.git('add', 'notes.md')
        done = self.run_check('--index', '--no-denylist')
        self.assertEqual(0, done.returncode, done.stdout)


class HookTests(unittest.TestCase):
    def test_hooks_require_the_denylist_and_preserve_foreign_hooks(self):
        with tempfile.TemporaryDirectory() as tmp:
            subprocess.run(['git', '-C', tmp, 'init', '-q'], check=True)
            hooks = Path(tmp) / '.git' / 'hooks'
            (hooks / 'pre-push').write_text('#!/bin/sh\necho mine\n', encoding='utf-8')
            written, kept = install_hooks.install(Path(tmp))
            self.assertEqual((['pre-commit'], ['pre-push']), (written, kept))
            text = (hooks / 'pre-commit').read_text(encoding='utf-8')
            self.assertIn('--require-denylist', text)
            self.assertIn('--index', text)
            self.assertEqual('#!/bin/sh\necho mine\n', (hooks / 'pre-push').read_text(encoding='utf-8'))
            (hooks / 'pre-push').unlink()
            self.assertEqual((['pre-commit', 'pre-push'], []), install_hooks.install(Path(tmp)))
            self.assertIn('--commit "$local_sha"', (hooks / 'pre-push').read_text(encoding='utf-8'))


class PublishTests(unittest.TestCase):
    def test_refuses_a_non_noreply_author(self):
        import publish_public
        with patch.object(publish_public, 'ident', return_value=('Someone', REAL_EMAIL)):
            with self.assertRaises(SystemExit) as raised:
                publish_public.require_noreply(ROOT)
        self.assertIn('Refused', str(raised.exception))

    def test_export_drops_outputs(self):
        import io
        import tarfile
        import publish_public
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode='w') as tar:
            for name in ('README.md', 'outputs/run.txt'):
                info = tarfile.TarInfo(name)
                info.size = 1
                tar.addfile(info, io.BytesIO(b'x'))
        with tempfile.TemporaryDirectory() as tmp:
            publish_public.export(buffer.getvalue(), Path(tmp))
            self.assertEqual(['README.md'], sorted(p.name for p in Path(tmp).iterdir()))


if __name__ == '__main__':
    unittest.main()
