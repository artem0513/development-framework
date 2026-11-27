# Universal template independent acceptance audit

Date: 2026-11-01. Evaluator: a separate AC-verifier agent; did not implement runtime, skills, templates or tests. Scope: the user's approved generic adaptation and the confirmed AC1–AC4 contract in outputs/universal-task.json. No donor project code or corporate data was imported.

## Results

| Criterion | Expected behavior | Status | Evidence and method |
| --- | --- | --- | --- |
| AC1 | Reusable assets have no donor company/account/provider/repository bindings; future project repositories/sources remain empty and confer no access | PASS | Inspected generated skill entrypoints, conditional references, project-profile/checkpoint/source templates and universal-template.md. Empty repositories/sources and publication disabled; descriptive worksheet explicitly not an execution loader. Donor-specific identifier scan found no matches in new reusable assets. |
| AC2 | Restore original scope/identity, current Git drift and exact next action; stale checkpoints reject atomically with retained history and project isolation | PASS | Independently executed real CLI lifecycle in temporary repositories: original contract and task locator recovered, revision0→1, stale revision rejected with exit3, unchanged source preserved, untracked content drift reported and another repository could not access the task. Independently executed continuity tests including two simultaneous writers, identity uniqueness and migration backup. Final fresh resume chat recovered the same task; independent read-only SQLite query confirmed exactly one task and checkpoint revisions1,2,3. |
| AC3 | Optional source ledger distinguishes empty/partial/blocked/analyzed, identifies unread ranges, requires full coverage and analysis, and rejects stale source replacement | PASS | Independent CLI scenario confirmed UNCONFIGURED/false for empty inventory; unread [5,8] for partial8-line source; rejected partial analyzed record; full range plus analysis became complete; new revision reset coverage; stale source update could not erase the newer blocked revision. Schema/status/range validation and source history were inspected. |
| AC4 | Complete workflow still runs from ordinary chat with independent read-only model receipts, fresh-chat restoration and successful full regressions | PASS | Actual fresh chat start implemented division, recorded hash-bound3-line source analysis, ran3 real unit tests and source check, invoked MCP verification_start/status, obtained separate AC/review judgments and APPROVED. Evaluator checked start and resume log hashes, both raw/log receipt hashes, read-only argv, and current source fingerprint. A separate fresh resume chat recovered original four-AC contract, reconciled Git settings, saved checkpoint2→3, retained COMPLETE source ledger, no drift and APPROVED. Full regression log records73 tests in111.953s, OK, zero skips. |

## Execution evidence

- outputs/universal-independent-cli.json: evaluator's real CLI boundary scenario with runtime hashes; no model adapter.
- outputs/universal-full-tests-final.log: complete73-test execution; examined final counts and success, distinct from independent judgment.
- outputs/demo-universal-final/start.session.log and start.receipt.json: actual ordinary-chat implementation and scoped MCP execution, exit0.
- outputs/demo-universal-final/resume.session.log and resume.receipt.json: distinct fresh-chat continuation, exit0. Read-only SQLite inspection independently confirmed task count1 and checkpoint history1,2,3.
- outputs/universal-positive-evidence/: command receipt, separate actual AC/review raw outputs and logs, deterministic APPROVED gate. Both roles exited0 and used --sandbox read-only. Source fingerprint remained `1c1d31d20517cd186e7c05afe0af5b58d30e744dfb83380e334ccfc5aec16e1b` before and after resume.
- outputs/universal-owner-red.log and universal-owner-green.log: actual owner-mismatch failure then13-test success after exact invocation-scoped safe.directory. Reviewer/helper disabling and host scope remained unchanged. Earlier failed chat is historical evidence, not counted as successful E2E.

## Limits

PASS is bounded to the confirmed mechanisms and exercised workflow. A declared coverage ledger cannot prove semantic reading or discover an unknown source inventory; the demo AC verifier independently read the complete agreed inventory. No external repositories, accounts, collections, schedules or remote publication were configured. The profile worksheet is intentionally descriptive. Only the repository roots explicitly registered with the MCP host are allowed. Git observations deliberately exclude system/global configuration; the fresh resume reconciled core.autocrlf differences rather than hiding them. Local SQLite/hashes provide reliability against accidental stale writes, not hostile same-user enforcement; trusted CI and server-side branch protection remain unavailable here. The separate framework-level deterministic evidence gate is evaluated after this audit is written.

## Inspected runtime hashes

- `ase/continuity.py`: `a88de7c24a45fc926b403f9540212d4b352b757d01b27c68e84029419fcc68a6`
- `ase/store.py`: `eb974883a630a5d72cae5d9f41b3848b015b1111c1626195929b8781566eba6f`
- `ase/__main__.py`: `e9ee9cf553a0d25a6109828aa716232e65734fda1475f09413eeac72450a4463`
- `ase/inspection.py`: `749e83731415df065ad4d5eda206b384c2313336d3bc311bcd3410df0473ccbc`
