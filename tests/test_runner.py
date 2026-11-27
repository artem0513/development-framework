"""Protocol-only subprocess fixtures; these are not independent model judgments."""
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest

from ase.runner import Runner, verify_receipt
from ase.adapters.codex import CodexAdapter
from ase.protocol import validate_payload


SCHEMA = {"type": "object", "properties": {"verdict": {"type": "string", "enum": ["PASS", "FAIL"]}},
          "required": ["verdict"], "additionalProperties": False}

FIXTURE = r'''
import json, os, pathlib, subprocess, sys, time, uuid
case, backend, *args = sys.argv[1:]
with pathlib.Path('invocations.jsonl').open('a') as log:
 log.write(json.dumps(args) + '\n')
def dump(value):
 return json.dumps(value).replace('"OVERFLOW_EXPONENT"', '1e999')
flags = {
 "codex": "--ignore-user-config --ephemeral --sandbox --skip-git-repo-check --cd --output-schema --output-last-message --json",
 "claude": "--safe-mode --tools --strict-mcp-config --mcp-config --no-session-persistence --output-format --json-schema",
 "copilot": "--output-format --no-custom-instructions --disable-builtin-mcps --no-auto-update --no-ask-user --no-experimental --no-bash-env --deny-tool"
}
if args == ['--version']:
 if case in {'cancel-version','slow-probes','shared-budget','fitting-budget'}:
  if case == 'cancel-version': pathlib.Path('probe.ready').touch()
  time.sleep(0.35 if case in {'cancel-version','slow-probes'} else 0.1 if case == 'shared-budget' else 0.02)
 print(backend + ' protocol-fixture-1'); sys.exit()
if args == (['exec','--help'] if backend == 'codex' else ['--help']):
 if case in {'cancel-help','slow-probes','shared-budget','fitting-budget'}:
  if case == 'cancel-help': pathlib.Path('probe.ready').touch()
  time.sleep(0.35 if case in {'cancel-help','slow-probes'} else 0.1 if case == 'shared-budget' else 0.02)
 print(flags[backend]); sys.exit()
if case == 'close-stdin':
 sys.stdin.close(); os.close(0); time.sleep(0.5); input_text = 'REQUEST (unread)'
else:
 input_text = sys.stdin.read()
stdin_backend = backend == 'codex' or (backend == 'claude' and case != 'legacy-argv')
assert bool(input_text) == stdin_backend, 'inherited or missing input'
if stdin_backend: assert 'REQUEST' in input_text, 'prompt not on stdin'
canary = pathlib.Path('.backend-startup.py')
if canary.exists(): exec(canary.read_text())
if case == 'sleep':
 child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
 pathlib.Path('descendant.pid').write_text(str(child.pid))
 time.sleep(30)
if case == 'flood':
 while True: print('x' * 4096, flush=True)
if case in {'shared-budget','fitting-budget'}: time.sleep(0.15 if case == 'shared-budget' else 0.02)
payload = {'verdict':'PASS'}
if case == 'large-integer': payload = {'verdict':10**400}
if case == 'overflow-float': payload = {'verdict':'OVERFLOW_EXPONENT'}
if case == 'schema': payload = {'verdict':True}
if case == 'numeric-mismatch': payload = {'count':1}
if case == 'coverage': payload = {'verdict':'PASS','missing_coverage':['service integration not observed']}
if case == 'plain': print('PASS'); sys.exit()
sid = str(uuid.uuid4())
if backend == 'codex':
 expected = ['exec','--ignore-user-config','--ephemeral','--sandbox','read-only','--skip-git-repo-check','--cd',os.getcwd(),'--output-schema',args[9],'--output-last-message',args[11],'--json','-']
 assert args == expected, repr(args)
 assert json.loads(pathlib.Path(args[9]).read_text())['type'] == 'object'
 pathlib.Path(args[11]).write_text(dump(payload))
 if case == 'overfinal': pathlib.Path(args[11]).write_bytes(b'x' * 65536)
 print(json.dumps({'type':'thread.started','thread_id':sid}))
 print(json.dumps({'type':'turn.started'}))
 malformed = {'malformed-item-null':{'type':'item.completed','item':None},
              'malformed-item-list':{'type':'item.completed','item':[]},
              'malformed-item-type':{'type':'item.completed','item':{'type':[]}},
              'malformed-event-null':{'type':None}, 'malformed-event-list':{'type':[]}}
 if case in malformed: print(json.dumps(malformed[case]))
 if case == 'tool': print(json.dumps({'type':'item.completed','item':{'type':'command_execution','command':'echo unsafe','exit_code':0}}))
 message = {'verdict':'FAIL'} if case == 'mismatch' else {'count':True} if case == 'numeric-mismatch' else payload
 print(dump({'type':'item.completed','item':{'type':'agent_message','text':dump(message)}}))
 if case != 'truncated': print(json.dumps({'type':'turn.completed','usage':{}}))
elif backend == 'claude':
 expected = ['--safe-mode','--tools','','--strict-mcp-config','--mcp-config','{"mcpServers":{}}','--no-session-persistence','--output-format','json','--json-schema',args[10],'-p', *(args[-1:] if case == 'legacy-argv' else [])]
 assert args == expected, repr(args)
 result = {'type':'result','subtype':'success','is_error':False,'session_id':sid,'result':'PASS','permission_denials':[]}
 if case != 'missing': result['structured_output'] = payload
 if case == 'denied': result['permission_denials'] = [{'tool_name':'Bash','tool_input':{'command':'write'}}]
 print(dump(result))
else:
 expected = ['--output-format','json','--no-custom-instructions','--disable-builtin-mcps','--no-auto-update','--no-ask-user','--no-experimental','--no-bash-env','--deny-tool','shell,write,read,url,memory','-p',args[-1]]
 assert args == expected, repr(args)
 print(json.dumps({'type':'assistant.message','data':{'content':json.dumps(payload)}}))
 if case != 'truncated': print(dump({'type':'unknown.result' if case == 'unknown' else 'result','subtype':'success','is_error':False,'session_id':sid,'result':dump(payload)}))
 if case == 'malformed': print('{bad json')
if case == 'exit': sys.exit(9)
'''


class TransportFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ase-transport-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.trusted = self.base / "trusted"
        self.trusted.mkdir()
        self.target = self.base / "target"
        self.target.mkdir()
        self.fixture = self.base / "fixture.py"
        self.fixture.write_text(FIXTURE, encoding="utf-8")

    def config(self, backend="codex", case="success", **extra):
        return {"executable": sys.executable, "executable_args": [str(self.fixture), case, backend],
                "trusted_cwd": str(self.trusted), "protocol_only": True, **extra}

    def request(self, role="ac"):
        return {"role": role, "project": {"root": str(self.target)}, "schema": SCHEMA,
                "bindings": {"fingerprint": "a" * 64, "contract_sha256": "b" * 64,
                             "proposal_sha256": "c" * 64, "plan_sha256": "d" * 64, "check_sha256": "e" * 64},
                "source_packet": {"requirements": ["Return one real verdict"],
                    "files": [{"path": "app.py", "content": "value = 1\n",
                               "sha256": "585c93666fcb046b7b264d3fa73202aa2a38254ae82a4b3ba19e873c2d5a9886"}],
                    "coverage": {"scope": "app.py fixture", "included": ["app.py"], "missing": []}}}

    def run_backend(self, adapter=None, backend="codex", case="success", **config):
        return Runner(self.config(backend, case, **config), self.base / "artifacts").run(
            adapter or CodexAdapter(), self.request(), timeout=3)

    def request_with_prompt_length(self, characters):
        """A request whose complete policy-bound prompt has exactly the given length."""
        from ase.protocol import packet_prompt
        from ase.runtime_policy import bind_policy
        def sized(body):
            request = self.request()
            request["source_packet"]["files"][0].update(content=body, sha256=hashlib.sha256(body.encode()).hexdigest())
            return request
        base = len(packet_prompt(bind_policy(sized(""))))
        request = sized("x" * (characters - base))
        self.assertEqual(len(packet_prompt(bind_policy(request))), characters)
        return request

    def assert_no_execution_artifacts(self, artifacts):
        self.assertEqual([path for path in artifacts.rglob("*") if path.is_dir()] if artifacts.exists() else [], [])
        self.assertEqual(list(artifacts.rglob("request.json")) if artifacts.exists() else [], [])


class RunnerTests(TransportFixture):
    def test_verified_identity_comes_from_captured_purpose_schema_stage_and_plan(self):
        request = self.request('review')
        request['project']['project_id'] = 'fixture-project'
        request['handoff'] = {'purpose': 'test_adequacy', 'stage_id': 'implement', 'test_plan_sha256': 'f' * 64}
        run = Runner(self.config(), self.base / 'artifacts').run(CodexAdapter(), request, timeout=3)
        identity = verify_receipt(run['receipt']).get('request_identity')
        self.assertEqual(identity, {'role': 'review', 'purpose': 'test_adequacy', 'stage_id': 'implement',
            'schema_sha256': hashlib.sha256(json.dumps(SCHEMA, sort_keys=True, ensure_ascii=False,
                separators=(',', ':')).encode()).hexdigest(), 'project_id': 'fixture-project', 'test_plan_sha256': 'f' * 64})

    def test_receipt_requires_real_subprocess(self):
        outcome = self.run_backend()
        self.assertEqual(outcome["result"]["status"], "COMPLETE", outcome)
        receipt = outcome["receipt"]
        self.assertGreater(receipt["pid"], 0)
        self.assertEqual(receipt["exit_code"], 0)
        self.assertEqual(receipt["evidence_kind"], "protocol_only")
        self.assertTrue(verify_receipt(receipt, expected_bindings=self.request()["bindings"])["valid"])
        with self.assertRaises(ValueError): verify_receipt({"status":"PASS"})

    def test_two_fresh_roles_and_current_inputs(self):
        runner = Runner(self.config(), self.base / "artifacts")
        runs = [runner.run(CodexAdapter(), self.request(role), timeout=3) for role in ("implementation", "ac", "review")]
        self.assertEqual(len({r["receipt"]["execution_id"] for r in runs}), 3)
        self.assertEqual(len({r["receipt"]["session_id"] for r in runs}), 3)
        wrong = {**self.request()["bindings"], "fingerprint": "f" * 64}
        with self.assertRaises(ValueError): verify_receipt(runs[1]["receipt"], expected_bindings=wrong)

    def test_changed_output_or_request_hash_is_rejected(self):
        receipt = self.run_backend()["receipt"]
        path = Path(receipt["artifacts"]["stdout"]["path"])
        path.write_bytes(path.read_bytes() + b"\n")
        with self.assertRaises(ValueError): verify_receipt(receipt)
        receipt = self.run_backend()["receipt"]
        request_path = Path(receipt["artifacts"]["request"]["path"])
        request_path.write_text('{}', encoding="utf-8")
        with self.assertRaises(ValueError): verify_receipt(receipt)

    def test_plain_pass_nonzero_schema_and_truncated_stream_block(self):
        for case in ("plain", "exit", "schema", "truncated", "mismatch", "tool"):
            with self.subTest(case=case):
                outcome = self.run_backend(case=case)
                self.assertEqual(outcome["result"]["status"], "BLOCKED", outcome)
                with self.assertRaises(ValueError): verify_receipt(outcome["receipt"])

    def test_timeout_and_output_cap_bound_real_children(self):
        for case, config in (("sleep", {}), ("flood", {"max_output_bytes": 8192})):
            with self.subTest(case=case):
                result = self.run_backend(case=case, **config)
                receipt = result["receipt"]
                self.assertEqual(result["result"]["status"], "BLOCKED")
                self.assertTrue(receipt["timed_out"] if case == "sleep" else receipt["output_limited"])
                self.assertLess(receipt["duration_seconds"], 8)
                self.assertLessEqual(Path(receipt["artifacts"]["stdout"]["path"]).stat().st_size, config.get("max_output_bytes", 1048576))
                if case == 'sleep':
                    pid = int((self.trusted / 'descendant.pid').read_text())
                    if os.name == 'nt':
                        import ctypes
                        from ctypes import wintypes
                        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
                        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
                        kernel.OpenProcess.restype = wintypes.HANDLE
                        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
                        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
                        handle = kernel.OpenProcess(0x1000, False, pid)
                        if handle:
                            try:
                                code = wintypes.DWORD()
                                self.assertTrue(kernel.GetExitCodeProcess(handle, ctypes.byref(code)))
                                self.assertNotEqual(code.value, 259, 'timed out descendant remains active')
                            finally: kernel.CloseHandle(handle)
                    else:
                        status = Path(f'/proc/{pid}/stat')
                        if status.exists(): self.assertEqual(status.read_text().split()[2], 'Z')

    def test_source_packet_hash_coverage_and_size_are_required(self):
        for change in ("hash", "coverage", "size"):
            request = self.request()
            if change == "hash": request["source_packet"]["files"][0]["sha256"] = "0" * 64
            if change == "coverage": del request["source_packet"]["coverage"]
            if change == "size": request["handoff"] = "x" * 3000000
            with self.subTest(change=change), self.assertRaises(ValueError):
                Runner(self.config(), self.base / "artifacts").run(CodexAdapter(), request, timeout=3)

    def test_large_codex_packet_uses_owned_input_and_preserves_exact_hash(self):
        request = self.request()
        content = 'value = 1\n' * 6000
        request['source_packet']['files'][0].update(content=content, sha256=hashlib.sha256(content.encode()).hexdigest())
        result = Runner(self.config(), self.base / 'artifacts').run(CodexAdapter(), request, timeout=3)
        self.assertEqual(result['result']['status'], 'COMPLETE', result)
        receipt = result['receipt']
        prompt = Path(receipt['artifacts']['stdin']['path']).read_bytes()
        self.assertGreater(len(prompt), 40000)
        self.assertEqual(receipt['stdin_sha256'], hashlib.sha256(prompt).hexdigest())
        self.assertEqual(receipt['argv'][-1], '-')

    def test_schema_rejects_unsupported_constraints_and_bool_integer(self):
        self.assertEqual(validate_payload({"verdict":"PASS"}, SCHEMA)["status"], "VALID")
        self.assertEqual(validate_payload(1, {"type":"integer", "multipleOf":2})["status"], "BLOCKED")
        self.assertEqual(validate_payload(True, {"type":"integer"})["status"], "INVALID")
        self.assertEqual(validate_payload({}, {"type":"object", "properties":{"optional":{"$ref":"somewhere"}}})["status"], "BLOCKED")

    def test_diagnostic_role_uses_a_fresh_bounded_process(self):
        try:
            result = Runner(self.config(), self.base / 'artifacts').run(CodexAdapter(), self.request('diagnostic'), timeout=3)
        except ValueError as error:
            self.fail('Diagnostic role must be supported: ' + str(error))
        self.assertEqual(result['result']['status'], 'COMPLETE')
        self.assertEqual(result['receipt']['role'], 'diagnostic')

    def test_missing_absolute_cli_is_a_typed_blocker_without_role_process(self):
        config = self.config(executable=str(self.base / 'missing.exe'))
        try:
            result = Runner(config, self.base / 'artifacts').run(CodexAdapter(), self.request(), timeout=3)
        except ValueError as error:
            self.fail('Absent CLI must produce a typed unavailable result: ' + str(error))
        self.assertEqual(result['result']['blocker'], 'CLI_NOT_INSTALLED')
        self.assertIsNone(result['receipt']['pid'])

    def test_terminal_metadata_cannot_be_overridden(self):
        receipt = self.run_backend()['receipt']
        receipt['terminal'] = {'type':'turn.completed','fake':True}
        (Path(receipt['artifact_root']) / 'receipt.json').write_text(json.dumps(receipt), encoding='utf-8')
        with self.assertRaises(ValueError): verify_receipt(receipt)

    def test_structured_final_and_stream_cannot_disagree_on_json_type(self):
        request = self.request()
        request['schema'] = {'type':'object','properties':{'count':{'type':'integer'}},'required':['count'],'additionalProperties':False}
        result = Runner(self.config(case='numeric-mismatch'), self.base / 'artifacts').run(CodexAdapter(), request, timeout=3)
        self.assertEqual(result['result']['status'], 'BLOCKED')
        self.assertEqual(result['result']['blocker'], 'OUTPUT_MISMATCH')

    def test_model_missing_coverage_is_exposed_without_synthesizing_semantic_pass(self):
        request = self.request()
        request['schema'] = {'type':'object','properties':{'verdict':{'type':'string'},'missing_coverage':{'type':'array','items':{'type':'string'}}},'required':['verdict','missing_coverage'],'additionalProperties':False}
        result = Runner(self.config(case='coverage'), self.base / 'artifacts').run(CodexAdapter(), request, timeout=3)
        self.assertEqual(result['result']['missing_coverage'], ['service integration not observed'])
        self.assertEqual(result['result']['payload']['verdict'], 'PASS')
        # COMPLETE is a protocol result. Only the parent semantic gate may approve.
        self.assertEqual(result['result']['status'], 'COMPLETE')

    def test_oversized_final_artifact_is_bounded_even_after_fast_exit(self):
        result = self.run_backend(case='overfinal', max_output_bytes=8192)
        self.assertEqual(result['result']['status'], 'BLOCKED')
        self.assertTrue(result['receipt']['output_limited'])
        raw = Path(result['receipt']['artifact_root']) / 'final.json'
        self.assertLessEqual(raw.stat().st_size, 8192)


class RunnerCaptureRegressionTests(TransportFixture):
    """Actual provider-shaped subprocesses, never semantic model judgments."""
    def assert_captured(self, outcome, status):
        receipt = outcome['receipt']
        root = Path(receipt['artifact_root'])
        self.assertEqual(outcome['result']['status'], status, outcome['result']['blocker'])
        self.assertEqual(json.loads((root / 'receipt.json').read_bytes()), receipt)
        self.assertEqual(json.loads((root / 'result.json').read_bytes()), outcome['result'])
        for artifact in receipt['artifacts'].values():
            raw = Path(artifact['path']).read_bytes()
            self.assertEqual(len(raw), artifact['bytes'])
            self.assertEqual(hashlib.sha256(raw).hexdigest(), artifact['sha256'])
        if status == 'COMPLETE':
            self.assertTrue(verify_receipt(receipt)['valid'])
        else:
            with self.assertRaises(ValueError): verify_receipt(receipt)

    def adapters(self):
        from ase.adapters.claude import ClaudeAdapter
        from ase.adapters.copilot import CopilotAdapter
        return [('codex', CodexAdapter()), ('claude', ClaudeAdapter()), ('copilot', CopilotAdapter())]

    def test_child_that_closes_stdin_early_is_never_credited(self):
        for backend, adapter in (('codex', CodexAdapter()), ('claude', self.adapters()[1][1])):
            with self.subTest(backend=backend):
                request = self.request()
                body = 'y' * 600000
                request['source_packet']['files'][0].update(content=body, sha256=hashlib.sha256(body.encode()).hexdigest())
                outcome = Runner(self.config(backend, 'close-stdin'), self.base / ('artifacts-' + backend)).run(adapter, request, timeout=20)
                self.assertEqual(outcome['result']['status'], 'BLOCKED', outcome['result'])
                self.assertIn('not fully delivered', outcome['receipt']['error'] or '')

    def test_malformed_codex_shapes_keep_raw_and_blocked_receipts(self):
        for case in ('malformed-item-null', 'malformed-item-list', 'malformed-item-type',
                     'malformed-event-null', 'malformed-event-list'):
            with self.subTest(case=case):
                outcome = self.run_backend(case=case)
                self.assert_captured(outcome, 'BLOCKED')
                self.assertEqual(outcome['result']['blocker'], 'SESSION_PROTOCOL_INVALID')
                self.assertIn(b'turn.completed', Path(outcome['receipt']['artifacts']['stdout']['path']).read_bytes())

    def test_large_integer_and_large_bounds_round_trip_without_float_conversion(self):
        request = self.request()
        request['schema'] = {'type':'object', 'properties':{'verdict':{'type':'integer',
            'minimum':10**399, 'maximum':10**401}}, 'required':['verdict'], 'additionalProperties':False}
        outcome = Runner(self.config(case='large-integer'), self.base / 'artifacts').run(CodexAdapter(), request, timeout=3)
        self.assert_captured(outcome, 'COMPLETE')
        self.assertEqual(outcome['result']['payload']['verdict'], 10**400)

    def test_large_integer_wrong_type_is_a_captured_schema_failure(self):
        outcome = self.run_backend(case='large-integer')
        self.assert_captured(outcome, 'BLOCKED')
        self.assertEqual(outcome['result']['blocker'], 'SCHEMA_INVALID')

    def test_exponent_overflow_is_rejected_with_raw_output_and_receipt(self):
        for backend, adapter in self.adapters():
            with self.subTest(backend=backend):
                outcome = self.run_backend(adapter, backend, 'overflow-float')
                self.assert_captured(outcome, 'BLOCKED')
                self.assertIn(b'1e999', Path(outcome['receipt']['artifacts']['stdout']['path']).read_bytes())

    def test_pre_cancelled_capture_never_launches(self):
        from ase.runner import capture_process
        marker = self.trusted / 'unexpected-launch'
        outcome = capture_process([sys.executable, '-c', f'from pathlib import Path;Path({str(marker)!r}).touch()'],
            self.trusted, timeout=3, max_output_bytes=4096, cancel_requested=lambda: True)
        self.assertIsNone(outcome['pid'])
        self.assertTrue(outcome['cancelled'])
        self.assertFalse(marker.exists())

    def test_pre_cancelled_runner_never_probes_or_launches_role(self):
        for backend, adapter in self.adapters():
            with self.subTest(backend=backend):
                outcome = Runner(self.config(backend), self.base / 'artifacts').run(adapter, self.request(),
                    timeout=3, cancel_requested=lambda: True)
                self.assert_captured(outcome, 'BLOCKED')
                self.assertEqual(outcome['result']['blocker'], 'PROCESS_CANCELLED')
                self.assertIsNone(outcome['receipt']['pid'])
                self.assertTrue(outcome['receipt']['cancelled'])
                self.assertFalse((self.trusted / 'invocations.jsonl').exists())

    def test_copilot_live_guard_preserves_probe_cancellation(self):
        from ase.adapters.copilot import CopilotAdapter
        outcome = Runner(self.config('copilot', protocol_only=False), self.base / 'artifacts').run(
            CopilotAdapter(), self.request(), timeout=3, cancel_requested=lambda: True)
        self.assert_captured(outcome, 'BLOCKED')
        self.assertEqual(outcome['result']['blocker'], 'PROCESS_CANCELLED')
        self.assertIsNone(outcome['receipt']['pid'])

    def test_codex_prompt_over_native_limit_blocks_before_any_artifact(self):
        artifacts = self.base / 'artifacts'
        config = self.config(max_packet_bytes=2097152)
        with self.assertRaisesRegex(ValueError, '^CODEX_INPUT_TOO_LARGE'):
            Runner(config, artifacts).run(CodexAdapter(), self.request_with_prompt_length(1048577), timeout=10)
        self.assert_no_execution_artifacts(artifacts)
        self.assertFalse((self.trusted / 'invocations.jsonl').exists())

    def test_codex_prompt_at_native_limit_proceeds(self):
        config = self.config(max_packet_bytes=2097152)
        outcome = Runner(config, self.base / 'artifacts').run(CodexAdapter(), self.request_with_prompt_length(1048576), timeout=20)
        self.assertEqual(outcome['result']['status'], 'COMPLETE', outcome['result'])
        self.assertEqual(outcome['receipt']['artifacts']['stdin']['bytes'], 1048576)
        self.assertTrue(verify_receipt(outcome['receipt'])['valid'])

    def test_probe_deadline_blocks_before_help_or_role_launch(self):
        for backend, adapter in self.adapters():
            with self.subTest(backend=backend):
                log = self.trusted / 'invocations.jsonl'
                log.unlink(missing_ok=True)
                began = time.monotonic()
                outcome = Runner(self.config(backend, 'slow-probes'), self.base / 'artifacts').run(adapter,
                    self.request(), timeout=0.2)
                self.assert_captured(outcome, 'BLOCKED')
                self.assertEqual(outcome['result']['blocker'], 'PROCESS_TIMEOUT')
                self.assertIsNone(outcome['receipt']['pid'])
                self.assertTrue(outcome['receipt']['timed_out'])
                # The deadline may kill startup before the fixture writes its first log.
                calls = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
                self.assertIn(calls, ([], [['--version']]))
                self.assertLess(time.monotonic() - began, 1.2)

    def test_cancel_during_version_or_help_never_launches_next_process(self):
        for backend, adapter in self.adapters():
            for case in ('cancel-version', 'cancel-help'):
                with self.subTest(backend=backend, case=case):
                    ready = self.trusted / 'probe.ready'
                    ready.unlink(missing_ok=True)
                    log = self.trusted / 'invocations.jsonl'
                    log.unlink(missing_ok=True)
                    outcome = Runner(self.config(backend, case), self.base / 'artifacts').run(adapter,
                        self.request(), timeout=3, cancel_requested=ready.exists)
                    self.assert_captured(outcome, 'BLOCKED')
                    self.assertEqual(outcome['result']['blocker'], 'PROCESS_CANCELLED')
                    self.assertIsNone(outcome['receipt']['pid'])
                    calls = [json.loads(line) for line in log.read_text().splitlines()]
                    self.assertEqual(len(calls), 1 if case == 'cancel-version' else 2)

    def test_probes_and_role_share_one_remaining_timeout(self):
        for backend, adapter in self.adapters():
            with self.subTest(backend=backend):
                outcome = Runner(self.config(backend, 'shared-budget'), self.base / 'artifacts').run(adapter,
                    self.request(), timeout=0.35)
                self.assert_captured(outcome, 'BLOCKED')
                self.assertEqual(outcome['result']['blocker'], 'PROCESS_TIMEOUT')

    def test_small_successful_calls_and_legacy_probe_config_still_work(self):
        for backend, adapter in self.adapters():
            with self.subTest(backend=backend):
                self.assertTrue(adapter.probe(self.config(backend))['available'])
                outcome = Runner(self.config(backend, 'fitting-budget'), self.base / 'artifacts').run(adapter,
                    self.request(), timeout=2)
                self.assert_captured(outcome, 'COMPLETE')

    def test_workflow_preflight_uses_and_charges_remaining_active_budget(self):
        from ase.store import Store
        from ase.tasks import TaskService
        from ase.workflow import WorkflowController
        from ase.discovery import discover_capabilities, record_discovery
        from ase.state import read_record
        from test_task_contract_v2 import contract
        store = Store(self.target, self.base / 'state')
        self.addCleanup(store.close)
        tasks = TaskService(store)
        spec = contract()
        spec['execution_policy'] = {'active_time_budget_seconds':1}
        task = tasks.create(spec)
        task = tasks.propose(task['task_id'], {'summary':'Draft only', 'stage_ids':['draft']}, expected_revision=task['revision'])
        tasks.approve(task['task_id'], proposal_hash=task['proposal_hash'], user_message_ref='chat:approved', approved_stage_ids=['draft'])
        record_discovery(store, task['task_id'], discover_capabilities(store.project,
            scope={'resources':['guide.md']}, backend_config={}), expected_revision=0)
        controller = WorkflowController(store, Runner(self.config(case='slow-probes'), self.base / 'artifacts'))
        controller.charge_time(task['task_id'], 0.9)
        outcome = controller.run(task['task_id'])
        self.assertEqual(outcome['status'], 'BLOCKED')
        self.assertGreaterEqual(read_record(store.db, 'budget', task['task_id'])['payload']['active_seconds'], 1)
        self.assertEqual(outcome['blocked_reasons'], ['PROCESS_TIMEOUT'])
        log = self.trusted / 'invocations.jsonl'
        calls = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
        self.assertIn(calls, ([], [['--version']]))
