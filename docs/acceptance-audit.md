# Independent acceptance audit

Follow-up for nested Windows verification: see [nested-acceptance-audit.md](nested-acceptance-audit.md). The original BLOCKED result here records the initial implementation checkpoint. The host-managed workflow was subsequently tested from an ordinary chat without external manual continuation; server-side production CI enforcement remains outside that fix.

Audit date: 2026-11-01, Europe/Bucharest. Scope: the original mission in
`docs/mission.md`, actual source, installed user files, executed integration
tests and raw disposable-demo artifacts. The auditor authored the automated
test harness and example generator, but did not author the runtime, gate,
installer, skills or documentation being audited. This is an independent
runtime acceptance pass with that test-authorship limitation.

Status vocabulary: PASS means the stated, bounded behavior has observable
evidence. UNVERIFIED means evidence is missing or the broader claim exceeds
the executed scenario. BLOCKED identifies a necessary external dependency.
A local gate approval is not a production security certification.

| ID | Expected behavior | Evidence and method | Result | Confidence limits / missing coverage |
|---|---|---|---|---|
| A01 | Research current Codex and all seven named candidate repositories before selection | `docs/research.md` has official source links, current installation routes, candidate matrix and local CLI inspection; `docs/ADR-001.md` records choices and trust boundary | PASS | This audit inspected the research artifact, not every upstream maintenance claim independently. Installation is evaluated separately. |
| A02 | Real executable implementation with small modular components | `ase/core.py`, `ase/store.py`, `ase/__main__.py`; actual subprocess CLI tests execute the runtime on disposable Git repos | PASS | Python stdlib is the permitted maintainable alternative to .NET. No client application certification follows from tool tests. |
| A03 | Global installation preserves existing configuration and creates required reusable skills and agents | `scripts/install.py`, eight `skills/*/SKILL.md`, three native `agents/*.toml`; user installation manifest and actual user files inspected; installer preservation/repeat-backup test passed; installed/source `core.py` SHA256 both `428352bafdacbc205f74a280faf97341c30d082105c602a36999a3e23e8f72fa` after final runtime fixes | PASS | Base user `config.toml` is preserved; separate `autonomous.config.toml` enables agents. Natural-language activation is guidance-driven. |
| A04 | Task understanding, confirmed ACs and lightweight planning before implementation | Workflow/skills and frozen task contract; actual `outputs/demo-natural/session.log` starts from an ordinary software request, creates task memory/contract, implements safe division and records 3 real passing tests | PASS | Established for the explicit small sample. Requirements/assumptions/planning are agent guidance rather than deterministic semantic understanding; materially ambiguous requirements still require human answers. The complete nested natural-language run remains separately BLOCKED in A19. |
| A05 | Conditional delegation without unsafe concurrent writers | the v1 CLI offered two disjoint read-only verifier sessions with `--parallel` (flag removed 2026-11-22 after the v1 engine's retirement; v2 runs AC and review as separate sessions); protocol concurrency test passed; runtime gives each role separate output paths | PASS | Parallel implementation worktree creation and merge conflict recovery are guidance, not an automated writer scheduler. No parallel writer scenario was executed. |
| A06 | AC verification and code review are separate from implementation and use raw evidence | `_verify` launches separate `codex exec --sandbox read-only` processes with original contract, baseline/diff, code and command logs; defective live demo contains actual Codex receipts, AC2 FAIL and reviewer P1 | PASS | Separate model contexts share user identity, provider and model limitations; they are not mutually distrustful OS principals. |
| A07 | Tests actually execute; missing tools, zero/skipped tests, failed build and timeout cannot pass | CLI integration tests launch real Python/Git processes and assert rejection/blocking; source command/log receipts include cwd, timestamps, exit, counts, source digest | PASS | .NET/Angular/Azure commands are detected/configured, but no real .NET, Angular or cloud integration application was exercised in this audit. Unsupported test output formats fail closed. |
| A08 | Deterministic gate rejects stale/altered evidence, missing review, failed AC and blocking review findings | Source fingerprint tests cover tracked and untracked changes; raw artifact tampering, superficial PASS, missing verifier and linked optional failing command cases passed | PASS | Hashes validate internal association and accidental alteration, not provenance against an adversary who can rewrite all receipts, database and runtime. Test protocol fixtures are explicitly not live independent judgments. |
| A09 | Protected path changes reject and HIGH risk requires human action | Protected-file test and HIGH-risk current-fingerprint approval test passed; default protected patterns inspected in runtime | PASS | Broader HIGH-risk regression/security selection remains agent policy and per-task command configuration. The local approval file is writable by the same user. |
| A10 | Safe feature-branch publishing is dry-run by default and explicit remote permission is required | Local bare-remote subprocess test performs dry-run, unauthorized refusal, authorized push and stale-defect refusal; main/trunk/staging and multiple push URLs reject | PASS | No real service was contacted. Generic secret scanning is heuristic. Local hooks/configuration and the same-user authorization file are not adversarial enforcement. |
| A11 | Native Git hook blocks protected/non-HEAD/unverified pushes and preserves existing hook | Actual Git for Windows pre-push execution test passed for main, divergent feature ref and missing gate; existing hook preservation test passed | PASS | Hooks can be bypassed. This establishes a local accident barrier only. |
| A12 | Trusted server-side publishing enforcement | `templates/trusted-ci-policy.md` defines separate protected runtime/policy, identities, attestations, credentials and required checks; included CI runs the tool tests | BLOCKED | No repository host, ruleset, protected CI identity or attestation service was configured. Included CI is not a production task gate. Production enforcement must be deployed and tested in the intended repository. |
| A13 | SQLite persistence, project isolation, migrations and backups | Separate canonical-path project DBs, schema versioning and SQLite backup API; restart/isolation/backup integration tests passed; focused v1-to-v2 migration test verified preserved tasks and actual pre-migration v1 backup | PASS | Path isolation is application isolation under one user, not multi-tenant access control. |
| A14 | Restart/recovery, duplicate prevention and bounded remediation | Live lock refusal, interrupted lock recovery, approved idempotent resume, complete workflow protocol and retry-cap integration tests passed; actual blocked natural-language task subsequently completed external check/verify with an independently recomputed APPROVED gate at 12:40:39 UTC | PASS | Lock recovery test uses a dead-PID receipt and deterministic Codex adapter; abrupt power loss during arbitrary external destructive side effects was not simulated. Runtime is intended for code/test work, not unattended deployment transactions. |
| A15 | Time attribution distinguishes agent, draft human and confirmed human work; corrections and summaries persist | CLI time/daily and focused weekly tests verify separate totals and date boundaries; focused correction test inspects actual before/after SQLite audit and confirmation reset; session summary test retrieves saved decisions/unresolved questions in a fresh CLI process | PASS | Agent runtime is not billable human time. Weekly dates use stored start-date offsets; intervals are not split at midnight or converted across zones. |
| A16 | Controlled defect is independently detected and cannot publish | `outputs/demo-live/.../check.json` records 3 executed tests, 1 failure, exit 1; `ac.raw.json` independently reproduces wrong ZeroDivisionError; `review.raw.json` reports P1; `outputs/defect-publish.json` is REJECTED/published false | PASS | The current negative demo is intentionally defective. Its rejection is success of the experiment, not approval of that application. |
| A17 | Complete live implement/test/independent-review/verify/report/publish demonstration | Actual Codex implementation exit 0 and 7-test execution in `outputs/demo-full`; `outputs/positive-report.json` contains both AC PASS and empty independent review, with both verifiers rerunning tests; auditor independently reran all 7 tests and recomputed gate APPROVED at 12:36:13 UTC; actual bare remote ref equals `outputs/publish-local.json` commit `de488ec805f3095cc13715a38cc4dedb80dd954a` | PASS | This starts from a confirmed task contract and uses a small Python sample. It does not prove correct autonomous requirements discovery for arbitrary real projects or production technology stacks. |
| A18 | Documentation and framework guidance are available without installing unrelated packages | Workflow, vendor routing, installed vendor installer and provenance mechanism exist; actual Angular and C# Microsoft Agent Framework skill files/locks inspected; `dotnet-test` plugin observed installed/enabled 1.0.0 | PASS | Other .NET/Azure/Mastra integrations are conditional routing/install support, not claimed exercised services. No real Azure service deployment is authorized or tested. |
| A19 | An ordinary request completes nested independent verification under the installed Windows sandbox | First `outputs/demo-natural` run correctly reported missing Codex home; launcher propagation was corrected/tested. Actual `outputs/demo-natural-fixed/session.log` still reports BLOCKED for nested CLI Access denied. External continuation in `outputs/natural-external-check.json`, `outputs/natural-external-verify.json` and `outputs/natural-external-report.json` subsequently APPROVED; auditor independently recomputed the current gate with matching fingerprint `642c8eea18fb6121686243875c8d76405f0f0244496188ee2d6c1af67cc9dd88` | BLOCKED | The entirely unattended nested-sandbox path remains blocked. The ordinary request plus permitted external continuation works and preserves truthful status. Parent-model exit 0/native judgments do not substitute for missing runner receipts. |

Final automated evidence: `python -m unittest discover -s tests -v` executed
**42 tests in 80.069 seconds, OK**; completed log: `outputs/tests.log`.
Current installer packaging/preservation was additionally checked after the
last packaging adjustment: **1 test, 0.844 seconds, OK**. The complete installed
launcher missing-environment and explicit-home regression, including absolute
Windows `--home=C:\...`, passed separately: **1 test, 9.312 seconds, OK**.
The suite includes actual native Windows Git hook execution. Tests that substitute
a deterministic subprocess for Codex prove orchestration/receipt behavior only;
the live positive and negative Codex demos provide separate judgment evidence.

Audit outcome: **17 bounded groups PASS; 2 groups BLOCKED** (A12 production
server-side enforcement and A19 entirely unattended nested Windows verification).
The local standalone CLI pipeline and permitted external continuation are
validated. Production-enforced publishing and universal unattended operation
are not approved by this audit. Source repository has no commit or remote at
the final checkpoint; demo commit identifiers refer only to disposable repos.

Production use remains conditional on repository-specific build/test/AC policy
and external publishing enforcement. The implementation reduces unsupported
completion claims through recorded execution, separate judgments and a
fail-closed gate; an unconditional claim that 90% of arbitrary engineering work
is automated or that hostile-agent bypass is impossible is not established.

Runtime regression discovered during the live exercise: a completed process
could wait for a background descendant to close inherited stdout. The focused
test first failed at 5.075 seconds, then passed in 0.075 seconds after the runner
switched from pipe EOF waiting to direct-process completion with an anonymous
output spool. This is separate evidence from the initial 32-test checkpoint.
The expanded suite completed successfully after the substantive fixes;
earlier red runs and in-progress logs were not treated as successful checks.

Data retention: SQLite stores concise structured records, while raw Codex
subprocess evidence logs can include tool output, source excerpts and
proprietary business details. Generic credential redaction is heuristic.
The runtime now lazily expires known unlocked task logs/handoffs after
30 days by default (`ASE_LOG_RETENTION_DAYS`, 1–365); focused tests passed for
expiration, summary preservation, unknown/current file preservation, active
lock protection and invalid configuration. Expiring raw evidence means old
tasks need fresh verification. Treat each external project state directory as
sensitive; locked tasks conservatively retain evidence until recovery.
