"""Standalone web terminal: browser xterm.js attached to a live mind session.

Split out of spark_to_bloom's /terminal page so the terminal has its own
origin (terminal.sparktobloom.com). Auth is not reimplemented — auth.py is
a verbatim copy of the hive site's, sharing HIVE_DB_PATH and
HIVE_SECRET_KEY, so a session issued by either site verifies on both.
"""

import asyncio
import json
import os
import re
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from contextlib import asynccontextmanager

import httpx
import websockets
from websockets.exceptions import WebSocketException

from fastapi import Depends, FastAPI, Form, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import auth
from auth import (
    clear_session_cookie,
    get_current_user_from_request,
    init_auth_db,
    require_auth,
    set_session_cookie,
    verify_user_credentials,
)
from config import BASE_DIR


@asynccontextmanager
async def lifespan(app: FastAPI):
    del app
    # No seed_first_user here: the auth db is shared with (and owned by)
    # the hive site, so users already exist by the time this starts.
    init_auth_db()
    yield


app = FastAPI(
    title="Terminal",
    description="Standalone web terminal for the hive mind",
    version="1.0.0",
    lifespan=lifespan,
)

app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")


def _gateway_base_url() -> str:
    return os.getenv("GATEWAY_API_URL") or "http://hive-comms:8424"


def _gateway_headers() -> dict:
    token = os.getenv("COMMS_BEARER_TOKEN", "")
    if token:
        return {"Authorization": f"Bearer {token}"}
    return {}


def _voice_api_url() -> str:
    return os.environ.get("VOICE_API_URL", "http://hive-mind-voice:8422").rstrip("/")


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


def _safe_next_path(next_path: str | None, fallback: str = "/terminal") -> str:
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


async def _proxy_session_events(session_id: str):
    url = f"{_gateway_base_url().rstrip('/')}/sessions/{session_id}/events"
    headers = {"Accept": "text/event-stream", **_gateway_headers()}
    timeout = httpx.Timeout(connect=10.0, read=300.0, write=10.0, pool=10.0)

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream("GET", url, headers=headers) as response:
                response.raise_for_status()
                async for raw_line in response.aiter_lines():
                    line = raw_line.strip()
                    if not line.startswith("data: "):
                        continue
                    yield f"{line}\n\n"
    except httpx.HTTPStatusError as exc:
        payload = {"type": "system", "content": f"upstream_error: {exc.response.status_code}"}
        yield f"data: {json.dumps(payload)}\n\n"
    except httpx.RequestError as exc:
        payload = {"type": "system", "content": f"upstream_error: {exc}"}
        yield f"data: {json.dumps(payload)}\n\n"


@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    if not get_current_user_from_request(request):
        return _login_redirect_for(request)
    return RedirectResponse(url="/terminal", status_code=303)


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, next: str = "/terminal"):
    current_user = get_current_user_from_request(request)
    if current_user:
        return RedirectResponse(url=_safe_next_path(next), status_code=303)
    return _render_template(request, "login.html", next_path=_safe_next_path(next))


@app.post("/login")
async def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    next: str = Form("/terminal"),
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


@app.get("/terminal", response_class=HTMLResponse)
async def terminal(request: Request):
    if not get_current_user_from_request(request):
        return _login_redirect_for(request)
    return _render_template(request, "terminal.html")


def _relative_age(now: int, last_active) -> str:
    try:
        seconds = max(0, int(now - float(last_active or 0)))
    except (TypeError, ValueError):
        return ""
    if seconds < 60:
        return f"{seconds}s ago"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86400}d ago"


@app.post("/api/terminal/tts")
async def api_terminal_tts(request: Request, user: dict = Depends(require_auth)):
    """Proxy text to the voice server's /tts endpoint and pipe back OGG audio.

    Only called when the user has the speaker toggle on, so the GPU isn't
    loaded for silent turns.
    """
    del user
    body = await request.json()
    text = (body.get("text") or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="text is required")
    voice_id = (body.get("voice_id") or "default").strip() or "default"
    payload = json.dumps({"text": text, "voice_id": voice_id}).encode()
    url = f"{_voice_api_url()}/tts"

    def _do_post():
        req = urllib.request.Request(
            url, data=payload,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=120) as resp:
            return resp.read(), resp.headers.get("content-type", "audio/ogg")

    try:
        audio, ctype = await asyncio.to_thread(_do_post)
    except urllib.error.HTTPError as exc:
        raise HTTPException(status_code=exc.code, detail="voice server rejected request") from exc
    except OSError as exc:
        raise HTTPException(status_code=502, detail=f"voice server unavailable: {exc}") from exc
    return Response(content=audio, media_type=ctype)


@app.get("/api/terminal/sessions")
async def api_terminal_sessions(user: dict = Depends(require_auth)):
    """Flat, mind-labeled session list — the /terminal session picker.

    Each row is "drop into this conversation," not "pick a mind": mind name,
    short mind_id, age, and status, sorted most-recently-active first.
    """
    del user
    try:
        minds = await _gateway_json("/broker/minds")
        if not isinstance(minds, list):
            minds = []
    except Exception:
        minds = []
    mind_names = {m.get("id"): m.get("name", "mind") for m in minds if isinstance(m, dict)}

    try:
        sessions = await _gateway_json("/sessions")
        if not isinstance(sessions, list):
            sessions = []
    except Exception:
        sessions = []

    now = int(time.time())
    rows = []
    for s in sessions:
        if not isinstance(s, dict) or s.get("owner_type") == "scheduler":
            continue
        mind_id = s.get("mind_id") or ""
        rows.append({
            "id": s.get("id"),
            "mind_id": mind_id,
            "mind_name": mind_names.get(mind_id, mind_id or "mind"),
            "short_id": (s.get("id") or "")[:8],
            "status": s.get("status"),
            "last_active": s.get("last_active"),
            "age": _relative_age(now, s.get("last_active")),
            "summary": (s.get("summary") or "").strip(),
            # Lineage, so a tile whose session rotated can identify its
            # actual replacement instead of adopting whichever sibling
            # session happens to be live on the same mind.
            "rotated_from": s.get("rotated_from") or "",
        })
    rows.sort(key=lambda r: -float(r.get("last_active") or 0))
    return rows


@app.post("/api/terminal/sessions")
async def api_terminal_create_session(request: Request, user: dict = Depends(require_auth)):
    """The "new session against mind X" affordance for when nothing relevant is live."""
    del user
    body = await request.json()
    mind_id = (body.get("mind_id") or "").strip()
    if not mind_id:
        raise HTTPException(status_code=400, detail="mind_id is required")
    return await _create_gateway_session(mind_id)


async def _create_gateway_session(mind_id: str) -> dict:
    # client_ref is the primary key of the gateway's active_sessions binding
    # table, so it has to be unique per terminal tile. A shared constant makes
    # every open tile overwrite the same row: rotation arms the wrong session
    # and carry-forward memory lands in a different tile than it was written
    # for.
    client_ref = f"terminal-{uuid.uuid4()}"

    def _do_post():
        url = f"{_gateway_base_url().rstrip('/')}/sessions"
        data = json.dumps({
            "mind_id": mind_id,
            "model": "sonnet",
            "owner_type": "web",
            "owner_ref": "terminal",
            "client_ref": client_ref,
        }).encode()
        req = urllib.request.Request(
            url, data=data,
            headers={"Content-Type": "application/json", **_gateway_headers()},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))

    try:
        return await asyncio.to_thread(_do_post)
    except urllib.error.HTTPError as exc:
        raise HTTPException(status_code=exc.code, detail="Failed to create session") from exc
    except OSError as exc:
        raise HTTPException(status_code=502, detail=f"Gateway unavailable: {exc}") from exc


@app.get("/api/minds")
async def api_minds(user: dict = Depends(require_auth)):
    del user
    return await _gateway_json("/broker/minds")


def _labels_db() -> sqlite3.Connection:
    """Terminal session labels (name + color) live next to the auth tables.

    Server-side so they follow the user across devices — localStorage kept
    them per-browser, which read as losing the session on the phone.
    """
    conn = auth._connect()
    conn.execute(
        """CREATE TABLE IF NOT EXISTS terminal_labels (
               session_id TEXT PRIMARY KEY,
               name TEXT NOT NULL DEFAULT '',
               color TEXT NOT NULL DEFAULT '',
               updated_at INTEGER NOT NULL
           )"""
    )
    conn.commit()
    return conn


_LABEL_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{3,8}$")


@app.get("/api/terminal/labels")
async def api_terminal_labels(user: dict = Depends(require_auth)):
    del user
    conn = _labels_db()
    try:
        rows = conn.execute("SELECT session_id, name, color FROM terminal_labels").fetchall()
    finally:
        conn.close()
    return {r["session_id"]: {"name": r["name"], "color": r["color"]} for r in rows}


@app.put("/api/terminal/labels/{session_id}")
async def api_terminal_label_put(
    session_id: str, request: Request, user: dict = Depends(require_auth)
):
    del user
    body = await request.json()
    name = (body.get("name") or "").strip()[:40]
    color = (body.get("color") or "").strip()
    if color and not _LABEL_COLOR_RE.match(color):
        raise HTTPException(status_code=400, detail="color must be a hex value")
    conn = _labels_db()
    try:
        if not name and not color:
            conn.execute("DELETE FROM terminal_labels WHERE session_id = ?", (session_id,))
        else:
            conn.execute(
                """INSERT INTO terminal_labels (session_id, name, color, updated_at)
                   VALUES (?, ?, ?, strftime('%s','now'))
                   ON CONFLICT(session_id) DO UPDATE
                   SET name = excluded.name, color = excluded.color,
                       updated_at = excluded.updated_at""",
                (session_id, name, color),
            )
        conn.commit()
    finally:
        conn.close()
    return {"ok": True, "session_id": session_id, "name": name, "color": color}


def _gateway_ws_url(session_id: str, cols: str = "80", rows: str = "24") -> str:
    ws_base = _gateway_base_url().rstrip("/").replace("https://", "wss://").replace("http://", "ws://")
    query = urllib.parse.urlencode({"cols": cols, "rows": rows})
    return f"{ws_base}/sessions/{session_id}/attach?{query}"


async def _pump_terminal_ws(browser_ws: WebSocket, mind_ws) -> None:
    """Bridge the browser's terminal WS and hive-comms' attach WS.

    Frame types are load-bearing on the browser→mind leg: BINARY frames
    are raw terminal bytes, TEXT frames are JSON control messages
    (resize) — so TEXT is forwarded as TEXT (websockets sends str frames
    as TEXT), never re-encoded into the byte stream. Whichever side
    closes first ends the bridge — an attached pty and a browser tab
    have no independent life of their own once either end is gone.
    """
    async def browser_to_mind() -> None:
        while True:
            msg = await browser_ws.receive()
            if msg.get("type") == "websocket.disconnect":
                return
            if msg.get("bytes"):
                await mind_ws.send(msg["bytes"])
            elif msg.get("text"):
                await mind_ws.send(msg["text"])

    async def mind_to_browser() -> None:
        async for data in mind_ws:
            if isinstance(data, str):
                data = data.encode()
            await browser_ws.send_bytes(data)

    tasks = [asyncio.ensure_future(browser_to_mind()), asyncio.ensure_future(mind_to_browser())]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    except WebSocketDisconnect:
        pass
    finally:
        for task in tasks:
            task.cancel()


def _relayable_close_code(code: int | None) -> int | None:
    """The gateway close code to pass to the browser, if any.

    Every real code relays, whatever its range. The ones that matter are
    not all private-use: 4410 is "session closed", 1012 is "another window
    took the keyboard", 1008 is "the mind refused the terminal" — the last
    two are how a tile knows to stand down instead of reconnecting. An
    earlier 4000-4999 filter swallowed 1012, so an evicted desktop tile
    read the eviction as a dropped connection and reattached, which
    evicted the phone, which reattached, which evicted the desktop: a
    tug-of-war that repainted both terminals about once a second and
    cross-fed each tile the other's geometry (a 140-column repaint
    shredded into a 44-column phone).

    1005/1006 are synthetic "no close frame arrived" markers that cannot
    legally go on the wire, so they relay as nothing.
    """
    if not code or code in (1005, 1006):
        return None
    return code


@app.websocket("/api/terminal/attach/{session_id}")
async def ws_terminal_attach(websocket: WebSocket, session_id: str):
    """Reverse-proxy a browser terminal WS into hive-comms' session attach.

    Gated by the same session cookie as every other page here; an
    unauthenticated caller is rejected outright since this is full shell
    access.
    """
    user = get_current_user_from_request(websocket)
    if user is None:
        await websocket.close(code=4401)
        return

    await websocket.accept()
    cols = websocket.query_params.get("cols") or "80"
    rows = websocket.query_params.get("rows") or "24"
    try:
        async with websockets.connect(
            _gateway_ws_url(session_id, cols=cols, rows=rows),
            additional_headers=_gateway_headers(),
        ) as mind_ws:
            await _pump_terminal_ws(websocket, mind_ws)
            code = _relayable_close_code(mind_ws.close_code)
            if code:
                try:
                    await websocket.close(code=code, reason=mind_ws.close_reason or "")
                except RuntimeError:
                    pass  # browser already gone
    except (OSError, WebSocketException):
        await websocket.close(code=1011, reason="terminal unreachable")


@app.get("/api/console/{session_id}/stream")
async def api_console_stream(session_id: str, user: dict = Depends(require_auth)):
    del user
    return StreamingResponse(
        _proxy_session_events(session_id),
        media_type="text/event-stream",
        headers={
            # SSE must never be buffered by intermediaries — a buffered
            # stream delivers events in delayed bursts and idle timeouts
            # sever it mid-turn.
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
        },
    )


async def _drain_gateway_message(session_id: str, text: str) -> None:
    url = f"{_gateway_base_url().rstrip('/')}/sessions/{session_id}/message"

    def _do_post():
        data = json.dumps({"content": text}).encode()
        req = urllib.request.Request(
            url, data=data,
            headers={"Content-Type": "application/json", **_gateway_headers()},
        )
        try:
            with urllib.request.urlopen(req, timeout=300) as resp:
                while True:
                    chunk = resp.read(4096)
                    if not chunk:
                        break
        except Exception:
            pass

    await asyncio.to_thread(_do_post)


@app.post("/api/console/{session_id}/message")
async def api_console_send_message(
    session_id: str, request: Request, user: dict = Depends(require_auth)
):
    del user
    body = await request.json()
    text = (body.get("text") or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="text is required")
    asyncio.create_task(_drain_gateway_message(session_id, text))
    return {"status": "sent"}


@app.delete("/api/terminal/sessions/{session_id}")
async def api_terminal_session_delete(session_id: str, user: dict = Depends(require_auth)):
    del user

    def _do_delete():
        url = f"{_gateway_base_url().rstrip('/')}/sessions/{session_id}"
        req = urllib.request.Request(url, headers=_gateway_headers(), method="DELETE")
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))

    try:
        return await asyncio.to_thread(_do_delete)
    except urllib.error.HTTPError as exc:
        raise HTTPException(status_code=exc.code, detail="Failed to delete session") from exc
    except OSError as exc:
        raise HTTPException(status_code=502, detail=f"Gateway unavailable: {exc}") from exc


@app.get("/health")
async def health():
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn

    import config

    uvicorn.run(app, host=config.HOST, port=config.PORT)
