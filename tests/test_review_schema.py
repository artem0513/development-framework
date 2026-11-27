"""Review format v2: checkable evidence/limits; unverifiable material changes block the gate."""
import copy
import unittest
from unittest.mock import patch

from ase.gates import evaluate_gate
from ase.protocol import validate_payload
from ase.review.schema import REVIEW_SCHEMA_V2, review_schema_for, validate_review_format
from ase.state import hash_value


def v2(**override):
    payload = {'findings': [], 'summary': 'Reviewed app.py and its two callers', 'inspected': ['app.py', 'api.py'],
               'limits': ['jobs/nightly.py body not supplied'], 'unverified_material': []}
    payload.update(override)
    return payload


class ReviewSchemaTests(unittest.TestCase):
    def test_v2_requires_evidence_consequence_and_fix_per_finding(self):
        finding = {'severity': 'P2', 'path': 'app.py', 'line': 4, 'message': 'Null rate', 'evidence': 'rate() returns None for GBP',
                   'consequence': 'Invoice shows 0', 'fix': 'Return the cross rate'}
        self.assertEqual(validate_payload(v2(findings=[finding]), REVIEW_SCHEMA_V2)['status'], 'VALID')
        bare = {k: finding[k] for k in ('severity', 'path', 'line', 'message')}
        self.assertEqual(validate_payload(v2(findings=[bare]), REVIEW_SCHEMA_V2)['status'], 'INVALID')
        self.assertEqual(validate_payload({'findings': [], 'summary': 's'}, REVIEW_SCHEMA_V2)['status'], 'INVALID')

    def test_only_opted_in_contracts_use_v2(self):
        legacy = {'legacy': True}
        self.assertIs(review_schema_for({'review_format': {'version': 2}}, legacy), REVIEW_SCHEMA_V2)
        self.assertIs(review_schema_for({}, legacy), legacy)
        for bad in ({'version': 1}, {'version': 2, 'x': 1}, 'v2'):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                validate_review_format(bad)

    def test_new_cli_contracts_opt_into_v2(self):
        from pathlib import Path
        from ase.__main__ import _new_contract
        from ase.tasks import validate_contract_v2
        contract = validate_contract_v2(_new_contract(str(Path(__file__).resolve().parents[1] / 'templates/task.json')))
        self.assertEqual(contract['review_format'], {'version': 2})


class ReviewGateTests(unittest.TestCase):
    def setUp(self):
        self.contract = {"profile": "document", "acceptance": [
            {"id": "AC1", "expected": "A confirmed result", "mandatory": True, "evidence_ids": ["manual"]}],
            "evidence": [{"id": "manual", "kind": "manual", "required": True}], "unknowns": []}
        self.current = {"contract_hash": hash_value(self.contract), "proposal_hash": "proposal", "plan_hash": "plan",
                        "fingerprint": "source", "check_hash": "check", "checks": self.contract["evidence"]}
        checks = {"kind": "checks", "fingerprint": "source", "contract_hash": self.current["contract_hash"], "plan_hash": "plan",
                  "results": [{"id": "manual", "kind": "manual", "confirmed": True, "user_message_ref": "chat:proof"}]}
        self.receipts = [checks, {"role": "ac", "execution_id": "a"}, {"role": "review", "execution_id": "r"}]

    def gate(self, review):
        def judgment(receipt, **kwargs):
            payload = ({"criteria": [{"id": "AC1", "expected": "A confirmed result", "status": "PASS", "evidence": ["manual"],
                                       "method": "compared", "limitations": [], "missing_coverage": []}]}
                       if receipt["role"] == "ac" else copy.deepcopy(review))
            return {"valid": True, "evidence_kind": "independent_cli",
                    "result": {"payload": payload, "session_id": receipt["role"] + "-session"}}
        with patch("ase.gates.verify_receipt", side_effect=judgment):
            return evaluate_gate(self.contract, self.current, self.receipts, [])

    def test_unverified_material_change_blocks_and_limits_alone_do_not(self):
        self.assertEqual(self.gate(v2())['status'], 'APPROVED')
        blocked = self.gate(v2(unverified_material=['New retry loop in sync.py: caller contract not supplied']))
        self.assertEqual(blocked['status'], 'BLOCKED')
        self.assertTrue(any('could not verify material change' in r for r in blocked['blocked_reasons']))

    def test_v2_contract_requires_v2_fields_in_the_review_result(self):
        self.contract['review_format'] = {'version': 2}
        self.current['contract_hash'] = hash_value(self.contract)
        self.receipts[0]['contract_hash'] = self.current['contract_hash']
        self.assertEqual(self.gate({'findings': [], 'summary': 'reviewed'})['status'], 'BLOCKED')
        self.assertEqual(self.gate(v2())['status'], 'APPROVED')

    def test_legacy_review_payload_is_unchanged(self):
        self.assertEqual(self.gate({'findings': [], 'summary': 'reviewed'})['status'], 'APPROVED')


if __name__ == '__main__':
    unittest.main()
