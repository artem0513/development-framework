# Project-local mode — validation, 12 November 2026

Project-local runtime, configuration, separate SQLite memory and dated work journal are implemented for the three installer targets. Use the [initialization command and usage guide](project-local.md). Existing shared mode and the previously requested project-example discovery/explicit opt-out remain available. No framework MCP, global activation or real user credential import was introduced.

## Delivered behavior

- Explicit `--project-local` installs runtime/settings under `.devframework` and persistent state, receipts, logs, vault and backups under ignored `.devframework/local`.
- Local launchers select their own project from unrelated directories; nested calls, explicit paths/UUIDs, hostile shared environment values, interpreter fallback and parent environment restoration are covered.
- Two projects retain independent identity, memory, dated journal and credential profiles. Registered state relocation requires explicit attachment preserving identity and fresh workspace observations.
- Strict config/path checks, a final private-directory ignore rule, tracked-state rejection and pre-read hardlink checks protect discovery, source packets and fingerprints. Private state updates do not self-invalidate application evidence. Installation remains previewable, preflighted, backed up and idempotent.

## Executed checks

| Check | Observed result |
| --- | --- |
| Windows Python3.15.0 full unittest discovery | **537 tests, OK**, zero skips,291.208s; exit0, no source drift |
| Windows Python3.12.14 full unittest discovery | **537 tests, OK**, zero skips,413.091s; exit0, no source drift |
| Clean wheel and installed CLI |39 members, dependencies[], new `ase/project_local.py`, no retired host/private/output state;32 captured helper processes and actual isolated installs/smokes on both interpreters |
| Actual project-local Codex workflow |3 fresh distinct real sessions: implementation, AC and review;3 immutable tests pass; gate APPROVED after launcher restart, task COMPLETED,3 dated journal events; second project unchanged |
| Installed-runtime integrity | All134 originally installed files unchanged after that actual workflow |
| Generated descriptors and adopted skills | Actual descriptor consistency command PASS;27-file adopted Codex skill audit PASS; whitespace check PASS |
| Final independent code-candidate review/AC | Separate real Codex sessions, **6/6 criteria PASS**, no P0–P3 findings, no mandatory missing coverage; receipts and immutable input hashes reverified |

The verified product inventory contains223 files. Freeze SHA256: `b1f643479287f714f5a441203950164fe50d03906123dc1bac66a3bbb01e4fd1`. Wheel SHA256: `2a687e4f6c6b19e8239eea03bbbcc323651b3d4d9006134285f90c01d4e7e053`. Final independent source sessions ran on this unchanged inventory; only the subsequent prose closeout/publication records differ.

## Corrections retained in history

Independent advisory probes exposed selective-ignore leakage, implicit identity reset after relocation and private hardlink aliases. Each was fixed with observed failing/green tests and independently rechecked. Integration also closed local UUID/launcher fallback, external verifier cwd and legacy shared-home alias cases.

The first frozen full suites found one knowledge compatibility regression: metadata for an unavailable shared-inode source must be retained without reading its bytes. The corrected version permits metadata-only references while actual verification rejects the read and returns UNAVAILABLE. Both final full suites, package and live Codex workflow were rerun against the new freeze. Earlier failures and earlier successful candidate evidence remain historical, not relabeled as final proof.

The final CLI enforces a1,048,576-character input limit. Review inputs were scoped to relevant current bodies, exact source/hash-bound fragments and complete execution facts. Repeated metadata and logs use explicitly verified lossless reconstructions; original raw artifacts remain untouched. The first review requested transfer/adapters/descriptors and generated-check evidence; these were supplied in the passing final pair. Code-candidate approval precedes publication; it does not itself prove push completion.

## Limits and local evidence

Live execution is Codex CLI0.167.0-alpha.4.1 on Windows. Claude/Copilot received actual disposable file/packaging/protocol coverage; their live subscriptions/routing were not tested by the user's choice. Linux runtime, Python3.11 runtime, arbitrary task correctness, automatic native UI skill routing and hostile same-user enforcement are not claimed. Native provider/third-party login caches remain managed by those tools. Existing shared memory was not migrated automatically.

Local raw proof is intentionally ignored by Git: `outputs/project-local-20261112/validation/full-python315-v2-20261112T174411Z-8bfccce4`, `full-python312-v2-20261112T174411Z-e3557bc0`, package `run-20261112T174410Z-7663825a`, live fixture `fixture-20261112T174357Z-5475b26b`, and final acceptance `run-20261112T181825Z-08c47cca`. The local `RESUME.md`, frozen inventories, full process/Runner receipts and publication result preserve continuation context. These raw paths are not published attachments.

Source/tests/skills/docs were published by normal fast-forward push to `artem0513/DevelopmentFramework`, branch `feature/project-opt-in`: [source commit181a6b7](https://github.com/artem0513/DevelopmentFramework/commit/181a6b73439a18fab40198a3b5dbfc7bd63569aa). The remote SHA was checked equal to the local commit and the working tree was clean. This subsequent documentation closeout records that observed operation; private state/raw logs remain outside Git. The latest local publication receipt records the final documentation commit separately.
