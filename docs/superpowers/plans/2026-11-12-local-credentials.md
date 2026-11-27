# Local credentials implementation plan

Approved scope: [local credential design](../specs/2026-11-12-local-credentials.md), implementing the user's explicit request and earlier chosen flow. Technical choices are delegated; no extra approval round is required.

Status: implemented and verified in the agreed Windows local scope. Final frozen suites:484 PASS on Python3.12 and3.15; actual independent AC6/6 PASS and review findings empty. The encoded-recipe rotation P1 and missing TLS/HEAD evidence were closed with preserved historical results. [Validation and limits](../../local-credentials-validation-2026-11-12.md). Only prose closeout changed after verification; no real credentials or user installation were applied.

1. Capture current source baseline; implement DPAPI encrypted vault and strict local JSON import with real failing/green tests in exclusive credentials module ownership.
2. In parallel implement common lazy-access executor with typed probe/login protocol, child-only secret injection, HTTP observations and real subprocess/server tests against the agreed vault API.
3. Integrate reference-only CLI, approved check/action access, literal-secret redaction, credential-revision evidence binding and canonical skill/docs/templates. Preserve existing execution and publication safeguards.
4. Independently review the integrated behavior and negative cases; run full suites on Python3.12/3.15, a clean wheel and disposable CLI end-to-end with fake credentials. Freeze product files while suites/model review run.
5. Actual separate Codex AC/review receives current source and evidence. Resolve actionable P0–P2, record exact limitations and save dated report/worklog/context. Do not install/use real credentials or change provider auth/config unless a separate actual target/file is explicitly supplied.

All credentials tests use owned disposable paths. Keep the prior drive-root incident untouched; no cleanup bypass. No commits/push/real user DB migration. Linux secret storage is explicitly unsupported in this Windows-first vault, with a typed blocker instead of plaintext storage.
