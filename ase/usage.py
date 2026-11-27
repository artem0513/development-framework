"""Token/cost accounting read from stored backend terminal envelopes; no receipt format change.

Usage is informational: it helps the owner see review cost per lane. It never gates completion.
"""
import json

# Records that hold the latest receipt of each independent or implementation session.
RECEIPT_KINDS = ('verification', 'verification_part', 'implementation', 'stage_implementation', 'baseline_implementation')
FIELDS = ('input_tokens', 'output_tokens', 'cache_read_tokens', 'cache_creation_tokens')


def _int(value):
    return value if type(value) is int and value >= 0 else 0


def session_usage(terminal):
    """Normalize Claude/Copilot result envelopes and Codex turn.completed usage; None if absent."""
    if not isinstance(terminal, dict) or not isinstance(terminal.get('usage'), dict):
        return None
    raw = terminal['usage']
    usage = {'input_tokens': _int(raw.get('input_tokens')),
             'output_tokens': _int(raw.get('output_tokens')),
             'cache_read_tokens': _int(raw.get('cache_read_input_tokens', raw.get('cached_input_tokens'))),
             'cache_creation_tokens': _int(raw.get('cache_creation_input_tokens'))}
    cost = terminal.get('total_cost_usd')
    if type(cost) in (int, float) and cost >= 0:
        usage['cost_usd'] = float(cost)
    return usage


def task_usage(store, task_id):
    """Sum usage over the current receipts of a task; sessions without usage are counted separately."""
    total = {name: 0 for name in FIELDS}
    total['cost_usd'] = 0.0
    sessions, unreported = [], 0
    placeholders = ','.join('?' * len(RECEIPT_KINDS))
    rows = store.db.execute(
        f"SELECT kind,key,payload FROM runtime_records WHERE kind IN ({placeholders}) "
        "AND (key=? OR substr(key,1,?)=?) ORDER BY kind,key",
        (*RECEIPT_KINDS, task_id, len(task_id) + 1, task_id + ':'))
    for kind, key, payload in rows:
        receipt = json.loads(payload)
        if not isinstance(receipt, dict):
            continue
        usage = session_usage(receipt.get('terminal'))
        if usage is None:
            unreported += 1
            continue
        for name in FIELDS:
            total[name] += usage[name]
        total['cost_usd'] += usage.get('cost_usd', 0.0)
        sessions.append({'kind': kind, 'key': key, **usage})
    total['cost_usd'] = round(total['cost_usd'], 6)
    return {'total': total, 'sessions': sessions, 'sessions_without_usage': unreported}
