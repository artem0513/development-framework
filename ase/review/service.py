"""Independent AC and review orchestration; no implementation or self-approval."""
import copy
from ..adapters import get_adapter
from ..evidence.representation import part_key, parts as review_parts
from ..findings import Findings, review_followup_schema
from ..gates import GateService, source_snapshot, verification_context_hash
from ..state import hash_value, read_record, transaction

from ..execution.ports import VerificationContext


def _scoped_ac_schema(base, acceptance_ids, *, enabled):
    """Constrain opt-in bugfix replies without changing legacy wire contracts."""
    if not enabled:
        return base
    schema = copy.deepcopy(base)
    criteria = schema['properties']['criteria']
    criteria.update(minItems=len(acceptance_ids), maxItems=len(acceptance_ids))
    if acceptance_ids:
        criteria['items']['properties']['id'] = {
            **criteria['items']['properties']['id'], 'enum': acceptance_ids}
    return schema


def _with_finding_history(journal, task, stage_id, part_id, schema, handoff):
    journal.seed_current_review(task, stage_id, part_id)
    history = journal.for_review(task, stage_id, part_id)
    if stage_id is None and part_id is None:
        active = {stage['id'] for stage in task['contract']['stages']}
        resolved = [row for row in journal.journal(task['task_id']) if row['stage_id'] in active
                    and row['status'] == 'RESOLVED']
        if resolved:
            handoff['resolved_stage_findings'] = resolved[-40:]
            handoff['instruction'] += (' Earlier stage closures in resolved_stage_findings are historical. '
                'Use them when judging the whole task and report a current regression only with current evidence.')
    if history:
        handoff['finding_history'] = list(history.values())
        handoff['instruction'] += (
            ' Reconcile earlier findings using handoff.finding_history. Focus the follow-up on the changed '
            'source and actual checks of each OPEN finding. List its ID in finding_followup with '
            'FIXED or DISPROVED only with concrete current evidence and its exact packet path in source. '
            'For a repair that removed the offending file, cite its removal in _ase_context/source_changes.json. '
            'Return STILL_OPEN or UNVERIFIED when the defect remains or proof is missing. For a still-current '
            'defect, report the current actionable finding and set same_finding_id to its prior ID even '
            'if the line or wording changed. RESOLVED and OWNER_DISPOSED findings are historical. Report one again only with '
            'current evidence of a regression. Use an empty same_finding_id for a genuinely new finding.')
    return review_followup_schema(schema, history), history


def _review_inputs_current(context, task_id, stage_id, approved_stage, authority, bindings):
    if context._stop_requested(task_id) or context._authority_binding(task_id, stage_id) != authority:
        return False
    task = context.tasks.snapshot(task_id)
    try:
        context.tasks.require_approval(task_id, approved_stage, allow_terminal=task['state'] == 'COMPLETED')
    except ValueError:
        return False
    checks = read_record(context.store.db, 'checks', task_id)['payload'] or {}
    if (task['contract_hash'] != bindings['contract_sha256'] or task['proposal_hash'] != bindings['proposal_sha256']
            or context.store.workspace_id != bindings['workspace_id'] or authority['plan_hash'] != bindings['plan_sha256']
            or hash_value(checks) != bindings['check_sha256'] or checks.get('source_changed_during_checks')
            or checks.get('task_id') != task_id or checks.get('workspace_id') != bindings['workspace_id']
            or checks.get('contract_hash') != bindings['contract_sha256'] or checks.get('plan_hash') != bindings['plan_sha256']
            or checks.get('fingerprint') != bindings['fingerprint']
            or source_snapshot(context.store, task['contract'], task_id=task_id)['fingerprint'] != bindings['fingerprint']
            or verification_context_hash(context.store, task['contract'], task_id, stage_id)
                != bindings['verification_context_sha256']):
        return False
    if stage_id is None:
        from ..lead_review import LeadReview
        return LeadReview(context.store).binding(task_id) == bindings.get('lead_review_sha256')
    return True


class VerificationService:
    def __init__(self, context: VerificationContext, ac_schema, review_schema):
        self.context = context
        self.ac_schema = ac_schema
        self.review_schema = review_schema

    def verify(self, task_id, stage_id=None):
        context = self.context
        task = context.tasks.snapshot(task_id)
        from ..testing.adequacy import TestAdequacy, required, ADEQUACY_SCHEMA
        adequacy = required(task['contract'], stage_id)
        test_plan = TestAdequacy(context.store).snapshot(task_id)['payload'] if adequacy else None
        if adequacy and not test_plan:
            raise ValueError('Save the original scenario/test adequacy plan before independent verification')
        if adequacy and (test_plan['contract_hash'] != task['contract_hash']
                or test_plan['proposal_hash'] != task['proposal_hash']
                or test_plan['execution_plan_hash'] != context.tasks.execution_plan(task_id)['plan_hash']):
            raise ValueError('Refresh the stale test adequacy plan before independent verification')
        approved = task["approved_stage_ids"] or ([task["contract"]["stages"][0]["id"]] if task["contract"].get("parent_task_id") else [])
        if not approved:
            raise ValueError("Independent verification requires current proposal approval")
        approved_stage = stage_id or approved[0]
        context.tasks.require_approval(task_id, approved_stage, allow_terminal=task["state"] == "COMPLETED")
        authority = context._authority_binding(task_id, stage_id)
        from ..tasks import backend_for
        runners, adapters = {}, {}
        for role in ('ac', 'review'):
            backend = backend_for(task['contract'], role)
            if backend not in adapters:
                runners[backend], adapters[backend] = context._runner(task_id, role), get_adapter(backend)
        from ..testing.bugfix import assessment_schema, required as bugfix_required
        from ..testing.adequacy import _compatible_stages
        from ..profiles import stage_evidence_specs
        checks=context.tasks.execution_plan(task_id)['checks']
        selected=[stage for stage in task['contract']['stages'] if stage_id is None or stage['id']==stage_id]
        active_ac={name for stage in selected for name in stage['acceptance_ids']}
        verification_scope={'stage_id':stage_id,
            'acceptance_ids':[row['id'] for row in task['contract']['acceptance'] if stage_id is None or row['id'] in active_ac],
            'check_ids':[row['id'] for row in stage_evidence_specs(task['contract'],checks,stage_id)],
            'scenario_ids':[row['id'] for row in (test_plan or {}).get('scenarios',[])
                if stage_id is None or stage_id in _compatible_stages(task['contract'],checks,row)],
            'bugfix_stage_ids':[stage['id'] for stage in selected if bugfix_required(task['contract'],stage['id'])]}
        active_scenarios={row['id']:row for row in (test_plan or {}).get('scenarios',[])}
        verification_scope['scenario_check_ids']={name:active_scenarios[name]['evidence_ids'] for name in verification_scope['scenario_ids']}
        verification_scope['bugfix_check_ids']={row['stage_id']:sorted({check for name in row['regression_scenario_ids'] for check in active_scenarios[name]['evidence_ids']})
            for row in (test_plan or {}).get('bugfixes',[]) if row['stage_id'] in verification_scope['bugfix_stage_ids']}
        ac_schema=_scoped_ac_schema(self.ac_schema,verification_scope['acceptance_ids'],
            enabled=bugfix_required(task['contract']))
        sessions = [('adequacy', 'review', assessment_schema(ADEQUACY_SCHEMA,task['contract'],stage_id,plan=test_plan))] if adequacy else []
        from .schema import review_schema_for
        sessions.extend((("ac", "ac", ac_schema), ("review", "review", review_schema_for(task['contract'], self.review_schema))))
        peer_instruction = (
            ' Do not wait for another verification receipt. Test adequacy runs independently in this same batch. '
            'A pending or missing adequacy receipt is expected in this packet and is checked separately by the gate. '
            'Judge the assigned criteria or code against actual test source and executed results. '
            'Report a concrete coverage gap when relevant to your judgment.'
        ) if adequacy else ''
        planned = []
        journal = Findings(context.store)
        journal_scopes = {}
        for label, role, schema in sessions:
            instruction = ("Independently assess the configured criteria against the original request, actual packet source, source_changes baseline manifest, captured before-files and command logs. Return exactly the configured criterion IDs and preserve expected wording. Evaluate original-request constraints under the relevant criteria; a missing material constraint is UNVERIFIED/BLOCKED, never an extra ignored criterion. PASS needs meaningful evidence. Broader limitations do not invalidate concrete evidenced behavior."
                           if role == "ac" else "Independently review actual source for actionable defects P0-P3. P0/P1/P2 block. Do not downgrade to obtain approval. State coverage limits in summary, or in limits when the schema has it; list in unverified_material each material change whose necessity or correctness the packet does not let you establish.")
            handoff = {'instruction': instruction, 'stage_id': stage_id,'verification_scope':verification_scope}
            if label != 'adequacy':
                handoff['instruction'] += peer_instruction
            if label == 'ac' and stage_id is None:
                from ..lead_review import LeadReview, acceptance_schema
                if LeadReview(context.store).configured(task_id):
                    schema = acceptance_schema(schema)
                    handoff['instruction'] += (' Explicitly challenge the original lead classification, its complete requirement-anchored critiques and evidence/resolutions. '
                        'Return the extra lead_review disposition. A structurally eligible but semantically complex exemption is DISAGREE; '
                        'three generic PASS summaries are INSUFFICIENT. A justified current trivial exemption may have sufficient exemption evidence without three passes. '
                        'Absent material proof is UNVERIFIED. Do not add uncontracted criterion IDs.')
            if label == 'adequacy':
                handoff.update(purpose='test_adequacy', test_plan_sha256=test_plan['test_plan_sha256'],
                    instruction='Independently assess test adequacy against original requirements, scenario plan, actual test sources/assertions and executed logs. Challenge omitted material risks and consumer boundaries. Positive counts alone do not prove adequacy. Return only the configured adequacy schema; missing required proof is UNVERIFIED/BLOCKED. '
                        'Use handoff.test_source_candidates to locate exact packet file paths for evidence.test_source. A declared private script is admissible only under its listed check ID. '
                        'Repository candidates are a shortlist; another supplied repository file is eligible if it contains the assertion actually exercised by the cited check. '
                        'Use logs and check summaries to substantiate execution. The test_source must be the current source body: exclude logs, summaries, diffs and before versions. '
                        'A build alone cannot show a behavioral test ran. When case-level execution is required, cite an observed successful case; otherwise report required proof you cannot establish as UNVERIFIED. '
                        'missing_coverage and additional_scenarios block completion: list there only material gaps of '
                        'handoff.verification_scope (a material ambiguity counts), and put optional suggestions and low-risk '
                        'notes in summary.' + (' Judge only this stage\'s scenarios; other stages are verified separately.'
                        if stage_id is not None else ''))
            if bugfix_required(task['contract'],stage_id):
                handoff['instruction'] += (' Independently challenge the bugfix investigation: trace the real caller, boundary, callee and authoritative contract to the first divergence; distinguish a confirmed cause from a symptom or workaround. '
                    'Establish why this correction is necessary, including alternatives to any new merger/wrapper/cache/retry and preservation of other legitimate response fields. '
                    'Inspect a discriminating original-failure regression through the actual consumer, grounded fixtures and before/after or supported fault-injection evidence. Helper-only green tests are insufficient. '
                    'In _ase_context/actual_checks.json, baseline_failures names executed checks from the original source; log_packet_path points to the verified before-fix log. Compare it with the current matching check and confirm that both exercise the named consumer case. '
                    'For adequacy return separate cause, necessity and regression judgments for each active software stage. Missing proof is UNVERIFIED/BLOCKED; confirmed inadequacy is FAIL. Do not invent missing facts or require destructive live reproduction.')
            handoff['instruction'] += (' The host-computed verification_scope names the obligations CURRENT at this gate. '
                'AC criteria IDs come ONLY from acceptance_ids; never put scenario IDs or stage IDs in criteria. Adequacy scenario IDs come ONLY from scenario_ids, and its bugfix stage IDs ONLY from bugfix_stage_ids. '
                'Use scenario_check_ids and bugfix_check_ids for the corresponding evidence.check_id values; an unrelated current check is not mapped proof. '
                'For a stage these sets may be empty; do not require a downstream check or a causal judgment outside this stage schema as current missing_coverage. '
                'Keep later/unselected obligations explicitly pending in limitations or summary, never claim they passed. At a whole-task gate all configured mandatory obligations remain current. '
                'Missing CURRENT evidence still blocks; substantive defects observable in current source remain actionable findings regardless of the later execution schedule.')
            if review_parts(task['contract']) and label == 'review':
                # Only code review is split: the whole-task review judges the cross-part level. Acceptance and test
                # adequacy are not told to ignore bodies they lack; their missing proof stays blocking.
                handoff['instruction'] += (' Changed-file bodies marked REVIEWED_IN_PART in _ase_context/changed_files.json '
                    'are code-reviewed by separate independent part sessions that the gate requires; judge the contract, '
                    'evidence and cross-part level and do not report those bodies as your gap. '
                    'Inspect context_only files for interactions that matter to this judgment.')
            # Requests (packets, DB reads) are built here on the owning thread; sessions run below.
            if label == 'review':
                schema, history = _with_finding_history(journal, task, stage_id, None, schema, handoff)
                journal_scopes[label] = (None, history)
            backend = backend_for(task['contract'], role)
            planned.append((label, runners[backend], adapters[backend], context._request(task_id, role, schema, handoff)))
        for part in review_parts(task['contract']):
            # Owner-approved split: each part's changed files get their own independent review session.
            handoff = {'instruction': ('Independently review actual source for actionable defects P0-P3. P0/P1/P2 block. '
                'Do not downgrade to obtain approval. This packet is review part ' + part['id'] + ' of an owner-approved split: '
                'review the changed files of this part (see _ase_context/changed_files.json); files of other parts (counted in in_other_parts) are '
                'reviewed by other independent sessions and are not your coverage gap. Inspect context_only files for interactions '
                'with this part. Their changes belong to the named part. State coverage limits in summary, or in '
                'limits when the schema has it; list in unverified_material each material change of this part whose necessity '
                'or correctness the packet does not let you establish.' + peer_instruction),
                'stage_id': stage_id, 'verification_scope': verification_scope, 'review_part': part}
            backend = backend_for(task['contract'], 'review')
            label = 'review_part:' + part['id']
            schema, history = _with_finding_history(journal, task, stage_id, part['id'],
                                                   review_schema_for(task['contract'], self.review_schema), handoff)
            journal_scopes[label] = (part['id'], history)
            planned.append((label, runners[backend], adapters[backend],
                            context._request(task_id, 'review', schema, handoff)))
        from . import blind
        if blind.enabled(task['contract']):
            # The blind pass sees the change and its base, not the request, proposal or earlier verdicts.
            backend = backend_for(task['contract'], 'review')
            planned.append((blind.LABEL, runners[backend], adapters[backend], context._request(task_id, 'review',
                review_schema_for(task['contract'], self.review_schema),
                {'instruction': blind.INSTRUCTION, 'stage_id': stage_id, 'purpose': blind.PURPOSE})))
        results, error = context._call(task_id, lambda timeout: _run_sessions(context, task_id, planned, timeout))
        # Completed sessions keep their receipts even when a sibling failed, so a retry is visible
        # and the gate judges each receipt on its own bindings.
        implementation = read_record(context.store.db, 'implementation', task_id)['payload'] or {}
        for label, _, _, request in planned:
            if label in results and (error is None or results[label]['receipt'].get('status') == 'COMPLETE'):
                receipt = results[label]['receipt']
                if label.startswith('review_part:'):
                    kind, key = 'verification_part', part_key(task_id, stage_id, label.split(':', 1)[1])
                elif label == blind.LABEL:
                    kind, key = 'verification_blind', blind.key(task_id, stage_id)
                else:
                    kind, key = 'verification', task_id + ':' + label + (':' + stage_id if label == 'adequacy' and stage_id else '')
                if label not in journal_scopes:
                    context._save(kind, key, receipt)
                    continue
                with transaction(context.store.db):
                    if receipt.get('status') == 'COMPLETE' and receipt.get('evidence_kind') == 'independent_cli':
                        part_id, history = journal_scopes[label]
                        session = receipt.get('session_id')
                        others = [implementation, *(result['receipt'] for other, result in results.items() if other != label)]
                        try:
                            if (not session or any(session == other.get('session_id') or
                                    receipt['execution_id'] == other.get('execution_id') for other in others)):
                                raise ValueError('The review reused another role or lacked an independent provider session')
                            journal.record_review(task, stage_id, part_id, receipt, presented=history, expected_request=request,
                                check_current=lambda: _review_inputs_current(context, task_id, stage_id, approved_stage,
                                                                              authority, request['bindings']))
                        except (ValueError, OSError, KeyError, TypeError) as journal_error:
                            from ..core import redact
                            journal.withhold(task_id, stage_id, part_id, receipt, redact(str(journal_error))[:1000])
                    context._save(kind, key, receipt)
        if error is not None:
            raise error
        return GateService(context.store).gate(task_id, stage_id=stage_id)


def _run_sessions(context, task_id, planned, timeout):
    """Run independent sessions concurrently in one accounted call; each stays its own process.

    SQLite is only touched on the owning thread: the stop request is polled here and handed to
    the workers through an event.
    """
    if len(planned) == 1:
        label, runner, adapter, request = planned[0]
        return {label: runner.run(adapter, request, timeout=timeout, cancel_requested=lambda: context._stop_requested(task_id))}, None
    import concurrent.futures
    import threading
    stop = threading.Event()
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(planned)) as pool:
        futures = {label: pool.submit(runner.run, adapter, request, timeout=timeout, cancel_requested=stop.is_set)
                   for label, runner, adapter, request in planned}
        pending = set(futures.values())
        while pending:
            done, pending = concurrent.futures.wait(pending, timeout=0.5)
            # A failed sibling or a stop request cancels the remaining sessions.
            if pending and not stop.is_set() and (any(f.exception() for f in done) or context._stop_requested(task_id)):
                stop.set()
        results, error = {}, None
        for label, future in futures.items():
            if future.exception() is None:
                results[label] = future.result()
            elif error is None:
                error = future.exception()
        return results, error
