# Independent audit: nested Codex verification fix

Date: 2026-11-01, Europe/Bucharest. Scope: fix the Windows nested Codex
`Access denied` failure while retaining implementation and reviewer sandboxes,
and complete an ordinary development request without manual external
verification. The auditor authored `tests/test_host.py` and earlier test
utilities, but did not implement `host.py`, `inspection.py`, runtime changes,
installation changes or the live demonstrations. This authorship limitation
does not replace the raw-artifact checks below.

**Outcome: all seven bounded acceptance groups PASS.** The original production
CI/branch-protection gap remains outside this fix; this is not a security
certification against an adversary controlling the same Windows identity.

| ID | Expected behavior | Implementation evidence and verification method | Result | Confidence limits / missing coverage |
|---|---|---|---|---|
| N1 | An ordinary development request implements and completes verification automatically | Initial `outputs/demo-chat-verified` run starts from independently inspected Git `NotImplementedError` and reaches APPROVED at 12:57:01 UTC. After the final report-instruction clarification, fresh `outputs/demo-chat-current/receipt.json` again records the ordinary request, `workspace-write` and exit 0; raw session records actual MCP start/status calls. Auditor recomputed current gate APPROVED at 13:06:48 UTC, fingerprint `41fda1916eaff81ea437bb149fa630fff97f0a4e92de9c8b892601c1d10de942`. Neither run used manual external continuation | PASS | Established for the allowed disposable Python repository and three explicit criteria. Other repositories require an explicitly configured host root and their own test conventions. |
| N2 | Independent AC/review processes produce current-revision evidence rather than verbal completion | Frozen `outputs/chat-positive-evidence/verify.json` and latest `outputs/chat-current-evidence/verify.json` have separate native `ac`/`review` receipts, exit 0, read-only sandbox, trusted CWD and recursive bridge disabled. Auditor recomputed raw/log/check hashes and revision associations for both: all match. Latest run has 3 executed passing tests, 3 AC PASS with empty missing coverage, and no review findings | PASS | The first positive snapshot became historical when its demo was deliberately broken; the latest separate demo is currently APPROVED. Separate contexts provide independent judgment, not separate OS identities or infallible semantics. |
| N3 | Fix preserves implementation/reviewer sandbox restrictions | Outer ordinary request remains `workspace-write`; verifier receipts remain `read-only`; no bypass/escalation flags. Actual child tool command in `outputs/nested-probe/readonly.session.log` attempted writes to workspace and outside-user canaries and reported both denied, exit 0. Host startup/config inspected: narrow MCP service, pinned native Codex, original CODEX_HOME, trusted import path/CWD | PASS | The probe catches OSError broadly and omits exact exception details; both parent directories are valid. This proves the tested write restrictions, not every possible Windows ACL capability. Host control-plane operations intentionally write only verification state. |
| N4 | Host exposes verification only and rejects arbitrary commands, roots, environment or publishing | `host.py` exposes exactly start/status for stored tasks; fixed state home and executable are operator configuration. Actual NDJSON tests cover tool/method allowlist, unknown/privileged arguments, startup pin, canonical traversal, directory junction escape, max-one active job, duplicate refusal, failure reporting and absence of check/run/publish calls | PASS | Global host root is currently this framework checkout. Registering another root is an explicit setup action. Jobs are in-memory; durable evidence/task state supports subsequent re-verification after service restart. |
| N5 | Moving verification outside the chat sandbox does not execute repository Git helpers or follow state links | Three real host canaries first reproduced outside-root writes through fsmonitor, textconv and clean filters; all pass after `inspection.py` hardening. A project-state junction also reproduced an outside report write before Store hardening. Current project/task junction and artifact-hardlink checks pass. Safe inspection disables helpers/filters/lazy protocols, uses pinned Git and supported inspection forms. Persistent-open MCP stdin regression verifies child EOF rather than channel inheritance | PASS | These are static regression cases, not exhaustive race/fuzz testing. Same-user filesystem tampering/TOCTOU and CI provenance remain outside the claimed adversarial boundary. Normal publishing hooks remain enabled in the ordinary CLI. |
| N6 | Real tests execute, including regression cases, and installed runtime matches tested source | Final `outputs/nested-full-tests-current.log`: **60 tests, 108.912 seconds, OK**, zero skipped tests; prior full run was 60/93.480 seconds. Earlier host-focused log: 18/23.021 seconds, OK. Auditor compared installed/source bytes for all four modules after final clarification: equal | PASS | Tests with a deterministic external model adapter validate protocol mechanics only; live N1/N2 provide separate native-model evidence. Tests were run on Windows; remote CI/platform matrix was not executed here. |
| N7 | A deliberate missing requirement is rejected truthfully through the same ordinary-chat path | Fresh `outputs/chat-defect.session.log` records actual MCP start/status calls and REJECTED. Frozen `outputs/chat-defect-evidence` has 3 executed tests/1 error, AC-1/2 PASS, AC-3 FAIL and reviewer P1. Auditor independently reran current tests: `ZeroDivisionError`, exit 1; independently recomputed current gate REJECTED at 12:59:36 UTC with matching fingerprint `fa8e0543624fe859bc9201530faca730c44487ba80213bbc301feb7850d18b83`. All frozen negative raw/log/check hashes validate | PASS | The disposable example intentionally remains defective as evidence. Rejection is the experiment's expected outcome. No push was requested or performed in either ordinary-chat run. |

The frozen positive revision fingerprint is
`f4fbe1d0ef3272b6272639b460d4fe543c69c9f6bb0ef3747007376e72dcfef0`;
the later negative fingerprint differs. This distinction prevents interpreting
the earlier approval as approval of the current defective code. A completed
parent model or MCP job is not treated as gate approval.

Installed/source SHA256 at the independent checkpoint:

| File | SHA256 |
|---|---|
| core.py | `40e9391ea9a16d961213bf267822fa74f8ce2096c5eb2a83ebf8ab771e2567ff` |
| store.py | `ce22f7074b6331ffa8e23ba30e412dfec2ffb5ba5bd78d908192e44cce7fa9e6` |
| host.py | `2178c5e2bdc107fc046fc34e7a42493fbe5c30023f1a5e8fc9858a2c95840a4a` |
| inspection.py | `3e0452d02a4996ae8d37c856ab912d677c0f252cafd8bc54c6c320628057d56e` |

Final refresh: the verifier instruction distinguishes gaps in confirmed
behavior from broader study/scope limitations. Genuine gaps still require
FAIL/UNVERIFIED/BLOCKED; the deterministic gate still refuses nonempty
`missing_coverage`. No status-import bypass or gate relaxation was introduced.
The final full suite and fresh ordinary-chat run above were executed after
this clarification; the earlier positive/negative experiment is retained.

No credential contents were required for this audit. The bridge reuses the
configured native Codex home; its tool API accepts neither credentials nor
execution/environment/permission overrides. User-owned receipts remain an
error-detection mechanism: trusted remote CI identities and branch rules are
still needed for enforced production publishing.
