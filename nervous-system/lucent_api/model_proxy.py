"""Where lucent's model calls go: the hive's inference proxy, with one key.

Every model lucent uses — the embedder behind every memory write and
search, the classifier behind the nightly prune — is reached through the
proxy named by `INFERENCE_PROXY_URL`, authenticated with
`INFERENCE_PROXY_KEY`. Nothing here calls Ollama.

A module of its own, imported by both `lucent_memory` and `prune_memory`,
because the prune module is imported lazily by the scheduler: a running
lucent whose `lucent_memory` was loaded before this existed still loads the
prune, and this, fresh.
"""

from __future__ import annotations

import os


def settings(model_var: str) -> tuple[str, str, str]:
    """(proxy root, proxy key, model) for a model call, read when it is made.

    `INFERENCE_PROXY_URL` is the proxy's root; a trailing `/v1` is dropped so
    either spelling addresses `/v1/...` once. Every one of the three is
    required: there is no default model and no fallback to Ollama, so an
    unset one is named rather than guessed.
    """
    root = os.environ.get("INFERENCE_PROXY_URL", "").strip().rstrip("/")
    if root.endswith("/v1"):
        root = root[: -len("/v1")]
    key = os.environ.get("INFERENCE_PROXY_KEY", "").strip()
    model = os.environ.get(model_var, "").strip()
    missing = [name for name, value in (("INFERENCE_PROXY_URL", root),
                                        ("INFERENCE_PROXY_KEY", key),
                                        (model_var, model)) if not value]
    if missing:
        raise RuntimeError(f"lucent cannot reach a model: {', '.join(missing)} unset")
    return root, key, model
