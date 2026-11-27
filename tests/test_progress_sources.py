"""Validate explicit immutable test boundaries used for recovery credit."""
import unittest
from ase.profiles import validate_evidence_spec


class ProgressSourceContractTests(unittest.TestCase):
    def spec(self, paths):
        return {'id':'unit','kind':'test','parser':'unittest',
                'argv':['python','-m','unittest','discover','-s','tests','-v'],
                'progress_test_sources':paths}

    def test_relative_explicit_sources_are_preserved(self):
        sources=['tests','test_support.py','pyproject.toml']
        self.assertEqual(validate_evidence_spec(self.spec(sources),'software')['progress_test_sources'],sources)

    def test_malformed_private_and_escaping_boundaries_are_rejected(self):
        for paths in [None,[],['tests','tests'],['../tests'],['/private'],['C:\\private'],
                      ['tests/../../private'],['.git/config'],['.devframework/local/memory.sqlite3'],
                      [True],['https://example.invalid/tests'],['tests\0']]:
            with self.subTest(paths=paths),self.assertRaises(ValueError):
                validate_evidence_spec(self.spec(paths),'software')

    def test_non_test_evidence_cannot_claim_case_progress(self):
        spec={'id':'artifact','kind':'artifact','path':'report.md','progress_test_sources':['tests']}
        with self.assertRaises(ValueError):validate_evidence_spec(spec,'document')


if __name__=='__main__':unittest.main()
