# Ollama-Backed Mind

How to point either harness at a local Ollama instance instead of the
default provider. Not a separate harness or template — the same
`minds/harness/claude_cli.py` / `codex_cli.py` module reads this from
`runtime.yaml` at spawn time.

## Claude CLI harness

Set `provider: ollama` and add the auth-bypass env vars Claude Code needs
to redirect its API calls:

```yaml
provider: ollama
env:
  ANTHROPIC_AUTH_TOKEN: ollama
  ANTHROPIC_API_KEY: ""
  ANTHROPIC_BASE_URL: http://<ollama-host>:11434
```

Pass the desired Ollama model as `default_model` (e.g. `gpt-oss:20b-32k`).
Context window: check the model's Ollama listing — 32k is the minimum
Ollama recommends, 64k+ is preferred if the mind's conversations run long.

## Codex CLI harness

Set `provider: ollama` and give the harness an OpenAI-compatible base URL;
`_provider_args()` in `codex_cli.py` builds the `-c model_provider=...`
override from it automatically — no other code changes needed:

```yaml
provider: ollama
resume_policy: provider-local
transport:
  type: codex_exec_json
env:
  OLLAMA_BASE_URL: http://<ollama-host>:11434/v1
```

## Both harnesses

- `provider` and `env` are the only fields that change — `name`, `mind_id`,
  `gateway_url`, prompt files, and everything else in `runtime.yaml` stay
  as they are for any other mind.
- Add the mind to `group_chat.available_minds` in `config.yaml` if it
  should participate in moderated group sessions.
- The model is switchable per-deployment by editing `runtime.yaml` and
  recreating the container — no code change required.
