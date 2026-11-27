"""Install local pre-commit and pre-push hooks that run the public-content check.

The hooks live in this clone's `.git/hooks` (never committed) and require the private
denylist, so a commit or push stops when a staged change or pushed commit leaks a local
path, an email address, a tracked `outputs/` file or a denylisted name. A hook that this
script did not write is preserved: integrate the check into it manually.
"""
import argparse
from pathlib import Path
import shlex
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
MARKER = '# development-framework public-content hook'
CHECK = ROOT / 'scripts' / 'check_public_content.py'


def hook_texts(python=sys.executable):
    # git runs hooks from the top of the work tree, so `.` is the clone being committed or pushed.
    check = shlex.join([Path(python).as_posix(), '-B', CHECK.as_posix(), '--require-denylist', '--repo', '.'])
    pre_commit = ('#!/bin/sh\n' + MARKER + '\nexec ' + check + ' --index\n')
    pre_push = ('#!/bin/sh\n' + MARKER + '\n'
                'while read local_ref local_sha remote_ref remote_sha; do\n'
                # An all-zero local sha deletes the remote ref: it pushes no content.
                '  case "$local_sha" in *[!0]*) ;; *) continue;; esac\n'
                '  ' + check + ' --commit "$local_sha" || exit 1\n'
                'done\n')
    return {'pre-commit': pre_commit, 'pre-push': pre_push}


def install(repo=ROOT, python=sys.executable):
    written, kept = [], []
    for name, text in hook_texts(python).items():
        path = Path(subprocess.check_output(['git', '-C', str(repo), 'rev-parse', '--git-path', 'hooks/' + name],
                                            text=True).strip())
        if not path.is_absolute():
            path = Path(repo) / path
        if path.exists() and MARKER not in path.read_text(encoding='utf-8', errors='replace'):
            kept.append(name)
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8', newline='\n')
        path.chmod(0o755)
        written.append(name)
    return written, kept


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--repo', type=Path, default=ROOT, help='clone to protect (default: this one)')
    a = parser.parse_args(argv)
    written, kept = install(a.repo.resolve())
    for name in written:
        print('Installed ' + name + ' hook.')
    for name in kept:
        print('Existing ' + name + ' hook preserved; add the public-content check to it manually.')
    return 1 if kept else 0


if __name__ == '__main__':
    raise SystemExit(main())
