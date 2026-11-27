"""Versioned task intent, explicit approval, and bounded execution plans."""
import copy
import json
import uuid

from .state import append_event, encode, hash_value, identifier, read_record, text, transaction, write_record
from .store import now
from .profiles import stage_profile, validate_access, validate_evidence_spec, validate_mixed_mapping, stage_evidence_specs, has_software, validate_scope_effects
from .projects import _safe_state


STATES = {"DRAFT", "RESEARCHING", "AWAITING_APPROVAL", "READY", "EXECUTING", "VALIDATING",
          "REVIEWING", "DIAGNOSING", "BLOCKED", "PAUSED", "COMPLETED", "CANCELLED"}
PROFILES = {"software", "infrastructure", "document", "research", "mixed"}
# Optional design-review fields: scope fence, preserved behaviour, rejected options, way back.
DESIGN_FIELDS = ("non_goals", "unchanged_behavior", "alternatives", "rollback")


BASELINE_REPOSITORY_CHANGED = ("The task's repository/base changed after its source baseline was recorded and no base ref "
                               "is declared to recompute it from; create a new task or set a base ref "
                               "(\"repository\": {\"path\": ..., \"base\": ...})")


def baseline_repository(baseline):
    """The repository and base ref a recorded baseline was taken for (older records carry the same keys)."""
    return {"path": baseline.get("repository"), "base": baseline.get("base_ref")}


def contract_repository(store, contract):
    if not has_software(contract):
        return {"path": None, "base": None}
    from .repositories import task_repository
    repository = task_repository(store.repo, contract)
    return {"path": repository["path"], "base": repository["base"]}


def matching_approval_baseline(store, task_id, contract):
    baseline = read_record(store.db, 'approval_baseline', task_id)['payload']
    if baseline and baseline_repository(baseline) == contract_repository(store, contract):
        return baseline
    return None


def record_baseline(store, task_id, contract, *, kind="baseline"):
    """Record a source baseline once per repository/base; the first run promotes the one taken at approval.

    A baseline taken for another repository or base ref (the contract was revised) is replaced, never
    kept: from the new base commit, so edits made after the first approval stay visible as changes.
    Without a base ref there is no trustworthy before state, so the replacement is refused.
    """
    wanted = contract_repository(store, contract)
    existing = read_record(store.db, kind, task_id)["payload"]
    if existing and baseline_repository(existing) == wanted:
        return None
    if existing and not wanted["base"]:
        raise ValueError(BASELINE_REPOSITORY_CHANGED)
    baseline = read_record(store.db, "approval_baseline", task_id)["payload"] if kind == "baseline" else None
    if baseline is not None and baseline_repository(baseline) != wanted:
        if not wanted["base"]:
            raise ValueError(BASELINE_REPOSITORY_CHANGED)
        baseline = None
    if baseline is None:
        from .gates import source_snapshot
        from .repositories import base_baseline, repository_path
        baseline = source_snapshot(store, contract, task_id=task_id)
        if has_software(contract):
            from .inspection import SOURCE_STATUS, safe_git, source_status
            from .project_local import ADDITION_GUARDS, enabled_addition_guards
            if wanted["base"]:
                # The task's own branch may already hold committed work: its base ref is the before state.
                baseline = {**baseline, **base_baseline(store.repo, wanted["path"], wanted["base"], baseline["files"])}
            baseline["dirty"] = source_status(safe_git(repository_path(store.repo, wanted["path"]), "status", *SOURCE_STATUS))
            enabled = enabled_addition_guards(store.repo)
            for guard in ADDITION_GUARDS:
                if guard in enabled or (existing or {}).get(guard):
                    baseline[guard] = True
    with transaction(store.db) as db:
        old = read_record(db, kind, task_id)
        if old["payload"] and baseline_repository(old["payload"]) == wanted:
            return None
        if old["payload"]:
            replaced = {"kind": kind, "previous": baseline_repository(old["payload"]),
                        "previous_fingerprint": old["payload"].get("fingerprint"), "current": wanted,
                        "current_fingerprint": baseline.get("fingerprint"), "recomputed_from": baseline.get("commit"),
                        "reason": "repository/base changed by a contract revision"}
            append_event(db, task_id + ":baseline_replaced:" + kind + ":" + str(old["revision"] + 1),
                         "baseline_replaced", task_id, replaced)
        write_record(db, kind, task_id, baseline, expected_revision=old["revision"])
    return baseline


def _validate_design_fields(proposal):
    for name in ("non_goals", "unchanged_behavior"):
        value = proposal.get(name, [])
        if not isinstance(value, list) or len(value) > 50 or not all(isinstance(v, str) and v.strip() for v in value):
            raise ValueError("Proposal " + name + " must be a list of non-empty strings")
    alternatives = proposal.get("alternatives", [])
    if (not isinstance(alternatives, list) or len(alternatives) > 10 or not all(
            isinstance(row, dict) and set(row) <= {"option", "why_not", "cost"} and isinstance(row.get("option"), str)
            and row["option"].strip() and isinstance(row.get("why_not"), str) and row["why_not"].strip()
            and isinstance(row.get("cost", ""), str) for row in alternatives)):
        raise ValueError("Proposal alternatives need option and why_not strings")
    if "rollback" in proposal and (not isinstance(proposal["rollback"], str) or not proposal["rollback"].strip()):
        raise ValueError("Proposal rollback must be a non-empty string")


def approval_hint(task):
    """The exact command that records the owner's approval of the implementation plan."""
    if not task.get('proposal_hash'):
        return 'save the proposal first (`ase task propose`), then record its approval'
    stages = ' '.join('--stage ' + s for s in (task.get('proposal') or {}).get('stage_ids', []))
    return ('show the owner the implementation plan, then record their reply: `ase task approve ' + task['task_id']
            + ' --proposal-hash ' + task['proposal_hash'] + ' ' + stages + ' --user-message-ref <owner reply>` '
            '(or cite the message in which the owner pre-approved the recommended plan, with --owner-preapproved); '
            'for work that changes no code, tests, data, configuration or infrastructure the original request suffices')


DIAGNOSTIC_APPROVAL_ACTION = ("Use the verified diagnostic to explain the scope change to the owner. "
                              "Record a revised proposal and the decision before resuming affected stages.")


def proposal_sequence(db, task_id):
    row = db.execute("SELECT MAX(sequence) FROM runtime_events WHERE task_id=? AND kind='proposal'", (task_id,)).fetchone()
    return row[0]


def decision_stages(approval):
    """Stages granted by this owner reply, excluding grants carried forward."""
    carried = approval.get("earlier_stage_grants", {})
    return [stage_id for stage_id in approval.get("approved_stage_ids", []) if stage_id not in carried]


def approval_for_stage(db, task_id, stage_id, *, current=None):
    """Return the approval record for the reply that granted this stage."""
    if current is None:
        current = read_record(db, "approval", task_id)
    approval = current["payload"] or {}
    revision = approval.get("earlier_stage_grants", {}).get(stage_id)
    if revision is None:
        return current
    original = read_record(db, "approval_version", f"{task_id}:{revision}")
    grant = original["payload"] or {}
    if (original["revision"] != 1 or stage_id not in decision_stages(grant)
            or any(grant[key] != approval[key] for key in ("contract_hash", "proposal_hash"))):
        raise ValueError("Original stage approval is unavailable")
    return {**original, "revision": revision}


def current_approval_decisions(db, task):
    current = read_record(db, "approval", task["task_id"])
    approval = current["payload"]
    if not approval or any(approval[key] != task[key] for key in ("contract_hash", "proposal_hash")):
        return []
    decisions = {}
    for stage_id in approval["approved_stage_ids"]:
        record = approval_for_stage(db, task["task_id"], stage_id, current=current)
        decisions[record["revision"]] = record["payload"]
    return [decisions[revision] for revision in sorted(decisions)]


def diagnostic_approval_pending(progress, task, sequence):
    return (progress.get("phase") == "NEEDS_APPROVAL" and progress.get("diagnostic_outcome") == "APPROVAL_REQUIRED"
            and progress.get("contract_hash") == task["contract_hash"] and progress.get("proposal_hash") == task["proposal_hash"]
            and "diagnostic_proposal_sequence" in progress and progress["diagnostic_proposal_sequence"] == sequence)


def current_candidate_blocker(progress, task, sequence):
    if (progress.get('phase') == 'CANDIDATE_BLOCKED' and progress.get('candidate_proposal_sequence') == sequence
            and all(progress.get(key) == task[key] for key in ('contract_hash', 'proposal_hash'))):
        return progress['candidate_blocker']
    return None


def run_command(store, task):
    """The command that continues approved work: resume after a stop or a blocked run, else run."""
    resume = task['state'] in {'BLOCKED', 'PAUSED'} or read_record(store.db, 'stop', task['task_id'])['payload']
    return '`ase task ' + ('resume' if resume else 'run') + ' ' + task['task_id'] + '`'


def finish_command(task_id):
    return ('`ase task finish ' + task_id + ' --base <target branch, e.g. origin/main> '
            '--body-file <PR body file outside the repository>`')


STOP_CHANGE_LIST_LIMIT = 20


def _baseline_changes(store, task):
    """Read-only list of in-scope paths whose content differs from the recorded task baseline."""
    from pathlib import Path
    from .core import redact
    task_id = task['task_id']
    baseline = (read_record(store.db, 'baseline', task_id)['payload']
                or read_record(store.db, 'approval_baseline', task_id)['payload'])
    if not baseline:
        return 'no source baseline is recorded for this task, so changed files cannot be listed'
    from .gates import source_snapshot
    try:
        current = source_snapshot(store, task['contract'], task_id=task_id)['files']
        root = store.repo.resolve()
        resources = []
        for resource in task['contract']['scope']['resources']:
            path = Path(resource)
            if path.is_absolute():
                if not path.is_relative_to(root):
                    continue
                path = path.relative_to(root)
            resources.append(path)
        changed = sorted(p for p in set(baseline['files']) | set(current) if baseline['files'].get(p) != current.get(p))
        scoped = [p for p in changed if any(Path(p).is_relative_to(r) for r in resources)]
    except Exception as error:
        # A hint never fails its caller, e.g. a git subprocess timeout.
        return 'changed files could not be listed (' + redact(error)[:200] + ')'
    if not scoped:
        listed = 'no file in the task scope differs from the recorded baseline'
    else:
        listed = (f'{len(scoped)} file(s) in the task scope differ from the recorded baseline: '
                  + redact(', '.join(scoped[:STOP_CHANGE_LIST_LIMIT]))[:2500]
                  + (f' (and {len(scoped) - STOP_CHANGE_LIST_LIMIT} more)' if len(scoped) > STOP_CHANGE_LIST_LIMIT else ''))
    outside = len(changed) - len(scoped)
    return listed + (f'; {outside} other changed path(s) lie outside the task scope' if outside else '')


def stop_next_action(store, task, *, list_changes=True):
    """Stop hint shared by status and the paused run closure; list_changes=False does no source work."""
    changes = (' (' + _baseline_changes(store, task) + ')') if list_changes else ''
    return ('A stop was requested: review the saved checkpoint, reconcile any UNKNOWN effects and inspect '
            'files changed since the task baseline (may include earlier runs or other edits); review before reverting'
            + changes + '; only an explicit decision to continue runs `ase task resume ' + task['task_id'] + '`')


def _post_run_next_action(store, task):
    """Next step after execution started, from recorded gate/lead evidence; runs no session."""
    task_id = task['task_id']
    gate = read_record(store.db, 'gate', task_id)['payload'] or {}
    # A committed stop wins over an earlier APPROVED gate, as in workflow._finish_run.
    if read_record(store.db, 'stop', task_id)['payload']:
        return stop_next_action(store, task)
    if task['state'] in {'BLOCKED', 'PAUSED'}:
        return 'Address the blockers the last run reported, then ' + run_command(store, task)
    if gate.get('status') == 'APPROVED' and gate.get('contract_hash') == task['contract_hash']:
        from .coordinator import CoordinatorEvents
        pending = CoordinatorEvents(store).snapshot(task_id)['pending_count']
        return ('The last whole-task gate is APPROVED: ' + finish_command(task_id)
                + (f', then process {pending} pending lead inbox event(s) (listed by `ase task finish`)' if pending else ''))
    if task['state'] in {'EXECUTING', 'VALIDATING', 'DIAGNOSING'}:
        return 'A run is in progress; if its coordinator ended without a closure, `ase task resume ' + task_id + '`'
    if gate.get('status') == 'REJECTED':
        return 'Independent verification rejected the work; repair it with ' + run_command(store, task)
    from .lead_review import LeadReview
    lead = LeadReview(store).gate(task_id)
    if lead['status'] == 'BLOCKED':
        return lead['next_action'] if lead.get('base_current') or lead.get('needs_owner_binding') else run_command(store, task)
    if lead['status'] == 'READY' and lead.get('verification_bound') is False:
        return lead['next_action']
    return '`ase task verify ' + task_id + '`'


def status_next_action(task, discovery_present, store=None):
    """The next command for a task; after execution started only when a store is given, else None."""
    task_id = task['task_id']
    if store is not None and task['state'] in {'AWAITING_APPROVAL', 'BLOCKED'}:
        sequence = proposal_sequence(store.db, task_id)
        for stage_id in task.get('approved_stage_ids', []):
            progress = read_record(store.db, 'cycle', task_id + ':' + stage_id)['payload'] or {}
            blocker = current_candidate_blocker(progress, task, sequence)
            if blocker:
                if read_record(store.db, 'stop', task_id)['payload']:
                    return stop_next_action(store, task)
                return blocker['next_action']
    if task['state'] in {'DRAFT', 'READY'} and not discovery_present:
        return ('`ase task discover ' + task_id + ' --expected-revision 0`, then '
                + ('save the proposal' if task['state'] == 'DRAFT' else '`ase task run ' + task_id + '`'))
    if task['state'] == 'DRAFT':
        return ('save the implementation proposal: `ase task propose ' + task_id
                + ' --file <proposal.json> --expected-revision ' + str(task['revision']) + '`')
    if task['state'] == 'AWAITING_APPROVAL':
        if store is not None:
            sequence = proposal_sequence(store.db, task_id)
            for stage_id in task.get('approved_stage_ids', []):
                progress = read_record(store.db, 'cycle', task_id + ':' + stage_id)['payload'] or {}
                if diagnostic_approval_pending(progress, task, sequence):
                    return DIAGNOSTIC_APPROVAL_ACTION
        return approval_hint(task)
    if task['state'] == 'READY':
        if store is None:
            return '`ase task run ' + task_id + '`'
        if 'lead_review' in task['contract'] and not task['contract'].get('parent_task_id'):
            coordinator = read_record(store.db, 'coordinator', task_id)
            if coordinator['payload'] is None:
                from .lead_review import bind_local_action
                return bind_local_action(task_id, coordinator['revision']) + ' Then ' + run_command(store, task)
        return run_command(store, task)
    if store is None or task['state'] in {'RESEARCHING', 'CANCELLED'} or task.get('legacy_v1'):
        return None
    return _post_run_next_action(store, task)


BACKENDS = {"codex", "claude", "copilot"}
VERIFIER_ROLES = {"ac", "review"}


def backend_for(contract, role=None):
    policy = contract["execution_policy"]
    return policy.get("role_backends", {}).get(role, policy["backend"])


DEFAULT_POLICY = {"recovery_mode": "progress", "stagnation_limit": 3,
                  "max_stage_cycles": None, "max_task_cycles": None, "repeat_failure_limit": 2,
                  "max_diagnostic_passes": None, "diagnostic_timeout_seconds": 1200,
                  "active_time_budget_seconds": None, "backend": "codex"}


def _scope(value):
    if not isinstance(value, dict) or set(value) - {"resources", "effects"}:
        raise ValueError("scope requires resources and effects")
    result = {}
    for key in ("resources", "effects"):
        items = value.get(key, [])
        if not isinstance(items, list) or len(items) > 1000:
            raise ValueError("Invalid scope " + key)
        result[key] = list(dict.fromkeys(text(item, key, limit=2000) for item in items))
    return result


def _new_scope(value):
    """Scope supplied by the caller now; stored scopes are read with _scope and never re-judged."""
    result = _scope(value)
    validate_scope_effects(result["effects"])
    return result


def _within(inner, outer):
    return all(set(inner[k]).issubset(outer[k]) for k in ("resources", "effects"))


def _unique(items, name):
    if not isinstance(items, list) or len(items) > 1000:
        raise ValueError(name + " must be a bounded list")
    ids = [identifier(item.get("id"), name + " id") for item in items if isinstance(item, dict)]
    if len(ids) != len(items) or len(ids) != len(set(ids)):
        raise ValueError(name + " requires unique object IDs")
    return set(ids)


def validate_contract_v2(value):
    allowed = {"id", "schema_version", "profile", "request", "scope", "acceptance", "evidence", "commands",
               "stages", "assumptions", "unknowns", "sources", "capabilities", "execution_policy", "reporting",
               "risk", "protected_paths", "parent_task_id", "parent_stage_id", "test_adequacy", "lead_review", "bugfix_evidence", "context_transport", "review_format", "blind_review", "lane",
               "repository", "check_inputs", "review_packet"}
    if not isinstance(value, dict) or set(value) - allowed:
        raise ValueError("Unknown task field; approval must be recorded separately")
    c = copy.deepcopy(value)
    if c.get("schema_version") != 2 or c.get("profile") not in PROFILES:
        raise ValueError("Task requires schema_version 2 and a supported profile")
    text(c.get("request"), "request")
    c["scope"] = _new_scope(c.get("scope"))
    if "repository" in c:
        from .repositories import validate_field
        c["repository"] = validate_field(c["repository"])
    if 'lane' in c:
        from .quick import validate_lane
        validate_lane({**c, 'risk': c.get('risk', 'MEDIUM')})
    if 'review_format' in c:
        from .review.schema import validate_review_format
        validate_review_format(c['review_format'])
    if 'blind_review' in c:
        from .review.blind import validate as validate_blind_review
        validate_blind_review(c['blind_review'])
    if 'context_transport' in c:
        from .protocol import validate_transport_policy
        validate_transport_policy(c['context_transport'])
    if 'lead_review' in c:
        from .lead_review import validate_policy, PASSES_BY_RISK
        if validate_policy(c['lead_review'])['additional_passes'] < PASSES_BY_RISK.get(c.get('risk') or 'MEDIUM', 3):
            raise ValueError('lead_review additional_passes is below the minimum for the contract risk; '
                             'raise it or omit lead_review on revise so it is recomputed')
    c.setdefault("risk", "MEDIUM")
    if c["risk"] not in {"LOW", "MEDIUM", "HIGH"}:
        raise ValueError("Invalid risk")
    for key in ("sources", "assumptions", "unknowns"):
        c.setdefault(key, [])
        if not isinstance(c[key], list):
            raise ValueError(key + " must be a list")
    unknown_ids = _unique(c["unknowns"], "unknowns")
    ac_ids = _unique(c.get("acceptance"), "acceptance")
    if not ac_ids:
        raise ValueError("Task requires observable acceptance criteria")
    checks = c.get("evidence", c.get("commands", []))
    check_ids = _unique(checks, "evidence")
    if not checks:
        raise ValueError("Task requires evidence specifications")
    c["evidence"] = checks
    for spec in checks:
        validate_evidence_spec(spec, c["profile"])
    for ac in c["acceptance"]:
        text(ac.get("expected"), "expected")
        if type(ac.get("mandatory", True)) is not bool:
            raise ValueError("mandatory must be boolean")
        refs = ac.get("evidence_ids", ac.get("commands", []))
        if not isinstance(refs, list) or not set(refs).issubset(check_ids) or (ac.get("mandatory", True) and not refs):
            raise ValueError("Mandatory AC requires known evidence IDs")
        ac["evidence_ids"] = refs
    stages = c.get("stages")
    stage_ids = _unique(stages, "stages")
    if not stage_ids:
        raise ValueError("Task requires explicit stages")
    for stage in stages:
        if c["profile"] != "mixed" and "profile" in stage and stage["profile"] != c["profile"]:
            raise ValueError("Different domain stages require a mixed parent profile")
        if "required_child_task_id" in stage:
            identifier(stage["required_child_task_id"], "required child task id")
            if stage["required_child_task_id"] == c.get("id"):
                raise ValueError("A task cannot require itself as a child")
        stage["scope"] = _new_scope(stage.get("scope", c["scope"]))
        if not _within(stage["scope"], c["scope"]):
            raise ValueError("Stage scope exceeds task scope")
        for key, known in (("acceptance_ids", ac_ids), ("depends_on", stage_ids), ("unknown_ids", unknown_ids)):
            stage.setdefault(key, [])
            if not isinstance(stage[key], list) or not set(stage[key]).issubset(known):
                raise ValueError("Stage references unknown " + key)
        actions = stage.get("actions", [])
        _unique(actions, "actions")
        if actions and "command" not in stage["scope"]["effects"]:
            raise ValueError("Stage actions require an approved command effect")
        for action in actions:
            if set(action) - {"id", "argv", "observation_argv", "timeout", "access"}:
                raise ValueError("Unknown action fields")
            if "access" in action:
                validate_access(action["access"])
                if action["access"]["target"] not in stage["scope"]["resources"]:
                    raise ValueError("Access target exceeds approved stage resources")
            for key in ("argv", "observation_argv"):
                validate_evidence_spec({"id": action["id"], "kind": "command", "argv": action.get(key), "timeout": action.get("timeout", 300)}, c["profile"])
    for spec in checks:
        if "access" in spec and not any(spec["access"]["target"] in stage["scope"]["resources"]
                and spec["id"] in {item["id"] for item in stage_evidence_specs(c, checks, stage["id"])} for stage in stages):
            raise ValueError("Access target exceeds approved stage resources")
    required_children = [s["required_child_task_id"] for s in stages if "required_child_task_id" in s]
    if len(required_children) != len(set(required_children)):
        raise ValueError("A required child must map to exactly one parent stage")
    validate_mixed_mapping(c)
    if 'check_inputs' in c:
        from .evidence.check_inputs import validate_check_inputs
        validate_check_inputs(c['check_inputs'], check_ids)
    if 'review_packet' in c:
        from .evidence.representation import validate_review_packet
        c['review_packet'] = validate_review_packet(c['review_packet'])
    if 'test_adequacy' in c:
        quality = c['test_adequacy']
        if (not isinstance(quality, dict) or set(quality) != {'version', 'required'}
                or type(quality['version']) is not int or quality['version'] != 1
                or quality['required'] is not True or not has_software(c)):
            raise ValueError('test_adequacy requires version 1, required true and software scope')
    if 'bugfix_evidence' in c:
        from .testing.bugfix import required
        if not required(c) or c.get('test_adequacy')!={'version':1,'required':True}:
            raise ValueError('bugfix_evidence requires software scope and required test adequacy')
        from .testing.bugfix import validate_contract
        validate_contract(c)
    visiting, visited = set(), set()
    by_id = {s["id"]: s for s in stages}

    def visit(stage_id):
        if stage_id in visiting:
            raise ValueError("Stage dependency cycle")
        if stage_id not in visited:
            visiting.add(stage_id)
            for dependency in by_id[stage_id]["depends_on"]:
                visit(dependency)
            visiting.remove(stage_id)
            visited.add(stage_id)
    for stage_id in by_id:
        visit(stage_id)
    for unknown in c["unknowns"]:
        text(unknown.get("statement"), "unknown statement")
        if unknown.get("status", "UNRESOLVED") not in {"UNRESOLVED", "CONFIRMED", "RESOLVED"}:
            raise ValueError("Invalid unknown status")
        if not set(unknown.get("blocking_stage_ids", [])).issubset(stage_ids):
            raise ValueError("Unknown references missing stage")
    policy = dict(DEFAULT_POLICY)
    supplied = c.get("execution_policy", {})
    if not isinstance(supplied, dict) or set(supplied) - set(DEFAULT_POLICY) - {"role_backends"}:
        raise ValueError("Unknown execution_policy field")
    role_backends = supplied.get("role_backends", {})
    # Optional: run the independent sessions on another provider than implementation.
    if (not isinstance(role_backends, dict) or set(role_backends) - VERIFIER_ROLES
            or not all(value in BACKENDS for value in role_backends.values())):
        raise ValueError("role_backends maps ac/review to codex, claude or copilot")
    policy.update(supplied)
    if "recovery_mode" not in supplied and any(supplied.get(key) is not None
            for key in ("max_stage_cycles", "max_task_cycles", "max_diagnostic_passes")):
        policy["recovery_mode"] = "fixed"
    if policy["recovery_mode"] not in {"fixed", "progress"}:
        raise ValueError("Invalid recovery mode")
    if policy["recovery_mode"] == "fixed":
        for key, default in (("max_stage_cycles", 3), ("max_task_cycles", 12), ("max_diagnostic_passes", 1)):
            if key not in supplied:
                policy[key] = default
    for key in ("max_stage_cycles", "max_task_cycles", "repeat_failure_limit", "max_diagnostic_passes", "diagnostic_timeout_seconds", "stagnation_limit"):
        if key in {"max_stage_cycles", "max_task_cycles", "max_diagnostic_passes"} and policy[key] is None and policy["recovery_mode"] == "progress":
            continue
        if type(policy[key]) is not int or not 1 <= policy[key] <= (3600 if key.endswith("seconds") else 100):
            raise ValueError("Invalid execution policy " + key)
    budget = policy["active_time_budget_seconds"]
    if budget is not None and (type(budget) is not int or budget <= 0):
        raise ValueError("Invalid active time budget")
    if policy["backend"] not in BACKENDS:
        raise ValueError("Unknown backend")
    c["execution_policy"] = policy
    c.setdefault("reporting", {"timezone": "local"})
    text(encode(c), "contract", limit=200000)
    return c


def validate_child_obligations(child_contract, parent_contract, stage, child_id):
    """A child inherits one concrete domain and cannot replace required business ACs."""
    if child_contract["profile"] != stage_profile(parent_contract, stage["id"]):
        raise ValueError("Child profile must match its parent stage domain")
    if stage.get("required_child_task_id") != child_id:
        return
    child_acceptance = {ac["id"]: ac for ac in child_contract["acceptance"]}
    for ac in parent_contract["acceptance"]:
        if ac["id"] not in stage["acceptance_ids"] or not ac.get("mandatory", True):
            continue
        mapped = child_acceptance.get(ac["id"])
        if not mapped or mapped["expected"] != ac["expected"] or not mapped.get("mandatory", True):
            raise ValueError("Required child acceptance must retain its parent business obligation: " + ac["id"])


class TaskService:
    def __init__(self, store):
        self.store = store

    def snapshot(self, task_id):
        task = self.store.task(identifier(task_id, "task id"))
        record = read_record(self.store.db, "task", task_id)
        if record["payload"] is None:
            return {"task_id": task_id, "revision": 0, "state": task["status"], "contract": task["contract"],
                    "legacy_v1": True, "contract_hash": hash_value(task["contract"]), "proposal_hash": None}
        return {**record["payload"], "revision": record["revision"], "legacy_v1": False}

    def create(self, contract):
        c = validate_contract_v2(contract)
        self._pin_repository(c)
        task_id = identifier(c.pop("id", None) or uuid.uuid4().hex[:16], "task id")
        parent = None
        if c.get("parent_task_id"):
            parent = self.require_approval(c["parent_task_id"], c.get("parent_stage_id"))
            parent_stage = next(s for s in parent["contract"]["stages"] if s["id"] == c["parent_stage_id"])
            if not _within(c["scope"], parent_stage["scope"]):
                raise ValueError("Child scope exceeds approved parent stage")
            if parent['contract'].get('test_adequacy') and has_software(c):
                c['test_adequacy'] = copy.deepcopy(parent['contract']['test_adequacy'])
            if parent['contract'].get('bugfix_evidence') and has_software(c):
                c['bugfix_evidence'] = copy.deepcopy(parent['contract']['bugfix_evidence'])
            if parent['contract'].get('lead_review'):
                from .lead_review import PASSES_BY_RISK
                c['lead_review'] = copy.deepcopy(parent['contract']['lead_review'])
                c['lead_review']['additional_passes'] = max(c['lead_review']['additional_passes'],
                                                            PASSES_BY_RISK.get(c.get('risk') or 'MEDIUM', 3))
            validate_child_obligations(c, parent["contract"], parent_stage, task_id)
            # Inherited policy can strengthen (and invalidate) a formerly valid
            # child. Reject it before inserting an authorized READY task.
            c = validate_contract_v2(c)
        payload = {"task_id": task_id, "state": "DRAFT", "contract": c, "contract_hash": hash_value(c),
                   "proposal_hash": None, "approved_stage_ids": [], "created": now()}
        if parent:
            payload.update(state="READY", parent_proposal_hash=parent["proposal_hash"],
                           proposal_hash=hash_value({"parent_proposal_hash": parent["proposal_hash"], "contract_hash": hash_value(c)}),
                           approved_stage_ids=[s["id"] for s in c["stages"]], inherited_approval=True)
        with transaction(self.store.db) as db:
            db.execute("INSERT INTO tasks VALUES(?,?,?,?,?,?)", (task_id, c["request"], "DRAFT", encode(c), now(), now()))
            self._save(payload, 0)
            write_record(db, "execution_plan", task_id, {"checks": c["evidence"], "plan_hash": hash_value(c["evidence"])}, expected_revision=0)
            write_record(db, "task_version", task_id + ":1", payload, expected_revision=0)
        if parent and has_software(c):
            # An inherited approval is an approval: native edits after it must stay outside the baseline.
            warning = self._approval_baseline(task_id, c)
            if warning:
                return {**self.snapshot(task_id), "baseline_warning": warning}
        return self.snapshot(task_id)

    def _save(self, payload, expected_revision):
        db = self.store.db
        write_record(db, "task", payload["task_id"], payload, expected_revision=expected_revision)
        db.execute("UPDATE tasks SET status=?,updated=? WHERE id=?", (payload["state"], now(), payload["task_id"]))

    def _repository(self, contract):
        """One task, one repository: a declared nested clone (with its optional base ref) or the root."""
        from .repositories import repository_path, task_repository
        repository = task_repository(self.store.repo, contract)
        path = repository_path(self.store.repo, repository["path"])
        if repository["base"] and has_software(contract):
            from .inspection import safe_git
            if not safe_git(path, "rev-parse", "--verify", "--end-of-options", repository["base"] + "^{commit}", check=False):
                raise ValueError("Task base ref is not a commit in " + (repository["path"] or "the workspace root")
                                 + ": " + repository["base"])
        return repository

    def _pin_repository(self, contract):
        # A later .meta/config edit cannot move an inferred task between repositories: the hash binds it.
        if not has_software(contract):
            return
        repository = self._repository(contract)
        if repository["path"] and "repository" not in contract:
            contract["repository"] = {"path": repository["path"]}

    def revise(self, task_id, contract, *, expected_revision):
        """A changed goal creates a new version and requires a fresh proposal."""
        if not isinstance(contract, dict) or not isinstance(contract.get("execution_policy", {}), dict):
            raise ValueError("Task revision and execution policy must be objects")
        incoming = copy.deepcopy(contract)
        current = self.snapshot(task_id)
        if not current["legacy_v1"]:
            policy = copy.deepcopy(current["contract"]["execution_policy"])
            policy.update(incoming.get("execution_policy", {}))
            if "recovery_mode" not in policy:
                policy["recovery_mode"] = "fixed"
            incoming["execution_policy"] = policy
            if current['contract'].get('test_adequacy'):
                incoming.setdefault('test_adequacy', copy.deepcopy(current['contract']['test_adequacy']))
            if current['contract'].get('bugfix_evidence'):
                incoming.setdefault('bugfix_evidence', copy.deepcopy(current['contract']['bugfix_evidence']))
            if current['contract'].get('lead_review') and 'lead_review' not in incoming:
                # A risk raise also raises review depth; it never lowers an existing depth.
                from .lead_review import policy_for_risk
                carried = copy.deepcopy(current['contract']['lead_review'])
                carried['additional_passes'] = max(carried['additional_passes'],
                    policy_for_risk(incoming.get('risk', current['contract'].get('risk')))['additional_passes'])
                incoming['lead_review'] = carried
            if current['contract'].get('context_transport'):
                incoming.setdefault('context_transport', copy.deepcopy(current['contract']['context_transport']))
            if current['contract'].get('review_packet'):
                incoming.setdefault('review_packet', copy.deepcopy(current['contract']['review_packet']))
            # Required test adequacy is how a quick task moves to the full path; otherwise the lane is kept.
            if (incoming.get('test_adequacy') or {}).get('required') is True:
                incoming.pop('lane', None)
            elif current['contract'].get('lane'):
                incoming.setdefault('lane', current['contract']['lane'])
            for field in ('review_format', 'blind_review'):
                if current['contract'].get(field):
                    incoming.setdefault(field, copy.deepcopy(current['contract'][field]))
        c = validate_contract_v2(incoming)
        self._pin_repository(c)
        if not current["legacy_v1"] and has_software(c):
            recorded = (read_record(self.store.db, "baseline", task_id)["payload"]
                        or read_record(self.store.db, "approval_baseline", task_id)["payload"])
            wanted = contract_repository(self.store, c)
            if recorded and baseline_repository(recorded) != wanted and not wanted["base"]:
                raise ValueError(BASELINE_REPOSITORY_CHANGED)
        supplied_id = c.pop("id", task_id)
        if supplied_id != task_id:
            raise ValueError("Revision cannot change task identity")
        with transaction(self.store.db) as db:
            task = self.snapshot(task_id)
            if task["state"] in {"COMPLETED", "CANCELLED"}:
                raise ValueError("Terminal task needs a new task")
            if type(expected_revision) is not int or expected_revision < 0:
                raise ValueError("expected_revision must be a nonnegative integer")
            if task["revision"] != expected_revision:
                raise ValueError("Stale task revision; reload and reconcile")
            if not task["legacy_v1"] and hash_value(c) == task["contract_hash"]:
                return task
            if task['contract'].get('lead_review') or c.get('lead_review'):
                from .lead_review import archive_approved_intent
                archive_approved_intent(self.store, task)
            legacy_budget = task.get("legacy_budget")
            if task["legacy_v1"]:
                maximum = task["contract"].get("max_attempts", 2)
                if type(maximum) is not int or not 1 <= maximum <= 5:
                    raise ValueError("Legacy attempt ceiling is invalid; inspect the original task")
                progress_path = self.store.root / "tasks" / task_id / "progress.json"
                _safe_state(progress_path)
                audited = db.execute("SELECT COUNT(*) FROM transitions WHERE task_id=? AND status='IMPLEMENTING'", (task_id,)).fetchone()[0]
                attempts = audited
                if progress_path.exists():
                    if not progress_path.is_file() or progress_path.stat().st_size > 32000:
                        raise ValueError("Invalid legacy attempt history")
                    try:
                        progress = json.loads(progress_path.read_text(encoding="utf-8-sig"))
                    except (ValueError, UnicodeError):
                        raise ValueError("Malformed legacy attempt history") from None
                    spent = progress.get("attempts") if isinstance(progress, dict) else None
                    if type(spent) is not int or not 0 <= spent <= maximum:
                        raise ValueError("Invalid legacy attempt history; refusing to reset the budget")
                    attempts = max(audited, spent)
                elif task["state"] in {"IMPLEMENTING", "ESCALATED", "RECOVERED"} or (progress_path.parent.exists() and any(progress_path.parent.glob("implementation-*"))):
                    raise ValueError("Legacy attempt history is missing; restore it before continuation")
                if attempts > maximum:
                    raise ValueError("Legacy attempt history exceeds its ceiling")
                legacy_budget = {"max_attempts": maximum, "attempts_at_migration": attempts}
                write_record(db, "task_version", task_id + ":legacy", task, expected_revision=0)
                current_budget = read_record(db, "budget", task_id)
                budget = current_budget["payload"] or {"task_cycles": 0, "stages": {}, "diagnostics": {}, "failures": {}}
                budget["task_cycles"] = max(budget["task_cycles"], attempts)
                key = task_id + ":" + c["stages"][0]["id"]
                budget["stages"][key] = max(budget["stages"].get(key, 0), attempts)
                write_record(db, "budget", task_id, budget, expected_revision=current_budget["revision"])
            if legacy_budget:
                for key in ("max_stage_cycles", "max_task_cycles"):
                    ceiling = c["execution_policy"][key]
                    c["execution_policy"][key] = min(ceiling, legacy_budget["max_attempts"]) if ceiling is not None else legacy_budget["max_attempts"]
                c["execution_policy"]["recovery_mode"] = "fixed"
                if c["execution_policy"]["max_diagnostic_passes"] is None:
                    c["execution_policy"]["max_diagnostic_passes"] = 1
            if c.get("parent_task_id") != task["contract"].get("parent_task_id") or c.get("parent_stage_id") != task["contract"].get("parent_stage_id"):
                raise ValueError("Revision cannot change parent authority")
            if c.get("parent_task_id"):
                parent = self.require_approval(c["parent_task_id"], c["parent_stage_id"])
                stage = next(s for s in parent["contract"]["stages"] if s["id"] == c["parent_stage_id"])
                if not _within(c["scope"], stage["scope"]):
                    raise ValueError("Child scope exceeds approved parent")
                validate_child_obligations(c, parent["contract"], stage, task_id)
            payload = {k: v for k, v in task.items() if k not in {"revision", "legacy_v1", "proposal"}}
            payload.update(contract=c, contract_hash=hash_value(c), proposal_hash=None, approved_stage_ids=[], state="DRAFT")
            if legacy_budget:
                payload["legacy_budget"] = legacy_budget
            if c.get("parent_task_id"):
                payload["parent_proposal_hash"] = parent["proposal_hash"]
            self._save(payload, expected_revision)
            db.execute("UPDATE tasks SET contract=?,request=? WHERE id=?", (encode(c), c["request"], task_id))
            old = read_record(db, "execution_plan", task_id)
            write_record(db, "execution_plan", task_id, {"checks": c["evidence"], "plan_hash": hash_value(c["evidence"])}, expected_revision=old["revision"])
            write_record(db, "task_version", task_id + ":" + str(expected_revision + 1), payload, expected_revision=0)
        return self.snapshot(task_id)

    def readiness(self, task_id, stage_id=None, *, include_progress=False):
        # include_progress (status only): the post-run hint reads the lead gate, which itself calls readiness.
        from .source_coverage import source_availability, availability_gaps, read_persisted_intake
        task = self.snapshot(task_id)
        if task["legacy_v1"]:
            if stage_id is not None:
                raise ValueError("legacy_v1 has no v2 readiness stage; revise the task contract first")
            present = self.store.db.execute("SELECT 1 FROM task_discoveries WHERE task_id=? LIMIT 1", (task_id,)).fetchone()
            return {"legacy_v1": True, "discovery_present": bool(present), "required_source_gaps": [],
                    "source_availability": [], "intake_validation": {"status": "LEGACY_UNASSESSED", "source_statuses": []},
                    "untracked_gap_ids": [], "clarity": "UNASSESSED", "semantic_validation": False, "execution_authorized": False,
                    "limitations": ["Legacy history requires a reviewed v2 task contract before new execution"]}
        row = self.store.db.execute("SELECT payload FROM task_discoveries WHERE task_id=? ORDER BY revision DESC LIMIT 1", (task_id,)).fetchone()
        discovery = json.loads(row[0]) if row else None
        intake_state = read_persisted_intake(discovery)
        intake = intake_state["intake"]
        gaps = [g for g in [*intake.get("gaps", []), *intake.get("contradictions", [])] if g.get("required", True) and g.get("status", "UNRESOLVED").upper() not in {"RESOLVED", "CONFIRMED"}]
        availability = source_availability(self.store, task["contract"], discovery, stage_id)
        gaps.extend(availability_gaps(availability))
        known = {u["id"] for u in task["contract"].get("unknowns", [])}
        if stage_id is not None:
            stages = task["contract"]["stages"]
            if stage_id not in {stage["id"] for stage in stages}:
                raise ValueError("Unknown readiness stage")
            relevant = []
            for gap in gaps:
                affected = gap.get("blocking_stage_ids")
                if affected is None:
                    unknown = next((u for u in task["contract"].get("unknowns", []) if u["id"] == gap["id"]), None)
                    if unknown and unknown.get("status", "UNRESOLVED") == "UNRESOLVED":
                        affected = set(unknown.get("blocking_stage_ids", [])) | {
                            stage["id"] for stage in stages if gap["id"] in stage.get("unknown_ids", [])}
                if not affected or not set(affected).issubset({stage["id"] for stage in stages}):
                    relevant.append(gap)
                    continue
                affected = set(affected)
                while True:
                    expanded = affected | {stage["id"] for stage in stages if set(stage.get("depends_on", [])) & affected}
                    if expanded == affected:
                        break
                    affected = expanded
                if stage_id in affected:
                    relevant.append(gap)
            gaps = relevant
        gaps.extend(intake_state["compatibility_gaps"])
        return {"discovery_present": bool(row), "required_source_gaps": gaps, "source_availability": availability, "intake_validation": intake_state["validation"],
                "untracked_gap_ids": [g["id"] for g in gaps if g["id"] not in known],
                "clarity": "BLOCKED" if gaps else "SCOPED" if row else "UNASSESSED",
                "semantic_validation": False,
                **({"next_action": status_next_action(task, bool(row), self.store if include_progress else None)}
                   if stage_id is None else {})}

    def propose(self, task_id, proposal, *, expected_revision):
        if not isinstance(proposal, dict) or set(proposal) - {"summary", "stage_ids", "business", "technical", "steps", "verification",
                "limitations", "assumptions", "effects", "task_id", *DESIGN_FIELDS}:
            raise ValueError("Invalid proposal fields")
        if proposal.get("task_id", task_id) != task_id:
            raise ValueError("Proposal belongs to another task")
        text(proposal.get("summary"), "proposal summary")
        _validate_design_fields(proposal)
        from .lead_review import validate_intent_text
        validate_intent_text(proposal, "proposal", limit=200000)
        with transaction(self.store.db) as db:
            task = self.snapshot(task_id)
            validate_mixed_mapping(task["contract"])
            if task["legacy_v1"]:
                raise ValueError("A legacy task needs a reviewed v2 contract before new approval")
            if task["state"] in {"COMPLETED", "CANCELLED"}:
                raise ValueError("Terminal task needs a new task")
            known = {s["id"] for s in task["contract"]["stages"]}
            stages = proposal.get("stage_ids", [])
            if (not isinstance(stages, list) or not stages or not all(isinstance(s, str) for s in stages)
                    or not set(stages).issubset(known)):
                raise ValueError("Proposal requires known stage IDs")
            self._validate_effects(task["contract"], proposal, scope_root=self.store.repo)
            # The task id inside the hashed proposal keeps one task's approval hash from matching another's.
            bound = {**copy.deepcopy(proposal), "task_id": task_id}
            proposal_hash = hash_value({"contract_hash": task["contract_hash"], "proposal": bound})
            if type(expected_revision) is not int or expected_revision < 0:
                raise ValueError("expected_revision must be a nonnegative integer")
            if task["revision"] != expected_revision:
                raise ValueError("Stale task revision; reload and reconcile")
            if proposal_hash == task["proposal_hash"]:
                return task
            if task['contract'].get('lead_review'):
                from .lead_review import archive_approved_intent
                archive_approved_intent(self.store, task)
            payload = {k: v for k, v in task.items() if k not in {"revision", "legacy_v1"}}
            payload.update(state="AWAITING_APPROVAL", proposal=bound, proposal_hash=proposal_hash, approved_stage_ids=[])
            self._save(payload, expected_revision)
            previous = read_record(db, "approval", task_id)
            write_record(db, "approval", task_id, None, expected_revision=previous["revision"])
            append_event(db, task_id + ":proposal:" + str(expected_revision + 1), "proposal", task_id,
                         {"proposal_hash": payload["proposal_hash"], "stage_ids": stages})
        return self.snapshot(task_id)

    def approve(self, task_id, *, proposal_hash, user_message_ref, approved_stage_ids, owner_preapproved=False):
        text(user_message_ref, "approval user_message_ref", limit=2000)
        if type(owner_preapproved) is not bool:
            raise ValueError("owner_preapproved must be boolean")
        with transaction(self.store.db) as db:
            task = self.snapshot(task_id)
            validate_mixed_mapping(task["contract"])
            if task["state"] in {"COMPLETED", "CANCELLED"}:
                raise ValueError("Terminal task needs a new task")
            if task["legacy_v1"] or not task["proposal_hash"] or proposal_hash != task["proposal_hash"]:
                raise ValueError("Approval hash differs from current proposal hash")
            if (not isinstance(approved_stage_ids, list) or not approved_stage_ids
                    or not all(isinstance(s, str) for s in approved_stage_ids)
                    or not set(approved_stage_ids).issubset(task["proposal"]["stage_ids"])):
                raise ValueError("Approval stage scope differs from proposal: pass "
                                 + " ".join("--stage " + s for s in task["proposal"]["stage_ids"])
                                 + " (the proposal's stage_ids; approve a subset with fewer --stage flags)")
            stages = {s["id"]: s for s in task["contract"]["stages"]}
            if not set(approved_stage_ids).issubset(stages):
                raise ValueError("Approval stage scope differs from contract")
            if task["contract"].get("lane") == "quick" and not owner_preapproved:
                # The request message is not an approval of a plan the owner has not seen yet, unless the
                # owner said in it to follow the recommended plan without asking (owner_preapproved).
                request = next((s for s in task["contract"].get("sources", [])
                                if isinstance(s, dict) and s.get("id") == "REQUEST"), {})
                request_ref = str(request.get("locator", "")).split(" (owner wrote", 1)[0].strip()
                given = user_message_ref.strip()
                if request_ref and given.casefold() == request_ref.casefold():
                    raise ValueError("Show the owner the implementation plan first; approve with their reply, not the request message")
                if request_ref.startswith("prompt:"):
                    from .prompts import resolve
                    reply = resolve(self.store, given) if given.startswith("prompt:") else None
                    request_prompt = resolve(self.store, request_ref)
                    if not reply or reply["captured"] <= request_prompt["captured"]:
                        raise ValueError("The request is a captured prompt; approve with the owner's captured reply (prompt:<id>) given after it")
            from .prompts import resolve
            cited = resolve(self.store, user_message_ref.strip())
            if cited is not None and owner_preapproved:
                # A pre-approval is the request itself or a later message, never an older one.
                request = next((s for s in task["contract"].get("sources", [])
                                if isinstance(s, dict) and s.get("id") == "REQUEST"), {})
                request_ref = str(request.get("locator", "")).split(" (owner wrote", 1)[0].strip()
                request_prompt = resolve(self.store, request_ref) if request_ref.startswith("prompt:") else None
                if request_prompt and cited["captured"] < request_prompt["captured"]:
                    raise ValueError("The cited pre-approval prompt predates the task request; cite the request or a later owner prompt")
            proposed = db.execute("SELECT created,sequence FROM runtime_events WHERE task_id=? AND kind='proposal' ORDER BY sequence DESC LIMIT 1",
                                  (task_id,)).fetchone()
            approval_record = read_record(db, "approval", task_id)
            current = approval_record["payload"] or {}
            if current.get("proposal_hash") != proposal_hash or current.get("contract_hash") != task["contract_hash"]:
                current = {}
            stage_ids = set(approved_stage_ids)
            active_stages = set(current.get("approved_stage_ids", []))
            same_decision = False
            # An A -> B -> A replay restores the hash of A, but it is a new proposal.
            # A reference already spent on an older proposal or a narrower stage grant
            # cannot be reused as a fresh decision, including through owner_preapproved.
            earlier = db.execute("SELECT sequence,payload FROM runtime_events WHERE kind='approval' AND task_id=?",
                                 (task_id,)).fetchall()
            for sequence, raw in earlier:
                previous = json.loads(raw)
                if previous.get("user_message_ref", "").strip() != user_message_ref.strip():
                    continue
                if (previous.get("proposal_hash") != proposal_hash
                        or (proposed and sequence <= proposed[1])
                        or not stage_ids.issubset(decision_stages(previous))):
                    raise ValueError("This owner decision was already used for a different proposal or stage grant")
                if previous.get("owner_preapproved", False) != owner_preapproved:
                    raise ValueError("This owner decision was already recorded with a different approval mode")
                same_decision = stage_ids <= active_stages
            if (not same_decision and earlier and cited is not None and not owner_preapproved
                    and proposed and cited["captured"] <= proposed[0]):
                raise ValueError("The cited owner prompt predates this proposal. Use the owner decision on the current proposal")
            verified = cited is not None and (owner_preapproved or bool(proposed and cited["captured"] > proposed[0]))
            for stage_id in approved_stage_ids:
                self._check_unknowns(task, stages[stage_id])
                self._check_effects(task, stages[stage_id])
            # Retrying the same owner decision must not renew its authority or review anchor.
            if not same_decision:
                revision = approval_record["revision"]
                carried = current.get("earlier_stage_grants", {})
                approval = {"proposal_hash": proposal_hash, "contract_hash": task["contract_hash"],
                            "approved_stage_ids": sorted(active_stages | stage_ids),
                            "user_message_ref": user_message_ref, "approved_at_utc": now(),
                            "provenance": "captured_prompt" if verified else "owner_asserted"}
                if active_stages - stage_ids:
                    approval["earlier_stage_grants"] = {stage_id: carried.get(stage_id, revision)
                                                        for stage_id in active_stages - stage_ids}
                if owner_preapproved:
                    approval["owner_preapproved"] = True
                if task["contract"].get("check_inputs"):
                    from .evidence.check_inputs import observe
                    approval["check_inputs"] = observe(self.store.repo, task["contract"])
                write_record(db, "approval", task_id, approval, expected_revision=revision)
                version_key = task_id + ":" + str(revision + 1)
                write_record(db, "approval_version", version_key, approval, expected_revision=0)
                append_event(db, task_id + ":approval:" + str(revision + 1), "approval", task_id, approval)
                payload = {k: v for k, v in task.items() if k not in {"revision", "legacy_v1"}}
                payload["approved_stage_ids"] = approval["approved_stage_ids"]
                if not active_stages or not active_stages.isdisjoint(stage_ids) or task["state"] not in {
                        "EXECUTING", "VALIDATING", "REVIEWING", "DIAGNOSING"}:
                    payload["state"] = "READY"
                self._save(payload, task["revision"])
                if task['contract'].get('lead_review'):
                    from .lead_review import archive_approved_intent
                    archive_approved_intent(self.store, self.snapshot(task_id))
        if has_software(task["contract"]):
            # Native edits may follow approval before `task run`; they must not become baseline.
            warning = self._approval_baseline(task_id, task["contract"])
            if warning:
                return {**self.snapshot(task_id), "baseline_warning": warning}
        return self.snapshot(task_id)

    def _approval_baseline(self, task_id, contract):
        try:
            record_baseline(self.store, task_id, contract, kind="approval_baseline")
            if read_record(self.store.db, "baseline", task_id)["payload"]:
                # A run baseline of an earlier repository/base must not outlive the revision.
                record_baseline(self.store, task_id, contract)
        except Exception as error:  # the approval is already committed; never report it as failed
            return ("Source baseline not recorded at approval (" + type(error).__name__ + ": " + str(error)[:300]
                    + "); make no native edits before `ase task run`, which records it")
        return None

    @staticmethod
    def _unknown_stages(stages, unknown):
        """Stages blocked by an unknown, including dependents (same rule as _check_unknowns)."""
        affected = set(unknown.get("blocking_stage_ids", [])) | {
            candidate["id"] for candidate in stages if unknown["id"] in candidate.get("unknown_ids", [])}
        if not affected or not affected.issubset({candidate["id"] for candidate in stages}):
            affected = {candidate["id"] for candidate in stages}
        while True:
            expanded = affected | {candidate["id"] for candidate in stages if set(candidate.get("depends_on", [])) & affected}
            if expanded == affected:
                return affected
            affected = expanded

    @classmethod
    def _validate_effects(cls, contract, proposal, *, scope_root=None):
        """Require a bound justification for each new user/system effect declared in a proposal.

        The declaration is optional so ordinary changes proceed as before. Each declared effect
        names what changes, the requirement/decision that makes it necessary (or the unknown that
        holds an unconfirmed assumption) and the scenario the existing path does not cover. The CLI
        checks presence and binding; the coordinator still judges the meaning against the source.
        """
        if "effects" not in proposal:
            return []
        effects = proposal["effects"]
        if not isinstance(effects, list) or len(effects) > 50:
            raise ValueError("Proposal effects must be a bounded list")
        if not effects:
            return effects
        required = {"id", "stage_ids", "change", "basis", "ref", "uncovered_scenario"}
        from .evidence.guarded_additions import EFFECT_FIELDS, effect_target, owner_decision_required
        kinds = {"code", "schema", "infrastructure"}
        stages = contract["stages"]

        def filled(value):
            return isinstance(value, str) and bool(value.strip())

        acceptance_ids = {a["id"] for a in contract.get("acceptance", []) if isinstance(a, dict) and isinstance(a.get("id"), str)}
        # Only a source with a usable locator can bind; a bare placeholder ID is prose with an identifier.
        source_ids = {s["id"] for s in contract.get("sources", [])
                      if isinstance(s, dict) and isinstance(s.get("id"), str) and filled(s.get("locator"))}
        stage_acceptance = {s["id"]: set(s.get("acceptance_ids", [])) for s in stages}
        unknowns = {u["id"]: u for u in contract.get("unknowns", []) if isinstance(u, dict) and isinstance(u.get("id"), str)}
        seen = set()
        for effect in effects:
            if not isinstance(effect, dict) or not required <= set(effect) <= required | {"kind"} | EFFECT_FIELDS:
                raise ValueError("Each proposal effect requires id, stage_ids, change, basis, ref and uncovered_scenario")
            if "kind" in effect and (not isinstance(effect["kind"], str) or effect["kind"] not in kinds):
                raise ValueError("Effect kind must be code, schema or infrastructure")
            effect_id = identifier(effect["id"], "effect id")
            if effect_id in seen:
                raise ValueError("Proposal effect IDs must be unique")
            seen.add(effect_id)
            text(effect["change"], "effect change", limit=2000)
            text(effect["uncovered_scenario"], "effect uncovered_scenario", limit=2000)
            affected = effect["stage_ids"]
            if (not isinstance(affected, list) or not affected or not all(isinstance(s, str) for s in affected)
                    or len(affected) != len(set(affected)) or not set(affected).issubset(proposal.get("stage_ids", []))
                    or not set(affected).issubset(stage_acceptance)):
                raise ValueError("Effect " + effect_id + " requires stage IDs within the proposal")
            addition = effect_target(effect)
            if addition is not None and scope_root is not None:
                field, path, _ = addition
                target = scope_root / path
                for stage in stages:
                    scope = stage["scope"]
                    if stage["id"] in affected and (not {"file_write", "command"}.intersection(scope["effects"])
                            or not any(target.is_relative_to(scope_root / resource) for resource in scope["resources"])):
                        detail = 'package path' if field == 'dotnet_package' else 'addition path'
                        raise ValueError("Effect " + effect_id + ' ' + detail + " exceeds stage mutation scope")
            ref = identifier(effect["ref"], "effect ref")
            # A task-wide source binds every stage; an acceptance criterion binds only stages that own it.
            requirement_refs = source_ids | ({ref} if ref in acceptance_ids and all(
                ref in stage_acceptance.get(stage_id, set()) for stage_id in affected) else set())
            if not isinstance(effect["basis"], str):
                raise ValueError("Effect basis must be REQUIREMENT, DECISION or ASSUMPTION")
            if effect["basis"] == "REQUIREMENT":
                if ref not in requirement_refs:
                    raise ValueError("Effect " + effect_id + " needs a known acceptance or source reference for its stages, not prose")
                if owner_decision_required(effect) and ref not in source_ids:
                    # Added/removed infrastructure, tables and columns are the owner's call: an explicit owner
                    # source, a resolved owner decision, or an assumption that holds the stage until answered.
                    raise ValueError("Effect " + effect_id + " is the owner's decision and needs the owner's explicit source or decision, not a derived acceptance criterion")
            elif effect["basis"] == "DECISION":
                # A decision binds only when the owner's recorded answer covers the unknown for these stages.
                decided = (ref in unknowns and unknowns[ref].get("status", "UNRESOLVED") in {"RESOLVED", "CONFIRMED"}
                           and filled(unknowns[ref].get("resolution"))
                           and set(affected).issubset(cls._unknown_stages(stages, unknowns[ref])))
                if ref not in requirement_refs and not decided:
                    raise ValueError("Effect " + effect_id + " needs a known acceptance or source reference for its stages, or a resolved unknown, not prose")
                if owner_decision_required(effect) and not decided and ref not in source_ids:
                    raise ValueError("Effect " + effect_id + " is the owner's decision and needs the owner's explicit source or decision, not a derived acceptance criterion")
            elif effect["basis"] == "ASSUMPTION":
                if ref not in unknowns or unknowns[ref].get("status", "UNRESOLVED") not in {"UNRESOLVED", "RESOLVED", "CONFIRMED"}:
                    raise ValueError("Effect " + effect_id + " rests on an assumption and needs a tracked unknown")
                if not set(affected).issubset(cls._unknown_stages(stages, unknowns[ref])):
                    raise ValueError("Effect " + effect_id + " unknown does not block its stages")
            else:
                raise ValueError("Effect basis must be REQUIREMENT, DECISION or ASSUMPTION")
        return effects

    @classmethod
    def _check_effects(cls, task, stage):
        """Revalidate declared effects against the current contract before a stage may start."""
        proposal = task.get("proposal")
        if not isinstance(proposal, dict) or "effects" not in proposal:
            return
        from .evidence.guarded_additions import owner_decision_required
        for effect in cls._validate_effects(task["contract"], proposal):
            if stage["id"] not in effect["stage_ids"] or effect["basis"] != "ASSUMPTION":
                continue
            unknown = next((u for u in task["contract"].get("unknowns", []) if u["id"] == effect["ref"]), {})
            # Fail closed: only an explicitly resolved or confirmed unknown releases the stage.
            if unknown.get("status", "UNRESOLVED") not in {"RESOLVED", "CONFIRMED"}:
                raise ValueError("Unconfirmed effect assumption blocks stage: " + effect["id"])
            resolution = unknown.get("resolution")
            if owner_decision_required(effect) and (not isinstance(resolution, str) or not resolution.strip()):
                raise ValueError("Owner-decision effect assumption needs a recorded resolution: " + effect["id"])

    @staticmethod
    def _check_unknowns(task, stage):
        stages = task["contract"]["stages"]
        for unknown in task["contract"]["unknowns"]:
            affected = set(unknown.get("blocking_stage_ids", [])) | {
                candidate["id"] for candidate in stages if unknown["id"] in candidate.get("unknown_ids", [])}
            if not affected or not affected.issubset({candidate["id"] for candidate in stages}):
                affected = {candidate["id"] for candidate in stages}
            while True:
                expanded = affected | {candidate["id"] for candidate in stages if set(candidate.get("depends_on", [])) & affected}
                if expanded == affected:
                    break
                affected = expanded
            if unknown.get("status", "UNRESOLVED") == "UNRESOLVED" and stage["id"] in affected:
                raise ValueError("Unresolved unknown blocks stage: " + unknown["id"])

    def require_approval(self, task_id, stage_id, *, allow_terminal=False, allow_stopped=False):
        task, _ = self.approved_lineage(task_id, stage_id, allow_terminal=allow_terminal,
                                        allow_stopped=allow_stopped)[0]
        return task

    def approved_lineage(self, task_id, stage_id, *, allow_terminal=False, allow_stopped=False, check_sources=True):
        """Return validated grants and mapped stages, with source readiness for execution by default."""
        task = self.snapshot(task_id)
        if not allow_stopped and read_record(self.store.db, "stop", task_id)["payload"]:
            raise ValueError("Task is paused by a stop request")
        if task["state"] == "CANCELLED":
            raise ValueError("Task is cancelled")
        validate_mixed_mapping(task["contract"])
        if task["legacy_v1"]:
            raise ValueError("legacy_v1 has no new execution approval")
        stages = {s["id"]: s for s in task["contract"]["stages"]}
        if stage_id not in stages:
            raise ValueError("Unknown stage scope")
        approval = approval_for_stage(self.store.db, task_id, stage_id)["payload"]
        parents = []
        if task["contract"].get("parent_task_id"):
            parents = self.approved_lineage(task["contract"]["parent_task_id"], task["contract"].get("parent_stage_id"),
                                            allow_terminal=allow_terminal, check_sources=check_sources)
            parent, parent_stage_id = parents[0]
            parent_stage = next(s for s in parent["contract"]["stages"] if s["id"] == parent_stage_id)
            if not _within(task["contract"]["scope"], parent_stage["scope"]):
                raise ValueError("Child scope exceeds approved parent")
            validate_child_obligations(task["contract"], parent["contract"], parent_stage, task_id)
            if task.get("parent_proposal_hash") != parent["proposal_hash"]:
                raise ValueError("Child parent approval binding is stale")
        inherited = bool(task.get("inherited_approval") and task["proposal_hash"] and not task.get("proposal"))
        if not inherited and (not approval or approval["proposal_hash"] != task["proposal_hash"] or approval["contract_hash"] != task["contract_hash"]
              or stage_id not in decision_stages(approval)):
            raise ValueError("Current stage requires explicit approval")
        self._check_unknowns(task, stages[stage_id])
        self._check_effects(task, stages[stage_id])
        if check_sources:
            readiness = self.readiness(task_id, stage_id=stage_id)
            if readiness["untracked_gap_ids"]:
                raise ValueError("Required source gaps need explicit stage obligations: " + ", ".join(readiness["untracked_gap_ids"]))
            if readiness["required_source_gaps"]:
                raise ValueError("Unresolved source gaps block stage: " + ", ".join(gap["id"] for gap in readiness["required_source_gaps"]))
        if not allow_terminal and task["state"] in {"CANCELLED", "COMPLETED"}:
            raise ValueError("Task is terminal")
        return [(task, stage_id), *parents]

    def execution_plan(self, task_id):
        self.snapshot(task_id)
        record = read_record(self.store.db, "execution_plan", task_id)
        if record["payload"] is None:
            raise ValueError("Legacy task has no v2 execution plan")
        return {**record["payload"], "revision": record["revision"]}

    def update_execution_plan(self, task_id, plan, *, expected_revision):
        if not isinstance(plan, dict) or set(plan) != {"checks"}:
            raise ValueError("Execution plan requires checks")
        _unique(plan["checks"], "checks")
        with transaction(self.store.db) as db:
            task = self.snapshot(task_id)
            current = self.execution_plan(task_id)
            by_id = {c["id"]: c for c in plan["checks"]}
            for baseline in current["checks"]:
                if by_id.get(baseline["id"]) != baseline:
                    raise ValueError("Existing required/baseline check cannot be weakened or removed")
            for check in plan["checks"]:
                validate_evidence_spec(check, task["contract"]["profile"])
                if "scope" in check and not _within(_new_scope(check["scope"]), task["contract"]["scope"]):
                    raise ValueError("Additional check scope exceeds approved scope")
            if "bugfix_evidence" in task["contract"]:
                from .testing.bugfix import validate_contract
                validate_contract(task["contract"], plan["checks"])
            payload = {"checks": copy.deepcopy(plan["checks"]), "plan_hash": hash_value(plan["checks"])}
            write_record(db, "execution_plan", task_id, payload, expected_revision=expected_revision)
        return self.execution_plan(task_id)

    def transition(self, task_id, target, *, expected_revision, reason):
        if target not in STATES:
            raise ValueError("Unknown workflow state")
        text(reason, "reason", limit=4000)
        with transaction(self.store.db) as db:
            task = self.snapshot(task_id)
            if task["legacy_v1"]:
                raise ValueError("Legacy state transitions require migration")
            if task["state"] in {"CANCELLED", "COMPLETED"} and target != task["state"]:
                raise ValueError("Terminal task cannot be reopened without a reviewed new task")
            if target == "COMPLETED":
                if any(u.get("status", "UNRESOLVED") == "UNRESOLVED" for u in task["contract"]["unknowns"]):
                    raise ValueError("Unresolved parent obligations prevent completion")
                from .gates import GateService
                gate = GateService(self.store).gate(task_id)
                plan = self.execution_plan(task_id)
                if (not gate or gate.get("status") != "APPROVED" or gate.get("contract_hash") != task["contract_hash"]
                        or gate.get("plan_hash") != plan["plan_hash"]):
                    raise ValueError("COMPLETED requires a current APPROVED gate")
            if target in {"EXECUTING", "VALIDATING", "REVIEWING", "DIAGNOSING"}:
                if not task["approved_stage_ids"] and not task["contract"].get("parent_task_id"):
                    raise ValueError("Execution requires approval")
            payload = {k: v for k, v in task.items() if k not in {"revision", "legacy_v1"}}
            payload.update(state=target, last_reason=reason)
            self._save(payload, expected_revision)
            append_event(db, task_id + ":transition:" + str(expected_revision + 1), "transition", task_id,
                         {"state": target, "reason": reason})
        return self.snapshot(task_id)
