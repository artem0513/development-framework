# Bugfix evidence implementation plan

> **For agentic workers:** Use superpowers:executing-plans inline; obtain fresh independent whole-change review and acceptance.

**Goal:** Prevent symptom-only fixes and self-fulfilling helper tests from claiming a verified causal repair.

**Architecture:** Extend the existing task policy and hashed adequacy plan with an explicit bugfix dossier. A small test-design module owns its validation and schema; existing Runner, gates and independent roles retain their responsibilities.

**Tech Stack:** Python 3.11+ standard library, existing SQLite records, Codex CLI.

**Spec:** docs/superpowers/specs/2026-11-19-bugfix-evidence.md

## Global constraints

- Legacy contracts remain unchanged; no database migration or memory deletion.
- Separate independent adequacy, AC and review; protocol fixtures never establish semantic approval.
- Preserve Codex, Claude and Copilot adapters without provider-setting changes.
- Preserve existing approval, authority, CAS, source freshness and three-pass policies.

## Review focus

- A workaround or an unknown cause must remain investigable without a false PASS.
- Helper-only green tests must not substitute for consumer regression evidence.
- Missing, duplicate, stale or cross-stage dossiers must fail closed.
- Old saved plans and non-bugfix tasks must continue to work unchanged.
- Unavailable reviewers and changed plan/source must not retain approval.

### Task 1: Executable bugfix evidence policy

Files: create `ase/testing/bugfix.py`, `tests/test_bugfix_evidence.py`; modify
`ase/testing/adequacy.py`, `ase/tasks.py`, `ase/__main__.py`, `ase/review/service.py`,
`ase/modules.py`.

Interfaces: `required(contract, stage_id=None)`, `validate_dossiers(payload,
contract=None, checks=None)`, `assessment_schema(base, contract, stage_id=None)`;
the existing TestAdequacy service owns persistence, receipt validation and gates.

- [x] Add failing contract/CLI, dossier validation, stage coverage, restart and assessment tests.
- [x] Run the tests and retain their failing result before implementation.
- [x] Implement explicit `task create --bugfix` policy, inheritance/preservation,
      bounded dossiers, conditional assessment schema and causal gate checks.
- [x] Re-run targeted tests, including legacy adequacy and module boundaries.

### Task 2: Active guidance and actual Codex scenarios

Files: modify canonical task-orchestrator, quality-gate, independent AC/reviewer
instructions and generated provider/runtime copies; add reusable scenario tests.

- [x] Document before-fix investigation, evidence-grounded alternatives and
      real-consumer tests; preserve useful diagnostics and honest limitations.
- [x] Regenerate/check all provider assets.
- [x] Execute Codex scenarios covering a symptom-only merger, a justified partial
      response merger, a caller ignoring a valid response, grounded DTO loss,
      and an unresolved cause; distinguish model behavior from actual tests.

### Task 3: Review, verification and delivery

- [x] Execute the complete unittest suite and actual independent Codex adequacy (ran on Claude; see note below),
      AC and substantive review on the current scoped sources and evidence.
- [x] Perform three additional main-chat critiques anchored to the original request.
- [x] Fix confirmed findings with regression tests; refresh affected independent checks.
- [x] Record dated results and limitations, commit and push to the current preview
      branch; do not merge its unfinished prior work into main implicitly.

Task 3 completed 2026-11-21 (c750162): the final whole-root independent adequacy, AC and review of task bugfix-evidence-20261119 ran on Claude, not Codex, because the complete packet exceeded the Codex prompt cap (see docs/context-transport.md). A later follow-up commit only adds a typed Windows command-line blocker for stdin delivery and these notes; it had its own independent review and full suite, and the whole-task acceptance applies to c750162.

Ruling: implement inline under the user's standing autonomous technical-choice
authorization; no additional design interview. Keep the paused large-context
transport work separate.
