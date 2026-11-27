# Confirm the cause and justify the correction

Apply to software bugfixes, including in-scope defects found during implementation/review.
A removed symptom, reachable endpoint or green helper test does not establish
the cause or justify a new layer. Mergers, retries, wrappers and caches can be
correct when the actual contract requires them; this is no blanket prohibition.

## Before a causal repair

1. Establish expected behavior from original requirements and the applicable
   contract. Trace the real caller through relevant boundaries and callee to the
   result the consumer uses. For a round trip compare outgoing object/wire
   request, raw response, deserialized object and consumer result where relevant.
   Do not log credentials or sensitive payloads.
2. Locate the first divergence supported by source and observations. Separate
   symptom, candidate cause and disproven alternatives. Use a safe reproduction,
   sanitized capture or discriminating fault injection. No forced destructive
   production reproduction. Continue useful authorized probes under existing
   recovery rules; no endless research or premature owner questions.
3. Explain why the correction belongs at this boundary. Before adding a new
   merger/wrapper/cache/retry/override, background process, timer/polling, global UI
   effect or shared-service change, inspect whether direct response use, existing
   mapping or existing project facilities meet the contract, and record the
   [scope-growth justification](scope-growth.md). Preserve
   other legitimate fields/behavior. Full-object responses differ from partial
   patches; omission, null and false/zero may differ. Do not invent an allowlist
   from one example.

For new managed bugfixes use `ase task create --file task.json --bugfix`, or
explicit `bugfix_evidence: {version:1, required:true}` plus required test adequacy
for programmatic creators. Do not silently rewrite legacy contracts or turn
unrelated feature/document tasks into bugfixes. For a defect within an existing
task, update its relevant scenarios; enable the policy through explicit revision
for a distinct bugfix scope. A defect outside the task (not in its ticket, not
caused by its changes) is reported in one line, never fixed
([out-of-scope discovery](../SKILL.md#out-of-scope-discovery)). Routine choices
and useful diagnostics need no additional owner approval interview.

Each cited test case must appear as successfully executed in the bound run,
not merely exist in source beside a green command. Current matching supports
verbose unittest case identities tied to the actual module/discovery root or
exact invoked script, and pytest node IDs with statuses. Quiet counts and
unsupported formats leave the specific case unverified: collect suitable
case-level output or a supported report internally, without an owner interview.
Never credit a different, failed or skipped case as the consumer regression.

Run the failing case through the approved `ase task check` before repairing the production
code. Strict bugfixes retain an executed failure for the unchanged check recipe, including
when unrelated checks are added to the plan. A regression test added before the fix can be
recorded when only declared `progress_test_sources` test files differ from the approval
baseline. Their recorded bytes must still match when the log is supplied. In the review
packet, `baseline_failures` in `_ase_context/actual_checks.json` names historical runs and
their `log_packet_path` when the exact bounded log is available. A later run that adds a
failed case can be retained alongside the first. Compare the actual case in the before and
after logs. For other reproductions, supply equivalent approved
fault-injection evidence.

The managed fresh causal-repair launch requires a current saved investigation
with a confirmed cause; it reserves no model candidate when that proof is not
ready. This is an internal investigation handoff, not an instruction to stop or
ask the owner. Continue useful authorized probes, including small reversible
diagnostic tests/instrumentation, before launching the causal repair. Preserve
their provisional nature and results. That refused `task run` leaves the task
BLOCKED: save the investigation with `ase adequacy plan TASK --file PLAN.json
--expected-revision N` (revision from `ase adequacy status TASK`), then continue
with `ase task resume TASK`; a repeated `task run` is refused until resume.
Resume/reconciliation of existing effects
is separate and must not be blocked or replayed by this fresh-candidate check.
An empty criterion list alone does not make a phase coordination: an editing
phase maps to its nearest causal acceptance descendants (or prerequisites for
delivery after validation). Their investigations must be current and confirmed.
Truly nonmutating coordination needs no fictitious regression. If no related
acceptance boundary exists, refine the technical stage mapping autonomously;
do not bypass the investigation or ask the owner about this routine plan detail.

The existing hashed scenario plan accepts `bugfixes`, one investigation for each
software stage with acceptance obligations (not empty coordination phases):
`stage_id`, `symptom`, `expected`, `first_divergence`,
`cause_status`, `trace`, `causal_evidence`, `fix_rationale`, `alternatives`,
`regression_scenario_ids`. Trace entries contain captured relative `path`, a
stable actual source `locator`, and `observation`. The `locator` must be literal
text copied from that file: an exact, case-sensitive substring of the captured
content (not a line number, symbol path or paraphrase). Optional `version` is `current`
(default) or `before`, using only the host-authenticated original source capture.
Use `before` when the repair removes/renames the faulty code; never rewrite an
original observation to pretend it describes current code. Regression IDs name required
scenarios compatible with the stage. Save through `ase adequacy plan`.

Use `CONFIRMED` only with discriminating evidence. `HYPOTHESIS` and `WORKAROUND`
are valid saved investigation states, but cannot establish a verified causal
repair. Continue safe supported investigation. When no useful authorized lead
remains, report the exact unknown and concise hypothesis. A temporary mitigation
is not a resolved root cause. Ask only for a material remaining access, contract
or business decision after checking retrievable facts.

## Regression and independent challenge

Test the original trigger through the actual relevant caller/consumer boundary,
including other legitimate returned changes. The new helper's test alone is
supplementary. Ground mocks/fixtures in an authoritative contract or observed
sanitized response. Show why the regression would fail under the original causal
defect with a captured baseline or relevant controlled broken variant. Tests can
faithfully preserve an invented requirement; green counts do not settle it.

Independent adequacy returns separate `cause`, `necessity` and `regression`
assessments for every active bugfix stage, with current executed test proof under
the configured schema. Independently inspect source, actual assertions and logs;
the dossier is a claim, not a verdict. AC and substantive review challenge these
claims too. Confirmed inadequacy is FAIL; missing causal/contract proof is
UNVERIFIED/BLOCKED. Do not invent defects from absent evidence or ask the owner
to choose ordinary technical details that evidence already settles.

Use the host's `verification_scope` to distinguish current stage obligations
from downstream acceptance. An editing phase with no acceptance criteria may
have current executed smoke checks without the later causal assessment schema.
Keep later proofs pending, not current missing coverage or an invented PASS.
The whole-task gate still requires every configured mandatory obligation.

The local gate checks structure, current receipts, source locators and required
judgments. It cannot mechanically establish semantic causality, exhaustive
coverage or honest same-user attestations. Keep these limits explicit.

Fresh repair admission pins the confirmed investigation before reservation and
rechecks the identical plan at actual candidate process creation under a short
transaction. The writer lock ends before waiting for the model. Withdrawal
before reservation spends no cycle; a later denied launch retains its committed
capture/intent for safe reconciliation, rather than rolling counters back or
blindly resubmitting. Continuing recovery remains separate.
