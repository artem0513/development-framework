"""Lazy credential access; secret-bearing process output is never returned as evidence."""
import base64
from contextlib import contextmanager
import hashlib
import json
import os
import sys
from pathlib import Path
import threading
import time
from urllib.parse import quote, quote_plus, urlsplit, urljoin

from . import core
from .credentials import CredentialError, normalize_target, validate_access_ref
from .runner import capture_process

_LOCKS = {}
_LOCKS_GUARD = threading.Lock()
_MAX_OUTPUT = 65536


def _failure(code):
    return CredentialError(code.lower())


def _remaining(deadline, cancel):
    if cancel is not None and cancel():
        raise _failure('CANCELLED')
    left = deadline - time.monotonic()
    if left <= 0:
        raise _failure('TIMEOUT')
    return left


@contextmanager
def _profile_lock(vault, ref, deadline, cancel):
    identity = os.path.normcase(os.path.abspath(str(getattr(vault, 'home', getattr(vault, 'root', vault))))) + ':' + ref
    key = hashlib.sha256(identity.encode()).hexdigest()
    with _LOCKS_GUARD:
        lock = _LOCKS.setdefault(key, threading.Lock())
    while not lock.acquire(timeout=min(0.05, _remaining(deadline, cancel))):
        pass
    handle = None
    try:
        if os.name == 'nt':
            import ctypes
            from ctypes import wintypes
            kernel = ctypes.WinDLL('kernel32', use_last_error=True)
            kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
            kernel.CreateMutexW.restype = wintypes.HANDLE
            kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
            kernel.WaitForSingleObject.restype = wintypes.DWORD
            kernel.ReleaseMutex.argtypes = [wintypes.HANDLE]
            kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            handle = kernel.CreateMutexW(None, False, 'Local\\ASE-credential-' + key)
            if not handle:
                raise _failure('LOCK_UNAVAILABLE')
            while True:
                result = kernel.WaitForSingleObject(handle, min(50, max(1, int(_remaining(deadline, cancel) * 1000))))
                if result in (0, 0x80):
                    break
                if result != 0x102:
                    raise _failure('LOCK_UNAVAILABLE')
        yield
    finally:
        if handle:
            kernel.ReleaseMutex(handle)
            kernel.CloseHandle(handle)
        lock.release()


def _env(profile):
    from .child_environment import child_environment
    result = child_environment()  # a profile's own secrets only, never a backend's API key
    for name, field in profile.get('env', {}).items():
        result[name] = profile['secrets'][field]
    return result


def _secret_variants(values):
    variants = set()
    for value in values:
        if not isinstance(value, str) or not value:
            continue
        variants.update((value, quote(value, safe=''), quote_plus(value), base64.b64encode(value.encode()).decode(), json.dumps(value, ensure_ascii=True)[1:-1]))
    return sorted(variants, key=len, reverse=True)


def _profile_secrets(profile):
    values = list(profile['secrets'].values())
    if profile['kind'] == 'http-basic':
        values.append(profile['secrets']['username'] + ':' + profile['secrets']['password'])
    return values


def _sanitize(value, secrets):
    result = str(value)
    for secret in _secret_variants(secrets):
        result = result.replace(secret, '[REDACTED]')
    return core.redact(result)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate key')
        result[key] = value
    return result


def _check_argv(argv, secrets):
    if not isinstance(argv, (list, tuple)) or not argv or not all(isinstance(x, str) and '\0' not in x for x in argv):
        raise _failure('INVALID_COMMAND')
    variants = _secret_variants(secrets)
    if any(secret in arg for arg in argv for secret in variants):
        raise _failure('SECRET_IN_ARGUMENT')


class CredentialAccess:
    def __init__(self, vault):
        self.vault = vault

    def _protocol(self, recipe, profile, deadline, cancel, launch_guard=None):
        argv = recipe['argv']
        _check_argv(argv, _profile_secrets(profile))
        if not Path(argv[0]).is_absolute():
            raise _failure('CLI_UNAVAILABLE')
        try:
            result = capture_process(argv, self.vault.home, timeout=min(recipe.get('timeout', 60), _remaining(deadline, cancel)),
                                     max_output_bytes=_MAX_OUTPUT, cancel_requested=cancel, env=_env(profile), launch_guard=launch_guard)
        except CredentialError:
            raise
        except (OSError, ValueError):
            raise _failure('CLI_UNAVAILABLE') from None
        if result['cancelled']:
            raise _failure('CANCELLED')
        if result['timed_out']:
            raise _failure('TIMEOUT')
        if result['error']:
            raise _failure('CLI_UNAVAILABLE')
        if result['output_limited'] or result['exit_code'] != 0:
            raise _failure('CLI_FAILED')
        try:
            payload = json.loads(result['stdout'].decode('utf-8', 'strict'), object_pairs_hook=_unique_object)
        except (ValueError, UnicodeError, RecursionError):
            raise _failure('INVALID_CLI_RESPONSE') from None
        if not isinstance(payload, dict) or not isinstance(payload.get('status'), str) or payload.get('status') not in {'authenticated', 'login_required', 'interaction_required', 'unavailable'}:
            raise _failure('INVALID_CLI_RESPONSE')
        return payload

    def _ensure(self, ref, target, deadline, cancel, launch_guard=None):
        validate_access_ref(ref)
        target = normalize_target(target)
        with _profile_lock(self.vault, ref, deadline, cancel):
            _remaining(deadline, cancel)
            profile = self.vault.resolve(ref, target, timeout=_remaining(deadline, cancel), cancel_requested=cancel)
            sensitive = _profile_secrets(profile)
            session = profile.get('session')
            status = 'prepared'
            if session:
                probe = self._protocol(session['probe'], profile, deadline, cancel, launch_guard)
                status = probe['status']
                if status == 'login_required':
                    login = self._protocol(session['login'], profile, deadline, cancel, launch_guard)
                    if login['status'] != 'authenticated':
                        raise _failure({'interaction_required':'INTERACTION_REQUIRED', 'unavailable':'CLI_UNAVAILABLE', 'login_required':'LOGIN_REQUIRED'}[login['status']])
                    changes = login.get('secrets', {})
                    if not isinstance(changes, dict) or any(k not in profile['secrets'] or not isinstance(v, str) or not v for k, v in changes.items()):
                        raise _failure('INVALID_CLI_RESPONSE')
                    if changes:
                        self.vault.update_secrets(ref, target, changes, profile['revision'], timeout=_remaining(deadline, cancel), cancel_requested=cancel)
                        profile = self.vault.resolve(ref, target, timeout=_remaining(deadline, cancel), cancel_requested=cancel)
                        sensitive.extend(_profile_secrets(profile))
                    status = self._protocol(session['probe'], profile, deadline, cancel, launch_guard)['status']
                if status != 'authenticated':
                    raise _failure({'interaction_required':'INTERACTION_REQUIRED', 'unavailable':'CLI_UNAVAILABLE', 'login_required':'LOGIN_REQUIRED'}[status])
            return profile, {'credential_ref': ref, 'target': target, 'revision': profile['revision'], 'status': status}, sensitive

    def ensure(self, ref, target, timeout=60, cancel_requested=None, launch_guard=None):
        if type(timeout) not in (int, float) or timeout <= 0:
            raise _failure('INVALID_TIMEOUT')
        return self._ensure(ref, target, time.monotonic() + timeout, cancel_requested, launch_guard)[1]

    def run(self, ref, target, argv, cwd, log, timeout=300, cancel_requested=None, launch_guard=None, effect_launch_guard=None):
        if type(timeout) not in (int, float) or timeout <= 0:
            raise _failure('INVALID_TIMEOUT')
        deadline = time.monotonic() + timeout
        profile, metadata, secrets = self._ensure(ref, target, deadline, cancel_requested, launch_guard)
        _check_argv(argv, secrets)
        receipt = core.execute(argv, cwd, log, timeout=_remaining(deadline, cancel_requested), env=_env(profile),
                               sensitive_values=secrets, cancel_requested=cancel_requested, max_output_bytes=1048576,
                               launch_guard=effect_launch_guard if effect_launch_guard is not None else launch_guard)
        receipt['access'] = {k: metadata[k] for k in ('credential_ref', 'target', 'revision')}
        return receipt

    def request(self, ref, target, path='/', method='GET', timeout=30, cancel_requested=None, launch_guard=None):
        if type(timeout) not in (int, float) or timeout <= 0:
            raise _failure('INVALID_TIMEOUT')
        if method not in {'GET', 'HEAD'} or not isinstance(path, str) or not path.startswith('/') or path.startswith('//'):
            raise _failure('INVALID_REQUEST')
        deadline = time.monotonic() + timeout
        profile, metadata, secrets = self._ensure(ref, target, deadline, cancel_requested, launch_guard)
        origin = metadata['target']
        url = urljoin(origin + '/', path)
        parts = urlsplit(url)
        if parts.fragment or normalize_target(parts.scheme + '://' + parts.netloc) != origin:
            raise _failure('INVALID_REQUEST')
        if any(v in path for v in _secret_variants(secrets)):
            raise _failure('SECRET_IN_ARGUMENT')
        if profile['kind'] == 'http-basic':
            token = base64.b64encode((profile['secrets']['username'] + ':' + profile['secrets']['password']).encode()).decode()
            authorization = 'Basic ' + token
            secrets.append(token)
        elif profile['kind'] == 'http-bearer':
            authorization = 'Bearer ' + profile['secrets']['token']
        else:
            raise _failure('HTTP_AUTH_UNSUPPORTED')
        # A separately owned process can be killed even while the platform DNS
        # resolver is blocked. Credentials travel only over its private stdin;
        # its stdout contains sanitized response data or a generic error code.
        worker_input = json.dumps({'origin': origin, 'path': parts.path + (('?' + parts.query) if parts.query else ''),
                                   'method': method, 'authorization': authorization, 'secrets': secrets}).encode('utf-8')
        worker = Path(__file__).with_name('_http_worker.py').resolve()
        captured = capture_process([sys.executable, '-I', '-S', str(worker)], self.vault.home,
                                   timeout=_remaining(deadline, cancel_requested), max_output_bytes=_MAX_OUTPUT * 6 + 4096,
                                   input_bytes=worker_input, cancel_requested=cancel_requested, launch_guard=launch_guard)
        if captured['cancelled']:
            raise _failure('CANCELLED')
        if captured['timed_out']:
            raise _failure('TIMEOUT')
        if captured['error'] or captured['exit_code'] != 0 or captured['output_limited']:
            raise _failure('HTTP_UNAVAILABLE')
        try:
            result = json.loads(captured['stdout'].decode('utf-8'), object_pairs_hook=_unique_object)
            if not isinstance(result, dict):
                raise ValueError()
            if result.get('error') in {'redirect_rejected', 'output_limit', 'http_unavailable', 'authentication_rejected', 'access_denied'}:
                raise _failure(result['error'])
            if type(result.get('status_code')) is not int or not isinstance(result.get('body'), str):
                raise ValueError()
        except CredentialError:
            raise
        except (ValueError, UnicodeError, RecursionError):
            raise _failure('HTTP_UNAVAILABLE') from None
        _remaining(deadline, cancel_requested)
        result['body'] = _sanitize(result['body'], secrets)
        result['access'] = {k: metadata[k] for k in ('credential_ref', 'target', 'revision')}
        return result
