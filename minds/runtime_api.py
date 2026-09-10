"""A mind's `runtime.yaml` — read at every boot, writable at runtime.

`minds/<name>/runtime.yaml` is the durable truth about what a mind is. The
broker's `minds` row is a cache of it: every mind re-registers from its own
file on start, so a rebuilt broker database, a mind moved to a new address,
or a row edited out of band all converge on the file rather than on whatever
was true at install time.

Editing goes the other way. The console never reaches into a bind-mounted
copy of this file — it PATCHes the mind over HTTP and then refreshes the
broker row, which is one code path for a container in this stack, a
bare-metal mind on the same host, and a mind on someone else's laptop.

Both harness servers mount these routes; see `install_runtime_routes`.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import secrets
import tempfile
from pathlib import Path
from typing import Any

import yaml
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from core.hive_logging import log_event

# An alias (`opus`), an Ollama tag (`qwen3:30b-a3b-instruct-2507-q4_K_M`),
# or a vendor id (`gpt-5.4`). The console validates the same shape.
_MODEL_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}")

# What a mind is willing to say about itself. runtime.yaml holds no secrets
# today; an allowlist keeps a future field from leaking by default.
PUBLIC_FIELDS = (
    "name",
    "mind_id",
    "description",
    "profile",
    "role",
    "deployment",
    "harness",
    "provider",
    "default_model",
    "gateway_url",
    "remote",
    "surfaces",
    "resume_policy",
)


def load_runtime(path: Path) -> dict[str, Any]:
    """A mind's runtime.yaml as a dict. Raises if absent or malformed."""
    try:
        loaded = yaml.safe_load(Path(path).read_text()) or {}
    except OSError as exc:
        raise ValueError(f"No runtime configuration at {path}") from exc
    except yaml.YAMLError as exc:
        raise ValueError(f"Runtime configuration is invalid: {exc}") from exc
    if not isinstance(loaded, dict):
        raise ValueError("Runtime configuration must be a mapping")
    return loaded


def public_runtime(path: Path) -> dict[str, Any]:
    """The allowlisted view served to the console."""
    loaded = load_runtime(path)
    return {k: loaded[k] for k in PUBLIC_FIELDS if k in loaded}


#: Fields the console may write, and the pattern each value must match. A
#: provider is a name in the proxy's providers table; a model is a deployment
#: name from that provider's listing.
WRITABLE_FIELDS = {
    "default_model": _MODEL_NAME_RE,
    "provider": re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}"),
}


def update_runtime_fields(path: Path, fields: dict[str, str]) -> dict[str, Any]:
    """Rewrite the given fields in place, atomically, preserving the rest.

    Line-level substitution rather than a YAML round-trip: dumping the parsed
    document back would strip the comments that explain each field to whoever
    opens the file next. Every field lands in one write, so a mind is never
    left holding a provider that does not host the model beside it.
    """
    if not fields:
        raise ValueError("Nothing to write")
    unknown = sorted(set(fields) - set(WRITABLE_FIELDS))
    if unknown:
        raise ValueError(f"Not a writable field: {', '.join(unknown)}")
    for field, value in fields.items():
        if not WRITABLE_FIELDS[field].fullmatch(value or ""):
            raise ValueError(f"{field} contains unsupported characters")

    path = Path(path)
    load_runtime(path)  # reject a malformed file before touching it
    updated = path.read_text()
    for field, value in fields.items():
        updated, count = re.subn(
            rf"^{field}\s*:.*$",
            f"{field}: {value}",
            updated,
            count=1,
            flags=re.MULTILINE,
        )
        if count != 1:
            raise ValueError(f"Runtime configuration has no {field} field")

    fd, temporary = tempfile.mkstemp(prefix="runtime-", suffix=".yaml", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(updated)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
    return load_runtime(path)


def registration_payload(path: Path, mind_name: str = "") -> dict[str, str]:
    """The broker registration this mind's runtime.yaml describes."""
    loaded = load_runtime(path)
    missing = [
        field
        for field in ("mind_id", "gateway_url", "default_model", "harness")
        if not str(loaded.get(field) or "").strip()
    ]
    if missing:
        raise ValueError(f"runtime.yaml is missing: {', '.join(missing)}")
    payload = {
        "mind_id": str(loaded["mind_id"]).strip(),
        "name": str(loaded.get("name") or mind_name).strip(),
        "gateway_url": str(loaded["gateway_url"]).strip(),
        "model": str(loaded["default_model"]).strip(),
        "harness": str(loaded["harness"]).strip(),
    }
    # The admin-guarded registration this mind already performs every boot is
    # the only channel by which the gateway learns the credential. Omitted
    # when there is none, because a registration that sent an empty one would
    # erase the gateway's working copy.
    token = session_token(Path(path).parent)
    if token:
        payload["session_token"] = token
    return payload


# The credential the gateway must present on every call it makes to this
# mind. Lives beside runtime.yaml rather than inside it: runtime.yaml is
# served to the console through `public_runtime`, and a secret one allowlist
# edit away from being published is a secret waiting to be published.
SESSION_TOKEN_FILENAME = "session_token"

log = logging.getLogger("hive-mind.runtime")


def session_token(mind_dir: Path) -> str:
    """This mind's own session credential, minted once and kept.

    Minted rather than issued: a mind nobody provisioned still ends up with a
    credential of its own, and one taken off it opens that mind and no other.
    `MIND_SESSION_TOKEN` overrides the file for installs that inject secrets
    instead of letting a container write them.

    Returns "" when there is no token and none can be written — a read-only
    mind directory must leave the mind serving as it did before, not brick it.
    """
    injected = os.environ.get("MIND_SESSION_TOKEN", "").strip()
    if injected:
        return injected

    path = Path(mind_dir) / SESSION_TOKEN_FILENAME
    existing = _read_token(path)
    if existing:
        return existing

    minted = secrets.token_urlsafe(32)
    try:
        handle = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        # Another worker in this process group won the race, or left an empty
        # file behind. Whatever is there now is the mind's token.
        raced = _read_token(path)
        if raced:
            return raced
        try:
            path.write_text(minted + "\n")
            path.chmod(0o600)
        except OSError:
            log.warning("Could not write session token at %s", path)
            return ""
        return minted
    except OSError:
        log.warning("Could not create session token at %s", path)
        return ""
    with os.fdopen(handle, "w") as stream:
        stream.write(minted + "\n")
    return minted


def _read_token(path: Path) -> str:
    try:
        return path.read_text().strip()
    except OSError:
        return ""


def presented_bearer(request: Request) -> str:
    """The credential on a request, from either place a client can put it.

    A browser cannot set headers on a WebSocket handshake, so the subprotocol
    is the only channel a direct attach has; the gateway's proxy uses the
    header.
    """
    header = request.headers.get("Authorization", "")
    if header.startswith("Bearer "):
        return header[7:]
    offered = request.headers.get("Sec-WebSocket-Protocol", "")
    for part in offered.split(","):
        part = part.strip()
        if part.startswith("bearer."):
            return part[7:]
    return ""


def authorize_session(request: Request, mind_dir: Path) -> JSONResponse | None:
    """Guard a session route. None means the caller may proceed.

    Accepts the mind's session token — the gateway, which is the only real
    caller — or the admin token, so the console or the operator can reach a
    wedged session directly. A mind holding no token of its own serves as it
    always did: that is what lets the fleet move one machine at a time.
    """
    expected = session_token(mind_dir)
    if not expected:
        return None
    presented = presented_bearer(request)
    if secrets.compare_digest(presented, expected):
        return None
    admin = admin_token()
    if admin and secrets.compare_digest(presented, admin):
        return None
    return JSONResponse({"error": "unauthorized"}, status_code=401)


def install_session_guard(app: FastAPI, *, mind_dir: Path) -> None:
    """Require this mind's credential on every `/sessions` HTTP route.

    One middleware rather than a decorator per route: a session route added
    later cannot ship open by being forgotten, and `DELETE /sessions/{id}`
    matters as much as the message route. The config surface is untouched —
    `/runtime`, `/skills`, `/files` and `/models` keep their admin guard.
    """

    @app.middleware("http")
    async def _guard_session_routes(request: Request, call_next):
        if request.url.path.startswith("/sessions"):
            denied = authorize_session(request, mind_dir)
            if denied is not None:
                return denied
        return await call_next(request)


async def refuse_session_websocket(websocket, denial: JSONResponse) -> None:
    """Refuse a WebSocket attach with a real HTTP status.

    A pre-accept `close()` presents to the gateway as HTTP 403 — which is also
    what a mind whose image predates the terminal routes answers — so the
    denial response is what keeps "refused your credential" from being read as
    "has no terminal".
    """
    try:
        await websocket.send_denial_response(denial)
    except (RuntimeError, AttributeError):
        # The server does not implement the denial-response extension.
        await websocket.close(code=4401, reason="unauthorized")


def admin_token() -> str:
    """Bearer accepted on a mind's config-write route.

    A dedicated `MIND_ADMIN_TOKEN` when the install has one; otherwise the
    gateway's admin bearer, which the console already holds. No token
    configured means the write route refuses rather than opens.
    """
    return (
        os.environ.get("MIND_ADMIN_TOKEN")
        or os.environ.get("COMMS_ADMIN_BEARER_TOKEN")
        or ""
    )


def authorize_admin(request: Request) -> JSONResponse | None:
    """Guard a config-write route. None means the caller may proceed."""
    expected = admin_token()
    if not expected:
        return JSONResponse(
            {"error": "no admin token configured on this mind"}, status_code=503
        )
    header = request.headers.get("Authorization", "")
    presented = header[7:] if header.startswith("Bearer ") else ""
    if not secrets.compare_digest(presented, expected):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return None


async def register_with_broker(path: Path, *, mind_name: str, mind_id: str, log) -> str:
    """Publish this mind's runtime.yaml to the broker's registry, once.

    Every boot, not just the install. `register_mind` upserts on `mind_id`,
    so this is the reconciliation step that makes the file authoritative.
    The console writes the file before it writes the row, so a config edit
    survives the restart that follows it.

    Never fatal: a mind that can't reach comms still serves its own
    sessions. Returns the outcome so `registration_loop` can decide what a
    failure means — "registered", "retry" (unreachable or broker-side
    error), "rejected" (the broker refused the payload or the token; trying
    again sends the same thing), or "skipped" (not configured to register).
    """
    import aiohttp

    comms_url = os.environ.get("COMMS_URL", "").rstrip("/")
    token = os.environ.get("COMMS_ADMIN_BEARER_TOKEN", "")
    if not comms_url or not token:
        log.info("No COMMS_URL/admin token — skipping broker self-registration")
        return "skipped"
    try:
        payload = registration_payload(path, mind_name)
    except ValueError as exc:
        # A file mid-write or momentarily unreadable must not end
        # registration for the process lifetime; only absent comms
        # configuration is a decision rather than a moment.
        log_event(log, "mind.register.failed", mind_id=mind_id, error=str(exc))
        return "retry"
    try:
        async with aiohttp.ClientSession() as http:
            async with http.post(
                f"{comms_url}/broker/minds",
                json=payload,
                headers={"Authorization": f"Bearer {token}"},
                timeout=aiohttp.ClientTimeout(total=10),
                allow_redirects=False,
            ) as resp:
                # Only a 2xx is a registration. aiohttp would otherwise
                # follow a redirect, downgrade the POST to a GET against
                # the (unauthenticated) listing route, and report the 200
                # as success while registering nothing.
                if resp.status >= 500:
                    log_event(
                        log, "mind.register.failed", mind_id=mind_id, status=resp.status
                    )
                    return "retry"
                if not 200 <= resp.status < 300:
                    log_event(
                        log, "mind.register.rejected", level=logging.WARNING,
                        mind_id=mind_id, status=resp.status,
                    )
                    return "rejected"
    except Exception as exc:
        log_event(
            log, "mind.register.failed", mind_id=mind_id, error_type=type(exc).__name__
        )
        return "retry"
    log_event(
        log, "mind.registered", mind_id=mind_id, mind_name=payload["name"],
        model=payload["model"], harness=payload["harness"],
        gateway_url=payload["gateway_url"],
    )
    return "registered"


async def registration_loop(
    path: Path,
    *,
    mind_name: str,
    mind_id: str,
    log,
    initial_delay: float = 1.0,
    max_delay: float = 60.0,
    heartbeat: float = 300.0,
    sleep=asyncio.sleep,
) -> None:
    """Keep this mind registered for as long as it runs.

    A mind that boots before comms does must not stay off the registry until
    someone restarts it — that race is real on every reboot, since systemd
    wins against a compose stack. Unreachable or erroring comms is retried
    with doubling delays capped at `max_delay`; an outright rejection stops
    the loop, because resending an unacceptable payload forever is noise,
    not persistence.

    After a success the loop keeps going as a heartbeat: re-registering
    every `heartbeat` seconds converges a broker row that was rebuilt or
    edited out from under the file.
    """
    delay = initial_delay
    while True:
        outcome = await register_with_broker(
            path, mind_name=mind_name, mind_id=mind_id, log=log
        )
        if outcome in ("rejected", "skipped"):
            log_event(
                log, "mind.register.loop_stopped", level=logging.WARNING,
                mind_id=mind_id, outcome=outcome,
            )
            return
        if outcome == "registered":
            delay = initial_delay
            await sleep(heartbeat)
        else:
            await sleep(delay)
            delay = min(delay * 2, max_delay)


def install_runtime_routes(app: FastAPI, *, path: Path, mind_id: str, log) -> None:
    """Mount GET/PATCH /runtime on a mind's FastAPI app."""

    @app.get("/runtime")
    async def get_runtime() -> Any:
        """This mind's runtime configuration, as the console renders it."""
        try:
            return {"configuration": public_runtime(path)}
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=500)

    @app.patch("/runtime")
    async def patch_runtime(req: Request) -> Any:
        """Set this mind's provider and default model, durably.

        Sessions already running keep the model they spawned with; the next
        one the gateway creates uses this.
        """
        denied = authorize_admin(req)
        if denied is not None:
            return denied
        body = await req.json()
        if not isinstance(body, dict):
            return JSONResponse({"error": "body must be an object"}, status_code=400)
        model = str(body.get("default_model") or "").strip()
        if not model:
            return JSONResponse({"error": "default_model required"}, status_code=400)
        fields = {"default_model": model}
        provider = str(body.get("provider") or "").strip()
        if provider:
            fields["provider"] = provider
        try:
            configuration = update_runtime_fields(path, fields)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        except OSError as exc:
            return JSONResponse(
                {"error": f"could not write runtime.yaml: {exc}"}, status_code=500
            )
        log_event(
            log, "mind.runtime.updated", mind_id=mind_id,
            default_model=model, provider=provider or None,
        )
        return {
            "saved": True,
            "configuration": {
                k: configuration[k] for k in PUBLIC_FIELDS if k in configuration
            },
        }
