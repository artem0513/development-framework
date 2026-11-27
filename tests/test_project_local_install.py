"""Real filesystem and launcher coverage for project-owned installations."""
import base64
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import tomllib
import unittest
from unittest.mock import patch

from scripts import install as installer


ROOT = Path(__file__).resolve().parents[1]


def snapshot(root):
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob('*') if p.is_file()}


class ProjectLocalInstallTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='ase-local-install-')
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.project = self.base / 'project with spaces'
        self.project.mkdir()
        self.home = self.project / '.devframework'

    def plan(self, targets=None, **kwargs):
        return installer.plan_install(targets or ['codex', 'claude', 'copilot'], self.project,
                                      runtime_home=self.home, project_local=True, **kwargs)

    def test_project_local_api_and_cli_preview_choose_exact_project_home_without_writes(self):
        before = snapshot(self.base)
        command = [sys.executable, str(ROOT / 'scripts/install.py'), '--project', str(self.project),
                   '--project-local', '--target', 'codex', '--preview']
        result = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        manifest = json.loads(result.stdout)
        self.assertEqual(manifest['runtime_home'], str(self.home))
        self.assertEqual(snapshot(self.base), before)
        self.assertEqual(self.plan()['runtime_home'], str(self.home))

    def test_all_targets_config_ignore_private_backups_and_idempotence(self):
        (self.project / 'CLAUDE.md').write_text('Keep human instructions\n')
        manifest = self.plan()
        installer.apply_install(manifest)
        self.assertEqual(tomllib.loads((self.home / 'config.toml').read_text()), {
            'schema_version': 1, 'mode': 'project-local', 'targets': ['codex', 'claude', 'copilot'],
            'timezone': 'local', 'log_retention_days': 30})
        self.assertTrue((self.home / 'autonomous-se/runtime/ase/__main__.py').is_file())
        self.assertIn('/local/', (self.home / '.gitignore').read_text().splitlines())
        for pointer in ('AGENTS.md', 'CLAUDE.md', '.github/copilot-instructions.md'):
            content = (self.project / pointer).read_text()
            self.assertIn('.devframework/bin/ase', content)
            self.assertIn('.devframework/config.toml', content)
        backups = [Path(item['backup']) for item in manifest['files'] if item['backup']]
        self.assertTrue(backups)
        self.assertTrue(all(p.is_relative_to(self.home / 'local/backups') for p in backups))
        self.assertTrue(any(p.read_text() == 'Keep human instructions\n' for p in backups))
        before = snapshot(self.base)
        self.assertEqual(installer.apply_install(self.plan())['changed'], [])
        self.assertEqual(snapshot(self.base), before)

    def test_existing_strict_config_comments_values_and_targets_are_preserved(self):
        self.home.mkdir()
        original = ('# team timezone\nschema_version=1\nmode="project-local"\n'
                    'targets=["claude"] # prior backend\ntimezone="Europe/Bucharest"\nlog_retention_days=14\n'
                    'guard_dotnet_package_additions = true # pilot setting\n'
                    'guard_efcore_schema_additions = true\nguard_arm_resource_additions = false\n')
        config = self.home / 'config.toml'
        config.write_text(original)
        installer.apply_install(self.plan(['codex']))
        body = config.read_text()
        self.assertIn('# team timezone', body)
        self.assertIn('# prior backend', body)
        self.assertEqual(tomllib.loads(body)['targets'], ['claude', 'codex'])
        self.assertEqual(tomllib.loads(body)['timezone'], 'Europe/Bucharest')
        self.assertEqual(tomllib.loads(body)['log_retention_days'], 14)
        self.assertIn('guard_dotnet_package_additions = true # pilot setting', body)
        self.assertIs(tomllib.loads(body)['guard_efcore_schema_additions'], True)
        self.assertIs(tomllib.loads(body)['guard_arm_resource_additions'], False)
        self.assertEqual(installer.apply_install(self.plan(['codex']))['changed'], [])

    def test_policy_upgrade_flags_open_review_follow_up(self):
        initial = installer.apply_install(self.plan(['codex']))
        self.assertFalse(initial.get('review_policy_changed'))
        policy = Path(initial['runtime']) / 'ase/data/runtime_policy.json'
        current = policy.read_bytes()
        policy.write_bytes(current + b'\n')

        preview = self.plan(['codex'])
        self.assertTrue(preview.get('review_policy_changed'))
        self.assertEqual(policy.read_bytes(), current + b'\n')
        applied = installer.apply_install(preview)
        self.assertTrue(applied['review_policy_changed'])
        self.assertEqual(policy.read_bytes(), current)
        self.assertFalse(installer.apply_install(self.plan(['codex']))['review_policy_changed'])

    def test_invalid_config_and_destination_conflicts_stop_before_any_write(self):
        self.home.mkdir()
        config = self.home / 'config.toml'
        for body in ('unknown=true\n', 'schema_version=1\nmode="project-local"\ntargets=[]\n',
                     'schema_version=1\nmode="project-local"\ntargets=["codex"]\ntimezone="UTC"\nlog_retention_days=0\n'):
            config.write_text(body)
            before = snapshot(self.base)
            with self.assertRaises(ValueError):
                self.plan()
            self.assertEqual(snapshot(self.base), before)
        config.unlink()
        (self.home / 'local').write_text('not a directory')
        with self.assertRaises(ValueError):
            self.plan()
        self.assertFalse(config.exists())

    def test_local_mode_requires_project_and_refuses_external_override(self):
        with self.assertRaises(ValueError):
            installer.plan_install(['codex'], None, runtime_home=self.home, project_local=True)
        with self.assertRaises(ValueError):
            installer.plan_install(['codex'], self.project, runtime_home=self.base / 'external', project_local=True)
        self.assertFalse(self.home.exists())

    def test_stale_preview_and_hardlinked_local_config_are_rejected_without_writes(self):
        manifest = self.plan()
        self.home.mkdir()
        (self.home / '.gitignore').write_text('concurrent edit\n')
        before = snapshot(self.base)
        with self.assertRaisesRegex(ValueError, '[Ss]tale'):
            installer.apply_install(manifest)
        self.assertEqual(snapshot(self.base), before)
        external = self.base / 'outside.toml'
        external.write_text('outside')
        os.link(external, self.home / 'config.toml')
        with self.assertRaises(ValueError):
            self.plan()
        self.assertEqual(external.read_text(), 'outside')

    def run_launcher(self, project, args, shell=False):
        unrelated = self.base / 'unrelated cwd'
        unrelated.mkdir(exist_ok=True)
        env = dict(os.environ, ASE_HOME=str(self.base / 'hostile shared state'))
        if shell:
            executable = shutil.which('sh') or ('C:/Program Files/Git/bin/bash.exe' if os.name == 'nt' else None)
            if not executable or not Path(executable).exists():
                self.skipTest('POSIX shell unavailable')
            command = [executable, str(project / '.devframework/bin/ase'), *args]
        else:
            executable = shutil.which('pwsh') or shutil.which('powershell')
            if not executable:
                self.skipTest('PowerShell unavailable')
            command = [executable, '-NoProfile', '-File', str(project / '.devframework/bin/ase.ps1'), *args]
        result = subprocess.run(command, cwd=unrelated, env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return json.loads(result.stdout)

    def test_powershell_launcher_isolates_two_projects_and_preserves_explicit_selectors(self):
        installer.apply_install(self.plan())
        other = self.base / 'second project'
        other.mkdir()
        installer.apply_install(installer.plan_install(['codex'], other, project_local=True))
        first = self.run_launcher(self.project, ['status'])
        second = self.run_launcher(other, ['status'])
        self.assertEqual(first['root'], str(self.project))
        self.assertEqual(second['root'], str(other))
        self.assertNotEqual(first['project_id'], second['project_id'])
        explicit = self.run_launcher(self.project, ['--repo', str(other), 'status'])
        self.assertEqual(explicit['project_id'], second['project_id'])
        explicit_project = self.run_launcher(self.project, ['--project', str(other), 'status'])
        self.assertEqual(explicit_project['project_id'], second['project_id'])
        for shell in (False, True):
            with self.subTest(shell=shell):
                by_uuid = self.run_launcher(self.project, ['--project', first['project_id'], 'status'], shell=shell)
                self.assertEqual(by_uuid['project_id'], first['project_id'])
        external = self.base / 'explicit external state'
        selected_home = self.run_launcher(self.project, ['--home', str(external), 'status'])
        self.assertEqual(selected_home['root'], str(self.project))
        self.assertTrue((external / 'registry.sqlite3').is_file())
        self.assertFalse((self.base / 'hostile shared state').exists())

    def test_shell_launcher_and_relocated_runtime_work_from_unrelated_cwd(self):
        installer.apply_install(self.plan())
        moved = self.base / 'relocated project with spaces'
        self.project.rename(moved)
        # No state was registered at the old physical path. A fresh portable
        # install can move freely; existing registered state requires reattach.
        data = self.run_launcher(moved, ['status'], shell=True)
        self.assertEqual(data['root'], str(moved))
        self.assertEqual(self.run_launcher(moved, ['status'])['project_id'], data['project_id'])
        self.assertFalse((self.base / 'hostile shared state').exists())

    def test_installed_runtime_can_reapply_without_copying_private_state(self):
        installer.apply_install(self.plan())
        private = self.home / 'local/memory-private.txt'
        private.parent.mkdir(parents=True, exist_ok=True)
        private.write_text('do not package')
        source_private = self.home / 'autonomous-se/runtime/docs/.devframework/local'
        source_private.mkdir(parents=True)
        (source_private / 'secret.txt').write_text('do not package nested state either')
        command = [sys.executable, str(self.home / 'autonomous-se/runtime/scripts/install.py'),
                   '--project', str(self.project), '--project-local', '--target', 'codex', '--preview']
        result = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        manifest = json.loads(result.stdout)
        self.assertFalse(any('secret.txt' in item['path'] or 'memory-private.txt' in item['path']
                             for item in manifest['files']))

    def test_explicit_timezone_is_validated_and_applied(self):
        installer.apply_install(self.plan(timezone='Europe/Bucharest'))
        self.assertEqual(tomllib.loads((self.home / 'config.toml').read_text())['timezone'], 'Europe/Bucharest')
        before = snapshot(self.base)
        for invalid in ('Not/AZone', ''):
            with self.subTest(timezone=invalid), self.assertRaises(ValueError):
                self.plan(timezone=invalid)
        self.assertEqual(snapshot(self.base), before)

    def test_tracked_private_state_blocks_install_and_apply_before_any_write(self):
        subprocess.run(['git', 'init', '-q', str(self.project)], check=True, capture_output=True)
        manifest = self.plan()
        local = self.home / 'local'
        local.mkdir(parents=True)
        (local / 'secret.txt').write_text('already tracked')
        subprocess.run(['git', '-C', str(self.project), 'add', '.devframework/local/secret.txt'], check=True, capture_output=True)
        before = snapshot(self.project)
        with self.assertRaisesRegex(ValueError, '[Pp]rivate|tracked'):
            self.plan()
        with self.assertRaisesRegex(ValueError, '[Pp]rivate|tracked'):
            installer.apply_install(manifest)
        self.assertEqual(snapshot(self.project), before)

    def test_nested_private_state_cannot_be_copied_into_backend_skills(self):
        installer.apply_install(self.plan())
        runtime = self.home / 'autonomous-se/runtime'
        nested = runtime / 'skills/task-orchestrator/.devframework/local'
        nested.mkdir(parents=True)
        (nested / 'secret.txt').write_text('private state')
        public = runtime / 'skills/task-orchestrator/local'
        public.mkdir()
        (public / 'reference.txt').write_text('legitimate local resource folder')
        result = subprocess.run([sys.executable, str(runtime / 'scripts/install.py'), '--project',
                                 str(self.project), '--project-local', '--target', 'codex', '--preview'],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        files = json.loads(result.stdout)['files']
        self.assertFalse(any('secret.txt' in item['path'] for item in files))
        self.assertTrue(any(item['path'].endswith('local\\reference.txt') or
                            item['path'].endswith('local/reference.txt') for item in files))

    def test_local_ignore_contains_exact_required_rule(self):
        self.home.mkdir()
        (self.home / '.gitignore').write_text(' /local/\n')
        installer.apply_install(self.plan())
        self.assertIn('/local/', (self.home / '.gitignore').read_text().splitlines())

    def test_launchers_fall_back_when_original_interpreter_is_unavailable(self):
        installer.apply_install(self.plan())
        for filename in ('ase.ps1', 'ase'):
            launcher = self.home / 'bin' / filename
            launcher.write_text(launcher.read_text().replace(str(Path(sys.executable)), str(self.base / 'missing-python')))
        first = self.run_launcher(self.project, ['status'])
        second = self.run_launcher(self.project, ['status'], shell=True)
        self.assertEqual(first['project_id'], second['project_id'])

    def test_powershell_launcher_restores_callers_environment(self):
        installer.apply_install(self.plan())
        powershell = shutil.which('pwsh') or shutil.which('powershell')
        if not powershell:
            self.skipTest('PowerShell unavailable')
        launcher = str(self.home / 'bin/ase.ps1').replace("'", "''")
        command = ("$env:PYTHONPATH = 'caller-value'; "
                   f"& '{launcher}' status | Out-Null; "
                   "if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }; "
                   "if ($env:PYTHONPATH -ne 'caller-value') { throw 'caller environment changed' }")
        result = subprocess.run([powershell, '-NoProfile', '-Command', command], cwd=self.base,
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_powershell_launcher_forwards_piped_input_as_utf8(self):
        installer.apply_install(self.plan())
        powershell = shutil.which('pwsh') or shutil.which('powershell')
        if not powershell:
            self.skipTest('PowerShell unavailable')
        launcher = str(self.home / 'bin/ase.ps1').replace("'", "''")
        command = ("$event = '{\"prompt\": \"approve \u043f\u043b\u0430\u043d\", \"session_id\": \"s\"}'; "
                   f"$before = $OutputEncoding.WebName; $event | & '{launcher}' prompt capture; $code = $LASTEXITCODE; "
                   "if ($OutputEncoding.WebName -ne $before) { throw 'caller encoding changed' }; exit $code")
        encoded = base64.b64encode(command.encode('utf-16-le')).decode('ascii')
        result = subprocess.run([powershell, '-NoProfile', '-EncodedCommand', encoded], cwd=self.base,
                                stdin=subprocess.DEVNULL, capture_output=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        listed = subprocess.run([sys.executable, '-m', 'ase', '--repo', str(self.project), 'prompt', 'list'], cwd=ROOT,
                                env=dict(os.environ, PYTHONIOENCODING='utf-8'), capture_output=True, encoding='utf-8')
        self.assertEqual(listed.returncode, 0, listed.stdout + listed.stderr)
        self.assertEqual(json.loads(listed.stdout)['prompts'][0]['excerpt'], 'approve \u043f\u043b\u0430\u043d')

    def test_shell_launcher_ignores_a_windows_style_caller_pythonpath(self):
        installer.apply_install(self.plan())
        with patch.dict(os.environ, {'PYTHONPATH': 'tests;.' if os.name == 'nt' else 'tests:.'}):
            data = self.run_launcher(self.project, ['status'], shell=True)
        self.assertEqual(data['root'], str(self.project))

    def test_ignore_repair_precedes_private_backup_creation(self):
        installer.apply_install(self.plan())
        ignore = self.home / '.gitignore'
        ignore.write_text('!/local/\n')
        manifest = self.plan(timezone='Europe/Bucharest')
        real_mkdir = Path.mkdir

        def guarded_mkdir(path, *args, **kwargs):
            if path.is_relative_to(self.home / 'local'):
                self.assertEqual(ignore.read_text().splitlines()[-1], '/local/',
                                 'private backups require effective ignore protection first')
            return real_mkdir(path, *args, **kwargs)

        with patch.object(Path, 'mkdir', guarded_mkdir):
            installer.apply_install(manifest)
        backups = [Path(item['backup']) for item in manifest['files'] if item['backup']]
        self.assertTrue(any(path.read_text() == '!/local/\n' for path in backups))

    def test_canonical_hardlink_to_private_canary_stops_preview_before_copy(self):
        source_root = self.base / 'owned canonical fixture'
        for name in ('ase', 'skills', 'agents', 'docs', 'templates', 'scripts'):
            (source_root / name).mkdir(parents=True)
        private = self.base / 'other project/.devframework/local/private-canary.txt'
        private.parent.mkdir(parents=True)
        private.write_text('PRIVATE-CANARY-MUST-NOT-ENTER-PACKAGE')
        for relative in ('ase/aliased.py', 'skills/task-orchestrator/references/aliased.txt'):
            with self.subTest(relative=relative):
                alias = source_root / relative
                alias.parent.mkdir(parents=True, exist_ok=True)
                os.link(private, alias)
                before = snapshot(self.project)
                real_read = Path.read_bytes
                aliased_reads = []

                def observe_read(path):
                    if path == alias:
                        aliased_reads.append(path)
                    return real_read(path)

                try:
                    with patch.object(installer, 'ROOT', source_root), patch.object(Path, 'read_bytes', observe_read), self.assertRaisesRegex(ValueError, '[Hh]ardlink|linked'):
                        self.plan()
                    self.assertEqual(aliased_reads, [], 'reject multiply-linked sources before reading their bytes')
                    self.assertEqual(snapshot(self.project), before)
                    self.assertFalse(self.home.exists())
                finally:
                    alias.unlink()

    def test_selective_unignore_is_repaired_without_losing_existing_rules(self):
        self.home.mkdir()
        ignore = self.home / '.gitignore'
        original = '/local/\n!/local/\n/local/*\n!/local/credential-canary.txt\n# preserve this comment\n'
        ignore.write_text(original)
        installer.apply_install(self.plan())
        body = ignore.read_text()
        self.assertTrue(body.startswith(original.rstrip()))
        meaningful = [line for line in body.splitlines() if line.strip() and not line.lstrip().startswith('#')]
        self.assertEqual(meaningful[-1], '/local/')
        self.assertEqual(installer.apply_install(self.plan())['changed'], [])
        subprocess.run(['git', 'init', '-q', str(self.project)], check=True, capture_output=True)
        ignore.write_text(original)
        installer.apply_install(self.plan())
        from ase.project_local import validate_local_state
        self.assertTrue(validate_local_state(self.home / 'local', project_root=self.project))
        self.assertEqual(installer.apply_install(self.plan())['changed'], [])

    def test_missing_owning_config_blocks_launchers_before_shared_state_creation(self):
        installer.apply_install(self.plan())
        (self.home / 'config.toml').unlink()
        powershell = shutil.which('pwsh') or shutil.which('powershell')
        shell = shutil.which('sh') or ('C:/Program Files/Git/bin/bash.exe' if os.name == 'nt' else None)
        commands = []
        if powershell:
            commands.append([powershell, '-NoProfile', '-File', str(self.home / 'bin/ase.ps1'), 'status'])
        if shell and Path(shell).exists():
            commands.append([shell, str(self.home / 'bin/ase'), 'status'])
        if not commands:
            self.skipTest('PowerShell/POSIX shell unavailable')
        for index, command in enumerate(commands):
            with self.subTest(launcher=command[0]):
                shared = self.base / ('hostile-global-state-' + str(index))
                result = subprocess.run(command, cwd=self.base, env=dict(os.environ, ASE_HOME=str(shared)),
                                        capture_output=True, text=True)
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertFalse(shared.exists(), 'broken local installation must not fall back to shared state')


if __name__ == '__main__':
    unittest.main()
