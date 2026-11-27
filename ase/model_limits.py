"""Source packet budget derived from the backend's current model instead of a fixed owner-approved number.

The Claude CLI reports the model it ran and that model's context window and output limit in its
result envelope (`modelUsage`), which the Runner keeps verbatim in each receipt's `terminal`. The
newest real independent session of a backend therefore says what the next one can read. A
backend that reports nothing keeps the conservative default. The budget never exceeds the quality
cap: a larger window accepts more text, but review attention drops on very long inputs, so for an
oversized change the host proposes a split into independently checked parts for the owner's
approval (see evidence.representation).
"""
import json

from .protocol import DEFAULT_PACKET_BYTES, MAX_DECLARED_PACKET_BYTES, lossless_transport

# Above this a review part is split even when the model window is larger: one session reads it attentively.
QUALITY_CAP_BYTES = MAX_DECLARED_PACKET_BYTES
# Canonical JSON of code and Cyrillic text averages more than 3 bytes per token; 3 keeps the estimate below the real window.
BYTES_PER_TOKEN = 3
# Room for the runtime policy, skills, schema and instructions sent beside the packet.
PROMPT_RESERVE_TOKENS = 64000
ROLES = ('ac', 'review')


def receipt_model(receipt):
    """{'model', 'context_window', 'max_output'} of the main model in one receipt, or None."""
    terminal = (receipt or {}).get('terminal')
    usage = terminal.get('modelUsage') if isinstance(terminal, dict) else None
    if not isinstance(usage, dict):
        return None
    rows = []
    for model, row in usage.items():
        if not isinstance(row, dict):
            continue
        window, output = row.get('contextWindow'), row.get('maxOutputTokens')
        if type(window) is not int or window <= 0 or type(output) is not int or output < 0:
            continue
        read = sum(row.get(key) or 0 for key in ('inputTokens', 'cacheReadInputTokens', 'cacheCreationInputTokens')
                   if type(row.get(key)) is int)
        rows.append((read, str(model), window, output))
    if not rows:
        return None
    # The model that read the prompt; a helper model reads little.
    _, model, window, output = max(rows)
    return {'model': model, 'context_window': window, 'max_output': output}


def observed_model(db, backend):
    """The model the backend's newest real independent session ran, or None.

    Only the newest session counts, whatever its outcome: after a switch to another model an older report
    would be stale, and a refused oversized packet still names the model that refused it. The report is the
    CLI's own envelope, never model output. Protocol fixtures never set the budget.
    """
    row = db.execute("SELECT payload FROM runtime_records WHERE kind='verification' "
                     "AND json_extract(payload,'$.backend')=? AND json_extract(payload,'$.evidence_kind')='independent_cli' "
                     "ORDER BY updated DESC, rowid DESC LIMIT 1",
                     (backend,)).fetchone()
    try:
        return receipt_model(json.loads(row[0])) if row else None
    except (TypeError, ValueError):
        return None


def model_budget(model):
    """Packet bytes one session of this model can read whole, capped for review quality."""
    tokens = model['context_window'] - model['max_output'] - PROMPT_RESERVE_TOKENS
    # At least one byte: a model too small for any packet refuses every packet instead of disabling the limit.
    return max(1, min(tokens * BYTES_PER_TOKEN, QUALITY_CAP_BYTES))


def backend_budget(contract, db, backend):
    """Packet byte limit for one backend: the observed model, else the default; an owner-approved bound still
    applies, but never beyond what the observed model can read, and a host backend_config limit always applies."""
    model = observed_model(db, backend) if db is not None else None
    limit = model_budget(model) if model is not None else DEFAULT_PACKET_BYTES
    transport = contract.get('context_transport')
    if lossless_transport(contract) and 'max_packet_bytes' in transport:
        owner = transport['max_packet_bytes']
        limit = min(owner, limit) if model is not None else owner
    if db is not None:
        from .state import read_record
        configured = (read_record(db, 'backend_config', backend)['payload'] or {}).get('max_packet_bytes')
        if type(configured) is int and configured > 0:
            # The Runner enforces this host override, so the packet must already fit it.
            limit = min(limit, configured)
    return limit


def packet_budget(contract, db, role=None):
    """Bound for the packet of one role's request, sized for that role's backend.

    Without a role the packet may go to both independent roles, so it must fit the smaller backend.
    """
    from .tasks import backend_for
    roles = (role,) if role is not None else ROLES
    return min(backend_budget(contract, db, backend) for backend in {backend_for(contract, name) for name in roles})


def runner_limits(contract, db, backend):
    """Host Runner ceiling. Packets are built only by the host, already bounded for their role's model; the
    Runner keeps the fixed quality cap, so a model report recorded while a run is in progress cannot make
    the two disagree. The request carries the packet plus at most the default again for policy and schema."""
    return {'max_packet_bytes': QUALITY_CAP_BYTES, 'max_input_bytes': QUALITY_CAP_BYTES + DEFAULT_PACKET_BYTES}
