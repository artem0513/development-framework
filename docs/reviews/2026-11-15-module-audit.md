# Independent technical module audit — 2026-11-15

Base: `37dc76509d2d1d26fda4b429d48333cca87ed13c` on `main`. Scope: each of the18catalogued modules, their real caller/callee contracts and cross-module failure boundaries. Preserve Python3.11+standard-library runtime, existing approval/evidence checks, native bot adapters, local/project-local memory and no MCP dependency.

## Review evidence

Each module received a distinct fresh-context advisory review with inspected source versions, actual module tests, reproduction evidence and explicit limits. These are versioned observations; initial results below are diagnostic history, not final-source approval. Counts overlap between suites/probes and are not summed as unique scenarios. The evidence module's initial failure was retained and its fixture repaired.

| Module | Initial findings | Actual initial module run |
| --- | --- | --- |
| storage | 3 | 54 tests; OK |
| tasks | 1 | 85 tests; OK |
| discovery | 4 | 101 tests; OK |
| test-design | 0 | 9 tests; OK |
| implementation | 1 | 85 tests; OK |
| testing | 1 | 38 tests; OK |
| review | 1 | 50 tests; OK |
| evidence | 2 | 71 tests; FAILED (errors=1) |
| memory | 0 | 73 tests; OK |
| reporting | 0 | 56 tests; OK |
| delegation | 2 | 56 tests; OK |
| recovery | 3 | 144 tests; OK |
| execution | 1 | 6 tests; OK |
| backends | 3 | 28 tests; OK |
| access | 0 | 48 tests; OK |
| delivery | 2 | 162 tests; OK |
| coordination | 1 | 50 tests; OK |
| legacy | 1 | 53 tests; OK |

A sealed mechanical index revalidated all18reports and105declared/transitive artifacts, distinguishing JSON-copy serialization from parsed-object equality. Three cross-module reviews then traced the integrated changes; they found six further P2 defects. The first final boundary pass found one further import approval-counter defect and one timing-only test defect; both have maintained regressions and repairs. Changed areas receive a fresh independent recheck; unchanged source is reused only after exact hash reconciliation. Separate original Codex acceptance/review remain required before delivery. Final executed results and exact publication SHA are recorded in the delivery commit message and dated local task journal; this source report does not claim future evidence.

## Confirmed repairs

- Storage: nested transitions retain the caller's transaction; timezone rules resolve before identity persistence; Windows SQLite database/backup opens use safe physical paths while keeping logical project identity.
- Tasks and implementation: inherited children satisfy the mapped parent's prerequisite closure; recovery authenticates owned crash staging before applying the source-inventory limit, resets its scoped context and preserves final authority/source guards. Late unsettled/corrupt prerequisite delegation is checked again during evidence capture and at the final file/process boundary, including inherited/transitive prerequisites.
- Discovery: governing metadata retains every origin's required/stage policy; readable raw bodies remain authoritative beside representations; bounded required ignored subtrees, including manifest-inferred local dependencies, affect fingerprints and actual packets; canonical resource paths are persisted. Optional/broad-root ignored secrets retain their exclusion rules.
- Testing, evidence and review: actual launched argv must match a declared effect/check/observer; independent judgments bind exact stage coverage and current action observations. Current whole-task judgments may cover included stages; narrower/wrong-stage receipts cannot cover broader/different obligations. Progress credit and delegated dependencies use those same current bindings.
- Recovery and execution: explicitly narrower child limits remain effective alongside shared root history; safe URI aliases cannot evade an unresolved operation. A completed synchronous host witness proves only a never-entered effect launch, distinguishing authentication/status processes from the actual effect. Hard crashes, possible Popen and lost acknowledgement remain UNKNOWN. Actions and executing publication reject caller-owned open transactions before effects.
- Backends: malformed nested events and non-finite/numeric edge cases produce durable BLOCKED captures; capability probes obey cancellation and one remaining call deadline. Native model selection and permission boundaries remain intact.
- Coordination/delegation: healthy approved stages continue when another stage has a scoped blocker. Large advisory history uses a complete identity index with verified result hashes and explicit omitted-detail labels. Required source bodies and original packet limits remain intact.
- Delivery: process-scoped named-remote pinning binds the approved resolved URL, including confirmation, without late pushURL fan-out/double rewrites; hooks/native auth configuration remain. Reviewed raw bytes, supported text CRLF normalization, tree paths/blob IDs/modes are checked before commit and after hooks. Exact durable push intents fence UNKNOWN across process crashes and task/remote aliases; returned receipts persist, atomic proof files survive interrupted acknowledgement, prior acknowledged owners cannot overwrite a newer attempt, and historical confirmed commits are distinguished from current ref observations; readonly reconciliation precedes any retry, and nonvisibility alone never proves nonapplication. Imported pending push records retain uncertainty rather than disappearing into passive history.
- Transfer: unsupported timezone import fails before activation; separate task and null approval counters retain high-water from current/historical tiers, version keys and canonical events across legacy/repeated imports, so new closures/proposals append without colliding with immutable old history. Imported approvals/verifier receipts remain historical.
- Legacy: failed v1start does not reserve an orphan task; duplicate existing work remains untouched.
- Tests: three stale protocol-fixture expectations were corrected while retaining meaningful final-writer, inventory and two-process CAS assertions. The timeout regressions also permit a valid empty invocation log when startup exhausts the deadline, while retaining timeout, charging and no-next-process assertions. The final full run also exposed two stale fixtures: strict mixed-domain receipt bindings now include the current verification context, and legacy terminal-action fixture writes commit explicitly before testing the real no-repeat guard. These are six test-only finding groups, not six additional product failures.

The coordinator resolution index retains41 confirmed issue groups (3 P1,37 P2 including6 test-only groups,1 P3 capacity issue) with original review IDs and repair evidence. It preserves failed/superseded probes instead of treating them as current PASS.

## Validation and limits

Final validation uses the complete unittest suite on frozen current bytes, targeted real module/CLI/Git/crash checks, built/installed wheel imports and retained actual native Codex sessions. The live prerequisite case prepares a parent stage, proves the child refuses changed prerequisite source, repairs it, resumes through separate acceptance/review and verifies terminal resume launches nothing again. The mapped parent delivery remains unfinished deliberately; this does not claim full parent approval or production infrastructure testing.

The final read-only acceptance phase retains the software profile and all original obligations. It uses bounded current-code/evidence packets with explicit whole-file omission labels, exact source-bound test changes and mandatory executed full-suite validators. Any selected function excerpts must be checked against exact current source; they never imply review of omitted bodies. It authorizes no further edits. Publication/remote confirmation and dated closure remain mandatory later work, not self-referential prerequisites of the source gate.

Claude/Copilot live execution is excluded by the owner's existing instruction; shared services and adapter contracts have unit coverage. Native POSIX symlinks, other Python/OS/Git versions, production providers, hostile same-user enforcement and power-loss durability are not established by Windows fixture results. Older Git lacking required destination/attribute semantics fails closed. Existing explicit ActionService coordinator ports remain a documented coupling; modules are responsibility boundaries, not independently deployable microservices.

The initial native fault-injection case returned UNVERIFIED when its first automatic baseline observed the injected NOT_READY state. That rejection is retained. The corrected disposable case observes the actual intake baseline prospectively before fault injection; it never rewrites a past observation or model receipt.

Publication preflight also found benign handle/alias names and an existing literal synthetic credential fixture conflicting with the unchanged credential guard. Local handles now use unambiguous names and the same fake fixture value is assembled only in the disposable test; no guard is disabled. Fault restoration preserves the exact original preparation bytes, including its allowed trailing newline, rather than normalizing the input.

The retained full1329-test run on the previous frozen source failed in three existing regressions. Two required the fixture corrections above; the third restored the preexisting REJECTED classification for invalid initial multi-URL publication configuration before recovery. That preflight remains read-only; later recovery uncertainty stays BLOCKED, and the strengthened dirty-source test proves no commit/index/journal/remote mutation. The original failed log and isolated RED/GREEN observations remain historical evidence; final-current complete checks are required separately.
