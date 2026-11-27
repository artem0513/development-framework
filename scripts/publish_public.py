"""Publish one commit of this private repository as a single release commit of the public repository.

1. Export the commit with `git archive` into a temporary folder, without `outputs/`.
2. Run `check_public_content.py` on the export (with the private denylist when present)
   and the test suite in the export.
3. Only when everything passes: fresh-clone the public repository, replace its tree with
   the export, make one commit "Release <label>" and push it to `main` without force.

The private repository's history and branches are never pushed: the public clone only ever
receives the new release commit on top of its own history. Publishing is refused unless the
configured git author and committer use a GitHub noreply address.
"""
import argparse
import io
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parents[1]
PUBLIC_REPOSITORY = 'artem0513/development-framework'
PUBLIC_URL = 'https://github.com/' + PUBLIC_REPOSITORY + '.git'
PUBLIC_BRANCH = 'main'
EXCLUDED = ('outputs',)
NOREPLY_SUFFIX = '@users.noreply.github.com'
CHECK = Path('scripts') / 'check_public_content.py'
TESTS = [str(Path('scripts') / 'run_tests.py'), 'discover', '-s', 'tests']


def git(top, *args, data=None, check=True, env=None):
    done = subprocess.run(['git', '-C', str(top), *args], input=data, capture_output=True,
                          stdin=None if data is not None else subprocess.DEVNULL,
                          env=None if env is None else {**os.environ, **env})
    if check and done.returncode:
        raise SystemExit('git ' + ' '.join(args[:2]) + ' failed: ' + done.stderr.decode('utf-8', 'replace').strip())
    return done


def ident(top, kind):
    """(name, email) of the identity git would record (`git var` honours both config and environment)."""
    line = git(top, 'var', 'GIT_' + kind + '_IDENT').stdout.decode('utf-8', 'replace')
    return line.split('<', 1)[0].strip(), line.split('<', 1)[-1].split('>', 1)[0].strip()


def require_noreply(top):
    """Return the verified identity as git environment variables, so every release commit uses it.

    The identity may live only in this repository's local config; the export and the public
    clone are new repositories that would not see it.
    """
    env = {}
    for kind in ('AUTHOR', 'COMMITTER'):
        name, email = ident(top, kind)
        if not email.lower().endswith(NOREPLY_SUFFIX):
            raise SystemExit('Refused: the git ' + kind.lower() + ' email is not a ' + NOREPLY_SUFFIX[1:]
                             + ' address. Set user.email to your GitHub noreply address first.')
        env.update({'GIT_' + kind + '_NAME': name, 'GIT_' + kind + '_EMAIL': email})
    return env


def excluded(name):
    first = name.split('/', 1)[0]
    return first in EXCLUDED


def export(archive, target):
    """Unpack the archive bytes into target, dropping excluded paths."""
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        members = [m for m in tar.getmembers() if not excluded(m.name) and m.name != 'pax_global_header']
        for member in members:
            # Python 3.11.0-3.11.3 lack extraction filters: refuse anything that leaves the folder.
            if member.issym() or member.islnk() or Path(member.name).is_absolute() or '..' in Path(member.name).parts:
                raise SystemExit('Refused: unexpected archive entry ' + member.name)
        tar.extractall(target, members=members, **({'filter': 'data'} if hasattr(tarfile, 'data_filter') else {}))


def remove_tree(path):
    def writable(function, name, _):
        os.chmod(name, stat.S_IWRITE)  # git marks object files read-only on Windows
        function(name)
    shutil.rmtree(path, onerror=writable)


def run(step, command, cwd):
    print('==> ' + step, flush=True)
    if subprocess.call(command, cwd=str(cwd), stdin=subprocess.DEVNULL):
        raise SystemExit('Refused: ' + step + ' failed; nothing was published.')


def replace_tree(clone, archive):
    for entry in clone.iterdir():
        if entry.name == '.git':
            continue
        remove_tree(entry) if entry.is_dir() and not entry.is_symlink() else entry.unlink()
    export(archive, clone)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--commit', default='HEAD', help='private commit to publish (default: HEAD)')
    parser.add_argument('--label', help='release label for the commit message (default: date and short hash)')
    parser.add_argument('--url', default=PUBLIC_URL, help='public repository URL (default: %(default)s)')
    parser.add_argument('--dry-run', action='store_true', help='prepare and verify the release commit, do not push')
    parser.add_argument('--keep', action='store_true', help='keep the temporary folder for inspection')
    a = parser.parse_args(argv)

    identity = require_noreply(ROOT)
    private_remotes = git(ROOT, 'remote', '-v').stdout.decode('utf-8', 'replace').split()
    if a.url in private_remotes:
        raise SystemExit('Refused: the target URL is a remote of this private repository.')
    sha = git(ROOT, 'rev-parse', '--verify', a.commit + '^{commit}').stdout.decode().strip()
    label = a.label or git(ROOT, 'show', '-s', '--format=%cs %h', sha).stdout.decode().strip()
    archive = git(ROOT, 'archive', '--format=tar', sha).stdout

    work = Path(tempfile.mkdtemp(prefix='ase-publish-'))
    try:
        tree = work / 'export'
        export(archive, tree)
        run('public-content check', [sys.executable, '-B', str(tree / CHECK), '--repo', str(tree)], tree)
        # The suite expects a git checkout; a throwaway local repository mirrors one.
        git(tree, 'init', '-q')
        git(tree, 'add', '-A')
        git(tree, 'commit', '-q', '--no-verify', '-m', 'export', env=identity)
        run('test suite', [sys.executable, *TESTS], tree)

        clone = work / 'public'
        print('==> fresh clone of ' + a.url, flush=True)
        done = subprocess.run(['git', 'clone', '-q', '--no-tags', a.url, str(clone)], stdin=subprocess.DEVNULL)
        if done.returncode:
            raise SystemExit('Refused: could not clone the public repository; nothing was published.')
        if git(clone, 'rev-parse', '--verify', '-q', 'refs/remotes/origin/' + PUBLIC_BRANCH, check=False).returncode == 0:
            git(clone, 'checkout', '-q', '-B', PUBLIC_BRANCH, 'origin/' + PUBLIC_BRANCH)
        else:
            git(clone, 'checkout', '-q', '--orphan', PUBLIC_BRANCH)
        replace_tree(clone, archive)
        git(clone, 'add', '-A')
        if not git(clone, 'status', '--porcelain').stdout.strip():
            print('The public repository already matches ' + sha[:12] + '; nothing to publish.')
            return 0
        git(clone, 'commit', '-q', '-m', 'Release ' + label, env=identity)
        recorded = git(clone, 'show', '-s', '--format=%ae%n%ce', 'HEAD').stdout.decode().split()
        if any(not email.lower().endswith(NOREPLY_SUFFIX) for email in recorded):
            raise SystemExit('Refused: the release commit does not carry the noreply identity; nothing was published.')
        run('public-content check of the release commit',
            [sys.executable, '-B', str(tree / CHECK), '--repo', str(clone), '--commit', 'HEAD'], clone)
        if a.dry_run:
            print('Dry run: release commit prepared in ' + str(clone) + '; not pushed.')
            a.keep = True
            return 0
        run('push to ' + PUBLIC_BRANCH, ['git', 'push', 'origin', 'HEAD:refs/heads/' + PUBLIC_BRANCH], clone)
        print('Published ' + sha[:12] + ' as "Release ' + label + '".')
        return 0
    finally:
        if a.keep:
            print('Temporary folder kept: ' + str(work))
        else:
            remove_tree(work)


if __name__ == '__main__':
    raise SystemExit(main())
