# Independent code review: optional CI diagnosis and historical knowledge

Date: 2026-11-01. Reviewer: separate /root/openchamber_review session, not an implementation author. Inspected actual runtime, schema migration, CLI, optional CI helper, skill routing/references, templates, licensing/provenance and installer. No code changes made by reviewer.

## Findings and corrections

- P2: malformed 41-character hexadecimal PR/run SHA was accepted by a 40-to64 range regex and could return PASS. Reviewer independently reproduced this with subprocess fixtures. The adapted helper now accepts exactly40 or64 characters; regression covers rejection.
- P2: an explicit requested PR7 could accept returned PR8 metadata and report its successful checks. Reviewer independently reproduced this. Returned PR number is now bound to the explicit numeric/URL identity; regression covers rejection, including subsequent head recheck.
- Root/worker additionally corrected mixed external-provider inventory handling to retain later GitHub diagnoses while remaining BLOCKED, and Bearer header redaction. Reviewer inspected and reran those regressions.

No unresolved actionable findings in the reviewed version.

## Independently executed verification

- `python -m unittest discover -s tests -p test_knowledge.py -v`:10 tests PASS in1.639s. Real SQLite/CLI, dated sourced records, historical supersession, exactly-one concurrent successor, old-data migration/backup, local-link lint and project isolation.
- `python -m unittest discover -s tests -p test_ci_inspector.py -v`:14 tests PASS in0.016s. Deterministic gh protocol fixtures only, not live GitHub validation. Includes malformed/empty/pending evidence, foreign URL/run/head refusal, SHA/PR binding, pending-log job metadata fallback, final PR-head recheck, read-only subprocess timeouts/output bounds, secret-screening, mixed-provider preservation and JSON statuses.
- Inspected isolated installer result under outputs/ci-install-smoke: CI helper, retained Apache2.0 license and conditional durable-knowledge reference present; global skill copy contains no __pycache__ or .pyc. Reviewed installer filtering change.

Knowledge uses append-only application history in the existing per-project SQLite database; supersession checks project/topic/date and stale predecessor within an immediate transaction, with a unique predecessor constraint. Knowledge lint flags structural issues/parallel claims and explicitly does not assert semantic contradiction or present authority. GitHub inspection is optional and never executes log instructions, reruns workflows or publishes. Evidence is bound to selected host/repository/PR/head, including successful checks; inaccessible and unknown evidence stays BLOCKED.

## Limits

This review is a bounded independent judgment, distinct from the full regression suite, fresh ordinary-chat acceptance scenario and final deterministic gate, which the parent executes separately. No remote GitHub/account was selected or queried. Conservatively skipped/neutral check states or successful jobs inside a failed run can remain BLOCKED. Log redaction is best effort. Local same-user integrity is a reliability boundary, not hostile-process enforcement. Exact upstream revision of the local donor package is unknown and is not represented as pinned; local source hashes, adaptation notice and Apache2.0 license are retained. Historical-knowledge mechanisms were reexpressed without copying malformed chief-of-staff packaging, blanket delegation or scheduler authority.

## Reviewed hashes

- `ase/knowledge.py`: `30d7313c9d6bfcc571e3676706842fbea50528bc2081f7089fe5b0d6d84ecd94`
- `ase/store.py`: `263214531f9193ba2e06ab36c356718d9cb59e8d41b99c67793ba0a463131717`
- `ase/__main__.py`: `052dff5e9fd7686da31926f3f568e153320c2a398b0399244ac822fded524d40`
- `skills/gh-fix-ci/scripts/inspect_pr_checks.py`: `07c370e2328ab9b572d57b8ce173e650683b51d8ae8e7d38eb5abcfd8da12338`
- `skills/gh-fix-ci/SKILL.md`: `774d9260555a2218b0f529488c5a15e75108d69189b3bc15283f839901eba808`
- `skills/gh-fix-ci/LICENSE.txt`: `44e03f7e263eb3ad256440c1eeab5e49168baff5a78ecef9a5447261cf3a91bc`
- `skills/gh-fix-ci/provenance.json`: `5711dbda0560a9e4f861009dfbddbd0645c4c884edaf8938c81ac6d384180d66`
- `skills/project-context-manager/SKILL.md`: `fcfca2a01328f812e930ca434247d2fbb06140981f445cbff06f7a60558ea525`
- `skills/project-context-manager/references/durable-knowledge.md`: `d173feea85c485a1bc2c6873197f2cc47897b4db9c46c8d63adee2f14271585b`
- `scripts/build_assets.py`: `20aef3b33243a806c10dbabf7cd360201d961bfa20e0687047d0e18b1b978bd5`
- `scripts/install.py`: `9e98ffcca0d783c3a5e6a951f06b28b25c9fac9b73f083c12bac4380152ac854`
- `tests/test_knowledge.py`: `c795182433c5e0b382a6173258a6c6c290ead2d88fc9aea3139798041b51bef5`
- `tests/test_ci_inspector.py`: `a1f6098e16ab5b13e50263ba2e61a70fcb1b493e781821d19765d1beacc7c13a`

## Subsequent scoped changes

After the bounded review above, CI inspection additionally scrubs ambient GH_REPO/GH_HOST/GH_DEBUG and GIT_* redirection while preserving native authentication, and ensures a bounded snippet retains its failure line. The independent AC session reran all16 final CI tests and inspected these changes. The final source-bound native code review in the task gate evaluates the resulting complete source; the earlier hashes above remain historical rather than claimed-current. No additional network access or sandbox changes occurred.

## Native review follow-up

The subsequent native code reviewer reproduced P2 job-log misattribution. Parent added a failing two-job regression and fixed collect_log to validate and retrieve the selected job;17 focused CI tests pass. Final fresh native review/gate evaluates this corrected source. Earlier reviewed hashes/counts above remain bounded historical judgments.

Final-source boundary: a subsequent native review also reproduced malformed-status false PASS alongside a pass bucket. Strict field typing/vocabulary and outcome reconciliation now reject it;18 CI tests PASS after seven RED cases. The final full suite and fresh native AC/review in outputs/ci-knowledge-root-report.json supersede prior source-bound judgments for this corrected inspector.

Final bounded-snippet follow-up: native review reproduced a long preceding line consuming the character budget and clipping out the selected error. The RED fixture covers both long context and a long failure line; character allocation now anchors around the matched failure.19 focused CI tests PASS (outputs/ci-character-bound-green.log). Final complete-suite and source-bound native gate cover this corrected helper; earlier counts/hashes remain historical.
