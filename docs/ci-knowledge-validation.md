# CI and knowledge validation

Date:2026-11-01. User-approved scope: optional gh-fix-ci and durable historical knowledge in the universal template. No donor company bindings, default sources/accounts, schedules or additional orchestrator were imported.

## Executed evidence

- Full current-source stdlib suite:99 tests in88.259s, OK, zero skips, exit0. Log outputs/ci-knowledge-full-tests-final.log; source/log hashes outputs/ci-knowledge-full-suite-binding.json. The earlier97-test pass predates the final environment and snippet regressions and is retained as historical evidence only.
- Focused10 knowledge tests exercise real SQLite/CLI, dated sources, history, supersession, stale/concurrent rejection, bounds, task/project isolation, lint limitations and schema3-to4 backup migration.
- Focused16 CI tests are controlled gh protocol fixtures: repository/PR/head bindings, exact SHA lengths, changed head, empty/pending/external/malformed evidence, mixed providers, field/job-log fallback, redaction, timeouts, output bounds and ambient scope overrides. They are not live remote CI validation.
- Independent AC evaluator executed separate real CLI knowledge scenarios, additional CI protocol cases and a temporary installer. Evidence outputs/openchamber-independent-cli.json, openchamber-independent-ci.json and openchamber-independent-install.json. No remote calls were made.
- A fresh ordinary chat using the installed project-context-manager read the synthetic local source, recorded two dated claims under storage-decision with explicit1-to2 supersession, queried retained history and ran structural lint. Repository stayed clean; zero tasks/schedules were created. Actual receipt/log under outputs/demo-durable-knowledge; independently checked SQLite and hashes in outputs/knowledge-chat-summary.json.
- Wheel build, compileall and diff check passed. Final installed-source equality and current-source gate are recorded in the final ignored manifest/report, after acceptance audit source freeze.

## Findings corrected

The CI inspector now requires exact40/64-character SHA and exact requested PR identity; an external check does not suppress later available GitHub failure diagnosis. It removes ambient GH repository/host/debug and GIT overrides, retains native auth without printing it, and keeps the relevant failure line within snippet bounds. The installer excludes generated skill caches; the known cache copied during the initial installation was removed explicitly. Independent code review and AC documents identify their bounded evidence and source revisions.

## Limits

No real GitHub repository was selected, so no live GitHub Actions diagnosis, authentication change, remote rerun or publication was performed. Mixed/unknown/skipped evidence remains conservative BLOCKED; a passing job in a failed aggregate workflow can require manual interpretation. Historical claims do not prove current code truth or authorization. Lint checks local link structure and competing claims, not semantic contradiction or remote evidence. SQLite/hashes provide local reliability, not hostile same-user enforcement. Existing sandbox, scoped MCP roots and publication gate remain unchanged. New chats load updated installed skills/runtime; an older already-running MCP process retains its old schema support until a fresh process is started.

## Final job-log correction

The first native gate allowed a P2 finding: a job-specific check retrieved aggregate workflow logs and could attribute a different job failure to it. A two-job fixture reproduced the error (outputs/ci-job-scope-red.log). The inspector now validates selected job id/run/head before fetching only that job log; workflow-level checks without a job retain run-log retrieval.17 focused CI tests pass after the fix (outputs/ci-job-scope-green.log). A fresh complete suite and native gate are required for this final source, recorded separately below/in the final report. The initial gate is historical, not final approval of this correction.

The job metadata head_sha/run_id and job-log endpoint were checked against [GitHub REST workflow-jobs documentation](https://docs.github.com/en/rest/actions/workflow-jobs). This verifies the documented protocol, not a live repository diagnosis.

Final corrected-source complete regression:100 tests in95.813s, OK, exit0, zero skips (outputs/ci-knowledge-full-tests-job-final.log).17CI tests include the selected-job two-job regression; knowledge remains10tests. Current runtime/test/CI hashes and final log digest are bound in outputs/ci-knowledge-full-suite-binding.json.

## Strict check-status follow-up

A second native review reproduced a P2 false PASS when a malformed/unknown status field accompanied a pass bucket. The focused regression failed seven mixed-invalid cases before the fix (outputs/ci-status-red.log). The inspector now validates every supplied status field by type and supported vocabulary, rejects pending/unresolved values and conflicting pass/fail meanings, and never coerces objects into strings.18 focused CI tests pass (outputs/ci-status-green.log); the final full suite and final native report bind this source. Prior100/99 passes are historical after this correction.

Final strict-status source regression:101 tests in99.113s, OK, exit0, zero skips (outputs/ci-knowledge-full-tests-status-final.log). Current runtime/test/CI hashes and final log digest are bound in outputs/ci-knowledge-full-suite-binding.json.18CI+10knowledge focused cases are included.

Final bounded-snippet follow-up: native review reproduced a long preceding line consuming the character budget and clipping out the selected error. The RED fixture covers both long context and a long failure line; character allocation now anchors around the matched failure.19 focused CI tests PASS (outputs/ci-character-bound-green.log). Final complete-suite and source-bound native gate cover this corrected helper; earlier counts/hashes remain historical.

Final bounded-snippet source regression:102 tests in104.444s, OK, exit0, zero skips (outputs/ci-knowledge-full-tests-bounds-final.log). Current runtime/test/CI hashes and final log digest are bound in outputs/ci-knowledge-full-suite-binding.json.19CI+10knowledge focused cases are included.
