# Project examples before implementation

Date: 2026-11-12. Scope: the user's requested default search for relevant existing project examples, with an explicit task-level opt-out, shared by Codex, Claude and Copilot. This does not implement the separately discussed fully project-local installation/state mode.

Before proposing implementation, inspect applicable requirements and find relevant precedents in the authorized project: for example an HTTP client and its tests in another microservice. Select a compatible, maintained example or shared abstraction after checking the current sources. Identify the source and what will be reused in one concise proposal line. An example is evidence, not automatically a project standard.

An explicit instruction such as "не брать по шаблону" or "без поиска аналогов" skips deliberate example search and reuse, including cached examples and delegated searches, for that task. Necessary inspection of target interfaces, dependencies, tests and mandatory local rules still happens. Do not ask which routine filename or pattern to choose when a safe choice is supported by the project.

## Approach and boundaries

Extend the existing task-discovery skill and link it from intake/orchestration, rather than introduce another orchestrator or a mandatory indexing service. A whole-project embedding/indexing system would add freshness and operating costs without being needed for this scoped lookup. A separate skill would make the default depend on an extra routing decision. Existing source-backed knowledge records can retain discovered examples and their hashes; revalidate affected sources when reused.

Search named resources first, then relevant shared packages and sibling services in the same authorized project/workspace. Use file/symbol/reference searches and inspect a small useful set of examples. Do not scan unrelated checkouts, fetch private repositories or change a sibling service merely to inspect it. An unavailable external example remains a limitation; perform the useful local search and proceed when the task is otherwise understood.

Prefer applicable explicit project rules and maintained, compatible examples. Retired code, old cache entries, insecure settings and unsafe retries are not templates to copy. Explain material divergence; ask only when conflicting current approaches expose an unresolved material decision.

## Implementation and verification

1. Preserve a current-policy control and evaluate realistic discovery decisions before editing the skills.
2. Add a concise discovery entry and a self-contained reference; update intake/orchestration and workflow documentation.
3. Re-evaluate default search, explicit opt-out, cached examples, conflicting/retired patterns and unavailable external examples with fresh Codex sessions. Keep simulation evidence separate from actual filesystem behavior.
4. Validate canonical skills, portable installer copies for all three targets and repository-local adopted copies. Obtain separate source-bound acceptance/review, save dated results and remaining limits.

No runtime schema, authentication, permission, approval or quality-gate change is required. Live Claude/Copilot execution remains untested by the user's earlier choice.

## Research checked on 2026-11-12

- [OpenAI Codex prompting guide](https://developers.openai.com/cookbook/examples/gpt-5/codex_prompting_guide): inspect prior art, reuse helpers and follow codebase conventions; explain necessary divergence.
- [Anthropic large-codebase guidance](https://claude.com/resources/articles/how-claude-code-works-in-large-codebases-best-practices-and-where-to-start): focused filesystem/reference searches against current code, with concise local context instead of indiscriminate whole-repository reads.
- [Claude Code best practices](https://code.claude.com/docs/en/best-practices): keep instructions focused; explicitly identify misleading legacy patterns and distinguish model guidance from enforced checks.
- [GitHub repository custom instructions](https://docs.github.com/en/copilot/how-tos/copilot-on-github/customize-copilot/add-custom-instructions/add-repository-instructions): repository-wide and path-specific guidance supplies relevant project conventions.

The conditional default, opt-out wording, selection record and use of existing local memory are this framework's design based on the user's requirements, not a guarantee made by those vendors.
