"""Current-source evidence gate; semantic judgments remain separate from proof."""
from contextlib import contextmanager
from contextvars import ContextVar
import fnmatch
import hashlib
import json
from pathlib import Path
import re

from .profiles import evaluate_contract_profiles, has_software, stage_evidence_specs, validate_mixed_mapping
from .runner import verify_receipt
from .state import hash_value, read_record, transaction, write_record
from .store import now
from .tasks import TaskService, _within, validate_child_obligations
from .native_paths import logical_path, state_io


def evaluate_gate(contract, current, receipts, judgments):
    rejected, blocked = [], []
    if current.get("contract_hash") != hash_value(contract):
        rejected.append("Current contract hash mismatch")
    checks = [r for r in receipts if r.get("kind") == "checks"]
    if len(checks) != 1:
        blocked.append("Exactly one current check bundle required")
        check = {}
    else:
        check = checks[0]
        for key in ("fingerprint", "contract_hash", "plan_hash"):
            if check.get(key) != current.get(key):
                rejected.append("Stale check evidence: " + key)
        if check.get("source_changed_during_checks"):
            rejected.append("Source changed during checks")
    specs = current.get("checks", contract.get("evidence", []))
    profile = evaluate_contract_profiles(contract, specs, check.get("results", []), stage_id=current.get("stage_id"))
    rejected.extend(profile["rejected_reasons"])
    blocked.extend(profile["blocked_reasons"])
    expected_bindings = {"fingerprint": current.get("fingerprint"), "contract_sha256": current.get("contract_hash"),
                         "proposal_sha256": current.get("proposal_hash"), "plan_sha256": current.get("plan_hash"),
                         "check_sha256": current.get("check_hash"),
                         "verification_context_sha256": current.get("verification_context_sha256", "missing")}
    if current.get("workspace_id"):
        expected_bindings["workspace_id"] = current["workspace_id"]
    if current.get('lead_review_required'):
        expected_bindings['lead_review_sha256'] = current.get('lead_review_sha256')
    roles, execution_ids, session_ids = {}, set(), set()
    for role in ("ac", "review"):
        matches = [r for r in receipts if r.get("role") == role]
        if len(matches) != 1:
            blocked.append("Independent " + role + " session missing or duplicated")
            continue
        receipt = matches[0]
        if receipt.get("execution_id") in execution_ids or receipt.get("execution_id") == current.get("implementation_execution_id"):
            rejected.append("Independent roles reused the same execution")
            continue
        execution_ids.add(receipt.get("execution_id"))
        try:
            role_bindings = dict(expected_bindings)
            captured = receipt.get("bindings")
            captured_context = captured.get("verification_context_sha256") if isinstance(captured, dict) else None
            allowed_contexts = current.get("verification_context_sha256s", (expected_bindings["verification_context_sha256"],))
            if captured_context in allowed_contexts:
                role_bindings["verification_context_sha256"] = captured_context
            if (current.get('stage_id') is not None and contract.get('lead_review') and not contract.get('parent_task_id')
                    and isinstance(captured, dict) and 'lead_review_sha256' in captured):
                # Whole-root lead disposition is not an intermediate stage's
                # business proof. Preserve the sealed extra binding as provenance.
                role_bindings['lead_review_sha256'] = captured['lead_review_sha256']
            verified = verify_receipt(receipt, expected_bindings=role_bindings)
            if verified["evidence_kind"] != "independent_cli":
                blocked.append("Protocol fixtures are not independent semantic approval: " + role)
                continue
            payload=verified['result']['payload']
            from .findings import substantive_review
            if (verified.get('request_identity',{}).get('purpose')=='test_adequacy'
                    or (role=='review' and not substantive_review(payload))):
                blocked.append('Independent '+role+' receipt is not substantive '+role+' evidence')
                continue
            session_id = verified["result"].get("session_id")
            # Every CLI adapter requires a session id; without one, role separation is unprovable.
            if not isinstance(session_id, str) or not session_id:
                blocked.append("Independent " + role + " receipt has no provider session id")
                continue
            if session_id in session_ids:
                rejected.append("Independent roles reused the same provider session")
                continue
            session_ids.add(session_id)
            roles[role] = verified["result"]["payload"]
        except (ValueError, OSError, KeyError, TypeError) as ex:
            blocked.append("Independent " + role + " receipt invalid: " + str(ex))
    criteria = roles.get("ac", {}).get("criteria", [])
    ids = [ac.get("id") for ac in criteria if isinstance(ac, dict)]
    if len(ids) != len(criteria) or len(ids) != len(set(ids)):
        rejected.append("Duplicate or malformed independent AC results")
    unexpected = set(ids) - {ac["id"] for ac in contract.get("acceptance", [])}
    if unexpected:
        blocked.append("Independent verifier reported uncontracted requirements; reconcile coverage: " + ", ".join(sorted(unexpected)))
    selected_stage = next((s for s in contract.get("stages", []) if s["id"] == current.get("stage_id")), None)
    for ac in contract.get("acceptance", []):
        if selected_stage is not None and ac["id"] not in selected_stage["acceptance_ids"]:
            continue
        if not ac.get("mandatory", True):
            continue
        linked = ac.get("evidence_ids", ac.get("commands", []))
        if not linked:
            blocked.append("Mandatory AC has no executable/evaluable evidence mapping: " + ac["id"])
        for evidence_id in linked:
            result = profile["by_id"].get(evidence_id)
            if not result or result["status"] != "PASS":
                (rejected if result and result["status"] == "FAIL" else blocked).append("Mandatory AC proof missing/failed: " + ac["id"] + "/" + evidence_id)
        matches = [r for r in criteria if isinstance(r, dict) and r.get("id") == ac["id"]]
        if len(matches) != 1:
            blocked.append("Independent mandatory AC result missing: " + ac["id"])
            continue
        result = matches[0]
        if result.get("expected") != ac["expected"]:
            rejected.append("Verifier changed expected behavior: " + ac["id"])
        if result.get("status") == "FAIL":
            rejected.append("Mandatory AC failed: " + ac["id"])
        elif (result.get("status") != "PASS" or not result.get("evidence") or not result.get("method") or result.get("missing_coverage")):
            blocked.append("Mandatory AC unverified: " + ac["id"])
    if contract.get("review_format") == {"version": 2} and "review" in roles and not isinstance(roles["review"].get("unverified_material"), list):
        blocked.append("Independent review result lacks the review format v2 fields")
    unverified = roles.get("review", {}).get("unverified_material")
    if isinstance(unverified, list) and unverified:
        # Review format v2: the reviewer could not establish these with the supplied evidence.
        blocked.extend("Independent review could not verify material change: " + str(item)[:300] for item in unverified[:20])
    from .findings import disposition_result, finding_id, applies
    dispositions, disposed = current.get("finding_dispositions") or {}, []
    for finding in roles.get("review", {}).get("findings", []):
        if not isinstance(finding, dict) or finding.get("severity") not in {"P0", "P1", "P2", "P3"}:
            blocked.append("Malformed independent review finding")
        elif finding["severity"] in {"P0", "P1", "P2"} and applies(finding, dispositions.get(finding_id(finding))):
            disposition = dispositions[finding_id(finding)]
            disposed.append(disposition_result(finding, disposition))
        elif finding["severity"] in {"P0", "P1", "P2"}:
            rejected.append(finding["severity"] + ": " + str(finding.get("message", "Blocking review finding")))
    if current.get("protected_changed"):
        rejected.append("Unauthorized protected file changes: " + ", ".join(current["protected_changed"]))
    if current.get('lead_review_required'):
        from .lead_review import disposition_errors
        blocked.extend(disposition_errors(roles.get('ac', {})))
    if contract.get("risk") == "HIGH" and not current.get("high_risk_approved"):
        blocked.append("HIGH risk requires current explicit approval")
    for unknown in contract.get("unknowns", []):
        if selected_stage is not None and selected_stage["id"] not in unknown.get("blocking_stage_ids", []) and unknown["id"] not in selected_stage.get("unknown_ids", []):
            continue
        if unknown.get("status", "UNRESOLVED") == "UNRESOLVED":
            blocked.append("Unresolved parent obligation: " + unknown["id"])
    extra = {"disposed_findings": disposed} if disposed else {}
    return {**extra, "status": "REJECTED" if rejected else "BLOCKED" if blocked else "APPROVED",
            "rejected_reasons": list(dict.fromkeys(rejected)), "blocked_reasons": list(dict.fromkeys(blocked)),
            "reasons": list(dict.fromkeys(rejected + blocked)), "contract_hash": current.get("contract_hash"),
            "proposal_hash": current.get("proposal_hash"), "plan_hash": current.get("plan_hash"),
            "fingerprint": current.get("fingerprint"), "verification_context_sha256": current.get("verification_context_sha256"),
            "profile_evidence": profile, "timestamp": now()}


def stage_checks(contract, checks, stage_id=None):
    return stage_evidence_specs(contract, checks, stage_id)


def verification_context_hash(store, contract, task_id, stage_id=None, *, _include_lead=True):
    """Bind exact judgment coverage and the relevant current action proof.

    This is separate from source identity: reobserving an external resource can
    change its evidence without changing any source file or executed check.
    """
    from .operations import OperationLedger
    by_id = {stage["id"]: stage for stage in contract["stages"]}
    selected = set()

    def include(name):
        if name not in by_id:
            raise ValueError("Unknown verification context stage")
        if name not in selected:
            selected.add(name)
            for dependency in by_id[name]["depends_on"]:
                include(dependency)

    if stage_id is None:
        selected.update(by_id)
    else:
        include(stage_id)
    ledger = OperationLedger(store)
    root_task_id = ledger.root_task_id(task_id)
    operations = [json.loads(row[0]) for row in store.db.execute(
        "SELECT payload FROM runtime_records WHERE kind='operation'")]
    actions = []
    for stage in contract["stages"]:
        if stage["id"] not in selected:
            continue
        for action in stage.get("actions", []):
            intent = {"project_id": store.project_id, "workspace_id": store.workspace_id,
                "action": action, "effect_scope": stage["scope"]}
            # Select canonical proof records, without treating another action's
            # in-flight overlap as a change to this prerequisite's own proof.
            # Conflict-aware lookup remains mandatory in packets and the gate.
            matches = [operation for operation in operations if operation
                and ledger.root_task_id(operation["task_id"]) == root_task_id
                and ledger._effect_identity(operation["intent"]) == ledger._effect_identity(intent)
                and ledger._same_scope(operation, intent)]
            actions.append({"stage_id": stage["id"], "action_id": action["id"],
                "proof_sha256": hash_value(sorted(matches, key=lambda item: item["action_id"]))})
    return hash_value({"version": 1, "stage_id": stage_id, "actions": actions})


def verification_context_hashes(store, contract, task_id, stage_id=None):
    """Current contexts whose captured coverage may authorize this gate."""
    selected = verification_context_hash(store, contract, task_id, stage_id)
    if stage_id is None:
        return (selected,)
    whole = verification_context_hash(store, contract, task_id)
    return tuple(dict.fromkeys((selected, whole)))


def source_resources(store, contract, task_id=None):
    """The same explicit source/dependency boundaries for hashes and packets."""
    from .source_coverage import declared_source_resources
    discovery = None
    if task_id is not None:
        row = store.db.execute("SELECT payload FROM task_discoveries WHERE task_id=? ORDER BY revision DESC LIMIT 1", (task_id,)).fetchone()
        if row:
            discovery = json.loads(row[0])
    return declared_source_resources(contract, discovery)


def _directory_sources(root, directory, excluded_files=(), retained=None):
    """Bound generic directory inventory without traversing private Git data."""
    from .inspection import is_bytecode_cache
    from .project_local import is_private_path
    pending, count = [directory], 0
    while pending:
        for candidate in pending.pop().iterdir():
            if candidate.name.lower() == ".git" or is_private_path(candidate.relative_to(root)):
                continue
            relative = candidate.relative_to(root).as_posix()
            if relative in excluded_files:
                continue
            if (retained is not None and relative not in retained and is_bytecode_cache(relative)
                    and not candidate.is_symlink() and candidate.is_file()):
                continue  # Untracked interpreter output, as in safe_snapshot.
            count += 1
            if count > 1000:
                raise ValueError("Scoped source inventory exceeds limit")
            resolved = candidate.resolve()
            if (candidate.is_symlink() or getattr(candidate, "is_junction", lambda: False)()
                    or not resolved.is_relative_to(root)
                    or any(p.lower() == ".git" for p in resolved.relative_to(root).parts)):
                raise ValueError("Source escapes workspace")
            if candidate.is_dir():
                pending.append(candidate)
            elif candidate.is_file():
                if candidate.stat().st_nlink > 1:
                    raise ValueError("Hardlinked files are not source inputs")
                yield candidate


_recovery_inventories = ContextVar('recovery_source_inventories', default=())


@contextmanager
def _source_inventory(store, task_ids, authenticate):
    """Scope exact authenticated recovery staging to participating observations."""
    scope_handle = _recovery_inventories.set((*_recovery_inventories.get(),
        (store, frozenset(task_ids), authenticate)))
    try:
        yield
    finally:
        _recovery_inventories.reset(scope_handle)


def _bound_source_fingerprint(fingerprint, runtime_policy, check_inputs=None):
    if check_inputs is not None:
        fingerprint = hash_value({"source_fingerprint": fingerprint, "check_inputs": check_inputs})
    return hash_value({"source_fingerprint": fingerprint, "runtime_policy_sha256": runtime_policy})


def source_snapshot(store, contract, task_id=None, *, _excluded_files=(), _runtime_policy_sha256s=None):
    _excluded_files = set(_excluded_files)
    for owner, task_ids, authenticate in _recovery_inventories.get():
        if owner is store and task_id in task_ids:
            _excluded_files.update(authenticate())
    from .project_local import is_private_path
    from .source_coverage import source_path, source_hash, declared_source_resources, normalized_discovery_policy
    row = store.db.execute("SELECT payload FROM task_discoveries WHERE task_id=? ORDER BY revision DESC LIMIT 1", (task_id,)).fetchone() if task_id is not None else None
    discovery = json.loads(row[0]) if row else None
    policy_sha256 = hash_value(normalized_discovery_policy(discovery)) if discovery is not None else None
    base = None
    from .repositories import task_repository, declared_repositories, owner, snapshot as repository_snapshot
    repository = task_repository(store.repo, contract)["path"] if has_software(contract) else None
    if has_software(contract):
        base = repository_snapshot(store.repo, repository, _excluded_files=_excluded_files)
    files, missing = dict(base["files"]) if base else {}, []
    unavailable = {}
    root = store.repo.resolve()
    declared = declared_repositories(root)
    inventories = {}
    resources = declared_source_resources(contract, discovery)
    declarations = [*(discovery or {}).get("scope", {}).get("dependencies", []),
                    *(discovery or {}).get("dependencies", [])]
    required_dependencies = {item["path"] for item in declarations if item.get("required", True)}
    dependencies = {}

    def capture(resource):
        if is_private_path(resource):
            raise ValueError("Private project-local state is not project source")
        if re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", resource):
            missing.append(resource)
            return
        path = Path(resource)
        if any(part.lower() == ".git" for part in path.parts):
            raise ValueError("Private Git data is not project source")
        if path.is_absolute():
            if not path.is_relative_to(root):
                # Explicit external resource identifiers use actual probes.
                missing.append(resource)
                return
            path = path.relative_to(root)
        path = source_path(root, path.as_posix())
        if path.relative_to(root).as_posix() in _excluded_files:
            return
        if is_private_path(path.relative_to(root)):
            raise ValueError("Private project-local state is not project source")
        if path.is_file() and path.stat().st_nlink > 1:
            raise ValueError("Hardlinked files are not source inputs")
        if any(part.lower() == ".git" for part in path.relative_to(root).parts):
            raise ValueError("Private Git data is not project source")
        required_directory = False
        if path.is_dir():
            # Broad workspace inventory keeps Git ignore semantics. A named
            # required dependency subtree includes manifest-inferred local paths:
            # "from" records provenance, not a weaker input requirement. Root
            # declarations never expand into ignored secrets.
            required_directory = path != root and path.relative_to(root).as_posix() in required_dependencies
            nested = owner(declared, path.relative_to(root).as_posix()) if path != root else None
            if required_directory and nested is not None:
                # A nested clone's own Git decides tracked and nonignored files (no bin/obj).
                if nested not in inventories:
                    inventories[nested] = repository_snapshot(root, nested, _excluded_files=_excluded_files)["files"]
                inventory = {name: value for name, value in inventories[nested].items() if (root / name).is_relative_to(path)}
                if len(inventory) > 50000:
                    raise ValueError("Scoped source inventory exceeds limit")
                for name, value in inventory.items():
                    if name not in files:
                        dependencies[name] = value
                        files[name] = value
                return
            candidates = ([root / name for name in base["files"] if (root / name).is_relative_to(path)]
                          if base is not None and not required_directory
                          else sorted(_directory_sources(root, path, _excluded_files,
                                                         base["files"] if base is not None else None)))
        else:
            candidates = [path]
        if len(candidates) > (50000 if repository is not None and not required_directory else 1000):
            raise ValueError("Scoped source inventory exceeds limit")
        for candidate in candidates:
            if (candidate.is_symlink() or not candidate.resolve().is_relative_to(root)
                    or any(p.lower() == ".git" for p in candidate.resolve().relative_to(root).parts)):
                raise ValueError("Source escapes workspace")
            if candidate.is_file():
                relative = str(candidate.relative_to(root)).replace("\\", "/")
                if base is not None and relative in files:
                    continue
                observed = source_hash(root, relative)
                if relative not in files:
                    dependencies[relative] = observed
                    files[relative] = observed
                elif base is None:
                    files[relative] = observed
    for resource in resources:
        try:
            capture(resource)
        except OSError as ex:
            # Preserve a stable observation without turning optional or deferred
            # inputs into a global read failure. Packet/readiness apply the
            # declared stage requirements; path-safety ValueErrors still raise.
            unavailable[resource] = {"status": "UNAVAILABLE", "error": type(ex).__name__, "errno": ex.errno}
    if base:
        additions = {"dependencies": dependencies, "unavailable": unavailable}
        observed = {**base, "fingerprint": hash_value({"base": base["fingerprint"], **additions}) if dependencies or unavailable else base["fingerprint"],
                    "files": files, "unavailable": unavailable, "dependency_paths": sorted(dependencies)}
    else:
        observed = {"fingerprint": hash_value({"files": files, "external_resources": missing, "unavailable": unavailable}), "files": files, "unavailable": unavailable,
                    "workspace_id": store.workspace_id, "timestamp": now()}
    if policy_sha256 is not None:
        observed["fingerprint"] = hash_value({"source_fingerprint": observed["fingerprint"], "discovery_policy_sha256": policy_sha256})
        observed["discovery_policy_sha256"] = policy_sha256
    if contract.get("check_inputs"):
        # Declared private check scripts are not source, but an edit to one makes the checks it ran stale.
        from .evidence.check_inputs import observe
        observed["source_fingerprint_without_checks"] = observed["fingerprint"]
        observed["check_inputs"] = observe(store.repo, contract)
    if _runtime_policy_sha256s is not None:
        # The same single read of the artifacts under named runtime policies; nothing is recorded.
        return [_bound_source_fingerprint(observed['fingerprint'], policy, observed.get('check_inputs'))
                for policy in _runtime_policy_sha256s]
    from .runtime_policy import policy_fingerprint
    observed['runtime_policy_sha256'] = policy_fingerprint()
    observed['fingerprint'] = _bound_source_fingerprint(observed['fingerprint'], observed['runtime_policy_sha256'],
                                                       observed.get('check_inputs'))
    return observed


def fingerprint_under_policy(store, contract, task_id, *runtime_policy_sha256s):
    """Current source fingerprints, from one read, as they would be under the given runtime policy identities."""
    if not runtime_policy_sha256s or any(not isinstance(item, str) or not re.fullmatch('[0-9a-f]{64}', item)
                                         for item in runtime_policy_sha256s):
        raise ValueError('Invalid runtime policy identity')
    return source_snapshot(store, contract, task_id, _runtime_policy_sha256s=runtime_policy_sha256s)


def fingerprint_with_check_inputs(source, check_inputs, *, runtime_policy=None):
    """Bind today's source to the check inputs observed in a sealed historical packet."""
    current = source.get("check_inputs")
    if (not current or not isinstance(check_inputs, dict) or current.keys() != check_inputs.keys()
            or any(not isinstance(digest, str) or not re.fullmatch("[a-f0-9]{64}", digest)
                   for digest in (*current.values(), *check_inputs.values()))):
        return None
    return _bound_source_fingerprint(source["source_fingerprint_without_checks"],
                                     runtime_policy or source["runtime_policy_sha256"], check_inputs)


class GateService:
    def __init__(self, store):
        self.store = store

    def required_child_blockers(self, task_id, stage_id=None):
        """Fresh pre-mutation child readiness; never launches a child controller."""
        task = TaskService(self.store).snapshot(task_id)
        if task["legacy_v1"]:
            return ["Required child readiness requires a reviewed v2 contract"]
        contract = task["contract"]
        if stage_id is not None and not any(stage["id"] == stage_id for stage in contract["stages"]):
            raise ValueError("Unknown required-child stage")
        try:
            validate_mixed_mapping(contract)
        except (ValueError, TypeError, KeyError) as error:
            return ["MIXED_PROFILE_UNMAPPED: " + str(error)]
        return self._required_children(task_id, contract, stage_id)

    def _required_children(self, task_id, contract, stage_id):
        blocked = []
        for stage in contract["stages"]:
            child_id = stage.get("required_child_task_id")
            if not child_id or (stage_id is not None and stage["id"] != stage_id):
                continue
            child = read_record(self.store.db, "task", child_id)["payload"]
            if child is None:
                blocked.append("Required child missing: " + child_id)
                continue
            c = child["contract"]
            if c.get("parent_task_id") != task_id or c.get("parent_stage_id") != stage["id"] or not _within(c["scope"], stage["scope"]):
                blocked.append("Required child parent/stage/scope mapping differs: " + child_id)
                continue
            try:
                validate_child_obligations(c, contract, stage, child_id)
            except ValueError as error:
                blocked.append("Required child domain/acceptance mapping invalid: " + str(error))
                continue
            if child["state"] != "COMPLETED":
                blocked.append("Required child incomplete: " + child_id)
                continue
            proof = self.gate(child_id)
            if proof["status"] != "APPROVED":
                blocked.append("Required child evidence is not currently approved: " + child_id)
        return blocked

    def gate(self, task_id, stage_id=None):
        tasks = TaskService(self.store)
        task = tasks.snapshot(task_id)
        if task["legacy_v1"]:
            return {"status": "BLOCKED", "legacy_v1": True,
                    "blocked_reasons": ["Legacy history does not grant v2 execution authority; revise and approve a v2 contract"],
                    "rejected_reasons": [], "reasons": ["Legacy task requires reviewed v2 migration"]}
        try:
            validate_mixed_mapping(task["contract"])
        except (ValueError, TypeError, KeyError) as error:
            return {"status": "BLOCKED", "blocker": "MIXED_PROFILE_UNMAPPED", "blocked_reasons": [str(error)],
                    "rejected_reasons": [], "reasons": [str(error)]}
        plan = tasks.execution_plan(task_id)
        stage = next((s for s in task["contract"]["stages"] if s["id"] == stage_id), None)
        if stage_id is not None and stage is None:
            raise ValueError("Unknown gate stage")
        source = source_snapshot(self.store, task["contract"], task_id=task_id)
        check = read_record(self.store.db, "checks", task_id)["payload"]
        current = {"contract_hash": task["contract_hash"], "proposal_hash": task["proposal_hash"], "plan_hash": plan["plan_hash"],
                   "fingerprint": source["fingerprint"], "check_hash": hash_value(check) if check else "missing",
                   "checks": plan["checks"], "workspace_id": self.store.workspace_id}
        if stage is not None:
            current.update(stage_id=stage_id, checks=stage_checks(task["contract"], plan["checks"], stage_id))
        current["verification_context_sha256s"] = verification_context_hashes(self.store, task["contract"], task_id, stage_id)
        current["verification_context_sha256"] = current["verification_context_sha256s"][0]
        from .lead_review import LeadReview
        lead_service = LeadReview(self.store)
        current['lead_review_required'] = stage_id is None and lead_service.configured(task_id)
        current['lead_review_sha256'] = lead_service.binding(task_id, stage_id)
        implementation = read_record(self.store.db, "implementation", task_id)["payload"]
        current["implementation_execution_id"] = (implementation or {}).get("execution_id")
        from .testing.adequacy import TestAdequacy
        quality_service=TestAdequacy(self.store)
        current['test_adequacy_sha256']=quality_service.binding(task_id,stage_id)
        verification_inputs=hash_value({role:read_record(self.store.db,'verification',task_id+':'+role)['payload']
            for role in ('ac','review')})
        receipts = [check] if check else []
        for role in ("ac", "review"):
            record = read_record(self.store.db, "verification", task_id + ":" + role)["payload"]
            if record:
                path = Path(record.get("artifact_root", "")).resolve()
                if not path.is_relative_to(self.store.root.resolve()):
                    continue
                receipts.append(record)
        baseline = read_record(self.store.db, "baseline", task_id)["payload"]
        changed_paths = ([p for p in set(baseline["files"]) | set(source["files"]) if baseline["files"].get(p) != source["files"].get(p)]
                         if baseline else [])
        if has_software(task["contract"]) and baseline:
            from .core import PROTECTED, project_protected_paths
            patterns = [*PROTECTED, *task["contract"].get("protected_paths", []), *project_protected_paths(self.store.repo)]
            changed = [p for p in set(baseline["files"]) | set(source["files"]) if baseline["files"].get(p) != source["files"].get(p)]
            authorization = read_record(self.store.db, "protected_approval", task_id)["payload"]
            allowed = authorization.get("paths", []) if authorization and authorization.get("fingerprint") == source["fingerprint"] and authorization.get("contract_hash") == task["contract_hash"] else []
            current["protected_changed"] = [p for p in changed if p not in allowed and any(fnmatch.fnmatchcase(p, pattern) for pattern in patterns)]
        high = read_record(self.store.db, "high_risk_approval", task_id)["payload"]
        current["high_risk_approved"] = bool(high and high.get("fingerprint") == source["fingerprint"] and high.get("contract_hash") == task["contract_hash"])
        from .findings import Findings
        findings = Findings(self.store)
        current["finding_dispositions"] = findings.current(task_id)
        result = evaluate_gate(task["contract"], current, receipts, [])
        result['test_adequacy_sha256']=current['test_adequacy_sha256']
        result['lead_review_sha256'] = current['lead_review_sha256']
        extra = []
        if has_software(task['contract']):
            from .evidence.guarded_additions import approval_blockers
            extra.extend(approval_blockers(tasks, task, stage_id, baseline, source, changed_paths))
        lead = lead_service.gate(task_id, stage_id=stage_id)
        result['lead_review'] = lead
        extra.extend('Lead review: ' + reason for reason in lead['blocked_reasons'])
        lead_pending = current['lead_review_required'] and lead.get('status') != 'READY' and not any(
                'lead_review_sha256' in (r.get('bindings') or {}) for r in receipts if r.get('role') in ('ac', 'review'))
        note = 'Final independent AC, review and test adequacy of the whole task run after the lead review pass'
        if lead_pending:
            # The latest AC/review judged a stage before the lead pass, so it cannot be the
            # whole-task verification yet. Say that, instead of calling those receipts stale.
            # Presentation only: the lead blocker above keeps this gate BLOCKED. Other receipt
            # failures (a crashed or timed-out session, say) stay visible as diagnostics.
            pending = [r for r in result['blocked_reasons'] if r.startswith('Independent ') and (
                r.endswith(('Receipt inputs are stale', 'session missing or duplicated'))
                or r.startswith(('Independent mandatory AC result missing: ',
                                 'Independent AC lead classification/pass assessment'))
            )]
            if pending:
                result['blocked_reasons'] = [r for r in result['blocked_reasons'] if r not in pending] + [note]
                result['reasons'] = [r for r in result['reasons'] if r not in pending] + [note]
        from .testing.adequacy import TestAdequacy
        adequacy = TestAdequacy(self.store).gate(task_id, current=current, stage_id=stage_id)
        result['test_adequacy'] = adequacy
        adequacy_reasons = adequacy['blocked_reasons']
        if lead_pending and stage_id is None and 'Independent test adequacy receipt missing' in adequacy_reasons:
            # Same presentation rule: whole-task adequacy runs with the final verification.
            adequacy_reasons = [r for r in adequacy_reasons if r != 'Independent test adequacy receipt missing']
            if note not in result['blocked_reasons']:
                extra.append(note)
        extra.extend(adequacy_reasons)
        if adequacy.get('rejected_reasons'):
            result['rejected_reasons'] = list(dict.fromkeys(result['rejected_reasons'] + adequacy['rejected_reasons']))
            result['reasons'] = list(dict.fromkeys(result['reasons'] + adequacy['rejected_reasons']))
            result['status'] = 'REJECTED'
        from .evidence.representation import part_gate
        from .evidence.check_inputs import gate_blockers
        extra.extend(gate_blockers(source.get("check_inputs", {})))
        if baseline and has_software(task["contract"]):
            from .tasks import baseline_repository, contract_repository
            try:
                foreign = baseline_repository(baseline) != contract_repository(self.store, task["contract"])
            except ValueError as error:
                foreign, reason = True, str(error)
            else:
                reason = "run the task again to recompute it from the base ref"
            if foreign:
                extra.append("Source baseline was recorded for another repository/base: " + reason)
        part_blocked, part_rejected = part_gate(self.store, task["contract"], task_id, stage_id, current, changed_paths)
        from .review.blind import blind_gate
        blind_blocked, blind_rejected, blind_dismissed = blind_gate(self.store, task["contract"], task_id, stage_id, current)
        part_blocked, part_rejected = part_blocked + blind_blocked, part_rejected + blind_rejected
        if blind_dismissed:
            result['blind_dismissed'] = blind_dismissed
        extra.extend(part_blocked)
        if part_rejected:
            result['rejected_reasons'] = list(dict.fromkeys(result['rejected_reasons'] + part_rejected))
            result['reasons'] = list(dict.fromkeys(result['reasons'] + part_rejected))
            result['status'] = 'REJECTED'
        from .framework import STAMP_FIELD, stale_verdicts, stamp
        result[STAMP_FIELD] = stamp()
        stale = stale_verdicts(self.store, task, stage_id, current=result[STAMP_FIELD])
        if stale:
            result['stale_verdicts'] = stale
            extra.extend(row['reason'] for row in stale)
        from .delegations import DelegationRegistry
        extra.extend(DelegationRegistry(self.store).closure_blockers(task_id, stage_id=stage_id))
        readiness = tasks.readiness(task_id, stage_id=stage_id)
        extra.extend("Unresolved required source gap: " + gap["id"] for gap in readiness["required_source_gaps"])
        extra.extend(self._required_children(task_id, task["contract"], stage_id))
        approval = read_record(self.store.db, "approval", task_id)["payload"]
        if not task["contract"].get("parent_task_id") and (not approval or approval.get("proposal_hash") != task["proposal_hash"] or approval.get("contract_hash") != task["contract_hash"]):
            from .tasks import approval_hint
            extra.append("Current proposal approval not recorded: " + approval_hint(task))
        elif stage_id is not None:
            try:
                tasks.require_approval(task_id, stage_id, allow_terminal=task["state"] == "COMPLETED")
            except ValueError as ex:
                extra.append(str(ex))
        if task["contract"].get("parent_task_id"):
            try:
                tasks.require_approval(task_id, stage_id or task["contract"]["stages"][0]["id"], allow_terminal=True)
            except ValueError as ex:
                extra.append("Inherited parent approval invalid: " + str(ex))
        specs = {s["id"]: s for s in current["checks"]}
        access_cache = {}

        def access_blockers(access, captured, label):
            from .credentials import CredentialVault
            key = (access["credential_ref"], access["target"])
            if key not in access_cache:
                try:
                    metadata = CredentialVault(self.store.state_home).metadata(*key, timeout=1)
                    access_cache[key] = {"credential_ref": metadata["id"], "target": metadata["target"], "revision": metadata["revision"]}
                except (ValueError, OSError):
                    access_cache[key] = None
            expected = access_cache[key]
            return [] if expected is not None and captured == expected else ["Credential evidence is missing or stale: " + label]

        proofs = {proof.get("id"): proof for proof in (check or {}).get("results", [])}
        for spec in current["checks"]:
            if "access" in spec:
                extra.extend(access_blockers(spec["access"], proofs.get(spec["id"], {}).get("access"), spec["id"]))
        for proof in (check or {}).get("results", []):
            spec = specs.get(proof.get("id"))
            if not spec:
                continue
            if spec["kind"] == "artifact":
                declared = (self.store.repo / spec["path"]).resolve()
                captured = Path(proof.get("path", "")).resolve()
                if declared != captured or not captured.is_relative_to(self.store.repo.resolve()):
                    extra.append("Artifact path differs from approved plan: " + spec["id"])
            elif spec["kind"] != "manual":
                try:
                    log = logical_path(Path(proof.get("log", "")))
                    valid = log.is_relative_to((self.store.root / "tasks" / task_id / "checks").resolve()) and Path(proof.get("cwd", "")).resolve() == self.store.repo.resolve()
                except (OSError, ValueError, TypeError):
                    valid = False
                if not valid:
                    extra.append("Command evidence outside selected task/workspace: " + spec["id"])
        if stage_id is None:
            for s in task["contract"]["stages"]:
                done = read_record(self.store.db, "stage", task_id + ":" + s["id"])["payload"]
                if not done or done.get("state") != "COMPLETED" or done.get("contract_hash") != task["contract_hash"] or done.get("proposal_hash") != task["proposal_hash"]:
                    extra.append("Stage incomplete: " + s["id"])
            for row in self.store.db.execute("SELECT key,payload FROM runtime_records WHERE kind='task'"):
                child = json.loads(row["payload"])
                if child and child["contract"].get("parent_task_id") == task_id and child["state"] != "COMPLETED":
                    extra.append("Child obligation incomplete: " + row["key"])
        for row in self.store.db.execute("SELECT payload FROM runtime_records WHERE kind='operation'"):
            operation = json.loads(row[0])
            if (operation and operation.get("task_id") == task_id
                    and (stage_id is None or operation.get("stage_id") == stage_id)
                    and operation.get("state") not in {"APPLIED", "ABSENT"}):
                extra.append("Uncertain external operation: " + operation["action_id"])
        from .operations import OperationLedger
        ledger = OperationLedger(self.store)
        for declared_stage in task["contract"]["stages"]:
            if stage_id is not None and declared_stage["id"] != stage_id:
                continue
            for action in declared_stage.get("actions", []):
                action_id = hash_value({"task": task_id, "stage": declared_stage["id"], "action": action["id"]})
                intent = {"project_id": self.store.project_id, "workspace_id": self.store.workspace_id, "action": action, "effect_scope": declared_stage["scope"]}
                try:
                    operation = ledger.lookup(task_id, declared_stage["id"], action_id, intent)
                except ValueError:
                    operation = None
                observation = (operation or {}).get("observation", {})
                legacy_intent = {key: value for key, value in intent.items() if key != "effect_scope"}
                same_legacy_intent = bool(operation and (operation.get("intent") == intent or (
                    "effect_scope" not in operation.get("intent", {}) and operation.get("intent") == legacy_intent)))
                legacy = bool(same_legacy_intent
                              and operation.get("task_id") == task_id and operation.get("stage_id") == declared_stage["id"]
                              and "action_hash" not in observation and "observing_workspace_id" not in observation)
                observer_task = observation.get("observing_task_id", task_id if legacy else None)
                bound = (observation.get("action_hash") == hash_value(action)
                         and observation.get("observing_workspace_id") == self.store.workspace_id)
                try:
                    same_root = bool(observer_task and ledger.root_task_id(observer_task) == ledger.root_task_id(task_id))
                except ValueError:
                    same_root = False
                if not operation or not (legacy or bound) or not same_root:
                    extra.append("Action evidence is missing or stale: " + action["id"])
                else:
                    if "access" in action:
                        extra.extend(access_blockers(action["access"], observation.get("access"), "action-" + action["id"]))
                    if operation.get("state") != "APPLIED" or observation.get("exists") is not True:
                        extra.append("Action postcondition is not established: " + action["id"])
                    try:
                        path, expected_hash = observation["proof_ref"].rsplit("#sha256=", 1)
                        path = logical_path(path)
                        required = self.store.root / "tasks" / observer_task / "actions" / operation["action_id"]
                        if (not path.is_relative_to(required.resolve())
                                or hashlib.sha256(state_io(path).read_bytes()).hexdigest() != expected_hash):
                            raise ValueError("Invalid observation proof")
                    except (KeyError, ValueError, OSError, TypeError):
                        extra.append("Action observation proof is missing or invalid: " + action["id"])
        if verification_context_hashes(self.store, task["contract"], task_id, stage_id) != current["verification_context_sha256s"]:
            extra.append("Action verification context changed during gate")
        if extra:
            result["blocked_reasons"] = list(dict.fromkeys(result["blocked_reasons"] + extra))
            result["reasons"] = list(dict.fromkeys(result["reasons"] + extra))
            if result["status"] == "APPROVED":
                result["status"] = "BLOCKED"
        if stage_id is not None:
            result["stage_id"] = stage_id
        with transaction(self.store.db) as db:
            from .evidence.representation import MAX_QUOTED_CHARACTERS, part_key, parts

            current_reviews = [read_record(db, 'verification', task_id + ':review')['payload']]
            current_reviews.extend(read_record(db, 'verification_part', part_key(task_id, stage_id, part['id']))['payload']
                                   for part in parts(task['contract']))
            pending = ["Review finding journal: " + reason for receipt in current_reviews
                       if (reason := findings.projection_error(task_id, stage_id, receipt))]
            open_findings, journal_disposed = findings.review_obligations(task, stage_id, current["finding_dispositions"])
            if journal_disposed:
                decisions = {row["id"]: row for row in [*result.get("disposed_findings", []), *journal_disposed]}
                result["disposed_findings"] = list(decisions.values())
            if open_findings:
                result["open_review_findings"] = [row["id"] for row in open_findings]
                for row in open_findings:
                    issue = row["finding"]
                    reason = issue["severity"] + ": " + issue["message"]
                    part_reason = issue["severity"] + ": " + issue["message"][:MAX_QUOTED_CHARACTERS]
                    if not any(rejected == reason or rejected.endswith(": " + part_reason)
                               for rejected in result["rejected_reasons"]):
                        pending.append("Independent follow-up required for open review finding " + row["id"]
                                       + " at " + issue["path"] + ":" + str(issue["line"]))
            if pending:
                result["blocked_reasons"] = list(dict.fromkeys(result["blocked_reasons"] + pending))
                result["reasons"] = list(dict.fromkeys(result["reasons"] + pending))
                if result["status"] == "APPROVED":
                    result["status"] = "BLOCKED"
                target_stage = stage_id or next((row["stage_id"] for row in open_findings if row["stage_id"]), None)
                stage_flag = " --stage " + target_stage if target_stage else ""
                target = task_id + stage_flag
                followup = "refresh checks and independent review with `ase task check " + target + "` and `ase task verify " + target + "`"
                result.setdefault("next_action", "Inspect `ase task findings " + task_id + "` and " + followup + ".")
            if adequacy.get('configured') or current['lead_review_required'] or open_findings or pending:
                latest = tasks.snapshot(task_id)
                current_check = read_record(db, 'checks', task_id)['payload']
                if (latest['contract_hash'] != current['contract_hash']
                        or latest['proposal_hash'] != current['proposal_hash']
                        or tasks.execution_plan(task_id)['plan_hash'] != current['plan_hash']
                        or (hash_value(current_check) if current_check else 'missing') != current['check_hash']
                        or source_snapshot(self.store, latest['contract'], task_id=task_id)['fingerprint'] != current['fingerprint']
                        or quality_service.binding(task_id,stage_id) != current['test_adequacy_sha256']
                        or lead_service.binding(task_id,stage_id) != current['lead_review_sha256']
                        or hash_value({role:read_record(db,'verification',task_id+':'+role)['payload'] for role in ('ac','review')}) != verification_inputs
                        or verification_context_hashes(self.store, latest['contract'], task_id, stage_id) != current['verification_context_sha256s']):
                    reason = 'Source or current evidence changed during gate'
                    result['blocked_reasons'] = list(dict.fromkeys(result['blocked_reasons'] + [reason]))
                    result['reasons'] = list(dict.fromkeys(result['reasons'] + [reason]))
                    if result['status'] == 'APPROVED':
                        result['status'] = 'BLOCKED'
            key = task_id if stage_id is None else task_id + ":" + stage_id
            old = read_record(db, "gate", key)
            write_record(db, "gate", key, result, expected_revision=old["revision"])
        return result
