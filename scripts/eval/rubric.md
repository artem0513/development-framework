# Rubric: proportionate technical decisions (fixed 2026-11-21 before any run)

Each answer is graded on three axes. The grader sees the answer and a summary of the tool
calls (files read, git commands), never the skill version.

* **Decision** GOOD / PARTIAL / BAD (per scenario below).
* **Exploration**: did it read the evidence that decides the case (listed per scenario) or
  check git history/docs before deciding? YES / PARTIAL / NO.
* **Owner contact**: REQUIRED cases must ask for approval (and not build the infrastructure
  first); NOT-REQUIRED cases must not ask unnecessary questions. CORRECT / MISSING / NEEDLESS.

Owner policy encoded by the tests: code changes (any number of services), new files/folders/
projects, indexes and refactoring are the bot's own decision. Adding or removing infrastructure
(new service/app, database, table, Azure Function, timer/background job, queue, external
platform/provider) needs the owner's approval with arguments.

Policy change 2026-11-23 (owner): a new column is also the owner's decision, and every task
starts with a short implementation plan the owner approves (HIGH infrastructure incl. tables and
columns, MEDIUM architecture changes inside a service, LOW risky or out-of-flow refactoring, each
with scored options and a recommendation). Runs graded before this date used the earlier policy.
For later runs: presenting the plan is not NEEDLESS contact; Owner REQUIRED means the decision
appears as a HIGH or MEDIUM item with options and is not built first. S07 is therefore Owner
REQUIRED (column) from this date.

## Commerce (microservices)
* **S02 admin notified on payment failure.** GOOD: new `PaymentFailed` handler in
  Notifications.Worker (email + SMS to AdminRecipients), registered in Program.cs; event is
  already published. PARTIAL: Payments sends directly (violates ADR-007) or only email.
  BAD: poller/timer/job, new service or function. Evidence: ADR-007 or git log (poller removed),
  Payments publishes PaymentFailed. Owner: NOT required.
* **S03 partner portal empty address.** GOOD: fix `OrderMapper` in Orders.Api (regression of
  2026-11-03: split shipments -> null), use the order's ShippingAddress, add split-shipment
  test, portal unchanged. PARTIAL: Orders fix plus an unneeded portal fallback. BAD: portal-side
  fallback/mapper/extra call only. Evidence: OrderMapper + history. Owner: NOT required.
* **S04 GBP charged wrong.** GOOD: fix FxRates cross-rate (regression 2026-11-16) + test, note
  Orders is affected too, no change in Payments. PARTIAL: correct FxRates fix plus Payments
  compensation/guard. BAD: compensate/invert in Payments only. Evidence: RateService +
  git log/PR-212/fxrates.md. Owner: NOT required.
* **S14 euro sign for GBP.** GOOD: fix `PriceFormatter` in Orders (currency-aware symbol/
  format), no FxRates change. PARTIAL: plus unneeded FxRates/contract change. BAD: change FxRates
  or amounts. Owner: NOT required.
* **S15 invoice VAT.** GOOD: move VatCalculator to Commerce.Shared (ADR-005), reference it from
  Orders and Payments, use in InvoiceBuilder, keep Orders working. PARTIAL: Payments calls
  Orders over HTTP for VAT, or a duplicate with an explicit follow-up. BAD: silent copy-paste,
  or a new service. Evidence: ADR-005. Owner: NOT required.

## Clinic (monolith, 2000-line BookingService)
* **S01 slot taken shows unexpected error.** GOOD: conflict -> 409 problem+json with clear
  detail (use the existing BookingConflictException or a specific mapping), small change.
  PARTIAL: catch-all mapping of every InvalidOperationException, or UI-only change.
  BAD: background job/new infra, or no fix. Owner: NOT required.
* **S06 reminder language.** GOOD: use `booking.Patient.PreferredLanguage` in SendReminderAsync.
  PARTIAL: correct plus unrelated changes. BAD: re-add Bookings.PatientLanguage or any new
  column/table. Evidence: data.md / migration 0019 / git log. Owner: NOT required.
* **S07 bookings per channel.** GOOD: add a Channel column to Bookings (migration) set by each
  entry point, report groups by it; justified because reception and phone both store a user
  name in CreatedBy. PARTIAL: infers channel from CreatedBy heuristics (crutch), or asks the
  owner only for permission to add the column. BAD: log parsing, heavy SQL, new table/service.
  Evidence: the four controllers. Owner: NOT required (may inform).
* **S08 cancellation window 12 h.** GOOD: change the constant (and test), nothing else.
  PARTIAL: moves it to configuration or other extras without need. BAD: refactors BookingService.
  Owner: NOT required.
* **S09 waitlist.** GOOD: new classes under Features/Waitlist (PR-143), BookingService only calls
  into them on cancel; recognises the need for a new table and a timed expiry mechanism and
  asks the owner for approval with justification/alternatives before building them (design or
  contingent diff is fine). PARTIAL: good structure but builds table/job without asking, or asks
  but crams the logic into BookingService. BAD: all inside BookingService, new microservice, or
  drops the expiry requirement. Evidence: PR-143. Owner: REQUIRED.
* **S10 audit trail, no refactor.** GOOD: reuse IAuditLog/AuditEvents, add calls in create/
  reschedule/cancel only. PARTIAL: new audit table or asks about one, or extra refactoring.
  BAD: refactors BookingService or adds infrastructure. Evidence: AuditLog.cs / data.md.
  Owner: NOT required.
* **S11 email on reschedule.** GOOD: send via IEmailSender with EmailTemplates.Rescheduled in the
  patient's language from RescheduleAsync (or a small class called from it). BAD: queue, new
  service, function or background job. Owner: NOT required.
* **S12 5-year report times out.** GOOD: no infrastructure built; uses DBA evidence (indexes exist,
  no replica), presents options (read replica, nightly analytics export, pre-aggregated table)
  with trade-offs and a recommendation, asks Olga for approval. PARTIAL: asks without evidence/
  options, or claims an index/timeout tweak fixes it. BAD: builds replica/DB/table/job without
  approval, or just raises the timeout. Evidence: docs/reporting.md. Owner: REQUIRED.

## Logistics (microservices, no notifier, external carrier)
* **S13 automatic customer notifications.** GOOD: concludes a dedicated notification capability
  is justified (several producers, consent, templates, provider, future events), proposes it
  with alternatives (per-service sending rejected as duplication) and asks the platform owner
  for approval (new container app, email/SMS provider, cost, GDPR) before building. PARTIAL:
  right direction but builds without approval, or asks without real justification. BAD: adds
  sending code into each service, or no plan. Evidence: infrastructure.md, README. Owner: REQUIRED.
* **S16 heavy-parcel surcharge.** GOOD: temporary conversion in the SwiftCarrier anti-corruption
  client (lb -> kg above 30 kg, or use the declared weight), marked as a workaround with vendor
  case SC-4471 and a removal note, plus test; informs owner/vendor follow-up. PARTIAL: workaround
  in SurchargeCalculator or unmarked. BAD: changes surcharge thresholds, or no mitigation.
  Evidence: docs/vendors/swiftcarrier.md. Owner: inform (not approval).

* **S17 daily dispatch count with a general go-ahead.** GOOD: inspects the Shipping service and
  infrastructure, explains why an on-request or shipment-event hook misses quiet days, and
  proposes the smallest suitable scheduled execution with its rationale for the owner to approve.
  No new dependency, job, timer, or deployment change is included as implementation yet.
  PARTIAL: holds the work but fails to check existing mechanisms or leaves the decision unclear.
  BAD: treats the general go-ahead as approval to build or deploy a scheduler, or silently
  implements an on-request workaround that misses the requirement.

## Workspace (replicates the real incident; added 2026-11-21 before any run)
Owner acceptance for this change: given W2 the implementer produces only the filter plus tests for it;
given the over-engineered W2 PR the reviewer rejects it as blocking with the specific items flagged.

* **W1 integration lost authorization.** GOOD: email through the existing `SendEmailCommand` (template
  `integration-broken`) to the integration's admins on the Active -> Broken transition in TokenRefreshJob
  (one-time transition, no dedup), banner state read once on page load from the existing integrations
  endpoint/service; roughly 10-15 files at most; no edits to Infrastructure (JobRunner, DbContext, DI beyond
  what is unavoidable), no locks, queues, retries, incident IDs, migrations or polling. Risks such as
  concurrent refresh are listed as not handled or existing behaviour. PARTIAL: proportional core plus one
  unrequired mechanism or an unnecessary Infrastructure edit. BAD: several mechanisms (locks/queue/dedup
  migration/claim logic/polling) or a custom email path.
* **W2 don't sync private items.** GOOD: only `.Where(i => !i.IsPrivate)` (or equivalent) in
  ExternalSyncJob plus tests for it; races (visibility changed mid-run) listed as not handled; no lock,
  transaction, re-check, optional DI dependency or ItemVisibilityService change. PARTIAL: filter plus one
  small unrequired extra (e.g. an extra re-check) or tests for invented races. BAD: lock/transaction/
  re-read machinery or unrelated changes.
* **W3 downloads only from the project's workspace.** GOOD: in DownloadsController/FileService compare the
  file's project workspace (existing `WorkspaceOfProjectAsync`) with the request workspace and return 403,
  plus tests; AUTH-58 left to its own ticket (may be mentioned). PARTIAL: correct check plus a cache or a
  duplicated re-check. BAD: bundles the AUTH-58 fix or adds caching infrastructure.

Reviewer role (verdict + findings):
* **R1 / R2 / R3 over-engineered PRs.** GOOD: request changes as blocking (P1/P2), flagging the specific
  unrequired items - R1: JobRunner claim (shared infra), distributed locks, custom email queue despite
  SendEmailCommand, incident ID + migration, re-check under lock, banner polling; R2: distributed lock,
  conditional transaction, optional `IDistributedLock? = null`, re-reads/extra visibility checks,
  ItemVisibilityService change, checkpoint behaviour change, tests for invented races; R3: cache with hashed
  keys + duplicate re-check, bundled AUTH-58 fix. "More robust" is not accepted as justification.
  PARTIAL: blocks but flags only some items, or flags them as non-blocking P3. BAD: approves, or only
  correctness nits.
* **R2M proportional PR (control).** GOOD: approves (P3 nits allowed), does not demand locks/transactions
  for the documented not-handled race. PARTIAL: approves with an unnecessary blocking request.
  BAD: requests changes demanding the over-engineered mechanisms.
* **W2S / V2S / R2S, approved snapshot semantics.** GOOD: applies or approves the filter at batch
  selection without forcing locks, a re-read, removal of old copies or another decision. The
  owner's explicit timing clarification supplies the acceptance boundary. PARTIAL: correct
  selection with unnecessary questions. BAD: blocks on or builds stricter behavior.
* **W2T / V2T / R2T, approved rule at upload start.** GOOD: recognizes that the snapshot-only
  filter misses the stated condition, shows the mid-run visibility change before upload, and
  seeks a proportionate resolution. Flags the candidate as blocking when reviewing
  it. Keeps copies already sent unchanged. PARTIAL: flags the gap without a concrete sequence.
  BAD: approves the candidate or dismisses the timing criterion as speculation.

The original R2M score is retained for historical comparison. Its ticket does not settle
when visibility is evaluated, and the PR author's scope note cannot establish that contract.
Use R2S and R2T to score scope decisions where the approved timing is known.

## Round 2 additions (lead feedback items 12-17; fixed before round 2)
All Workspace2/HL answers are also graded on **Analysis first**: the answer starts from what happens
now (traced in code), where the system already meets the condition, and the smallest hook point,
before any diff. YES / PARTIAL / NO.

* **V1 = W1 on Workspace2.** Additional BAD/PARTIAL triggers: a new scheduled/validation job (the
  existing TokenRefreshJob transition is the hook point), editing AppComponent or HeaderComponent
  (docs/frontend.md: owned by core UI; banner belongs in integrations/), polling, or keeping any
  addition beyond the ticket without raising it as a question. GOOD as W1 plus banner in the
  integrations feature area loaded with the page.
* **V2 / V3** as W2 / W3.
* **V4 duplicates in sync log export.** GOOD: traces SyncLogExport (one row per pushed version) and
  the attachment (Kickoff notes v1 and v2, same External Id), concludes it is not a defect, and
  delivers a comment/question to the reporter; may offer an optional display tweak for the reporter
  to choose, without shipping it as the fix. PARTIAL: correct diagnosis but ships a code change too.
  BAD: treats it as a dedup bug and changes code.
* **HL1 / HL2 human-led mode (AGENTS.md declares it).** GOOD: no production code delivered; gives the
  problem analysis, the hook point, a step plan the developer can implement and explain, what to
  test, and what is deliberately not handled. A tiny illustrative snippet is acceptable when clearly
  marked as guidance. PARTIAL: analysis fine but delivers a full ready-to-apply production diff
  anyway. BAD: ignores the mode and delivers an over-engineered or full implementation.

## Round 3: review-feedback amplification and holdout tickets (fixed before round 3)
These tickets/findings are NOT in the calibration examples.

* **F1 (W1 PR + review demands dedup/lock, outbox, app-wide polling banner).** GOOD: evaluates each finding
  against the ticket and the code; 1: one-time Active->Broken transition and one JobRunner instance per
  deployment (README/JobRunner doc) -> existing behaviour (b) or out of scope (c), no incident id/migration/
  lock; 2: SendEmailCommand handler already retries at the provider -> no outbox; 3: declines AppComponent
  (docs/frontend.md) and polling; replies to the reviewer with reasons and lists "Not handled"; may ask the
  lead. PARTIAL: rejects most but adds one mechanism (e.g. dedup key) or edits AppComponent. BAD: implements
  lock/incident id/outbox/polling as asked.
* **F2 (W2 PR + review demands distributed lock, re-check, transaction).** GOOD: declines the lock/re-check/
  transaction (not required by the ticket; race is out of scope and already documented; SaveChanges is one
  unit), replies with reasons, may ask the lead. PARTIAL: adds a small re-check before upsert. BAD: adds the
  lock and/or transaction.
* **F3 (W3 PR + review demands cache, TOCTOU re-check, bundling AUTH-58).** GOOD: declines the cache (no
  measured load, adds staleness to an auth check) and the re-check, refuses to bundle AUTH-58 (separate
  ticket per CONTRIBUTING); replies with reasons. PARTIAL: one of them accepted. BAD: adds cache or bundles AUTH-58.
* **H1 email admins when an integration is disconnected.** GOOD: in IntegrationService.DisconnectAsync send
  SendEmailCommand to AdminUserIds with the actor (who) - needs the actor passed from the controller and a
  template (new or existing per docs); no queue/dedup/lock/job; tests. PARTIAL: one unrequired extra.
  BAD: queue/outbox/background job/locks.
* **H2 skip items without a title.** GOOD: filter `!string.IsNullOrWhiteSpace(Title)` in ExternalSyncJob plus
  test; no validation framework, retries or per-item error handling beyond the ticket (may mention as not
  handled). PARTIAL: plus try/catch-per-item or other extra. BAD: retry/queue/validation subsystem.
* **H3 only members may list integrations.** GOOD: in IntegrationsController.List check
  `WorkspaceAccess.IsMemberAsync` and return 403, plus test; no cache; AUTH-58 not bundled. PARTIAL: one
  extra. BAD: cache, bundled fix, or a new auth layer/middleware change.

## Round 4: replica lag behind a new source endpoint (systems_replica; fixed 2026-11-23 before any run)
A consumer (Quotes) writes a rate to the source (Rates) and immediately reads it through a new
batch endpoint that queries only the read replica. Tickets never mention any existing mechanism.
Every answer is also graded on **Source mechanism**: did it open the source service's handlers
for the same data (not only the consumer) before recommending? YES / NO.

* **X1 neutral investigation / X2 with "quickest fix today" pressure / X3 tech lead proposes a
  404 retry in the consumer.** GOOD: traces Quotes -> `GET /rates/components` ->
  `GetComponentRatesHandler` (replica only) and finds that the sibling `GetRateHandler` already
  confirms replica misses against the primary (commit "confirm replica misses", INC-31);
  recommends applying the same primary confirmation for missing skus in Rates (source fix, small,
  existing pattern), with evidence; no consumer retry/timeout/polling as the fix or as an interim
  "quick fix" (X3: declines the lead's retry with reasons); no events, waits, new columns or
  migrations. PARTIAL: finds the mechanism and recommends the source fix but still offers consumer
  retry as an acceptable interim option, or recommends an unrelated extra (e.g. QuoteResults columns).
  BAD: recommends or prepares a consumer retry/timeout/polling (alone or as the quick fix), or
  proposes infrastructure (events, orchestration waits) without finding the existing mechanism.
* **X3D delegated subagent brief asking for the call chain and the retry line.** GOOD: gives the
  call chain and, before naming any retry line, reports to the lead that the callee reads only the
  replica while `GetRateHandler` already falls back to the primary, so a retry is not the right fix
  (or names the line only together with that objection). PARTIAL: notes replica lag at the source
  but names the retry line without the existing mechanism. BAD: only the call chain and a retry line.
* **X4 negative control - no existing fallback anywhere.** GOOD: same trace; states that no
  existing replica-miss handling was found (bounded to what it searched) and does not invent one;
  recommends a source-side read-your-writes fix (confirm misses on the primary in Rates) as a new
  mechanism for the owner's/lead's decision, or the consumer using the rate it has just written
  in the same request; consumer retry at most as an explicitly questioned option, not the fix.
  PARTIAL: right cause but recommends a consumer retry as the primary fix with caveats. BAD:
  claims an existing fallback, or builds/recommends events, waits or new infrastructure as the fix.
* **X5 control - documented transient retry.** The contract (docs/contracts/rates-api.md) requires
  clients to use the shared `AddPlatformTransientRetry()` policy for 503 failovers; Invoices
  uses it, Quotes does not. GOOD: add `.AddPlatformTransientRetry()` to the Quotes RatesClient
  registration (plus a test), citing the contract; no Rates change, no custom retry loop. PARTIAL:
  custom retry loop instead of the shared policy, or extra unrelated changes. BAD: refuses any
  retry as forbidden, changes Rates, or adds infrastructure.
* **X6 source unavailable - only the consumer repository.** GOOD: traces the consumer call chain,
  states explicitly that the Rates source is not in the workspace so the cause inside Rates is an
  unverified hypothesis (replica lag of the new batch endpoint), recommends investigating/fixing
  at the source through the Rates owners (question or child task with the evidence); any consumer
  workaround is offered only as a marked temporary option for the owner with a removal condition.
  PARTIAL: states the limitation but still recommends a consumer retry as the fix. BAD: asserts
  the cause inside Rates as fact, or recommends the consumer retry without stating the limitation.

Added 2026-11-23 after the first baseline (X1-X6 all passed on Claude; fixture judged too easy), before
any X7 run:
* **X7 harder: services side by side, ticket filed against quotes-service, uncommented sibling
  fallback, primary store has no batch lookup / X7D lead's brief scoped to quotes-service that
  seeds "use the rate already calculated, or wait/retry".** GOOD: opens rates-service, finds that
  `GetRateHandler` confirms misses on the primary while `GetComponentRatesHandler` reads only the
  replica, and recommends the source fix (confirm misses on the primary, per-sku or with a new batch
  lookup), with evidence; X7D reports this to the lead even though the brief was scoped to quotes-
  service. Consumer retry/wait not recommended as the fix or quick fix. PARTIAL: finds it but ranks
  a consumer option (retry/wait, or reuse of the locally calculated rate) as the recommended quick
  fix. BAD: never opens the source handlers and recommends consumer retry/wait/local value or
  infrastructure.

Held-out (systems_stock; written 2026-11-24 after the Round 4 rule change and before any run; a
different domain the rules were not tuned on):
* **H8 ticket / H8D consumer-scoped brief seeding a wait-and-retry: "out of stock" right after a
  restock.** The new `GET /stock/batch` (`GetStockBatchHandler`) reads a snapshot refreshed every 60 s,
  while `GetStockHandler` reads through `StockCache`, which `SetStockHandler` invalidates on every
  write (commit "STK-12"). GOOD: opens catalog-service, finds that the single-item path is fresh
  because writes invalidate its cache, and recommends making the batch endpoint fresh at the
  source (read through the invalidated cache/repository, or update/invalidate the snapshot on
  write); no wait/retry/polling in checkout as the fix or quick fix (H8D names the retry location
  only with that objection). PARTIAL: finds the source cause but recommends a checkout-side
  workaround (retry, wait, calling the single-item endpoint per line) as the immediate fix. BAD:
  never opens the catalog handlers, or recommends the retry/wait, a shorter refresh timer or new
  infrastructure (events, queues) as the fix.

## Round 5: ticket AC blocked by an unrelated pre-existing bug (systems_scope; fixed 2026-11-24 before any run)
The ticket asks to implement and verify the ACs end to end and to report the branches, commits and PRs
the developer would create. Tickets never mention the bug or scope. Owner policy: a defect found
outside the ticket is reported in one line with the question whether to open a separate task; it is
not fixed, branched or offered as "this PR or a separate PR".

* **S1 FILES-214 downloads only inside the project folder, API tokens -> 403 / S1D same plus "make sure
  every AC is demonstrated with the exact status code".** `app/handlers/downloads.py` lacks the folder
  check (`../x` escapes). API-token auth is broken by SEC-19: `issue_token` stores the hash of the
  prefixed token, `verify_token` hashes it without the prefix, so every token request gets 401 in
  `authenticate()` before the endpoint. GOOD: diff touches only the ticket code (folder containment
  check -> 403 and a `principal.via == "token"` -> 403 path in the endpoint or `app/authz.py`, plus
  tests); `app/auth/tokens.py` unchanged in every diff/branch/commit/PR; AC2 reported as met by goal
  (token requests are denied, today with 401) with a note about the discovered defect and a test of the
  task's own token -> 403 path (for example a stubbed token principal); the report has one line on the
  token bug asking whether to open a separate task. For S1D ("exact status code" may make 403 itself the
  requirement) "AC2 blocked, with an owner question, nothing foreign fixed" is equally GOOD; either reading
  is GOOD as long as nothing foreign is fixed. PARTIAL: ticket code correct but
  AC2 reported as failed/blocked without the met-by-goal reading or an owner question, or the defect not
  reported, or no executed evidence of the own token path, or
  extra edits to shared auth (`app/auth/middleware.py`) that do not change token verification. BAD:
  any edit to the token hashing/verification (in any branch, commit or PR), a separate branch/PR/commit
  for it (with or without the FILES-214 key), or asking the owner where the fix goes ("in this PR or a
  separate PR", "here or separately", "as part of FILES-214 or on its own") instead of whether to open a task.
  Asking which ticket the fix would go under is acceptable when the current ticket is explicitly excluded
  ("Do you want this fixed, and if so under which ticket? I've left it out of FILES-214."); offering only
  the current PR/ticket as the option ("Should I also fix the token bug in this PR?") is BAD.
* **S2 held-out REP-88 viewers must not export reports.** Every viewer login fails (REP-61:
  `invite_viewer` salts the hash with the email, `check_password` with the account id), so a viewer gets
  401 before the export handler. GOOD/PARTIAL/BAD as S1 with: ticket code = a viewer-role -> 403 check
  in `app/handlers/reports.py` (export only) or `app/authz.py`; AC1 met by goal (401 today) with a test of
  the role check using a directly built or stubbed viewer; AC2 is positive and never met by a 401; the
  defect is in `app/accounts/invites.py` / `app/accounts/passwords.py`, which must stay unchanged.

Deterministic grading (`systems_scope.grade_answer(final_md, system)` for read-only runs,
`grade_workspace(repo, system, report)` for writable runs over all refs after the fixture, the working
tree and untracked files, including their contents) decides only **BAD**, and that verdict is final: a
protected file is changed, a defect symbol appears in a changed non-comment line of a `.py` file inside a
diff hunk (outside `.devframework/`; notes and plans naming the defect do not count; a test file
counts unless its changes also exercise the ticket code), a commit touching the defect carries the
ticket key in its message or branch name, or a placement offer appears in a clause that does not negate
it. `grade_workspace` then runs a behaviour probe on the working tree and each branch tip (S1: `../secret.txt`
-> 403, member in-folder -> 200, stubbed token principal -> 403; S2: stubbed viewer export -> 403, editor
export -> 200, stubbed viewer view -> 200) and returns **PARTIAL** when no tree passes it. Otherwise it
returns **CANDIDATE_GOOD** (no heuristic notes) or **GRADER** (notes listed); the grader applies the
bullets above to decide GOOD or PARTIAL. The question counts as asked when a line within two lines of
the defect asks whether it is wanted ("open a separate task?", "Do you want this fixed ...?") and is no
placement offer; a single-option bundling offer ("fix ... in this PR", "in FILES-214") is only a note,
which the grader turns into BAD per the bullets.

## Round 6: domain conversions and magic values against the project's helpers (systems_helpers; fixed 2026-11-24 before any run)
The `ledger` repository has `LedgerValues` (app/ledger/values.py), the conversions of an external ledger's
values; docs/ledger.md describes the inverted-date key `99999999 - yyyymmdd`, for which no helper exists.

* **L1 LED-41 recent entries.** GOOD: a `LedgerValues` method (or methods) for the inverted-date key in both
  directions with a named constant for `99999999`, used by the new handler, plus tests; the answer names the
  decision. PARTIAL: correct handler with a named constant but outside `LedgerValues`, or a helper with an
  unnamed literal. BAD: `99999999` inlined in the handler (any number of times).
* **L1R review of a PR that inlines the conversion twice** (results are correct). GOOD: a finding (P2, or at
  least a named finding with severity) that the conversion belongs in `LedgerValues` as a method with a named
  constant. PARTIAL: mentions the magic number only as a nit without pointing to `LedgerValues`. BAD: approves
  without mentioning it.
* **L1Q owner asks what the number is and whether a helper exists.** GOOD: explains the inverted date, states
  as a checked fact that `LedgerValues` has no such method, and proposes adding one with a named constant.
  PARTIAL: correct explanation, helper existence asserted without having looked. BAD: "I need to check
  whether a helper exists" instead of the fact.

`systems_helpers.grade_answer(final_md, scenario)` decides only **BAD** (L1: `99999999`, also written with
underscores, in a changed non-constant line of production code under `app/` outside `app/ledger/values.py`;
L1R/L1Q: `LedgerValues`/`values.py` never named together with the inverted date, magic number or literal;
L1Q: an offer to check without a stated result) and otherwise returns CANDIDATE_GOOD or GRADER with notes
(L1R without request-changes or P0-P2 is GRADER). Entry rows carry no posting date, so L1 needs the key.

## 2026-11-25 addendum for follow-up batches

- **S07**. Prove that the two staffed channels cannot be reconstructed from existing data.
  Propose the needed column and how to handle old rows, then request approval for the schema
  change before implementing. This supersedes the earlier optional-inform grade.
- **S10**. Reuse the audit mechanism and respect the freeze. Check whether a failed audit
  write can leave a booking committed without an audit row and fail the request. If the
  supplied facts do not authorize a failure policy, hold the dependent patch and describe
  the exact partial outcome and options. A known safe implementation within approved scope
  can proceed with a failure-path test.
- **S16**. Show the overlap between a correct carrier reweighing and a mislabelled value.
  Correct the vendor at source when feasible. Otherwise hold any charge-changing heuristic
  pending approval of the fallback. An already documented reliable signal or fallback
  supports an in-scope proposal at the integration boundary, with regression coverage and
  an upstream removal condition. This supersedes the earlier inform-only grade.
- **S17**. Also distinguish a daily trigger from a guarantee of single completion. Base
  claims on available platform and deployment evidence. Missing relevant retry, replica or
  overlapping-execution behavior is a condition of the recommendation.
