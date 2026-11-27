"""Blind review pass: packet contents, its own independent session and its gate verdict.

The provider is the protocol fixture, so these tests prove packet, binding and gate protocol,
never review quality.
"""
import json
from pathlib import Path
import shutil
import unittest
from unittest.mock import patch

from ase.gates import GateService
from ase.review import blind
from ase.review.examples import PACKET_PATH, PATH as EXAMPLES_PATH
from ase.runner import verify_receipt
from ase.state import read_record, transaction, write_record
from ase.tasks import record_baseline, validate_contract_v2
from ase.workflow import WorkflowController
from test_review_packet import PacketFixture, contract
import test_review_packet

PROVIDER = test_review_packet.PROVIDER.replace(
    " payload = {'findings':findings,",
    " if request['handoff'].get('purpose') == 'blind_review' and pathlib.Path('p2-blind').exists():\n"
    "  findings = [{'severity':'P2','path':'src/svc/alpha/m000.py','line':1,'message':'blind reader defect'}]\n"
    " payload = {'findings':findings,")


@unittest.skipUnless(shutil.which("git"), "git required")
class BlindReviewTests(PacketFixture):
    FILES_PER_FOLDER = 2
    LINES_PER_FILE = 20

    def approve_blind(self, task_id):
        value = {**contract(task_id), "blind_review": dict(blind.POLICY)}
        task = self.tasks.create(value)
        task = self.tasks.propose(task_id, {"summary": "Rename", "stage_ids": ["change"]}, expected_revision=task["revision"])
        task = self.tasks.approve(task_id, proposal_hash=task["proposal_hash"], user_message_ref="test:owner-approved",
                                  approved_stage_ids=["change"])
        record_baseline(self.store, task_id, task["contract"])
        return task

    def controller(self, task_id):
        with patch.object(test_review_packet, "PROVIDER", PROVIDER):
            return super().controller(task_id)

    def request(self, task_id, role, handoff):
        return WorkflowController(self.store)._request(task_id, role, {"type": "object"}, handoff)

    def test_blind_packet_withholds_intent_and_keeps_the_change(self):
        self.approve_blind("blind")
        (self.root / EXAMPLES_PATH).parent.mkdir(exist_ok=True)
        (self.root / EXAMPLES_PATH).write_text("Rejected: a second error mapper beside the base one.\n", encoding="utf-8")
        scope = self.request("blind", "review", {"stage_id": None})
        hidden = self.request("blind", "review", {"stage_id": None, "purpose": blind.PURPOSE})
        acceptance = self.request("blind", "ac", {"stage_id": None})
        paths = {row["path"] for row in hidden["source_packet"]["files"]}
        self.assertIn("src/svc/alpha/m000.py", paths)
        self.assertIn("_ase_context/source_changes.json", paths)
        for withheld in ("_ase_context/contract.json", "_ase_context/stage_history.json",
                         "_ase_context/execution_plan.json", "_ase_context/discovery.json"):
            self.assertNotIn(withheld, paths)
            self.assertIn(withheld, {row["path"] for row in scope["source_packet"]["files"]})
        self.assertEqual(hidden["source_packet"]["requirements"], [blind.WITHHELD])
        self.assertNotIn("Rename old_helper", json.dumps(hidden["source_packet"]))
        self.assertEqual(hidden["handoff"]["task_proposal"], {"withheld": blind.WITHHELD})
        self.assertIn("proposal", scope["handoff"]["task_proposal"])
        # Project-owned examples reach both review passes, not acceptance.
        self.assertIn(PACKET_PATH, paths)
        self.assertIn(PACKET_PATH, {row["path"] for row in scope["source_packet"]["files"]})
        self.assertNotIn(PACKET_PATH, {row["path"] for row in acceptance["source_packet"]["files"]})

    def test_blind_session_is_required_and_its_blocking_findings_reject_the_gate(self):
        self.approve_blind("blind")
        verdict = self.gate_at_protocol_boundary("blind")
        self.assertIn("Blind review: independent review session missing", verdict["blocked_reasons"])
        self.controller("blind").verify("blind")
        receipt = read_record(self.store.db, "verification_blind", blind.key("blind", None))["payload"]
        self.assertEqual(receipt["status"], "COMPLETE")
        request = json.loads(Path(receipt["artifacts"]["request"]["path"]).read_text(encoding="utf-8"))
        self.assertEqual(request["handoff"]["purpose"], blind.PURPOSE)
        verdict = self.gate_at_protocol_boundary("blind")
        self.assertFalse([reason for reason in verdict["reasons"] if reason.startswith("Blind review")], verdict["reasons"])
        # The ordinary review receipt cannot stand in for the blind pass.
        review = read_record(self.store.db, "verification", "blind:review")["payload"]
        with transaction(self.store.db) as db:
            old = read_record(db, "verification_blind", blind.key("blind", None))
            write_record(db, "verification_blind", blind.key("blind", None), review, expected_revision=old["revision"])
        verdict = self.gate_at_protocol_boundary("blind")
        self.assertIn("Blind review: reused another execution", verdict["rejected_reasons"])
        # A blocking blind finding rejects the gate and names its pass.
        (self.base / "provider" / "p2-blind").write_text("", encoding="utf-8")
        self.controller("blind").verify("blind")
        verdict = self.gate_at_protocol_boundary("blind")
        self.assertEqual(verdict["status"], "REJECTED")
        self.assertIn("Blind review: P2: blind reader defect", verdict["rejected_reasons"])

    def test_contract_field_is_validated_and_kept_across_revision(self):
        with self.assertRaisesRegex(ValueError, "blind_review"):
            validate_contract_v2({**contract("bad"), "blind_review": {"version": 2}})
        self.approve_blind("blind")
        task = self.tasks.snapshot("blind")
        revised = self.tasks.revise("blind", contract("blind"), expected_revision=task["revision"])
        self.assertEqual(revised["contract"]["blind_review"], blind.POLICY)

    def test_new_cli_software_contracts_opt_in(self):
        from ase.__main__ import _new_contract
        path = self.base / "task.json"
        path.write_text(json.dumps(contract("cli")), encoding="utf-8")
        self.assertEqual(_new_contract(path)["blind_review"], blind.POLICY)


if __name__ == "__main__":
    unittest.main()
