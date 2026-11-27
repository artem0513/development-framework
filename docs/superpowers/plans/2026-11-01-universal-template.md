# Universal project template implementation plan

Goal: adapt the approved continuity, guarded checkpoint, source completeness and skill routing mechanisms for future projects without preselected organizations, repositories or services.

Architecture: keep SQLite as canonical state, add revision-checked checkpoint history and a task-local source ledger. Skills route to conditional shared references; a portable profile template is descriptive configuration, never permission or automatic network execution. Existing independent verification and publication gates remain unchanged.

Constraints: Python 3.11+ stdlib; preserve sandbox and MCP allowlist; no corporate content, provider bindings or new external authority. Existing user approval covers this adaptation. No remote publication.

- [x] Implement schema v3, CAS checkpoints, task identity and safe restore observations, source revisions/ranges and explicit gaps. Tests first: stale writers, isolation, failed migration, partial/changed sources, empty inventory.
- [x] Update generated skill routers and conditional reference links; add generic profile/checkpoint/source templates and documented lifecycle. No duplicate mutable Markdown truth.
- [x] Run complete unittest suite and CLI lifecycle in two disposable repositories; exercise partial coverage, stale checkpoint, restore drift and deliberate failing tests.
- [x] Independently verify AC and review actual code in separate sessions, fix findings and rerun affected checks.
- [ ] Install backed-up assets and run fresh ordinary-chat E2E using scoped MCP independent verification; inspect receipts and deterministic gate. Report limits separately.
