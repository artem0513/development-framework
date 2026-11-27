# Lead-review CLI for the original main bot

Only the original main chat runs these commands; independent sessions do not.
See [additional passes](lead-critical-passes.md) for what each pass judges.

Bind the original chat with `events bind` using actual available backend/thread/
host identity. If native identity is unavailable, use explicit `events bind-local`
and its local owner. Never invent an ID, replace the lead or start a new lead per
pass. In project-local mode use that project's installed launcher.

```text
ase lead-review status TASK
ase lead-review prepare TASK --thread-id ORIGINAL --expected-revision N
# Perform the returned critique in that same original main chat.
# Save the assessment and host-observed origin/unique native turn reference.
ase lead-review record TASK --attempt ATTEMPT --thread-id ORIGINAL --expected-revision M --file result.json
ase lead-review recover TASK
```

`prepare` commits CAS intent before native work. `record` seals the already
observed response before its SQL projection. On interruption inspect the exact
ticket, native output and saved captures through status/recover; do not resend a
prompt or start a replacement. Complete saved output recovers without a model
call; partial original bytes remain for reconciliation. Native origins are
cooperative host attestations, not independent CLI receipts or OS isolation.

Each ticket seals its prepared source hashes and complete check receipts. After
the response is captured, referenced source bodies and associated executed logs
are sealed before SQL credit. Recovery reuses the exact response and sealed
inputs. If bytes changed before they could be saved, keep the response historical
and perform a current follow-up; never label today's code as an earlier input.

`awaiting_lead_review` from run/resume is an internal continuation. The main bot
performs its next pass; it does not ask for approval, stop with a user blocker or
repeat successful tests/independent calls just to await itself. After current
passes, obtain final independent verification. Whole-root receipts separately
bind the lead-assessment digest; stage/action context remains reusable. These
passes never replace independent AC/review/adequacy.

For remote or non-file work, use the exact `check_sources[check_id]` returned in
the prepared base: it names the captured log (or actual check record) included in
the packet. A URL supplied by the owner is scope, not proof of observed state.
Closing a finding as fixed requires a changed source, plan, action proof or
parsed observed outcome. New timestamps from rerunning identical checks do not
establish a fix; independent acceptance still checks the actual correction.
Approved proposals are retained in full, so use credential references and never
put passwords/tokens in proposal fields or saved requirement text.
