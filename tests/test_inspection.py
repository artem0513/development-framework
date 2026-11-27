"""Real Git canaries retained from the retired coordinator, without its transport."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from ase import inspection


class InspectionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='ase-inspection-')
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.repo = self.base / 'selected'
        self.repo.mkdir()
        self.git('init', '-b', 'feature/inspection')
        self.git('config', 'user.name', 'Inspection Tests')
        self.git('config', 'user.email', 'inspection@example.invalid')
        (self.repo / 'app.py').write_text('value = 1\n')
        self.git('add', '.')
        self.git('commit', '-m', 'initial')

    def git(self, *args):
        return subprocess.run(['git', '-C', str(self.repo), *args], check=True,
                              capture_output=True, text=True).stdout

    def program(self, name, tail):
        marker = self.base / (name + '-executed.txt')
        program = self.repo / '.git' / (name + '.py')
        program.write_text('from pathlib import Path\nimport sys\n'
                           + 'Path(' + repr(str(marker)) + ').write_text("canary executed")\n' + tail)
        command = '"' + Path(sys.executable).as_posix() + '" "' + program.as_posix() + '"'
        return marker, command

    def test_nested_directory_is_not_a_separate_git_root(self):
        nested = self.repo / 'nested'
        nested.mkdir()
        from contextlib import closing
        from ase.publishing import Publisher
        from ase.store import Store
        self.git('remote', 'add', 'origin', str(self.base / 'remote.git'))
        with closing(Store(nested, self.base / 'state')) as store:
            with self.assertRaisesRegex(ValueError, 'repository root'):
                Publisher(store).authorize('origin')
            self.assertFalse((store.root / 'publishing.json').exists())

    def test_safe_git_reads_do_not_execute_repository_fsmonitor(self):
        marker, command = self.program('fsmonitor', "sys.stdout.buffer.write(b'token\\x00')\n")
        self.git('config', 'core.fsmonitor', command)
        self.git('status', '--porcelain')
        self.assertTrue(marker.exists(), 'Control did not exercise the real Git helper')
        marker.unlink()
        inspection.safe_snapshot(self.repo)
        inspection.safe_git(self.repo, 'status', '--porcelain')
        self.assertFalse(marker.exists())

    def test_safe_diff_does_not_execute_repository_textconv(self):
        marker, command = self.program('textconv', "print('converted source')\n")
        self.git('config', 'diff.canary.textconv', command)
        (self.repo / '.gitattributes').write_text('app.py diff=canary\n')
        (self.repo / 'app.py').write_text('value = 2\n')
        self.git('diff', '--textconv')
        self.assertTrue(marker.exists(), 'Control did not exercise the real Git helper')
        marker.unlink()
        inspection.safe_git(self.repo, 'diff', 'HEAD')
        inspection.safe_snapshot(self.repo)
        self.assertFalse(marker.exists())

    def test_safe_diff_does_not_execute_repository_clean_filter(self):
        marker, command = self.program('clean', 'sys.stdout.write(sys.stdin.read())\n')
        self.git('config', 'filter.canary.clean', command)
        (self.repo / '.gitattributes').write_text('app.py filter=canary\n')
        (self.repo / 'app.py').write_text('value = 2\n')
        self.git('diff')
        self.assertTrue(marker.exists(), 'Control did not exercise the real Git helper')
        marker.unlink()
        inspection.safe_git(self.repo, 'diff', 'HEAD')
        inspection.safe_snapshot(self.repo)
        self.assertFalse(marker.exists())

    def test_safe_reads_do_not_execute_repository_index_hook(self):
        marker, command = self.program('index-hook', '')
        hook = self.repo / '.git/hooks/post-index-change'
        hook.write_text('#!/bin/sh\n' + command + '\n', encoding='utf-8')
        hook.chmod(0o755)
        (self.repo / 'app.py').write_text('value = 2\n')
        self.git('add', 'app.py')
        self.assertTrue(marker.exists(), 'Control did not exercise the real Git hook')
        marker.unlink()
        inspection.safe_git(self.repo, 'status', '--porcelain')
        inspection.safe_git(self.repo, 'diff', '--cached')
        inspection.safe_snapshot(self.repo)
        self.assertFalse(marker.exists())

    def test_only_supported_read_operations_are_accepted(self):
        before = inspection.safe_snapshot(self.repo)
        for args in (('commit', '-am', 'unauthorized'), ('push',),
                     ('diff', '--ext-diff'), ('status', '--ignored'),
                     ('diff', '--', '../outside')):
            with self.subTest(args=args), self.assertRaises(ValueError):
                inspection.safe_git(self.repo, *args)
        after = inspection.safe_snapshot(self.repo)
        self.assertEqual(before['fingerprint'], after['fingerprint'])
        self.assertEqual(before['commit'], after['commit'])

    def write(self, relative, data=b'\x00bytecode'):
        path = self.repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def test_untracked_bytecode_cache_after_baseline_is_not_source(self):
        # No .gitignore: running the project's tests leaves untracked __pycache__.
        before = inspection.safe_snapshot(self.repo)
        self.write('__pycache__/app.cpython-315.pyc')
        self.write('pkg/__pycache__/mod.cpython-315.opt-1.pyc')
        self.write('pkg/__pycache__/old.cpython-34.pyo')
        self.assertIn('?? __pycache__/', self.git('status', '--porcelain'))
        after = inspection.safe_snapshot(self.repo)
        self.assertEqual(after['fingerprint'], before['fingerprint'])
        self.assertFalse(any('__pycache__' in path for path in after['files']))

    def test_tracked_bytecode_and_other_untracked_files_remain_source(self):
        tracked = self.write('pkg/__pycache__/kept.cpython-315.pyc')
        self.git('add', 'pkg/__pycache__/kept.cpython-315.pyc')
        self.git('commit', '-m', 'track bytecode')
        before = inspection.safe_snapshot(self.repo)
        self.assertIn('pkg/__pycache__/kept.cpython-315.pyc', before['files'])
        tracked.write_bytes(b'\x00changed bytecode')
        self.assertNotEqual(inspection.safe_snapshot(self.repo)['fingerprint'], before['fingerprint'])
        tracked.write_bytes(b'\x00bytecode')
        self.assertEqual(inspection.safe_snapshot(self.repo)['fingerprint'], before['fingerprint'])
        # Sourceless .pyc outside __pycache__ is importable; other cache-dir files are not bytecode.
        for relative in ('sourceless.pyc', 'pkg/__pycache__/helper.py', 'notes.txt'):
            with self.subTest(relative=relative):
                created = self.write(relative)
                observed = inspection.safe_snapshot(self.repo)
                self.assertIn(relative, observed['files'])
                self.assertNotEqual(observed['fingerprint'], before['fingerprint'])
                created.unlink()

    def test_task_source_snapshot_and_required_directory_inventory_ignore_untracked_bytecode(self):
        from contextlib import closing
        from ase.gates import _directory_sources, source_snapshot
        from ase.store import Store
        tracked = self.write('pkg/__pycache__/kept.cpython-315.pyc')
        self.git('add', '.')
        self.git('commit', '-m', 'track bytecode')
        contract = {'profile': 'software', 'scope': {'resources': ['app.py', 'pkg']}}
        with closing(Store(self.repo, self.base / 'state')) as store:
            before = source_snapshot(store, contract)
            self.write('pkg/__pycache__/mod.cpython-315.pyc')
            self.assertEqual(source_snapshot(store, contract)['fingerprint'], before['fingerprint'])
            tracked.write_bytes(b'\x00changed bytecode')
            self.assertNotEqual(source_snapshot(store, contract)['fingerprint'], before['fingerprint'])
            retained = inspection.safe_snapshot(self.repo)['files']
        inventory = {path.relative_to(self.repo).as_posix() for path in _directory_sources(self.repo, self.repo / 'pkg', retained=retained)}
        self.assertEqual(inventory, {'pkg/__pycache__/kept.cpython-315.pyc'})
        # Without a Git inventory nothing is presumed untracked.
        inventory = {path.relative_to(self.repo).as_posix() for path in _directory_sources(self.repo, self.repo / 'pkg')}
        self.assertEqual(inventory, {'pkg/__pycache__/kept.cpython-315.pyc', 'pkg/__pycache__/mod.cpython-315.pyc'})

    def commit_lf_app(self):
        # safe_git ignores the host's global attributes, so commit bytes that need no EOL conversion.
        (self.repo / 'app.py').write_bytes(b'value = 1\n')
        self.git('add', 'app.py')
        self.git('commit', '--allow-empty', '-m', 'lf')

    def test_source_status_omits_only_untracked_bytecode(self):
        def status():
            return inspection.source_status(inspection.safe_git(self.repo, 'status', *inspection.SOURCE_STATUS))
        self.commit_lf_app()
        self.assertEqual(status(), '')
        self.write('pkg/__pycache__/mod.cpython-315.pyc')
        self.assertEqual(status(), '')
        self.write('pkg/__pycache__/helper.py')
        self.assertEqual(status(), '?? pkg/__pycache__/helper.py')
        tracked = self.write('pkg/__pycache__/helper.py')
        self.git('add', 'pkg/__pycache__/helper.py', 'pkg/__pycache__/mod.cpython-315.pyc')
        self.git('commit', '-m', 'track cache dir files')
        tracked.unlink()
        (self.repo / 'pkg/__pycache__/mod.cpython-315.pyc').write_bytes(b'\x00changed')
        self.assertEqual(sorted(entry.strip() for entry in status().split('\0')),
                         ['D pkg/__pycache__/helper.py', 'M pkg/__pycache__/mod.cpython-315.pyc'])

    def test_task_baseline_dirty_ignores_only_untracked_bytecode(self):
        from contextlib import closing
        from ase.state import read_record
        from ase.store import Store
        from ase.tasks import record_baseline
        contract = {'profile': 'software', 'scope': {'resources': ['app.py']}}
        self.commit_lf_app()
        self.write('pkg/__pycache__/mod.cpython-315.pyc')
        with closing(Store(self.repo, self.base / 'state')) as store:
            self.assertEqual(record_baseline(store, 'clean', contract)['dirty'], '')
            self.write('pkg/helper.py', b'x = 1\n')
            self.assertEqual(record_baseline(store, 'dirty', contract)['dirty'], '?? pkg/helper.py')
            self.assertEqual(read_record(store.db, 'baseline', 'clean')['payload']['dirty'], '')

    def test_launched_python_ignores_repository_bytecode_caches(self):
        import py_compile
        from ase.core import execute
        (self.repo / 'mod.py').write_text('V = "reviewed"\n')
        forged = self.base / 'forged.py'
        forged.write_text('V = "unreviewed bytecode"\n')
        # Explicit in-repo path: cache_from_source would follow this process's own PYTHONPYCACHEPREFIX.
        cache = self.repo / '__pycache__' / ('mod.' + sys.implementation.cache_tag + '.pyc')
        py_compile.compile(str(forged), cfile=str(cache),
                           invalidation_mode=py_compile.PycInvalidationMode.UNCHECKED_HASH, doraise=True)
        argv = [sys.executable, '-c', 'import mod; print(mod.V)']
        control = subprocess.run(argv, cwd=self.repo, capture_output=True, text=True,
                                 env={k: v for k, v in os.environ.items() if k != 'PYTHONPYCACHEPREFIX'})
        self.assertEqual(control.stdout.strip(), 'unreviewed bytecode', 'Control did not load the planted cache')
        result = execute(argv, self.repo, self.base / 'logs' / 'run.log')
        self.assertEqual(result['exit_code'], 0, result)
        self.assertIn('reviewed', (self.base / 'logs' / 'run.log').read_text())
        self.assertNotIn('unreviewed', (self.base / 'logs' / 'run.log').read_text())


if __name__ == '__main__':
    unittest.main()
