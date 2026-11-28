"""HITL (Human-in-the-Loop) gate dependency and helpers for HiveTools."""

from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import Depends, Request

import store
from auth_api import require_api_token

log = logging.getLogger(__name__)

#: How long a call that needs a person's yes is held open, in seconds, when
#: `HITL_WAIT_SECONDS` names nothing usable. 110 seconds: just under a shell
#: tool's default call limit of two minutes, so the caller always sees the
#: refusal and not the shell's cut (Daniel, 27 Nov 2026); the hive site's 310
#: second gated calls outlast it with room to spare.
HITL_WAIT_DEFAULT_SECONDS = 110

#: The setting that overrides it. Zero means do not wait: the request is
#: recorded for the page and the call is refused at once.
HITL_WAIT_ENV = "HITL_WAIT_SECONDS"

# Auto-registration: every hitl_gate("tool_name") call records the name here
# at import time. sync_tool_registry() upserts these into tool_hitl_settings
# at startup so the control panel automatically lists every gated tool with
# a sane default mode. No manual seed step required for new tools.
_REGISTERED_TOOLS: set[str] = set()

AUTO_REGISTER_DEFAULT_MODE = "on"
AUTO_REGISTER_DEFAULT_SUDO_TIMEOUT_MINUTES = 15

#: The only two things a person can do to a pending request. Declared, because
#: the decision used to be `"approved" if action == "approve" else "denied"` -
#: so `{"action": "approved"}`, the natural past-tense mistake, and `"yes"`,
#: and an empty string, all denied the request irrevocably and answered 200
#: with `denied`. A caller told "denied" has no reason to think it meant
#: approve.
ACTIONS = {"approve": "approved", "deny": "denied"}


class UnknownAction(ValueError):
    """Raised for a decision that is neither approve nor deny."""


class UncapturableCall(ValueError):
    """Raised when a gated call cannot be stored as the call it was.

    A NUL byte is the case that exists. `errors="replace"` fixes invalid
    UTF-8 and leaves `\x00` alone, because a NUL *is* valid UTF-8; SQLite
    stored it and Postgres refuses it outright, so the same call was queued
    on one store and 500ed on the other.

    Refused at the route rather than escaped on the way in. An escaped body
    is a different body, and the whole purpose of recording it is that the
    person sees the call they are approving - so a gate that quietly rewrote
    it would show Daniel one call and run another. A caller whose body cannot
    be stored faithfully is told so, now, instead of being held against a
    request that does not show what it will run.
    """


def _capturable(text: str | None, what: str) -> str | None:
    """The captured text, or a refusal naming what cannot be stored."""
    if text is not None and "\x00" in text:
        raise UncapturableCall(
            f"this call's {what} holds a NUL byte, which the hive's database "
            "cannot store in a text column. The gate records the call so a "
            "person sees exactly what they approve, and a body stored with "
            "the NUL removed would show a different call - so the call is "
            "refused here rather than held against a request that does not "
            "show it.")
    return text


class HITLRefused(Exception):
    """A held call that a person denied, or that nobody answered in time.

    The same call is answered with this: a plain refusal that tells the mind
    no, and says why, rather than a promise of an answer later.
    """

    def __init__(self, request_id: str, status: str, decided_by: str | None,
                 waited: int) -> None:
        self.request_id = request_id
        self.status = status
        self.decided_by = decided_by
        if status == "denied":
            who = decided_by or "a person"
            self.message = (
                f"Refused: {who} denied this request. The call did not run. "
                "Do not repeat it unless asked to.")
        else:
            self.message = (
                f"Refused: nobody approved this request within {waited} "
                "seconds, so it timed out. The call did not run. Tell the "
                "person you work for it is waiting for their approval on the "
                "hive site, and try again only if they ask.")
        super().__init__(self.message)


async def _check_hitl(
    tool_name: str,
    caller_mind: str,
    params_json: str = "{}",
    tool_method: str | None = None,
    tool_path: str | None = None,
    tool_body: str | None = None,
    gone: Callable[[], Awaitable[bool]] | None = None,
) -> str:
    """Core HITL logic. Returns caller_mind once the call is allowed (at once, or
    when a person approves the held call); raises HITLRefused if it is denied
    or times out."""
    # Checked here rather than at each gated route, so a route added later
    # cannot ship without it. Before the mode is read, because a call that
    # cannot be captured is refused whether or not it would have been gated
    # - a tool whose gate is off today and on tomorrow must not change
    # whether the same body is accepted.
    params_json = _capturable(params_json, "parameters") or "{}"
    tool_body = _capturable(tool_body, "body")

    row = await store.setting(tool_name)

    if row is None:
        return caller_mind

    mode = row["mode"]

    if mode == "off":
        return caller_mind

    if mode == "sudo":
        now = int(time.time())
        grant = await store.active_grant(tool_name, caller_mind, now)
        if grant is not None:
            return caller_mind

    # mode == "on" or "sudo" without valid grant: record the request and hold
    # the caller's own call open until a person decides it or the wait runs
    # out. Nothing is replayed later; the call that comes out of here is the
    # one the mind made, and it carries on to the tool.
    request_id = str(uuid.uuid4())
    now = _now()
    expires_at = now + wait_seconds()

    await store.create_request(
        request_id, tool_name, caller_mind, params_json, now, expires_at,
        tool_method, tool_path, tool_body,
    )

    try:
        notify_daniel(tool_name, caller_mind, params_json, request_id)
    except Exception:
        log.warning("HITL notification failed for %s", request_id, exc_info=True)

    status, decided_by = await _held(request_id, expires_at, gone)
    if status == "approved":
        return caller_mind
    raise HITLRefused(request_id, status, decided_by, wait_seconds())


def wait_seconds() -> int:
    """How long a call needing a person's yes is held open: the setting
    `HITL_WAIT_SECONDS`, or 110 seconds when it is unset or not a whole
    number of seconds. Zero means not at all: the request is recorded for the
    page and the call is refused at once. Read on each call, so a changed
    setting needs no code change."""
    raw = os.environ.get(HITL_WAIT_ENV, "").strip()
    try:
        seconds = int(raw)
    except ValueError:
        return HITL_WAIT_DEFAULT_SECONDS
    return seconds if seconds >= 0 else HITL_WAIT_DEFAULT_SECONDS


#: The clock the hold reads. A seam, so the hold's deadline is proven with
#: an injected time rather than by sleeping through it.
def _now() -> int:
    return int(time.time())


#: Requests being held, each with the signal its decision sets. The signal is
#: only a prompt to look at the record sooner: the record is what is believed.
_WAKE: dict[str, asyncio.Event] = {}

#: How often a held call looks at its record when nothing has signalled, so a
#: decision made through another process is still seen.
HOLD_POLL_SECONDS = 2.0


async def _pause(event: asyncio.Event, seconds: float) -> None:
    """Wait up to `seconds` for the signal. The other half of the clock seam."""
    try:
        await asyncio.wait_for(event.wait(), seconds)
    except asyncio.TimeoutError:
        pass


async def _held(request_id: str, expires_at: int,
                gone: Callable[[], Awaitable[bool]] | None = None,
                ) -> tuple[str, str | None]:
    """Hold until the request is decided or its time runs out.

    Returns `(status, decided_by)`: `approved`, `denied`, or `expired`. The
    record is read each time round and its expiry is the rule that ends the
    hold (`effective_status`), so a request past its time is refused here as
    it is everywhere else, whatever else happens.

    A caller that goes away (`gone` says the connection dropped, or the hold
    is cancelled as the process stops) ends the hold; its request is then let
    run out, so that a yes given afterwards cannot run a tool nobody is
    waiting on.
    """
    event = _WAKE.setdefault(request_id, asyncio.Event())
    try:
        while True:
            row = await store.request(request_id)
            if row is None:
                return "expired", None
            now = _now()
            status = effective_status(row, now)
            if status != "pending":
                return status, row.get("decided_by")
            if gone is not None and await gone():
                await _abandon(request_id)
                return "expired", None
            if now >= row["expires_at"]:
                # Time is up. Closed in the one statement the decision is
                # guarded by, so a yes landing at this very second either
                # wins (and is read next time round) or finds it closed.
                await store.run_out(request_id, now)
                continue
            event.clear()
            await _pause(event, min(HOLD_POLL_SECONDS,
                                    row["expires_at"] - now))
    except asyncio.CancelledError:
        await asyncio.shield(_abandon(request_id))
        raise
    finally:
        _WAKE.pop(request_id, None)


async def _abandon(request_id: str) -> None:
    """Let a request whose caller has gone run out now."""
    try:
        await store.run_out(request_id, _now())
    except Exception:
        log.warning("HITL request %s could not be closed after its caller "
                    "went away", request_id, exc_info=True)


async def close_waiting_requests() -> int:
    """At start: every request still waiting belongs to a call this process
    never held (the one that held it died with the last process), so a yes
    given now would run nothing. Let each run out."""
    closed = 0
    now = _now()
    for row in await store.waiting_requests(now):
        closed += await store.run_out(str(row["id"]), now)
    return closed


def hitl_gate(tool_name: str, who=None):
    """Factory that returns a FastAPI dependency for HITL checks.

    Side effect: records `tool_name` in `_REGISTERED_TOOLS` at import time so
    `sync_tool_registry` can upsert it into `tool_hitl_settings` at startup.

    `who` is the dependency that names the caller: a mind's key by default;
    the routes the hive site calls for a person pass one that also takes a
    person's own key.
    """
    _REGISTERED_TOOLS.add(tool_name)

    async def _check(
        request: Request,
        caller_mind: str = Depends(who or require_api_token),
    ) -> str:
        body_bytes = await request.body()
        return await _check_hitl(
            tool_name,
            caller_mind,
            tool_method=request.method,
            tool_path=str(request.url.path),
            tool_body=body_bytes.decode("utf-8", errors="replace"),
            gone=request.is_disconnected,
        )

    return _check


# Default HITL settings: tool_name -> mode
_DEFAULT_HITL_SETTINGS: list[tuple[str, str, int]] = [
    # Calendar read tools -- off
    ("calendar_list_events", "off", 15),
    ("calendar_get_event", "off", 15),
    ("calendar_list", "off", 15),
    ("calendar_check_availability", "off", 15),
    # Calendar write tools -- on
    ("calendar_create_event", "on", 15),
    ("calendar_quick_add", "on", 15),
    ("calendar_update_event", "on", 15),
    ("calendar_delete_event", "on", 15),
    ("calendar_invite", "on", 15),
    # Gmail read tools -- off
    ("gmail_read_emails", "off", 15),
    ("gmail_get_email", "off", 15),
    ("gmail_list_labels", "off", 15),
    # Gmail write tools -- on
    ("gmail_send_email", "on", 15),
    ("gmail_reply_email", "on", 15),
    ("gmail_delete_email", "on", 15),
    ("gmail_move_email", "on", 15),
    ("gmail_create_label", "on", 15),
    ("gmail_unsubscribe", "on", 15),
    # LinkedIn -- on
    ("linkedin_post", "on", 15),
    # Docker mutating -- on
    ("docker_compose_up", "on", 15),
    ("docker_compose_restart", "on", 15),
    ("docker_compose_down", "on", 15),
    # Docker read-only -- off
    ("docker_compose_logs", "off", 15),
    ("docker_compose_status", "off", 15),
    ("docker_list_containers", "off", 15),
    ("docker_list_networks", "off", 15),
    # Browser -- on for actions that introduce external content or change page state
    ("browser_navigate", "on", 15),
    ("browser_click", "on", 15),
    ("browser_type", "on", 15),
    ("browser_web_search", "on", 15),
    # Ollama generic LLM endpoints — read-only side effects, no approval needed
    ("ollama_structured", "off", 15),
    ("ollama_chat", "off", 15),
    # Schema mutation — always HITL-gated. Adding new node/edge types is a
    # durable schema decision and should never auto-execute.
    ("schema_new_type", "on", 15),
    # Training pipeline — off, because the only caller is the console and a
    # console click is already a human act taken behind a login. Gating them
    # would page the same person who just pressed the button, and the panel
    # would sit on a pending response with nothing to show.
    ("training_curate", "off", 15),
    ("training_export", "off", 15),
    ("training_train", "off", 15),
    ("training_deploy", "off", 15),
]


async def seed_default_hitl_settings() -> None:
    """Give each shipped tool its mode only if it has none.

    Insert-if-absent, and that is the point: this runs on every boot, so a
    replacing write would re-arm every approval gate the operator had turned
    off, on every deploy, silently.
    """
    for tool_name, mode, timeout in _DEFAULT_HITL_SETTINGS:
        await store.seed_setting(tool_name, mode, timeout)


async def sync_tool_registry(
    default_mode: str = AUTO_REGISTER_DEFAULT_MODE,
    default_sudo_timeout_minutes: int = AUTO_REGISTER_DEFAULT_SUDO_TIMEOUT_MINUTES,
) -> int:
    """Upsert any tool name discovered at hitl_gate import time.

    Existing rows in tool_hitl_settings are left alone (insert-if-absent)
    so the shipped modes in seed_default_hitl_settings and user-edited modes
    both survive. Returns the number of new rows inserted.
    """
    inserted = 0
    for name in sorted(_REGISTERED_TOOLS):
        inserted += await store.seed_setting(
            name, default_mode, default_sudo_timeout_minutes)
    if inserted:
        log.info("sync_tool_registry: registered %d new gated tool(s)", inserted)
    return inserted


def effective_status(row: dict[str, Any], now: int) -> str:
    """What a request's status actually is at `now`.

    Nothing ever writes `expired` to a row: the expiry is a rule applied
    when the row is read. One function for it rather than the rule inline at
    each reader, because the approvals page had its own copy - which is to
    say it had none, and drew working Approve and Deny buttons on eleven
    requests that had been past their expiry for up to ninety-six days.
    """
    if row["status"] == "pending" and now > row["expires_at"]:
        return "expired"
    return str(row["status"])


async def respond_to_hitl(
    request_id: str, action: str, decided_by: str | None = None
) -> dict[str, Any] | None:
    """Approve or deny a HITL request.

    The decision is one statement guarded on the request still being pending
    and not yet expired, so two people reaching the queue at the same moment
    produce one decision. What it changed is what it hands back: a request
    that was already settled, or that expired while the page was open,
    changes nothing and is reported as already resolved rather than being
    overwritten.

    The decision and the sudo grant it may earn are **one** unit of work.
    Separately committed they leave an end state nothing can recover from: a
    grant that fails after the decision lands leaves the request saying
    approved with no grant, and un-decidable for ever because the decision is
    guarded on `pending`.

    Nothing runs here. The call that asked is being held open on this
    request (`_held`); the decision wakes it, and it carries on to the tool
    as the mind's own call. Nothing is saved to be replayed later.
    """
    try:
        new_status = ACTIONS[action]
    except KeyError:
        raise UnknownAction(
            f"{action!r} is not a decision. A request is approved or denied, "
            f"and nothing else: {', '.join(sorted(ACTIONS))}. Anything "
            "unrecognised used to be taken as a denial, so the past-tense "
            "spelling of approve denied the request and answered 200.") \
            from None
    now = _now()

    if new_status == "approved":
        # Asked again at the decision, not only when the call was made: a
        # request raised before a mind's owner changed must not run one
        # person's integrations for another's mind. Such a request is
        # denied, never run.
        from auth_api import owner_refusal

        pending = await store.request(request_id)
        if pending is not None and await owner_refusal(
                str(pending.get("caller_mind") or "")):
            new_status = "denied"

    async with store.transaction():
        row = await store.decide_request(request_id, new_status, now, now,
                                         decided_by)

        if row is None:
            # Nothing was decided. Say which of the three reasons it was,
            # because "no such request" and "somebody got there first" are
            # different sentences, and an expired one is neither.
            existing = await store.request(request_id)
            if existing is None:
                return None
            if existing["status"] != "pending":
                return {"error": "already_resolved",
                        "current_status": existing["status"]}
            return {"error": "already_resolved", "current_status": "expired"}

        if new_status == "approved":
            setting = await store.setting(row["tool_name"])
            if setting and setting["mode"] == "sudo":
                timeout_minutes = int(setting["sudo_timeout_minutes"])
                await store.grant_sudo(
                    row["tool_name"], row["caller_mind"], now,
                    now + timeout_minutes * 60)

    result = {
        "hitl_request_id": request_id,
        "status": new_status,
        "tool_name": row["tool_name"],
        "caller_mind": row["caller_mind"],
        "decided_at": now,
        "decided_by": decided_by,
    }
    # The call that asked is held open on this request: wake it to read the
    # decision. Only after the decision has committed.
    wake = _WAKE.get(request_id)
    if wake is not None:
        wake.set()
    return result


def approval_url(request_id: str) -> str:
    """Where a person decides `request_id`: a page on the hive site.

    The link carries the request's identifier and nothing else; holding it
    decides nothing and shows nothing, the page asks who is signed in. With
    no public address of the site configured it is the path, and the notice
    says it is on the hive site.
    """
    base = (os.environ.get("HIVE_SITE_PUBLIC_URL", "").strip().rstrip("/"))
    return f"{base}/approvals/{request_id}"


async def review_of(request_id: str, now: int) -> dict[str, Any] | None:
    """Everything an approval page shows of one request, or None.

    The content is the call being held for the person's yes: the method, path
    and body of the call, as stored.
    """
    row = await store.request_payload(request_id)
    if row is None:
        return None
    full = await store.request(request_id)
    assert full is not None
    status = effective_status(full, now)
    return {
        "hitl_request_id": request_id,
        "caller_mind": full["caller_mind"],
        "tool_name": full["tool_name"],
        "status": status,
        "created_at": full["created_at"],
        "expires_at": full["expires_at"],
        "decided_at": full["decided_at"],
        "decided_by": full.get("decided_by"),
        "params": row["params_json"],
        "method": row["tool_method"],
        "path": row["tool_path"],
        "body": row["tool_body"],
    }


def notify_daniel(
    tool_name: str,
    caller_mind: str,
    params_json: str,
    request_id: str,
) -> None:
    """Send a Telegram notification with inline keyboard buttons."""
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_OWNER_CHAT_ID")

    if not bot_token or not chat_id:
        log.warning(
            "HITL notification skipped: TELEGRAM_BOT_TOKEN or TELEGRAM_OWNER_CHAT_ID not set"
        )
        return

    try:
        import httpx

        _TOOL_ICONS = {
            "gmail": "📧",
            "calendar": "📅",
            "docker": "🐳",
            "linkedin": "💼",
        }
        prefix = tool_name.split("_")[0]
        icon = _TOOL_ICONS.get(prefix, "🔧")
        label = tool_name.replace(f"{prefix}_", "").replace("_", " ").title()

        # No buttons: a decision is made on the hive site, signed in as the
        # person, so the notice says where.
        text = (f"{icon} {label}\nRequested by: {caller_mind}\n"
                f"Approve or deny it on the hive site, signed in: "
                f"{approval_url(request_id)}")

        payload = {"chat_id": chat_id, "text": text}

        httpx.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            json=payload,
            timeout=10,
        )
    except Exception:
        log.warning("Failed to send HITL Telegram notification", exc_info=True)
