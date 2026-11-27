---
name: project-context-manager
description: "Use when local project memory, dated decisions, source freshness, task continuation, checkpoints or session closure need retrieval or persistence."
---

# Local project continuity

Retain relevant [evidence decisions](../task-orchestrator/references/evidence-decisions.md): target/reference role and authority, scoped checks, required readiness, artifact purpose/state/version and semantic assumptions. New evidence refreshes affected artifacts, checks and cached conclusions; retain superseded versions and already-applied effects. Use existing supported fields/source references rather than custom JSON keys or another tracker.

Resolve the explicit project/workspace identity with `ase`; use the same local SQLite state across Codex, Claude and Copilot within that project. Selection is explicit `--home`, selected project-local configuration, `ASE_HOME`, then platform default. In project-local mode use `.devframework/bin/ase.ps1` or `.devframework/bin/ase`; memory/journal/receipts live in ignored `.devframework/local` and must not become application source or Git input. Another computer or cloud agent needs an explicit reviewed export; there is no automatic synchronization. Project paths and source locators grant no authority.

Before implementation or rescanning, retrieve fresh relevant project/meeting context and knowledge topics. Preserve provenance, version/freshness observations, supersession and uncertainty. Check obvious assignment gaps, then re-evaluate them after discovery or an owner answer. Refresh only affected assumptions, changed dependencies and their dependents rather than every project record. Git commit alone does not describe dirty files or a non-Git workspace.

On start/resume/switch, use `ase --project PROJECT restore TASK`, reconcile current observations and preserve newer/staged user work. Reuse the task identity, original-assignment authorization, recovery/problem/probe history and explicit root limits across aliases and delegates. An approved implementation plan (or, for work that changes nothing, the clear assignment) needs no renewed approval. Use revision-checked checkpoints with one exact next action; on stale CAS reload, merge facts and retry. A handoff export is a view, not a second mutable truth.

At stage boundaries, stop and session end, read [task continuity](references/task-continuity.md) and task-orchestrator's [finish and reports](../task-orchestrator/references/finish-and-reports.md). Save a dated closure for completed, awaiting-approval, blocked, paused, cancelled and interrupted sessions. Closing a session does not mark the task completed.

Store concise evidence references, decisions and blockers; exclude credentials, raw conversations, full transcripts and recordings. For answer investigations, retain inspected scope/freshness, observations, intent hypothesis separately from recommendation, remaining unknowns, next useful lead or stopping reason, actual effort and any explicit limits; exclude raw logs and database rows. No default research pass/time/cost cap is implied. Preserve verified progress and unsuccessful cycles for each original problem so rewording cannot reset history. A past exhausted search is not permanent absence evidence. For durable claims or recurring corrections read [durable knowledge](references/durable-knowledge.md). Use `ase backup` before maintenance. Missing facts remain unknown.

Credential values live only in the separate encrypted local vault. Persist references, exact target, captured revision and safe auth status, never import-file contents, headers, tokens or session responses. Project export does not carry the vault. Rotation/configuration changes require fresh affected checks and observations; keep historical facts labeled historical and never replay completed mutations to refresh credentials.
