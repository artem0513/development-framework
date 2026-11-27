"""Private one-shot HTTP observation worker; invoked with isolated Python."""
import base64
import http.client
import ipaddress
import json
import re
import sys
from urllib.parse import quote, quote_plus, urlsplit

_LIMIT = 65536
_SECRET = re.compile(r'(?i)(?:sk-[a-z0-9_-]{16,}|gh[pousr]_[a-z0-9_]{16,}|Bearer\s+[a-z0-9._-]{12,}|(?:password|api[_-]?key|token|secret)\s*[=:]\s*[^\s,;]{8,})')


def _sanitize(value, secrets):
    variants = set()
    for secret in secrets:
        if isinstance(secret, str) and secret:
            variants.update((secret, quote(secret, safe=''), quote_plus(secret),
                base64.b64encode(secret.encode()).decode(), json.dumps(secret, ensure_ascii=True)[1:-1]))
    for secret in sorted(variants, key=len, reverse=True):
        value = value.replace(secret, '[REDACTED]')
    value = re.sub(r'(?i)(https?://)[^/@\s]+:[^/@\s]+@', r'\1[REDACTED]@', value)
    value = re.sub(r'(?i)([?&](?:token|api[_-]?key|secret|password)=)[^&\s]+', r'\1[REDACTED]', value)
    return _SECRET.sub('[REDACTED]', value)


def main():
    conn = None
    try:
        data = json.loads(sys.stdin.buffer.read(10 * 1024 * 1024))
        origin = urlsplit(data['origin'])
        if (origin.scheme not in ('http', 'https') or not origin.hostname or origin.username is not None
                or origin.password is not None or origin.path or origin.query or origin.fragment
                or data['method'] not in ('GET', 'HEAD') or not isinstance(data['path'], str)
                or not data['path'].startswith('/') or data['path'].startswith('//') or '#' in data['path']):
            raise ValueError()
        if origin.scheme == 'http' and origin.hostname != 'localhost' and not ipaddress.ip_address(origin.hostname).is_loopback:
            raise ValueError()
        conn_class = http.client.HTTPSConnection if origin.scheme == 'https' else http.client.HTTPConnection
        conn = conn_class(origin.hostname, origin.port)
        conn.request(data['method'], data['path'], headers={'Authorization': data['authorization']})
        response = conn.getresponse()
        if response.status == 401:
            result = {'error': 'authentication_rejected'}
        elif response.status == 403:
            result = {'error': 'access_denied'}
        elif 300 <= response.status < 400:
            result = {'error': 'redirect_rejected'}
        else:
            body = response.read(_LIMIT + 1)
            if len(body) > _LIMIT:
                result = {'error': 'output_limit'}
            else:
                result = {'status_code': response.status, 'body': _sanitize(body.decode('utf-8', 'replace'), data['secrets'])}
    except Exception:
        result = {'error': 'http_unavailable'}
    finally:
        if conn is not None:
            conn.close()
    sys.stdout.write(json.dumps(result, ensure_ascii=True))


if __name__ == '__main__':
    main()
