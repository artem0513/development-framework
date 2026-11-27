"""Filesystem/protocol checks; these do not establish model routing behavior."""
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import tomllib
import unittest
from unittest.mock import patch
import zipfile

from scripts import install as installer
from scripts import vendor_skills
from scripts import audit_project_scope


ROOT = Path(__file__).resolve().parents[1]


def snapshot(root):
    return {p.relative_to(root).as_posix(): None if p.is_dir() else p.read_bytes()
            for p in root.rglob("*")}


class PortableInstallTests(unittest.TestCase):
    def test_all_three_entrypoints_require_investigation_before_owner_questions(self):
        """Installed entrypoint coverage; native behavior is checked separately."""
        pointers = ('AGENTS.md', 'CLAUDE.md', '.github/copilot-instructions.md')
        for name in pointers:
            path = self.project / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('Keep existing owner instructions.\n', encoding='utf-8')
        installer.apply_install(self.plan(['codex', 'claude', 'copilot']))
        for name in pointers:
            with self.subTest(entrypoint=name):
                body = (self.project / name).read_text(encoding='utf-8')
                self.assertTrue(body.startswith('Keep existing owner instructions.'))
                managed = body.split(installer.BEGIN, 1)[1].split(installer.END, 1)[0]
                self.assertIn('Before asking', managed)
                self.assertIn('supplied context', managed)
                self.assertIn('read-only', managed)
                self.assertIn('owner-only decisions', managed)
                self.assertIn('hypothesis/recommendation', managed)

    def test_local_credential_reference_is_copied_for_all_three_backends(self):
        installer.apply_install(self.plan(['codex', 'claude', 'copilot']))
        for directory in ('.agents/skills', '.claude/skills', '.github/skills'):
            root = self.project / directory
            reference = root / 'task-orchestrator/references/local-credentials.md'
            self.assertEqual(reference.read_bytes(), (ROOT / 'skills/task-orchestrator/references/local-credentials.md').read_bytes())
            self.assertIn('references/local-credentials.md', (root / 'task-orchestrator/SKILL.md').read_text())

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="ase-portable-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.home, self.project = self.base / "tooling", self.base / "workspace"
        self.home.mkdir()
        self.project.mkdir()

    def plan(self, targets):
        self.assertTrue(callable(getattr(installer, "plan_install", None)),
                        "Portable installation needs a read-only plan API")
        return installer.plan_install(targets, self.project, runtime_home=self.home)

    def test_one_source_three_targets(self):
        before = snapshot(self.base)
        manifest = self.plan(["codex", "claude", "copilot"])
        self.assertEqual(snapshot(self.base), before, "preview must not create paths")
        self.assertEqual(set(manifest["targets"]), {"codex", "claude", "copilot"})
        json.dumps(manifest)  # Reviewable, serializable manifest, no hidden Path/bytes state.
        result = installer.apply_install(json.loads(json.dumps(manifest)))
        self.assertEqual(result["scope"], "project")
        canonical = sorted((ROOT / "skills").glob("*/SKILL.md"))
        self.assertEqual(len(canonical), 11)
        for target, relative, native in (("codex", ".agents/skills", ".codex/agents"),
                                         ("claude", ".claude/skills", ".claude/agents"),
                                         ("copilot", ".github/skills", ".github/agents")):
            installed = self.project / relative
            self.assertEqual(len(list(installed.glob("*/SKILL.md"))), 11)
            for source in (ROOT / "skills").rglob("*"):
                if source.is_file() and "__pycache__" not in source.parts and source.suffix != ".pyc":
                    self.assertEqual((installed / source.relative_to(ROOT / "skills")).read_bytes(), source.read_bytes())
            roles = list((self.project / native).glob("*"))
            self.assertEqual(len(roles), 3, target)
            for role in roles:
                if target == "codex":
                    body = tomllib.loads(role.read_text())["developer_instructions"]
                else:
                    body = role.read_text()
                self.assertIn(relative + "/", body)
                for skill in ("task-orchestrator", "independent-ac-verifier", "independent-code-reviewer"):
                    if skill in body:
                        self.assertTrue((installed / skill / "SKILL.md").is_file())
                self.assertNotIn("$CODEX_HOME", body)
                self.assertIn("never stage, commit or mention framework files", body)
                self.assertIn("treat a lead's suggested fix as a hypothesis", body)
                read_only = role.name.split(".")[0] in ("ase-ac-verifier", "ase-code-reviewer")
                self.assertEqual("Stay read-only" in body, read_only, role.name)
                if target != "codex":
                    header = body.split("\n---\n", 1)[0]
                    tools = [line for line in header.splitlines() if line.startswith("tools:")]
                    expected = {"claude": ["tools: Read, Grep, Glob, Bash"],
                                "copilot": ["tools: ['read', 'search', 'execute']"]}[target]
                    self.assertEqual(tools, expected if read_only else [], role.name)
        for pointer, relative in (("AGENTS.md", ".agents/skills"),
                                  ("CLAUDE.md", ".claude/skills"),
                                  (".github/copilot-instructions.md", ".github/skills")):
            self.assertIn(relative + "/task-orchestrator/SKILL.md", (self.project / pointer).read_text())

    def test_path_input_selects_exact_directory_instead_of_path_root_property(self):
        manifest = self.plan(["codex"])
        self.assertEqual(manifest["project"], str(self.project.resolve()))
        self.assertNotEqual(manifest["project"], self.project.root)
        project_paths = [Path(item["path"]) for item in manifest["files"]
                         if item["scope_root"] != str(self.home)]
        self.assertTrue(project_paths)
        self.assertTrue(all(path.is_relative_to(self.project) for path in project_paths))

    def test_native_parallel_policy_pointer_resolves_for_each_backend(self):
        """Descriptor/package structure only; model behavior is tested separately."""
        installer.apply_install(self.plan(['codex', 'claude', 'copilot']))
        for target, skills, roles in (('codex', '.agents/skills', '.codex/agents'),
                                      ('claude', '.claude/skills', '.claude/agents'),
                                      ('copilot', '.github/skills', '.github/agents')):
            with self.subTest(target=target):
                reference = skills + '/task-orchestrator/references/parallel-work.md'
                self.assertTrue((self.project / reference).is_file(), 'Missing portable conditional parallel policy')
                for role in (self.project / roles).iterdir():
                    content = (tomllib.loads(role.read_text())['developer_instructions']
                               if target == 'codex' else role.read_text())
                    # Read-only reviewer/verifier roles cannot spawn subagents.
                    if role.name.startswith(('ase-ac-verifier', 'ase-code-reviewer')):
                        self.assertNotIn(reference, content)
                    else:
                        self.assertIn(reference, content)

    def test_install_preserves_other_projects_and_user_settings(self):
        other = self.base / "other"
        other.mkdir()
        (other / "CLAUDE.md").write_text("Other project stays independent.\n")
        (self.home / "config.toml").write_text('# Private user settings\nmodel="preserve"\n')
        (self.home / "AGENTS.md").write_text("Personal instructions\n")
        config = self.project / ".codex/config.toml"
        config.parent.mkdir()
        original = '# project comments\nmodel="team"\nskills={config=[{name="finance",enabled=false}]}\n'
        config.write_text(original)
        (self.project / "CLAUDE.md").write_text("Keep project rules.\n")
        protected = {p: p.read_bytes() for p in (other / "CLAUDE.md", self.home / "config.toml", self.home / "AGENTS.md")}
        installer.apply_install(self.plan(["codex", "claude", "copilot"]))
        first = snapshot(self.base)
        second = installer.apply_install(self.plan(["codex", "claude", "copilot"]))
        self.assertEqual(second["changed"], [])
        self.assertEqual(snapshot(self.base), first)
        for path, data in protected.items():
            self.assertEqual(path.read_bytes(), data)
        self.assertTrue(any(p.read_text() == original for p in (self.home / "backups").rglob("config.toml")))
        parsed = tomllib.loads(config.read_text())
        self.assertEqual(parsed["skills"]["config"][0], {"name": "finance", "enabled": False})
        self.assertIn("# project comments", config.read_text())
        self.assertFalse((self.home / "agents").exists())
        self.assertFalse((self.home / ".claude").exists())

    def test_apply_rejects_changed_inputs_and_tampered_payload_before_any_write(self):
        manifest = self.plan(["codex"])
        (self.project / "AGENTS.md").write_text("Concurrent human edit\n")
        before = snapshot(self.base)
        with self.assertRaisesRegex(ValueError, "[Ss]tale|changed"):
            installer.apply_install(manifest)
        self.assertEqual(snapshot(self.base), before)
        manifest = self.plan(["codex"])
        manifest["files"][0]["content_base64"] = "dGFtcGVyZWQ="
        with self.assertRaisesRegex(ValueError, "[Hh]ash|[Mm]anifest|digest"):
            installer.apply_install(manifest)
        self.assertEqual(snapshot(self.base), before)

    def test_apply_manifest_accepts_powershell_redirect_encodings_and_rejects_undecodable_bytes(self):
        text = json.dumps(self.plan(["codex"]), indent=2) + "\r\n"
        for name, data in (("utf-16le-bom", b"\xff\xfe" + text.encode("utf-16-le")),
                           ("utf-16be-bom", b"\xfe\xff" + text.encode("utf-16-be")),
                           ("utf-8-bom", b"\xef\xbb\xbf" + text.encode("utf-8"))):
            with self.subTest(encoding=name):
                manifest = self.base / (name + ".json")
                manifest.write_bytes(data)
                self.assertEqual(installer.read_manifest(manifest), json.loads(text))
        manifest = self.base / "utf-16le-bom.json"
        with contextlib.redirect_stdout(io.StringIO()) as output:
            installer.main(["--apply-manifest", str(manifest)])
        self.assertEqual(json.loads(output.getvalue())["scope"], "project")
        self.assertTrue((self.project / "AGENTS.md").is_file())
        before = snapshot(self.base)
        for name, data in (("bomless-utf-16", text.encode("utf-16-le")), ("not-json", b"\xef\xbb\xbfnot json"),
                           ("array", b"[]")):
            with self.subTest(invalid=name):
                bad = self.base / (name + ".json")
                bad.write_bytes(data)
                before[bad.name] = data
                with contextlib.redirect_stderr(io.StringIO()) as error, self.assertRaises(SystemExit) as stop:
                    installer.main(["--apply-manifest", str(bad)])
                self.assertEqual(stop.exception.code, 2)
                self.assertIn("--apply-manifest", error.getvalue())
                self.assertIn("regenerate it with --preview", error.getvalue())
                self.assertEqual(snapshot(self.base), before)

    def test_preflight_rejects_hardlinks_and_all_target_type_conflicts(self):
        for relative in (".claude/skills/task-orchestrator/SKILL.md", ".github/agents/ase-ac-verifier.agent.md",
                         ".github/copilot-instructions.md", ".codex/config.toml"):
            with self.subTest(relative=relative), tempfile.TemporaryDirectory(dir=self.base) as temp:
                project = Path(temp)
                (project / relative).mkdir(parents=True)
                before = snapshot(self.base)
                with self.assertRaises(ValueError):
                    installer.plan_install(["codex", "claude", "copilot"], project, runtime_home=self.home)
                self.assertEqual(snapshot(self.base), before)
        external = self.base / "outside.md"
        external.write_text("Outside\n")
        os.link(external, self.project / "CLAUDE.md")
        before = snapshot(self.base)
        with self.assertRaises(ValueError):
            self.plan(["claude"])
        self.assertEqual(snapshot(self.base), before)

    def test_runtime_only_has_no_discoverable_activation_and_target_is_explicit(self):
        self.assertTrue(callable(getattr(installer, "plan_install", None)))
        installer.apply_install(installer.plan_install(["codex"], None, runtime_home=self.home))
        self.assertTrue((self.home / "bin/ase.ps1").is_file())
        self.assertEqual(list(self.project.iterdir()), [])
        self.assertFalse((self.home / "AGENTS.md").exists())
        self.assertFalse((self.home / "config.toml").exists())
        launcher = (self.home / "bin/ase.ps1").read_text()
        self.assertNotIn("$env:CODEX_HOME", launcher)
        with self.assertRaises(ValueError):
            self.plan([])
        with self.assertRaises(ValueError):
            self.plan(["unrecognized"])

    def test_read_only_manifest_audit_detects_a_modified_owned_file(self):
        self.assertTrue(callable(getattr(audit_project_scope, 'audit_install_manifest', None)),
                        'Portable manifests need a backend-neutral file audit')
        manifest = self.plan(['codex', 'claude', 'copilot'])
        installer.apply_install(manifest)
        before = snapshot(self.base)
        self.assertEqual(audit_project_scope.audit_install_manifest(manifest)['status'], 'PASS')
        self.assertEqual(snapshot(self.base), before)
        skill = self.project / '.claude/skills/task-intake/SKILL.md'
        skill.write_text('Modified after installation\n')
        result = audit_project_scope.audit_install_manifest(manifest)
        self.assertEqual(result['status'], 'FAIL')
        self.assertIn(str(skill), result['mismatched_files'])

    def test_runtime_only_each_backend_stays_in_disposable_tooling_scope(self):
        before = snapshot(self.project)
        for target in ('codex', 'claude', 'copilot'):
            with self.subTest(target=target):
                home = self.home / target
                manifest = installer.plan_install([target], None, runtime_home=home)
                self.assertFalse(home.exists())
                result = installer.apply_install(manifest)
                self.assertEqual(result['scope'], 'runtime')
                self.assertEqual(result['targets'], [target])
                self.assertTrue((home / 'bin/ase.ps1').is_file())
                self.assertFalse((home / 'AGENTS.md').exists())
                self.assertFalse((home / '.claude').exists())
                self.assertFalse((home / '.github').exists())
        self.assertEqual(snapshot(self.project), before)

    def test_deprecated_registration_is_explicit_error_without_writes(self):
        before = snapshot(self.base)
        command = [sys.executable, str(ROOT / "scripts/install.py"), "--runtime-only", "--home", str(self.home),
                   "--register-verifier", "--root", str(self.project)]
        result = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn("deprecated", result.stderr)
        self.assertEqual(snapshot(self.base), before)


class ManagedMcpCleanupTests(unittest.TestCase):
    def test_managed_mcp_cleanup_requires_explicit_authorization(self):
        self.assertTrue(callable(getattr(installer, "plan_mcp_cleanup", None)),
                        "Existing registration cleanup needs an explicit preview")
        with tempfile.TemporaryDirectory(prefix="ase-cleanup-") as temp:
            home = Path(temp)
            config = home / "config.toml"
            original = ('# personal settings\nmodel="keep"\n[permissions]\nmode="prompt"\n'
                        '[mcp_servers.other]\ncommand="keep-tool"\n'
                        '[mcp_servers.autonomous_se]\ncommand="python"\nargs=["-P","-m","ase.host","--root","D:/project"]\n'
                        '[mcp_servers.autonomous_se.env]\nPYTHONPATH="D:/tooling/autonomous-se/runtime"\n'
                        '[mcp_servers.autonomous_se.tools.verification_start]\napproval_mode="approve"\n'
                        '[hooks]\nvalue="keep-hook"\n[auth]\nmethod="preserve"\n')
            config.write_text(original)
            before = snapshot(home)
            manifest = installer.plan_mcp_cleanup(config)
            self.assertEqual(snapshot(home), before)
            self.assertEqual(manifest["owned_entry"], "autonomous_se")
            with self.assertRaisesRegex(ValueError, "authorization"):
                installer.apply_mcp_cleanup(manifest, authorization_ref=" ")
            self.assertEqual(snapshot(home), before)
            result = installer.apply_mcp_cleanup(json.loads(json.dumps(manifest)), authorization_ref="user:explicit-migration")
            expected = tomllib.loads(original)
            del expected["mcp_servers"]["autonomous_se"]
            self.assertEqual(tomllib.loads(config.read_text()), expected)
            self.assertIn("# personal settings", config.read_text())
            self.assertEqual(Path(result["backup"]).read_text(), original)
            self.assertEqual(result["authorization_ref"], "user:explicit-migration")
            self.assertFalse(installer.plan_mcp_cleanup(config)["changed"])

    def test_cleanup_rejects_unowned_registration_and_stale_preview(self):
        self.assertTrue(callable(getattr(installer, "plan_mcp_cleanup", None)))
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "config.toml"
            path.write_text('[mcp_servers.autonomous_se]\ncommand="someone-elses-tool"\n')
            before = path.read_bytes()
            with self.assertRaisesRegex(ValueError, "owned|ownership"):
                installer.plan_mcp_cleanup(path)
            self.assertEqual(path.read_bytes(), before)

    def test_cleanup_preserves_comments_multiline_values_and_rejects_stale_config(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'config.toml'
            original = ('# user note\n[mcp_servers."autonomous_se"]\ncommand="python"\n'
                        'args = [\n"-P",\n"-m",\n"ase.host",\n]\n'
                        'env={PYTHONPATH="D:/home/autonomous-se/runtime"}\n'
                        '\n# keep this unrelated comment\n[other]\n'
                        'date=2026-11-12\ntext="""\n[mcp_servers.autonomous_se]\nnot_a_header\n"""\n')
            path.write_text(original)
            manifest = installer.plan_mcp_cleanup(path)
            path.write_text(original + '# concurrent edit\n')
            before = snapshot(path.parent)
            with self.assertRaisesRegex(ValueError, 'Stale|changed'):
                installer.apply_mcp_cleanup(manifest, authorization_ref='user:migrate')
            self.assertEqual(snapshot(path.parent), before)
            manifest = installer.plan_mcp_cleanup(path)
            installer.apply_mcp_cleanup(manifest, authorization_ref='user:migrate')
            self.assertIn('# keep this unrelated comment', path.read_text())
            self.assertIn('not_a_header', path.read_text())


class VendorPackageTests(unittest.TestCase):
    def package(self, entries=None):
        content = io.BytesIO()
        with zipfile.ZipFile(content, 'w') as archive:
            for name, value in (entries or {
                'angular-developer/SKILL.md': b'---\nname: angular-developer\n---\nUse references/check.md\n',
                'angular-developer/references/check.md': b'Pinned reference\n',
                'angular-developer/scripts/tool.py': b'raise RuntimeError("must never run")\n',
            }).items():
                archive.writestr('skills-' + 'a' * 40 + '/' + name, value)
        return content.getvalue()

    def test_pinned_vendor_copies_complete_package_without_provider_installer(self):
        with tempfile.TemporaryDirectory(prefix='ase-vendor-package-') as temp:
            project = Path(temp)
            config = project / '.codex/config.toml'
            config.parent.mkdir()
            config.write_text('# retain\nmodel="team"\n')
            with patch.object(vendor_skills.urllib.request, 'urlopen', return_value=io.BytesIO(self.package())), \
                 patch.object(subprocess, 'run', side_effect=AssertionError('No downloaded installer execution')), \
                 contextlib.redirect_stdout(io.StringIO()):
                vendor_skills.main(['--stack', 'angular', '--project', str(project), '--ref', 'a' * 40])
            installed = project / '.agents/skills/angular-developer'
            self.assertEqual((installed / 'references/check.md').read_text(), 'Pinned reference\n')
            self.assertTrue((installed / 'scripts/tool.py').is_file())
            lock = json.loads((installed.parent / 'ase-vendor-angular.json').read_text())
            self.assertEqual(lock['commit'], 'a' * 40)
            self.assertEqual(lock['files']['angular-developer/references/check.md'], hashlib.sha256(b'Pinned reference\n').hexdigest())
            self.assertEqual(tomllib.loads(config.read_text())['model'], 'team')
            self.assertIn('# retain', config.read_text())

    def test_vendor_archive_escape_fails_before_project_or_config_write(self):
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            malicious = self.package({'angular-developer/../../escape.md': b'escape',
                                      'angular-developer/SKILL.md': b'---\nname: angular-developer\n---\n'})
            before = snapshot(project)
            with patch.object(vendor_skills.urllib.request, 'urlopen', return_value=io.BytesIO(malicious)), \
                 self.assertRaises(ValueError):
                vendor_skills.main(['--stack', 'angular', '--project', str(project), '--ref', 'a' * 40])
            self.assertEqual(snapshot(project), before)

    def test_vendor_archive_file_parent_collision_fails_before_any_write(self):
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            malicious = self.package({'angular-developer/SKILL.md': b'---\nname: angular-developer\n---\n',
                                      'angular-developer/SKILL.md/child.md': b'Impossible file parent\n'})
            before = snapshot(project)
            with patch.object(vendor_skills.urllib.request, 'urlopen', return_value=io.BytesIO(malicious)), \
                 self.assertRaises(ValueError):
                vendor_skills.main(['--stack', 'angular', '--project', str(project), '--ref', 'a' * 40])
            self.assertEqual(snapshot(project), before)


if __name__ == "__main__":
    unittest.main()
