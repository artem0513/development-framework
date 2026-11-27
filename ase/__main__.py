"""Dependency-free command interface. All output is structured JSON."""
import argparse
from dataclasses import asdict
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

from .core import LocalRecords, load, redact
from .continuity import Continuity
from .knowledge import Knowledge
from .projects import ProjectRegistry, local_config, resolve_state_home
from .project_local import validate_local_state
from .store import Store
from .tasks import TaskService
from .workflow import WorkflowController
from .gates import GateService, source_snapshot
from .journal import Journal
from .discovery import discover_capabilities, record_discovery, discovery_refresh_scope
from .transfer import export_project, import_project
from .publishing import Publisher
from .state import read_record, text, transaction, write_record
from .credentials import CredentialError, CredentialVault, _json, _read, _safe_path


class UsageError(ValueError):
    def __init__(self, message, usage):
        super().__init__(message)
        self.usage = usage


class JSONArgumentParser(argparse.ArgumentParser):
    # Subparsers inherit this class; Python 3.14+ argparse would otherwise emit ANSI colors.
    def __init__(self, *args, **kwargs):
        if sys.version_info >= (3, 14):
            kwargs.setdefault('color', False)
        super().__init__(*args, **kwargs)

    def error(self, message):
        raise UsageError(message, self.format_usage().strip())


def _delegation_command(store, args):
    from .delegations import DelegationRegistry
    registry = DelegationRegistry(store)
    if args.action == 'list':
        return registry.snapshot(args.task_id)
    if args.action == 'register':
        return registry.register(args.task_id, load(args.file), expected_revision=args.expected_revision)
    if args.action == 'recover':
        return registry.recover(args.task_id, args.work_id)
    kwargs = {'expected_revision': args.expected_revision}
    if args.action == 'dispatch':
        return registry.dispatch(args.task_id, args.work_id, coordinator=args.coordinator, backend=args.backend, **kwargs)
    if args.action in {'attach', 'result'}:
        return getattr(registry, args.action)(args.task_id, args.work_id, args.attempt_id, load(args.file), **kwargs)
    if args.action == 'accept':
        return registry.accept(args.task_id, args.work_id, args.attempt_id, coordinator=args.coordinator, summary=args.summary, **kwargs)
    return registry.cancel(args.task_id, args.work_id, reason=args.reason, **kwargs)


def _events_command(store, args):
    from .coordinator import CoordinatorEvents
    service = CoordinatorEvents(store)
    if args.action == 'list':
        return service.snapshot(args.task_id)
    if args.action == 'recover':
        return service.recover(args.task_id)
    if args.action == 'bind':
        return service.bind(args.task_id, load(args.file),
            user_message_ref=args.user_message_ref, expected_revision=args.expected_revision)
    if args.action == 'bind-local':
        result = service.bind_local(args.task_id,backend=args.backend,
            user_message_ref=args.user_message_ref,expected_revision=args.expected_revision)
        from .lead_review import LeadReview
        next_action = LeadReview(store).gate(args.task_id)['next_action']
        return {**result, 'next_action': next_action} if next_action and 'next_action' not in result else result
    kwargs = {'expected_revision': args.expected_revision}
    if args.action == 'dispatch':
        return service.dispatch(args.task_id, args.event_id, **kwargs)
    if args.action == 'observe':
        return service.observe(args.task_id, args.event_id, attempt_id=args.attempt,
            observation=load(args.file), **kwargs)
    if args.action == 'claim':
        return service.claim(args.task_id, args.event_id, thread_id=args.thread_id, **kwargs)
    return service.ack(args.task_id, args.event_id, processing_id=args.processing_id,
        thread_id=args.thread_id, outcome=args.outcome, evidence=args.evidence, **kwargs)


THREAD_ID_HELP = 'payload.destination.thread_id printed by `ase events bind-local` (also in `ase events list`)'

LEAD_REVIEW_HELP = {
    'classify': 'Record the trivial/nontrivial classification of a configured root task from --file',
    'prepare': 'Open the next whole-solution critique ticket and print its prompt; one unsettled ticket at a time',
    'record': 'Record the assessment for the prepared ticket (--attempt is its ticket id) from --file',
    'status': 'Show the lead-review record (its revision is the next --expected-revision) and policy',
    'recover': 'Commit a critique capture left pending by an interrupted record; never relaunches',
}

EVENTS_HELP = {
    'bind': 'Bind the root task\'s original coordinator chat from a destination --file; immutable once bound',
    'bind-local': 'Bind this chat as the original coordinator with a minted local inbox id when the host has no chat id',
    'list': 'Show the coordinator binding and the root task\'s events with delivery and processing state',
    'recover': 'Re-enqueue captured independent results as events; never starts a worker',
    'dispatch': 'Reserve native delivery of an event to the bound chat and print its handoff prompt',
    'observe': 'Record the outcome (DELIVERED/UNKNOWN) of a native delivery attempt from --file',
    'claim': 'Claim an event for processing by the original coordinator thread before acting on it; prints its '
             'captured_result counts and superseded_by hint when reliably available',
    'ack': 'Mark a claimed event processed with its outcome and evidence',
}

TASK_HELP = {
    'create': 'Create a task from a v2 JSON contract file',
    'propose': 'Record the plan proposal the owner approves',
    'revise': 'Change the contract; creates a new version needing a fresh proposal',
    'plan': 'Replace the execution plan checks',
    'approve': 'Record owner approval of the current proposal',
    'discover': 'Record capability/source discovery (scans when --file is omitted)',
    'status': 'Show task state, readiness and usage',
    'check': 'Run the planned checks and record results',
    'run': 'Run the approved stages',
    'resume': 'Continue approved stages, clearing this task\'s own stop',
    'verify': 'Run verification and report its verdict',
    'gate': 'Evaluate the quality gate',
    'stop': 'Request the task to stop',
    'authorize-protected': 'Record owner authorization for protected paths at the current source',
    'approve-high-risk': 'Record owner approval for HIGH-risk work at the current source',
}


def parser():
    p = JSONArgumentParser(description="Project-local, evidence-driven supervised workflow")
    p.add_argument("--repo", default=".")
    p.add_argument("--home", help="Explicit state home (outside source or validated project-local state)")
    p.add_argument("--project", help="Registered project UUID or workspace alias")
    commands = p.add_subparsers(dest="command", required=True)
    lead = commands.add_parser('lead-review', help='Additional whole-task critiques by the original main bot')
    lead_commands = lead.add_subparsers(dest='action', required=True)
    for name in ('classify', 'prepare', 'record', 'status', 'recover'):
        command = lead_commands.add_parser(name, help=LEAD_REVIEW_HELP[name])
        command.add_argument('task_id')
        if name in {'classify', 'prepare', 'record'}:
            command.add_argument('--thread-id', required=True, help=THREAD_ID_HELP)
            command.add_argument('--expected-revision', required=True, type=int)
        if name in {'classify', 'record'}:
            command.add_argument('--file', required=True)
        if name == 'record':
            command.add_argument('--attempt', required=True)
    events = commands.add_parser('events', help='Durable original-coordinator result inbox; native host owns delivery')
    event_commands = events.add_subparsers(dest='action', required=True)
    for name in ('bind', 'bind-local', 'list', 'recover', 'dispatch', 'observe', 'claim', 'ack'):
        command = event_commands.add_parser(name, help=EVENTS_HELP[name])
        command.add_argument('task_id')
        if name not in {'bind', 'bind-local', 'list', 'recover'}:
            command.add_argument('event_id')
        if name in {'bind', 'bind-local'}:
            command.add_argument('--expected-revision', type=int, required=True,
                                 help="Revision of the root task's coordinator binding record: 0 to bind the first time; "
                                      'to repeat an existing binding, the revision the earlier bind printed')
        elif name not in {'list', 'recover'}:
            command.add_argument('--expected-revision', type=int, required=True,
                                 help='Current revision of this event record, as shown in `ase events list`')
        if name in {'bind', 'observe'}:
            command.add_argument('--file', required=True)
        if name in {'bind','bind-local'}:
            command.add_argument('--user-message-ref', required=True)
        if name=='bind-local':
            command.add_argument('--backend',choices=('codex','claude','copilot'),required=True)
        if name == 'observe':
            command.add_argument('--attempt', required=True)
        if name in {'claim', 'ack'}:
            command.add_argument('--thread-id', required=True, help=THREAD_ID_HELP)
        if name == 'ack':
            command.add_argument('--processing-id', required=True)
            command.add_argument('--outcome', choices=('handled', 'stale', 'blocked'), required=True,
                                 help='How the lead classified the claimed result; every outcome marks the event processed '
                                      'and grants no acceptance, gate or publication authority. handled: checked against current '
                                      'evidence and acted on (finding entered the repair/test/review loop, or the result was '
                                      'integrated or needs no action); stale: historical, e.g. superseded_by a later event or '
                                      'bound to an older source/contract; blocked: needs an owner decision or unavailable '
                                      'evidence, recorded in the task checkpoint')
            command.add_argument('--evidence', required=True,
                                 help='Short checkpoint reference of how the lead handled the event, e.g. a commit SHA, '
                                      'gate status, superseding event id or a brief note; nonempty, at most 2000 '
                                      'characters, no secrets or raw result content')
    adequacy = commands.add_parser('adequacy', help='Original scenario plans for independent software test adequacy')
    adequacy_commands = adequacy.add_subparsers(dest='action', required=True)
    for name in ('plan', 'status'):
        command = adequacy_commands.add_parser(name)
        command.add_argument('task_id')
        if name == 'plan':
            command.add_argument('--file', required=True)
            command.add_argument('--expected-revision', required=True, type=int)
    delegation = commands.add_parser('delegation', help='Persist native delegated work; bots keep creating subagents')
    delegation_commands = delegation.add_subparsers(dest='action', required=True)
    for name in ('register', 'list', 'dispatch', 'attach', 'result', 'accept', 'cancel', 'recover'):
        command = delegation_commands.add_parser(name)
        command.add_argument('task_id')
        if name == 'recover':
            command.add_argument('work_id', nargs='?')
        elif name not in {'register', 'list'}:
            command.add_argument('work_id')
        if name not in {'list', 'recover'}:
            command.add_argument('--expected-revision', type=int, default=0, required=name != 'register')
        if name in {'register', 'attach', 'result'}:
            command.add_argument('--file', required=True)
        if name in {'attach', 'result', 'accept'}:
            command.add_argument('--attempt', required=True, dest='attempt_id')
        if name in {'dispatch', 'accept'}:
            command.add_argument('--coordinator', required=True)
        if name == 'dispatch':
            command.add_argument('--backend', choices=('codex', 'claude', 'copilot'), required=True)
        if name == 'accept':
            command.add_argument('--summary', required=True)
        if name == 'cancel':
            command.add_argument('--reason', required=True)
    credentials = commands.add_parser("credentials", help="Manage encrypted local credential profiles").add_subparsers(dest="action", required=True)
    credential_import = credentials.add_parser("import")
    credential_import.add_argument("--file", required=True)
    credentials.add_parser("list")
    credential_configure = credentials.add_parser("configure")
    credential_configure.add_argument("ref")
    credential_configure.add_argument("--target", required=True)
    credential_configure.add_argument("--file", required=True)
    access = commands.add_parser("access", help="Use approved task access").add_subparsers(dest="action", required=True)
    for name in ("ensure", "request"):
        command = access.add_parser(name)
        command.add_argument("task_id")
        command.add_argument("--stage", required=True)
        command.add_argument("--credential", required=True)
        command.add_argument("--target", required=True)
        if name == "request":
            command.add_argument("--path", default="/")
            command.add_argument("--method", choices=("GET", "HEAD"), default="GET")
    ci = commands.add_parser("ci-check", help="Deterministic merge checks for a trusted CI runner (rules from the base revision)")
    ci.add_argument("--base", required=True, help="Base revision, e.g. origin/main")
    ci.add_argument("--head", default="HEAD")
    ci.add_argument("--max-lines", type=int, default=None)
    ci.add_argument("--max-files", type=int, default=None)
    ci.add_argument("--allow-protected", action="store_true", help="Owner override for protected-path changes")
    ci.add_argument("--allow-large", action="store_true", help="Owner override for the diff size budget")
    ci.add_argument("--allow-test-removal", action="store_true", help="Owner override for deleted tests or removed assertions")
    ci.add_argument("--allow-test-skip", action="store_true", help="Owner override for added skip/xfail markers")
    ci.add_argument("--text", action="store_true", help="Print a human summary instead of JSON")
    prompt = commands.add_parser("prompt", help="Captured owner prompts for verifiable prompt:<id> approval references")
    prompt_commands = prompt.add_subparsers(dest="action", required=True)
    prompt_commands.add_parser("capture", help="Read a UserPromptSubmit hook event from stdin; prints nothing")
    prompt_list = prompt_commands.add_parser("list")
    prompt_list.add_argument("--limit", type=int, default=20)
    commands.add_parser("init", help="Inspect repository and show candidate commands")
    capabilities = commands.add_parser("capabilities", help="Read scoped tool/skill metadata and candidate checks without executing them")
    capabilities.add_argument("--file", help="JSON with scope and backend_config; default scope is project-root build metadata")
    start = commands.add_parser("start", help="Create a task from a v2 JSON contract (same as task create)")
    start.add_argument("contract")
    for name in ("check", "gate", "report", "approve-high-risk"):
        sub = commands.add_parser(name)
        sub.add_argument("task_id")
        if name == "report":
            sub.add_argument("--markdown", action="store_true", help="Print a pull request body instead of JSON")
        if name == "approve-high-risk":
            sub.add_argument("--user-message-ref")
            sub.add_argument("--reason")
    for name in ("run", "resume", "verify"):
        sub = commands.add_parser(name)
        sub.add_argument("task_id")
    status = commands.add_parser("status")
    status.add_argument("task_id", nargs="?")
    commands.add_parser("context")
    commands.add_parser("backup")
    daily = commands.add_parser("daily")
    daily.add_argument("--date")
    weekly = commands.add_parser("weekly", help="Draft seven days ending on --date; groups by stored start-date")
    weekly.add_argument("--date")
    authorize = commands.add_parser("authorize")
    authorize.add_argument("--remote", default="origin")
    authorize.add_argument("--disable", action="store_true")
    authorize.add_argument("--repository", help="Declared nested repository (workspace-relative, e.g. src/orders) whose remote is authorized")
    protected = commands.add_parser("authorize-protected", help="Record explicit user authorization for exact protected source paths at current revision")
    protected.add_argument("task_id")
    protected.add_argument("--path", action="append", required=True, dest="paths")
    protected.add_argument("--reason", required=True)
    protected.add_argument("--user-message-ref")
    publish = commands.add_parser("publish")
    publish.add_argument("task_id")
    publish.add_argument("--execute", action="store_true")
    publish.add_argument("--remote", default="origin")
    publish.add_argument("--commit", dest="commit_message")
    decision = commands.add_parser("decision")
    decision.add_argument("--task-id")
    decision.add_argument("--context", required=True)
    decision.add_argument("--decision", required=True)
    decision.add_argument("--rationale", required=True)
    entry = commands.add_parser("time")
    entry.add_argument("task_id")
    entry.add_argument("--activity", required=True)
    entry.add_argument("--start", required=True)
    entry.add_argument("--end", required=True)
    entry.add_argument("--source", choices=["agent", "human"], default="agent")
    entry.add_argument("--confirmed", action="store_true")
    correction = commands.add_parser("correct-time", help="Correct an entry with an atomic before/after audit")
    correction.add_argument("entry_id", type=int)
    correction.add_argument("--activity")
    correction.add_argument("--start")
    correction.add_argument("--end")
    correction.add_argument("--source", choices=["agent", "human"])
    confirmation = correction.add_mutually_exclusive_group()
    confirmation.add_argument("--confirmed", dest="confirmed", action="store_true", default=None)
    confirmation.add_argument("--unconfirmed", dest="confirmed", action="store_false")
    correction.add_argument("--reason", default="Explicit CLI correction")
    session = commands.add_parser("session-summary", help="Save concise decisions and unresolved questions for resumption")
    session.add_argument("task_id")
    session.add_argument("--summary", required=True)
    session.add_argument("--decision", default="")
    session.add_argument("--unresolved", default="")
    for name in ("continuity-snapshot", "restore", "coverage"):
        sub = commands.add_parser(name)
        sub.add_argument("task_id")
    checkpoint = commands.add_parser("checkpoint", help="Save a source-observed handoff with revision compare-and-swap")
    checkpoint.add_argument("task_id")
    checkpoint.add_argument("--expected-revision", required=True, type=int)
    checkpoint.add_argument("--file", required=True)
    source = commands.add_parser("source-record", help="Record declared source ranges and analysis gaps; no fetching")
    source.add_argument("task_id")
    source.add_argument("--file", required=True)
    source.add_argument("--expected-sequence", type=int, required=True)
    knowledge = commands.add_parser("knowledge-record", help="Append a dated, sourced historical claim")
    knowledge.add_argument("--file", required=True)
    query = commands.add_parser("knowledge-query", help="Read historical project knowledge, including superseded claims")
    query.add_argument("--topic")
    commands.add_parser("knowledge-lint", help="Check local reference structure and parallel claims without semantic authority")
    project = commands.add_parser("project").add_subparsers(dest="action", required=True)
    registration = project.add_parser("register")
    registration.add_argument("--root")
    registration.add_argument("--kind", required=True)
    registration.add_argument("--timezone", help="Reporting timezone: local (default) or an IANA name; project-local config may override")
    project.add_parser("list")
    attach = project.add_parser("attach")
    attach.add_argument("project_id")
    attach.add_argument("--root", required=True)
    attach.add_argument("--expected-revision", type=int, required=True)
    exporting = project.add_parser("export")
    exporting.add_argument("destination")
    exporting.add_argument("--include-verbose", action="store_true")
    importing = project.add_parser("import")
    importing.add_argument("archive")
    importing.add_argument("--root", required=True)
    importing.add_argument("--mode", choices=["copy", "restore"], default="copy")
    task = commands.add_parser("task").add_subparsers(dest="action", required=True)
    creation = task.add_parser("create", help=TASK_HELP["create"])
    creation.add_argument("--file", required=True)
    creation.add_argument("--bugfix", action="store_true", help="Require causal evidence, fix necessity and consumer regression proof")
    creation.add_argument('--compact-context', action='store_true', help='Enable bounded exact context body representations')
    for name in ("propose", "revise", "plan"):
        command = task.add_parser(name, help=TASK_HELP[name])
        command.add_argument("task_id")
        command.add_argument("--file", required=True)
        command.add_argument("--expected-revision", required=True, type=int)
    approval = task.add_parser("approve", help=TASK_HELP["approve"])
    approval.add_argument("task_id")
    approval_input = approval.add_mutually_exclusive_group(required=True)
    approval_input.add_argument("--file")
    approval_input.add_argument("--proposal-hash")
    approval.add_argument("--user-message-ref")
    approval.add_argument("--stage", action="append", dest="approved_stage_ids", metavar="STAGE_ID",
                          help="Approved stage id from the proposal's stage_ids; repeat for each (required with --proposal-hash)")
    approval.add_argument("--owner-preapproved", action="store_true",
                          help="The cited owner message approved following the recommended plan before it existed")
    discovery = task.add_parser("discover", help=TASK_HELP["discover"])
    discovery.add_argument("task_id")
    discovery.add_argument("--file", help="Explicit structured discovery, including source intake")
    discovery.add_argument("--expected-revision", type=int, required=True)
    for name in ("status", "check", "run", "resume", "verify", "gate"):
        command = task.add_parser(name, help=TASK_HELP[name])
        command.add_argument("task_id")
        if name in {"check", "verify"}:
            command.add_argument("--stage", help="Refresh evidence for one approved stage")
    findings = task.add_parser("findings", help="List current independent findings, their journal and owner dispositions")
    findings.add_argument("task_id")
    dispose = task.add_parser("dispose-finding", help="Record the owner's waive (P2) or disprove decision for a finding")
    dispose.add_argument("task_id")
    dispose.add_argument("--finding", required=True, dest="finding_id")
    dispose.add_argument("--disposition", required=True, choices=("waive", "disprove"))
    dispose.add_argument("--reason", required=True)
    dispose.add_argument("--user-message-ref", required=True)
    dispose.add_argument("--owner-quote", help="Owner's exact words when prompt capture is unavailable (reported as not verified)")
    quick = task.add_parser("quick", help="Small LOW-risk change: create, discover and propose in one step; the owner approves the plan")
    quick.add_argument("--id", required=True, dest="quick_id")
    quick.add_argument("--request", required=True, help="The owner's request (becomes the acceptance criterion)")
    quick.add_argument("--resource", action="append", required=True, dest="resources")
    quick.add_argument("--test", required=True, dest="test_command", help='Test command, e.g. "python -m pytest"')
    quick.add_argument("--summary", required=True, help="Planned change in one or two sentences")
    quick.add_argument("--user-message-ref", required=True)
    quick.add_argument("--backend", default="claude", choices=("codex", "claude", "copilot"))
    quick.add_argument("--base", help="Default branch; refuses the quick lane if the branch is already over its budget")
    finish = task.add_parser("finish", help="Before push/PR: gate + clean tree + ci-check + PR body in one answer")
    finish.add_argument("task_id")
    finish.add_argument("--base", required=True, help="Base revision, e.g. origin/main")
    finish.add_argument("--body-file", help="Write the PR body (markdown) to this file; outside the framework's own "
                        "repository it must be outside the repository, and an existing file is read as your own "
                        "description, checked for framework mentions and kept (owner exceptions appended)")
    for flag in ("--allow-protected", "--allow-large", "--allow-test-removal", "--allow-test-skip"):
        finish.add_argument(flag, action="store_true", help="Owner override passed to ci-check; needs --override-ref")
    finish.add_argument("--override-ref", help="Owner decision for the overrides: prompt:<id>, or a chat ref with --override-quote")
    finish.add_argument("--override-quote", help="Owner's exact words when prompt capture is unavailable (reported as not verified)")
    stop = task.add_parser("stop", help=TASK_HELP["stop"])
    stop.add_argument("task_id")
    stop.add_argument("--reason", required=True)
    for name in ("authorize-protected", "approve-high-risk"):
        command = task.add_parser(name, help=TASK_HELP[name])
        command.add_argument("task_id")
        command.add_argument("--user-message-ref", required=True)
        command.add_argument("--reason", required=True)
        if name == "authorize-protected":
            command.add_argument("--path", action="append", required=True, dest="paths")
    session = commands.add_parser("session").add_subparsers(dest="action", required=True)
    for name in ("start", "finish"):
        command = session.add_parser(name)
        command.add_argument("task_id")
        command.add_argument("session_id", nargs="?")
        command.add_argument("--session-id", dest="session_option")
        if name == "finish":
            command.add_argument("--file", required=True)
            command.add_argument("--closure-key", required=True)
            command.add_argument("--expected-revision", required=True, type=int)
            command.add_argument("--expected-context-revision", required=True, type=int)
    for name in ("worklog", "meeting"):
        command = commands.add_parser(name, aliases=["meeting-report"] if name == "meeting" else [])
        command.add_argument("--start", "--from", dest="start", required=True)
        command.add_argument("--until", required=True)
        command.add_argument("--timezone")
        if name == "meeting":
            command.add_argument("--as-of-end", action="store_true")
    knowledge = commands.add_parser("knowledge").add_subparsers(dest="action", required=True)
    retrieval = knowledge.add_parser("retrieve")
    retrieval.add_argument("--file", required=True, help="Object containing explicit scope and optional topic/refresh")
    observations = knowledge.add_parser("observations")
    observations.add_argument("--file", required=True)
    observations.add_argument("--expected-revision", type=int, required=True)
    return p


def _task_command(store, action, args):
    tasks = TaskService(store)
    if action == "quick":
        from .quick import start
        return start(store, task_id=args.quick_id, request=args.request, resources=args.resources,
                     test_command=args.test_command, summary=args.summary, user_message_ref=args.user_message_ref,
                     backend=args.backend, base=args.base)
    if action == "create":
        contract=_new_contract(args.file)
        if args.bugfix:
            contract['bugfix_evidence']={'version':1,'required':True}
        if getattr(args, 'compact_context', False):
            contract['context_transport']={'version':1,'mode':'lossless'}
        return tasks.create(contract)
    task = tasks.snapshot(args.task_id)
    if getattr(args, "user_message_ref", None):
        from .prompts import resolve
        resolve(store, args.user_message_ref)  # a prompt:<id> reference must name a captured prompt
    if action == "status":
        from .usage import task_usage
        return {**task, "readiness": tasks.readiness(args.task_id, include_progress=True), "usage": task_usage(store, args.task_id)}
    if action in {"findings", "dispose-finding"}:
        from .findings import Findings
        service = Findings(store)
        if action == "findings":
            return service.list(args.task_id)
        return service.dispose(args.task_id, args.finding_id, args.disposition,
                               reason=args.reason, user_message_ref=args.user_message_ref, owner_quote=args.owner_quote)
    if action == "revise":
        return tasks.revise(args.task_id, load(args.file), expected_revision=args.expected_revision)
    if task["legacy_v1"]:
        raise ValueError("New execution and approvals require a reviewed v2 task contract; legacy history remains readable")
    if action == "propose":
        return tasks.propose(args.task_id, load(args.file), expected_revision=args.expected_revision)
    if action == "plan":
        return tasks.update_execution_plan(args.task_id, load(args.file), expected_revision=args.expected_revision)
    if action == "approve":
        if args.file:
            if args.user_message_ref is not None or args.approved_stage_ids is not None or args.owner_preapproved:
                raise ValueError("Approval file cannot be mixed with approval flags")
            payload = load(args.file)
            if isinstance(payload, dict) and isinstance(payload.get("user_message_ref"), str):
                from .prompts import resolve
                resolve(store, payload["user_message_ref"])
        else:
            payload = {"proposal_hash": args.proposal_hash, "user_message_ref": args.user_message_ref, "approved_stage_ids": args.approved_stage_ids,
                       "owner_preapproved": args.owner_preapproved}
        result = tasks.approve(args.task_id, **payload)
        contract = task["contract"]
        if contract.get("lane") == "quick" and isinstance(result, dict):
            from .quick import steps_after_approval
            steps = steps_after_approval(args.task_id, contract["scope"]["resources"], contract["execution_policy"]["backend"])
            if result.get("baseline_warning"):
                steps = ["# " + result["baseline_warning"], *steps]
            result = {**result, "next": steps}
        return result
    if action == "discover":
        payload = load(args.file) if args.file else discover_capabilities(store.project,
            scope=discovery_refresh_scope(store, args.task_id), backend_config={})
        return record_discovery(store, args.task_id, payload, expected_revision=args.expected_revision)
    if action == "gate":
        return GateService(store).gate(args.task_id)
    if action == "finish":
        from .finish import finish
        result = finish(store, args.task_id, args.base, allow_protected=args.allow_protected,
                        allow_large=args.allow_large, allow_test_removal=args.allow_test_removal,
                        allow_test_skip=args.allow_test_skip, body_file=args.body_file,
                        override_ref=args.override_ref, override_quote=args.override_quote)
        if args.body_file and result.get("write_body_file", True):
            Path(args.body_file).write_text(result["pr_body"], encoding="utf-8")
        return result
    if action in {"authorize-protected", "approve-high-risk"}:
        text(args.user_message_ref, "user_message_ref", limit=2000)
        text(args.reason, "reason", limit=4000)
        source = source_snapshot(store, task["contract"], task_id=args.task_id)
        payload = {"fingerprint": source["fingerprint"], "contract_hash": task["contract_hash"],
                   "reason": args.reason, "user_message_ref": args.user_message_ref}
        kind = "high_risk_approval"
        if action == "authorize-protected":
            from .workflow import _target
            payload["paths"] = list(dict.fromkeys(_target(store.repo.resolve(), value).relative_to(store.repo).as_posix() for value in args.paths))
            kind = "protected_approval"
        with transaction(store.db) as db:
            old = read_record(db, kind, args.task_id)
            write_record(db, kind, args.task_id, payload, expected_revision=old["revision"])
        return payload
    controller = WorkflowController(store)
    stage_id = getattr(args, "stage", None)
    if action == "check":
        return controller.check(args.task_id, stage_id) if stage_id else controller.check(args.task_id)
    if action == "verify" and stage_id:
        return controller.verify(args.task_id, stage_id)
    if action == "stop":
        return controller.request_stop(args.task_id, reason=args.reason)
    if action in {"run", "resume", "verify"}:
        if task["state"] == "COMPLETED":
            if action == "verify":
                return controller.verify(args.task_id)
            return GateService(store).gate(args.task_id)
        # Prevent even backend probing until at least one current stage is approved.
        approved = task.get("approved_stage_ids", []) or ([task["contract"]["stages"][0]["id"]] if task["contract"].get("parent_task_id") else [])
        if not approved:
            from .tasks import approval_hint
            raise ValueError("Stage approval not recorded for the current v2 proposal: " + approval_hint(task))
        blockers = []
        for stage_id in approved:
            # Validate one runnable stage before probing any backend. The
            # scheduler retains scoped blockers and continues independent work.
            # Resume may clear only this task's own stop; ancestor stops and
            # cancellation/approval checks remain active in require_approval.
            try:
                tasks.require_approval(args.task_id, stage_id, allow_stopped=action == "resume")
            except ValueError as error:
                blockers.append(str(error))
            else:
                if action != "verify":
                    break
        else:
            if blockers:
                raise ValueError("; ".join(dict.fromkeys(blockers)))
        if action == "verify":
            return controller.verify(args.task_id)
        return controller.run(args.task_id, resume=action == "resume")
    raise ValueError("Unknown task command")


def _new_contract(path):
    from .profiles import has_software
    contract = load(path)
    if isinstance(contract, dict) and contract.get('schema_version') == 2:
        from .lead_review import policy_for_risk
        contract.setdefault('lead_review', policy_for_risk(contract.get('risk')))
        contract.setdefault('review_format', {'version': 2})
    if isinstance(contract, dict) and contract.get('schema_version') == 2 and has_software(contract):
        contract.setdefault('test_adequacy', {'version': 1, 'required': True})
        from .review.blind import POLICY as BLIND_REVIEW
        contract.setdefault('blind_review', dict(BLIND_REVIEW))
    return contract


def _state_project(args):
    """A UUID selects within the repo/cwd registry, never a filesystem root."""
    if args.project:
        if Path(args.project).expanduser().is_dir():
            return args.project
        if local_config(args.repo) is None:
            return args.project
    return args.repo


def _project_command(args):
    selection = getattr(args, "root", None) or _state_project(args)
    home = resolve_state_home(args.home, project=selection)
    if args.action == "import":
        return asdict(import_project(Path(args.archive), home, root=Path(args.root), mode=args.mode))
    with_registry = ProjectRegistry(home, project=selection)
    try:
        if args.action == "register":
            config = local_config(selection)
            root = config["root"] if config else args.root or args.repo
            result = asdict(with_registry.register(root, kind=args.kind, timezone=args.timezone))
            requested = Path(args.root or args.repo).expanduser().resolve()
            if config and requested != Path(root).resolve() and requested.is_relative_to(Path(root).resolve()):
                from .repositories import declared_repositories, owner
                nested = owner(declared_repositories(root), requested.relative_to(Path(root).resolve()).as_posix())
                if nested:
                    # One installation serves every declared clone: tasks name it by scope/repository.
                    result.update(repository=nested, note="Declared nested repository of this workspace project; "
                                  "create its tasks here with scope resources under " + nested
                                  + " (optionally \"repository\": {\"path\": \"" + nested + "\", \"base\": \"origin/master\"})")
            return result
        if args.action == "attach":
            return asdict(with_registry.attach(args.project_id, args.root, expected_revision=args.expected_revision))
        return {"projects": [dict(row) for row in with_registry.db.execute(
            "SELECT p.project_id,p.storage_key,p.kind,p.timezone,p.status,p.revision,w.workspace_id,w.root "
            "FROM projects p JOIN workspaces w ON p.current_workspace_id=w.workspace_id ORDER BY p.created,p.project_id")]}
    finally:
        with_registry.close()


def _credential_command(args):
    selection = _state_project(args)
    config = local_config(selection)
    home = resolve_state_home(args.home, project=selection)
    repo_path = selection if Path(selection).expanduser().is_dir() else args.repo
    repo = config["root"] if config else Path(repo_path).expanduser().resolve()
    if args.project and not Path(args.project).expanduser().is_dir():
        registry = ProjectRegistry(home, project=selection)
        try:
            repo = registry.resolve(args.project).root
        finally:
            registry.close()
    # Do not erase a supplied external alias before the credential path check.
    supplied = args.home if args.home is not None else (os.environ.get("ASE_HOME") if config is None else None)
    if supplied is not None:
        _safe_path(Path(supplied).expanduser())
    valid_local = validate_local_state(home, project_root=repo)
    if home.is_relative_to(repo) and not valid_local:
        raise CredentialError("unsafe_path")
    vault = CredentialVault(home)
    if args.action == "import":
        source = _safe_path(args.file)
        if source.is_relative_to(repo):
            raise CredentialError("unsafe_path")
        return {"credentials": vault.import_file(args.file)}
    if args.action == "list":
        return {"credentials": vault.list()}
    config = _json(_read(_safe_path(args.file)))
    return vault.configure(args.ref, args.target, config)


def _contract_version(path):
    contract = load(path)
    if not isinstance(contract, dict):
        raise ValueError("Task contract must be a JSON object")
    return contract.get("schema_version")


def main(argv=None):
    store = None
    args = None
    try:
        args = parser().parse_args(argv)
        cmd = args.command
        if cmd == "ci-check":
            from . import ci_check
            result = ci_check.run(Path(args.repo), args.base, args.head,
                max_lines=args.max_lines or ci_check.DEFAULT_MAX_LINES, max_files=args.max_files or ci_check.DEFAULT_MAX_FILES,
                allow_protected=args.allow_protected, allow_large=args.allow_large, allow_test_removal=args.allow_test_removal,
                allow_test_skip=args.allow_test_skip)
            if args.text:
                print(ci_check.summary_text(result))
                return 0 if result["status"] == "PASS" else 2
        elif cmd == "credentials":
            result = _credential_command(args)
        elif cmd == "project" and args.action != "export":
            result = _project_command(args)
        else:
            if args.project and local_config(args.project) is not None:
                store = Store(args.project, args.home)
            elif args.project:
                registry = ProjectRegistry(args.home, project=_state_project(args))
                try:
                    selected = registry.resolve(args.project)
                finally:
                    registry.close()
                store = Store(selected)
            else:
                store = Store(args.repo, args.home)
            if cmd == "prompt":
                from . import prompts
                if args.action == "capture":
                    # Hook stdout would be injected into the agent context, so capture stays silent.
                    prompts.capture(store, json.loads(sys.stdin.buffer.read().decode("utf-8-sig")))
                    return 0
                result = {"prompts": prompts.recent(store, max(1, min(args.limit, 200)))}
            elif cmd == "task":
                result = _task_command(store, args.action, args)
            elif cmd == 'lead-review':
                from .lead_review import LeadReview
                service = LeadReview(store)
                if args.action == 'status':
                    result = {'record': service.snapshot(args.task_id), 'policy': service.gate(args.task_id)}
                elif args.action == 'recover':
                    result = service.recover(args.task_id)
                elif args.action == 'prepare':
                    result = service.prepare(args.task_id, thread_id=args.thread_id, expected_revision=args.expected_revision)
                elif args.action == 'classify':
                    result = service.classify(args.task_id, load(args.file), thread_id=args.thread_id, expected_revision=args.expected_revision)
                else:
                    result = service.record(args.task_id, args.attempt, load(args.file), thread_id=args.thread_id, expected_revision=args.expected_revision)
                    result = {**result, 'next_action': service.gate(args.task_id)['next_action']}
            elif cmd == 'delegation':
                result = _delegation_command(store, args)
            elif cmd == 'events':
                result = _events_command(store, args)
            elif cmd == 'adequacy':
                from .testing.adequacy import TestAdequacy
                service = TestAdequacy(store)
                result = (service.snapshot(args.task_id) if args.action == 'status' else
                    service.plan(args.task_id, load(args.file), expected_revision=args.expected_revision))
            elif cmd == "access":
                controller = WorkflowController(store)
                access = {"credential_ref": args.credential, "target": args.target}
                if args.action == "ensure":
                    result = controller.ensure_access(args.task_id, args.stage, access)
                else:
                    result = controller.request_access(args.task_id, args.stage, access, path=args.path, method=args.method)
            elif cmd == "init":
                result = {"project_id": store.project_id, "workspace_id": store.workspace_id, "root": str(store.repo),
                          "discovery": discover_capabilities(store.project, scope={"resources": ["."]}, backend_config={})}
            elif cmd == "capabilities":
                payload = load(args.file) if args.file else {"scope": {"resources": ["."]}, "backend_config": {}}
                result = discover_capabilities(store.project, **payload)
            elif cmd == "project":
                result = export_project(store.project, Path(args.destination), include_verbose=args.include_verbose)
            elif cmd in {"run", "resume", "verify"}:
                result = _task_command(store, cmd, args)
            elif cmd in {"authorize-protected", "approve-high-risk"}:
                result = _task_command(store, cmd, args)
            elif cmd == "publish":
                result = Publisher(store).publish(args.task_id, args.execute, args.remote, args.commit_message)
            elif cmd == "authorize":
                result = Publisher(store).authorize(args.remote, args.disable, repository=args.repository)
            elif cmd in {"check", "gate"}:
                result = _task_command(store, cmd, args)
            elif cmd == "status":
                result = ({**TaskService(store).snapshot(args.task_id), "readiness": TaskService(store).readiness(args.task_id, include_progress=True)}
                          if args.task_id else {"project_id": store.project_id, "workspace_id": store.workspace_id,
                          "root": str(store.repo), "tasks": [dict(row) for row in store.db.execute("SELECT id,status,updated FROM tasks ORDER BY updated DESC")]})
            elif cmd == "context":
                result = {"project_id": store.project_id, "repository": str(store.repo), "journal": Journal(store).context(),
                          "tasks": [dict(row) for row in store.db.execute("SELECT id,request,status,updated FROM tasks ORDER BY updated DESC LIMIT 20")],
                          "decisions": [dict(row) for row in store.db.execute("SELECT * FROM decisions ORDER BY id DESC LIMIT 20")],
                          "sessions": [dict(row) for row in store.db.execute("SELECT * FROM sessions ORDER BY id DESC LIMIT 10")]}
            elif cmd == "backup": result = {"backup": store.backup()}
            elif cmd == "continuity-snapshot": result = Continuity(store).snapshot(args.task_id)
            elif cmd == "checkpoint": result = Continuity(store).checkpoint(args.task_id, args.expected_revision, load(args.file))
            elif cmd == "restore": result = Continuity(store).restore(args.task_id)
            elif cmd == "source-record": result = Continuity(store).source_record(args.task_id, load(args.file), args.expected_sequence)
            elif cmd == "coverage": result = Continuity(store).coverage(args.task_id)
            elif cmd == "knowledge-record": result = Knowledge(store).record(load(args.file))
            elif cmd == "knowledge-query": result = Knowledge(store).query(args.topic)
            elif cmd == "knowledge-lint": result = Knowledge(store).lint()
            elif cmd == "knowledge":
                payload = load(args.file)
                if args.action == "retrieve":
                    result = Knowledge(store).retrieve(**payload)
                else:
                    result = Knowledge(store).record_observations(**payload, expected_revision=args.expected_revision)
            elif cmd == "session":
                if args.session_id and args.session_option:
                    raise ValueError("Supply the session identifier once")
                args.session_id = args.session_id or args.session_option
                if not args.session_id:
                    raise ValueError("Session identifier is required")
                journal = Journal(store)
                result = (journal.start_session(args.task_id, args.session_id) if args.action == "start" else
                          journal.finish_session(args.task_id, args.session_id, load(args.file), closure_key=args.closure_key,
                              expected_revision=args.expected_revision, expected_context_revision=args.expected_context_revision))
            elif cmd in {"worklog", "meeting", "meeting-report"}:
                journal = Journal(store)
                result = (journal.worklog(args.start, args.until, timezone=args.timezone or store.project.timezone) if cmd == "worklog" else
                          journal.meeting_report(args.start, args.until, timezone=args.timezone or store.project.timezone, as_of_end=args.as_of_end))
            elif cmd == "start" and _contract_version(args.contract) == 2:
                result = TaskService(store).create(_new_contract(args.contract))
            elif cmd == "report" and not TaskService(store).snapshot(args.task_id)["legacy_v1"]:
                from .delegations import DelegationRegistry
                from .coordinator import CoordinatorEvents
                result = {"task": TaskService(store).snapshot(args.task_id), "quality_gate": GateService(store).gate(args.task_id),
                          "project_id": store.project_id, "workspace_id": store.workspace_id,
                          "delegations": DelegationRegistry(store).snapshot(args.task_id),
                          "coordinator_events": CoordinatorEvents(store).snapshot(args.task_id)}
                if args.markdown:
                    from .report_md import render
                    from .usage import task_usage
                    print(render({**result, "usage": task_usage(store, args.task_id)}), end="")
                    status = result["quality_gate"].get("status")
                    return 2 if status == "REJECTED" else 3 if status == "BLOCKED" else 0
            elif cmd == "start":
                version = _contract_version(args.contract)
                raise ValueError(("v1 task contracts are no longer accepted; the v1 engine was retired. " if version is None else
                                  "Unsupported contract schema_version " + json.dumps(version) + ". ")
                                 + "Use a v2 contract (schema_version 2, see templates/task.json).")
            else:
                # Time, decisions, session summaries and read-only v1 history.
                records = LocalRecords(store)
                if cmd == "report": result = records.legacy_report(args.task_id)
                elif cmd == "daily": result = records.daily(args.date)
                elif cmd == "weekly": result = records.weekly(args.date)
                elif cmd == "correct-time": result = records.correct_time(args.entry_id, args.activity, args.start, args.end, args.source, args.confirmed, args.reason)
                elif cmd == "session-summary": result = records.session_summary(args.task_id, args.summary, args.decision, args.unresolved)
                elif cmd == "decision": result = records.decision(args.context, args.decision, args.rationale, args.task_id)
                elif cmd == "time": result = records.record_time(args.task_id, args.activity, args.start, args.end, args.source, args.confirmed)
                else: raise ValueError("Unsupported command: " + cmd)
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        status = result.get("status") or result.get("quality_gate", {}).get("status")
        # verify reports its substantive verdict in JSON; successful execution is
        # distinct from gate approval. `gate` supplies the required CI exit code.
        if cmd == "verify" and status != "BLOCKED":
            return 0
        return 2 if status in {"REJECTED", "FAIL", "NOT_READY", "REVIEW"} else 3 if status == "BLOCKED" else 0
    except UsageError as ex:
        raw_args = sys.argv[1:] if argv is None else argv
        # Argument values may be secrets for these commands; the usage line holds none.
        sensitive = any(x in {"credentials", "access"} for x in raw_args)
        print(json.dumps({"status": "USAGE_ERROR", "error": "invalid arguments" if sensitive else redact(ex),
                          "usage": ex.usage}, ensure_ascii=False))
        return 2
    except (ValueError, OSError, KeyError, TypeError, sqlite3.Error, subprocess.SubprocessError) as ex:
        raw_args = sys.argv[1:] if argv is None else argv
        sensitive_command = (args is not None and args.command in {"credentials", "access"}) or (args is None and any(x in {"credentials", "access"} for x in raw_args))
        error = (ex.code if isinstance(ex, CredentialError) else "command_failed") if sensitive_command else redact(ex)
        print(json.dumps({"status": "BLOCKED", "error": error}, ensure_ascii=False))
        return 3
    finally:
        if store:
            store.close()


if __name__ == "__main__":
    sys.exit(main())
