# Proportionate solution design

Use before choosing how to implement a software or infrastructure change. Build the smallest
change that meets the ticket's acceptance criteria and fits the project's existing patterns, the
way a careful developer on this team would. Correct code with unrequested machinery is a failed
delivery: the team has to remove it. A workaround that avoids touching the right code is not
"small" either. Scale the steps below to the task: one line each for a trivial change. See the
[calibration examples](solution-calibration.md).

## 0. Understand the problem first; code comes after

The first deliverable is a short problem analysis, before any code:

- **What happens now**, reproduced or traced in the code: the actual flow and data.
- **Where the system already meets the condition**: the existing point that detects or handles
  it (the place where using a token fails, the status transition, the query that selects items).
- **The smallest hook point** for the change.
- **The ticket class**: code change, investigation/clarification, or both.
- **For a defect**, its scope: every entry point with the same root cause and any earlier
  compensations for it.

For a bug, first check that the behaviour is really a defect. When it is a question of
expectations (rows that differ by version, data the reporter misread), the deliverable is a
finding for the owner and, when needed, a draft question for the reporter. Compare the original
example or attachment with the actual behavior before deciding. When ticket text and a later
clarification differ, identify the approving source, the scope of this delivery and who owns the
remaining obligation. Write no code until the analysis exists and matches the authorized scope.

## 1. Understand the system before deciding

- **Business need.** Restate what the requester needs to happen, not only the wording.
- **Bug scenario.** Before editing, pin down the actor or caller, exact operation and trigger, the
  scope of the rule, what actually happens and what should happen. Check the original ticket,
  examples and approved clarifications. Two operations using the same integration can have
  different settings, permissions and events. If the expected result still depends on a product
  decision, present the smallest distinguishing example to the owner before implementing that
  behavior. A failing test, trace or concrete reproduction should cover the reported path.
  Record limitations when that evidence is unavailable.
- **Scoped system map** (C4, only the part the change touches): dependent apps/users;
  services/apps, how they exchange data and **who owns each contract**; components and the
  existing path for this kind of work; the files to change and their size. Use code, config,
  docs/ADRs, Git history and PR/issue discussion; mark what is unverified.
- **Reuse first.** Before adding any mechanism, search for the project's existing one
  (email/notification command, job, transaction pattern, validation, caching, auth check) and use
  it. Record what you searched and found.
- **Precedent.** Follow the team's established way unless evidence shows it no longer fits. An
  explicit example-search opt-out skips deliberate example search while preserving inspection of
  actual callers and dependencies. On a
  codebase new to you or the team, match the existing code style and change size over general best
  practice, and apply this guide strictly.
- **Code placement.** For nontrivial logic, inspect the existing owner of the rule and one nearby
  caller or maintained equivalent. Compare reusing the existing method, extending its owner and
  keeping the change local. Extend a class when the behavior belongs to its responsibility and its
  callers can share the same contract. Similar code alone is insufficient reason to create a shared
  helper or dependency. Follow applicable project conventions and nearby maintained code. Choose
  clear names and control flow the team can read without chasing unnecessary wrappers or branches.
  Keep a related refactor within the changed flow and check the affected callers.
- **Review evidence.** In the technical plan name the existing path, the chosen class or method,
  the relevant precedent and why the change belongs there. If the search finds no suitable
  precedent, record where you looked. Include the few unchanged source files decisive to the review
  as read-only discovery dependencies. Examples include the existing owner of the behavior, an
  affected caller, or the applicable project rule. Keep the mutation scope limited to the task.
  Include the relevant test locations in the edit scope when the task needs new regression proof.
  For a shared contract, name the callers and the behavior they rely on.
- **Root cause.** For wrong or missing data, trace caller -> boundary -> callee to the first place
  the value becomes wrong. Read the callee's implementation and its sibling paths over the same
  data; an existing handling of the same condition there (fallback to the primary store,
  consistency check) is the first candidate fix. Record what you searched in the callee.

## 2. Triage every risk; only required risks become code

For each risk scenario you can think of (race, transient failure, stale data, duplicates,
concurrent edits), classify it:

- **(a) Required** -> code and test it. A risk is required when:
  - the ticket or an acceptance criterion asks for it, or a reviewer demonstrates the concrete
    counterexample below; or
  - without it, the change itself would break the project's existing authorization, validation
    or error conventions on new paths, existing invariants and contracts, lose or corrupt data,
    or duplicate external or financial effects (with a concrete counterexample).
- **(b) Already handled** by existing behaviour (a one-time state transition, an existing
  transaction, a later re-check) -> note where.
- **(c) Unlikely or out of scope** -> do not code it; list it under "Not handled" in the PR with
  the reason ("not required by AC-x; no observed trigger").

"More robust" is not a requirement. A theoretical race is (c) unless the ticket or evidence makes
it real. A risk found later (review, lead pass, adequacy check) follows the same triage: without an
(a) counterexample it goes under "Not handled", not into code or tests.

**Hold edits that depend on an unresolved decision.** Follow the proposed failure path through
the caller response and any committed write or external effect. A failure after a write can be a
new regression even when the same pattern is used elsewhere. If the known contract settles the
failure behavior, use it. If the acceptable result after failure needs an owner decision, show
what is saved, what is missing and what a replay can do, then present conditional options.
Keep independently approved edits moving. An open owner question about the behavior is never
approval of a patch that already chooses it.

## 3. Hook into existing flows; mechanisms need a reason

Prefer the place where the system already encounters the condition. Add no new scheduled job,
trigger, pipeline, polling, or global/root UI component (app shell, root component, global
layout) unless the ticket explicitly requires it.
For proposed background work, separate the schedule from delivery guarantees. Check the actual
platform contract and deployment configuration for relevant replica count, retries and overlap.
If those facts are unavailable, state them as conditions on the recommendation. A time trigger
alone establishes no guarantee that a job will complete exactly once.

Locks (local or distributed), new or conditional transactions, atomic claims, retries, timeouts or
waits, queues, caches, polling, version/incident IDs, new columns/tables/migrations, and optional or nullable DI
dependencies are added only when:

- a category (a) risk needs them,
- a documented contract prescribes them for that flow (for example a published transient-error
  retry policy for idempotent calls; use the shared implementation), or
- the project already uses that exact mechanism for that exact flow.

This applies to recommended options and interim "quick fixes" as well as code.

Otherwise leave the risk documented and unhandled, or ask the owner. Do not wrap new code in
defensive re-checks of state the existing code already guarantees.

Whether an addition beyond the ticket is "really needed" is not your call. Keep it only as an
explicit question to the developer or lead, with its reason; never keep it silently.

## 4. Fix at the source, for every consumer

For a persisted key or wire-format correction, verify existing records and old callers as well
as new writes. For affected response fields, cover omission, null, false and zero where the
contract gives them distinct meanings.

- Before changing a service boundary format, check the current producer and consumer contract.
  Compare maintained neighbors using that contract when example search is allowed. Preserve
  established wire values, units, casing and absence semantics. If there is a material
  conflict, cite the evidence and ask the contract owner. Include the decisive contract or
  unchanged caller in review.
- When removing an integration, search for the service and provider names, registration symbols,
  configuration and environment keys, and endpoint aliases. Check code, settings for each
  environment, deployment files and docs. Remove obsolete references owned by the removal and
  explain any live shared references that remain. Give review the searched locations and results.
  Record key names and paths while keeping secret values out of evidence.

- Fix the component that produces the wrong value. Compare the real output at the boundary to the
  agreed contract and check callers, existing compensations and downstream effects. A compatible
  repair in approved scope can proceed at the source, including in shared code. Declare a shared
  change and its affected consumers. When the correction has broad downstream impact, consumers
  may rely on the bad output, or you cannot establish a safe boundary, show the owner what can
  change and ask them to decide how to proceed before editing the affected flow.
- Code in another repository (another team's service) is changed through its own managed task or
  child task, with its own baseline, gate, review and publication authorization. Prepare that
  change; the other team's code review is not an owner approval and does not hold your work.
  Their ownership alone does not justify a consumer-side workaround.
- A consumer-side workaround (mapper, fallback, re-computation, retry, wait, cache) needs a
  documented requirement or contract, or the owner's explicit approval when the source cannot
  be changed. Put temporary workarounds at the integration boundary and record the upstream
  reference and removal condition. Data the source accepted but does not yet return (replica
  lag, stale read) is a source fault unless the source's documented contract allows that lag.
- When valid and invalid responses overlap, try the same observed input under both
  interpretations. Show the different outcomes if the data cannot distinguish them. A
  plausible number is insufficient evidence for a workaround
  affecting a charge, permission, or persisted record. If the contract gives no reliable
  signal or authorized fallback, present the ambiguous example and choices, then wait for
  the owner's decision before implementing a rule that changes outcomes.
- Fix or remove existing compensations only when the approved repair needs that change. Record
  the remaining consumer impact for the owner.
- When the same fault of the ticket's defect exists at several entry points, fix every one in this change. Use a shared
  handler/mapping when one already exists for that purpose; otherwise apply the same small fix at
  each entry point and offer the shared version as a separate ticket. These hunks belong to the
  defect, not to "unrelated changes".

## 5. Choose a proportionate form

State the chosen form, why not simpler and why not more complex.

| Situation | Proportionate form | Wrong in either direction |
|---|---|---|
| A few lines in a large file | Edit those lines; tidy only the touched code when the risk is low or medium | Restructuring the file "while here" |
| Substantial new logic for a large file | New classes/files (folder, or a project in the solution when large), called from the existing code | Growing the large file further |
| The task changes most of a component's logic | Restructure that part, tests first, risk stated | Patching around it |
| The ticket says how to do it ("do not refactor") | Follow it; note any risk | Overriding the instruction |
| Data available through one simple join/navigation | Read it there | A duplicate column |
| Data required by the ticket and not stored anywhere | A new column, approved by the owner (see 6) | Heuristics, log parsing, heavy procedures |
| A new entity with its own lifecycle | A new table (infrastructure, see 6) | Overloading unrelated tables |
| A monolith needs a capability | A module/project in the solution, reusing existing services | A new service, function or queue |
| Several services need a capability none provides | A dedicated component/service (infrastructure, see 6) | Copying it into each service |
| A policy value changes | Change the value and its test | Config systems, date switches, flags |

## 6. Who decides

- **You decide** (justify in the proposal): code in any number of services in this workspace, new
  files/folders/projects inside a solution, refactoring whose risk you judge low or medium in the
  task's code or the flows it tests, additions that follow a pattern the service already uses,
  root-cause fixes in shared code, `kind: "schema"` changes - an index or constraint migration a
  requirement needs. Identify affected consumers of shared code and preserve behavior their
  contracts require. Routine decisions remain within the approved change.
- **In the implementation plan for the owner** (task-orchestrator, Plan and approval): HIGH -
  every item in the list below, including a new table or column and replacing a technology;
  MEDIUM - a new direct third-party dependency or direct dependency on a shared library in the
  affected service, a material architecture change (for example the first Unit of Work), or a
  material change to shared behavior or a cross-service contract. An externally consumed message
  type on an existing queue is a contract decision. LOW is risky or
  complex refactoring required by the task. A pattern used only in a sibling service can be a
  precedent and still be a material change in the target. Give the reason, the simplest existing
  alternative, the concrete gap it leaves, the affected consumers and your recommendation.
  A general go-ahead covers routine choices. For HIGH or MEDIUM changes, present the rationale
  before editing. The owner's reply to a plan they saw can cover the named change. Prior
  authorization must name the change or a bounded delegated choice in the owner's own words.
  Recording a self-authored plan under general pre-approval does not supply that authority.
- **Incidental shared-code changes** - hardening, claim logic or other edits to job runners,
  schedulers, publishers, DB context, DI setup or middleware that the root cause does not need -
  stay out of a feature ticket. Paths listed in `.devframework/protected-paths.txt` additionally
  block the gate until `ase authorize-protected` records the owner's permission; ask for it in the
  approval format below and build no workaround meanwhile.
- **The owner approves before you build** any added or removed infrastructure (`kind:
  "infrastructure"`):
  - a service or application;
  - a column, and replacing a technology (message broker, database engine);
  - a database or a table;
  - an Azure Function or other serverless unit;
  - any timer, scheduler or background job, including an in-process hosted service;
  - a queue, topic or subscription;
  - an external platform or provider;
  - a new cloud resource.

  DB functions, procedures and triggers count as infrastructure unless the project already uses
  them for that kind of logic. Declare each with basis `ASSUMPTION` on a blocking unknown; only the
  owner's answer resolves it, never a retrievable fact (`DECISION` once answered). A source ref
  counts only when the owner's own text names that infrastructure and covers the intended action.
  The CLI checks the binding and reviewers check what the owner actually authorized.
  Under pre-approval the owner can authorize preparing infrastructure files in the pull request
  and explicitly allow a dev environment for application. Record the message as the
  `resolution`, marked pre-approved, with the environment and authorized action.
- **The approval request** (in the implementation plan it is the HIGH item, not a separate message) gives:
  - the business need and the evidence;
  - the options from simplest up (for example lazy expiry on access, an existing job, sending from
    each service), and why each is or is not enough;
  - the recommendation and its impact on other services, operations and cost;
  - one decision to make.
- **Do not ask** about what code, docs or history answer, internal design choices, which team
  reviews code, or release timing for a change you can prepare now. When the ticket implies a date
  ("from next month"), state the date you read and proceed; put it in the release note instead of
  holding the change.

## 7. Developer ownership and human-led mode

- Give the developer a short walkthrough of the original behavior, why this location is the
  right hook, the observed result of the relevant check and any material remaining obligation.
  The PR description should let a teammate understand the decision and its verification.
- **Human-led mode** is on only when the task explicitly asks for it, or when AGENTS.md contains the
  exact line `DevelopmentFramework mode: human-led` (then for the whole project). Nothing else
  enables it. In this mode the developer writes the production code. DF:
  - gives the analysis, hook point, step plan and test proposals;
  - writes tests only on request;
  - independently reviews the developer's diff and polishes text.

  The task runs under the research/review profile, without an implementation gate.

## 8. One ticket, proportionate tests, plain PR

- One ticket per PR. A defect outside the ticket found on the way is reported in one line ("Found
  separate defect X (evidence); outside this ticket, left untouched. Open a separate task?"), with no
  fix, branch or PR, current or separate ([out-of-scope discovery](../SKILL.md#out-of-scope-discovery)).
- Tests cover the acceptance criteria and category (a) risks, written, not promised; prove the
  original failure through the real consumer path where practical. When no test project covers the
  changed layer, add the smallest one to the solution (your call) instead of leaving a test plan.
  No tests for invented races, and no test volume several times larger than the change.
- Required side effects (messages, payments) avoid duplicates on retry and keep related writes in
  one existing transaction where the context allows (an instance of 2(a)). Prefer a reliable signal
  over a heuristic.
- PR description: short and plain - what the ticket asked, what changed, what was deliberately not
  handled, how it was verified. No showcase of mechanisms.
- For a nonobvious public signature change, add one sentence on why the caller needs the new
  contract and check affected callers. Put the reason in the proposal summary so it reaches
  pre-publication review, and retain it in the PR description. Example reason given to reviewers
  would be that the caller needs the assigned identifier without a second read.

Independent review checks proportionality as a blocking criterion: unrequired mechanisms,
incidental shared-code edits, bundled unrelated changes, avoidable workarounds and unapproved
infrastructure are findings. A diff far larger than the minimal sketch triggers that scrutiny; the
finding names the specific unrequired hunks.
