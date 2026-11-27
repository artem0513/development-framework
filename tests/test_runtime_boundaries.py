"""Direct local runtime boundaries, replacing coordinator-only wrappers."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from ase.store import Store
from ase.tasks import TaskService
from ase.workflow import WorkflowController
from ase.runner import _write
from test_task_contract_v2 import contract


ROOT = Path(__file__).resolve().parents[1]


def directory_link(link, target):
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        if os.name != 'nt':
            raise
        result = subprocess.run(['cmd', '/c', 'mklink', '/J', str(link), str(target)],
                                capture_output=True, text=True)
        if result.returncode:
            raise RuntimeError(result.stdout + result.stderr)


class RuntimeBoundaryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='ase-direct-boundary-')
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.repo, self.home = self.base / 'work', self.base / 'state'
        self.repo.mkdir()
        self.store = Store(self.repo, self.home)
        self.addCleanup(self.store.close)
        tasks = TaskService(self.store)
        task = tasks.create(contract())
        task = tasks.propose(task['task_id'], {'summary': 'Draft the selected guide', 'stage_ids': ['draft']}, expected_revision=task['revision'])
        tasks.approve(task['task_id'], proposal_hash=task['proposal_hash'], user_message_ref='user:approved', approved_stage_ids=['draft'])
        (self.repo / 'guide.md').write_text('Confirmed workflow\n')

    def test_project_state_junction_cannot_redirect_reopened_store(self):
        project_state = self.store.root
        self.store.close()
        outside = self.base / 'outside-project-state'
        project_state.rename(outside)
        directory_link(project_state, outside)
        before = {p.relative_to(outside).as_posix(): p.read_bytes() for p in outside.rglob('*') if p.is_file()}
        with self.assertRaisesRegex(ValueError, 'reparse|project directory'):
            Store(self.repo, self.home)
        after = {p.relative_to(outside).as_posix(): p.read_bytes() for p in outside.rglob('*') if p.is_file()}
        self.assertEqual(after, before)

    def test_task_junction_cannot_redirect_current_check_writes(self):
        task = self.store.root / 'tasks' / 'approval-task'
        task.mkdir(parents=True, exist_ok=True)
        outside = self.base / 'outside-task-state'
        outside.mkdir()
        (outside / 'sentinel').write_text('Preserve this file\n')
        task.rmdir()
        directory_link(task, outside)
        with self.assertRaisesRegex(ValueError, 'reparse|project directory'):
            WorkflowController(self.store).check('approval-task')
        self.assertEqual(list(outside.iterdir()), [outside / 'sentinel'])
        self.assertEqual((outside / 'sentinel').read_text(), 'Preserve this file\n')

    def test_runner_exclusive_artifact_write_cannot_overwrite_external_hardlink(self):
        outside = self.base / 'external.log'
        outside.write_text('Outside sentinel\n')
        target = self.base / 'owned-artifact.log'
        os.link(outside, target)
        with self.assertRaises(FileExistsError):
            _write(target, b'overwrite attempt')
        self.assertEqual(outside.read_text(), 'Outside sentinel\n')

    def test_same_root_execution_lock_is_held_across_real_processes(self):
        lock = self.store.root / 'tasks/approval-task/execution.lock'
        helper = self.base / 'hold-lock.py'
        helper.write_text('from ase.workflow import _exclusive\nfrom pathlib import Path\nimport sys\n'
                          'with _exclusive(Path(' + repr(str(lock)) + ')):\n'
                          ' print("locked",flush=True)\n sys.stdin.read(1)\n')
        process = subprocess.Popen([sys.executable, str(helper)], cwd=ROOT,
                                   env={**os.environ, 'PYTHONPATH': str(ROOT)}, stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            self.assertEqual(process.stdout.readline().strip(), 'locked')
            with self.assertRaisesRegex(ValueError, 'Another execution'):
                WorkflowController(self.store).run('approval-task')
            self.assertEqual(TaskService(self.store).snapshot('approval-task')['state'], 'READY')
        finally:
            if process.poll() is None:
                process.communicate('release', timeout=5)
            for pipe in (process.stdin, process.stdout, process.stderr):
                if not pipe.closed:
                    pipe.close()


class ProcessInputBoundaryTests(unittest.TestCase):
    def test_retired_coordinator_module_is_not_an_active_cli_service(self):
        result = subprocess.run([sys.executable, '-m', 'ase.host', '--help'], cwd=ROOT,
                                capture_output=True, text=True, timeout=5)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('No module named ase.host', result.stderr)

    def test_execute_and_runner_capture_do_not_inherit_open_parent_input(self):
        with tempfile.TemporaryDirectory(prefix='ase-input-boundary-') as temp:
            base = Path(temp)
            helper = base / 'helper.py'
            helper.write_text('from ase.core import execute\nfrom ase.runner import capture_process\n'
                              'from pathlib import Path\nimport json,sys\n'
                              'argv=[sys.executable,"-c","import sys; print(repr(sys.stdin.read()))"]\n'
                              'cwd=Path(' + repr(str(base)) + ')\n'
                              'legacy=execute(argv,cwd,cwd/"child.log",timeout=1)\n'
                              'direct=capture_process(argv,cwd,timeout=1,max_output_bytes=4096)\n'
                              'owned=capture_process(argv,cwd,timeout=1,max_output_bytes=4096,input_bytes=b"owned-packet")\n'
                              'print(json.dumps({"legacy":legacy,"direct":[direct["timed_out"],direct["exit_code"],direct["stdout"].decode().strip()],'
                              '"owned":[owned["timed_out"],owned["exit_code"],owned["stdout"].decode().strip()]}),flush=True)\n')
            process = subprocess.Popen([sys.executable, str(helper)], cwd=ROOT,
                                       env={**os.environ, 'PYTHONPATH': str(ROOT)}, stdin=subprocess.PIPE,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                # Keep parent input open and unused. Children must see EOF or
                # exactly an owned packet rather than waiting on this channel.
                process.wait(timeout=6)
                output, errors = process.stdout.read(), process.stderr.read()
                self.assertEqual(process.returncode, 0, output + errors)
                result = json.loads(output)
                self.assertFalse(result['legacy']['timed_out'])
                self.assertEqual(result['legacy']['exit_code'], 0)
                self.assertEqual((base / 'child.log').read_text().strip(), "''")
                self.assertEqual(result['direct'], [False, 0, "''"])
                self.assertEqual(result['owned'], [False, 0, "'owned-packet'"])
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)
                for pipe in (process.stdin, process.stdout, process.stderr):
                    pipe.close()


if __name__ == '__main__':
    unittest.main()
