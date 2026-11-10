"""Deliverable D: the inference proxy's own command, and its model listing.

As built (inference-proxy PRs #19 and #20), inside the proxy container
(`python -m app.cli`); stdout is one JSON object, prints go to stderr; exit 0
ok, 2 refused {"error"}, 1 the database unreachable or not migrated:

    add-provider --name N --kind anthropic|openai|ollama [--base-url U]
                 [--api-key-env NAME] [--update]
        the key read from the env name, passed with `exec -T -e NAME`, so it
        is never in an argv; --update rewrites address and key ("updated")
    add-model --provider N --model M --context-window W [--update]
    mint-key --client NAME [--kind mind] -> {"client","created","minted","key"};
        an existing live key -> minted false, key null
    check-model --model M [--harness dsh] -> exit 0 {"ok": true, "status": 200,
        "reply"}; exit 2 {"error", "status": 401|404|502}; up to ~45s
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
HOST_PORT = 8888
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


#: The env name the provider key rides in on: `exec -T -e NAME` passes it
#: from the installer's own environment, so it is never in an argv.
KEY_ENV = "PROVIDER_API_KEY"
CHECK_TIMEOUT = 60


def _cli(sh: Shell, compose: list[str], args: list[str], *, env: dict | None = None,
         quiet: bool = False, timeout: float = 60) -> tuple[int, dict]:
    """(exit code, the JSON on stdout). Exit 0 ok, 2 refused {"error"}, 1 the
    database unreachable or not migrated; prints go to stderr."""
    passed = [x for name in (env or {}) for x in ("-e", name)]
    proc = sh.run([*compose, "exec", "-T", *passed, SERVICE, *CLI, *args], timeout=timeout,
                  env=env, check=False, quiet=quiet)
    try:
        out = json.loads((proc.stdout or "").strip() or "{}")
    except ValueError:
        out = {}
    if not isinstance(out, dict):
        out = {}
    if proc.returncode not in (0, 2) or (proc.returncode == 2 and not out.get("error")):
        tail = " | ".join((proc.stderr or proc.stdout or "").strip().splitlines()[-3:])
        raise StepFailed(f"the proxy's `{args[0]}` failed (exit {proc.returncode}): {tail}")
    return proc.returncode, out


def _ok(code: int, out: dict, what: str) -> dict:
    if code != 0:
        raise StepFailed(f"the proxy refused {what}: {out.get('error')}")
    return out


def add_provider(sh, compose, *, name, kind, base_url="", api_key="", update=False) -> dict:
    """Create the provider, or with `update` rewrite its address and key."""
    args = ["add-provider", "--name", name, "--kind", kind]
    if base_url:
        args += ["--base-url", base_url]
    env = None
    if api_key:
        args += ["--api-key-env", KEY_ENV]
        env = {KEY_ENV: api_key}
    if update:
        args.append("--update")
    return _ok(*_cli(sh, compose, args, env=env), f"the provider {name!r}")


def add_model(sh, compose, *, provider, model, context_window, update=False) -> dict:
    args = ["add-model", "--provider", provider, "--model", model,
            "--context-window", str(context_window)]
    if update:
        args.append("--update")
    return _ok(*_cli(sh, compose, args), f"the model {model!r}")


def check_model(sh, compose, *, model: str, harness: str = "dsh") -> dict:
    """One real completion through the proxy. Its own upstream retries take
    up to about 45 seconds."""
    code, out = _cli(sh, compose, ["check-model", "--model", model, "--harness", harness],
                     timeout=CHECK_TIMEOUT)
    if code == 0 and out.get("ok"):
        return out
    status = out.get("status")
    why = out.get("error") or "no answer"
    if status == 401:
        raise StepFailed(f"the provider refused the key for {model!r} (401): {why}")
    raise StepFailed(f"a test completion on {model!r} failed ({status}): {why}")


def mint_key(sh, compose, *, client) -> str | None:
    """The new key, or None when the client already had one (minted nothing)."""
    out = _ok(*_cli(sh, compose, ["mint-key", "--client", client, "--kind", "mind"],
                    quiet=True), f"a key for {client!r}")
    return str(out["key"]) if out.get("minted") and out.get("key") else None


def list_models(sh: Shell, proxy_url: str, key: str, harness: str = "dsh",
                retries: int = 4, pause: float = 3.0, proxy_env: dict | None = None) -> list[str]:
    """The model names the proxy offers this harness."""
    root = proxy_url.rstrip("/")
    root = root[: -len("/v1")] if root.endswith("/v1") else root
    for attempt in range(retries + 1):
        out = http_in_network(sh, "GET", f"{root}{LISTING}?harness={harness}",
                              headers={"Authorization": f"Bearer {key}"}, proxy_env=proxy_env)
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
