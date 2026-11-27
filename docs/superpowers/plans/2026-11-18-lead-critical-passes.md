# Original-lead Critical Passes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans.

**Goal:** Require three additional, requirement-anchored whole-task critiques by
the original lead for nontrivial work without replacing independent verification.

**Architecture:** Add one cohesive `ase.lead_review.LeadReview` service over the
existing runtime-record unit of work and private captures. Integrate contract/CLI,
whole-root gate, packet/current verification context and native bot guidance.

**Tech Stack:** Python 3.11+ standard library, SQLite, native Codex/Claude/Copilot.

**Spec:** `docs/superpowers/specs/2026-11-18-lead-critical-passes.md`.

## Global Constraints

- No MCP, daemon, model SDK or external runtime dependency.
- Original lead owns critique; other reviewers do not replace its passes.
- Legacy saved contracts, independent AC/review/adequacy and recovery limits stay.
- User authorized in-scope fixes and main publication; no repeat approval request.
- Reuse clean current main checkout under the app's workspace guidance. No writes
  to other installations or unrelated user data for this source task.

## Review Focus

- A newly approved clarification must invalidate old-series completion while
  retaining the original request and supersession provenance.
- A fix after pass two must preserve previous work and require a final current
  critique; a fix after pass three must require a corrective follow-up.
- A crash after receiving a native result must recover the same ticket/capture
  without another native turn or discarding its response.
- A child or intermediate stage must not deadlock awaiting final-root critique.
- Trivial classification and vague PASS text must not bypass business obligations,
  current executed evidence or independent review.

### 1. Persisted lead-review service and adversarial tests

Files: create `ase/lead_review.py`, `tests/test_lead_review.py`,
`tests/test_lead_review_crash.py`; use existing state/coordinator/receipt APIs.

- [x] Write and run RED tests for exact original ownership, three ordered passes,
  requirement anchor/currentness, findings, structural trivial exemptions,
  idempotent captures, wrong projects/turn reuse and post-capture crash recovery.
- [x] Implement `LeadReview.requirements`, `classify`, `prepare`, `record`,
  `recover`, `snapshot`, `binding`, `gate`; keep schema/policy bounds explicit.
- [x] GREEN targeted tests and independent design challenge of the boundary.

### 2. Contract, CLI and whole-task integration

Files: `ase/tasks.py`, `ase/__main__.py`, `ase/gates.py`, `ase/workflow.py`,
`ase/evidence/packets.py`, `ase/modules.py`, integration/CLI/gate tests.

- [x] RED tests for new CLI policy, legacy compatibility, child/stage boundaries,
  missing classification/rounds, current verification context and internal handoff.
- [x] Validate explicit policy and retain it on revisions/required children.
- [x] Add `ase lead-review classify|prepare|record|status|recover`; expose exact
  native destination and actionable next prompt without spawning another lead.
- [x] Gate whole-root completion, include bounded assessment/anchor in packets,
  and return awaiting-lead-review without redundant test/model execution.
- [x] GREEN affected integration suites.

### 3. Skills, generated providers and real Codex acceptance

Files: canonical orchestrator/quality/intake/discovery/independent-role guidance,
new lead-review reference/template, generated provider assets/runtime policy,
README/workflow/module docs, actual Codex validation artifacts in ignored outputs.

- [x] State each main-bot pass's task, requirement/evidence rubric, applicability,
  fix/retest behavior and stopping rules. Regenerate all three providers.
- [x] Exercise actual same-main-session Codex follow-up prompts and capture/reopen
  recovery; protocol fixtures do not count as semantic/model evidence.
- [ ] Run affected suites and one full verbose unittest suite on frozen source.
- [ ] Perform this task's own three original-lead passes, current independent
  adequacy/AC/review, fix confirmed findings and rerun affected evidence.
- [ ] Guarded normal push to main; verify exact remote SHA and all four CI jobs.
- [ ] Process original inbox and save dated local context/session closure/report.

## Execution Ledger

- Independent advisory design challenge accepted before source implementation:
  seven lifecycle/provenance refinements, sealed report SHA256
  `9d298f8805e7fb2ac1a948e6fe79dc69aa6e71742b8cd3d604b0f80d90a74357`.
- Ruling: immutable approved-intent snapshots retain full contracts/proposals;
  hashes alone cannot reconstruct superseded approved clarifications.
- Ruling: use a separate `lead_review_sha256` binding on whole-root independent
  receipts. Keep stage/action context stable so valid intermediate/whole-scoped
  business proof can be reused while final root disposition is refreshed.
- RED17 confirmed absent service, GREEN17; additional adversarial REDs exposed
  capture windows, reused turns, provenance and closed-schema weaknesses. All
  36 service/crash/integration checks now pass, including actual process death.
- Canonical/three-provider guidance generated and adopted copies refreshed.
  Added a RED/GREEN resolution guard: a new prose claim on identical evidence
  cannot close a confirmed finding as fixed. Current targeted set: 37 PASS.

- Initial baseline: main `c646c75`; clean source checkout. Incoming release was
  already tested in four matrix jobs; new feature still needs fresh checks.
- Ruling: native host owns analysis, ase owns durable proof/currentness. Native
  identity remains a cooperative attestation; no hostile same-user guarantee.
- Ruling: use a single final whole-root requirement rather than three passes per
  stage/child; this preserves dependent-stage progress and original lead ownership.

### Source-freeze checkpoint

- 43 focused service, crash and integration checks pass after independent reviews.
- Confirmed review corrections cover unchanged recheck false closure, structured
  credential fields, remote-only evidence, and the real pre-edit implementation
  versus post-edit stage/check lifecycle. The last correction accepts only the
  exact candidate linked to current authenticated independent stage proof.
- A real Codex ledger chain found three seeded defects, generated tests and fixes,
  observed tests-first failures and passing regressions, and recovered process
  exit86 after capture without native replay. Phase-local gap guidance avoids
  circular waits for the response itself or later independent roles. Additional
  corrective passes retain earlier depth rather than restarting the chain.
- A separate real native ordinary run changed the actual document, completed
  independent stage AC/review, and prepared the first original-lead ticket from
  its original pre-edit receipt without implementation replay.
- Full frozen-suite proof, final original-lead/source roles and guarded release
  remain explicit later checkpoints; their actual results belong in the dated
  local work journal rather than being inferred from this planning snapshot.

- Final initial-source reconciliation found a further P2: a current mechanical
  exemption returned READY before checking a newly credited material critique.
  The original main recorded the reproduced finding in pass2 before repair.
  Credited critiques now supersede the exemption; its original classification
  remains explicitly historical. A new regression failed before this correction
  and all44 focused checks passed after it. The earlier1457-test full result is
  historical; a new complete frozen suite and current native proof are required.

### Owner stop-window checkpoint

- A further independently reproduced same-capture finding/`not_a_bug` closure
  loophole is fixed: resolutions now require a finding open before that capture.
  Current45 feature tests plus related suites/module checks total169 PASS.
- Fresh Codex whole acceptance correctly held missing historical source/check
  inputs for earlier critiques. An authenticated archive-enrichment experiment
  reached APPROVED, validating the data-flow diagnosis. Automatic generic
  per-pass evidence snapshot/hydration is still required; the host experiment
  is explicitly not a production implementation of that missing behavior.
- Final bounded native follow-up timed out and produced no judgment. No
  source-wide final approval, current complete full suite, commit or push is
  claimed after these last corrections. Preserve all captures and continue
  from the dated private checkpoint after the owner-requested stop window.

### Historical-input correction

- Resumed the existing task and approval; expired stop windows are not reapplied.
  Added a separate lead-input helper after an independently reviewed design.
- Prepared basis records exact source hashes and full executed checks. Referenced
  original source/log bodies are sealed after the response and before SQL credit.
  Input manifests join final bindings and are revalidated at every read.
- Root packets hydrate deduplicated historical proof automatically. Legacy input
  recovery requires an exactly bound authenticated Runner request; absent bytes
  remain a visible gap. Intermediate stages do not inherit the root barrier.
- Initial10 regression cases failed before integration and passed after it.
  Additional real process-death, prefix, hardlink, legacy-tamper and stage-isolation
  cases cover both sides of the response/input/SQL boundary. Current full-suite,
  native-chain and independent acceptance results remain separate release checks.
- Technical advisory reproduced one P2: quoted nested JSON credentials bypassed
  the inline archive scanner. Added three regressions before repair (all failed),
  then structural validation of every JSON object pair, including duplicates,
  before prepared-basis/blob writes and on snapshot/legacy reads. Credential
  references and legitimate empty/plain inputs remain supported. Final current
  feature checks, full suite and fresh independent review are still required.
- Follow-up advisory identified premature rejection of a JSON-shaped prefix in
  valid plain code. Structural checking now waits for complete JSON syntax,
  retains duplicate pairs until inspection, and bounds nesting explicitly.
  Full-path plain-source and excessive-nesting regressions failed before repair.
- Generic source bodies now distinguish credential text from JSON Schema
  declarations and boolean/numeric metadata. Descendants and text arrays remain
  checked, including duplicate keys; the stricter approval-intent rule is not
  changed. Two full-path source/check regressions failed before this correction.
- Final native adequacy/acceptance held two missing boundaries: competing
  connections and execution-plan/action-proof drift. A new deterministic
  two-connection probe then exposed a P2: both record calls could append the
  same empty staging prefix, leaving two concatenated responses and no credit.
  The original main recorded the finding before repair. Record and recovery
  now serialize their filesystem staging through the existing SQLite writer
  transaction; response fsync still precedes committed credit. No state format
  or migration changes are introduced. Competing prepare, identical/conflicting
  records and real plan/observed-action currentness tests cover the boundaries.
- Recheck reproduced the same race under caller-owned deferred transactions:
  a nested SAVEPOINT alone does not reserve a writer. Capture/recovery now
  acquire the existing SQLite writer mode with a zero-row update before any
  staging access, retaining savepoint/caller rollback semantics. Deferred
  identical/conflicting delivery regressions failed before this correction;
  caller rollback must preserve the fsynced reply for later single recovery.
