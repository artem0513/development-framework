# Hard-crash local edits implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans or superpowers:subagent-driven-development to implement this plan task-by-task.

**Goal:** Complete the same admitted local corrective edit after a hard-killed coordinator without losing pending work or repeating known postimages.

**Architecture:** Existing SQLite runtime records store a compact receipt/authority-bound intent. A pure filesystem helper preflights and applies exact before/after states; WorkflowController owns admission, current authority, recovery and final independent gates.

**Tech Stack:** Python 3.11+ standard library, SQLite, existing Codex/Claude/Copilot adapters and immutable Runner artifacts.

**Spec:** docs/superpowers/specs/2026-11-14-hard-crash-edits.md

## Global constraints

- No MCP, new service or mandatory external dependency.
- Preserve original approval ancestry, explicit limits, independent judgments and UNKNOWN effect reconciliation.
- No current approval from historical evidence; no arbitrary temporary-file cleanup.
- Report injected faults, protocol fixtures, actual Codex sessions and process-kill coverage separately.

## Tasks

- [x] Retain real hard-kill protocol and actual Codex baseline failures before edits.
- [x] Implement pure metadata-plan and idempotent local-file application with focused filesystem tests.
- [x] Admit a durable exact-candidate intent before file mutation and resume it under original/current authority without a new reservation.
- [x] Add real killed-subprocess recovery regressions and authority/drift/unsafe-temp negative controls.
- [ ] Run current full suite and actual Codex crash matrix against frozen source.
- [ ] Obtain fresh technical implementation review plus independent actual-evidence audit; record architecture/technology advice from official primary sources.
- [ ] Complete native source acceptance/review/gate, authorized delivery and dated context/report after actual verification.
