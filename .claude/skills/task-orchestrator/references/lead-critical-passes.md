# Additional passes by the original main bot

After initial implementation and meaningful executed checks, the original main
chat must reconsider the whole solution `additional_passes` times for configured
nontrivial root tasks. Do not wait for the user to request edge cases. Workers
may investigate bounded questions; their reviews do not count as main-bot passes.
Independent acceptance, substantive review and software adequacy remain separate.

## Applicability and requirement authority

New CLI tasks get `additional_passes` 1 (LOW/MEDIUM risk) or 3 (HIGH).
Programmatic creators carry the explicit policy. Saved unconfigured contracts
stay historical; never fabricate old passes or silently rewrite their policy.
The requirement applies to final root completion in every supported domain.
Children/intermediate stages keep their own checks without another root barrier.

Default uncertain complexity to nontrivial. For genuinely mechanical LOW-risk
single-stage work, `lead-review classify` needs the concrete reason and current
passing checks/source. No actions, access, unresolved discovery/requirement gaps,
required children or mixed work. Reassess changed input. Independent AC must
explicitly agree with the exemption; a cheap implementation is not triviality.
Once a substantive critique is credited, the earlier mechanical classification
is historical: it cannot bypass recorded findings or the required review depth.

Every pass reads the original request and full approved contract/proposal
history from `prepare`. Include approved owner clarifications and label explicit
supersessions. Current code, implementation summaries and generated tests cannot
redefine expectations. Assess every mandatory criterion and original/latest
request using its provided requirement hash and concrete source/check evidence.
Missing relevant facts are UNVERIFIED, not invented business policies or bugs.

## Pass focuses

Each pass inspects the complete task. One pass covers all three focuses:

1. Requirements/business scenarios: omitted constraints, contradictions,
   unsupported assumptions and consistency with approved clarifications. For a bug,
   trace the reported actor, trigger, scope and expected result to the changed
   decision and executed evidence.
2. Attempts to break the result: boundaries, invalid inputs, dependency errors,
   repeated/concurrent effects, recovery, relevant security and performance.
   Performance conclusions need measurements. Justify inapplicable areas.
3. Whole-solution requirement reconciliation after fixes: interactions, actual
   consumer behavior, meaningful assertions and regression coverage. Check every
   original/current obligation again.

Every pass also sweeps the whole diff for domain conversions and magic values
(date/code formats, units, external-system numbers or keys, unexplained
literals): each one uses an existing helper or constant, goes into the existing
helper class of its domain as a method or named constant, or becomes a named
constant with a reason. Code that is correct is not exempt.

For software, read the final diff as the developer who will explain it. Check whether each
nontrivial rule belongs in the chosen class, whether a maintained neighbor already handles it
and whether names and control flow fit the project. Trace material contract changes to callers,
including callers outside the ticket's immediate path. Check actual dependency and deployment
files against the approved decisions. For an integration removal, verify remaining references
in configuration, deployment and docs. For a boundary mapping, compare the actual producer and
consumer contract and any compatible examples inspected within scope. Check original producer
output and the affected consumers before accepting a workaround. Check the rationale in the PR
draft for nonobvious public signature changes.
If an in-scope repair needs a material decision outside the plan, get approval before editing.
Report unrelated defects, including ones that block a check, through
[out-of-scope discovery](../SKILL.md#out-of-scope-discovery). Preserve the owner of an approved
deferred obligation in the handoff.

Zero findings is legitimate. A finding identifies a requirement, concrete
violation/counterexample, source and executed evidence. Investigate suspicions;
do not mutate for speculative defects, preferences or invented requirements. A
newly found risk without a category (a) counterexample ([risk triage](solution-design.md#2-triage-every-risk-only-required-risks-become-code))
goes under "Not handled".
Where useful, check that tests detect a relevant deliberately broken behavior;
passing counts alone do not establish fault detection.

**Record the assessment before fixes.** Repair confirmed in-scope findings under
existing authority, rerun affected checks/regressions, then perform the next
pass on current facts. If input changed before capture, retain the original
response as stale, settle its ticket and prepare a current critique. Earlier
valid passes survive later fixes, but the final critique must be current. A fix
after the last pass requires a corrective follow-up. Explicitly resolve earlier
finding IDs; omitting them cannot close them.

A new approved requirement anchor starts its own pass depth. It does not
close old material findings, even when a criterion is renamed or removed. The
host replays authenticated prior series, including old databases without carry
fields. `prepare.finding_ledger` identifies each finding's original anchor.
New response schemas require `origin_anchor_sha256` in resolutions: use an empty
string for this series or the exact inherited origin for an older finding.
IDs may be reused in another series without conflating their obligations.
Legacy responses remain local to their original series. A `fixed` disposition
needs changed evidence; `not_a_bug` needs counterproof under the original rule
or a concrete explicit saved owner-approved supersession, challenged by AC.
Old open findings also block a later trivial exemption. Original inputs for
inherited obligations remain available beside current passes; old passes never
count toward current depth. Missing/tampered credited history blocks completion.

Pass depth is initial checking depth, not a guarantee or a repair cap. Continue for
material gaps under existing progress-sensitive recovery. Three unsuccessful
repair cycles without verified progress trigger reassessment; another review
paragraph is not progress. Ask only for a genuine remaining decision/access/scope
blocker. Routine fixes of defects inside the approved task scope or caused by this
task's changes, and further supported checks, do not need another approval; any
other defect is reported, never fixed ([out-of-scope discovery](../SKILL.md#out-of-scope-discovery)).

## Reading earlier passes in a packet

Each pass's `inputs` in `_ase_context/lead_review.json` names its `packet_path`:
an identical current file or `_ase_lead_history/<hash>.txt`. Compare earlier
findings with those original inputs and with the current corrections. A
`line_delta_v1` reference names a JSON recipe over a complete current base file
in the same packet (`base_packet_path`): split the base with
`splitlines(keepends=True)` (`line_delta_v2` first normalises CRLF to LF), copy
base lines before each `start`, append its `content`, skip base lines up to
`until` (exclusive), then append the rest; indexes refer to the original base. A `$ref` contract points to the identical
`_ase_context/contract.json`; a `/scope` reference may list `exclude_resources`
to drop from the current order. The host has already checked every hash, size and
range before the packet was built, so do not claim to have recomputed hashes;
reconstruct read-only and never treat it as an edit. Missing, tampered or
oversized history is listed as missing coverage and prevents verified completion.
A URL the owner supplied is scope, not proof of observed state; remote checks are
proven by the captured log named in `check_sources`. Do not ask the owner to rebuild
retrievable local evidence or drop an earlier finding to obtain approval. Stage packets avoid the final-root barrier.

The original main bot's commands, recovery and sealing rules are in
[lead-review CLI](lead-review-cli.md).
