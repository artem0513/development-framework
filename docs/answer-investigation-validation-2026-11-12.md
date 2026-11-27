# Answer investigation — validation, 12 November 2026

The approved update extends existing discovery, intake, communication and continuity skills. The [investigation reference](../skills/task-discovery/references/answer-investigation.md) is loaded for unresolved questions; the ordinary workflow stays concise. It ships through the existing installer to Codex, Claude and Copilot without MCP or a new runtime dependency.

## Resulting behavior

Select authorized evidence for the question rather than scan every source category: current code/contracts, relevant Git/PR/ADR rationale, bounded logs/traces, schema/migrations or read-only incident data, and relevant service contracts. Preserve the example-search opt-out. Distinguish observations, inference, unknowns, unavailable sources and uninspected leads.

Default investigation effort is one targeted pass and at most one useful follow-up for the same unresolved question/group, shared across delegates and resume. A different bound can be agreed or recorded before starting within existing task constraints. Stop early on sufficient evidence, no useful new lead, exhausted/unavailable relevant sources, or an owner-only decision. A reached limit does not establish universal absence of information.

Ask only for a remaining consequential decision with no supported safe in-scope choice. Present checked evidence, a recommendation and its reason to the owner first. Team questions need owner review of the concrete question and destination; standing channel permission does not bypass it, and already approved concrete questions need no repeated approval. Continue independent already approved work. A lower-risk hypothesis alone does not authorize a new business commitment.

Save concise provenance, uncertainty, stopping reason and shared effort in existing local knowledge/checkpoints and dated records, with freshness checks on reuse. Do not store raw logs, database rows or credentials. No memory schema or deterministic investigation counter was added; these are portable model instructions, and a counter alone would not establish search quality.

## Executed checks and behavioral evidence

- **17 portable-install tests passed.** Existing three-target tests compare installed skill resources byte-for-byte with canonical sources in disposable projects, including the new reference. Runtime code was unchanged; no new full runtime-suite run is claimed.
- Generated native descriptors match. The adopted repository-local Codex package was synchronized with backups of changed existing files. Its 28 owned files pass the hash audit. Four changed entrypoints pass the skill-creator frontmatter validator; local skill Markdown links resolve. Provider/global configuration was not changed.
- **Five baseline and five updated Codex sessions, eight scenarios each.** Independent reading covers all 80 simulated decisions: targeted history, bounded incident diagnostics, exhausted research, access denial, owner-first routing, no-template opt-out, sufficient evidence and a new business decision. The baseline already handled these situations correctly; these runs do not establish a measured improvement or error-rate guarantee. Updated runs make the research bounds and source limitations more explicit.
- A separate native evaluation exercises shared lead/delegate effort, standing team-question permission and contradictory sources. It exposed an unsupported assumption that a lower-risk settlement choice was an approved default. The reference was clarified to require current rules/contracts or delegated discretion. That original evaluation is preserved rather than rewritten as a success.
- Five fresh Codex sessions on the final wording exercise shared effort at the limit with an unread lead, standing team-question permission, a useful remaining contradiction check, reuse of a concrete owner approval, and a supported routine default. Their results and the final independent acceptance/review are recorded separately in the local evidence directory and dated closeout; this document does not substitute a model reply for an evidence gate.

The first focused packet described eight hypothetical decisions although it actually contained five. Some responses consequently reported three missing cases. Its results remain preserved with that qualification; the collector description was corrected and only that focused group was repeated against unchanged final skill bytes. It is not used as an unqualified coverage proof.

The initial formal gate also remained blocked: the collector placed role receipts outside the canonical project store, and acceptance requested a source-preservation inventory and actual memory/journal readback. The gate's receipt boundary was preserved. The collector was corrected to use the store, and the additional evidence was captured for fresh independent judgments; earlier blocked results remain historical evidence.

These are planning simulations using supplied synthetic facts, not live GitHub navigation, production log/database queries, VM provisioning or team messages. Live Claude/Copilot behavior was not tested at the user's request. Structural portability is checked; equivalent model judgment is not guaranteed. The investigation bounds and owner-first route are model guidance, not enforcement against another process with the same OS identity.

Local evidence, intentionally excluded from Git, is under `outputs/clarification-research-20261112/`: immutable requests/results/receipts, behavior observations, focused scenarios, installer logs, skill validation, adopted-copy backup/audit and session context. Published consumers receive the skills and this report, not private local evidence files.

## Basis and design choices

The prior research used [Anthropic's context engineering guidance](https://www.anthropic.com/research/effective-context-engineering-for-ai-agents) for relevant just-in-time evidence, [Google SRE troubleshooting](https://sre.google/sre-book/effective-troubleshooting/) for discriminating hypotheses and observations, and [Anthropic's agent patterns](https://www.anthropic.com/research/building-effective-agents) and [OpenAI's practical guide](https://openai.com/business/guides-and-resources/a-practical-guide-to-building-ai-agents/) for explicit stopping conditions and human intervention. The exact one-pass/one-follow-up default and owner-first team-review rule are this project's design for the user's preferences, not universal vendor standards.
