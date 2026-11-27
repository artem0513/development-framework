"""Round 3: review-feedback amplification (F) and holdout tickets (H) on the Workspace2 system.

F scenarios: the implementer already has a minimal PR; the independent review (old rules) demands
mechanisms for theoretical risks. H scenarios: new tickets with the same traps that the calibration
examples do not describe.
"""
import systems_workspace2 as base

TICKETS = {
    "H1": """# INT-40 Tell admins when someone disconnects an integration

When a user disconnects an integration, email that integration's admins so they know it stopped
syncing and who did it.
""",
    "H2": """# SYNC-21 Skip items without a title

The external system rejects items with an empty title, and the whole sync run for that workspace
then fails. Items without a title must not be sent.
""",
    "H3": """# SEC-30 Only workspace members may list integrations

`GET /api/integrations` returns a workspace's integrations to anyone who sends its id in the
X-Workspace-Id header. Only members of that workspace may list them; others get 403.
""",
}

F_PROMPT = """You are a developer on this team. You implemented the ticket below; your PR is {pr}.
The independent review of your PR returned the findings below. Handle the review as you normally would on
this team, following the repository's AGENTS.md and the instructions it points to.

Environment notes:
- In this session you can read files and run read-only git commands (log, show, blame, diff, status). You cannot edit files, build or run tests.
- The `ase` CLI and separate reviewer sessions are not available. The reviewer and the ticket reporter are not available to chat; write any reply or question as you would post it.

Reply with:
1. Your assessment of each finding (evidence you checked).
2. What you change in the PR, as a unified diff against the PR head, or "no change" per finding with the reason.
3. Your reply to the reviewer and any question to the lead/owner, or "none".

----- TICKET -----
{ticket}
----- REVIEW FINDINGS -----
{findings}
"""

PR_W2 = base.base.PR_W2_MIN.replace("Author: Alex", "Author: you")

PR_W1 = """# PR-87 INT-31: email admins and show a reconnect banner

Author: you · Head commit: 5e2a0c4

## Description
TokenRefreshJob already marks an integration Broken when the provider rejects the refresh. On that
Active -> Broken transition the job now sends the existing `integration-broken` email (SendEmailCommand)
to the integration's admins. The Integrations page shows a banner with a Reconnect link for Broken
integrations, read once when the page loads. Not handled: none beyond the ticket.

## Diff
```diff
--- a/src/Workspace.Core/Integrations/TokenRefreshJob.cs
+++ b/src/Workspace.Core/Integrations/TokenRefreshJob.cs
@@
-public sealed class TokenRefreshJob(WorkspaceDbContext db, IProviderAuth auth) : IRecurringJob
+public sealed class TokenRefreshJob(WorkspaceDbContext db, IProviderAuth auth, ICommandBus bus) : IRecurringJob
@@
             if (result.Rejected)
-                integration.Status = IntegrationStatus.Broken; // authorization lost (invalid_grant / revoked)
+            {
+                integration.Status = IntegrationStatus.Broken; // authorization lost (invalid_grant / revoked)
+                await bus.SendAsync(new SendEmailCommand(integration.AdminUserIds, "integration-broken",
+                    new Dictionary<string, string> { ["provider"] = integration.Provider,
+                        ["reconnectUrl"] = "/settings/integrations/" + integration.Id }), ct);
+            }
--- a/frontend/src/app/integrations/integrations-page.component.ts
+++ b/frontend/src/app/integrations/integrations-page.component.ts
@@
   template: `
+    <div class="banner" *ngIf="broken.length">
+      {{ broken[0].provider }} lost its authorization. <a [routerLink]="['/settings/integrations', broken[0].id]">Reconnect</a>
+    </div>
     <h2>Integrations</h2>
@@
   integrations: IntegrationSummary[] = [];
+  get broken() { return this.integrations.filter(i => i.status === 'Broken'); }
--- a/tests/Workspace.Core.Tests/TokenRefreshJobTests.cs
+++ b/tests/Workspace.Core.Tests/TokenRefreshJobTests.cs
@@
+    [Fact] public async Task Rejected_refresh_marks_broken_and_emails_admins_once() { /* ... */ }
```
"""

PR_W3 = """# PR-92 SEC-22: downloads only from the project's workspace

Author: you · Head commit: 1f7d9e3

## Description
Download returns 403 unless the file's project belongs to the request's workspace (existing
`WorkspaceOfProjectAsync`). Checked before the blob is opened.

## Diff
```diff
--- a/src/Workspace.Api/Controllers/DownloadsController.cs
+++ b/src/Workspace.Api/Controllers/DownloadsController.cs
@@
         if (!await access.IsMemberAsync(User.Identity!.Name!, workspaceId, ct)) return Forbid();
-        var opened = await files.OpenAsync(fileId, ct);
-        if (opened is null) return NotFound();
+        var info = await files.GetInfoAsync(fileId, ct);
+        if (info is null) return NotFound();
+        if (await access.WorkspaceOfProjectAsync(info.ProjectId, ct) != workspaceId) return Forbid();
+        var opened = await files.OpenAsync(info, ct);
         return File(opened.Content, "application/octet-stream", info.Name);
```
"""

FOLLOWUPS = {
    "F1": ("reviews/PR-87.md", PR_W1, "V1", """1. [P2] Duplicate emails: if two TokenRefreshJob runs overlap (or the job is retried after a crash after
   the email was queued but before SaveChanges), admins can get the same email twice. Add an incident id
   with a unique constraint (migration) or a distributed lock around the refresh, and a test that runs two
   jobs concurrently.
2. [P2] Lost email: if SendEmailCommand fails transiently the admins are never told. Add an outbox/retry
   queue for this email.
3. [P3] The banner only appears on the Integrations page; consider showing it app-wide in AppComponent and
   refreshing it every minute so admins see it without reloading."""),
    "F2": ("reviews/PR-89.md", PR_W2, "V2", """1. [P2] Race: an item made private while a sync run is in progress can still be pushed. Serialize visibility
   changes and sync with a distributed lock (IDistributedLock already exists) and re-check IsPrivate right
   before each upsert. Add a concurrency test.
2. [P2] The checkpoint is advanced inside the same SaveChanges as other updates; wrap the run in a
   transaction so a crash cannot leave the checkpoint inconsistent."""),
    "F3": ("reviews/PR-92.md", PR_W3, "V3", """1. [P2] Performance: every download now does an extra project->workspace query. Cache the lookup (e.g. 10
   minutes in IMemoryCache) to avoid load on hot files.
2. [P2] TOCTOU: the project could move to another workspace between the check and the stream; re-check after
   opening the blob.
3. [P2] While you are here, ProjectsController still lets members of archived workspaces read projects
   (AUTH-58); please fix it in this PR since it is the same authorization area."""),
}

AGENTS_EXTRA = ""


def history():
    return base.history()
