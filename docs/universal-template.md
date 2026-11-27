# Universal project adoption and continuity

The framework supplies reusable engineering mechanisms. No repositories, document sources, accounts, provider bindings, schedules or publishing permission are inherited from a donor project. The project-profile worksheet starts with empty repositories/sources. Its fields are documentation for adoption, not a configuration loader or access-control engine.

## Adopt a future project

Only after an explicit request to adopt the template, resolve the supplied canonical checkout, read its own AGENTS and conventions, and activate it with `scripts/install.py --project <repo>`. This installs project-local skills, roles, routing and exact local skill-enable entries, without enabling engineering in other chats. Identify the relevant task/source scope and review actual test commands. Fill a local copy of templates/project-profile.json if useful. Register an MCP repository root separately and only within the user's authorization. Shared runtime or verifier registration is not adoption. No data collection or external sends are implied. Existing gate branch constraints remain feature/* or bugfix/*; the worksheet does not change them. See [project adoption](project-adoption.md).

Use one task contract per logical task. An optional full source locator binds identity within a project; numeric issue IDs alone are ambiguous across repositories. SQLite remains authoritative for the confirmed contract, checkpoint history, source ledger and execution receipts. Files exported for handoff are views and must not become a parallel editable state store. Raw source artifacts remain local references under the adopter's retention/privacy rules.

## Lifecycle

1. `init`, inspect conventions and prepare the confirmed v2 contract from templates/task.json (software) or templates/task-v2.json (document); `ase task create --file PATH` records it. `context` retrieves known tasks before creating replacements.
2. `restore TASK` recovers the contract and saved handoff, compares current branch/full SHA/content/worktree to saved observations and reports drift. It never checks out, fetches, resets or continues side effects. Missing Git state fails closed; an unborn commit is null.
3. `continuity-snapshot TASK` returns revision0 before the first checkpoint. Copy templates/checkpoint.json, record a concise stage/summary/exact next_action plus optional blockers/source_refs/changed_paths/tests/decisions/remaining/task_url. Save with `checkpoint TASK --expected-revision N --file PATH`. Stale writes are rejected; reconcile current facts and retry. Every successful checkpoint appends immutable application-level history in SQLite. This protects accidental stale writers, not a hostile same-user process.
4. If requirements depend on documents/discussions, use templates/source.json. Record stable id/locator/revision/positive extent/unit (lines, cues or bytes) and inclusive 1-based ranges. `source-record TASK --expected-sequence N --file PATH` replaces the current declared source state only when its sequence matches (0 initially). History is retained. Changed revisions do not inherit old ranges. `coverage TASK` exposes unread ranges and explicit gaps. Fully_read requires all ranges; analyzed additionally needs an analysis reference and no gaps. A blocked source requires a concrete blocker. Empty inventory is UNCONFIGURED, never complete.
5. Implement and run `check TASK`. Invoke the scoped MCP verification tools from a chat; inspect the deterministic gate. Source ledger completeness is declared evidence, not independent semantic validation. A mandatory source requirement belongs in an observable task AC with command-backed checks. Updating metadata does not manufacture a gate receipt or approve publishing.
6. Checkpoint significant results and before handoff/final reporting. Restore on the next chat, reconcile drift, and continue one precise next action. Checkpoints are active-agent persistence, not guaranteed autosave. Distinguish local changes, checks, independent judgment, commits, publication and merge.

## Skill routing

The core skills remain the entrypoints. project-context-manager reaches its task-continuity reference for handoffs; task-orchestrator reaches routing and conditional source completeness; project-bootstrap handles future project adoption. Development methods from installed upstream skills operate within this single workflow. User instructions and local policy retain authority. No corporate approval ritual, provider or fixed workday is embedded.

## Verification boundaries

Schema v3 migration backs up existing v2 state; prior tasks, sessions, decisions and gates remain readable. Existing read-only reviewer sandbox and host path/tool allowlist are unchanged. CLI/unit tests prove data and protocol behavior; fresh independent model sessions judge actual AC/review. The framework remains a local reliability layer; enforcement against a hostile same-user process needs trusted CI and branch protection.

Safe Git inspection explicitly trusts only the selected canonical checkout for that one invocation, because the Windows sandbox account can differ from its owner. Global Git configuration remains ignored; fsmonitor, filters, hooks and external diff helpers remain disabled. This is not a sandbox permission change or a global safe.directory setting.

## Optional CI and durable knowledge

For source-backed historical decisions and corrections, project-context-manager conditionally loads durable-knowledge.md. SQLite stores dated claims and supersession history; it does not maintain a second editable Markdown wiki. For a selected failing GitHub Actions PR, optional gh-fix-ci gathers read-only diagnosis. Installing it does not authenticate accounts, select a repository, rerun CI, change source or publish. See [CI and knowledge](ci-and-knowledge.md).
