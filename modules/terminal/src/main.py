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
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from contextlib import asynccontextmanager

import httpx
import websockets
from websockets.exceptions import WebSocketException

from fastapi import (
    Depends,
    FastAPI,
    File,
    Form,
    HTTPException,
    Request,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import auth
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    del app
    init_auth_db()
    # In the hive deployment the auth db is shared with, and owned by, the
    # hive site, so users already exist by the time this starts and the
    # seed variables are unset — making this call a no-op there.
    #
    # A standalone deployment has no sibling site to have created an
    # account, so without this it comes up with an empty users table, a
    # working login page, and no way in: every attempt renders "invalid
    # username or password", which is a truthful-looking message for
    # entirely the wrong reason.
    seed_first_user()
    yield


app = FastAPI(
    title="Terminal",
    description="Standalone web terminal for the hive mind",
    version="1.0.0",
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
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


# Two minutes of Opus is well under a megabyte; the cap is loose enough that
# no real recording meets it and tight enough that the route is not a place
# to post a film.
MAX_STT_AUDIO_BYTES = 25 * 1024 * 1024
# C0 and C1, less the whitespace the collapse above already handles.
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")


def _multipart_audio(filename: str, content_type: str, audio: bytes) -> tuple[bytes, dict]:
    """One-file multipart body, built by hand.

    The voice server reads an UploadFile, so the audio has to arrive as a
    form part rather than a raw body — and both the filename and the content
    type matter downstream: /stt picks its decode path from them.
    """
    boundary = uuid.uuid4().hex
    safe_name = re.sub(r'[\r\n"]', "", filename) or "audio"
    safe_type = re.sub(r"[\r\n]", "", content_type) or "application/octet-stream"
    head = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{safe_name}"\r\n'
        f"Content-Type: {safe_type}\r\n\r\n"
    ).encode()
    body = head + audio + f"\r\n--{boundary}--\r\n".encode()
    return body, {
        "Content-Type": f"multipart/form-data; boundary={boundary}",
        "Content-Length": str(len(body)),
    }


@app.post("/api/terminal/stt")
async def api_terminal_stt(
    file: UploadFile = File(...),
    user: dict = Depends(require_auth),
):
    """Proxy recorded audio to the voice server's /stt and hand back the text.

    The browser's MediaRecorder produces WebM/Opus on Chrome, MP4 on Safari
    and Ogg/Opus on Firefox. Only the last takes the voice server's OGG
    branch; the other two decode anyway, because faster-whisper probes the
    container rather than trusting the extension.
    """
    del user
    audio = await file.read()
    if not audio:
        raise HTTPException(status_code=400, detail="audio is required")
    if len(audio) > MAX_STT_AUDIO_BYTES:
        raise HTTPException(status_code=413, detail="audio is too large")
    payload, headers = _multipart_audio(
        file.filename or "audio.webm",
        file.content_type or "application/octet-stream",
        audio,
    )
    url = f"{_voice_api_url()}/stt"

    def _do_post():
        req = urllib.request.Request(url, data=payload, headers=headers)
        with urllib.request.urlopen(req, timeout=120) as resp:
            return json.loads(resp.read().decode("utf-8"))

    try:
        result = await asyncio.to_thread(_do_post)
    except urllib.error.HTTPError as exc:
        raise HTTPException(status_code=exc.code, detail="voice server rejected the audio") from exc
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=502, detail=f"voice server unavailable: {exc}") from exc

    if not isinstance(result, dict):
        raise HTTPException(status_code=502, detail="voice server returned an unexpected body")

    # faster-whisper joins its segments with a space each of which already
    # carries a leading one, so a multi-sentence transcript arrives with
    # doubled gaps. Collapse them; the words themselves are untouched.
    raw = result.get("text")
    text = re.sub(r"\s+", " ", raw if isinstance(raw, str) else "").strip()
    # Whatever comes back is typed straight into a pty. A newline would be a
    # submit the user never pressed — the whitespace collapse already covers
    # that — but backspace, escape and the rest of C0 are not whitespace, and
    # would erase the line the user is holding or be read as an escape
    # sequence by whatever is running in the pane.
    text = _CONTROL_CHARS.sub("", text)
    return {"text": text}


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
        if (
            not isinstance(s, dict)
            or s.get("owner_type") == "scheduler"
            or s.get("status") == "closed"
        ):
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
    # Explicitly uncacheable, for the reason the labels route gives: with
    # no directive a browser may cache a 200 GET heuristically, and a
    # stale read of *this* list is worse than a stale label. It is what
    # the tile re-reads the instant a session is suspended or ended, so a
    # cached copy shows the session exactly where it was — the action
    # having plainly succeeded, and nothing having visibly happened.
    return JSONResponse(rows, headers={"Cache-Control": "no-store"})


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
        # No model: the mind's own default is registered in the broker, and
        # the gateway falls back to it when the caller names none. A model
        # here overrode every mind's configured default with one hardcoded
        # alias, so the browser terminal ran a different model than the same
        # mind's Telegram surface.
        data = json.dumps({
            "mind_id": mind_id,
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


# Same reasoning as the session list: it changes when a mind comes or
# goes, and a cached copy offers a mind that is not there.
@app.get("/api/minds")
async def api_minds(user: dict = Depends(require_auth)):
    del user
    return JSONResponse(
        await _gateway_json("/broker/minds"), headers={"Cache-Control": "no-store"}
    )


def _rename_headers() -> dict:
    """The gateway headers plus the one credential that may change a name.

    Two credentials on one request, deliberately. The service token gets past
    the gateway's global gate; the rename token is what the name route itself
    demands, and it is the same small secret this site used to check locally —
    a copy of it escaping is worth exactly one renamed conversation, which is
    why the capability was never guarded by the key that opens everything.
    """
    token = os.getenv("TERMINAL_LABELS_TOKEN", "")
    headers = dict(_gateway_headers())
    if token:
        headers["X-Rename-Token"] = token
    return headers


def require_auth_or_label_writer(request: Request) -> dict:
    """A logged-in person, or whoever holds the label token.

    Labels are the operator's, and the mind edits them on their behalf
    when a rename arrives over Telegram. It has no browser, so it cannot
    hold a session cookie — and minting it one would mean issuing a login
    that never expires and belongs to nobody.

    **Deliberately not ``COMMS_BEARER_TOKEN``.** That secret opens every
    route on the mind, the one that types into a live pane included, and
    a pane runs the harness with permissions skipped. Guarding the
    smallest capability in the system with the largest key in it is
    backwards: this token renames a conversation and does nothing else,
    so a copy that escapes — into a log, a screenshot, a transcript — is
    worth exactly one renamed conversation.

    Unset means no bearer is accepted at all, rather than an empty string
    matching an absent header.

    Compared in constant time: a token checked with ``==`` leaks its own
    length and prefix to anything that can time the answer.
    """
    token = os.getenv("TERMINAL_LABELS_TOKEN", "")
    header = request.headers.get("authorization", "")
    if token and header.startswith("Bearer "):
        if secrets.compare_digest(header[len("Bearer ") :].strip(), token):
            return {"username": "label-writer", "service": True}
    # Not a label write, or not a token we know. Fall through to the
    # cookie so a person is answered exactly as before, 401 included.
    return require_auth(request)


@app.get("/api/terminal/labels")
async def api_terminal_labels(user: dict = Depends(require_auth_or_label_writer)):
    """Every name, read from the one row that holds it. Explicitly uncacheable.

    This site kept its own `terminal_labels` table until the names moved onto
    the gateway's session row. The shape of the answer is unchanged because it
    has readers beyond this page — `mini-boss` reads it with a login cookie,
    which is the reason the route survives the move at all rather than every
    caller being pointed at the gateway.

    Without a cache directive a browser may cache a 200 GET heuristically, and
    a stale read here is indistinguishable from a current one: it reads as the
    server having lost names it still holds. The client asks with ``no-store``
    as well; both ends, because either alone is one deployment away from being
    the only one.
    """
    del user
    names = await _gateway_json("/sessions/names")
    if not isinstance(names, dict):
        names = {}
    return JSONResponse(
        {
            session_id: {
                "name": (value or {}).get("name") or "",
                "color": (value or {}).get("color") or "",
            }
            for session_id, value in names.items()
        },
        headers={"Cache-Control": "no-store"},
    )


@app.put("/api/terminal/labels/{session_id}")
async def api_terminal_label_put(
    session_id: str, request: Request, user: dict = Depends(require_auth_or_label_writer)
):
    """Set a name, a colour, or both, on the gateway's session row.

    Only the fields the caller actually sent are forwarded. Sending both
    unconditionally is how a client that knew about names blanked a colour
    picked at the tile, and how a tab working from a minutes-stale cache
    overwrote the other tab's colour while changing only a name.

    The gateway's refusals are passed through as themselves. A 409 means the
    conversation has rotated away and the name landed nowhere — reported as a
    refusal rather than the 200 that used to be indistinguishable from success.
    """
    del user
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="expected an object")
    payload: dict[str, str] = {}
    if "name" in body:
        payload["name"] = (body.get("name") or "").strip()[:40]
    if "color" in body:
        payload["color"] = (body.get("color") or "").strip()
    if not payload:
        raise HTTPException(status_code=400, detail="nothing to set")

    def _do_put():
        request_ = urllib.request.Request(
            f"{_gateway_base_url().rstrip('/')}/sessions/{urllib.parse.quote(session_id)}/name",
            data=json.dumps(payload).encode("utf-8"),
            headers={**_rename_headers(), "Content-Type": "application/json"},
            method="PUT",
        )
        with urllib.request.urlopen(request_, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8") or "{}")

    try:
        row = await asyncio.to_thread(_do_put)
    except urllib.error.HTTPError as exc:
        raise HTTPException(
            status_code=exc.code, detail="Could not set the name"
        ) from exc
    except OSError as exc:
        raise HTTPException(status_code=502, detail=f"Gateway unavailable: {exc}") from exc
    return {
        "ok": True,
        "session_id": session_id,
        "name": row.get("name") or "",
        "color": row.get("color") or "",
    }


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


async def _gateway_session_action(
    session_id: str, action: str, body: dict | None = None
) -> dict:
    data = json.dumps(body).encode() if body is not None else None

    def _do_post():
        url = f"{_gateway_base_url().rstrip('/')}/sessions/{session_id}/{action}"
        headers = _gateway_headers()
        if data is not None:
            headers = {"Content-Type": "application/json", **headers}
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))

    try:
        return await asyncio.to_thread(_do_post)
    except urllib.error.HTTPError as exc:
        raise HTTPException(status_code=exc.code, detail=f"Failed to {action} session") from exc
    except OSError as exc:
        raise HTTPException(status_code=502, detail=f"Gateway unavailable: {exc}") from exc


@app.post("/api/terminal/sessions/{session_id}/suspend")
async def api_terminal_session_suspend(
    session_id: str, user: dict = Depends(require_auth)
):
    del user
    return await _gateway_session_action(session_id, "suspend")


@app.post("/api/terminal/sessions/{session_id}/resume")
async def api_terminal_session_resume(
    session_id: str, user: dict = Depends(require_auth)
):
    del user
    return await _gateway_session_action(
        session_id,
        "resume",
        {"client_type": "web", "client_ref": f"terminal-{uuid.uuid4()}"},
    )


@app.delete("/api/terminal/sessions/{session_id}")
async def api_terminal_session_end(
    session_id: str, user: dict = Depends(require_auth)
):
    """End a conversation for good, and take its label with it.

    Distinct from suspend, which puts a conversation down and keeps it,
    and from the panel's close button, which only removes a tile from
    this grid. This is the one that stops the harness -- a harness left
    running has permission checks disabled and goes on editing files
    after the conversation is gone from every list.

    The label is dropped here rather than left behind: it is keyed by
    session id, and a row nobody can reach again is a name nobody can
    delete again.
    """
    del user

    def _do_delete():
        url = f"{_gateway_base_url().rstrip('/')}/sessions/{session_id}"
        req = urllib.request.Request(url, headers=_gateway_headers(), method="DELETE")
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8") or "{}")

    try:
        result = await asyncio.to_thread(_do_delete)
    except urllib.error.HTTPError as exc:
        raise HTTPException(status_code=exc.code, detail="Failed to end session") from exc
    except OSError as exc:
        raise HTTPException(status_code=502, detail=f"Gateway unavailable: {exc}") from exc

    # No name to clean up: it is a column on the session row the gateway just
    # closed, so it goes where the row goes. The delete that stood here existed
    # because the old store was a table with no lifecycle — rows outlived the
    # sessions they named, and one of them outlived three rotations.
    return result


@app.get("/health")
async def health():
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn

    import config

    uvicorn.run(app, host=config.HOST, port=config.PORT)
