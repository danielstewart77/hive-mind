"""HITL (Human-in-the-Loop) gate dependency and helpers for HiveTools."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from typing import Any

import httpx
from fastapi import Depends, Request

import store
from auth_api import require_api_token

log = logging.getLogger(__name__)

HITL_EXPIRY_SECONDS = 300  # 5 minutes

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
    is a different body, and the whole purpose of capturing it is that the
    replay re-makes the call the person approved - so a gate that quietly
    rewrote it would show Daniel one call and run another. A caller whose
    body cannot be stored faithfully is told so, now, instead of being
    queued against a replay that cannot be honoured.
    """


def _capturable(text: str | None, what: str) -> str | None:
    """The captured text, or a refusal naming what cannot be stored."""
    if text is not None and "\x00" in text:
        raise UncapturableCall(
            f"this call's {what} holds a NUL byte, which the hive's database "
            "cannot store in a text column. The gate captures the call so an "
            "approval can replay it exactly, and a body stored with the NUL "
            "removed would replay a different call - so the call is refused "
            "here rather than queued against a replay that cannot be "
            "honoured.")
    return text


class HITLPendingException(Exception):
    """Raised when a tool call requires human approval.

    Use a custom exception instead of HTTPException so the response body
    is returned flat ({"hitl_request_id": ..., "status": "pending"}) rather
    than wrapped under the FastAPI {"detail": ...} envelope.
    """

    def __init__(self, request_id: str) -> None:
        self.request_id = request_id
        super().__init__(f"HITL approval required: {request_id}")


async def _check_hitl(
    tool_name: str,
    caller_mind: str,
    params_json: str = "{}",
    tool_method: str | None = None,
    tool_path: str | None = None,
    tool_body: str | None = None,
) -> str:
    """Core HITL logic. Returns caller_mind if allowed, raises HITLPendingException otherwise."""
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

    # mode == "on" or "sudo" without valid grant — create HITL request
    request_id = str(uuid.uuid4())
    now = int(time.time())
    expires_at = now + HITL_EXPIRY_SECONDS

    await store.create_request(
        request_id, tool_name, caller_mind, params_json, now, expires_at,
        tool_method, tool_path, tool_body,
    )

    try:
        notify_daniel(tool_name, caller_mind, params_json, request_id)
    except Exception:
        log.warning("HITL notification failed for %s", request_id, exc_info=True)

    raise HITLPendingException(request_id)


#: The replay's transport. None is httpx's real one, to this process's own
#: port; tests swap in a MockTransport for the far side of the call.
_REPLAY_TRANSPORT: httpx.AsyncBaseTransport | None = None

#: The transport to hive-comms, for telling the asking mind. Same rule.
_COMMS_TRANSPORT: httpx.AsyncBaseTransport | None = None

#: How long a replay may take to answer. Long, because the gated tools
#: include a browser search that waits on a page for over a minute and a
#: compose `up` that may pull or build. A replay cut off early is reported
#: as failed while the tool may still finish, which invites a retry that
#: does the thing twice.
REPLAY_TIMEOUT_SECONDS = 900
COMMS_TIMEOUT_SECONDS = 10

#: The longest error carried in the message to the mind.
MESSAGE_ERROR_CHARS = 1000

#: The longest result carried in the message to the mind. The full body is
#: on the request row and `GET /hitl/{id}` returns it; the message is a
#: wake-up prompt, and a browser page in one is a turn spent reading it.
MESSAGE_RESULT_CHARS = 4000

#: What the broker files the message under, and what a mind's skill keys on.
HITL_RESULT_REQUEST_TYPE = "hitl_result"

#: Replays still running, and whose they are. Held because the event loop
#: keeps only a weak reference to a task, so a fire-and-forget replay can be
#: collected before it finishes; and so shutdown can land an outcome for
#: each one before the store closes.
_IN_FLIGHT: dict[asyncio.Task[None], tuple[str, str, str, str | None]] = {}

#: What a replay still running at shutdown is recorded as.
CUT_OFF_ERROR = (
    "hive-tools shut down before the replay answered, so whether the tool "
    "ran is unknown; check its effect before making the call again")


def schedule_replay(request_id: str, tool_name: str, caller_mind: str,
                    replay: dict[str, str] | None,
                    person_key: str | None = None) -> asyncio.Task[None]:
    """Run an approved request's call in the background and report it.

    `person_key` is the deciding person's own key, held in memory for this
    task alone to tell the asking mind with; it is never stored."""
    task = asyncio.create_task(
        replay_and_report(request_id, tool_name, caller_mind, replay, person_key))
    _IN_FLIGHT[task] = (request_id, tool_name, caller_mind, person_key)
    task.add_done_callback(lambda done: _IN_FLIGHT.pop(done, None))
    return task


async def settle_replays(timeout: float | None = None) -> None:
    """Wait for every replay in flight to land its outcome.

    Given a timeout, a replay still running when it passes is cancelled and
    recorded as cut off, and the mind told so, rather than left approved
    with no outcome for ever - which `GET /hitl/{id}` cannot tell from a
    replay still running.
    """
    pending = dict(_IN_FLIGHT)
    if not pending:
        return
    _, unfinished = await asyncio.wait(set(pending), timeout=timeout)
    for task in unfinished:
        task.cancel()
    if unfinished:
        await asyncio.wait(unfinished)
    for task in unfinished:
        request_id, tool_name, caller_mind, person_key = pending[task]
        await _land(request_id, tool_name, caller_mind, None,
                    json.dumps({"error": CUT_OFF_ERROR}), person_key)


def _parsed(text: str) -> Any:
    """A response body as JSON when it is JSON, otherwise the text."""
    try:
        return json.loads(text)
    except ValueError:
        return text


def _error_in(status: int | None, result: Any) -> str | None:
    """The tool's own error, or None when the call succeeded.

    Every gated tool answers through the same HTTP surface, so success is
    judged there and not per tool: a status outside 2xx is a refusal, and a
    2xx is still a failure when the body says so in any of the shapes this
    service's routes report a caught failure in - `{"error": ...}` (gmail,
    calendar, sms, linkedin), `{"ok": false, ...}` (schema), `{"status":
    "error", ...}` (docker compose exiting non-zero) and `{"_error": ...}`
    (ollama). Counting any of them as success is how an approved send that
    never left would be reported as sent.
    """
    if isinstance(result, dict):
        for key in ("error", "_error"):
            if result.get(key):
                return str(result[key])
        if result.get("ok") is False:
            return str(result.get("detail") or result.get("code")
                       or "the tool answered ok: false")
        if result.get("status") == "error":
            return str(result.get("stderr") or result.get("detail")
                       or f"the tool exited {result.get('return_code')}")
    if status is None or not 200 <= status < 300:
        if isinstance(result, dict) and result.get("detail"):
            return str(result["detail"])
        text = result if isinstance(result, str) else json.dumps(result)
        return (f"the replay answered HTTP {status}: {text[:500]}"
                if status is not None else text[:500])
    return None


async def _run_replay(method: str, path: str,
                      body: str) -> tuple[int | None, str]:
    """Re-make the call, as `(HTTP status, body)`; status None if it never ran."""
    from config import INTERNAL_SECRET

    url = f"http://127.0.0.1:9421{path}"
    headers = {
        "Authorization": f"Internal {INTERNAL_SECRET}",
        "Content-Type": "application/json",
    }
    try:
        async with httpx.AsyncClient(transport=_REPLAY_TRANSPORT) as client:
            response = await client.request(
                method, url, content=body, headers=headers,
                timeout=REPLAY_TIMEOUT_SECONDS)
    except httpx.TimeoutException as exc:
        log.warning("HITL replay timed out: %s %s", method, path)
        return None, json.dumps(
            {"error": f"the replay did not answer within "
                      f"{REPLAY_TIMEOUT_SECONDS}s ({type(exc).__name__}), so "
                      "whether the tool ran is unknown; check its effect "
                      "before making the call again"})
    except Exception as exc:  # noqa: BLE001 - every failure is an outcome
        log.warning("HITL replay failed: %s %s", method, path, exc_info=True)
        return None, json.dumps(
            {"error": f"the replay never got an answer: "
                      f"{type(exc).__name__}: {exc}"})
    return response.status_code, response.text


async def replay_and_report(request_id: str, tool_name: str, caller_mind: str,
                            replay: dict[str, str] | None,
                            person_key: str | None = None) -> None:
    """Replay an approved call, store what it did, then tell the mind.

    Stored before it is sent, and a failure to send never touches what was
    stored: the row is the record, and the message is only the asking mind
    being told the record exists. A request approved with no captured call
    has nothing to replay, which is a failure the mind is told about rather
    than an approval that silently did nothing.
    """
    if replay is None:
        status: int | None = None
        text = json.dumps({"error": "the request was approved, but it holds "
                                    "no captured call to replay, so the tool "
                                    "did not run"})
    else:
        status, text = await _run_replay(
            replay["method"], replay["path"], replay["body"])
    await _land(request_id, tool_name, caller_mind, status, text, person_key)


async def _land(request_id: str, tool_name: str, caller_mind: str,
                status: int | None, text: str,
                person_key: str | None = None) -> None:
    """Store an outcome, then tell the mind; the store first, always."""
    result = _parsed(text)
    error = _error_in(status, result)
    ok = error is None
    if ok:
        log.info("HITL replay succeeded: %s %s -> %s", tool_name, request_id,
                 status)
    else:
        log.warning("HITL replay failed: %s %s -> %s: %s", tool_name,
                    request_id, status, error)
    try:
        written = await store.record_outcome(
            request_id, ok, status, text, int(time.time()))
    except Exception:
        log.exception("HITL outcome for %s could not be stored", request_id)
        return
    if not written:
        # Already has one. A second replay of one approval should not exist;
        # if it does, the first answer stands and nobody is told twice.
        log.warning("HITL outcome for %s was already recorded", request_id)
        return
    try:
        await notify_caller(request_id, tool_name, caller_mind, ok, status,
                            result, error, person_key)
    except Exception:
        log.warning("HITL result for %s could not be sent to %s; the outcome "
                    "is stored and GET /hitl/%s returns it", request_id,
                    caller_mind, request_id, exc_info=True)


def _comms(person_key: str | None) -> tuple[str, dict[str, str]] | None:
    """The gateway and the bearer for telling the asking mind: the key of the
    person who decided the request, as that person. The tools hold no key of
    their own for the gateway."""
    url = os.environ.get("COMMS_URL", "").strip().rstrip("/")
    token = (person_key or "").strip()
    if not url or not token:
        return None
    return url, {"Authorization": f"Bearer {token}"}


async def notify_caller(request_id: str, tool_name: str, caller_mind: str,
                        ok: bool, status: int | None, result: Any,
                        error: str | None, person_key: str | None = None) -> bool:
    """Send the asking mind one broker message carrying the outcome.

    Addressed the way a scheduler wakes a mind: from the mind to itself,
    which the broker delivers into that mind's standing wake session, on a
    conversation id of its own. hive-tools knows the caller by its API token
    name and the broker routes by mind id, so the name is resolved against
    the broker's registry first. It is sent with the key of the person who
    decided the request, as that person (an owner of the mind or an
    administrator, who may write to it): the tools hold no gateway key of
    their own. Without that key (a replay cut off at shutdown, which kept
    none) nothing is sent and the stored outcome is the record. Returns
    whether it was sent.
    """
    comms = _comms(person_key)
    if comms is None:
        log.warning("HITL result for %s not sent: COMMS_URL is unset or no "
                    "deciding person's key is at hand", request_id)
        return False
    base, headers = comms
    async with httpx.AsyncClient(transport=_COMMS_TRANSPORT,
                                 timeout=COMMS_TIMEOUT_SECONDS) as client:
        listed = await client.get(f"{base}/broker/minds", headers=headers)
        listed.raise_for_status()
        wanted = caller_mind.strip().lower()
        mind_id = next(
            (str(m["id"]) for m in listed.json()
             if str(m.get("id", "")).lower() == wanted
             or str(m.get("name", "")).lower() == wanted), None)
        if mind_id is None:
            log.warning("HITL result for %s not sent: no registered mind is "
                        "called %r", request_id, caller_mind)
            return False
        shown = result if isinstance(result, str) else json.dumps(result)
        if len(shown) > MESSAGE_RESULT_CHARS:
            shown = (shown[:MESSAGE_RESULT_CHARS]
                     + f"... [truncated; GET /hitl/{request_id} has it whole]")
        if error is not None and len(error) > MESSAGE_ERROR_CHARS:
            error = error[:MESSAGE_ERROR_CHARS] + "... [truncated]"
        verdict = "succeeded" if ok else f"FAILED: {error}"
        content = (
            f"HITL result for your {tool_name} call (request {request_id}): "
            f"Daniel approved it, hive-tools ran it, and it {verdict} "
            f"(HTTP {status if status is not None else 'none'}).\n"
            f"Result: {shown}\n"
            "Tell Daniel what actually happened.")
        sent = await client.post(f"{base}/broker/messages", headers=headers,
                                 json={
            "message_id": str(uuid.uuid4()),
            "conversation_id": str(uuid.uuid4()),
            "from": mind_id,
            "to": mind_id,
            "content": content,
            "rolling_summary": "",
            "metadata": {
                "request_type": HITL_RESULT_REQUEST_TYPE,
                "triggered_by": "hive-tools",
                "expects_reply": False,
                "hitl_request_id": request_id,
                "tool_name": tool_name,
                "success": ok,
                "http_status": status,
                "error": error,
                "result": shown,
            },
        })
        sent.raise_for_status()
    log.info("HITL result for %s sent to %s (%s)", request_id, caller_mind,
             mind_id)
    return True


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
        # Internal replay calls bypass the gate entirely
        if caller_mind == "internal":
            return caller_mind

        body_bytes = await request.body()
        return await _check_hitl(
            tool_name,
            caller_mind,
            tool_method=request.method,
            tool_path=str(request.url.path),
            tool_body=body_bytes.decode("utf-8", errors="replace"),
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


async def get_hitl_status(request_id: str,
                          caller: str | None = None) -> dict[str, Any] | None:
    """Get the current status of a HITL request, accounting for expiry.

    The outcome is the tool's whole answer - a page's text, a sent message's
    id - so it goes to the mind that asked and nobody else: given a `caller`
    that is not the request's, the status is reported without it.
    """
    row = await store.request(request_id)

    if row is None:
        return None

    now = int(time.time())
    status = effective_status(row, now)

    answer: dict[str, Any] = {
        "hitl_request_id": row["id"],
        "status": status,
        "tool_name": row["tool_name"],
        "caller_mind": row["caller_mind"],
        "decided_at": row["decided_at"],
    }
    if status == "approved" and (caller is None
                                 or caller == row["caller_mind"]):
        # None while the replay is still running, and for a request approved
        # before outcomes were recorded: approved is not the same as done.
        answer["outcome"] = None
        if row.get("outcome_at") is not None:
            stored = await store.request_outcome(request_id)
            if stored is not None:
                answer["outcome"] = outcome_view(stored)
    return answer


def outcome_view(stored: dict[str, Any]) -> dict[str, Any]:
    """A stored outcome as `GET /hitl/{id}` reports it."""
    status = stored["outcome_status"]
    result = _parsed(stored["outcome_body"] or "")
    return {
        "success": bool(stored["outcome_ok"]),
        "http_status": status,
        "result": result,
        "error": None if stored["outcome_ok"] else _error_in(status, result),
        "at": stored["outcome_at"],
    }


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
    approved, the call it authorised never replayed (the route fires the
    replay after this returns, so an exception loses it), and the request
    un-decidable for ever because the decision is guarded on `pending`. The
    page would show one approval and the operator would have no way back.
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
    now = int(time.time())

    if new_status == "approved":
        # Asked again at the decision, not only when the call was made: a
        # request raised before a mind's owner changed, or before this check
        # existed, must not replay one person's integrations for another's
        # mind. Such a request is denied, never run.
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

        # Replay data returned to the caller — fired as a background task by
        # the route handler, after this unit has committed.
        replay: dict[str, str] | None = None
        if new_status == "approved":
            r_method = row.get("tool_method")
            r_path = row.get("tool_path")
            r_body = row.get("tool_body")
            if r_method and r_path and r_body is not None:
                replay = {"method": r_method, "path": r_path, "body": r_body}

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
    if replay:
        result["_replay"] = replay
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

    The content is what the tools hold and will replay: the method, path and
    body of the saved call, as stored.
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
