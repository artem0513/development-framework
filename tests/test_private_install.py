"""Private installation: outside its own repository the framework never reaches a project's Git history."""
from pathlib import Path
import importlib.util
import shutil
import subprocess
import tempfile
import unittest

from unittest.mock import patch
import sqlite3

from ase.finish import finish, plain_body, private_repository
from ase.project_local import PRIVATE_BEGIN

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('ase_private_installer', ROOT / 'scripts/install.py')
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)

ALL = ['codex', 'claude', 'copilot']


@unittest.skipUnless(shutil.which('git'), 'git required')
class PrivateInstallTests(unittest.TestCase):
    def setUp(self):
        self.base = Path(tempfile.mkdtemp(prefix='ase-private-')).resolve()
        self.addCleanup(shutil.rmtree, self.base, ignore_errors=True)
        self.project, self.home = self.base / 'team', self.base / 'tooling'
        self.project.mkdir()
        self.home.mkdir()
        self.git('init', '-q', '-b', 'main')
        self.git('config', 'user.email', 'team@example.invalid')
        self.git('config', 'user.name', 'team')
        self.write('AGENTS.md', 'Team rules for every agent.\n')
        self.write('app.py', 'print(1)\n')
        self.commit('team baseline')

    def git(self, *args):
        return subprocess.run(['git', '-C', str(self.project), *args], check=True, capture_output=True).stdout.decode()

    def write(self, rel, text):
        path = self.project / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode('utf-8'))

    def commit(self, message):
        self.git('add', '-A')
        self.git('commit', '-q', '-m', message)

    def visible(self):
        """What a teammate's commit could pick up: every change Git reports, untracked files included."""
        return self.git('status', '--porcelain', '--untracked-files=all').splitlines()

    def install(self, targets=ALL, **options):
        home = None if options.get('project_local') else self.home
        return installer.apply_install(installer.plan_install(targets, self.project, runtime_home=home, **options))

    def test_installation_is_invisible_to_git_and_leaves_team_instructions_alone(self):
        result = self.install()
        self.assertTrue(result['private'])
        self.assertEqual(self.visible(), [])
        self.assertEqual((self.project / 'AGENTS.md').read_text(encoding='utf-8'), 'Team rules for every agent.\n')
        override = (self.project / 'AGENTS.override.md').read_text(encoding='utf-8')
        self.assertIn('read and follow this directory\'s AGENTS.md', override)
        for pointer in ('AGENTS.override.md', 'CLAUDE.local.md', '.github/instructions/devframework.instructions.md'):
            with self.subTest(pointer=pointer):
                text = (self.project / pointer).read_text(encoding='utf-8')
                self.assertIn('Push only the task\'s own changes', text)
                self.assertIn('never add framework CI, hooks', text)
        self.assertTrue((self.project / '.github/instructions/devframework.instructions.md').read_text(
            encoding='utf-8').startswith('---\napplyTo: "**"\n---\n'))
        self.assertTrue((self.project / '.claude/skills/task-orchestrator/SKILL.md').is_file())
        for role in ('.codex/agents/ase-code-reviewer.toml', '.claude/agents/ase-ac-verifier.md',
                     '.github/agents/ase-implementer.agent.md'):
            with self.subTest(role=role):
                text = (self.project / role).read_text(encoding='utf-8')
                self.assertIn('CLAUDE.local.md', text)
                self.assertIn('AGENTS.override.md', text)
                self.assertIn('never stage, commit or mention framework files', text)
        self.assertFalse((self.project / 'CLAUDE.md').exists())
        self.assertEqual(self.install()['changed'], [])

    def test_owner_hints_reach_every_local_instruction_file_and_stay_invisible(self):
        result = self.install()
        self.assertIsNone(result['hints'])
        self.assertNotIn(installer.HINTS_INTRO, (self.project / 'CLAUDE.local.md').read_text(encoding='utf-8'))
        # Written after the install: already excluded, so a teammate's commit cannot pick it up.
        self.write(installer.HINTS_FILE, '- clones/analytics is a nested repo; git grep does not see it.\n')
        self.assertEqual(self.visible(), [])
        result = self.install()
        self.assertEqual(result['hints'], str(self.project / installer.HINTS_FILE))
        for pointer in ('AGENTS.override.md', 'CLAUDE.local.md', '.github/instructions/devframework.instructions.md'):
            with self.subTest(pointer=pointer):
                text = (self.project / pointer).read_text(encoding='utf-8')
                self.assertIn(installer.HINTS_INTRO + '- clones/analytics is a nested repo', text)
                self.assertLess(text.index('nested repo'), text.index(installer.END))
        self.assertEqual((self.project / 'AGENTS.md').read_text(encoding='utf-8'), 'Team rules for every agent.\n')
        self.assertEqual(self.visible(), [])
        self.assertEqual(self.install()['changed'], [])
        self.write(installer.HINTS_FILE, '- Ask the owner before changing what we send to ShopHub.\n')
        text = (self.project / 'CLAUDE.local.md').read_text(encoding='utf-8') if self.install() else ''
        self.assertIn('ShopHub', text)
        self.assertNotIn('nested repo', text)

    def test_project_local_hints_are_used_and_invisible(self):
        self.write(installer.HINTS_FILE, 'ParcelNet is documented in docs/vendors; read it, do not ask.\n')
        self.install(['claude'], project_local=True)
        self.assertIn('ParcelNet is documented', (self.project / 'CLAUDE.local.md').read_text(encoding='utf-8'))
        self.assertEqual(self.visible(), [])

    def test_unusable_hints_are_refused_before_any_write(self):
        for text in ('x' * (installer.MAX_HINTS_BYTES + 1), 'see ' + installer.END):
            with self.subTest(size=len(text)):
                self.write(installer.HINTS_FILE, text)
                with self.assertRaises(ValueError):
                    installer.plan_install(['claude'], self.project, runtime_home=self.home)
                self.assertFalse((self.project / 'CLAUDE.local.md').exists())

    def test_project_local_installation_is_invisible_too(self):
        result = self.install(['claude'], project_local=True)
        self.assertTrue(result['private'])
        self.assertEqual(self.visible(), [])
        self.assertTrue((self.project / '.devframework/bin/ase.ps1').is_file())

    def test_later_targets_keep_earlier_exclusions(self):
        self.install(['codex'])
        self.install(['claude'])
        self.assertEqual(self.visible(), [])
        exclude = (self.project / '.git/info/exclude').read_text(encoding='utf-8')
        self.assertIn('/.agents/skills/task-orchestrator/', exclude)
        self.assertIn('/CLAUDE.local.md', exclude)
        self.assertEqual(exclude.count(PRIVATE_BEGIN), 1)

    def test_an_earlier_shared_block_is_removed_from_team_files(self):
        self.write('CLAUDE.md', 'Team Claude rules.\n')
        self.commit('team claude rules')
        self.write('CLAUDE.md', 'Team Claude rules.\n\n' + installer._routing('.claude/skills') + '\n')
        result = self.install(['claude'])
        self.assertEqual((self.project / 'CLAUDE.md').read_text(encoding='utf-8'), 'Team Claude rules.\n')
        self.assertEqual(result['removed_shared_blocks'], [str(self.project / 'CLAUDE.md')])
        self.assertEqual(self.visible(), [])

    def test_tracked_owned_files_are_refused_before_any_write(self):
        self.write('CLAUDE.local.md', 'committed by mistake\n')
        self.commit('oops')
        before = self.visible()
        with self.assertRaisesRegex(ValueError, 'tracked by Git'):
            installer.plan_install(['claude'], self.project, runtime_home=self.home)
        self.assertEqual(self.visible(), before)
        self.assertFalse((self.project / '.claude').exists())

    def test_a_file_tracked_after_the_preview_blocks_apply(self):
        manifest = installer.plan_install(['claude'], self.project, runtime_home=self.home)
        self.write('CLAUDE.local.md', 'tracked meanwhile\n')
        self.commit('meanwhile')
        with self.assertRaisesRegex(ValueError, 'tracked by Git since the preview'):
            installer.apply_install(manifest)
        self.assertFalse((self.project / '.claude').exists())

    def test_framework_source_is_the_only_shared_repository(self):
        self.assertTrue(installer.is_framework_source(ROOT))
        self.assertFalse(installer.is_framework_source(self.project))
        plain = self.base / 'not-git'
        plain.mkdir()
        manifest = installer.plan_install(['claude'], plain, runtime_home=self.home)
        self.assertFalse(manifest['private'])
        self.assertTrue(any(item['path'].endswith('CLAUDE.md') for item in manifest['files']))

    def test_hints_stay_out_of_shared_instruction_files(self):
        plain = self.base / 'not-git'
        (plain / '.devframework').mkdir(parents=True)
        (plain / installer.HINTS_FILE).write_text('ShopHub: ask the owner.\n', encoding='utf-8')
        manifest = installer.plan_install(['claude'], plain, runtime_home=self.home)
        self.assertIsNone(manifest['hints'])
        self.write(installer.HINTS_FILE, 'ShopHub: ask the owner.\n')
        self.git('remote', 'add', 'origin', 'https://github.com/artem0513/DevelopmentFramework.git')
        manifest = installer.plan_install(['claude'], self.project, runtime_home=self.home)
        self.assertFalse(manifest['private'])
        self.assertIsNone(manifest['hints'])
        for item in manifest['files']:
            if item['path'].endswith('CLAUDE.md'):
                self.assertNotIn(b'ShopHub', installer.base64.b64decode(item['content_base64']))

    def test_crlf_hints_are_normalized_and_control_characters_refused(self):
        self.write(installer.HINTS_FILE, '- first\r\n- second\r\n')
        self.install(['claude'])
        self.assertIn(installer.HINTS_INTRO + '- first\n- second\n',
                      (self.project / 'CLAUDE.local.md').read_text(encoding='utf-8'))
        self.write(installer.HINTS_FILE, '- first\0\n')
        with self.assertRaisesRegex(ValueError, 'control characters'):
            installer.plan_install(['claude'], self.project, runtime_home=self.home)

    def test_negating_gitignore_stops_before_any_framework_file(self):
        self.write('.gitignore', '*\n!*/\n!*.md\n')
        self.git('add', '-f', '.gitignore')
        self.git('commit', '-q', '-m', 'whitelist ignore')
        manifest = installer.plan_install(['claude'], self.project, runtime_home=self.home)
        with self.assertRaisesRegex(ValueError, 'negating .gitignore'):
            installer.apply_install(manifest)
        self.assertFalse((self.project / '.claude').exists())
        self.assertFalse((self.project / 'CLAUDE.local.md').exists())

    def test_committed_shared_block_is_left_for_the_owner(self):
        self.write('CLAUDE.md', 'Team Claude rules.\n\n' + installer._routing('.claude/skills') + '\n')
        self.commit('shared install committed earlier')
        manifest = installer.plan_install(['claude'], self.project, runtime_home=self.home)
        self.assertEqual(manifest['owner_cleanup'], [str(self.project / 'CLAUDE.md')])
        installer.apply_install(manifest)
        self.assertEqual(self.visible(), [])

    def test_untracked_file_holding_only_a_shared_block_is_deleted(self):
        self.write('CLAUDE.md', installer._routing('.claude/skills') + '\n')
        self.install(['claude'])
        self.assertFalse((self.project / 'CLAUDE.md').exists())
        self.assertNotIn('/CLAUDE.md\n', (self.project / '.git/info/exclude').read_text(encoding='utf-8'))
        self.assertEqual(self.visible(), [])

    def test_subdirectory_project_and_linked_worktree_stay_invisible(self):
        sub = self.project / 'service'
        sub.mkdir()
        installer.apply_install(installer.plan_install(['claude'], sub, runtime_home=self.home))
        self.assertIn('/service/CLAUDE.local.md', (self.project / '.git/info/exclude').read_text(encoding='utf-8'))
        self.assertEqual(self.visible(), [])
        worktree = self.base / 'wt'
        self.git('worktree', 'add', '-q', '-b', 'side', str(worktree))
        installer.apply_install(installer.plan_install(['codex'], worktree, runtime_home=self.home))
        status = subprocess.run(['git', '-C', str(worktree), 'status', '--porcelain', '--untracked-files=all'],
                                check=True, capture_output=True).stdout.decode()
        self.assertEqual(status, '')

    def finish_here(self, body_file=None):
        store = type('Store', (), {})()
        store.repo = self.project
        store.db = sqlite3.connect(':memory:')
        store.db.execute('CREATE TABLE runtime_records(kind TEXT, key TEXT, revision INT, payload TEXT, updated TEXT)')
        task = {'task_id': 'wc', 'contract': {'id': 'wc', 'request': 'Count words separated by any whitespace.'},
                'proposal': {'summary': 'Use str.split().'}}
        store.project_id = 'private-project'
        store.task = lambda task_id: task
        gate = {'status': 'APPROVED', 'reasons': [], 'profile_evidence': {'by_id': {'tests': {'status': 'PASS',
                'counts': {'total': 8, 'failed': 0, 'skipped': 0}}}}}
        with patch('ase.tasks.TaskService.snapshot', side_effect=lambda task_id: task), \
                patch('ase.finish.GateService.gate', return_value=gate):
            return finish(store, 'wc', 'main', body_file=body_file)

    def feature(self, rel='app.py', text='print(2)\n', message='Count words on any whitespace'):
        self.git('checkout', '-q', '-B', 'feature', 'main')
        self.write(rel, text)
        self.git('add', '-f', rel)
        self.git('commit', '-q', '-m', message)

    def test_finish_in_a_team_repository_writes_only_the_change(self):
        self.install(['claude'])
        self.assertTrue(private_repository(self.project))
        self.feature()
        result = self.finish_here()
        self.assertEqual(result['status'], 'READY', result['reasons'])
        self.assertIn('Count words separated by any whitespace.', result['pr_body'])
        self.assertIn('8 tests run, 0 failed.', result['pr_body'])
        for word in ('gate', 'ase', 'lane', 'approved', 'framework', 'independent', 'wc', 'enforcement'):
            with self.subTest(word=word):
                self.assertNotRegex(result['pr_body'].lower(), r'\b' + word + r'\b')

    def test_finish_refuses_framework_traces_without_override(self):
        self.install(['claude'])
        cases = (('CLAUDE.local.md', 'x\n', 'Add notes', 'Framework files are in this branch'),
                 ('.claude/skills/task-orchestrator/extra.md', 'x\n', 'Add notes', 'Framework files are in this branch'),
                 ('app.py', '# see DevelopmentFramework\n', 'Tweak', 'Added lines mention the framework'),
                 ('app.py', 'print(3)\n', 'Tweak via DevelopmentFramework', 'Commit messages or the branch name mention'))
        for rel, text, message, reason in cases:
            with self.subTest(rel=rel, message=message):
                self.feature(rel, text, message)
                result = self.finish_here()
                self.assertNotEqual(result['status'], 'READY')
                self.assertTrue(any(reason in r for r in result['reasons']), result['reasons'])

    def test_finish_refuses_a_pr_body_file_inside_the_repository(self):
        self.install(['claude'])
        self.feature()
        result = self.finish_here(body_file=str(self.project / 'pr.md'))
        self.assertTrue(any('outside the repository' in r for r in result['reasons']), result['reasons'])

    def test_plain_body_flags_framework_words_from_the_request(self):
        from ase.finish import _mentions
        task = {'contract': {'request': 'Through ase, quick lane: count words.'}, 'proposal': {}}
        self.assertEqual(sorted(_mentions(plain_body(task, {}))), ['ase', 'quick lane'])
        self.assertEqual(_mentions('Count words; the base case stays.'), [])


    def test_ordinary_words_and_team_ase_code_do_not_block(self):
        from ase.finish import _mentions, FRAMEWORK_TERMS
        self.assertEqual(_mentions('# autonomous sensing pipeline; autonomous-service; import ase', FRAMEWORK_TERMS), [])
        self.install(['claude'])
        self.feature('app.py', 'import ase  # autonomous sensing\n', 'Add autonomous sensing step')
        self.assertEqual(self.finish_here()['status'], 'READY')

    def test_agent_written_body_outside_the_repository_is_used_and_scanned(self):
        self.install(['claude'])
        self.feature()
        own = self.base / 'body.md'
        own.write_text('Words now split on any whitespace.\n', encoding='utf-8')
        result = self.finish_here(body_file=str(own))
        self.assertEqual((result['status'], result['pr_body']), ('READY', 'Words now split on any whitespace.\n'))
        own.write_text('Done through the ase quick lane.\n', encoding='utf-8')
        self.assertTrue(any('PR description would mention' in r for r in self.finish_here(body_file=str(own))['reasons']))

    def test_framework_remote_must_be_github_itself(self):
        from ase.project_local import FRAMEWORK_REMOTE
        for url in ('https://github.com/artem0513/DevelopmentFramework.git', 'git@github.com:artem0513/DevelopmentFramework.git'):
            self.assertTrue(FRAMEWORK_REMOTE.search(url), url)
        self.assertFalse(FRAMEWORK_REMOTE.search('https://mirror.corp.example/github.com/artem0513/DevelopmentFramework.git'))


class FrameworkRoutingTests(unittest.TestCase):
    def test_repository_block_is_the_installer_routing(self):
        text = (ROOT / 'AGENTS.md').read_text(encoding='utf-8')
        block = text[text.index(installer.BEGIN):text.index(installer.END) + len(installer.END)]
        self.assertEqual(block, installer._routing('.agents/skills'))


if __name__ == '__main__':
    unittest.main()
