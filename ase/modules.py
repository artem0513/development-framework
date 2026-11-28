"""Functional module boundaries and independently runnable regression suites.

This catalogue is developer tooling, not the task acceptance/evidence gate.
Run ``python -B -m ase.modules`` or ``python -B -m ase.modules memory --test``.
While iterating, ``python -B -m ase.modules --affected FILE... --test`` runs the suites of the
modules owning those files plus every module that depends on them; run the full suite before push.
"""
import argparse
from dataclasses import asdict, dataclass
import json
from pathlib import Path
import sys
import unittest


@dataclass(frozen=True)
class Module:
    name: str
    responsibility: str
    sources: tuple[str, ...]
    tests: tuple[str, ...]
    depends_on: tuple[str, ...] = ()
    skills: tuple[str, ...] = ()


def _module(name, responsibility, sources, tests, depends=(), skills=()):
    return Module(name, responsibility, tuple('ase/'+p for p in sources.split()),
        tuple('test_'+p+'.py' for p in tests.split()), tuple(depends), tuple(skills))


MODULES = (
    _module('storage', 'Local project identity, SQLite schema and transaction unit of work.',
        'store.py state.py projects.py project_local.py native_paths.py timezones.py',
        'state schema_v5 projects project_local project_local_state timezones'),
    _module('tasks', 'Versioned commands/queries, original scope, approval and obligations.',
        'tasks.py', 'task_contract_v2 task_effects audit_authority scope_audit partial_progress mixed_profiles', ('storage',), ('task-intake',)),
    _module('discovery', 'Relevant sources, existing examples, capabilities and blocker investigation.',
        'discovery.py source_coverage.py inspection.py repositories.py',
        'discovery discovery_policy_freshness discovery_refresh_retention directory_sources inspection scoped_readiness progress_sources audit_source_evidence nested_repositories',
        ('storage','tasks'), ('task-discovery',)),
    _module('test-design', 'Bot-owned test design from requirements, project patterns and domain skills; no automatic generator.',
        'testing/adequacy.py testing/bugfix.py', 'production_reasoning_scenarios reasoning_rule_evaluation adequacy_policy test_adequacy execution_evidence bugfix_evidence native_bugfix_evidence', ('tasks','discovery','backends'), ('task-orchestrator','quality-gate')),
    _module('implementation', 'Persist/apply scoped edits with before-image and crash reconciliation.',
        'edit_application.py local_edit_intent.py candidate_capture.py',
        'edit_application local_edit_authority local_edit_inventory local_edit_recovery candidate_capture candidate_capture_recovery audit_candidate_authority',
        ('storage','tasks')),
    _module('testing', 'Execute approved checks, capture logs and actual test counts.',
        'testing/__init__.py testing/execution.py', 'bun_counts command_log_paths secret_execution ci_runner',
        ('storage','tasks','discovery','execution')),
    _module('review', 'Run independent acceptance and review sessions against current evidence.',
        'review/__init__.py review/service.py review/schema.py review/blind.py review/examples.py usage.py findings.py ci_check.py prompts.py report_md.py finish.py quick.py', 'stage_history revision_history audit_prerequisite usage findings ci_check agent_hooks report_md eval_suite replica_eval_fixture scope_eval_fixture helper_eval_fixture finish review_schema quick blind_review',
        ('tasks','evidence','backends','test-design'), ('independent-ac-verifier','independent-code-reviewer')),
    _module('lead-review', 'Original-main-bot whole-task critiques, approved requirements and capture recovery.',
        'lead_review.py lead_inputs.py', 'lead_review lead_review_crash lead_review_integration lead_inputs',
        ('storage','tasks','backends','discovery'), ('task-orchestrator','quality-gate')),
    _module('evidence', 'Current proof/coverage, blocker evidence, policy and deterministic gate.',
        'evidence/__init__.py evidence/packets.py evidence/compact.py evidence/impact.py evidence/check_inputs.py evidence/representation.py evidence/dotnet_packages.py evidence/guarded_additions.py evidence/efcore_schema.py evidence/arm_resources.py gates.py profiles.py action_evidence.py runtime_policy.py model_limits.py framework.py',
        'action_packet_evidence change_impact context_transport review_packet model_limits packet_baseline_reads gate_regression profiles runtime_policy runtime_boundaries protected_authorization dotnet_package_approval guarded_additions framework_update',
        ('storage','tasks','discovery','backends','test-design'), ('quality-gate',)),
    _module('memory', 'Dated knowledge, dependency freshness, alternatives and durable task context.',
        'knowledge.py continuity.py memory/__init__.py memory/context.py memory/conflicts.py memory/reverify.py',
        'knowledge knowledge_refresh knowledge_conflicts knowledge_scope_isolation continuity context_modules',
        ('storage','tasks'), ('project-context-manager',)),
    _module('reporting', 'Append-only work history, atomic closure and effective dated meeting projections.',
        'journal.py memory/repository.py memory/reports.py', 'journal report_projection',
        ('storage','tasks','memory'), ('time-reporter',)),
    _module('delegation', 'Native-worker registry, prerequisite DAG, exact attempts and sealed results.',
        'delegations.py delegation_artifacts.py', 'delegations delegation_artifacts delegation_crash delegation_integration',
        ('storage','tasks','evidence')),
    _module('recovery', 'Progress-based stopping, diagnostic leads and durable effect reconciliation.',
        'recovery.py operations.py', 'progress_recovery stopping stopping_operations operation_alias_gate operation_reconciliation operations process_ownership audit_recovery_identity',
        ('storage','tasks')),
    _module('execution', 'Approved command/access effects, ownership and operation observations.',
        'execution/__init__.py execution/actions.py execution/ports.py', 'access_revalidation', ('storage','tasks','recovery','access')),
    _module('backends', 'Codex/Claude/Copilot/DeepSeek adapters, process capture, protocol and receipts.',
        'adapters/__init__.py adapters/codex.py adapters/claude.py adapters/copilot.py adapters/deepseek.py '
        'adapters/deepseek_worker.py child_environment.py runner.py protocol.py',
        'adapter_security codex_adapter claude_adapter copilot_adapter deepseek_adapter runner', ('storage',)),
    _module('access', 'Encrypted local credential profiles and approved authenticated access.',
        'access.py credentials.py _http_worker.py', 'access credentials credential_cli credential_integration', ('storage','tasks')),
    _module('delivery', 'Publication fence, observed Git state, transfer and project installation.',
        'publishing.py transfer.py doctor.py', 'publishing_v2 publishing_observed_inputs transfer portable_install project_install project_local_install private_install ci_inspector public_content',
        ('storage','tasks','evidence','memory'), ('project-bootstrap','gh-fix-ci')),
    _module('coordination', 'CLI and workflow manager joining modules in the approved stage order.',
        '__init__.py __main__.py workflow.py modules.py coordinator.py', 'cli_v2 workflow_v2 workflow_scenarios module_boundaries reasoning_rule_installation coordinator_events',
        ('tasks','discovery','implementation','testing','review','reporting','delegation','recovery'), ('task-orchestrator',)),
    # core.py holds shared runtime used by v2 (execute, redact, protected paths, test counts, command
    # validation) plus LocalRecords (time, decisions, session summaries, read-only v1 history). The v1
    # execution engine was retired; new v2 features go elsewhere, only shared helpers stay here.
    _module('legacy', 'Shared executable receipts/redaction/protected paths plus time/decision records and read-only v1 history.',
        'core.py', 'system', ('storage','backends')),
)


def get_module(name):
    for module in MODULES:
        if module.name == name:
            return module
    raise ValueError('Unknown module: '+name)


def test_files(name, *, root=None):
    root = Path(root) if root is not None else Path(__file__).resolve().parent.parent
    paths = [root/'tests'/file for file in get_module(name).tests]
    missing = [str(p) for p in paths if not p.is_file()]
    if missing:
        raise ValueError('Module tests unavailable in this checkout: '+', '.join(missing))
    return sorted(set(paths))


def affected(paths):
    """Modules owning the given files plus all transitive dependents; tests and skills map by name."""
    normalized = {str(p).replace('\\', '/').removeprefix('./') for p in paths}
    owners = {m.name for m in MODULES if normalized & set(m.sources)
              or any(t in normalized for t in ('tests/' + name for name in m.tests))
              or any(p.startswith('skills/' + s + '/') for s in m.skills for p in normalized)}
    selected, grew = set(owners), True
    while grew:
        grew = False
        for m in MODULES:
            if m.name not in selected and selected & set(m.depends_on):
                selected.add(m.name)
                grew = True
    return [m for m in MODULES if m.name in selected]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('module', nargs='?')
    parser.add_argument('--test', action='store_true', help='Run this module\'s real unittest suite')
    parser.add_argument('--list-tests', action='store_true', help='List test files without executing them')
    parser.add_argument('--affected', nargs='+', metavar='FILE', help='Select modules owning these files and their dependents')
    args = parser.parse_args(argv)
    if (args.test or args.list_tests) and not (args.module or args.affected):
        parser.error('--test/--list-tests requires a module or --affected')
    try:
        modules = (affected(args.affected) if args.affected else
                   [get_module(args.module)] if args.module else list(MODULES))
        if args.affected and not modules:
            raise ValueError('No module owns these files; run the full suite')
        unowned = sorted({p.replace('\\', '/').removeprefix('./') for p in args.affected or []}
                         - {s for m in MODULES for s in m.sources} - {'tests/' + t for m in MODULES for t in m.tests})
        unowned = [p for p in unowned if not any(p.startswith('skills/' + s + '/') for m in MODULES for s in m.skills)]
        if unowned:
            print(json.dumps({'warning': 'No module owns these files; cover them with the full suite', 'files': unowned}),
                  file=sys.stderr)
        if args.test or args.list_tests:
            paths = sorted({p for m in modules for p in test_files(m.name)})
            if args.list_tests:
                print(json.dumps([str(p) for p in paths], indent=2))
                return 0
            suite = unittest.TestSuite()
            for path in paths:
                suite.addTests(unittest.TestLoader().discover(str(path.parent), pattern=path.name))
            if not suite.countTestCases():
                raise ValueError('Selected module has zero executable tests')
            result = unittest.TextTestRunner(verbosity=2).run(suite)
            return 0 if result.wasSuccessful() else 1
        print(json.dumps([asdict(module) for module in modules], indent=2))
        return 0
    except ValueError as error:
        print(json.dumps({'error':str(error)}), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
