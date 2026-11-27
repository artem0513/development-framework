# Review packets for large changes, private check inputs and baseline revisions

Status: implemented 2026-11-24 (owner-approved direction). Python standard library only; no
schema migration. Code: `ase/evidence/representation.py`, `ase/evidence/check_inputs.py`,
`ase/tasks.py` (`record_baseline`), wiring in `ase/evidence/packets.py`, `ase/review/service.py`,
`ase/gates.py`. Tests: `tests/test_review_packet.py`, `tests/test_nested_repositories.py`.

## Baseline after a revised repository or base

A task's source baseline is recorded at approval and promoted by the first run. It records the
repository and base ref it was taken for. When `task revise` changes the contract's
`repository` path or `base` and the task is approved again, the baseline is recomputed **from the
new base commit** (never from the working tree, so native edits made after the first approval
stay visible as changes), and a `baseline_replaced` event in the task history names the old and
new repository/base and fingerprints. Without a base ref there is no trustworthy before state:
`revise` (and a run that meets such a baseline) refuses with "create a new task or set a base
ref". An unchanged repository/base keeps its baseline as before. If recording the new baseline at
approval failed (reported as `baseline_warning`), the gate blocks while the recorded baseline
belongs to another repository/base; the next `task run` recomputes it.

Limit: a file created natively after the first approval and never committed is indistinguishable
from a pre-existing untracked file at the new base and is treated as present before the task.

## Private check scripts as review context (`check_inputs`)

Evidence commands may run project check scripts kept privately. Declare them in the contract:

```json
"check_inputs": [{"path": ".devframework/local/checks/no_old_helper.ps1", "evidence_ids": ["rename-check"]}]
```

- Only files under `.devframework/local/checks/` qualify: never the state database, credentials
  or anything else under `.devframework/local/`. Links, hardlinks and files over 64 KiB are refused;
  at most 20 inputs.
- Independent sessions receive each body redacted under `_ase_check_inputs/<path>` plus
  `_ase_context/check_inputs.json` (path, the checks that run it, current and approval-time sha256,
  whether it changed since approval (the approval record keeps the hashes seen at approval),
  supplied/redacted/unavailable).
- A declared input that cannot be read (deleted, linked, hardlinked, too large) blocks the gate.
- The hashes join the source fingerprint (not the changed-file inventory). Editing a declared
  script after approval makes the check bundle and every judgment over it stale; checks and
  independent sessions must run again.
- When project source and approved task identity are unchanged, a completed implementation capture
  can still anchor the first lead review after fresh checks. The framework binds the old script
  hashes from a sealed packet to the current source to confirm the match. A capture made before
  applying code edits still needs proof for that exact completed stage.
- The private home stays excluded from Git: the scripts are no changed file, never staged,
  committed, named in the PR body or `ci-check`, and `finish` private-leak checks still hold.

Recommendation for such scripts: print one explicit result per rule (what is checked, the
threshold, the violations found), and keep a separate sensitivity run with an injected break
that must fail, so a reviewer can see the check can detect what it claims.

## Compact changed-file representation (`review_packet`)

Default (no field): each changed file goes in full plus its full `_ase_before/` version; the
packet bound follows the backend's current model, capped at 2 MiB (see context-transport.md). For a large
mechanical change the owner may approve:

```json
"review_packet": {"version": 1, "changed_files": "diff_context", "context_lines": 20, "full_body_max_bytes": 4096}
```

When changed files use compact or split representation, the packet omits the redundant whole-branch
patch. Coverage still reports missing required per-file inputs or base versions.

- Large modified files are sent as a unified diff with `context_lines` (3-200) around each hunk
  under `_ase_diff/<path>` instead of two full copies. Files up to `full_body_max_bytes`, new files
  and required artifact evidence keep full bodies.
- For a caller, middleware or dependency whose complete body is needed to judge behavior, name
  the exact file in discovery's existing `scope.dependencies`. Record why it matters, the related
  path in `from` when known, and `blocking_stage_ids` when the need is stage-specific. A required
  file is supplied whole, including the available before version when it changed. This uses the
  existing source and packet limits. Directory declarations keep their normal source coverage rules.
- The mode is a contract field: it is part of the contract hash, needs the owner's approval and
  is **never chosen automatically by size**, because an automatic switch would silently weaken
  review of a large semantic change. When a full-copy packet overflows, the error tells the bot
  to ask the owner for this mode.
- `_ase_context/changed_files.json` and `coverage.changed_file_mode` tell the reviewer the mode
  and, per changed file, whether it got `FULL_BODY` (with reason), `DIFF_CONTEXT`, `DELETED`, or which
  part reviews it.
- A diff needs the before version: a base ref in `repository`, or an earlier implementation
  capture. Without one every file falls back to `FULL_BODY` and the packet does not shrink. The
  existing limit of 1000 changed files per packet still applies. Code outside a diff's context was not supplied; reviewers name judgments that need
  it as limits.

## Review parts when even the compact packet does not fit

If the compact packet still exceeds the bound, the error proposes a split by folder (a JSON
`parts` list computed from the actual per-file sizes). It is a proposal: the owner approves it as

```json
"review_packet": {"version": 1, "changed_files": "diff_context", "parts": [
  {"id": "p01", "resources": ["src/svc/alpha"]}, {"id": "p02", "resources": ["src/svc/beta", "src/svc/gamma"]}]}
```

- `verify` then runs one extra independent review session per part (at most 8 parts; they run
  at the same time as the whole-task sessions, inside the same call budget). A part packet carries
  its assigned changes plus required discovery files for its stage. When a required file belongs to
  another part, `context_only` marks its full body as evidence for interactions and `part` identifies
  the part that reviews its changes. `in_other_parts` counts changes from other parts whose bodies
  were omitted. Whole-task packets can also use the required files and mark omitted changed bodies
  `REVIEWED_IN_PART`.
- Each part receipt is stored as `verification_part` and bound to the current fingerprint,
  contract, plan, checks and the part's own hash, so one part's session never stands in for
  another.
- The gate refuses unless every changed file is covered by **exactly one** declared part and every
  part has its own current, independent, substantive review without P0-P2 findings or unverified
  material. A missing, failed, truncated or bypassed part review never becomes PASS. P0-P2 part
  findings have no disposition path: they are fixed and reviewed again.
- Only code review is split. Changed test files (the `ci-check` test-path pattern) stay in the
  whole-task packets, so acceptance and test adequacy read the tests themselves. Their manifest
  says the other bodies were not supplied to them and that part sessions only code-review; a
  judgment that needs such a body is to be returned UNVERIFIED, which blocks. This last step
  relies on the independent session reporting the gap: no gate rule can tell whether a verdict
  needed a body. A task whose acceptance needs the changed production code itself is better
  split into separate tasks.
- Cross-part judgments depend on the actual contract, evidence and required files supplied to the
  session. The owner approves the split and the mode.

## Context files stay smaller than the change

A real 444-of-1318-file mechanical change produced a packet whose context files outweighed
the change. Now:

- `_ase_context/change_impact.json` is built after the representation, so `caller_in_packet`
  names only bodies the session really has. Each caller path is listed once in `caller_paths`
  and named by index per change; `excerpts` and `excerpt_omitted` are keyed by index, and
  `callers_not_listed` counts matches beyond the 30-caller limit. A part lists only its own
  files; a whole-task packet with parts keeps a paths-only index of the files its parts review
  (`CHANGE_NOT_CARRIED`).
- `changed_files.json` rows no longer repeat the hashes in `source_changes.json`; a part packet
  counts other parts' files in `in_other_parts`.
- With an approved `review_packet`, `source_changes.json` keeps the unchanged path list within
  32 KB (`unchanged_path_count` stays exact; `current_fingerprint` binds the inventory).
- An overflow error ends with a size breakdown: changed files, context files (largest first),
  other.

Measured on the fixture: logical packet 2.94 MB to about 1.7 MB, `change_impact.json` 1.16 MB
to about 140 KB.

## Not built (needs a separate owner decision)

A reviewer fetching additional files during its session (HIGH, trust boundary: it would turn the
sealed, hashed packet into an open read of the workspace). Sessions stay closed over the packet.
