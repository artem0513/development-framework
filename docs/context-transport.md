# Bounded exact review context

Opt in with `ase task create --file task.json --compact-context`, or the task
contract field `context_transport: {"version":1,"mode":"lossless"}`. Saved legacy
contracts keep their literal packet behavior; revisions preserve an existing
transport choice. This is available to every task profile and backend.

Large opted-in managed packets use readable lossless representations before packet
admission (the model-derived bound described below). Current
source, requirements, original approved clarifications, historical lead inputs and
actual check evidence retain their identities. No SQLite migration or historical
deletion is required.

Exact duplicate bodies share a same-packet reference. Canonical JSON may share
repeated values or exact source bodies. Standard verbose unittest success lines
are grouped by class; every test name, order and line ending is retained.
Failures, skips, tracebacks and nonstandard multiline output remain literal.
Each delivered representation and reconstructed original have separate hashes;
the host verifies exact restoration. All receipt consumers expand through the
same bounded function. Unknown/cyclic/missing references, corrupt hashes and
excessive depth, node count or expanded bytes fail closed.

Opted-in sessions do not receive prior adequacy verdict prose as input to a
fresh judgment. Original requirements, scenario plans, source, logs and required
historical lead inputs remain present. The gate still checks the separate current
adequacy receipt; withholding prior prose grants no approval.

Historical deltas optionally normalize the authenticated current base from CRLF
to LF before applying edits. The original base hash is checked first and the
exact historical result hash afterward. Historical bytes are never replaced
with today's source; legacy deltas/receipts continue to use their original view.

Large requests explicitly select `handoff.context_transport` layout
`source_text_v1`. The stored request remains ordinary sealed JSON; the model gets
its complete manifest plus length-delimited literal bodies, avoiding repeated
JSON escaping. This view uses every body and is checked against the same receipt.
Old requests without the marker keep their original exact prompt formatting.
Codex's complete prompt is checked against its native character limit before
launch; raw request and packet byte limits remain independent. Other adapters
retain their existing command-line limits and fail closed when exceeded.

The packet bound follows the backend's current model (`ase/model_limits.py`), for every task,
with or without lossless transport. The Claude CLI reports the model it ran with its context
window and output limit (`modelUsage` in the result envelope, kept in each receipt's `terminal`).
The newest independent session (any outcome) of the backend that will read the packet sets its
budget: (window - output limit - 64,000 reserved tokens) x 3 bytes per token, capped at 2 MiB for
review quality. Only that newest session counts, so a switch to another model takes effect at
once; protocol fixtures never count. Without a report the 1 MiB default stays, and a model too
small for a packet refuses it. An oversized change gets a proposed split into independently
checked parts, which the owner approves; parts approved for a larger model may need re-approval
after a switch to a smaller one. The Runner keeps the fixed 2 MiB ceiling: packets are built only
by the host, never from request content.

An owner may still set a bound for one lossless task with
`context_transport: {"version":1,"mode":"lossless","max_packet_bytes":N}`, where N is
an integer from 1 MiB to 2 MiB; it applies, but never beyond what the observed model can
read, and a host `backend_config` `max_packet_bytes` bounds the packet too. Literal (not
opted-in) packets keep their layout but get the same model-derived bound. The Runner
always has the fixed 2 MiB packet and 3 MiB request ceiling; request content, including
any handoff field, cannot raise it.
Each adapter keeps its own native limit: Codex still rejects a complete prompt over
1,048,576 characters, so a larger packet needs a backend that accepts it. Claude
receives the prompt on stdin after a bare `-p`, which avoids the Windows command-line
limit; older Claude receipts with the prompt as the `-p` argument still verify.

These are transport optimizations, not semantic approval or an unlimited context
window. Genuinely excessive unique content still requires a narrower justified
scope or another independently accepted bounded workflow. Missing inputs or an
unavailable reviewer cannot become PASS. Native tool-using advisory reports do
not replace the final managed independent Runner receipts and gate.
