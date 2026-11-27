# Progress-based workflow Implementation Plan

> **For agentic workers:** Execute the approved tasks continuously with TDD, disjoint ownership, integration checks and independent final review. The owner already requested implementation and testing; no renewed design approval is needed.

**Goal:** Implement the agreed autonomous flow and repair the reproduced recovery/reconciliation weaknesses.

**Architecture:** Keep the existing local CLI/SQLite/adapter architecture. Separate progress-based recovery, authoritative operation reconciliation and stage-scoped discovery; preserve legacy contracts and the evidence gate.

**Tech Stack:** Python3.11+ standard library, portable Markdown skills, existing Codex/Claude/Copilot adapters.

**Spec:** `docs/superpowers/specs/2026-11-13-progress-flow.md`.

## Global Constraints

- No MCP/Mastra/runtime dependency, provider credential/config change or live cloud mutation.
- Existing clear task assignment is authority within its actual scope; source metadata is not authority.
- Keep ignored private state, copied skills, project boundaries and actual independent verification intact.
- Existing saved recovery/hard-budget policies remain usable; no reset via new labels or sessions.
- Jira/wiki-specific source amendments stay withdrawn.

## Review Focus

- A delayed external read cannot turn an attempted mutation into permission for a duplicate.
- Alternating/reworded failures and stage renames cannot manufacture new progress or allowances.
- A stage-specific unknown cannot stop or authorize unrelated stages incorrectly.
- New default progress recovery cannot weaken explicit old hard limits or falsify verified progress.
- An instruction update cannot silently authorize business/architecture/external effects beyond the assignment.

## Task 1 — Authoritative operation reconciliation

**Files:** `ase/operations.py`, new operation regression tests. Return workflow integration requirements to the coordinator; do not concurrently edit `ase/workflow.py`.

- [ ] RED: delayed visibility after start remains UNKNOWN; started refuses unresolved replay; authoritative terminal absence can allow retry; applied resources reconcile without duplication.
- [ ] GREEN: implement typed observation/transition rules, preserving initial absence and known application compatibility.
- [ ] Verify existing operation tests and new regression tests; report exact output and integration API.

## Task 2 — Progress and recovery history

**Files:** `ase/workflow.py`, optional focused `ase/recovery.py`, new recovery tests. Coordinate contract validation/default-policy fields with Task3 before relying on them.

- [ ] RED: same criterion with changed accompanying prose remains a repeat; rename preserves underlying allowance; monotonic verified progress continues beyond three total cycles; oscillation/source-only edits do not reset stagnation.
- [ ] GREEN: durable recovery records and new-policy recovery loop, with old saved policies/hard caps honored. Integrate Task1's UNKNOWN guard.
- [ ] Verify recovery/workflow regressions, diagnostic continuation/stop and resume/delegation boundaries.

## Task 3 — Scoped readiness, policy contract and skills

**Files:** `ase/tasks.py`, `ase/discovery.py`, `ase/gates.py`, intake/task tests, canonical skills/references/templates/docs and adopted copies.

- [ ] RED: explicit stage-scoped gap holds affected/dependent stages while independent approval survives; unknown/empty mappings remain conservative; invalid stage IDs fail.
- [ ] GREEN: scoped readiness in task/gate with no blanket authorization inheritance for changed business contracts.
- [ ] Integrate versioned progress-policy validation/defaults without rewriting old policies.
- [ ] Update skills for clear-task execution, repeated understanding checks, concise owner questions, architectural escalation, progress-driven research and colleague examples.
- [ ] Sync owned local copies with backups; preserve unrelated settings and all three installer targets.

## Task 4 — Integration, acceptance and delivery

- [ ] Run affected tests and full unittest discovery after runtime changes on available supported interpreters.
- [ ] Exercise actual Codex decision/execution, acceptance and review in separate sessions; protocol fixtures remain protocol evidence only.
- [ ] Independent whole-change review, fix actionable in-scope findings, rerun affected checks/judgments.
- [ ] Save dated memory/checkpoint/worklog, report implementation/tests/limits accurately, and use the previously authorized same-repository feature-branch publication scope.
