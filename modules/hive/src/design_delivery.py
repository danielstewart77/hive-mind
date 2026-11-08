"""Sending a design-session submission into the conversation as a message.

The design page is a surface, like Telegram or the browser terminal. What a
button press does is what typing does: the conversation is adopted by this
surface, and the submission arrives as an ordinary user turn. Nothing types
into a pane and nothing injects keystrokes — a keystroke is indistinguishable
from Daniel typing, which means it fires the pane's rotation hook and
respawns the pane out from under the delivery it was carrying.

Adoption is not an optimisation, it is the invariant. One live harness
process per conversation: a message sent to a conversation whose harness is
running inside tmux would spawn a second process on the same transcript,
both appending turns the other never sees. `activate_session` releases the
outgoing surface first, which is exactly what Telegram's `/switch` does.

Delivery is claimed, then sent, then acknowledged. Build is the signal that
starts a harness writing code, so delivering it twice puts two processes in
one working tree; acknowledging before sending loses it entirely when the
gateway is down. Claim-send-ack is the only order that is wrong in neither
direction.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

import design_data

log = logging.getLogger("hive.design")

_TIMEOUT = 900.0
SURFACE = "design"


def gateway_base_url() -> str:
    return os.getenv("GATEWAY_API_URL") or "http://hive-comms:8424"


def _service_headers() -> dict[str, str]:
    token = os.getenv("COMMS_BEARER_TOKEN", "")
    if not token:
        raise RuntimeError("COMMS_BEARER_TOKEN is not configured")
    return {"Authorization": f"Bearer {token}"}


def compose_message(record: dict[str, Any], page: str, kind: str) -> str:
    """The turn the Mind receives, built from the page as it now stands.

    The whole page travels, not a "go and look" ping. A notice would make
    the Mind fetch, which needs a credential pointing back at the console
    and turns one delivery into two failure modes; and the Mind answers by
    publishing to the page regardless, so the round trip buys nothing.

    Items with nothing on them are left out. A revision only ever touches
    what Daniel flagged, so a page of forty requirements with two questions
    on it should arrive as two questions.
    """
    lines = [
        f"<design-session id=\"{record['id']}\" page=\"{page}\" action=\"{kind}\">",
        f"Design session: {record['title']}",
    ]
    if kind == "build":
        # Build carries the whole agreed specification, not the flagged rows.
        # It is the last message of the design and the first of the work: a
        # go signal arriving with no requirements, no tests and no call graph
        # asks a mind to build from memory.
        lines.append(
            "Daniel pressed Build. The three pages are agreed — write the code."
        )
        for whole_page in design_data.PAGES:
            lines.append("")
            lines.append(f"## {whole_page}")
            for position, item in enumerate(
                design_data.ordered_items(record, whole_page), start=1
            ):
                label = item.get("text") or item.get("sentence") or item.get("name") or ""
                lines.append(f"{position}. {label}")
                symbol = item.get("symbol") or {}
                if symbol.get("name"):
                    lines.append(
                        f"   function: {symbol['name']} ({symbol.get('path') or 'planned'})"
                    )
                for case in item.get("cases") or []:
                    lines.append(
                        f"   {case.get('outcome', '')}: {case.get('input', '')}"
                        f" → {case.get('expected', '')}"
                    )
                if item.get("calls"):
                    lines.append("   calls: " + ", ".join(item["calls"]))
                if item.get("called_by"):
                    lines.append("   called by: " + ", ".join(item["called_by"]))
        lines.append("</design-session>")
        return "\n".join(lines)

    if kind == "accept":
        # An acceptance carries the page it accepted, whole. The feedback
        # branch below reports only the rows Daniel flagged, so an empty
        # acceptance composed through it arrives saying he changed nothing
        # and asked nothing — true, and useless: what the Mind needs is the
        # agreed wording it must now write the next page against.
        lines.append(
            f"Daniel accepted the {page} page as it stands. Write the next page."
        )
        lines.append("")
        lines.append(f"## {page} (agreed)")
        for position, item in enumerate(
            design_data.ordered_items(record, page), start=1
        ):
            label = item.get("text") or item.get("sentence") or item.get("name") or ""
            lines.append(f"{position}. {label}")
        lines.append("</design-session>")
        return "\n".join(lines)

    lines.append("Daniel submitted feedback on the design page.")
    lines.append("")

    for position, item in enumerate(design_data.ordered_items(record, page), start=1):
        question = str(item.get("feedback") or "").strip()
        edited = bool(item.get("edited"))
        if not question and not edited:
            continue
        label = item.get("text") or item.get("sentence") or item.get("name") or ""
        lines.append(f"{position}. {label}")
        if edited:
            lines.append("   (Daniel edited this wording.)")
        if question:
            lines.append(f"   He asks: {question}")
        lines.append("")

    if len(lines) == 4:
        lines.append("He changed nothing and asked nothing on this page.")
    lines.append("</design-session>")
    return "\n".join(lines)


def _post_sync(path: str, body: dict, *, headers: dict, stream: bool) -> None:
    url = f"{gateway_base_url().rstrip('/')}{path}"
    payload = json.dumps(body).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json", **headers},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=_TIMEOUT) as response:
        if stream:
            # The message route answers as a stream of the whole turn. It is
            # read to completion and discarded: what the Mind says comes back
            # on the surface Daniel is watching, and abandoning the socket
            # early would look to the gateway like the client vanished.
            for _ in response:
                pass
        else:
            response.read()


async def adopt_conversation(design_id: str, conversation_id: str) -> None:
    """Move the conversation onto this surface, ending whatever held it."""
    await asyncio.to_thread(
        _post_sync,
        f"/sessions/{urllib.parse.quote(conversation_id)}/activate",
        {
            "client_type": SURFACE,
            "client_ref": design_id,
            "surface": SURFACE,
            "surface_id": design_id,
        },
        headers=_service_headers(),
        stream=False,
    )


async def send_turn(conversation_id: str, content: str) -> None:
    await asyncio.to_thread(
        _post_sync,
        f"/sessions/{urllib.parse.quote(conversation_id)}/message",
        {"content": content},
        headers=_service_headers(),
        stream=True,
    )


async def deliver_notice(notice_id: int) -> bool:
    """Claim one notice, adopt the conversation, send the turn, acknowledge.

    Returns False for a notice that is not deliverable *yet* — nothing is
    attached, or the gateway refused — having put it back for the next
    attempt. It never raises into the caller: this runs detached from the
    request that queued it, and a submission that is safely stored must not
    be lost because the send failed.
    """
    try:
        notice = await design_data.claim_notice(notice_id)
    except design_data.DesignNotFound:
        return False
    try:
        record = await design_data.get_design_session(notice["design_id"])
        conversation_id = record["conversation_id"]
        if not conversation_id:
            await design_data.release_notice(notice_id)
            return False
        content = compose_message(record, notice["page"], notice["kind"])
        await adopt_conversation(record["id"], conversation_id)
        await send_turn(conversation_id, content)
    except Exception:  # noqa: BLE001 — a failed send is never a lost submission
        log.exception("design notice %s could not be delivered", notice_id)
        await design_data.release_notice(notice_id)
        return False
    except BaseException:
        # BaseException, not Exception. This runs detached, so a console
        # shutting down cancels it inside `send_turn`; a CancelledError that
        # skipped the release would leave the notice claimed forever, and
        # `deliver_pending` steps over claimed notices — so that submission
        # would never be delivered by any later path.
        log.exception("design notice %s could not be delivered", notice_id)
        await design_data.release_notice(notice_id)
        raise
    try:
        await design_data.mark_delivered(notice_id)
    except design_data.DesignNotFound:
        # The design session was deleted while the turn was in flight. The
        # message has already reached the mind; there is nothing left to mark
        # and nothing to retry, and raising here would abort the rest of the
        # queue.
        log.info("design notice %s vanished before it could be acknowledged", notice_id)
    return True


async def deliver_pending() -> int:
    """Send everything still owed, oldest first.

    Run on startup as well as after a submission: a console restarted
    between the queue and the send would otherwise leave a submission
    stored, unsent, and invisible until Daniel asked why nothing happened.
    """
    sent = 0
    for notice in await design_data.pending_notices():
        if notice.get("claimed_at"):
            continue
        try:
            if await deliver_notice(notice["id"]):
                sent += 1
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — one bad notice must not strand the queue
            log.exception("design notice %s aborted the queue", notice["id"])
    return sent
