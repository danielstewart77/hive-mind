#!/usr/bin/env python3
"""Remap surface-namespaced owner_type/client_type from MIND_NAME to MIND_ID.

Before this change, the telegram bot built its session namespace as
``f"telegram:{MIND_NAME}"`` while the rest of the system keys minds by
UUID (``broker.minds.mind_id``, ``sessions.mind_id``, lucent ``mind_id``).
Two parallel naming systems for the same concept caused dangling rows
in active_sessions (e.g. a stray ``telegram:14cb820b-...`` row from an
ad-hoc write sitting alongside the canonical ``telegram:skippy`` row).

This migration:

  1. Reads broker.minds to build a ``name -> mind_id`` map.
  2. For each (mind_name, mind_id) and each surface prefix in
     {telegram, discord, scheduler, hivemind}, rewrites:
       - sessions.owner_type ``"<surface>:<mind_name>"`` ->
         ``"<surface>:<mind_id>"``
       - active_sessions.client_type same rewrite, with a conflict
         handler: if both the short-name row and the UUID row already
         exist for the same client_ref, keep the UUID row (it's the
         intended target) and drop the short-name row.

Run inside the hive-comms container so the DB paths line up:

    docker exec -i hive-comms python3 < 2026-05-15-surface-namespace-uuid.py

Idempotent: re-running after a successful pass is a no-op because the
short-name rows no longer exist.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

SURFACES = ("telegram", "discord", "scheduler", "hivemind")

SESSIONS_DB = Path("/data/sessions.db")
BROKER_DB = Path("/data/broker.db")


def main() -> int:
    if not SESSIONS_DB.is_file():
        print(f"ERROR: {SESSIONS_DB} not found", file=sys.stderr)
        return 1
    if not BROKER_DB.is_file():
        print(f"ERROR: {BROKER_DB} not found", file=sys.stderr)
        return 1

    broker = sqlite3.connect(str(BROKER_DB))
    broker.row_factory = sqlite3.Row
    name_to_uuid: dict[str, str] = {}
    for row in broker.execute("SELECT name, mind_id FROM minds"):
        if row["name"] and row["mind_id"]:
            name_to_uuid[row["name"]] = row["mind_id"]
    broker.close()

    if not name_to_uuid:
        print("WARN: broker.minds is empty — nothing to migrate", file=sys.stderr)
        return 0

    print(f"name -> mind_id map: {name_to_uuid}")

    sdb = sqlite3.connect(str(SESSIONS_DB))
    sdb.row_factory = sqlite3.Row

    rewrites = 0
    conflicts = 0
    try:
        with sdb:
            for mind_name, mind_id in name_to_uuid.items():
                for surface in SURFACES:
                    old = f"{surface}:{mind_name}"
                    new = f"{surface}:{mind_id}"

                    # 1. sessions table — id is PK so renaming owner_type is
                    #    always safe (no uniqueness constraint on owner_type).
                    cur = sdb.execute(
                        "UPDATE sessions SET owner_type = ? WHERE owner_type = ?",
                        (new, old),
                    )
                    if cur.rowcount:
                        print(f"  sessions: {old} -> {new}  ({cur.rowcount} rows)")
                        rewrites += cur.rowcount

                    # 2. active_sessions — (client_type, client_ref) is PK.
                    #    Conflict can occur if both old and new already exist
                    #    for the same client_ref. Resolution: keep the row
                    #    that maps to the more recently active session.
                    rows = list(sdb.execute(
                        """SELECT a.client_ref, a.session_id, s.last_active
                             FROM active_sessions a
                             LEFT JOIN sessions s ON s.id = a.session_id
                            WHERE a.client_type = ?""",
                        (old,),
                    ))
                    for r in rows:
                        client_ref = r["client_ref"]
                        old_session = r["session_id"]
                        old_last_active = r["last_active"] or 0
                        existing = sdb.execute(
                            "SELECT session_id FROM active_sessions WHERE client_type = ? AND client_ref = ?",
                            (new, client_ref),
                        ).fetchone()
                        if existing is None:
                            sdb.execute(
                                "UPDATE active_sessions SET client_type = ? WHERE client_type = ? AND client_ref = ?",
                                (new, old, client_ref),
                            )
                            print(f"  active_sessions: ({old}, {client_ref}) -> {new}")
                            rewrites += 1
                        else:
                            new_session = existing["session_id"]
                            new_last_active_row = sdb.execute(
                                "SELECT last_active FROM sessions WHERE id = ?",
                                (new_session,),
                            ).fetchone()
                            new_last_active = (new_last_active_row or {"last_active": 0})["last_active"] or 0
                            if old_last_active > new_last_active:
                                sdb.execute(
                                    "DELETE FROM active_sessions WHERE client_type = ? AND client_ref = ?",
                                    (new, client_ref),
                                )
                                sdb.execute(
                                    "UPDATE active_sessions SET client_type = ? WHERE client_type = ? AND client_ref = ?",
                                    (new, old, client_ref),
                                )
                                print(f"  active_sessions: ({old}, {client_ref}) wins over ({new}, {client_ref}) — replaced")
                            else:
                                sdb.execute(
                                    "DELETE FROM active_sessions WHERE client_type = ? AND client_ref = ?",
                                    (old, client_ref),
                                )
                                print(f"  active_sessions: ({new}, {client_ref}) wins over ({old}, {client_ref}) — old dropped")
                            conflicts += 1
    finally:
        sdb.close()

    print(f"\nDONE — {rewrites} rows rewritten, {conflicts} conflicts resolved")
    return 0


if __name__ == "__main__":
    sys.exit(main())
