---
name: quality-gate
description: "Use when current managed-task evidence must determine completion readiness or authorized publication eligibility."
---

# Evidence gate

For software bugfixes apply the [causal evidence procedure](../task-orchestrator/references/bugfix-evidence.md): confirm cause, justify the correction and independently judge real consumer regressions. Read its operating details only when applicable.

Configured nontrivial roots also require the original main bot's
[additional whole-task passes](../task-orchestrator/references/lead-critical-passes.md)
(one for LOW/MEDIUM risk, three for HIGH).
Original/approved requirements, accumulated findings and the current final
assessment must remain evidenced. Independent AC explicitly challenges the
classification and pass sufficiency. A justified mechanical exemption skips the
lead passes, not normal checks or independent judgments. An owner may waive a P2
or disprove a finding (`ase task dispose-finding`); the gate lists it as disposed. Pending lead work is
an internal continuation; do not ask the owner to repeat a testing request.

Apply relevant [evidence decisions](../task-orchestrator/references/evidence-decisions.md) in independent judgments: required readiness, current artifact purpose/state/version, bounded claims and meaningful semantic checks. Preparing a script or observing an object is not evidence of execution or required consumer behavior. The runtime gate validates configured evidence; these semantic rules still need actual independent assessment.

Run `ase --project PROJECT gate TASK` and `report TASK`. Only current source/contract/execution-plan evidence can authorize COMPLETED; session closure and process exit zero do not imply approval. Respect the configured acceptance profile and parent coverage for mixed work.

Software retains successful mandatory commands, positive executed test counts, no skipped required tests, independent AC/code review and no P0/P1/P2 findings. P3 is advisory. Other profiles still require executable facts or explicitly specified confirmations and separate substantive judgments, rather than software-shaped placeholders. Reject stale, missing, expired, tampered or forged evidence.

Every independent receipt and gate decision records the framework commit and the hash of the runtime policy it ran under. A required verdict of an open task stamped with another policy hash, or recorded before stamping, is a stale verdict: the gate lists it (old commit -> new commit) and blocks until that acceptance, review, test adequacy or review part is re-run under the current policy. A COMPLETED or CANCELLED task is never reopened or re-reviewed because of an update.

A defect outside the task that blocks verifying a criterion is never fixed to obtain evidence. Judge the criterion's goal only for a denial or other negative goal refused deliberately by a working control (4xx authentication or authorization, no data returned; never a 5xx, timeout, crash or empty 200) and only with executed evidence of the task's own changed code path (for example a test with a stubbed principal): then it is met with a note naming the defect (401 where 403 was written). Without that evidence it is missing coverage, not met. A positive criterion is never met by a refusal, a literal form the requirement itself prescribes (a documented status-code contract) stays required, and a goal not shown is a blocker for the owner ([out-of-scope discovery](../task-orchestrator/SKILL.md#out-of-scope-discovery)).

Configured software also requires current independent [test adequacy](references/test-adequacy.md): original risk scenarios, meaningful actual assertions and required consumer boundaries. Legacy unconfigured contracts remain readable and must not be described as having passed adequacy. Before completion the original lead processes current result/coverage events through its [durable inbox](../task-orchestrator/references/coordinator-events.md); handling an event is not semantic approval.

Inspect blocked reasons separately from the overall verdict: any unresolved blocker stops mutation even if the overall verdict is REJECTED. In-scope repairs require fresh affected checks and independent receipts within persisted limits. Missing capabilities are not PASS and cannot be repaired by editing status fields.

Publication by any route is bound to the exact head: `ase publish` or git push, and also creating or updating a PR/branch through Azure DevOps or another service CLI, API or connector. Before a git/gh/az push or PR, commit and run `ase task finish TASK --base <default branch> --body-file <file outside the repository>` (gate for this clean HEAD plus `ci-check`; READY to push, REVIEW means owner decisions to show first; read `status`, not only the exit code); for `ase publish` its dry run compares the current source fingerprint with the APPROVED gate. A new commit, including a later follow-up or review fix, needs its own current gate before the PR is updated. A review or gate for another SHA, or a solo review, does not cover it. Record the PR id and published SHA. Outside `ase publish` this is procedure, not mechanical enforcement; trusted CI and server-side branch rules enforce it.

Gate exit codes are 0 APPROVED, 2 REJECTED, 3 BLOCKED. Publication remains a dry run until explicitly authorized for the exact destination. Local receipts and hooks are a reliability layer; trusted CI and server-side rules are needed for enforcement against a hostile same-user process.
