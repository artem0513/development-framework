# Hybrid delegation implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans for root integration. Delegate disjoint tests/read-only investigation under project instructions; final review remains independent of implementation authorship.

**Goal:** Preserve native delegated work and dependencies across coordinator failures without moving native spawning into ase.

**Architecture:** A bounded SQLite registry owns operational identity and CAS transitions; a small artifact helper seals exact attempt results. Existing approval/source/operation/gate services remain authoritative. Native bots own worker lifecycle and supply observed handles/results through the CLI.

**Tech Stack:** Python3.11+stdlib, SQLite, current three bot skill mirrors and native capabilities.

**Spec:** docs/superpowers/specs/2026-11-14-hybrid-delegation.md

## Global constraints

- No MCP, mandatory new dependency/service, universal pool or bypass of current permissions.
- Unknown launch cannot be retried on timeout/absence; result acceptance is not independent AC.
- Source fingerprints exclude operational registry progress; publication rechecks registry closure under its existing launch fence.
- Shared root mutations remain controlled; native workers return analysis/proposals and private output artifacts.

## Review focus

- Same-root task/stage/native attempt aliases must not reset original authority or create duplicate launches.
- Lost spawn acknowledgement and lost result DB commit must have distinct safe recovery routes.
- Stale approval/source/dependency/output artifacts must block dispatch/acceptance.
- Native result metadata must remain recordable while the parent waits, without an execution-lock deadlock.
- Late registry mutation before publication must hold the original launch authority.

## Tasks

- [x] Capture clean c4b3d14 baseline and inspect existing CAS/intent/approval/gate patterns; independent design risk review.
- [x] Write failing registry/CLI/crash scenarios before implementation; retain the old CLI baseline and red technical controls. Live native guidance exercise remains in the live-chain task.
- [x] Implement registry/artifact lifecycle and integrate CLI/restore/report/gate/publication checks.
- [x] Update canonical bot guidance/generated assets and verify all three installation layouts.
- [ ] Freeze source; run full regression and live Codex dependency/crash/recovery chain.
- [ ] Fresh independent technical/evidence review and current native AC/review/gate.
- [ ] Authorized push, remote SHA/postcondition verification, dated context/closure/report.
