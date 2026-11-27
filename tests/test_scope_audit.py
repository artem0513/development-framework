import importlib.util
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import unittest


spec = importlib.util.spec_from_file_location(
    "scope_audit", Path(__file__).resolve().parents[1] / "scripts/audit_project_scope.py")
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)


class ScopeAuditTests(unittest.TestCase):
    def test_actual_file_audit_passes_then_fails_if_domain_skills_disappear(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home, project, backup, domain = [root / x for x in ("home", "project", "backup", "domain")]
            for p in (home, project, backup / "agents", domain / ".agents/skills/finance-workflow"):
                p.mkdir(parents=True)
            source = Path(__file__).resolve().parents[1] / "skills"
            shutil.copytree(source, project / ".agents/skills")
            shutil.copytree(source, root / "global-skills")
            names = [p.parent.name for p in source.glob("*/SKILL.md")]
            base_config = 'model = "unchanged"\n'
            (backup / "config.toml").write_text(base_config)
            (home / "config.toml").write_text(base_config + "\n".join(
                '[[skills.config]]\nname = ' + json.dumps(n) + '\nenabled = false\n' for n in names))
            (project / ".codex/agents").mkdir(parents=True)
            (project / ".codex/config.toml").write_text("\n".join(
                '[[skills.config]]\npath = ' + json.dumps(str(p)) + '\nenabled = true\n'
                for p in (project / ".agents/skills").glob("*/SKILL.md")))
            (backup / "AGENTS.md").write_text("Preserved guidance\n<!-- autonomous-se managed begin -->old<!-- autonomous-se managed end -->")
            (home / "AGENTS.md").write_text("Preserved guidance\n<!-- project-workflow-scope begin -->local only<!-- project-workflow-scope end -->")
            (project / "AGENTS.md").write_text("<!-- autonomous-se managed begin -->.agents/skills/task-orchestrator/SKILL.md<!-- autonomous-se managed end -->")
            for p in (backup / "agents/ase-ac-verifier.toml", project / ".codex/agents/ase-ac-verifier.toml"):
                p.write_text("role fixture")
            skill = domain / ".agents/skills/finance-workflow/SKILL.md"
            skill.write_text("---\nname: finance-workflow\ndescription: Domain analysis\n---\nAnalyze portfolio data.")
            def observed(p):
                return {"path": str(p), "name": p.parent.name,
                        "sha256": hashlib.sha256(p.read_bytes()).hexdigest()}
            plan = {"skill_observations": [observed(p) for p in (root / "global-skills").glob("*/SKILL.md")],
                    "domain_files": [observed(skill)]}
            (backup / "scope-plan.json").write_text(json.dumps(plan))
            proof = {
                "global_config_sha256": hashlib.sha256((home / "config.toml").read_bytes()).hexdigest(),
                "project_config_sha256": hashlib.sha256((project / ".codex/config.toml").read_bytes()).hexdigest(),
                "rows": [
                    {"cwd": str(root / "neutral"), "enabled_engineering": [], "enabled_finance": []},
                    {"cwd": str(domain), "enabled_engineering": [], "enabled_finance": ["finance-workflow"]},
                    {"cwd": str(project), "enabled_engineering": [
                        {"name": p.parent.name, "scope": "repo", "path": str(p)}
                        for p in (project / ".agents/skills").glob("*/SKILL.md")], "enabled_finance": []},
                ],
            }
            (backup / "host-discovery.json").write_text(json.dumps(proof))
            self.assertEqual(audit.run(home, project, backup, domain)["status"], "PASS")
            (backup / "config.toml").write_text(base_config +
                '[[skills.config]]\nname = "unrelated-document-skill"\nenabled = false\n')
            lost_selector = audit.run(home, project, backup, domain)
            self.assertEqual(lost_selector["status"], "FAIL")
            self.assertFalse(lost_selector["checks"]["unrelated_skill_selectors_preserved"])
            (backup / "config.toml").write_text(base_config)
            proof["rows"][0]["enabled_engineering"] = [{"name": "tdd", "scope": "user", "path": "global"}]
            (backup / "host-discovery.json").write_text(json.dumps(proof))
            leaked = audit.run(home, project, backup, domain)
            self.assertEqual(leaked["status"], "FAIL")
            self.assertFalse(leaked["checks"]["fresh_host_discovery_matches_scope_and_configuration"])
            proof["rows"][0]["enabled_engineering"] = []
            (backup / "host-discovery.json").write_text(json.dumps(proof))
            skill.unlink()
            result = audit.run(home, project, backup, domain)
            self.assertEqual(result["status"], "FAIL")
            self.assertFalse(result["checks"]["domain_files_unchanged"])
            self.assertFalse(result["checks"]["domain_skills_enabled"])

    def test_project_path_reenables_only_the_selected_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            chosen = Path(tmp) / "project/.agents/skills/tdd/SKILL.md"
            other = Path(tmp) / "other/.agents/skills/tdd/SKILL.md"
            entries = [{"name": "tdd", "enabled": False},
                       {"path": str(chosen), "enabled": True}]
            self.assertTrue(audit.enabled_for(entries, {"tdd"}, chosen))
            self.assertFalse(audit.enabled_for(entries, {"tdd"}, other))

    def test_plugin_alias_is_recognized(self):
        self.assertFalse(audit.enabled_for(
            [{"name": "superpowers:tdd", "enabled": False}],
            {"tdd", "superpowers:tdd"}, Path("irrelevant/SKILL.md")))

    def test_domain_skill_remains_enabled(self):
        self.assertTrue(audit.enabled_for(
            [{"name": "tdd", "enabled": False}], {"finance-research"},
            Path("finance-research/SKILL.md")))

    def test_last_matching_entry_wins(self):
        self.assertFalse(audit.enabled_for(
            [{"name": "tdd", "enabled": True}, {"name": "tdd", "enabled": False}],
            {"tdd"}, Path("tdd/SKILL.md")))

    def test_scope_update_must_not_mask_other_configuration_changes(self):
        before = {"model": "original", "skills": {"config": []},
                  "mcp_servers": {"local": {"args": ["restricted-root"]}}}
        changed = {**before, "skills": {"config": [{"name": "tdd", "enabled": False}]}}
        self.assertEqual(audit.unrelated_config(before), audit.unrelated_config(changed))
        changed["mcp_servers"] = {"local": {"args": ["unrestricted-root"]}}
        self.assertNotEqual(audit.unrelated_config(before), audit.unrelated_config(changed))

    def test_unrelated_skill_selector_changes_fail_preservation(self):
        original = [{"name": "document-work", "enabled": False},
                    {"name": "finance-research", "enabled": True}]
        engineering = {"engineering_names": ["tdd"], "skill_observations": []}
        before = {"skills": {"config": original}}
        after = {"skills": {"config": original + [{"name": "tdd", "enabled": False}]}}
        self.assertEqual(audit.unrelated_skill_entries(before, engineering),
                         audit.unrelated_skill_entries(after, engineering))
        for entries in (original[1:], list(reversed(original)),
                        [{"name": "document-work", "enabled": True}, original[1]]):
            with self.subTest(entries=entries):
                self.assertNotEqual(audit.unrelated_skill_entries(before, engineering),
                                    audit.unrelated_skill_entries({"skills": {"config": entries}}, engineering))
