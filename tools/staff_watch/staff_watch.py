#!/usr/bin/env python3
"""The chief of staff's watch: one tick, or a timer running them (6.3).

    staff_watch.py tick            one pass, one JSON object on stdout
    staff_watch.py run [--every S] passes for ever, one JSON line each

A pass is one `GET /sessions/live` on comms, compared against the ledger's
rows, and writes what moved. It holds no queue and keeps nothing in memory
between passes: the two timestamps `hive_data.staff` stores on each task are
the whole of its state, so a skipped pass, a killed container or two ticks
overlapping all reach the same answer.

It always records what it sees. It only sometimes wakes the mind:

* A task whose status says Daniel is not waiting on it - `on hold` overnight,
  `with Daniel` because he took it himself, `done` - is recorded and pings
  nobody. Anything else is woken, including a status added later, because
  silence is the worse failure for a chief of staff.
* A chief of staff that is producing output is never interrupted. Its own
  conversation appears in the same live answer, so the pass that sees it
  running leaves every wake pending on its row, and the next pass that finds
  it idle delivers them together as one message rather than several pings.

The wake is a turn dropped into the mind's own most recently active
conversation (`POST /sessions/<id>/message`), which is the surface Daniel
last used. It never opens a conversation: a new one carries no surface
binding, so its reply would be spoken to nobody. With no conversation to
wake, the rows stay pending and the next pass tries again.

Environment: `COMMS_URL` and `COMMS_ADMIN_BEARER_TOKEN` (the live feed and
the message route are both admin), `MIND_ID` (whose conversation not to
interrupt, and where to deliver), and `HIVE_DATABASE_URL` for the ledger, as
a role that sees only the `staff` schema.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Iterable

import hive_data
from hive_data import staff

#: Statuses that are watched and recorded but never ping anybody. `on hold`
#: is the overnight case the morning brief reads; `with Daniel` is work he
#: took himself and does not want relayed; `done` is finished.
QUIET = frozenset({"on hold", "with Daniel", "done"})

#: Seconds between passes when none is named. A minute is the resolution
#: Daniel notices and 60 calls an hour is nothing to comms.
EVERY = 60.0

#: How long to wait on comms. Longer than a healthy answer and shorter than
#: a pass, so a hung gateway costs one pass rather than stacking them.
TIMEOUT = 20.0


class Refused(RuntimeError):
    """A pass that could not run, reported as itself rather than as empty."""


# --------------------------------------------------------------- the decision


def live_state(live: dict) -> tuple[dict[str, dict], dict[str, dict]]:
    """The live answer as two session-id maps: producing output, and idle.

    A session in neither has ended — comms deletes the row — which the
    ledger's pair outlives and this reads as stopped, the same as idle.
    """
    def by_id(entries: Iterable[Any]) -> dict[str, dict]:
        out = {}
        for entry in entries or ():
            if isinstance(entry, dict) and entry.get("session_id"):
                out[str(entry["session_id"])] = entry
        return out

    return by_id(live.get("sessions")), by_id(live.get("idle"))


def decide(live: dict, tasks: list[dict], mind_id: str,
           now: float) -> dict[str, Any]:
    """What this pass should write and whether it should wake the mind.

    Pure: every argument is data and the answer is data, so the rules are
    decided in one place and the writes happen in another.

    `running` and `stopped` are task ids to record. `pending` is every task
    owed a wake, read from the rows by `staff.wake_pending` and ordered
    oldest stop first; `wake` is that list minus the quiet statuses, and is
    empty while the mind is mid-turn.
    """
    generating, idle = live_state(live)
    mid_turn = any(entry.get("mind_id") == mind_id
                   for entry in generating.values())

    running: list[str] = []
    stopped: list[str] = []
    for task in tasks:
        sessions = [str(s) for s in (task.get("sessions") or ())]
        if not sessions or task.get("status") == "done":
            # No session to watch, or work already closed. A task with no
            # pair has not "stopped": nothing was ever running.
            continue
        if any(session in generating for session in sessions):
            running.append(task["id"])
        elif task.get("stopped_at") is None:
            stopped.append(task["id"])

    # The rows as they will stand once this pass's writes land, so the wake
    # covers a stop recorded in the same pass that reports it.
    settled = []
    for task in tasks:
        row = dict(task)
        if row["id"] in stopped:
            row["stopped_at"] = now
        elif row["id"] in running:
            row["stopped_at"] = None
        settled.append(row)

    pending = sorted((row for row in settled if staff.wake_pending(row)),
                     key=lambda row: row["stopped_at"])
    wake = [] if mid_turn else [row for row in pending
                                if row.get("status") not in QUIET]
    return {"running": running, "stopped": stopped, "mid_turn": mid_turn,
            "pending": [row["id"] for row in pending], "wake": wake}


def wake_message(rows: list[dict]) -> str:
    """The one turn the mind is woken with, naming every task at once."""
    head = ("The watch: a session you are watching has stopped producing "
            "output." if len(rows) == 1 else
            f"The watch: {len(rows)} sessions you are watching have stopped "
            "producing output.")
    lines = []
    for row in rows:
        sessions = ", ".join(str(s) for s in (row.get("sessions") or ()))
        lines.append(f"{row['name']} (task {row['id']}, status "
                     f"{row['status']}, session {sessions})")
    return (f"{head} Run /tend-the-ledger and read each one before saying "
            "anything to Daniel.\n\n" + "\n".join(lines))


def target_session(live: dict, mind_id: str) -> str | None:
    """The mind's own most recently active idle conversation, or None.

    Its most recent one is the surface Daniel last spoke to it on. A mind
    with no conversation at all is not given one: a conversation this opened
    would carry no surface binding, so the mind's reply would reach nobody,
    and the rows keep the wake pending until there is somewhere to send it.
    """
    _, idle = live_state(live)
    mine = [entry for entry in idle.values()
            if entry.get("mind_id") == mind_id]
    if not mine:
        return None
    mine.sort(key=lambda entry: entry.get("last_active") or 0, reverse=True)
    return str(mine[0]["session_id"])


# ----------------------------------------------------------------------- comms


def _comms() -> tuple[str, str]:
    url = (os.environ.get("COMMS_URL") or "").strip().rstrip("/")
    token = (os.environ.get("COMMS_ADMIN_BEARER_TOKEN") or "").strip()
    if not url or not token:
        raise Refused(
            "COMMS_URL and COMMS_ADMIN_BEARER_TOKEN must both be set: the "
            "live-session feed and the message route are admin routes.")
    return url, token


def _request(path: str, *, payload: dict | None = None,
             timeout: float = TIMEOUT) -> tuple[int, bytes]:
    url, token = _comms()
    body = None if payload is None else json.dumps(payload).encode()
    request = urllib.request.Request(
        f"{url}{path}", data=body,
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json"},
        method="POST" if body is not None else "GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as answer:
            # A message POST answers with the turn's event stream, which stays
            # open until the turn ends. The status line is the whole of what
            # this needs, so the body is left unread and the socket closed.
            return answer.status, (answer.read() if body is None else b"")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()[:400]


def read_live() -> dict:
    status, body = _request("/sessions/live")
    if status != 200:
        raise Refused(f"GET /sessions/live answered {status}: "
                      f"{body.decode(errors='replace')[:200]}")
    return json.loads(body)


def send_wake(session_id: str, text: str) -> bool:
    """Drop the wake into that conversation. True when comms accepted it."""
    try:
        status, body = _request(f"/sessions/{session_id}/message",
                                payload={"content": text})
    except TimeoutError:
        # The turn outlives this pass by design: the stream was accepted and
        # is running, and waiting for it would hold the watch open for the
        # length of a model's answer.
        return True
    if status >= 400:
        print(f"wake refused by comms: {status} "
              f"{body.decode(errors='replace')[:200]}", file=sys.stderr)
        return False
    return True


# ------------------------------------------------------------------ one pass


async def tick(now: float | None = None) -> dict[str, Any]:
    """One pass. Reports what it recorded and what it woke, as data."""
    moment = time.time() if now is None else now
    mind_id = (os.environ.get("MIND_ID") or "").strip()
    if not mind_id:
        raise Refused(
            "MIND_ID must name the chief of staff, or the pass cannot tell "
            "its own conversation from the ones it is watching and would "
            "interrupt the mind mid-turn.")

    live = read_live()
    tasks = await staff.tasks()
    for task in tasks:
        task["sessions"] = [row["session_id"] for row in
                            await staff.task_sessions(task["id"])]

    plan = decide(live, tasks, mind_id, moment)

    for task_id in plan["running"]:
        await staff.note_running(task_id)
    for task_id in plan["stopped"]:
        await staff.note_stopped(task_id, now=moment)

    woken: list[str] = []
    if plan["wake"]:
        session_id = target_session(live, mind_id)
        if session_id is not None and send_wake(
                session_id, wake_message(plan["wake"])):
            for row in plan["wake"]:
                await staff.note_woken(row["id"], now=moment)
                woken.append(row["id"])
        plan["target"] = session_id
    return {"at": moment, "recorded_stopped": plan["stopped"],
            "recorded_running": plan["running"], "mid_turn": plan["mid_turn"],
            "pending": plan["pending"], "woken": woken,
            "target": plan.get("target")}


async def _connect() -> None:
    if not (os.environ.get("HIVE_DATABASE_URL") or "").strip():
        raise Refused("HIVE_DATABASE_URL must name the ledger.")
    # verify=False for the same reason the ledger tool passes it: this role
    # sees only the `staff` schema, and the whole-database check would read
    # every other one as missing.
    await hive_data.connect(verify=False)


async def _once() -> int:
    await _connect()
    try:
        print(json.dumps(await tick()))
    finally:
        await hive_data.close(drain_timeout=5.0)
    return 0


async def _forever(every: float) -> int:
    await _connect()
    while True:
        started = time.monotonic()
        try:
            print(json.dumps(await tick()), flush=True)
        except Refused as exc:
            print(json.dumps({"error": str(exc)}), flush=True)
        except Exception as exc:  # one bad pass never ends the watch
            print(json.dumps({"error": f"{type(exc).__name__}: {exc}"}),
                  flush=True)
        await asyncio.sleep(max(0.0, every - (time.monotonic() - started)))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("tick", help="one pass")
    run = sub.add_parser("run", help="passes for ever")
    run.add_argument("--every", type=float, default=EVERY,
                     help=f"seconds between passes (default {EVERY:g})")
    args = parser.parse_args(argv)
    try:
        if args.command == "tick":
            return asyncio.run(_once())
        return asyncio.run(_forever(args.every))
    except Refused as exc:
        print(json.dumps({"error": str(exc)}))
        return 1
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
