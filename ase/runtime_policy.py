"""Host-owned, versioned role guidance, separate from task source data.

Canonical skills generate the package resource. Never discover active policy
from a target project's files, cwd, environment, or an unresolved skill link.
"""
from pathlib import Path, PurePosixPath
import re

from .protocol import canonical, decode_json, sha256


BUNDLE_PATH = Path(__file__).with_name('data') / 'runtime_policy.json'
COMMON = ('skills/task-orchestrator/references/evidence-decisions.md',
          'skills/task-orchestrator/references/lead-critical-passes.md')
# Proportionality rules must reach independent sessions, which cannot read repository links.
DESIGN = ('skills/task-orchestrator/references/solution-design.md',
          'skills/task-orchestrator/references/solution-calibration.md')
ROLE_FILES = {
    # Only the original main bot runs lead-review commands; independent roles get the judging rules only.
    'implementation': ('skills/task-orchestrator/SKILL.md', *COMMON, 'skills/task-orchestrator/references/lead-review-cli.md', *DESIGN),
    'ac': ('skills/independent-ac-verifier/SKILL.md', *COMMON,
        'skills/task-orchestrator/references/profile-evidence.md'),
    'review': ('skills/independent-code-reviewer/SKILL.md', *COMMON,
        'skills/task-orchestrator/references/profile-evidence.md',
        'skills/quality-gate/references/test-adequacy.md', *DESIGN),
    # Read-only investigations recommend fixes, so they get the same solution rules.
    'diagnostic': ('skills/task-discovery/SKILL.md', *COMMON, *DESIGN),
}


def build_bundle(source_root):
    roles = {}
    for role, paths in ROLE_FILES.items():
        roles[role] = []
        for path in paths:
            content = (Path(source_root) / path).read_text(encoding='utf-8')
            roles[role].append({'path': path, 'content': content,
                'sha256': sha256(content.encode('utf-8'))})
    return {'version': 1, 'roles': roles}


def _files(files):
    if not isinstance(files, list) or not 1 <= len(files) <= 8:
        raise ValueError('Runtime policy requires bounded files')
    seen = set()
    for row in files:
        if not isinstance(row, dict) or set(row) != {'path', 'content', 'sha256'}:
            raise ValueError('Invalid runtime policy file')
        path, content = row['path'], row['content']
        if (not isinstance(path, str) or not path.startswith('skills/') or len(path) > 512
                or '\\' in path or '..' in PurePosixPath(path).parts or path in seen
                or not isinstance(content, str) or len(content.encode('utf-8')) > 65536):
            raise ValueError('Invalid runtime policy path/content')
        if row['sha256'] != sha256(content.encode('utf-8')):
            raise ValueError('Runtime policy content hash mismatch')
        seen.add(path)


def _bundle():
    try:
        if BUNDLE_PATH.is_symlink() or BUNDLE_PATH.stat().st_nlink > 1:
            raise ValueError('Runtime policy must be a regular package file')
        with BUNDLE_PATH.open('rb') as stream:
            raw = stream.read(262145)
        if len(raw) > 262144:
            raise ValueError('Runtime policy bundle exceeds limit')
        value = decode_json(raw)
        if (not isinstance(value, dict) or set(value) != {'version', 'roles'}
                or type(value['version']) is not int or value['version'] != 1
                or not isinstance(value['roles'], dict) or set(value['roles']) != set(ROLE_FILES)):
            raise ValueError('Invalid runtime policy bundle')
        for role, files in value['roles'].items():
            _files(files)
            if [row['path'] for row in files] != list(ROLE_FILES[role]):
                raise ValueError('Runtime policy role files differ from the package allowlist')
        return value
    except (OSError, UnicodeError, TypeError, KeyError) as error:
        raise ValueError('Runtime policy package is missing or invalid') from error


def _identity(bundle):
    # Guidance and its delivery semantics both invalidate existing approval.
    # Functional services now live in subpackages. Bind the complete installed
    # engine so moving logic out of a facade cannot leave old approval current.
    package = Path(__file__).parent
    engine = {path.relative_to(package).as_posix(): sha256(path.read_bytes())
              for path in sorted(package.rglob('*.py'))}
    return sha256(canonical({'bundle': bundle, 'engine': engine}))


def policy_fingerprint():
    return _identity(_bundle())


def current_policy(role):
    if role not in ROLE_FILES:
        raise ValueError('Unknown runtime policy role')
    bundle = _bundle()
    value = {'version': 1, 'prompt_version': 1, 'role': role,
        'policy_id': _identity(bundle), 'files': bundle['roles'][role]}
    return {**value, 'sha256': sha256(canonical(value))}


def validate_policy(request, *, require_current=True):
    handoff = request.get('handoff', {})
    if not isinstance(handoff, dict):
        raise ValueError('Invalid runtime policy handoff')
    value = handoff.get('runtime_policy')
    if value is None:
        if require_current:
            raise ValueError('Current runtime policy is missing')
        return {'policy_current': False, 'policy_id': None, 'policy_status': 'LEGACY_UNBOUND'}
    if (not isinstance(value, dict) or set(value) != {'version', 'prompt_version', 'role', 'policy_id', 'files', 'sha256'}
            or type(value['version']) is not int or value['version'] != 1
            or type(value['prompt_version']) is not int or value['prompt_version'] != 1 or value['role'] != request['role']
            or not isinstance(value['policy_id'], str) or not re.fullmatch('[0-9a-f]{64}', value['policy_id'])):
        raise ValueError('Invalid runtime policy identity or role')
    _files(value['files'])
    body = {key: item for key, item in value.items() if key != 'sha256'}
    if value['sha256'] != sha256(canonical(body)):
        raise ValueError('Runtime policy envelope hash mismatch')
    try:
        current = canonical(value) == canonical(current_policy(request['role']))
    except ValueError:
        if require_current:
            raise
        current = False
    if require_current and not current:
        raise ValueError('Runtime policy is stale or differs from the trusted package')
    return {'policy_current': current, 'policy_id': value['policy_id'],
        'policy_status': 'CURRENT' if current else 'HISTORICAL'}


def bind_policy(request):
    """Copy caller data; only a missing policy may be filled by the host."""
    handoff = request.get('handoff', {})
    if not isinstance(handoff, dict):
        raise ValueError('Invalid runtime policy handoff')
    value = {**request, 'handoff': {**handoff}}
    if 'runtime_policy' not in handoff:
        value['handoff']['runtime_policy'] = current_policy(request['role'])
    validate_policy(value)
    return value
