# Original coordinator and durable results

Use for managed delegated work and independent verification results. Keep the chat that owns the original assignment as the lead. Native workers return bounded findings; they do not create a second lead or impersonate a user approval. Preserve the task, project, provider and exact original thread/host identity observed from the current native environment. A share URL, a guessed thread or a worker ID is not a destination.

Bind the original endpoint once through the selected project's local CLI:

```text
ase events bind TASK --file endpoint.json --user-message-ref ORIGINAL_AUTH --expected-revision 0
```

The endpoint contains `backend`, `thread_id`, `host_id`. Use the actual host identity, not a portable hardcoded value. Existing binding is immutable; a different chat must not silently take ownership. Results may queue before an endpoint is available. Do not ask for a missing technical ID until inspecting the native environment; do not claim an unavailable endpoint is bound.

If the provider exposes no usable original chat ID, use `ase events bind-local TASK --backend codex|claude|copilot --user-message-ref ORIGINAL_AUTH --expected-revision 0`. Retain its returned local owner token in the original session context and use it as `--thread-id` for claim/ack. This explicitly local identity permits inbox processing without a messaging tool; it is never a native chat address, and dispatch refuses it. Do not ask the owner to discover a routine unavailable platform ID or claim native delivery from this fallback.

## Process results without another user prompt

Inspect `ase events list TASK` at restore, after worker/check/verifier completion, and before the final report. `ase restore TASK` and `report TASK` include the inbox. `ase events recover TASK` reconciles sealed delegated/captured verifier facts after interruption; it never launches a worker or upgrades historical evidence to current acceptance.

For each unprocessed event:

1. Check its original task, source attempt/receipt, evidence hashes and current task/source/check bindings. COMPLETE means a result was captured, not that its conclusions passed acceptance.
2. Commit `ase events claim TASK EVENT --thread-id ORIGINAL_THREAD --expected-revision N` before responding with effects. Keep that processing ID in the task checkpoint together with the existing workflow/repair action reference. A claimed event is resumed/reconciled, never freshly claimed after a crash. Existing root locks, effect ledger and progress history still own execution.
3. Classify the result: a confirmed current in-scope defect/required coverage gap enters the existing repair/test/independent-review loop; a stale or already-applied result is historical; a genuine unresolved owner decision gets one concise question. Continue unrelated approved work.
4. Finish processing with `ase events ack TASK EVENT --processing-id CLAIM --thread-id ORIGINAL_THREAD --outcome handled|stale|blocked --evidence CHECKPOINT --expected-revision N`. Acknowledgement records handling, not AC, publication authority or overall completion.

An informational successful result needs no new model run. Do not respond to every verification event by launching verification again. Run fresh checks/judgments when changed artifacts, actionable findings or unresolved evidence require them. Duplicate notifications reuse the same event and original work history. Never rename work, reset attempts or weaken coverage to get approval.

## Optional native return to the original chat

The active lead can consume its inbox directly; this needs no messaging integration. When an authorized native host supports exact-thread messaging, commit a delivery ticket first:

```text
ase events dispatch TASK EVENT --expected-revision N
```

Pass the ticket's exact destination and service prompt through the host's native message tool. Preserve service-event provenance; this message carries evidence under existing authority. Save the actual host response and observed matching event, then record delivery separately:

```text
ase events observe TASK EVENT --attempt ATTEMPT --file observation.json --expected-revision N
```

Observation contains `state` (`DELIVERED` or `UNKNOWN`), the exact `destination` and an actual bounded evidence `reference`. Native acceptance/delivery and lead processing are separate. A timeout, missing response or absent list entry never authorizes resend. A DISPATCHING/UNKNOWN attempt retains its identity until positively reconciled; no timer expires it. A delivered message does not prove its findings were processed.

For Codex, Claude and Copilot inspect the actual host tools before promising routing. Native child return, active-chat messaging and waking an idle desktop chat from an unrelated CLI are distinct capabilities. The local queue alone cannot wake an application. Without an available authorized transport, keep the event pending and let the original active lead consume it; accurately disclose the wake-up limit. Do not introduce mandatory MCP, credentials changes or a second orchestrator to hide this limit.
