"""Transport canaries and state boundaries; see the retired-host safeguard map."""
import os
from pathlib import Path
from test_runner import TransportFixture
from ase.runner import Runner
from ase.adapters.codex import CodexAdapter
from ase.adapters.claude import ClaudeAdapter
from ase.adapters.copilot import CopilotAdapter


class AdapterSecurityTests(TransportFixture):
    def test_backend_config_is_not_target_code(self):
        canary = self.target / "executed"
        (self.target / '.backend-startup.py').write_text(f"open({str(canary)!r}, 'w').write('bad')")
        for backend, adapter in (("codex", CodexAdapter()), ("claude", ClaudeAdapter()), ("copilot", CopilotAdapter())):
            with self.subTest(backend=backend):
                result = self.run_backend(adapter, backend)
                self.assertEqual(result["result"]["status"], "COMPLETE", result)
                self.assertFalse(canary.exists())
                self.assertEqual(list(self.target.iterdir()), [self.target / '.backend-startup.py'])

    def test_missing_cli_names_checked_locations_and_uses_the_native_claude_install(self):
        from unittest.mock import patch
        from ase.adapters import installed_executable
        home = self.base / "home"
        local = home / ".local" / "bin" / ("claude.exe" if os.name == "nt" else "claude")
        with patch.object(Path, "home", return_value=home):
            for backend, expected in (("claude", "PATH, " + str(local)), ("codex", "PATH)")):
                with self.subTest(backend=backend), self.assertRaises(ValueError) as raised:
                    installed_executable(backend)
                self.assertIn(f"CLI_NOT_INSTALLED: {backend} (checked {expected}", str(raised.exception))
                self.assertIn('"executable" in this project\'s backend_config', str(raised.exception))
            local.parent.mkdir(parents=True)
            local.write_bytes(b"")
            self.assertEqual(installed_executable("claude"), str(local))
            with self.assertRaises(ValueError):
                installed_executable("codex")

    def test_relative_executable_target_cwd_and_request_overrides_rejected(self):
        for config in ({"executable":"python"}, {"trusted_cwd":str(self.target)}):
            with self.subTest(config=config), self.assertRaises(ValueError):
                self.run_backend(**config)
        for key, value in (("argv", ["evil"]), ("env", {"X":"Y"}), ("cwd", str(self.target))):
            request = self.request()
            request[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                Runner(self.config(), self.base / 'artifacts').run(CodexAdapter(), request, timeout=3)

    def test_state_symlink_is_rejected(self):
        outside = self.base / "outside"
        outside.mkdir()
        artifacts = self.base / "artifacts"
        try:
            artifacts.symlink_to(outside, target_is_directory=True)
        except OSError:
            if os.name != 'nt': raise
            import subprocess
            subprocess.run(['cmd', '/c', 'mklink', '/J', str(artifacts), str(outside)], check=True, capture_output=True)
        with self.assertRaises(ValueError):
            self.run_backend()
        self.assertEqual(list(outside.iterdir()), [])

    def test_output_hardlink_cannot_substitute_an_external_artifact(self):
        from ase.runner import verify_receipt
        result = self.run_backend()
        receipt = result['receipt']
        path = Path(receipt['artifacts']['stdout']['path'])
        outside = self.base / 'external'
        outside.write_bytes(path.read_bytes())
        path.unlink()
        os.link(outside, path)
        with self.assertRaises(ValueError): verify_receipt(receipt)
        self.assertEqual(outside.read_bytes(), path.read_bytes())

    def test_concurrent_roles_have_disjoint_artifacts(self):
        from concurrent.futures import ThreadPoolExecutor
        runner = Runner(self.config(), self.base / 'artifacts')
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(lambda role: runner.run(CodexAdapter(), self.request(role), timeout=3), ['ac','review']))
        self.assertTrue(all(o['result']['status'] == 'COMPLETE' for o in outcomes), outcomes)
        self.assertEqual(len({o['receipt']['artifact_root'] for o in outcomes}), 2)

    def test_pinned_executable_ignores_ambient_agent_command_override(self):
        previous = os.environ.get('ASE_CODEX')
        os.environ['ASE_CODEX'] = str(self.base / 'evil.exe')
        try:
            result = self.run_backend()
        finally:
            if previous is None: os.environ.pop('ASE_CODEX', None)
            else: os.environ['ASE_CODEX'] = previous
        self.assertEqual(result['result']['status'], 'COMPLETE')
        self.assertNotEqual(result['receipt']['executable'], str(self.base / 'evil.exe'))

    def test_packet_paths_cannot_escape_and_missing_coverage_remains_visible(self):
        request = self.request()
        request['source_packet']['files'][0]['path'] = '../secret'
        with self.assertRaises(ValueError):
            Runner(self.config(), self.base / 'artifacts').run(CodexAdapter(), request, timeout=3)
        request = self.request()
        request['source_packet']['coverage']['missing'] = ['integration configuration']
        result = Runner(self.config(), self.base / 'artifacts').run(CodexAdapter(), request, timeout=3)
        self.assertEqual(result['result']['status'], 'BLOCKED')
        self.assertIn('integration configuration', result['result']['missing_coverage'])
