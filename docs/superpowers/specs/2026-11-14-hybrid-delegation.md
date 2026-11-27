# Hybrid native delegation with a durable local registry

The owner approved a hybrid: bots choose and create native subagents; ase records delegated assignments, dependencies, execution references, results and recovery. Implement with the existing Python3.11+stdlib/SQLite project storage and CLI, without MCP, a universal worker pool, new service or automatic cloud effects. Native support is capability-dependent; only Codex is available for live validation here.

## Assignment and authority

Each immutable assignment has an ID unique inside the managed root, owning task/stage, goal, expected result, mode (`read_only` or `proposal`), bounded resource footprint and dependency IDs. These modes return analysis or proposed changes; actual target changes continue through the existing controlled coordinator/apply/operation paths. Registration is neither action authorization nor semantic acceptance. Validate the managed stage's current original approval ancestry and declared scope; persist project/workspace/contract/proposal/plan/source bindings. Registry rows are operational state and do not enter source fingerprints.

Use existing SQLite transactions/CAS and concise events, not the parent's execution lock: native results must be recordable while the parent waits. Registration is idempotent only for the same immutable specification. Dependencies refer to previously registered same-root assignments; this makes cycles/forward references invalid. Recompute readiness from current authority/context and verified accepted dependency results. Do not reuse stale dependency facts or create another logical managed task to reset limits.

## Lifecycle and recovery

Readiness and dispatch apply the same duplicate/uncertain-overlap checks. Before a dependent dispatch or acceptance, validate the entire prerequisite DAG, including each accepted node's current authority, artifact integrity and retained dependency-result bindings. Shared prerequisites are checked once per traversal; the ticket still records its direct dependencies.

Dispatch must own its SQLite transaction so its returned launch ticket is already committed. Embedded callers must commit their outer transaction before requesting dispatch; releasing a nested savepoint is not durable permission to start a native worker. Other metadata operations retain their existing transaction behavior.

`PENDING -> DISPATCHING -> RUNNING -> RESULT_READY -> ACCEPTED`.

Commit one exact attempt ID, backend, coordinator, handoff context and dependency-result bindings before the bot calls its native spawn tool. Return a portable ticket including the attempt ID and a private output directory. `attach` records the exact observed native handle; it never starts a process. A missing acknowledgement or interrupted coordinator produces `UNKNOWN`, never an automatic replacement attempt based on timeout, PID absence or elapsed time. The bot reconciles the native worker; a positive matching result or observed handle can restore that same attempt. This version has no blind retry/reset API.

Capture a bounded result envelope outside SQLite under the exact attempt directory, with task/work/attempt/context/native identity and hashes of bounded output artifacts. SQLite stores its locator/SHA and summary metadata. Seal the complete result before publishing its RESULT_READY record; a kill after sealing but before DB commit is recovered from that exact known artifact. Invalid/partial/foreign files remain explicit holds; no latest-result scan. Repeated attach/result/recover calls do not spawn workers or apply changes.

The coordinator separately accepts a COMPLETE result after current approval/source/dependency and artifact integrity checks. Acceptance only means the delegated advisory work was received and accepted; independent original AC/code review and the current gate remain mandatory. Accepted advisory history survives subsequent legitimate integration, while a new dependent dispatch must use fresh context. A coordinator may cancel undispatched work or discard a recorded terminal result with a reason; active/unknown attempts cannot be cancelled away without a terminal observation. No timer grants permission to restart.

## Closure and integration

An unacknowledged DISPATCHING launch blocks a new overlapping assignment even before recover labels it UNKNOWN. Once the original native handle is positively attached, distinct RUNNING analyses may share read-only inputs. The same immutable intent (all specification fields except the work ID, with resource/dependency order ignored) cannot run again under another ID while its earlier attempt is unsettled. These cooperative alias checks do not infer semantic equivalence between arbitrarily rewritten goals.

Expose register/list/dispatch/attach/result/accept/cancel/recover through `ase delegation`. Include registry state and exact next actions in restore/report. Pending/unsettled same-root work blocks whole-task closure; stage gates consider work owned by that stage. Recheck registry blockers/revisions in Publisher's existing short transactional launch guard before add/commit/push to prevent a gate-to-launch race. Do not add registry progress to source fingerprints or promote native summaries to independent Runner receipts.

Update canonical parallel-work guidance, generated bot mirrors and role resources. The bot registers useful independent work, dispatches only ready nodes, passes the explicit ticket/context to each worker, records actual native handles and captured output, then accepts or investigates results. Native task lists are execution views; ase is the durable registry. Existing provider permissions and authorization remain unchanged.

## Verification boundary

Meaningful tests cover DAG readiness, CAS claims, exact attempt/native/source/authority binding, root/child isolation, stop/reapproval/source drift, sealed-result crash recovery, unknown dispatch non-replay, artifact tamper, cancellation and closure/publication races. Live Codex exercises real native subagents and dependent waves, a killed local coordinator, same-worker/result recovery and final independent native source acceptance. Protocol fixtures and coordinator attestations are labelled separately; no Claude/Copilot live, raw-on-wire/power-loss or hostile same-user guarantee is claimed. Registration cannot prevent a bot that bypasses the controlled protocol from making direct actions.
