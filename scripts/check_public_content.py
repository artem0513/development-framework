"""Check that tracked framework files contain no trace of real projects, people or machines.

Scans the text files that git tracks: the working tree (default), the index (`--index`) or one
commit (`--commit`). A folder that is not a git work tree (such as a `git archive` export) is
scanned file by file.

Built-in generic checks: user-home paths, the scanned checkout's own absolute location, email
addresses outside the documentation domains, and any tracked file under `outputs/`. An optional private denylist adds one case-insensitive
regex per line (`#` starts a comment). It lives outside the repository: `ASE_PUBLIC_DENYLIST`,
else `~/.config/development-framework/public-denylist.txt`. Its patterns are never printed;
a hit shows only the file, the line number and the matched text.

A line containing `public-check: allow` is exempt; keep such fixtures tiny.
Exit status: 0 clean, 1 hits found, 2 usage or configuration error.
"""
import argparse
import os
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
DENYLIST_ENV = 'ASE_PUBLIC_DENYLIST'
DEFAULT_DENYLIST = Path('~/.config/development-framework/public-denylist.txt')
ALLOW_MARKER = 'public-check: allow'
OUTPUTS_PREFIX = 'outputs/'
# Bytes read to decide whether a file is binary (git uses the same NUL heuristic).
BINARY_SNIFF = 8192
# git's file mode for a submodule entry: its content lives in another repository.
SUBMODULE_MODE = '160000'
CONFIG_ERROR = 2
# A user name segment: placeholders such as <name>, %USERNAME% or $HOME never match it.
_NAME = r'[A-Za-z0-9][\w.-]*'
# Generic stand-ins that name no real account.
GENERIC_HOME_NAMES = {'public', 'default', 'user', 'username', 'name', 'you', 'me', 'example', 'runner'}
HOME_PATTERNS = (
    re.compile(r'(?<![\w])[A-Za-z]:[\\/]+Users[\\/]+(' + _NAME + ')', re.I),
    re.compile(r'(?<![\w.:/])/(?:[A-Za-z]/)?Users/(' + _NAME + ')'),
    re.compile(r'(?<![\w.:/])/home/(' + _NAME + ')'),
)
# A backslash before the local part is an escape (a newline before `@decorator`), not an address.
EMAIL = re.compile(r'(?<![\w.%+\\-])([A-Za-z0-9._%+-]+)@((?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,})(?![\w-])')
# Documentation domains (RFC 2606) and GitHub's private commit address.
ALLOWED_EMAIL_DOMAINS = ('example.com', 'example.org', 'example.net', 'users.noreply.github.com')
# Reserved top-level names (RFC 2606, RFC 6761) can never reach a real mailbox.
RESERVED_EMAIL_TLDS = ('example', 'invalid', 'test', 'localhost')
# Unmodified third-party data whose own credits name its upstream contributors (hash-pinned in provenance.json).
VENDORED_UPSTREAM = ('ase/data/timezones/',)
# `git@host:owner/repo` is an SSH remote, not a mailbox.
SSH_REMOTE_USER = 'git'


class ConfigError(SystemExit):
    def __init__(self, message):
        print(message, file=sys.stderr)
        super().__init__(CONFIG_ERROR)


def _git(top, *args, data=None):
    done = subprocess.run(['git', '-C', str(top), *args], input=data, capture_output=True,
                          stdin=None if data is not None else subprocess.DEVNULL)
    if done.returncode:
        raise ConfigError('git ' + ' '.join(args[:2]) + ' failed: ' + done.stderr.decode('utf-8', 'replace').strip())
    return done.stdout


def _is_work_tree(top):
    done = subprocess.run(['git', '-C', str(top), 'rev-parse', '--show-toplevel'], capture_output=True,
                          stdin=subprocess.DEVNULL)
    return done.returncode == 0 and Path(done.stdout.decode().strip()).resolve() == Path(top).resolve()


def _text(data):
    if b'\0' in data[:BINARY_SNIFF]:
        return None
    return data.decode('utf-8', 'replace')


def _blobs(top, entries):
    """Yield (path, text or None) for (path, object id) pairs read in one git process."""
    if not entries:
        return
    out = _git(top, 'cat-file', '--batch', data=''.join(oid + '\n' for _, oid in entries).encode('ascii'))
    offset = 0
    for name, _ in entries:
        header_end = out.index(b'\n', offset)
        size = int(out[offset:header_end].split()[2])
        body = out[header_end + 1:header_end + 1 + size]
        offset = header_end + 1 + size + 1
        yield name, _text(body)


def _entries(listing, oid_field):
    """Parse `-z` listings of ls-tree (`mode type oid<TAB>path`) or ls-files -s (`mode oid stage<TAB>path`)."""
    entries = []
    for record in listing.decode('utf-8').split('\0'):
        if '\t' not in record:
            continue
        meta, name = record.split('\t', 1)
        fields = meta.split()
        if fields[0] != SUBMODULE_MODE:
            entries.append((name, fields[oid_field]))
    return entries


def tracked_files(top, commit=None, index=False):
    """Yield (relative posix path, text or None for binary) for every tracked file.

    Default: the working-tree copy of each tracked file. `commit`: that commit's tree.
    `index`: the staged content (what a pre-commit hook is about to record).
    """
    top = Path(top)
    if commit is not None:
        yield from _blobs(top, _entries(_git(top, 'ls-tree', '-r', '-z', '--full-tree', commit), 2))
        return
    if index:
        yield from _blobs(top, _entries(_git(top, 'ls-files', '-s', '-z'), 1))
        return
    if not _is_work_tree(top):
        for path in sorted(p for p in top.rglob('*') if p.is_file() and '.git' not in p.relative_to(top).parts):
            yield path.relative_to(top).as_posix(), _text(path.read_bytes())
        return
    for name in _git(top, 'ls-files', '-z').decode('utf-8').split('\0'):
        if name:
            path = top / name
            # A deletion not yet staged leaves nothing to publish from the work tree.
            yield name, _text(path.read_bytes()) if path.is_file() else ''


def denylist_path():
    configured = os.environ.get(DENYLIST_ENV)
    return Path(configured).expanduser() if configured else DEFAULT_DENYLIST.expanduser()


def load_denylist(path, required):
    """Compile the private patterns; errors name the line number, never the pattern."""
    if not path.is_file():
        if required or os.environ.get(DENYLIST_ENV):
            raise ConfigError('Private denylist not found (set ' + DENYLIST_ENV + ' or create the default file).')
        return []
    patterns = []
    for number, line in enumerate(path.read_text(encoding='utf-8-sig').splitlines(), 1):
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        try:
            patterns.append(re.compile(line, re.I))
        except re.error:
            raise ConfigError('Private denylist line ' + str(number) + ' is not a valid regex.')
    return patterns


def checkout_pattern(top):
    """This checkout's own absolute location, in any separator spelling or JSON escaping."""
    parts = [re.escape(part) for part in Path(top).resolve().parts[1:]]
    drive = Path(top).resolve().drive
    head = re.escape(drive) if drive else ''
    return re.compile(head + r'(?:[\\/]+)' + r'[\\/]+'.join(parts), re.I) if parts else None


def line_hits(line, private=(), vendored=False):
    """Return (check, matched text) for one line."""
    if ALLOW_MARKER in line:
        return []
    hits = []
    for pattern in HOME_PATTERNS:
        for match in pattern.finditer(line):
            if match.group(1).lower() not in GENERIC_HOME_NAMES:
                hits.append(('home-path', match.group(0)))
    for match in EMAIL.finditer(line):
        local, domain = match.group(1), match.group(2).lower()
        if any(domain == d or domain.endswith('.' + d) for d in ALLOWED_EMAIL_DOMAINS):
            continue
        if domain.rsplit('.', 1)[-1] in RESERVED_EMAIL_TLDS or local == SSH_REMOTE_USER or vendored:
            continue
        hits.append(('email', match.group(0)))
    for pattern in private:
        for match in pattern.finditer(line):
            if match.group(0):
                hits.append(('private-denylist', match.group(0)))
    return hits


def scan(top, commit=None, private=(), index=False):
    """Return a list of (path, line number or 0, check, matched text)."""
    found = []
    own = checkout_pattern(top)
    for name, text in tracked_files(top, commit, index):
        if name.startswith(OUTPUTS_PREFIX):
            found.append((name, 0, 'tracked-outputs', name))
        for check, matched in line_hits(name, private):
            found.append((name, 0, check + ' (path)', matched))
        if not text:
            continue
        vendored = name.startswith(VENDORED_UPSTREAM)
        for number, line in enumerate(text.splitlines(), 1):
            for check, matched in line_hits(line, private, vendored):
                found.append((name, number, check, matched))
            if own and ALLOW_MARKER not in line:
                found.extend((name, number, 'checkout-path', m.group(0)) for m in own.finditer(line))
    return found


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--repo', type=Path, default=ROOT, help='repository or exported folder (default: this one)')
    source = parser.add_mutually_exclusive_group()
    source.add_argument('--commit', help='scan this commit instead of the working tree')
    source.add_argument('--index', action='store_true', help='scan the staged content (pre-commit)')
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--no-denylist', action='store_true', help='run only the generic checks')
    group.add_argument('--require-denylist', action='store_true', help='fail when the private denylist is missing')
    a = parser.parse_args(argv)
    private = [] if a.no_denylist else load_denylist(denylist_path(), a.require_denylist)
    hits = scan(a.repo, a.commit, private, a.index)
    for name, number, check, matched in hits:
        print(f'{name}:{number}: {check}: {matched}')
    extra = 'private denylist (' + str(len(private)) + ' patterns)' if private else 'none'
    print(f'public-content check: {len(hits)} hit(s); extra patterns: {extra}', file=sys.stderr)
    return 1 if hits else 0


if __name__ == '__main__':
    raise SystemExit(main())
