"""Windows CurrentUser encrypted credential storage for explicitly imported profiles."""
from __future__ import annotations

import base64
import copy
import ctypes
from ctypes import wintypes
import ipaddress
import json
import os
from pathlib import Path
import re
import tempfile
import time
from urllib.parse import quote, quote_plus, urlsplit

_MAX = 1024 * 1024
_REF = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$')
_ENV = re.compile(r'^[A-Za-z_][A-Za-z0-9_]{0,127}$')


class CredentialError(ValueError):
    def __init__(self, code, generic_message=None):
        self.code = code
        super().__init__(code if generic_message is None else f'{code}: {generic_message}')


def validate_access_ref(ref):
    if not isinstance(ref, str) or not _REF.fullmatch(ref):
        raise CredentialError('invalid_reference')
    return ref


def normalize_target(target):
    try:
        if not isinstance(target, str) or any(ord(c) < 33 for c in target) or '\\' in target:
            raise ValueError()
        u = urlsplit(target)
        if u.scheme.lower() not in ('https', 'http') or not u.hostname or u.username is not None or u.password is not None or u.path not in ('', '/') or '?' in target or '#' in target or u.netloc.endswith(':'):
            raise ValueError()
        host = u.hostname.lower().encode('idna').decode('ascii')
        try:
            ipaddress.ip_address(host)
        except ValueError:
            if len(host) > 253 or any(not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', label) for label in host.split('.')):
                raise ValueError()
        if u.scheme.lower() == 'http':
            if host != 'localhost' and not ipaddress.ip_address(host).is_loopback:
                raise ValueError()
        port = u.port
        if port == 0:
            raise ValueError()
        if ':' in host:
            host = '[' + host + ']'
        if port and port != (443 if u.scheme.lower() == 'https' else 80):
            host += ':' + str(port)
        return u.scheme.lower() + '://' + host
    except (ValueError, UnicodeError):
        raise CredentialError('invalid_target') from None


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError()
        result[key] = value
    return result


def _json(data):
    if len(data) > _MAX:
        raise CredentialError('invalid_data')
    try:
        return json.loads(data.decode('utf-8'), object_pairs_hook=_pairs, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, UnicodeError, RecursionError):
        raise CredentialError('invalid_data') from None


def _safe_path(path):
    p = Path(os.path.abspath(path))
    if os.name == 'nt':
        drive = p.drive
        if drive.startswith('\\\\') or not drive:
            raise CredentialError('unsafe_path')
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.GetDriveTypeW.argtypes = [wintypes.LPCWSTR]
        kernel.GetDriveTypeW.restype = wintypes.UINT
        if kernel.GetDriveTypeW(drive + '\\') != 3:  # fixed local disk
            raise CredentialError('unsafe_path')
    for part in (p, *p.parents):
        try:
            st = part.lstat()
        except FileNotFoundError:
            continue
        except OSError:
            raise CredentialError('unsafe_path') from None
        if part.is_symlink() or getattr(st, 'st_file_attributes', 0) & 0x400:
            raise CredentialError('unsafe_path')
        if part == p and not part.is_dir() and st.st_nlink > 1:
            raise CredentialError('unsafe_path')
    return p


def _outside_git(path):
    for parent in (path, *path.parents):
        if (parent / '.git').exists():
            raise CredentialError('unsafe_path')


def _read(path):
    try:
        with path.open('rb') as stream:
            data = stream.read(_MAX + 1)
        if len(data) > _MAX:
            raise CredentialError('invalid_data')
        return data
    except OSError:
        raise CredentialError('storage_error') from None


_REPLACE_ATTEMPTS = 40
_REPLACE_DELAY = 0.05


def _replace(source, target):
    """Atomic replace; on Windows retry briefly while another process (scanner, indexer) holds the target open."""
    for attempt in range(_REPLACE_ATTEMPTS):
        try:
            return os.replace(source, target)
        except PermissionError:
            if os.name != 'nt' or attempt == _REPLACE_ATTEMPTS - 1:
                raise
            time.sleep(_REPLACE_DELAY)
            _safe_path(target)  # The target must still be the same safe path after the wait.


class _Blob(ctypes.Structure):
    _fields_ = [('cbData', wintypes.DWORD), ('pbData', ctypes.POINTER(ctypes.c_ubyte))]


def _crypt(data, protect):
    if os.name != 'nt':
        raise CredentialError('unsupported_platform')
    source = ctypes.create_string_buffer(data, len(data))
    inp = _Blob(len(data), ctypes.cast(source, ctypes.POINTER(ctypes.c_ubyte)))
    out = _Blob()
    crypt = ctypes.WinDLL('crypt32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    fn = crypt.CryptProtectData if protect else crypt.CryptUnprotectData
    fn.argtypes = [ctypes.POINTER(_Blob), ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_Blob)]
    fn.restype = wintypes.BOOL
    if not fn(ctypes.byref(inp), None, None, None, None, 1, ctypes.byref(out)):
        raise CredentialError('encryption_error' if protect else 'corrupt_vault')
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        kernel.LocalFree(out.pbData)


def _profile(value, revision=False):
    if not isinstance(value, dict) or set(value) - {'id', 'target', 'kind', 'secrets', 'env', 'session', 'revision'} or (not revision and 'revision' in value):
        raise CredentialError('invalid_profile')
    ref = validate_access_ref(value.get('id'))
    target = normalize_target(value.get('target'))
    kind = value.get('kind')
    if kind not in ('environment', 'http-basic', 'http-bearer'):
        raise CredentialError('invalid_profile')
    secrets = value.get('secrets')
    if not isinstance(secrets, dict) or not secrets or len(secrets) > 128:
        raise CredentialError('invalid_profile')
    for k, v in secrets.items():
        if not isinstance(k, str) or not _REF.fullmatch(k) or not isinstance(v, str) or not v or len(v) > 65536 or '\x00' in v:
            raise CredentialError('invalid_profile')
        try:
            v.encode('utf-8')
        except UnicodeError:
            raise CredentialError('invalid_profile') from None
    if kind == 'http-basic' and not {'username', 'password'} <= secrets.keys() or kind == 'http-bearer' and 'token' not in secrets:
        raise CredentialError('invalid_profile')
    env = value.get('env', {})
    if not isinstance(env, dict) or len(env) > 128 or any(not isinstance(k, str) or not _ENV.fullmatch(k) or not isinstance(v, str) or v not in secrets for k, v in env.items()):
        raise CredentialError('invalid_profile')
    result = {'id': ref, 'target': target, 'kind': kind, 'secrets': dict(secrets), 'env': dict(env)}
    if 'session' in value:
        session = value['session']
        if not isinstance(session, dict) or set(session) != {'probe', 'login'}:
            raise CredentialError('invalid_profile')
        sensitive = list(secrets.values())
        if kind == 'http-basic':
            sensitive.append(secrets['username'] + ':' + secrets['password'])
        variants = {form for secret in sensitive for form in (
            secret, quote(secret, safe=''), quote_plus(secret),
            base64.b64encode(secret.encode('utf-8')).decode('ascii'),
            json.dumps(secret, ensure_ascii=True)[1:-1])}
        for recipe in session.values():
            if not isinstance(recipe, dict) or set(recipe) - {'argv', 'timeout'} or not isinstance(recipe.get('argv'), list) or not recipe['argv'] or len(recipe['argv']) > 128 or any(not isinstance(a, str) or not a or '\x00' in a or len(a) > 32768 for a in recipe['argv']) or not os.path.isabs(recipe['argv'][0]) or not isinstance(recipe.get('timeout'), (int, float)) or isinstance(recipe['timeout'], bool) or not 0 < recipe['timeout'] <= 3600:
                raise CredentialError('invalid_profile')
            try:
                for arg in recipe['argv']: arg.encode('utf-8')
            except UnicodeError:
                raise CredentialError('invalid_profile') from None
            if any(secret in arg for secret in variants for arg in recipe['argv']):
                raise CredentialError('invalid_profile')
        result['session'] = copy.deepcopy(session)
    if revision:
        rev = value.get('revision')
        if type(rev) is not int or rev < 1:
            raise CredentialError('corrupt_vault')
        result['revision'] = rev
    return result


def _metadata(profile):
    result = {k: copy.deepcopy(profile[k]) for k in ('id', 'target', 'kind', 'revision') if k in profile}
    result.update(secret_fields=sorted(profile['secrets']), env_names=sorted(profile.get('env', {})),
                  has_session=bool(profile.get('session')))
    return result


class CredentialVault:
    def __init__(self, home):
        if os.name != 'nt':
            raise CredentialError('unsupported_platform')
        self.home = _safe_path(home)
        from .project_local import validate_local_state
        try:
            local = validate_local_state(self.home)
        except ValueError:
            raise CredentialError('unsafe_path') from None
        if not local:
            _outside_git(self.home)
        self.directory = self.home / 'credentials'
        self.path = self.directory / 'vault.json'
        self.lock_path = self.directory / 'vault.lock'

    def _locked(self, timeout=None, cancel_requested=None):
        return _Lock(self, timeout, cancel_requested)

    def _load(self):
        _safe_path(self.path)
        if not self.path.exists():
            return {}
        data = _json(_crypt(_read(self.path), False))
        if not isinstance(data, dict) or set(data) != {'schema_version', 'credentials'} or type(data['schema_version']) is not int or data['schema_version'] != 1 or not isinstance(data['credentials'], list):
            raise CredentialError('corrupt_vault')
        entries = {}
        for item in data['credentials']:
            p = _profile(item, True)
            if p['id'] in entries:
                raise CredentialError('corrupt_vault')
            entries[p['id']] = p
        return entries

    def _save(self, entries):
        raw = json.dumps({'schema_version': 1, 'credentials': list(entries.values())}, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
        if len(raw) > _MAX - 4096:
            raise CredentialError('invalid_data')
        encrypted = _crypt(raw, True)
        tmp = None
        try:
            fd, tmp = tempfile.mkstemp(prefix='.vault-', dir=self.directory)
            with os.fdopen(fd, 'wb') as stream:
                stream.write(encrypted)
                stream.flush()
                os.fsync(stream.fileno())
            _safe_path(self.path)
            _replace(tmp, self.path)
            tmp = None
        except OSError:
            raise CredentialError('storage_error') from None
        finally:
            if tmp:
                try: os.unlink(tmp)
                except OSError: pass

    def import_file(self, path):
        path = _safe_path(path)
        _outside_git(path.parent)
        raw = _json(_read(path))
        if not isinstance(raw, dict) or set(raw) != {'schema_version', 'credentials'} or type(raw['schema_version']) is not int or raw['schema_version'] != 1 or not isinstance(raw['credentials'], list):
            raise CredentialError('invalid_data')
        incoming = {}
        for item in raw['credentials']:
            p = _profile(item)
            if p['id'] in incoming:
                raise CredentialError('invalid_data')
            incoming[p['id']] = p
        with self._locked():
            entries = self._load()
            changed = False
            for ref, p in incoming.items():
                old = entries.get(ref)
                if old and old['target'] != p['target']:
                    raise CredentialError('target_mismatch')
                if old and {k: v for k, v in old.items() if k != 'revision'} == p:
                    incoming[ref] = old
                    continue
                p['revision'] = old['revision'] + 1 if old else 1
                entries[ref] = p
                changed = True
            if changed: self._save(entries)
        return [_metadata(p) for p in incoming.values()]

    def list(self):
        with self._locked():
            return [_metadata(p) for p in self._load().values()]

    def resolve(self, ref, target, *, timeout=None, cancel_requested=None):
        ref, target = validate_access_ref(ref), normalize_target(target)
        with self._locked(timeout, cancel_requested):
            p = self._load().get(ref)
            if not p: raise CredentialError('not_found')
            if p['target'] != target: raise CredentialError('target_mismatch')
            return copy.deepcopy(p)

    def metadata(self, ref, target, *, timeout=None, cancel_requested=None):
        return _metadata(self.resolve(ref, target, timeout=timeout, cancel_requested=cancel_requested))

    def configure(self, ref, target, config):
        if not isinstance(config, dict) or set(config) - {'env', 'session'}:
            raise CredentialError('invalid_profile')
        def update(p):
            values = copy.deepcopy(config)
            if 'session' in values and values['session'] is None:
                p.pop('session', None)
                del values['session']
            p.update(values)
        return self._change(ref, target, update)

    def update_secrets(self, ref, target, changes, expected_revision, *, timeout=None, cancel_requested=None):
        if not isinstance(changes, dict) or not changes or type(expected_revision) is not int:
            raise CredentialError('invalid_profile')
        def update(p):
            if p['revision'] != expected_revision: raise CredentialError('revision_conflict')
            if not changes.keys() <= p['secrets'].keys(): raise CredentialError('invalid_profile')
            p['secrets'].update(changes)
        return self._change(ref, target, update, timeout, cancel_requested)

    def _change(self, ref, target, change, timeout=None, cancel_requested=None):
        ref, target = validate_access_ref(ref), normalize_target(target)
        with self._locked(timeout, cancel_requested):
            entries = self._load()
            p = entries.get(ref)
            if not p: raise CredentialError('not_found')
            if p['target'] != target: raise CredentialError('target_mismatch')
            old = copy.deepcopy(p)
            change(p)
            p = _profile(p, True)
            if p != old:
                p['revision'] += 1
                entries[ref] = p
                self._save(entries)
            return _metadata(p)


class _Lock:
    def __init__(self, vault, timeout=None, cancel_requested=None):
        self.vault = vault
        self.timeout = 30 if timeout is None else timeout
        self.cancel_requested = cancel_requested
    def __enter__(self):
        import msvcrt
        self.stream = None
        try:
            _safe_path(self.vault.directory)
            self.vault.directory.mkdir(parents=True, exist_ok=True)
            _safe_path(self.vault.lock_path)
            self.stream = self.vault.lock_path.open('a+b')
            deadline = time.monotonic() + self.timeout
            while True:
                if self.cancel_requested is not None and self.cancel_requested():
                    raise CredentialError('cancelled')
                if time.monotonic() >= deadline:
                    raise CredentialError('timeout')
                try:
                    self.stream.seek(0); msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1); break
                except OSError:
                    time.sleep(min(.05, max(0, deadline - time.monotonic())))
            # Windows permits locking past EOF. Initialize only while owning
            # byte zero, so another holder cannot reject an unguarded write.
            self.stream.seek(0, 2)
            if self.stream.tell() == 0:
                self.stream.write(b'0'); self.stream.flush()
            return self
        except CredentialError:
            if self.stream: self.stream.close()
            raise
        except OSError:
            if self.stream: self.stream.close()
            raise CredentialError('storage_error') from None
    def __exit__(self, *args):
        import msvcrt
        try:
            self.stream.seek(0); msvcrt.locking(self.stream.fileno(), msvcrt.LK_UNLCK, 1)
        finally: self.stream.close()
