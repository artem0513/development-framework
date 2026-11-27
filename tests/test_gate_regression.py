"""Gate protocol fixtures are not independent semantic model validation."""
import unittest
from unittest.mock import patch

from ase.gates import evaluate_gate
from ase.state import hash_value


class GateRegressionTests(unittest.TestCase):
    def setUp(self):
        self.contract = {"profile": "document", "acceptance": [
            {"id": "AC1", "expected": "A confirmed result", "mandatory": True, "evidence_ids": ["manual"]}],
            "evidence": [{"id": "manual", "kind": "manual", "required": True}], "unknowns": []}
        self.current = {"contract_hash": hash_value(self.contract), "proposal_hash": "proposal", "plan_hash": "plan",
                        "fingerprint": "source", "check_hash": "check", "checks": self.contract["evidence"]}
        self.checks = {"kind": "checks", "fingerprint": "source", "contract_hash": self.current["contract_hash"],
                       "plan_hash": "plan", "results": [{"id": "manual", "kind": "manual", "confirmed": True,
                                                           "user_message_ref": "chat:user:manual-proof"}]}
        self.ac = {"role": "ac", "execution_id": "ac-process"}
        self.review = {"role": "review", "execution_id": "review-process"}

    def judgment(self, receipt, **kwargs):
        payload = ({"criteria": [{"id": "AC1", "expected": "A confirmed result", "status": "PASS",
                                   "evidence": ["manual confirmation"], "method": "compared", "limitations": [], "missing_coverage": []}]}
                   if receipt["role"] == "ac" else {"findings": [], "summary": "reviewed"})
        return {"valid": True, "evidence_kind": "independent_cli",
                "result": {"payload": payload, "session_id": receipt["role"] + "-session"}}

    def assertGate(self, result, status, reason):
        """Status alone can pass for the wrong reason; the expected reason must be among the reported ones."""
        self.assertEqual(result["status"], status, result)
        self.assertTrue(any(reason in r for r in result["reasons"]), (reason, result["reasons"]))

    def test_missing_independent_roles_block_approval(self):
        result = evaluate_gate(self.contract, self.current, [self.checks], [])
        self.assertGate(result, "BLOCKED", "Independent ac session missing")
        self.assertGate(result, "BLOCKED", "Independent review session missing")

    def test_test_adequacy_receipt_cannot_replace_substantive_review(self):
        def adequacy_only(receipt, **kwargs):
            value = self.judgment(receipt)
            if receipt['role'] == 'review':
                value['request_identity'] = {'role': 'review', 'purpose': 'test_adequacy'}
                value['result']['payload'] = {'scenarios': [], 'additional_scenarios': [],
                    'missing_coverage': [], 'summary': 'Only test adequacy was assessed'}
            return value
        with patch('ase.gates.verify_receipt', side_effect=adequacy_only):
            result = evaluate_gate(self.contract, self.current, [self.checks, self.ac, self.review], [])
        self.assertGate(result, 'BLOCKED', 'Independent review receipt is not substantive review evidence')

    def test_protocol_fixtures_cannot_supply_semantic_approval(self):
        def fixture(receipt, **kwargs):
            return {**self.judgment(receipt), "evidence_kind": "protocol_only"}
        with patch("ase.gates.verify_receipt", side_effect=fixture):
            result = evaluate_gate(self.contract, self.current, [self.checks, self.ac, self.review], [])
        self.assertGate(result, "BLOCKED", "Protocol fixtures are not independent semantic approval")

    def test_independent_receipts_without_a_session_id_block(self):
        for missing in (None, ""):
            def no_session(receipt, **kwargs):
                value = self.judgment(receipt)
                value["result"]["session_id"] = missing
                return value
            with self.subTest(session_id=missing), patch("ase.gates.verify_receipt", side_effect=no_session):
                result = evaluate_gate(self.contract, self.current, [self.checks, self.ac, self.review], [])
            self.assertGate(result, "BLOCKED", "Independent ac receipt has no provider session id")
            self.assertGate(result, "BLOCKED", "Independent review receipt has no provider session id")

    def test_changed_execution_plan_requires_fresh_receipts(self):
        self.checks["plan_hash"] = "old-plan"
        with patch("ase.gates.verify_receipt", side_effect=self.judgment):
            result = evaluate_gate(self.contract, self.current, [self.checks, self.ac, self.review], [])
        self.assertGate(result, "REJECTED", "Stale check evidence: plan_hash")

    def test_p2_and_missing_mandatory_coverage_reject(self):
        def finding(receipt, **kwargs):
            result = self.judgment(receipt)
            if receipt["role"] == "review":
                result["result"]["payload"]["findings"] = [{"severity": "P2", "path": "result", "line": 1, "message": "Requirement missing"}]
            return result
        with patch("ase.gates.verify_receipt", side_effect=finding):
            result = evaluate_gate(self.contract, self.current, [self.checks, self.ac, self.review], [])
        self.assertGate(result, "REJECTED", "P2: Requirement missing")
        self.contract["acceptance"][0]["evidence_ids"] = []
        self.current["contract_hash"] = hash_value(self.contract)
        self.checks["contract_hash"] = self.current["contract_hash"]
        with patch("ase.gates.verify_receipt", side_effect=self.judgment):
            result = evaluate_gate(self.contract, self.current, [self.checks, self.ac, self.review], [])
        self.assertGate(result, "BLOCKED", "Mandatory AC has no executable/evaluable evidence mapping: AC1")

    def test_unverified_assertion_never_overrides_actual_proof(self):
        self.checks["results"][0]["confirmed"] = False
        with patch("ase.gates.verify_receipt", side_effect=self.judgment):
            result = evaluate_gate(self.contract, self.current, [self.checks, self.ac, self.review], [])
        self.assertGate(result, "BLOCKED", "Mandatory AC proof missing/failed: AC1/manual")

    def test_extra_unverified_requirement_cannot_be_silently_ignored(self):
        def extra(receipt, **kwargs):
            value = self.judgment(receipt)
            if receipt["role"] == "ac":
                value["result"]["payload"]["criteria"].append({"id": "request_constraints", "status": "UNVERIFIED"})
            return value
        with patch("ase.gates.verify_receipt", side_effect=extra):
            result = evaluate_gate(self.contract, self.current, [self.checks, self.ac, self.review], [])
        self.assertEqual(result["status"], "BLOCKED")


class VerificationContextTests(unittest.TestCase):
    """Real observer/Runner receipts; the semantic-kind seam is protocol-only.

    Removing context comparison must fail the stale-proof and scope tests.
    These deterministic judgments never establish independent acceptance.
    """
    def setUp(self):
        import json
        import sys
        import tempfile
        from pathlib import Path
        from ase.discovery import discover_capabilities, record_discovery
        from ase.runner import Runner
        from ase.store import Store
        from ase.tasks import TaskService
        from ase.workflow import WorkflowController
        from test_runner import FIXTURE
        temporary = tempfile.TemporaryDirectory(prefix="ase-vcontext-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.repo = self.base / "w"
        self.repo.mkdir()
        self.remote = self.base / "external-identity"
        self.remote.write_text("required-resource")
        (self.repo / "proof.txt").write_text("Shared current guide")
        (self.repo / "b.txt").write_text("Required only by stage b")
        self.store = Store(self.repo, self.base / "s")
        self.addCleanup(self.store.close)
        self.tasks = TaskService(self.store)
        scope = {"resources": ["proof.txt"], "effects": ["file_write", "command"]}
        action = {"id": "deliver", "argv": [sys.executable, "-c", "raise AssertionError('Existing effect must not replay')"],
            "observation_argv": [sys.executable, "-c", "from pathlib import Path;import json;print(json.dumps({'exists':True,'resource_id':Path(" + repr(str(self.remote)) + ").read_text()}))"]}
        contract = {"id": "context", "schema_version": 2, "profile": "document", "risk": "LOW",
            "request": "Review both stages and the required external resource.", "scope": scope,
            "acceptance": [{"id": name, "expected": name + " is valid", "evidence_ids": ["proof"]} for name in ("A", "B")],
            "evidence": [{"id": "proof", "kind": "artifact", "path": "proof.txt"}],
            "stages": [{"id": "a", "scope": scope, "acceptance_ids": ["A"], "depends_on": []},
                {"id": "b", "scope": scope, "acceptance_ids": ["B"], "depends_on": [], "actions": [action]}]}
        task = self.tasks.create(contract)
        task = self.tasks.propose("context", {"summary": contract["request"], "stage_ids": ["a", "b"]}, expected_revision=task["revision"])
        self.task = self.tasks.approve("context", proposal_hash=task["proposal_hash"], user_message_ref="test:disposable-context", approved_stage_ids=["a", "b"])
        discovery = discover_capabilities(self.store.project, scope={"resources": ["proof.txt"], "dependencies": [
            {"path": "b.txt", "reason": "Stage b authoritative input", "blocking_stage_ids": ["b"]}]}, backend_config={})
        record_discovery(self.store, "context", discovery, expected_revision=0)
        trusted = self.base / "provider"
        trusted.mkdir()
        fixture = trusted / "provider.py"
        body = """
request = json.loads(input_text.split('REQUEST_JSON:\\n',1)[1])
role = request['role']
if role == 'implementation':
 payload = {'edits':[],'summary':'Protocol-only empty candidate'}
elif role == 'ac':
 payload = {'criteria':[{'id':name,'expected':name+' is valid','status':'PASS','evidence':['Protocol fixture'],
  'method':'Deterministic boundary fixture','limitations':['No semantic AC'],'missing_coverage':[]} for name in ('A','B')]}
else:
 payload = {'findings':[],'summary':'Protocol-only boundary fixture, not semantic review'}
"""
        fixture.write_text(FIXTURE.replace("payload = {'verdict':'PASS'}", body))
        self.runner = Runner({"executable": sys.executable, "executable_args": [str(fixture), "success", "codex"],
            "trusted_cwd": str(trusted), "protocol_only": True}, self.store.root / "tasks/context/sessions")
        self.controller = WorkflowController(self.store, runner=self.runner)
        self.stage_b = self.task["contract"]["stages"][1]
        self.controller.execute_actions("context", self.stage_b)
        self.controller.check("context")

    def judgments(self, stage_id, *, legacy=False):
        from ase.adapters import get_adapter
        from ase.workflow import AC_SCHEMA, REVIEW_SCHEMA
        self.requests = []
        for role, schema in (("ac", AC_SCHEMA), ("review", REVIEW_SCHEMA)):
            request = self.controller._request("context", role, schema, {"stage_id": stage_id})
            if legacy:
                request["bindings"].pop("verification_context_sha256", None)
            self.requests.append(request)
            receipt = self.runner.run(get_adapter("codex"), request, timeout=5)["receipt"]
            self.assertEqual(receipt["status"], "COMPLETE")
            self.controller._save("verification", "context:" + role, receipt)
        return self.requests

    def gate_at_protocol_judgment_boundary(self, stage_id):
        from ase.gates import GateService
        from ase.runner import verify_receipt
        def protocol_only_semantic_kind_seam(receipt, **kwargs):
            verified = verify_receipt(receipt, **kwargs)
            self.assertEqual(verified["evidence_kind"], "protocol_only")
            return {**verified, "evidence_kind": "independent_cli"}
        with patch("ase.gates.verify_receipt", side_effect=protocol_only_semantic_kind_seam):
            return GateService(self.store).gate("context", stage_id)

    def capture_protocol_stage(self, stage_id):
        from ase.adapters import get_adapter
        from ase.workflow import EDIT_SCHEMA
        stage = next(s for s in self.task["contract"]["stages"] if s["id"] == stage_id)
        receipt = self.runner.run(get_adapter("codex"), self.controller._request("context", "implementation", EDIT_SCHEMA,
            {"stage": stage}), timeout=5)["receipt"]
        self.controller._save("stage_implementation", "context:" + stage_id, receipt)
        self.judgments(stage_id)
        verdict = self.gate_at_protocol_judgment_boundary(stage_id)
        self.assertEqual(verdict["status"], "APPROVED", verdict)
        # Historical scheduler marker only; protocol receipts cannot approve.
        self.controller._capture_stage("context", stage_id, verdict)

    def test_changed_action_observation_requires_new_judgments(self):
        from ase.gates import GateService
        from ase.state import read_record
        self.judgments("b")
        self.assertEqual(GateService(self.store).gate("context", "b")["status"], "BLOCKED")
        self.assertEqual(self.gate_at_protocol_judgment_boundary("b")["status"], "APPROVED")
        previous = self.requests[0]["bindings"].copy()
        checks = read_record(self.store.db, "checks", "context")
        self.remote.write_text("different-resource")
        self.controller.execute_actions("context", self.stage_b)
        self.assertEqual(read_record(self.store.db, "checks", "context"), checks)
        self.assertEqual(self.gate_at_protocol_judgment_boundary("b")["status"], "BLOCKED")
        self.judgments("b")
        self.assertNotEqual(previous, self.requests[0]["bindings"])
        self.assertEqual(self.gate_at_protocol_judgment_boundary("b")["status"], "APPROVED")

    def test_stage_judgments_cannot_authorize_other_or_broader_gate(self):
        self.capture_protocol_stage("a")
        self.capture_protocol_stage("b")
        requests = self.judgments("a")
        self.assertNotIn("b.txt", requests[0]["source_packet"]["coverage"]["included"])
        self.assertEqual(self.gate_at_protocol_judgment_boundary("a")["status"], "APPROVED")
        self.assertEqual(self.gate_at_protocol_judgment_boundary("b")["status"], "BLOCKED")
        self.assertEqual(self.gate_at_protocol_judgment_boundary(None)["status"], "BLOCKED")

    def test_fresh_whole_and_exact_stage_contexts_remain_valid(self):
        self.capture_protocol_stage("a")
        self.capture_protocol_stage("b")
        self.judgments(None)
        self.assertIn("b.txt", self.requests[0]["source_packet"]["coverage"]["included"])
        self.assertEqual(self.gate_at_protocol_judgment_boundary(None)["status"], "APPROVED")
        self.judgments("b")
        self.assertEqual(self.gate_at_protocol_judgment_boundary("b")["status"], "APPROVED")

    def test_whole_task_judgments_authorize_current_included_stage(self):
        self.capture_protocol_stage("a")
        self.capture_protocol_stage("b")
        self.judgments(None)
        self.assertEqual(self.gate_at_protocol_judgment_boundary(None)["status"], "APPROVED")
        self.assertEqual(self.gate_at_protocol_judgment_boundary("a")["status"], "APPROVED")
        self.assertEqual(self.gate_at_protocol_judgment_boundary("b")["status"], "APPROVED")

    def test_changed_whole_action_context_refuses_stale_whole_stage_proof(self):
        self.capture_protocol_stage("a")
        self.capture_protocol_stage("b")
        self.judgments(None)
        self.assertEqual(self.gate_at_protocol_judgment_boundary("b")["status"], "APPROVED")
        self.remote.write_text("different-resource")
        self.controller.execute_actions("context", self.stage_b)
        self.assertEqual(self.gate_at_protocol_judgment_boundary("b")["status"], "BLOCKED")
        self.judgments(None)
        self.assertEqual(self.gate_at_protocol_judgment_boundary("b")["status"], "APPROVED")

    def test_unrelated_stage_observation_does_not_stale_selected_stage(self):
        self.judgments("a")
        self.remote.write_text("different-resource")
        self.controller.execute_actions("context", self.stage_b)
        self.assertEqual(self.gate_at_protocol_judgment_boundary("a")["status"], "APPROVED")

    def test_historical_receipts_without_context_still_validate(self):
        from ase.runner import verify_receipt
        from ase.state import read_record
        self.judgments("a", legacy=True)
        for role in ("ac", "review"):
            receipt = read_record(self.store.db, "verification", "context:" + role)["payload"]
            self.assertTrue(verify_receipt(receipt, require_current_policy=False)["valid"])


if __name__ == "__main__":
    unittest.main()
