# Native parallel work with a durable registry

Use when at least two independent ready parts benefit from available, permitted native subagents: focused research, disjoint implementation proposals or read-only acceptance/review investigations. Use serial work for trivial/dependent tasks or unavailable native tools. Optional native absence alone is no blocker; no parallel SDK or MCP service is required.

For managed delegated work, ase is the durable task registry and the bot owns native spawning/messages. Ordinary questions do not need a managed task just to use subagents. Do not create another agent loop or treat a provider's transient task list as the only project memory.

## Register before native launch

Use the selected project's existing CLI/state home. Save a bounded assignment JSON with `id`, `stage_id`, `goal`, `expected_result`, `mode` (`read_only` or `proposal`), `resources`, and `depends_on` IDs. Register dependencies before their consumers. Resource footprints stay inside the approved stage; these modes return analysis/proposed changes and grant no direct mutation of target resources. Actual integration and external effects keep their existing controlled paths.

```text
ase delegation register TASK --file assignment.json
ase delegation list TASK
ase delegation dispatch TASK WORK --expected-revision N --coordinator LEAD --backend codex
```

Dispatch commits one exact attempt before returning its handoff. Pass that full handoff, original requirements and necessary project/skill instructions to the native worker. Have it write bounded output artifacts only to the ticket's `artifact_dir`. Then call the platform's permitted native spawn tool. Queue excess ready work; no fixed worker count or budget is invented. Registering or dispatching in ase does not itself launch a worker.

Record the actual handle returned by the bot as JSON `{ "backend": "codex", "agent_id": "actual-native-handle" }` using:

```text
ase delegation attach TASK WORK --attempt ATTEMPT --expected-revision N --file native.json
```

Use the actual selected backend (`codex`, `claude` or `copilot`) consistently. Check that the current client exposes the required lifecycle tools. Explicitly carry task/attempt/context/instructions into workers: inheritance differs by platform. In Copilot custom subagents, repository instructions require the appropriate opt-in; a clean-start verifier can intentionally disable them and instead use its captured host policy. Native availability does not establish live support for an ase adapter.

## Capture, accept and recover

The result JSON has `attempt_id`, actual `agent_id`, `status` (`COMPLETE`, `FAILED` or `BLOCKED`), a concise `summary`, and `artifacts` containing relative output paths and actual SHA256 hashes. Never place credentials/raw transcripts in summaries. ase seals and validates the exact result outside SQLite before publishing its reference.

```text
ase delegation result TASK WORK --attempt ATTEMPT --expected-revision N --file result.json
ase delegation accept TASK WORK --attempt ATTEMPT --expected-revision N --coordinator LEAD --summary "Compared result with original inputs"
ase delegation recover TASK
ase restore TASK
```

The lead checks actual output before accepting it. `RESULT_READY` is receipt of a result; `ACCEPTED` is coordinator acceptance of advisory work, not original semantic AC or a permission to apply edits. Dependent dispatch requires intact accepted results and current original context. Accepted research remains dated history after legitimate integration; register fresh work when changed context invalidates a dependency. Shared runtime mutations stay serialized through their existing authority/operation paths.

After interruption, inspect restore/recover before spawning. A complete sealed result can be recovered for the same attempt without another worker. `DISPATCHING`, `RUNNING` or `UNKNOWN` without a recoverable result requires observing the original native worker; attach or record its positive matching result. Timeout, missing acknowledgement and absence from one list do not prove that it never started. There is no automatic retry/expiry/reset. Cancel only undispatched work or discard a recorded terminal result with a reason; active/unknown attempts must be reconciled. Reuse the logical work ID rather than creating aliases to evade its history.

Unsettled delegated work blocks task closure/publication; ase checks again at publication launch. Native observation metadata is supplied by the trusted coordinator and is not independently authenticated provider evidence. The registry cannot prevent a bot from bypassing this cooperative protocol. Complete valid saved results/process crashes are covered; raw transient output, power loss and hostile same-user enforcement are not.

Keep one lead coordinator. Before dispatch, give each agent a bounded brief with these required fields:

| Field | Contract |
| --- | --- |
| Goal and scope | Approved parent outcome/stage and the exact bounded subtask |
| Ownership | Read paths, exclusive write paths, workspace or proposal-only mode |
| Inputs | Original requirements, current source/evidence references and versions, the applicable solution rules; the lead's ideas as hypotheses to test, never a pre-chosen fix |
| Output and acceptance | Structured status/result, evidence references, proposed or actual changed paths, blockers/limitations and observable success conditions |
| Dependencies | Prerequisites, integration owner and when the result may be used |

Maintain a dependency graph rather than a fixed agent batch. After every result, failure, new dependency or changed capacity, recompute the ready set. Dispatch only tasks whose prerequisites are satisfied, bounded by currently available host/provider slots, permissions, resources and the remaining approved budget. Queue excess ready work; do not invent capacity or hardcode one provider's current agent limit. Scale down or up within the approved scope without another user interview. A failed prerequisite holds its dependents while independent work can continue.

A graph may reveal **1 → 4 → 1 → 5** logical ready sets: one prerequisite, four investigations, one integration barrier, then five follow-ups. These are not fixed process counts. Queue excess work until permitted slots free; recompute readiness and release follow-ups after their barrier succeeds.

For mutations use separate authorized workspaces with exclusive write ownership, or have agents return proposed edits for the lead to apply. Shared-root runtime mutations remain serialized even for disjoint filenames. Preserve root execution leases, CAS, operation reconciliation and receipt bindings; do not launch conflicting `ase run`/check/verify calls or bypass a root lock. Integrate at dependency barriers and run final affected tests and independent reviews against the combined current result.

Reuse original-assignment authorization, persisted recovery history for each underlying problem and any explicit root/stage limits. Decomposition, aliases, additional agents, new waves and session changes cannot reset unsuccessful-cycle/diagnostic/probe history or create external authority. A delegate's new prose is not verified progress. New material outcome, serious architecture, resource, cost/risk or authorization changes still follow questions-and-approval.

Native advisory answers help investigation and integration. They are not formal acceptance/code-review evidence unless a genuine independent Runner execution captured the required receipt for the current source/contract/plan/check bindings. Optional native delegation and formal independent verification are separate capabilities; serial fallback does not waive mandatory verification.

Pattern basis (primary sources, checked 2026-11-12): [Anthropic](https://www.anthropic.com/research/building-effective-agents) describes dynamic workers and synthesis; [OpenAI](https://developers.openai.com/api/docs/guides/agents/orchestration) retains a manager's reply ownership with bounded specialists; [LangGraph Send](https://docs.langchain.com/oss/python/langgraph/graph-api#send) supports runtime-sized fan-out and explicit shared-state reduction; [GitHub](https://docs.github.com/en/copilot/concepts/agent-capabilities/choosing-and-delegating-agent-work/delegating-and-parallelizing-work-with-agents) covers dependencies, context, integration and coordination costs. Local design inference: use a lightweight ready queue and native capabilities without adding those frameworks as dependencies.

Before dispatching an overlapping assignment, positively attach the existing native handle: DISPATCHING is already an unresolved launch, even before recover marks UNKNOWN. Distinct RUNNING analyses may share read-only inputs, but an unsettled identical intent cannot be relaunched under a new work ID. Resource/dependency order does not distinguish intents; rewritten goals are not machine-proven semantic equivalence.
