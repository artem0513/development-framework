import unittest

from ase.report_md import render


class MarkdownReportTests(unittest.TestCase):
    def report(self):
        return {'task': {'task_id': 't1', 'contract': {'id': 'fx-rate', 'request': 'Show the FX rate | in the invoice'},
                         'proposal': {'summary': 'Reuse the existing rate source.', 'non_goals': ['No new cache'],
                                      'unchanged_behavior': ['Invoice totals'], 'rollback': 'Revert the commit',
                                      'alternatives': [{'option': 'New service', 'why_not': 'Infrastructure without need'}],
                                      'effects': [{'id': 'col', 'kind': 'schema', 'basis': 'REQUIREMENT', 'ref': 'JIRA-1', 'change': 'Add rate column'}]}},
                'quality_gate': {'status': 'REJECTED', 'rejected_reasons': ['P1: Null dereference'], 'blocked_reasons': [],
                                 'profile_evidence': {'by_id': {'tests': {'status': 'PASS'}, 'lint': {'status': 'FAIL', 'reason': 'E501'}}},
                                 'lead_review': {'required_passes': 1, 'completed_passes': 1, 'status': 'READY'},
                                 'disposed_findings': [{'severity': 'P2', 'disposition': 'waive', 'user_message_ref': 'prompt:abc', 'message': 'Naming'}]},
                'usage': {'total': {'input_tokens': 100, 'output_tokens': 20, 'cache_read_tokens': 5, 'cache_creation_tokens': 0, 'cost_usd': 0.5},
                          'sessions': [{}, {}], 'sessions_without_usage': 0}}

    def test_sections_keep_facts_judgments_and_enforcement_apart(self):
        text = render(self.report())
        for expected in ('## fx-rate: gate REJECTED (1 finding(s) cleared by owner, not verified)', 'Show the FX rate \\| in the invoice', '- No new cache',
                         'New service: not chosen because Infrastructure without need', '**Rollback:** Revert the commit',
                         '| col | schema | REQUIREMENT JIRA-1 | Add rate column |', '| lint | FAIL | E501 |',
                         '### Independent judgments', 'Lead critiques: 1/1', 'P2 finding waive by owner (prompt:abc, owner-asserted, not verified)',
                         '- P1: Null dereference', '100 in / 20 out tokens', '$0.50', '### Enforcement'):
            self.assertIn(expected, text)
        self.assertLess(text.index('### Executed checks'), text.index('### Independent judgments'))

    def test_owner_prompt_text_never_reaches_the_pr_body(self):
        report = self.report()
        report['quality_gate']['disposed_findings'][0]['prompt_excerpt'] = 'private owner words'
        self.assertNotIn('private owner words', render(report))

    def test_minimal_task_renders_without_optional_sections(self):
        text = render({'task': {'task_id': 't2', 'contract': {'request': 'r'}, 'proposal': None},
                       'quality_gate': {'status': 'BLOCKED', 'blocked_reasons': ['Independent ac session missing']}})
        self.assertIn('No executed check results are recorded.', text)
        self.assertIn('- Independent ac session missing', text)
        self.assertNotIn('Non-goals', text)


if __name__ == '__main__':
    unittest.main()
