"""Run unittest with a physical interpreter path, preserving its result.

Hosted Python installations may expose an interpreter through a symlink. Test
fixtures pin sys.executable as a trusted host executable, whose existing path
guards deliberately reject aliases. Resolve only this launcher-selected Python;
runtime validation of configured host paths remains unchanged. Likewise, pin
the child test process's temporary root to its physical path: hosted Windows
TEMP may use an 8.3 spelling that deliberately fails lexical identity guards.
"""
import os
from pathlib import Path
import subprocess
import sys
import tempfile


def main():
    args = sys.argv[1:] or ["discover", "-s", "tests", "-v"]
    environment = os.environ.copy()
    temporary_root = str(Path(tempfile.gettempdir()).resolve(strict=True))
    environment.update({key: temporary_root for key in ("TMPDIR", "TEMP", "TMP")})
    return subprocess.call([str(Path(sys.executable).resolve(strict=True)), "-B", "-m", "unittest", *args],
        env=environment)


if __name__ == "__main__":
    raise SystemExit(main())
