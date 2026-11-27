# Three additional critical passes by the original lead

User-approved outcome: after initial implementation/checking, the original main
bot re-examines the complete solution three more times for nontrivial tasks,
always against the original requirements and approved clarifications. Codex live
validation, independent review, and publication to main are authorized.

## Approach and boundary

Use existing native bots plus a small SQLite-backed `LeadReview` service. The
bot does the thinking and in-scope fixes; ase stores immutable requirements,
pass tickets, captured assessments and current completion policy. Do not add
MCP, a daemon, a model SDK or another orchestration framework. A prompt-only
instruction would not detect missing passes; three new independent reviewers
would not meet the requirement that the original lead reconsider the result.

The service is a cooperative local reliability mechanism. Native identities
and turn references are host observations/attestations, not OS isolation or
proof that an arbitrary model thought deeply. Independent AC, substantive
review and software test adequacy remain separate and mandatory.

## Required behavior

- New CLI tasks enable explicit version-1 `lead_review`, required true, three
  additional passes. Saved tasks without this field remain unconfigured; never
  rewrite historical contracts to manufacture completed reviews.
- All nontrivial domains use the workflow. Default unknown complexity to
  nontrivial. A recorded trivial exemption requires a concrete mechanical-work
  reason, current evidence and source references. It is structurally restricted
  to LOW risk, one stage, no actions/access, unresolved unknowns, required child
  tasks or mixed-domain work. Independent AC challenges semantic misclassification.
- The original root chat owns these passes. Children do not create three new
  leads or block their parent's integration awaiting a circular root review.
- Each pass rechecks every current mandatory criterion, the original request,
  and the latest approved request. Supply the original saved contract and
  approved version/clarification history. Expected results come from this
  requirement anchor, never from current implementation or generated tests.
  Superseded obligations remain visible and are labelled by approved versions;
  an unapproved revision cannot change the anchor.
- Passes are sequential: (1) requirements/business completeness and conflicts,
  (2) failure/edge/performance/security risks where applicable, (3) whole-solution
  requirement reconciliation and adequacy of actual evidence after fixes. All
  three consider the complete task. Later passes are corrective follow-ups.
- Findings require a requirement, concrete scenario/violation, source and check
  evidence. Performance claims require measurements; absence of evidence is a
  gap, not a fabricated bug. No findings is a legitimate outcome. The bot fixes
  confirmed in-scope problems, reruns affected checks and applicable regressions,
  and reassesses the full requirements. No new owner interview for routine fixes.
- Three is the initial required depth, not a completion guarantee or a repair
  retry ceiling. Open P0/P1/P2 findings and required gaps hold completion. Later
  passes explicitly resolve earlier findings using current evidence; deleting
  a finding from a report cannot close it. Existing progress-sensitive stopping,
  unknown-effect reconciliation and explicit limits continue to apply.

## Persistence and currentness

- Persist a CAS pass intent before a native critique. Bind task/project/root,
  original coordinator, approved requirement anchor, previous pass, source,
  execution plan, actual checks and external action evidence. No simultaneous or
  duplicate pass; no repeated native turn counts as another pass.
- Capture structured results to an immutable private artifact before committing
  the SQL projection. Recovery consumes the existing artifact/attempt; it never
  starts a model or resends a prompt. Retain stale captures as history.
- A result captured against changed input is historical and cannot increment
  current required depth. A later legitimate fix does not erase earlier completed
  passes: their work/history remains, while the final assessment must match the
  current source/checks/context and resolve accumulated material findings.
  A new approved requirement/proposal anchor starts a fresh series.
- Enforce these passes at the whole-root completion gate, not before every
  intermediate stage. `run/resume` returns an internal awaiting-lead-review
  action instead of repeating successful independent sessions or asking the user.
- Independent verification receives the lead assessments and classification.
  Bind its current context to that evidence, so previous acceptance cannot
  silently approve newly recorded main-bot judgments or exemptions.

## Acceptance and examples

Test missing/duplicate/out-of-order passes, wrong lead/project/root, original and
approved requirements, unapproved changes, trivial justification and unsafe
exemptions, child/stage integration, source/check/action drift, accumulated
findings, fixes between passes, final currentness, crash before SQL projection,
reopen/recover without a second model call, and legacy contracts.

Use actual Codex native main-session turns for the three prompts, including a
counterexample missed initially, correction/current rechecking, and restoration
from a saved result. Separate actual independent AC/review/adequacy from protocol
fixtures. Measure the behavior against task requirements, not three PASS words.

Preserve all three generated providers, local installation/memory and current
checks. Source approval precedes publication; exact pushed SHA and actual remote
matrix CI are later delivery obligations, not circular source criteria.
