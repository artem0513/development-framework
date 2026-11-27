# Local credential access

Use for an authorized task that needs a service account. Codex, Claude and Copilot share the same local `ase` home/vault under the same OS user; do not create backend-specific copies. Put global `--home`, `--repo` and `--project` selectors before the command. Windows DPAPI CurrentUser is the implemented encrypted backend; another OS reports an explicit blocker, never plaintext fallback.

In project-local mode invoke that project's `.devframework/bin/ase.ps1` or `.devframework/bin/ase`. The vault is separate per project inside protected ignored `.devframework/local/credentials`; an external import file is still required. Do not fall back to a global vault or another project to resolve a missing profile. Native provider/session caches are not relocated.

Start with `ase credentials list`. Public metadata contains the reference, exact target origin, kind, revision, secret-field/environment names and `has_session`, not secret values or command arguments. Match the approved service and intended account. Retrieve existing context before asking; two unresolved production/test identities are a real decision, not a reason to guess.

Import only the exact local file the user designated, outside workspaces/Git: `ase credentials import --file ABSOLUTE_FILE`. The importer reads and encrypts it locally. Do not open that file with model-facing read tools, paste it into a prompt, copy it into project sources or ask for secrets in chat. The original file remains unchanged and plaintext unless its owner stores/protects it separately. No automatic search of other credential files, browser stores or provider homes.

Known CLI authentication may be configured with a nonsecret JSON file via `ase credentials configure REF --target ORIGIN --file CONFIG`. Choose supported local tools/flags after discovery and keep helper writes/external effects within the existing approved task. Configure child environment names from the advertised secret-field names; never interpolate values into argv or export them into the parent/model environment. No new user interview for routine supported setup.

Optional CLI session recipes have probe/login argv plus timeouts. Their structured status is `authenticated`, `login_required`, `interaction_required` or `unavailable`; a successful login may refresh existing secret fields into the encrypted vault. The executor probes first, logs in at most once when needed, then probes again. A configured recipe (`has_session`) is availability, not a valid session. `prepared` means environment preparation, not verified authentication.

Declare references only in executable check/action specs:

```json
{"access":{"credential_ref":"vm-lab","target":"https://lab.example"}}
```

The exact origin must be in the executing stage's approved `scope.resources`; ordinary file scope or another stage's approval does not grant it. Existing checks and infrastructure actions use access automatically, including resource observations. For an explicitly planned binding use `ase access ensure TASK --stage STAGE --credential REF --target ORIGIN`. `access request` supports GET/HEAD observations for HTTP Basic/Bearer; it verifies TLS, never follows redirects, and is not a universal mutation/browser-login adapter.

No recipe for an environment profile is not automatically a blocker: a supported command may authenticate directly from its child environment. Use actual checks/HTTP observations/session probes before claiming authentication. Missing mandatory CLI/protocol support, invalid access, denied authority, manual 2FA/passkey/CAPTCHA, or unresolved business identity are concrete blockers. Explain the recommended resolution once, continue independent work, and resume after observing the required action in the service's own local flow. Do not bypass a challenge or weaken provider/platform permissions.

Reuse prior task approval for technical corrections and repeated access within the same target/outcome. New resources, material cost/risk or authority still need scope approval. A password does not grant all operations. Avoid prompting per command, per probe or per bot.

Never retain raw auth command output in reports. Logs, SQLite, model packets and exports contain safe references/status only. Secret rotation or recipe configuration changes invalidate affected evidence. Recheck; completed tasks refresh observations without replaying their resource mutations or resetting budgets. An absent resource remains a blocker.
