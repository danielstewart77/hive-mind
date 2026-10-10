"""Shared, mind-agnostic harness adapters.

A mind folder (``minds/<name>/``) holds configuration only — ``runtime.yaml``,
prompts, ``container/compose.yaml``, per-mind data. The container runs
``minds.mind_server``, which mounts all of these adapters and routes each
session to the harness it names; ``MIND_NAME`` points it at the folder:

* ``minds.harness.claude_cli`` — long-lived Claude CLI subprocess per
  session, stream-json transport (Anthropic- or Ollama-backed via
  ``runtime.yaml`` env).
* ``minds.harness.codex_cli`` — one Codex CLI subprocess per turn
  (OpenAI- or Ollama-backed via ``runtime.yaml`` provider/env).
* ``minds.harness.dsh_cli`` — one dsh process per turn, through the
  resumable surface in the mounted dsh tree.

Each module still serves as a one-harness app on its own, which is how the
tests drive it.

Because the deployed minds run these exact modules, the shipped harness can
never drift from the wiring that is actually in production.
"""
