# CI-F1: common job identity validation

User-approved correction of the finding in outputs/fresh-audit-ci/report.md. A successful job-specific check could previously return PASS without proving that its job existed in the selected run and PR head. The failure branch already validated these fields while retrieving logs.

The inspector now calls a shared validate_job before assigning either PASS or FAIL. It verifies job id/run_id/head_sha and blocks unavailable or malformed metadata. Failure log retrieval reuses that validated metadata instead of fetching it twice; standalone collect_log callers retain validation. A successful job performs one metadata read and no log retrieval. Run-level checks without job ids retain their previous path. No authentication, sandbox, publishing policy or automatic job rerun was changed.

Regression test first:22 tests exposed8 failures before the fix (outputs/ci-f1-red.log), then all22 passed (outputs/ci-f1-green.log). Added valid/nonexistent/unavailable/wrong-id/run/head/malformed/missing-head successful jobs, failure metadata call-count/log ordering, and run-only success checks. Independent verifier matrix18/18 and audit probes20/20 pass; focused independent reviewer ran22 tests plus16 own probes with no actionable findings. Their reports remain separate judgments under outputs/ci-f1-ac/ and outputs/ci-f1-review/.

A complete current-source suite, installed-source comparison and fresh separate native AC/review gate are recorded in outputs/ci-f1-report.json and outputs/ci-f1-verification-manifest.json after execution. Historical audit evidence is preserved; the original false-green probes were not rewritten to hide the defect. These CI scenarios use controlled gh responses; no live GitHub repository or server-side enforcement was tested.

Executed full regression:105 tests in100.146s, exit0, zero skips (outputs/ci-f1-full-tests.log), source/log binding in outputs/ci-f1-full-binding.json. Independent verifier subsequently confirmed all5 package files equal in source, global installation and installed runtime. Protocol scope remains separate from live GitHub validation.
