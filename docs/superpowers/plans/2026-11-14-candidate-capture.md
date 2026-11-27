# Candidate capture recovery implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans for root integration. Disjoint tests and read-only investigations may be delegated under project rules; final review must be independent.

**Goal:** Reuse one already completed model capture after a coordinator dies before local-edit admission, with no new candidate/model call and no downtime charge.

**Architecture:** A small candidate_capture module owns exact transport identity and verified artifact lookup. Runner accepts a host-reserved ID; WorkflowController persists that reference before transport and joins it to existing original-authority local-edit admission on resume. Existing operation reconciliation, checks and gates remain authoritative.

**Tech Stack:** Python 3.11+ standard library, SQLite, existing Codex/Claude/Copilot adapters.

**Spec:** docs/superpowers/specs/2026-11-14-candidate-capture.md

## Global constraints

- No MCP/service/new dependencies, automatic engine migration or business/cloud effects.
- No latest-receipt scan, stale approval rebinding, fabricated semantic approval or budget reset.
- Full valid Runner capture is the boundary; retain partial artifacts without promoting them.
- Separate implementation, independent AC and review; publish only after a current gate.

## Review focus

- Wrong task/stage/candidate/request/policy/receipt references must not execute saved edits.
- A same-proposal replacement approval must not refresh original candidate authority.
- An active child call may use a root key, but recovery must resolve the child's own capture.
- Delay before resume must not consume a completed call's active-time allowance twice.
- Missing/failed captures and legacy/no-reference rows remain conservative across repeated resumes.

## Tasks

- [x] Confirm actual killed-subprocess failures on pinned44344e3 and preserve baseline evidence.
- [x] Add host-reserved capture identity and exact validated lookup with focused controls.
- [x] Integrate pre-admission recovery and completed-call accounting; pass actual killed-subprocess regressions.
- [ ] Freeze and execute full suite plus actual Codex pre-admission kill matrix.
- [ ] Complete independent implementation/evidence review and native current checks/AC/review/gate.
- [ ] Publish verified source, compare remote SHA, and save dated project context/report.
