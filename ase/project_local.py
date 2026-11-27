"""Explicit project-local configuration and the private persistent-state boundary."""
import json
import os
from pathlib import Path
import re
import stat
import sys
import tomllib


DIRECTORY = '.devframework'
STATE = 'local'
TARGETS = {'codex', 'claude', 'copilot'}
FIELDS = {'schema_version', 'mode', 'targets', 'timezone', 'log_retention_days'}
DOTNET_PACKAGE_GUARD = 'guard_dotnet_package_additions'
EFCORE_SCHEMA_GUARD = 'guard_efcore_schema_additions'
ARM_RESOURCE_GUARD = 'guard_arm_resource_additions'
ADDITION_GUARDS = {DOTNET_PACKAGE_GUARD, EFCORE_SCHEMA_GUARD, ARM_RESOURCE_GUARD}
OPTIONAL_FIELDS = {'repositories', *ADDITION_GUARDS}
# Marks a private installation in .git/info/exclude: the framework is invisible to that repository.
PRIVATE_BEGIN = '# autonomous-se private files begin (local only, never commit)'
PRIVATE_END = '# autonomous-se private files end'
FRAMEWORK_SOURCE = Path(__file__).resolve().parents[1]
FRAMEWORK_REMOTE = re.compile(r'^(?:https?://(?:[^@/]+@)?(?:www\.)?|ssh://git@|git@)github\.com[:/]'
                              r'artem0513/(?:DevelopmentFramework|development-framework)(?:\.git)?/?$', re.I)


def _plain_git(top, *args):
    import subprocess
    done = subprocess.run(['git', '-c', 'safe.directory=' + Path(top).as_posix(), '-C', str(top), *args],
                          capture_output=True, timeout=60, stdin=subprocess.DEVNULL)
    return done.stdout.decode('utf-8', 'replace') if done.returncode == 0 else None


def is_framework_source(top):
    """The owner's framework repository is the one place framework files are tracked and published."""
    top = Path(top).resolve()
    if top == FRAMEWORK_SOURCE:
        return True
    url = _plain_git(top, 'config', '--get', 'remote.origin.url')
    return bool(url and FRAMEWORK_REMOTE.search(url.strip()))


def private_patterns(top):
    """Exclude patterns of a private installation, or [] when there is none."""
    path = _plain_git(top, 'rev-parse', '--git-path', 'info/exclude')
    if not path:
        return []
    exclude = Path(top) / path.strip()
    try:
        text = exclude.read_text(encoding='utf-8')
    except (OSError, UnicodeError):
        return []
    if PRIVATE_BEGIN not in text:
        return []
    return [line.strip() for line in text.split(PRIVATE_BEGIN, 1)[1].split(PRIVATE_END, 1)[0].splitlines() if line.strip()]


def matches_private(pattern, path):
    """Match one anchored pattern written by the installer (`/file` or `/folder/`)."""
    literal = re.sub(r'\\(.)', r'\1', pattern.lstrip('/'))
    fold = (lambda value: value.casefold()) if os.name == 'nt' or sys.platform == 'darwin' else (lambda value: value)
    literal, path = fold(literal), fold(path)
    return path.startswith(literal) if literal.endswith('/') else path == literal


def validate_local_settings(value):
    if (not isinstance(value, dict) or not FIELDS <= set(value) or set(value) - FIELDS - OPTIONAL_FIELDS
            or type(value['schema_version']) is not int or value['schema_version'] != 1
            or value['mode'] != 'project-local'):
        raise ValueError('Invalid project-local configuration schema')
    repositories = value.get('repositories', [])
    if (not isinstance(repositories, list) or len(repositories) > 100
            or not all(isinstance(item, str) and 0 < len(item) <= 500 for item in repositories)):
        raise ValueError('Project-local repositories must be a list of workspace-relative paths')
    for key in sorted(ADDITION_GUARDS & value.keys()):
        if type(value[key]) is not bool:
            raise ValueError('Project-local ' + key + ' must be a boolean')
    targets = value['targets']
    if (not isinstance(targets, list) or not targets or not all(isinstance(t, str) and t in TARGETS for t in targets)
            or len(targets) != len(set(targets))):
        raise ValueError('Project-local targets must be unique supported backends')
    days = value['log_retention_days']
    if type(days) is not int or not 1 <= days <= 365:
        raise ValueError('Project-local log_retention_days must be an integer from 1 to 365')
    timezone = value['timezone']
    if not isinstance(timezone, str) or not timezone or len(timezone) > 100:
        raise ValueError('Project-local timezone must be local or an IANA name')
    from .timezones import resolve_timezone
    resolve_timezone(timezone)
    return dict(value)


def config_bytes(targets, timezone='local', log_retention_days=30):
    value = validate_local_settings({'schema_version': 1, 'mode': 'project-local', 'targets': targets,
                       'timezone': timezone, 'log_retention_days': log_retention_days})
    return ('# DevelopmentFramework project settings; no secrets.\n'
        'schema_version = 1\nmode = "project-local"\ntargets = ' + json.dumps(value['targets']) +
        '\ntimezone = ' + json.dumps(value['timezone']) +
        '\nlog_retention_days = ' + str(value['log_retention_days']) + '\n').encode('utf-8')


def _managed(root, path):
    """Reject redirected or shared managed descendants before reading or writing."""
    root, path = Path(root), Path(path)
    if not path.is_relative_to(root):
        raise ValueError('Project-local path escapes selected project')
    items = [root]
    for part in path.relative_to(root).parts:
        items.append(items[-1] / part)
    for item in items:
        try:
            info = item.lstat()
        except FileNotFoundError:
            continue
        if (stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400
                or (stat.S_ISREG(info.st_mode) and info.st_nlink != 1)):
            raise ValueError('Linked project-local managed paths are not supported')
    if os.path.normcase(str(path.resolve())) != os.path.normcase(str(path.absolute())):
        raise ValueError('Project-local managed path resolves elsewhere')


def find_local_config(start=None):
    """Read the nearest explicitly adopted project, without creating state."""
    start = Path.cwd() if start is None else Path(start).expanduser()
    anchor = start.resolve()
    if anchor.is_file():
        anchor = anchor.parent
    for root in [anchor, *anchor.parents]:
        owned = root / DIRECTORY
        path = owned / 'config.toml'
        if not path.exists() and not path.is_symlink():
            continue
        _managed(root, path)
        if not path.is_file() or path.stat().st_size > 65536:
            raise ValueError('Invalid project-local configuration file')
        try:
            settings = validate_local_settings(tomllib.loads(path.read_bytes().decode('utf-8')))
        except (UnicodeError, tomllib.TOMLDecodeError, KeyError, TypeError) as error:
            raise ValueError('Invalid project-local configuration') from error
        return {'root': root, **settings}
    return None


def enabled_addition_guards(root):
    config = find_local_config(root)
    return {key for key in ADDITION_GUARDS if config and config['root'] == root and config.get(key)}


def is_private_path(relative):
    parts = str(relative).replace('\\', '/').lower().split('/')
    return any(parts[index:index + 2] == [DIRECTORY, STATE] for index in range(len(parts) - 1))


def validate_local_state(home, project_root=None):
    """Admit only the exact protected state home, never another project's home.

    Ordinary outside-project shared homes return False; callers retain their
    existing outside-root check. A reserved local candidate fails closed.
    """
    home = Path(os.path.abspath(Path(home).expanduser()))
    resolved = home.resolve()
    if is_private_path(home) and not (home.name.lower() == STATE and home.parent.name.lower() == DIRECTORY):
        raise ValueError('Only the exact project-local state home may be selected')
    if is_private_path(resolved) and not (resolved.name.lower() == STATE and resolved.parent.name.lower() == DIRECTORY):
        raise ValueError('Private project state descendants cannot be another state home')
    if (resolved.name.lower() == STATE and resolved.parent.name.lower() == DIRECTORY
            and os.path.normcase(str(resolved)) != os.path.normcase(str(home))):
        raise ValueError('Linked aliases of project-local state are not supported')
    if home.name.lower() != STATE or home.parent.name.lower() != DIRECTORY:
        return False
    root = home.parent.parent.resolve()
    config = find_local_config(root)
    if config is None or config['root'] != root:
        raise ValueError('Project-local state requires its explicit project configuration')
    if project_root is not None and Path(project_root).expanduser().resolve() != root:
        raise ValueError('Project-local state belongs to a different project')
    _managed(root, home)
    if home.exists() and not home.is_dir():
        raise ValueError('Project-local state must be a directory')
    ignore = root / DIRECTORY / '.gitignore'
    _managed(root, ignore)
    if not ignore.is_file() or ignore.stat().st_size > 65536:
        raise ValueError('Project-local private state requires ignore protection')
    try:
        lines = ignore.read_text(encoding='utf-8').splitlines()
    except UnicodeError as error:
        raise ValueError('Invalid project-local ignore protection') from error
    meaningful = [line for line in lines if line.strip() and not line.lstrip().startswith('#')]
    if not meaningful or meaningful[-1] != '/local/':
        raise ValueError('Project-local ignore protection must finish with /local/')
    if any((parent / '.git').exists() for parent in [root, *root.parents]):
        # The safe inspector disables hooks, filters, fsmonitor and global config.
        from .inspection import _run
        # The directory itself must be excluded: a single ignored probe can
        # coexist with selectively unignored databases or credentials beneath it.
        _run(root, ['check-ignore', '--no-index', '--quiet', '--', '.devframework/local/'])
        tracked = _run(root, ['ls-files', '--cached', '-z']).decode('utf-8', 'strict').split('\0')
        if any(is_private_path(path) for path in tracked if path):
            raise ValueError('Private project-local state must not be tracked by Git')
    return True
