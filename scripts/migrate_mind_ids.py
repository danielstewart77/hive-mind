#!/usr/bin/env python3
"""One-time migration: rewrite short-name mind identifiers to UUIDs in lucent and broker DBs.

Run AFTER deploying Phase 3 code (which adds mind_id to runtime.yaml and registers the
new schema). Idempotent.
"""

import re
import sqlite3
import sys
import yaml
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
MINDS_DIR = REPO / "minds"
LUCENT_DB = REPO / "data" / "lucent.db"
BROKER_DB = REPO / "data" / "broker.db"

UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def load_name_to_uuid() -> dict[str, str]:
    mapping: dict[str, str] = {}
    for sub in MINDS_DIR.iterdir():
        if not sub.is_dir():
            continue
        rt = sub / "runtime.yaml"
        if not rt.exists():
            continue
        data = yaml.safe_load(rt.read_text())
        name = data.get("name")
        mid = data.get("mind_id")
        if not name or not mid:
            print(f"SKIP: {sub.name} missing name or mind_id in runtime.yaml")
            continue
        mapping[name] = mid
    return mapping


def migrate_lucent(name_to_uuid: dict[str, str]) -> None:
    if not LUCENT_DB.exists():
        print(f"lucent DB not found at {LUCENT_DB}, skipping")
        return
    con = sqlite3.connect(LUCENT_DB)
    try:
        for table in ("nodes", "edges", "memories"):
            cols = [c[1] for c in con.execute(f"PRAGMA table_info({table})")]
            if "agent_id" not in cols:
                continue
            for short, uid in name_to_uuid.items():
                cur = con.execute(
                    f"UPDATE {table} SET agent_id = ? WHERE agent_id = ?",
                    (uid, short),
                )
                if cur.rowcount:
                    print(f"lucent: {table}.agent_id  {short!r} -> {uid}  ({cur.rowcount} rows)")
        con.commit()
    finally:
        con.close()


def migrate_broker(name_to_uuid: dict[str, str]) -> None:
    if not BROKER_DB.exists():
        print(f"broker DB not found at {BROKER_DB}, skipping")
        return
    con = sqlite3.connect(BROKER_DB)
    try:
        # messages.from_mind / messages.to_mind
        for col in ("from_mind", "to_mind"):
            for short, uid in name_to_uuid.items():
                cur = con.execute(
                    f"UPDATE messages SET {col} = ? WHERE {col} = ?",
                    (uid, short),
                )
                if cur.rowcount:
                    print(f"broker: messages.{col}  {short!r} -> {uid}  ({cur.rowcount} rows)")

        # minds.name -> rename column to mind_id and store UUID values
        cols = [c[1] for c in con.execute("PRAGMA table_info(minds)")]
        if "name" in cols and "mind_id" not in cols:
            con.execute("ALTER TABLE minds RENAME COLUMN name TO mind_id")
            print("broker: minds.name -> minds.mind_id (column renamed)")
            cols = [c[1] for c in con.execute("PRAGMA table_info(minds)")]
        if "mind_id" in cols:
            for short, uid in name_to_uuid.items():
                cur = con.execute(
                    "UPDATE minds SET mind_id = ? WHERE mind_id = ?",
                    (uid, short),
                )
                if cur.rowcount:
                    print(f"broker: minds.mind_id  {short!r} -> {uid}  ({cur.rowcount} rows)")

        # secret_scopes.mind_name -> rename to mind_id
        cols = [c[1] for c in con.execute("PRAGMA table_info(secret_scopes)")]
        if "mind_name" in cols and "mind_id" not in cols:
            con.execute("ALTER TABLE secret_scopes RENAME COLUMN mind_name TO mind_id")
            print("broker: secret_scopes.mind_name -> secret_scopes.mind_id (column renamed)")
            cols = [c[1] for c in con.execute("PRAGMA table_info(secret_scopes)")]
        if "mind_id" in cols:
            for short, uid in name_to_uuid.items():
                cur = con.execute(
                    "UPDATE secret_scopes SET mind_id = ? WHERE mind_id = ?",
                    (uid, short),
                )
                if cur.rowcount:
                    print(f"broker: secret_scopes.mind_id  {short!r} -> {uid}  ({cur.rowcount} rows)")
        con.commit()
    finally:
        con.close()


def main() -> int:
    mapping = load_name_to_uuid()
    if not mapping:
        print("no mind_id mappings found — runtime.yaml files missing mind_id?")
        return 1
    print("name -> uuid mapping:")
    for n, u in mapping.items():
        print(f"  {n} -> {u}")
    migrate_lucent(mapping)
    migrate_broker(mapping)
    print("done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
