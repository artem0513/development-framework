# Mission: Build a Production-Grade Autonomous Software Engineering System for Codex

You are acting as a **Principal AI Systems Architect, Senior Software Engineer, and Agent Orchestration Engineer**.

Your mission is to design, install, configure, implement, test, and document a reusable **Autonomous Software Engineering System** built around OpenAI Codex.

This is an actual implementation task, not a research-only assignment.

I want to automate approximately 90% of routine software development activities while maintaining strong engineering quality, independent verification, and human control over critical decisions.

**Critical requirement:** I have experienced situations where coding agents confidently reported that a task was finished even though the required functionality was missing, incomplete, or incorrect.

Your highest priority is preventing this failure mode.

Do not treat an agent's statement that something is complete as proof of completion.

---

# 1. Research Existing Solutions First

Before implementing anything, research the latest official documentation and existing open-source solutions.

Investigate:

- OpenAI Codex CLI and its current agent orchestration capabilities.
- Codex subagents and parallel execution.
- Codex global and project-level skills.
- Codex plugins, hooks, and available automation mechanisms.
- https://github.com/obra/superpowers
- https://github.com/github/spec-kit
- https://github.com/microsoft/skills
- https://github.com/MicrosoftDocs/Agent-Skills
- https://github.com/mastra-ai/skills
- https://github.com/openai/plugins

Investigate additional mature, well-maintained solutions if relevant.

For each candidate, evaluate:

1. Compatibility with the currently installed Codex version.
2. Maintenance activity and documentation quality.
3. What it provides out of the box.
4. Whether it supports automated or independent verification.
5. Compatibility with Windows and PowerShell.
6. Overlap with other components.
7. Integration and security risks.

Prefer existing, proven solutions over custom implementations.

Do not blindly install every discovered plugin or skill.

Avoid duplicating capabilities that Codex or Superpowers already provides.

If a repository or installation instruction has changed, use the current official mechanism.

Produce a concise architecture decision record explaining which components you selected and why.

Then proceed with implementation.

# 2. Target Environment

Primary environment:

- Windows
- PowerShell
- OpenAI Codex CLI
- Git
- .NET / C#
- ASP.NET Core
- Angular / TypeScript
- Azure
- Azure Functions
- Azure Service Bus
- Azure Cosmos DB
- Entity Framework Core
- SQL Server / PostgreSQL
- Docker
- Mastra
- Microsoft Agent Framework

The system must also work with repositories using other technologies.

Prioritize a cross-platform design, but ensure Windows support is first-class.

Global components should be installed and configured in the appropriate user-level directories.

Project-specific configuration must remain inside the relevant repository.

Do not overwrite existing user configuration without backing it up and describing the change.

# 3. Desired Autonomous Workflow

When I give Codex a development task, it should execute the following workflow.

## Phase 1: Task Understanding

The system must:

1. Read the user request.
2. Inspect the repository.
3. Discover existing architecture, coding conventions, and tests.
4. Identify the relevant business logic.
5. Read existing project context and previous decisions.
6. Identify ambiguities, dependencies, and risks.
7. Ask clarifying questions when missing information could materially affect correctness.
8. Avoid unnecessary questions when the answer is already available from the repository or project context.

Before implementation, produce:

- Task summary.
- Functional requirements.
- Non-functional requirements.
- Acceptance Criteria.
- Assumptions.
- Risks.
- Implementation boundaries.

Do not invent business requirements and present them as confirmed facts.

Use explicit statuses for unresolved assumptions.

For straightforward tasks, keep this phase lightweight.

## Phase 2: Planning

Create an implementation plan.

Identify:

- Relevant files and modules.
- Required changes.
- Dependencies between tasks.
- Testing strategy.
- Potential regressions.
- Opportunities for parallel execution.

Estimate whether parallel agents would improve efficiency.

Parallelism must be based on task independence, not simply the number of available agents.

Do not create unnecessary agents for trivial changes.

Prefer sequential execution when parallelism introduces excessive coordination overhead.

## Phase 3: Implementation

Use specialized implementation agents when appropriate.

Potential agents:

- Backend Developer
- Frontend Developer
- Test Engineer
- Database Engineer
- Azure Infrastructure Engineer

These are logical roles, not necessarily permanently running agents.

When multiple agents operate in parallel:

- Give them clearly scoped tasks.
- Prefer isolated Git worktrees or equivalent workspace isolation.
- Avoid concurrent modifications to the same files.
- Identify task dependencies.
- Merge changes in a controlled order.
- Resolve conflicts explicitly.
- Never allow parallel agents to silently overwrite each other's changes.

Agents should follow repository conventions and avoid unrelated refactoring.

Preserve existing behavior unless explicitly required to change it.

Use existing project dependencies before introducing new ones.

## Phase 4: Independent Acceptance Criteria Verification

This is a mandatory phase.

Create a dedicated **AC Verifier**.

The AC Verifier must operate independently of the implementation agent.

It must receive:

- Original task description.
- Confirmed Acceptance Criteria.
- Relevant repository state.
- Final code diff.
- Test artifacts.

It must NOT rely on the implementation agent's self-assessment.

For each Acceptance Criterion, produce:

- Criterion identifier.
- Expected behavior.
- Implementation evidence.
- Verification method.
- Verification result.
- Confidence limitations.
- Missing coverage, if any.

Possible statuses:

- PASS
- FAIL
- UNVERIFIED
- BLOCKED
- NOT_APPLICABLE

PASS requires meaningful evidence.

A code change that merely appears related to a requirement is insufficient proof.

A test that passes but does not validate the actual requirement is insufficient proof.

If an Acceptance Criterion cannot be verified automatically, mark it UNVERIFIED or BLOCKED and describe the missing evidence.

Never silently convert uncertainty into success.

## Phase 5: Independent Code Review

Create an independent **Code Reviewer** agent.

The reviewer must inspect the actual implementation rather than simply accepting the developer's summary.

Review for:

- Functional correctness.
- Missing requirements.
- Incorrect assumptions.
- Edge cases.
- Null handling.
- Error handling.
- Async/concurrency problems.
- Race conditions.
- Security vulnerabilities.
- Data integrity.
- Performance regressions.
- Database consistency.
- Transaction boundaries.
- Backward compatibility.
- Breaking API changes.
- Unnecessary complexity.
- Missing tests.
- Unrelated modifications.
- Maintainability.

For .NET projects, additionally consider:

- Dependency Injection lifetimes.
- async/await correctness.
- CancellationToken propagation.
- EF Core query behavior.
- Thread safety.
- Resource disposal.
- Serialization compatibility.
- Azure service failure handling.
- Idempotency for event-driven systems.

Report findings with severity, file path, and relevant line references.

Blocking findings must be resolved before the pipeline succeeds.

After fixes, rerun affected verification stages.

Avoid infinite review/fix loops. Use configurable retry limits and escalate unresolved issues.

## Phase 6: Automated Testing

Detect the project's technology stack and available test commands.

Run all applicable checks:

1. Build.
2. Formatting/linting.
3. Static analysis, when configured.
4. Unit tests.
5. Integration tests, when infrastructure is available.
6. Regression tests.
7. Relevant security checks.
8. Smoke tests when appropriate.

For .NET, support commands such as:

- dotnet restore
- dotnet build
- dotnet test

For Angular and TypeScript, detect project-specific npm scripts.

Never assume commands without inspecting the project.

Important:

**Tests must actually execute.**

Do not report PASS based on:

- A command that was never executed.
- A command that failed to start.
- A missing SDK.
- An unavailable database.
- A skipped test suite.
- A fabricated tool result.
- A previous unrelated test run.

Record:

- Command.
- Working directory.
- Exit code.
- Execution result.
- Test counts.
- Relevant logs.
- Timestamp.
- Git commit SHA or working tree fingerprint.

Where possible, independently generated tests should derive from the Acceptance Criteria rather than the implementation.

## Phase 7: Deterministic Quality Gate

Implement an external, deterministic verification mechanism.

The quality gate must not depend solely on the coding agent's verbal conclusion.

The gate must validate machine-readable evidence and required tool exit codes.

Required conditions:

- All mandatory Acceptance Criteria verified.
- No unresolved blocking review findings.
- Required builds successful.
- Required tests successful.
- No unresolved critical errors.
- Verification artifacts correspond to the actual current code revision.
- No unauthorized changes to protected files.

An agent must not be able to mark its own work as verified merely by writing PASS into a report.

Separate generation of evidence from validation of evidence.

The verifier should validate source artifacts, execution results, and their association with the tested code revision.

Prevent stale evidence reuse.

Result statuses:

- APPROVED
- REJECTED
- BLOCKED

Only APPROVED permits the configured automated Git publishing step.

Do not give the implementation agent permission to bypass this mechanism.

For enforced protection, integrate with Git hooks and CI branch protection where appropriate. Do not claim that prompts alone provide enforceable security.

## Phase 8: Final Report

Generate a structured report containing:

- Original task.
- Summary of completed changes.
- Files changed.
- Acceptance Criteria results.
- Independent review findings.
- Test results.
- Build result.
- Identified risks.
- Unverified behavior.
- Git status.
- Commit SHA when available.
- PR link when available.
- Final quality gate status.

Clearly differentiate:

- Implemented.
- Tested.
- Verified.
- Not verified.
- Blocked.

Never say everything is complete when mandatory verification has failed.

## Phase 9: Git Automation

When all required gates pass:

1. Verify branch and repository status.
2. Review intended staged changes.
3. Exclude secrets and unrelated files.
4. Create a meaningful commit.
5. Push to the configured feature branch.
6. Optionally create a Pull Request.
7. Include the verification summary in the PR.

Never push directly to main, master, or another protected branch.

Never force-push unless explicitly authorized.

Never merge into a protected branch automatically.

Require explicit human authorization before enabling automatic remote pushes for a repository. Support dry-run mode by default.

If authentication or permissions are unavailable, report the blocker instead of attempting unsafe workarounds.

# 4. Persistent Memory Using SQLite

Implement lightweight persistent project memory.

Preferred technology:

- SQLite.
- A small .NET CLI or similarly maintainable local application.
- CLI or MCP integration with Codex.

The database should support:

Projects:
- Project ID.
- Repository path.
- Technology stack.
- Configuration.
- Relevant architecture notes.

Tasks:
- Task ID.
- User request.
- Status.
- Acceptance Criteria.
- Related commits.
- Completion evidence.

Sessions:
- Session ID.
- Project ID.
- Task ID.
- Start/end time.
- Session summary.
- Important decisions.
- Unresolved questions.

Decisions:
- Decision ID.
- Context.
- Decision.
- Rationale.
- Date.

VerificationRuns:
- Task ID.
- Revision/fingerprint.
- AC results.
- Build/test results.
- Review findings.
- Quality gate outcome.

TimeEntries:
- Project.
- Task.
- Activity.
- Start/end.
- Duration.
- Source.
- Confirmation status.

Reports:
- Report type.
- Project.
- Date.
- Generated content.

Do not store the entire raw conversation indefinitely by default.

Prefer concise, searchable summaries and structured records.

Implement database migrations and basic backups.

Use project isolation so information from different clients cannot leak into one another.

Avoid storing secrets, credentials, sensitive customer data, or proprietary source code unnecessarily.

# 5. Custom Global Skills

Create reusable global skills using the currently supported Codex skill format.

The following skills are required.

## project-context-manager

Responsibilities:

- Detect current project.
- Load relevant memory.
- Retrieve previous implementation decisions.
- Summarize recent project history.
- Save significant new context.
- Preserve unresolved questions.
- Avoid cross-project contamination.

## task-orchestrator

Responsibilities:

- Start the development workflow.
- Determine when clarifying questions are necessary.
- Create an implementation plan.
- Decide whether parallel execution is beneficial.
- Delegate tasks to agents.
- Track task dependencies.
- Control workflow transitions.

## independent-ac-verifier

Responsibilities:

- Verify business requirements.
- Independently evaluate acceptance evidence.
- Identify missing functionality.
- Refuse unsupported completion claims.

## independent-code-reviewer

Responsibilities:

- Review implementation independently.
- Find correctness defects and regressions.
- Produce actionable findings.
- Trigger fixes for blocking defects.

## quality-gate

Responsibilities:

- Execute deterministic validation.
- Inspect real test/build artifacts.
- Prevent publishing unverified changes.
- Generate machine-readable results.

## time-reporter

Responsibilities:

- Track task-related activity.
- Distinguish agent execution time from human working time.
- Generate daily and weekly summaries.
- Provide draft timesheets.
- Allow corrections and human confirmation.

Never fabricate billable hours or classify agent runtime as human work without explicit user-defined rules.

## communication-assistant

Responsibilities:

- Generate professional project status updates.
- Prepare technical explanations.
- Draft Slack/Teams messages.
- Summarize implementation progress.
- Prepare PR descriptions.
- Prepare stakeholder reports.

Do not send messages to external services without explicit authorization.

## project-bootstrap

Responsibilities:

- Detect repository technology.
- Discover build/test conventions.
- Identify coding standards.
- Configure project-specific verification.
- Create or update project-level agent instructions.
- Avoid overwriting existing configuration.

# 6. Integration with Framework-Specific Skills

Investigate and integrate relevant official skills for:

- Mastra.
- Microsoft Agent Framework.
- Azure OpenAI.
- Azure AI Search.
- Azure Functions.
- Azure Service Bus.
- .NET development.
- Angular development.

Prefer official vendor-maintained resources.

Do not install irrelevant skills.

Ensure specialized skills are invoked when the relevant technology is detected.

Skills are reusable instructions and workflows, not a substitute for executable verification.

# 7. Reliability and Security Requirements

The system must:

- Be restartable.
- Preserve task progress.
- Recover from interrupted executions where feasible.
- Avoid executing duplicate destructive actions.
- Log important workflow transitions.
- Detect stale verification evidence.
- Avoid infinite retry loops.
- Use atomic SQLite transactions where needed.
- Keep credentials out of logs and databases.
- Require approval for destructive operations.
- Respect project and repository boundaries.
- Never run arbitrary repository instructions as privileged commands without evaluating their trust.
- Never silently modify unrelated repositories.
- Never push unverified code.
- Never claim success without evidence.

Use a configurable maximum number of automatic remediation attempts.

Provide a safe fallback when independent subagents, CI, or other features are unavailable.

Do not pretend that a fallback is equivalent to fully independent verification.

# 8. Efficiency and Token Optimization

I want a high level of automation without excessive token consumption.

Implement:

- Conditional parallelism.
- Lightweight workflows for trivial tasks.
- Full verification for substantial changes.
- Context retrieval on demand.
- Concise agent handoffs.
- Reuse of verified project knowledge.
- Avoidance of redundant repository scans.
- Configurable task risk levels.

Suggested risk levels:

LOW:
- Small changes.
- Limited verification.

MEDIUM:
- Feature development.
- Independent review and relevant tests.

HIGH:
- Business-critical functionality.
- Authentication.
- Finance.
- Data migrations.
- Security.
- Distributed systems.

HIGH-risk tasks should trigger stricter quality gates, broader regression checks, and human approval when appropriate.

Efficiency must never override mandatory safety checks.

# 9. Implementation Deliverables

Create the actual system, including:

1. Installation/configuration of selected existing skills.
2. Global Codex configuration.
3. Specialized agent definitions.
4. Custom skill files.
5. SQLite memory application.
6. Database schema and migrations.
7. CLI/MCP integration where justified.
8. Workflow orchestration logic.
9. Independent verification logic.
10. Deterministic quality gate scripts.
11. Git publishing integration.
12. Project bootstrap workflow.
13. Configuration templates.
14. Documentation.
15. Automated tests for custom components.
16. End-to-end demonstration.

Use a clean, maintainable directory structure.

Do not build a huge custom framework if existing Codex functionality is sufficient.

Keep custom code modular and independently testable.

# 10. End-to-End Validation

After implementation, execute a controlled test.

Create a small disposable example project outside existing client repositories.

Provide a sample development task with multiple Acceptance Criteria.

Run the complete workflow:

- Analyze request.
- Plan.
- Implement.
- Review.
- Verify AC.
- Run tests.
- Generate report.
- Evaluate quality gate.

Then deliberately introduce a defect or unmet Acceptance Criterion.

Confirm that:

1. The independent verifier identifies the issue.
2. The quality gate rejects the result.
3. The workflow does not publish the defective implementation.
4. The failure is reported accurately.
5. Remediation is possible.

Also test:

- SQLite persistence across sessions.
- Project isolation.
- Recovery from interruption.
- Missing test infrastructure.
- Failed build.
- Stale verification artifacts.
- Parallel task execution, if supported.

Use a disposable local Git remote to test publishing. Do not push test changes to my real repositories.

Do not mark the system validated if these scenarios were not actually executed.

# 11. Documentation and User Experience

Provide simple commands to:

- Initialize a repository.
- Start a development task.
- Resume a previous task.
- Inspect task status.
- Run independent verification.
- Generate a daily work report.
- Review stored project context.
- Configure push permissions.

I should be able to open an existing repository and say:

"Implement this task. Ask questions if necessary, use parallel agents when beneficial, independently verify all acceptance criteria, review the code, run tests, prepare a report, and push to a feature branch after successful verification."

The system should automatically select the appropriate workflow.

I should not have to manually invoke every skill or agent.

# 12. Execution Rules

This request is for a working implementation.

Do not stop after producing an architecture diagram or proposal.

Proceed in this order:

1. Inspect the environment and current Codex installation.
2. Research current capabilities and official installation methods.
3. Select reusable existing components.
4. Produce a concise architecture and implementation plan.
5. Implement the system incrementally.
6. Run tests after each substantial component.
7. Integrate components.
8. Execute end-to-end validation.
9. Fix identified defects.
10. Deliver a final verified report.

Ask me questions only when a decision is genuinely blocking, such as authorization for a security-sensitive operation.

You may make reasonable technical decisions independently.

Never invent tool results or claim to have installed or tested components that were not actually installed or tested.

If you encounter environment limitations, explain precisely what is blocked, complete the remaining feasible work, and provide executable instructions for unresolved steps.

**Final success criteria:**

I can provide Codex with a normal software development task, and it automatically coordinates implementation, independently verifies the result against Acceptance Criteria, reviews the code, executes real tests, maintains persistent project memory, generates a truthful report, and safely publishes verified code when authorized.

The key success condition is not simply writing code faster.

It is making false completion claims significantly less likely through independent checks, observable evidence, and enforceable workflow gates.

Begin now.