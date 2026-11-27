# Local credentials — validation, 12 November 2026

The authorized local credential feature is implemented and verified in the repository. One explicitly designated local JSON file imports profiles into a Windows DPAPI CurrentUser vault shared by Codex, Claude and Copilot through the common CLI. Values are handled by the local executor; tasks, memory and model packets use references. Existing task/stage approval remains required. No framework MCP or runtime package dependency was added.

## Executed results

| Check | Result and actual evidence |
| --- | --- |
| Windows Python 3.15.0 | **484 tests, OK**, 305.690 seconds, exit0/no timeout/cancellation/drift; [log](../outputs/local-credentials-20261112/validation/full-python315-v2-20261112T104656Z-e2caff8e/suite.log), [process](../outputs/local-credentials-20261112/validation/full-python315-v2-20261112T104656Z-e2caff8e/result.json) |
| Windows Python 3.12.14 | **484 tests, OK**, 445.948 seconds, same terminal conditions; [log](../outputs/local-credentials-20261112/validation/full-python312-v2-20261112T104720Z-d93999fc/suite.log), [process](../outputs/local-credentials-20261112/validation/full-python312-v2-20261112T104720Z-d93999fc/result.json) |
| Encrypted storage | Actual Windows DPAPI encryption/decryption, damaged data, strict JSON/duplicates, atomic import/configuration, concurrent locking, idempotence, per-profile CAS and cancellation tests |
| Lazy sessions | Actual controlled subprocess probe/login/reuse/refresh, manual-interaction and unavailable/invalid-protocol blockers; bounded timeout/output/cancellation; no parent environment injection |
| Integration | Actual public CLI before/after task approval, exact stage/target binding, encrypted fake import, owned child environment, safe output, secret-free project SQLite/model packet/export; revision invalidation and completed-task observation refresh without effect replay |
| HTTP | [Actual public CLI probes](../outputs/local-credentials-20261112/review/http-public-cli-probes.json): sanitized authorized response, denied stage, redirects, typed401/403; [TLS/HEAD](../outputs/local-credentials-20261112/review/tls-head-probes.json): real HEAD, trusted fixture TLS success, untrusted certificate and hostname mismatch rejection, parent/trust-store unchanged |
| Backend-neutral packaging | All three disposable native skill installations contain the same credential reference. Native advisory conformance covers supported setup without routine questions, session status distinctions, manual2FA and account ambiguity; this is not live provider or automatic-routing proof |
| Clean wheel | [Package summary](../outputs/local-credentials-20261112/validation/package-20261112T104721Z-b3d966a7/summary.json):38 members, dependencies[], no ase.host, private HTTP worker included; isolated installed CLI plus actual fake DPAPI import/list on both interpreters. Python3.11 grammar checked, not runtime executed |
| Independent model acceptance/review | [Actual final pair](../outputs/local-credentials-20261112/acceptance/run-20261112T110143-0d398b53/summary.json): **6/6 PASS**, findings empty, missing coverage empty in agreed scope; distinct sessions/processes and independently revalidated receipts |

Wheel SHA-256: `5a61f8ea58dfe970086d24ae43e9c25a8dc12fca818ae42981786d1e68513d38`.

202-file source freeze SHA-256: `88571a83e149202d06088b2784af7dc33059dddb7215fc17d2f2e6f958a0726e`. Final reviewed candidate: `20f42e3475a044380f6025b30426e615309cfa2d0f31d6b7e03f35c5780171ef`. Source and evidence matched before/after both final model roles. Only prose closeout/worklog changed afterward; executable code, tests, skills and packaged inputs stayed unchanged.

## Findings and retained history

The first coherent candidate passed481 tests on both interpreters. A later independent real-process probe found a P1: encoded old secret material in a stored recipe could become executable after secret rotation. Strict profile validation now rejects raw, URL/plus/base64/JSON-escaped and composite Basic values at import/configure/load/rotation. New failing/green tests and an independent [recheck](../outputs/local-credentials-20261112/review/rotation-encoded-argv-recheck.json) confirm rejection before any child starts, without partial configuration changes. The original [failing probe](../outputs/local-credentials-20261112/review/rotation-encoded-argv-probe.json) and prepatch results remain intact.

The first actual model pair returned5 PASS/1 PARTIAL and no code findings because executed TLS/HEAD proof was missing. Actual local fixtures supplied that proof without a code change; a new independent pair then returned6 PASS and no findings. [Initial raw judgments](../outputs/local-credentials-20261112/acceptance/run-20261112T105541-9b4b857b/ac-model-report.json) were not overwritten. Protocol fixtures, native advice, executed host facts and actual model judgments remain separate evidence classes.

Publication preparation later marked three DPAPI-only access/integration/revalidation test classes as Windows-only, matching the Ubuntu/Windows CI matrix. All29 affected tests still executed and passed on Windows; unsupported-platform tests remain separate. Extra empty EOF lines in eight Python files were normalized with identical Python ASTs. These are test-selection/formatting changes, not a runtime behavior change or a claim of locally executed Linux tests. Raw evidence under `outputs/` remains local and is intentionally excluded from Git; versioned reports retain results and boundaries.

## Scope and limits

All credentials, service adapters, certificates and resources used in acceptance are owned disposable fixtures. No real credential file, provider authentication, Windows trust store, user project database or active runtime installation was changed. Current-repository commit/push and real VM/cloud configuration were not performed.

The encrypted backend is Windows DPAPI CurrentUser. Other operating systems return a blocker; there is no plaintext fallback. Protection of persisted data does not isolate secrets from malicious code running as the same Windows user. [Microsoft documents the account-oriented protection and noninteractive flag](https://learn.microsoft.com/en-us/windows/win32/api/dpapi/nf-dpapi-cryptprotectdata); [decryption checks and limits](https://learn.microsoft.com/en-us/windows/win32/api/dpapi/nf-dpapi-cryptunprotectdata) apply. Python immutable values are not claimed to be securely erased from all memory.

The import file remains unchanged and may still contain plaintext. Native service CLIs may maintain their own caches according to their own policy. The broker encrypts its vault and suppresses secret-bearing auth outputs in retained framework artifacts; it does not install an OS firewall around arbitrary approved programs. HTTP observations enforce exact origin, HTTPS verification and no redirects; configured CLI adapters remain trusted code.

HTTP built-ins cover Basic/Bearer GET/HEAD observations; supported CLI recipes cover other services. Universal GUI/OAuth/provider-specific login is not promised. Manual2FA/passkey/CAPTCHA, wrong/expired access, unresolved account identity or unsupported mandatory protocol are real blockers. `prepared` and `has_session` do not prove authentication. New resource/risk/cost authority still requires approval.

Live Claude/Copilot remain excluded by the user due to absent subscriptions. Their shared CLI/skill packaging is verified; live service/backend compatibility is not inferred. The historical62-file drive-root test incident remains a separate unresolved manual cleanup item; automatic safety review rejected deletion and no bypass was attempted.
