# Correction of error (COE)

Use after an agent outcome the owner had to correct: a wrong fix, needless infrastructure, a
missed requirement, a review that blocked a proportional change. Blameless: the question is
which rule, evidence or check was missing, not who erred.

## 1. What happened

- Task / PR / date:
- What the agent did (one or two sentences, with links to the diff or transcript):
- What should have happened:
- How it was detected (owner, reviewer, CI, production):
- Impact (time lost, wrong data, extra infrastructure, rework):

## 2. Why: five whys to a mechanism

Stop at something the framework can change: a skill rule, a packet input, a gate check, a
missing test or an eval scenario. "The model made a mistake" is not a root cause.

1.
2.
3.
4.
5.

## 3. Evidence that existed

Which file, history, doc or owner decision would have led to the right outcome? Did the agent
open it (check the tool log)? If not, why: not in the packet, rule in an unread reference file,
search not prompted?

## 4. Corrections

| Change | Where | Owner | Status |
|---|---|---|---|
| Skill rule (decisive rules go in SKILL.md) | | | |
| Deterministic check (gate or `ase ci-check`) | | | |
| Eval scenario reproducing the incident | `scripts/eval/` | | |
| Test for the runtime part | `tests/` | | |

## 5. Verification

- Eval scenario added with its rubric entry written before the run.
- Old arm reproduces the incident; new arm fixes it without regressions in other scenarios.
- Links to batch results and grades:
