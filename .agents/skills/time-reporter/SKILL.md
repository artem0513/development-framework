---
name: time-reporter
description: "Use when measured activity or explicitly supplied human durations need local time entries, timesheet drafts, corrections or dated activity summaries."
---

# Time and dated activity

Use isolated local `ase` time entries. Separate agent execution from human work and billable time. Use measured intervals or explicit user-provided durations; retain confirmation status and never infer billable hours from model elapsed time. Corrections preserve prior attribution and require a reason.

Legacy `daily` and `weekly` summarize stored time-entry start dates with their existing semantics; timezone offsets are not silently rewritten into billing days. `correct-time` retains atomic before/after values and resets confirmation unless explicitly confirmed again.

Use `ase --project PROJECT worklog --from DATE --until DATE` for dated task events and `meeting-report` for results, decisions and blockers. The upper date is exclusive. Those views use occurred timestamps, retained corrections and explicit current/as-of blockers; they do not create human time entries. New projects default to `local`: convert event instants and report day bounds using the executing computer's date-specific OS rules, including DST; do not capture the current fixed offset. Respect explicit existing project settings, and use `--timezone local` when the owner requests current-PC time for a previously named-zone project. Missing rules for an explicitly named zone block instead of silently falling back. UTC event storage stays unchanged.

Save session closures with project-context-manager when stopping or finishing. Draft timesheets stay local; sending or writing to an external timesheet needs explicit destination authorization.
