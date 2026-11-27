# Evidence, readiness and decisions

Use relevant sections for comparisons, blockers, dependencies, actionable artifacts, semantic changes or review findings. This refines the existing workflow; it adds no universal form, new approval or mandatory service. Clear authorized work proceeds. Use the [evidence brief](../assets/evidence-brief.md) when several material facts must stay aligned; otherwise retain them in existing notes.

## 1. Establish the target and reference

Identify the actual resource and relevant environment, tenant/account and consumer. For a comparison or migration, identify the reference's role and why it is authoritative for the requested result: an applicable owner decision, contract or verified provenance. Record its locator and current version. Deployed configuration proves what is used; availability, similar names and a colleague's implementation do not prove the right business baseline.

Separate inherited behavior/settings from changes introduced by this task. Reuse compatible project conventions; avoid an override solely because an inherited value differs from a guess. Investigate a material conflict with the approved target. Escalate an unresolved owner-only choice using the existing short question format. An unconfirmed reference does not authorize changing the target or unrelated systems.

## 2. Bound the claim and prove the blocker

A blocking claim names the required outcome, affected stage/dependents, and actual observation: operation, target, safe account/access-method reference, time/version, result and inspected scope. Keep credentials and raw responses out of notes. Distinguish a known failure from an unperformed check.

"Object not found in schema S with account A" is bounded evidence. "The object does not exist" needs corresponding coverage. One denied login or failed optional adapter does not prove the application is broken or all access is impossible. Try a concrete useful authorized alternative when available; an optional integration failure does not block usable CLI/file work. Do not request new access or change permissions silently.

Continue research while a specific lead can distinguish hypotheses. Stop on sufficient evidence, no useful lead, unavailable required access, an owner-only decision or an explicit stop/limit. Record the stopping reason and remaining unknowns; exhausted inspected sources are not absence everywhere. Then proceed with a supported in-scope choice or ask one material question: checked facts, unconfirmed hypothesis, recommendation/reason and decision needed. Hold affected work and continue independent approved work. Reconcile UNKNOWN external effects rather than replaying them.

A useful authorized alternative or a supported in-scope choice never includes fixing a defect outside the ticket that this task did not cause, even when it blocks your check: judge the criterion's goal under the conditions of [out-of-scope discovery](../SKILL.md#out-of-scope-discovery) and report the defect in one line.

## 3. Check the readiness the obligation actually needs

These are separate observations, not an automatic promotion ladder:

| Observation | What it establishes |
| --- | --- |
| Promised/announced | Delivery is unverified. |
| Exists/visible in metadata | The object/configuration is visible in the inspected scope. |
| Usable by intended account | The required authorized operation actually succeeds through the relevant access path. |
| Structurally complete | Required fields, interfaces and version are present. |
| Data suitable for this task | Bounded actual data/postconditions meet the task requirements. |
| Consumer behavior verified | A meaningful consumer-level check demonstrates required behavior. |

Record only the levels needed for the affected obligation, with evidence and unknowns. Installed tools are not usable sessions. Metadata is not a successful SELECT. Readable empty data is valid when the contract allows emptiness; it is not proof of representative data or correct calculations. Probe the relevant intended account/consumer within existing authority; do not broaden access or copy secrets. Once required postconditions are evidenced, proceed without a ceremonial checklist or new approval.

Bind required proof to the phase and action that can produce it. A current gate judges evidence causally attainable before its authorization; a criterion it judges must not require that same AC/review/gate approval or the later effects that approval authorizes. Independent AC/review and the current APPROVED gate remain mandatory authorization checks. If proof depends on the authorized action, retain that obligation as mandatory later work, leave its result pending, and verify it after the action before declaring the overall assignment complete.

Applying a migration, backfill or delete to shared or persistent data requires a backup, dry run on a copy and rollback plan, plus authorization for the exact target. Source preparation is separate.

If Publisher requires the whole-task gate, moving later proof to another stage of that same task does not remove the cycle. Scope its gated contract to source acceptance and retain the later mandatory obligations in an explicit publication/finish plan or separate delivery task. As applicable, these include actual publication, intended-consumer postconditions, matching remote/local SHA and dated closure. Source approval alone does not complete the overall assignment. Correcting this phase allocation within existing authorized scope needs no new approval; preserve every original obligation, independent verification and the gate, and never label future evidence PASS.

## 4. Separate artifact purpose, state and version

For an artifact that may be executed, handed off or used as evidence, retain:

- Purpose: discussion draft, reference/example, or intended execution/delivery.
- State: draft, prepared, applied/delivered, or independently verified, supported by actual evidence.
- Identity: path/object, content hash or meaningful version, target and relevant requirement/dependency source versions.
- Unresolved placeholders/assumptions and the current next action.

A reference CREATE script for an existing object is not a command to execute. Prepared text is not an applied resource. A filename such as final.sql is not readiness evidence. Before authorized execution or execution-ready handoff, inspect exact current bytes/version, target, required inputs and unresolved placeholders. A legitimate runtime parameter is not an unresolved placeholder. Check copied text for meaningful encoding/invisible-character corruption; do not strip whitespace or rewrite strings/identifiers blindly. Reuse authorization when its scope still applies; preparation/verification grants no external action by itself.

After a material answer or changed source, identify affected assumptions, artifact versions, acceptance checks and cached conclusions. Refresh these and their dependents, mark old versions historical/superseded, and update the current brief before the next action. Keep unaffected work valid and preserve evidence of already-applied effects; refreshing a draft does not authorize replay. Use existing intake/checkpoint/knowledge fields and source references. The brief is an optional view, not a new schema or second tracker.

## 5. Verify meaning when changing implementation

For migration, translation or optimization, identify required preserved behavior and its authority. Compare old/reference and proposed behavior using relevant examples: normal/boundary cases, null/missing values, duplicates, errors, ordering, rounding/units/time windows or permission effects as applicable. Use meaningful differential checks and the actual consumer contract. HTTP success, matching columns or one happy-path row alone do not prove equivalence.

A documented platform capability is a hypothesis about this execution path until checked. A narrowing filter needs evidence that excluded data cannot affect the required result. Separate a new regression from ambiguity already in the reference. Preserve supported behavior within scope; do not silently invent a business rule to resolve ambiguity. Hold only a material unresolved choice and its dependents, with a concise recommendation; continue independent work.

## 6. Substantiate and respond to review

Trace the current trigger through caller, boundary, callee, contract and observations; cite the first divergence/consequence. Missing code is a coverage gap to investigate, never a defect. Unknowns stay UNVERIFIED/BLOCKED.

For new software bugs use `--bugfix`. Distinguish symptom, cause and workaround. Justify the correction and new layers against the contract; preserve legitimate behavior. Use grounded consumer regressions with baseline/fault-injection evidence: helper-only green tests are insufficient. Continue useful authorized diagnostics before owner questions. See [bugfix procedure](bugfix-evidence.md) on demand.

Repair confirmed in-scope findings, rerun affected checks and fresh separate AC/review. Reject disproven findings with counterproof retained in history. Never speculate or downgrade P0/P1/P2 for approval. Preserve scope/recovery limits.

## 7. Wording and scope growth

Ambiguous wording read one way is an assumption. Declare new background, polling, global UI or shared-service changes in proposal `effects` before code ([procedure](scope-growth.md)); reviewers judge their necessity.
