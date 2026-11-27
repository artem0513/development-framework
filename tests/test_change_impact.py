"""Caller index for changed files and per-role verifier backends."""
import copy
import shutil
import tempfile
import unittest
from pathlib import Path

from ase.evidence.impact import change_impact, _terms
from ase.tasks import backend_for


def callers(impact, row=0):
    """{caller path: its excerpt, or the reason it has none, or 'IN_PACKET'} for one change."""
    change = impact['changes'][row]
    table, in_packet = impact['caller_paths'], set(impact['caller_in_packet'])
    reasons = {number: name for name, numbers in change.get('excerpt_omitted', {}).items() for number in numbers}
    return {table[number]: ('IN_PACKET' if number in in_packet else change.get('excerpts', {}).get(str(number), reasons.get(number)))
            for number in change['callers']}


class ChangeImpactTests(unittest.TestCase):
    def setUp(self):
        self.repo = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.repo, ignore_errors=True)
        files = {'app/rates.py': 'def rate():\n    return 1\n',
                 'app/api.py': 'from app.rates import rate\n',
                 'jobs/nightly.py': 'import app.rates as r\n',
                 'app/separates.py': 'x = "separates"\n',
                 'pkg/billing/__init__.py': 'X = 1\n',
                 'app/uses_pkg.py': 'from pkg import billing\n',
                 'README.md': 'rates are documented here\n'}
        for rel, body in files.items():
            (self.repo / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.repo / rel).write_text(body, encoding='utf-8')
        self.paths = {rel: 'h' for rel in files}

    def test_lists_callers_and_marks_those_outside_the_packet(self):
        impact = change_impact(self.repo, self.paths, {'app/rates.py', 'README.md'}, {'app/rates.py', 'app/api.py'})
        (row,) = impact['changes']
        self.assertEqual(row['path'], 'app/rates.py')
        listed = callers(impact)
        self.assertEqual(set(listed), {'app/api.py', 'jobs/nightly.py'})
        self.assertEqual(listed['app/api.py'], 'IN_PACKET')
        self.assertEqual(row['callers_outside_packet'], 1)
        self.assertIn('1: import app.rates as r', listed['jobs/nightly.py'])
        self.assertNotIn('callers_not_listed', row)

    def test_excerpt_windows_prefer_uses_of_changed_names_over_imports(self):
        (self.repo / 'app/report.py').write_text('from app.rates import rate\n' + 'x = 1\n' * 20 + 'total = rate() * 3\n',
                                                 encoding='utf-8')
        paths = {**self.paths, 'app/report.py': 'h'}
        impact = change_impact(self.repo, paths, {'app/rates.py'}, set())
        excerpt = callers(impact)['app/report.py']
        self.assertIn('22: total = rate() * 3', excerpt)
        self.assertNotIn('1: from app.rates import rate', excerpt)

    def test_packet_over_bound_drops_excerpts_first(self):
        import json
        from ase.evidence.packets import _drop_caller_excerpts
        impact = {'caller_paths': ['a.py'], 'caller_in_packet': [], 'changes': [{'callers': [0], 'excerpts': {'0': 'x' * 100}}]}
        packet = {'files': [{'path': '_ase_context/change_impact.json', 'content': json.dumps(impact), 'sha256': ''}],
                  'coverage': {'missing': []}}
        _drop_caller_excerpts(packet)
        self.assertEqual(callers(json.loads(packet['files'][0]['content'])), {'a.py': 'OMITTED_PACKET_BUDGET'})
        self.assertTrue(packet['coverage']['missing'])

    def test_excerpts_respect_source_policy_redaction_and_budget(self):
        (self.repo / 'jobs/secret.py').write_text('import app.rates\nTOKEN = "sk-' + 'a' * 30 + '"\n', encoding='utf-8')
        paths = {**self.paths, 'jobs/secret.py': 'h'}
        impact = change_impact(self.repo, paths, {'app/rates.py'}, set(), omitted=lambda p: p == 'jobs/nightly.py')
        listed = callers(impact)
        self.assertEqual(listed['jobs/nightly.py'], 'OMITTED_BY_SOURCE_POLICY')
        self.assertNotIn('sk-' + 'a' * 30, listed['jobs/secret.py'])
        from unittest.mock import patch
        with patch('ase.evidence.impact.EXCERPT_BUDGET_CHARS', 10):
            limited = change_impact(self.repo, paths, {'app/rates.py'}, set())
        self.assertEqual(set(callers(limited).values()), {'OMITTED_BUDGET'})

    def test_callers_beyond_the_limit_are_counted(self):
        from unittest.mock import patch
        with patch('ase.evidence.impact.MAX_CALLERS', 1):
            impact = change_impact(self.repo, self.paths, {'app/rates.py'}, set())
        (row,) = impact['changes']
        self.assertEqual((len(row['callers']), row['callers_not_listed']), (1, 1))

    def test_index_and_excerpts_follow_what_the_packet_carries(self):
        changed = {'app/rates.py', 'pkg/billing/__init__.py'}
        narrowed = change_impact(self.repo, self.paths, changed, set(), index_paths={'pkg/billing/__init__.py'})
        self.assertEqual([row['path'] for row in narrowed['changes']], ['pkg/billing/__init__.py'])
        self.assertEqual(narrowed['caller_paths'], ['app/uses_pkg.py'])
        self.assertIsNone(change_impact(self.repo, self.paths, changed, set(), index_paths={'README.md'}))
        # A change reviewed in another part keeps its callers, without excerpts and with the reason.
        withheld = change_impact(self.repo, self.paths, changed, set(), excerpt_paths={'pkg/billing/__init__.py'})
        rates = withheld['changes'].index(next(row for row in withheld['changes'] if row['path'] == 'app/rates.py'))
        self.assertEqual(set(callers(withheld, rates).values()), {'CHANGE_NOT_CARRIED'})
        self.assertNotIn('excerpts', withheld['changes'][rates])

    def test_index_like_files_use_the_package_name_and_docs_only_changes_have_no_index(self):
        self.assertEqual(_terms('pkg/billing/__init__.py'), ['billing'])
        self.assertEqual(_terms('utils.py'), [])
        impact = change_impact(self.repo, self.paths, {'pkg/billing/__init__.py'}, set())
        self.assertEqual(list(callers(impact)), ['app/uses_pkg.py'])
        self.assertIsNone(change_impact(self.repo, self.paths, {'README.md'}, set()))


class RoleBackendTests(unittest.TestCase):
    def contract(self, **policy):
        return {'execution_policy': {'backend': 'codex', **policy}}

    def test_verifier_roles_may_use_another_backend(self):
        contract = self.contract(role_backends={'review': 'claude'})
        self.assertEqual((backend_for(contract, 'review'), backend_for(contract, 'ac'), backend_for(contract)),
                         ('claude', 'codex', 'codex'))

    def test_contract_validation_rejects_unknown_roles_and_backends(self):
        from ase.tasks import validate_contract_v2 as normalize_contract
        base = {'id': 't', 'schema_version': 2, 'profile': 'document', 'request': 'r',
                'scope': {'resources': ['doc.md'], 'effects': ['file_write']}, 'sources': [],
                'acceptance': [{'id': 'A', 'expected': 'e', 'evidence_ids': ['m']}],
                'evidence': [{'id': 'm', 'kind': 'manual', 'required': True}], 'unknowns': [],
                'stages': [{'id': 's', 'acceptance_ids': ['A']}]}
        ok = copy.deepcopy(base)
        ok['execution_policy'] = {'role_backends': {'ac': 'claude', 'review': 'copilot'}}
        self.assertEqual(normalize_contract(ok)['execution_policy']['role_backends'], {'ac': 'claude', 'review': 'copilot'})
        for bad in ({'implementation': 'claude'}, {'review': 'gpt'}, ['review']):
            broken = copy.deepcopy(base)
            broken['execution_policy'] = {'role_backends': bad}
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                normalize_contract(broken)


if __name__ == '__main__':
    unittest.main()
