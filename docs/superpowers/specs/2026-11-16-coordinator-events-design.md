# Coordinator events and test adequacy

The owner approved skills plus a durable local event queue, results returned to the original main chat, test-adequacy assessment, actual Codex validation, independent review and publication to origin/main. Routine decisions are delegated. Preserve Python 3.11+ standard library, SQLite, project isolation, Codex/Claude/Copilot and no mandatory MCP.

## Design

Keep one original coordinator. Store its explicit provider/thread identity per root task. A delegated worker or independent verifier produces evidence, never a new user authorization. Persist immutable bounded event data and delivery state in the existing transactional runtime records. Publish result events in the producer's transaction; reconcile sealed historical results after interruption. Events identify their source attempt/receipt and deduplicate that same result. Current source/task bindings accompany results; outdated results remain history rather than current approval.

Native tools remain responsible for sending to the exact registered chat. The CLI supplies a handoff and records an explicit dispatch before sending, then captures delivery acknowledgement separately from coordinator processing. Unknown delivery is reconciled, never retried because a timer elapsed. A duplicate event cannot trigger another repair. Host transport is optional: the active coordinator can consume its own durable inbox through the CLI. A missing wake-up capability is disclosed and must not be represented as a delivered message. Neither a share URL nor an arbitrary coordinator label proves a live desktop destination.

Test adequacy is a separate independent assessment of whether the checks address the original behavior and applicable risks. Record scenarios, required observable boundaries, actual executed evidence and applicability rationale. Positive test counts alone do not prove adequacy. Relevant examples include simultaneous consumers, state changes during I/O, tied ordering keys, actual HTTP/SQL behavior, failed/repeated external effects and recovery. Applicability is task-specific, not an exhaustive universal checklist. Missing required scenarios prevents an adequate verdict; source, contract, plan or check changes invalidate old proof. Existing saved contracts remain compatible; current adoption skills must explicitly enable the new policy for new software work and disclose legacy unconfigured coverage.

The lead processes findings, verifies them against current code, repairs in-scope defects and repeats affected checks and fresh independent assessments. Preserve existing progress-sensitive stopping and owner-only questions. Notifications do not reset attempts, expand scope, authorize publishing or turn advisory worker output into formal acceptance. Test exclusion from a published PR never implies exclusion from local checks.

## Delivery and limits

Use the existing registry and record CAS, receipt verification and source fingerprints. No new universal worker pool, polling timer, broker or model framework. Codex gets actual native-worker and current-chat return tests. Claude/Copilot get protocol/packaging checks; live accounts remain unavailable by the owner's earlier instruction. Verify exact desktop routing capabilities before describing wake-up as supported. Actual publication and remote CI observations remain later delivery work, outside the pre-publication source gate.

## Alternatives

Skills alone are simpler but cannot retain delivery state. A separate always-running orchestrator would duplicate existing ownership and introduce operational dependencies. The approved hybrid keeps the existing bot as lead and uses ase for durable state, evidence and recovery.
