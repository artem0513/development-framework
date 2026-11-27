import json
import sqlite3
import unittest

from ase.usage import session_usage, task_usage


class _Store:
    def __init__(self):
        self.db = sqlite3.connect(':memory:')
        self.db.execute('CREATE TABLE runtime_records(kind TEXT, key TEXT, revision INT, payload TEXT, updated TEXT)')

    def put(self, kind, key, payload):
        self.db.execute('INSERT INTO runtime_records VALUES(?,?,?,?,?)', (kind, key, 1, json.dumps(payload), 'now'))


class UsageTests(unittest.TestCase):
    def test_claude_and_codex_envelopes_normalize(self):
        claude = {'type': 'result', 'total_cost_usd': 0.25,
                  'usage': {'input_tokens': 10, 'output_tokens': 5, 'cache_read_input_tokens': 100, 'cache_creation_input_tokens': 7}}
        self.assertEqual(session_usage(claude), {'input_tokens': 10, 'output_tokens': 5, 'cache_read_tokens': 100,
                                                 'cache_creation_tokens': 7, 'cost_usd': 0.25})
        codex = {'type': 'turn.completed', 'usage': {'input_tokens': 3, 'cached_input_tokens': 2, 'output_tokens': 1}}
        self.assertEqual(session_usage(codex)['cache_read_tokens'], 2)
        self.assertNotIn('cost_usd', session_usage(codex))
        for bad in (None, {}, {'usage': 'x'}):
            self.assertIsNone(session_usage(bad))
        self.assertEqual(session_usage({'usage': {'input_tokens': -1, 'output_tokens': True}})['input_tokens'], 0)

    def test_task_usage_sums_only_this_task_and_counts_unreported_sessions(self):
        store = _Store()
        store.put('verification', 't1:ac', {'terminal': {'usage': {'input_tokens': 10, 'output_tokens': 1}, 'total_cost_usd': 0.1}})
        store.put('verification', 't1:review', {'terminal': {'usage': {'input_tokens': 20, 'output_tokens': 2}, 'total_cost_usd': 0.2}})
        store.put('implementation', 't1', {'terminal': None})
        store.put('verification', 't10:ac', {'terminal': {'usage': {'input_tokens': 999}}})
        store.put('cycle', 't1:s', {'terminal': {'usage': {'input_tokens': 999}}})
        result = task_usage(store, 't1')
        self.assertEqual((result['total']['input_tokens'], result['total']['output_tokens'], result['total']['cost_usd']), (30, 3, 0.3))
        self.assertEqual((len(result['sessions']), result['sessions_without_usage']), (2, 1))


if __name__ == '__main__':
    unittest.main()
