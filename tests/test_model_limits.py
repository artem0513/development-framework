"""Packet budget follows the backend's observed model, capped for review quality, never above what it can read."""
import json
import sqlite3
import unittest

from ase import model_limits as limits
from ase.protocol import DEFAULT_PACKET_BYTES, MAX_DECLARED_PACKET_BYTES


def envelope(window=1000000, output=128000, model='claude-opus-5-5', helper=None):
    usage = {model: {'inputTokens': 2, 'cacheReadInputTokens': 649, 'cacheCreationInputTokens': 2645,
                     'contextWindow': window, 'maxOutputTokens': output}}
    if helper:
        usage[helper] = {'inputTokens': 1, 'contextWindow': 50000, 'maxOutputTokens': 8000}
    return {'type': 'result', 'modelUsage': usage}


def contract(backend='claude', roles=None, transport=None):
    value = {'execution_policy': {'backend': backend, **({'role_backends': roles} if roles else {})}}
    if transport is not None:
        value['context_transport'] = transport
    return value


class ModelLimitTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.addCleanup(self.db.close)
        self.db.execute('CREATE TABLE runtime_records(kind TEXT, key TEXT, revision INTEGER, payload TEXT, updated TEXT,'
                        ' PRIMARY KEY(kind, key))')

    def receipt(self, key, backend, terminal, updated, kind='independent_cli', status='COMPLETE'):
        payload = {'backend': backend, 'terminal': terminal, 'evidence_kind': kind, 'status': status}
        self.db.execute('INSERT INTO runtime_records VALUES(?,?,?,?,?)',
                        ('verification', key, 1, json.dumps(payload), updated))

    def test_the_main_model_is_read_from_the_cli_envelope(self):
        self.assertEqual(limits.receipt_model({'terminal': envelope(helper='claude-haiku-5-5')}),
                         {'model': 'claude-opus-5-5', 'context_window': 1000000, 'max_output': 128000})
        for terminal in (None, {}, {'modelUsage': {'m': {'contextWindow': '1000000', 'maxOutputTokens': 1}}},
                         {'modelUsage': {'m': {'contextWindow': 0, 'maxOutputTokens': 1}}}):
            with self.subTest(terminal=terminal):
                self.assertIsNone(limits.receipt_model({'terminal': terminal}))

    def test_large_window_is_capped_for_review_quality(self):
        self.assertEqual(limits.model_budget({'context_window': 1000000, 'max_output': 128000}), limits.QUALITY_CAP_BYTES)
        self.assertLessEqual(limits.QUALITY_CAP_BYTES, MAX_DECLARED_PACKET_BYTES)

    def test_small_window_gets_less_than_the_old_default_and_never_zero(self):
        small = limits.model_budget({'context_window': 200000, 'max_output': 32000})
        self.assertEqual(small, (200000 - 32000 - limits.PROMPT_RESERVE_TOKENS) * limits.BYTES_PER_TOKEN)
        self.assertLess(small, DEFAULT_PACKET_BYTES)
        self.assertEqual(limits.model_budget({'context_window': 50000, 'max_output': 8000}), 1)

    def test_no_observation_keeps_the_default(self):
        self.assertEqual(limits.packet_budget(contract(), self.db), DEFAULT_PACKET_BYTES)
        self.receipt('t:review', 'codex', {'type': 'turn.completed'}, '2026-11-25T01:00:00')
        self.assertEqual(limits.packet_budget(contract('codex'), self.db), DEFAULT_PACKET_BYTES)

    def test_the_newest_real_session_of_that_backend_decides(self):
        self.receipt('a:review', 'claude', envelope(window=200000, output=32000), '2026-11-24T01:00:00')
        self.receipt('b:review', 'claude', envelope(), '2026-11-25T01:00:00')
        self.receipt('c:review', 'claude', envelope(window=50000, output=8000), '2026-11-25T02:00:00', kind='protocol_only')
        self.receipt('e:review', 'codex', envelope(window=50000, output=8000), '2026-11-25T04:00:00')
        self.assertEqual(limits.observed_model(self.db, 'claude')['context_window'], 1000000)
        self.assertEqual(limits.packet_budget(contract(), self.db), limits.QUALITY_CAP_BYTES)

    def test_a_refused_session_of_a_smaller_model_lowers_the_budget_at_once(self):
        self.receipt('a:review', 'claude', envelope(), '2026-11-24T01:00:00')
        self.receipt('b:review', 'claude', envelope(window=200000, output=32000), '2026-11-25T01:00:00', status='BLOCKED')
        self.assertEqual(limits.packet_budget(contract(), self.db),
                         limits.model_budget({'context_window': 200000, 'max_output': 32000}))

    def test_a_host_backend_config_limit_also_bounds_the_packet(self):
        self.db.execute('INSERT INTO runtime_records VALUES(?,?,?,?,?)',
                        ('backend_config', 'claude', 1, json.dumps({'max_packet_bytes': DEFAULT_PACKET_BYTES}), 'x'))
        self.receipt('a:review', 'claude', envelope(), '2026-11-25T01:00:00')
        self.assertEqual(limits.packet_budget(contract(), self.db), DEFAULT_PACKET_BYTES)

    def test_a_newer_session_that_reports_no_model_falls_back_to_the_default(self):
        self.receipt('a:review', 'claude', envelope(), '2026-11-24T01:00:00')
        self.receipt('b:review', 'claude', {'type': 'result'}, '2026-11-25T01:00:00')
        self.assertIsNone(limits.observed_model(self.db, 'claude'))
        self.assertEqual(limits.packet_budget(contract(), self.db), DEFAULT_PACKET_BYTES)

    def test_runner_keeps_the_fixed_ceiling_whatever_is_observed(self):
        ceiling = {'max_packet_bytes': limits.QUALITY_CAP_BYTES,
                   'max_input_bytes': limits.QUALITY_CAP_BYTES + DEFAULT_PACKET_BYTES}
        self.assertEqual(limits.runner_limits(contract(), self.db, 'claude'), ceiling)
        self.receipt('a:review', 'claude', envelope(window=200000, output=32000), '2026-11-25T01:00:00')
        self.assertEqual(limits.runner_limits(contract(), self.db, 'claude'), ceiling)

    def test_one_packet_fits_the_smallest_backend_of_its_roles(self):
        self.receipt('a:review', 'claude', envelope(), '2026-11-25T01:00:00')
        self.receipt('b:ac', 'codex', envelope(window=200000, output=32000), '2026-11-25T01:00:00')
        mixed = contract('claude', roles={'ac': 'codex'})
        small = limits.model_budget({'context_window': 200000, 'max_output': 32000})
        self.assertEqual(limits.packet_budget(mixed, self.db), small)
        # Each role's own request is sized for its own backend; implementation uses the default backend.
        self.assertEqual(limits.packet_budget(mixed, self.db, 'ac'), small)
        self.assertEqual(limits.packet_budget(mixed, self.db, 'review'), limits.QUALITY_CAP_BYTES)
        self.assertEqual(limits.packet_budget(mixed, self.db, 'implementation'), limits.QUALITY_CAP_BYTES)

    def test_owner_bound_applies_but_never_beyond_the_observed_model(self):
        owner = {'version': 1, 'mode': 'lossless', 'max_packet_bytes': MAX_DECLARED_PACKET_BYTES}
        self.assertEqual(limits.packet_budget(contract(transport=owner), self.db), MAX_DECLARED_PACKET_BYTES)
        self.receipt('a:review', 'claude', envelope(window=200000, output=32000), '2026-11-25T01:00:00')
        self.assertEqual(limits.packet_budget(contract(transport=owner), self.db),
                         limits.model_budget({'context_window': 200000, 'max_output': 32000}))


if __name__ == '__main__':
    unittest.main()
