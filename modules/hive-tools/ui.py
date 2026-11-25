"""Management UI routes for HiveTools."""

from __future__ import annotations

import time
import urllib.parse
from typing import Any

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

import store
from auth_api import generate_token, list_tokens, revoke_token
from auth_session import get_current_user_from_request
from config import BASE_DIR
from hitl import (
    UnknownAction,
    effective_status,
    respond_to_hitl,
    schedule_replay,
)

router = APIRouter()

templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


# ---- Tools ----


_METHOD_ORDER = {"GET": 0, "POST": 1, "PATCH": 2, "DELETE": 3}


def _infer_method(tool_name: str) -> str:
    """Derive HTTP method from semantic tool name."""
    # Strip group prefix (e.g. "calendar_", "gmail_")
    parts = tool_name.split("_", 1)
    stem = parts[1] if len(parts) > 1 else tool_name

    if any(stem.startswith(p) for p in ("list", "get", "check", "read", "logs", "status")):
        return "GET"
    if any(stem.startswith(p) for p in ("delete", "down", "remove")):
        return "DELETE"
    if any(stem.startswith(p) for p in ("update", "move")):
        return "PATCH"
    return "POST"


@router.get("/tools", response_class=HTMLResponse)
async def tools_page(request: Request) -> Any:
    user = await get_current_user_from_request(request)
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    rows = await store.settings()
    tools = [dict(r) | {"method": _infer_method(r["tool_name"])} for r in rows]
    tools.sort(key=lambda t: (t["tool_name"].split("_")[0], _METHOD_ORDER.get(t["method"], 9), t["tool_name"]))

    return templates.TemplateResponse(request, "tools.html", {"user": user, "tools": tools})


@router.post("/tools/settings")
async def tools_settings_save(
    request: Request,
    tool_name: str = Form(...),
    mode: str = Form(...),
    sudo_timeout_minutes: int = Form(15),
) -> Any:
    user = await get_current_user_from_request(request)
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    await store.set_setting(tool_name, mode, sudo_timeout_minutes)
    return RedirectResponse(url="/tools", status_code=303)


# ---- Tokens ----


@router.get("/tokens", response_class=HTMLResponse)
async def tokens_page(request: Request) -> Any:
    user = await get_current_user_from_request(request)
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    tokens = await list_tokens()
    return templates.TemplateResponse(
        request, "tokens.html",
        {"user": user, "tokens": tokens,
         "notice": request.query_params.get("notice") or ""})


@router.post("/tokens/create", response_class=HTMLResponse)
async def tokens_create(
    request: Request,
    name: str = Form(...),
) -> Any:
    user = await get_current_user_from_request(request)
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    raw_token = await generate_token(name)
    tokens = await list_tokens()
    return templates.TemplateResponse(
        request,
        "tokens.html",
        {"user": user, "tokens": tokens, "new_token": raw_token, "new_token_name": name},
    )


@router.post("/tokens/{token_id}/revoke")
async def tokens_revoke(
    request: Request,
    token_id: int,
) -> Any:
    user = await get_current_user_from_request(request)
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    # The count, which the revocation was changed to report and this route
    # used to throw away - so the operator could not tell a revocation they
    # performed from one they merely repeated.
    revoked = await revoke_token(token_id)
    notice = ("Token revoked." if revoked
              else "That token was already revoked; nothing changed.")
    return RedirectResponse(
        url=f"/tokens?notice={urllib.parse.quote(notice)}", status_code=303)


# ---- HITL UI ----


@router.get("/hitl-ui", response_class=HTMLResponse)
async def hitl_page(request: Request) -> Any:
    user = await get_current_user_from_request(request)
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    # The status the page draws is the status at this instant, not the one in
    # the column: nothing ever writes `expired`, the expiry is a rule applied
    # when a row is read, and this page had no copy of that rule. Eleven
    # requests on the live queue were being drawn as pending with working
    # Approve and Deny buttons, one of them ninety-six days past its expiry.
    now = int(time.time())
    requests_list = [dict(r) | {"status": effective_status(dict(r), now)}
                     for r in await store.recent_requests()]
    return templates.TemplateResponse(
        request, "hitl.html",
        {"user": user, "requests": requests_list,
         "notice": request.query_params.get("notice") or ""},
    )


@router.post("/hitl-ui/{request_id}/respond")
async def hitl_ui_respond(
    request: Request,
    request_id: str,
    action: str = Form(...),
) -> Any:
    user = await get_current_user_from_request(request)
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    # What the decision answered, carried back to the page. The one caller a
    # human uses was the one that discarded it: clicking Approve on a request
    # that had expired, or that somebody else had just decided, reloaded the
    # page, changed nothing, and said nothing.
    try:
        result = await respond_to_hitl(request_id, action) or {}
    except UnknownAction as exc:
        return _back_to_queue(str(exc))

    if not result:
        return _back_to_queue(f"There is no request {request_id}.")
    if result.get("error") == "already_resolved":
        settled = result.get("current_status")
        if settled == "expired":
            return _back_to_queue(
                "That request expired before this decision reached it, so "
                "nothing changed. The call has to be made again rather than "
                "approved.")
        return _back_to_queue(
            f"That request was already {settled}, so nothing changed.")

    replay = result.pop("_replay", None)
    if result["status"] == "approved":
        schedule_replay(request_id, result["tool_name"],
                        result["caller_mind"], replay)
    return _back_to_queue(f"Request {result['status']}.")


def _back_to_queue(notice: str) -> RedirectResponse:
    """The queue again, carrying one sentence about what just happened."""
    return RedirectResponse(
        url=f"/hitl-ui?notice={urllib.parse.quote(notice)}", status_code=303)
