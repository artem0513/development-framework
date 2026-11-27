# Everything in the project folder

Keep the framework checkout anywhere available, or place its files in a folder alongside your application sources. From the framework checkout initialize the application's directory once:

```powershell
python scripts/install.py --project D:/Work/MyProject --project-local --target codex --target claude --target copilot
```

Python3.11+ and the selected bot are required. Remove unwanted targets; descriptors do not install a CLI or provide a subscription. Initialization is scoped to that directory. No global skills, provider permissions or account credentials are installed/migrated. For a read-only review first, add `--preview`, save the JSON (UTF-8 or BOM-marked UTF-16, so PowerShell 5.1 `>` redirection works), then apply with `python scripts/install.py --apply-manifest MANIFEST.json`. Existing instructions/settings/comments remain; changed owned files have private backups. Known conflicts and stale previews fail before writes. This is local reliability, not an IO transaction or isolation from another hostile process with the same OS identity.

## Layout

```text
MyProject/
  application sources...
  .devframework/
    config.toml                  project settings, no secrets
    .gitignore                   excludes /local/
    bin/ase.ps1                  PowerShell launcher
    bin/ase                      shell launcher
    autonomous-se/runtime/       self-contained framework runtime
    local/                       private, excluded from Git
      registry.sqlite3
      projects/<project-id>/     SQLite memory, journal, checks, receipts
      credentials/               encrypted vault, only if explicitly used
      backups/                   installation backups
  .agents/skills, .codex/agents   Codex-specific project files
  .claude/skills, .claude/agents  Claude-specific project files
  .github/skills, .github/agents  Copilot-specific project files
```

Native instruction files select project skills and the same local launcher. Nonsecret config/runtime/skills can be versioned; `.devframework/local` must remain ignored. Installer/runtime reject tracked private state, missing/ineffective ignore protection, linked managed paths and using one project's local home for another project. Generic discovery, source packets and fingerprints cannot treat private state as application source.

## Everyday commands

```powershell
.\.devframework\bin\ase.ps1 status
.\.devframework\bin\ase.ps1 project register --root . --kind software
.\.devframework\bin\ase.ps1 worklog --from 2026-11-12 --until 2026-11-13
.\.devframework\bin\ase.ps1 meeting-report --from 2026-11-12 --until 2026-11-13
```

Use the shell launcher analogously. Dates are examples; `--until` is exclusive. Journal entries reflect real task/check/session records; installation does not invent history. New installations use `timezone="local"`: reporting follows the OS local timezone on the computer running ase, including the rules for each event/date and daylight-saving transitions. UTC event timestamps remain unchanged. New registrations default to the configured timezone. Existing registered metadata/history remains unchanged; report commands can override reporting timezone.

Subdirectories find the nearest local configuration. An absolute local launcher path selects its own project even from another directory. Explicit `--repo`, `--project PATH` and `--home` remain available. `--project UUID` is a key within the selected registry, never a reason to fall back silently to global state. State selection is explicit `--home` > selected local config > `ASE_HOME` > platform default. Each project has independent registry/UUIDs, memory, journal and vault; its three bots share that state. Existing shared memory is not copied automatically.

The strict config has `schema_version=1`, `mode="project-local"`, unique selected `targets`, `timezone` (`local` by default, or an explicit IANA name), and `log_retention_days` from1 to365. The private path is fixed. Local retention overrides the global retention environment default. Native provider and third-party session caches follow their own tools' policies. Optional credential import still requires a designated file outside source/Git; the protected local vault remains encrypted with Windows DPAPI, with unsupported OSes blocked rather than writing plaintext.

### Approval for new direct .NET packages

Set `guard_dotnet_package_additions = true` in the workspace's `.devframework/config.toml`
to pilot this check. The software gate compares `PackageReference` and
`GlobalPackageReference` `Include` entries in changed `.csproj`, `.fsproj`, `.vbproj`,
`.props` and `.targets` files (including `Directory.Build.props` and
`Directory.Packages.props`), and `package id` entries in `packages.config`, with the
recorded task baseline. A literal ID is compared case-insensitively; an evaluated
Include such as `$(ExtraPackage)` is identified by its exact text. Edits to an
existing package's version, `Update` items and ordinary source edits preserve the
current approval. Central version values remain part of independent review.

For an approved new dependency, add `"dotnet_package": {"path": "src/Billing.csproj", "package": "New.Lib"}`
to its existing proposal effect. Include the affected stage, requirement or decision,
and why the current dependencies are insufficient. Record the owner's reply to that
plan with `ase task approve`. Exact manifest and package identity survive subsequent
repairs under the same approval. Another package or manifest needs its own decision.
If the baseline manifest cannot be read and matched to its recorded hash, the gate
reports the missing comparison. A committed baseline supports this comparison.
When a task records an enabled baseline, the check stays enabled for that task.

### Approval for EF Core and ARM additions

For software tasks, enable `guard_efcore_schema_additions = true` or
`guard_arm_resource_additions = true` in the same workspace config. The former
recognizes `CreateTable` and `AddColumn` calls, with named or positional arguments
and also inside `if` or `foreach` blocks, in any `void Up(MigrationBuilder ...)`
method, block- or expression-bodied. Files under a `Migrations` directory are always
analyzed; another `.cs` file is analyzed when it declares such an `Up` method. The
latter recognizes resource declarations in resource group, subscription, management
group and tenant deployment templates: `azuredeploy.json` and `*.arm.json` are always
analyzed, another `.json` file when it has a deployment template `$schema` member. It
covers relative and full-type nested declarations, explicitly scoped resources and
inline nested deployments; a resource marked `"existing": true` is a reference, not
an addition, but its nested children are. The scope is part of the identity: the
enclosing inline deployments, outermost first (`deployment=logs;resourceGroup=other`),
then an explicit `scope`, joined by ` | `. The same name at another scope or in
another deployment is a separate addition, and so is a repeated declaration of an
identity the file already has (for example a second migration loop).

A string literal identifies its value. A declaration the guard cannot evaluate, such
as an ARM expression name `[parameters('storageName')]` or a table name
`TableNames.Archive`, is still an addition, identified by its exact source text
(whitespace collapsed for C#). The owner approves that text like any other target.

Record each exact target on an approved proposal effect with the existing owner
decision source: `kind: "schema"` (or `"infrastructure"`) for `efcore_schema`, and
`kind: "infrastructure"` for `arm_resource`. A table or column addition needs the
owner's explicit source or resolved decision whatever its kind. Use `efcore_schema`
with `path`, `table`, and the `schema` and `column` when present. A table addition
has no `column` field. Use `arm_resource` with `path`, `type`, `name` and, when the
gate reports one, `scope` exactly as reported. For example,
a nested queue has the full type `Microsoft.ServiceBus/namespaces/queues` and name
`billing/settlement`. Paths are workspace-relative. The owner's explicit reply covers
that target in the named stage and remains valid through repairs under the same
proposal. After a proposal revision, a stage that already completed keeps its
approved targets in the diff while another stage runs.

For all three enabled guards, `ase task run` and `ase task resume` compare the
candidate edits with the current files before writing any file in the batch or
launching stage actions. If an addition is needed, explain why and get the exact
target approved on the proposal. Use `ase task resume` to try an alternative
within the current approval. Ordinary repairs and already approved targets
continue under that approval. The final gate also checks the current local diff
before approval or publication.

Raw SQL, column declarations inside `CreateTable`, operations issued through helper
methods, linked templates, MSBuild SDK references, Bicep and Terraform require review
of their actual source. A template or migration over the 512 KB packet bound, or a
template that is not strict JSON or has a nested deployment whose `properties` or
inline `template` is an expression, is reported as a comparison gap.
Executing a migration or deployment requires separate authorization.

## Several repositories in one workspace

The root may be a Git repository that ignores a folder (for example `src/`) of independent clones, each with its own `.git` and remote. Declare them in the root `.meta` manifest (`{"projects": {"src/orders": "<url>", "src/billing": "<url>"}}`) and/or with the optional config key `repositories = ["src/orders"]`; there may be one or many. The single root installation then treats each declared clone as a task source, without installing the framework inside it, and `ase project register` from a clone reports the root project plus its `repository`.

A task changes exactly one repository: the root or one clone. Its scope must stay inside it; otherwise `task create` fails with "A task changes one repository". The clone is inferred from the scope, or named explicitly with `"repository": {"path": "src/orders", "base": "origin/master"}`. With a `base`, the task baseline is that ref, so work committed on the branch before approval is part of the change, and evidence packets carry `source_changes` against the base, `_ase_context/source_diff.patch` and `_ase_before/` base versions of changed files. The snapshot uses the clone's own Git (tracked and nonignored files only, `.git` private, link/junction and escape checks unchanged); a required dependency on another clone uses that clone's Git inventory. `ase task finish TASK --base origin/master`, its ci-check, clean-tree, secret and publication checks run in the clone; push authorization is per clone (`ase authorize --repository src/orders`). Approved evidence commands still start in the workspace root, so they name the clone path. A single-repository project behaves as before.

## Updates, clones and moves

Reapply the same local command from current framework sources to update. An installed runtime also contains its installer at `.devframework/autonomous-se/runtime/scripts/install.py`. Reinitialize after cloning to refresh machine-local absolute native skill selectors. Launchers find runtime relatively and use an available Python3.11+ interpreter.

Git clones exclude private history. For another computer use explicit reviewed export/import; secrets are excluded and DPAPI ciphertext remains tied to its user/machine. Moving a folder containing registered private state requires explicit reattachment/recovery and fresh affected evidence, not invented registry identity or replayed approvals. No MCP/daemon is needed. Independent sessions retain clean external OS temporary working directories; persistent captures/receipts remain inside the project's private state.

## Local time and existing settings

`local` is a portable selection, not a captured current UTC offset or a fixed region: another computer uses its own OS rules. Explicit existing configuration and registered timezone metadata are preserved. For an existing named-zone registration, add `--timezone local` to a report to select the current computer deliberately; the historical UTC events are not rewritten. An explicitly named IANA zone with unavailable rules still blocks instead of silently substituting local time. Native OS date-range/rule availability applies to local conversion.
