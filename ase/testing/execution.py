"""Actual approved check execution; test design and acceptance are separate."""
import hashlib
import uuid
from ..gates import source_snapshot, stage_checks
from ..profiles import validate_evidence_spec
from ..projects import _safe_state
from ..native_paths import state_io
from ..state import transaction, hash_value, read_record
from ..store import now

from ..execution.ports import CheckContext
from .bugfix import baseline_check_binding, baseline_log, baseline_test_manifest, required as bugfix_required


BASELINE_RUNS_PER_CHECK = 4


def _capture_baseline_failures(store, task_id, bundle, source, baseline, specs):
    if (not baseline or bundle["source_changed_during_checks"]
            or source.get("check_inputs") != baseline.get("check_inputs")):
        return []
    from ..ci_check import TEST_PATH
    from .adequacy import executed_failure_cases, failed_case_sources

    original, observed = baseline["files"], source["files"]
    changed = {path: observed.get(path) for path in sorted(original.keys() | observed.keys())
               if original.get(path) != observed.get(path)}
    failures = []
    for row in bundle["results"]:
        if (row["kind"] != "test" or not row.get("exit_code") or not row.get("log_sha256") or
                any(row.get(flag) for flag in ("error", "timed_out", "cancelled", "output_limited"))):
            continue
        spec = specs[row["id"]]
        roots = [path.replace("\\", "/").rstrip("/") for path in spec.get("progress_test_sources", [])]
        # Only explicitly declared, recognisable test files may differ from
        # the approved source. Never call a changed production body a baseline.
        if any(digest is None or not TEST_PATH.search(path) or not any(
                root == "." or path == root or path.startswith(root + "/") for root in roots)
                for path, digest in changed.items()):
            continue
        try:
            output = baseline_log(store, task_id, row).decode("utf-8")
            cases = executed_failure_cases(output, spec.get("parser"))
            test_sources = failed_case_sources(cases, observed, store.repo.resolve(), row)
        except (OSError, ValueError):
            continue
        if test_sources and (manifest := baseline_test_manifest(spec, source, test_sources)):
            failures.append({**row, "fingerprint": bundle["fingerprint"], "plan_hash": bundle["plan_hash"],
                             "check_spec_sha256": hash_value(spec), "test_source_changes": changed,
                             "test_sources_sha256": hash_value(manifest), "executed_test_sources": test_sources,
                             "check_inputs_sha256": hash_value(source.get("check_inputs")),
                             "failed_cases": cases})
    return failures


def _preserve_baseline_failures(store, task_id, bundle, previous, identity, specs, captured):
    historical = (previous or {}).get("baseline_failures") or {}
    failures = {}
    if historical.get("identity") == identity:
        for row in historical.get("results", []):
            if row["id"] in specs and row.get("check_spec_sha256") == hash_value(specs[row["id"]]):
                failures.setdefault(row["id"], []).append(row)
    for row in captured:
        runs, covered = [], set()
        for prior in failures.get(row["id"], []):
            if prior.get("test_sources_sha256") == row["test_sources_sha256"]:
                try:
                    baseline_log(store, task_id, prior)
                except (OSError, ValueError, KeyError, TypeError):
                    continue  # A fresh authentic run can replace unavailable evidence.
                covered.update(prior.get("failed_cases", []))
            runs.append(prior)
        if set(row["failed_cases"]) - covered:
            # Keep the first run and the most recent runs that add case coverage.
            runs.append(row)
        failures[row["id"]] = runs[:1] + runs[1:][-(BASELINE_RUNS_PER_CHECK - 1):]
    if failures:
        bundle["baseline_failures"] = {"identity": identity, "results": [row for runs in failures.values() for row in runs]}


class CheckService:
    def __init__(self, context: CheckContext, *, target):
        self.context = context
        self.target = target

    def check(self, task_id, stage_id=None):
        context = self.context
        from ..core import execute
        with transaction(context.store.db):
            task = context.tasks.snapshot(task_id)
            authorities = {stage["id"]: context._authority_binding(task_id, stage["id"])
                           for stage in task["contract"]["stages"] if stage_id is None or stage["id"] == stage_id}
        allowed = []
        for name in authorities:
            try:
                context.tasks.require_approval(task_id, name, allow_terminal=task["state"] == "COMPLETED")
            except ValueError:
                if stage_id is not None:
                    raise
                continue
            allowed.append(name)
        if not allowed:
            raise ValueError("Checks require current proposal approval")
        if task["state"] == "COMPLETED":
            # Credential rotation permits fresh observations/checks, never replay
            # a completed resource mutation or reopen its business authority.
            from ..operations import OperationLedger
            ledger = OperationLedger(context.store)
            for stage in task["contract"]["stages"]:
                if stage["id"] not in allowed:
                    continue
                for action in stage.get("actions", []):
                    if "access" not in action:
                        continue
                    action_id = hash_value({"task": task_id, "stage": stage["id"], "action": action["id"]})
                    intent = {"project_id": context.store.project_id, "workspace_id": context.store.workspace_id, "action": action, "effect_scope": stage["scope"]}
                    operation = ledger.lookup(task_id, stage["id"], action_id, intent)
                    if operation:
                        action_id = operation["action_id"]
                        folder = context.store.root / "tasks" / task_id / "actions" / action_id
                        _safe_state(folder)
                        state_io(folder).mkdir(parents=True, exist_ok=True)
                        ledger.reconcile(action_id, context._observe_action(task_id, stage, action, folder,
                            allow_terminal=True, authority_binding=authorities[stage["id"]]))
        plan = context.tasks.execution_plan(task_id)
        before = source_snapshot(context.store, task["contract"], task_id=task_id)
        folder = context.store.root / "tasks" / task_id / "checks" / uuid.uuid4().hex
        _safe_state(folder)
        state_io(folder).mkdir(parents=True)
        results = []
        check_stages = {}
        for approved_stage in allowed:
            stage = next(s for s in task["contract"]["stages"] if s["id"] == approved_stage)
            for raw in stage_checks(task["contract"], plan["checks"], approved_stage):
                if context._stage_permits_command(stage, raw):
                    check_stages.setdefault(raw["id"], approved_stage)
                elif stage_id is not None:
                    raise ValueError("Check access or scope exceeds the explicitly selected approved stage")
        for raw in plan["checks"]:
            if raw["id"] not in check_stages:
                continue
            actual_stage = check_stages[raw["id"]]
            context.tasks.require_approval(task_id, actual_stage, allow_terminal=task["state"] == "COMPLETED")
            spec = validate_evidence_spec(raw, task["contract"]["profile"])
            if spec["kind"] == "artifact":
                path = self.target(context.store.repo.resolve(), spec["path"])
                result = {"id": spec["id"], "kind": "artifact", "path": str(path),
                          "sha256": hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None}
            elif spec["kind"] == "manual":
                # No ase command records a manual confirmation, so a check run can never prove one.
                result = {"id": spec["id"], "kind": "manual", "confirmed": False}
            else:
                result = context._call(task_id, lambda timeout: context._execute_command(task_id, spec, spec["argv"],
                    folder / (spec["id"] + ".log"), timeout, actual_stage, allow_terminal=task["state"] == "COMPLETED",
                    authority_binding=authorities[actual_stage]), spec.get("timeout", 300))
                result.update(id=spec["id"], kind=spec["kind"], parser=spec.get("parser"), configured_argv=spec["argv"])
            results.append(result)
        after = source_snapshot(context.store, task["contract"], task_id=task_id)
        bundle = {"kind": "checks", "task_id": task_id, "fingerprint": before["fingerprint"], "workspace_id": context.store.workspace_id,
                  "contract_hash": task["contract_hash"], "plan_hash": plan["plan_hash"], "results": results,
                  "source_changed_during_checks": before["fingerprint"] != after["fingerprint"], "timestamp": now()}
        if bugfix_required(task["contract"]):
            baseline, identity = baseline_check_binding(context.store, task)
            specs = {raw["id"]: raw for raw in plan["checks"]}
            captured = _capture_baseline_failures(context.store, task_id, bundle, before, baseline, specs)
            # Retain original failure coverage in the atomic update whose
            # digest the current verifier already binds.
            with transaction(context.store.db):
                previous = read_record(context.store.db, "checks", task_id)["payload"]
                _preserve_baseline_failures(context.store, task_id, bundle, previous, identity, specs, captured)
                return context._save("checks", task_id, bundle)
        return context._save("checks", task_id, bundle)
