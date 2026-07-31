"""Hive admin site: graph, memory, and standing rules for the hive mind.

Auth lives in the hive itself (see auth.py) so a cloned hive is
self-contained; the auth db and secret are shared with the terminal site.
"""

import asyncio
import ipaddress
import json
import logging
import os
import re
import shlex
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
import uuid
from contextlib import asynccontextmanager, suppress
from pathlib import Path

import httpx
from fastapi import (
    Depends,
    FastAPI,
    Form,
    HTTPException,
    Request,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from auth import (
    clear_session_cookie,
    get_current_user_from_request,
    init_auth_db,
    require_auth,
    seed_first_user,
    set_session_cookie,
    verify_user_credentials,
)
from capability_install import clone_inference_proxy, inference_proxy_status
from config import BASE_DIR
from configuration_data import (
    DEFAULT_OLLAMA_BASE_URL,
    clear_capability_endpoint,
    get_capability_endpoint,
    get_edge_mind_metadata,
    internal_mind_exists,
    local_runtime_configuration,
    normalize_endpoint,
    scaffold_internal_mind,
    set_capability_endpoint,
    set_edge_mind_metadata,
)
from graph_data import get_graph_data
import mind_runtime
from hive_logging import configure_logging, install_fastapi_logging, log_event
from credentials_data import list_credentials, rotate_credential, set_github_pat
from settings_data import (
    build_mind_registry,
    hive_tools_status,
    list_downloads,
    resolve_download_path,
    save_voice_ref,
    voice_ref_status,
)

_canvas_connections: set[WebSocket] = set()
_canvas_elements: list[dict] = []
log = configure_logging("hive-control-plane")


@asynccontextmanager
async def lifespan(app: FastAPI):
    del app
    init_auth_db()
    seed_first_user()
    global _canvas_elements
    _canvas_elements = _load_canvas_state()
    log_event(log, "service.started", component="hive-control-plane")
    try:
        yield
    finally:
        log_event(log, "service.stopped", component="hive-control-plane")


app = FastAPI(
    title="Hive",
    description="Admin console for the hive mind",
    version="1.0.0",
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)
install_fastapi_logging(app, log, "hive-control-plane")

app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")


def _canvas_state_path() -> Path:
    # Absolute default, not BASE_DIR-relative: hive's Dockerfile copies src/
    # into /app/src (unlike spark_to_bloom's flat /app), so BASE_DIR.parent
    # is /app, not the container root the ./data:/data mount lands on.
    return Path(os.getenv("CANVAS_STATE_PATH", "/data/canvas_state.json"))


def _load_canvas_state() -> list[dict]:
    p = _canvas_state_path()
    try:
        if p.exists():
            with open(p) as f:
                data = json.load(f)
            return data.get("elements", []) if isinstance(data, dict) else []
    except Exception:
        pass
    return []


def _save_canvas_state() -> None:
    p = _canvas_state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w") as f:
        json.dump({"elements": _canvas_elements}, f)


_NUM_RE = re.compile(r"-?\d+(?:\.\d+)?")


def _xml_escape(s: str) -> str:
    return (
        str(s)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _canvas_bounds(elements: list[dict]) -> tuple[float, float, float, float]:
    """Bounding box across all elements. Falls back to a default frame when empty."""
    xs: list[float] = []
    ys: list[float] = []
    for el in elements:
        t = el.get("type")
        if t == "path":
            nums = [float(n) for n in _NUM_RE.findall(el.get("d", ""))]
            # path data is a flat stream of x,y pairs once commands are stripped
            xs.extend(nums[0::2])
            ys.extend(nums[1::2])
        elif t == "text":
            xs.append(float(el.get("x", 0)))
            ys.append(float(el.get("y", 0)))
        elif t == "image":
            x, y = float(el.get("x", 0)), float(el.get("y", 0))
            xs.extend([x, x + float(el.get("w", 0))])
            ys.extend([y, y + float(el.get("h", 0))])
    if not xs or not ys:
        return (0.0, 0.0, 1280.0, 800.0)
    pad = 40.0
    minx, miny = min(xs) - pad, min(ys) - pad
    w = max(xs) - min(xs) + pad * 2
    h = max(ys) - min(ys) + pad * 2
    return (minx, miny, max(w, 1.0), max(h, 1.0))


def _canvas_to_svg() -> str:
    """Compose the current board into a standalone dark-background SVG.

    This is the snapshot Skippy rasterizes (chrome --headless) so he can
    actually see what Daniel drew, since he cannot watch the live websocket.
    """
    elements = _canvas_elements
    minx, miny, w, h = _canvas_bounds(elements)
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'viewBox="{minx:.1f} {miny:.1f} {w:.1f} {h:.1f}" '
        f'width="{w:.0f}" height="{h:.0f}">',
        f'<rect x="{minx:.1f}" y="{miny:.1f}" width="{w:.1f}" height="{h:.1f}" fill="#090912"/>',
    ]
    for el in elements:
        t = el.get("type")
        color = _xml_escape(el.get("color", "#c9a84c"))
        if t == "path":
            parts.append(
                f'<path d="{_xml_escape(el.get("d", ""))}" stroke="{color}" '
                f'stroke-width="{el.get("sw", 2)}" fill="none" '
                f'stroke-linecap="round" stroke-linejoin="round"/>'
            )
        elif t == "text":
            parts.append(
                f'<text x="{el.get("x", 0)}" y="{el.get("y", 0)}" fill="{color}" '
                f'font-family="monospace" font-size="14">{_xml_escape(el.get("content", ""))}</text>'
            )
        elif t == "image":
            parts.append(
                f'<image x="{el.get("x", 0)}" y="{el.get("y", 0)}" '
                f'width="{el.get("w", 200)}" height="{el.get("h", 150)}" '
                f'href="{_xml_escape(el.get("src", ""))}" preserveAspectRatio="xMidYMid meet"/>'
            )
    parts.append("</svg>")
    return "".join(parts)


def _gateway_base_url() -> str:
    return os.getenv("GATEWAY_API_URL") or "http://hive-comms:8424"


def _lucent_base_url() -> str:
    return os.getenv("LUCENT_API_URL") or "http://hive-lucent:8424"


def _lucent_bearer_token() -> str:
    return os.getenv("LUCENT_BEARER_TOKEN", "")


def _gateway_headers() -> dict:
    token = os.getenv("COMMS_BEARER_TOKEN", "")
    if token:
        return {"Authorization": f"Bearer {token}"}
    return {}


def _asset_versions() -> dict[str, int]:
    static_dir = BASE_DIR / "static"
    return {
        path.name: int(path.stat().st_mtime_ns)
        for path in static_dir.iterdir()
        if path.is_file()
    }


def _render_template(request: Request, template_name: str, **context) -> HTMLResponse:
    context.setdefault("current_user", get_current_user_from_request(request))
    context.setdefault("asset_versions", _asset_versions())
    context["request"] = request
    return templates.TemplateResponse(request, template_name, context)


def _safe_next_path(next_path: str | None, fallback: str = "/overview") -> str:
    if next_path and next_path.startswith("/") and not next_path.startswith("//"):
        return next_path
    return fallback


def _actor_fields(user: dict | None) -> dict:
    """Stable, non-secret actor identity for mutation audit records."""
    return {
        "actor_id": user.get("id") if user else None,
        "actor_username": user.get("username") if user else None,
        "actor_type": "user" if user else "anonymous",
    }


def _login_redirect_for(request: Request) -> RedirectResponse:
    next_path = urllib.parse.quote(request.url.path, safe="/")
    return RedirectResponse(url=f"/login?next={next_path}", status_code=303)


def _gateway_json_sync(path: str, params: dict | None = None) -> dict | list:
    query = f"?{urllib.parse.urlencode(params)}" if params else ""
    url = f"{_gateway_base_url().rstrip('/')}{path}{query}"
    headers = {"Accept": "application/json", **_gateway_headers()}
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=15) as response:
        return json.loads(response.read().decode("utf-8"))


async def _gateway_json(path: str, params: dict | None = None) -> dict | list:
    try:
        return await asyncio.to_thread(_gateway_json_sync, path, params)
    except urllib.error.HTTPError as exc:
        raise HTTPException(status_code=exc.code, detail=f"Gateway request failed: {path}") from exc
    except OSError as exc:
        raise HTTPException(status_code=502, detail=f"Gateway unavailable: {exc}") from exc


def _admin_headers() -> dict:
    """The gateway's admin bearer, required by every consequential route.

    Registering a Mind, changing its row, and writing its runtime.yaml are
    all admin operations — the service token comms hands out for reads is
    rejected on all of them.
    """
    token = os.getenv("COMMS_ADMIN_BEARER_TOKEN", "")
    if not token:
        raise HTTPException(
            status_code=503,
            detail="COMMS_ADMIN_BEARER_TOKEN is not configured for this console",
        )
    return {"Authorization": f"Bearer {token}"}


async def _gateway_request(
    method: str,
    path: str,
    *,
    params: dict | None = None,
    json_body: dict | None = None,
    admin: bool = False,
) -> dict | list:
    url = f"{_gateway_base_url().rstrip('/')}{path}"
    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.request(
            method,
            url,
            params=params,
            json=json_body,
            headers=_admin_headers() if admin else _gateway_headers(),
        )
    if response.status_code >= 400:
        raise HTTPException(status_code=response.status_code, detail=response.text)
    if not response.content:
        return {}
    data = response.json()
    return data if isinstance(data, (dict, list)) else {}


def _lucent_headers() -> dict:
    token = _lucent_bearer_token()
    if not token:
        raise HTTPException(status_code=503, detail="lucent bearer token not configured")
    return {"Authorization": f"Bearer {token}"}


async def _lucent_request(
    method: str,
    path: str,
    *,
    params: dict | None = None,
    json_body: dict | None = None,
):
    url = f"{_lucent_base_url().rstrip('/')}{path}"
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.request(
            method, url, params=params, json=json_body, headers=_lucent_headers()
        )
        if resp.status_code >= 400:
            raise HTTPException(status_code=resp.status_code, detail=resp.text)
        if resp.status_code == 204 or not resp.content:
            return {}
        return resp.json()


@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    if not get_current_user_from_request(request):
        return _login_redirect_for(request)
    return RedirectResponse(url="/overview", status_code=303)


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, next: str = "/overview"):
    current_user = get_current_user_from_request(request)
    if current_user:
        return RedirectResponse(url=_safe_next_path(next), status_code=303)
    return _render_template(request, "login.html", next_path=_safe_next_path(next))


@app.post("/login")
async def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    next: str = Form("/graph"),
):
    user = verify_user_credentials(username, password)
    if not user:
        log_event(log, "auth.login.rejected", level=logging.WARNING, actor_username=username)
        return _render_template(
            request,
            "login.html",
            error="Invalid username or password.",
            next_path=_safe_next_path(next),
        )

    response = RedirectResponse(url=_safe_next_path(next), status_code=303)
    set_session_cookie(response, user)
    log_event(log, "auth.login.succeeded", **_actor_fields(user))
    return response


@app.post("/logout")
async def logout(request: Request):
    user = get_current_user_from_request(request)
    response = RedirectResponse(url="/login", status_code=303)
    clear_session_cookie(response)
    log_event(log, "auth.logout", **_actor_fields(user))
    return response


# ── Control center ─────────────────────────────────────────────────────


def _console_page(request: Request, template_name: str, **context):
    if not get_current_user_from_request(request):
        return _login_redirect_for(request)
    return _render_template(request, template_name, **context)


@app.get("/overview", response_class=HTMLResponse)
async def overview_page(request: Request):
    return _console_page(request, "overview.html")


@app.get("/setup", response_class=HTMLResponse)
async def setup_page(request: Request):
    return _console_page(request, "setup.html")


@app.get("/minds", response_class=HTMLResponse)
async def minds_page(request: Request):
    return _console_page(request, "minds.html")


# Registered before /minds/{mind_id}: FastAPI matches in declaration order,
# so the parameterised route would otherwise swallow this literal one.
@app.get("/minds/new", response_class=HTMLResponse)
async def new_mind_page(request: Request):
    return _console_page(
        request,
        "mind_new.html",
        install_options=_mind_install_options(),
        default_ollama_url=get_capability_endpoint("ollama")
        or os.getenv("OLLAMA_URL", "")
        or DEFAULT_OLLAMA_BASE_URL,
    )


@app.get("/minds/{mind_id}", response_class=HTMLResponse)
async def mind_detail_page(request: Request, mind_id: str):
    return _console_page(request, "mind_detail.html", mind_id=mind_id)


@app.get("/edge-minds", response_class=HTMLResponse)
async def edge_minds_page(request: Request):
    return _console_page(request, "edge_minds.html")


@app.get("/edge-minds/new", response_class=HTMLResponse)
async def new_edge_mind_page(request: Request):
    return _console_page(
        request,
        "edge_mind_new.html",
        edge_comms_url=os.getenv("EDGE_COMMS_URL", ""),
        edge_lucent_url=os.getenv("EDGE_LUCENT_URL", ""),
        edge_hive_tools_url=os.getenv("EDGE_HIVE_TOOLS_URL", ""),
        edge_ollama_url=get_capability_endpoint("ollama")
        or os.getenv("OLLAMA_URL", ""),
        install_options=_mind_install_options(),
    )


@app.get("/api/minds/install-options")
async def api_mind_install_options(user: dict = Depends(require_auth)):
    """Valid harness/provider/role/deployment combinations, for the install
    forms. One source of truth, so the console cannot drift from setup.sh."""
    del user
    return _mind_install_options()


@app.post("/api/minds")
async def api_create_mind(request: Request, user: dict = Depends(require_auth)):
    """Scaffold a containerised Mind in this Hive's own stack.

    Writes runtime.yaml and container/compose.yaml, then returns the commands
    that build and register it. Personality and soul stay conversational —
    this creates a runtime, not an identity.
    """
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Request body must be an object")

    name = str(body.get("name") or "").strip().lower()
    harness = str(body.get("harness") or "")
    provider = str(body.get("provider") or "")
    if not re.fullmatch(r"[a-z][a-z0-9-]{1,31}", name):
        raise HTTPException(
            status_code=400,
            detail="Name must be 2 to 32 lowercase letters, numbers, or hyphens",
        )
    try:
        _validate_harness_provider(harness, provider)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    default_model = str(
        body.get("default_model") or _PROVIDER_DEFAULT_MODELS[provider]
    ).strip()
    mind_id = str(body.get("mind_id") or uuid.uuid4())
    try:
        mind_id = str(uuid.UUID(mind_id))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Mind ID must be a UUID") from exc

    if await _mind_name_is_taken(name):
        raise HTTPException(status_code=409, detail="That Mind name is already taken")

    ollama_base_url = ""
    if provider == "ollama":
        ollama_base_url = str(
            body.get("ollama_url")
            or get_capability_endpoint("ollama")
            or os.getenv("OLLAMA_URL")
            or DEFAULT_OLLAMA_BASE_URL
        )
        ollama_base_url = normalize_endpoint(ollama_base_url)

    try:
        written = scaffold_internal_mind(
            name,
            mind_id=mind_id,
            harness=harness,
            provider=provider,
            default_model=default_model,
            description=str(body.get("description") or ""),
            ollama_base_url=ollama_base_url,
            ollama_api_key=str(body.get("ollama_api_key") or ""),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(
            status_code=500, detail=f"Could not write the Mind scaffold: {exc}"
        ) from exc

    log_event(
        log, "mind.scaffolded", **_actor_fields(user), mind_name=name,
        mind_id=mind_id, harness=harness, provider=provider,
    )
    return {
        "mind_id": mind_id,
        "written": written,
        "registration": {
            "name": name,
            "mind_id": mind_id,
            "model": default_model,
            "harness": harness,
            "provider": provider,
        },
        "commands": [
            {
                "name": "Build and start the Mind",
                "command": (
                    "cd $HIVE_MIND_DIR && /generate-compose && "
                    f"docker compose up -d --build {shlex.quote(name)}"
                ),
            }
        ],
        # The soul is authored in conversation, not here — see setup-personality.
        "next_step": (
            f"Give {name} a soul: run /hivemind:setup-personality, then talk to it."
        ),
    }


@app.post("/api/minds/{name}/register")
async def api_register_mind(
    name: str, request: Request, user: dict = Depends(require_auth)
):
    """Register a scaffolded Mind with the broker once its container is up."""
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Request body must be an object")
    normalized = name.strip().lower()
    if not re.fullmatch(r"[a-z][a-z0-9-]{1,31}", normalized):
        raise HTTPException(status_code=400, detail="Invalid Mind name")
    try:
        configuration = local_runtime_configuration(normalized)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    broker_result = await _gateway_request(
        "POST",
        "/broker/minds",
        json_body={
            "mind_id": str(configuration.get("mind_id") or ""),
            "name": normalized,
            "gateway_url": str(configuration.get("gateway_url") or ""),
            "model": str(configuration.get("default_model") or ""),
            "harness": str(configuration.get("harness") or ""),
        },
    )
    log_event(
        log, "mind.registered", **_actor_fields(user), mind_name=normalized,
        mind_id=str(configuration.get("mind_id") or ""),
    )
    return {"registered": True, "broker": broker_result}


@app.get("/capability-nodes", response_class=HTMLResponse)
async def capability_nodes_page(request: Request):
    return _console_page(request, "capability_nodes.html")


@app.get("/outposts", include_in_schema=False)
@app.get("/outposts/new", include_in_schema=False)
async def legacy_outpost_page(request: Request):
    if not get_current_user_from_request(request):
        return _login_redirect_for(request)
    target = "/edge-minds/new" if request.url.path.endswith("/new") else "/edge-minds"
    return RedirectResponse(target, status_code=308)


@app.get("/tools", include_in_schema=False)
async def legacy_tools_page(request: Request):
    if not get_current_user_from_request(request):
        return _login_redirect_for(request)
    return RedirectResponse("/capability-nodes", status_code=308)


@app.get("/activity", response_class=HTMLResponse)
async def activity_page(request: Request):
    return _console_page(request, "activity.html")


@app.get("/training", response_class=HTMLResponse)
async def training_page(request: Request):
    return _console_page(request, "training.html")


@app.get("/api/overview")
async def api_overview(user: dict = Depends(require_auth)):
    del user
    minds = await _broker_minds_safe()
    try:
        sessions_raw = await _gateway_json("/sessions")
        sessions = sessions_raw if isinstance(sessions_raw, list) else []
    except Exception:
        sessions = []
    comms_health, lucent_health = await asyncio.gather(
        _service_health(
            f"{_gateway_base_url().rstrip('/')}/health",
            headers=_gateway_headers(),
            service_name="Hive Comms",
            repair_action="Verify COMMS_BEARER_TOKEN and the Hive Comms endpoint, then retry.",
        ),
        _service_health(
            f"{_lucent_base_url().rstrip('/')}/health",
            headers={"Authorization": f"Bearer {_lucent_bearer_token()}"}
            if _lucent_bearer_token()
            else {},
            service_name="Lucent",
            repair_action="Verify LUCENT_BEARER_TOKEN and the Lucent endpoint, then retry.",
        ),
    )
    registry = _add_session_activity(build_mind_registry(minds), sessions)
    sessions = _add_session_mind_names(sessions, registry)
    capability_data = await _capability_node_data()
    tools_node = next(
        node for node in capability_data["nodes"] if node["id"] == "hive-tools"
    )
    edge_minds = _edge_mind_rows(registry)
    active_sessions = [
        session for session in sessions if session.get("status") in {"active", "running"}
    ]
    return {
        "counts": {
            "minds": len(minds),
            "edge_minds": len(edge_minds),
            "capability_nodes": capability_data["summary"]["configured"],
            "sessions": len(active_sessions),
        },
        "minds": registry,
        "edge_minds": edge_minds,
        "capability_nodes": capability_data["nodes"],
        "sessions": sessions,
        "services": {
            "comms": comms_health,
            "lucent": lucent_health,
            "tools": {
                "installed": tools_node["installed"],
                **tools_node["health"],
            },
        },
    }


def _edge_mind_rows(registry: list[dict]) -> list[dict]:
    rows = []
    profile_overrides = _edge_mind_profile_overrides()
    name_overrides = _edge_mind_name_overrides() | set(profile_overrides)
    for mind in registry:
        local_value = mind.get("local_config")
        local = local_value if isinstance(local_value, dict) else {}
        config = local.get("config") or {}
        name = str(mind.get("name") or "")
        stored = get_edge_mind_metadata(name) or {}
        gateway_url = str(mind.get("gateway_url") or "")
        parsed = urllib.parse.urlparse(gateway_url)
        external_address = False
        if not local and parsed.hostname:
            with suppress(ValueError):
                address = ipaddress.ip_address(parsed.hostname)
                external_address = not address.is_loopback and not address.is_unspecified
        is_edge_mind = (
            bool(config.get("remote"))
            or bool(config.get("role"))
            or bool(config.get("deployment"))
            or name.lower() in name_overrides
            or external_address
        )
        if not is_edge_mind:
            continue
        profile = str(
            config.get("profile")
            or stored.get("profile")
            or profile_overrides.get(name.lower())
            or "standard"
        )
        if profile not in _EDGE_MIND_PROFILES:
            profile = "standard"
        rows.append(
            {
                "name": name or "Unnamed",
                "id": mind.get("id") or "",
                "host": parsed.hostname or "Remote machine",
                "gateway_url": gateway_url,
                "profile": profile,
                "role": config.get("role") or stored.get("role") or "Connected",
                "deployment": config.get("deployment")
                or stored.get("deployment")
                or "Remote",
                "harness": mind.get("harness") or config.get("harness") or "Unknown",
                "model": mind.get("model") or config.get("default_model") or "Unknown",
                "last_seen": mind.get("last_seen"),
                "last_activity": mind.get("last_activity"),
                "activity_status": mind.get("activity_status") or "registered",
            }
        )
    return sorted(rows, key=lambda row: str(row["name"]).lower())


@app.get("/api/outposts")
async def legacy_api_outposts(user: dict = Depends(require_auth)):
    del user
    return RedirectResponse("/api/edge-minds", status_code=307)


@app.get("/api/edge-minds")
async def api_edge_minds(user: dict = Depends(require_auth)):
    del user
    registry = build_mind_registry(await _broker_minds_safe())
    try:
        sessions_raw = await _gateway_json("/sessions")
        sessions = sessions_raw if isinstance(sessions_raw, list) else []
    except Exception:
        sessions = []
    registry = _add_session_activity(registry, sessions)
    return {"edge_minds": _edge_mind_rows(registry)}


_EDGE_MIND_PROFILES = {"standard", "sentinel"}
_EDGE_MIND_BOOTSTRAP_PROFILES = {"standard"}
# `sandboxed` is the word setup.sh accepts. The console said `satellite`, so
# every satellite bootstrap it generated died on setup.sh's role check.
_EDGE_MIND_ROLES = {"operator", "sandboxed"}
_EDGE_MIND_DEPLOYMENTS = {"systemd", "container", "windows-task"}

# Harness and provider are separate axes, matching mind_templates/ and
# minds/<name>/runtime.yaml. The harness picks the CLI; the provider picks
# where inference goes. Each CLI speaks one first-party dialect, and ollama is
# the alternative both share.
_MIND_HARNESSES = {"claude_cli", "codex_cli"}
_MIND_PROVIDERS = {"anthropic", "openai", "ollama"}
_HARNESS_PROVIDERS = {
    "claude_cli": ("anthropic", "ollama"),
    "codex_cli": ("openai", "ollama"),
}
_PROVIDER_DEFAULT_MODELS = {
    "anthropic": "sonnet",
    "openai": "gpt-5.4",
    "ollama": "qwen3:8b",
}
_EDGE_MIND_SURFACES = {"telegram", "discord"}


def _mind_install_options() -> dict:
    """The one source of truth the install forms render from.

    Hardcoding these in the template is how the console drifted out of step
    with setup.sh in the first place.
    """
    return {
        "harnesses": [
            {
                "value": harness,
                "label": "Claude CLI" if harness == "claude_cli" else "Codex CLI",
                "providers": list(providers),
            }
            for harness, providers in sorted(_HARNESS_PROVIDERS.items())
        ],
        "providers": [
            {"value": provider, "default_model": _PROVIDER_DEFAULT_MODELS[provider]}
            for provider in sorted(_MIND_PROVIDERS)
        ],
        "roles": sorted(_EDGE_MIND_ROLES),
        "deployments": sorted(_EDGE_MIND_DEPLOYMENTS),
        "surfaces": sorted(_EDGE_MIND_SURFACES),
    }


def _validate_harness_provider(harness: str, provider: str) -> None:
    if harness not in _MIND_HARNESSES:
        raise ValueError("Unknown harness")
    if provider not in _MIND_PROVIDERS:
        raise ValueError("Unknown provider")
    if provider not in _HARNESS_PROVIDERS[harness]:
        allowed = " or ".join(_HARNESS_PROVIDERS[harness])
        raise ValueError(f"{harness} cannot use the {provider} provider (want {allowed})")


def _edge_mind_name_overrides() -> set[str]:
    return {
        name.strip().lower()
        for name in os.getenv("EDGE_MIND_NAMES", "").split(",")
        if name.strip()
    }


def _edge_mind_profile_overrides() -> dict[str, str]:
    overrides = {}
    for item in os.getenv("EDGE_MIND_PROFILES", "").split(","):
        name, separator, profile = item.strip().partition(":")
        if not separator:
            name, separator, profile = item.strip().partition("=")
        normalized_profile = profile.strip().lower()
        if separator and name.strip() and normalized_profile in _EDGE_MIND_PROFILES:
            overrides[name.strip().lower()] = normalized_profile
    return overrides

_EDGE_MIND_BUNDLES = {
    "standard": [
        {
            "name": "Edge Mind runtime",
            "kind": "runtime",
            "description": "One mind runtime, its harness, and selected conversation surfaces.",
        }
    ],
}


def _build_edge_mind_bootstrap(body: dict) -> dict:
    name = str(body.get("name") or "").strip().lower()
    profile = str(body.get("profile") or "standard")
    role = str(body.get("role") or "")
    deployment = str(body.get("deployment") or "")
    harness = str(body.get("harness") or "")
    provider = str(body.get("provider") or "")
    surfaces = body.get("surfaces") or []
    raw_mind_id = str(body.get("mind_id") or uuid.uuid4())
    try:
        port = int(body.get("port", 8421))
    except (TypeError, ValueError) as exc:
        raise ValueError("Port must be a number") from exc
    if not re.fullmatch(r"[a-z][a-z0-9-]{1,31}", name):
        raise ValueError("Name must be 2 to 32 lowercase letters, numbers, or hyphens")
    if profile not in _EDGE_MIND_BOOTSTRAP_PROFILES:
        raise ValueError("Sentinel profile installation is not automated yet")
    if role not in _EDGE_MIND_ROLES:
        raise ValueError("Unknown Edge Mind role")
    if deployment not in _EDGE_MIND_DEPLOYMENTS:
        raise ValueError("Unknown deployment type")
    _validate_harness_provider(harness, provider)
    default_model = str(
        body.get("default_model") or _PROVIDER_DEFAULT_MODELS[provider]
    ).strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}", default_model):
        raise ValueError("Invalid default model")
    if not isinstance(surfaces, list) or any(
        item not in _EDGE_MIND_SURFACES for item in surfaces
    ):
        raise ValueError("Unknown conversation surface")
    if port < 1024 or port > 65535:
        raise ValueError("Port must be between 1024 and 65535")
    try:
        mind_id = str(uuid.UUID(raw_mind_id))
    except ValueError as exc:
        raise ValueError("Mind ID must be a UUID") from exc
    endpoint_settings = {
        "COMMS_URL": str(body.get("comms_url") or os.getenv("EDGE_COMMS_URL") or ""),
        "LUCENT_URL_SELF": str(
            body.get("lucent_url") or os.getenv("EDGE_LUCENT_URL") or ""
        ),
        "HIVE_TOOLS_URL": str(
            body.get("hive_tools_url") or os.getenv("EDGE_HIVE_TOOLS_URL") or ""
        ),
    }
    for key, value in endpoint_settings.items():
        if value:
            endpoint_settings[key] = normalize_endpoint(value)
    ollama_url = ""
    if provider == "ollama":
        ollama_url = str(
            body.get("ollama_url")
            or os.getenv("OLLAMA_URL")
            or get_capability_endpoint("ollama")
            or ""
        )
        if not ollama_url:
            raise ValueError("Ollama URL is required for the Ollama provider")
        ollama_url = normalize_endpoint(ollama_url)
    args = [
        "./setup.sh",
        "--name",
        name,
        "--profile",
        profile,
        "--role",
        role,
        "--deployment",
        deployment,
        "--harness",
        harness,
        "--provider",
        provider,
        "--surfaces",
        ",".join(surfaces) or "none",
        "--port",
        str(port),
    ]
    setup_command = " ".join(shlex.quote(part) for part in args)
    environment_commands = [
        "cp .env.example .env",
        f"sed -i {shlex.quote(f's|^MIND_ID=.*|MIND_ID={mind_id}|')} .env",
    ]
    for key, value in endpoint_settings.items():
        if value:
            environment_commands.append(
                f"sed -i {shlex.quote(f's|^{key}=.*|{key}={value}|')} .env"
            )
    if endpoint_settings["COMMS_URL"]:
        environment_commands.append(
            "printf '%s\\n' "
            + shlex.quote(
                f"HIVE_MIND_SERVER_URL={endpoint_settings['COMMS_URL']}"
            )
            + " >> .env"
        )
    prepare_environment = " && ".join(environment_commands)
    runtime_commands = [
        setup_command,
        "sed -i "
        + shlex.quote(f"s|^default_model:.*|default_model: {default_model}|")
        + f" minds/{shlex.quote(name)}/runtime.yaml",
        # No provider sed: setup.sh --provider already wrote it. Only the
        # model needs overriding, since setup.sh picks a per-provider default.
        "sed -i "
        + shlex.quote(f"s|^default_model:.*|default_model: {default_model}|")
        + " config.yaml",
    ]
    if ollama_url:
        runtime_commands.append(
            "sed -i "
            + shlex.quote(
                f"s|http://your-ollama-host:11434|{ollama_url}|g"
            )
            + " config.yaml"
        )
    configure_runtime = " && ".join(runtime_commands)
    commands = [
        {
            "name": "Install Edge Mind runtime",
            "command": (
                "git clone https://github.com/danielstewart77/hive-edge-minds.git && "
                f"cd hive-edge-minds && {prepare_environment} && {configure_runtime}"
            ),
        }
    ]
    return {
        "mind_id": mind_id,
        "profile": profile,
        "command": commands[0]["command"],
        "commands": commands,
        "components": _EDGE_MIND_BUNDLES[profile],
        "required_settings": {
            key: value or None for key, value in endpoint_settings.items()
        },
        "registration": {
            "name": name,
            "mind_id": mind_id,
            "model": default_model,
            "harness": harness,
            "provider": provider,
            "profile": profile,
            "role": role,
            "deployment": deployment,
        },
    }


@app.post("/api/outposts/bootstrap")
async def legacy_api_outpost_bootstrap(
    request: Request, user: dict = Depends(require_auth)
):
    del request, user
    return RedirectResponse("/api/edge-minds/bootstrap", status_code=307)


@app.post("/api/edge-minds/bootstrap")
async def api_edge_mind_bootstrap(request: Request, user: dict = Depends(require_auth)):
    try:
        body = await request.json()
        result = _build_edge_mind_bootstrap(body if isinstance(body, dict) else {})
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    log_event(
        log, "edge_mind.bootstrap.created", **_actor_fields(user),
        mind_name=result["registration"]["name"], mind_id=result["registration"]["mind_id"],
        deployment=result["registration"]["deployment"],
    )
    return result


async def _probe_edge_mind(gateway_url: str) -> dict:
    endpoint = normalize_endpoint(gateway_url)
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            response = await client.get(f"{endpoint}/health")
        response.raise_for_status()
        data = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(status_code=502, detail=f"Edge Mind health check failed: {exc}") from exc
    if not isinstance(data, dict):
        raise HTTPException(status_code=502, detail="Edge Mind returned invalid health data")
    return data


@app.post("/api/edge-minds/verify")
async def api_edge_mind_verify(request: Request, user: dict = Depends(require_auth)):
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Request body must be an object")
    name = str(body.get("name") or "").strip().lower()
    mind_id = str(body.get("mind_id") or "")
    gateway_url = str(body.get("gateway_url") or "")
    model = str(body.get("model") or "")
    harness = str(body.get("harness") or "")
    provider = str(body.get("provider") or "")
    profile = str(body.get("profile") or "")
    role = str(body.get("role") or "")
    deployment = str(body.get("deployment") or "")
    if not re.fullmatch(r"[a-z][a-z0-9-]{1,31}", name):
        raise HTTPException(status_code=400, detail="Invalid Mind name")
    try:
        mind_id = str(uuid.UUID(mind_id))
        gateway_url = normalize_endpoint(gateway_url)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}", model):
        raise HTTPException(status_code=400, detail="Invalid model name")
    try:
        _validate_harness_provider(harness, provider)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if profile not in _EDGE_MIND_PROFILES:
        raise HTTPException(status_code=400, detail="Unknown profile")
    if role not in _EDGE_MIND_ROLES or deployment not in _EDGE_MIND_DEPLOYMENTS:
        raise HTTPException(status_code=400, detail="Invalid deployment metadata")
    health = await _probe_edge_mind(gateway_url)
    if str(health.get("mind_id") or "") != mind_id:
        raise HTTPException(
            status_code=400,
            detail="The responding Edge Mind identity does not match this deployment plan",
        )
    if health.get("status") != "ok":
        raise HTTPException(
            status_code=502,
            detail="The responding Edge Mind is not ready for registration",
        )
    existing = next(
        (
            mind
            for mind in await _broker_minds_safe()
            if str(mind.get("name") or "").lower() == name
        ),
        None,
    )
    if existing and str(existing.get("id") or "") != mind_id:
        raise HTTPException(status_code=409, detail="That Mind name is already registered")
    broker_result = await _gateway_request(
        "POST",
        "/broker/minds",
        json_body={
            "mind_id": mind_id,
            "name": name,
            "gateway_url": gateway_url,
            "model": model,
            # Already the broker's vocabulary — no collapsing step needed now
            # that the console and the edge repo agree on the harness names.
            "harness": harness,
        },
    )
    set_edge_mind_metadata(
        name,
        profile=profile,
        role=role,
        deployment=deployment,
    )
    log_event(
        log, "edge_mind.registered", **_actor_fields(user), mind_name=name,
        mind_id=mind_id, deployment=deployment, profile=profile, role=role,
    )
    return {"registered": True, "health": health, "broker": broker_result}


@app.get("/api/tools")
async def api_tools(user: dict = Depends(require_auth)):
    del user
    data = await _capability_node_data()
    node = next(item for item in data["nodes"] if item["id"] == "hive-tools")
    return {
        "present": node["installed"],
        "path": node.get("path"),
        "capabilities": node.get("capabilities", []),
        "health": node["health"],
    }


@app.get("/api/capability-nodes")
async def api_capability_nodes(user: dict = Depends(require_auth)):
    del user
    return await _capability_node_data()


def _find_capability_node(data: dict, node_id: str) -> dict:
    node = next((item for item in data["nodes"] if item["id"] == node_id), None)
    if node is None:
        raise HTTPException(status_code=404, detail="Unknown Capability Node")
    return node


@app.put("/api/capability-nodes/{node_id}/configuration")
async def api_capability_node_configure(
    node_id: str, request: Request, user: dict = Depends(require_auth)
):
    body = await request.json()
    try:
        endpoint = set_capability_endpoint(
            node_id, str(body.get("endpoint") if isinstance(body, dict) else "")
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    data = await _capability_node_data()
    log_event(
        log, "capability_node.configured", **_actor_fields(user),
        node_id=node_id, endpoint=endpoint,
    )
    return {"endpoint": endpoint, "node": _find_capability_node(data, node_id)}


@app.delete("/api/capability-nodes/{node_id}/configuration")
async def api_capability_node_configuration_delete(
    node_id: str, user: dict = Depends(require_auth)
):
    try:
        removed = clear_capability_endpoint(node_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    log_event(
        log, "capability_node.configuration.cleared", **_actor_fields(user),
        node_id=node_id, removed=removed,
    )
    return {"removed": removed}


@app.post("/api/capability-nodes/inference-proxy/install")
async def api_inference_proxy_install(user: dict = Depends(require_auth)):
    del user
    try:
        result = await asyncio.to_thread(clone_inference_proxy)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return result


def _ollama_endpoint() -> str:
    endpoint = get_capability_endpoint("ollama") or os.getenv("OLLAMA_URL") or os.getenv(
        "OLLAMA_BASE_URL"
    )
    if not endpoint:
        raise HTTPException(status_code=409, detail="Configure the Ollama endpoint first")
    try:
        return normalize_endpoint(endpoint)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


async def _ollama_request(
    method: str, path: str, *, json_body: dict | None = None
) -> dict:
    timeout = 1800.0 if path == "/api/pull" else 30.0
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.request(
                method,
                f"{_ollama_endpoint()}{path}",
                json=json_body,
            )
        response.raise_for_status()
        data = response.json() if response.content else {}
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(status_code=502, detail=f"Ollama request failed: {exc}") from exc
    return data if isinstance(data, dict) else {}


def _validated_model_name(body: object) -> str:
    model = str(body.get("model") if isinstance(body, dict) else "").strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}", model):
        raise HTTPException(status_code=400, detail="Invalid model name")
    return model


@app.get("/api/capability-nodes/ollama/models")
async def api_ollama_models(user: dict = Depends(require_auth)):
    del user
    version, installed, running = await asyncio.gather(
        _ollama_request("GET", "/api/version"),
        _ollama_request("GET", "/api/tags"),
        _ollama_request("GET", "/api/ps"),
    )
    return {
        "version": version.get("version"),
        "models": installed.get("models", []),
        "running": running.get("models", []),
    }


@app.post("/api/capability-nodes/ollama/models/pull")
async def api_ollama_model_pull(
    request: Request, user: dict = Depends(require_auth)
):
    model = _validated_model_name(await request.json())
    result = await _ollama_request(
        "POST", "/api/pull", json_body={"model": model, "stream": False}
    )
    log_event(log, "ollama.model.pulled", **_actor_fields(user), model=model)
    return {"model": model, "result": result}


@app.delete("/api/capability-nodes/ollama/models")
async def api_ollama_model_delete(
    request: Request, user: dict = Depends(require_auth)
):
    model = _validated_model_name(await request.json())
    result = await _ollama_request("DELETE", "/api/delete", json_body={"model": model})
    log_event(log, "ollama.model.deleted", **_actor_fields(user), model=model)
    return {"model": model, "result": result}


# ── Graph ──────────────────────────────────────────────────────────────


@app.get("/graph/data")
async def graph_data(limit: int = 400, user: dict = Depends(require_auth)):
    del user
    return get_graph_data(_lucent_base_url(), limit=limit, bearer_token=_lucent_bearer_token())


@app.get("/graph/public-data")
async def graph_public_data(limit: int = 400, user: dict = Depends(require_auth)):
    del user
    data = get_graph_data(_lucent_base_url(), limit=limit, bearer_token=_lucent_bearer_token())
    public_nodes = [n for n in data.get("nodes", []) if n.get("type") != "Person"]
    public_ids = {n["id"] for n in public_nodes}
    public_edges = [
        e for e in data.get("edges", [])
        if e.get("source") in public_ids and e.get("target") in public_ids
    ]
    return {"nodes": public_nodes, "edges": public_edges}


@app.get("/graph", response_class=HTMLResponse)
async def graph(request: Request):
    if not get_current_user_from_request(request):
        return _login_redirect_for(request)
    return _render_template(request, "graph.html")


# ── Memory ─────────────────────────────────────────────────────────────

_TABLE_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_MEMORY_ROW_LIMIT_DEFAULT = 200
_MEMORY_ROW_LIMIT_MAX = 1000


def _lucent_db_path() -> str:
    return os.getenv("LUCENT_DB_PATH", "/data/lucent.db")


def _open_lucent_readonly() -> sqlite3.Connection:
    path = _lucent_db_path()
    if not os.path.exists(path):
        raise HTTPException(status_code=503, detail="lucent database not mounted")
    uri = f"file:{path}?mode=ro&immutable=0"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _memory_cell_for_json(value):
    if isinstance(value, (bytes, bytearray, memoryview)):
        return f"<BLOB {len(bytes(value))} bytes>"
    return value


def _list_lucent_tables(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    return [r["name"] for r in rows]


@app.get("/memory", response_class=HTMLResponse)
async def memory_page(request: Request):
    if not get_current_user_from_request(request):
        return _login_redirect_for(request)
    return _render_template(request, "memory.html")


@app.get("/api/memory/tables")
async def api_memory_tables(user: dict = Depends(require_auth)):
    del user
    conn = _open_lucent_readonly()
    try:
        tables = _list_lucent_tables(conn)
        results = []
        for name in tables:
            count_row = conn.execute(f'SELECT COUNT(*) AS c FROM "{name}"').fetchone()
            results.append({"name": name, "row_count": int(count_row["c"])})
    finally:
        conn.close()
    return {"tables": results}


@app.get("/api/memory/rows")
async def api_memory_rows(
    table: str,
    limit: int = _MEMORY_ROW_LIMIT_DEFAULT,
    offset: int = 0,
    user: dict = Depends(require_auth),
):
    del user
    if not _TABLE_NAME_RE.match(table):
        raise HTTPException(status_code=404, detail="table not found")
    safe_limit = max(1, min(int(limit), _MEMORY_ROW_LIMIT_MAX))
    safe_offset = max(0, int(offset))
    conn = _open_lucent_readonly()
    try:
        if table not in _list_lucent_tables(conn):
            raise HTTPException(status_code=404, detail="table not found")
        columns = [
            r["name"]
            for r in conn.execute(f'PRAGMA table_info("{table}")').fetchall()
        ]
        total = int(conn.execute(f'SELECT COUNT(*) AS c FROM "{table}"').fetchone()["c"])
        rows = [
            {col: _memory_cell_for_json(row[col]) for col in columns}
            for row in conn.execute(
                f'SELECT * FROM "{table}" LIMIT ? OFFSET ?',
                (safe_limit, safe_offset),
            ).fetchall()
        ]
    finally:
        conn.close()
    return {
        "table": table,
        "columns": columns,
        "rows": rows,
        "total": total,
        "limit": safe_limit,
        "offset": safe_offset,
    }


# ── Standing rules ─────────────────────────────────────────────────────

_RULE_SOURCES = {"always-remember", "user"}


def _normalize_rule_row(row: dict, alias_index: dict) -> dict:
    raw_mind = row.get("mind_id") or ""
    entry = alias_index.get(raw_mind)
    display = entry["name"] if entry else raw_mind
    return {
        "id": row.get("id"),
        "content": row.get("content") or "",
        "mind_id": raw_mind,
        "mind_display": display,
        "tier": row.get("tier") or "",
        "source": row.get("source") or "",
        "data_class": row.get("data_class") or "",
        "tags": row.get("tags") or "",
        "created_at": row.get("created_at"),
    }


async def _broker_minds_safe() -> list[dict]:
    try:
        raw = await _gateway_json("/broker/minds")
    except Exception:
        return []
    if not isinstance(raw, list):
        return []
    return [m for m in raw if isinstance(m, dict)]


def _build_alias_index(minds: list[dict]) -> dict:
    """Map every alias (UUID and short name) to a canonical mind entry.

    Lucent rows historically stamp `mind_id` with either the UUID or the
    short name, depending on which write path created them. This index lets
    the rules dashboard treat them as the same mind for filtering and
    display.
    """
    index: dict = {}
    for m in minds:
        uuid = (m.get("id") or "").strip()
        name = (m.get("name") or "").strip()
        if not uuid and not name:
            continue
        entry = {"uuid": uuid, "name": name or uuid, "aliases": [a for a in (uuid, name) if a]}
        for alias in entry["aliases"]:
            index[alias] = entry
    index["shared"] = {"uuid": "shared", "name": "shared", "aliases": ["shared"]}
    return index


async def _fetch_rules_for_mind(
    tier: str, mind_id: str | None, alias_index: dict
) -> list[dict]:
    aliases: list[str | None] = [None]
    if mind_id and mind_id != "all":
        entry = alias_index.get(mind_id)
        aliases = entry["aliases"] if entry else [mind_id]
    seen_ids: set = set()
    out: list[dict] = []
    for alias in aliases:
        params: dict = {"tier": tier, "limit": 100, "offset": 0}
        if alias is not None:
            params["mind_id"] = alias
        fetched = 0
        while True:
            data = await _lucent_request("GET", "/memory/list", params=params)
            entries = data.get("entries") if isinstance(data, dict) else []
            if not entries:
                break
            for e in entries:
                if e.get("source") not in _RULE_SOURCES:
                    continue
                rid = e.get("id")
                if rid in seen_ids:
                    continue
                seen_ids.add(rid)
                out.append(_normalize_rule_row(e, alias_index))
            fetched += len(entries)
            total = int(data.get("total") or 0)
            if fetched >= total or fetched >= 1000:
                break
            params["offset"] = fetched
    return out


@app.get("/rules", response_class=HTMLResponse)
async def rules_page(request: Request):
    if not get_current_user_from_request(request):
        return _login_redirect_for(request)
    minds = await _broker_minds_safe()
    alias_to_canonical: dict[str, str] = {"shared": "shared"}
    for m in minds:
        uuid = (m.get("id") or "").strip()
        name = (m.get("name") or "").strip()
        canonical = uuid or name
        if uuid:
            alias_to_canonical[uuid] = canonical
        if name:
            alias_to_canonical[name] = canonical
    return _render_template(
        request,
        "rules.html",
        minds=minds,
        alias_to_canonical_json=json.dumps(alias_to_canonical),
    )


@app.get("/api/rules")
async def api_rules_list(
    mind_id: str | None = None,
    tier: str | None = None,
    user: dict = Depends(require_auth),
):
    del user
    tiers = ["standing", "contextual"] if not tier or tier == "all" else [tier]
    minds = await _broker_minds_safe()
    alias_index = _build_alias_index(minds)
    rows: list[dict] = []
    seen_ids: set = set()
    for t in tiers:
        for row in await _fetch_rules_for_mind(t, mind_id, alias_index):
            if row["id"] in seen_ids:
                continue
            seen_ids.add(row["id"])
            rows.append(row)
    rows.sort(key=lambda r: (r["tier"] != "standing", -(r["created_at"] or 0)))
    return {"rules": rows, "count": len(rows)}


@app.post("/api/rules")
async def api_rules_create(request: Request, user: dict = Depends(require_auth)):
    body = await request.json()
    content = (body.get("content") or "").strip()
    mind_id = (body.get("mind_id") or "").strip()
    tier = (body.get("tier") or "").strip()
    data_class = (body.get("data_class") or "feedback").strip()
    tags = (body.get("tags") or "").strip()
    if not content:
        raise HTTPException(status_code=400, detail="content is required")
    if not mind_id:
        raise HTTPException(status_code=400, detail="mind_id is required")
    if tier not in {"standing", "contextual"}:
        raise HTTPException(status_code=400, detail="tier must be standing or contextual")
    source = "always-remember" if tier == "standing" else "user"
    payload = {
        "content": content,
        "mind_id": mind_id,
        "tier": tier,
        "source": source,
        "data_class": data_class,
        "tags": tags,
    }
    result = await _lucent_request("POST", "/memory/store", json_body=payload)
    log_event(
        log, "rule.created", **_actor_fields(user), rule_id=result.get("id"),
        mind_id=mind_id, tier=tier, data_class=data_class, content_chars=len(content),
    )
    return result


@app.put("/api/rules/{rule_id}")
async def api_rules_update(rule_id: str, request: Request, user: dict = Depends(require_auth)):
    body = await request.json()
    content = (body.get("content") or "").strip()
    tags = (body.get("tags") or "").strip()
    new_mind = (body.get("mind_id") or "").strip()
    if not content:
        raise HTTPException(status_code=400, detail="content is required")
    # Intentionally never forward data_class on update: lucent's memory_update
    # rewrites `tier` from DATA_CLASS_REGISTRY whenever data_class is set,
    # which would silently demote standing rules to contextual.
    payload: dict = {"content": content, "tags": tags}
    if new_mind:
        alias_index = _build_alias_index(await _broker_minds_safe())
        entry = alias_index.get(new_mind)
        if entry:
            payload["mind_id"] = entry["uuid"] or entry["name"]
        elif new_mind == "shared":
            payload["mind_id"] = "shared"
        else:
            raise HTTPException(status_code=400, detail=f"unknown mind: {new_mind}")
    result = await _lucent_request("PUT", f"/memory/{rule_id}", json_body=payload)
    log_event(
        log, "rule.updated", **_actor_fields(user), rule_id=rule_id,
        mind_id=payload.get("mind_id"), content_chars=len(content),
    )
    return result


@app.delete("/api/rules/{rule_id}")
async def api_rules_delete(rule_id: str, user: dict = Depends(require_auth)):
    result = await _lucent_request("DELETE", f"/memory/{rule_id}")
    log_event(log, "rule.deleted", **_actor_fields(user), rule_id=rule_id)
    return result


# ── Canvas ─────────────────────────────────────────────────────────────

_CANVAS_KEYS = {
    "path": ("type", "id", "color", "d", "sw"),
    "text": ("type", "id", "x", "y", "content", "color"),
    "image": ("type", "id", "x", "y", "w", "h", "src"),
}


def _apply_canvas_message(data: dict) -> bool:
    """Apply one canvas mutation to in-memory state and persist it.

    Shared by the websocket surface and the bearer-guarded /canvas/push
    endpoint so the two paths can never drift. Mutates ``data`` in place to
    fill a missing id. Returns True when a known message type was handled.
    """
    msg_type = data.get("type")
    if msg_type == "clear":
        _canvas_elements.clear()
    elif msg_type in _CANVAS_KEYS:
        data.setdefault("id", str(uuid.uuid4()))
        _canvas_elements.append({k: data[k] for k in _CANVAS_KEYS[msg_type] if k in data})
    elif msg_type == "move":
        el_id = data.get("id")
        nx, ny = data.get("x"), data.get("y")
        for el in _canvas_elements:
            if el.get("id") == el_id and el.get("type") in ("text", "image"):
                el["x"] = nx
                el["y"] = ny
                break
    elif msg_type == "delete":
        el_id = data.get("id")
        _canvas_elements[:] = [e for e in _canvas_elements if e.get("id") != el_id]
    else:
        return False
    _save_canvas_state()
    return True


async def _broadcast_canvas(data: dict, exclude: WebSocket | None = None) -> None:
    for conn in list(_canvas_connections):
        if conn is exclude:
            continue
        try:
            await conn.send_json(data)
        except Exception:
            _canvas_connections.discard(conn)


def _canvas_push_token() -> str:
    """Bearer that authorizes Skippy's /canvas/push writes.

    Prefers a dedicated CANVAS_PUSH_TOKEN; falls back to the house
    COMMS_BEARER_TOKEN, which both this container and Skippy already hold,
    so drawing works without recreating the container.
    """
    return os.getenv("CANVAS_PUSH_TOKEN") or os.getenv("COMMS_BEARER_TOKEN") or ""


@app.post("/canvas/push")
async def canvas_push(request: Request):
    expected = _canvas_push_token()
    if not expected:
        raise HTTPException(status_code=503, detail="canvas push token not configured")
    if request.headers.get("Authorization", "") != f"Bearer {expected}":
        raise HTTPException(status_code=401, detail="unauthorized")
    data = await request.json()
    if not isinstance(data, dict) or not _apply_canvas_message(data):
        raise HTTPException(status_code=400, detail="unknown or malformed canvas message")
    await _broadcast_canvas(data)
    log_event(
        log, "canvas.changed", actor_type="mind", actor_username="canvas-push",
        operation=data.get("type"), element_id=data.get("id"),
    )
    return {"ok": True, "id": data.get("id"), "type": data.get("type")}


@app.get("/canvas/render.svg")
async def canvas_render_svg():
    """Standalone SVG snapshot of the board, for Skippy to rasterize and read."""
    return Response(content=_canvas_to_svg(), media_type="image/svg+xml")


@app.post("/canvas/submit")
async def canvas_submit(request: Request):
    """Daniel's 'I'm done drawing' poke. Fires a Skippy turn via the broker."""
    user = get_current_user_from_request(request)
    if not user:
        raise HTTPException(status_code=401, detail="unauthorized")
    try:
        body = await request.json()
    except Exception:
        body = {}
    note = (body or {}).get("note", "") if isinstance(body, dict) else ""
    element_count = len(_canvas_elements)
    content = (
        "Daniel finished a drawing pass on the hive whiteboard and wants you "
        f"to look. The board currently has {element_count} element(s). Rasterize "
        "GET /canvas/render.svg and respond on Telegram with what you see."
    )
    if note:
        content += f" Daniel's note: {note}"

    target_name = os.getenv("MIND_NAME", "skippy")
    headers = _gateway_headers()
    headers["Content-Type"] = "application/json"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            # The broker keys minds on their UUID (mind_id), not the display
            # name, so resolve the id from the registry before dispatching.
            minds_resp = await client.get(
                f"{_gateway_base_url()}/broker/minds", headers=headers
            )
            minds_resp.raise_for_status()
            minds = minds_resp.json()
            mind_id = next(
                (m.get("id") for m in minds if m.get("name") == target_name), None
            )
            if not mind_id:
                raise HTTPException(
                    status_code=502, detail=f"mind '{target_name}' not registered in broker"
                )
            payload = {
                "conversation_id": str(uuid.uuid4()),
                "from_mind": "canvas",
                "to_mind": mind_id,
                "content": content,
                "metadata": {"request_type": "canvas_review", "triggered_by": "canvas"},
            }
            resp = await client.post(
                f"{_gateway_base_url()}/broker/messages", json=payload, headers=headers
            )
            resp.raise_for_status()
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"could not reach Skippy: {exc}") from exc
    log_event(
        log, "canvas.review.dispatched", **_actor_fields(user),
        mind_name=target_name, element_count=element_count, note_chars=len(note),
    )
    return {"ok": True, "dispatched_to": target_name, "element_count": element_count}


@app.get("/canvas", response_class=HTMLResponse)
async def canvas(request: Request):
    if not get_current_user_from_request(request):
        return _login_redirect_for(request)
    return _render_template(request, "canvas.html")


@app.websocket("/ws/canvas")
async def ws_canvas(websocket: WebSocket):
    user = get_current_user_from_request(websocket)
    can_draw = user is not None
    await websocket.accept()
    _canvas_connections.add(websocket)
    try:
        await websocket.send_json({"type": "state", "elements": _canvas_elements})
        while True:
            data = await websocket.receive_json()
            if not can_draw:
                continue
            if _apply_canvas_message(data):
                await _broadcast_canvas(data, exclude=websocket)
    except WebSocketDisconnect:
        _canvas_connections.discard(websocket)
    except Exception:
        _canvas_connections.discard(websocket)


# ── Settings ───────────────────────────────────────────────────────────


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    if not get_current_user_from_request(request):
        return _login_redirect_for(request)
    return RedirectResponse(url="/settings/general", status_code=303)


_SETTINGS_SECTIONS = {"general", "access", "connections", "voice", "credentials", "advanced"}


@app.get("/settings/{section}", response_class=HTMLResponse)
async def settings_section_page(request: Request, section: str):
    if not get_current_user_from_request(request):
        return _login_redirect_for(request)
    if section not in _SETTINGS_SECTIONS:
        raise HTTPException(status_code=404, detail="settings section not found")
    return _render_template(request, "settings.html", settings_section=section)


@app.get("/api/settings/minds")
async def api_settings_minds(user: dict = Depends(require_auth)):
    del user
    broker_minds = await _broker_minds_safe()
    try:
        sessions_raw = await _gateway_json("/sessions")
        sessions = sessions_raw if isinstance(sessions_raw, list) else []
    except Exception:
        sessions = []
    return {"minds": _add_session_activity(build_mind_registry(broker_minds), sessions)}


async def _mind_gateway_url(name: str) -> str:
    """The registered gateway URL for a Mind, by display name."""
    for mind in await _broker_minds_safe():
        if str(mind.get("name") or "").lower() == name.strip().lower():
            return str(mind.get("gateway_url") or "")
    raise HTTPException(status_code=404, detail="That Mind is not registered")


@app.get("/api/minds/{name}/configuration")
async def api_mind_configuration(name: str, user: dict = Depends(require_auth)):
    del user
    configuration = await mind_runtime.fetch_configuration(
        await _mind_gateway_url(name)
    )
    try:
        model_rows = await _gateway_json("/models")
    except HTTPException:
        model_rows = []
    provider = str(configuration.get("provider") or "")
    harness = str(configuration.get("harness") or "")
    model_provider = (
        "ollama"
        if provider == "ollama"
        else "anthropic"
        if harness.startswith("claude")
        else ""
    )
    models = [
        str(row.get("name"))
        for row in model_rows
        if isinstance(row, dict)
        and row.get("name")
        and row.get("provider") == model_provider
    ]
    return {
        "configuration": {
            "name": configuration.get("name") or name,
            "harness": configuration.get("harness"),
            "provider": configuration.get("provider"),
            "default_model": configuration.get("default_model"),
            "deployment": configuration.get("deployment") or "container",
        },
        "models": models,
    }


@app.patch("/api/minds/{name}/configuration")
async def api_mind_configuration_update(
    name: str, request: Request, user: dict = Depends(require_auth)
):
    body = await request.json()
    default_model = str(
        body.get("default_model") if isinstance(body, dict) else ""
    ).strip()
    gateway_url = await _mind_gateway_url(name)

    # The Mind's own file first, then the broker row that caches it. In this
    # order a failure leaves the two agreeing rather than diverging: the Mind
    # re-registers from the file on its next start, so a row written against
    # a file that never took would be undone anyway.
    previous_configuration = await mind_runtime.fetch_configuration(gateway_url)
    configuration = await mind_runtime.push_default_model(gateway_url, default_model)
    try:
        await _gateway_request(
            "PUT",
            f"/broker/minds/{urllib.parse.quote(name)}",
            json_body={"model": default_model},
            admin=True,
        )
    except HTTPException:
        previous_model = str(previous_configuration.get("default_model") or "")
        if previous_model:
            await mind_runtime.push_default_model(gateway_url, previous_model)
        raise
    log_event(
        log, "mind.configuration.updated", **_actor_fields(user), mind_name=name,
        previous_model=previous_configuration.get("default_model"),
        default_model=default_model,
    )
    deployment = str(configuration.get("deployment") or "container")
    return {
        "saved": True,
        # Live on the next session this Mind is asked for. The broker row the
        # gateway reads at session creation has already been updated, and the
        # Mind's own file now says the same thing, so a restart would change
        # nothing about the model.
        "restart_required": False,
        # Only a containerized Mind can be bounced from here; anything else
        # runs under an init system this console does not reach.
        "restartable": deployment == "container",
        "configuration": {
            "name": configuration.get("name") or name,
            "harness": configuration.get("harness"),
            "provider": configuration.get("provider"),
            "default_model": configuration.get("default_model"),
            "deployment": deployment,
        },
    }


@app.get("/api/settings/hive-comms/sessions")
async def api_settings_hive_comms_sessions(user: dict = Depends(require_auth)):
    del user
    sessions = await _gateway_json("/sessions")
    if not isinstance(sessions, list):
        sessions = []
    registry = build_mind_registry(await _broker_minds_safe())
    return {"sessions": _add_session_mind_names(sessions, registry)}


@app.delete("/api/settings/hive-comms/sessions/{session_id}")
async def api_settings_hive_comms_session_delete(
    session_id: str, user: dict = Depends(require_auth)
):
    url = f"{_gateway_base_url().rstrip('/')}/sessions/{session_id}"
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.delete(url, headers=_gateway_headers())
        if resp.status_code >= 400:
            raise HTTPException(status_code=resp.status_code, detail=resp.text)
        result = resp.json() if resp.content else {"ok": True}
    log_event(log, "session.deleted", **_actor_fields(user), session_id=session_id)
    return result


def _hive_tools_base_url() -> str:
    return os.getenv("HIVE_TOOLS_URL") or "http://hive-tools:9421"


def _hive_tools_headers() -> dict[str, str]:
    token = os.getenv("HIVE_TOOLS_TOKEN", "")
    if not token:
        raise HTTPException(status_code=503, detail="Hive Tools authorization is not configured")
    return {"Authorization": f"Bearer {token}"}


async def _hive_tools_request(
    method: str,
    path: str,
    *,
    params: dict | None = None,
    json_body: dict | None = None,
    timeout: float = 30.0,
) -> dict:
    """Call Hive Tools.

    ``timeout`` is a parameter because the training pipeline's curate and
    export passes walk the whole corpus and run for minutes — far past the
    30 seconds that every other Hive Tools call comfortably fits in.
    """
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.request(
                method,
                f"{_hive_tools_base_url().rstrip('/')}{path}",
                params=params,
                json=json_body,
                headers=_hive_tools_headers(),
            )
        response.raise_for_status()
        data = response.json() if response.content else {}
    except HTTPException:
        raise
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(status_code=502, detail=f"Hive Tools request failed: {exc}") from exc
    return data if isinstance(data, dict) else {}


# Curate and export walk the whole corpus; on a corpus of ~9k turns curation
# is around 25 seconds and grows with capture. Give them real headroom —
# Hive Tools already caps them server-side.
_TRAINING_LONG_TIMEOUT_SECONDS = 1_800.0
_TRAINING_RUN_KINDS = {"curate", "export", "train"}


@app.get("/api/training/status")
async def api_training_status(user: dict = Depends(require_auth)):
    """Corpus counts, curation breakdown, GPU state, latest run per kind."""
    del user
    return await _hive_tools_request("GET", "/training/status")


@app.get("/api/training/base-models")
async def api_training_base_models(user: dict = Depends(require_auth)):
    """Candidate bases, annotated with what this host can train and serve."""
    del user
    return await _hive_tools_request("GET", "/training/base-models")


@app.get("/api/training/runs")
async def api_training_runs(
    kind: str | None = None, limit: int = 20, user: dict = Depends(require_auth)
):
    del user
    if kind and kind not in _TRAINING_RUN_KINDS:
        raise HTTPException(status_code=400, detail=f"Unknown run kind: {kind}")
    params: dict = {"limit": max(1, min(limit, 200))}
    if kind:
        params["kind"] = kind
    return await _hive_tools_request("GET", "/training/runs", params=params)


@app.post("/api/training/curate")
async def api_training_curate(request: Request, user: dict = Depends(require_auth)):
    """Run a curation pass. Writes verdicts in place; never deletes rows."""
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Expected a JSON object")
    result = await _hive_tools_request(
        "POST",
        "/training/curate",
        json_body=body,
        timeout=_TRAINING_LONG_TIMEOUT_SECONDS,
    )
    log_event(log, "training.curate.requested", **_actor_fields(user), options=body)
    return result


@app.post("/api/training/export")
async def api_training_export(request: Request, user: dict = Depends(require_auth)):
    """Write train/eval JSONL from kept rows, with credentials redacted."""
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Expected a JSON object")
    result = await _hive_tools_request(
        "POST",
        "/training/export",
        json_body=body,
        timeout=_TRAINING_LONG_TIMEOUT_SECONDS,
    )
    log_event(log, "training.export.requested", **_actor_fields(user), options=body)
    return result


@app.post("/api/training/train")
async def api_training_train(request: Request, user: dict = Depends(require_auth)):
    """Plan a fine-tune, or launch one when the caller sets dry_run false."""
    body = await request.json()
    if not isinstance(body, dict) or not str(body.get("train_file") or "").strip():
        raise HTTPException(status_code=400, detail="train_file is required")
    result = await _hive_tools_request("POST", "/training/train", json_body=body)
    log_event(
        log,
        "training.train.requested",
        **_actor_fields(user),
        dry_run=bool(body.get("dry_run", True)),
    )
    return result


@app.post("/api/training/deploy")
async def api_training_deploy(request: Request, user: dict = Depends(require_auth)):
    """Serve a trained adapter through Ollama, or start the merge first."""
    body = await request.json()
    if not isinstance(body, dict) or not str(body.get("adapter_dir") or "").strip():
        raise HTTPException(status_code=400, detail="adapter_dir is required")
    if not str(body.get("name") or "").strip():
        raise HTTPException(status_code=400, detail="name is required")
    result = await _hive_tools_request(
        "POST",
        "/training/deploy",
        json_body=body,
        timeout=_TRAINING_LONG_TIMEOUT_SECONDS,
    )
    log_event(
        log,
        "training.deploy.requested",
        **_actor_fields(user),
        model_name=body.get("name"),
        strategy=body.get("strategy", "adapter"),
    )
    return result


@app.post("/api/minds/{name}/restart")
async def api_mind_restart(name: str, user: dict = Depends(require_auth)):
    try:
        configuration = local_runtime_configuration(name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    deployment = str(configuration.get("deployment") or "container")
    if deployment != "container":
        raise HTTPException(
            status_code=400,
            detail=(
                f"This Mind runs as {deployment}, not a container. "
                "Restart it on its host to activate the new model."
            ),
        )
    result = await _hive_tools_request(
        "POST",
        "/docker/restart",
        params={
            "path": os.getenv(
                "HIVE_MIND_PROJECT_PATH", "/home/daniel/Storage/Dev/hive_mind"
            ),
            "service": name,
        },
    )
    log_event(log, "mind.restart.requested", **_actor_fields(user), mind_name=name)
    return result


@app.get("/api/operations/{request_id}")
async def api_operation_status(
    request_id: str, user: dict = Depends(require_auth)
):
    del user
    if not re.fullmatch(r"[A-Za-z0-9-]{1,100}", request_id):
        raise HTTPException(status_code=400, detail="Invalid operation ID")
    return await _hive_tools_request("GET", f"/hitl/{request_id}")


def _capability_node_specs() -> list[dict]:
    voice_urls = _voice_server_urls()
    proxy_installation = inference_proxy_status()
    return [
        {
            "id": "hive-tools",
            "name": "Hive Tools",
            "kind": "Action gateway",
            "description": "Approval-aware access to privileged actions and external systems.",
            "endpoint": _hive_tools_base_url(),
            "health_path": "/health",
            "auto_discover": True,
            "configured": bool(os.getenv("HIVE_TOOLS_URL")),
            "access": "Operator policy and approval gates",
            "consumers": "Authorized operator minds",
            "icon": "shield-check",
        },
        {
            "id": "inference-proxy",
            "name": "Inference Proxy",
            "kind": "Inference gateway",
            "description": "One private endpoint for upstream models, issued client keys, routing, and usage accounting.",
            "endpoint": get_capability_endpoint("inference-proxy")
            or os.getenv("INFERENCE_PROXY_URL")
            or "http://inference-proxy:8888",
            "health_path": "/health",
            "auto_discover": True,
            "configured": bool(
                get_capability_endpoint("inference-proxy")
                or os.getenv("INFERENCE_PROXY_URL")
            ),
            "configurable": True,
            "installable": True,
            "installed": proxy_installation["installed"],
            "repository": proxy_installation["repository"],
            "path": proxy_installation["path"],
            "access": "Proxy-issued client keys",
            "consumers": "Minds and applications using hosted models",
            "icon": "waypoints",
        },
        {
            "id": "ollama",
            "name": "Ollama",
            "kind": "Inference",
            "description": "Local model inference supplied without embedding model infrastructure in a mind.",
            "endpoint": get_capability_endpoint("ollama")
            or os.getenv("OLLAMA_URL")
            or os.getenv("OLLAMA_BASE_URL")
            or "",
            "health_path": "/api/tags",
            "configurable": True,
            "access": "Network-restricted model API",
            "consumers": "Minds configured for local inference",
            "icon": "cpu",
        },
        {
            "id": "voice-chatterbox",
            "name": "Chatterbox",
            "kind": "Voice",
            "description": "Reference-voice synthesis for minds assigned to Chatterbox.",
            "endpoint": voice_urls["chatterbox"],
            "health_path": "/health",
            "auto_discover": True,
            "configured": bool(os.getenv("CHATTERBOX_VOICE_URL")),
            "access": "Hive network",
            "consumers": "Minds with Chatterbox voice assignment",
            "icon": "audio-lines",
        },
        {
            "id": "voice-kokoro",
            "name": "Kokoro",
            "kind": "Voice",
            "description": "Lightweight speech synthesis for minds assigned to Kokoro.",
            "endpoint": voice_urls["kokoro"],
            "health_path": "/health",
            "auto_discover": True,
            "configured": bool(os.getenv("KOKORO_VOICE_URL")),
            "access": "Hive network",
            "consumers": "Minds with Kokoro voice assignment",
            "icon": "audio-waveform",
        },
        {
            "id": "pihole",
            "name": "Pi-hole",
            "kind": "Network service",
            "description": "Network-level DNS filtering and operational visibility.",
            "endpoint": get_capability_endpoint("pihole")
            or os.getenv("PIHOLE_URL")
            or "",
            "health_path": "/api/auth",
            "configurable": True,
            "accepted_statuses": {401},
            "authentication_required_statuses": {401},
            "access": "Administrative network service",
            "consumers": "Operators and approved automations",
            "icon": "shield",
        },
    ]


async def _capability_node_data() -> dict:
    tools = hive_tools_status()
    nodes: list[dict] = []
    probes = []
    for spec in _capability_node_specs():
        node = dict(spec)
        endpoint = str(node.pop("endpoint") or "").rstrip("/")
        health_path = str(node.pop("health_path") or "")
        auto_discover = bool(node.pop("auto_discover", False))
        accepted_statuses = set(node.pop("accepted_statuses", set()))
        authentication_required_statuses = set(
            node.pop("authentication_required_statuses", set())
        )
        explicitly_configured = bool(node.pop("configured", bool(endpoint)))
        installed = (
            bool(tools.get("present"))
            if node["id"] == "hive-tools"
            else node.get("installed")
        )
        configured = bool(endpoint) and (explicitly_configured or installed is True)
        if endpoint and (configured or auto_discover):
            probes.append(
                (
                    len(nodes),
                    _service_health(
                        f"{endpoint}{health_path}",
                        service_name=node["name"],
                        repair_action=(
                            f"Check the {node['name']} endpoint and service, then retry."
                        ),
                        accepted_statuses=accepted_statuses,
                        authentication_required_statuses=authentication_required_statuses,
                    ),
                )
            )
            health = None
        else:
            health = {
                "reachable": False,
                "ready": False,
                "detail": "Not configured",
                "message": f"{node['name']} is not configured for this Hive.",
                "action": f"Configure the {node['name']} endpoint to manage it here.",
            }
        node.update(
            {
                "configured": configured,
                "installed": installed,
                "endpoint": endpoint or None,
                "health": health,
            }
        )
        if node["id"] == "hive-tools":
            node["path"] = tools.get("path")
            node["capabilities"] = tools.get("capabilities", [])
        nodes.append(node)
    if probes:
        results = await asyncio.gather(*(probe for _, probe in probes))
        for (node_index, _), health in zip(probes, results, strict=True):
            node = nodes[node_index]
            if health["reachable"]:
                node["configured"] = True
                node["health"] = health
            elif node["configured"]:
                node["health"] = health
            else:
                node["health"] = {
                    "reachable": False,
                    "ready": False,
                    "detail": "Not configured",
                    "message": f"{node['name']} is not configured for this Hive.",
                    "action": f"Configure the {node['name']} endpoint to manage it here.",
                }
    return {
        "summary": {
            "total": len(nodes),
            "configured": sum(bool(node["configured"]) for node in nodes),
            "healthy": sum(
                bool(node["health"].get("ready", node["health"]["reachable"]))
                for node in nodes
            ),
            "attention": sum(
                bool(
                    node["configured"]
                    and not node["health"].get(
                        "ready", node["health"]["reachable"]
                    )
                )
                for node in nodes
            ),
        },
        "nodes": nodes,
    }


def _add_session_activity(registry: list[dict], sessions: list[dict]) -> list[dict]:
    """Add real conversation activity without treating registration as a heartbeat."""
    session_activity: dict[str, dict] = {}
    for session in sessions:
        if not isinstance(session, dict):
            continue
        mind_id = str(session.get("mind_id") or "")
        if not mind_id:
            continue
        raw_timestamp = session.get("last_active") or session.get("created_at")
        timestamp: float | None = None
        if isinstance(raw_timestamp, (int, float, str)):
            with suppress(ValueError):
                timestamp = float(raw_timestamp)
        activity = session_activity.setdefault(
            mind_id, {"last_activity": None, "active_session_count": 0}
        )
        if timestamp is not None and (
            activity["last_activity"] is None or timestamp > activity["last_activity"]
        ):
            activity["last_activity"] = timestamp
        if session.get("status") in {"active", "running"}:
            activity["active_session_count"] += 1

    enriched = []
    for mind in registry:
        row = dict(mind)
        mind_activity = session_activity.get(
            str(row.get("id") or "")
        ) or session_activity.get(str(row.get("name") or ""))
        row["last_activity"] = mind_activity["last_activity"] if mind_activity else None
        row["active_session_count"] = (
            mind_activity["active_session_count"] if mind_activity else 0
        )
        row["activity_status"] = "active" if row["active_session_count"] else "registered"
        enriched.append(row)
    return enriched


def _add_session_mind_names(
    sessions: list[dict], registry: list[dict]
) -> list[dict]:
    """Add display names to sessions without replacing their routing IDs."""
    names: dict[str, str] = {}
    for mind in registry:
        name = str(mind.get("name") or "").strip()
        mind_id = str(mind.get("id") or "").strip()
        if not name:
            continue
        names[name] = name
        if mind_id:
            names[mind_id] = name

    return [
        {
            **session,
            "mind_name": names.get(str(session.get("mind_id") or ""), "Unknown mind"),
        }
        for session in sessions
    ]


async def _service_health(
    url: str,
    *,
    headers: dict | None = None,
    service_name: str = "Service",
    repair_action: str = "Check the service configuration and retry.",
    accepted_statuses: set[int] | None = None,
    authentication_required_statuses: set[int] | None = None,
) -> dict:
    """Best-effort health probe — used purely for status display, never gates a route."""
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.get(url, headers=headers or {})
            accepted = accepted_statuses or set()
            authentication_required = authentication_required_statuses or set()
            if resp.status_code not in accepted:
                resp.raise_for_status()
            try:
                detail = resp.json() if resp.content else {}
            except ValueError:
                detail = {
                    "content_type": resp.headers.get("content-type", ""),
                    "response": resp.text[:300],
                }
            needs_authentication = resp.status_code in authentication_required
            return {
                "reachable": True,
                "ready": not needs_authentication,
                "authentication_required": needs_authentication,
                "endpoint": url,
                "status_code": resp.status_code,
                "detail": detail,
                "message": (
                    f"{service_name} responded, but administrative authentication is required."
                    if needs_authentication
                    else f"{service_name} responded normally."
                ),
                "action": (
                    f"Authenticate with {service_name} to enable administrative controls."
                    if needs_authentication
                    else "No action is required."
                ),
            }
    except httpx.HTTPStatusError as exc:
        response = exc.response
        try:
            detail = response.json()
        except ValueError:
            detail = response.text[:300]
        return {
            "reachable": False,
            "ready": False,
            "endpoint": url,
            "status_code": response.status_code,
            "detail": detail,
            "message": f"{service_name} returned HTTP {response.status_code}.",
            "action": repair_action,
        }
    except Exception as exc:
        return {
            "reachable": False,
            "ready": False,
            "endpoint": url,
            "detail": str(exc),
            "message": f"{service_name} could not be reached: {exc}",
            "action": repair_action,
        }


@app.get("/api/settings/hive-tools")
async def api_settings_hive_tools(user: dict = Depends(require_auth)):
    del user
    status = hive_tools_status()
    status["container_health"] = await _service_health(
        f"{_hive_tools_base_url()}/health",
        service_name="Hive Tools",
        repair_action="Start the Hive Tools service, then retry.",
    )
    status["remote_configured"] = False  # no git remote set up for hive-tools yet
    return status


def _voice_server_urls() -> dict[str, str]:
    return {
        "chatterbox": os.getenv("CHATTERBOX_VOICE_URL") or "http://hive-mind-voice:8422",
        "kokoro": os.getenv("KOKORO_VOICE_URL") or "http://hive-mind-voice-kokoro:8422",
    }


@app.get("/api/settings/voice")
async def api_settings_voice(user: dict = Depends(require_auth)):
    del user
    server_urls = _voice_server_urls()
    servers = {
        name: await _service_health(f"{url}/health") for name, url in server_urls.items()
    }
    broker_minds = await _broker_minds_safe()
    minds = []
    for m in broker_minds:
        if not isinstance(m, dict):
            continue
        name = m.get("name") or ""
        minds.append({"name": name, "voice_ref": voice_ref_status(name) if name else None})
    minds.sort(key=lambda r: str(r["name"]))
    return {"servers": servers, "minds": minds}


@app.post("/api/settings/voice/{name}/voice-ref")
async def api_settings_voice_upload(
    name: str, file: UploadFile, user: dict = Depends(require_auth)
):
    data = await file.read()
    try:
        target = save_voice_ref(name, data)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    log_event(
        log, "mind.voice_ref.updated", **_actor_fields(user), mind_name=name,
        size_bytes=len(data), path=str(target),
    )
    return {"ok": True, "path": str(target), "size_bytes": len(data)}


@app.get("/api/settings/downloads")
async def api_settings_downloads(user: dict = Depends(require_auth)):
    del user
    return {"downloads": list_downloads()}


@app.get("/api/settings/credentials")
async def api_settings_credentials(user: dict = Depends(require_auth)):
    del user
    return {"credentials": list_credentials()}


@app.post("/api/settings/credentials/{key}/rotate")
async def api_settings_credentials_rotate(key: str, user: dict = Depends(require_auth)):
    try:
        result = rotate_credential(key)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    log_event(
        log, "credential.rotated", **_actor_fields(user),
        credential=result["key"], written_to=[w["file"] for w in result["written"]],
    )
    return result


@app.post("/api/settings/credentials/github_pat/set")
async def api_settings_credentials_set_github_pat(
    payload: dict, user: dict = Depends(require_auth)
):
    try:
        result = set_github_pat(str(payload.get("token", "")))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    log_event(
        log, "credential.set", **_actor_fields(user),
        credential=result["key"], written_to=[w["file"] for w in result["written"]],
    )
    return result


@app.get("/downloads/{filename}")
async def download_asset(filename: str, user: dict = Depends(require_auth)):
    del user
    path = resolve_download_path(filename)
    if path is None:
        raise HTTPException(status_code=404, detail="file not found")
    return FileResponse(path, filename=filename)


async def _mind_name_is_taken(name: str) -> bool:
    """A name is taken if the broker knows it or the scaffold already exists.
    Checking only the broker would happily overwrite an unregistered mind."""
    broker_minds = await _broker_minds_safe()
    if any(
        isinstance(m, dict) and (m.get("name") or "").lower() == name
        for m in broker_minds
    ):
        return True
    try:
        return internal_mind_exists(name)
    except ValueError:
        return False


@app.get("/api/settings/new-mind/check-name")
async def api_settings_check_mind_name(name: str, user: dict = Depends(require_auth)):
    """Name availability, checked against the broker and the minds/ tree."""
    del user
    normalized = name.strip().lower()
    taken = await _mind_name_is_taken(normalized)
    return {"name": normalized, "available": bool(normalized) and not taken}


@app.get("/health")
async def health():
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn

    import config

    uvicorn.run(app, host=config.HOST, port=config.PORT, log_config=None, access_log=False)
