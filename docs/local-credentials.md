# Local credentials and approved access

In [project-local mode](project-local.md), use that project's local launcher without `--home` to select its own encrypted vault under ignored `.devframework/local/credentials`. Each project is separate; Codex/Claude/Copilot within it share profiles. The existing explicit external-home commands below still select independent shared state. Import files must still be outside workspaces/Git; only the validated protected local state vault is admitted inside a source root.

Codex, Claude, and Copilot use the same `ase` CLI and the same local state home. On Windows, explicitly imported profiles are encrypted with Windows DPAPI for the current user. This protects persisted bytes under the Windows account; it does not isolate secrets from other code running as that user. Other operating systems fail closed instead of writing a plaintext vault. The framework does not copy existing provider credentials.

Create a private JSON import file **outside the current workspace and any Git working tree**. Use `templates/credentials.example.json` as a shape reference; put actual values only in the private file. The import leaves that file unchanged, so handle it as sensitive afterward. Use a unique profile ID and the exact HTTPS origin, without userinfo, a path, query, or fragment. HTTP is allowed only for loopback services. `http-basic` requires `username` and `password`, `http-bearer` requires `token`, and `environment` supports child environment variables. The `env` object maps environment variable names to keys in `secrets`.

```powershell
ase --home C:\Private\ASE credentials import --file C:\Private\service-import.json
ase --home C:\Private\ASE credentials list
```

Omit `--home` to use `ASE_HOME` or the normal local state home. Import validates the whole batch before writing; reimporting an identical profile is idempotent. A profile cannot be rebound to another origin. Importing changed values for the same origin rotates it and advances its revision. List and import output only metadata, never secret values. These commands do not create a project.

To change only the nonsecret `env` and/or `session` settings, create a separate JSON object such as `{"env":{"SERVICE_TOKEN":"token"}}` and run:

```powershell
ase --home C:\Private\ASE credentials configure example-service --target https://service.example --file C:\Private\settings.json
```

Use `{"session":null}` to remove a session recipe. Do not put secret values in command arguments. An optional `session` object has `probe` and `login` recipes, each with an absolute executable path in `argv` and a timeout in seconds, for example:

```json
{"session":{"probe":{"argv":["C:\\Tools\\service-adapter.exe","probe"],"timeout":30},"login":{"argv":["C:\\Tools\\service-adapter.exe","login"],"timeout":60}}}
```

The adapter receives only the configured secret fields through its child environment. It returns a JSON object on stdout with `status`: `authenticated`, `login_required`, `interaction_required`, or `unavailable`. The framework probes first; only `login_required` triggers one login, followed by another probe. A successful login may return a `secrets` object containing replacement values for existing keys, which are stored encrypted with a revision check. Recipe stdout and stderr are not returned as evidence. A profile without a session recipe is prepared for child execution; this alone does not establish live authentication.

Declare access on an evidence check or infrastructure action, and include the exact target in both the task scope and the relevant stage's scope resources. For example, these are excerpts from a task contract (the other required contract fields are omitted):

```json
{
  "scope": {"resources": ["https://service.example"], "effects": ["command"]},
  "evidence": [{"id": "service-check", "kind": "command", "argv": ["service-check"], "required": true,
                "access": {"credential_ref": "example-service", "target": "https://service.example"}}],
  "acceptance": [{"id": "AC1", "expected": "Service check succeeds", "mandatory": true, "evidence_ids": ["service-check"]}],
  "stages": [{"id": "observe", "scope": {"resources": ["https://service.example"], "effects": ["command"]},
              "acceptance_ids": ["AC1"], "depends_on": []}]
}
```

Infrastructure actions use the same `access` mapping for observation and execution; their stage scope must also permit the relevant effects. Obtain current approval for the stage and exact origin. Credentials do not themselves grant task authorization. With a selected project, explicit access operations use the same approval and declared-access checks as the workflow:

```powershell
ase --home C:\Private\ASE --project PROJECT access ensure TASK --stage STAGE --credential example-service --target https://service.example
ase --home C:\Private\ASE --project PROJECT access request TASK --stage STAGE --credential example-service --target https://service.example --path /health --method GET
```

The built-in HTTP observation supports only GET and HEAD with basic or bearer authentication, verifies TLS for HTTPS, and rejects redirects. Services requiring another protocol need an appropriate configured CLI adapter. No general HTTP mutation or automatic interactive login is provided. Updating secrets or configuration advances the profile revision and invalidates evidence bound to the previous revision. All three supported agent workflows call this shared CLI; this does not assert that any specific external agent or service has been live-tested.

Recipe arguments cannot contain raw or supported encoded secret forms, including composite Basic credentials. Invalid configuration is rejected atomically, including during rotation. HTTP401/403 are typed authentication/authority blockers; raw response bodies from those failures are withheld. A completed task may refresh authenticated observations/checks but cannot replay its completed mutations.

The input import file stays unchanged; protect or remove that original file separately. Native CLIs may maintain their own session caches. Windows DPAPI protects this vault under the current user, not against other code with that user's rights; see [Microsoft's API documentation](https://learn.microsoft.com/en-us/windows/win32/api/dpapi/nf-dpapi-cryptprotectdata). Actual final checks, source boundaries, supported protocols and remaining limits are recorded in the [credential validation report](local-credentials-validation-2026-11-12.md).
