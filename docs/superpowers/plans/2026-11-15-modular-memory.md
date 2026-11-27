# Modular runtime and reliable project memory implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans for root integration; bounded proposal workers and a fresh independent reviewer may assist. The user explicitly authorized autonomous execution after the map, then tests and review; no further design approval is required.

**Goal:** Make DevelopmentFramework's responsibilities individually reviewable/testable and repair the five confirmed memory/report defects without breaking existing APIs.

**Architecture:** Keep the existing Python/SQLite services and transaction boundary. Extract journal repository/report/context components and workflow check/packet services behind compatibility entry points; expose a testable module catalogue. Preserve immutable history and distinguish source freshness from claim agreement.

**Tech Stack:** Python 3.11+ standard library, SQLite, unittest, installed native Codex.

**Spec:** `docs/module-map.md`; confirmed regressions in `outputs/memory-review-20261115/report.json` and `probes.json`.

## Global constraints

- No MCP, extra runtime dependency, universal worker pool or schema-destructive migration.
- Existing commands/imports and local/project-local state remain compatible.
- Preserve current evidence, original authority, independent AC/review and safe UNKNOWN recovery.
- Source acceptance precedes authorized main publication and dated closure; those remain mandatory later actions.
- Work in the existing clean main checkout as authorized. Root alone integrates proposals; proposal workers never edit shared source.

## Review focus

- Corrections outside the requested period must amend current reports without rewriting raw history; historical as-of views retain their cutoff.
- Completed publication follow-up sessions must not duplicate task completion or resurrect stale next actions.
- A claim-ID selection must not hide a conflicting same-topic/scope peer; disjoint scopes must remain usable.
- Two task closures and a failed nested transaction must preserve unrelated context and atomicity.
- Refactoring must preserve mocks, runtime policy invalidation, source coverage and installed package contents.

### Task 1: Journal queries and effective report projections

**Files:** create `ase/memory/__init__.py`, `repository.py`, `reports.py`; modify `ase/journal.py`; add maintained journal regressions.

**Interfaces:** `JournalRepository.events()` returns immutable ordered event rows. `ReportService.worklog(...)` and `meeting_report(...)` retain existing keyword signatures; Journal delegates queries.

- [ ] Reproduce completion omission, terminal next actions and out-of-period corrections with actual disposable Stores; retain passing provenance/date controls.
- [ ] Extract repository and report queries; implement effective amendments, deduplicated completion, terminal-state handling and corrected blocker projection.
- [ ] Run all journal/timezone/continuity cases; verify raw events are unchanged by queries and as-of behavior is explicit.

### Task 2: Knowledge conflict policy

**Files:** modify `ase/knowledge.py`; add maintained knowledge conflict tests.

**Interfaces:** retain `Knowledge.retrieve(topic=None, scope=..., refresh=...)`; add explicit conflict/source-validity metadata without promoting historical or unrefreshed records.

- [ ] Prove opposite same-topic current claims can both appear usable, including selected-claim and disjoint-scope controls.
- [ ] Mark unresolved overlapping alternatives for review, preserve source validity separately and propagate unusable prerequisite state.
- [ ] Verify explicit supersession restores a single usable claim; run all knowledge tests.

### Task 3: Persistent per-task context

**Files:** create `ase/memory/context.py`; modify Journal closure/context, CLI context projection, relevant skills/templates if their contract changes.

**Interfaces:** `ContextService.snapshot()` preserves current `project_context` envelope and adds per-task/topic projections from immutable versions. `record_task(...)` joins the current transaction; closure replay is idempotent.

- [ ] Add two-task retention, legacy-version projection, repeated closure and failure rollback tests.
- [ ] Retain older task context, write a dated per-task knowledge summary with explicit historical authority and typed source refs; avoid inferring semantic business facts from prose.
- [ ] Run context, journal, knowledge and CLI regressions; back up production memory before any maintenance.

### Task 4: Workflow service extraction and module catalogue

**Files:** create `ase/testing/execution.py`, `ase/evidence/packets.py`, `ase/review/service.py`, `ase/execution/actions.py`, `ase/execution/ports.py`, package entrypoints and `ase/modules.py`; modify workflow, runtime policy and packaging; add architecture/catalogue tests.

**Interfaces:** `CheckService.check(task_id, stage_id=None)` and `PacketService.packet(task_id, stage_id=None)` consume explicit structural context ports; review and action services do likewise. WorkflowController retains existing delegators. Catalogue resolves actual test files for each named module and runs unittest without interpreting it as original acceptance.

- [ ] Add compatibility/architecture and catalogue selection controls, verify missing new boundaries fail before extraction.
- [ ] Move the existing check/packet logic without changing authority/coverage semantics. Include extracted engine files in runtime policy identity and package discovery.
- [ ] Verify focused integration suites and catalogue coverage of every runtime source/test file; detect unowned new files.

### Task 5: Current-source verification, review and delivery

**Files:** add concise architecture/testing documentation and dated evidence summary; use ignored outputs for raw logs and native sessions.

- [ ] Freeze current source and run module suites, asset checks, whitespace and full `python -B -m unittest discover -s tests -v`.
- [ ] Run real Codex implementation reconciliation plus separate acceptance/review on frozen source and a meaningful disposable memory/report scenario. Do not reuse fixtures as semantic proof.
- [ ] Fix substantiated blocking findings with regression tests, rerun affected checks and refresh stale independent proof.
- [ ] Commit exact reviewed paths, nonforce push to the already authorized origin/main, verify remote SHA and clean source.
- [ ] Save dated session closure and concise outcome/blockers/next action; never label a paused/unverified task completed.
