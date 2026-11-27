"""Approved command/access effects and observation through the operation ledger."""
import json
import uuid
from ..gates import source_snapshot, stage_checks
from ..profiles import validate_access
from ..projects import _safe_state
from ..native_paths import state_io
from ..state import append_event, encode, hash_value, text, transaction

from ..execution.ports import ActionContext


class ActionService:
    def __init__(self, context: ActionContext):
        self.context = context

    def access_stage(self, task_id, spec, stage_id=None, *, allow_terminal=False):
        context = self.context
        from ..credentials import CredentialError
        access = validate_access(spec["access"])
        task = context.tasks.snapshot(task_id)
        plan = context.tasks.execution_plan(task_id)
        for stage in task["contract"]["stages"]:
            if stage_id is not None and stage["id"] != stage_id:
                continue
            if access["target"] not in stage["scope"]["resources"]:
                continue
            selected = stage_checks(task["contract"], plan["checks"], stage["id"])
            declared = [*selected, *stage.get("actions", [])]
            if not any(item.get("id") == spec.get("id") and item.get("access") == access for item in declared):
                continue
            try:
                context.tasks.require_approval(task_id, stage["id"], allow_terminal=allow_terminal)
            except ValueError:
                if stage_id is not None:
                    raise
                continue
            return stage
        raise CredentialError("access_scope_denied")

    def access_client(self):
        context = self.context
        from ..access import CredentialAccess
        from ..credentials import CredentialVault
        return CredentialAccess(CredentialVault(context.store.state_home))

    def execute_command(self, task_id, spec, argv, log, timeout, stage_id=None, *, allow_terminal=False, prerequisite_binding=None, authority_binding=None, launch_witness=None):
        context = self.context
        from ..core import execute
        if stage_id is None:
            raise ValueError("Command requires its actual approved stage")
        task = context.tasks.snapshot(task_id)
        stage = next(s for s in task["contract"]["stages"] if s["id"] == stage_id)
        if not isinstance(argv, (list, tuple)):
            raise ValueError("Command argv must name an approved recipe")
        argv = list(argv)  # Pin the actual recipe before authentication/launch.
        allowed = [spec["argv"]]
        if spec in stage.get("actions", []):
            allowed.append(spec["observation_argv"])
        if argv not in allowed:
            raise ValueError("Command argv differs from its approved recipe")
        if (context._prerequisite_stages(task, stage) and spec in stage.get("actions", [])
                and list(argv) == spec["argv"] and prerequisite_binding is None):
            raise ValueError("Dependent effect requires current prerequisite evidence binding")
        cancel, launch_guard = context._command_control(task_id, spec, stage_id, allow_terminal=allow_terminal,
            prerequisite_binding=prerequisite_binding, authority_binding=authority_binding)
        effect_guard = launch_witness.guard(launch_guard) if launch_witness is not None else launch_guard
        if "access" not in spec:
            return execute(argv, context.store.repo, log, timeout, cancel_requested=cancel, launch_guard=effect_guard)
        context._access_stage(task_id, spec, stage_id, allow_terminal=allow_terminal)
        access = spec["access"]
        receipt = context._access_client().run(access["credential_ref"], access["target"], argv, context.store.repo,
            log, timeout=timeout, cancel_requested=cancel, launch_guard=launch_guard, effect_launch_guard=effect_guard)
        with transaction(context.store.db) as db:
            append_event(db, "access:" + uuid.uuid4().hex, "access_used", task_id, receipt["access"])
        return receipt

    def declared_access(self, task_id, stage_id, access):
        context = self.context
        from ..credentials import CredentialError
        validate_access(access)
        task = context.tasks.snapshot(task_id)
        stage = next((s for s in task["contract"]["stages"] if s["id"] == stage_id), None)
        if stage is None:
            raise CredentialError("access_scope_denied")
        declared = [*stage_checks(task["contract"], context.tasks.execution_plan(task_id)["checks"], stage_id), *stage.get("actions", [])]
        spec = next((item for item in declared if item.get("access") == access), None)
        if spec is None:
            raise CredentialError("access_not_planned")
        context._access_stage(task_id, spec, stage_id)
        return spec

    def ensure_access(self, task_id, stage_id, access):
        context = self.context
        with context._ownership(task_id):
            with transaction(context.store.db):
                context.tasks.require_approval(task_id, stage_id)
                authority = context._authority_binding(task_id, stage_id)
            spec = context._declared_access(task_id, stage_id, access)
            cancel, launch_guard = context._command_control(task_id, spec, stage_id, authority_binding=authority)
            return context._call(task_id, lambda timeout: context._access_client().ensure(
                access["credential_ref"], access["target"], timeout=timeout,
                cancel_requested=cancel, launch_guard=launch_guard), 60)

    def request_access(self, task_id, stage_id, access, path='/', method='GET'):
        context = self.context
        with context._ownership(task_id):
            with transaction(context.store.db):
                context.tasks.require_approval(task_id, stage_id)
                authority = context._authority_binding(task_id, stage_id)
            spec = context._declared_access(task_id, stage_id, access)
            cancel, launch_guard = context._command_control(task_id, spec, stage_id, authority_binding=authority)
            return context._call(task_id, lambda timeout: context._access_client().request(
                access["credential_ref"], access["target"], path=path, method=method, timeout=timeout,
                cancel_requested=cancel, launch_guard=launch_guard), 30)

    def observe_action(self, task_id, stage, action, folder, *, allow_terminal=False, authority_binding=None):
        context = self.context
        with transaction(context.store.db):
            current_authority = context._command_authority(task_id, action, stage["id"], allow_terminal=allow_terminal)
            authority = current_authority if authority_binding is None else authority_binding
            if current_authority != authority:
                raise ValueError("Approved observation authority changed during preparation")
            task = context.tasks.snapshot(task_id)
            declared_stage = next(item for item in task["contract"]["stages"] if item["id"] == stage["id"])
            if "scope" in stage and stage["scope"] != declared_stage["scope"]:
                raise ValueError("Observation stage scope differs from current declaration")
            provenance = {"version": 2, "kind": "observation", "task_id": task_id, "stage_id": stage["id"],
                "project_id": context.store.project_id, "workspace_id": context.store.workspace_id,
                "contract_hash": task["contract_hash"], "proposal_hash": task["proposal_hash"], "plan_hash": authority["plan_hash"],
                "action_sha256": hash_value(action), "argv_sha256": hash_value(action["argv"]),
                "observation_argv_sha256": hash_value(action["observation_argv"]), "declared_stage": declared_stage,
                "authority": authority, "execution_plan_checks": context.tasks.execution_plan(task_id)["checks"]}
        proof = context._call(task_id, lambda timeout: context._execute_command(task_id, action,
            action["observation_argv"], folder / (uuid.uuid4().hex + ".log"), timeout, stage["id"],
            allow_terminal=allow_terminal, authority_binding=authority), action.get("timeout", 300))
        if proof.get("exit_code") != 0 or proof.get("error") or proof.get("timed_out") or proof.get("cancelled"):
            raise ValueError("Resource observation failed; effect remains UNKNOWN")
        try:
            value = json.loads(state_io(proof["log"]).read_text(encoding="utf-8"))
        except ValueError:
            raise ValueError("Resource observation is not structured JSON; effect remains UNKNOWN") from None
        if (not isinstance(value, dict) or type(value.get("exists")) is not bool
                or (value["exists"] and (not isinstance(value.get("resource_id"), str)
                    or "[REDACTED]" in value["resource_id"]))):
            raise ValueError("Resource observation must prove exists and a nonsecret resource_id")
        observation = {"exists": value["exists"], "resource_id": value.get("resource_id"), "proof_ref": proof["log"] + "#sha256=" + proof["log_sha256"],
                       "action_hash": hash_value(action), "observing_task_id": task_id, "observing_stage_id": stage["id"],
                       "observing_workspace_id": context.store.workspace_id}
        if "absence_confirmed" in value:
            if type(value["absence_confirmed"]) is not bool:
                raise ValueError("Resource absence_confirmed must be boolean")
            observation["absence_confirmed"] = value["absence_confirmed"]
        if "provider_attempt_id" in value:
            text(value["provider_attempt_id"], "Provider attempt ID", limit=2000)
            if "[REDACTED]" in value["provider_attempt_id"]:
                raise ValueError("Provider attempt identity must be nonsecret and actual")
            observation["provider_attempt_id"] = value["provider_attempt_id"]
        if "access" in proof:
            observation["access"] = proof["access"]
        provenance["command_receipt"] = proof
        provenance["command_receipt_sha256"] = hash_value(proof)
        provenance["sha256"] = hash_value(provenance)
        observation["provenance"] = provenance
        return observation

    def execute_actions(self, task_id, stage):
        context = self.context
        """Probe actual state before every effect, including uncertain resumes."""
        if context.store.db.in_transaction:
            raise ValueError('Action execution requires a committed write-ahead boundary')
        from ..core import execute
        from ..operations import OperationLedger
        task = context.tasks.snapshot(task_id)
        authority = context._stage_authority(task, stage)
        ledger = OperationLedger(context.store)
        results = []
        for action in stage.get("actions", []):
            intent = {"project_id": context.store.project_id, "workspace_id": context.store.workspace_id, "action": action, "effect_scope": stage["scope"]}
            action_id = hash_value({"task": task_id, "stage": stage["id"], "action": action["id"]})
            prepared = ledger.prepare(task_id, stage["id"], action_id, intent)
            action_id = prepared["action_id"]
            folder = context.store.root / "tasks" / task_id / "actions" / action_id
            _safe_state(folder)
            state_io(folder).mkdir(parents=True, exist_ok=True)

            def observe():
                observation = context._observe_action(task_id, stage, action, folder, authority_binding=authority)
                if context._stage_authority(task, stage) != authority:
                    raise ValueError("Approved action authority changed during observation")
                return observation

            observation = observe()
            reconciled = ledger.reconcile(action_id, observation)
            if reconciled["state"] == "UNKNOWN":
                raise ValueError("External effect remains UNKNOWN; reconcile authoritative state before retry")
            if reconciled["state"] == "ABSENT":
                # A prior action or its observation may have changed a shared
                # prerequisite since the stage's candidate was checked.
                prerequisite_blockers, prerequisite_binding = context._revalidate_prerequisites(task, stage)
                if prerequisite_blockers:
                    raise ValueError("; ".join(prerequisite_blockers))
                action_source = source_snapshot(context.store, task["contract"], task_id=task_id)
                execution_context = {"version": 2, "task_id": task_id, "stage_id": stage["id"], "workspace_id": context.store.workspace_id,
                    "project_id": context.store.project_id, "contract_hash": task["contract_hash"], "proposal_hash": task["proposal_hash"],
                    "plan_hash": authority["plan_hash"], "action_sha256": hash_value(action), "argv_sha256": hash_value(action["argv"]),
                    "declared_stage": stage, "authority": authority,
                    "execution_plan_checks": context.tasks.execution_plan(task_id)["checks"],
                    "source_fingerprint_before": action_source["fingerprint"], "source_files": action_source["files"],
                    "source_files_sha256": hash_value(action_source["files"])}
                if len(encode(execution_context).encode()) > 131072:
                    raise ValueError("Action source evidence exceeds bound before submission")
                with transaction(context.store.db):
                    if context._stage_authority(task, stage) != authority:
                        raise ValueError("Approved action authority changed during prerequisite verification")
                    context._available(task_id, action.get("timeout", 300))
                    launch_witness = ledger.start_launch(action_id)
                # UNKNOWN above commits before authentication or possible Popen.
                # Only this completed host scope may certify actual nonlaunch;
                # process entry or a hard crash leaves the attempt UNKNOWN.
                try:
                    with launch_witness:
                        execution = context._call(task_id, lambda timeout: context._execute_command(task_id, action,
                            action["argv"], folder / (uuid.uuid4().hex + ".log"), timeout, stage["id"],
                            prerequisite_binding=prerequisite_binding, authority_binding=authority,
                            launch_witness=launch_witness), action.get("timeout", 300))
                finally:
                    if launch_witness.phase == 'NOT_LAUNCHED':
                        ledger.record_nonlaunch(launch_witness)
                if launch_witness.phase == 'NOT_LAUNCHED':
                    raise ValueError('Effect command did not reach process launch')
                ledger.record_submission(action_id, execution, context=execution_context)
                observation = observe()
                ledger.reconcile(action_id, observation)
                if execution.get("exit_code") != 0 or not observation["exists"]:
                    raise ValueError("Infrastructure action did not establish its actual postcondition")
            results.append(observation)
        return results
