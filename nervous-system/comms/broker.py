"""
Hive Mind — Message broker data layer.

Inter-mind conversations and messages, the mind registry, secret scopes and
the gateway's runtime settings, in the hive's one database through
`hive_data`. Pure data operations — no FastAPI dependency, no session manager
dependency.

Nothing here opens a database. The pool is opened once by the gateway's
lifespan, and every function below reaches it through a named `hive_data`
operation.
"""

import asyncio
import json
import logging
import time
import uuid

import hive_data
from hive_data import broker_ops, core

from comms import discord_out

from hive_logging import log_event

log = logging.getLogger("hive-mind.broker")

# ---------------------------------------------------------------------------
# Collection backstop — 8x notification threshold per request_type
# ---------------------------------------------------------------------------
BACKSTOP_SECONDS: dict[str, int] = {
    "quick_query": 2400,           # 40 min
    "research": 9600,              # 160 min
    "code_review": 9600,           # 160 min
    "content_generation": 7200,    # 120 min
    "data_analysis": 9600,         # 160 min
    "security_triage": 14400,      # 240 min
    "security_remediation": 43200, # 720 min
}

_DEFAULT_BACKSTOP = 21600  # 6 hours


def get_backstop_seconds(request_type: str | None) -> int:
    """Look up the collection backstop for a request_type."""
    if request_type is None:
        return _DEFAULT_BACKSTOP
    return BACKSTOP_SECONDS.get(request_type, _DEFAULT_BACKSTOP)


# ---------------------------------------------------------------------------
# Conversations
# ---------------------------------------------------------------------------
async def create_conversation(conversation_id: str) -> None:
    """Insert a new conversation row. A conversation that exists is left alone."""
    await core.create_conversation(id=conversation_id, created_at=time.time())


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------
def _storable(value):
    """A NUL character is refused by a Postgres `text` column where SQLite
    stored it; one in a message must not fail the write. Same rule as the
    session manager's."""
    if isinstance(value, str) and "\x00" in value:
        return value.replace("\x00", "\ufffd")
    return value


async def insert_message(
    *,
    message_id: str,
    conversation_id: str,
    from_mind: str,
    to_mind: str,
    content: str,
    rolling_summary: str,
    metadata: dict | None,
    status: str,
    message_number: int | None = None,
) -> dict:
    """Insert a message. Idempotent on message_id — returns existing row if duplicate.

    Auto-creates the conversation row if it doesn't exist. A duplicate id is
    reported as `existing` rather than raised, because the sms-gateway
    re-POSTs the same webhook on any non-2xx and a duplicate that raised
    would become a 500 retried for ever.

    `message_number` left unset is the next one in the conversation, decided
    inside the same unit as the insert while the conversation is held. Read
    beforehand by the caller, two concurrent deliveries derive the same
    number and the second collides on (conversation_id, message_number) —
    which SQLite's `INSERT OR IGNORE` swallowed as a silent drop reported as
    a duplicate, and Postgres refuses outright.
    """
    metadata_json = json.dumps(metadata) if metadata else None
    async with hive_data.transaction() as unit:
        await unit.hold_conversation(conversation_id)
        await core.create_conversation(
            id=conversation_id, created_at=time.time(), unit=unit)
        number = message_number
        if number is None:
            number = await core.next_message_number(conversation_id, unit=unit)
        written = await core.add_message(
            id=message_id, conversation_id=conversation_id,
            from_mind=from_mind, to_mind=to_mind, message_number=number,
            content=_storable(content), rolling_summary=_storable(rolling_summary),
            metadata=metadata_json, status=status, timestamp=time.time(),
            unit=unit)
        fetched = await broker_ops.get_message(message_id, unit=unit)
    inserted = written["rows"] > 0
    result = dict(fetched) if fetched else {"id": message_id}
    result["existing"] = not inserted
    log_event(
        log, "broker.message.accepted", message_id=message_id,
        conversation_id=conversation_id, from_mind=from_mind, to_mind=to_mind,
        message_number=result.get("message_number", number),
        content_chars=len(content), status=status,
        existing=not inserted, request_type=(metadata or {}).get("request_type"),
    )
    return result


async def get_messages(conversation_id: str) -> list[dict]:
    """Get all messages for a conversation, ordered by message_number."""
    return await core.list_messages(conversation_id)


async def get_message(message_id: str) -> dict | None:
    """Get a single message by ID."""
    return await broker_ops.get_message(message_id)


async def update_message_status(
    message_id: str,
    status: str,
    *,
    recipient_session_id: str | None = None,
    response_error: str | None = None,
) -> None:
    """Update message status and optional fields."""
    fields: dict = {"status": status}
    if recipient_session_id is not None:
        fields["recipient_session_id"] = recipient_session_id
    if response_error is not None:
        fields["response_error"] = _storable(response_error)
    await core.update_message(message_id, **fields)


async def get_next_message_number(conversation_id: str) -> int:
    """The next message number for a conversation, read on its own.

    For display only. A caller about to insert leaves `message_number` unset
    on `insert_message`, which decides it inside the insert's own unit.
    """
    return await core.next_message_number(conversation_id)


# ---------------------------------------------------------------------------
# Restart recovery
# ---------------------------------------------------------------------------
_RESTART_ERROR = "server_restart_during_delivery"


async def get_stranded_messages() -> dict:
    """Find messages stranded in non-terminal status."""
    return {
        "pending": await broker_ops.messages_with_status("pending"),
        "dispatched": await broker_ops.messages_with_status("dispatched"),
    }


async def recover_stranded_messages() -> list[dict]:
    """Recover stranded messages on startup.

    - dispatched → failed (callee session is dead after restart)
    - Returns pending messages for re-dispatch by the caller.
    """
    moved = await broker_ops.move_messages(
        was="dispatched", now="failed", response_error=_RESTART_ERROR)
    pending = await broker_ops.messages_with_status("pending")
    log.info("broker: startup recovery pending=%d dispatched_failed=%d",
             len(pending), moved["rows"])
    return pending


# ---------------------------------------------------------------------------
# Where the answer goes
# ---------------------------------------------------------------------------
def channel_destination(metadata: dict | None) -> str | None:
    """The Discord channel this dispatch's answer belongs in, or None.

    A channel id is all digits. Anything else - a channel *name* somebody
    typed instead of its id, a placeholder left in a skill - is not one, and
    is dropped rather than carried forward as a destination that 404s on the
    first post. The same rule the scheduler applies to `discord_channel` in a
    scheduled skill's frontmatter, for the same reason: `isdigit` alone is
    true of superscripts and fullwidth forms, which address no channel.
    """
    value = str((metadata or {}).get("discord_channel") or "").strip()
    return value if value.isascii() and value.isdigit() else None


async def _discord_claim(mind_id: str) -> str:
    """The Discord user this mind's bot answers, or empty.

    The channel is the session's `client_ref`; `surface_id` is the person,
    which is what the bot keys its own sessions on. Getting it wrong mints a
    second session for a channel that already has one, and Daniel's replies
    then follow a different conversation than the escalation opened.
    """
    try:
        from comms import surface_claims  # noqa: PLC0415

        state = await surface_claims.claim_state(mind_id, "discord")
        return state.claimed_ref or ""
    except Exception:
        log.warning("broker: could not read the discord claim mind=%s", mind_id)
        return ""


# ---------------------------------------------------------------------------
# Wakeup prompt
# ---------------------------------------------------------------------------
def build_wakeup_prompt(
    from_mind: str,
    to_mind: str,
    conversation_id: str,
    content: str,
    rolling_summary: str,
    message_number: int,
    discord_channel: str | None = None,
) -> str:
    """Construct the wakeup prompt sent to the callee's new session.

    `discord_channel` changes who the answer is addressed to. A dispatch with
    no channel is answered to the mind that asked, and the caller is the only
    reader. A dispatch with one is also posted into that channel, where the
    reader is Daniel - and a mind that was not told so writes a report to
    another machine and Daniel gets handed it.
    """
    parts = [f"You have a new message from {from_mind}.", "", f"Conversation ID: {conversation_id}", ""]

    if message_number > 1 and rolling_summary:
        parts.extend([
            "Summary of conversation so far:",
            rolling_summary,
            "",
        ])

    parts.extend(["New message:", content, ""])

    if discord_channel:
        parts.extend([
            "Complete the requested work. Your response is posted into Daniel's",
            f"Discord channel {discord_channel} as well as returned to {from_mind},",
            "and that channel is where he will read it and reply to you. So write it",
            "to Daniel: what happened, what you did about it, and what is left for",
            f"him to decide. Not a report addressed to {from_mind}.",
        ])
    else:
        parts.extend([
            "Complete the requested work and respond with your findings. Your response will be",
            f"automatically collected and delivered back to {from_mind}.",
        ])

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Response collection
# ---------------------------------------------------------------------------
async def _collect_response(send_generator) -> str:
    """Iterate an SSE async generator and extract assistant text.

    Same extraction logic as tools/stateful/inter_mind.py:70-86.
    """
    response_text = ""
    async for event in send_generator:
        if not isinstance(event, dict):
            continue
        if event.get("type") == "assistant":
            for block in event.get("message", {}).get("content", []):
                if block.get("type") == "text":
                    response_text += block.get("text", "")
        elif event.get("type") == "result":
            if not response_text:
                response_text = event.get("result", "")
    return response_text


# ---------------------------------------------------------------------------
# Wakeup and collect
# ---------------------------------------------------------------------------
async def wakeup_and_collect(
    session_mgr,
    *,
    message_id: str,
    conversation_id: str,
    from_mind: str,
    to_mind: str,
    content: str,
    rolling_summary: str,
    message_number: int,
    metadata: dict | None,
) -> None:
    """Background task: wake callee, collect response, write to DB.

    1. Update message to dispatched
    2. Create callee session
    3. Send wakeup prompt and collect SSE response (with backstop timeout)
    4. Write response as new message
    5. Update original message to completed
    6. Kill callee session
    """
    request_type = (metadata or {}).get("request_type")
    backstop = get_backstop_seconds(request_type)
    session_id = None
    # A mind addressing itself is the standing wake channel: one conversation
    # per mind, reused and never killed. A dispatch between two different
    # minds keeps its own session per message, because that one is a task with
    # a beginning and an end and a caller waiting on its result.
    #
    # A dispatch naming a Discord channel is neither. It lands in that
    # channel's own session - the one Daniel is already talking to there - so
    # the answer arrives in a thread he can reply into, and his reply
    # continues the conversation the escalation opened rather than opening a
    # second one nobody is reading. An explicit destination wins over the
    # self-wake, because a mind that names a channel has said where it wants
    # the answer read.
    channel_id = channel_destination(metadata)
    standing = from_mind == to_mind and channel_id is None
    # Both of these outlive the exchange. Killing the channel's session would
    # take Daniel's thread with it.
    resident = standing or channel_id is not None
    if channel_id is not None:
        client_type, client_ref = f"discord:{to_mind}", channel_id
    elif standing:
        client_type, client_ref = "broker", f"broker-self-{to_mind}"
    else:
        client_type, client_ref = "broker", f"broker-{conversation_id}-{to_mind}"

    try:
        # 1. Dispatched
        await update_message_status(message_id, "dispatched")

        # 2. The callee's session — use the mind's configured model
        mind_model = None
        row = await get_mind_by_id(to_mind)
        if row:
            mind_model = row.get("model")
        mind_name = (row or {}).get("name") or to_mind
        if channel_id is not None:
            # Reuse before create, and keyed exactly as the Discord bot keys
            # its own: `client_type` carries the mind so one bot's channel is
            # not another's, `client_ref` is the channel, `surface_id` the
            # person. A mismatch on any of the three mints a parallel session
            # for a channel that already has one.
            existing = await session_mgr.get_active_session(client_type, client_ref)
            if existing:
                session = existing
            else:
                session = await session_mgr.create_session(
                    surface=client_type,
                    surface_id=await _discord_claim(to_mind) or channel_id,
                    client_ref=client_ref,
                    mind_id=to_mind,
                    model=mind_model,
                )
        elif standing:
            # A mind waking itself is a scheduler, a watch or a pass, and it
            # does it over and over: the sentinel alone had sent 21,114 of
            # these, one session minted and killed per wake, which is a
            # session list nobody can read and a mind with no memory of the
            # last thing it was told. So a self-addressed wake has one
            # conversation per mind, keyed on the mind rather than on the
            # message, and it is named so the operator can find it.
            existing = await session_mgr.get_active_session("broker", client_ref)
            if existing:
                session = existing
            else:
                session = await session_mgr.create_session(
                    surface="broker",
                    surface_id=client_ref,
                    client_ref=client_ref,
                    mind_id=to_mind,
                    model=mind_model,
                )
                try:
                    await session_mgr.set_session_name(
                        session["id"], name=f"{mind_name}-wakes"
                    )
                except Exception:
                    # A name is how the operator finds it, not how the wake
                    # reaches the mind. Losing the label must not lose the
                    # dispatch.
                    log.warning(
                        "broker: could not name standing wake session=%s",
                        session["id"],
                    )
        else:
            session = await session_mgr.create_session(
                surface="broker",
                surface_id=f"broker-{conversation_id}",
                client_ref=client_ref,
                mind_id=to_mind,
                model=mind_model,
            )
        session_id = session["id"]
        await update_message_status(message_id, "dispatched", recipient_session_id=session_id)

        # 3. Send wakeup and collect response
        from_row = await get_mind_by_id(from_mind)
        from_display = (from_row.get("name") if from_row else None) or from_mind
        prompt = build_wakeup_prompt(
            from_display, to_mind, conversation_id, content, rolling_summary,
            message_number, discord_channel=channel_id,
        )
        response_text = await asyncio.wait_for(
            _collect_response(session_mgr.send_message(session_id, prompt)),
            timeout=backstop,
        )

        # 4. Write response as new message
        await insert_message(
            message_id=str(uuid.uuid4()),
            conversation_id=conversation_id,
            from_mind=to_mind,
            to_mind=from_mind,
            content=response_text,
            rolling_summary="",
            metadata=None,
            status="completed",
        )

        # 5. Mark original as completed
        await update_message_status(message_id, "completed")

        # 6. The copy for Daniel, last and separately. The caller already has
        # its answer and the message row already says so, so a Discord outage
        # or an unset token costs the copy and not the dispatch - which is the
        # right way round, because the mind cannot be asked to do the work
        # twice.
        if channel_id is not None and (response_text or "").strip():
            try:
                await discord_out.post(channel_id, response_text)
            except Exception:
                log.exception(
                    "broker: discord delivery failed channel=%s message=%s",
                    channel_id, message_id,
                )
                log_event(log, "broker.wakeup.delivery_failed",
                          level=logging.ERROR, conversation_id=conversation_id,
                          message_id=message_id, from_mind=from_mind,
                          to_mind=to_mind, channel_id=channel_id,
                          session_id=session_id)
        log.info("broker: wakeup complete conversation=%s from=%s to=%s", conversation_id, from_mind, to_mind)
        log_event(log, "broker.wakeup.completed", conversation_id=conversation_id,
                  message_id=message_id, from_mind=from_mind, to_mind=to_mind,
                  session_id=session_id)

    except asyncio.TimeoutError:
        await update_message_status(
            message_id, "timed_out",
            response_error=f"backstop exceeded ({backstop}s)",
        )
        log.warning("broker: backstop timeout conversation=%s message=%s backstop=%ds", conversation_id, message_id, backstop)
        log_event(log, "broker.wakeup.timed_out", level=logging.ERROR,
                  conversation_id=conversation_id, message_id=message_id,
                  from_mind=from_mind, to_mind=to_mind, timeout_seconds=backstop)

    except Exception as e:
        await update_message_status(
            message_id, "failed",
            response_error=str(e),
        )
        log.error("broker: wakeup failed conversation=%s message=%s error=%s", conversation_id, message_id, e)
        log_event(log, "broker.wakeup.failed", level=logging.ERROR,
                  conversation_id=conversation_id, message_id=message_id,
                  from_mind=from_mind, to_mind=to_mind,
                  error_type=type(e).__name__, exc_info=True)

    finally:
        # 7. Kill the callee session — unless it is one that outlives the
        # exchange: the standing wake channel, which the next wake reuses, or
        # a Discord channel's session, which is Daniel's thread. Killing is
        # what made every wake mint a new row, and a killed session is also a
        # mind that opens its next wake having forgotten the last one.
        if session_id is not None and not resident:
            try:
                await session_mgr.kill_session(session_id)
            except Exception:
                log.warning("broker: failed to kill callee session=%s", session_id)


# ---------------------------------------------------------------------------
# Mind registration
# ---------------------------------------------------------------------------
class OwnerRequired(ValueError):
    """A mind comms has never seen registered without naming its owner."""


def normalize_owner(owner: str | None) -> str | None:
    """The owner key: the user's lowercase username, or None when unnamed."""
    value = (owner or "").strip().lower()
    return value or None


async def register_mind(
    *,
    mind_id: str,
    name: str,
    gateway_url: str,
    model: str,
    harness: str,
    session_token: str | None = None,
    voice: str | None = None,
    voice_engine: str | None = None,
    owner: str | None = None,
) -> None:
    """Register (or update) a mind in the broker database.

    If the mind already exists (matched by mind_id), updates the mutable
    fields and preserves registered_at. The UUID `mind_id` is the durable
    identity; `name` is a human-readable label that may be renamed.

    `session_token` is the credential the gateway must present on every call
    it makes to that mind. A registration that omits it leaves the stored one
    in place: a mind re-registering from an older build, or one that failed to
    read its own token file, must not silently lock the gateway out.

    `voice` and `voice_engine` are cached the same way and for the same
    reason: the mind's `runtime.yaml` is the truth, this row is what the
    speaking surfaces read, and a registration that omitted either must not
    blank a working voice.

    `owner` is the user the mind belongs to, held here and keyed by
    `mind_id`. A known mind may omit it and keeps the stored one; a mind this
    gateway has never seen must name one (`OwnerRequired`). Naming a different
    owner than the stored one is ignored with a warning and the rest of the
    registration lands: only `set_mind_owner`, behind the admin route, moves a
    mind between users, and refusing the whole registration would freeze a
    mind an admin had just moved until its runtime.yaml caught up. A row that
    predates ownership (owner still unset) takes the one named.

    The read and the write are one unit holding the mind's identity, so two
    registrations racing at boot cannot both find no row and both decide the
    owner rule on a stale read.
    """
    owner = normalize_owner(owner)
    now = time.time()
    async with hive_data.transaction() as unit:
        await unit.hold(f"hive:mind:{mind_id}")
        existing = await core.get_mind(mind_id, unit=unit)

        if not existing and owner is None:
            raise OwnerRequired(f"mind {mind_id} is not registered and names no owner")
        if existing and owner and existing["owner"] and owner != existing["owner"]:
            log_event(log, "mind.owner_ignored", level=logging.WARNING,
                      mind_id=mind_id, stored_owner=existing["owner"], named_owner=owner)
            owner = existing["owner"]

        if existing:
            fields: dict = {
                "name": name, "gateway_url": gateway_url, "model": model,
                "harness": harness, "last_seen": now,
            }
            if owner and not existing["owner"]:
                fields["owner"] = owner
            if session_token:
                fields["session_token"] = session_token
            if voice:
                fields["voice"] = voice
            if voice_engine:
                fields["voice_engine"] = voice_engine
            changed = await broker_ops.update_mind(mind_id, unit=unit, **fields)
            if changed["rows"] == 0:  # pragma: no cover - the hold rules it out
                raise RuntimeError(f"mind {mind_id} vanished under its own hold")
        else:
            await core.upsert_mind(
                mind_id=mind_id, name=name, gateway_url=gateway_url,
                model=model, harness=harness, registered_at=now, last_seen=now,
                session_token=session_token or None, voice=voice or None,
                voice_engine=voice_engine or None, owner=owner, unit=unit)
    log_event(log, "mind.registered" if not existing else "mind.updated",
              mind_id=mind_id, mind_name=name, gateway_url=gateway_url,
              model=model, harness=harness,
              owner=owner or (existing["owner"] if existing else None))


async def set_mind_owner(mind_id: str, owner: str) -> dict | None:
    """Move a mind to another user. The admin path, and the only one.

    Returns the updated mind, or None when no mind has that id. A blank owner
    raises `ValueError`: every mind has exactly one.
    """
    normalized = normalize_owner(owner)
    if normalized is None:
        raise ValueError("owner must name a user")
    changed = await broker_ops.update_mind(mind_id, owner=normalized)
    if changed["rows"] == 0:
        return None
    log_event(log, "mind.owner_set", mind_id=mind_id, owner=normalized)
    return await get_mind_by_id(mind_id)


def _public_mind(row) -> dict:
    """A mind row as any caller may see it.

    `session_token` is the credential that authenticates the gateway to that
    mind, so it is stripped here rather than at each call site: every listing
    answers to the service token every surface bot holds, and a `SELECT *`
    reaching one of them would hand Arnold's token to Zack's bot. `hive_data`
    withholds it from every read already; the strip stays as the second lock.
    """
    d = dict(row)
    d["id"] = d.pop("mind_id")
    d.pop("session_token", None)
    return d


async def get_mind_session_token(mind_id: str) -> str | None:
    """The credential this gateway must present when calling that mind.

    The only path that returns it. No HTTP route exposes it.
    """
    return await core.mind_session_token(mind_id) or None


async def get_registered_minds() -> list[dict]:
    """Return all registered minds as a list of dicts.

    Response shape per the migration spec: each entry includes `id` (the
    UUID), `name` (display label), and `gateway_url` (where to dispatch).
    """
    return [_public_mind(r) for r in await core.list_minds()]


async def get_mind(name: str) -> dict | None:
    """Get a single mind by display name. Returns dict or None if not found."""
    row = await core.get_mind_by_name(name)
    return _public_mind(row) if row else None


async def get_mind_by_id(mind_id: str) -> dict | None:
    """Get a single mind by UUID (mind_id). The canonical routing lookup —
    broker resolves mind_id → gateway_url through this function.
    """
    row = await core.get_mind(mind_id)
    return _public_mind(row) if row else None


#: What the console's partial update may change. `last_seen` is always set.
_UPDATABLE = frozenset({"gateway_url", "model", "harness", "voice", "voice_engine"})


async def update_mind(name: str, **fields) -> dict | None:
    """Partially update a mind's fields. Always updates last_seen.

    Allowed fields: gateway_url, model, harness, voice, voice_engine.
    Returns updated dict or None if mind not found.
    """
    existing = await core.get_mind_by_name(name)
    if existing is None:
        return None

    updates = {k: v for k, v in fields.items() if k in _UPDATABLE}
    updates["last_seen"] = time.time()
    changed = await broker_ops.update_mind(existing["mind_id"], **updates)
    if changed["rows"] == 0:
        # Deleted between the read and the write.
        return None
    log_event(log, "mind.updated", mind_name=name, changed_fields=sorted(updates))
    return await get_mind(name)


class MindHasConversations(RuntimeError):
    """A mind that still has conversations, which deleting it would orphan.

    `core.sessions.mind_id` is ON DELETE RESTRICT: every conversation names
    the mind it ran on, and a delete that cascaded would take the operator's
    history with it. So the delete is refused and says why, rather than
    surfacing the database's foreign-key violation as a 500.
    """

    def __init__(self, name: str, sessions: int) -> None:
        self.name = name
        self.sessions = sessions
        super().__init__(
            f"mind '{name}' still has {sessions} conversation"
            f"{'' if sessions == 1 else 's'}; it cannot be deleted while "
            "they exist"
        )


class MindStillReferenced(RuntimeError):
    """A mind something other than a conversation still points at.

    Its graph edges and design sessions are RESTRICT as well. Counted
    conversations are the common case and get their own sentence; anything
    else the database refuses arrives here, named, rather than as a 500.
    """

    def __init__(self, name: str) -> None:
        self.name = name
        super().__init__(
            f"mind '{name}' is still referenced by records that name it "
            "(its graph or its design sessions); it cannot be deleted while "
            "they exist"
        )


async def delete_mind(name: str) -> bool:
    """Delete a mind by name. Returns True if deleted, False if not found.

    One unit: the mind's secret scopes are cleared with it, because they are
    keyed by name with nothing cascading to them, and the next mind
    registered under the same name would otherwise inherit every secret the
    old one could read. Its surface claims go by cascade. Its conversations
    do not go at all: a mind that still has any is refused with
    `MindHasConversations`, and one still referenced by anything else the
    database protects with `MindStillReferenced`. Nothing here deletes
    metering or audit rows.
    """
    try:
        # The identity's hold - the one `register_mind` takes - rather than
        # the name's. Under a name hold a mind re-registering at boot read
        # itself, this deleted it, and the registration's update changed
        # nothing and answered as though it had. A unit takes one hold and
        # takes it before any read, so the identity is looked up first and
        # confirmed under the hold.
        named = await core.get_mind_by_name(name)
        if named is None:
            return False
        async with hive_data.transaction() as unit:
            await unit.hold(f"hive:mind:{named['mind_id']}")
            mind = await core.get_mind_by_name(name, unit=unit)
            if mind is not None and mind["mind_id"] != named["mind_id"]:
                raise hive_data.errors.Conflict(
                    f"the mind named {name} changed while it was being deleted")
            if mind is None:
                deleted = False
            else:
                sessions = await broker_ops.count_mind_sessions(
                    mind["mind_id"], unit=unit)
                if sessions:
                    raise MindHasConversations(name, sessions)
                await broker_ops.revoke_all_secret_scopes(name, unit=unit)
                result = await broker_ops.delete_mind(mind["mind_id"], unit=unit)
                deleted = result["rows"] > 0
    except hive_data.MissingReference as exc:
        log_event(log, "mind.delete_refused", level=logging.WARNING,
                  mind_name=name, reason="still_referenced")
        raise MindStillReferenced(name) from exc
    except MindHasConversations as exc:
        log_event(log, "mind.delete_refused", level=logging.WARNING,
                  mind_name=name, reason="has_conversations",
                  sessions=exc.sessions)
        raise
    log_event(log, "mind.deleted", mind_name=name, deleted=deleted)
    return deleted


# ---------------------------------------------------------------------------
# Secret scoping policy
# ---------------------------------------------------------------------------
async def grant_secret_scope(mind_name: str, secret_key: str) -> None:
    """Grant a mind access to a secret key. Idempotent."""
    await core.grant_secret_scope(
        mind_name=mind_name, secret_key=secret_key, granted_at=time.time())


async def revoke_secret_scope(mind_name: str, secret_key: str) -> None:
    """Revoke a mind's access to a secret key."""
    await core.revoke_secret_scope(mind_name=mind_name, secret_key=secret_key)


async def get_secret_scopes(mind_name: str) -> list[str]:
    """Return all secret keys a mind is allowed to access."""
    return [row["secret_key"] for row in await core.list_secret_scopes(mind_name)]


async def check_secret_scope(mind_name: str, secret_key: str) -> bool:
    """Check if a mind is allowed to access a specific secret key."""
    return secret_key in await get_secret_scopes(mind_name)


# ---------------------------------------------------------------------------
# Settings (small key/value store for runtime feature flags)
# ---------------------------------------------------------------------------
async def get_setting(key: str, default: str | None = None) -> str | None:
    """Return the stored value for a settings key, or `default` if absent."""
    row = await core.get_setting(key)
    return row["value"] if row else default


async def set_setting(key: str, value: str) -> None:
    """Upsert a settings row."""
    await core.put_setting(key, value, time.time())
