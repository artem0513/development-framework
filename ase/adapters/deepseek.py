"""DeepSeek chat-completions backend for packet-only independent roles.

There is no DeepSeek agent CLI here: the pinned executable is a Python interpreter running the
isolated worker beside this file, which makes one API call and prints one result line. The
packet travels on stdin, the schema in the run's schema artifact, and the API key only in the
DEEPSEEK_API_KEY environment variable. The model has no tools at all, so the packet-only
permission policy holds by construction; there is still no OS sandbox.
"""
import hashlib
from pathlib import Path
import sys
from urllib.parse import urlsplit

from . import invocation, probe_cli
from .deepseek_worker import DEFAULT_BASE_URL, LOOPBACK_HOSTS
from ..protocol import decode_json, normalized, packet_prompt

WORKER = Path(__file__).with_name('deepseek_worker.py')
# Listed by the provider's /models endpoint and answered a live packet on 2026-11-28.
DEFAULT_MODEL = 'deepseek-flash'
# DeepSeek holds no tools here, so it may only judge a packet; implementation stays with a host CLI.
ROLES = frozenset({'ac', 'review'})


def worker_args():
    """The executable arguments that run the worker in isolated mode (no site packages, no cwd imports)."""
    return ['-I', str(WORKER)]


def _worker_identity(argv_prefix):
    """The script the pinned interpreter actually runs (after -I) and its hash, or None for both."""
    if len(argv_prefix) == 3 and argv_prefix[1] == '-I' and Path(argv_prefix[2]).is_file():
        return {'worker': argv_prefix[2], 'worker_sha256': hashlib.sha256(Path(argv_prefix[2]).read_bytes()).hexdigest()}
    return {'worker': None, 'worker_sha256': None}


def _interpreter():
    """The interpreter the bundled worker is meant to run under: the one running ase."""
    return sys.executable


def _allowed_base_url(base_url, config):
    """The key goes to the provider's own host, a loopback test server, or a host the owner opted into."""
    return (base_url.rstrip('/') == DEFAULT_BASE_URL or urlsplit(base_url).hostname in LOOPBACK_HOSTS
            or config.get('allow_custom_base_url') is True)


class DeepSeekAdapter:
    name = 'deepseek'

    def probe(self, config, *, deadline=None, cancel_requested=None):
        caps = probe_cli(self.name, config, ['--model', '--schema-file', '--base-url'],
                         deadline=deadline, cancel_requested=cancel_requested)
        from ..child_environment import backend_key_present
        # Presence only: the value is never read here, logged or stored.
        has_key = backend_key_present(self.name)
        base_url = config.get('base_url') or DEFAULT_BASE_URL
        # The receipt records the interpreter and its hash; the script it runs is recorded here.
        caps.update(model=config.get('model') or DEFAULT_MODEL, base_url=base_url,
                    auth_availability='key-present' if has_key else 'key-missing', key_delivery=False,
                    permission_policy='packet-only; the model has no tools', **_worker_identity(caps['argv_prefix']))
        if not caps['available']:
            return caps
        interpreter = (caps['executable'] is not None and Path(caps['executable']).resolve() == Path(_interpreter()).resolve()
                       or config.get('allow_custom_interpreter') is True)
        blocker = ('BASE_URL_NOT_ALLOWED' if not _allowed_base_url(base_url, config)
                   else None if caps['protocol_only']
                   else 'WORKER_NOT_PINNED' if caps['worker'] != str(WORKER) or not interpreter
                   else 'API_KEY_MISSING' if not has_key else None)
        if blocker:
            caps.update(available=False, blocker=blocker)
        # Only this interpreter running the bundled worker, never a protocol fixture, receives the key.
        caps['key_delivery'] = caps['available'] and not caps['protocol_only']
        return caps

    def build_invocation(self, request, capabilities):
        if request.get('role') not in ROLES:
            raise ValueError('DEEPSEEK_ROLE_UNSUPPORTED: only the packet-only ac and review roles run on deepseek')
        call = invocation(capabilities, ['--model', capabilities.get('model') or DEFAULT_MODEL,
                                         '--schema-file', capabilities['artifact_paths']['schema'],
                                         '--base-url', capabilities.get('base_url') or DEFAULT_BASE_URL])
        call['stdin'] = packet_prompt(request)
        call['permission_description']['policy'] = 'packet-only; the model has no tools'
        return call

    def parse_result(self, raw_events, final_payload):
        if len(raw_events) != 1 or raw_events[0].get('type') != 'result':
            return normalized(blocker='TERMINAL_RESULT_MISSING')
        terminal = raw_events[0]
        session_id = terminal.get('session_id')
        if terminal.get('subtype') != 'success' or terminal.get('is_error') is not False:
            blocker = 'API_KEY_MISSING' if terminal.get('subtype') == 'api_key_missing' else 'BACKEND_ERROR'
            return normalized(blocker=blocker, session_id=session_id, terminal=terminal)
        if not isinstance(session_id, str) or not session_id:
            return normalized(blocker='SESSION_PROTOCOL_INVALID', terminal=terminal)
        try:
            payload = decode_json(terminal['result'])
        except (ValueError, TypeError, KeyError):
            return normalized(blocker='STRUCTURED_OUTPUT_INVALID', session_id=session_id, terminal=terminal)
        return normalized(payload, session_id=session_id, terminal=terminal)
