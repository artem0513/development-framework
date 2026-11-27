import json
import base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
import time
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import ase.access as access_module
from ase.access import CredentialAccess
from ase.access import capture_process as actual_capture_process
from ase.credentials import CredentialVault, CredentialError


@unittest.skipUnless(os.name == 'nt', 'requires actual Windows DPAPI')
class AccessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.vault = CredentialVault(self.root / 'home')
        self.state = self.root / 'session'
        self.script = self.root / 'provider.py'
        self.script.write_text('''import json, os, pathlib, sys\nstate = pathlib.Path(sys.argv[2])\nif sys.argv[1] == "probe":\n print(json.dumps({"status": "authenticated" if state.exists() else "login_required"}))\nelse:\n state.write_text("ok")\n print(json.dumps({"status": "authenticated", "secrets": {"password": "refreshed-fake-secret"}}))\n''', encoding='utf-8')
        self.profile = {'id':'test','target':'http://127.0.0.1:7777','kind':'environment',
                        'secrets':{'password':'initial-fake-secret'},'env':{'ASE_TEST_PASSWORD':'password'},
                        'session':{'probe':{'argv':[sys.executable,str(self.script),'probe',str(self.state)],'timeout':5},
                                   'login':{'argv':[sys.executable,str(self.script),'login',str(self.state)],'timeout':5}}}
        self.import_profiles([self.profile])
        self.access = CredentialAccess(self.vault)

    def import_profiles(self, profiles):
        source = self.root / 'import.json'
        source.write_text(json.dumps({'schema_version':1,'credentials':profiles}), encoding='utf-8')
        self.vault.import_file(source)

    def test_lazy_login_refresh_and_session_reuse(self):
        self.assertFalse(self.state.exists())
        first = self.access.ensure('test', self.profile['target'])
        self.assertTrue(self.state.exists())
        self.assertEqual(first['revision'], 2)
        self.assertEqual(self.vault.resolve('test', self.profile['target'])['secrets']['password'], 'refreshed-fake-secret')
        second = self.access.ensure('test', self.profile['target'])
        self.assertEqual(second['revision'], 2)
        self.state.unlink()
        self.access.ensure('test', self.profile['target'])
        self.assertTrue(self.state.exists())

    def test_run_injects_only_child_and_redacts_echo(self):
        before = os.environ.get('ASE_TEST_PASSWORD')
        log = self.root / 'run.log'
        receipt = self.access.run('test', self.profile['target'], [sys.executable, '-c', 'import os; print(os.environ["ASE_TEST_PASSWORD"])'], self.root, log)
        self.assertEqual(receipt['exit_code'], 0)
        self.assertNotIn('refreshed-fake-secret', log.read_text(encoding='utf-8'))
        self.assertEqual(os.environ.get('ASE_TEST_PASSWORD'), before)
        self.assertEqual(receipt['access']['revision'], 2)

    def test_target_mismatch_is_rejected(self):
        with self.assertRaises(CredentialError):
            self.access.ensure('test', 'http://127.0.0.1:7778')
        self.assertFalse(self.state.exists())


    def test_interaction_and_missing_cli_are_typed_without_raw_output(self):
        self.script.write_text('import json; print(json.dumps({"status":"interaction_required", "secret":"initial-fake-secret"}))', encoding='utf-8')
        with self.assertRaises(CredentialError) as error:
            self.access.ensure('test', self.profile['target'])
        self.assertEqual(error.exception.code.lower(), 'interaction_required')
        self.assertNotIn('initial-fake-secret', str(error.exception))
        config = {'env': self.profile['env'], 'session': {'probe': {'argv':[str(self.root / 'missing.exe')], 'timeout':2}, 'login':self.profile['session']['login']}}
        self.vault.configure('test', self.profile['target'], config)
        with self.assertRaises(CredentialError) as error:
            self.access.ensure('test', self.profile['target'])
        self.assertEqual(error.exception.code.lower(), 'cli_unavailable')

    def test_timeout_cancel_and_secret_argv_prevent_execution(self):
        self.script.write_text('import time; time.sleep(3)', encoding='utf-8')
        started = time.monotonic()
        with self.assertRaises(CredentialError) as error:
            self.access.ensure('test', self.profile['target'], timeout=.15)
        self.assertEqual(error.exception.code.lower(), 'timeout')
        self.assertLess(time.monotonic() - started, 2)
        with self.assertRaises(CredentialError) as error:
            self.access.ensure('test', self.profile['target'], cancel_requested=lambda: True)
        self.assertEqual(error.exception.code.lower(), 'cancelled')
        self.import_profiles([{'id':'no-session','target':self.profile['target'],'kind':'environment','secrets':{'password':'initial-fake-secret'},'env':{}}])
        with self.assertRaises(CredentialError) as error:
            self.access.run('no-session', self.profile['target'], [sys.executable, 'initial-fake-secret'], self.root, self.root / 'no.log')
        self.assertEqual(error.exception.code.lower(), 'secret_in_argument')
        self.assertFalse((self.root / 'no.log').exists())

    def test_http_bearer_redacts_body_and_rejects_redirect(self):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path == '/redirect':
                    self.send_response(302); self.send_header('Location', 'http://example.com/'); self.end_headers(); return
                self.send_response(200); self.end_headers()
                self.wfile.write(self.headers.get('Authorization', '').encode())
            def log_message(self, *args): pass
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join, 2)
        self.addCleanup(server.shutdown)
        target = 'http://127.0.0.1:' + str(server.server_port)
        self.import_profiles([{'id':'http','target':target,'kind':'http-bearer','secrets':{'token':'fake-bearer-canary'},'env':{}}])
        response = self.access.request('http', target)
        self.assertEqual(response['status_code'], 200)
        self.assertNotIn('fake-bearer-canary', response['body'])
        with self.assertRaises(CredentialError) as error:
            self.access.request('http', target, '/redirect')
        self.assertEqual(error.exception.code.lower(), 'redirect_rejected')
        with self.assertRaises(CredentialError):
            self.access.request('http', target, '//example.com/')
        with self.assertRaises(CredentialError):
            self.access.request('http', target, '/', method='POST')

    def test_concurrent_ensure_performs_one_login(self):
        count = self.root / 'count'
        self.script.write_text("""import json, pathlib, sys, time
state = pathlib.Path(sys.argv[2])
if sys.argv[1] == 'probe':
 print(json.dumps({'status':'authenticated' if state.exists() else 'login_required'}))
else:
 time.sleep(.2)
 pathlib.Path(sys.argv[3]).write_text(pathlib.Path(sys.argv[3]).read_text() + 'x' if pathlib.Path(sys.argv[3]).exists() else 'x')
 state.write_text('ok')
 print(json.dumps({'status':'authenticated'}))
""", encoding='utf-8')
        session = self.profile['session']
        session['login']['argv'].append(str(count))
        self.vault.configure('test', self.profile['target'], {'session': session})
        failures = []
        def ensure():
            try: self.access.ensure('test', self.profile['target'])
            except Exception as error: failures.append(error)
        threads = [threading.Thread(target=ensure) for _ in range(3)]
        for thread in threads: thread.start()
        for thread in threads: thread.join(5)
        self.assertFalse(failures)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(count.read_text(), 'x')

    def test_run_budget_includes_authentication(self):
        # Mechanical deadline seam, not authentication evidence. The real
        # provider timeout is exercised separately below; core still owns a
        # real child here and keeps its unmodified monotonic clock.
        profile = self.vault.resolve('test', self.profile['target'])
        readings = iter([100.0])
        deadlines, budgets = [], []
        def auth_boundary(ref, target, deadline, cancel, launch_guard=None):
            deadlines.append(deadline)
            return profile, {'credential_ref':ref, 'target':target, 'revision':profile['revision']}, [profile['secrets']['password']]
        actual_execute = access_module.core.execute
        def execute(*args, **kwargs):
            budgets.append(kwargs['timeout'])
            return actual_execute(*args, **kwargs)
        completed = self.root / 'command-completed'
        command = [sys.executable, '-c', "import time; from pathlib import Path; time.sleep(30); Path(" + repr(str(completed)) + ").write_text('late')"]
        clock = SimpleNamespace(monotonic=lambda: next(readings, 100.25))
        with patch.object(access_module, 'time', clock), patch.object(self.access, '_ensure', side_effect=auth_boundary), patch.object(access_module.core, 'execute', side_effect=execute):
            receipt = self.access.run('test', self.profile['target'], command, self.root, self.root / 'timeout.log', timeout=.5)
        self.assertEqual(deadlines, [100.5])
        self.assertEqual(budgets, [.25])
        self.assertTrue(receipt['timed_out'])
        self.assertIsNotNone(receipt['exit_code'])
        self.assertFalse(completed.exists())

    def test_run_authentication_timeout_prevents_command_execution(self):
        self.script.write_text('import time; time.sleep(30)', encoding='utf-8')
        effect, log = self.root / 'command-ran', self.root / 'auth-timeout.log'
        command = [sys.executable, '-c', "from pathlib import Path; Path(" + repr(str(effect)) + ").write_text('unexpected')"]
        actual_execute = access_module.core.execute
        with patch.object(access_module.core, 'execute', wraps=actual_execute) as execute:
            with self.assertRaises(CredentialError) as error:
                self.access.run('test', self.profile['target'], command, self.root, log, timeout=.5)
        self.assertEqual(error.exception.code, 'timeout')
        execute.assert_not_called()
        self.assertFalse(log.exists())
        self.assertFalse(effect.exists())

    def test_http_response_stream_obeys_total_timeout(self):
        class Slow(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200); self.send_header('Content-Length', '50'); self.end_headers()
                for _ in range(50):
                    try: self.wfile.write(b'x'); self.wfile.flush(); time.sleep(.05)
                    except OSError: break
            def log_message(self, *args): pass
        server = ThreadingHTTPServer(('127.0.0.1', 0), Slow)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        self.addCleanup(server.server_close); self.addCleanup(thread.join, 2); self.addCleanup(server.shutdown)
        target = 'http://127.0.0.1:' + str(server.server_port)
        self.import_profiles([{'id':'slow','target':target,'kind':'http-basic','secrets':{'username':'fake-user','password':'fake-password'},'env':{}}])
        started = time.monotonic()
        with self.assertRaises(CredentialError) as error:
            self.access.request('slow', target, timeout=.25)
        self.assertEqual(error.exception.code, 'timeout')
        self.assertLess(time.monotonic() - started, 1.5)

    def test_environment_only_is_prepared_without_claiming_authentication(self):
        self.import_profiles([{'id':'plain','target':self.profile['target'],'kind':'environment','secrets':{'password':'sample-value'},'env':{}}])
        result = self.access.ensure('plain', self.profile['target'])
        self.assertEqual(result['status'], 'prepared')
        self.assertFalse(self.state.exists())

    def test_probe_duplicate_status_is_rejected_without_leaking_output(self):
        self.script.write_text("print('{\"status\":\"authenticated\",\"status\":\"initial-fake-secret\"}')", encoding='utf-8')
        with self.assertRaises(CredentialError) as error:
            self.access.ensure('test', self.profile['target'])
        self.assertEqual(error.exception.code, 'invalid_cli_response')
        self.assertNotIn('initial-fake-secret', str(error.exception))

    def test_basic_combined_secret_and_json_escaped_arguments_are_protected(self):
        self.import_profiles([{'id':'basic','target':self.profile['target'],'kind':'http-basic','secrets':{'username':'fake-user','password':'páss-fake'},'env':{'BASIC_USER':'username','BASIC_PASS':'password'}}])
        encoded = base64.b64encode('fake-user:páss-fake'.encode()).decode()
        log = self.root / 'basic.log'
        code = 'import os,base64; print(base64.b64encode((os.environ["BASIC_USER"]+":"+os.environ["BASIC_PASS"]).encode()).decode())'
        receipt = self.access.run('basic', self.profile['target'], [sys.executable, '-c', code], self.root, log)
        self.assertEqual(receipt['exit_code'], 0)
        self.assertNotIn(encoded, log.read_text(encoding='utf-8'))
        self.assertIn('[REDACTED]', log.read_text(encoding='utf-8'))
        escaped = json.dumps('páss-fake')[1:-1]
        with self.assertRaises(CredentialError) as error:
            self.access.run('basic', self.profile['target'], [sys.executable, escaped], self.root, self.root / 'blocked.log')
        self.assertEqual(error.exception.code, 'secret_in_argument')

    def test_invalid_status_types_are_typed(self):
        for response in ('{"status":["authenticated"]}', '{"status":{}}'):
            self.script.write_text('print(' + repr(response) + ')', encoding='utf-8')
            with self.assertRaises(CredentialError) as error:
                self.access.ensure('test', self.profile['target'])
            self.assertEqual(error.exception.code, 'invalid_cli_response')

    def test_http_cancel_stops_owned_worker_and_socket(self):
        disconnected = threading.Event()
        connected = threading.Event()
        connected_at = []
        class Slow(BaseHTTPRequestHandler):
            def do_GET(self):
                connected_at.append(time.monotonic()); connected.set()
                self.send_response(200); self.send_header('Content-Length', '100000'); self.end_headers()
                for _ in range(1000):
                    try: self.wfile.write(b'x' * 100); self.wfile.flush(); time.sleep(.01)
                    except OSError: disconnected.set(); break
            def log_message(self, *args): pass
        server = ThreadingHTTPServer(('127.0.0.1', 0), Slow)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        self.addCleanup(server.server_close); self.addCleanup(thread.join, 2); self.addCleanup(server.shutdown)
        target = 'http://127.0.0.1:' + str(server.server_port)
        self.import_profiles([{'id':'cancel-http','target':target,'kind':'http-bearer','secrets':{'token':'fake-cancel-token'},'env':{}}])
        owner = threading.get_ident(); started = time.monotonic(); callback_threads = []
        def cancel():
            callback_threads.append(threading.get_ident())
            return connected.is_set() and time.monotonic() - connected_at[0] > .1
        def slow_start(*args,**kwargs):
            time.sleep(.4)
            return actual_capture_process(*args,**kwargs)
        with patch('ase.access.capture_process',side_effect=slow_start), self.assertRaises(CredentialError) as error:
            self.access.request('cancel-http', target, timeout=5, cancel_requested=cancel)
        self.assertEqual(error.exception.code, 'cancelled')
        self.assertTrue(callback_threads)
        self.assertEqual(set(callback_threads), {owner})
        self.assertTrue(connected.is_set())
        self.assertLess(time.monotonic() - connected_at[0], 1.5)
        self.assertTrue(disconnected.wait(2))

    def test_http_cancel_before_start_does_not_require_a_socket(self):
        target='http://127.0.0.1:1'
        self.import_profiles([{'id':'pre-cancel','target':target,'kind':'http-bearer','secrets':{'token':'fake-pre-cancel'},'env':{}}])
        with patch('ase.access.capture_process') as start:
            with self.assertRaises(CredentialError) as error:
                self.access.request('pre-cancel',target,timeout=5,cancel_requested=lambda:True)
        self.assertEqual(error.exception.code,'cancelled')
        start.assert_not_called()

    def test_http_auth_failures_are_typed_without_exposing_response_body(self):
        class Rejected(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(401 if self.path == '/unauthorized' else 403)
                self.end_headers()
                self.wfile.write(b'fake-auth-response-canary')
            def log_message(self, *args): pass
        server = ThreadingHTTPServer(('127.0.0.1', 0), Rejected)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        self.addCleanup(server.server_close); self.addCleanup(thread.join, 2); self.addCleanup(server.shutdown)
        target = 'http://127.0.0.1:' + str(server.server_port)
        self.import_profiles([{'id':'rejected','target':target,'kind':'http-bearer','secrets':{'token':'fake-bearer-token'},'env':{}}])
        for path, expected in (('/unauthorized', 'authentication_rejected'), ('/forbidden', 'access_denied')):
            with self.subTest(path=path):
                with self.assertRaises(CredentialError) as error:
                    self.access.request('rejected', target, path)
                self.assertEqual(error.exception.code, expected)
                self.assertNotIn('fake-auth-response-canary', str(error.exception))
                self.assertNotIn('fake-bearer-token', str(error.exception))

if __name__ == '__main__':
    unittest.main()
