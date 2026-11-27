"""Explicit project activation must not leak engineering routing to other work."""
import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import tomllib
import unittest
import zipfile
from unittest.mock import patch

from scripts import install as installer
from scripts import vendor_skills


ROOT = Path(__file__).resolve().parents[1]


class ProjectInstallationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ase-project-install-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.home, self.project = self.base / "home", self.base / "project"
        self.home.mkdir()
        self.project.mkdir()
        self.source = self.base / "source"
        for directory in ("ase", "docs", "templates", "scripts", "agents"):
            (self.source / directory).mkdir(parents=True)
        (self.source / "ase/__init__.py").write_text("# fixture\n")
        (self.source / "ase/core.py").write_text("# fixture runtime\n")
        (self.source / "agents/reviewer.toml").write_text('name = "reviewer"\n')
        for name in ("task-orchestrator", "quality-gate"):
            p = self.source / "skills" / name / "SKILL.md"
            p.parent.mkdir(parents=True)
            p.write_text(f"---\nname: {name}\ndescription: Fixture\n---\nFixture body\n")
        self.global_agents = self.home / "AGENTS.md"
        self.global_agents.write_text("# Personal instructions\nKeep these intact.\n")
        self.config = self.home / "config.toml"
        self.config.write_text('model = "keep-model"\n')
        self.addCleanup(patch.stopall)
        patch.object(installer, "ROOT", self.source).start()

    def install(self, **kwargs):
        with contextlib.redirect_stdout(io.StringIO()) as output:
            installer.install(self.home, **kwargs)
        return json.loads(output.getvalue())

    def test_project_activation_preserves_global_routing_and_other_projects(self):
        other = self.base / "unrelated-report"
        other.mkdir()
        (other / "AGENTS.md").write_text("Financial analysis only.\n")
        before = {p: p.read_bytes() for p in (self.global_agents, self.config, other / "AGENTS.md")}
        result = self.install(project=self.project)
        self.assertEqual(result["scope"], "project")
        self.assertTrue((self.project / ".agents/skills/task-orchestrator/SKILL.md").is_file())
        self.assertTrue((self.project / ".codex/agents/reviewer.toml").is_file())
        text = (self.project / "AGENTS.md").read_text()
        self.assertIn(".agents/skills/task-orchestrator/SKILL.md", text)
        self.assertIn("code or configuration implementation", text)
        self.assertIn("domain", text)
        for p, content in before.items():
            self.assertEqual(p.read_bytes(), content)
        self.assertFalse((self.home / "agents").exists())

    def test_project_config_keeps_existing_values_and_enables_exact_skill_paths(self):
        p = self.project / ".codex/config.toml"
        p.parent.mkdir()
        original = '# Team settings\nmodel = "team-model"\n[[skills.config]]\nname = "quality-gate"\nenabled = false\n'
        p.write_text(original)
        self.install(project=self.project)
        text = p.read_text()
        self.assertTrue(text.startswith(original))
        data = tomllib.loads(text)
        self.assertEqual(data["model"], "team-model")
        self.assertEqual(data["skills"]["config"][0], {"name": "quality-gate", "enabled": False})
        entries = data["skills"]["config"][1:]
        self.assertEqual({e["path"] for e in entries}, {
            str(self.project.resolve() / ".agents/skills" / name / "SKILL.md")
            for name in ("task-orchestrator", "quality-gate")})
        self.assertTrue(all(e["enabled"] is True for e in entries))

    def test_existing_inline_skill_arrays_preserve_values_comments_and_reapplication(self):
        samples = {
            "empty": '[skills]\nconfig = [] # keep config comment\n[other]\nvalue = "retain"\n',
            "populated": '[skills]\nconfig = [{name="finance-research",enabled=false}] # keep config comment\n[other]\nvalue = 3\n',
            "multiline": '[skills]\nconfig = [\n# keep config comment\n{name="finance-research",enabled=false},\n]\n[other]\nvalue = [1,2]\n',
            "dotted": '# keep config comment\nskills.config = [{name="finance-research",enabled=false}]\nmodel = "unchanged"\n',
            "inline_parent": '# keep config comment\nskills = {config=[{name="finance-research",enabled=false}],bundled={enabled=true}}\nmodel = "unchanged"\n',
            "quoted_keys": '# keep config comment\n"skills"."config" = []\nmodel = "unchanged"\n',
            "strings_not_syntax": 'note = """[skills]\nconfig = [] # not configuration\n"""\n[skills]\nconfig=[{name="finance-research",enabled=false}] # keep config comment\n[other]\nvalue="[literal] # value"\n',
        }
        config = self.project / ".codex/config.toml"
        config.parent.mkdir()
        for case, original in samples.items():
            with self.subTest(case=case):
                config.write_text(original)
                before = tomllib.loads(original)
                original_entries = before["skills"]["config"]
                self.install(project=self.project)
                first = config.read_bytes()
                text = first.decode("utf-8")
                after = tomllib.loads(text)
                entries = after["skills"]["config"]
                self.assertEqual(entries[:len(original_entries)], original_entries)
                self.assertEqual(len(entries), len(original_entries) + 2)
                self.assertTrue(all(e["enabled"] is True for e in entries[len(original_entries):]))
                after["skills"]["config"] = original_entries
                self.assertEqual(after, before)
                self.assertIn("# keep config comment", text)
                self.assertEqual(text.count(installer.SKILLS_BEGIN), 1)
                self.install(project=self.project)
                self.assertEqual(config.read_bytes(), first)

    def test_reapply_preserves_project_guidance_and_does_not_duplicate_managed_blocks(self):
        path = self.project / "AGENTS.md"
        original = "# Team rules\nUse our conventions.\n"
        path.write_text(original)
        self.install(project=self.project)
        first = path.read_bytes()
        config = (self.project / ".codex/config.toml").read_bytes()
        self.install(project=self.project)
        self.assertEqual(path.read_bytes(), first)
        self.assertEqual((self.project / ".codex/config.toml").read_bytes(), config)
        self.assertEqual(path.read_text().count(installer.BEGIN), 1)
        self.assertTrue(path.read_text().startswith(original.rstrip()))
        self.assertTrue(any(p.read_text() == original for p in (self.home / "backups").rglob("AGENTS.md")))

    def test_relocated_project_rebuilds_only_owned_absolute_skill_selectors(self):
        old_project = self.base / 'old-project'
        old_project.mkdir()
        old_skill = old_project / '.agents/skills/task-orchestrator/SKILL.md'
        config = self.project / '.codex/config.toml'
        config.parent.mkdir()
        unrelated = '# user settings\nmodel="preserve"\n[[skills.config]]\npath="unrelated/domain/SKILL.md"\nenabled=false\n'
        owned = ('\n' + installer.SKILLS_BEGIN + '\n[[skills.config]]\npath=' + json.dumps(str(old_skill))
                 + '\nenabled=true\n' + installer.SKILLS_END + '\n')
        config.write_text(unrelated + owned)
        self.install(project=self.project)
        text = config.read_text()
        self.assertTrue(text.startswith(unrelated))
        self.assertNotIn(str(old_skill).replace('\\', '\\\\'), text)
        entries = tomllib.loads(text)['skills']['config']
        self.assertEqual(entries[0], {'path': 'unrelated/domain/SKILL.md', 'enabled': False})
        self.assertEqual({entry['path'] for entry in entries[1:]}, {
            str(self.project / '.agents/skills' / name / 'SKILL.md') for name in ('task-orchestrator', 'quality-gate')})
        self.assertEqual(list(old_project.iterdir()), [])

    def test_existing_active_override_receives_routing_instead_of_ignored_agents(self):
        override = self.project / "AGENTS.override.md"
        override.write_text("# Active team override\n")
        agents = self.project / "AGENTS.md"
        agents.write_text("# Inactive fallback\n")
        result = self.install(project=self.project)
        self.assertIn(installer.BEGIN, override.read_text())
        self.assertEqual(agents.read_text(), "# Inactive fallback\n")
        self.assertEqual(result["instruction_file"], str(override))

    def test_invalid_project_toml_fails_before_any_runtime_or_project_activation(self):
        path = self.project / ".codex/config.toml"
        path.parent.mkdir()
        path.write_text("[invalid\n")
        with self.assertRaises(ValueError):
            self.install(project=self.project)
        self.assertFalse((self.home / "autonomous-se").exists())
        self.assertFalse((self.project / ".agents").exists())
        self.assertEqual(path.read_text(), "[invalid\n")

    def test_file_destination_directory_conflicts_leave_all_installation_state_unchanged(self):
        cases = {
            "skill_entry": ("project", ".agents/skills/task-orchestrator/SKILL.md"),
            "role": ("project", ".codex/agents/reviewer.toml"),
            "routing": ("project", "AGENTS.md"),
            "project_config": ("project", ".codex/config.toml"),
            "shared_runtime": ("home", "autonomous-se/runtime/ase/core.py"),
            "launcher": ("home", "bin/ase.ps1"),
            "manifest": ("home", "autonomous-se/installation.json"),
        }

        def snapshot(root):
            return {p.relative_to(root).as_posix(): ("directory" if p.is_dir() else p.read_bytes())
                    for p in root.rglob("*")}

        for case, (scope, relative) in cases.items():
            with self.subTest(case=case), tempfile.TemporaryDirectory(dir=self.base, prefix=case) as temp:
                root = Path(temp)
                home, project = root / "home", root / "project"
                home.mkdir()
                project.mkdir()
                target = (home if scope == "home" else project) / relative
                target.mkdir(parents=True)
                before = (snapshot(home), snapshot(project))
                with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(ValueError):
                    installer.install(home, project=project)
                self.assertEqual((snapshot(home), snapshot(project)), before)
                self.assertFalse((project / ".agents/skills/task-orchestrator/SKILL.md").is_file())
                self.assertFalse((project / ".codex/agents/reviewer.toml").is_file())

    def test_global_file_conflict_is_preflighted_before_shared_runtime_writes(self):
        skills = self.base / "explicit-global-skills"
        (skills / "task-orchestrator/SKILL.md").mkdir(parents=True)
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(ValueError):
            installer.install(self.home, skills, global_install=True)
        self.assertFalse((self.home / "autonomous-se").exists())
        self.assertEqual(self.global_agents.read_text(), "# Personal instructions\nKeep these intact.\n")

    def test_directory_target_file_conflict_is_preflighted_before_any_writes(self):
        target = self.project / ".agents"
        target.mkdir()
        (target / "skills").write_text("keep this file\n")
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(ValueError):
            self.install(project=self.project)
        self.assertEqual((target / "skills").read_text(), "keep this file\n")
        self.assertFalse((self.home / "autonomous-se").exists())

    def test_project_symlink_escape_is_rejected_before_writes(self):
        outside = self.base / "outside"
        outside.mkdir()
        link = self.project / ".agents"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except OSError:
            if os.name != "nt":
                raise
            p = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(outside)], capture_output=True, text=True)
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        with self.assertRaises(ValueError):
            self.install(project=self.project)
        self.assertEqual(list(outside.iterdir()), [])
        self.assertFalse((self.home / "autonomous-se").exists())

    def test_hardlinked_instruction_file_cannot_mutate_outside_project(self):
        outside = self.base / "external-rules.md"
        outside.write_text("# External rules\n")
        os.link(outside, self.project / "AGENTS.md")
        with self.assertRaises(ValueError):
            self.install(project=self.project)
        self.assertEqual(outside.read_text(), "# External rules\n")

    def test_managed_file_symlink_flag_is_rejected_before_any_writes(self):
        target = self.project / ".agents/skills/task-orchestrator/SKILL.md"
        target.parent.mkdir(parents=True)
        target.write_text("Unrelated target content\n")
        # Windows file-symlink creation requires a privilege unavailable to CI
        # users. This unit test isolates the link predicate; real directory
        # link/junction behavior is exercised in the neighboring integration test.
        original = Path.is_symlink
        with patch.object(Path, "is_symlink", autospec=True,
                          side_effect=lambda p: p == target or original(p)):
            with self.assertRaises(ValueError):
                self.install(project=self.project)
        self.assertEqual(target.read_text(), "Unrelated target content\n")
        self.assertFalse((self.home / "autonomous-se").exists())

    def test_in_project_managed_directory_alias_cannot_overwrite_unrelated_files(self):
        unrelated = self.project / "reference"
        unrelated.mkdir()
        original = unrelated / "SKILL.md"
        original.write_text("Keep unrelated reference\n")
        link = self.project / ".agents/skills/task-orchestrator"
        link.parent.mkdir(parents=True)
        try:
            link.symlink_to(unrelated, target_is_directory=True)
        except OSError:
            if os.name != "nt":
                raise
            p = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(unrelated)], capture_output=True, text=True)
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        with self.assertRaises(ValueError):
            self.install(project=self.project)
        self.assertEqual(original.read_text(), "Keep unrelated reference\n")
        self.assertFalse((self.home / "autonomous-se").exists())

    def test_explicit_project_root_directory_alias_still_selects_canonical_project(self):
        alias = self.base / "project-alias"
        try:
            alias.symlink_to(self.project, target_is_directory=True)
        except OSError:
            if os.name != "nt":
                raise
            p = subprocess.run(["cmd", "/c", "mklink", "/J", str(alias), str(self.project)], capture_output=True, text=True)
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        result = self.install(project=alias)
        self.assertEqual(result["project"], str(self.project.resolve()))
        self.assertTrue((self.project / ".agents/skills/task-orchestrator/SKILL.md").is_file())

    def test_runtime_only_installs_tooling_without_global_or_project_activation(self):
        self.install()
        self.assertTrue((self.home / "bin/ase.ps1").exists())
        self.assertEqual(self.global_agents.read_text(), "# Personal instructions\nKeep these intact.\n")
        self.assertFalse((self.home / "agents").exists())
        self.assertFalse((self.project / ".agents").exists())

    def test_global_powershell_launcher_restores_callers_pythonpath(self):
        powershell = shutil.which("pwsh") or shutil.which("powershell")
        if not powershell:
            self.skipTest("PowerShell unavailable")
        launcher = self.base / "ase.ps1"
        launcher.write_bytes(installer._launcher_bytes(ROOT)[0])
        quoted = str(launcher).replace("'", "''")
        command = ("Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue; "
                   f"& '{quoted}' --help | Out-Null; if ($LASTEXITCODE -ne 0) {{ exit $LASTEXITCODE }}; "
                   "if (Test-Path Env:PYTHONPATH) { throw 'unset PYTHONPATH was left set' }; "
                   "$env:PYTHONPATH = 'caller-value'; "
                   f"& '{quoted}' --help | Out-Null; if ($LASTEXITCODE -ne 0) {{ exit $LASTEXITCODE }}; "
                   "if ($env:PYTHONPATH -ne 'caller-value') { throw 'caller PYTHONPATH changed' }")
        result = subprocess.run([powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
                                cwd=self.base, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_no_cli_mode_fails_without_mutating_home(self):
        p = subprocess.run([sys.executable, str(ROOT / "scripts/install.py"), "--home", str(self.home),
                            "--skills", str(self.base / "isolated-skills")],
                           capture_output=True, text=True)
        self.assertEqual(p.returncode, 2)
        self.assertIn("--project", p.stderr)
        self.assertFalse((self.home / "autonomous-se").exists())

    def test_deprecated_verifier_registration_does_not_call_installer_or_register(self):
        with patch.object(installer, "install") as install, patch.object(installer, "register_verifier") as register, \
             contextlib.redirect_stderr(io.StringIO()) as output, self.assertRaises(SystemExit) as error:
            installer.main(["--home", str(self.home), "--register-verifier", "--root", str(self.project)])
        self.assertEqual(error.exception.code, 2)
        self.assertIn("deprecated", output.getvalue())
        install.assert_not_called()
        register.assert_not_called()

    def test_missing_verifier_runtime_is_reported_without_registration_or_config_change(self):
        before = self.config.read_bytes()
        with patch.object(installer.shutil, "which", return_value=sys.executable), \
             patch.object(installer.subprocess, "run") as process, self.assertRaises(SystemExit) as error:
            installer.register_verifier(self.home, [self.project], self.base / "state")
        self.assertIn("deprecated", str(error.exception))
        process.assert_not_called()
        self.assertEqual(self.config.read_bytes(), before)

    def test_project_skill_path_with_spaces_and_quotes_is_valid_toml(self):
        project = self.base / 'project "quoted"' if os.name != "nt" else self.base / "project 'quoted'"
        project.mkdir()
        self.install(project=project)
        entries = tomllib.loads((project / ".codex/config.toml").read_text())["skills"]["config"]
        self.assertTrue(all(Path(e["path"]).is_relative_to(project.resolve()) for e in entries))

    def test_malformed_managed_routing_is_not_duplicated_or_overwritten(self):
        path = self.project / "AGENTS.md"
        original = "# Team rules\n" + installer.BEGIN + "\nincomplete custom repair\n"
        path.write_text(original)
        with self.assertRaises(ValueError):
            self.install(project=self.project)
        self.assertEqual(path.read_text(), original)
        self.assertFalse((self.home / "autonomous-se").exists())

    def test_project_cli_rejects_global_destination_override(self):
        with self.assertRaises(SystemExit) as error, contextlib.redirect_stderr(io.StringIO()):
            installer.main(["--project", str(self.project), "--skills", str(self.base / "outside-skills")])
        self.assertEqual(error.exception.code, 2)
        self.assertFalse((self.base / "outside-skills").exists())

    def test_this_repository_installed_roles_equal_generated_sources(self):
        for source, installed, pattern in (("agents", ".codex/agents", "*.toml"),
                                           ("templates/agents/claude", ".claude/agents", "*.md"),
                                           ("templates/agents/copilot", ".github/agents", "*.md")):
            sources = {p.name: p.read_bytes() for p in (ROOT / source).glob(pattern)}
            copies = {p.name: p.read_bytes() for p in (ROOT / installed).glob(pattern)}
            self.assertEqual(len(sources), 3, source)
            self.assertEqual(copies, sources, installed + " is stale; run python scripts/build_assets.py")


class VendorDestinationTests(unittest.TestCase):
    def test_vendor_install_requires_destination_before_any_network_request(self):
        with patch.object(vendor_skills.urllib.request, "urlopen") as network:
            with contextlib.redirect_stderr(io.StringIO()) as output, self.assertRaises(SystemExit) as error:
                vendor_skills.main(["--stack", "angular"])
        self.assertEqual(error.exception.code, 2)
        self.assertIn("--dest", output.getvalue())
        network.assert_not_called()

    def test_vendor_inspection_needs_no_destination_and_performs_no_installation(self):
        with patch.object(vendor_skills.urllib.request, "urlopen", return_value=io.BytesIO(b"---\nname: angular-developer\n---\nFixture")), \
             patch.object(subprocess, "run") as process, contextlib.redirect_stdout(io.StringIO()) as output:
            vendor_skills.main(["--stack", "angular", "--ref", "a" * 40, "--inspect"])
        process.assert_not_called()
        self.assertIn("angular-developer", output.getvalue())

    def test_vendor_project_activation_keeps_owned_and_custom_disabled_entries(self):
        with tempfile.TemporaryDirectory(prefix="ase-vendor-project-") as temp:
            project = Path(temp)
            config = project / ".codex/config.toml"
            config.parent.mkdir()
            disabled = '[skills]\nconfig = [{name="finance-research", enabled=false}]\n'
            config.write_text(disabled)
            owned = project / ".agents/skills/quality-gate/SKILL.md"
            owned.parent.mkdir(parents=True)
            owned.write_text("# Fixture\n")
            _, data = installer.project_skill_config(project.resolve(), [owned])
            config.write_bytes(data)

            package = io.BytesIO()
            with zipfile.ZipFile(package, "w") as archive:
                archive.writestr("skills-" + "a" * 40 + "/angular-developer/SKILL.md",
                                 "---\nname: angular-developer\n---\nFixture\n")

            with patch.object(vendor_skills.urllib.request, "urlopen", return_value=io.BytesIO(package.getvalue())), \
                 patch.object(subprocess, "run", side_effect=AssertionError("No external installer")), \
                 patch.dict(os.environ, {"CODEX_HOME": str(project / "isolated-codex")}), \
                 contextlib.redirect_stdout(io.StringIO()):
                vendor_skills.main(["--stack", "angular", "--project", str(project), "--ref", "a" * 40])
            entries = tomllib.loads(config.read_text())["skills"]["config"]
            self.assertEqual(entries[0], {"name": "finance-research", "enabled": False})
            self.assertEqual({e["path"] for e in entries[1:]}, {
                str(owned), str(project / ".agents/skills/angular-developer/SKILL.md")})
            self.assertTrue(all(e["enabled"] is True for e in entries[1:]))
            lock = json.loads((project / ".agents/skills/ase-vendor-angular.json").read_text())
            self.assertEqual(lock["commit"], "a" * 40)
            self.assertTrue(any((project / ".codex/autonomous-se-backups").rglob("config.toml")))

    def test_vendor_project_symlink_escape_is_rejected_before_network(self):
        with tempfile.TemporaryDirectory(prefix="ase-vendor-escape-") as temp:
            root = Path(temp)
            project, outside = root / "project", root / "outside"
            project.mkdir()
            outside.mkdir()
            link = project / ".agents"
            try:
                link.symlink_to(outside, target_is_directory=True)
            except OSError:
                if os.name != "nt":
                    raise
                p = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(outside)], capture_output=True, text=True)
                self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
            with patch.object(vendor_skills.urllib.request, "urlopen") as network, self.assertRaises(ValueError):
                vendor_skills.main(["--stack", "angular", "--project", str(project), "--ref", "a" * 40])
            network.assert_not_called()
            self.assertEqual(list(outside.iterdir()), [])

    def test_vendor_known_file_type_conflicts_are_rejected_before_network_or_download(self):
        cases = [("--project", ".agents/skills/angular-developer/SKILL.md"),
                 ("--project", ".agents/skills/ase-vendor-angular.json"),
                 ("--project", ".codex/config.toml"),
                 ("--dest", "angular-developer/SKILL.md"),
                 ("--dest", "ase-vendor-angular.json")]
        for mode, relative in cases:
            with self.subTest(mode=mode, target=relative), tempfile.TemporaryDirectory(prefix="ase-vendor-file-conflict-") as temp:
                root = Path(temp)
                (root / relative).mkdir(parents=True)
                before = sorted(p.relative_to(root).as_posix() for p in root.rglob("*"))
                with patch.object(vendor_skills.urllib.request, "urlopen") as network, \
                     patch.object(subprocess, "run") as download, self.assertRaises(ValueError):
                    vendor_skills.main(["--stack", "angular", mode, str(root), "--ref", "a" * 40])
                network.assert_not_called()
                download.assert_not_called()
                self.assertEqual(sorted(p.relative_to(root).as_posix() for p in root.rglob("*")), before)


if __name__ == "__main__":
    unittest.main()
