# Component research and selection

Research date: 2026-11-01. Sources were fetched live; installation and runtime validation are separate activities. Local CLI inspected: `codex-cli 0.166.0-alpha.5.1`. No installation was performed by this research pass.

## Codex baseline

Local `codex --help`, `codex exec --help`, `codex plugin --help`, `codex plugin add --help`, and `codex features list` establish that this executable exposes noninteractive execution, structured final outputs, JSONL events, resume, managed worktrees, plugin marketplaces, and code review. `multi_agent`, `hooks`, and `plugins` report stable/enabled. This establishes command availability, not successful model execution or account entitlement.

Official documentation now redirects old Codex URLs to ChatGPT Learn. [Subagents](https://learn.chatgpt.com/docs/agent-configuration/subagents) documents independent delegated contexts, inherited permissions, and standalone agent TOML files in `~/.codex/agents/` or `.codex/agents/`. Required fields are `name`, `description`, and `developer_instructions`. Agent instructions do not establish a separate security sandbox; child permissions inherit from the parent. Prefer independent read-heavy verification/review; isolate parallel writers.

[Skills](https://learn.chatgpt.com/docs/build-skills) currently documents `.agents/skills` for repository/user discovery and `SKILL.md` frontmatter. Skills provide instructions, not execution proofs. [Windows sandbox](https://learn.chatgpt.com/docs/windows/windows-sandbox) supports native PowerShell and documents MXC and legacy fallbacks. Installed SDKs and actual sandbox execution must still be checked locally.

[Plugin packaging](https://developers.openai.com/plugins/build/plugins) supports portable manifests and Codex compatibility manifests. Bundled hooks require execution-environment scripts and trust review; installing/enabling does not automatically trust hooks. Public directory restrictions differ from manual desktop installation. Consequently use executable Git/CI gates for publication control, with hooks as local assistance rather than an immutable boundary.

## Candidate matrix

Compatibility below means documented format/CLI compatibility unless actual execution is explicitly recorded elsewhere. Recent push dates establish activity only, not an audit or quality guarantee. All repositories below were unarchived at lookup.

| Candidate and source | Activity observed (UTC), documentation | Provides / verification | Codex and Windows | Overlap, risk, selection |
|---|---|---|---|---|
| [obra/superpowers](https://github.com/obra/superpowers) | Pushed 2026-10-29; extensive workflow docs | Planning, TDD, worktrees, delegated implementation, spec/code review, verification-before-completion; no tamper-resistant evidence service | Current README recommends official OpenAI marketplace for App/CLI; local CLI supports plugin add; upstream packaging/testing includes shell scripts | Strong overlap with general development skills. Reuse its development guidance; custom work should focus on persistent evidence and deterministic gate. Review installed hooks on Windows. Old `.codex/INSTALL.md` fetched as **404**; old clone/junction instructions are obsolete. |
| [github/spec-kit](https://github.com/github/spec-kit) and [Codex integration](https://github.com/github/spec-kit/blob/main/docs/integrations/codex.md) | Pushed 2026-10-30; detailed integration/workflow reference | Specification workflows, reusable steps, persisted workflow execution and gates | Current setup: `uv tool install specify-cli`, then `specify init <project> --integration codex`; Python 3.11+/uv, Windows supported. Codex skills target `.agents/skills`; workflow steps dispatch `codex exec` | Substantial overlap with task orchestration. Defer default install; permit opt-in for specification-heavy repositories. Its shell/gate steps still require trusted evidence policy; do not claim this is merely a prompt-template package. |
| [microsoft/skills](https://github.com/microsoft/skills) | Pushed 2026-10-31; catalog and SDK skill evaluations | Vendor SDK guidance, including .NET OpenAI/Search/Service Bus; upstream skill test harness | `npx skills add microsoft/skills`, select individual skills and Codex/global scope; Markdown portable, helper prerequisites vary | Install only relevant .NET skills, preserve references. Broad install adds irrelevant languages, duplicate creators and SDKs. Upstream skill tests are not tests of client application requirements. |
| [MicrosoftDocs/Agent-Skills](https://github.com/MicrosoftDocs/Agent-Skills) | Pushed 2026-11-01; catalog, bundles, Codex plugin instructions | Azure service operational guidance, including Functions/Service Bus/Search | Current README offers `codex plugin marketplace add MicrosoftDocs/agent-skills` and plugin browser; manual selected skill copies also documented; helper commands need local tool checks | Whole plugin is broad (193+ documented skills); prefer a selected Azure Functions skill with dependencies rather than all service skills. Overlaps Microsoft SDK guidance; operational vs SDK boundaries matter. |
| [mastra-ai/skills](https://github.com/mastra-ai/skills) | Pushed 2026-10-31; focused reference routing | `mastra` uses installed package docs first; `mastra-factory` supervises deployed Factory instances | Official install `npx skills add mastra-ai/skills`, select `mastra`, Codex and global scope. Text portable; Node/Mastra commands require project dependencies | Select `mastra`; omit Factory supervisor without a Factory deployment. No independent application acceptance gate supplied. Preserve reference files and package-version lookup. |
| [openai/plugins](https://github.com/openai/plugins) | Pushed 2026-10-30; manifests and curated examples | Native marketplace packages, including Superpowers and service-specific skill/MCP bundles | Local CLI supports `codex plugin add PLUGIN@MARKETPLACE`; marketplace names must be read from configuration/catalog, not guessed | Select as Superpowers distribution source. Existing connected plugins should be reused. Installing connectors introduces account permissions and external-service effects; omit unrelated integrations. |

## Additional official framework coverage

| Framework | Official source / current mechanism | Decision and limits |
|---|---|---|
| .NET / ASP.NET Core / EF Core | [dotnet/skills](https://github.com/dotnet/skills), pushed 2026-11-01; `codex plugin marketplace add dotnet/skills`, marketplace `dotnet-agent-skills` | Prefer selected `dotnet-test`, `dotnet-aspnetcore`, and `dotnet-data` skills/packages for detected stack. Upstream explicitly distinguishes Copilot `.agent.md` files from native Codex TOML agents; do not install those as Codex agents. Native marketplace documented for CLI 0.121+. SDK availability remains a local check. |
| Angular / TypeScript | [Angular official skills documentation](https://angular.dev/ai/agent-skills), [angular/skills](https://github.com/angular/skills), pushed 2026-10-31 | Official `angular-developer` and `angular-new-app`; install only developer initially (`npx skills add https://github.com/angular/skills`, select scope/agent). Guidance follows current Angular and must be reconciled with installed project version. CLI MCP is optional; avoid adding persistent servers for documentation alone. |
| Microsoft Agent Framework, C# | [Official builder skill](https://github.com/microsoft/agent-framework/tree/main/skills/agent-framework-builder-dotnet), repository pushed 2026-10-30 | Select this actual C# entry point and its references when `Microsoft.Agents.AI.*` is detected. The similarly named skill in microsoft/skills is Python-specific. Repository contributor build/sample skills should not be installed as general app-development skills. |
| Azure OpenAI / AI Search / Service Bus | [Microsoft .NET SDK skill directory](https://github.com/microsoft/skills/tree/main/.github/plugins/azure-sdk-dotnet/skills) | Relevant paths: `azure-ai-openai-dotnet`, `azure-search-documents-dotnet`, `azure-servicebus-dotnet`. Skills must not imply authorization to provision Azure resources or expose secrets. |
| Azure Functions | [MicrosoftDocs Functions skill](https://github.com/MicrosoftDocs/Agent-Skills/tree/main/skills/azure-functions) | Operational guidance complements .NET compilation/tests. Emulator/cloud execution remains BLOCKED or UNVERIFIED until real infrastructure is available. |

## Source revision snapshot

Recorded GitHub API `main` commit SHAs (for reproducible review, not automatic update permission):

```text
obra/superpowers             2ce8aa15d05bd57213e6f746f953aafb7ded7a8b
github/spec-kit              7370e592f3cd9c06e5b698983957e227c75b30e2
microsoft/skills            3f5b461b2314e01c400564051e92469114c5b68f
MicrosoftDocs/Agent-Skills   95544200a6621ee7481b253beb115ac0be701689
mastra-ai/skills             34180fa43c67fe38197f30d8d975fb73454156aa
openai/plugins              0722921d5542fc593105c27bd52630babd8b8c2a
angular/skills              2a2b801eb9bf3304424ddedfa1ace347e1fff6a6
microsoft/agent-framework   b7884ad88eb85c27b2a7bf40c0e15948e7ccc28d
dotnet/skills               95e7d0cb7994d47b5789baa68e13f7a2f348b956
```

Metadata source: each repository's first-party GitHub REST endpoints `/repos/{owner}/{repo}` and `/repos/{owner}/{repo}/commits/main`. Preserve installation provenance separately because marketplace content may differ from upstream main.

For example, the fetched official `openai/plugins/plugins/superpowers/.codex-plugin/plugin.json` declared **6.3.0**, while upstream Obra declared **7.0.1**. Both currently declared `hooks: {}`. Current upstream version is not the version installed from the official curated marketplace. The exact official marketplace is `.agents/plugins/marketplace.json` and Superpowers package is `plugins/superpowers`. If a full marketplace checkout encounters Windows path-length failures in unrelated plugins, the local CLI documents repeated `--sparse` options: `codex plugin marketplace add openai/plugins --sparse .agents/plugins --sparse plugins/superpowers --json`. This narrows the checkout without changing global Git long-path configuration; installation outcome must still be recorded.

An earlier owner report identified an obsolete cache/config entry at `~/.codex/plugins/cache/openai-curated/superpowers/82fd64bc/.codex-plugin/plugin.json`, version **5.0.7**. This was **not proof of an active installed plugin**: final native listing did not mark that installation active. The owner removed only that obsolete entry and installed through `codex plugin add superpowers@openai-curated-remote --json`.

Final independent verification executed `codex plugin list --json` and observed **superpowers@openai-curated-remote**, version **7.0.1**, **installed: true**, **enabled: true**. The actual manifest at `~/.codex/plugins/cache/openai-curated-remote/superpowers/7.0.1/.codex-plugin/plugin.json` also declares **7.0.1**. Its manifest has no hook declaration, and recursive inspection found no files matching `*hook*` under that package. No hook trust was granted by the reviewer. Selected installed-list evidence is saved at `outputs/review-superpowers-installed.json`. The fetched live Git marketplace **6.3.0** and obsolete cache **5.0.7** remain historical distribution snapshots, not the final installed version. No broad marketplace upgrade was performed.

## Architecture decision

Reuse Codex execution, subagents, custom agents, skill discovery, resume and worktrees. Reuse Superpowers for general development procedure and targeted vendor skills for framework details. Defer Spec Kit as optional because a second default lifecycle would duplicate coordination; its current workflow engine is capable and should be reconsidered if custom orchestration grows.

Build only the missing policy/evidence layer: isolated SQLite project memory, runner-owned execution records, revision fingerprints, independent AC/review handoffs, fail-closed deterministic gate, truthful reports, and opt-in feature-branch publishing. Separate implementation context from verifier/reviewer contexts and require explicit missing-evidence statuses.

Local shared-user agents can edit scripts, reports and configuration, and can invoke Git directly. Therefore local prompts, hashes, hooks and database records are useful integrity checks, **not adversarial security against the same OS identity**. Enforced publishing requires a trusted CI runner, protected policy/workflow ownership, protected branches and credentials unavailable to implementation agents. CI branch protection needs remote-admin configuration and cannot be truthfully declared enabled from a local template.

Execution order: inventory/backups; targeted installations; memory/runner/gate components and negative tests; workflow/skills/bootstrap integration; isolated end-to-end validation including defective code and a local bare remote. Record account/tool limitations rather than treating an unexecuted stage as PASS.
