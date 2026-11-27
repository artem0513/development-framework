# Runner Policy Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Deliver the six rules to normal isolated roles and validate safe continuation with phase-correct acceptance.

**Architecture:** Generated trusted package guidance, reserved handoff policy, fingerprint freshness and immutable prompt proof. Existing runtime approval, receipts, memory and publishing remain the integration points.

**Tech Stack:** Python 3.11+ standard library, existing CLI/adapters/SQLite, Markdown skills.

**Spec:** docs/superpowers/specs/2026-11-14-runner-policy-design.md

## Global constraints

- No MCP, new service dependency or database migration.
- Preserve sandbox/tool isolation, current checks, scoped authority, no-replay behavior and legacy provenance.
- Keep clear authorized work autonomous; preserve actual future delivery obligations.
- Only actual model sessions establish semantic judgments; protocol fixtures and mechanical artifact checks are identified separately.

## Review focus

- Forged/stale/wrong-role policy must fail before backend launch; client skills cannot become trusted policy.
- Actual prompt must contain the captured policy; argument and stdin adapters retain existing limits.
- Policy upgrades invalidate approval but preserve historical evidence without replay.
- Legitimate owner revisions must not be confused with tampered old stage hashes.
- Positive controls must proceed without a question; tests must also catch real semantic/callee drift.

## Tasks

- [x] Reproduce omitted policy in normal scoped requests and retain failing controls.
- [x] Add package generation/loading, policy role validation and prompt delivery checks; prove with focused tests.
- [x] Add phase-local rules and exact adopted mirrors; preserve later delivery/closure requirements.
- [ ] Repair the live-discovered legitimate revision/history blocker with original authorization provenance and negative tamper controls.
- [ ] Freeze the combined source and run current full suite plus production Codex cases.
- [ ] Independently inspect semantic results and current source; repair confirmed defects and reverify.
- [ ] Require the actual source gate, publish with the standard guarded CLI, confirm remote SHA and save dated closure/report.

## Execution record

The policy regression initially failed on missing role guidance and unvalidated delivered prompts. Current focused controls cover all three adapter protocols, actual installed package use, forged policies, stale checks and historical evidence. Pilot failures/partial results remain separate from final acceptance; current results and exact next action live in the canonical project task and local outputs/policy-transport-fix-20261113 record.

Independent reading of the first 25 live cases found23 PASS, one future-phase gap incorrectly placed in current AC coverage, and one ambiguous owner-update provenance. Those105 actual sessions are retained as diagnostic evidence. The trusted AC guidance/schema and actual owner-answer handoff are clarified; fresh targets and current-policy verification are required. Hard cycle limits are preserved, and old implementations are not relabeled current.
