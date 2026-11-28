"""DeepSeek protocol coverage against a loopback fake API; no real key or network required."""
import json
import os
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from test_runner import TransportFixture
from ase.adapters import get_adapter
from ase.adapters import deepseek_worker
from ase.adapters.deepseek import DeepSeekAdapter, worker_args
from ase.protocol import packet_prompt
from ase.runner import Runner, verify_receipt

FAKE_KEY = "fake-test-key-0123456789"


class FakeApi(BaseHTTPRequestHandler):
    reply = None
    seen = []

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        FakeApi.seen.append({"path": self.path, "authorization": self.headers.get("Authorization"),
                             "body": json.loads(body)})
        status, payload = FakeApi.reply
        data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


def completion(content, finish_reason="stop"):
    return {"id": "cmpl-fixture-1", "model": "deepseek-flash", "usage": {"prompt_tokens": 10},
            "choices": [{"finish_reason": finish_reason, "message": {"role": "assistant", "content": content}}]}


class DeepSeekAdapterTests(TransportFixture):
    def setUp(self):
        super().setUp()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeApi)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        FakeApi.seen = []
        FakeApi.reply = (200, completion(json.dumps({"verdict": "PASS"})))

    def deepseek_config(self, **extra):
        return {"executable": sys.executable, "executable_args": worker_args(), "trusted_cwd": str(self.trusted),
                "base_url": f"http://127.0.0.1:{self.server.server_address[1]}", **extra}

    def run_deepseek(self, key=FAKE_KEY, **extra):
        environment = {deepseek_worker.KEY_VARIABLE: key} if key else {}
        # Never the owner's real key: the key held at import is hidden for every test run.
        with patch.dict("ase.child_environment._HELD", {}, clear=True), patch.dict(os.environ, environment):
            if not key:
                os.environ.pop(deepseek_worker.KEY_VARIABLE, None)
            return Runner(self.deepseek_config(**extra), self.base / "artifacts").run(
                DeepSeekAdapter(), self.request(), timeout=30)

    def artifact_bytes(self, outcome):
        root = Path(outcome["receipt"]["artifact_root"])
        return b"".join(path.read_bytes() for path in root.rglob("*") if path.is_file())

    def test_a_schema_answer_completes_and_the_packet_travels_on_stdin(self):
        outcome = self.run_deepseek()
        self.assertEqual(outcome["result"]["status"], "COMPLETE", outcome)
        self.assertEqual(outcome["result"]["payload"], {"verdict": "PASS"})
        self.assertEqual(outcome["result"]["session_id"], "cmpl-fixture-1")
        self.assertEqual(get_adapter("deepseek").name, "deepseek")
        sent = FakeApi.seen[0]
        self.assertEqual((sent["path"], sent["authorization"]), ("/chat/completions", "Bearer " + FAKE_KEY))
        self.assertEqual(sent["body"]["model"], "deepseek-flash")
        self.assertEqual(sent["body"]["response_format"], {"type": "json_object"})
        self.assertIn('"verdict"', sent["body"]["messages"][0]["content"])  # the schema reaches the model
        captured = json.loads(Path(outcome["receipt"]["artifacts"]["request"]["path"]).read_bytes())
        self.assertEqual(sent["body"]["messages"][1]["content"], packet_prompt(captured))
        self.assertNotIn(packet_prompt(captured), outcome["receipt"]["argv"])
        self.assertTrue(verify_receipt(outcome["receipt"])["valid"])
        self.assertEqual(outcome["receipt"]["capabilities"]["auth_availability"], "key-present")
        self.assertNotIn(FAKE_KEY.encode(), self.artifact_bytes(outcome))  # never in argv, receipt or output

    def test_the_configured_model_is_used(self):
        self.run_deepseek(model="deepseek-v4-pro")
        self.assertEqual(FakeApi.seen[0]["body"]["model"], "deepseek-v4-pro")

    def test_an_api_refusal_blocks_and_never_echoes_the_key(self):
        FakeApi.reply = (401, {"error": {"message": "invalid key " + FAKE_KEY}})
        outcome = self.run_deepseek()
        self.assertEqual((outcome["result"]["status"], outcome["result"]["blocker"]), ("BLOCKED", "BACKEND_ERROR"))
        self.assertEqual(outcome["result"]["terminal"]["status"], 401)
        self.assertIn("[REDACTED]", outcome["result"]["terminal"]["error"])
        self.assertNotIn(FAKE_KEY.encode(), self.artifact_bytes(outcome))

    def test_truncated_or_non_json_answers_block(self):
        FakeApi.reply = (200, completion('{"verdict": "PA', finish_reason="length"))
        self.assertEqual(self.run_deepseek()["result"]["blocker"], "BACKEND_ERROR")
        FakeApi.reply = (200, completion("PASS"))
        self.assertEqual(self.run_deepseek()["result"]["blocker"], "STRUCTURED_OUTPUT_INVALID")
        FakeApi.reply = (200, completion(json.dumps({"verdict": True})))
        self.assertEqual(self.run_deepseek()["result"]["blocker"], "SCHEMA_INVALID")
        FakeApi.reply = (200, b"<html>gateway</html>")
        self.assertEqual(self.run_deepseek()["result"]["blocker"], "BACKEND_ERROR")

    def test_without_a_key_nothing_is_sent(self):
        outcome = self.run_deepseek(key=None)
        self.assertEqual((outcome["result"]["status"], outcome["result"]["blocker"]), ("BLOCKED", "API_KEY_MISSING"))
        self.assertEqual(outcome["receipt"]["capabilities"]["auth_availability"], "key-missing")
        self.assertEqual(FakeApi.seen, [])

    def test_plain_http_is_refused_beyond_loopback(self):
        with self.assertRaisesRegex(ValueError, "loopback"):
            deepseek_worker._connection("http://api.example.invalid")
        with self.assertRaisesRegex(ValueError, "invalid base URL"):
            deepseek_worker._connection("https://user:secret@api.example.invalid")
        self.assertEqual(deepseek_worker._connection(deepseek_worker.DEFAULT_BASE_URL).host, "api.deepseek.com")

    def test_only_the_verifier_roles_may_run_on_deepseek(self):
        import copy
        from ase.tasks import validate_contract_v2
        base = {"id": "t", "schema_version": 2, "profile": "document", "request": "r",
                "scope": {"resources": ["doc.md"], "effects": ["file_write"]}, "sources": [],
                "acceptance": [{"id": "A", "expected": "e", "evidence_ids": ["m"]}],
                "evidence": [{"id": "m", "kind": "manual", "required": True}], "unknowns": [],
                "stages": [{"id": "s", "acceptance_ids": ["A"]}]}
        verifier = copy.deepcopy(base)
        verifier["execution_policy"] = {"role_backends": {"ac": "deepseek", "review": "deepseek"}}
        self.assertEqual(validate_contract_v2(verifier)["execution_policy"]["role_backends"]["review"], "deepseek")
        host = copy.deepcopy(base)
        host["execution_policy"] = {"backend": "deepseek"}  # implementation needs tools DeepSeek does not have
        with self.assertRaises(ValueError):
            validate_contract_v2(host)
        request = self.request(role="implementation")
        caps = {"available": True, "argv_prefix": ["python"], "trusted_cwd": "x", "artifact_paths": {"schema": "s"}}
        with self.assertRaisesRegex(ValueError, "DEEPSEEK_ROLE_UNSUPPORTED"):
            DeepSeekAdapter().build_invocation(request, caps)

    def test_a_swapped_worker_or_foreign_host_never_receives_the_key(self):
        other = self.base / "other_worker.py"
        other.write_bytes(deepseek_worker.__file__ and Path(deepseek_worker.__file__).read_bytes())
        outcome = self.run_deepseek(executable_args=["-I", str(other)])
        self.assertEqual(outcome["result"]["blocker"], "WORKER_NOT_PINNED")
        outcome = self.run_deepseek(base_url="https://api.example.invalid")
        self.assertEqual(outcome["result"]["blocker"], "BASE_URL_NOT_ALLOWED")
        self.assertEqual(FakeApi.seen, [])
        # A receipt whose argv names another script than the recorded worker does not verify.
        receipt = self.run_deepseek()["receipt"]
        receipt["capabilities"]["worker"] = str(other)
        from ase.protocol import canonical
        (Path(receipt["artifact_root"]) / "receipt.json").write_bytes(canonical(receipt))
        with self.assertRaisesRegex(ValueError, "delivered prompt"):
            verify_receipt(receipt)

    def test_malformed_or_oversized_bodies_are_result_lines_not_tracebacks(self):
        for body in ([], {"choices": ["x"]}, {"choices": [{"message": "x"}]}):
            FakeApi.reply = (200, body)
            outcome = self.run_deepseek()
            with self.subTest(body=body):
                self.assertEqual(outcome["result"]["blocker"], "BACKEND_ERROR")
                self.assertEqual(outcome["result"]["terminal"]["subtype"], "malformed_response")
        FakeApi.reply = (200, b"x" * (deepseek_worker.MAX_RESPONSE_BYTES + 10))
        outcome = self.run_deepseek()
        self.assertEqual(outcome["result"]["terminal"]["subtype"], "response_too_large")

    def test_encoded_forms_of_the_key_are_redacted(self):
        import base64
        from urllib.parse import quote
        key = FAKE_KEY + "/x"
        encoded = base64.b64encode(key.encode()).decode()
        text = deepseek_worker._redacted("a " + quote(key, safe="") + " b " + encoded, key)
        self.assertEqual(text, "a [REDACTED] b [REDACTED]")

    def test_only_the_deepseek_worker_inherits_the_key(self):
        from ase.child_environment import backend_environment, child_environment
        with patch.dict("ase.child_environment._HELD", {}, clear=True),                 patch.dict(os.environ, {deepseek_worker.KEY_VARIABLE: FAKE_KEY}):
            self.assertNotIn(deepseek_worker.KEY_VARIABLE, child_environment())
            self.assertNotIn(deepseek_worker.KEY_VARIABLE, backend_environment("claude"))
            self.assertEqual(backend_environment("deepseek")[deepseek_worker.KEY_VARIABLE], FAKE_KEY)
            from ase.runner import capture_process
            probe = capture_process([sys.executable, "-c", "import os,sys; sys.stdout.write(str(os.environ.get('"
                                     + deepseek_worker.KEY_VARIABLE + "')))"], str(self.trusted),
                                    timeout=30, max_output_bytes=4096)
            self.assertEqual(probe["stdout"], b"None")  # a test command or another backend sees no key
            from ase.core import execute
            log = self.base / "logs" / "probe.log"
            execute([sys.executable, "-c", "import os; print('key=' + str(os.environ.get('"
                     + deepseek_worker.KEY_VARIABLE + "')))"], str(self.trusted), log, timeout=30)
            self.assertIn("key=None", log.read_text(encoding="utf-8", errors="replace"))
            self.assertNotIn(FAKE_KEY, log.read_text(encoding="utf-8", errors="replace"))
            from ase.access import _env
            self.assertNotIn(deepseek_worker.KEY_VARIABLE, _env({"env": {}, "secrets": {}}))

    def test_importing_ase_moves_the_key_out_of_the_process_environment(self):
        script = ("import os; os.environ['" + deepseek_worker.KEY_VARIABLE + "'] = 'k' * 20; import ase; "
                  "from ase.child_environment import backend_environment, backend_key_present; "
                  "print(os.environ.get('" + deepseek_worker.KEY_VARIABLE + "'), backend_key_present('deepseek'), "
                  "backend_environment('deepseek')['" + deepseek_worker.KEY_VARIABLE + "'] == 'k' * 20)")
        import subprocess
        done = subprocess.run([sys.executable, "-c", script], cwd=str(Path(__file__).resolve().parents[1]),
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(done.stdout.split(), ["None", "True", "True"], done.stderr)

    def test_protocol_fixtures_and_foreign_interpreters_never_receive_the_key(self):
        outcome = self.run_deepseek(protocol_only=True)
        self.assertFalse(outcome["receipt"]["capabilities"]["key_delivery"])
        self.assertEqual(outcome["result"]["blocker"], "API_KEY_MISSING")  # the fixture worker got no key
        self.assertEqual(FakeApi.seen, [])
        outcome = self.run_deepseek(protocol_only=True, base_url="https://api.example.invalid")
        self.assertEqual(outcome["result"]["blocker"], "BASE_URL_NOT_ALLOWED")
        with patch("ase.adapters.deepseek._interpreter", return_value=str(self.base / "another-python")),                 patch.dict("ase.child_environment._HELD", {}, clear=True),                 patch.dict(os.environ, {deepseek_worker.KEY_VARIABLE: FAKE_KEY}):
            caps = DeepSeekAdapter().probe(self.deepseek_config())  # this interpreter is now a foreign one
        self.assertEqual(caps["blocker"], "WORKER_NOT_PINNED")
        self.assertFalse(caps["key_delivery"])

    def test_the_workflow_default_runs_the_bundled_worker(self):
        from ase.adapters.deepseek import WORKER
        self.assertEqual(worker_args(), ["-I", str(WORKER)])


if __name__ == "__main__":
    unittest.main()
