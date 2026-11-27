"""Install an opt-in pre-push hook in exactly one repository; preserve existing hook."""
import argparse
from pathlib import Path
import shlex
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--repo', type=Path, required=True)
parser.add_argument('--task', required=True)
parser.add_argument('--home', type=Path, required=True)
a = parser.parse_args()
repo = a.repo.resolve(strict=True)
hook_path = subprocess.check_output(['git', '-C', str(repo), 'rev-parse', '--git-path', 'hooks/pre-push'], text=True).strip()
hook = Path(hook_path)
if not hook.is_absolute():
    hook = repo / hook
if hook.exists():
    raise SystemExit('Existing pre-push hook preserved. Integrate the gate manually after reviewing it.')
sys.path.insert(0, str(ROOT))
from ase.project_local import is_framework_source  # noqa: E402
top = Path(subprocess.check_output(['git', '-C', str(repo), 'rev-parse', '--show-toplevel'], text=True).strip()).resolve()
git_dir = Path(subprocess.check_output(['git', '-C', str(repo), 'rev-parse', '--absolute-git-dir'], text=True).strip()).resolve()
if not is_framework_source(top) and hook.resolve().is_relative_to(top) and not hook.resolve().is_relative_to(git_dir):
    # core.hooksPath points into the work tree (husky, lefthook): the hook would become a project file.
    raise SystemExit('The hooks folder is part of this repository; the framework stays private here, so no hook was written.')
command = [sys.executable, '-m', 'ase', '--repo', str(repo), '--home', str(a.home.resolve()), 'gate', a.task]
text = ('#!/bin/sh\n# Local accident barrier; server-side protection is still required.\n'
        'head=$(git rev-parse HEAD) || exit 1\n'
        'branch=$(git symbolic-ref --quiet --short HEAD) || exit 1\n'
        'case "$branch" in feature/*|bugfix/*) ;; *) echo "ASE: feature branches only" >&2; exit 1;; esac\n'
        'while read local_ref local_sha remote_ref remote_sha; do\n'
        '  test "$remote_ref" = "refs/heads/$branch" || { echo "ASE: destination must match verified feature branch" >&2; exit 1; }\n'
        '  test "$local_sha" = "$head" || { echo "ASE: only verified HEAD may be pushed" >&2; exit 1; }\n'
        'done\n'
        'PYTHONPATH=' + shlex.quote(str(ROOT)) + ' exec ' + shlex.join(command) + '\n')
hook.parent.mkdir(parents=True, exist_ok=True)
hook.write_text(text, encoding='utf-8', newline='\n')
hook.chmod(0o755)
print(str(hook))
