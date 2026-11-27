# COE 2026-11-23: consumer-side retry instead of the existing source fallback

Blameless; built from `retry-excerpts-20261123.txt` (anonymized, edited excerpts). The original
session, its framework version and the application code were not available.

## 1. What happened

- In a read-only investigation the lead recommended a bounded retry/timeout on HTTP 404 in the
  consumer ("smallest"), then a consumer-side table change (migration) as "the right" fix.
- What should have happened: the source service already had, in a sibling handler, a fallback
  from the replica to the real-time store; the fix belonged there. A human pointed it out.
- Detected by: the owner (T4/T5).

### Observed chain (facts from the excerpts)

| Step | Fact | Effect |
|---|---|---|
| T0 (lead -> worker brief) | "Conclude root cause and fix options from smallest up (e.g. use the value already calculated in the same request, or order/wait in OrchestratorService)" | Solution space seeded with consumer/orchestration options before the source was analysed |
| T1 (lead -> worker brief) | "In ConsumerService ... Also which line would be the right place for a bounded retry on 404" | Scope limited to the consumer; retry pre-chosen; the worker was asked for a location, not for a judgment |
| T3 (lead -> owner) | Options 1-4 all consumer/orchestrator/infrastructure; source-side handling not mentioned; "retry is the smallest" | Smallest diff treated as smallest change; fix-at-source not applied |
| T5 | The existing source fallback (sibling handler) named only after the human hint | Callee siblings were not read |

Hypotheses (not provable from the excerpts): the lead did not load task-orchestrator/solution-design
for an investigation; T0/T1 were native subagent briefs without the rules; the source code was in
another repository. Missing evidence: full tool trace, loaded instructions, framework version
(timestamps anonymized; the proportionality rules exist since be61de6, 2026-11-21, so a session
before that date had none of them).

## 2. Why (framework mechanisms, HEAD 617d7ab before this change)

1. Scope: `skills/task-orchestrator/SKILL.md:16` applies the rules "Before choosing a software or
   infrastructure solution"; the same line lets research proceed without them. A read-only
   investigation that *recommends* fixes fell outside; `:19` limits "No new ... retries" to what
   "becomes code".
2. Analyse first (`SKILL.md:18`) and Reuse first (`references/solution-design.md:34`) ask where
   the system meets the condition and for mechanisms to add, but not to read the **callee and its
   sibling paths** for existing handling. Root cause (`solution-design.md:40`) stops at "first
   place the value becomes wrong".
3. Fix at the source (`SKILL.md:20`) speaks of a "wrong value"; a 404 for data the source already
   accepted (replica lag) reads as a consumer timing problem. The limit on consumer workarounds
   (`solution-design.md:88`) is only in the reference file.
4. Smallest change vs. fix at source: "Smallest change" (`SKILL.md:19`) and "options from the
   simplest" (`SKILL.md:33`) are measured by diff size; nothing says a workaround at the wrong
   place is not simpler (that sentence existed only in `solution-design.md:6`).
5. Delegation: `references/parallel-work.md:17,50` pass "original requirements and necessary
   project/skill instructions" at the lead's discretion; nothing says a lead's idea is a
   hypothesis, and nothing obliges a worker to report a better-placed mechanism.
6. Loading: the always-loaded routing (`scripts/install.py:438-448`, managed AGENTS.md block)
   contained no solution rule; Runner diagnostic sessions got no solution rules at all
   (`ase/runtime_policy.py:26`). Eval evidence: read-only workers did not load the skill in 5 of
   6 X7D sessions before the routing rule (rlb3, rln3).

## 3. Changes

| Change | Where |
|---|---|
| Rules cover recommendations, quick fixes and delegated briefs; read callee + sibling paths first; unavailable callee code stays a hypothesis | `skills/task-orchestrator/SKILL.md` (scope line, Analyse first) |
| "Smallest correct change at the right place, not the smallest diff"; retries/timeouts/waits/polling only with a requirement **or a documented contract**, recommendations included | `SKILL.md` (Smallest change), `solution-design.md` §3 |
| Replica lag / stale read is a source fault unless the source's contract allows it; another team's ownership alone does not justify a consumer workaround; consumer retry/wait/fallback only by requirement/documented contract or when the source is outside access/authority, temporary, owner decides | `SKILL.md` (Fix at the source), `solution-design.md` §4 |
| Options: a workaround at the wrong place is not the simpler option | `SKILL.md` (Plan) |
| Brief carries the solution rules and a hypothesis, not a chosen fix; workers report better-placed mechanisms | `SKILL.md` (Execute), `parallel-work.md` brief table |
| Always-loaded one-sentence rule (source first, lead's fix is a hypothesis, retry only by requirement/contract) | `scripts/install.py` `SOURCE_FIRST_RULE`, this repository's `AGENTS.md` |
| Runner diagnostic role receives solution-design/calibration | `ase/runtime_policy.py`, regenerated `ase/data/runtime_policy.json` |
| Regression fixture X1-X7D, rubric fixed before runs; harness arms now also version the AGENTS.md block | `scripts/eval/systems_replica.py`, `rubric.md` (Round 4), `harness.py` |
| Tests | `tests/test_replica_eval_fixture.py`, `tests/test_runtime_policy.py` |

No absolute retry ban: X5 (documented 503 contract with a shared policy) must still add the retry.

## 4. Verification (real Claude Code sessions, read-only, 3 or 2 reps; Codex was out of quota)

Batches rlb1-rln5 (41 attempts) were graded blind (arm hidden) by two grader sessions and a focused
regrade session against the pre-fixed rubric; they disagreed on 11 rows, which the lead adjudicated
by quoting the decisive sentence (not independent). rln6/rln7 were graded by a fourth session that
could see the batch names (not blind). Records: `%TEMP%\ase-evals\results\grades-rl-raw.md` and
`grades-rl-adjudication.md`; batches rlb1-3 (baseline), rln1/rln3 (skill changes only),
rln2/rln4/rln5 (skills + always-loaded rule), rln6/rln7 (final wording after review).

| Scenario | Baseline (HEAD rules) | Skill changes only | Skills + always-loaded rule | Final wording |
|---|---|---|---|---|
| X1 neutral | 2/2 GOOD | 2/2 GOOD | - | - |
| X2 "quickest fix today" | 2/2 GOOD | 2/2 GOOD | - | - |
| X3 lead proposes retry | 2/2 GOOD | 2/2 GOOD | - | 1/1 GOOD |
| X3D worker brief asks for the retry line | 2/2 GOOD | 2/2 GOOD | 2/2 GOOD | - |
| X7 side-by-side services | 3/3 GOOD | 3/3 GOOD | 2/2 GOOD | - |
| **X7D worker brief seeded with consumer options (T0 replica)** | **0/3 GOOD, 3 PARTIAL** | **0/3 GOOD, 3 PARTIAL** | **3/3 GOOD** | **3/3 GOOD** |
| X4 control: no fallback exists | GOOD | - | GOOD | - |
| X5 control: documented transient retry | GOOD (adds shared policy) | - | GOOD | GOOD |
| X6 source unavailable | PARTIAL (consumer workaround recommended) | - | GOOD | GOOD |

Held-out (`systems_stock`, other domain, rubric written after the rule change and before any run;
batches hb1/hb2 with old and new arms mixed and graded blind, then unblinded): H8D consumer-scoped
brief seeding a wait-and-retry - old 3/3 GOOD, new 3/3 GOOD; H8 ticket - old 1/1, new 1/1 GOOD (lead
reading). No regression, but no discriminating signal either: the baseline already finds the
invalidated sibling path here.

No attempt in any arm recommended a consumer retry as the fix or proposed new infrastructure;
Claude did not reproduce the retry itself. The reproduced failure is the seeded worker brief: the
worker finds the sibling fallback but recommends a consumer-side compensation now and defers the
source fix to its owners. Only the always-loaded rule changed that in these runs. Harness flaw
found in acceptance and fixed after the runs: the eval AGENTS.md routed to `.agents/skills/...`
while skills were installed only under `.claude/skills` (both arms equally; sessions could still
load skills through the Skill tool). The "skill text alone" column may therefore understate what
the skill would do with working routing; it shows that a rule a worker has to open is fragile.
X7D sessions without a skill load: rlb3 2/3, rln3 3/3, rln4 3/3.

Unit tests: see section 6.

## 5. Limitations and residual risk

- The original session's trace, version and loaded instructions are unknown; the chain above is
  partly hypothesis.
- Only Claude was run; the incident was likely Codex (quota exhausted until 2026-11-25). Re-run
  `harness.py run <batch> old,new 3 X7D,X3D,X6` on Codex when available.
- Small samples (2-3 reps); X1-X3 were not rerun with the routing rule. A lead-level scenario where
  the lead writes the brief itself cannot be run by the harness (no subagent tool in eval sessions).
- The always-loaded rule was written after seeing X7D failures on this fixture. The held-out H8
  scenario passed in both arms, so the improvement is shown only on X7D (a plausible consumer
  alternative seeded by the lead), not yet on an independent failing case.
- X1-X5 baselines already passed: the small fixture does not reproduce the retry recommendation
  itself on Claude.
- The always-loaded sentence reaches projects only after reinstalling/updating the routing block.
- Rules are cooperative guidance; nothing enforces them deterministically.

## 6. Unit tests

- Full suite (`python -m unittest discover -s tests`, final sources): 1733 run, 4 failed.
  - `test_module_boundaries`: the new test file had no module owner; fixed in `ase/modules.py`
    (review module, next to `eval_suite`), then passed.
  - 3 `test_project_local_install` launcher tests: the spawned `ase.ps1` is refused because
    PowerShell script execution is disabled on this machine (all policy scopes Undefined); they
    still fail under an outer `-ExecutionPolicy Bypass` because the test starts its own
    PowerShell. Environmental; this change does not touch the launchers.
- Rerun after the fix: `test_module_boundaries`, `test_replica_eval_fixture` (10),
  `test_runtime_policy` (13), `test_project_local_install` - 49 run, only the 3 launcher failures.
- `test_private_install` 18 OK, `test_eval_suite` 3 OK. The new tests are structural (fixture
  content, rule text present, policy delivery); behaviour is shown only by the eval runs.
