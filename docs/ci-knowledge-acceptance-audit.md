# Independent acceptance audit: optional CI and durable knowledge

Date: 2026-11-01. Evaluator: separate /root/openchamber_ac session, not an author of runtime, skills or tests. Scope: user-approved optional gh-fix-ci and durable-knowledge adaptation; confirmed contract outputs/ci-knowledge-task.json. No implementation changes were made by the evaluator. This audit precedes the final framework-level deterministic gate and does not claim that gate has already run.

## Confirmed criteria

| Criterion | Expected behavior | Status | Evidence and independent method |
| --- | --- | --- | --- |
| AC1 | Optional read-only CI diagnosis binds repo/PR/head, handles scoped logs and refuses false success on unknown or stale evidence; protocol fixtures are distinct from remote validation | PASS | Inspected final script and retained scope guards, native-auth handling, ambient GH/GIT scope scrubbing, exact SHA/PR/run/head matching, final head recheck, bounded subprocess and snippets, credential-screening and JSON statuses. Independently executed16 CI tests, all PASS. An additional independent gh protocol scenario retained an external provider as BLOCKED without fetching it and still diagnosed a later GitHub failure with a bounded log. An actual CLI call with nonexistent repository returned structured BLOCKED exit3. No live GitHub reads were performed or inferred. |
| AC2 | Dated source-backed immutable project knowledge, retained supersession, invalid/stale/concurrent refusal, structural lint without semantic authority, backed-up migration | PASS | Independently executed10 knowledge tests PASS, including actual SQLite/CLI, concurrent successor and v3-to-v4 backup cases. Separate actual CLI lifecycle confirmed retained original plus successor, stale predecessor rejection without write, timezone-less date rejection, missing-link and parallel-claim review signals, repository isolation and no generated wiki or repository mutation. Reviewed immediate transaction and unique predecessor constraint. |
| AC3 | Conditional context-manager routing, optional CI license/provenance, generic scope and no schedule/orchestrator authority; tested assets install cleanly | PASS | Inspected generated context-manager pointer, durable reference, optional CI entrypoint, templates and adoption docs. Apache2.0 license and source hashes are retained; unknown upstream revision is explicitly null. Independent actual installer run in temporary destinations verified byte equality for final CI helper/license/provenance/SKILL and knowledge/runtime/reference files, with no skill Python caches. The initially copied global CI cache was identified and removal independently confirmed. No account/source/repository is preselected by these assets; no schedules, blanket delegation or new orchestrator are introduced. |
| AC4 | Full regressions execute and fresh ordinary chat uses installed skill to retain two dated decisions with explicit supersession while preserving repository/task state | PASS | Read final regression log:99 tests in88.259s, OK, zero skips. Independently hash-checked fresh chat receipt/session log, parsed actual executed knowledge-record/query/lint commands and inspected real project SQLite with read-only mode: exactly2 claims, dates and local source preserved, claim2 supersedes claim1, zero tasks. Git status was clean. Chat used installed project-context-manager and reported NO_STRUCTURAL_FINDINGS without asserting present truth or authority. Independent AC and code-review judgments remain separate from executed commands and the later deterministic gate. |

## Execution artifacts

- outputs/openchamber-independent-cli.json: evaluator's seven-group actual CLI lifecycle and runtime hashes; no network or model adapter.
- outputs/openchamber-independent-ci.json: evaluator's additional synthetic gh protocol scenario, actual CLI refusal, preserved initial script hash and final16-test execution/hash. Synthetic gh behavior is protocol evidence only.
- outputs/openchamber-independent-install.json: evaluator's current-source temporary installation and byte comparisons.
- outputs/demo-durable-knowledge/receipt.json and session.log: actual separate ordinary-chat execution, exit0; receipt log hash `32e78270a6b030f96e62e251248259a47abcd11f967a73aeb2b55acdd836b00e` independently matched. Real read-only SQLite inspection confirmed history and task count; Git confirmed unchanged files.
- outputs/ci-knowledge-full-tests-final.log: final99-test regression evidence, distinct from historical initial97-test run. Earlier runs are not substituted for final current-source evidence.

## Limits

No selected live GitHub repository/account was supplied for remote validation; the CI feature is verified through deterministic protocol fixtures and read-only command inspection, not actual remote CI diagnosis. Conservative unknown/skipped/neutral checks and inaccessible evidence remain BLOCKED. Redaction is best effort and raw logs require review before sharing. Historical knowledge and local source references do not prove semantic truth, current code behavior or authority; external evidence is not fetched by lint. SQLite/application append-only history protects accidental stale writers, not a hostile same-user process. Trusted CI/server-side branch protection remain separate enforcement work. The final framework-level native AC/review receipts, global installed-source comparison and deterministic gate are evaluated after this audit is frozen.

## Final inspected hashes

- ase/knowledge.py: `30d7313c9d6bfcc571e3676706842fbea50528bc2081f7089fe5b0d6d84ecd94`
- ase/store.py: `263214531f9193ba2e06ab36c356718d9cb59e8d41b99c67793ba0a463131717`
- ase/__main__.py: `052dff5e9fd7686da31926f3f568e153320c2a398b0399244ac822fded524d40`
- scripts/install.py: `9e98ffcca0d783c3a5e6a951f06b28b25c9fac9b73f083c12bac4380152ac854`
- skills/gh-fix-ci/scripts/inspect_pr_checks.py: `8082da1a148625f3e908d507e8a8ae227a32d322295fdf3b3feb256640be67ac`
- skills/project-context-manager/SKILL.md: `fcfca2a01328f812e930ca434247d2fbb06140981f445cbff06f7a60558ea525`
- skills/project-context-manager/references/durable-knowledge.md: `d173feea85c485a1bc2c6873197f2cc47897b4db9c46c8d63adee2f14271585b`

## Subsequent correction boundary

After this independent audit, native review found job-specific checks could select an unrelated failure from an aggregate run log. A reproduced two-job test now requires job-specific retrieval after id/run/head validation (17 CI tests PASS). The final complete-suite and fresh independent native AC/review gate bind the corrected source; the prior99-test audit/hash remains a historical bounded evaluation. See ci-knowledge-validation.md and final outputs/ci-knowledge-root-report.json.

Final-source boundary: a subsequent native review also reproduced malformed-status false PASS alongside a pass bucket. Strict field typing/vocabulary and outcome reconciliation now reject it;18 CI tests PASS after seven RED cases. The final full suite and fresh native AC/review in outputs/ci-knowledge-root-report.json supersede prior source-bound judgments for this corrected inspector.

Final bounded-snippet follow-up: native review reproduced a long preceding line consuming the character budget and clipping out the selected error. The RED fixture covers both long context and a long failure line; character allocation now anchors around the matched failure.19 focused CI tests PASS (outputs/ci-character-bound-green.log). Final complete-suite and source-bound native gate cover this corrected helper; earlier counts/hashes remain historical.
