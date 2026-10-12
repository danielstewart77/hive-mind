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

None of them is a server: ``minds.mind_server`` owns the HTTP surface and
calls each adapter's session functions. Because the deployed minds run these
exact modules, the shipped harness can never drift from the wiring that is
actually in production.
"""


def chat_surface(body: dict) -> str:
    """The surface a chat process serves, as its ``HIVE_SURFACE``.

    The gateway names it on every spawn (telegram, discord, ...); the owner
    type's prefix stands in for a gateway that names none. The process's
    hooks read it: the per-turn context tells the model where a message
    arrived, and the context report names it, since the gateway learns a
    harness's own overhead only from the surface a conversation opened in.

    A chat process is never ``terminal``, whatever the row's owner says: a
    conversation a pane holds is also answered by chat processes (the
    console's message route, a model change), and every hook reads
    ``terminal`` as "I am inside a pane" — staging a pane's rotation that no
    pane will fire, ledgering turns the chat path already ledgers, and
    teaching the gateway a pane's overhead from a process. Those processes
    serve the console.
    """
    surface = str(body.get("surface") or "").strip()
    if not surface:
        surface = str(body.get("owner_type") or "").split(":", 1)[0].strip()
    return "console" if surface in ("terminal", "web") else surface
