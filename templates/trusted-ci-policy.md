# Server-side enforcement checklist

The included CI workflow tests this tool; it is not a configured production gate for another repository.

For an actual repository, an administrator must:

1. Run a **pinned trusted copy** of this runtime and the required task policy from a separate protected repository or immutable image. Never load the gate implementation/policy from the untrusted feature branch.
2. Use separate jobs/identities for build evidence, independent AC/review and publication. Bind artifacts to repository, commit/tree digest, task contract and runner identity using the CI artifact/attestation service. Local SQLite receipts are not signed attestations.
3. Require the named gate status with rulesets/branch protection, forbid direct/force pushes and require CODEOWNERS review for CI, task policy and protected paths. Keep any publish credential outside the implementer sandbox; use short-lived least-privilege credentials only in the protected publish job.
4. Execute untrusted tests without production secrets or privileged Azure access. Review dependency restore and integration endpoints. Separate approval environments for destructive/data/security operations.
5. Exercise rejected AC, failed build, stale artifact and modified policy cases on that actual CI before describing remote enforcement as validated.

Optional local hook: `python scripts/install_hook.py --repo <path> --home <state-home> --task <id>`. It refuses to overwrite an existing hook and rejects non-HEAD/non-feature pushes. Hooks remain bypassable via `--no-verify`, Git configuration or filesystem access. Repository administrators, not this installer, control server-side protection.

## Ready-made pieces

- `templates/ci/ase-merge-check.yml`: reusable GitHub Actions job running `ase ci-check` from a pinned trusted framework commit. Rules (protected paths) are read from the base revision, so the pull request cannot loosen them. Checks: diff size budget, deleted tests (moves excluded), added skip/xfail/ignore/skipTest markers, net removed assertions, protected paths, dependency manifest changes. Owner overrides are the `ase:allow-protected` / `ase:allow-large` / `ase:allow-test-removal` labels; restrict who may label. `templates/ci/caller-example.yml` shows the caller. Require the job's status in branch protection.
- `python scripts/install_agent_hooks.py --repo <path> [--ci-base origin/main]`: writes project-level `.claude/settings.json` hooks only (never user settings). PreToolUse denies default-branch and force pushes, `--no-verify`, `gh pr merge` and branch-protection changes; with `--ci-base` it runs `ase ci-check` before `git push` and `gh pr create`, denying only FAIL-level findings and reporting owner-review items. UserPromptSubmit runs `templates/hooks/prompt_capture.py`, so approvals may cite `prompt:<id>` from `ase prompt list`; an unknown `prompt:` reference is refused, and disproving an independent finding requires a prompt captured after that review. Where agent hooks are not allowed (for example at work), disproof uses `--owner-quote` with the owner's exact words and is reported as owner-asserted, not verified; `ase ci-check`, skills and independent sessions keep working locally without CI or hooks. Both are local accident barriers: a same-user agent can bypass them.
