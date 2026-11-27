# Decision evals for skill changes

Unit tests prove the runtime. They do not show whether a skill edit changes what an agent
decides. `scripts/eval/` measures that with blind A/B runs of real agent sessions.

## What it contains

- `systems_*.py`: synthetic repositories (microservices commerce, clinic, logistics, a
  workspace with PR-review and follow-up cases) with real Git history, ADRs and docs, so the
  deciding evidence is discoverable the way it would be in a real project.
- `rubric.md`: per-scenario GOOD/PARTIAL/BAD decisions, the evidence that decides each case and
  whether owner contact is REQUIRED. It is written before any run and not edited to fit results.
- `harness.py`: builds the systems and runs attempts; `unblind.py`: joins grades with arms.

Scenarios cover: fixing at the source instead of compensating downstream, avoiding new
infrastructure without need (and asking the owner when it is needed), reading history before
asking, proportionate refactoring, reviewer behaviour on proportional PRs, and follow-ups.

## Running

```powershell
python scripts/eval/harness.py build
python scripts/eval/harness.py run b1 old,new 3            # all scenarios, 3 reps per arm
python scripts/eval/harness.py run b2 old,new 3 S03,S04    # selected scenarios
```

`old` uses the skills at Git HEAD, `new` the working tree. Results go to
`$ASE_EVAL_HOME/results/<batch>` (default: the system temp folder), never into the repository.
Sessions are read-only (Read/Grep/Glob and read-only git) and run with project settings only.
Each attempt is a real paid session; size batches deliberately.

## Grading

Graders are separate sessions given `rubric.md` plus each attempt's `final.md` and `tools.txt`
only. They never see `<batch>-map.json`. Output one table row per attempt:
`| rid | scenario | DECISION | exploration | owner contact | note |`. Then:

```powershell
cd $env:ASE_EVAL_HOME/results
python <framework>/scripts/eval/unblind.py b1 grades-b1-*.md
```

## Reading results

- Compare arms per scenario; a change is an improvement only if the new arm wins on the
  targeted scenarios without new BAD results elsewhere.
- Three reps are a signal, not significance. Rerun close calls.
- Check `tools.txt` before trusting a decision: an agent that never opened the deciding file
  was lucky, not right. Rules that live only in reference files are rarely read; decisive
  rules belong in SKILL.md.
- Apply the latest applicable rubric addendum to new batches. Keep historical grades tied
  to the authority rules they used.

## Adding a scenario

Add the ticket and any history to a `systems_*.py` module, add its rubric entry first, then
run. Turn each real incident into a scenario through the [COE template](coe-template.md).

## Decision boundaries on Codex

`scripts/eval/decision_boundaries.py` prepares separate disposable targets and installs the
workspace policy. The scoring rubric is outside the prompts and is written after a live batch. The prompt
includes the complete captured scenario files. This measures planning from supplied evidence
even when the Windows read-only shell cannot inspect the project. It does not measure tool
discovery, code implementation or execution against infrastructure.

```powershell
python -B scripts/eval/decision_boundaries.py --output D:/Lab/boundary-run --run-live --parallelism 6
```

Omit `--run-live` to prepare packets only, or repeat `--case` to select a subset. Use a new
output directory each time. An independent grader compares each `final.json` with the scenario
source and `rubric.json`. Check the actual plan and owner question even when the decision label
matches. Treat session or input failures separately from decision scores.
