---
name: communication-assistant
description: "Use when a managed task needs a concise proposal, final status, meeting summary, PR explanation or stakeholder draft based on current evidence."
---

# Concise task communication

Use relevant [evidence decisions](../task-orchestrator/references/evidence-decisions.md) for the current target/reference, artifact purpose/version and scoped blocker evidence. State the current object set and unresolved placeholders when material; mark older drafts/claims historical after new answers. Keep the first message concise; the internal evidence brief is not a questionnaire for the owner.

For a clear assignment that changes code, tests, data, configuration or infrastructure, draft the short implementation plan for the owner (task-orchestrator, Plan and approval); for other work record the original authorization and execute within scope without another approval interview. A short progress update can state the intended action and verification. Keep outcome, approach/reason, technical means/reuse, main steps and material assumptions in the separately inspectable contract.

A PR description is short and plain: what the ticket asked, what changed, what was deliberately not handled, how it was verified; no showcase of mechanisms.

For status read `ase --project PROJECT report TASK`, the current checkpoint and source coverage. Distinguish implemented, executed, independently verified, unavailable, blocked and actually published results. Cite existing artifacts and actual URLs. Empty source inventory is unconfigured, not completed analysis.

Before drafting an owner question, use task-discovery's answer investigation for retrievable facts. Never claim a probe ran when it was only planned, or infer global absence from one denied/unread source. Owner-only decisions need no research theater.

For dated activity use `ase worklog` and `ase meeting-report` with explicit date bounds. New projects use the executing computer's local time by default; do not ask the owner to choose a region for routine reports. Respect an explicit existing project choice or the owner's requested `--timezone local` override. Draft results, decisions, blockers and next actions; label current versus historical/as-of blockers. Read [finish and reports](../task-orchestrator/references/finish-and-reports.md) before session closure or requested stop. Summaries do not alter task approval or evidence.

For a real unresolved business/access/authority blocker or serious architectural change, follow [questions and approval](../task-orchestrator/references/questions-and-approval.md) for the message format and team routing. Choose routine technical details autonomously. After an answer refresh affected assumptions and keep independently approved work moving.

Drafting is authorized by a drafting request or as internal preparation within the approved task. Other sending to email/chat or publishing needs explicit authorization for the destination. Never infer billable hours from agent activity.
