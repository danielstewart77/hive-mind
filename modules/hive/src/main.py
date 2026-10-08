"""Hive admin site: graph, memory, and standing rules for the hive mind.

Auth lives in the hive itself (see auth.py) so a cloned hive is
self-contained; the auth db and secret are shared with the terminal site.
"""

import asyncio
import hmac
import ipaddress
import json
import logging
import os
import re
import shlex
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from contextlib import asynccontextmanager, suppress
from pathlib import Path

import httpx
import yaml
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
from capability_install import (
    HIVE_TOOLS_PLACEMENT_NOTE,
    InstallInProgress,
    clone_hive_tools,
    clone_inference_proxy,
    hive_tools_install_status,
    inference_proxy_status,
)
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
import design_data
import design_delivery
import design_graph
import mind_symbols
import dashboard_data
import mind_runtime
import mind_soul
import mind_files
import mind_mounts
import kanban_data
import mind_skills
import sentinel_review
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


# Live design-delivery tasks, held so the event loop's weak references
# cannot collect one while it is waiting out a model turn.
_DESIGN_DELIVERIES: set = set()


@asynccontextmanager
async def lifespan(app: FastAPI):
    del app
    init_auth_db()
    seed_first_user()
    global _canvas_elements
    _canvas_elements = _load_canvas_state()
    log_event(log, "service.started", component="hive-control-plane")
    # Anything a design session was owed when this process last stopped. A
    # console restarted between a submission being stored and its send is
    # otherwise a submission nobody is ever told about, and the page shows
    # it as saved with no error and nothing pending.
    startup_delivery = asyncio.create_task(design_delivery.deliver_pending())
    _DESIGN_DELIVERIES.add(startup_delivery)
    startup_delivery.add_done_callback(_DESIGN_DELIVERIES.discard)
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
            x, y = float(el.get("x", 0)), float(el.get("y", 0))
            size = float(el.get("size", 14))
            # Estimate the run's extent. The renderer is monospace, so a glyph
            # is about 0.6em wide; without this a right-hand column of labels
            # falls outside the viewBox and the snapshot silently crops it.
            xs.extend([x, x + 0.6 * size * len(str(el.get("content", "")))])
            ys.extend([y - size, y])
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


def _canvas_to_svg(slide: int | None = None) -> str:
    """Compose the current board into a standalone dark-background SVG.

    This is the snapshot Skippy rasterizes (chrome --headless) so he can
    actually see what Daniel drew, since he cannot watch the live websocket.
    """
    elements = _canvas_elements
    if slide is not None:
        want = _slide_of(slide)
        elements = [e for e in elements if _slide_of(e.get("slide")) == want]
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
                f'font-family="monospace" font-size="{el.get("size", 14)}">{_xml_escape(el.get("content", ""))}</text>'
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


def _gateway_json_sync(
    path: str, params: dict | None = None, admin: bool = False
) -> dict | list:
    query = f"?{urllib.parse.urlencode(params)}" if params else ""
    url = f"{_gateway_base_url().rstrip('/')}{path}{query}"
    auth = _admin_headers() if admin else _gateway_headers()
    headers = {"Accept": "application/json", **auth}
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=15) as response:
        return json.loads(response.read().decode("utf-8"))


async def _gateway_json(
    path: str, params: dict | None = None, admin: bool = False
) -> dict | list:
    try:
        return await asyncio.to_thread(_gateway_json_sync, path, params, admin)
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
    response = _render_template(request, template_name, **context)
    # Every console page carries its own script inline, so a heuristically
    # cached copy is a cached application. A phone that fetched a page before
    # a fix keeps running the broken version across reloads — and there is no
    # gesture on iOS that reliably bypasses it, so the symptom is a control
    # that silently sends nothing while the server shows no request at all.
    response.headers["Cache-Control"] = "no-store, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    return response


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


@app.get("/kanban", response_class=HTMLResponse)
async def kanban_page(request: Request):
    return _console_page(request, "kanban.html")


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


# Kanban API deliberately uses one REST mapping: collection POST, item PATCH/DELETE.
_KANBAN_CREATE_FIELDS = {
    "epics": {"title"},
    "features": {"epic_id", "title"},
    "stories": {"title", "feature_id", "epic_id", "status"},
}
_KANBAN_UPDATE_FIELDS = {
    "epics": {"title"},
    "features": {"epic_id", "title"},
    "stories": {"title", "feature_id", "epic_id", "status"},
}


async def _kanban_body(request: Request, allowed: set[str], required: set[str] = frozenset()):
    try:
        body = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="Malformed JSON body") from exc
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Request body must be an object")
    missing = required - body.keys()
    extra = body.keys() - allowed
    if missing:
        raise HTTPException(status_code=400, detail=f"Missing required fields: {', '.join(sorted(missing))}")
    if extra:
        raise HTTPException(status_code=400, detail=f"Unsupported fields: {', '.join(sorted(extra))}")
    if "status" in body and body["status"] not in kanban_data.STATUSES:
        raise HTTPException(status_code=400, detail=f"unsupported status: {body['status']}")
    return body


def _kanban_call(function, *args, **kwargs):
    try:
        return function(*args, **kwargs)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc.args[0])) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/kanban/data")
async def api_kanban_data(user: dict = Depends(require_auth)):
    del user
    return kanban_data.get_data()


@app.post("/api/kanban/epics")
@app.post("/api/kanban/features")
@app.post("/api/kanban/stories")
async def api_kanban_create(request: Request, user: dict = Depends(require_auth)):
    del user
    kind = request.url.path.rsplit("/", 1)[-1]
    fields = _KANBAN_CREATE_FIELDS[kind]
    required = {"title"} if kind == "stories" else fields
    body = await _kanban_body(request, fields, required)
    if kind == "stories" and ((body.get("feature_id") is None) == (body.get("epic_id") is None)):
        raise HTTPException(status_code=400, detail="story requires exactly one of feature_id or epic_id")
    entity = {"epics": "epic", "features": "feature", "stories": "story"}[kind]
    return _kanban_call(getattr(kanban_data, f"create_{entity}"), **body)


@app.patch("/api/kanban/{kind}/{record_id}")
async def api_kanban_update(kind: str, record_id: str, request: Request, user: dict = Depends(require_auth)):
    del user
    if kind not in _KANBAN_UPDATE_FIELDS:
        raise HTTPException(status_code=404, detail="Kanban resource not found")
    body = await _kanban_body(request, _KANBAN_UPDATE_FIELDS[kind])
    if not body:
        raise HTTPException(status_code=400, detail="At least one update field is required")
    entity = {"epics": "epic", "features": "feature", "stories": "story"}[kind]
    return _kanban_call(getattr(kanban_data, f"update_{entity}"), record_id, **body)


@app.delete("/api/kanban/{kind}/{record_id}")
async def api_kanban_delete(kind: str, record_id: str, user: dict = Depends(require_auth)):
    del user
    if kind not in _KANBAN_UPDATE_FIELDS:
        raise HTTPException(status_code=404, detail="Kanban resource not found")
    entity = {"epics": "epic", "features": "feature", "stories": "story"}[kind]
    return _kanban_call(getattr(kanban_data, f"delete_{entity}"), record_id)


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


@app.get("/harness-runs", response_class=HTMLResponse)
async def harness_runs_page(request: Request):
    return _console_page(request, "harness_runs.html")


# The run collector writes this file; the console only reads it. The model-testing
# checkout is not mounted into this container, and the hive's own data directory
# already is — so the measurement travels as one file rather than as a new mount.
HARNESS_RUNS_FILE = Path("/data/harness-runs.json")


@app.get("/api/harness-runs")
async def api_harness_runs(user: dict = Depends(require_auth)):
    """Every recorded harness run's tool traffic.

    An absent file is reported as no runs rather than as an error: the collector
    has simply not run yet on this host, which is a different sentence from the
    console being broken, and the page says which.
    """
    try:
        return json.loads(HARNESS_RUNS_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"generated": None, "source": str(HARNESS_RUNS_FILE), "runs": []}
    except (OSError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=502, detail=f"harness run file unreadable: {exc}") from exc


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard_page(request: Request):
    return _console_page(request, "dashboard.html")


def _proxy_base_url() -> str:
    return (
        get_capability_endpoint("inference-proxy")
        or os.getenv("INFERENCE_PROXY_URL")
        or "http://inference-proxy:8888"
    ).rstrip("/")


def _proxy_headers() -> dict:
    """The console's own proxy key.

    Absent is a state, not a failure: the allowance panel reports "not
    configured" and the rest of the page renders. A console that refused to
    load because one key was unset would hide the minds, the hosts and the
    conversations over a panel nobody had wired yet.
    """
    token = os.getenv("INFERENCE_PROXY_KEY", "")
    return {"Authorization": f"Bearer {token}"} if token else {}


async def _proxy_json(path: str) -> dict:
    headers = _proxy_headers()
    if not headers:
        raise RuntimeError("INFERENCE_PROXY_KEY is not configured for this console")
    async with httpx.AsyncClient(timeout=6.0) as client:
        response = await client.get(f"{_proxy_base_url()}{path}", headers=headers)
    response.raise_for_status()
    return response.json()


async def _fetch_provider_limits() -> dict:
    return {"providers": (await _proxy_json("/v1/providers/limits")).get("providers", [])}


async def _fetch_model_catalog() -> dict:
    """Deployment name to declared context window, from the model map.

    The proxy owns that map, so this is a relay rather than a table the
    console keeps — a second copy would disagree the first time somebody
    edited a row.
    """
    listing = await _proxy_json("/v1/models")
    return {
        "catalog": {
            row["id"]: row.get("context_window")
            for row in listing.get("data", [])
            if isinstance(row, dict) and row.get("id")
        }
    }


async def _probe_mind_host(mind: dict) -> dict:
    """One mind's account of the machine it runs on.

    Every failure keeps its own shape. A refused mind is up and holding a
    token we do not have; one running an older build has no route yet; one
    that never answered may be asleep, firewalled or off. Three remedies,
    three machines — collapsing them is how an evening goes into rebooting
    a box whose only problem was an environment variable.
    """
    name = mind.get("name") or ""
    gateway = (mind.get("gateway_url") or "").strip().rstrip("/")
    if not gateway:
        return {"mind": name, "mind_id": mind.get("id"), "last_seen": mind.get("last_seen"), "status": "unconfigured",
                "reading": None, "error": "no gateway URL in the registry"}
    # Outside the try, and deliberately. `admin_headers()` raises when the
    # console has no admin token, and inside the try that exception would be
    # caught by the unreachable arm — naming every machine in the hive as
    # unreachable because of one unset variable on this box, which is the
    # exact conflation `host_status_for` exists to prevent.
    try:
        headers = mind_runtime.admin_headers()
    except HTTPException as exc:
        return {"mind": name, "mind_id": mind.get("id"), "last_seen": mind.get("last_seen"), "status": "unconfigured",
                "reading": None, "error": str(exc.detail)}
    try:
        async with httpx.AsyncClient(timeout=dashboard_data.HOST_PROBE_TIMEOUT) as client:
            response = await client.get(f"{gateway}/host", headers=headers)
    except Exception as exc:  # noqa: BLE001 — unreachable is a state
        return {"mind": name, "mind_id": mind.get("id"), "last_seen": mind.get("last_seen"), "status": "unreachable",
                "reading": None, "error": str(exc)}
    status = dashboard_data.host_status_for(response.status_code)
    if status != "reporting":
        return {"mind": name, "mind_id": mind.get("id"), "last_seen": mind.get("last_seen"), "status": status,
                "reading": None, "error": response.text[:200]}
    return {"mind": name, "mind_id": mind.get("id"), "last_seen": mind.get("last_seen"), "status": "reporting",
            "reading": response.json(), "error": None}


async def _fetch_hosts() -> dict:
    """Every mind's machine, asked all at once.

    Concurrently and with a per-mind deadline, because one sleeping Windows
    box DROPs rather than refuses: asked in sequence it would hold up every
    other machine's figures, and the page would present them all as current.
    """
    minds = await _broker_minds_safe()
    reports = await asyncio.gather(
        *[_probe_mind_host(mind) for mind in minds], return_exceptions=True
    )
    settled = [
        report
        if isinstance(report, dict)
        # Named, not blanked: a nameless failure row merges with every other
        # nameless one, and the second dead machine disappears behind the
        # first.
        else {"mind": mind.get("name") or "", "mind_id": mind.get("id"),
              "last_seen": mind.get("last_seen"),
              "status": "unreachable", "reading": None, "error": str(report)}
        for mind, report in zip(minds, reports)
    ]
    merged = dashboard_data.merge_hosts(settled)
    return {**dashboard_data.watchable_hosts(merged), "reports": settled}


_host_snapshot = dashboard_data.SharedSnapshot(
    _fetch_hosts, ttl=dashboard_data.HOST_TTL_SECONDS
)
_provider_snapshot = dashboard_data.SharedSnapshot(
    _fetch_provider_limits, ttl=dashboard_data.PROVIDER_TTL_SECONDS
)
_catalog_snapshot = dashboard_data.SharedSnapshot(
    _fetch_model_catalog, ttl=dashboard_data.CATALOG_TTL_SECONDS
)


async def _live_sessions() -> dict:
    """Which conversations are producing, asked of the only party that knows."""
    # Admin-guarded on the gateway: the body carries assistant prose from
    # every conversation in the hive, which the service token every surface
    # bot holds must not open.
    payload = await _gateway_json("/sessions/live", admin=True)
    if not isinstance(payload, dict) or "sessions" not in payload:
        # A gateway too old to know this route answers 200 with the body of
        # GET /sessions/{id} for the id "live". Folding that into an empty
        # list renders a running hive as a silent one, which is the failure
        # this page exists to catch.
        raise RuntimeError(
            "the gateway answered /sessions/live with an unexpected shape "
            "(it may predate the route)"
        )
    return payload


@app.get("/api/dashboard")
async def api_dashboard(user: dict = Depends(require_auth)):
    """One reading of the hive: who is talking, how full, and on what.

    Guarded like every other console API. This one carries context counts,
    host inventory and subscription spend — guarding only the HTML would
    leave all of it on an unauthenticated JSON route.
    """
    del user
    live, hosts, providers, catalog = await asyncio.gather(
        _live_sessions(),
        _host_snapshot.get(),
        _provider_snapshot.get(),
        _catalog_snapshot.get(),
        return_exceptions=True,
    )
    windows = catalog.get("catalog", {}) if isinstance(catalog, dict) else {}
    sessions = live.get("sessions", []) if isinstance(live, dict) else []
    for session in sessions:
        context = dict(session.get("context") or {})
        context["window"] = dashboard_data.resolve_window(
            context.get("window"), dashboard_data.window_for(session.get("model"), windows)
        )
        session["context"] = dashboard_data.context_reading(context)
    registry = await _broker_minds_safe()
    columns = dashboard_data.name_columns(
        dashboard_data.ordered_columns(sessions), registry
    )
    return {
        "columns": columns,
        "hosts": hosts if isinstance(hosts, dict) else {"hosts": [], "stale": True,
                                                        "error": str(hosts)},
        "providers": dashboard_data.annotate_providers(providers)
        if isinstance(providers, dict)
        else {"providers": [], "stale": True, "error": str(providers)},
        "sessions_error": None if isinstance(live, dict) else str(live),
        "observed_at": time.time(),
    }


@app.get("/api/dashboard/text")
async def api_dashboard_text(
    session_id: str, since: int = 0, user: dict = Depends(require_auth)
):
    """Everything one live conversation has done since `since`.

    Relayed from comms, which types each block rather than filtering it:
    prose, thinking, the tool call with its input and the result with its
    body. The console spends its admin bearer here on behalf of whoever is
    logged in, so any account in the console's user table reads any mind's
    command output and file contents. That is the page working as specified
    — the dashboard exists to show the work — and it is the reason this
    route is behind `require_auth` at all.
    """
    del user
    return await _gateway_json(
        "/sessions/live/text",
        {"session_id": session_id, "since": since},
        admin=True,
    )


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
    "anthropic": "claude-sonnet-5",
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
    except InstallInProgress as exc:
        # Transient: the operator double-clicked, or has two tabs open.
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return result


@app.post("/api/capability-nodes/hive-tools/install")
async def api_hive_tools_install(user: dict = Depends(require_auth)):
    del user
    try:
        result = await asyncio.to_thread(clone_hive_tools)
    except InstallInProgress as exc:
        # Transient: the operator double-clicked, or has two tabs open.
        raise HTTPException(status_code=503, detail=str(exc)) from exc
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


@app.get("/sentinel", response_class=HTMLResponse)
async def sentinel_page(request: Request):
    if not get_current_user_from_request(request):
        return _login_redirect_for(request)
    return _render_template(
        request, "sentinel.html", sentinel_configured=bool(sentinel_review.base_url())
    )


@app.get("/api/sentinel/review")
async def api_sentinel_review(user: dict = Depends(require_auth)):
    del user
    return await sentinel_review.fetch_review_queue()


@app.get("/api/sentinel/queue")
async def api_sentinel_queue(user: dict = Depends(require_auth)):
    del user
    return await sentinel_review.fetch_decision_queue()


@app.post("/api/sentinel/review/mark")
async def api_sentinel_mark(request: Request, user: dict = Depends(require_auth)):
    del user
    body = await request.json()
    kind = (body.get("kind") or "").strip()
    if kind not in {"rule", "class"}:
        raise HTTPException(status_code=400, detail="kind must be rule or class")
    try:
        row_id = int(body.get("id"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="id must be an integer") from None
    return await sentinel_review.mark_reviewed(kind, row_id, body.get("note"))


@app.post("/api/sentinel/review/overturn")
async def api_sentinel_overturn(request: Request, user: dict = Depends(require_auth)):
    del user
    body = await request.json()
    try:
        rule_id = int(body.get("rule_id"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="rule_id must be an integer") from None
    return await sentinel_review.overturn_rule(
        rule_id,
        action_kind=body.get("action_kind"),
        retire=bool(body.get("retire")),
        note=body.get("note"),
    )


@app.post("/api/sentinel/queue/apply")
async def api_sentinel_queue_apply(request: Request, user: dict = Depends(require_auth)):
    del user
    body = await request.json()
    return await sentinel_review.apply_queue(body.get("class_slug"))


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
    "path": ("type", "id", "color", "d", "sw", "slide"),
    "text": ("type", "id", "x", "y", "content", "color", "size", "slide"),
    "image": ("type", "id", "x", "y", "w", "h", "src", "slide"),
}


def _slide_of(value: object) -> int:
    """Slide number an element or message belongs to, 1-based.

    Anything unnumbered or unparseable is slide one — the board predates
    slides, and its existing content is the first slide of the deck.
    """
    try:
        n = int(value)  # type: ignore[call-overload]
    except (TypeError, ValueError):
        return 1
    return n if n >= 1 else 1


def _apply_canvas_message(data: dict) -> bool:
    """Apply one canvas mutation to in-memory state and persist it.

    Shared by the websocket surface and the bearer-guarded /canvas/push
    endpoint so the two paths can never drift. Mutates ``data`` in place to
    fill a missing id. Returns True when a known message type was handled.
    """
    msg_type = data.get("type")
    if msg_type == "clear":
        if "slide" in data:
            # Scoped clear: presenting a deck, wiping the whole board because
            # one slide went wrong is not a recoverable mistake.
            target = _slide_of(data.get("slide"))
            data["slide"] = target
            _canvas_elements[:] = [
                e for e in _canvas_elements if _slide_of(e.get("slide")) != target
            ]
        else:
            _canvas_elements.clear()
    elif msg_type in _CANVAS_KEYS:
        data.setdefault("id", str(uuid.uuid4()))
        data["slide"] = _slide_of(data.get("slide"))
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
async def canvas_render_svg(slide: int | None = None):
    """Standalone SVG snapshot of the board, for Skippy to rasterize and read.

    With ``?slide=N`` the snapshot is that slide alone — what an audience sees
    at that point in the presentation. Without it, the whole board.
    """
    return Response(content=_canvas_to_svg(slide), media_type="image/svg+xml")


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
    return _console_page(request, "canvas.html")


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
    gateway_url = await _mind_gateway_url(name)
    configuration = await mind_runtime.fetch_configuration(gateway_url)
    # Asked of the Mind, not the gateway: the Mind holds the proxy client key
    # whose permissions decide the answer, and the proxy already filtered the
    # listing to the request shape this Mind's harness speaks. Nothing is
    # re-sorted here — the console groups by the provider each row names, so a
    # provider added to the proxy appears with no change on this side.
    models = [
        {
            "name": str(row.get("name")),
            "label": str(row.get("label") or row.get("name")),
            "provider": str(row.get("provider") or ""),
            "provider_label": str(
                row.get("provider_label") or row.get("provider") or ""
            ),
        }
        for row in await mind_runtime.fetch_models(gateway_url)
        if isinstance(row, dict) and row.get("name")
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


@app.get("/api/minds/{name}/settings")
async def api_mind_settings(name: str, user: dict = Depends(require_auth)):
    """The settings panel for one Mind.

    Which controls come back is the Mind's answer, not this console's: only
    the Mind knows which harness it runs, and a control its runner ignores is
    worse than an absent one because nothing on screen could tell the
    operator it did nothing.
    """
    del user
    gateway_url = await _mind_gateway_url(name)
    return await mind_runtime.fetch_settings(gateway_url)


@app.patch("/api/minds/{name}/settings")
async def api_mind_settings_update(
    name: str, request: Request, user: dict = Depends(require_auth)
):
    """Write the changed settings into this Mind's own runtime.yaml."""
    body = await request.json()
    settings = body.get("settings") if isinstance(body, dict) else None
    if not isinstance(settings, dict) or not settings:
        raise HTTPException(status_code=400, detail="Nothing was changed")
    gateway_url = await _mind_gateway_url(name)
    saved = await mind_runtime.push_settings(gateway_url, settings)
    log_event(
        log, "mind.settings.updated", **_actor_fields(user), mind_name=name,
        settings=sorted(settings),
    )
    return {"saved": True, **saved}


# ---------------------------------------------------------------------------
# The project folders a Mind can reach
#
# Written into the Mind's own compose fragment, which is the durable truth,
# and applied by recreating the container: a restart replays the config the
# container was created with and cannot add a mount.
# ---------------------------------------------------------------------------


def _mount_deployment(name: str) -> str:
    try:
        configuration = local_runtime_configuration(name)
    except ValueError:
        return ""
    return str(configuration.get("deployment") or "container")


@app.get("/api/minds/{name}/mounts")
async def api_mind_mounts(name: str, user: dict = Depends(require_auth)):
    """The project folders this Mind's compose fragment declares.

    A Mind that is not a container reports them as not editable rather than
    rendering a control that would write a file nothing reads — the same way
    the restart action is refused for one.
    """
    del user
    deployment = _mount_deployment(name)
    if deployment != "container":
        return {
            "editable": False,
            "deployment": deployment,
            "detail": (
                f"This Mind runs as {deployment or 'something this console cannot see'}, "
                "not a container in this stack, so its mounts are not editable from here."
            ),
            "mounts": [],
            "infrastructure": [],
            "roots": [],
        }
    try:
        return {"editable": True, "deployment": deployment, **mind_mounts.read_mounts(name)}
    except mind_mounts.NoComposeFile as exc:
        return {
            "editable": False,
            "deployment": deployment,
            "detail": str(exc),
            "mounts": [],
            "infrastructure": [],
            "roots": [],
        }
    except (ValueError, OSError, yaml.YAMLError) as exc:
        # A fragment this console cannot read or parse is a containerised Mind
        # with a problem, not a Mind that runs under something else. Folding
        # the two together sends the operator to the wrong machine.
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/minds/{name}/mounts/folders")
async def api_mind_mount_folders(
    name: str, root: str, path: str = "", user: dict = Depends(require_auth)
):
    """The folders directly under one place in a declared root."""
    del user, name
    try:
        return mind_mounts.list_folders(root, path)
    except (ValueError, OSError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/minds/{name}/mounts/folders")
async def api_mind_mount_folder_create(
    name: str, request: Request, user: dict = Depends(require_auth)
):
    """Create a folder under a declared root, for mounting in the same action."""
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="A folder is named in an object")
    try:
        created = mind_mounts.create_folder(
            str(body.get("root") or ""),
            str(body.get("parent") or ""),
            str(body.get("name") or ""),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(status_code=400, detail=f"Could not create that folder: {exc}") from exc
    log_event(
        log, "mind.mounts.folder_created", **_actor_fields(user), mind_name=name,
        root=created["root"], path=created["path"],
    )
    return created


@app.put("/api/minds/{name}/mounts")
async def api_mind_mounts_update(
    name: str, request: Request, user: dict = Depends(require_auth)
):
    """Write these folders into the Mind's fragment, then recreate it from it.

    The write comes first and the recreate second, because the fragment is
    what the next `up` reads however this call ends. A recreate that fails or
    is still waiting on approval is reported as such rather than as a save
    that worked — the folders are on disk, but nothing inside the Mind can
    see them until its container is replaced.
    """
    body = await request.json()
    rows = body.get("mounts") if isinstance(body, dict) else None
    revision = str((body or {}).get("revision") or "") if isinstance(body, dict) else ""
    if not isinstance(rows, list):
        raise HTTPException(status_code=400, detail="mounts must be a list")
    deployment = _mount_deployment(name)
    if deployment != "container":
        raise HTTPException(
            status_code=400,
            detail=(
                f"This Mind runs as {deployment or 'something this console cannot see'}, "
                "not a container in this stack. Its mounts are set on its own host."
            ),
        )
    try:
        target = mind_mounts.compose_target(name)
        state = mind_mounts.save_mounts(name, rows, revision)
    except mind_mounts.StaleFragment as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except mind_mounts.NoComposeFile as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except (ValueError, OSError, yaml.YAMLError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    log_event(
        log, "mind.mounts.updated", **_actor_fields(user), mind_name=name,
        mounts=[row["container_path"] for row in state["mounts"]],
    )
    # Every way this call can fail from here is a fragment that was written
    # and a container that was not replaced. Saying only "Hive Tools request
    # failed" reads as nothing having happened, and the next `up` for any
    # reason then applies an edit the operator believes was refused.
    written = (
        "The folders were written to this Mind's compose file, but recreating "
        "its container did not happen, so nothing inside it has changed yet: "
    )
    try:
        recreate = await _hive_tools_request(
            "POST",
            "/docker/up",
            params={"path": target.project_path, "service": target.service},
            timeout=310.0,
        )
    except HTTPException as exc:
        raise HTTPException(status_code=502, detail=written + str(exc.detail)) from exc
    if recreate.get("hitl_request_id"):
        return {
            "saved": True,
            "recreated": False,
            "operation_id": str(recreate["hitl_request_id"]),
            "detail": (
                "Saved. Recreating this Mind's container is waiting on your "
                "approval in Hive Tools; the new folders appear inside it once "
                "that goes through."
            ),
            **state,
        }
    if recreate.get("error") or recreate.get("status") == "error":
        raise HTTPException(
            status_code=502,
            detail=written
            + str(recreate.get("error") or recreate.get("stderr") or "no detail"),
        )
    return {"saved": True, "recreated": True, **state}


@app.get("/api/minds/{name}/voice")
async def api_mind_voice(name: str, user: dict = Depends(require_auth)):
    """This Mind's voice, and the voices the speech engine can actually speak.

    The current value comes from the Mind's own file; the catalogue comes from
    the voice server, because what is selectable is a property of the running
    engine and its language rather than of any one Mind. Only Kokoro has a
    catalogue — Chatterbox clones from a reference recording, so for a Mind on
    Chatterbox the question is which recording it has, not which voice it picked.
    """
    del user
    gateway_url = await _mind_gateway_url(name)
    configuration = await mind_runtime.fetch_configuration(gateway_url)
    offered = await mind_runtime.fetch_voice_catalogue(_voice_server_urls()["kokoro"])
    return {
        "voice": str(configuration.get("voice") or ""),
        "engine": offered.get("engine") or "",
        "default_voice": offered.get("default_voice") or "",
        "catalogue": offered.get("catalogue") or {},
    }


@app.put("/api/minds/{name}/voice")
async def api_mind_voice_update(
    name: str, request: Request, user: dict = Depends(require_auth)
):
    """Set this Mind's voice.

    The Mind's own file first, then the broker row that caches it, in that
    order for the same reason the model takes: the Mind re-registers from its
    file on its next start, so a row written against a file that never took
    would be undone anyway.
    """
    del user
    voice = await _named_voice(request)
    gateway_url = await _mind_gateway_url(name)
    configuration = await mind_runtime.push_voice(gateway_url, voice)
    await _gateway_request(
        "PUT",
        f"/broker/minds/{urllib.parse.quote(name)}",
        json_body={"voice": voice},
        admin=True,
    )
    return {"saved": True, "voice": str(configuration.get("voice") or voice)}


@app.patch("/api/minds/{name}/configuration")
async def api_mind_configuration_update(
    name: str, request: Request, user: dict = Depends(require_auth)
):
    body = await request.json()
    default_model = str(
        body.get("default_model") if isinstance(body, dict) else ""
    ).strip()
    provider = str(body.get("provider") if isinstance(body, dict) else "").strip()
    gateway_url = await _mind_gateway_url(name)

    # The Mind's own file first, then the broker row that caches it. In this
    # order a failure leaves the two agreeing rather than diverging: the Mind
    # re-registers from the file on its next start, so a row written against
    # a file that never took would be undone anyway.
    previous_configuration = await mind_runtime.fetch_configuration(gateway_url)
    configuration = await mind_runtime.push_default_model(
        gateway_url, default_model, provider
    )
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
            await mind_runtime.push_default_model(
                gateway_url,
                previous_model,
                str(previous_configuration.get("provider") or ""),
            )
        raise
    log_event(
        log, "mind.configuration.updated", **_actor_fields(user), mind_name=name,
        previous_model=previous_configuration.get("default_model"),
        default_model=default_model,
        provider=provider or None,
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


@app.get("/api/minds/{name}/surface-token")
async def api_mind_surface_token(name: str, user: dict = Depends(require_auth)):
    """Whether this Mind's surface has a bot token the bot API accepts.

    Relayed from the Mind, which is the only party that can see where its own
    token lives. Never the token itself: a console that could read one would
    put every bot in the hive one session cookie away from being
    impersonated.
    """
    del user
    gateway_url = await _mind_gateway_url(name)
    state = await mind_runtime.fetch_surface_token(gateway_url)
    return {
        "stored": bool(state.get("stored")),
        "accepted": state.get("accepted"),
        "bot_username": str(state.get("bot_username") or ""),
        "where": str(state.get("where") or ""),
        "detail": str(state.get("detail") or ""),
    }


@app.put("/api/minds/{name}/surface-token")
async def api_mind_surface_token_set(
    name: str, request: Request, user: dict = Depends(require_auth)
):
    """Hand this Mind a new bot token for it to verify and store.

    The Mind writes it, because the token has to land where that Mind's
    surface reads it — its own `.env` on an edge install, a per-mind keyring
    key in the stack — and this console can see neither.

    The Mind verifies against the bot API before storing, so a token refused
    there leaves the working one in place. The audit line records which Mind
    and what came of it; the value never enters a log, a response, or an
    exception message.
    """
    del user
    body = await request.json()
    token = str((body or {}).get("token") or "").strip()
    if not token:
        raise HTTPException(status_code=400, detail="A bot token is required")
    gateway_url = await _mind_gateway_url(name)
    try:
        state = await mind_runtime.push_surface_token(gateway_url, token)
    except HTTPException as exc:
        log_event(
            log, "mind.surface_token.refused", level=logging.WARNING,
            mind=name, status=exc.status_code,
        )
        raise
    log_event(
        log, "mind.surface_token.updated", mind=name,
        bot_username=str(state.get("bot_username") or ""),
        where=str(state.get("where") or ""),
    )
    return {
        "saved": True,
        "stored": True,
        "accepted": True,
        "bot_username": str(state.get("bot_username") or ""),
        "where": str(state.get("where") or ""),
        # The surface read its token when it started and keeps it until it is
        # recreated, which the page says rather than implying otherwise.
        "restart_required": True,
    }


@app.get("/api/minds/{name}/soul")
async def api_mind_soul(name: str, user: dict = Depends(require_auth)):
    """This Mind's soul, straight from the graph, with its change record.

    Asked of lucent rather than of the Mind: the soul lives in the graph, and
    a Mind whose soul is wrong is exactly the Mind you may have stopped.
    """
    del user
    soul = await mind_soul.fetch_soul(name)
    return {
        "soul_values": soul.get("soul_values") or [],
        "change_count": soul.get("change_count") or 0,
        "history": await mind_soul.fetch_history(name),
    }


@app.put("/api/minds/{name}/soul")
async def api_mind_soul_update(
    name: str, request: Request, user: dict = Depends(require_auth)
):
    """Replace this Mind's soul, recorded as a console edit.

    The whole list, not a delta: rewriting a line and removing one are the
    edits this page exists for, and neither is expressible as an append.
    """
    del user
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Expected a JSON object")
    values = body.get("soul_values")
    if not isinstance(values, list) or not all(isinstance(v, str) for v in values):
        raise HTTPException(
            status_code=400, detail="soul_values must be a list of strings"
        )
    await mind_soul.push_soul(name, values, str(body.get("reason") or ""))
    soul = await mind_soul.fetch_soul(name)
    return {
        "soul_values": soul.get("soul_values") or [],
        "change_count": soul.get("change_count") or 0,
        "history": await mind_soul.fetch_history(name),
    }


async def _named_voice(request: Request) -> str:
    """The voice named in a request body, refusing anything that is not one.

    `str(body.get("voice"))` turns an absent key and a JSON null alike into the
    literal string "None", which is truthy, matches the name pattern, and gets
    written into a Mind's runtime.yaml as its voice. A body that is not JSON at
    all is the caller's mistake and answers 400 rather than 500.
    """
    try:
        body = await request.json()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Expected a JSON object") from exc
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Expected a JSON object")
    voice = body.get("voice")
    voice = voice.strip() if isinstance(voice, str) else ""
    if not voice:
        raise HTTPException(status_code=400, detail="A voice is required")
    return voice


@app.post("/api/voices/sample")
async def api_voice_sample(request: Request, user: dict = Depends(require_auth)):
    """Speak a sample line in one voice, so it can be picked by ear.

    Not per Mind: the voices a sample can be spoken in are a property of the
    speech engine, exactly as the catalogue is, and every Mind on this hive
    reads from the same one.
    """
    del user
    voice = await _named_voice(request)
    audio = await mind_runtime.sample_voice(_voice_server_urls()["kokoro"], voice)
    return Response(content=audio, media_type="audio/ogg")


@app.get("/api/minds/{name}/skills")
async def api_mind_skills(name: str, user: dict = Depends(require_auth)):
    """Every skill this Mind knows, repo side against installed side."""
    del user
    return await mind_skills.fetch_skills(await _mind_gateway_url(name))


@app.get("/api/minds/{name}/skills/{skill}/diff")
async def api_mind_skill_diff(name: str, skill: str, user: dict = Depends(require_auth)):
    del user
    return await mind_skills.fetch_diff(await _mind_gateway_url(name), skill)


@app.post("/api/minds/{name}/skills/{skill}/install")
async def api_mind_skill_install(
    name: str, skill: str, user: dict = Depends(require_auth)
):
    """Copy the repo's version onto this Mind — applying and reverting alike."""
    result = await mind_skills.install(await _mind_gateway_url(name), skill)
    log_event(
        log, "mind.skill.installed", **_actor_fields(user), mind_name=name, skill=skill
    )
    return result


@app.post("/api/minds/{name}/skills/{skill}/write-back")
async def api_mind_skill_write_back(
    name: str, skill: str, user: dict = Depends(require_auth)
):
    """Copy this Mind's version into its repo checkout. Nothing is committed."""
    result = await mind_skills.write_back(await _mind_gateway_url(name), skill)
    log_event(
        log, "mind.skill.written_back", **_actor_fields(user), mind_name=name, skill=skill
    )
    return result


@app.delete("/api/minds/{name}/skills/{skill}")
async def api_mind_skill_remove(
    name: str, skill: str, user: dict = Depends(require_auth)
):
    result = await mind_skills.remove(await _mind_gateway_url(name), skill)
    log_event(
        log, "mind.skill.removed", **_actor_fields(user), mind_name=name, skill=skill
    )
    return result


@app.get("/api/minds/{name}/files/{tree}")
async def api_mind_files(name: str, tree: str, user: dict = Depends(require_auth)):
    """Every file under one of a Mind's two editable trees."""
    del user
    return await mind_files.fetch_tree(await _mind_gateway_url(name), tree)


@app.get("/api/minds/{name}/files/{tree}/content")
async def api_mind_file_content(
    name: str, tree: str, path: str = "", user: dict = Depends(require_auth)
):
    """One file's text, or the Mind's reason it cannot be shown as text."""
    del user
    return await mind_files.fetch_file(await _mind_gateway_url(name), tree, path)


@app.put("/api/minds/{name}/files/{tree}/content")
async def api_mind_file_save(
    name: str, tree: str, body: dict, user: dict = Depends(require_auth)
):
    """Save one file back to the Mind, in place.

    `revision` is what the read handed back; the Mind refuses with 409 if it
    no longer describes the file, which is what stops a stale tab from
    silently undoing whatever changed it.
    """
    # Coerced rather than validated, `text` would turn a missing field, a
    # null, or a dict into `""` or a Python repr — and the Mind's own
    # "text required" guard would never be reached. This route writes
    # executables that run every turn; truncating one to nothing on a
    # 200 OK is not an acceptable reading of a malformed body.
    path = body.get("path")
    text = body.get("text")
    revision = body.get("revision")
    if not isinstance(path, str) or not path:
        raise HTTPException(status_code=400, detail="path required")
    if not isinstance(text, str):
        raise HTTPException(status_code=400, detail="text required")
    # Required, not optional: a staleness check the caller may omit is one
    # a refactored fetch will omit, and then a save silently overwrites
    # whatever landed since the file was read.
    if not isinstance(revision, str) or not revision:
        raise HTTPException(
            status_code=400,
            detail="revision required — reopen the file and save again",
        )
    result = await mind_files.save_file(
        await _mind_gateway_url(name), tree, path, text, revision
    )
    log_event(
        log,
        "mind.file.saved",
        **_actor_fields(user),
        mind_name=name,
        tree=tree,
        path=path,
        # The bytes, not just the name: this writes hooks that run every
        # turn as the Mind's own user, and "a file changed" with no way to
        # tell which change is not an audit trail.
        size=result.get("size"),
        revision=result.get("revision"),
    )
    return result


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


# Hostnames that never resolve in the operator's browser: the placeholder
# .env.example ships for every LAN url, and the compose service names, which
# resolve on the container network and nowhere else.
_UNREACHABLE_HOSTS = frozenset({"hive-host", "hive-tools", "hive-comms", "hive-lucent"})


def _hive_tools_base_url() -> str:
    return os.getenv("HIVE_TOOLS_URL") or "http://hive-tools:9421"


def _hive_tools_console_url() -> str:
    """A hive-tools URL a *browser* can reach.

    The service-to-service base url is a compose hostname, which resolves for
    this container and for nothing the operator is sitting in front of.

    .env.example ships the LAN urls pointed at the literal placeholder host, so
    a fresh install that has not been filled in yet is treated as unconfigured
    rather than rendered as a link that opens a dead tab.
    """
    for candidate in (
        os.getenv("HIVE_TOOLS_PUBLIC_URL"),
        os.getenv("EDGE_HIVE_TOOLS_URL"),
        os.getenv("HIVE_TOOLS_URL"),
    ):
        candidate = (candidate or "").strip()
        if not candidate.startswith(("http://", "https://")):
            continue
        if urllib.parse.urlsplit(candidate).hostname in _UNREACHABLE_HOSTS:
            continue
        return candidate
    return ""


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


@app.get("/api/training/in-flight")
async def api_training_in_flight(user: dict = Depends(require_auth)):
    """The run holding the card, so a reloaded page can reattach to it."""
    del user
    return await _hive_tools_request("GET", "/training/in-flight")


@app.post("/api/training/kill")
async def api_training_kill(request: Request, user: dict = Depends(require_auth)):
    """Stop a run in flight.

    Restoring the voice stack and closing the ledger is the watcher's job,
    not this route's — two restore paths racing is how the voice stack
    comes back twice.
    """
    del user
    body = await request.json()
    if not isinstance(body, dict) or not str(body.get("name", "")).strip():
        raise HTTPException(status_code=400, detail="name is required")
    return await _hive_tools_request(
        "POST", "/training/kill", json_body={"name": str(body["name"]).strip()}
    )


@app.get("/api/training/logs")
async def api_training_logs(
    name: str, tail: int = 200, user: dict = Depends(require_auth)
):
    """Recent trainer output, for watching a run go by.

    Read-only in the strictest sense: nothing here starts, stops or reaps
    anything, so a closed tab is not an event the run can notice.
    """
    del user
    if not name.strip():
        raise HTTPException(status_code=400, detail="name is required")
    return await _hive_tools_request(
        "GET",
        "/training/logs",
        params={"name": name.strip(), "tail": max(1, min(tail, 2_000))},
    )


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
        quantization=body.get("quantization"),
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
    tools_installation = hive_tools_install_status()
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
            "installable": True,
            "installed": tools_installation["installed"],
            "installing": tools_installation["installing"],
            "repository": tools_installation["repository"],
            "path": tools_installation["path"],
            # Recreate, not restart: both the bind mount source and this
            # container's own HIVE_TOOLS_DIR are fixed when the container is
            # created, so `docker restart` re-reads neither.
            "path_hint": (
                "Set HIVE_TOOLS_DIR in the Hive's .env, then recreate the Hive "
                "container (docker compose up -d) to clone somewhere else."
            ),
            "placement_note": HIVE_TOOLS_PLACEMENT_NOTE,
            "configure_url": _hive_tools_console_url(),
            "configure_label": "Configure",
            "configure_hint": (
                "Set EDGE_HIVE_TOOLS_URL in the Hive's .env to an address your "
                "browser can reach, then recreate the Hive container, to link "
                "straight to the Hive Tools console from here."
            ),
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
            "installing": proxy_installation["installing"],
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
        installed = node.get("installed")
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
            # `path` stays the host path from the spec — the mount point this
            # container sees is not something the operator can act on.
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
    status["remote_configured"] = hive_tools_install_status()["installed"]
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
        complete=result["complete"],
        skipped=[r["file"] for r in result["locations"] if not r["written"]],
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
        complete=result["complete"],
        skipped=[r["file"] for r in result["locations"] if not r["written"]],
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



# ---------------------------------------------------------------------------
# Design sessions
# ---------------------------------------------------------------------------
DESIGN_PREFIX = "/api/design"


def _design_push_token() -> str:
    """The credential a Mind publishes design pages with.

    Deliberately its own, with no fallback to the house tokens. Every Mind
    in the hive already holds `COMMS_ADMIN_BEARER_TOKEN` and
    `COMMS_BEARER_TOKEN`, so either of those as the fallback would let any
    Mind — a kid's Windows box included — rewrite Daniel's requirements and
    press Build. Unset means the publish routes refuse rather than open.
    """
    return os.getenv("DESIGN_PUSH_TOKEN", "")


def require_design_writer(request: Request) -> dict:
    """Daniel's browser login, or the Mind's own bearer. Nothing else.

    Applied to every design route including the reads: a design session
    carries source text pulled off Daniel's disks.
    """
    header = request.headers.get("Authorization", "")
    if header.startswith("Bearer "):
        expected = _design_push_token()
        presented = header[7:]
        if not expected:
            raise HTTPException(
                status_code=503, detail="DESIGN_PUSH_TOKEN is not configured"
            )
        # Bytes, not str: compare_digest raises TypeError on a str holding
        # anything outside ASCII, and that 500 would read to a Mind as a
        # console that is down rather than a credential that was refused.
        if not hmac.compare_digest(
            presented.encode("utf-8", "surrogatepass"),
            expected.encode("utf-8", "surrogatepass"),
        ):
            raise HTTPException(status_code=401, detail="unauthorized")
        return {"sub": "mind", "via": "bearer"}
    user = get_current_user_from_request(request)
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required")
    return dict(user, via="cookie")


def _design_error(exc: Exception) -> HTTPException:
    """One mapping, so every design route refuses in the same shape."""
    if isinstance(exc, design_data.DesignNotFound):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, design_data.DesignConflict):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, design_data.DesignRefused):
        return HTTPException(status_code=409, detail=str(exc))
    return HTTPException(status_code=400, detail=str(exc))


@app.get("/design", response_class=HTMLResponse)
async def design_index(request: Request, user: dict = Depends(require_auth)):
    del user
    return _render_template(request, "design_list.html", path="/design")


@app.get("/design/{design_id}", response_class=HTMLResponse)
async def design_detail(
    design_id: str, request: Request, user: dict = Depends(require_auth)
):
    del user
    try:
        design_data.get_design_session(design_id)
    except design_data.DesignNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return _render_template(
        request, "design_detail.html", path="/design", design_id=design_id
    )


@app.get(f"{DESIGN_PREFIX}/sessions")
async def api_design_list(actor: dict = Depends(require_design_writer)):
    del actor
    rows = []
    for record in design_data.list_design_sessions():
        rows.append(
            {
                "id": record["id"],
                "title": record["title"],
                "conversation_id": record["conversation_id"],
                "surface": record["surface"],
                "updated_at": record["updated_at"],
                "counts": {page: len(record[page]) for page in design_data.PAGES},
            }
        )
    return {"sessions": rows}


@app.post(f"{DESIGN_PREFIX}/sessions")
async def api_design_create(
    payload: dict, actor: dict = Depends(require_design_writer)
):
    try:
        record = design_data.create_design_session(
            str(payload.get("title") or ""),
            conversation_id=payload.get("conversation_id"),
            surface=payload.get("surface"),
            mind_id=payload.get("mind_id"),
            repos=payload.get("repos") or [],
        )
    except design_data.DesignError as exc:
        raise _design_error(exc) from exc
    log_event(log, "design.created", **_actor_fields(actor), design_id=record["id"])
    return record


@app.get(f"{DESIGN_PREFIX}/sessions/{{design_id}}")
async def api_design_get(design_id: str, actor: dict = Depends(require_design_writer)):
    del actor
    try:
        record = design_data.get_design_session(design_id)
    except design_data.DesignError as exc:
        raise _design_error(exc) from exc
    return dict(
        record,
        pages={page: design_data.ordered_items(record, page) for page in design_data.PAGES},
        gates={page: design_data.can_advance(design_id, page) for page in design_data.PAGES},
    )


@app.delete(f"{DESIGN_PREFIX}/sessions/{{design_id}}")
async def api_design_delete(
    design_id: str, actor: dict = Depends(require_design_writer)
):
    try:
        design_data.delete_design_session(design_id)
    except design_data.DesignError as exc:
        raise _design_error(exc) from exc
    log_event(log, "design.deleted", **_actor_fields(actor), design_id=design_id)
    return {"deleted": True, "id": design_id}


@app.post(f"{DESIGN_PREFIX}/sessions/{{design_id}}/attach")
async def api_design_attach(
    design_id: str, payload: dict, actor: dict = Depends(require_design_writer)
):
    del actor
    try:
        record = design_data.attach_design_session(
            design_id,
            payload.get("conversation_id"),
            surface=payload.get("surface"),
            mind_id=payload.get("mind_id"),
        )
    except design_data.DesignError as exc:
        raise _design_error(exc) from exc
    # Anything submitted while nothing was attached is owed to this
    # conversation. Without this, "stored and read when next attached" is a
    # promise nothing keeps: re-attaching delivers none of it.
    task = asyncio.create_task(design_delivery.deliver_pending())
    _DESIGN_DELIVERIES.add(task)
    task.add_done_callback(_DESIGN_DELIVERIES.discard)
    return record


@app.post(f"{DESIGN_PREFIX}/sessions/{{design_id}}/pages/{{page}}/publish")
async def api_design_publish(
    design_id: str, page: str, payload: dict,
    actor: dict = Depends(require_design_writer),
):
    del actor
    try:
        return design_data.publish_page(
            design_id,
            page,
            payload.get("items") or {},
            revision=payload.get("revision"),
        )
    except design_data.DesignError as exc:
        raise _design_error(exc) from exc


@app.post(f"{DESIGN_PREFIX}/sessions/{{design_id}}/pages/{{page}}/submit")
async def api_design_submit(
    design_id: str, page: str, payload: dict,
    actor: dict = Depends(require_design_writer),
):
    del actor
    try:
        record = design_data.submit_page(
            design_id,
            page,
            items=payload.get("items") or {},
            action=str(payload.get("action") or "feedback"),
            revision=payload.get("revision"),
        )
    except design_data.DesignError as exc:
        raise _design_error(exc) from exc
    # Detached: the send adopts the conversation and waits out a whole model
    # turn, and the browser must not sit on that. The submission is already
    # stored, so a delivery that fails is retried rather than lost.
    #
    # The handle is kept until it finishes. A bare `create_task` holds no
    # strong reference, and a task collected mid-send loses the submission
    # exactly as silently as no task at all.
    task = asyncio.create_task(design_delivery.deliver_pending())
    _DESIGN_DELIVERIES.add(task)
    task.add_done_callback(_DESIGN_DELIVERIES.discard)
    return record


@app.post(f"{DESIGN_PREFIX}/sessions/{{design_id}}/pages/{{page}}/accept")
async def api_design_accept(
    design_id: str, page: str, payload: dict,
    actor: dict = Depends(require_design_writer),
):
    del actor
    try:
        record = design_data.accept_page(
            design_id, page, revision=payload.get("revision")
        )
    except design_data.DesignError as exc:
        raise _design_error(exc) from exc
    # Detached and handle-kept, for the reasons the submit route gives: the
    # send adopts the conversation and waits out a whole model turn, and a
    # bare create_task can be collected mid-send.
    task = asyncio.create_task(design_delivery.deliver_pending())
    _DESIGN_DELIVERIES.add(task)
    task.add_done_callback(_DESIGN_DELIVERIES.discard)
    return record


@app.post(f"{DESIGN_PREFIX}/trim-graph")
async def api_design_trim_graph(
    payload: dict, actor: dict = Depends(require_design_writer)
):
    del actor
    nodes = payload.get("nodes") or []
    return {
        "rows": design_graph.trim_call_graph(nodes),
        "entry_points_without_callers": design_graph.entry_points_without_callers(nodes),
    }


@app.get(f"{DESIGN_PREFIX}/symbols")
async def api_design_symbol(
    mind: str, path: str, name: str, repo: str = "", design_id: str = "",
    actor: dict = Depends(require_design_writer),
):
    """Resolve a function through the Mind whose disk it lives on.

    The repository is named, never inferred. A Mind asked for a bare name
    resolves against whichever checkout it happens to have declared first,
    which on a host holding three of them returns real source from the wrong
    one — with `resolved: true` and a line span that checks out.
    """
    del actor
    if not repo and design_id:
        targets = design_data.get_design_session(design_id)["repos"]
        if len(targets) == 1:
            repo = targets[0]
    if not repo:
        raise HTTPException(
            status_code=400,
            detail="Name the repository — this host holds more than one checkout",
        )
    gateway_url = await _mind_gateway_url(mind)
    return await mind_symbols.resolve(gateway_url, repo, path, name)


@app.get(f"{DESIGN_PREFIX}/symbol-roots")
async def api_design_symbol_roots(
    mind: str, actor: dict = Depends(require_design_writer)
):
    del actor
    gateway_url = await _mind_gateway_url(mind)
    return await mind_symbols.roots(gateway_url)


@app.get("/health")
async def health():
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn

    import config

    uvicorn.run(app, host=config.HOST, port=config.PORT, log_config=None, access_log=False)
