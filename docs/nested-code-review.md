# Independent review: host verification transport

Reviewed on 2026-11-01 independently of implementation. Scope: ase/host.py, ase/inspection.py, injected inspection/verifier boundaries in ase/core.py, state-path checks in ase/store.py, host registration in scripts/install.py, and task-orchestrator routing. No implementation files modified by this reviewer.

## Current findings

No unresolved actionable P0/P1/P2 finding identified in the inspected final host transport. This applies to the concrete local reliability boundaries reviewed here; it is not a blanket production-security or complete mission-acceptance sign-off.

## Boundary evidence

- MCP exposes only verification_start and verification_status. Calls cannot supply an executable, environment, state home, permission profile, arbitrary command, implementation, tests or publication operation. Canonical repository, Git metadata/common-directory and fixed state paths are checked; tested junction/hardlink escapes fail closed.
- Host Codex executable is pinned at startup. Reviewer startup uses the installed runtime's trusted directory, **not the target repository**. The target's .codex/config.toml, hooks and stdio MCP commands therefore are not loaded as reviewer bootstrap configuration. The target remains explicit untrusted inspection data in the handoff. Child flags retain --sandbox read-only; --skip-git-repo-check permits the trusted nonrepository startup location and does not disable that sandbox. The bridge disables its own MCP entry in children. Existing user/managed configuration in the trusted host remains part of the trust boundary.
- Registration uses Python -P and fixed runtime PYTHONPATH, resolving the current-directory package-shadow issue. Independent import probes demonstrated the unsafe form selected a repository package and the safe form selected the intended runtime.
- Host inspection uses a separate allowlisted Git adapter. It selects an absolute executable from host PATH, excludes current/relative PATH entries, removes inherited GIT_* overrides, disables global/system configuration, lazy fetch and transport protocols, and neutralizes fsmonitor, hooks, external diff, textconv and configured clean/smudge/process filters. Publisher Git retains its separately authorized ordinary behavior. Actual marker regressions confirm fsmonitor/textconv/clean helpers do not execute in host inspection. The earlier external-diff reproduction established why this boundary was necessary; the final adapter supplies --no-ext-diff and --no-textconv.
- Child and Git stdin use DEVNULL, so they cannot consume or wait for the live MCP stream. Concurrency is bounded to one host job; engine/SQLite connections are created and closed in that job's thread.
- Supported native tool policy allowlists only the two verification endpoints, sets the server default to prompt, and explicitly approves those endpoints under the user's authorization for automatic verification. It changes no sandbox settings and does not falsely label evidence-writing operations read-only. Mechanism: [official MCP configuration](https://learn.chatgpt.com/docs/extend/mcp).
- No copied credentials, hardlinked credentials, broad ACL changes or unsandboxed reviewer tools were introduced. COMPLETED is explicitly distinguished from the quality gate's APPROVED/REJECTED/BLOCKED outcome.
- Final AC prompt clarification inspected at ase/core.py:437: confirmed behavior without meaningful evidence must remain UNVERIFIED/BLOCKED/FAIL; broader generalization limits and optional studies belong in limitations. Existing source-bound, integrity-checked execution can be evaluated without inventing an additional rerun requirement. The prompt expressly forbids hiding uncertainty to obtain PASS. The gate at ase/core.py:590 still blocks PASS with missing evidence/method or nonempty missing_coverage; no forced-PASS path or validator relaxation was introduced by this wording change.

## Executed checks

Independent command: python -m unittest discover -s tests -p test_host.py -v.

Result: **18 tests executed, 18 passed, exit code 0**, 17.717 seconds. Coverage includes MCP framing, unsupported inputs, path/symlink/state junction/hardlink rejection, nested Git-root rejection, concurrency, pinned executable, unavailable verification, real receipt protocol via an identified model fixture, Git fsmonitor/textconv/clean-helper rejection, and MCP stdin isolation.

The fixture validates transport/receipt protocol, not live semantic acceptance. The fresh ordinary-prompt demonstration under outputs/demo-chat-verified and complete suite were still running in the parent task during this review. Their results must come from actual final artifacts; neither is inferred from these tests.

No real remote, production repository, cloud deployment or credential mutation was performed by this reviewer. Same-user modification of trusted installed code/config remains outside the documented local reliability boundary.

## Reviewed SHA-256

```text
ase/host.py          2178C5E2BDC107FC046FC34E7A42493FBE5C30023F1A5E8FC9858A2C95840A4A
ase/inspection.py    3E0452D02A4996AE8D37C856AB912D677C0F252CAFD8BC54C6C320628057D56E
ase/core.py          40E9391EA9A16D961213BF267822FA74F8CE2096C5EB2A83EBF8AB771E2567FF
ase/store.py         CE22F7074B6331FFA8E23BA30E412DFEC2FFB5BA5BD78D908192E44CCE7FA9E6
scripts/install.py   3E80299A69C2738BE77E87538DF8862AB1C532D6463EBA2CFD186E3917B1F958
tests/test_host.py   9A7F149A06B0218A4DA49C4268D8ED4009616CB2DC8E340F9BBF58821C68DB2A
```

Later code changes require affected re-review and checks.

