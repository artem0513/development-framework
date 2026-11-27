"""Local gate tests with actual Git baselines and stage approvals."""
import json
from hashlib import sha256
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from ase.discovery import discover_capabilities, record_discovery
from ase.gates import GateService
from ase.project_local import config_bytes
from ase.runner import Runner
from ase.state import read_record, transaction, write_record
from ase.store import Store
from ase.tasks import TaskService
from ase.workflow import WorkflowController
from test_runner import FIXTURE


MIGRATION = 'src/Migrations/20261126_Billing.cs'
TEMPLATE = 'infra/azuredeploy.json'
MANIFEST = 'src/Billing.csproj'
SCHEMA = 'EF Core schema addition requires owner approval: '
RESOURCE = 'ARM resource addition requires owner approval: '
PACKAGE = 'Direct .NET dependency requires owner approval: '
SUBSCRIPTION = 'https://schema.management.azure.com/schemas/2018-05-01/subscriptionDeploymentTemplate.json#'
CREATE_TABLE = '''migrationBuilder.CreateTable(
    name: "Invoices", schema: "dbo",
    columns: table => new { Id = table.Column<int>(nullable: false) },
    constraints: table => { table.PrimaryKey("PK_Invoices", x => x.Id); });'''
ADD_COLUMN = '''migrationBuilder.AddColumn<string>(name: "ExternalId",
    table: "Invoices", schema: "dbo", nullable: true);'''
NAMESPACE = {'type': 'Microsoft.ServiceBus/namespaces', 'name': 'billing', 'apiVersion': '2021-11-01'}
QUEUE = {'type': 'queues', 'name': 'settlement', 'apiVersion': '2021-11-01'}


class GuardedAdditionTests(unittest.TestCase):
    def setUp(self):
        workspace = tempfile.TemporaryDirectory(prefix='ase-guarded-additions-')
        self.addCleanup(workspace.cleanup)
        root = Path(workspace.name)
        self.root = root
        self.repo = root / 'work'
        self.repo.mkdir()
        self.write('.gitignore', '/.devframework/\n')
        self.migration(CREATE_TABLE)
        self.template([NAMESPACE])
        self.write(MANIFEST, '<Project><ItemGroup><PackageReference Include="Existing" Version="1" /></ItemGroup></Project>\n')
        self.git('init', '--quiet')
        self.git('add', '.')
        self.git('commit', '--quiet', '--no-gpg-sign', '-m', 'Baseline')
        self.configure()
        self.store = Store(self.repo, root / 'state')
        self.addCleanup(self.store.close)
        self.tasks = TaskService(self.store)
        self.contract = {'id': 'task', 'schema_version': 2, 'profile': 'software', 'request': 'Repair billing.',
            'scope': {'resources': ['src', 'infra'], 'effects': ['file_write']},
            'acceptance': [{'id': 'C', 'expected': 'Billing uses the agreed scope.', 'evidence_ids': ['billing']}],
            'evidence': [{'id': 'billing', 'kind': 'artifact', 'path': MIGRATION}],
            'sources': [{'id': 'owner', 'locator': 'chat:owner-choice'}],
            'stages': [{'id': 'fix', 'acceptance_ids': ['C']}], 'unknowns': []}

    def git(self, *args):
        subprocess.run(['git', '-c', 'core.autocrlf=false', '-c', 'user.name=Fixture',
                        '-c', 'user.email=fixture@example.invalid', '-C', str(self.repo), *args],
                       check=True, capture_output=True, timeout=30)

    def write(self, path, content):
        target = self.repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding='utf-8', newline='')

    def configure(self, enabled=True):
        config = config_bytes(['codex']).decode()
        if enabled:
            config += ('guard_efcore_schema_additions = true\nguard_arm_resource_additions = true\n'
                       'guard_dotnet_package_additions = true\n')
        self.write('.devframework/config.toml', config)

    def migration(self, up, *, down='', path=MIGRATION):
        self.write(path, 'using Microsoft.EntityFrameworkCore.Migrations;\n'
            'public partial class Billing : Migration {\n'
            f'protected override void Up(MigrationBuilder migrationBuilder) {{\n{up}\n}}\n'
            f'protected override void Down(MigrationBuilder migrationBuilder) {{\n{down}\n}}\n' + '}\n')

    def template(self, resources, *, path=TEMPLATE,
                 schema='https://schema.management.azure.com/schemas/2019-04-01/deploymentTemplate.json#'):
        self.write(path, json.dumps({'$schema': schema, 'contentVersion': '1.0.0.0', 'resources': resources}))

    def effect(self, key, target, **overrides):
        return {'id': key, 'stage_ids': ['fix'], 'kind': 'infrastructure', 'basis': 'REQUIREMENT',
            'ref': 'owner', 'change': 'Add the exact owner-approved target.',
            'uncovered_scenario': 'Current structure cannot store the approved event.', key: target, **overrides}

    def authorize(self, *effects, preapproved=False):
        self.tasks.create(self.contract)
        return self.approve_plan(*effects, preapproved=preapproved)

    def approve_plan(self, *effects, preapproved=False):
        task = self.tasks.snapshot('task')
        task = self.tasks.propose('task', {'summary': 'Make the named billing change.', 'stage_ids': ['fix'],
            'effects': list(effects)}, expected_revision=task['revision'])
        return self.tasks.approve('task', proposal_hash=task['proposal_hash'], approved_stage_ids=['fix'],
                                  user_message_ref='chat:approval:' + str(task['revision']), owner_preapproved=preapproved)

    def reasons(self, stage_id='fix'):
        gate = GateService(self.store).gate('task', stage_id=stage_id)
        return [reason for reason in gate['blocked_reasons']
                if reason.startswith(('EF Core schema', 'ARM resource', 'Direct .NET'))]

    def candidate_edit(self, path, content, *, operation='write'):
        source = self.repo / path
        return {'path': path, 'operation': operation, 'content': content,
                'before_sha256': sha256(source.read_bytes()).hexdigest() if source.exists() else ''}

    def candidate_runner(self, edits):
        discovery = discover_capabilities(self.store.project, scope={'resources': ['src', 'infra']}, backend_config={})
        record_discovery(self.store, 'task', discovery, expected_revision=0)
        self.set_candidate(edits)
        provider = self.root / 'provider.py'
        provider.write_text(FIXTURE.replace("payload = {'verdict':'PASS'}", '''request = json.loads(input_text.split('REQUEST_JSON:\\n', 1)[1])
if request['role'] == 'implementation':
 import pathlib
 payload = {'edits': json.loads(pathlib.Path(__file__).with_name('edits.json').read_text(encoding='utf-8')), 'summary': 'Local protocol fixture'}
elif request['role'] == 'ac': payload = {'criteria': []}
else: payload = {'findings': [], 'summary': 'Protocol fixture'}'''), encoding='utf-8')
        runner = Runner({'executable': sys.executable, 'executable_args': [str(provider), 'success', 'codex'],
            'trusted_cwd': str(self.root), 'protocol_only': True}, self.store.root / 'tasks/task/sessions')
        return WorkflowController(self.store, runner=runner)

    def set_candidate(self, edits):
        (self.root / 'edits.json').write_text(json.dumps(edits), encoding='utf-8')

    def new_package(self):
        before = (self.repo / MANIFEST).read_text(encoding='utf-8')
        return self.candidate_edit(MANIFEST, before.replace('</ItemGroup>',
            '<PackageReference Include="New.Lib" Version="1" /></ItemGroup>'))

    def test_candidate_asks_for_exact_additions_before_writing_any_file_or_running_actions(self):
        self.authorize()
        migration = (self.repo / MIGRATION).read_text(encoding='utf-8')
        template = json.loads((self.repo / TEMPLATE).read_text(encoding='utf-8'))
        template['resources'] = [{**NAMESPACE, 'resources': [QUEUE]}]
        routine = 'src/Fix.cs'
        edits = [self.candidate_edit(routine, 'internal class Fixed {}'), self.new_package(),
                 self.candidate_edit(MIGRATION, migration.replace(CREATE_TABLE, CREATE_TABLE.replace('Invoices', 'Archive'))),
                 self.candidate_edit(TEMPLATE, json.dumps(template))]
        controller = self.candidate_runner(edits)
        with patch.object(controller, 'execute_actions') as execute, patch.object(controller, 'check') as check:
            result = controller.run('task')
        self.assertTrue(result['awaiting_approval'], result)
        self.assertTrue(result['needs_owner_input'])
        targets = result['addition_approval']['targets']
        self.assertCountEqual(targets, [
            {'dotnet_package': {'path': MANIFEST, 'package': 'new.lib'}},
            {'efcore_schema': {'path': MIGRATION, 'schema': 'dbo', 'table': 'Archive'}},
            {'arm_resource': {'path': TEMPLATE, 'type': 'microsoft.servicebus/namespaces/queues', 'name': 'billing/settlement'}},
        ])
        self.assertEqual(self.tasks.snapshot('task')['state'], 'AWAITING_APPROVAL')
        self.assertIn('Archive', self.tasks.readiness('task', include_progress=True)['next_action'])
        self.assertFalse((self.repo / routine).exists())
        for entry in edits[1:]:
            self.assertEqual(sha256((self.repo / entry['path']).read_bytes()).hexdigest(), entry['before_sha256'])
        cycle = read_record(self.store.db, 'cycle', 'task:fix')['payload']
        self.assertIsNone(read_record(self.store.db, 'local_edit_application', cycle['candidate_id'])['payload'])
        execute.assert_not_called()
        check.assert_not_called()
        with patch.object(controller.runner, 'run') as launch:
            repeated = controller.run('task')
        self.assertEqual(repeated['addition_approval'], result['addition_approval'])
        launch.assert_not_called()

    def test_recovering_a_captured_candidate_still_requires_approval_before_any_write(self):
        self.authorize()
        edit = self.new_package()
        controller = self.candidate_runner([edit])
        save = controller._save

        def interrupt_after_capture(kind, key, payload):
            save(kind, key, payload)
            if kind == 'stage_implementation' and key == 'task:fix':
                raise KeyboardInterrupt

        with patch.object(controller, '_save', side_effect=interrupt_after_capture):
            interrupted = controller.run('task')
        self.assertEqual(interrupted['closure']['finish_reason'], 'interrupted')
        with patch.object(controller.runner, 'run') as launch:
            recovered = controller.run('task', resume=True)
        self.assertTrue(recovered['awaiting_approval'], recovered)
        self.assertEqual(recovered['addition_approval']['targets'],
                         [{'dotnet_package': {'path': MANIFEST, 'package': 'new.lib'}}])
        self.assertEqual(sha256((self.repo / MANIFEST).read_bytes()).hexdigest(), edit['before_sha256'])
        launch.assert_not_called()

    def test_unreadable_candidate_is_held_without_asking_the_owner_to_approve_it(self):
        self.authorize()
        edit = self.candidate_edit(TEMPLATE, '{"resources": [')
        controller = self.candidate_runner([edit])
        result = controller.run('task')
        self.assertFalse(result['awaiting_approval'], result)
        self.assertFalse(result['needs_owner_input'])
        self.assertIn('ARM resource addition comparison unavailable: ' + TEMPLATE, result['blocked_reasons'])
        self.assertEqual(sha256((self.repo / TEMPLATE).read_bytes()).hexdigest(), edit['before_sha256'])

    def test_resuming_with_a_routine_repair_preserves_the_existing_approve(self):
        approved = self.authorize()
        controller = self.candidate_runner([self.new_package()])
        blocked = controller.run('task')
        self.assertTrue(blocked['awaiting_approval'], blocked)
        existing = (self.repo / MANIFEST).read_text(encoding='utf-8')
        routine = 'src/Fix.cs'
        self.set_candidate([self.candidate_edit(routine, 'internal class Fixed {}'),
                            self.candidate_edit(MANIFEST, existing.replace('Version="1"', 'Version="2"'))])
        with patch.object(controller, 'execute_actions', wraps=controller.execute_actions) as execute:
            result = controller.run('task', resume=True)
        self.assertFalse(result['awaiting_approval'], result)
        self.assertIn('Version="2"', (self.repo / MANIFEST).read_text(encoding='utf-8'))
        self.assertNotIn('New.Lib', (self.repo / MANIFEST).read_text(encoding='utf-8'))
        self.assertTrue((self.repo / routine).exists())
        self.assertEqual(self.tasks.snapshot('task')['proposal_hash'], approved['proposal_hash'])
        self.assertEqual(self.tasks.snapshot('task')['approved_stage_ids'], ['fix'])
        execute.assert_called_once()

    def test_exact_package_approval_releases_a_fresh_candidate(self):
        self.authorize()
        controller = self.candidate_runner([self.new_package()])
        blocked = controller.run('task')
        self.assertTrue(blocked['awaiting_approval'], blocked)
        old_cycle = read_record(self.store.db, 'cycle', 'task:fix')['payload']
        self.approve_plan(self.effect('dotnet_package', {'path': MANIFEST, 'package': 'New.Lib'}, kind='code'))
        result = controller.run('task', resume=True)
        self.assertFalse(result['awaiting_approval'], result)
        self.assertIn('New.Lib', (self.repo / MANIFEST).read_text(encoding='utf-8'))
        cycle = read_record(self.store.db, 'cycle', 'task:fix')['payload']
        self.assertNotEqual(cycle['candidate_id'], old_cycle['candidate_id'])
        self.assertIsNone(read_record(self.store.db, 'local_edit_application', old_cycle['candidate_id'])['payload'])

    def test_ordinary_approval_does_not_cover_a_new_column_or_queue(self):
        self.authorize()
        self.migration(CREATE_TABLE + '\n' + ADD_COLUMN)
        self.template([{**NAMESPACE, 'resources': [QUEUE]}])
        self.assertCountEqual(self.reasons(), [SCHEMA + MIGRATION + ' (dbo.Invoices.ExternalId)',
            RESOURCE + TEMPLATE + ' (microsoft.servicebus/namespaces/queues: billing/settlement)'])

    def test_approved_exact_targets_survive_repairs_and_leave_other_targets_blocked(self):
        self.authorize(self.effect('efcore_schema', {'path': MIGRATION, 'schema': 'dbo',
                       'table': 'Invoices', 'column': 'ExternalId'}),
                       self.effect('arm_resource', {'path': TEMPLATE, 'type': 'Microsoft.ServiceBus/namespaces/queues',
                       'name': 'billing/settlement'}))
        self.migration(CREATE_TABLE + '\n' + ADD_COLUMN.replace('nullable: true', 'nullable: false'))
        self.template([{**NAMESPACE, 'resources': [{**QUEUE, 'properties': {'requiresSession': True}}]}])
        self.write('src/Repair.cs', 'internal class Repair {}\n')
        self.assertEqual(self.reasons(), [])
        self.migration(CREATE_TABLE + '\n' + ADD_COLUMN + '\n' + ADD_COLUMN.replace('dbo', 'extra'))
        self.template([{**NAMESPACE, 'resources': [QUEUE, {**QUEUE, 'name': 'other'}]}])
        self.assertCountEqual(self.reasons(), [SCHEMA + MIGRATION + ' (extra.Invoices.ExternalId)',
            RESOURCE + TEMPLATE + ' (microsoft.servicebus/namespaces/queues: billing/other)'])

    def test_new_table_is_distinct_from_a_column(self):
        self.authorize(self.effect('efcore_schema', {'path': MIGRATION, 'schema': 'dbo', 'table': 'Archive', 'column': 'Id'}))
        self.migration(CREATE_TABLE + '\n' + CREATE_TABLE.replace('Invoices', 'Archive'))
        self.assertEqual(self.reasons(), [SCHEMA + MIGRATION + ' (dbo.Archive)'])

    def test_down_comments_sql_strings_and_existing_resource_updates_are_routine(self):
        self.authorize()
        self.migration(CREATE_TABLE + '\n/* ' + ADD_COLUMN + ' */\n'
            'migrationBuilder.Sql("""\n' + ADD_COLUMN + '\n""");', down=ADD_COLUMN)
        self.template([{**NAMESPACE, 'properties': {'minimumTlsVersion': '1.2'}}])
        self.assertEqual(self.reasons(), [])

    def test_schema_targets_use_schema_kind_and_still_need_the_owners_decision(self):
        target = {'path': MIGRATION, 'schema': 'dbo', 'table': 'Invoices', 'column': 'ExternalId'}
        self.tasks.create(self.contract)
        with self.assertRaisesRegex(ValueError, "owner's explicit source or decision"):
            self.approve_plan(self.effect('efcore_schema', target, kind='schema', ref='C'))
        with self.assertRaisesRegex(ValueError, 'requires effect kind infrastructure or schema'):
            self.approve_plan(self.effect('efcore_schema', target, kind='code'))
        resource = {'path': TEMPLATE, 'type': 'Microsoft.ServiceBus/namespaces/queues', 'name': 'billing/settlement'}
        with self.assertRaisesRegex(ValueError, 'requires effect kind infrastructure'):
            self.approve_plan(self.effect('arm_resource', resource, kind='schema'))
        self.approve_plan(self.effect('efcore_schema', target, kind='schema'))
        self.migration(CREATE_TABLE + '\n' + ADD_COLUMN)
        self.assertEqual(self.reasons(), [])

    def test_existing_infrastructure_kind_for_a_schema_target_stays_valid(self):
        self.authorize(self.effect('efcore_schema', {'path': MIGRATION, 'schema': 'dbo', 'table': 'Invoices',
                                                     'column': 'ExternalId'}))
        self.migration(CREATE_TABLE + '\n' + ADD_COLUMN)
        self.assertEqual(self.reasons(), [])

    def test_literal_child_resource_identity_survives_a_change_in_declaration_layout(self):
        self.template([NAMESPACE, {**QUEUE, 'type': 'Microsoft.ServiceBus/namespaces/queues',
                                   'name': 'billing/settlement'}])
        self.git('add', TEMPLATE)
        self.git('commit', '--quiet', '--no-gpg-sign', '-m', 'Existing queue baseline')
        self.authorize()
        self.template([{**NAMESPACE, 'resources': [QUEUE]}])
        self.assertEqual(self.reasons(), [])

    def test_nested_child_with_a_full_type_is_an_addition_without_a_second_provider(self):
        self.authorize()
        full = {**QUEUE, 'type': 'Microsoft.ServiceBus/namespaces/queues'}
        self.template([{**NAMESPACE, 'resources': [{**full, 'name': 'billing/settlement'}, {**full, 'name': 'refunds'},
            {**full, 'name': 'billing/audit', 'resources': [{'type': 'authorizationRules', 'name': 'listen'}]}]}])
        queues = RESOURCE + TEMPLATE + ' (microsoft.servicebus/namespaces/queues: billing/'
        self.assertCountEqual(self.reasons(), [queues + 'settlement)', queues + 'refunds)', queues + 'audit)',
            RESOURCE + TEMPLATE + ' (microsoft.servicebus/namespaces/queues/authorizationrules: billing/audit/listen)'])

    def test_expression_names_and_scopes_are_additions_identified_by_their_text(self):
        storage = {'type': 'Microsoft.Storage/storageAccounts', 'name': "[parameters('storageName')]",
                   'apiVersion': '2023-01-01'}
        lock = {'type': 'Microsoft.Authorization/locks', 'name': 'keep', 'scope': "[resourceId('x', 'y')]"}
        self.authorize(self.effect('arm_resource', {'path': TEMPLATE, 'type': storage['type'], 'name': storage['name']}))
        self.template([NAMESPACE, storage, {**storage, 'name': "[concat('a', 'b')]"}, lock])
        self.assertCountEqual(self.reasons(), [
            RESOURCE + TEMPLATE + " (microsoft.storage/storageaccounts: [concat('a', 'b')])",
            RESOURCE + TEMPLATE + " (microsoft.authorization/locks: keep: [resourceId('x', 'y')])"])

    def test_same_name_at_another_scope_is_a_separate_addition(self):
        lock = {'type': 'Microsoft.Authorization/locks', 'name': 'keep', 'scope': "[resourceId('a')]"}
        storage = {'type': 'Microsoft.Storage/storageAccounts', 'name': 'billingdata'}
        self.template([NAMESPACE, lock, storage])
        self.git('add', TEMPLATE)
        self.git('commit', '--quiet', '--no-gpg-sign', '-m', 'Existing lock and storage')
        self.authorize(self.effect('arm_resource', {'path': TEMPLATE, 'type': lock['type'], 'name': 'keep',
                                                    'scope': "[resourceId('b')]"}))
        inline = {'type': 'Microsoft.Resources/deployments', 'name': 'copy', 'resourceGroup': 'other',
                  'properties': {'mode': 'Incremental', 'template': {'resources': [storage]}}}
        self.template([NAMESPACE, lock, storage, {**lock, 'scope': "[resourceId('b')]"}, inline])
        self.assertCountEqual(self.reasons(), [RESOURCE + TEMPLATE + ' (microsoft.resources/deployments: copy)',
            RESOURCE + TEMPLATE + ' (microsoft.storage/storageaccounts: billingdata: deployment=copy;resourceGroup=other)'])

    def test_repeated_identity_in_another_deployment_or_loop_is_an_addition(self):
        storage = {'type': 'Microsoft.Storage/storageAccounts', 'name': "[parameters('name')]"}

        def module(name, *resources):
            return {'type': 'Microsoft.Resources/deployments', 'name': name,
                    'properties': {'mode': 'Incremental', 'template': {'resources': list(resources)}}}

        lock = {'type': 'Microsoft.Authorization/locks', 'name': 'keep', 'scope': "[resourceId('s')]"}
        self.template([module('storage', storage), module('logs'), lock])
        first = '\nforeach (var name in First) { migrationBuilder.CreateTable(name: name, columns: t => new { }); }'
        self.migration(CREATE_TABLE + first)
        self.git('add', TEMPLATE, MIGRATION)
        self.git('commit', '--quiet', '--no-gpg-sign', '-m', 'Existing modules and loop')
        self.authorize()
        self.template([module('storage', storage), module('logs', storage, {**lock, 'resourceGroup': 'x'}), lock,
                       {**lock, 'scope': "[resourceId('s')]"}])
        self.migration(CREATE_TABLE + first + '\nforeach (var name in Second) '
                       '{ migrationBuilder?.CreateTable(name: name, columns: t => new { }); }')
        self.assertCountEqual(self.reasons(), [
            RESOURCE + TEMPLATE + " (microsoft.storage/storageaccounts: [parameters('name')]: deployment=logs)",
            RESOURCE + TEMPLATE + " (microsoft.authorization/locks: keep: deployment=logs | [resourceId('s')])",
            RESOURCE + TEMPLATE + " (microsoft.authorization/locks: keep: [resourceId('s')])",
            SCHEMA + MIGRATION + ' (name)'])

    def test_inline_template_given_by_an_expression_is_a_gap_and_null_forgiving_calls_count(self):
        self.authorize()
        self.template([NAMESPACE, {'type': 'Microsoft.Resources/deployments', 'name': 'nested',
                                   'properties': {'mode': 'Incremental', 'template': "[variables('nested')]"}}])
        self.migration(CREATE_TABLE + '\nmigrationBuilder!.CreateTable("Archive", t => new { });')
        self.assertCountEqual(self.reasons(), ['ARM resource addition comparison unavailable: ' + TEMPLATE,
                                               SCHEMA + MIGRATION + ' (Archive)'])

    def test_linked_and_bare_deployments_stay_routine_when_unchanged(self):
        linked = {'type': 'Microsoft.Resources/deployments', 'name': 'linked',
                  'properties': {'mode': 'Incremental', 'templateLink': {'uri': "[variables('uri')]"}}}
        bare = {'type': 'Microsoft.Resources/deployments', 'name': 'bare'}
        self.template([NAMESPACE, linked, bare])
        self.git('add', TEMPLATE)
        self.git('commit', '--quiet', '--no-gpg-sign', '-m', 'Existing deployments')
        self.authorize()
        self.template([{**NAMESPACE, 'properties': {'minimumTlsVersion': '1.2'}}, linked, bare])
        self.assertEqual(self.reasons(), [])

    def test_text_that_only_mentions_a_template_schema_or_migrations_stays_routine(self):
        self.authorize()
        self.write('src/.vscode/settings.json', '{ // editor settings\n "json.schemas": [{"url": '
                   '"https://schema.management.azure.com/schemas/2019-04-01/deploymentTemplate.json#"}] }\n')
        (self.repo / 'src/Startup.cs').write_bytes(
            '// Привет\nclass Startup { void Configure(Builder b) { b.MigrationsAssembly("Data"); } }\n'.encode('cp1251'))
        self.assertEqual(self.reasons(), [])

    def test_existing_references_are_not_additions_but_their_new_children_are(self):
        self.authorize()
        self.template([{**NAMESPACE, 'existing': True, 'resources': [QUEUE]}])
        self.assertEqual(self.reasons(),
                         [RESOURCE + TEMPLATE + ' (microsoft.servicebus/namespaces/queues: billing/settlement)'])

    def test_subscription_templates_inline_deployments_and_any_template_file_name_are_checked(self):
        self.authorize()
        path = 'infra/subscription.json'
        group = {'type': 'Microsoft.Resources/resourceGroups', 'name': 'billing-rg', 'apiVersion': '2022-09-01'}
        inline = {'type': 'Microsoft.Resources/deployments', 'name': 'storage', 'apiVersion': '2022-09-01',
                  'properties': {'mode': 'Incremental', 'template': {'resources': [
                      {'type': 'Microsoft.Storage/storageAccounts', 'name': 'billingdata'}]}}}
        self.template([group, inline], path=path, schema=SUBSCRIPTION)
        self.assertCountEqual(self.reasons(), [RESOURCE + path + ' (microsoft.resources/resourcegroups: billing-rg)',
            RESOURCE + path + ' (microsoft.resources/deployments: storage)',
            RESOURCE + path + ' (microsoft.storage/storageaccounts: billingdata: deployment=storage)'])

    def test_other_json_and_csharp_files_stay_routine_at_any_size_or_encoding(self):
        self.authorize()
        self.write('src/tsconfig.json', '{ // comments are not JSON\n "compilerOptions": {} }\n')
        self.write('src/package-lock.json', json.dumps({'packages': {str(i): 'x' * 64 for i in range(9000)}}))
        self.assertGreater((self.repo / 'src/package-lock.json').stat().st_size, 512000)
        (self.repo / 'src/Legacy.cs').write_bytes('// Привет\nclass Legacy {}\n'.encode('cp1251'))
        self.write('src/Generated.cs', 'class Generated { }\n' + '// filler\n' * 60000)
        self.assertEqual(self.reasons(), [])

    def test_migration_outside_migrations_folder_and_nonliteral_names_are_checked(self):
        self.authorize()
        path = 'src/Data/Schema/20261127_Archive.cs'
        self.migration('migrationBuilder.CreateTable(name: TableNames.Archive, columns: table => new { });\n'
                       'migrationBuilder.CreateTable("Ledger", table => new { }, "dbo");\n'
                       'migrationBuilder.AddColumn<int>("Total", "Ledger");', path=path)
        self.assertCountEqual(self.reasons(), [SCHEMA + path + ' (TableNames.Archive)', SCHEMA + path + ' (dbo.Ledger)',
                                               SCHEMA + path + ' (Ledger.Total)'])
        self.approve_plan(self.effect('efcore_schema', {'path': path, 'table': 'TableNames.Archive'}, kind='schema'))
        self.assertCountEqual(self.reasons(), [SCHEMA + path + ' (dbo.Ledger)', SCHEMA + path + ' (Ledger.Total)'])

    def test_calls_inside_blocks_and_other_up_signatures_are_checked(self):
        self.authorize()
        self.migration('foreach (var name in TableNames.All) { migrationBuilder.CreateTable(name: name, columns: t => new { }); }\n'
                       'if (Extra) { migrationBuilder.AddColumn<int>("Total", "Ledger", null, null, null, false, "audit"); }')
        self.assertCountEqual(self.reasons(), [SCHEMA + MIGRATION + ' (name)', SCHEMA + MIGRATION + ' (audit.Ledger.Total)'])
        path = 'src/Data/Archive.cs'
        self.write(path, 'public class Archive : Microsoft.EntityFrameworkCore.Migrations.Migration {\n'
                   'override protected void Up(Microsoft.EntityFrameworkCore.Migrations.MigrationBuilder b) =>\n'
                   '    b.CreateTable(@"Archive", t => new { });\n'
                   'protected override void Down(MigrationBuilder b) { }\n}\n')
        self.assertIn(SCHEMA + path + ' (Archive)', self.reasons())

    def test_candidate_migration_outside_migrations_folder_asks_for_approval(self):
        self.authorize()
        path = 'src/Data/Ledger.cs'
        self.migration('migrationBuilder.CreateTable("Ledger", table => new { });', path=path)
        content = (self.repo / path).read_text(encoding='utf-8')
        (self.repo / path).unlink()
        controller = self.candidate_runner([self.candidate_edit(path, content)])
        result = controller.run('task')
        self.assertTrue(result['awaiting_approval'], result)
        self.assertEqual(result['addition_approval']['targets'], [{'efcore_schema': {'path': path, 'table': 'Ledger'}}])
        self.assertFalse((self.repo / path).exists())

    def test_imported_msbuild_files_global_and_evaluated_references_are_checked(self):
        self.authorize()
        self.write('src/Directory.Build.props', '<Project><ItemGroup><PackageReference Include="Shared.Lib" />'
                   '<PackageReference Update="Existing" Version="2" /></ItemGroup></Project>')
        self.write('src/Directory.Packages.props', '<Project><ItemGroup><GlobalPackageReference Include="Analyzers" />'
                   '<PackageVersion Include="Shared.Lib" Version="1" /></ItemGroup></Project>')
        manifest = (self.repo / MANIFEST).read_text(encoding='utf-8')
        self.write(MANIFEST, manifest.replace('</ItemGroup>', '<PackageReference Include="$(ExtraPackage)" /></ItemGroup>'))
        self.write('src/Legacy/packages.config', '<packages><package id="Old.Lib" version="1" /></packages>')
        self.assertCountEqual(self.reasons(), [PACKAGE + 'src/Directory.Build.props (shared.lib)',
            PACKAGE + 'src/Directory.Packages.props (analyzers)', PACKAGE + MANIFEST + ' ($(extrapackage))',
            PACKAGE + 'src/Legacy/packages.config (old.lib)'])

    def test_stage_approval_still_covers_its_completed_additions_after_a_proposal_revision(self):
        self.contract['stages'] = [{'id': 'fix', 'acceptance_ids': ['C']}, {'id': 'docs', 'acceptance_ids': ['C']}]
        self.tasks.create(self.contract)
        queue = {'path': TEMPLATE, 'type': 'Microsoft.ServiceBus/namespaces/queues', 'name': 'billing/settlement'}

        def approve(summary):
            current = self.tasks.snapshot('task')
            proposed = self.tasks.propose('task', {'summary': summary, 'stage_ids': ['fix', 'docs'],
                'effects': [self.effect('arm_resource', queue)]}, expected_revision=current['revision'])
            return self.tasks.approve('task', proposal_hash=proposed['proposal_hash'], approved_stage_ids=['fix', 'docs'],
                                      user_message_ref='chat:approval:' + str(proposed['revision']))

        first = approve('Add the queue, then document it.')
        self.template([{**NAMESPACE, 'resources': [QUEUE]}])
        reason = RESOURCE + TEMPLATE + ' (microsoft.servicebus/namespaces/queues: billing/settlement)'
        self.assertEqual(self.reasons('docs'), [reason])  # Approved for fix, which has not run yet.
        with transaction(self.store.db) as db:
            write_record(db, 'stage', 'task:fix', {'state': 'COMPLETED', 'contract_hash': first['contract_hash'],
                         'proposal_hash': first['proposal_hash']}, expected_revision=0)
        self.assertEqual(self.reasons('docs'), [])
        approve('Add the queue, then document it with the agreed wording.')
        self.assertEqual(self.reasons('docs'), [])

    def test_new_file_without_a_baseline_reports_its_literal_table(self):
        self.authorize()
        extra = 'src/Migrations/20261127_Archive.cs'
        self.migration(CREATE_TABLE.replace('Invoices', 'Archive'), path=extra)
        self.assertEqual(self.reasons(), [SCHEMA + extra + ' (dbo.Archive)'])

    def test_unreadable_comparison_holds_the_gate_with_a_specific_path(self):
        self.authorize()
        self.write(TEMPLATE, '{"resources": [')
        self.assertEqual(self.reasons(), ['ARM resource addition comparison unavailable: ' + TEMPLATE])

    def test_existing_opt_in_cannot_be_disabled_by_the_same_task(self):
        self.authorize()
        self.configure(False)
        self.migration(CREATE_TABLE + '\n' + ADD_COLUMN)
        self.assertEqual(self.reasons(), [SCHEMA + MIGRATION + ' (dbo.Invoices.ExternalId)'])

    def test_general_preapproval_does_not_authorize_a_resource(self):
        self.authorize(self.effect('arm_resource', {'path': TEMPLATE, 'type': 'Microsoft.ServiceBus/namespaces/queues',
                       'name': 'billing/settlement'}), preapproved=True)
        self.template([{**NAMESPACE, 'resources': [QUEUE]}])
        self.assertEqual(self.reasons(), [RESOURCE + TEMPLATE + ' (microsoft.servicebus/namespaces/queues: billing/settlement)'])

    def test_disabled_guards_leave_design_review_to_existing_checks(self):
        self.configure(False)
        self.authorize()
        self.migration(CREATE_TABLE + '\n' + ADD_COLUMN)
        self.template([{**NAMESPACE, 'resources': [QUEUE]}])
        self.assertEqual(self.reasons(), [])
