# Launcher canonical-path validation

The Windows CI failure compared a canonical state directory with an unresolved temporary-directory spelling. A controlled directory alias reproduced the same assertion failure locally. Tests now resolve the expected root while also requiring the runtime output to remain canonical. Default and explicit state homes and recovered child environment remain checked; runtime protections and CI configuration are unchanged.

Supplemental Linux execution artifacts are retained locally in ignored `outputs/ci-launcher-linux.json` and `outputs/ci-launcher-linux.log`. They are execution evidence for independent evaluation, not native ASE verifier receipts or evidence of a GitHub Actions result. The JSON includes exact commands, timestamps, exit status, source-file hashes and the log SHA-256.

Runtime: existing Docker Desktop WSL Linux, Python 3.12.16, Git 2.52.0. Signed official Alpine v3.23 packages were extracted only into `/tmp/ase-ci-check-20261102` using apk's separate root and disabled scripts. The managed distro and Docker Desktop services were not installed or reconfigured. Environment paths apply only to the test subprocess.

Command: `python3 -m unittest discover -s tests -v`, working directory `/mnt/host/e/Tools/DevelopmentFramework`.

Test-file SHA-256: `f68cf9924fd435bf33b9dc14343e000b7b2c13a9c4af35ea203f21760b5b5030`.

The real GitHub Actions Windows/Ubuntu Python 3.11/3.13 matrix still requires a run on the published fix; the supplemental Linux runtime is Python 3.12 and does not substitute for that remote matrix.
