"""Hive admin site: graph, memory, and standing rules for the hive mind.

Auth lives in the hive itself (see auth.py) so a cloned hive is
self-contained; the auth db and secret are shared with the terminal site.
"""

import asyncio
import json
import os
import re
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
from contextlib import asynccontextmanager

import httpx
from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    del app
    init_auth_db()
    seed_first_user()
    yield


app = FastAPI(
    title="Hive",
    description="Admin console for the hive mind",
    version="1.0.0",
    lifespan=lifespan,
)

app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")


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


def _safe_next_path(next_path: str | None, fallback: str = "/graph") -> str:
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
    return RedirectResponse(url="/graph", status_code=303)


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, next: str = "/graph"):
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
    aliases = [None]
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


@app.get("/health")
async def health():
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn

    import config

    uvicorn.run(app, host=config.HOST, port=config.PORT)
