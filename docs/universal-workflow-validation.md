# Universal workflow validation

Date: 2026-11-12, Europe/Bucharest. Current executed results and their exact source boundaries are listed first. Earlier failures, timeouts and partial model judgments remain historical evidence. A successful fixture or a model's written PASS alone does not establish the entire framework.

## Resumed final checks — current implementation

| Check | Actual result |
| --- | --- |
| Windows, Python 3.15.0 | **430 tests, OK**, 221.191 seconds; [complete log](../outputs/universal-workflow/resumed-20261112/full-suite-py315.log), [process result](../outputs/universal-workflow/resumed-20261112/full-suite-py315-result.json) |
| Windows, Python 3.12.14 | **430 tests, OK**, 321.096 seconds; [complete log](../outputs/universal-workflow/resumed-20261112/full-python312-20261112T091213Z-624e34bb/suite.log), [strict reparse](../outputs/universal-workflow/resumed-20261112/full-python312-20261112T091213Z-624e34bb/reparsed-result.json) |
| Source consistency | [112-file freeze](../outputs/universal-workflow/resumed-20261112/product-freeze.json) matched throughout both suites, actual CLI acceptance and final model sessions; package additionally verified runtime/build inputs and expanded product inventory |
| Directory defects | Six new RED/GREEN regressions and [independent actual probes](../outputs/universal-workflow/resumed-20261112/directory-independent-probes.json): Git root/worktree, ignore behavior, dependency body/hash refresh, binary gap and redirected/unreadable sources |
| Actual Codex public CLI | [Fresh mixed root-directory run](../outputs/universal-workflow/resumed-20261112/live-mixed/r-7bc46c49/report.md): software/document stages and final parent APPROVED/COMPLETED, 3/3 tests, eight valid distinct receipts, original tests/brief/policy and runtime unchanged |
| Final independent supplement | [Actual separate AC/review](../outputs/universal-workflow/resumed-20261112/acceptance-20261112T092304-b60de48a/report.md): all six focused criteria PASS, missing coverage empty within that scope; review findings empty; distinct processes/provider sessions and receipt integrity independently revalidated |
| Fresh package | [Clean wheel validation](../outputs/universal-workflow/resumed-20261112/packaging-validation-report.md): 35 members, no ase.host, dependencies [], isolated/module smoke on 3.12/3.15; Python 3.11 grammar only |

Current wheel SHA-256: `ba76bdf6556bca5c17cdba9764a68da88b400dba7f040fc8ec9a5d8e97fc9ad1`. The same code was tested, packaged and reviewed. Only prose closeout and the dated worklog were updated afterward; no runtime/skill/script/test changes were made after verification.

The Python 3.12 process completed once with exit zero, no timeout/cancellation/error and a complete 430-test terminal OK. Its auxiliary output parser initially rejected extra blank Windows log separators. The original NOT_PASS record and runner are retained; a separate strict reparse validates the unchanged log hash and terminal flags. This is a parser correction, not another test execution or a rewritten failure. The earlier overnight 359-second timed-out run remains NOT_PASS.

The final model packet includes the actual parallel-policy consumer input/response/receipt, current live stage history and package/full-suite proof that were missing from earlier packets. Candidate `64173aebf0db39f7232118c551236293d488ee9e8d2a411ec2f7371abb32a43f` was unchanged during both final roles. The six criteria are AC01, AC09, AC13, DIR_P2, PARALLEL_POLICY and REGRESSION_BOUNDARY. This is incremental acceptance against the prior immutable source/coverage boundary, not a claim of fresh semantic review of every unchanged file or a new fourteen-criterion whole-project PASS. Raw previous judgments remain unchanged.

Both open directory P2s from the overnight review are fixed. Software directory discovery uses Git's tracked/nonignored inventory; explicit ignored file dependencies still work. Generic directory traversal excludes private Git metadata before descent, preserves bounds and rejects redirection. Snapshot and model packets share the same direct/discovered boundary list, and dependency directories expand from the actual snapshot. Undeclared dependencies are not assumed complete.

Current local implementation work is complete within the agreed scope. Unavailable environments and the separate unresolved manual drive-root cleanup remain explicitly limited below. [Dated worklog](worklog/2026-11-12.md), [saved continuation context](../outputs/universal-workflow/RESUME.md).

## Earlier executed results — historical

| Check | Result |
| --- | --- |
| Windows, Python 3.15.0 | **421 tests, OK**, 249.887 seconds; [complete log](../outputs/universal-workflow/release-candidate-py315.log); before the final stage-history amendment |
| Windows, Python 3.12.14 | **421 tests, OK**, 361.612 seconds; [complete log](../outputs/universal-workflow/packaging/full-suite-python312.log); same source boundary |
| Candidate stability | [68 product-file hashes](../outputs/universal-workflow/release-candidate-freeze.json), no drift during both 421-test suites; later amendments require fresh proof |
| Package | Clean isolated build; no ase.host; dependencies []; extracted-wheel smoke on both versions |
| Python 3.11 | Grammar compatibility checked; interpreter unavailable, so execution is not claimed |
| Generated assets | Eleven canonical skills, three native targets, descriptor consistency and scoped hash audit passed |

The 421-test candidate wheel SHA-256 is `1cd0daa31a4df812fab55d474770ce8279d15a2a552490c32bf29686c718b0b1`. [Packaging report](../outputs/universal-workflow/packaging/report.md) records source equality, installed execution and the metadata-only migration preview. This wheel predates the last stage-history amendment. The wheel supplies runtime/CLI; the installer supplies skills/descriptors separately. Earlier concurrent-change failures and stale wheels remain historical outputs, not final success evidence.

[Frozen public CLI acceptance](../outputs/universal-workflow/final-codex-smoke/r-3c242977/report.md) uses real default-PATH Codex, separate implementation/AC/review, 3/3 tests, exact change/before-source proof, COMPLETED and a dated closure. Production pre-push hook and Publisher successfully pushed only a disposable local bare repository. The repaired non-Git document draft received independent approval while its parent correctly stayed BLOCKED on publication. Research content and its no-external-search evidence are reported separately; missing method proof initially remained BLOCKED, and any evidence extension/resume is retained in that same report.

[Workflow review](../outputs/universal-workflow/workflow-review.md) and [integration review](../outputs/universal-workflow/integration-review.md) record independently reproduced and corrected P1/P2 findings. No unresolved P0/P1/P2 remained in their checked slices. The later legacy-budget, source-packet, exact-criterion and document fixes also passed 43 independently executed focused tests before the final suites. Legacy continuation now preserves spent attempts/stricter ceilings and original history. Packet review includes actual change hashes and captured before-files; extra uncontracted criteria cannot be silently ignored.

The research scenario subsequently completed through the existing public interface: an additional required structured probe verified real implementation receipts/events; one resume ran fresh AC/review and completed with no new implementation cycle. Contract/proposal/approval, source bytes and runtime hashes stayed unchanged; the task/stage counters remained one. [Recovery proof](../outputs/universal-workflow/final-codex-smoke/r-3c242977/research-recovery/summary.json) retains the initial blocked history and limits its attestation to captured model events, not provider startup/out-of-band network traffic.

That recovery exposed an owned-blocker projection defect. It was fixed with observed RED/GREEN regression and independent focused review. A [fresh public-CLI live test](../outputs/universal-workflow/final-codex-smoke/projection-de672c03/report.md) on the final runtime genuinely lacked Codex first, then restored it and completed through three real roles. Cycles were 0 → 0 → 1; both current worklog/meeting blockers are empty while opened/resolved events and the original blocked closure remain. Unrelated business blockers are not automatically resolved. This patch-specific proof supersedes the older reporting caveat without modifying historical test stores.

[Whole-candidate independent model acceptance/review](../outputs/universal-workflow/final-acceptance-20261112T003955-9af8cea5/report.md) found one P2 directory-packet defect and judged 11 criteria PASS, three PARTIAL. Directory discovery, standalone stop/cancellation and concrete mixed-stage mapping were subsequently fixed with actual regression evidence. The original model judgments are preserved; later supplements do not overwrite them.

[Actual mixed public-CLI acceptance](../outputs/universal-workflow/mixed-live/r-d26a137f/report.md) produced eight valid distinct Codex receipts. Both concrete stage gates approved (software 3/3 tests; document artifact), one implementation cycle each. Final parent acceptance remained BLOCKED because its supplied packet omitted stage-specific history needed to establish stage scope and order. That is a genuine evidence-selection gap. A prospective stage-history amendment and its own fresh checks are tracked in the final checkpoint; no retrospective history or semantic approval is invented.

[Adaptive native subagent policy](../outputs/universal-workflow/parallel-policy/report.md) was checked by a genuine Codex skill consumer and targeted local tests. Independence, dynamic dependency waves, available host slots, shared budgets and integration boundaries are explicit; unavailable parallel execution falls back to serial work. The framework adds no mandatory agent SDK, LangGraph or MCP dependency.

Current delivery limits: no live Claude/Copilot, actual VM/hypervisor/cloud account, remote CI, Linux execution or real Python 3.11 execution. Copy/import never revives old approval. Real user state was inspected for [migration preview](../outputs/universal-workflow/packaging/migration-preview.json), not migrated. Old installed runtimes may retain obsolete modules on reinstall; explicit upgrade remains separate. The 62-file drive-root cleanup incident below remains unresolved. These limits are not relabelled as passing optional checks.

The approved scope was clarified: live backend validation is Codex only because Claude/Copilot subscriptions are unavailable; those two receive protocol checks and a stated risk assessment, without installation or claims of live operation. No real hypervisor/VM or remote infrastructure action is authorized or claimed. No user runtime/configuration migration is authorized by the implementation plan.

## Evidence classes

- Executed local facts: real files, SQLite transactions, commands, process receipts, test counts and integrity checks.
- Independent judgments: actual separate Codex AC/review or source-packet consuming sessions, bound to their captured inputs.
- Protocol fixtures: deterministic subprocesses exercise parsing/permissions/receipts. They cannot provide semantic approval.
- Unavailable: actual Claude/Copilot subscriptions, live VM provisioning, remote trusted CI and final reviewed source where not yet run.

## Initial acceptance matrix — historical

| ID | Actual evidence available | Status and limits |
| --- | --- | --- |
| AC01: workflow without framework MCP | Retired-module negative test; direct Git/state/stdin tests; disposable software live fix/check/AC/review/gate/finish in codex-workflow-live | Captured runtime snapshot completed without the ASE host. Later source changed; final coherent runtime acceptance pending. Existing private user registration is deliberately not migrated. |
| AC02: backend execution | Actual installed Codex 0.167.0-alpha.4.1 implementation and two independent sessions; Runner/adapter protocol suites for all three backends | Codex snapshot evidence only. Claude/Copilot are explicitly protocol-only, never installed/live-tested here. Live resume/expanded behavior is not inferred from the one fix scenario. |
| AC03: strict software checks preserved | Real three-case unittest defect/fix/regression; fresh AC FAIL and P2 after regression; gate/profile/legacy regression suites | Deliberate defect rejected, old evidence rejected and positive tests captured. Final all-source regression/independent review pending; legacy frozen test drivers do not replace current v2 checks. |
| AC04: selective knowledge refresh | task4 real-file/dependency/CAS tests; scenario with identical workspace basenames and isolated source freshness | Local source/currentness behavior executed; declared dependency coverage is not complete semantic analysis or cloud synchronization. |
| AC05: exact proposal authorization | Task contract tests and executable task/proposal templates; demo without observed approval creates no deliverable | Existing scope approval reused through service APIs. Approval references are local accounting for observed authority, not authenticated signatures. Final adversarial review pending. |
| AC06: meeting gaps and partial work | Scenario validates attributed timestamped meeting suggestion/gap, refuses untracked gap, maps it to publication unknown, permits draft, saves actual artifact/blocked closure | Executed with structured fictional meeting data; no actual recording transcription. Missing publication decision remains unresolved and does not become authority. |
| AC07: bounded repair | Root twelve-cycle scenario spans four children, reopen and revised root; existing stage/diagnostic/stop/resume tests | Actual persisted counters tested without model stubs. One live small fix is not proof of every diagnostic behavior. |
| AC08: reconcile effects | Operation-ledger lost-ack/local controlled resource tests and current workflow action tests | Executable controlled local protocol only. No live VM/hypervisor/resource acceptance; never reported as actual infrastructure provisioning. |
| AC09: dated atomic finish/stop | Journal atomicity/idempotency/CAS tests; direct cross-process root lock; actual demo and live software closure | Real local transactions and blocked/awaiting closures executed. Hard-kill persistence guarantees remain limited to committed checkpoints. |
| AC10: timezone/report isolation | Pinned TZif tests, Europe/Bucharest journal suites, legacy daily/weekly attribution tests and actual local meeting report | DST/midnight are separately tested, not duplicated as string assertions. No inferred human/billable time and no external report sending. |
| AC11: migration/transfer | Registry/schema migration and export/import disposable tests, source-backed reports task1/task11 | History/UUID/copy-import behavior tested. Real user runtime migration, divergent merge and external transfer are not performed. |
| AC12: portable skills/opt-in | Eleven canonical packages; three disposable targets; TOML/isolation/preflight/backup tests; source-project scoped 29-file audit; real Codex skills consumer | File/protocol checks and consumer response pass with limits. Native automatic discovery across backends is not inferred. Earlier drive-root test incident remains unresolved pending manual cleanup; see below. |
| AC13: profiles/parent obligations | Software live evidence; profile gate tests; actual document artifact/check and blocked mixed-obligation scenario | Document/local facts executed, substantive independent document/research acceptance and real infrastructure acceptance remain separate. Mixed parent is not completed while obligations remain. |
| AC14: capability limits visible | Probe metadata and adapters, missing-CLI typed blocker, actual consumer recommends local Codex without MCP/other subscriptions | Actual CLI/version/permission combinations only. CLI startup warnings/network behavior and same-user enforcement limits remain explicit. |

## Captured Codex evidence

[Canonical-skill consumer report](../outputs/universal-workflow/skills-green/report.md) records one actual no-tools, read-only source-packet response. Packet SHA-256 is `dd5f7d769939b02592b27ad20a9f5188a815f8e72f20d63b10e5c6d666d5fd97`; session `01a1ccf3-4789-7a82-905b-b1f983cb48ae` used execution `e3a6cd8de71c4241a46b2f445f43823e`. It selected non-code profiles, a concise proposal, genuine material questions, limited existing authorization, persisted limits and a dated stop closure. Missing MCP and unavailable other backends did not become prerequisites. It prescribed behavior; it did not execute a VM, transcript, artifact rendering or database closure. Baseline and GREEN had different supplied reference coverage, so the comparison is not a causal error-rate estimate. Stderr startup warnings are retained; absence of model tool events does not establish absence of CLI startup network activity.

[Live software workflow report](../outputs/universal-workflow/codex-workflow-live/report.md) records a real addition defect repaired by Codex, three actual passing unittest cases, independent AC/review, APPROVED and atomic COMPLETED closure. Restoring subtraction rejected stale evidence; a fresh independent task then executed the failing tests, received AC FAIL and a P2 finding, and closed blocked. The happy path used sessions `01a1ccf9-e2fe-7302-a30b-9ff5d844d827`, `01a1ccf9-ffa5-7041-a268-ff8e7fad76d2`, `01a1ccfa-251f-7a33-95a6-0cef792ca807`. Full role receipts, request bindings and integrity checks are in that directory.

That process retained its imported runtime while gates.py/profiles.py changed concurrently. [Runtime consistency](../outputs/universal-workflow/codex-workflow-live/runtime-consistency.json) explicitly lists those changes and [runtime snapshot](../outputs/universal-workflow/codex-workflow-live/runtime-snapshot.json) preserves the imported source evidence. Reviewers noted Windows log newline/provenance ambiguity and absent baseline/diff in their supplied packet. Those limitations remain in the saved judgments; later fixes need fresh affected evidence. This report does not relabel the earlier snapshot as final HEAD acceptance.

## Executable local document example

From the source checkout:

```powershell
python examples/universal_workflow_demo.py
```

The default creates only a disposable source fixture, records scoped discovery/proposal, and closes AWAITING_APPROVAL with no guide. It does not invent approval. For an explicitly authorized disposable draft, supply the observed source of that authorization:

```powershell
python examples/universal_workflow_demo.py --approval-user-message-ref OBSERVED_USER_MESSAGE_REF
```

That invocation creates an actual provisional guide and artifact check, then closes BLOCKED with the unresolved destination and unexecuted independent review. It never publishes or marks the parent complete. Default fixture/state paths are deleted after printing; exported JSON is a demonstration record rather than a retained artifact store. Existing user workspaces require both `--workspace` (containing brief.md, no existing guide.md) and explicit external `--state-home` to retain evidence. Subsequent continuation uses the saved task/CLI rather than replacing it with a new example run.

Optional `--live-codex ABSOLUTE_EXECUTABLE` additionally requires `--state-home` and `--approval-user-message-ref`; it runs genuine local Runner AC/review with controlled stdin/read-only permissions, no fake verdicts or provider permission changes. It does not resolve publication authority or complete the parent. This optional example path was not independently live-run in these example tests; the separately captured Codex workflow above is the actual live evidence.

Seven tests in `tests/test_workflow_scenarios.py` exercise child-root twelve-cycle persistence, meeting-gap stage mapping, actual document closure, same-basename cache isolation, approval-free example behavior, approved-but-unverified example behavior template service validation and CLI refusal to start live review without observed scope approval (some are combined per scenario). They use real temporary files/SQLite and current services without semantic model fixtures. `templates/task-v2.json` and `templates/proposal.json` are editable worksheets; their service validation never grants approval. `templates/project-profile.json` leaves repositories, sources, backend targets and account boundaries unconfigured.

## Initial closeout notes — historical

Parent final integration suite, fresh built-package inspection, independent acceptance/code review and coherent final-source live smoke are pending as of this initial matrix. Remote CI is not claimed unless a real run is recorded. Local hashes/prompts/hooks cannot enforce publishing against another process with the same OS identity; trusted CI and server-side policy remain required.

An early installer test incorrectly treated Path.root as ProjectContext and wrote 62 files beneath the D: drive root. The bug has a dedicated exact-root regression and subsequent disposable tests pass, but automatic approval review blocked cleanup. [Exact incident inventory](../outputs/universal-workflow/accidental-test-output-files.json) and [Task10 report](../outputs/universal-workflow/task10-report.md) record the unresolved manual cleanup. Repo-only source asset regeneration was later explicitly authorized, preflighted, backed up and hash-audited; it is a different, bounded action. No actual user runtime/MCP registration migration or real Claude/Copilot setup is reported.
