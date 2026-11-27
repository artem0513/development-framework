"""Project-owned config and private state boundary behavior, using real files/Git."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


class ProjectLocalFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='ase-local-boundary-')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / 'project'
        self.root.mkdir()
        self.owned = self.root / '.devframework'
        self.owned.mkdir()
        (self.owned / 'config.toml').write_text(
            'schema_version=1\nmode="project-local"\ntargets=["codex","claude","copilot"]\n'
            'timezone="UTC"\nlog_retention_days=30\n', encoding='utf-8')
        (self.owned / '.gitignore').write_text('/local/\n', encoding='utf-8')

    def git(self, *args):
        from ase.inspection import trusted_git
        env = {k: v for k, v in os.environ.items() if not k.upper().startswith('GIT_')}
        env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull, GIT_CONFIG_NOSYSTEM='1')
        return subprocess.run([trusted_git(), '-c', 'safe.directory=' + self.root.as_posix(),
            '-C', str(self.root), *args], capture_output=True, env=env, check=True)


class ProjectLocalBoundaryTests(ProjectLocalFixture):
    def test_find_nearest_config_from_nested_directory_without_writes(self):
        from ase.project_local import find_local_config
        nested = self.root / 'services/a/src'
        nested.mkdir(parents=True)
        before = set(self.root.rglob('*'))
        config = find_local_config(nested)
        self.assertEqual(config['root'], self.root.resolve())
        self.assertEqual(config['targets'], ['codex', 'claude', 'copilot'])
        self.assertEqual(config['timezone'], 'UTC')
        self.assertEqual(set(self.root.rglob('*')), before)

    def test_invalid_config_does_not_fall_back_to_parent_or_shared(self):
        from ase.project_local import find_local_config
        nested = self.root / 'child/.devframework'
        nested.mkdir(parents=True)
        (nested / 'config.toml').write_text('schema_version=77\n', encoding='utf-8')
        with self.assertRaises(ValueError):
            find_local_config(nested.parent)

    def test_config_schema_rejects_unknown_keys_and_invalid_settings(self):
        from ase.project_local import find_local_config
        path = self.owned / 'config.toml'
        original = path.read_text()
        for changed in [original + 'state="../../escape"\n', original.replace('30', '0'),
                        original.replace('["codex","claude","copilot"]', '["codex","codex"]'),
                        original.replace('"UTC"', '"Not/A_Real_Zone"'),
                        original.replace('schema_version=1', 'schema_version=true')]:
            with self.subTest(config=changed):
                path.write_text(changed, encoding='utf-8')
                with self.assertRaises(ValueError): find_local_config(self.root)

    def test_config_bytes_round_trips_strict_schema(self):
        from ase.project_local import config_bytes, find_local_config
        (self.owned / 'config.toml').write_bytes(config_bytes(['claude'], timezone='Europe/Bucharest', log_retention_days=45))
        config = find_local_config(self.root)
        self.assertEqual((config['targets'], config['timezone'], config['log_retention_days']), (['claude'], 'Europe/Bucharest', 45))

    def test_only_exact_protected_local_home_is_admitted(self):
        from ase.project_local import validate_local_state
        home = self.owned / 'local'
        with patch('ase.inspection._run', side_effect=AssertionError('Non-Git workspace must not invoke Git')):
            self.assertTrue(validate_local_state(home, self.root))
        self.assertFalse(home.exists())
        self.assertFalse(validate_local_state(self.base / 'shared', self.root))
        with self.assertRaises(ValueError): validate_local_state(home, self.base / 'other')
        with self.assertRaises(ValueError): validate_local_state(home / 'child', self.base / 'other')

    def test_missing_ignore_or_later_negation_rejects_before_state_creation(self):
        from ase.project_local import validate_local_state
        (self.owned / '.gitignore').unlink()
        with self.assertRaises(ValueError): validate_local_state(self.owned / 'local', self.root)
        self.assertFalse((self.owned / 'local').exists())
        self.git('init', '--quiet')
        (self.owned / '.gitignore').write_text('/local/\n!/local/\n', encoding='utf-8')
        with self.assertRaises(ValueError): validate_local_state(self.owned / 'local', self.root)

    def test_git_ignore_is_effective_and_tracked_private_bytes_are_rejected(self):
        from ase.project_local import validate_local_state
        self.git('init', '--quiet')
        self.assertTrue(validate_local_state(self.owned / 'local', self.root))
        home = self.owned / 'local'
        home.mkdir()
        (home / 'private.txt').write_text('private-state-canary', encoding='utf-8')
        self.git('add', '-f', '.devframework/local/private.txt')
        with self.assertRaises(ValueError): validate_local_state(home, self.root)

    def test_selectively_unignored_memory_directory_is_rejected(self):
        from ase.project_local import validate_local_state
        self.git('init', '--quiet')
        (self.owned / '.gitignore').write_text('/local/\n!/local/\n/local/*\n!/local/projects/\n', encoding='utf-8')
        with self.assertRaises(ValueError): validate_local_state(self.owned / 'local', self.root)
        self.assertFalse((self.owned / 'local').exists())

    def test_hardlinked_config_is_rejected(self):
        from ase.project_local import find_local_config
        alias = self.base / 'config-alias.toml'
        os.link(self.owned / 'config.toml', alias)
        with self.assertRaises(ValueError): find_local_config(self.root)

    def test_private_path_detection_includes_nested_projects(self):
        from ase.project_local import is_private_path
        for path in ['.devframework/local/vault.json', 'service/.devframework/local/projects/db', '.devframework/local']:
            self.assertTrue(is_private_path(path), path)
        for path in ['.devframework/config.toml', '.devframework/locality/source.py', 'src/local.py']:
            self.assertFalse(is_private_path(path), path)


class ProjectLocalSourceTests(ProjectLocalFixture):
    def test_hardlink_to_private_state_cannot_be_fingerprinted_or_read_as_knowledge(self):
        from ase.inspection import safe_snapshot
        from ase.knowledge import Knowledge
        from types import SimpleNamespace
        self.git('init', '--quiet')
        local = self.owned / 'local'
        local.mkdir()
        private = local / 'journal.txt'
        private.write_text('private-state-canary', encoding='utf-8')
        os.link(private, self.root / 'notes.txt')
        with self.assertRaises(ValueError): safe_snapshot(self.root)
        with self.assertRaises(ValueError): Knowledge(SimpleNamespace(repo=self.root))._local_path('local:notes.txt')

    def test_hardlink_to_private_state_cannot_be_discovered(self):
        from ase.discovery import discover_capabilities
        local = self.owned / 'local'
        local.mkdir()
        private = local / 'journal.txt'
        private.write_text('private-state-canary', encoding='utf-8')
        os.link(private, self.root / 'notes.txt')
        with self.assertRaises(ValueError):
            discover_capabilities(self.root, scope={'resources': ['notes.txt']}, backend_config={})
    def test_native_runner_keeps_trusted_cwd_outside_source_and_receipts_local(self):
        from ase.store import Store
        from ase.workflow import WorkflowController
        store = Store(self.root)
        self.addCleanup(store.close)
        controller = WorkflowController(store)
        with patch.object(controller.tasks, 'snapshot', return_value={'contract': {'execution_policy': {'backend': 'codex'}}}), \
                patch('ase.workflow.shutil.which', return_value=sys.executable):
            runner = controller._runner('fixture')
        self.assertFalse(Path(runner.config['trusted_cwd']).is_relative_to(self.root))
        self.assertTrue(runner.artifact_root.is_relative_to(self.owned / 'local'))
    def test_private_updates_do_not_change_git_source_fingerprint(self):
        from ase.inspection import safe_snapshot
        self.git('init', '--quiet')
        (self.root / 'app.py').write_text('value=1\n', encoding='utf-8')
        before = safe_snapshot(self.root)
        local = self.owned / 'local'
        local.mkdir()
        (local / 'memory.sqlite3').write_bytes(b'private-state-canary')
        self.assertEqual(safe_snapshot(self.root)['fingerprint'], before['fingerprint'])
        self.git('add', '-f', '.devframework/local/memory.sqlite3')
        with self.assertRaises(ValueError): safe_snapshot(self.root)

    def test_private_file_cannot_be_explicitly_discovered(self):
        from ase.discovery import discover_capabilities
        local = self.owned / 'local'
        local.mkdir()
        (local / 'secret.txt').write_text('private-state-canary', encoding='utf-8')
        with self.assertRaises(ValueError):
            discover_capabilities(self.root, scope={'resources': ['.devframework/local/secret.txt']}, backend_config={})

    def test_private_file_cannot_be_used_as_knowledge_source(self):
        from ase.knowledge import Knowledge
        from types import SimpleNamespace
        knowledge = Knowledge(SimpleNamespace(repo=self.root))
        with self.assertRaises(ValueError): knowledge._local_path('local:.devframework/local/secret.txt')

    def test_non_git_directory_inventory_skips_private_state(self):
        from ase.gates import _directory_sources
        (self.root / 'app.txt').write_text('public', encoding='utf-8')
        local = self.owned / 'local'
        local.mkdir()
        (local / 'secret.txt').write_text('private-state-canary', encoding='utf-8')
        paths = [p.relative_to(self.root).as_posix() for p in _directory_sources(self.root, self.root)]
        self.assertIn('app.txt', paths)
        self.assertNotIn('.devframework/local/secret.txt', paths)


if __name__ == '__main__': unittest.main()
