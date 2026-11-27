# Explicit protected source change authorization

The default gate rejects changes to AGENTS.md, .agents, .codex, CI workflows and hooks. When a user explicitly requests such a change, the trusted local CLI can record a task-scoped exception:

```powershell
python -m ase --repo C:\Work\MyProject authorize-protected my-task --path AGENTS.md --path .agents/skills/my-skill/SKILL.md --reason "User explicitly requested these skills and project instructions"
```

Repeat --path for each exact changed file. Wildcards, directories, absolute/noncanonical paths and traversal are rejected. Record after implementation settles: the receipt binds the complete source fingerprint, contract hash, project and task. Any source edit invalidates it; run fresh checks and obtain new authorization only within existing user scope. A new receipt appends an audit file; it does not reset the baseline or erase older records. Gate output lists effective authorized protected paths. Other protected files still reject and all test, AC, review and publishing requirements remain intact. The task contract cannot grant itself this exception.

Receipt JSON lives outside source under the task protected-authorizations directory. Explicit CLI invocation attests the caller has user authorization; the tool does not authenticate a human or protect against a malicious process with the same OS identity. Trusted CI must separately control who can authorize protected changes. This command grants no trades, external sends, schedules or publishing permission.
