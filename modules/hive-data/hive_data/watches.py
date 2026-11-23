"""Gateway watches: who is woken when a briefed session stops (feature 2.1).

A watch is owned by the conversation that sent a brief and names the session
it briefed. Both ends are `core.sessions.id`, the id that survives a
rotation. Every operation takes an optional unit, like the rest of this
package, and the ones that decide who is owed a wake do it in one statement so
two paths finding the same stop cannot both create it.

Nothing here sends anything. The gateway reads these rows and does the
sending; what this module guarantees is the bookkeeping that makes "once":
a stop is one row, a wake for it is one row per watch, and a wake is claimed
and delivered by conditional updates.
"""

from __future__ import annotations

import time
from typing import Any

from . import _sql  # noqa: F401 - kept beside its siblings
from .core import _execute, _fetch, _fetch_one
from .rows import UNSET  # noqa: F401
from .unit import Unit

Row = dict[str, Any]

#: Stops kept for each session. The answer a sender fetches is the newest.
STOPS_KEPT = 5


def _now(now: float | None) -> float:
    return time.time() if now is None else now


async def ask(watched: str, sender: str, task: str, digest: bytes, *,
              presented: bytes | None = None, now: float | None = None,
              unit: Unit | None = None) -> Row:
    """Create the watch, or answer for the one that is already there.

    Asking twice from one conversation about one session leaves one watch.
    `created` says whether this call made it; only then does the caller hand
    out the capability `digest` was made from. A repeat ask changes the task
    only when it presents the capability the watch was made with, so another
    conversation of the same mind naming this sender cannot rewrite it. A new
    watch is not armed: it wakes nothing until `arm` says its brief started.
    """
    made = await _fetch_one(
        unit,
        'INSERT INTO core.session_watches (watched_session_id, '
        'sender_session_id, task, capability_digest, created_at) '
        'VALUES ($1, $2, $3, $4, $5) '
        'ON CONFLICT (watched_session_id, sender_session_id) DO NOTHING '
        'RETURNING watched_session_id', watched, sender, task, digest,
        _now(now))
    if made is None and presented is not None:
        await _execute(
            unit,
            'UPDATE core.session_watches SET task = $3 '
            'WHERE watched_session_id = $1 AND sender_session_id = $2 '
            'AND capability_digest = $4', watched, sender, task, presented)
    row = await _fetch_one(
        unit,
        'SELECT watched_session_id, sender_session_id, task, armed, '
        'since_turn_id, created_at FROM core.session_watches '
        'WHERE watched_session_id = $1 AND sender_session_id = $2',
        watched, sender)
    assert row is not None
    return {**row, "created": made is not None}


async def arm(watched: str, sender: str, since_turn_id: int, *,
              unit: Unit | None = None) -> Row:
    """The brief's turn has started: this watch now wakes at stops.

    Only a watch that is not armed already moves, so a second brief cannot
    push `since_turn_id` past a stop the watch is owed.
    """
    return await _execute(
        unit,
        'UPDATE core.session_watches SET armed = true, since_turn_id = $3 '
        'WHERE watched_session_id = $1 AND sender_session_id = $2 AND NOT armed',
        watched, sender, since_turn_id)


async def held_by(sender: str, *, unit: Unit | None = None) -> list[Row]:
    """The watches a conversation holds, never the capability."""
    return await _fetch(
        unit,
        'SELECT watched_session_id, sender_session_id, task, armed, '
        'created_at FROM core.session_watches WHERE sender_session_id = $1 '
        'ORDER BY created_at, watched_session_id', sender)


async def on_session(watched: str, *, unit: Unit | None = None) -> list[Row]:
    return await _fetch(
        unit,
        'SELECT watched_session_id, sender_session_id, task, armed, '
        'since_turn_id, created_at FROM core.session_watches '
        'WHERE watched_session_id = $1 ORDER BY sender_session_id', watched)


async def finish(watched: str, sender: str, digest: bytes, *,
                 unit: Unit | None = None) -> str:
    """The owner says it is finished: `finished`, `absent` or `refused`.

    The watch goes and the wakes owed under it go with it. Already gone is
    `absent`, which a caller treats as the same success; a watch that exists
    and is not opened by this capability is `refused` and untouched.
    """
    deleted = await _fetch_one(
        unit,
        'DELETE FROM core.session_watches WHERE watched_session_id = $1 '
        'AND sender_session_id = $2 AND capability_digest = $3 '
        'RETURNING watched_session_id', watched, sender, digest)
    if deleted is not None:
        return "finished"
    exists = await _fetch_one(
        unit,
        'SELECT 1 AS found FROM core.session_watches '
        'WHERE watched_session_id = $1 AND sender_session_id = $2',
        watched, sender)
    return "refused" if exists is not None else "absent"


async def end_on_session(watched: str, *, unit: Unit | None = None) -> Row:
    """The briefed session is being ended: its watches end, no wake is sent."""
    return await _execute(
        unit, 'DELETE FROM core.session_watches WHERE watched_session_id = $1',
        watched)


async def record_stop(session_id: str, stop_key: str, outcome: str, *,
                      wake: bool = False, turn_id: int | None = None,
                      answer_turn_id: int | None = None,
                      now: float | None = None,
                      unit: Unit | None = None) -> list[str]:
    """Record that a turn of this session ended, and owe its watchers a wake.

    `outcome` is `ended` or `died`. `wake` marks a turn that was itself a
    wake: it is recorded, so the answer can still be fetched, and wakes nobody. A stop key already recorded is the
    same stop found a second way: it changes nothing and returns []. The
    returned senders are the conversations newly owed a wake. A watch that is
    not armed, or armed after this stop's turn, is not owed one.
    """
    moment = _now(now)
    stored = await _fetch_one(
        unit,
        'INSERT INTO core.session_stops (session_id, stop_key, outcome, wake, '
        'turn_id, answer_turn_id, stopped_at) VALUES ($1, $2, $3, $4, $5, $6, $7) '
        'ON CONFLICT (session_id, stop_key) DO NOTHING RETURNING id',
        session_id, stop_key, outcome, wake, turn_id, answer_turn_id, moment)
    if stored is None:
        return []
    await _execute(
        unit,
        'DELETE FROM core.session_stops WHERE session_id = $1 AND id <= '
        '(SELECT id FROM core.session_stops WHERE session_id = $1 '
        'ORDER BY id DESC OFFSET $2 LIMIT 1)', session_id, STOPS_KEPT)
    if wake:
        return []
    rows = await _fetch(
        unit,
        'INSERT INTO core.session_wakes (watched_session_id, '
        'sender_session_id, stop_key, outcome, created_at) '
        'SELECT watched_session_id, sender_session_id, $2, $3, $4 '
        'FROM core.session_watches WHERE watched_session_id = $1 AND armed '
        'AND sender_session_id <> $1 AND since_turn_id < COALESCE($5, since_turn_id + 1) '
        'ON CONFLICT (watched_session_id, sender_session_id, stop_key) '
        'DO NOTHING RETURNING sender_session_id',
        session_id, stop_key, outcome, moment, turn_id)
    return [row["sender_session_id"] for row in rows]


async def owe_wake(watched: str, sender: str, stop_key: str, outcome: str, *,
                   now: float | None = None,
                   unit: Unit | None = None) -> bool:
    """Owe one watch one wake for a stop that is not a stop of the session.

    A brief that was accepted and then failed before its turn began has no
    turn to end. Its sender is still owed the news, and nobody else is.
    Arms the watch as well, so the death is not left behind an unarmed row.
    """
    row = await _fetch_one(
        unit,
        'INSERT INTO core.session_wakes (watched_session_id, '
        'sender_session_id, stop_key, outcome, created_at) '
        'SELECT watched_session_id, sender_session_id, $3, $4, $5 '
        'FROM core.session_watches WHERE watched_session_id = $1 '
        'AND sender_session_id = $2 '
        'ON CONFLICT (watched_session_id, sender_session_id, stop_key) '
        'DO NOTHING RETURNING id', watched, sender, stop_key, outcome,
        _now(now))
    return row is not None


async def latest_stop(session_id: str, *,
                      unit: Unit | None = None) -> Row | None:
    return await _fetch_one(
        unit,
        'SELECT id, session_id, stop_key, outcome, wake, turn_id, answer_turn_id, '
        'stopped_at FROM core.session_stops WHERE session_id = $1 '
        'ORDER BY id DESC LIMIT 1', session_id)


async def turn_is_wake(sender: str, *, unit: Unit | None = None) -> bool:
    """Whether the sender is inside a turn that was itself a wake."""
    row = await _fetch_one(
        unit,
        'SELECT 1 AS open FROM core.session_wakes WHERE sender_session_id = $1 '
        'AND turn_open LIMIT 1', sender)
    return row is not None


async def end_wake_turn(sender: str, *, unit: Unit | None = None) -> int:
    """The sender's wake turn is over. How many wakes it was carrying."""
    rows = await _fetch(
        unit,
        'UPDATE core.session_wakes SET turn_open = false '
        'WHERE sender_session_id = $1 AND turn_open RETURNING id', sender)
    return len(rows)


async def claim_pending(sender: str, *, now: float | None = None,
                        unit: Unit | None = None) -> list[Row]:
    """Take every wake owed to this sender in one statement.

    A claimed wake is invisible to a second claimer, which is what keeps the
    event path and the slow check from sending one stop twice. Oldest first,
    carrying the task each watch was asked with.
    """
    return await _fetch(
        unit,
        'WITH taken AS (UPDATE core.session_wakes SET state = \'claimed\', '
        'claimed_at = $2 WHERE sender_session_id = $1 AND state = \'pending\' '
        'RETURNING id, watched_session_id, sender_session_id, stop_key, '
        'outcome, created_at) '
        'SELECT t.id, t.watched_session_id, t.sender_session_id, t.stop_key, '
        't.outcome, t.created_at, w.task FROM taken t '
        'JOIN core.session_watches w ON w.watched_session_id = '
        't.watched_session_id AND w.sender_session_id = t.sender_session_id '
        'ORDER BY t.created_at, t.id', sender, _now(now))


async def release_claims(ids: list[int], *, unit: Unit | None = None) -> int:
    """A delivery did not happen: the wakes go back to being owed."""
    rows = await _fetch(
        unit,
        'UPDATE core.session_wakes SET state = \'pending\', claimed_at = NULL '
        'WHERE id = ANY($1::bigint[]) AND state = \'claimed\' RETURNING id',
        ids)
    return len(rows)


async def mark_delivered(ids: list[int], *, now: float | None = None,
                         unit: Unit | None = None) -> int:
    """The wake turn has been handed to the sender. Never claimed again."""
    rows = await _fetch(
        unit,
        'UPDATE core.session_wakes SET state = \'delivered\', turn_open = true, '
        'delivered_at = $2 WHERE id = ANY($1::bigint[]) AND state = \'claimed\' '
        'RETURNING id', ids, _now(now))
    return len(rows)


async def release_stale_claims(older_than: float, *,
                               unit: Unit | None = None) -> int:
    """A claim whose claimer died (a restart mid-delivery) is owed again."""
    rows = await _fetch(
        unit,
        'UPDATE core.session_wakes SET state = \'pending\', claimed_at = NULL '
        'WHERE state = \'claimed\' AND claimed_at < $1 RETURNING id', older_than)
    return len(rows)


async def senders_owed(*, unit: Unit | None = None) -> list[str]:
    """Every conversation with at least one wake waiting."""
    rows = await _fetch(
        unit,
        'SELECT DISTINCT sender_session_id FROM core.session_wakes '
        'WHERE state = \'pending\' ORDER BY sender_session_id')
    return [row["sender_session_id"] for row in rows]


async def armed_watches(*, unit: Unit | None = None) -> list[Row]:
    return await _fetch(
        unit,
        'SELECT watched_session_id, sender_session_id, since_turn_id, '
        'created_at FROM core.session_watches WHERE armed '
        'ORDER BY watched_session_id, sender_session_id')


async def newest_turn_id(session_id: str, *, unit: Unit | None = None) -> int:
    row = await _fetch_one(
        unit, 'SELECT COALESCE(max(id), 0) AS id FROM core.session_turns '
        'WHERE session_id = $1', session_id)
    return int(row["id"]) if row else 0


async def unrecorded_answers(session_id: str, since_turn_id: int, *,
                             unit: Unit | None = None) -> list[Row]:
    """Assistant turns after the watch armed that no recorded stop accounts for.

    A stop the gateway never saw happen as an event: a terminal's report
    whose stop was never recorded, or a gateway that died between a reply
    being stored and its stop being written.
    """
    return await _fetch(
        unit,
        'SELECT t.id FROM core.session_turns t WHERE t.session_id = $1 '
        'AND t.role = \'assistant\' AND t.id > $2 AND NOT EXISTS ('
        'SELECT 1 FROM core.session_stops s WHERE s.session_id = $1 AND '
        '(s.turn_id = t.id OR s.answer_turn_id = t.id)) ORDER BY t.id',
        session_id, since_turn_id)


async def trailing_user_turn(session_id: str, since_turn_id: int, *,
                             unit: Unit | None = None) -> Row | None:
    """The newest turn, when it is a user turn after the watch armed that
    nothing answered and no stop covers: a turn that died with its process."""
    return await _fetch_one(
        unit,
        'SELECT t.id FROM core.session_turns t WHERE t.session_id = $1 '
        'AND t.id > $2 AND t.role <> \'assistant\' AND t.id = ('
        'SELECT max(id) FROM core.session_turns WHERE session_id = $1) '
        'AND NOT EXISTS (SELECT 1 FROM core.session_stops s WHERE '
        's.session_id = $1 AND s.turn_id = t.id)', session_id, since_turn_id)


async def reset_open_turns(*, unit: Unit | None = None) -> int:
    """A gateway start: no wake turn is running, except one owed a re-send."""
    rows = await _fetch(
        unit,
        'UPDATE core.session_wakes w SET turn_open = false WHERE w.turn_open '
        'AND NOT EXISTS (SELECT 1 FROM core.sessions s WHERE s.id = '
        'w.sender_session_id AND s.resend_content IS NOT NULL) RETURNING w.id')
    return len(rows)


async def undeliver(ids: list[int], *, unit: Unit | None = None) -> int:
    """The sender's mind never took the wake turn: the wakes are owed again."""
    rows = await _fetch(
        unit,
        'UPDATE core.session_wakes SET state = \'pending\', turn_open = false, '
        'claimed_at = NULL, delivered_at = NULL WHERE id = ANY($1::bigint[]) '
        'AND state = \'delivered\' RETURNING id', ids)
    return len(rows)


async def wake_state(mind_id: str, *, since: float | None = None,
                     unit: Unit | None = None) -> dict:
    """What a mind would lose if its container went down now.

    `owed` counts wakes waiting or being delivered to any conversation of the
    mind; `delivered_since` lists wakes handed to it after `since`, which a
    restart in that window may have cut off.
    """
    owed = await _fetch_one(
        unit,
        'SELECT count(*) AS n FROM core.session_wakes w JOIN core.sessions s '
        'ON s.id = w.sender_session_id WHERE s.mind_id = $1 '
        'AND w.state IN (\'pending\', \'claimed\')', mind_id)
    delivered = []
    if since is not None:
        delivered = await _fetch(
            unit,
            'SELECT w.watched_session_id, w.sender_session_id, w.stop_key, '
            'w.delivered_at FROM core.session_wakes w JOIN core.sessions s '
            'ON s.id = w.sender_session_id WHERE s.mind_id = $1 '
            'AND w.state = \'delivered\' AND w.delivered_at >= $2 '
            'ORDER BY w.delivered_at', mind_id, since)
    return {"owed": int(owed["n"]) if owed else 0, "delivered_since": delivered}
