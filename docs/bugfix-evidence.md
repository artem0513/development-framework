# Causal evidence for software bugfixes

Use `ase task create --file task.json --bugfix` for new bugfix work. Programmatic
contracts use `bugfix_evidence: {"version":1,"required":true}` and required test
adequacy. Other new tasks and saved legacy tasks do not silently acquire this
policy. Instructions classify actual work; the runtime does not guess keywords.

Save the usual scenario plan with an additional `bugfixes` array, one entry for
every software stage with acceptance obligations; empty coordination/delivery
phases do not need fictitious regressions. Example investigation:

```json
{
  "stage_id": "repair",
  "symptom": "Updated response fields disappear at the consumer",
  "expected": "Consumer uses the complete response under API v2",
  "first_divergence": "Caller returns its old request instead of the response",
  "cause_status": "CONFIRMED",
  "trace": [{"path":"client.py", "locator":"def apply_rules", "observation":"Response is assigned, then the old request is returned"}],
  "causal_evidence": "Captured sanitized response and controlled old-caller failure; see executed regression logs",
  "fix_rationale": "Return the response at the existing boundary",
  "alternatives": "A merger masks this defect and can drop other legitimate changes",
  "regression_scenario_ids": ["rules-consumer"]
}
```

The scenario maps to the stage's acceptance and executed check IDs. Each trace
`locator` must be an exact, case-sensitive substring of the referenced file's
independently captured content. A trace's optional
`version` selects `current` (default) or authenticated `before` source, allowing
deleted/renamed faulty code to remain attributable without changing history.
There is no fallback between versions. `HYPOTHESIS` and
`WORKAROUND` can be saved and investigated, but cannot pass as confirmed causal
repairs. Existing CAS/hash/receipt bindings invalidate changed investigations and
stale judgments. No SQLite migration or historical-memory deletion is needed.

A fresh managed causal-repair candidate requires a current confirmed
investigation before reservation/model launch. Otherwise it returns an internal
investigation next action with `needs_owner_input: false`. Authorized diagnostic
probes/reversible tests remain available. The refused `task run` leaves the task
BLOCKED; save the investigation with `ase adequacy plan`, then use
`ase task resume` (a repeated `task run` is refused). Resume/reconciliation does not replay
or block existing effects under this fresh-candidate guard.
An editing stage with no criteria maps to its nearest causal acceptance
descendants, or prerequisites for a later delivery phase. This protects split
`edit -> validate` workflows without inventing scenarios for nonmutating
coordination. Missing stage mapping needs internal technical-plan refinement.

The separate adequacy session returns `bugfixes` entries with `stage_id`, `cause`,
`necessity`, `regression`. Each assessment has `status`, `evidence`,
`missing_coverage`, `reason`, using ordinary executed-test proof. AC and review
also challenge the claims. Missing proof, unavailable reviewers and protocol
fixtures cannot authorize verified completion.

Every independent request carries host-computed `verification_scope`: current
stage, acceptance IDs, check IDs, scenario IDs, bugfix stage IDs and their
per-scenario/per-bugfix check mappings. AC IDs and causal proof
check IDs are also constrained by the generated response schema. This keeps
a downstream consumer proof explicitly pending instead of falsely requiring it
at an earlier editing gate. Missing current proof still blocks; current source
defects remain review findings. The whole-task gate keeps all mandatory criteria.

The gate validates evidence structure/currentness and independent judgments,
not semantic causality by itself. Partial-patch contracts can justify merging;
full-object responses or DTO defects can require other corrections. Use safe
baseline/fault-injection evidence and actual consumer tests, without destructive
production reproduction. See the [workflow guidance](../skills/task-orchestrator/references/bugfix-evidence.md).

A green command is insufficient when the cited consumer case did not run.
The gate checks case-level successful execution identity in the bound output,
including source/discovery/script binding and pytest parameter IDs. Verbose
unittest and pytest case statuses are recognized; aggregate-only or unsupported
output remains unverified until suitable evidence is collected. No PASS is
inferred from a test name merely appearing in source.

The confirmed plan is pinned at reservation and revalidated at actual candidate
process launch. Withdrawal blocks launch; the short writer transaction is not
held while waiting for the model. A late denied launch preserves its durable
reserved intent for reconciliation instead of risking replay.

`scripts/check_native_bugfix_evidence.py --consumer <capture> --cases <capture>`
provides an ordinary mapped check for this repository's disposable native test
fixture and advisory batch. It verifies authentic current captures and emits
complete case outputs for independent interpretation; it does not launch models
or grade advisory decisions. It preserves captured data and rejects unavailable,
stale or protocol-only evidence. Packet decisions remain distinct from actual
consumer execution.
