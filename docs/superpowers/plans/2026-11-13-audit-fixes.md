# Post-release audit fixes implementation plan

> **For agentic workers:** Use systematic debugging and test-first implementation; independent bounded components may run through native subagents. The lead integrates and obtains fresh independent acceptance and review.

**Goal:** Fix all nine owner-approved findings from the independent audit of commit `3eb3ec9` without weakening existing checks.

**Architecture:** Keep the standard-library local runtime. Bind command launch to current approved task/stage authority and check every ancestor for stop/cancellation. Preserve original recovery history and revalidate prerequisite evidence before dependent effects. Carry explicit source availability and hash-bound readable artifact representations into independent verification.

**Tech stack:** Python 3.11+ standard library, SQLite transactions, existing Codex adapter and portable skill assets.

**Spec:** The owner-approved audit is captured in `outputs/postrelease-audit-20261113/report.json`; its nine requirements are restated below so this plan is reviewable without ignored artifacts.

## Global constraints

- No MCP, Mastra or new runtime dependencies.
- Preserve project isolation, local memory, dated journal, independent AC/review and actual evidence gate.
- No real cloud mutation; live Codex only. Claude/Copilot live checks remain excluded by owner choice.
- User authorized all nine fixes and routine technical decisions. No new approval interview is required.
- Reuse the clean existing `feature/project-opt-in` checkout; do not create another worktree or modify unrelated files.

## Review focus

- Revocation can occur after observation or during authentication; no stale command may cross the launch boundary.
- Cancellation/stop on an intermediate ancestor must block queued work and cancel active descendants.
- Shared checks/resources must not reset history or unnecessarily couple unchanged independent stages.
- A stale completed prerequisite must not authorize mutation; revalidation must not repeat its mutation.
- Missing future outputs differ from missing required inputs; optional sources and readable binary representations must preserve honest coverage.

## Task 1 — P1 execution authority, then unapproved checks (A1–A4)

**Files:** `ase/workflow.py`, `ase/core.py`, `ase/access.py`, `ase/tasks.py` approval only as needed; focused `tests/test_audit_authority.py`.

- [x] Add failing regressions for contract withdrawal during observation, cancellation, intermediate ancestor stop, and task-wide checks executing an unapproved stage.
- [x] Capture current command authority; revalidate the stage, command and ancestry at launch. Use a short SQLite transaction around final validation and process creation, never around process runtime.
- [x] Check the complete ancestry for stop/cancellation in scheduling and active-process callbacks; keep uncertain submitted effects UNKNOWN until reconciled.
- [x] Select and authorize checks by their actual approved stages, including commands without credentials.
- [x] Run focused authority, operation, credential and workflow tests.

## Task 2 — Recovery identity (R1)

**Files:** `ase/recovery.py`, `tests/test_audit_recovery_identity.py`.

- [x] Reproduce explicit-cap and stagnant-history reset when A is renamed and borrows B's criterion ID.
- [x] Preserve unchanged-stage isolation; combine credible original-obligation identities for revised stages rather than short-circuiting on incidental IDs.
- [x] Run recovery suites and regressions for delegates/shared checks/independent stages.

## Task 3 — Current prerequisite obligations (R2)

**Files:** `ase/workflow.py` scheduler region, `tests/test_audit_prerequisite.py`.

- [x] Reproduce deletion/regression of a completed prerequisite before resume.
- [x] Revalidate relevant prerequisite obligations before dependent work; block affected work without replaying prerequisite mutations or invalidating independent branches.
- [x] Verify failed and unchanged prerequisites, changed dependencies and unaffected resume.

## Task 4 — Evidence and discovery (E1–E3)

**Files:** `ase/discovery.py`, `ase/gates.py` source-resource selection, `ase/workflow.py` packet method, `ase/tasks.py` readiness, focused helper/tests and documentation as necessary.

- [x] Add failing tests for binary DOCX with readable representation, missing inspected input before/after refresh, and uppercase unavailable status.
- [x] Normalize a finite source-status vocabulary; require attributed gaps consistently.
- [x] Preserve required/optional input availability and stage impact in readiness and independent packets; do not classify intentionally absent outputs as required inputs.
- [x] Support safe, bounded readable representations explicitly bound to current artifact and representation hashes; reject stale/escaping representations.
- [x] Run discovery, evidence, packet, profile and readiness regressions.

## Task 5 — Integration, independent review and journal

- [ ] Run the full unittest suite after integration; record actual positive counts and source fingerprint.
- [ ] Check generated portable assets and whitespace; verify compatibility on available Python versions.
- [ ] Obtain fresh independent acceptance and code review; fix actionable in-scope findings and rerun affected checks/review before reporting success.
- [ ] Save dated local context/report with proof and limitations. Do not replace historical audit or prior task verification records.

## Progress

- Initial: audit scenarios reproduced; clean existing feature checkout confirmed. A1–A4 lead-owned; R1, R2 and E1–E3 assigned to separate bounded native workers.

- Integration review: added software/empty-criterion ownership, eligible shared-credential stage selection and explicit resume compatibility; prerequisite freshness now applies after candidate edits and per effect/retry. Candidate file edits are bound to original approval/ancestry and serialized with revision/cancellation. Source dependency policies use monotone propagation and stage packet omission retains current mandatory/owned bodies. Validation and final review status is recorded in the dated local report.
- Whole-diff review follow-ups: carry validated prerequisite input observations into final file-write and post-authentication launch guards; bind normalized semantic discovery policy into verification fingerprints. Equivalent refreshes preserve evidence, material coverage changes invalidate it, and saved pre-fix intake is validated without rewriting history.
