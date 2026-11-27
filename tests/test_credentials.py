import importlib
import json
import os
from pathlib import Path
import tempfile
import unittest


class VaultTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.home = self.base / 'home'
        self.input = self.base / 'import.json'
        self.entry = {'id': 'lab', 'target': 'https://EXAMPLE.com:443', 'kind': 'http-basic',
                      'secrets': {'username': 'fake-user-canary', 'password': 'fake-pass-canary'},
                      'env': {'LAB_USER': 'username'}}

    def write_input(self, entries):
        self.input.write_text(json.dumps({'schema_version': 1, 'credentials': entries}), encoding='utf-8')

    def vault(self):
        try:
            module = importlib.import_module('ase.credentials')
        except ModuleNotFoundError:
            self.fail('credential vault module is missing')
        return module.CredentialVault(self.home)

    @unittest.skipUnless(os.name == 'nt', 'requires actual Windows DPAPI')
    def test_real_dpapi_roundtrip_and_metadata(self):
        self.write_input([self.entry])
        before = self.input.read_bytes()
        vault = self.vault()
        vault.import_file(self.input)
        self.assertEqual(before, self.input.read_bytes())
        blob = (self.home / 'credentials' / 'vault.json').read_bytes()
        self.assertNotIn(b'fake-pass-canary', blob)
        self.assertNotIn('fake-pass-canary', repr(vault.list()))
        self.assertEqual(vault.resolve('lab', 'https://example.com')['secrets']['password'], 'fake-pass-canary')
        self.assertEqual(vault.metadata('lab', 'https://example.com')['revision'], 1)

    @unittest.skipUnless(os.name == 'nt', 'requires actual Windows DPAPI')
    def test_batch_revision_cas_and_tampering(self):
        self.write_input([self.entry])
        vault = self.vault()
        vault.import_file(self.input)
        vault.import_file(self.input)
        self.assertEqual(vault.metadata('lab', 'https://example.com')['revision'], 1)
        vault.update_secrets('lab', 'https://example.com', {'password': 'new-canary'}, 1)
        self.assertEqual(vault.metadata('lab', 'https://example.com')['revision'], 2)
        with self.assertRaises(ValueError):
            vault.update_secrets('lab', 'https://example.com', {'password': 'stale'}, 1)
        bad = dict(self.entry, target='https://other.example')
        self.write_input([bad])
        with self.assertRaises(ValueError):
            vault.import_file(self.input)
        self.assertEqual(vault.resolve('lab', 'https://example.com')['secrets']['password'], 'new-canary')
        path = self.home / 'credentials' / 'vault.json'
        data = bytearray(path.read_bytes()); data[len(data)//2] ^= 1; path.write_bytes(data)
        with self.assertRaises(ValueError) as error:
            vault.list()
        self.assertNotIn('canary', str(error.exception))

    @unittest.skipUnless(os.name == 'nt', 'requires actual Windows DPAPI')
    def test_invalid_batch_is_atomic_and_argv_cannot_include_secrets(self):
        self.write_input([self.entry])
        vault = self.vault()
        vault.import_file(self.input)
        second = dict(self.entry, id='second', session={
            'probe': {'argv': [os.path.abspath(os.sys.executable), 'fake-pass-canary'], 'timeout': 1},
            'login': {'argv': [os.path.abspath(os.sys.executable)], 'timeout': 1}})
        self.write_input([dict(self.entry, secrets={'username':'changed', 'password':'changed'}), second])
        with self.assertRaises(ValueError) as error:
            vault.import_file(self.input)
        self.assertNotIn('fake-pass-canary', str(error.exception))
        self.assertEqual(vault.resolve('lab', 'https://example.com')['revision'], 1)
        self.assertEqual(len(vault.list()), 1)

    @unittest.skipUnless(os.name == 'nt', 'requires actual Windows DPAPI')
    def test_concurrent_process_imports_merge(self):
        import subprocess
        entries = [dict(self.entry, id='lab' + str(i)) for i in range(4)]
        paths = []
        for i, entry in enumerate(entries):
            path = self.base / ('input' + str(i) + '.json')
            path.write_text(json.dumps({'schema_version': 1, 'credentials': [entry]}), encoding='utf-8')
            paths.append(path)
        script = 'import sys; from ase.credentials import CredentialVault; CredentialVault(sys.argv[1]).import_file(sys.argv[2])'
        processes = [subprocess.Popen([os.sys.executable, '-c', script, str(self.home), str(path)]) for path in paths]
        self.assertEqual([p.wait(timeout=30) for p in processes], [0] * len(paths))
        self.assertEqual({p['id'] for p in self.vault().list()}, {p['id'] for p in entries})

    @unittest.skipUnless(os.name == 'nt', 'requires Windows replace-while-open semantics')
    def test_save_waits_out_a_transient_open_handle_on_the_vault(self):
        import threading
        self.write_input([self.entry])
        vault = self.vault()
        vault.import_file(self.input)
        holder = open(vault.path, 'rb')  # like a scanner/indexer: os.replace fails while it is open
        self.addCleanup(holder.close)
        timer = threading.Timer(0.3, holder.close)
        self.addCleanup(timer.join)
        timer.start()
        self.write_input([dict(self.entry, id='second')])
        vault.import_file(self.input)  # integration only; the retry count is proven deterministically below
        self.assertEqual({p['id'] for p in vault.list()}, {'lab', 'second'})

    @unittest.skipUnless(os.name == 'nt', 'requires actual Windows DPAPI')
    def test_replace_retries_exactly_the_transient_permission_errors(self):
        from unittest import mock
        module = importlib.import_module('ase.credentials')
        self.write_input([self.entry])
        vault = self.vault()
        real, failures = module.os.replace, [PermissionError(13, 'in use')] * 3

        def flaky(source, target):
            if failures:
                raise failures.pop()
            return real(source, target)
        with mock.patch.object(module.os, 'replace', side_effect=flaky), \
                mock.patch.object(module.time, 'sleep') as slept:
            vault.import_file(self.input)
        self.assertEqual(slept.call_count, 3)
        self.assertEqual({p['id'] for p in vault.list()}, {'lab'})

    @unittest.skipUnless(os.name == 'nt', 'requires Windows replace-while-open semantics')
    def test_save_still_fails_closed_when_the_vault_stays_held(self):
        from unittest import mock
        module = importlib.import_module('ase.credentials')
        self.write_input([self.entry])
        vault = self.vault()
        vault.import_file(self.input)
        self.write_input([dict(self.entry, id='second')])
        with open(vault.path, 'rb'), mock.patch.object(module, '_REPLACE_ATTEMPTS', 3):
            with self.assertRaises(module.CredentialError) as error:
                vault.import_file(self.input)
        self.assertEqual(error.exception.code, 'storage_error')
        self.assertEqual({p['id'] for p in vault.list()}, {'lab'})
        self.assertEqual([p.name for p in vault.directory.glob('.vault-*')], [])

    @unittest.skipUnless(os.name == 'nt', 'requires actual Windows byte-range locking')
    def test_empty_lock_file_waits_before_initialization_and_preserves_cancellation(self):
        import subprocess
        from ase.credentials import CredentialError
        vault = self.vault()
        vault.directory.mkdir(parents=True)
        script = ('import sys,msvcrt; f=open(sys.argv[1],"a+b"); f.seek(0); '
                  'msvcrt.locking(f.fileno(),msvcrt.LK_NBLCK,1); print("ready",flush=True); '
                  'sys.stdin.read(1); f.seek(0); msvcrt.locking(f.fileno(),msvcrt.LK_UNLCK,1); f.close()')
        proc = subprocess.Popen([os.sys.executable, '-B', '-c', script, str(vault.lock_path)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            self.assertEqual(proc.stdout.readline().strip(), 'ready')
            self.assertEqual(vault.lock_path.stat().st_size, 0)
            with self.assertRaises(CredentialError) as error:
                with vault._locked(timeout=.1):
                    self.fail('The other process still owns byte zero')
            self.assertEqual(error.exception.code, 'timeout')
            with self.assertRaises(CredentialError) as error:
                with vault._locked(timeout=1, cancel_requested=lambda: True):
                    self.fail('Cancelled acquisition cannot initialize the file')
            self.assertEqual(error.exception.code, 'cancelled')
            self.assertEqual(vault.lock_path.stat().st_size, 0)
        finally:
            try:
                proc.communicate(input='release' if proc.poll() is None else None, timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.communicate(timeout=10)
                raise
        self.assertEqual(proc.returncode, 0)
        with vault._locked(timeout=1):
            pass
        self.assertEqual(vault.lock_path.read_bytes(), b'0')

    def test_invalid_origins_are_rejected(self):
        from ase.credentials import normalize_target
        for target in ['https://example.com:', 'https://example.com?', 'https://example.com#',
                       'https://example.com\\evil', 'https://u:p@example.com',
                       'https://example.com/path', 'http://example.com']:
            with self.subTest(target=target), self.assertRaises(ValueError):
                normalize_target(target)

    @unittest.skipUnless(os.name == 'nt', 'requires actual Windows DPAPI')
    def test_duplicate_keys_and_configure_are_safe(self):
        vault = self.vault()
        self.write_input([self.entry]); vault.import_file(self.input)
        vault.configure('lab', 'https://example.com', {'env': {'PASSWORD_ENV': 'password'}})
        self.assertEqual(vault.metadata('lab', 'https://example.com')['revision'], 2)
        self.input.write_text('{"schema_version":1,"schema_version":1,"credentials":[]}', encoding='utf-8')
        with self.assertRaises(ValueError): vault.import_file(self.input)
        self.assertEqual(vault.metadata('lab', 'https://example.com')['revision'], 2)

    def test_unsupported_platform_fails_before_creating_home(self):
        from unittest.mock import patch
        from ase.credentials import CredentialError, CredentialVault
        with patch('ase.credentials.os.name', 'unsupported'):
            with self.assertRaises(CredentialError) as error:
                CredentialVault(self.home)
        self.assertEqual(error.exception.code, 'unsupported_platform')
        self.assertFalse(self.home.exists())

    @unittest.skipUnless(os.name == 'nt', 'requires actual Windows DPAPI')
    def test_git_import_is_rejected_without_reading_or_modifying_input(self):
        repo = self.base / 'repo'; repo.mkdir(); (repo / '.git').mkdir()
        path = repo / 'credentials.json'
        path.write_text('secret-canary', encoding='utf-8')
        with self.assertRaises(ValueError) as error:
            self.vault().import_file(path)
        self.assertNotIn('secret-canary', str(error.exception))
        self.assertEqual(path.read_text(encoding='utf-8'), 'secret-canary')

    @unittest.skipUnless(os.name == 'nt', 'requires actual Windows DPAPI')
    def test_metadata_hides_recipe_and_configuration_can_remove_it(self):
        import base64
        encoded = base64.b64encode(b'unrelated-recipe-canary').decode()
        entry = dict(self.entry, session={
            'probe': {'argv': [os.path.abspath(os.sys.executable), encoded], 'timeout': 1},
            'login': {'argv': [os.path.abspath(os.sys.executable)], 'timeout': 1}})
        self.write_input([entry]); vault = self.vault()
        vault.import_file(self.input)
        self.assertNotIn(encoded, repr(vault.list()))
        vault.configure('lab', 'https://example.com', {'session': None})
        self.assertNotIn('session', vault.resolve('lab', 'https://example.com'))

    def test_zero_port_is_not_default_origin(self):
        from ase.credentials import normalize_target
        with self.assertRaises(ValueError): normalize_target('https://example.com:0')

    @unittest.skipUnless(os.name == 'nt', 'requires actual Windows DPAPI')
    def test_lock_wait_respects_timeout_and_cancellation(self):
        import subprocess, time
        vault = self.vault()
        self.write_input([self.entry]); vault.import_file(self.input)
        script = ('import sys,time,msvcrt; f=open(sys.argv[1],"r+b"); '
                  'msvcrt.locking(f.fileno(),msvcrt.LK_LOCK,1); print("ready",flush=True); '
                  'time.sleep(3)')
        proc = subprocess.Popen([os.sys.executable, '-c', script, str(vault.lock_path)], stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(proc.stdout.readline().strip(), 'ready')
            start = time.monotonic()
            with self.assertRaises(ValueError) as error:
                vault.resolve('lab', 'https://example.com', timeout=.1)
            self.assertEqual(error.exception.code, 'timeout')
            self.assertLess(time.monotonic() - start, 1)
            with self.assertRaises(ValueError) as error:
                vault.update_secrets('lab', 'https://example.com', {'password': 'x'}, 1, cancel_requested=lambda: True)
            self.assertEqual(error.exception.code, 'cancelled')
        finally:
            proc.terminate(); proc.wait(); proc.stdout.close()
        self.assertEqual(vault.metadata('lab', 'https://example.com')['revision'], 1)

    @unittest.skipUnless(os.name == 'nt', 'requires actual Windows DPAPI')
    def test_metadata_exposes_configuration_names_without_values(self):
        self.write_input([self.entry]); vault = self.vault()
        imported = vault.import_file(self.input)[0]
        listed = vault.list()[0]
        for metadata in (imported, listed, vault.metadata('lab', 'https://example.com')):
            self.assertEqual(metadata['secret_fields'], ['password', 'username'])
            self.assertEqual(metadata['env_names'], ['LAB_USER'])
            self.assertFalse(metadata['has_session'])
            self.assertNotIn('fake-pass-canary', repr(metadata))
            self.assertNotIn('fake-user-canary', repr(metadata))

    @unittest.skipUnless(os.name == 'nt', 'requires actual Windows DPAPI')
    def test_import_rejects_secret_encodings_in_recipe_arguments(self):
        import base64
        from urllib.parse import quote, quote_plus
        password = 'fake pass/é-canary'
        combined = 'user-fixture:' + password
        variants = [base64.b64encode(password.encode()).decode(), quote(password, safe=''),
                    quote_plus(password), json.dumps(password, ensure_ascii=True)[1:-1],
                    base64.b64encode(combined.encode()).decode()]
        for variant in variants:
            entry = dict(self.entry, secrets={'username': 'user-fixture', 'password': password}, session={
                'probe': {'argv': [os.path.abspath(os.sys.executable), variant], 'timeout': 1},
                'login': {'argv': [os.path.abspath(os.sys.executable)], 'timeout': 1}})
            self.write_input([entry])
            with self.subTest(encoded=variant), self.assertRaises(ValueError):
                self.vault().import_file(self.input)
        self.assertFalse((self.home / 'credentials' / 'vault.json').exists())

    @unittest.skipUnless(os.name == 'nt', 'requires actual Windows DPAPI')
    def test_configure_rejects_encoded_secret_arguments(self):
        import base64
        self.write_input([self.entry]); vault = self.vault(); vault.import_file(self.input)
        encoded = base64.b64encode(b'fake-pass-canary').decode()
        recipe = {'probe': {'argv': [os.path.abspath(os.sys.executable), encoded], 'timeout': 1},
                  'login': {'argv': [os.path.abspath(os.sys.executable)], 'timeout': 1}}
        with self.assertRaises(ValueError):
            vault.configure('lab', 'https://example.com', {'session': recipe})
        self.assertEqual(vault.metadata('lab', 'https://example.com')['revision'], 1)
        self.assertNotIn('session', vault.resolve('lab', 'https://example.com'))

    @unittest.skipUnless(os.name == 'nt', 'requires actual Windows DPAPI')
    def test_rotation_rejects_new_secret_encoded_in_existing_recipe(self):
        import base64
        future = 'next-fake-secret-canary'
        encoded = base64.b64encode(future.encode()).decode()
        entry = dict(self.entry, session={
            'probe': {'argv': [os.path.abspath(os.sys.executable), encoded], 'timeout': 1},
            'login': {'argv': [os.path.abspath(os.sys.executable)], 'timeout': 1}})
        self.write_input([entry]); vault = self.vault(); vault.import_file(self.input)
        with self.assertRaises(ValueError):
            vault.update_secrets('lab', 'https://example.com', {'password': future}, 1)
        self.assertEqual(vault.metadata('lab', 'https://example.com')['revision'], 1)
