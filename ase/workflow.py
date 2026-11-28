"""Supervised packet implementation, actual checks and bounded repair cycles."""
from datetime import datetime
from contextlib import contextmanager, nullcontext
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import time
import threading
import tempfile
import uuid
import weakref
from .evidence.compact import MAX_EXPANDED, expand_packet
from .protocol import MAX_DECLARED_PACKET_BYTES

from .adapters import get_adapter
from .gates import GateService, source_resources, source_snapshot, stage_checks, verification_context_hash, verification_context_hashes
from .profiles import validate_access, validate_evidence_spec, has_software, evaluate_contract_profiles
from .runner import Runner, verify_receipt
from .state import append_event, encode, hash_value, read_record, text, transaction, write_record
from .store import now
from .projects import _safe_state
from .native_paths import logical_path, state_io
from .tasks import (DIAGNOSTIC_APPROVAL_ACTION, TaskService, approval_for_stage, current_candidate_blocker,
                    diagnostic_approval_pending, proposal_sequence, record_baseline)
from .recovery import initial_history, bind_stage, canonical_failures, progress_tokens, group_tokens, original_case_progress, task_bound_contexts, initialize_timing, task_active_seconds


def _object(properties):
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}


# Only genuine independent sessions may replace completed stage proof on a policy refresh.
_REFRESH_EVIDENCE = frozenset({"independent_cli"})
STR = {"type": "string"}
STRINGS = {"type": "array", "items": STR}
AC_SCHEMA = _object({"criteria": {"type": "array", "items": _object({
    "id": STR, "status": {"type": "string", "enum": ["PASS", "FAIL", "UNVERIFIED", "BLOCKED", "NOT_APPLICABLE"]},
    "expected": STR, "evidence": STRINGS, "method": STR,
    "limitations": {**STRINGS, "description": "Nonblocking limits and explicitly pending later delivery obligations. They do not establish overall completion."},
    "missing_coverage": {**STRINGS, "description": "Unmet proof required by THIS criterion at THIS gate only. Any nonempty value blocks a mandatory criterion, including status PASS. Put nonblocking limits and later-phase delivery prerequisites in limitations."}})}})
REVIEW_SCHEMA = _object({"findings": {"type": "array", "items": _object({
    "severity": {"type": "string", "enum": ["P0", "P1", "P2", "P3"]}, "path": STR,
    "line": {"type": "integer", "minimum": 1}, "message": STR})}, "summary": STR})
EDIT_SCHEMA = _object({"edits": {"type": "array", "maxItems": 100, "items": _object({
    "path": STR, "operation": {"type": "string", "enum": ["write", "delete"]},
    "before_sha256": STR, "content": STR})}, "summary": STR})
DIAGNOSTIC_SCHEMA = _object({"cause": STR, "recommendation": STR, "requires_new_approval": {"type": "boolean"}})


@contextmanager
def _exclusive(path):
    """OS releases this advisory lock even when the coordinator crashes."""
    _safe_state(path)
    state_io(path.parent).mkdir(parents=True, exist_ok=True)
    physical = state_io(path)
    if physical.is_symlink() or (physical.exists() and physical.stat().st_nlink > 1):
        raise ValueError("Invalid execution lock")
    with physical.open("a+b") as stream:
        try:
            stream.seek(0)
            if not stream.read(1):
                stream.write(b"0")
                stream.flush()
            stream.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise ValueError("Another execution owns this root task") from None
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _target(root, value):
    from .project_local import is_private_path
    if not isinstance(value, str) or not value or Path(value).is_absolute() or ".." in Path(value).parts:
        raise ValueError("Path escapes approved scope")
    candidate = root / value
    if is_private_path(value) or (candidate.resolve().is_relative_to(root) and is_private_path(candidate.resolve().relative_to(root))):
        raise ValueError("Private project-local state is outside source edit/packet scope")
    if (candidate.is_symlink() or not candidate.resolve().is_relative_to(root)
            or any(part.lower() == ".git" for part in candidate.resolve().relative_to(root).parts)):
        raise ValueError("Path escapes approved scope")
    if candidate.is_file() and candidate.stat().st_nlink > 1:
        raise ValueError("Hardlinked source cannot be mutated")
    return candidate


def apply_edits(root, scope, edits, *, application=None):
    from .edit_application import plan_edits, apply_planned_edits
    plan = application if application is not None else plan_edits(root, scope, edits, uuid.uuid4().hex, _target)
    return apply_planned_edits(root, scope, edits, plan, _target)


class WorkflowController:
    def __init__(self, store, runner=None):
        self.store = store
        self.tasks = TaskService(store)
        self.runner = runner
        self._ownership_state = threading.local()

    @contextmanager
    def _ownership(self, task_id):
        root_id = self._root_task(task_id)
        held = getattr(self._ownership_state, "held", set())
        if root_id in held:
            yield
            return
        with _exclusive(self.store.root / "tasks" / root_id / "execution.lock"):
            self._ownership_state.held = held | {root_id}
            try:
                yield
            finally:
                self._ownership_state.held = held

    def _root_task(self, task_id):
        seen = set()
        while True:
            if task_id in seen:
                raise ValueError("Parent task cycle")
            seen.add(task_id)
            task = self.tasks.snapshot(task_id)
            parent = task["contract"].get("parent_task_id")
            if not parent:
                return task_id
            task_id = parent

    def _repair_readiness(self, task_id, stage_id, *, expected_plan_sha256=None):
        """Pin causal readiness and its exact saved plan under one writer."""
        from .testing.bugfix import repair_readiness, repair_targets
        from .testing.adequacy import TestAdequacy
        def with_commands(blocked):
            revision = read_record(self.store.db, 'test_adequacy_plan', task_id)['revision']
            return {**blocked, 'next_action': blocked['next_action'] + (
                ' Save the bugfix dossier (scenarios and bugfixes with the causal trace): `ase adequacy plan '
                f'{task_id} --expected-revision {revision} --file <plan.json>`, then `ase task resume {task_id}`.')}
        with transaction(self.store.db):
            readiness = repair_readiness(self.store, task_id, stage_id)
            if not readiness['ready']:
                return with_commands(readiness)
            targets = repair_targets(self.tasks.snapshot(task_id)['contract'], stage_id)
            plan_sha256 = (TestAdequacy(self.store).snapshot(task_id)['payload']['test_plan_sha256']
                if targets else None)
            if expected_plan_sha256 is not None and plan_sha256 != expected_plan_sha256:
                return with_commands({'ready': False, 'reason': 'Bugfix investigation changed before causal-repair launch',
                    'needs_owner_input': False,
                    'next_action': 'Continue authorized investigation and save current causal evidence before proposing another repair.'})
            return {**readiness, **({'test_plan_sha256': plan_sha256} if plan_sha256 is not None else {})}

    def reserve_cycle(self, task_id, stage_id, *, repair_plan_sha256=None):
        self.tasks.require_approval(task_id, stage_id)
        root_id = self._root_task(task_id)
        with transaction(self.store.db) as db:
            if repair_plan_sha256 is not None:
                readiness = self._repair_readiness(task_id, stage_id, expected_plan_sha256=repair_plan_sha256)
                if not readiness['ready']:
                    raise ValueError(readiness['reason'])
            if read_record(db, "stop", task_id)["payload"] or read_record(db, "stop", root_id)["payload"]:
                raise ValueError("Task is paused by a stop request")
            root = self.tasks.snapshot(root_id)
            policy = root["contract"]["execution_policy"]
            record = read_record(db, "budget", root_id)
            value = record["payload"] or {"task_cycles": 0, "stages": {}, "diagnostics": {}, "failures": {}}
            recovery_record, history, groups = self._recovery_context(db, task_id, stage_id)
            initialize_timing(value, record["updated"])
            owners = task_bound_contexts(self.store, history, self._ancestry(task_id), value, stage_id, updated=record["updated"])
            limits = history["hard_limits"]
            if limits.get("active_time_budget_seconds") is not None and value.get("active_seconds", 0) >= limits["active_time_budget_seconds"]:
                raise ValueError("Active time budget exhausted")
            key = task_id + ":" + stage_id
            count = max([value["stages"].get(key, 0)] + [history["groups"][name]["cycles"] for name in groups])
            if ((limits.get("max_stage_cycles") is not None and count >= limits["max_stage_cycles"])
                    or (limits.get("max_task_cycles") is not None and value["task_cycles"] >= limits["max_task_cycles"])):
                raise ValueError("Implementation cycle budget exhausted")
            for owner, bounds, local_groups in owners:
                local = bounds["hard_limits"]
                if local.get("active_time_budget_seconds") is not None and task_active_seconds(self.store, owner, value) >= local["active_time_budget_seconds"]:
                    raise ValueError("Active time budget exhausted: " + owner["task_id"])
                count_local = max(bounds["groups"][name]["cycles"] for name in local_groups)
                if ((local.get("max_stage_cycles") is not None and count_local >= local["max_stage_cycles"])
                        or (local.get("max_task_cycles") is not None and bounds["task_cycles"] >= local["max_task_cycles"])):
                    raise ValueError("Implementation cycle budget exhausted: " + owner["task_id"])
            if policy.get("recovery_mode", "fixed") == "progress" and any(history["groups"][name]["stopped"] for name in groups):
                raise ValueError("Corrective probe made no verified progress; automatic mutation is stopped")
            if any(history["groups"][name].get("probe_inflight") for name in groups):
                raise ValueError("Corrective probe is already inflight; reconcile its existing candidate before mutation")
            if any(history["groups"][name].get("diagnostic_inflight") for name in groups):
                raise ValueError("Diagnostic has no validated completion; fresh original progress is required before mutation")
            if any(history["groups"][name]["probe_pending"] and not self._probe_authorized(task_id, stage_id, name, history["groups"][name]) for name in groups):
                raise ValueError("Corrective probe requires a validated diagnostic authorization for the current evidence")
            if policy.get("recovery_mode", "fixed") == "progress" and any(
                    history["groups"][name]["stagnation"] >= policy.get("stagnation_limit", 3)
                    and not history["groups"][name]["probe_pending"] for name in groups):
                raise ValueError("Stagnation requires reassessment and diagnosis before another candidate")
            value["task_cycles"] += 1
            value["stages"][key] = count + 1
            for owner, bounds, local_groups in owners:
                count_local = max(bounds["groups"][name]["cycles"] for name in local_groups)
                bounds["task_cycles"] += 1
                for name in local_groups:
                    bounds["groups"][name]["cycles"] = count_local + 1
            candidate_id = uuid.uuid4().hex
            corrective = any(history["groups"][name]["probe_pending"] for name in groups)
            for name in groups:
                history["groups"][name]["cycles"] = count + 1
                if corrective:
                    history["groups"][name].update(probe_pending=False, probe_authorization=None, probe_inflight={"candidate_id": candidate_id,
                        "task_id": task_id, "stage_id": stage_id, "task_cycle": value["task_cycles"]})
            task = self.tasks.snapshot(task_id)
            cycle = read_record(db, "cycle", key)
            write_record(db, "cycle", key, {"phase": "IMPLEMENTING", "candidate_id": candidate_id,
                "corrective_probe": corrective, "contract_hash": task["contract_hash"], "proposal_hash": task["proposal_hash"]}, expected_revision=cycle["revision"])
            write_record(db, "recovery", root_id, history, expected_revision=recovery_record["revision"])
            write_record(db, "budget", root_id, value, expected_revision=record["revision"])
        return {"task_cycles": value["task_cycles"], "stage_cycles": count + 1, "candidate_id": candidate_id, "corrective_probe": corrective}

    def _recovery_context(self, db, task_id, stage_id=None):
        root_id = self._root_task(task_id)
        record = read_record(db, "recovery", root_id)
        original = read_record(db, "task_version", root_id + ":1")["payload"]
        history = record["payload"] or initial_history((original or self.tasks.snapshot(root_id))["contract"])
        limits = history.setdefault("hard_limits", {})
        for key, value in self.tasks.snapshot(root_id)["contract"]["execution_policy"].items():
            if key in {"max_stage_cycles", "max_task_cycles", "max_diagnostic_passes", "active_time_budget_seconds"} and value is not None:
                limits[key] = min(value, limits.get(key, value))
        budget = (read_record(db, "budget", root_id)["payload"] or {})
        for name, group in history["groups"].items():
            group["cycles"] = max(group["cycles"], budget.get("stages", {}).get(root_id + ":" + name, 0))
            group["diagnostics"] = max(group["diagnostics"], budget.get("diagnostics", {}).get(root_id + ":" + name, 0))

        def bind(current_id, current_stage):
            contract = self.tasks.snapshot(current_id)["contract"]
            parent = contract.get("parent_task_id")
            inherited = bind(parent, contract["parent_stage_id"]) if parent else None
            return bind_stage(history, current_id, contract, current_stage, root_id=root_id, parent_groups=inherited)

        groups = bind(task_id, stage_id) if stage_id is not None else []
        if record["payload"] is None:
            # Old v2 coordinators stored raw stage names and reason hashes only.
            # Import conservatively once; missing old progress proof earns no reset.
            imported = {name: {"cycles": 0, "diagnostics": 0, "failures": 0} for name in history["groups"]}
            for field, target in (("stages", "cycles"), ("diagnostics", "diagnostics"), ("failures", "failures")):
                for old_key, count in budget.get(field, {}).items():
                    parts = old_key.split(":")
                    if len(parts) < 2:
                        inherited = list(history["groups"])
                    else:
                        try:
                            if self._root_task(parts[0]) != root_id:
                                continue
                            inherited = bind(parts[0], parts[1])
                        except (ValueError, KeyError):
                            inherited = list(history["groups"])
                    for name in inherited:
                        imported[name][target] += count
            for name, counters in imported.items():
                group = history["groups"][name]
                group["cycles"] = max(group["cycles"], counters["cycles"])
                group["diagnostics"] = max(group["diagnostics"], counters["diagnostics"])
                group["legacy_failures"] = counters["failures"]
                group["stagnation"] = max(group["stagnation"], counters["failures"])
        return record, history, groups

    def charge_time(self, task_id, seconds, *, attributed=True):
        if seconds < 0:
            raise ValueError("Elapsed time cannot be negative")
        root_id = self._root_task(task_id)
        with transaction(self.store.db) as db:
            record = read_record(db, "budget", root_id)
            value = record["payload"] or {"task_cycles": 0, "stages": {}, "diagnostics": {}, "failures": {}}
            initialize_timing(value, record["updated"])
            value["active_seconds"] = value.get("active_seconds", 0) + seconds
            if attributed:
                value["active_by_task"][task_id] = value["active_by_task"].get(task_id, 0) + seconds
            else:
                value["unattributed_active_seconds"] += seconds
                value["unattributed_before"] = now()
            write_record(db, "budget", root_id, value, expected_revision=record["revision"])

    def _available(self, task_id, timeout):
        root_id = self._root_task(task_id)
        if self._stop_requested(task_id):
            raise ValueError("Task or ancestor is paused or cancelled")
        with transaction(self.store.db) as db:
            record = read_record(db, "budget", root_id)
            value = record["payload"] or {"task_cycles": 0, "stages": {}, "diagnostics": {}, "failures": {}}
            initialize_timing(value, record["updated"])
            recovery_record, history, _ = self._recovery_context(db, task_id)
            owners = task_bound_contexts(self.store, history, self._ancestry(task_id), value, updated=record["updated"])
            remaining = timeout
            budgets = [(history["hard_limits"], value.get("active_seconds", 0))]
            budgets.extend((bounds["hard_limits"], task_active_seconds(self.store, owner, value)) for owner, bounds, _ in owners)
            for limits, used in budgets:
                limit = limits.get("active_time_budget_seconds")
                if limit is not None:
                    if used >= limit:
                        raise ValueError("Active time budget exhausted")
                    remaining = min(remaining, limit - used)
            if value != record["payload"]:
                write_record(db, "budget", root_id, value, expected_revision=record["revision"])
            if history != recovery_record["payload"]:
                write_record(db, "recovery", root_id, history, expected_revision=recovery_record["revision"])
        return remaining

    def _stop_requested(self, task_id):
        return any(task["state"] == "CANCELLED" or read_record(self.store.db, "stop", task["task_id"])["payload"]
                   for task in self._ancestry(task_id))

    def _ancestry(self, task_id):
        tasks, seen = [], set()
        while task_id:
            if task_id in seen:
                raise ValueError("Parent task cycle")
            seen.add(task_id)
            task = self.tasks.snapshot(task_id)
            tasks.append(task)
            task_id = task["contract"].get("parent_task_id")
        return tasks

    def _command_authority(self, task_id, spec, stage_id, *, allow_terminal=False):
        """Current declared recipe and the full inherited authority it relies on."""
        with transaction(self.store.db):
            task = self.tasks.require_approval(task_id, stage_id, allow_terminal=allow_terminal)
            stage = next(s for s in task["contract"]["stages"] if s["id"] == stage_id)
            plan = self.tasks.execution_plan(task_id)
            declared = [*stage_checks(task["contract"], plan["checks"], stage_id), *stage.get("actions", [])]
            if spec not in declared or not self._stage_permits_command(stage, spec):
                raise ValueError("Command differs from the current approved stage")
            if self._stop_requested(task_id):
                raise ValueError("Task or ancestor is paused or cancelled")
            return self._authority_binding(task_id, stage_id)

    def _authority_binding(self, task_id, stage_id=None):
        ancestry = []
        for item in self._ancestry(task_id):
            approval = approval_for_stage(self.store.db, item["task_id"], stage_id)
            ancestry.append({"task_id": item["task_id"], "contract_hash": item["contract_hash"], "proposal_hash": item["proposal_hash"],
                "approval_revision": approval["revision"],
                "approval_mode": "inherited" if item.get("inherited_approval") and not item.get("proposal") else "explicit"})
            stage_id = item["contract"].get("parent_stage_id")
        return {"plan_hash": self.tasks.execution_plan(task_id)["plan_hash"], "ancestry": ancestry}

    def _stage_authority(self, task, stage):
        with transaction(self.store.db):
            current = self.tasks.require_approval(task["task_id"], stage["id"])
            if (current["contract_hash"] != task["contract_hash"] or current["proposal_hash"] != task["proposal_hash"]
                    or next(s for s in current["contract"]["stages"] if s["id"] == stage["id"]) != stage):
                raise ValueError("Approved stage context changed; discard the stale candidate")
            return self._authority_binding(task["task_id"], stage["id"])

    @staticmethod
    def _stage_permits_command(stage, spec):
        if "access" in spec and validate_access(spec["access"])["target"] not in stage["scope"]["resources"]:
            return False
        if "scope" in spec:
            from .tasks import _scope, _within
            if not _within(_scope(spec["scope"]), stage["scope"]):
                return False
        return True

    def _command_control(self, task_id, spec, stage_id, *, allow_terminal=False, prerequisite_binding=None, authority_binding=None):
        current_binding = self._command_authority(task_id, spec, stage_id, allow_terminal=allow_terminal)
        binding = current_binding if authority_binding is None else authority_binding
        if current_binding != binding:
            raise ValueError("Approved command authority changed during preparation")

        def current():
            return self._command_authority(task_id, spec, stage_id, allow_terminal=allow_terminal) == binding

        def cancelled():
            try:
                return not current()
            except ValueError:
                return True

        @contextmanager
        def launch_guard():
            # Serialize validation + OS process creation with revise/stop/cancel.
            # Never hold the DB writer while waiting for commands or auth.
            with transaction(self.store.db):
                if not current():
                    raise ValueError("Approved command authority changed before launch")
                # Check the verified inputs after authentication, immediately
                # before process creation. Own effects may legitimately change
                # them afterward, so cancellation keeps checking authority only.
                # Read-only checks/observers remain available for reconciliation;
                # mutating recipes require their binding before reaching here.
                if prerequisite_binding is not None:
                    self._require_prerequisite_binding(task_id, stage_id, prerequisite_binding)
                yield

        return cancelled, launch_guard

    def _call(self, task_id, function, timeout=1200, *, capture_reference=None):
        timeout = self._available(task_id, timeout)
        root_id = self._root_task(task_id)
        nonce = uuid.uuid4().hex
        with transaction(self.store.db) as db:
            old = read_record(db, "active_call", root_id)
            if old["payload"]:
                raise ValueError("An unfinished call needs explicit resume/reconciliation")
            active = {"started": now(), "timeout": timeout, "owner_nonce": nonce, "pid": os.getpid(), "task_id": task_id}
            if capture_reference is not None:
                if capture_reference.get('task_id') != task_id:
                    raise ValueError('Implementation capture differs from active task')
                active['implementation_capture'] = capture_reference
            write_record(db, "active_call", root_id, active, expected_revision=old["revision"])
        started = time.monotonic()
        try:
            return function(timeout)
        finally:
            with transaction(self.store.db) as db:
                self.charge_time(task_id, time.monotonic() - started)
                old = read_record(db, "active_call", root_id)
                if (old["payload"] or {}).get("owner_nonce") == nonce:
                    write_record(db, "active_call", root_id, None, expected_revision=old["revision"])

    def execute_actions(self, task_id, stage):
        with self._ownership(task_id):
            return self._execute_actions(task_id, stage)

    def _access_stage(self, task_id, spec, stage_id=None, *, allow_terminal=False):
        from .execution.actions import ActionService
        return ActionService(self).access_stage(task_id, spec, stage_id, allow_terminal=allow_terminal)

    def _access_client(self):
        from .execution.actions import ActionService
        return ActionService(self).access_client()

    def _execute_command(self, task_id, spec, argv, log, timeout, stage_id=None, *, allow_terminal=False, prerequisite_binding=None, authority_binding=None, launch_witness=None):
        from .execution.actions import ActionService
        return ActionService(self).execute_command(task_id, spec, argv, log, timeout, stage_id, allow_terminal=allow_terminal, prerequisite_binding=prerequisite_binding, authority_binding=authority_binding, launch_witness=launch_witness)

    def _declared_access(self, task_id, stage_id, access):
        from .execution.actions import ActionService
        return ActionService(self).declared_access(task_id, stage_id, access)

    def ensure_access(self, task_id, stage_id, access):
        from .execution.actions import ActionService
        return ActionService(self).ensure_access(task_id, stage_id, access)

    def request_access(self, task_id, stage_id, access, path='/', method='GET'):
        from .execution.actions import ActionService
        return ActionService(self).request_access(task_id, stage_id, access, path, method)

    def _observe_action(self, task_id, stage, action, folder, *, allow_terminal=False, authority_binding=None):
        from .execution.actions import ActionService
        return ActionService(self).observe_action(task_id, stage, action, folder, allow_terminal=allow_terminal, authority_binding=authority_binding)

    def _execute_actions(self, task_id, stage):
        from .execution.actions import ActionService
        return ActionService(self).execute_actions(task_id, stage)

    def record_failure(self, task_id, stage_id, reasons):
        return self._record_recovery(task_id, stage_id, reasons, failed=True)

    def _record_recovery(self, task_id, stage_id, reasons, *, failed):
        root_id = self._root_task(task_id)
        with transaction(self.store.db) as db:
            record = read_record(db, "budget", root_id)
            value = record["payload"] or {"task_cycles": 0, "stages": {}, "diagnostics": {}, "failures": {}}
            recovery_record, history, groups = self._recovery_context(db, task_id, stage_id)
            key = ":".join(groups) + ":" + hash_value(canonical_failures(history, groups, reasons))
            inherited_failures = max(history["groups"][name].get("legacy_failures", 0) for name in groups)
            value["failures"][key] = value["failures"].get(key, inherited_failures) + int(failed)
            policy = self.tasks.snapshot(root_id)["contract"]["execution_policy"]
            tokens = self._verified_progress(task_id, stage_id, history, groups)
            newly_verified = sorted(set(tokens) - set(history["high_water"]))
            history["high_water"] = sorted(set(history["high_water"]) | set(tokens))
            for name in groups:
                group = history["groups"][name]
                if set(newly_verified) & group_tokens(name, group):
                    group.update(stagnation=0, probe_pending=False, probe_inflight=None, diagnostic_inflight=None,
                                 probe_authorization=None, stopped=False)
                    group["progress_generation"] = group.get("progress_generation", 0) + 1
                elif failed:
                    group["stagnation"] += 1
                    if group["probe_pending"] or group.get("probe_inflight"):
                        group.update(stopped=True, probe_pending=False, probe_inflight=None, probe_authorization=None)
                elif not failed:
                    group["probe_inflight"] = None
            stagnation = max(history["groups"][name]["stagnation"] for name in groups)
            stopped = any(history["groups"][name]["stopped"] for name in groups)
            history["events"].append({"task_id": task_id, "stage_id": stage_id, "reasons": reasons,
                "failure_key": key, "newly_verified": newly_verified, "stagnation": stagnation, "stopped": stopped, "timestamp": now()})
            if newly_verified:
                history["events"][-1]["proof"] = {"checks": read_record(db, "checks", task_id)["payload"],
                    "verification": {role: read_record(db, "verification", task_id + ":" + role)["payload"] for role in ("ac", "review")}}
            write_record(db, "recovery", root_id, history, expected_revision=recovery_record["revision"])
            write_record(db, "budget", root_id, value, expected_revision=record["revision"])
        diagnostic = (stagnation >= policy.get("stagnation_limit", 3) and not stopped if policy.get("recovery_mode", "fixed") == "progress"
                      else value["failures"][key] >= policy["repeat_failure_limit"])
        return {"diagnostic_required": diagnostic, "repetitions": value["failures"][key],
                "stagnation": stagnation, "stopped": stopped, "newly_verified": newly_verified}

    def _verified_progress(self, task_id, stage_id, history, groups):
        """Credit only fresh evaluated original checks and independent judgments."""
        task = self.tasks.snapshot(task_id)
        contract, plan = task["contract"], self.tasks.execution_plan(task_id)
        bundle = read_record(self.store.db, "checks", task_id)["payload"] or {}
        source = source_snapshot(self.store, contract, task_id=task_id)
        if (bundle.get("fingerprint") != source["fingerprint"] or bundle.get("contract_hash") != task["contract_hash"]
                or bundle.get("plan_hash") != plan["plan_hash"] or bundle.get("workspace_id") != self.store.workspace_id
                or bundle.get("source_changed_during_checks")):
            return []
        specs = stage_checks(contract, plan["checks"], stage_id)
        profile = evaluate_contract_profiles(contract, specs, bundle.get("results", []), stage_id=stage_id)
        checked = {key for key, result in profile["by_id"].items() if result["status"] == "PASS"}
        case_tokens = original_case_progress(self.store, self._root_task(task_id), task_id, history, groups,
                                            contract, stage_id, bundle, source, checked)
        bindings = {"fingerprint": source["fingerprint"], "contract_sha256": task["contract_hash"],
            "proposal_sha256": task["proposal_hash"], "plan_sha256": plan["plan_hash"],
            "check_sha256": hash_value(bundle), "workspace_id": self.store.workspace_id}
        contexts = verification_context_hashes(self.store, contract, task_id, stage_id)
        roles, execution_ids, session_ids = {}, set(), set()
        implementation = read_record(self.store.db, "implementation", task_id)["payload"] or {}
        for role in ("ac", "review"):
            receipt = read_record(self.store.db, "verification", task_id + ":" + role)["payload"] or {}
            try:
                if not Path(receipt["artifact_root"]).resolve().is_relative_to(self.store.root.resolve()):
                    continue
                context = receipt.get("bindings", {}).get("verification_context_sha256")
                if context not in contexts:
                    continue
                role_bindings = {**bindings, "verification_context_sha256": context}
                if contract.get('lead_review') and not contract.get('parent_task_id') and 'lead_review_sha256' in receipt.get('bindings', {}):
                    role_bindings['lead_review_sha256'] = receipt['bindings']['lead_review_sha256']
                verified = verify_receipt(receipt, expected_bindings=role_bindings)
                execution_id, session_id = verified["execution_id"], verified["result"].get("session_id")
                if (verified["evidence_kind"] != "independent_cli" or verified["result"].get("status") != "COMPLETE"
                        or execution_id in execution_ids or execution_id == implementation.get("execution_id")
                        or (session_id and session_id in session_ids)):
                    continue
                execution_ids.add(execution_id)
                if session_id:
                    session_ids.add(session_id)
                roles[role] = verified["result"]["payload"]
            except (ValueError, OSError, KeyError, TypeError):
                continue
        accepted = set()
        criteria = roles.get("ac", {}).get("criteria", [])
        if len({item.get("id") for item in criteria}) == len(criteria):
            for ac in contract["acceptance"]:
                matches = [item for item in criteria if item.get("id") == ac["id"]]
                if len(matches) == 1:
                    item = matches[0]
                    if (item.get("status") == "PASS" and item.get("expected") == ac["expected"] and item.get("evidence")
                            and item.get("method") and not item.get("missing_coverage")
                            and set(ac["evidence_ids"]).issubset(checked)):
                        accepted.add(ac["id"])
        review = roles.get("review", {})
        clean_review = bool(review.get("summary")) and isinstance(review.get("findings"), list) and all(
            isinstance(finding, dict) and finding.get("severity") == "P3" for finding in review["findings"])
        return sorted(set(progress_tokens(history, groups, contract, stage_id, checked, accepted, clean_review)) | case_tokens)

    def reserve_diagnostic(self, task_id, stage_id):
        root_id = self._root_task(task_id)
        with transaction(self.store.db) as db:
            record = read_record(db, "budget", root_id)
            value = record["payload"] or {"task_cycles": 0, "stages": {}, "diagnostics": {}, "failures": {}}
            key = task_id + ":" + stage_id
            policy = self.tasks.snapshot(root_id)["contract"]["execution_policy"]
            recovery_record, history, groups = self._recovery_context(db, task_id, stage_id)
            count = max([value["diagnostics"].get(key, 0)] + [history["groups"][name]["diagnostics"] for name in groups])
            owners = task_bound_contexts(self.store, history, self._ancestry(task_id), value, stage_id, updated=record["updated"])
            limit = history["hard_limits"].get("max_diagnostic_passes")
            if limit is not None and count >= limit:
                raise ValueError("diagnostic budget exhausted")
            for owner, bounds, local_groups in owners:
                local_limit = bounds["hard_limits"].get("max_diagnostic_passes")
                if local_limit is not None and max(bounds["groups"][name]["diagnostics"] for name in local_groups) >= local_limit:
                    raise ValueError("diagnostic budget exhausted: " + owner["task_id"])
            if any(history["groups"][name].get("diagnostic_inflight") for name in groups):
                raise ValueError("Unresolved diagnostic reservation requires fresh original progress")
            reservation = {"id": uuid.uuid4().hex, "task_id": task_id, "stage_id": stage_id,
                           "bindings": self._diagnostic_bindings(task_id), "status": "RUNNING",
                           "proposal_sequence": proposal_sequence(db, task_id),
                           "generations": {name: history["groups"][name].get("progress_generation", 0) for name in groups}}
            if policy.get("recovery_mode", "fixed") == "progress":
                if any(history["groups"][name]["stopped"] or history["groups"][name]["diagnosed_generation"] == history["groups"][name].get("progress_generation", 0) for name in groups):
                    raise ValueError("No verified progress since the prior diagnostic; automatic mutation is stopped")
                for name in groups:
                    group = history["groups"][name]
                    group.update(diagnosed_generation=group.get("progress_generation", 0))
            for name in groups:
                history["groups"][name].update(diagnostics=count + 1, diagnostic_inflight=reservation,
                                               probe_pending=False, probe_authorization=None)
            value["diagnostics"][key] = count + 1
            for owner, bounds, local_groups in owners:
                local_count = max(bounds["groups"][name]["diagnostics"] for name in local_groups)
                for name in local_groups:
                    bounds["groups"][name]["diagnostics"] = local_count + 1
            write_record(db, "recovery", root_id, history, expected_revision=recovery_record["revision"])
            write_record(db, "budget", root_id, value, expected_revision=record["revision"])
        return value["diagnostics"][key]

    def _diagnostic_bindings(self, task_id):
        task = self.tasks.snapshot(task_id)
        checks = read_record(self.store.db, "checks", task_id)["payload"]
        return {"fingerprint": source_snapshot(self.store, task["contract"], task_id=task_id)["fingerprint"],
            "contract_sha256": task["contract_hash"], "proposal_sha256": task["proposal_hash"] or "unapproved",
            "plan_sha256": self.tasks.execution_plan(task_id)["plan_hash"],
            "check_sha256": hash_value(checks) if checks else "missing", "workspace_id": self.store.workspace_id}

    @staticmethod
    def _diagnostic_context(reservation):
        return {"reservation_id": reservation.get("id"), "task_id": reservation.get("task_id"),
            "stage_id": reservation.get("stage_id"), "groups": sorted(reservation.get("generations", {})),
                **({"proposal_sequence": reservation["proposal_sequence"]} if "proposal_sequence" in reservation else {}),
            "generations": reservation.get("generations", {})}

    def _diagnostic_payload(self, receipt, bindings, reservation_context):
        """Return only an authenticated, complete diagnostic for this reservation."""
        try:
            if (not reservation_context or not reservation_context.get("reservation_id") or receipt.get("role") != "diagnostic"
                    or not Path(receipt["artifact_root"]).resolve().is_relative_to(self.store.root.resolve())):
                return None
            verified = verify_receipt(receipt, expected_bindings=bindings)
            captured = receipt["artifacts"]["request"]
            data = Path(captured["path"]).read_bytes()
            if hashlib.sha256(data).hexdigest() != captured["sha256"]:
                return None
            request = json.loads(data)
            if (request.get("role") != "diagnostic" or request.get("schema") != DIAGNOSTIC_SCHEMA
                    or request.get("handoff", {}).get("diagnostic_reservation") != reservation_context):
                return None
            result = verified["result"]
            payload = result.get("payload", {})
            if (result.get("status") == "COMPLETE" and bool(payload.get("cause", "").strip())
                    and bool(payload.get("recommendation", "").strip())):
                return payload
        except (ValueError, OSError, KeyError, TypeError, AttributeError):
            pass
        return None

    def _diagnostic_supported(self, receipt, bindings, reservation_context):
        payload = self._diagnostic_payload(receipt, bindings, reservation_context)
        return payload is not None and payload.get("requires_new_approval") is False

    def _probe_authorized(self, task_id, stage_id, name, group):
        permission = group.get("probe_authorization") or {}
        return (permission.get("task_id") == task_id and permission.get("stage_id") == stage_id
                and permission.get("generations", {}).get(name) == group.get("progress_generation", 0)
                and permission.get("bindings") == self._diagnostic_bindings(task_id)
                and self._diagnostic_supported(permission.get("receipt", {}), permission["bindings"], permission.get("reservation")))

    def _complete_diagnostic(self, task_id, stage_id, diagnosis, *, interrupted=False):
        """Only captured supported completion can mint one durable probe permission."""
        root_id = self._root_task(task_id)
        with transaction(self.store.db) as db:
            record, history, groups = self._recovery_context(db, task_id, stage_id)
            reservations = [history["groups"][name].get("diagnostic_inflight") or {} for name in groups]
            reservation = reservations[0]
            receipt = diagnosis.get("receipt", {})
            current = (bool(reservation.get("id")) and all(item.get("id") == reservation["id"] for item in reservations)
                and reservation.get("task_id") == task_id and reservation.get("stage_id") == stage_id
                and reservation.get("status") == "RUNNING" and reservation.get("bindings") == self._diagnostic_bindings(task_id)
                and reservation.get("proposal_sequence") == proposal_sequence(db, task_id)
                and all(reservation.get("generations", {}).get(name) == history["groups"][name].get("progress_generation", 0) for name in groups))
            payload = self._diagnostic_payload(receipt, reservation["bindings"], self._diagnostic_context(reservation)) if current and not interrupted else None
            supported = payload is not None and payload.get("requires_new_approval") is False
            approval_required = payload is not None and payload.get("requires_new_approval") is True
            outcome = "SUPPORTED" if supported else "APPROVAL_REQUIRED" if approval_required else "INTERRUPTED" if interrupted else "FAILED"
            permission = {"task_id": task_id, "stage_id": stage_id, "reservation_id": reservation.get("id"),
                "bindings": reservation.get("bindings"), "receipt": receipt,
                "generations": reservation.get("generations", {}), "reservation": self._diagnostic_context(reservation)}
            for name in groups:
                group = history["groups"][name]
                if supported:
                    group.update(diagnostic_inflight=None, probe_pending=True, probe_authorization=permission)
                else:
                    group.update(diagnostic_inflight={**reservation, "status": outcome}, probe_pending=False,
                                 probe_authorization=None, stopped=True)
            history["events"].append({"task_id": task_id, "stage_id": stage_id, "timestamp": now(),
                "diagnostic_reservation": reservation.get("id"), "supported": bool(supported), "outcome": outcome, "receipt": receipt})
            # Permission and its resumable coordinator phase are one commit.
            # A crash after completion must not leave valid permission hidden
            # behind the earlier DIAGNOSING checkpoint.
            cycle_key = task_id + ":" + stage_id
            cycle = read_record(db, "cycle", cycle_key)
            task = self.tasks.snapshot(task_id)
            write_record(db, "cycle", cycle_key, {**(cycle["payload"] or {}),
                "phase": "REJECTED" if supported else "NEEDS_APPROVAL" if approval_required else "DIAGNOSTIC_INTERRUPTED" if interrupted else "DIAGNOSTIC_BLOCKED",
                "diagnostic_outcome": outcome,
                "contract_hash": task["contract_hash"], "proposal_hash": task["proposal_hash"],
                "diagnostic_proposal_sequence": reservation.get("proposal_sequence"),
                "diagnostic_reservation_id": reservation.get("id")}, expected_revision=cycle["revision"])
            write_record(db, "recovery", root_id, history, expected_revision=record["revision"])
        return bool(supported)

    @staticmethod
    def _diagnostic_stop_reason(progress):
        phase = progress.get("phase")
        if phase == "NEEDS_APPROVAL" and progress.get("diagnostic_outcome") == "APPROVAL_REQUIRED":
            return "Verified diagnostic requires a revised approved proposal"
        if phase in {"DIAGNOSING", "DIAGNOSTIC_INTERRUPTED"}:
            return "Diagnostic was interrupted without a validated completion; automatic mutation is stopped"
        if phase in {"NEEDS_APPROVAL", "DIAGNOSTIC_BLOCKED"}:
            # Legacy NEEDS_APPROVAL also represented missing/failed evidence.
            return "Diagnostic has no validated corrective evidence; automatic mutation is stopped"
        return progress.get("diagnostic_blocker")

    def _diagnostic_stop_verdict(self, progress, task, sequence):
        verdict = {"status": "BLOCKED", "blocked_reasons": [self._diagnostic_stop_reason(progress)]}
        if diagnostic_approval_pending(progress, task, sequence):
            verdict.update(awaiting_approval=True, next_action=DIAGNOSTIC_APPROVAL_ACTION,
                           diagnostic_proposal_sequence=sequence)
        elif progress.get("phase") == "NEEDS_APPROVAL" and progress.get("diagnostic_outcome") == "APPROVAL_REQUIRED":
            verdict["blocked_reasons"] = ["The saved diagnostic does not establish a change to the current proposal"]
        return verdict

    def request_stop(self, task_id, *, reason):
        from .journal import Journal
        text(reason, "stop reason", limit=4000)
        with transaction(self.store.db) as db:
            task = self.tasks.snapshot(task_id)
            if task["state"] in {"COMPLETED", "CANCELLED"}:
                return {**task, "stop": {"status": "ALREADY_TERMINAL"}}
            record = read_record(db, "stop", task_id)
            if record["payload"] and record["payload"].get("reason") == reason and record["payload"].get("closure"):
                return {**task, "stop": record["payload"]}
            request_id = uuid.uuid4().hex
            root_id = self._root_task(task_id)
            pending = bool(read_record(db, "active_workflow", root_id)["payload"] or read_record(db, "active_call", root_id)["payload"])
            next_action = "Review the saved checkpoint, reconcile any UNKNOWN effects, then explicitly resume task " + task_id
            value = {"reason": reason, "requested_at": now(), "request_id": request_id, "pending_execution_at_request": pending}
            write_record(db, "stop", task_id, value, expected_revision=record["revision"])
            journal = Journal(self.store)
            session_id = "stop-" + request_id
            journal.start_session(task_id, session_id)
            revision = db.execute("SELECT COALESCE(MAX(revision),0) FROM checkpoints WHERE task_id=?", (task_id,)).fetchone()[0]
            closure = journal.finish_session(task_id, session_id, {"finish_reason": "paused",
                "summary": "Pause requested: " + reason, "next_action": next_action,
                "context": {"summary": task["contract"]["request"], "limitations": ["This closure saves the stop request; a running owner records its actual settled outcome separately"]},
                "events": [{"kind": "pause", "summary": reason, "next_action": next_action}],
                "blockers": [{"id": "execution-blocker", "reason": "Paused by request: " + reason, "next_probe": next_action}]},
                closure_key=session_id + ":closure", expected_revision=revision, expected_context_revision=journal.context()["revision"])
            value["closure"] = closure
            latest = read_record(db, "stop", task_id)
            write_record(db, "stop", task_id, value, expected_revision=latest["revision"])
        return {**self.tasks.snapshot(task_id), "stop": value}

    def _save(self, kind, task_id, value):
        with transaction(self.store.db) as db:
            old = read_record(db, kind, task_id)
            write_record(db, kind, task_id, value, expected_revision=old["revision"])
            if kind == 'verification' and value.get('receipt_version') == 2:
                from .coordinator import CoordinatorEvents
                owner, role = task_id.split(':', 1)
                CoordinatorEvents(self.store).record_receipt(owner, role, value)
        return value

    def check(self, task_id, stage_id=None):
        with self._ownership(task_id):
            return self._check(task_id, stage_id)

    def _check(self, task_id, stage_id=None):
        from .testing.execution import CheckService
        return CheckService(self, target=_target).check(task_id, stage_id)

    def _capture_stage(self, task_id, stage_id, verdict):
        task = self.tasks.snapshot(task_id)
        stage = next(item for item in task["contract"]["stages"] if item["id"] == stage_id)
        saved = read_record(self.store.db, "stage", task_id + ":" + stage_id)
        previous = saved["payload"] or {}
        plan_hash = self.tasks.execution_plan(task_id)["plan_hash"]
        previous_plan = previous.get("proof", {}).get("checks", {}).get("plan_hash")
        if (previous.get("state") == "COMPLETED" and all(previous.get(key) == task[key] for key in ("contract_hash", "proposal_hash"))
                and previous_plan in {None, plan_hash}):
            raise ValueError("Existing completed stage history cannot be replaced implicitly")
        kind, key = "stage_implementation", task_id + ":" + stage_id
        selected = read_record(self.store.db, kind, key)
        if selected["payload"] is None:
            kind, key = "implementation", task_id
            selected = read_record(self.store.db, kind, key)
        receipt = selected["payload"]
        if not receipt:
            raise ValueError("Stage implementation receipt is unavailable")
        expected = {"contract_sha256": task["contract_hash"], "proposal_sha256": task["proposal_hash"],
            "plan_sha256": plan_hash, "workspace_id": self.store.workspace_id}
        root = logical_path(receipt["artifact_root"])
        if not root.is_relative_to(logical_path(self.store.root / "tasks" / task_id / "sessions")):
            raise ValueError("Stage implementation receipt escapes this task's sessions")
        if any(receipt.get("bindings", {}).get(name) != value for name, value in expected.items()):
            raise ValueError("Implementation stage receipt bindings are stale")
        verify_receipt(receipt, require_current_policy=False)
        data = state_io(receipt["artifacts"]["request"]["path"]).read_bytes()
        if hashlib.sha256(data).hexdigest() != receipt["artifacts"]["request"]["sha256"]:
            raise ValueError("Stage implementation request changed after verification")
        request = json.loads(data)
        captured = request.get("handoff", {}).get("stage", {})
        if (receipt.get("role") != "implementation" or request.get("role") != "implementation"
                or captured.get("id") != stage_id or ("scope" in captured and captured["scope"] != stage["scope"])
                or request["project"].get("project_id") != self.store.project_id
                or logical_path(request["project"]["root"]) != logical_path(self.store.repo)):
            raise ValueError("Stage implementation receipt has another stage/project identity")
        # Artifact IO/verification stays outside the writer. Publish this exact
        # prospective capture only if none of its selected identities changed.
        with transaction(self.store.db):
            current = self.tasks.snapshot(task_id)
            if (any(current[name] != task[name] for name in ("contract_hash", "proposal_hash"))
                    or self.tasks.execution_plan(task_id)["plan_hash"] != expected["plan_sha256"]
                    or read_record(self.store.db, kind, key) != selected
                    or read_record(self.store.db, "stage", task_id + ":" + stage_id)["revision"] != saved["revision"]):
                raise ValueError("Stage capture identities changed during verification")
            self._save("stage", task_id + ":" + stage_id, {"state": "COMPLETED", "contract_hash": task["contract_hash"],
                "proposal_hash": task["proposal_hash"], "fingerprint": verdict["fingerprint"], "gate": verdict, "completed_at": now(),
                "proof": {"version": 1, "checks": read_record(self.store.db, "checks", task_id)["payload"],
                    "roles": {"implementation": receipt, **{role: read_record(self.store.db, "verification", task_id + ":" + role)["payload"]
                        for role in ("ac", "review")}}}})

    def _stage_policy_refresh_needed(self, task, done):
        """A completed stage whose independent receipts predate the current runtime policy
        while its approved implementation source is unchanged (for example after an ase upgrade)."""
        proof = done.get("proof")
        roles = proof.get("roles") if isinstance(proof, dict) else None
        if (not isinstance(roles, dict) or proof.get("version") != 1
                or not all(isinstance(roles.get(role), dict) for role in ("implementation", "ac", "review"))):
            return False
        return self._unchanged_under_historical_policy(task, proof)

    def _unchanged_under_historical_policy(self, task, proof, *, current_fingerprint=None):
        """True when the stage's AC/review predate the current runtime policy and today's
        artifacts, read under that same historical policy, reproduce the approved fingerprint.
        With current_fingerprint, the same single read must also give that current-policy value."""
        from .gates import fingerprint_under_policy, fingerprint_with_check_inputs
        from .runtime_policy import policy_fingerprint
        try:
            implementation = verify_receipt(proof["roles"]["implementation"], require_current_policy=False)
            verified = [verify_receipt(proof["roles"][role], require_current_policy=False) for role in ("ac", "review")]
            if any(item["evidence_kind"] not in _REFRESH_EVIDENCE for item in (implementation, *verified)):
                return False
            if all(item["policy_current"] for item in verified):
                return False
            policy_ids = {item["policy_id"] for item in verified}
            if len(policy_ids) != 1 or None in policy_ids:
                return False
            archived_policy = policy_ids.pop()
            if task["contract"].get("check_inputs"):
                from .evidence.check_inputs import captured_hashes
                from .protocol import decode_json, sha256
                from .runner import _read
                receipt = proof["roles"]["ac"]
                raw_request = _read(receipt["artifacts"]["request"]["path"], 8388608)
                if sha256(raw_request) != receipt["request_sha256"]:
                    return False
                request = decode_json(raw_request)
                source = source_snapshot(self.store, task["contract"], task_id=task["task_id"])
                files = expand_packet(request["source_packet"])["files"]
                historical = fingerprint_with_check_inputs(source, captured_hashes(files), runtime_policy=archived_policy)
                current = source["fingerprint"]
            else:
                historical, current = fingerprint_under_policy(self.store, task["contract"], task["task_id"],
                                                               archived_policy, policy_fingerprint())
            return historical == proof["checks"]["fingerprint"] and current_fingerprint in {None, current}
        except (ValueError, OSError, KeyError, TypeError):
            return False  # Unverifiable history is never refreshed silently.

    def _refresh_stage_policy(self, task, stage, done):
        """Fresh checks and independent AC/review under the current policy for unchanged
        implementation source. The original implementation receipt stays as provenance; changed source
        still needs a new implementation cycle."""
        task_id, stage_id = task["task_id"], stage["id"]
        key = task_id + ":" + stage_id
        saved = read_record(self.store.db, "stage", key)
        if saved["payload"] != done:
            return {"status": "BLOCKED", "blocked_reasons": ["Stage record changed before policy refresh"]}
        blockers, _ = self._revalidate_prerequisites(task, stage)
        if blockers:
            return {"status": "BLOCKED", "blocked_reasons": blockers}
        original = done["proof"]
        plan_hash = original["checks"]["plan_hash"]
        if self.tasks.execution_plan(task_id)["plan_hash"] != plan_hash:
            return {"status": "BLOCKED", "blocked_reasons": ["Execution plan changed; revalidate the stage instead"]}
        if not self._unchanged_under_historical_policy(task, original):
            return {"status": "BLOCKED", "blocked_reasons": [
                "Stage source changed since completion, or its history cannot be verified; a fresh implementation cycle is required"]}
        checks = self.check(task_id, stage_id)
        current = source_snapshot(self.store, task["contract"], task_id=task_id)["fingerprint"]
        if (checks.get("source_changed_during_checks") or checks["fingerprint"] != current
                or checks.get("plan_hash") != plan_hash or not self._unchanged_under_historical_policy(task, original)):
            return {"status": "BLOCKED", "blocked_reasons": ["Stage source changed during policy refresh"]}
        verdict = self.verify(task_id, stage_id)
        checks_record = read_record(self.store.db, "checks", task_id)
        # Receipts count only when bound to exactly these checks of this stage's source.
        bindings = {"fingerprint": checks["fingerprint"], "contract_sha256": task["contract_hash"],
            "proposal_sha256": task["proposal_hash"], "plan_sha256": plan_hash,
            "check_sha256": hash_value(checks_record["payload"]), "workspace_id": self.store.workspace_id}
        roles, independent = {}, {}
        for role in ("ac", "review"):
            record = read_record(self.store.db, "verification", task_id + ":" + role)
            expected = dict(bindings)
            receipt_bindings = (record["payload"] or {}).get("bindings") or {}
            if "verification_context_sha256" in receipt_bindings:
                expected["verification_context_sha256"] = verification_context_hash(self.store, task["contract"], task_id, stage_id)
            if "lead_review_sha256" in receipt_bindings:  # lead-review provenance, as _stage_history accepts it
                expected["lead_review_sha256"] = receipt_bindings["lead_review_sha256"]
            try:
                verified = (verify_receipt(record["payload"], expected_bindings=expected, require_current_policy=False)
                            if record["payload"] else {})
            except ValueError:
                verified = {}
            independent[role] = bool(verified.get("policy_current") and verified.get("evidence_kind") in _REFRESH_EVIDENCE)
            roles[role] = record

        def unchanged(db):
            current_task = self.tasks.snapshot(task_id)
            return not (any(current_task[name] != task[name] for name in ("contract_hash", "proposal_hash"))
                or self.tasks.execution_plan(task_id)["plan_hash"] != plan_hash
                or read_record(db, "stage", key)["revision"] != saved["revision"]
                or read_record(db, "checks", task_id) != checks_record
                or not self._unchanged_under_historical_policy(task, original, current_fingerprint=checks["fingerprint"])
                or any(read_record(db, "verification", task_id + ":" + role) != record for role, record in roles.items()))

        if verdict["status"] != "APPROVED":
            if verdict.get("rejected_reasons") and any(independent.values()):
                # A current independent rejection is durable even beside a blocker: the stage
                # leaves COMPLETED, so the next run repairs it instead of re-rolling reviewers.
                with transaction(self.store.db) as db:
                    if not unchanged(db):
                        raise ValueError("Stage policy refresh identities changed during verification")
                    write_record(db, "stage", key, {"state": "REFRESH_REJECTED", "contract_hash": task["contract_hash"],
                        "proposal_hash": task["proposal_hash"], "gate": verdict, "rejected_at": now(), "previous": done},
                        expected_revision=saved["revision"])
                return verdict
            return {**verdict, "status": "BLOCKED", "blocked_reasons": verdict.get("blocked_reasons") or [
                "Refresh rejection is not current independent evidence bound to these checks"]}
        if not all(independent.values()) or verdict.get("fingerprint") != checks["fingerprint"]:
            return {"status": "BLOCKED", "blocked_reasons": [
                "Refreshed stage verification is not current independent evidence bound to these checks"]}
        with transaction(self.store.db) as db:
            if not unchanged(db):
                raise ValueError("Stage policy refresh identities changed during verification")
            write_record(db, "stage", key, {**done, "gate": verdict, "fingerprint": verdict["fingerprint"], "proof": {
                "version": 1, "checks": checks_record["payload"],
                "roles": {"implementation": original["roles"]["implementation"],
                          **{role: record["payload"] for role, record in roles.items()}},
                "policy_refresh": {"previous_proof_sha256": hash_value(original), "refreshed_at": now()}}},
                expected_revision=saved["revision"])
        return verdict

    def _stage_history(self, task):
        history, missing = {"historical_only": True, "stages": []}, []
        for stage in task["contract"]["stages"]:
            saved = read_record(self.store.db, "stage", task["task_id"] + ":" + stage["id"])["payload"]
            if not saved or saved.get("state") != "COMPLETED":
                continue
            try:
                supersession = None
                if saved.get("contract_hash") != task["contract_hash"] or saved.get("proposal_hash") != task["proposal_hash"]:
                    from .action_evidence import _superseded_stage
                    original_stage, supersession = _superseded_stage(self.store, task, stage["id"],
                        saved.get("contract_hash"), saved.get("proposal_hash"),
                        captured_at=saved.get("proof", {}).get("roles", {}).get("implementation", {}).get("started_at_utc"))
                proof = saved["proof"]
                if proof.get("version") != 1 or not saved.get("completed_at"):
                    raise ValueError("stage has no prospective proof capture")
                checks = proof["checks"]
                current_plan_hash = self.tasks.execution_plan(task["task_id"])["plan_hash"]
                plan_supersession = ({"original_plan_hash": checks["plan_hash"], "current_plan_hash": current_plan_hash}
                    if checks["plan_hash"] != current_plan_hash else None)
                bindings = {"fingerprint": checks["fingerprint"], "contract_sha256": saved["contract_hash"],
                    "proposal_sha256": saved["proposal_hash"], "plan_sha256": checks["plan_hash"],
                    "check_sha256": hash_value(checks), "workspace_id": self.store.workspace_id}
                entry = {"stage_id": stage["id"], "completed_at": saved["completed_at"], "contract_hash": saved["contract_hash"],
                    "proposal_hash": saved["proposal_hash"], "gate": saved["gate"], "checks": checks, "roles": {}, "observations": [],
                    "current_identity": supersession is None and plan_supersession is None,
                    **({"supersession": supersession} if supersession else {}),
                    **({"plan_supersession": plan_supersession} if plan_supersession else {})}
                observations = {}
                for role in ("implementation", "ac", "review"):
                    receipt = proof["roles"][role]
                    root = Path(receipt["artifact_root"])
                    _safe_state(root)
                    if not root.resolve().is_relative_to(self.store.root.resolve()):
                        raise ValueError("historical receipt escapes project state")
                    historical_bindings = dict(bindings)
                    if "verification_context_sha256" in receipt["bindings"]:
                        # Archived context is provenance, not current authority.
                        historical_bindings["verification_context_sha256"] = receipt["bindings"]["verification_context_sha256"]
                    if 'lead_review_sha256' in receipt['bindings']:
                        historical_bindings['lead_review_sha256'] = receipt['bindings']['lead_review_sha256']
                    verified = verify_receipt(receipt, expected_bindings=historical_bindings if role != "implementation" else None,
                        require_current_policy=False)
                    if any(receipt["bindings"].get(key) != bindings[key] for key in ("contract_sha256", "proposal_sha256", "workspace_id", "plan_sha256")):
                        raise ValueError("historical receipt identity differs")
                    request = json.loads(Path(receipt["artifacts"]["request"]["path"]).read_bytes())
                    if request.get("role") != role:
                        raise ValueError("historical role mismatch")
                    handoff = request.get("handoff", {})
                    captured_stage = handoff.get("stage", {}).get("id") if role == "implementation" else handoff.get("stage_id")
                    if captured_stage != stage["id"]:
                        # The stage gate also accepts a whole-task AC/review.
                        # Recover its original boundary from the sealed source,
                        # never from the current (possibly revised) contract.
                        if role == "implementation" or captured_stage is not None:
                            raise ValueError("historical stage boundary mismatch")
                        supplied = {item["path"]: item for item in expand_packet(request["source_packet"])["files"]}
                        original_contract = json.loads(supplied["_ase_context/contract.json"]["content"])
                        original_stages = [item for item in original_contract["stages"] if item["id"] == stage["id"]]
                        expected_stage = original_stage if supersession else stage
                        if (hash_value(original_contract) != saved["contract_hash"]
                                or original_stages != [expected_stage]
                                or request["project"].get("project_id") != self.store.project_id
                                or logical_path(request["project"]["root"]) != logical_path(self.store.repo)):
                            raise ValueError("historical whole-task stage boundary mismatch")
                    if supersession or plan_supersession:
                        supplied = {item["path"]: item for item in expand_packet(request["source_packet"])["files"]}
                        contract = json.loads(supplied["_ase_context/contract.json"]["content"])
                        plan = json.loads(supplied["_ase_context/execution_plan.json"]["content"])
                        if (hash_value(contract) != saved["contract_hash"] or plan["plan_hash"] != checks["plan_hash"]
                                or hash_value(plan["checks"]) != checks["plan_hash"]
                                or (role == "implementation" and "scope" in handoff.get("stage", {})
                                    and handoff["stage"]["scope"] != (original_stage if supersession else stage)["scope"])
                                or request["project"].get("project_id") != self.store.project_id
                                or logical_path(request["project"]["root"]) != logical_path(self.store.repo)):
                            raise ValueError("superseded history origin differs")
                    entry["roles"][role] = {"execution_id": verified["execution_id"], "evidence_kind": verified["evidence_kind"],
                        "bindings": receipt["bindings"], "started_at_utc": receipt.get("started_at_utc"), "ended_at_utc": receipt.get("ended_at_utc"),
                        "session_id": receipt.get("session_id"), "receipt_sha256": hash_value(receipt),
                        "policy_current": verified['policy_current'], "policy_status": verified['policy_status']}
                    files = expand_packet(request["source_packet"])["files"]
                    if role == "ac" and task["contract"].get("check_inputs"):
                        from .evidence.check_inputs import captured_hashes
                        entry["check_inputs"] = captured_hashes(files)
                    for item in files:
                        if item["path"] not in {"_ase_context/source_changes.json", "_ase_context/actual_checks.json"} and not item["path"].startswith("_ase_context/logs/"):
                            continue
                        # Logical bounds only: identical bodies (for example the current suite log)
                        # compact to same-packet references, and packet admission enforces the wire bound.
                        if len(item["content"].encode("utf-8")) > MAX_DECLARED_PACKET_BYTES:
                            raise ValueError("historical observation exceeds packet bound")
                        key = (item["path"], item["sha256"])
                        if key not in observations:
                            observations[key] = {**item, "roles": []}
                        observations[key]["roles"].append(role)
                entry["observations"] = list(observations.values())
                entry['policy_current'] = all(entry['roles'][role]['policy_current'] for role in ('ac', 'review'))
                entry["semantic_approval"] = entry["current_identity"] and saved["gate"].get("status") == "APPROVED" and entry['policy_current'] and all(entry["roles"][role]["evidence_kind"] == "independent_cli" for role in ("ac", "review"))
                if len(encode({"stages": history["stages"] + [entry]}).encode("utf-8")) > MAX_EXPANDED:
                    raise ValueError("stage history exceeds bounded packet budget")
                history["stages"].append(entry)
            except (ValueError, OSError, KeyError, TypeError) as error:
                missing.append("Stage history unavailable: " + stage["id"] + ": " + str(error))
        return history, missing

    def _packet(self, task_id, stage_id=None, part_id=None, role=None):
        from .evidence.packets import PacketService
        return PacketService(self, target=_target).packet(task_id, stage_id, part_id, role=role)

    def _request(self, task_id, role, schema, handoff=None):
        task = self.tasks.snapshot(task_id)
        plan = self.tasks.execution_plan(task_id)
        check = read_record(self.store.db, "checks", task_id)["payload"]
        source = source_snapshot(self.store, task["contract"], task_id=task_id)
        handoff = dict(handoff or {})
        for field in ('runtime_policy', 'task_proposal'):
            if field in handoff:
                raise ValueError(field + ' is reserved for the trusted host')
        from .runtime_policy import current_policy
        handoff['runtime_policy'] = current_policy(role)
        if role == "diagnostic":
            history = read_record(self.store.db, "recovery", self._root_task(task_id))["payload"] or {}
            reservations = {}
            for group in history.get("groups", {}).values():
                reservation = group.get("diagnostic_inflight") or {}
                if (reservation.get("status") == "RUNNING" and reservation.get("task_id") == task_id
                        and (handoff.get("stage_id") is None or handoff["stage_id"] == reservation.get("stage_id"))):
                    reservations[reservation["id"]] = reservation
            if len(reservations) != 1:
                raise ValueError("Diagnostic request requires one current reservation and stage")
            handoff["diagnostic_reservation"] = self._diagnostic_context(next(iter(reservations.values())))
        stage_id = handoff.get("stage_id") or handoff.get("stage", {}).get("id")
        bindings = {"fingerprint": source["fingerprint"], "contract_sha256": task["contract_hash"],
            "proposal_sha256": task["proposal_hash"] or "unapproved", "plan_sha256": plan["plan_hash"],
            "check_sha256": hash_value(check) if check else "missing", "workspace_id": self.store.workspace_id}
        if role in {"ac", "review"}:
            bindings["verification_context_sha256"] = verification_context_hash(self.store, task["contract"], task_id, stage_id)
            if stage_id is None and handoff.get('purpose') != 'test_adequacy':
                from .lead_review import LeadReview
                lead_binding = LeadReview(self.store).binding(task_id)
                if lead_binding is not None:
                    bindings['lead_review_sha256'] = lead_binding
        part = handoff.get("review_part")
        if part is not None:
            # One independent session per owner-approved review part; the binding names the part.
            from .evidence.representation import part_hash
            bindings["review_part_sha256"] = part_hash(part)
        packet = self._packet(task_id, stage_id=stage_id, part_id=None if part is None else part["id"], role=role)
        from .review import blind
        blind_pass = role == "review" and handoff.get("purpose") == blind.PURPOSE
        if role == "review":
            from .evidence.packets import amend_packet
            from .model_limits import packet_budget
            from .review.examples import packet_file
            examples = packet_file(self.store.repo)

            def review_view(logical):
                if examples is not None:
                    logical = {**logical, "files": [*logical["files"], examples], "coverage": {
                        **logical["coverage"], "included": [*logical["coverage"]["included"], examples["path"]]}}
                return blind.blind_packet(logical) if blind_pass else logical

            if examples is not None or blind_pass:
                packet = amend_packet(packet, review_view, max_bytes=packet_budget(task['contract'], self.store.db, role))
        if handoff.get('purpose') == 'test_adequacy' and 'verification_scope' in handoff:
            from .testing.adequacy import test_source_candidates
            handoff['test_source_candidates'] = test_source_candidates(
                packet, task['contract'], plan['checks'], handoff['verification_scope'])
        if task['contract'].get('context_transport') and len(encode(packet).encode('utf-8')) > 700000:
            handoff['context_transport'] = {'version': 1, 'layout': 'source_text_v1'}
        approved = task['approved_stage_ids']
        proposal = {'proposal': task.get('proposal'), 'approved_stage_ids': approved}
        if task['contract'].get('parent_task_id'):
            if not approved:
                raise ValueError('Current stage requires explicit approval')
            # Take a consistent snapshot of the linked grants without reserving the writer.
            # Stage authorization checks source readiness separately.
            approved_stage = stage_id or approved[0]
            allow_terminal = role in {'ac', 'review'} and task['state'] == 'COMPLETED'
            with transaction(self.store.db, immediate=False):
                lineage = self.tasks.approved_lineage(task_id, approved_stage, allow_terminal=allow_terminal,
                                                      check_sources=False)
            (current, _), *parents = lineage
            if any(current.get(field) != task.get(field)
                   for field in ('contract_hash', 'proposal_hash', 'parent_proposal_hash')):
                raise ValueError('Task approval changed during request preparation')
            proposal['approved_stage_ids'] = current['approved_stage_ids']
            proposal['ancestor_proposals'] = [
                {'task_id': parent['task_id'], 'stage_id': mapped_stage, 'proposal': parent['proposal'],
                 'proposal_sha256': parent['proposal_hash']}
                for parent, mapped_stage in parents if parent.get('proposal')]
        handoff['task_proposal'] = {'withheld': blind.WITHHELD} if blind_pass else proposal
        return {"role": role, "project": {"root": str(self.store.repo), "project_id": self.store.project_id}, "schema": schema,
                "bindings": bindings, "source_packet": packet, "handoff": handoff}

    def _runner(self, task_id, role=None):
        if self.runner is not None:
            return self.runner
        contract = self.tasks.snapshot(task_id)["contract"]
        from .tasks import backend_for
        backend = backend_for(contract, role)
        from .model_limits import runner_limits
        config = {**runner_limits(contract, self.store.db, backend),
                  **(read_record(self.store.db, "backend_config", backend)["payload"] or {})}
        if backend == "deepseek" and not config.get("executable"):
            # No agent CLI: this interpreter runs the isolated API worker shipped with the adapter.
            from .adapters.deepseek import worker_args
            config = {**config, "executable": sys.executable, "executable_args": worker_args()}
        executable = config.get("executable") or shutil.which({"codex": "codex", "claude": "claude", "copilot": "copilot"}.get(backend, backend))
        if not executable:
            from .adapters import installed_executable
            executable = installed_executable(backend)
        from .project_local import validate_local_state
        temporary = None
        if validate_local_state(self.store.state_home, self.store.repo):
            temporary = tempfile.TemporaryDirectory(prefix="ase-local-runner-")
            trusted = Path(temporary.name)
        else:
            trusted = self.store.state_home / "runner"
            trusted.mkdir(parents=True, exist_ok=True)
        runner = Runner({**config, "executable": str(Path(executable).resolve()), "trusted_cwd": str(trusted)}, self.store.root / "tasks" / task_id / "sessions")
        # Keep the clean empty cwd alive for the Runner's whole operation; only
        # subprocess captures/receipts persist, under the project's private state.
        runner._temporary_cwd = temporary
        if temporary is not None:
            weakref.finalize(runner, temporary.cleanup)
        return runner

    def verify(self, task_id, stage_id=None):
        with self._ownership(task_id):
            result = self._verify(task_id, stage_id)
        if stage_id is None and result.get('status') == 'APPROVED' and not result.get('next_action'):
            from .tasks import finish_command
            result = {**result, 'next_action': finish_command(task_id)}
        return result

    def _verify(self, task_id, stage_id=None):
        from .review.service import VerificationService
        return VerificationService(self, ac_schema=AC_SCHEMA, review_schema=REVIEW_SCHEMA).verify(task_id, stage_id)

    def run(self, task_id, *, resume=False):
        from .journal import Journal
        task = self.tasks.snapshot(task_id)
        if task["legacy_v1"]:
            raise ValueError("Legacy tasks require a reviewed v2 contract")
        if task["state"] == "COMPLETED":
            return GateService(self.store).gate(task_id)
        root_id = self._root_task(task_id)
        with self._ownership(task_id):
            active = read_record(self.store.db, "active_call", root_id)
            if active["payload"]:
                if not resume:
                    raise ValueError("Interrupted call needs explicit resume")
                elapsed = max(0, (datetime.fromisoformat(now()) - datetime.fromisoformat(active["payload"]["started"])).total_seconds())
                if active['payload'].get('implementation_capture'):
                    try:
                        from .candidate_capture import completed_interval
                        elapsed = completed_interval(self, active['payload'], root_id)
                    except (ValueError, OSError, KeyError, TypeError):
                        # Partial/invalid/legacy calls cannot establish a trusted
                        # completion time; retain conservative bounded charging.
                        pass
                with transaction(self.store.db) as db:
                    owner = active["payload"].get("task_id")
                    if owner is not None and self._root_task(owner) != root_id:
                        raise ValueError("Interrupted call owner differs from its root")
                    self.charge_time(owner or root_id, min(elapsed, active["payload"]["timeout"]), attributed=owner is not None)
                    write_record(db, "active_call", root_id, None, expected_revision=active["revision"])
            journal = Journal(self.store)
            old_workflow = read_record(self.store.db, "active_workflow", root_id)
            if old_workflow["payload"]:
                previous = old_workflow["payload"]
                old_session = journal.session(previous["task_id"], previous["session_id"])
                if old_session["payload"] and old_session["payload"]["status"] == "RUNNING":
                    previous_task = self.tasks.snapshot(previous["task_id"])
                    revision = self.store.db.execute("SELECT COALESCE(MAX(revision),0) FROM checkpoints WHERE task_id=?", (previous["task_id"],)).fetchone()[0]
                    journal.finish_session(previous["task_id"], previous["session_id"], {"finish_reason": "interrupted",
                        "summary": "Previous coordinator ended without a closure", "next_action": "Reconcile actual effects and resume approved work",
                        "context": {"summary": previous_task["contract"]["request"]}}, closure_key=previous["session_id"] + ":recovered",
                        expected_revision=revision, expected_context_revision=journal.context()["revision"])
            session_id = uuid.uuid4().hex
            journal.start_session(task_id, session_id)
            self._save("active_workflow", root_id, {"task_id": task_id, "session_id": session_id})
            journal.append({"kind": "work_started", "task_id": task_id, "session_id": session_id, "summary": "Resuming approved work" if resume else "Starting approved work"}, event_key=session_id + ":started")
            reason = "blocked"
            try:
                verdict = self._run(task_id, resume=resume)
                reason = "completed" if verdict["status"] == "APPROVED" else "paused" if read_record(self.store.db, "stop", task_id)["payload"] else "awaiting_lead_review" if verdict.get('awaiting_lead_review') else "awaiting_approval" if verdict.get("awaiting_approval") else "blocked"
            except KeyboardInterrupt:
                reason = "interrupted"
                verdict = {"status": "BLOCKED", "blocked_reasons": ["Execution interrupted; inspect uncertain effects before resume"]}
            except (ValueError, OSError, KeyError, TypeError) as ex:
                from .core import redact
                verdict = {"status": "BLOCKED", "blocked_reasons": [redact(ex)]}
                if read_record(self.store.db, "stop", task_id)["payload"]:
                    reason = "paused"
            # Source/git work for the stop hint stays outside the closure's write transaction.
            stop_hint = None
            if self._stop_requested(task_id):
                from .tasks import stop_next_action
                stop_hint = stop_next_action(self.store, self.tasks.snapshot(task_id))
            with transaction(self.store.db):
                return self._finish_run(task_id, task, session_id, verdict, reason, journal, root_id, stop_hint)

    def _finish_run(self, task_id, task, session_id, verdict, reason, journal, root_id, stop_hint=None):
        # BEGIN IMMEDIATE serializes this decision with a concurrent stop. A
        # stop committed first wins even when the last verifier returned PASS.
        if self._stop_requested(task_id):
            reason = "paused"
            if verdict["status"] == "APPROVED":
                verdict = {"status": "BLOCKED", "verified_gate": verdict,
                           "blocked_reasons": ["Stop requested before final closure"]}
        addition = verdict.get('addition_approval')
        if reason == "awaiting_approval" and ("diagnostic_proposal_sequence" in verdict or addition):
            current = self.tasks.snapshot(task_id)
            expected_sequence = (addition['proposal_sequence'] if addition else verdict['diagnostic_proposal_sequence'])
            if (current["proposal_hash"] != task["proposal_hash"]
                    or expected_sequence != proposal_sequence(self.store.db, task_id)):
                reason = "superseded"
                context = 'candidate admission' if addition else 'diagnostic reassessment'
                verdict = {"status": "BLOCKED", "awaiting_approval": False,
                           "blocked_reasons": ["The proposal changed during " + context],
                           "next_action": "Inspect the current task with `ase task status " + task_id + "`"}
        remaining = [s["id"] for s in self.tasks.snapshot(task_id)["contract"]["stages"] if (read_record(self.store.db, "stage", task_id + ":" + s["id"])["payload"] or {}).get("state") != "COMPLETED"]
        summary = "All approved task obligations verified" if reason == "completed" else "; ".join(verdict.get("reasons", verdict.get("blocked_reasons", ["Work paused"])))[:3500]
        checkpoint_revision = self.store.db.execute("SELECT COALESCE(MAX(revision),0) FROM checkpoints WHERE task_id=?", (task_id,)).fetchone()[0]
        resolved = []
        if reason in {'completed', 'awaiting_lead_review'} and any(b["task_id"] == task_id and b["id"] == "execution-blocker" for b in journal.active_blockers()):
            resolved = [{"kind": "blocker_resolved", "blocker_id": "execution-blocker", "summary": "Current task verification resolved the framework execution blocker"}]
        if reason == 'superseded' and any(b["task_id"] == task_id and b["id"] == "execution-blocker"
                and b["reason"].startswith("Verified diagnostic requires") for b in journal.active_blockers()):
            resolved = [{"kind": "blocker_resolved", "blocker_id": "execution-blocker",
                         "summary": "The pending diagnostic decision belongs to a replaced proposal"}]
        if reason == "paused":
            from .tasks import stop_next_action
            next_action = stop_hint or stop_next_action(self.store, self.tasks.snapshot(task_id), list_changes=False)
        elif reason == "completed":
            next_action = "No remaining task work"
        elif verdict.get("next_action"):
            next_action = verdict["next_action"]
        elif reason == "awaiting_approval":
            next_action = "Record the owner's decision on the pending stage before executing it"
        else:
            next_action = "Inspect the reported blockers and reconcile actual effects before you resume"
        closure = journal.finish_session(task_id, session_id, {"finish_reason": reason, "summary": summary,
            "next_action": next_action,
            "context": {"summary": task["contract"]["request"], "limitations": ["Dated task context is retained alongside other tasks; verify current sources before reuse"]},
            "remaining": remaining, "workflow_outcome": verdict["status"], "events": resolved,
            "blockers": [] if reason in {'completed', 'awaiting_lead_review', 'superseded'} else [{"id": "execution-blocker", "reason": summary, "next_probe": "Inspect current gate and stage state before resume"}]},
            closure_key=session_id + ":finish", expected_revision=checkpoint_revision, expected_context_revision=journal.context()["revision"])
        self._save("active_workflow", root_id, None)
        return {**verdict, "session_id": session_id, "closure": closure}

    def _prerequisite_stages(self, task, stage):
        """Dependencies of this stage and every inherited parent-stage mapping.

        The mapped parent stage itself may be implemented by this child; only
        its prerequisite closure is inherited. Keep each owner identity so its
        independent evidence and source boundaries are checked in that task.
        """
        lineage, seen = [(task, stage)], {task["task_id"]}
        while task["contract"].get("parent_task_id"):
            parent_id = task["contract"]["parent_task_id"]
            if parent_id in seen:
                raise ValueError("Parent task cycle")
            seen.add(parent_id)
            parent = self.tasks.snapshot(parent_id)
            mapped = next((item for item in parent["contract"]["stages"]
                if item["id"] == task["contract"].get("parent_stage_id")), None)
            if mapped is None:
                raise ValueError("Inherited parent stage is unavailable")
            lineage.append((parent, mapped))
            task = parent
        ordered, selected = [], set()
        for owner, mapped in reversed(lineage):
            by_id = {item["id"]: item for item in owner["contract"]["stages"]}
            def include(stage_id):
                key = (owner["task_id"], stage_id)
                if key in selected:
                    return
                selected.add(key)
                for dependency in by_id[stage_id]["depends_on"]:
                    include(dependency)
                ordered.append((owner, stage_id))
            for dependency in mapped["depends_on"]:
                include(dependency)
        return ordered

    def _prerequisite_current_inputs(self, owner, stage_id):
        """Read exact prerequisite inputs and scoped closure under one writer."""
        from .delegations import DelegationRegistry
        from .testing.adequacy import TestAdequacy
        with transaction(self.store.db):
            owner = self.tasks.require_approval(owner["task_id"], stage_id)
            record = read_record(self.store.db, "stage", owner["task_id"] + ":" + stage_id)
            proof = record["payload"] or {}
            if (proof.get("state") != "COMPLETED" or proof.get("contract_hash") != owner["contract_hash"]
                    or proof.get("proposal_hash") != owner["proposal_hash"]):
                raise ValueError("Current completed prerequisite stage proof is missing")
            blockers = DelegationRegistry(self.store).closure_blockers(owner["task_id"], stage_id=stage_id)
            if blockers:
                raise ValueError("Prerequisite delegation closure requires reconciliation: " + "; ".join(blockers))
            result={"task_id": owner["task_id"], "stage_id": stage_id, "workspace_id": self.store.workspace_id,
                "contract_hash": owner["contract_hash"], "proposal_hash": owner["proposal_hash"],
                "plan_hash": self.tasks.execution_plan(owner["task_id"])["plan_hash"],
                "fingerprint": source_snapshot(self.store, owner["contract"], task_id=owner["task_id"])["fingerprint"],
                "stage_revision": record["revision"], "authority": self._authority_binding(owner["task_id"], stage_id),
                "verification_context_sha256": verification_context_hash(self.store, owner["contract"], owner["task_id"], stage_id)}
            quality=TestAdequacy(self.store).binding(owner['task_id'],stage_id)
            if quality is not None:
                result['test_adequacy_sha256']=quality
            return result

    def _revalidate_prerequisites(self, task, stage):
        """Refresh inherited and local proof without replaying completed work."""
        ordered = self._prerequisite_stages(task, stage)
        if not ordered:
            return [], None
        source = source_snapshot(self.store, task["contract"], task_id=task["task_id"])
        binding = {"task_id": task["task_id"], "stage_id": stage["id"], "workspace_id": self.store.workspace_id,
            "contract_hash": task["contract_hash"], "proposal_hash": task["proposal_hash"],
            "plan_hash": self.tasks.execution_plan(task["task_id"])["plan_hash"], "fingerprint": source["fingerprint"],
            "prerequisites": []}
        observed_sources = {}
        for owner, stage_id in ordered:
            label = stage_id if owner["task_id"] == task["task_id"] else owner["task_id"] + ":" + stage_id
            try:
                record = read_record(self.store.db, "stage", owner["task_id"] + ":" + stage_id)
                proof = record["payload"] or {}
                if (proof.get("state") != "COMPLETED" or proof.get("contract_hash") != owner["contract_hash"]
                        or proof.get("proposal_hash") != owner["proposal_hash"]):
                    raise ValueError("Current completed stage proof is missing")
                self.tasks.require_approval(owner["task_id"], stage_id)
                authority = self._authority_binding(owner["task_id"], stage_id)
                checks = self.check(owner["task_id"], stage_id)
                specs = stage_checks(owner["contract"], self.tasks.execution_plan(owner["task_id"])["checks"], stage_id)
                profile = evaluate_contract_profiles(owner["contract"], specs, checks["results"], stage_id=stage_id)
                verdict = (self.verify(owner["task_id"], stage_id) if profile["status"] == "PASS"
                           else GateService(self.store).gate(owner["task_id"], stage_id=stage_id))
                if verdict["status"] != "APPROVED":
                    reasons = list(dict.fromkeys(verdict.get("blocked_reasons", []) + verdict.get("rejected_reasons", [])))
                    return (["Prerequisite " + label + " requires current verification: " + reason
                             for reason in (reasons or ["Current independent approval is unavailable"])], None)
                observed = {"task_id": owner["task_id"], "stage_id": stage_id, "workspace_id": checks["workspace_id"],
                    "contract_hash": checks["contract_hash"], "proposal_hash": owner["proposal_hash"],
                    "plan_hash": checks["plan_hash"], "fingerprint": checks["fingerprint"],
                    "stage_revision": record["revision"], "authority": authority,
                    "verification_context_sha256": verdict.get("verification_context_sha256")}
                if verdict.get('test_adequacy_sha256') is not None:
                    observed['test_adequacy_sha256']=verdict['test_adequacy_sha256']
                if (checks.get("source_changed_during_checks") or verdict.get("fingerprint") != observed["fingerprint"]
                        or observed["verification_context_sha256"] != verification_context_hash(
                            self.store, owner["contract"], owner["task_id"], stage_id)):
                    raise ValueError("Prerequisite source or action proof changed during current verification")
                # Native work may have appeared after the verifier's gate.
                # Retain its stage-local closure guard through final mutation.
                if self._prerequisite_current_inputs(owner, stage_id) != observed:
                    raise ValueError("Prerequisite evidence changed during current verification")
                previous = observed_sources.setdefault(owner["task_id"], observed["fingerprint"])
                if previous != observed["fingerprint"] or (owner["task_id"] == task["task_id"]
                        and binding["fingerprint"] != observed["fingerprint"]):
                    raise ValueError("Prerequisite source changed between dependency observations")
                binding["prerequisites"].append(observed)
            except (ValueError, OSError, KeyError, TypeError) as ex:
                from .core import redact
                return (["Prerequisite " + label + " requires current verification: " + redact(ex)], None)
        return [], binding

    def _require_prerequisite_binding(self, task_id, stage_id, binding):
        """Compare the exact verified inputs inside the final mutation writer."""
        task = self.tasks.snapshot(task_id)
        stage = next(item for item in task["contract"]["stages"] if item["id"] == stage_id)
        prerequisites = self._prerequisite_stages(task, stage)
        if binding is None:
            if prerequisites:
                raise ValueError("Prerequisite evidence binding is required before mutation")
            return
        current = {"task_id": task_id, "stage_id": stage_id, "workspace_id": self.store.workspace_id,
            "contract_hash": task["contract_hash"], "proposal_hash": task["proposal_hash"],
            "plan_hash": self.tasks.execution_plan(task_id)["plan_hash"],
            "fingerprint": source_snapshot(self.store, task["contract"], task_id=task_id)["fingerprint"]}
        captured = binding.get("prerequisites", [])
        if (not isinstance(captured, list) or current != {key: value for key, value in binding.items() if key != "prerequisites"}
                or [(item["task_id"], stage) for item, stage in prerequisites]
                != [(item.get("task_id"), item.get("stage_id")) for item in captured]):
            raise ValueError("Prerequisite evidence changed before dependent mutation; reassess current prerequisites")
        for (owner, dependency), expected in zip(prerequisites, captured):
            observed = self._prerequisite_current_inputs(owner, dependency)
            if observed != expected:
                raise ValueError("Prerequisite evidence changed before dependent mutation; reassess current prerequisites")

    def _run(self, task_id, *, resume=False):
        task = self.tasks.snapshot(task_id)
        if task["state"] in {"BLOCKED", "PAUSED"} and not resume:
            return {"status": "BLOCKED", "blocked_reasons": ["Task is stopped or blocked; inspect current evidence and explicitly resume with `ase task resume " + task_id + "`"]}
        if resume:
            if task["state"] == "COMPLETED":
                return GateService(self.store).gate(task_id)
            with transaction(self.store.db) as db:
                stopped = read_record(db, "stop", task_id)
                if stopped["payload"]:
                    write_record(db, "stop", task_id, None, expected_revision=stopped["revision"])
        if not self.tasks.readiness(task_id)["discovery_present"]:
            return {"status": "BLOCKED", "blocked_reasons": ["Scoped environment and capability discovery is required before implementation"]}
        runner = self._runner(task_id)
        adapter = get_adapter(task["contract"]["execution_policy"]["backend"])
        caps = self._call(task_id, lambda timeout: adapter.probe(runner.config,
            deadline=time.monotonic() + timeout, cancel_requested=lambda: self._stop_requested(task_id)))
        if not caps.get("available"):
            return {"status": "BLOCKED", "blocked_reasons": [caps.get("blocker", "INDEPENDENT_SESSION_UNAVAILABLE")]}
        record_baseline(self.store, task_id, task["contract"])
        pending = list(task["contract"]["stages"])
        ordered = []
        while pending:
            ready = next(s for s in pending if set(s["depends_on"]).issubset({x["id"] for x in ordered}))
            ordered.append(ready)
            pending.remove(ready)
        stage_blockers = {}
        stage_results = {}
        for stage in ordered:
            stage_id = stage["id"]
            try:
                self._available(task_id, 1)
                current = self.tasks.snapshot(task_id)
                if current["state"] == "CANCELLED" or self.tasks.snapshot(self._root_task(task_id))["state"] == "CANCELLED":
                    raise ValueError("Task is cancelled")
                if current["contract_hash"] != task["contract_hash"] or current["proposal_hash"] != task["proposal_hash"]:
                    raise ValueError("Approved task context changed; resume against current authority")
            except ValueError as ex:
                return {"status": "BLOCKED", "blocked_reasons": [str(ex)], "stage_blockers": stage_blockers, "stage_results": stage_results}
            done = read_record(self.store.db, "stage", task_id + ":" + stage_id)["payload"] or {}
            completed_identity = (done.get("state") == "COMPLETED" and done.get("contract_hash") == task["contract_hash"]
                and done.get("proposal_hash") == task["proposal_hash"])
            revalidate_completed = (completed_identity and done.get("proof", {}).get("checks", {}).get("plan_hash")
                != self.tasks.execution_plan(task_id)["plan_hash"])
            refresh_policy = (completed_identity and not revalidate_completed
                and self._stage_policy_refresh_needed(task, done))
            if completed_identity and not revalidate_completed and not refresh_policy:
                continue
            try:
                self.tasks.require_approval(task_id, stage_id)
            except ValueError as ex:
                stage_blockers[stage_id] = [str(ex)]
                continue
            dependencies = {dep: read_record(self.store.db, "stage", task_id + ":" + dep)["payload"] or {} for dep in stage["depends_on"]}
            missing_dependencies = [dep for dep, proof in dependencies.items() if proof.get("state") != "COMPLETED"
                or proof.get("contract_hash") != task["contract_hash"] or proof.get("proposal_hash") != task["proposal_hash"]]
            if missing_dependencies:
                stage_blockers[stage_id] = ["Uncompleted stage dependencies: " + ", ".join(missing_dependencies)]
                continue
            child_blockers = GateService(self.store).required_child_blockers(task_id, stage_id)
            if child_blockers:
                stage_blockers[stage_id] = child_blockers
                continue
            try:
                if revalidate_completed:
                    # A stronger plan needs fresh proof, not a repeated effect
                    # or a new implementation author for unchanged artifacts.
                    blockers, _ = self._revalidate_prerequisites(task, stage)
                    if blockers:
                        verdict = {"status": "BLOCKED", "blocked_reasons": blockers}
                    else:
                        self.check(task_id, stage_id)
                        verdict = self.verify(task_id, stage_id)
                    if verdict["status"] == "APPROVED":
                        stage_results[stage_id] = verdict
                        continue
                    if not verdict.get("blocked_reasons"):
                        verdict = self._run_stage(task, stage, runner, adapter, resume=False)
                elif refresh_policy:
                    # Same approved artifacts, newer runtime policy: refresh the
                    # independent proof; a rejection enters the normal repair cycle.
                    verdict = self._refresh_stage_policy(task, stage, done)
                    if verdict["status"] == "APPROVED":
                        stage_results[stage_id] = verdict
                        continue
                    if not verdict.get("blocked_reasons"):
                        verdict = self._run_stage(task, stage, runner, adapter, resume=False)
                else:
                    verdict = self._run_stage(task, stage, runner, adapter, resume=resume)
            except (ValueError, OSError, KeyError, TypeError) as ex:
                from .core import redact
                verdict = {"status": "BLOCKED", "blocked_reasons": [redact(ex)]}
            if verdict is not None:
                stage_results[stage_id] = verdict
                stage_blockers[stage_id] = list(dict.fromkeys(verdict.get("blocked_reasons", []) + verdict.get("rejected_reasons", []))) or ["Stage did not complete"]
            else:
                stage_results[stage_id] = (read_record(self.store.db, "stage", task_id + ":" + stage_id)["payload"] or {}).get("gate", {"status": "APPROVED"})
        incomplete = [s["id"] for s in ordered if s["id"] in stage_blockers
            or (read_record(self.store.db, "stage", task_id + ":" + s["id"])["payload"] or {}).get("state") != "COMPLETED"]
        if incomplete:
            reasons = list(dict.fromkeys(reason for blockers in stage_blockers.values() for reason in blockers))
            for stage_id, blockers in stage_blockers.items():
                stage_results.setdefault(stage_id, {"status": "BLOCKED", "blocked_reasons": blockers})
            original = stage_results.get(ordered[0]["id"], {}) if len(ordered) == 1 else {}
            owner_decision = next((row for row in stage_results.values() if row.get("awaiting_approval")), None)
            if owner_decision:
                original = {**original, **{key: owner_decision[key] for key in
                    ('next_action', 'diagnostic_proposal_sequence', 'addition_approval', 'needs_owner_input')
                    if key in owner_decision}}
            reasons += ["Remaining stage obligations: " + ", ".join(incomplete)]
            return {**original, "status": "BLOCKED", "awaiting_approval": bool(owner_decision) or not stage_blockers,
                    "stage_blockers": stage_blockers, "stage_results": stage_results, "reasons": reasons, "blocked_reasons": reasons}
        return self._final_review(task_id)

    def _final_review(self, task_id):
        full_gate = GateService(self.store).gate(task_id)
        if full_gate["status"] == "APPROVED":
            return full_gate
        lead = full_gate.get('lead_review', {})
        if not lead.get('configured') or lead['status'] == 'NOT_REQUIRED':
            self.check(task_id)
            return self.verify(task_id)
        if not lead.get('base_current') and not lead.get('needs_owner_binding'):
            self.check(task_id)
            full_gate = GateService(self.store).gate(task_id)
            if full_gate["status"] == "APPROVED":
                return full_gate
            lead = full_gate['lead_review']
            if not lead.get('base_current') and not lead.get('needs_owner_binding'):
                return {**full_gate, 'next_action': lead['next_action']}
        if lead.get('initial_capture_blocked'):
            return {**full_gate, 'next_action': lead['next_action']}
        if lead['status'] == 'READY':
            # Only final independent judgments became stale when native lead
            # evidence changed; current successful checks need no repetition.
            return self.verify(task_id)
        # Whole-task AC/review/adequacy receipts are created by the verify that follows the lead pass.
        from .testing.adequacy import required as adequacy_required
        checks = ('independent AC/review and test adequacy'
                  if adequacy_required(self.tasks.snapshot(task_id)['contract']) else 'independent AC/review')
        return {**full_gate, 'awaiting_lead_review': True, 'awaiting_approval': False,
            'next_action': lead['next_action'].rstrip('. ') + '. After the lead review is READY, run `ase task verify '
                + task_id + '`: it runs the whole-task ' + checks + ', so their '
                'missing whole-task receipts here are expected until then.'}

    def _run_stage(self, task, stage, runner, adapter, *, resume):
        """Settle one stage; the scheduler retains its blockers and runs independent work."""
        task_id, stage_id = task["task_id"], stage["id"]
        while True:
            progress_key = task_id + ":" + stage_id
            progress = read_record(self.store.db, "cycle", progress_key)["payload"] or {}
            from .local_edit_intent import recovery_inventory
            inventory = (recovery_inventory(self, task, stage, progress, _target, EDIT_SCHEMA)
                if resume and progress.get('phase') == 'APPLYING' and progress.get('local_edit_application_id')
                else nullcontext())
            with inventory:
                # Every corrective iteration needs current prerequisite proof before
                # reserving another candidate, including explicit resumes.
                prerequisite_blockers, prerequisite_binding = self._revalidate_prerequisites(task, stage)
                if prerequisite_blockers:
                    return {"status": "BLOCKED", "blocked_reasons": prerequisite_blockers}
                sequence = proposal_sequence(self.store.db, task_id)
                candidate_feedback = current_candidate_blocker(progress, task, sequence)
                if candidate_feedback and not resume:
                    return candidate_feedback
                same_plan = (progress.get("contract_hash") == task["contract_hash"]
                             and progress.get("proposal_hash") == task["proposal_hash"])
                valid_stop = (progress.get("phase") != "NEEDS_APPROVAL"
                              or diagnostic_approval_pending(progress, task, sequence))
                diagnostic_blocker = self._diagnostic_stop_reason(progress) if same_plan and valid_stop else None
                resume_verdict = None
                recovered_application = False
                if progress.get('phase') == 'CAPTURE_BLOCKED' and not resume:
                    return {'status': 'BLOCKED', 'blocked_reasons': [progress['capture_recovery_blocker']]}
                if resume and progress.get('phase') in {'IMPLEMENTING', 'CAPTURE_BLOCKED'} and progress.get('implementation_capture'):
                    try:
                        from .candidate_capture import completed
                        reference = progress['implementation_capture']
                        if (not isinstance(reference, dict) or
                                (reference.get('task_id'), reference.get('stage_id'), reference.get('candidate_id')) != (task_id, stage_id, progress.get('candidate_id'))):
                            raise ValueError('Candidate capture reservation differs from the interrupted stage')
                        candidate = completed(self, reference)
                        self._save('implementation', task_id, candidate['receipt'])
                        self._save('stage_implementation', progress_key, candidate['receipt'])
                        if not read_record(self.store.db, 'baseline_implementation', task_id)['payload']:
                            self._save('baseline_implementation', task_id, candidate['receipt'])
                        from .local_edit_intent import prepare_local_edits
                        progress = prepare_local_edits(self, task, stage, progress, candidate['receipt'],
                            progress.get('candidate_authority'), prerequisite_binding, _target, EDIT_SCHEMA)
                    except (ValueError, OSError, KeyError, TypeError) as ex:
                        from .core import redact
                        progress.update(phase='CAPTURE_BLOCKED',
                            capture_recovery_blocker='Saved candidate capture requires reconciliation: ' + redact(ex))
                        self._save('cycle', progress_key, progress)
                        return {'status': 'BLOCKED', 'blocked_reasons': [progress['capture_recovery_blocker']]}
                    if progress.get('candidate_blocker'):
                        return progress['candidate_blocker']
                elif progress.get('phase') == 'CAPTURE_BLOCKED':
                    return {'status': 'BLOCKED', 'blocked_reasons': [progress['capture_recovery_blocker']]}
                if resume and progress.get("local_edit_application_id") and progress.get("phase") == "APPLYING":
                    from .local_edit_intent import finish_local_edits
                    progress = finish_local_edits(self, task, stage, progress, prerequisite_binding, _target, apply_edits, EDIT_SCHEMA)
                    recovered_application = True
                elif resume and progress.get("local_edit_application_id") and progress.get("phase") == "CHECKING":
                    from .local_edit_intent import finish_local_edits
                    try:
                        progress = finish_local_edits(self, task, stage, progress, prerequisite_binding,
                            _target, apply_edits, EDIT_SCHEMA, require_applied=True)
                        recovered_application = True
                    except (ValueError, OSError, KeyError, TypeError) as ex:
                        # An earlier action may legitimately have changed source.
                        # Keep the conservative observation-only recovery route;
                        # a completed local intent never authorizes file replay.
                        from .core import redact
                        progress.update(phase="VERIFYING", read_only_recovery=True,
                            local_edit_continuation_blocker="Saved local edit continuation requires reconciliation: " + redact(ex))
                        self._save("cycle", progress_key, progress)
            if resume:
                _, recovery, recovery_groups = self._recovery_context(self.store.db, task_id, stage_id)
                stopped = any(recovery["groups"][name]["stopped"] or recovery["groups"][name].get("diagnostic_inflight")
                    or (recovery["groups"][name]["probe_pending"] and not self._probe_authorized(task_id, stage_id, name, recovery["groups"][name])) for name in recovery_groups)
                inflight = any(recovery["groups"][name].get("probe_inflight") for name in recovery_groups)
                if stopped:
                    # Explicit resume can observe a changed external artifact,
                    # but does not replay actions or reserve another candidate.
                    self.check(task_id, stage_id)
                    prior_progress = progress
                    preserve_diagnostic = bool(diagnostic_blocker or (
                        prior_progress.get("phase") == "NEEDS_APPROVAL"
                        and prior_progress.get("diagnostic_outcome") == "APPROVAL_REQUIRED"))
                    progress = {"phase": "VERIFYING", "contract_hash": task["contract_hash"],
                        "proposal_hash": task["proposal_hash"], "read_only_recovery": True, "resumed_progress": True,
                        **({"diagnostic_blocker": diagnostic_blocker} if diagnostic_blocker else {})}
                    if not preserve_diagnostic:
                        self._save("cycle", progress_key, progress)
                    current = self.tasks.snapshot(task_id)
                    self.tasks.transition(task_id, "REVIEWING", expected_revision=current["revision"], reason="Read-only independent reassessment on explicit resume")
                    resume_verdict = self.verify(task_id, stage_id)
                    reconciled = self._record_recovery(task_id, stage_id, [], failed=False)
                    if not reconciled["newly_verified"] and resume_verdict["status"] != "APPROVED":
                        if preserve_diagnostic:
                            stopped = self._diagnostic_stop_verdict(prior_progress, task, sequence)
                        else:
                            stopped = {"status": "BLOCKED", "blocked_reasons": [
                                "Corrective probe remains stopped; fresh read-only checks and independent verification established no new original progress"]}
                        stopped["blocked_reasons"].extend(resume_verdict.get("blocked_reasons", []))
                        return {**stopped, "previous_gate": resume_verdict}
                    if preserve_diagnostic:
                        self._save("cycle", progress_key, progress)
                elif (inflight and not recovered_application) or (progress.get("phase") == "IMPLEMENTING"
                        and progress.get("contract_hash") == task["contract_hash"]
                        and progress.get("proposal_hash") == task["proposal_hash"]):
                    # Applying several edits and saving the next checkpoint
                    # are not atomic. Assess actual artifacts, never resubmit
                    # the reserved corrective probe after an interruption.
                    progress.update(phase="VERIFYING", contract_hash=task["contract_hash"],
                                    proposal_hash=task["proposal_hash"], read_only_recovery=True)
                    self._save("cycle", progress_key, progress)
            if (progress.get("phase") in {"DIAGNOSING", "NEEDS_APPROVAL", "DIAGNOSTIC_BLOCKED", "DIAGNOSTIC_INTERRUPTED"}
                    and progress.get("proposal_hash") == task["proposal_hash"]):
                return self._diagnostic_stop_verdict(progress, task, sequence)
            continuing = (resume and progress.get("phase") in {"CHECKING", "VERIFYING"}
                          and progress.get("contract_hash") == task["contract_hash"]
                          and progress.get("proposal_hash") == task["proposal_hash"])
            if not continuing:
                readiness = self._repair_readiness(task_id, stage_id)
                if not readiness['ready']:
                    return {'status':'BLOCKED','blocked_reasons':[readiness['reason']],
                        'next_action':readiness['next_action'],'needs_owner_input':False}
                repair_plan_sha256 = readiness.get('test_plan_sha256')
                reservation_options = ({'repair_plan_sha256': repair_plan_sha256}
                    if repair_plan_sha256 is not None else {})
                try:
                    reservation = self.reserve_cycle(task_id, stage_id, **reservation_options)
                except ValueError as ex:
                    if repair_plan_sha256 is not None:
                        readiness = self._repair_readiness(task_id, stage_id, expected_plan_sha256=repair_plan_sha256)
                        if not readiness['ready']:
                            return {'status': 'BLOCKED', 'blocked_reasons': [readiness['reason']],
                                'next_action': readiness['next_action'], 'needs_owner_input': False}
                    return {"status": "BLOCKED", "blocked_reasons": [str(ex)], "task_id": task_id}
                current = self.tasks.snapshot(task_id)
                self.tasks.transition(task_id, "EXECUTING", expected_revision=current["revision"], reason="Executing approved stage " + stage_id)
                progress = {"phase": "IMPLEMENTING", "contract_hash": task["contract_hash"], "proposal_hash": task["proposal_hash"],
                            "candidate_id": reservation["candidate_id"], "corrective_probe": reservation["corrective_probe"]}
                self._save("cycle", progress_key, progress)
                feedback = candidate_feedback or read_record(self.store.db, "gate", progress_key)["payload"]
                candidate_authority = self._stage_authority(task, stage)
                progress["candidate_authority"] = candidate_authority
                if repair_plan_sha256 is not None:
                    progress['repair_test_plan_sha256'] = repair_plan_sha256
                self._save("cycle", progress_key, progress)
                request = self._request(task_id, "implementation", EDIT_SCHEMA,
                    {"instruction": "Propose minimal approved-scope edits. Empty before_sha256 means a new file. Do not change requirements or proof state.",
                     "stage": stage, "candidate_id": reservation["candidate_id"], "previous_feedback": feedback, "diagnostic": read_record(self.store.db, "diagnostic", task_id)["payload"],
                     **({'test_plan_sha256': repair_plan_sha256} if repair_plan_sha256 is not None else {})})
                from .candidate_capture import reference
                capture, request = reference(task_id, stage_id, reservation['candidate_id'], uuid.uuid4().hex, request)
                progress['implementation_capture'] = capture
                self._save('cycle', progress_key, progress)
                launch_options, launch_denial = {}, {}
                if repair_plan_sha256 is not None:
                    @contextmanager
                    def launch_guard():
                        # Serialize the pinned investigation check with actual
                        # Popen only; probes and model waits hold no DB writer.
                        with transaction(self.store.db):
                            readiness = self._repair_readiness(task_id, stage_id, expected_plan_sha256=repair_plan_sha256)
                            if not readiness['ready']:
                                launch_denial.update(readiness)
                                raise ValueError(readiness['reason'])
                            yield
                    launch_options['launch_guard'] = launch_guard
                candidate = self._call(task_id, lambda timeout: runner.run(adapter, request,
                    timeout=timeout, cancel_requested=lambda: self._stop_requested(task_id), execution_id=capture['execution_id'],
                    **launch_options), capture_reference=capture)
                self._save("implementation", task_id, candidate["receipt"])
                # Preserve unfinished independent stages before a later stage
                # replaces the task-wide latest candidate. This is a proposal
                # receipt, not a checkpoint asserting that edits were applied.
                self._save("stage_implementation", task_id + ":" + stage_id, candidate["receipt"])
                if launch_denial:
                    return {'status': 'BLOCKED', 'blocked_reasons': [launch_denial['reason']],
                        'next_action': launch_denial['next_action'], 'needs_owner_input': False}
                if candidate["result"]["status"] != "COMPLETE":
                    return {"status": "BLOCKED", "blocked_reasons": [candidate["result"]["blocker"]]}
                if not read_record(self.store.db, "baseline_implementation", task_id)["payload"]:
                    self._save("baseline_implementation", task_id, candidate["receipt"])
                # Candidate generation may be long. Renew the actual prerequisite
                # proof after it returns, before considering any candidate writes.
                prerequisite_blockers, prerequisite_binding = self._revalidate_prerequisites(task, stage)
                if prerequisite_blockers:
                    return {"status": "BLOCKED", "blocked_reasons": prerequisite_blockers}
                # File writes share the same linearized authority boundary as
                # process creation. A replacement approval cannot authorize a
                # candidate generated under the previous scope or ancestry.
                from .local_edit_intent import prepare_local_edits, finish_local_edits
                progress = prepare_local_edits(self, task, stage, progress, candidate["receipt"],
                    candidate_authority, prerequisite_binding, _target, EDIT_SCHEMA)
                if progress.get('candidate_blocker'):
                    return progress['candidate_blocker']
                progress = finish_local_edits(self, task, stage, progress, prerequisite_binding,
                    _target, apply_edits, EDIT_SCHEMA)
            if progress["phase"] == "CHECKING" and not progress.get("read_only_recovery"):
                # Approved overlapping edits may regress a prerequisite. Assess
                # their actual result before any dependent action is launched.
                prerequisite_blockers, _ = self._revalidate_prerequisites(task, stage)
                if prerequisite_blockers:
                    return {"status": "BLOCKED", "blocked_reasons": prerequisite_blockers}
                self.execute_actions(task_id, stage)
            if resume_verdict is None:
                current = self.tasks.snapshot(task_id)
                self.tasks.transition(task_id, "VALIDATING", expected_revision=current["revision"], reason="Checking actual stage result")
                # A fresh check also reconciles drift since an interrupted call;
                # it cannot reserve another implementation attempt.
                self.check(task_id, stage_id)
                progress.update(phase="VERIFYING", fingerprint=source_snapshot(self.store, task["contract"], task_id=task_id)["fingerprint"])
                self._save("cycle", progress_key, progress)
                current = self.tasks.snapshot(task_id)
                self.tasks.transition(task_id, "REVIEWING", expected_revision=current["revision"], reason="Independent acceptance and review")
                verdict = self.verify(task_id, stage_id)
            else:
                # Rechecking here would invalidate the just-captured AC and
                # review bindings; use this same read-only observation cycle.
                verdict = resume_verdict
            if verdict["status"] == "APPROVED":
                self._record_recovery(task_id, stage_id, [], failed=False)
                self._capture_stage(task_id, stage_id, verdict)
                from .journal import Journal
                Journal(self.store).append({"kind": "stage_result", "task_id": task_id, "summary": "Verified stage " + stage_id, "status": "COMPLETED", "evidence_refs": ["stage:" + stage_id]}, event_key=task_id + ":" + stage_id + ":" + task["proposal_hash"])
                break
            if progress.get("local_edit_continuation_blocker"):
                return {"status": "BLOCKED", "blocked_reasons": [progress["local_edit_continuation_blocker"],
                    *verdict.get("blocked_reasons", []), *verdict.get("rejected_reasons", [])], "previous_gate": verdict}
            if verdict["blocked_reasons"]:
                return verdict
            progress.update(phase="REJECTED")
            self._save("cycle", progress_key, progress)
            resume = False
            failure = self._record_recovery(task_id, stage_id, verdict["rejected_reasons"], failed=not progress.get("resumed_progress", False))
            if failure["stopped"] and task["contract"]["execution_policy"].get("recovery_mode", "fixed") == "progress":
                return {"status": "BLOCKED", "blocked_reasons": ["Corrective probe made no verified progress; automatic mutation is stopped"], "previous_gate": verdict}
            if failure["diagnostic_required"]:
                try:
                    self.reserve_diagnostic(task_id, stage_id)
                except ValueError as ex:
                    return {"status": "BLOCKED", "blocked_reasons": [str(ex)]}
                current = self.tasks.snapshot(task_id)
                self.tasks.transition(task_id, "DIAGNOSING", expected_revision=current["revision"], reason="Repeated failure requires root-cause diagnosis")
                progress.update(phase="DIAGNOSING")
                self._save("cycle", progress_key, progress)
                try:
                    diagnosis = self._call(task_id, lambda timeout: runner.run(adapter, self._request(task_id, "diagnostic", DIAGNOSTIC_SCHEMA,
                        {"instruction": "Find the root cause from actual proof and prior feedback; do not propose new authority.", "failure": verdict, "stage_id": stage_id}),
                        timeout=timeout, cancel_requested=lambda: self._stop_requested(task_id)), task["contract"]["execution_policy"]["diagnostic_timeout_seconds"])
                except KeyboardInterrupt:
                    self._complete_diagnostic(task_id, stage_id, {}, interrupted=True)
                    raise
                except (ValueError, OSError, KeyError, TypeError):
                    self._complete_diagnostic(task_id, stage_id, {})
                    raise
                self._save("diagnostic", task_id, diagnosis)
                if not self._complete_diagnostic(task_id, stage_id, diagnosis):
                    stopped = read_record(self.store.db, "cycle", progress_key)["payload"]
                    return self._diagnostic_stop_verdict(stopped, task, sequence)
                progress.update(phase="REJECTED")
                self._save("cycle", progress_key, progress)
