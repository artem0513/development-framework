# Executed validation and deployment status

Follow-up: the original nested-Windows limitation documented below was resolved at the workflow boundary with a scoped MCP verification host. A fresh ordinary chat completed implementation, checks, independent sessions and APPROVED without manual continuation. See [current evidence](nested-workflow.md) and the [independent follow-up audit](nested-acceptance-audit.md). Historical failed runs below remain historical evidence, not the current automation status.

Date: 2026-11-01, Windows / PowerShell. Original request: [mission](mission.md).

The local implementation includes SQLite memory/migrations/backups, isolated projects, bounded orchestration, separate Codex AC/review execution, command evidence, a fail-closed gate, dry-run publishing, installed skills/agents and documentation. Production server-side enforcement is **BLOCKED pending an actual repository/CI administrative configuration**; local hashes and prompts do not protect against another same-user process.

## Installation actually performed

| Component | Observed installation |
|---|---|
| Codex | Existing `0.166.0-alpha.5.1`; not upgraded |
| User runtime | `~/.codex/autonomous-se/runtime` |
| Launcher | `~/.codex/bin/ase.ps1` |
| Eight custom skills | `~/.agents/skills/{project-context-manager,task-orchestrator,independent-ac-verifier,independent-code-reviewer,quality-gate,time-reporter,communication-assistant,project-bootstrap}` |
| Three native agents | `~/.codex/agents/ase-{implementer,ac-verifier,code-reviewer}.toml` |
| Global routing | Managed section in `.codex/AGENTS.md`; separate `autonomous.config.toml`, existing model/sandbox settings preserved |
| Superpowers | `codex plugin add superpowers@openai-curated-remote`; **7.0.1**, independently checked in native `plugin list` as installed/enabled and in its cached manifest. Initial legacy-channel cache `82fd64bc` / 5.0.7 was superseded and only that obsolete installation removed |
| .NET testing | `codex plugin add dotnet-test@dotnet-agent-skills`, **1.0.0** |
| Angular developer | Immutable official commit `2a2b801eb9bf3304424ddedfa1ace347e1fff6a6`; user discovery directory plus provenance lock |
| Agent Framework C# | Immutable official commit `b7884ad88eb85c27b2a7bf40c0e15948e7ccc28d`; user discovery directory plus provenance lock; git sparse method used after Windows ZIP path-length failure |

Backups: `.codex/backups/autonomous-se/`; the original config before native plugin changes is `config.before-install-20261101.toml`. Each installer run backs up changed owned files and records its manifest at `.codex/autonomous-se/installation.json`. Unchanged files are not rewritten. No client repository was bootstrapped or published.

Eight skills passed the bundled `quick_validate.py`; PyYAML 6.0.3 was installed only in ignored `outputs/validator` to run that validator. Native `codex exec --strict-config -p autonomous --sandbox read-only --ephemeral ...` actually returned `PROFILE_OK`. That verifies loading, not universal skill behavior. Existing unrelated plugin/icon warnings do not establish a failure of these components.

## Actual controlled experiments

| Experiment | Actual evidence and result |
|---|---|
| Positive independent verification | First disposable calculator: real Codex AC and reviewer sessions; 3 tests; APPROVED before deliberate mutation |
| Deliberate missing requirement | Removing the zero-denominator guard caused actual unittest exit 1, 3 tests / 1 error; independent AC2 FAIL and reviewer P1; gate REJECTED |
| Defective publish prevention | `outputs/defect-publish.json`: REJECTED / published false; disposable negative remote had no refs. Frozen failure evidence: `outputs/defect-evidence`, `outputs/defect-report.json` |
| Full live implementation | `outputs/demo-full`: baseline raises NotImplementedError; real Codex implements division and adds tests; 7 executed tests PASS; separate AC and reviewer independently rerun tests; final gate APPROVED |
| Interrupted execution/recovery | The live runner exposed an inherited-stdout Windows hang. It was stopped after identity verification; `resume --recover --parallel` reused completed implementation, reran checks and independent sessions and produced the positive report |
| Local publication with real hook | Actual dry-run, explicit disposable remote authorization, commit and push through installed native Git pre-push hook succeeded; bare remote ref `refs/heads/feature/calculator` equals `de488ec805f3095cc13715a38cc4dedb80dd954a`; `outputs/publish-local.json` and `outputs/positive-report.json` |
| Missing/failed infrastructure | Actual subprocess integration tests cover missing executable, zero/skipped tests, failed build, timeout and unavailable verifier. None can approve |
| Stale/forged evidence | Integration tests change tracked/untracked source, verifier output and report-only PASS; stale/missing/tampered evidence rejects or blocks |
| Independent review corrections | Executed reproductions found multiple push URLs, unlinked failed AC command and hook destination/source defects; fixes plus positive and negative regressions are in `docs/code-review.md` |
| Process completion regression | A completed parent spawning a child holding stdout reproduced false timeout: RED 5.075s, GREEN 0.075s after anonymous spool/direct-process wait. Actual timeout test remains covered |
| SQLite/state | Integration tests execute separate CLI processes for persistence/isolation, backups/migration v1→v2, read-only v1 history after the v1 engine's retirement, summary/unresolved questions, time correction audit and human/agent attribution |

Model adapters inside automated tests are explicitly **protocol fixtures**, not independent semantic approval. The live Codex experiments above are separate evidence. A source review is not equivalent to exercising real Azure, Angular, EF Core or client infrastructure; those application integrations were not deployed or tested.

## Normal language activation

`examples/natural_demo.py` launches a fresh Codex session with an ordinary division request and the globally configured workflow, without manually invoking skills. The first actual run routed correctly, created a confirmed contract, implemented code, ran 3 real tests and saved memory, but nested Codex could not resolve its home under the Windows sandbox. It truthfully reported **BLOCKED**, not completion. Its raw receipt is `outputs/demo-natural/receipt.json` and `session.log`.

The installer was corrected to supply trusted missing home variables and a stable default state home, preserving explicit overrides. A fresh `outputs/demo-natural-fixed` experiment tests the corrected launcher; its completed receipts, rather than the outer Codex exit code, determine the outcome. Final outcome is recorded in the acceptance audit.

Actual corrected result: home resolution succeeded, but nested CLI execution inside the native Windows sandbox failed with **Access denied**. The outer agent used native independent agents for useful feedback and correctly retained the CLI gate as **BLOCKED**. Continuing the same persisted task from ordinary user PowerShell (`check`, then `verify --parallel`) succeeded with **APPROVED** at `2026-11-01T12:40:23.976331+00:00`; captured `outputs/natural-external-check.json`, `natural-external-verify.json` and `natural-external-report.json` bind the current source fingerprint `642c8eea18fb6121686243875c8d76405f0f0244496188ee2d6c1af67cc9dd88`. This is a verified manual continuation, not a claim that nested sandbox automation passed.

## Final component checks

The expanded integration suite executed **42 tests in 80.069 seconds, OK**; raw output is `outputs/tests.log`. It covers real CLI subprocesses and Git on Windows, including publishing with the native hook, absolute/split launcher state overrides, missing home variables, evidence expiration, SQLite migration/recovery and bounded retries. Separate focused launcher checks exercised the final absolute `--home=C:\...` case after discovering PowerShell's argument splitting. Eight custom skill validators passed again after installation. Wheel build and syntax checks succeeded; the final packaging command uses the default isolated build environment. A trial with `--no-build-isolation` failed because this Python installation has no global setuptools; the supported isolated command was rerun successfully, not reclassified as a successful nonisolated build.

## Remaining operational boundaries

- Actual trusted CI, signed runner evidence, protected repository rules and external publish credentials have not been provisioned. Follow [trusted CI policy](../templates/trusted-ci-policy.md) in the intended repository. No remote production gate is claimed.
- Cross-platform CI is supplied as a template; only Windows execution has been performed here. Specialized vendor guidance is installed/routed, but real .NET/Angular/Azure application acceptance remains repository-specific.
- HIGH-risk broader checks are selected in the reviewed task contract; only current-fingerprint human approval is additionally enforced generically. This tool cannot infer a complete business security policy.
- Project separation is canonical-path application isolation under one identity, not multi-tenant ACL security. Dependencies ignored by Git are environment inputs, not fingerprinted source.
- SQLite keeps concise records; verbose agent logs may contain source/tool output. Known verbose evidence expires lazily after 30 days (configurable 1–365), except locked tasks pending recovery. Old missing logs invalidate old approval; generic secret redaction is heuristic.
- No real remote push, PR creation or merge occurred. Framework changes are in the local `feature/autonomous-engineering` checkout, with no production remote or commit claimed for this repository.

See the independent [acceptance audit](acceptance-audit.md) for current criterion statuses and the final executed test count. Source packaging and syntax were actually built/checked with `python -m pip wheel . --no-deps --wheel-dir outputs/wheels` and `python -m compileall -q ase scripts examples`.
