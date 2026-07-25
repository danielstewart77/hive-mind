"""Hive admin site: graph, memory, and standing rules for the hive mind.

Auth lives in the hive itself (see auth.py) so a cloned hive is
self-contained; the auth db and secret are shared with the terminal site.
"""

import asyncio
import json
import os
import re
import shlex
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
import uuid
from contextlib import asynccontextmanager
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
from config import BASE_DIR
from graph_data import get_graph_data
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    del app
    init_auth_db()
    seed_first_user()
    global _canvas_elements
    _canvas_elements = _load_canvas_state()
    yield


app = FastAPI(
    title="Hive",
    description="Admin console for the hive mind",
    version="1.0.0",
    lifespan=lifespan,
)

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
        return _render_template(
            request,
            "login.html",
            error="Invalid username or password.",
            next_path=_safe_next_path(next),
        )

    response = RedirectResponse(url=_safe_next_path(next), status_code=303)
    set_session_cookie(response, user)
    return response


@app.post("/logout")
async def logout():
    response = RedirectResponse(url="/login", status_code=303)
    clear_session_cookie(response)
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


@app.get("/minds/{mind_id}", response_class=HTMLResponse)
async def mind_detail_page(request: Request, mind_id: str):
    return _console_page(request, "mind_detail.html", mind_id=mind_id)


@app.get("/outposts", response_class=HTMLResponse)
async def outposts_page(request: Request):
    return _console_page(request, "outposts.html")


@app.get("/outposts/new", response_class=HTMLResponse)
async def new_outpost_page(request: Request):
    return _console_page(request, "outpost_new.html")


@app.get("/tools", response_class=HTMLResponse)
async def tools_page(request: Request):
    return _console_page(request, "tools.html")


@app.get("/activity", response_class=HTMLResponse)
async def activity_page(request: Request):
    return _console_page(request, "activity.html")


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
        _service_health(f"{_gateway_base_url().rstrip('/')}/health"),
        _service_health(f"{_lucent_base_url().rstrip('/')}/health"),
    )
    tools = hive_tools_status()
    tools_health = (
        await _service_health(f"{_hive_tools_base_url().rstrip('/')}/health")
        if tools.get("present")
        else {"reachable": False, "detail": "Not installed"}
    )
    return {
        "counts": {"minds": len(minds), "sessions": len(sessions)},
        "minds": build_mind_registry(minds),
        "sessions": sessions,
        "services": {
            "comms": comms_health,
            "lucent": lucent_health,
            "tools": {"installed": bool(tools.get("present")), **tools_health},
        },
    }


def _outpost_rows(registry: list[dict]) -> list[dict]:
    rows = []
    for mind in registry:
        local = mind.get("local_config") or {}
        config = local.get("config") or {}
        is_remote = local == {} or bool(config.get("remote"))
        is_outpost = is_remote or bool(config.get("role")) or bool(config.get("deployment"))
        if not is_outpost:
            continue
        gateway_url = mind.get("gateway_url") or ""
        parsed = urllib.parse.urlparse(gateway_url)
        rows.append(
            {
                "name": mind.get("name") or "Unnamed",
                "id": mind.get("id") or "",
                "host": parsed.hostname or "Remote machine",
                "gateway_url": gateway_url,
                "role": config.get("role") or "Connected",
                "deployment": config.get("deployment") or "Remote",
                "harness": mind.get("harness") or config.get("harness") or "Unknown",
                "model": mind.get("model") or config.get("default_model") or "Unknown",
                "last_seen": mind.get("last_seen"),
            }
        )
    return sorted(rows, key=lambda row: str(row["name"]).lower())


@app.get("/api/outposts")
async def api_outposts(user: dict = Depends(require_auth)):
    del user
    registry = build_mind_registry(await _broker_minds_safe())
    return {"outposts": _outpost_rows(registry)}


_OUTPOST_ROLES = {"operator", "satellite"}
_OUTPOST_DEPLOYMENTS = {"systemd", "container", "windows-task"}
_OUTPOST_HARNESSES = {
    "claude_cli_claude",
    "claude_cli_ollama",
    "claude_sdk_claude",
    "claude_sdk_ollama",
    "codex_cli_codex",
    "codex_cli_ollama",
    "codex_sdk_codex",
    "codex_sdk_ollama",
}
_OUTPOST_SURFACES = {"telegram", "discord"}


def _build_outpost_bootstrap(body: dict) -> str:
    name = str(body.get("name") or "").strip().lower()
    role = str(body.get("role") or "")
    deployment = str(body.get("deployment") or "")
    harness = str(body.get("harness") or "")
    surfaces = body.get("surfaces") or []
    try:
        port = int(body.get("port", 8421))
    except (TypeError, ValueError) as exc:
        raise ValueError("Port must be a number") from exc
    if not re.fullmatch(r"[a-z][a-z0-9-]{1,31}", name):
        raise ValueError("Name must be 2 to 32 lowercase letters, numbers, or hyphens")
    if role not in _OUTPOST_ROLES:
        raise ValueError("Unknown Outpost role")
    if deployment not in _OUTPOST_DEPLOYMENTS:
        raise ValueError("Unknown deployment type")
    if harness not in _OUTPOST_HARNESSES:
        raise ValueError("Unknown harness")
    if not isinstance(surfaces, list) or any(item not in _OUTPOST_SURFACES for item in surfaces):
        raise ValueError("Unknown conversation surface")
    if port < 1024 or port > 65535:
        raise ValueError("Port must be between 1024 and 65535")
    args = [
        "./setup.sh",
        "--name",
        name,
        "--role",
        role,
        "--deployment",
        deployment,
        "--harness",
        harness,
        "--surfaces",
        ",".join(surfaces),
        "--port",
        str(port),
    ]
    setup_command = " ".join(shlex.quote(part) for part in args)
    return (
        "git clone https://github.com/danielstewart77/hive-outpost.git && "
        f"cd hive-outpost && {setup_command}"
    )


@app.post("/api/outposts/bootstrap")
async def api_outpost_bootstrap(request: Request, user: dict = Depends(require_auth)):
    del user
    try:
        body = await request.json()
        command = _build_outpost_bootstrap(body if isinstance(body, dict) else {})
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"command": command}


@app.get("/api/tools")
async def api_tools(user: dict = Depends(require_auth)):
    del user
    status = hive_tools_status()
    health = (
        await _service_health(f"{_hive_tools_base_url().rstrip('/')}/health")
        if status.get("present")
        else {"reachable": False, "detail": "Not installed"}
    )
    return {**status, "health": health}


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
    del user
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
    return await _lucent_request("POST", "/memory/store", json_body=payload)


@app.put("/api/rules/{rule_id}")
async def api_rules_update(rule_id: str, request: Request, user: dict = Depends(require_auth)):
    del user
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
    return await _lucent_request("PUT", f"/memory/{rule_id}", json_body=payload)


@app.delete("/api/rules/{rule_id}")
async def api_rules_delete(rule_id: str, user: dict = Depends(require_auth)):
    del user
    return await _lucent_request("DELETE", f"/memory/{rule_id}")


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
    return {"ok": True, "id": data.get("id"), "type": data.get("type")}


@app.get("/canvas/render.svg")
async def canvas_render_svg():
    """Standalone SVG snapshot of the board, for Skippy to rasterize and read."""
    return Response(content=_canvas_to_svg(), media_type="image/svg+xml")


@app.post("/canvas/submit")
async def canvas_submit(request: Request):
    """Daniel's 'I'm done drawing' poke. Fires a Skippy turn via the broker."""
    if not get_current_user_from_request(request):
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


_SETTINGS_SECTIONS = {"general", "access", "connections", "voice", "advanced"}


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
    return {"minds": build_mind_registry(broker_minds)}


@app.get("/api/settings/hive-comms/sessions")
async def api_settings_hive_comms_sessions(user: dict = Depends(require_auth)):
    del user
    sessions = await _gateway_json("/sessions")
    if not isinstance(sessions, list):
        sessions = []
    return {"sessions": sessions}


@app.delete("/api/settings/hive-comms/sessions/{session_id}")
async def api_settings_hive_comms_session_delete(
    session_id: str, user: dict = Depends(require_auth)
):
    del user
    url = f"{_gateway_base_url().rstrip('/')}/sessions/{session_id}"
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.delete(url, headers=_gateway_headers())
        if resp.status_code >= 400:
            raise HTTPException(status_code=resp.status_code, detail=resp.text)
        return resp.json() if resp.content else {"ok": True}


def _hive_tools_base_url() -> str:
    return os.getenv("HIVE_TOOLS_URL") or "http://hive-tools:9421"


async def _service_health(url: str) -> dict:
    """Best-effort health probe — used purely for status display, never gates a route."""
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            return {"reachable": True, "detail": resp.json() if resp.content else {}}
    except Exception as exc:
        return {"reachable": False, "detail": str(exc)}


@app.get("/api/settings/hive-tools")
async def api_settings_hive_tools(user: dict = Depends(require_auth)):
    del user
    status = hive_tools_status()
    status["container_health"] = await _service_health(f"{_hive_tools_base_url()}/health")
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
    del user
    data = await file.read()
    try:
        target = save_voice_ref(name, data)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "path": str(target), "size_bytes": len(data)}


@app.get("/api/settings/downloads")
async def api_settings_downloads(user: dict = Depends(require_auth)):
    del user
    return {"downloads": list_downloads()}


@app.get("/downloads/{filename}")
async def download_asset(filename: str, user: dict = Depends(require_auth)):
    del user
    path = resolve_download_path(filename)
    if path is None:
        raise HTTPException(status_code=404, detail="file not found")
    return FileResponse(path, filename=filename)


@app.get("/api/settings/new-mind/check-name")
async def api_settings_check_mind_name(name: str, user: dict = Depends(require_auth)):
    """The one safely-automatable piece of mind creation today: name
    availability. Everything else (personality, soul, harness choice)
    stays conversational — see the /settings new-mind tab."""
    del user
    normalized = name.strip().lower()
    broker_minds = await _broker_minds_safe()
    taken = any(
        isinstance(m, dict) and (m.get("name") or "").lower() == normalized
        for m in broker_minds
    )
    return {"name": normalized, "available": bool(normalized) and not taken}


@app.get("/health")
async def health():
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn

    import config

    uvicorn.run(app, host=config.HOST, port=config.PORT)
