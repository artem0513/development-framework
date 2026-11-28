"""One packet-only DeepSeek chat completion; invoked with isolated Python (-I) by the DeepSeek adapter.

The prompt arrives on stdin and the JSON schema in a file, the API key only through the
DEEPSEEK_API_KEY environment variable: it never reaches argv, receipts or output. Prints one
JSON result line; an API refusal is a result with is_error true, never a traceback.
"""
import base64
import http.client
import ipaddress
import json
import os
import re
import sys
from urllib.parse import quote, quote_plus, urlsplit

VERSION = 'ase-deepseek-worker 1'
FLAGS = ('--model', '--schema-file', '--base-url')
KEY_VARIABLE = 'DEEPSEEK_API_KEY'
DEFAULT_BASE_URL = 'https://api.deepseek.com'
COMPLETIONS_PATH = '/chat/completions'
# Plain HTTP (no TLS for the key) is accepted only for these test-server hosts.
LOOPBACK_HOSTS = ('localhost', '127.0.0.1', '::1')
# The Runner's own timeout stops the whole process; this only bounds one stalled socket read.
SOCKET_TIMEOUT_SECONDS = 1800
# An API error body is diagnostic only; a bounded prefix is enough.
ERROR_BODY_CHARS = 2000
MAX_PROMPT_BYTES = 10 * 1024 * 1024
# Above the Runner's default 1 MiB output cap: a larger answer could never be captured whole anyway.
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
# Same generic secret shapes as the HTTP observation worker; this worker cannot import the package (-I).
_SECRET = re.compile(r'(?i)(?:sk-[a-z0-9_-]{16,}|Bearer\s+[a-z0-9._-]{12,}|(?:password|api[_-]?key|token|secret)\s*[=:]\s*[^\s,;]{8,})')


def _arguments(argv):
    values = {}
    items = iter(argv)
    for flag in items:
        if flag not in FLAGS:
            raise ValueError('unknown argument ' + flag)
        values[flag] = next(items)
    return values


def _connection(base_url):
    origin = urlsplit(base_url)
    if (origin.scheme not in ('http', 'https') or not origin.hostname or origin.username is not None
            or origin.password is not None or origin.path.strip('/') or origin.query or origin.fragment):
        raise ValueError('invalid base URL')
    if origin.scheme == 'http':
        # Plain HTTP would expose the key; only a loopback test server may use it.
        try:
            loopback = origin.hostname in LOOPBACK_HOSTS or ipaddress.ip_address(origin.hostname).is_loopback
        except ValueError:
            loopback = False
        if not loopback:
            raise ValueError('plain HTTP is allowed only for a loopback server')
        return http.client.HTTPConnection(origin.hostname, origin.port, timeout=SOCKET_TIMEOUT_SECONDS)
    return http.client.HTTPSConnection(origin.hostname, origin.port, timeout=SOCKET_TIMEOUT_SECONDS)


def _redacted(text, key):
    """Text without the key in raw, URL-quoted, base64 or JSON-escaped form, or any generic secret shape."""
    if key:
        variants = {key, quote(key, safe=''), quote_plus(key), base64.b64encode(key.encode()).decode(),
                    json.dumps(key, ensure_ascii=True)[1:-1]}
        for variant in sorted(variants, key=len, reverse=True):
            text = text.replace(variant, '[REDACTED]')
    return _SECRET.sub('[REDACTED]', text)


def _failure(subtype, message, key, **extra):
    return {'type': 'result', 'subtype': subtype, 'is_error': True, 'session_id': None,
            'error': _redacted(str(message), key)[:ERROR_BODY_CHARS], **extra}


def complete(prompt, schema, model, base_url, key):
    """The result line for one completion request."""
    if not key:
        return _failure('api_key_missing', KEY_VARIABLE + ' is not set for this process', key)
    system = ('Return exactly one JSON object and nothing else. It must satisfy this JSON schema; '
              'use only its fields: ' + json.dumps(schema, ensure_ascii=False, sort_keys=True))
    body = json.dumps({'model': model, 'stream': False, 'response_format': {'type': 'json_object'},
                       'messages': [{'role': 'system', 'content': system}, {'role': 'user', 'content': prompt}]},
                      ensure_ascii=False).encode('utf-8')
    connection = _connection(base_url)
    try:
        connection.request('POST', COMPLETIONS_PATH, body=body, headers={
            'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json', 'Accept': 'application/json'})
        response = connection.getresponse()
        raw = response.read(MAX_RESPONSE_BYTES + 1)
    finally:
        connection.close()
    if len(raw) > MAX_RESPONSE_BYTES:
        return _failure('response_too_large', f'response exceeds {MAX_RESPONSE_BYTES} bytes', key)
    text = raw.decode('utf-8', 'replace')
    if response.status != 200:
        return _failure('http_error', text, key, status=response.status)
    data = json.loads(text)
    choices = data.get('choices') if isinstance(data, dict) else None
    choice = choices[0] if isinstance(choices, list) and choices and isinstance(choices[0], dict) else None
    message = choice.get('message') if choice else None
    if not isinstance(message, dict):
        return _failure('malformed_response', 'no choices[0].message object in the response', key)
    content = message.get('content')
    common = {'session_id': data.get('id'), 'model': data.get('model'), 'usage': data.get('usage'),
              'finish_reason': choice.get('finish_reason')}
    if choice.get('finish_reason') != 'stop' or not isinstance(content, str):
        return {**_failure('incomplete_output', 'finish_reason ' + str(choice.get('finish_reason')), key), **common}
    return {'type': 'result', 'subtype': 'success', 'is_error': False, 'result': content, **common}


def main(argv):
    if argv == ['--version']:
        print(VERSION)
        return 0
    if argv == ['--help']:
        print('usage: deepseek_worker.py ' + ' '.join(flag + ' VALUE' for flag in FLAGS) + ' < prompt')
        return 0
    key = os.environ.get(KEY_VARIABLE, '')
    try:
        options = _arguments(argv)
        schema = json.loads(open(options['--schema-file'], encoding='utf-8').read())
        prompt = sys.stdin.buffer.read(MAX_PROMPT_BYTES + 1)
        if len(prompt) > MAX_PROMPT_BYTES:
            raise ValueError('prompt exceeds the byte limit')
        result = complete(prompt.decode('utf-8'), schema, options['--model'],
                          options.get('--base-url', DEFAULT_BASE_URL), key)
    except (OSError, ValueError, KeyError, StopIteration, RecursionError, http.client.HTTPException) as error:
        result = _failure('worker_error', f'{type(error).__name__}: {error}', key)
    sys.stdout.write(json.dumps(result, ensure_ascii=True) + '\n')
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
