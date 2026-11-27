# Solution calibration examples

Real tickets where correct but over-engineered changes were rejected by the team lead
("I hired a person, not an AI agent"), and the proportional solution. Use them to judge
size and mechanisms; they are not templates.

## 1. Integration lost authorization: email admins, show a banner with a reconnect link

- **Over-engineered (31 files, +545 lines):**
  - atomic "claim job" logic in the shared background-job runner, affecting every job;
  - distributed locks around connect, disconnect and email sending;
  - a custom deferred email queue with retries and stale-email cancellation, although the
    project already had a standard send-email command;
  - a per-incident ID plus a migration to deduplicate emails;
  - a token re-check inside code that already held a lock;
  - a banner that polled the server every minute.
- **Proportional:** send the email through the existing command when the integration status
  changes from Active to Broken. Verify the existing transition prevents repeat alerts on a
  subsequent call. If the approved scope includes a banner, use the existing feature area. If
  the owner limits this delivery to email, record the clarification and who owns the UI work.

## 2. "Don't sync private items to the external system"

- **Over-engineered:**
  - the filter, plus a project-wide distributed lock between "edit visibility" and "sync";
  - a conditional transaction (`tx = locked ? null : BeginTransaction()`);
  - an optional constructor dependency (`ILock lock = null` plus a null guard);
  - re-reads under the lock and two extra visibility re-checks;
  - unrelated behaviour changes.
- **Proportional when visibility is defined at batch selection:** one query filter excluding
  private items, plus tests for it. If the requirement instead covers changes committed before
  upload starts, the snapshot-only filter misses that criterion. Use the established, applicable
  timing contract when the ticket leaves it implicit. Ask the owner only if the timing remains
  materially ambiguous after checking authoritative sources and changes the decision.

## 3. "Allow downloads only from the project's workspace; return 403 for others"

- **Over-engineered:** the correct check, plus:
  - a 10-minute cache with hashed keys that duplicated a re-check already done on download;
  - an unrelated auth bug fix bundled into the same PR.
- **Proportional:** the workspace check returning 403, plus tests. The unrelated auth bug is
  reported in one line ("Found separate defect X (evidence); outside this ticket, left untouched.
  Open a separate task?"), with no fix, branch or PR, current or separate
  ([out-of-scope discovery](../SKILL.md#out-of-scope-discovery)).

## 4. "Duplicates in the sync log export" (a bug ticket that was a question)

- **Wrong:** went straight to a code change.
- **Right:**
  - the rows differed by version, so the expected behaviour was already in place;
  - explain the difference to the owner and draft a question to the reporter if the desired
    display remains unclear.

## 5. "Email admins when auth lapses" (a new pipeline instead of the existing point)

- **Wrong:**
  - invented a scheduled validation job, which is a new pipeline;
  - changed the root app component to show a banner, although the lead said that component must
    not be touched;
  - a "simplified" version kept both, because the agent judged them "really needed" without asking.
- **Right:** send the email at the existing point where using the token fails. Clarify the scope
  if the ticket still lists a UI alert and the lead's decision covers email only.

## 6. "Use the server's permissions for the project settings card"

The adjacent settings card derives `CanEdit` from the same role rule as its write commands.
The feature owner confirms that a user arriving through the organization view retains edit
rights when also a project captain.

- **Wrong:** add another frontend permissions helper or copy the server's role logic into the
  component. The UI can again disagree with the write commands.
- **Right:** compute the flag from the existing project permission rule and read it in the card.
  Verify a captain, a view-only user and a user with both roles. Check that the banner describes
  the agreed behavior. Supply the adjacent settings query and the write guards as review inputs.

## 7. "Explain why connecting a project failed"

The current connect path and a background sync both call a shared session service. Changing it
from throwing an error to returning an empty result can send the background caller down a
different path even if the connect UI shows the expected message.

- **Right:** inspect both callers. Prefer the existing error mapping if it can carry the message.
  If the shared contract must change, show the owner the effect on both flows in the plan and
  verify each. The review needs the unchanged background caller to judge the effect.

## 8. "Schedule a daily reminder"

A sibling service uses a package for scheduling. The target service already has a queue and a
worker but no scheduling package or timer deployment. The owner has given a general go-ahead.

- **Wrong:** add the sibling package or deploy a new timer on the assumption that a nearby
  precedent or the general go-ahead supplies approval.
- **Right:** first check whether the existing worker has a suitable trigger. If a new package or
  timer is needed, name it and explain the gap and affected deployment before editing. Approval
  of a plan naming that decision covers it. An ordinary handler using a trigger already established
  in the target service remains a routine implementation choice.

## 9. "Remove the legacy carrier client"

Search the client name and configuration keys across all environment settings, registrations,
deployment and docs. An unchanged production override can still point to the deleted client.
Remove obsolete references in scope and give the reviewer the matching paths or search result.

## 10. "Send the site type to the destination service"

The peer producer sends the canonical site codes verbatim. Confirm that both integrations
use the same contract and forward those values. If an incoming representation is invalid,
trace the producer first. A guessed lookup table or consumer converter changes the contract
and can mask the source defect.

## 11. A service returns an amount in the wrong format

Capture the value at the producer boundary and compare it with the agreed representation.
If the producer violates it, correct the producer in the approved scope and verify the affected
consumer path. Check current consumers and any code that already compensates for the value.
If the correction has broad downstream impact or callers may rely on the old output, give the
owner examples of affected callers and the compatibility options before editing. A converter
in the consumer leaves the producer fault in place and needs separate justification and approval.

## Size rule of thumb

Sketch the minimal solution first. Either of these triggers close scrutiny of the diff:

- a diff more than about 2x that estimate;
- more than about 50 production lines for a one-filter or one-check ticket.

The finding itself names the specific unrequired hunks, unless a category (a) requirement
explains the difference.
