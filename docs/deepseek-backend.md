# DeepSeek backend

DeepSeek can run the independent packet-only roles (`ac`, `review`) through its
OpenAI-compatible chat completions API. It is not a host chat backend: the lead,
coordinator, native delegation and `bind-local` stay on Codex, Claude Code or Copilot.

## Use

- Set the API key in the `DEEPSEEK_API_KEY` environment variable of the process that runs
  `ase`. The key is never put in argv, receipts, artifacts or state; the probe records only
  `auth_availability: key-present | key-missing`. Without the key the role is blocked with
  `API_KEY_MISSING` and nothing is sent.
- Only the DeepSeek worker receives the key: importing `ase` moves it out of the process
  environment into memory (`ase/child_environment.py`), so every other process `ase` starts
  (project test and access commands, Git with its hooks and fsmonitor, the other backends,
  protocol fixtures) runs without it. A user-level variable is still visible to every process the
  owner starts outside `ase`; an encrypted credential profile would narrow that further.
- Select it per role in the task contract:
  `"execution_policy": {"role_backends": {"ac": "deepseek", "review": "deepseek"}}`.
  `"backend": "deepseek"` is rejected: that backend also runs implementation, which needs
  tools, and the adapter refuses every role except `ac` and `review`.
- Optional host `backend_config` record `deepseek`: `model` (default `deepseek-flash`),
  `base_url` and the usual runner limits. The key goes only to `https://api.deepseek.com`,
  a loopback test server over plain HTTP, or another host with `allow_custom_base_url: true`;
  anything else is blocked with `BASE_URL_NOT_ALLOWED`.

## How it runs

There is no DeepSeek agent CLI. The pinned executable is the Python interpreter running
`ase/adapters/deepseek_worker.py` in isolated mode (`-I`). The packet goes on stdin, the
schema in the run's schema artifact; the worker makes one request with
`response_format: json_object` and prints one result line. The receipt records the interpreter
and its hash. The probe records the script the interpreter actually runs and that script's hash,
and blocks with `WORKER_NOT_PINNED` unless the interpreter is the one running `ase` (or
`allow_custom_interpreter: true`) and the script is the bundled worker. Only then is the key
delivered. A receipt verifies only when its argv ran that recorded worker script. API errors, malformed or oversized responses are result lines
with `is_error: true`, with the key redacted in raw and encoded forms. The model is given no tools, so the
packet-only policy holds by construction; there is no OS sandbox.

## Limits

- JSON mode guarantees a JSON object, not the schema: the Runner validates the answer
  against the schema and blocks with `SCHEMA_INVALID` otherwise.
- The API does not report the model's context window, so the packet budget stays at the
  conservative default.
- Like the other backends, this is a local reliability layer: a hostile same-user agent can
  still change the worker or the environment. Only a trusted CI runner can enforce more.
