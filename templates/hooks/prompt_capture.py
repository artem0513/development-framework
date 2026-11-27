"""Claude Code UserPromptSubmit hook: record the owner's prompt via `ase prompt capture`.

Runs with an explicit interpreter and path so the hook command needs no `cd`, `&&` or
environment syntax (Claude Code runs hook commands through its POSIX shell, Git Bash on Windows). It prints nothing
(hook stdout would be added to the agent context) and never blocks the prompt.
"""
import argparse
import contextlib
import io
import sys
from pathlib import Path

FRAMEWORK = Path(__file__).resolve().parents[2]


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--repo', required=True)
    args = parser.parse_args(argv)
    sys.path.insert(0, str(FRAMEWORK))
    try:
        from ase.__main__ import main as ase_main
        # Error JSON from the CLI would also reach the agent context, so all output is dropped.
        with contextlib.redirect_stdout(io.StringIO()):
            ase_main(['--repo', args.repo, 'prompt', 'capture'])
    except Exception:  # A capture failure must not stop the owner's prompt.
        pass
    return 0


if __name__ == '__main__':
    sys.exit(main())
