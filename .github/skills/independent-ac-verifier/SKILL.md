---
name: independent-ac-verifier
description: "Use when a managed task needs a separate acceptance judgment against the original request, current contract and actual profile-specific evidence."
---

# Independent acceptance

When `lead_review` is present, judge the original lead's classification and critiques.
Check omitted requirements, unsupported claims, stale evidence and unresolved
material findings. A cosmetic P3 is advisory. Generic summaries cannot pass.
A justified mechanical exemption needs no lead passes. Keep criterion IDs and wording.

Apply relevant [evidence decisions](../task-orchestrator/references/evidence-decisions.md): actual target/reference authority, required dependency postconditions and current artifact purpose/version. For migration/optimization assess meaningful equivalence and relevant boundaries/preexisting ambiguity. Metadata or prepared drafts are not consumer/execution evidence. Missing relevant code is a coverage gap to investigate or disclose; it does not establish absent behavior.

Act in a separate session from implementation. Read the original request and sources, approved proposal/stages, acceptance obligations, current artifacts and actual executed evidence. Inspect the delivered result directly; an implementation summary is only a locator.

For each criterion report its ID, expected behavior, source/evidence references, verification method, PASS/FAIL/UNVERIFIED/BLOCKED/NOT_APPLICABLE, limitations and missing coverage. Mandatory unexecuted checks cannot PASS. Do not weaken acceptance to fit the implementation or infer a real resource/document/model run from a fixture.

When a defect outside the task (not in the ticket, not caused by the change) blocks the literal check of a denial or other negative criterion, judge the criterion's goal: a deliberate refusal by a working control (4xx authentication or authorization, no data returned; never a 5xx, timeout, crash or empty 200, for example 401 where 403 was written) together with executed evidence of the task's own changed code path (for example a test with a stubbed principal) is PASS with a limitation naming the defect; the refusal without that evidence is UNVERIFIED (missing coverage); goal not shown is BLOCKED. A positive criterion is never met by a refusal, and a literal form the requirement itself prescribes (a documented status-code contract) stays required. A fix of that foreign defect in the deliverable is not acceptance evidence; report it as out of scope ([out-of-scope discovery](../task-orchestrator/SKILL.md#out-of-scope-discovery)).

Judge actual test source and executed evidence for your criteria. Inspect any supplied current adequacy assessment. The gate separately checks pending sibling receipts. Report concrete test gaps. An omitted risk needs a category (a) [counterexample](../task-orchestrator/references/solution-design.md#2-triage-every-risk-only-required-risks-become-code). Counts and verdicts alone cannot prove behavior. AC cannot replace independent adequacy.

Use missing_coverage only for unmet proof required by this configured criterion at this gate. Any nonempty value blocks a mandatory criterion even if status says PASS. Put nonblocking coverage limits and mandatory later delivery inputs/effects in limitations and the pending delivery plan; they do not become prerequisites for current source approval. A real current evidence gap remains UNVERIFIED/BLOCKED. Passing a source criterion never establishes later publication or overall completion.

Apply task-orchestrator's [profile evidence](../task-orchestrator/references/profile-evidence.md). Software retains mandatory successful commands, positive actual test counts, no skipped required tests and current source/contract/plan fingerprints. Documents, research and infrastructure need substantive independent evaluation of their own deliverables. Suggest AC-derived checks independently of implementation assumptions.

Use `ase --project PROJECT verify TASK` through the authorized local backend adapter to capture independent receipts. A missing optional integration is not a prerequisite; unavailable independent-session capability is an explicit blocker. Do not manufacture receipts, copy credentials or relax permissions. Separate model judgment does not establish a separate OS security identity.
