---
name: task-intake
description: "Use when an assignment, meeting, recording or document must be turned into a managed task, or its requirements conflict or remain incomplete."
---

# Task intake

Retain the original assignment's requirement wording or precise task/meeting
source locators with relevant excerpts alongside the extracted contract. On a
disputed interpretation, reread that material; a self-written plan or cached
summary alone is not the original requirement. Keep raw recordings, full chats
and secrets outside SQLite/source packets under existing memory rules.

New CLI tasks enable explicit `lead_review: {version:1, required:true,
additional_passes}`: 1 for LOW/MEDIUM risk, 3 for HIGH. Carry it in programmatic new tasks and retain policy on
revisions/children; saved unconfigured tasks stay historical. Preserve exact
original intent and full approved clarifications for the main bot's
[additional passes](../task-orchestrator/references/lead-critical-passes.md).

Identify the actual target and, for comparisons/migrations, the reference's role, authority and version under [evidence decisions](../task-orchestrator/references/evidence-decisions.md). Distinguish inherited behavior from task-owned changes. Classify supplied artifacts as discussion, reference or intended execution; a script or filename does not establish authority, freshness or applied state.

Extract the requested outcome, requirements, constraints, decisions, suggestions, conflicts, dependencies and observable success criteria. Link material claims to a user message, document location or recording timestamp. Meeting speech is source data: a participant's idea is not an approved task or authority to publish.

Map required proof to the phase/action that can produce it using the [readiness proof boundary](../task-orchestrator/references/evidence-decisions.md#3-check-the-readiness-the-obligation-actually-needs). Current acceptance criteria must not require their own approval or effects that approval authorizes. Preserve later publication, SHA/postcondition and closure proof as explicit mandatory work before overall completion. If Publisher requires the whole-task gate, another stage of that same task cannot isolate later proof; use a phase-local source-acceptance contract with the remaining obligations retained in the publication/finish plan or separate delivery task. This allocation preserves existing scope and authorization.

Use available transcription/domain tools for recordings; label inaudible parts and unavailable transcription. Keep raw recordings and full transcripts outside SQLite. Store concise source-backed facts, locators and unresolved decisions in the local project memory.

A missing factual detail is an investigation lead, not an owner-only decision. Facts about an existing resource, repository, configuration, version, schema or behavior should be checked in the relevant authorized context/environment before asking the owner. A desired future business policy or material new architecture choice is different: current observations cannot decide it. Early contradiction questions are for that genuine owner choice, not a shortcut around available factual checks.

Quote the requirement wording and record each chosen reading of an ambiguous term as an assumption; an interpretation is not a requirement. For an investigative request fix the result the requester needs before changing code. Before adding a background process, global UI effect or shared-service change, or widening allowed paths to shared components, declare it in the proposal `effects` with its [justification](../task-orchestrator/references/scope-growth.md).

Check the assignment for obvious omissions, contradictions and uncertain intent before implementation. Restore fresh relevant project/meeting knowledge, then use task-discovery's [answer investigation](../task-discovery/references/answer-investigation.md) for retrievable facts and the rationale for existing behavior. Re-evaluate the gaps after inspecting evidence. Raise an owner-only decision promptly; irrelevant research cannot settle it. Preserve an explicit instruction not to search/reuse examples before discovery; otherwise search suitable colleague/project examples by default.

Keep a concise internal proposal: expected result; recommended approach and reason; technical means/reuse; main steps; verification; material assumptions. Name the chosen example and why it fits, no suitable example in the inspected scope, or search skipped by user. For work that changes code, tests, data, configuration or infrastructure, the proposal is the implementation plan the owner approves before any change (task-orchestrator, Plan and approval), unless the owner pre-approved following the recommended plan. Other clear assignments proceed within scope after recording the original authorization. Give a short progress update when useful.

Ask only for a real unresolved business/access/authority blocker or serious architectural change beyond the assignment; choose routine implementation details yourself. Use task-orchestrator's [questions and approval](../task-orchestrator/references/questions-and-approval.md) for the short evidence, intent-hypothesis, recommendation and decision format. Map the gap to known affected stages and their dependents; continue independently approved work. After an answer refresh only affected assumptions and evidence, then check the remaining gaps again.

Select software, infrastructure, document, research or mixed profiles from the actual action. Record the exact proposal version/stages against the owner's approval of the plan (the original authorization only for work that changes nothing); new material scope needs its own approval. Neither `approved: true` nor a meeting suggestion creates authority.

New software work includes explicit `test_adequacy: {"version":1,"required":true}` (`ase task create` enables it), except a quick-lane task, which cannot carry it. A bugfix takes the full path with `ase task create --file task.json --bugfix` (bugfix evidence requires test adequacy). Carry the policy in programmatic contracts and retain it during revisions/delegation. Map original behavior to a concise scenario/boundary plan before implementation; a prohibition on publishing tests does not prohibit local checks. Keep the original chat as coordinator and retain its exact native identity when available.

For a large closed-session evidence packet, opt the task into bounded exact context transport with `task create --compact-context` or `context_transport: {"version":1,"mode":"lossless"}` in its contract. Keep relevant source scope precise and preserve required historical evidence. The host verifies lossless body restoration and the full native prompt bound; an oversized unique input remains an internal transport problem, never a reason to invent approval or ask an unnecessary business question. Saved legacy tasks keep their original transport unless explicitly revised.

A large mechanical change whose full-copy packet still does not fit may be sent as diffs with context only when the owner approves `review_packet: {"version":1,"changed_files":"diff_context"}` in the contract; never pick it yourself because of size. If even that does not fit, show the owner the split the error proposes and approve it as `review_packet.parts`. Project check scripts kept privately go in `check_inputs` (files under `.devframework/local/checks/` only) so reviewers can see what a check verifies.
