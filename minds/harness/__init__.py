"""Shared, mind-agnostic harness services.

A mind folder (``minds/<name>/``) holds configuration only — ``runtime.yaml``,
prompts, ``container/compose.yaml``, per-mind data. The code that runs the
container is one of these harness modules, selected by the fragment's
``command`` and pointed at the folder via the ``MIND_NAME`` env var:

* ``minds.harness.claude_cli`` — long-lived Claude CLI subprocess per
  session, stream-json transport (Anthropic- or Ollama-backed via
  ``runtime.yaml`` env).
* ``minds.harness.codex_cli`` — one Codex CLI subprocess per turn
  (OpenAI- or Ollama-backed via ``runtime.yaml`` provider/env).

Because the deployed minds run these exact modules, the shipped harness can
never drift from the wiring that is actually in production.
"""
