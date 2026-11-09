"""Deliverable D: the inference proxy's own command, and its model listing.

As built (inference-proxy PR #19), inside the proxy container
(`python -m app.cli`), JSON on stdout; exit 0 ok, 2 refused {"error"}, 1 the
database unreachable or not migrated; a name that exists is reused untouched:

    add-provider --name N --kind anthropic|openai|ollama [--base-url U] [--api-key KEY]
    add-model --provider N --model M --context-window W
    mint-key --client NAME [--kind mind] -> {"client","created","minted","key"};
        an existing live key -> minted false, key null
    list-models [--harness dsh]

A newly minted key is accepted within KEY_CACHE_REFRESH_SECONDS (5s), so the
listing is retried briefly. The container migrates its own schema on start
(`hive_data.migrate` creates no proxy tables) and may create it only while the
`proxy` schema has no `alembic_version` table — PROXY_ALLOW_SCHEMA_CREATE is
set from that, never left on.

The models offered for the mind are read from the proxy's HTTP listing,
`GET /v1/models?harness=dsh` with the mind's client key — the same listing a
dsh mind reads at run time, and the only one an existing proxy (whose
container is not ours to exec into) can give.
"""

from __future__ import annotations

import json
import time

from installer.shell import Shell, StepFailed, http_in_network

SERVICE = "inference-proxy"
PORT = 8888
URL = f"http://{SERVICE}:{PORT}"
CLI = ["python", "-m", "app.cli"]
LISTING = "/v1/models"
#: The proxy creates its own tables on a first start only when told to.
ALLOW_CREATE = "PROXY_ALLOW_SCHEMA_CREATE"
SCHEMA = "proxy"


def environment(env_ref, *, dsn_ref: str) -> dict[str, str]:
    return {
        "DATABASE_URL": dsn_ref,
        "DB_SCHEMA": SCHEMA,
        ALLOW_CREATE: env_ref(ALLOW_CREATE),
        "SESSION_SECRET": env_ref("PROXY_SESSION_SECRET"),
        "ADMIN_USERNAME": env_ref("HIVE_SEED_USERNAME"),
        "ADMIN_PASSWORD": env_ref("PROXY_ADMIN_PASSWORD"),
        "PORT": str(PORT),
        "USE_HTTPS": "false",
    }


def _cli(sh: Shell, compose: list[str], args: list[str], redact=()) -> dict:
    proc = sh.run([*compose, "exec", "-T", SERVICE, *CLI, *args], timeout=60, redact=redact,
                  check=False)
    lines = [l for l in (proc.stdout or "").splitlines() if l.strip()]
    try:
        out = json.loads(lines[-1])
    except (IndexError, ValueError):
        out = {}
    if proc.returncode != 0:
        why = out.get("error") or " | ".join(((proc.stderr or "") + (proc.stdout or "")).strip()
                                              .splitlines()[-3:])
        for secret in redact:
            why = why.replace(secret, "***") if secret else why
        raise StepFailed(f"the proxy's `{args[0]}` refused (exit {proc.returncode}): {why}")
    if not isinstance(out, dict):
        raise StepFailed(f"the proxy's `{args[0]}` printed no JSON")
    return out


def add_provider(sh, compose, *, name, kind, base_url="", api_key="") -> dict:
    args = ["add-provider", "--name", name, "--kind", kind]
    if base_url:
        args += ["--base-url", base_url]
    if api_key:
        args += ["--api-key", api_key]
    return _cli(sh, compose, args, redact=(api_key,))


def add_model(sh, compose, *, provider, model, context_window) -> dict:
    return _cli(sh, compose, ["add-model", "--provider", provider, "--model", model,
                              "--context-window", str(context_window)])


def mint_key(sh, compose, *, client) -> str | None:
    """The new key, or None when the client already had one (minted nothing)."""
    out = _cli(sh, compose, ["mint-key", "--client", client, "--kind", "mind"])
    return str(out["key"]) if out.get("minted") and out.get("key") else None


def list_models(sh: Shell, proxy_url: str, key: str, harness: str = "dsh",
                retries: int = 4, pause: float = 3.0) -> list[str]:
    """The model names the proxy offers this harness."""
    root = proxy_url.rstrip("/")
    root = root[: -len("/v1")] if root.endswith("/v1") else root
    for attempt in range(retries + 1):
        out = http_in_network(sh, "GET", f"{root}{LISTING}?harness={harness}",
                              headers={"Authorization": f"Bearer {key}"})
        # A key minted a moment ago is refused until the proxy's key cache
        # refreshes.
        if out.get("status") != 401 or attempt == retries:
            break
        time.sleep(pause)
    if out.get("status") != 200:
        raise StepFailed(f"the proxy's model listing answered {out.get('status')}: "
                         f"{out.get('error') or out.get('body', '')[:200]}")
    try:
        rows = json.loads(out["body"]).get("data") or []
    except ValueError as exc:
        raise StepFailed("the proxy's model listing is not JSON") from exc
    return [str(r["id"]) for r in rows if r.get("id")]
