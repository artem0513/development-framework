# Coordinator Events Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans for root integration; bounded independent investigations and final review may use native agents. Keep shared mutations serialized.

**Goal:** Return durable worker/verification results to the original coordinator and require meaningful independent test adequacy for opted-in software tasks.

**Architecture:** Existing SQLite records/CAS own the inbox and dispatch lifecycle; native bots own transport. Separate test-design and independent assessment records feed current evidence gates.

**Tech Stack:** Python 3.11+ standard library, SQLite, existing CLI, native provider tools.

**Spec:** docs/superpowers/specs/2026-11-16-coordinator-events-design.md

## Global Constraints

- Preserve project-local isolation, original authorization, root ownership, recovery and current receipts.
- No mandatory MCP, broker, background timer or new model SDK.
- Native messages are evidence and cannot impersonate an owner approval.
- Live Codex validation; no live Claude/Copilot account requirement.

## Review Focus

- Crash after delivery but before acknowledgement: preserve UNKNOWN and forbid blind resend.
- Duplicate sealed results and concurrent claims: one logical event, no duplicate repair launch.
- Stale source/checks or changed coordinator: never route to a guessed thread or accept old adequacy.
- Happy-path unit tests with mocked SQL/HTTP: report missing actual required boundary proof.
- A sleeping/unavailable host: preserve pending work and accurately disclose transport limits.

### Task 1: Durable coordinator inbox and routing protocol

Files: create ase/coordinator.py and tests/test_coordinator_events.py; integrate ase/delegations.py, ase/review/service.py, ase/workflow.py, ase/__main__.py.

Interfaces: CoordinatorEvents binds an original endpoint, enqueues idempotent source events, lists inbox, reserves delivery, records delivery/UNKNOWN and acknowledges processing. Existing result producers call it transactionally.

- [x] Write failing behavior tests for persistence, exact destination, source dedup/conflicts, CAS claims, delivery/processing distinction, UNKNOWN reconciliation and producer atomicity.
- [x] Run RED; implement the minimal protocol and CLI; run focused GREEN.
- [x] Validate active native Codex results and actual current-chat transport where available; preserve evidence and limitations.

### Task 2: Independent test adequacy

Files: create ase/testing/adequacy.py and tests/test_test_adequacy.py; integrate task policy, evidence packets, independent verifier and gate.

Interfaces: scenario plan plus source/check-bound independent assessment. The existing gate consults configured adequacy and blocks missing/stale/insufficient proof.

- [x] Independently derive risk scenarios from the task and inspect existing proof boundaries.
- [x] Write failing tests for missing plan, omitted required risk, false PASS, stale bindings and legacy compatibility.
- [x] Implement and execute focused GREEN, plus genuine Codex adequate/inadequate scenarios.

### Task 3: Skills, delivery and review

Files: canonical skills/references, generated provider assets, docs/module-map.md, ase/modules.py, README.md.

- [x] Connect main-chat inbox consumption, test-adequacy planning/assessment and repair loops in all three providers.
- [ ] Regenerate assets; run full unittest suite, asset consistency and whitespace checks.
- [ ] Obtain fresh independent AC and technical review on the combined source; reproduce and repair confirmed defects, rerun affected checks.
- [ ] Publish exact verified changes to origin/main, inspect remote CI, save dated context/worklog and report actual results.
