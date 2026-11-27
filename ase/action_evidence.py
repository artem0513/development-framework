"""Bounded, immutable action/proposal transport; never semantic acceptance."""
import hashlib
import json
from datetime import datetime
from pathlib import Path
import re

from .core import redact, validate_command_argv
from .native_paths import logical_path, state_io
from .runner import verify_receipt
from .state import encode, hash_value, identifier, read_record
from .evidence.compact import expand_packet

MAX_LOG = 32768
MAX_READ = 16 * 1024 * 1024
MAX_TOTAL_LOG = 131072
MAX_MANIFEST = 262144


def _file(path, content):
    return {"path": path, "content": content, "sha256": hashlib.sha256(content.encode()).hexdigest()}


def _redacted(value):
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, list):
        return [_redacted(item) for item in value]
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            safe = redact(key)
            if safe in result:
                raise ValueError("Redaction would merge distinct evidence keys")
            result[safe] = _redacted(item)
        return result
    return value


def _execution_origin(store, ledger, task, operation, context):
    from .tasks import decision_stages

    actor = identifier(context["task_id"], "Execution actor task")
    if (context.get("version") != 2 or context.get("workspace_id") != store.workspace_id
            or context.get("project_id") != store.project_id
            or ledger.root_task_id(actor) != ledger.root_task_id(task["task_id"])):
        raise ValueError("Execution actor is outside this workspace/managed root")
    stage = context["declared_stage"]
    if stage.get("id") != context["stage_id"]:
        raise ValueError("Execution original stage differs")
    action = next((item for item in stage.get("actions", []) if hash_value(item) == context.get("action_sha256")), None)
    if action is None or context.get("argv_sha256") != hash_value(action["argv"]):
        raise ValueError("Execution original action differs")
    intent = {"project_id": store.project_id, "workspace_id": store.workspace_id, "action": action, "effect_scope": stage["scope"]}
    if ledger._effect_identity(intent) != ledger._effect_identity(operation["intent"]) or not ledger._same_scope(operation, intent):
        raise ValueError("Execution original effect/footprint differs")
    authority = context["authority"]
    ancestry = authority["ancestry"]
    if (not ancestry or ancestry[0]["task_id"] != actor or authority["plan_hash"] != context["plan_hash"]
            or any(ancestry[0].get(key) != context.get(key) for key in ("contract_hash", "proposal_hash"))):
        raise ValueError("Execution approval identity differs")
    if hash_value(context.get("execution_plan_checks")) != context["plan_hash"]:
        raise ValueError("Execution original check-plan hash differs")
    for index, ancestor in enumerate(ancestry):
        current = store.task(ancestor["task_id"])["contract"]
        parent = ancestry[index + 1]["task_id"] if index + 1 < len(ancestry) else None
        if current.get("parent_task_id") != parent:
            raise ValueError("Execution ancestry differs from immutable parent links")
        if ancestor.get("approval_mode") == "inherited":
            if parent is None:
                raise ValueError("Inherited execution authority has no parent")
            continue
        grant = read_record(store.db, "approval_version", ancestor["task_id"] + ":" + str(ancestor["approval_revision"]))["payload"]
        if not grant or any(grant.get(key) != ancestor.get(key) for key in ("contract_hash", "proposal_hash")):
            raise ValueError("Original execution approval version is unavailable")
        if index == 0 and stage["id"] not in decision_stages(grant):
            raise ValueError("Original execution stage was outside the captured grant")
    return actor, stage, action


def _captured_time(value):
    if not isinstance(value, str):
        raise ValueError("Historical authority requires a captured UTC time")
    result = datetime.fromisoformat(value)
    if result.tzinfo is None:
        raise ValueError("Historical authority time must include its timezone")
    return result


def _original_authority(store, task_id, stage_id, proposal_hash, *, before, contract_hash=None, seen=()):
    """Resolve an original explicit/inherited grant from immutable local archives."""
    from .tasks import _within, decision_stages, validate_child_obligations
    failure = "Original historical stage authority is unavailable"
    if task_id in seen:
        raise ValueError("Historical authority ancestry has a cycle")
    cutoff = _captured_time(before)
    prefix = task_id + ":"
    versions = []
    current_parent = store.task(task_id)["contract"].get("parent_task_id")
    for row in store.db.execute("SELECT key,revision,payload,updated FROM runtime_records WHERE kind='task_version' AND substr(key,1,?)=?", (len(prefix), prefix)):
        origin = json.loads(row["payload"])
        suffix = row["key"][len(prefix):]
        if (row["revision"] == 1 and suffix.isdigit() and isinstance(origin, dict) and origin.get("task_id") == task_id
                and isinstance(origin.get("contract"), dict) and hash_value(origin["contract"]) == origin.get("contract_hash")
                and (contract_hash is None or origin["contract_hash"] == contract_hash)
                and origin["contract"].get("parent_task_id") == current_parent and _captured_time(row["updated"]) <= cutoff):
            versions.append((int(suffix), row["key"], origin, row["updated"]))
    events = [(row["sequence"], row["event_key"], row["kind"], json.loads(row["payload"]), row["created"])
        for row in store.db.execute("SELECT sequence,event_key,kind,payload,created FROM runtime_events WHERE task_id=? AND kind IN ('proposal','approval') ORDER BY sequence", (task_id,))]
    candidates = []
    for revision, key, origin, created in versions:
        if (origin.get("inherited_approval") is True and not origin.get("proposal")
                and origin.get("proposal_hash") == proposal_hash
                and proposal_hash == hash_value({"parent_proposal_hash": origin.get("parent_proposal_hash"), "contract_hash": origin["contract_hash"]})
                and current_parent and stage_id in origin.get("approved_stage_ids", [])):
            candidates.append((origin, {"task_version_key": key, "approval_mode": "inherited"}, created))
    for sequence, event_key, kind, grant, event_time in reversed(events):
        suffix = event_key.removeprefix(prefix + "approval:")
        if (kind != "approval" or not suffix.isdigit() or not isinstance(grant, dict)
                or grant.get("proposal_hash") != proposal_hash or stage_id not in decision_stages(grant)
                or _captured_time(event_time) > cutoff or _captured_time(grant.get("approved_at_utc")) > cutoff):
            continue
        archived = read_record(store.db, "approval_version", prefix + suffix)
        if archived["revision"] != 1 or archived["payload"] != grant:
            continue
        proposals = [(position, int(key.rsplit(":", 1)[-1])) for position, key, event_kind, value, timestamp in events
            if event_kind == "proposal" and key.startswith(prefix + "proposal:") and key.rsplit(":", 1)[-1].isdigit()
            and position < sequence and isinstance(value, dict) and value.get("proposal_hash") == proposal_hash
            and stage_id in value.get("stage_ids", []) and _captured_time(timestamp) <= cutoff]
        for _, proposal_revision in reversed(proposals):
            for revision, key, origin, created in reversed(versions):
                if revision <= proposal_revision and origin["contract_hash"] == grant.get("contract_hash"):
                    candidates.append((origin, {"task_version_key": key, "approval_mode": "explicit",
                        "approval_version_key": prefix + suffix, "approval_sha256": hash_value(grant)}, event_time))
    for origin, metadata, granted_at in candidates:
        stage = next((item for item in origin["contract"]["stages"] if item["id"] == stage_id), None)
        if stage is None:
            continue
        parent = origin["contract"].get("parent_task_id")
        if parent:
            try:
                parent_origin, parent_stage, parent_metadata = _original_authority(store, parent,
                    origin["contract"]["parent_stage_id"], origin.get("parent_proposal_hash"),
                    before=granted_at, seen=(*seen, task_id))
                if not _within(origin["contract"]["scope"], parent_stage["scope"]):
                    raise ValueError("Historical child scope exceeds its original parent stage")
                validate_child_obligations(origin["contract"], parent_origin["contract"], parent_stage, task_id)
                metadata["parent_authority"] = parent_metadata
            except ValueError:
                continue
        return origin, stage, metadata
    raise ValueError(failure)


def _superseded_stage(store, task, stage_id, contract_hash, proposal_hash, *, captured_at=None):
    """Authenticate older scoped authority; it supplies history, never permission."""
    from .tasks import TaskService
    failure = "stale contract/proposal history has no verified authorized supersession"
    TaskService(store).require_approval(task["task_id"], stage_id, allow_terminal=True, allow_stopped=True)
    current = read_record(store.db, "approval", task["task_id"])
    grant = current["payload"]
    if (not isinstance(grant, dict) or not task.get("proposal")
            or hash_value(task["contract"]) != task["contract_hash"]
            or hash_value({"contract_hash": task["contract_hash"], "proposal": task["proposal"]}) != task["proposal_hash"]
            or task["proposal"].get("task_id", task["task_id"]) != task["task_id"]
            or any(grant.get(key) != task.get(key) for key in ("contract_hash", "proposal_hash", "approved_stage_ids"))):
        raise ValueError(failure)
    current_version = read_record(store.db, "approval_version", task["task_id"] + ":" + str(current["revision"]))
    if current_version["revision"] != 1 or current_version["payload"] != grant:
        raise ValueError(failure)
    prefix = task["task_id"] + ":"
    versions = []
    for row in store.db.execute("SELECT key,revision,payload FROM runtime_records WHERE kind='task_version' AND substr(key,1,?)=?", (len(prefix), prefix)):
        value = json.loads(row["payload"])
        suffix = row["key"][len(prefix):]
        if (row["revision"] == 1 and suffix.isdigit() and isinstance(value, dict) and value.get("task_id") == task["task_id"]
                and hash_value(value.get("contract")) == value.get("contract_hash")):
            versions.append((int(suffix), row["key"], value))
    events = [(row["sequence"], row["event_key"], row["kind"], json.loads(row["payload"]))
        for row in store.db.execute("SELECT sequence,event_key,kind,payload FROM runtime_events WHERE task_id=? AND kind IN ('proposal','approval') ORDER BY sequence", (task["task_id"],))]
    if any(not isinstance(value, dict) for _, _, _, value in events):
        raise ValueError(failure)
    current_event = next((sequence for sequence, key, kind, value in events
        if key == task["task_id"] + ":approval:" + str(current["revision"]) and kind == "approval" and value == grant), None)
    proposal_events = [(sequence, int(key.rsplit(":", 1)[-1]), value) for sequence, key, kind, value in events
        if kind == "proposal" and key.startswith(task["task_id"] + ":proposal:") and key.rsplit(":", 1)[-1].isdigit()]
    current_proposal = next(((sequence, revision) for sequence, revision, value in reversed(proposal_events)
        if current_event is not None and sequence < current_event and value.get("proposal_hash") == task["proposal_hash"]
        and value.get("stage_ids") == task["proposal"]["stage_ids"]), None)
    if (current_proposal is None or not any(revision <= current_proposal[1] and value["contract_hash"] == task["contract_hash"]
            for revision, _, value in versions)):
        raise ValueError(failure)
    proposal_time = store.db.execute("SELECT created FROM runtime_events WHERE sequence=?", (current_proposal[0],)).fetchone()[0]
    if _captured_time(captured_at) >= _captured_time(proposal_time):
        raise ValueError(failure)
    _, original_stage, authority = _original_authority(store, task["task_id"], stage_id, proposal_hash,
        contract_hash=contract_hash, before=captured_at)
    return original_stage, {**authority,
        "superseded_by_contract_hash": task["contract_hash"], "superseded_by_proposal_hash": task["proposal_hash"],
        "current_approval_revision": current["revision"]}


def _check_plan_extension(plan, original, contract):
    from .profiles import validate_evidence_spec
    from .tasks import _scope, _within
    if hash_value(plan.get("checks")) != plan.get("plan_hash") or hash_value(original.get("checks")) != original.get("plan_hash"):
        raise ValueError("Check-plan source hash differs")
    current_checks = {item["id"]: item for item in plan["checks"]}
    if (len(current_checks) != len(plan["checks"])
            or any(current_checks.get(item["id"]) != item for item in original["checks"])):
        raise ValueError("Historical check plan was changed or weakened")
    for item in plan["checks"]:
        validate_evidence_spec(item, contract["profile"])
        if "scope" in item and not _within(_scope(item["scope"]), contract["scope"]):
            raise ValueError("Current check-plan scope exceeds approval")


def _proposal(store, task, plan, stage, snapshot):
    saved = read_record(store.db, "stage", task["task_id"] + ":" + stage["id"])["payload"] or {}
    captured = saved.get("state") == "COMPLETED"
    retained = read_record(store.db, "stage_implementation", task["task_id"] + ":" + stage["id"])["payload"]
    receipt = ((saved.get("proof") or {}).get("roles") or {}).get("implementation") if captured else retained
    source = "COMPLETED_STAGE_PROOF" if captured else "LATEST_STAGE_CAPTURE"
    if not captured and not receipt:
        receipt = read_record(store.db, "implementation", task["task_id"])["payload"]
        source = "LATEST_TASK_CAPTURE"
    if not receipt:
        return {"status": "NOT_CAPTURED", "limitation": "No implementation proposal was captured; no edits or application are inferred"}
    expected = {"contract_sha256": task["contract_hash"], "proposal_sha256": task["proposal_hash"],
                "plan_sha256": plan["plan_hash"], "workspace_id": store.workspace_id}
    if hash_value(plan.get("checks")) != plan["plan_hash"]:
        raise ValueError("Current check-plan hash differs")
    historical = None
    plan_supersession = None
    if captured and any(saved.get(key) != task[key] for key in ("contract_hash", "proposal_hash")):
        original_stage, historical = _superseded_stage(store, task, stage["id"],
            saved.get("contract_hash"), saved.get("proposal_hash"), captured_at=receipt.get("started_at_utc"))
        if retained and all(retained.get("bindings", {}).get(key) == value for key, value in expected.items()):
            receipt, captured, source, historical = retained, False, "LATEST_STAGE_CAPTURE", None
        else:
            stage = original_stage
            expected.update(contract_sha256=saved["contract_hash"], proposal_sha256=saved["proposal_hash"],
                plan_sha256=saved["proof"]["checks"]["plan_hash"])
    elif captured and saved["proof"]["checks"]["plan_hash"] != plan["plan_hash"]:
        if retained and all(retained.get("bindings", {}).get(key) == value for key, value in expected.items()):
            # Authenticate the superseded plan before preferring a new candidate;
            # a current receipt cannot turn a weakened old obligation into an extension.
            verify_receipt(receipt, require_current_policy=False)
            original_bytes = state_io(receipt["artifacts"]["request"]["path"]).read_bytes()
            if hashlib.sha256(original_bytes).hexdigest() != receipt["artifacts"]["request"]["sha256"]:
                raise ValueError("Original plan request changed after receipt verification")
            original_request = json.loads(original_bytes)
            original_files = {item["path"]: item for item in expand_packet(original_request["source_packet"])["files"]}
            original_plan = json.loads(original_files["_ase_context/execution_plan.json"]["content"])
            if original_plan.get("plan_hash") != saved["proof"]["checks"]["plan_hash"]:
                raise ValueError("Original check-plan identity differs")
            _check_plan_extension(plan, original_plan, task["contract"])
            receipt, captured, source = retained, False, "LATEST_STAGE_CAPTURE"
        else:
            expected["plan_sha256"] = saved["proof"]["checks"]["plan_hash"]
            plan_supersession = {"original_plan_hash": expected["plan_sha256"], "current_plan_hash": plan["plan_hash"]}
    if any(receipt.get("bindings", {}).get(key) != value for key, value in expected.items()):
        if not captured:
            return {"status": "NOT_CURRENT", "limitation": "Latest candidate belongs to another approved context"}
        raise ValueError("Captured proposal has stale task/proposal/plan/workspace identity")
    root = logical_path(receipt["artifact_root"])
    if not root.is_relative_to(logical_path(store.root / "tasks" / task["task_id"] / "sessions")):
        raise ValueError("Proposal receipt is outside this task's sessions")
    verified = verify_receipt(receipt, require_current_policy=False)
    request_bytes = state_io(receipt["artifacts"]["request"]["path"]).read_bytes()
    if hashlib.sha256(request_bytes).hexdigest() != receipt["artifacts"]["request"]["sha256"]:
        raise ValueError("Proposal request changed after receipt verification")
    request = json.loads(request_bytes)
    actual_stage = request.get("handoff", {}).get("stage", {})
    if actual_stage.get("id") != stage["id"]:
        if not captured:
            return {"status": "NOT_CAPTURED", "limitation": "Latest candidate belongs to a different stage"}
        raise ValueError("Captured proposal stage differs")
    if request.get("role") != "implementation" or receipt.get("role") != "implementation":
        raise ValueError("Proposal role differs")
    if logical_path(request["project"]["root"]) != logical_path(store.repo) or request["project"].get("project_id") != store.project_id:
        raise ValueError("Proposal project/workspace differs")
    if "scope" in actual_stage and actual_stage["scope"] != stage["scope"]:
        raise ValueError("Proposal stage scope differs")
    supplied = {item["path"]: item for item in expand_packet(request["source_packet"])["files"]}
    contract = supplied.get("_ase_context/contract.json")
    if historical and contract is None:
        raise ValueError("Historical proposal contract source is unavailable")
    if contract and hash_value(json.loads(contract["content"])) != expected["contract_sha256"]:
        raise ValueError("Proposal contract source differs")
    observed = supplied.get("_ase_context/source_changes.json")
    if observed:
        observed = json.loads(observed["content"])
        if observed and observed.get("current_fingerprint") != receipt["bindings"]["fingerprint"]:
            raise ValueError("Proposal source fingerprint differs from captured observation")
    captured_plan = supplied.get("_ase_context/execution_plan.json")
    if captured_plan:
        captured_plan = json.loads(captured_plan["content"])
        if captured_plan.get("plan_hash") != expected["plan_sha256"] or hash_value(captured_plan.get("checks")) != expected["plan_sha256"]:
            raise ValueError("Proposal check-plan source differs")
        if plan_supersession:
            _check_plan_extension(plan, captured_plan, task["contract"])
    elif historical or plan_supersession:
        raise ValueError("Historical proposal check-plan source is unavailable")
    payload = verified["result"].get("payload") or {}
    if not isinstance(payload.get("edits"), list):
        return {"status": "UNSUPPORTED_LEGACY_FORMAT", "execution_id": receipt["execution_id"],
                "limitation": "Verified implementation session did not capture a structured edit proposal"}
    edits = []
    for edit in payload["edits"]:
        path = Path(edit["path"])
        if path.is_absolute() or ".." in path.parts or any(part.lower() == ".git" for part in path.parts):
            raise ValueError("Proposal edit path escapes source")
        name = path.as_posix()
        if not any(path == Path(resource) or path.is_relative_to(Path(resource)) for resource in stage["scope"]["resources"]):
            raise ValueError("Proposal edit exceeds its declared stage")
        data = edit["content"].encode()
        edits.append({"path": name, "operation": edit.get("operation", "write"),
            "before_sha256": edit.get("before_sha256") or None,
            "captured_preimage_sha256": (supplied.get(name) or {}).get("sha256"),
            "after_sha256": None if edit.get("operation") == "delete" else hashlib.sha256(data).hexdigest(),
            "proposed_content_bytes": len(data), "current_source_sha256": snapshot["files"].get(name)})
    return {"status": "HISTORICAL_VERIFIED_CAPTURE" if historical or plan_supersession else "VERIFIED_CAPTURE", "proposal_only": True,
        "current_identity": historical is None and plan_supersession is None, **({"supersession": historical} if historical else {}),
        **({"plan_supersession": plan_supersession} if plan_supersession else {}),
        "limitation": "A proposal is neither proof of applied edits nor the original task baseline; proposed bodies are omitted",
        "execution_id": receipt["execution_id"], "evidence_kind": verified["evidence_kind"], "capture_source": source,
        "receipt_sha256": hash_value(receipt), "request_sha256": receipt["request_sha256"],
        "bindings": receipt["bindings"], "edits": edits}


def _stage_context(store, task, stage):
    """Observe current approval/checkpoint facts; never infer application."""
    from .tasks import TaskService, approval_for_stage
    tasks = TaskService(store)
    approval = {"status": "CURRENT_APPROVAL", "task_id": task["task_id"], "stage_id": stage["id"],
        "contract_hash": task["contract_hash"], "proposal_hash": task["proposal_hash"],
        "mode": "inherited" if task.get("inherited_approval") and not task.get("proposal") else "explicit",
        "declared_approved_stage_ids": task["approved_stage_ids"]}
    try:
        record = approval_for_stage(store.db, task["task_id"], stage["id"])
        approval.update(approval_record_revision=record["revision"], approval_record_sha256=hash_value(record["payload"]))
        current = tasks.require_approval(task["task_id"], stage["id"], allow_terminal=True, allow_stopped=True)
        if any(current[key] != task[key] for key in ("contract_hash", "proposal_hash", "approved_stage_ids")):
            raise ValueError("Approval changed during packet preparation")
        if approval_for_stage(store.db, task["task_id"], stage["id"]) != record:
            raise ValueError("Approval record changed during packet preparation")
    except ValueError as error:
        approval.update(status="NOT_CURRENTLY_EXECUTABLE", reason=redact(error))
    cycle = read_record(store.db, "cycle", task["task_id"] + ":" + stage["id"])["payload"] or {}
    if cycle and any(cycle.get(key) != task[key] for key in ("contract_hash", "proposal_hash")):
        progress = {"status": "NOT_CURRENT", "limitation": "Saved coordinator cycle belongs to another context"}
    else:
        progress = {key: cycle[key] for key in ("phase", "candidate_id", "contract_hash", "proposal_hash", "fingerprint") if key in cycle}
        progress["status"] = "RECORDED_CHECKPOINT" if cycle else "NOT_RECORDED"
    progress["limitation"] = "A coordinator checkpoint is not proof of applied edits, action execution, or semantic acceptance"
    return approval, progress


def action_packet_evidence(store, task, plan, snapshot, *, stage_id=None):
    """Supply declared current-stage/ancestor evidence without replay or guessing."""
    from .operations import OperationLedger
    by_id = {stage["id"]: stage for stage in task["contract"]["stages"]}
    selected = set()
    def include(name):
        if name not in selected:
            selected.add(name)
            for dependency in by_id[name]["depends_on"]:
                include(dependency)
    if stage_id is None:
        selected.update(by_id)
    else:
        include(stage_id)
    files, missing, entries, total_log = [], [], [], 0
    ledger = OperationLedger(store)

    def log_file(proof_ref, operation_id, observer_task, label, observation=None):
        nonlocal total_log
        path_text, digest = proof_ref.rsplit("#sha256=", 1)
        if not re.fullmatch(r"[0-9a-f]{64}", digest) or not re.fullmatch(r"[0-9a-f]{64}", operation_id):
            raise ValueError("Action log requires full immutable hashes")
        path = logical_path(path_text)
        expected = logical_path(store.root / "tasks" / observer_task / "actions" / operation_id)
        if path.parent != expected or not re.fullmatch(r"[0-9a-f]{32}\.log", path.name):
            raise ValueError("Action log is outside the exact task/action folder")
        physical = state_io(path)
        if physical.stat().st_size > MAX_READ:
            raise ValueError("Action log exceeds integrity-read bound")
        observed = hashlib.sha256()
        body = bytearray()
        with physical.open("rb") as stream:
            for chunk in iter(lambda: stream.read(65536), b""):
                observed.update(chunk)
                if len(body) < MAX_LOG:
                    body.extend(chunk[:MAX_LOG - len(body)])
        if observed.hexdigest() != digest:
            raise ValueError("Action log hash differs from immutable evidence: " + label)
        if observation is not None:
            payload = json.loads(bytes(body).decode("utf-8"))
            if not isinstance(payload, dict) or any(payload.get(key) != observation.get(key)
                    for key in ("exists", "resource_id", "absence_confirmed", "provider_attempt_id")):
                raise ValueError("Observation claim differs from hashed JSON log")
        content = redact(body.decode("utf-8", "replace"))
        content = content.encode()[:MAX_LOG].decode("utf-8", "ignore")
        if total_log + len(content.encode()) > MAX_TOTAL_LOG or len(files) >= 32:
            raise ValueError("Action log packet budget exhausted")
        total_log += len(content.encode())
        virtual = "_ase_context/action_logs/" + label + ".txt"
        files.append(_file(virtual, content))
        truncated = physical.stat().st_size > len(body)
        if truncated:
            missing.append("Action log truncated: " + label)
        return {"proof_ref": proof_ref, "packet_path": virtual, "original_sha256": digest,
                "supplied_sha256": files[-1]["sha256"], "truncated": truncated, "content_is_redacted": True}

    for stage in task["contract"]["stages"]:
        if stage["id"] not in selected:
            continue
        entry = {"stage_id": stage["id"], "actions": []}
        try:
            entry["implementation_proposal"] = _proposal(store, task, plan, stage, snapshot)
        except (ValueError, OSError, KeyError, TypeError) as error:
            entry["implementation_proposal"] = {"status": "INVALID", "reason": redact(error)}
            missing.append("Implementation proposal unavailable: " + stage["id"] + ": " + redact(error))
        for action in stage.get("actions", []):
            name = stage["id"] + "/" + action["id"]
            item = {"id": action["id"], "state": "NOT_STARTED", "observations": []}
            entry["actions"].append(item)
            intent = {"project_id": store.project_id, "workspace_id": store.workspace_id,
                      "action": action, "effect_scope": stage["scope"]}
            try:
                declared = hash_value({"task": task["task_id"], "stage": stage["id"], "action": action["id"]})
                operation = ledger.lookup(task["task_id"], stage["id"], declared, intent)
                if operation is None:
                    continue
                operation_id = operation["action_id"]
                if operation.get("intent_hash") != hash_value(operation["intent"]):
                    raise ValueError("Operation intent hash differs")
                item.update({"state": operation["state"], "operation_id": operation_id,
                    "attempt_number": operation.get("attempt_number", 0), "ever_attempted": operation.get("ever_attempted", False),
                    "ever_applied": operation.get("ever_applied", operation["state"] == "APPLIED"),
                    "intent_sha256": operation["intent_hash"], "declared_action_sha256": hash_value(action),
                    "intent": operation["intent"], "effect_scope": operation.get("effect_scope"),
                    "submission_binding": operation.get("submission_binding"),
                    "consumed_provider_attempt_ids": operation.get("consumed_provider_attempt_ids", []),
                    "execution_history_coverage": "Current attempt receipt; prior attempts remain in durable operation events and are not expanded here",
                    "execution_coverage": "LEGACY_NOT_RECORDED"})
                capture = operation.get("execution_receipt")
                if capture:
                    item["execution_coverage"] = "UNVERIFIED_CAPTURE"
                    if capture.get("sha256") != hash_value({key: value for key, value in capture.items() if key != "sha256"}):
                        raise ValueError("Execution receipt metadata hash differs")
                    context = capture.get("context") or {}
                    actor, original_stage, original_action = _execution_origin(store, ledger, task, operation, context)
                    if context.get("source_files_sha256") != hash_value(context.get("source_files")):
                        raise ValueError("Execution source observation hash differs")
                    if (not isinstance(context.get("source_files"), dict)
                            or not re.fullmatch(r"[0-9a-f]{64}", context.get("source_fingerprint_before", ""))
                            or any(not isinstance(key, str) or not isinstance(value, str) for key, value in context["source_files"].items())):
                        raise ValueError("Execution source observation is malformed")
                    if capture.get("attempt_number") != operation.get("attempt_number") or capture.get("log_integrity") != "VERIFIED":
                        raise ValueError("Execution attempt/integrity differs")
                    execution = capture["execution"]
                    if logical_path(execution["cwd"]) != logical_path(store.repo):
                        raise ValueError("Execution workspace path differs")
                    validate_command_argv(execution, original_action["argv"])
                    log = log_file(execution["log"] + "#sha256=" + execution["log_sha256"], operation_id, actor, name + "/execution")
                    item["execution_receipt"] = {**capture, "log_evidence": log}
                    item["execution_coverage"] = "RECORDED"
                else:
                    item["limitation"] = "Legacy operation has no captured execution receipt; no command or success is inferred or replayed"
                rows = store.db.execute("SELECT sequence,payload FROM runtime_events WHERE kind='operation_reconciled' AND event_key LIKE ? ORDER BY sequence",
                    ("operation:" + operation_id + ":observation:%",)).fetchmany(33)
                if len(rows) > 32:
                    missing.append("Action observation history truncated: " + name)
                observations = [(row[0], json.loads(row[1]).get("observation"), json.loads(row[1]).get("attempt_number", 0),
                                 json.loads(row[1]).get("state")) for row in rows[:32]]
                current_observation = operation.get("observation")
                if current_observation and not any(observation == current_observation for _, observation, _, _ in observations):
                    observations.append(("current", current_observation, operation.get("attempt_number", 0), operation["state"]))
                for sequence, observation, attempt, observed_state in observations:
                    if not isinstance(observation, dict) or observation.get("observing_workspace_id") != store.workspace_id:
                        raise ValueError("Action observation has no matching scoped caller/recipe identity")
                    observer = identifier(observation["observing_task_id"], "Observation actor")
                    if ledger.root_task_id(observer) != ledger.root_task_id(task["task_id"]):
                        raise ValueError("Observation belongs to another managed root")
                    provenance = observation.get("provenance")
                    if provenance is not None:
                        if (provenance.get("kind") != "observation" or provenance.get("sha256") !=
                                hash_value({key: value for key, value in provenance.items() if key != "sha256"})):
                            raise ValueError("Observation declaration capture hash differs")
                        owner, observer_stage, recipe = _execution_origin(store, ledger, task, operation, provenance)
                        if (owner != observer or observer_stage["id"] != observation["observing_stage_id"]
                                or hash_value(recipe) != observation["action_hash"]
                                or hash_value(recipe["observation_argv"]) != provenance.get("observation_argv_sha256")):
                            raise ValueError("Observation caller differs from captured declaration")
                        command = provenance["command_receipt"]
                        validate_command_argv(command, recipe["observation_argv"])
                        if (provenance.get("command_receipt_sha256") != hash_value(command)
                                or logical_path(command["cwd"]) != logical_path(store.repo)
                                or command["log"] + "#sha256=" + command["log_sha256"] != observation["proof_ref"]
                                or command.get("exit_code") != 0 or any(command.get(key) for key in ("error", "timed_out", "cancelled"))):
                            raise ValueError("Observer command receipt differs from its captured origin/log")
                        origin = ("CURRENT_DECLARATION" if owner == task["task_id"] and observer_stage["id"] == stage["id"]
                                  and hash_value(recipe) == hash_value(action) else "HISTORICAL_VERIFIED_OBSERVER")
                    else:
                        origins = [(task["task_id"], stage["id"], action),
                                   (operation["task_id"], operation["stage_id"], operation["intent"]["action"])]
                        if capture:
                            origins.append((actor, original_stage["id"], original_action))
                        matched = next((index for index, (owner, stage_name, recipe) in enumerate(origins)
                            if owner == observer and stage_name == observation.get("observing_stage_id")
                            and hash_value(recipe) == observation.get("action_hash")), None)
                        if matched is None:
                            item.setdefault("omitted_historical_observations", []).append({"sequence": sequence,
                                "observing_task_id": observer, "observing_stage_id": observation.get("observing_stage_id"),
                                "action_hash": observation.get("action_hash"), "attempt_number": attempt,
                                "limitation": "Legacy intermediate declaration was not captured; log body omitted, not supplied or asserted verified"})
                            continue
                        origin = "CURRENT_DECLARATION" if matched == 0 else "HISTORICAL_ORIGIN"
                    log = log_file(observation["proof_ref"], operation_id, observer, name + "/observation-" + str(sequence), observation)
                    item["observations"].append({"sequence": sequence, "origin": origin,
                        "attempt_number": attempt, "state_after_observation": observed_state,
                        "observation": observation, "log_evidence": log})
                item["current_observation"] = current_observation
                item["current_observation_log_verified"] = any(item["observation"] == current_observation for item in item["observations"])
            except (ValueError, OSError, KeyError, TypeError) as error:
                missing.append("Action evidence unavailable: " + name + ": " + redact(error))
                item["coverage_error"] = redact(error)
        entries.append(entry)
    other_proposals, inflight_dependents = [], []
    def depends_on(name, prerequisite):
        return any(dependency == prerequisite or depends_on(dependency, prerequisite) for dependency in by_id[name]["depends_on"])
    for stage in task["contract"]["stages"]:
        if stage["id"] in selected or stage["id"] not in task["approved_stage_ids"]:
            continue
        approval, progress = _stage_context(store, task, stage)
        entry = {"stage_id": stage["id"], "stage": {key: stage[key] for key in ("id", "scope", "acceptance_ids", "depends_on")},
            "current_approval": approval, "cycle_observation": progress,
            "purpose": "Cross-stage change attribution only; this inventory supplies no action logs or acceptance verdict"}
        try:
            entry["implementation_proposal"] = _proposal(store, task, plan, stage, snapshot)
        except (ValueError, OSError, KeyError, TypeError) as error:
            entry["implementation_proposal"] = {"status": "INVALID", "reason": redact(error)}
            missing.append("Other approved stage proposal unavailable: " + stage["id"] + ": " + redact(error))
        other_proposals.append(entry)
        if (stage_id is not None and depends_on(stage["id"], stage_id)
                and progress.get("phase") in {"IMPLEMENTING", "CHECKING", "VERIFYING"}):
            inflight_dependents.append(stage["id"])
    manifest = {"version": 1, "kind": "action_and_candidate_evidence", "selected_stage": stage_id,
        "current_source_fingerprint": snapshot["fingerprint"], "stages": entries, "metadata_strings_redacted": True,
        "other_approved_stage_proposals": other_proposals,
        "verification_context": {"purpose": "Current selected-stage assessment, including fresh prerequisite reassessment when a dependent is in flight",
            "stage_id": stage_id, "gated_acceptance_ids": by_id[stage_id]["acceptance_ids"] if stage_id else None,
            "inflight_dependents": inflight_dependents,
            "limitation": "Other-stage proposals explain separately authorized source changes; they neither extend selected-stage ownership nor establish other-stage acceptance"},
        "capture_hashes_refer_to_original_records": True,
        "limitation": "Executed observations and proposal captures support independent judgment; neither local ledger state nor protocol fixtures establish semantic approval"}
    try:
        content = encode(_redacted(manifest))
    except ValueError as error:
        missing.append("Action evidence metadata could not be safely redacted: " + str(error))
        content = encode({"version": 1, "status": "OMITTED_REDACTION_COLLISION", "stages": []})
    if len(content.encode()) > MAX_MANIFEST:
        missing.append("Action evidence manifest exceeds packet bound")
        content = encode({"version": 1, "status": "OMITTED_OVER_LIMIT", "stages": [], "limitation": "Full evidence was not supplied"})
    files.insert(0, _file("_ase_context/action_evidence.json", content))
    return files, list(dict.fromkeys(missing))
