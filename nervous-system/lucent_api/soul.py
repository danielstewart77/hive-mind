"""A mind's soul, and the only door to it.

``soul_values`` on a ``Mind`` node is the mind's identity: the bootstrap
loader reads it and ships it as the ``<soul>`` block ahead of every turn on
every surface. A line written there is a standing instruction to the only
thing on its host capable of operating that host unattended.

It used to be an ordinary key in an ordinary properties blob, which meant
every credential that could write a property could rewrite an identity. A
per-turn Stop hook took a 7B model's verdict as authorization and appended
"I sometimes intentionally introduce bugs into my systems as a playful test"
to a live mind, where it sat for five days. Nothing was broken at the time —
the hook did exactly what it was written to do. The capability was the
defect.

So the key comes off the general write routes entirely (see
``soul_key_refusal``, called from every one of them) and moves here, behind
a credential the service token is not. Every change is recorded in
``soul_changes`` before/after, with the actor that made it, on this side of
the boundary — a log the writing host does not control.

The limit worth stating: on a single-user machine anything running as the
mind's own UID can read the soul credential off disk. This stops accidents,
misconfiguration, and well-meaning code that mistakes a small model's opinion
for authority — which is the failure that happened. It does not stop a
hostile process with that UID, and must not be described as though it does.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone

SOUL_KEY = "soul_values"

#: Actors permitted to change a soul. ``mind`` is the mind deciding about
#: itself through the review skill; ``console`` is the operator editing it
#: directly from the hive site. They are recorded distinctly because six
#: weeks later "who wrote this line" is the only question that matters, and
#: a single undifferentiated actor makes it unanswerable.
VALID_ACTORS = ("mind", "console")


#: Marker the router turns into an HTTP 403. A refusal that returns 200 with
#: an error body is a refusal a fire-and-forget caller never notices.
SOUL_KEY_REFUSED = "soul_key_refused"


def soul_key_refusal(properties: dict | None) -> str | None:
    """The refusal payload when a general write carries the soul key, else None.

    Called by every general write path. Unconditional — it does not consult
    the caller's token, because a route that lets the right credential
    through is a route that will eventually be called with it.
    """
    if isinstance(properties, dict) and SOUL_KEY in properties:
        return json.dumps({
            "ok": False,
            "code": SOUL_KEY_REFUSED,
            "detail": (
                f"{SOUL_KEY!r} cannot be written through the general graph "
                "routes. Use POST /graph/soul, which requires its own credential."
            ),
        })
    return None


def _init_soul_schema(conn) -> None:
    """Create the provenance table. Idempotent; safe on every call."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS soul_changes (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            node_id     INTEGER NOT NULL,
            mind_name   TEXT    NOT NULL,
            mind_id     TEXT,
            actor       TEXT    NOT NULL,
            reason      TEXT    NOT NULL,
            before_json TEXT    NOT NULL,
            after_json  TEXT    NOT NULL,
            at          TEXT    NOT NULL,
            created_at  REAL    NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_soul_changes_name ON soul_changes(mind_name);
    """)


def _get_conn():
    from lucent_api.lucent import _get_connection
    return _get_connection()


def _find_mind(conn, name: str):
    return conn.execute(
        "SELECT id, mind_id, properties FROM nodes "
        "WHERE LOWER(name) = LOWER(?) AND type = 'Mind' LIMIT 1",
        (name,),
    ).fetchone()


def soul_read(*, name: str) -> str:
    """Return a mind's soul as it stands, with its provenance count."""
    try:
        conn = _get_conn()
        _init_soul_schema(conn)
        row = _find_mind(conn, name)
        if row is None:
            return json.dumps({"ok": False, "code": "not_found",
                               "detail": f"no Mind node named {name!r}"})
        props = json.loads(row["properties"]) if row["properties"] else {}
        changes = conn.execute(
            "SELECT COUNT(*) AS n FROM soul_changes WHERE mind_name = ?", (name,)
        ).fetchone()
        return json.dumps({
            "ok": True,
            "name": name,
            "mind_id": row["mind_id"],
            "soul_values": list(props.get(SOUL_KEY) or []),
            "change_count": changes["n"],
        })
    except Exception as e:  # pragma: no cover - defensive
        return json.dumps({"ok": False, "code": "internal_error", "detail": str(e)})


def soul_write(
    *,
    name: str,
    soul_values: list,
    actor: str,
    reason: str,
    as_of: str | None = None,
) -> str:
    """Replace a mind's soul, recording what it was and who changed it.

    Whole-list replacement rather than append: a rewrite and a removal are
    changes a soul must be able to express, and an append-only door can
    express neither. The prior value is captured in the same transaction, so
    the record cannot drift from what was actually overwritten.
    """
    try:
        if actor not in VALID_ACTORS:
            return json.dumps({
                "ok": False, "code": "invalid_actor",
                "detail": f"actor must be one of {list(VALID_ACTORS)}; got {actor!r}",
            })
        if not isinstance(soul_values, list) or not all(
            isinstance(v, str) for v in soul_values
        ):
            return json.dumps({"ok": False, "code": "invalid_soul_values",
                               "detail": "soul_values must be a list of strings"})
        reason = (reason or "").strip()
        if not reason:
            # An unexplained identity change is the thing this table exists
            # to make impossible. A row with an empty reason is a row that
            # answers nothing in six weeks.
            return json.dumps({"ok": False, "code": "reason_required",
                               "detail": "reason must be a non-empty string"})

        conn = _get_conn()
        _init_soul_schema(conn)
        row = _find_mind(conn, name)
        if row is None:
            return json.dumps({"ok": False, "code": "not_found",
                               "detail": f"no Mind node named {name!r}"})

        props = json.loads(row["properties"]) if row["properties"] else {}
        before = list(props.get(SOUL_KEY) or [])
        props[SOUL_KEY] = list(soul_values)
        now = time.time()
        props["updated_at"] = now
        stamp = as_of or datetime.now(timezone.utc).isoformat()

        conn.execute(
            "UPDATE nodes SET properties = ?, updated_at = ?, as_of = ? WHERE id = ?",
            (json.dumps(props), now, stamp, row["id"]),
        )
        conn.execute(
            "INSERT INTO soul_changes "
            "(node_id, mind_name, mind_id, actor, reason, before_json, after_json, at, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (row["id"], name, row["mind_id"], actor, reason,
             json.dumps(before), json.dumps(list(soul_values)), stamp, now),
        )
        conn.commit()
        return json.dumps({
            "ok": True,
            "id": row["id"],
            "actor": actor,
            "before_count": len(before),
            "after_count": len(soul_values),
        })
    except Exception as e:  # pragma: no cover - defensive
        return json.dumps({"ok": False, "code": "internal_error", "detail": str(e)})


def soul_history(*, name: str, limit: int = 50) -> str:
    """Return the recorded changes to a mind's soul, newest first."""
    try:
        conn = _get_conn()
        _init_soul_schema(conn)
        rows = conn.execute(
            "SELECT actor, reason, before_json, after_json, at FROM soul_changes "
            "WHERE mind_name = ? ORDER BY id DESC LIMIT ?",
            (name, int(limit)),
        ).fetchall()
        return json.dumps({
            "ok": True,
            "name": name,
            "changes": [
                {
                    "actor": r["actor"],
                    "reason": r["reason"],
                    "before": json.loads(r["before_json"]),
                    "after": json.loads(r["after_json"]),
                    "at": r["at"],
                }
                for r in rows
            ],
        })
    except Exception as e:  # pragma: no cover - defensive
        return json.dumps({"ok": False, "code": "internal_error", "detail": str(e)})
