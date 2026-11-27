# Project-local mode Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans for inline work; independent bounded tasks use the parallel-agent workflow. Track checked steps and preserve existing user approval.

**Goal:** Install/run the framework with independent configuration, memory and dated journal in each project's folder for all three bots, validate on Codex, then push all approved pending changes.

**Architecture:** Keep the existing runtime, registry, SQLite and installer. Add one strict project-local configuration/path boundary module, select it automatically in the existing state routing, and allow only its protected private state inside a source root. Extend the reviewed manifest installer and relative launchers instead of adding a service.

**Tech Stack:** Python3.11+ standard library, existing native CLIs and Git; actual available validation interpreters3.12/3.15.

**Spec:** `docs/superpowers/specs/2026-11-12-project-local-design.md`.

## Global Constraints

- No framework MCP, global activation, provider authentication migration, permission relaxation or runtime third-party dependency.
- Private persistent state is `.devframework/local`; config is `.devframework/config.toml`; installer-generated backups stay private.
- Preserve shared mode and pending project-example changes. Use current feature branch; no new worktree or destructive cleanup.
- Ask only for genuine unavailable authority/information; publication to the existing exact GitHub destination is authorized.

## Review Focus

- A global ASE_HOME or unrelated cwd must not mix two local projects.
- Broken ignore rules, tracked private state and redirected paths must stop writes before SQLite/vault creation.
- Private state must not self-invalidate source-bound evidence or leak through generic discovery.
- Relative launchers and installer reapplication must preserve user config and work with spaces and explicit state selectors.
- Actual independent CLI judgments, deterministic protocol tests and unavailable live backends must remain distinct.

### Task1: Local configuration and privacy boundary (lead)

**Files:** create `ase/project_local.py`, `tests/test_project_local.py`; integrate `ase/credentials.py`, `ase/inspection.py`, `ase/discovery.py`, `ase/knowledge.py`, `ase/core.py` only as required.

**Interfaces:** `find_local_config(start=None)` returns `None` or a dict including canonical `root` Path and parsed strict settings; `validate_local_state(home, project_root=None)` returns boolean for recognized valid local state and raises on unsafe local candidates; `is_private_path(relative)` identifies reserved private paths. `config_bytes(targets, timezone="UTC", log_retention_days=30)` emits validated TOML.

- [x] Write failing behavior tests for discovery precedence/path/config/privacy and verify failure.
- [x] Implement the central module and narrow credential/source integrations; keep unsupported paths rejected.
- [x] Run focused tests, review negative cases and send stable interface evidence to other owners.

### Task2: State routing and CLI (state worker)

**Files:** `ase/projects.py`, `ase/store.py`, `ase/__main__.py`; new `tests/test_project_local_state.py` and scoped changes to existing project tests if needed.

**Interfaces:** extend `resolve_state_home(home=None, project=None)`; explicit home > selected project config > ASE_HOME > platform. Use `find_local_config` and `validate_local_state` from Task1. Local config timezone/retention apply as defaults, explicit user selections preserve authority.

- [x] Add failing tests for two projects, nested cwd, hostile ASE_HOME, existing shared mode and unsafe local-home reuse.
- [x] Integrate registry/store/CLI without weakening arbitrary inside-workspace denial or saved evidence/identity checks.
- [x] Run state/CLI tests and return exact changed files and limitations; no independent commit/push.

### Task3: Local installation and launchers (installer worker)

**Files:** `scripts/install.py`; create `tests/test_project_local_install.py`.

**Interfaces:** add `project_local=False` to preview planning; new CLI `--project-local`. Select exact `<project>/.devframework` home, strict config and `.gitignore`, local backup root. Existing preview/apply format remains auditable. Canonical helper `config_bytes` is supplied by Task1.

- [x] Add failing full filesystem/CLI/launcher tests, including all3 backends, dry preview, reapply, conflicts, spaces, unrelated cwd and moved runtime paths.
- [x] Implement local layout/relative launchers/routing and complete manifest preflight. No outside-user/global changes.
- [x] Run installer tests and provide a disposable local installation for the lead's actual Codex scenario.

### Task4: Integration, evidence and publication (lead with separate reviewers)

**Files:** README, adoption/workflow/credential guides, canonical/adopted relevant skills, dated worklog and validation report.

- [x] Run focused integration, full suites and clean package with a complete source freeze and raw byte-preserved logs.
- [x] Execute real local launcher + SQLite/journal isolation and separate Codex AC/review using the installed local runtime; record full receipts, original source inventory and actual source packet.
- [x] Resolve in-scope P0–P2 findings within existing persisted budgets; retain unverified coverage explicitly.
- [x] Refresh adopted copies with backups, save context/worklog, inspect the staged source inventory for private state, commit and fast-forward push to the existing authorized remote/branch, then verify remote SHA.

Observed source publication: `181a6b73439a18fab40198a3b5dbfc7bd63569aa` on `origin/feature/project-opt-in`; remote SHA equals local. This prose closeout follows that actual push. Final evidence and limits are in `docs/project-local-validation-2026-11-12.md`; private raw receipts and continuation context remain under ignored outputs.
