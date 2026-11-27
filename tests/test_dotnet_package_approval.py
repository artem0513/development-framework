"""Direct .NET additions are checked against the approved stage plan."""
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from ase.discovery import discover_capabilities, record_discovery
from ase.gates import GateService
from ase.project_local import config_bytes
from ase.state import transaction, write_record
from ase.store import Store
from ase.tasks import TaskService, record_baseline


MANIFEST = 'src/Billing.csproj'
REQUIRE_APPROVAL = 'Direct .NET dependency requires owner approval: '
UNVERIFIED = 'Direct .NET dependency comparison unavailable: '


class DotnetPackageApprovalTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='ase-dotnet-approval-')
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.repo = root / 'work'
        self.repo.mkdir()
        self.manifest('Existing')
        self.manifest('Existing', path='src/Ledger.csproj')
        self.git('init', '--quiet')
        self.git('add', '.')
        self.git('commit', '--quiet', '--no-gpg-sign', '-m', 'Baseline')
        self.store = Store(self.repo, root / 'state')
        self.addCleanup(self.store.close)
        self.tasks = TaskService(self.store)
        self.contract = {'id': 'task', 'schema_version': 2, 'profile': 'software',
            'request': 'Repair a billing operation.', 'risk': 'LOW',
            'scope': {'resources': ['src'], 'effects': ['file_write']},
            'acceptance': [{'id': 'C', 'expected': 'The billing action is available.', 'evidence_ids': ['billing']}],
            'evidence': [{'id': 'billing', 'kind': 'artifact', 'path': MANIFEST}], 'unknowns': [],
            'stages': [{'id': 'fix', 'acceptance_ids': ['C'], 'depends_on': []},
                       {'id': 'later', 'acceptance_ids': [], 'depends_on': ['fix']}]}
        self.configure()

    def git(self, *args):
        subprocess.run(['git', '-c', 'core.autocrlf=false', '-c', 'user.name=Test',
                        '-c', 'user.email=test@example.invalid', '-C', str(self.repo), *args],
                       check=True, capture_output=True, timeout=30)

    def write(self, path, content):
        target = self.repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding='utf-8', newline='')

    def configure(self, enabled=True):
        text = config_bytes(['codex']).decode()
        self.write('.devframework/config.toml', text + ('guard_dotnet_package_additions = true\n' if enabled else ''))

    def manifest(self, *packages, path=MANIFEST, version='1.0', other=''):
        items = ''.join(f'<PackageReference Include="{name}" Version="{version}" />' for name in packages)
        self.write(path, f'<Project><ItemGroup>{items}{other}</ItemGroup></Project>\n')

    def authorize(self, package=None, *, stage='fix', preapproved=False, deps=(), promote=True):
        if package:
            self.contract['sources'] = [{'id': 'owner', 'locator': 'chat:need'}]
        self.tasks.create(self.contract)
        if deps:
            discovery = discover_capabilities(self.store.project, scope={
                'resources': ['src'], 'dependencies': [{'path': path, 'reason': 'Read-only reference'} for path in deps]},
                backend_config={})
            record_discovery(self.store, 'task', discovery, expected_revision=0)
        plan = {'summary': 'Change the billing action.', 'stage_ids': ['fix', 'later']}
        if package:
            plan['effects'] = [{'id': 'package', 'stage_ids': [stage], 'change': 'Add ' + package,
                'basis': 'REQUIREMENT', 'ref': 'owner', 'uncovered_scenario': 'The existing path needs this API.',
                'dotnet_package': {'path': MANIFEST, 'package': package}}]
        task = self.tasks.propose('task', plan, expected_revision=self.tasks.snapshot('task')['revision'])
        self.tasks.approve('task', proposal_hash=task['proposal_hash'], user_message_ref='chat:stage-plan',
                           approved_stage_ids=[stage], owner_preapproved=preapproved)
        if promote:
            record_baseline(self.store, 'task', self.contract)
        return task

    def package_reasons(self, stage='fix', task_id='task'):
        verdict = GateService(self.store).gate(task_id, stage_id=stage)
        return [row for row in verdict['blocked_reasons'] if row.startswith((REQUIRE_APPROVAL, UNVERIFIED))]

    def test_green_local_scope_cannot_add_a_package_without_its_owner_decision(self):
        self.authorize()
        self.manifest('Existing', 'New.Lib')
        self.assertEqual(self.package_reasons(), [REQUIRE_APPROVAL + MANIFEST + ' (new.lib)'])

    def test_native_work_uses_the_recorded_approval_baseline(self):
        self.authorize(promote=False)
        self.manifest('Existing', 'New.Lib')
        self.assertEqual(self.package_reasons(), [REQUIRE_APPROVAL + MANIFEST + ' (new.lib)'])

    def test_changes_in_ignored_read_only_dependencies_do_not_ask_for_package_approval(self):
        reference = 'reference/Example.csproj'
        self.write('.git/info/exclude', '/reference/\n')
        self.manifest('Existing', path=reference)
        self.authorize(deps=[reference])
        self.manifest('Existing', 'New.Lib', path=reference)
        self.assertEqual(self.package_reasons(), [])

    def test_exact_approval_survives_repairs_and_separate_grants_but_covers_one_target(self):
        task = self.authorize('New.Lib')
        self.tasks.approve('task', proposal_hash=task['proposal_hash'], user_message_ref='chat:later-plan',
                           approved_stage_ids=['later'])
        self.manifest('Existing', 'new.lib', version='1.2')
        self.write('src/repair.cs', 'internal class Repair {}\n')
        self.assertEqual(self.package_reasons(), [])
        self.manifest('Existing', 'new.lib', path='src/Ledger.csproj')
        self.manifest('Existing', 'new.lib', 'Other.Lib')
        self.assertEqual(self.package_reasons(), [REQUIRE_APPROVAL + MANIFEST + ' (other.lib)',
                                                 REQUIRE_APPROVAL + 'src/Ledger.csproj (new.lib)'])

    def test_earlier_stage_cannot_borrow_later_stage_approval(self):
        task = self.authorize('New.Lib', stage='later')
        self.tasks.approve('task', proposal_hash=task['proposal_hash'], user_message_ref='chat:ordinary-fix',
                           approved_stage_ids=['fix'])
        self.manifest('Existing', 'New.Lib')
        self.assertEqual(self.package_reasons(), [REQUIRE_APPROVAL + MANIFEST + ' (new.lib)'])
        self.assertEqual(self.package_reasons('later'), [])

    def test_verified_earlier_stage_keeps_its_approved_addition_in_later_checks(self):
        task = self.authorize('New.Lib')
        self.tasks.approve('task', proposal_hash=task['proposal_hash'], user_message_ref='chat:later',
                           approved_stage_ids=['later'])
        with transaction(self.store.db):
            write_record(self.store.db, 'stage', 'task:fix', {'state': 'COMPLETED',
                'contract_hash': task['contract_hash'], 'proposal_hash': task['proposal_hash']}, expected_revision=0)
        self.manifest('Existing', 'New.Lib')
        self.assertEqual(self.package_reasons('later'), [])

    def test_child_uses_exact_mapped_owner_decision(self):
        self.authorize('New.Lib')
        child = {**self.contract, 'id': 'child', 'parent_task_id': 'task', 'parent_stage_id': 'fix',
                 'stages': [self.contract['stages'][0]]}
        self.tasks.create(child)
        record_baseline(self.store, 'child', child)
        self.manifest('Existing', 'New.Lib')
        self.assertEqual(self.package_reasons(task_id='child'), [])

    def test_parent_recognizes_completed_required_childs_exact_owner_decision(self):
        self.contract['stages'][0]['required_child_task_id'] = 'child'
        self.authorize()
        child_stage = {key: value for key, value in self.contract['stages'][0].items()
                       if key != 'required_child_task_id'}
        child = self.tasks.create({**self.contract, 'id': 'child', 'parent_task_id': 'task', 'parent_stage_id': 'fix',
                    'sources': [{'id': 'owner', 'locator': 'chat:child-dependency'}], 'stages': [child_stage]})
        child = self.tasks.propose('child', {'summary': 'Use the named package for billing.', 'stage_ids': ['fix'],
            'effects': [{'id': 'lib', 'stage_ids': ['fix'], 'change': 'Add New.Lib', 'basis': 'REQUIREMENT',
                'ref': 'owner', 'uncovered_scenario': 'The current path cannot parse the required message.',
                'dotnet_package': {'path': MANIFEST, 'package': 'New.Lib'}}]}, expected_revision=child['revision'])
        child = self.tasks.approve('child', proposal_hash=child['proposal_hash'], user_message_ref='chat:child-approve',
                                   approved_stage_ids=['fix'])
        self.manifest('Existing', 'New.Lib')
        self.assertEqual(self.package_reasons(task_id='child'), [])
        self.assertEqual(self.package_reasons(), [REQUIRE_APPROVAL + MANIFEST + ' (new.lib)'])
        proof = {'status': 'APPROVED', 'contract_hash': child['contract_hash'],
                 'plan_hash': self.tasks.execution_plan('child')['plan_hash']}
        with patch.object(GateService, 'gate', return_value=proof):
            self.tasks.transition('child', 'COMPLETED', expected_revision=child['revision'], reason='Fixture verified')
        self.assertEqual(self.package_reasons(), [])
        self.manifest('Existing', 'New.Lib', 'Other.Lib')
        self.assertEqual(self.package_reasons(), [REQUIRE_APPROVAL + MANIFEST + ' (other.lib)'])

    def test_general_preapproval_does_not_authorize_a_package(self):
        self.authorize('New.Lib', preapproved=True)
        self.manifest('Existing', 'New.Lib')
        self.assertEqual(self.package_reasons(), [REQUIRE_APPROVAL + MANIFEST + ' (new.lib)'])

    def test_version_update_central_metadata_and_ordinary_source_changes_do_not_ask(self):
        self.authorize()
        self.manifest('EXISTING', version='2.0', other='<PackageReference Update="Another" Version="1" />')
        self.write('src/Directory.Packages.props', '<Project><PackageVersion Include="Ready" Version="1" /></Project>')
        self.write('src/BillingService.cs', 'internal class BillingService {}\n')
        self.assertEqual(self.package_reasons(), [])

    def test_opt_out_preserves_existing_adoption(self):
        self.configure(False)
        self.authorize()
        self.manifest('Existing', 'New.Lib')
        self.assertEqual(self.package_reasons(), [])

    def test_editing_local_setting_does_not_remove_task_guard(self):
        self.write('.git/info/exclude', '/.devframework/\n')
        self.authorize(promote=False)
        self.configure(False)
        self.manifest('Existing', 'New.Lib')
        self.assertEqual(self.package_reasons(), [REQUIRE_APPROVAL + MANIFEST + ' (new.lib)'])

    def test_exact_manifest_move_does_not_make_its_packages_new(self):
        self.authorize()
        path = self.repo / MANIFEST
        path.rename(self.repo / 'src/Moved.csproj')
        self.assertEqual(self.package_reasons(), [])
        self.manifest('Existing', path='src/Copy.csproj')
        self.assertEqual(self.package_reasons(), [REQUIRE_APPROVAL + 'src/Copy.csproj (existing)',
                                                 REQUIRE_APPROVAL + 'src/Moved.csproj (existing)'])

    def test_manifest_relocation_into_another_component_needs_its_own_decision(self):
        self.authorize()
        moved = self.repo / 'src/portal/Billing.csproj'
        moved.parent.mkdir()
        (self.repo / MANIFEST).rename(moved)
        self.assertEqual(self.package_reasons(), [REQUIRE_APPROVAL + 'src/portal/Billing.csproj (existing)'])

    def test_approval_attached_to_stage_without_manifest_scope_is_insufficient(self):
        self.write('src/review.md', 'Original scope review.\n')
        self.contract['scope']['resources'].append('src/review.md')
        self.contract['stages'][0]['scope'] = {'resources': ['src/review.md'], 'effects': ['file_write']}
        with self.assertRaisesRegex(ValueError, 'package path exceeds stage mutation scope'):
            self.authorize('New.Lib')

    def test_unavailable_before_content_is_a_verification_gap(self):
        self.manifest('Existing', 'DirtyBefore')
        self.authorize()
        self.manifest('Existing', 'DirtyBefore', 'New.Lib')
        reasons = self.package_reasons()
        self.assertEqual(len(reasons), 1)
        self.assertTrue(reasons[0].startswith(UNVERIFIED + MANIFEST), reasons)
