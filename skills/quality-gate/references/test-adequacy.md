# Test adequacy before software completion

Use for software and software stages of mixed tasks. A large passing test count establishes executed examples; judge whether those examples can catch plausible violations of the original requirements.

New software tasks created through `ase task create` enable `test_adequacy: {"version":1,"required":true}`; quick-lane tasks cannot carry it. Programmatic task creators must carry that explicit policy. Preserve saved contracts and label old unconfigured adequacy honestly. A request to exclude tests from a commit/PR concerns publication; it does not remove local testing or adequacy obligations.

## Before implementation

Inspect current code, relevant existing tests, project examples and actual available test tools. Derive a concise scenario plan from original acceptance and triaged risks ([risk triage](../../task-orchestrator/references/solution-design.md#2-triage-every-risk-only-required-risks-become-code)). Only a category (a) risk - required by the ticket or an acceptance criterion, or with a concrete counterexample (yours or a reviewer's) that the change breaks existing auth, invariants or data or duplicates an external or financial effect - becomes a required scenario, code and tests; a risk existing behaviour already handles, or an unlikely/out-of-scope one, goes to `not_applicable` with its reason and to the PR's "Not handled" list. Every mandatory software criterion/stage needs a required scenario, except on the quick lane (`ase task quick`), which carries no `test_adequacy` and no separate assessment. Save it through `ase adequacy plan TASK --file plan.json --expected-revision N`.

Each scenario has `id`, `acceptance_ids`, `risk`, `boundary`, `expected`, `evidence_ids`, `required`. The plan also contains `not_applicable` rows with `risk` and a source-supported `reason`. Choose meaningful scenarios rather than an exhaustive checklist. Keep ordinary technical choices autonomous.

When a required risk applies, choose evidence that can catch it. Examples of relevant boundaries (they are not a checklist of risks to add):

| Requirement/risk | Discriminating evidence |
| --- | --- |
| Two workers must not process the same item | Real concurrent workers/connections synchronized at the competing operation; one claim/effect, including restart. Sequential calls through one stub do not exercise the race. |
| State/permissions can change during I/O | Pause at the relevant boundary, change the state, resume and observe the required externally visible result. Initially-private fixtures alone do not cover Public to Private during a read. |
| Select latest version | Equal ordering keys, duplicates, missing values and insertion orders when relevant. Distinct timestamps alone do not cover ties. |
| HTTP validation/status | Request through the actual HTTP boundary and inspect response/status/persistence. A handler exception alone does not establish what a caller receives. |
| SQL claim/transaction semantics | Actual supported database/connection behavior for the relevant transaction; an in-memory mock alone cannot establish database concurrency. |
| Retry/crash/external effect | Lost acknowledgement, known/unknown application, deduplication and recovery of the same operation. A missing observation is not nonapplication. |

Record only applicable risks. A local file operation does not need an invented HTTP test. If the approved environment cannot supply a required boundary, identify exactly which result remains unverified and seek a useful authorized alternative before asking the owner.

When a defect outside the task blocks a scenario, judge the criterion's goal, not its literal form, only for a denial or other negative goal: a request refused deliberately by a working control (4xx authentication or authorization, no data returned) where 403 was written still shows access denied; a 5xx, timeout, crash or empty 200 does not. Goal met and the task's own changed code path exercised by executed evidence (for example a test with a directly built or stubbed principal): PASS with a note naming the defect. Goal met without that evidence: UNVERIFIED, a coverage gap to close with that test. Goal not shown, a positive criterion (never met by a 401), or a literal form that is itself the requirement (for example a documented status-code contract): BLOCKED and an owner question. Never fix the foreign defect to obtain the expected form ([out-of-scope discovery](../../task-orchestrator/SKILL.md#out-of-scope-discovery)).

## Independent assessment

After actual checks, run the configured independent adequacy session in addition to ordinary AC and substantive review. It uses a separate captured execution/provider session and examines original requirements, plan, actual test source/assertions, executed logs, relevant production paths and current evidence. Implementation summaries and test names alone are locators.

When `handoff.purpose` is `test_adequacy`, return the configured adequacy schema. Evaluate each planned scenario and add relevant omitted scenarios independently; an omitted risk is required only with a category (a) counterexample, otherwise it goes to `not_applicable`/"Not handled". For every PASS supply current check IDs, test source, test IDs and the assertion that discriminates the failure. Report missing required boundary/source evidence as UNVERIFIED/BLOCKED; confirmed violated behavior is FAIL. A boolean adequate, a large count, source text containing a test name or a mocked success cannot substitute for actual proof. Do not claim exhaustive bug absence.

Find current test bodies in `handoff.test_source_candidates` and the actual packet. Supply their exact relative packet paths as `test_source`. A private script under `_ase_check_inputs/` is admissible only for its declared check ID. Repository candidates are a shortlist. Use another supplied repository body when it contains the relevant assertion exercised by that check. Cite logs and summaries for execution, and source bodies for assertions. Diffs and before versions are historical or partial context. For a behavioral scenario, identify the test or probe that exercised it. A build by itself establishes only that the build ran.

The runtime binds the assessment to the exact plan, source, contract, proposal, checks and relevant stage/context. Missing, insufficient, reused or stale assessment blocks configured completion. A passing source assessment does not establish publication or future deployment results.

The original lead handles current coverage gaps/findings without waiting for the owner to say "check edge cases". Confirm findings, repair in scope, execute affected tests and obtain fresh independent judgments. Preserve the existing progress-sensitive stop/reassessment rules; do not loop until another model says PASS. Ask only for material remaining owner decisions, unavailable required access or scope changes, with a short checked-evidence hypothesis and recommendation.
