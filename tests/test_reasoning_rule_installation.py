"""Packaging/reachability checks, not a claim of model compliance."""
from pathlib import Path
import re
import tempfile
import unittest

from scripts import install as installer


ROOT = Path(__file__).resolve().parents[1]
REFERENCE = 'task-orchestrator/references/evidence-decisions.md'
BRIEF = 'task-orchestrator/assets/evidence-brief.md'
ENTRYPOINTS = ('task-orchestrator', 'task-intake', 'task-discovery',
    'independent-ac-verifier', 'independent-code-reviewer',
    'project-context-manager', 'quality-gate', 'communication-assistant')


def reachable(start, target, root):
    pending, visited = [start], set()
    while pending:
        current = pending.pop().resolve()
        if current in visited:
            continue
        if current == target.resolve():
            return True
        visited.add(current)
        for link in re.findall(r'\[[^\]]*\]\(([^)]+\.md)(?:#[^)]*)?\)', current.read_text(encoding='utf-8')):
            if '://' in link:
                continue
            linked = (current.parent / link).resolve()
            if not linked.is_relative_to(root.resolve()):
                raise AssertionError('Skill reference escapes installed skill root: ' + link)
            if not linked.is_file():
                raise AssertionError('Broken installed skill link: ' + str(linked))
            pending.append(linked)
    return False


class ReasoningRuleInstallationTests(unittest.TestCase):
    def test_all_three_packages_deliver_reachable_reference_and_brief(self):
        with tempfile.TemporaryDirectory(prefix='ase-reasoning-install-') as temporary:
            base = Path(temporary)
            project, home = base / 'project', base / 'tooling'
            project.mkdir()
            home.mkdir()
            result = installer.apply_install(installer.plan_install(
                ['codex', 'claude', 'copilot'], project, runtime_home=home))
            self.assertEqual(result['scope'], 'project')
            for relative in ('.agents/skills', '.claude/skills', '.github/skills'):
                skills = project / relative
                for artifact in (REFERENCE, BRIEF):
                    with self.subTest(backend=relative, artifact=artifact):
                        self.assertTrue((skills / artifact).is_file(), 'Required portable artifact is missing')
                        self.assertEqual((skills / artifact).read_bytes(),
                            (ROOT / 'skills' / artifact).read_bytes())
                for name in ENTRYPOINTS:
                    with self.subTest(backend=relative, entrypoint=name):
                        entrypoint = skills / name / 'SKILL.md'
                        self.assertTrue(reachable(entrypoint, skills / REFERENCE, skills))
                        self.assertTrue(reachable(entrypoint, skills / BRIEF, skills))

    def test_adopted_repository_mirrors_are_current(self):
        for name in ENTRYPOINTS:
            canonical = ROOT / 'skills' / name
            for source in canonical.rglob('*.md'):
                with self.subTest(path=str(source.relative_to(ROOT))):
                    adopted = ROOT / '.agents' / source.relative_to(ROOT)
                    self.assertEqual(adopted.read_bytes(), source.read_bytes())


if __name__ == '__main__':
    unittest.main()
