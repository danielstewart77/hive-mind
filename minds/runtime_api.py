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


def update_default_model(path: Path, model: str) -> dict[str, Any]:
    """Rewrite `default_model` in place, atomically, preserving the rest.

    A line-level substitution rather than a YAML round-trip: dumping the
    parsed document back would strip the comments that explain each field to
    whoever opens the file next.
    """
    if not _MODEL_NAME_RE.fullmatch(model or ""):
        raise ValueError("Model name contains unsupported characters")
    path = Path(path)
    load_runtime(path)  # reject a malformed file before touching it
    text = path.read_text()
    updated, count = re.subn(
        r"^default_model\s*:.*$",
        f"default_model: {model}",
        text,
        count=1,
        flags=re.MULTILINE,
    )
    if count != 1:
        raise ValueError("Runtime configuration has no default_model field")

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
    return {
        "mind_id": str(loaded["mind_id"]).strip(),
        "name": str(loaded.get("name") or mind_name).strip(),
        "gateway_url": str(loaded["gateway_url"]).strip(),
        "model": str(loaded["default_model"]).strip(),
        "harness": str(loaded["harness"]).strip(),
    }


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


async def register_with_broker(path: Path, *, mind_name: str, mind_id: str, log) -> None:
    """Publish this mind's runtime.yaml to the broker's registry.

    Every boot, not just the install. `register_mind` upserts on `mind_id`,
    so this is the reconciliation step that makes the file authoritative.
    The console writes the file before it writes the row, so a config edit
    survives the restart that follows it.

    Never fatal: a mind that can't reach comms still serves its own
    sessions, and the next boot re-registers.
    """
    import aiohttp

    comms_url = os.environ.get("COMMS_URL", "").rstrip("/")
    token = os.environ.get("COMMS_ADMIN_BEARER_TOKEN", "")
    if not comms_url or not token:
        log.debug("No COMMS_URL/admin token — skipping broker self-registration")
        return
    try:
        payload = registration_payload(path, mind_name)
    except ValueError as exc:
        log_event(log, "mind.register.skipped", mind_id=mind_id, error=str(exc))
        return
    try:
        async with aiohttp.ClientSession() as http:
            async with http.post(
                f"{comms_url}/broker/minds",
                json=payload,
                headers={"Authorization": f"Bearer {token}"},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status >= 400:
                    log_event(
                        log, "mind.register.failed", mind_id=mind_id, status=resp.status
                    )
                    return
    except Exception as exc:
        log_event(
            log, "mind.register.failed", mind_id=mind_id, error_type=type(exc).__name__
        )
        return
    log_event(
        log, "mind.registered", mind_id=mind_id, mind_name=payload["name"],
        model=payload["model"], harness=payload["harness"],
        gateway_url=payload["gateway_url"],
    )


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
        """Set this mind's default model, durably.

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
        try:
            configuration = update_default_model(path, model)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        except OSError as exc:
            return JSONResponse(
                {"error": f"could not write runtime.yaml: {exc}"}, status_code=500
            )
        log_event(log, "mind.runtime.updated", mind_id=mind_id, default_model=model)
        return {
            "saved": True,
            "configuration": {
                k: configuration[k] for k in PUBLIC_FIELDS if k in configuration
            },
        }
