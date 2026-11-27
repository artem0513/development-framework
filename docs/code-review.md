# Independent code review

Date: 2026-11-01. Reviewer did not implement the runtime. Scope: actual `ase/`, installer/vendor/hook scripts, task orchestration, evidence/publishing logic, SQLite changes, and executable tests. Review did not modify implementation files. Disposable repositories and protocol fixtures were used for deterministic regression checks; these fixtures do not prove independent model judgment.

## Current findings

No unresolved actionable P0/P1/P2 finding identified in the final reviewed local reliability implementation. The final retention/launcher pass found a Windows equals-form override defect, subsequently fixed and independently rerun as described below. This is scoped review evidence, not a claim that all mission acceptance criteria or production CI enforcement are satisfied.

## Findings resolved and independently checked

1. **P1: Multiple remote push destinations bypassed authorization.** Publisher and authorizer now inspect all push URLs and require exactly one; publisher rechecks after commit/hooks (`ase/core.py:710`, `ase/core.py:742`, `ase/core.py:770`). The regression test `test_multiple_push_urls_cannot_bypass_remote_authorization` executed successfully. Earlier independent reproduction observed the corrected publisher rejecting an added unauthorized second destination.
2. **P1: Mandatory ACs ignored their failed optional evidence commands.** Gate now checks linked command execution/failure/test counts and blocks absent mappings (`ase/core.py:500`). Original independent reproduction approved AC1 despite its mapped command exiting 7 and unrelated tests passing. After correction, `test_failed_optional_command_linked_to_mandatory_ac_rejects` executed successfully.
3. **P2: Branch blacklist treated arbitrary branches as feature branches.** Publisher now permits only `feature/*` and `bugfix/*` (`ase/core.py:732`). `test_non_feature_branch_publishing_rejected` executed successfully. Arbitrary remote-side protected naming remains an administrator policy concern: no remote branch-protection state was configured or verified here.
4. **P1: Hook rejected valid publisher output and inspected the wrong side of the refspec.** Original hook rejected `HEAD:refs/heads/feature/test` and would permit a valid local feature source aimed at `main`. Corrected hook checks current symbolic branch, exact destination branch and verified HEAD SHA (`scripts/install_hook.py:25`). Independent disposable runs observed feature publisher success, rejection of `feature/test:main`, and bugfix publisher success. The newly added positive integration test also executed successfully.
5. **P2: Unix launcher used Python quoting as shell quoting.** `scripts/install.py:46` now uses `shlex.quote` for executable/runtime paths. Source correction inspected. Unix installation with unusual directory names was not executed on this Windows host.
6. **P2: Windows absolute equals-form state override was split by PowerShell.** An executed regression and independent minimal probe showed `pwsh -File` converts `--home=C:\path` into separate `--home=C` and `\path` arguments. The final installer reconstructs this exact drive-letter conversion before forwarding to Python. The focused installed-launcher test then passed. An additional independent disposable installation successfully executed an absolute `--home=C:\...\absolute equals` override containing a space and verified the resulting state directory was inside that exact target (exit 0).

## New component assessment

Final change review: bounded retention validates 1..365 days (default 30), deletes only known verbose evidence patterns, retains structured memory, skips locked tasks, and checks resolved paths/symlinks before deletion. Three retention-focused scenarios executed successfully (expiry preserves memory/unknown files; active lock preserves logs; invalid retention blocks). Launcher fallback source correctly provides absent CODEX_HOME/HOME/USERPROFILE and an installation-time stable state home. Separate-form, relative equals-form, and independently exercised absolute Windows equals-form overrides passed; no remaining finding identified in these two final changed components.

SQLite migration backs up existing v1 state before adding audit/session fields. Time corrections preserve an audit record, reset prior confirmation unless explicitly supplied, and detect concurrent edits through a conditional update. Weekly summaries preserve agent/human attribution and clearly state start-date grouping without midnight splitting. Session summaries persist decisions and unresolved questions. Execution now waits for the command process while spooling output, avoiding inherited stdout pipe lifetime stalls; the bounded-process regression executed successfully.

Installer preserves existing user configuration through backed-up managed writes; vendor installation pins a full commit and retains a source manifest. Generic credential redaction is heuristic and cannot guarantee removal of arbitrary confidential prose; documented review before sharing remains necessary.

## Execution record and limitations

- Targeted changed-component command: `python -m unittest discover -s tests -v -k time -k weekly -k session_summary -k multiple_push -k optional_command -k non_feature -k approved_publish -k completed_parent` — **9 tests, exit 0**.
- Corrected hook denial test: `python -m unittest discover -s tests -p test_system.py -k native_git_hook -v` — **1 test, exit 0**.
- Positive hook publishing test: `python -m unittest discover -s tests -p test_system.py -k verified_publisher -v` — **1 test, exit 0**.
- Final retention/installer checks: installation preservation + active lock + invalid retention **3 tests, exit 0**; expiry/memory preservation **1 test passed** in the run that initially exposed the launcher defect; after correction `python -m unittest discover -s tests -v -k installed_launcher` — **1 test, exit 0**, plus the independent absolute equals-form probe above.
- Earlier full-suite execution started before a concurrent hook-test correction: **37 tests, exit 1**, one outdated assertion expected the SHA denial message where the corrected hook first denied the destination. That test subsequently passed as recorded above. This earlier run is **not** a full-suite PASS; the final owner must run the current complete suite.
- Shared-user local receipts/hooks are intentionally an accident-detection boundary. Trusted CI, protected policy ownership, inaccessible publishing credentials and server-side branch protection were not audited or enabled by this review.
- No real remote was used; no PR was created or published. No cloud integrations or Linux runtime were exercised by this reviewer.
- The earlier owner-reported Superpowers **5.0.7** cache/config entry was superseded: it was not an active installation in the final native listing. After the owner's targeted correction, this reviewer independently executed `codex plugin list --json` and observed **superpowers@openai-curated-remote 7.0.1**, **installed:true**, **enabled:true**. The actual `~/.codex/plugins/cache/openai-curated-remote/superpowers/7.0.1/.codex-plugin/plugin.json` matches **7.0.1**; it declares no hooks and recursive package inspection found no hook-named files. Evidence: `outputs/review-superpowers-installed.json`. No hooks were trusted and no broad marketplace upgrade was performed by this reviewer. Live model demonstrations remain separately attributable to their actual execution records.

## Reviewed file SHA-256

Hashes bind this review to inspected code, independently of Git commit availability. Later edits require affected review/checks again.

```text
ase/core.py                428352BAFDACBC205F74A280FAF97341C30D082105C602A36999A3E23E8F72FA
ase/store.py               38B957C3CCEA20F075373D6A3F7E9AAE1926017E262DD97F407EAFDC53C42B73
ase/__main__.py            D61671ACF436ACFCE8E46E229E70AEE9BFEB8D939D0834C293FBAE9D87D5D5BC
scripts/install.py         887AEAF4D8B47F81701358FB1183627BF34D6F93E787E1FFDA17A3BFE1B11273
scripts/install_hook.py    C1869E15E017E70CBED999EC83EF8369CF20C96D426BF287DA32D2030DCB7E53
scripts/vendor_skills.py   CD55196059C3F8C61C26B01A29541615A75D89822A9AAE76FB761A54B61CE6E9
tests/test_system.py       6AF5430CB9322F834B30E9D244760EAAC78616A4C9590FE658539065F6D054CD
```
