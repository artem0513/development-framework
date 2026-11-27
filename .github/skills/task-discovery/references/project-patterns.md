# Existing project examples

Use during discovery before proposing implementation. This applies to code, infrastructure, documents and other managed work: look for a relevant existing client, deployment recipe, report or other deliverable in the authorized project.

## Choose the search mode first

Default: perform a bounded search for existing project examples. An explicit instruction such as "не брать по шаблону", "без поиска аналогов", "do not use existing examples" or "from scratch, without templates" skips deliberate example search and reuse for that task. Do not retrieve cached example bodies, search peer implementations or delegate the same search after this opt-out. Do not use an example already in context as a design template. Continue reading necessary target interfaces, actual dependencies, contracts, target tests and applicable mandatory instructions. Record the opt-out in the task constraints; it does not disable another task's default unless the user explicitly makes it a project-wide rule.

## Find a useful precedent

1. Retrieve relevant source-backed example knowledge when search is enabled. Validate current source bytes/versions before reuse; a matching Git commit alone does not cover dirty files. Refresh affected knowledge rather than rescanning the whole project.
2. Start in the target component and applicable local instructions. Search relevant shared libraries and sibling services in the same authorized project/workspace using file names, symbols, imports and references. A sibling need not be a runtime dependency to be a relevant precedent. Inspect useful candidates and their tests, not every source file. Stop once there is enough evidence for a sound choice.
3. Use another repository only when it is explicitly available and within the permitted task context. Do not scan unrelated checkouts, clone repositories or request new access merely to find a template. If an external example is unavailable, still do the useful bounded local search. Report the unavailable comparison and proceed if it does not block the required outcome.

## Decide what to reuse

Prefer applicable explicit project rules, then maintained examples compatible with the target component, its stack and current dependencies. Reuse an existing shared helper when it fits. Before writing a conversion of a domain value or a magic literal, search for the literal, its format and the domain name; when the project has a helper class for that domain, the new method or named constant goes there. One colleague's implementation is a precedent, not proof of a team-wide standard. Distinguish documented rules from observed examples and an inferred recommendation.

Check the behavior relevant to the task. For an HTTP client, that may include configuration/DI and lifetime, timeout/cancellation, serialization, error mapping, safe retry semantics, observability and the existing testing approach. Do not carry over insecure settings, secrets, retired code or inappropriate retries. Adapt the pattern to the new contract rather than duplicate service-specific behavior or introduce a dependency solely for resemblance.

Before a cross-service mapping, read the current producer and consumer contract even when examples were opted out. When example search is permitted, compare a maintained neighboring implementation that uses the same contract. Compare wire values, units and null handling as applicable. Reuse the compatible representation. If contract and practice disagree materially, state the evidence and obtain the contract owner's decision.

When current examples conflict, choose the closest applicable documented and compatible approach and briefly explain why. Ask only if a remaining conflict materially changes the outcome, compatibility, risk, cost or authority and has no safe project-supported choice. An optional missing example alone is not a blocker. If the user explicitly requires exact conformity to an inaccessible example, report that missing requirement instead of guessing conformity.

## Record the decision concisely

The discovery result and proposal reuse line contain one of:

- **Found:** concrete source locators, what will be reused and why it fits; distinguish inspected evidence from files still to inspect.
- **Not found within scope:** the bounded area searched and the proposed approach. Do not claim that no example exists anywhere.
- **Skipped by user:** the task instruction that disabled search/reuse; normal dependency and requirement discovery continues.

When an unchanged class, caller or rule is decisive to judging code placement or compatibility, record its exact file as a required discovery dependency for the relevant stage. This puts the source in the independent review packet under the existing source policy. The write scope remains limited to the task.

For example: "Use the client factory and MockTransport test approach from `services/shipping/clients/catalog.py` and its tests; adapt endpoints and errors for invoices." Use such a line only after checking those actual sources. Save concise locators, freshness and the decision through existing project knowledge/checkpoint/journal records; do not copy whole implementations or raw source into memory. Reuse does not grant permission to modify the example service or bypass checks, approval or independent review.
