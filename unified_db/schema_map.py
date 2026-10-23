"""Which table of which SQLite file becomes which table of the one database.

This is the decided layout, not a discovered one. Every table in every source
file is named here exactly once, and `unmapped()` is what makes that true
rather than aspirational: a table nobody has decided about is an error, so a
store gaining a table cannot be quietly left behind by a migration that
reports success.

Six schemas rather than one flat namespace because the collisions are real.
`credentials` exists three times across the sources meaning three different
things, `users` twice, and health's singular `conversation` sits beside
broker's `conversations`. Renaming tables to dodge that would rewrite every
query for no gain; Postgres joins across schemas without ceremony.
"""

from __future__ import annotations

#: Source SQLite files, relative to the directory holding the Hive checkouts
#: (``/home/daniel/Storage`` on this host). Relative rather than absolute so
#: the same map runs against a copy of the tree, which is how the loader is
#: rehearsed without touching live data.
SOURCES: dict[str, str] = {
    "broker": "Dev/hive_mind/nervous-system/data/broker.db",
    "sessions": "Dev/hive_mind/nervous-system/data/sessions.db",
    "lucent": "Dev/hive_mind/nervous-system/data/lucent.db",
    "console": "Dev/hive/data/hive.db",
    "tools": "hive-tools/data/hivetools.db",
    "health": "Dev/hive-health/data/health.db",
    "proxy": "Dev/inference-proxy/data/proxy.db",
}

#: Files that exist beside the sources and are deliberately not stores.
NOT_STORES: dict[str, str] = {
    "Dev/hive_mind/nervous-system/data/comms.db":
        "Zero bytes; nothing opens it. Delete.",
    "Dev/hive_mind/nervous-system/data/hive.db":
        "Zero bytes; the console's hive.db is the real one. Delete.",
    "hive-tools/data/hive_tools.db":
        "Zero bytes; hivetools.db is the real one. Delete.",
    "Dev/hive_mind/nervous-system/data/lucent-backups/lucent-premindid-20260811.db":
        "A cold backup file no service opens. Stays on disk as a file; not a "
        "store and not migrated.",
}


def source_paths(base: str) -> dict[str, str]:
    """Absolute path per source, under the given checkout root."""
    from os.path import join
    return {name: join(base, rel) for name, rel in SOURCES.items()}


#: (source, table) -> (schema, table). The whole decision, in one place.
PLACEMENT: dict[tuple[str, str], tuple[str, str]] = {
    # core — the nervous system. broker and sessions join on every turn and
    # only ever lived in two files because of a bind mount.
    ("broker", "minds"): ("core", "minds"),
    ("broker", "surface_claims"): ("core", "surface_claims"),
    ("broker", "secret_scopes"): ("core", "secret_scopes"),
    ("broker", "settings"): ("core", "settings"),
    ("broker", "conversations"): ("core", "conversations"),
    ("broker", "messages"): ("core", "messages"),
    ("sessions", "sessions"): ("core", "sessions"),
    ("sessions", "active_sessions"): ("core", "active_sessions"),
    ("sessions", "group_sessions"): ("core", "group_sessions"),
    ("sessions", "session_memory"): ("core", "session_memory"),
    ("sessions", "session_turns"): ("core", "session_turns"),
    ("sessions", "context_overhead"): ("core", "context_overhead"),
    # core — identity, from the console's own file.
    ("console", "users"): ("core", "users"),
    ("console", "groups"): ("core", "groups"),
    ("console", "group_members"): ("core", "group_members"),
    ("console", "auth_settings"): ("core", "auth_settings"),
    ("console", "sso_bindings"): ("core", "sso_bindings"),

    # memory — lucent.
    ("lucent", "memories"): ("memory", "memories"),
    ("lucent", "nodes"): ("memory", "nodes"),
    ("lucent", "edges"): ("memory", "edges"),
    ("lucent", "schema_types"): ("memory", "schema_types"),
    ("lucent", "schema_edges"): ("memory", "schema_edges"),
    ("lucent", "soul_changes"): ("memory", "soul_changes"),

    # board — the console's board and design pages.
    ("console", "work_items"): ("board", "work_items"),
    ("console", "design_sessions"): ("board", "design_sessions"),
    ("console", "design_notices"): ("board", "design_notices"),
    ("console", "capability_node_settings"): ("board", "capability_node_settings"),
    ("console", "edge_mind_settings"): ("board", "edge_mind_settings"),

    # tools — hive-tools.
    ("tools", "api_tokens"): ("tools", "api_tokens"),
    ("tools", "tool_hitl_settings"): ("tools", "tool_hitl_settings"),
    ("tools", "hitl_requests"): ("tools", "hitl_requests"),
    ("tools", "sudo_grants"): ("tools", "sudo_grants"),

    # proxy — the inference proxy. Its `credentials` is upstream provider
    # secrets, an unrelated concept to core's bearer registry, and keeps its
    # name inside its own schema.
    ("proxy", "providers"): ("proxy", "providers"),
    ("proxy", "models"): ("proxy", "models"),
    ("proxy", "keys"): ("proxy", "keys"),
    ("proxy", "clients"): ("proxy", "clients"),
    ("proxy", "usage_log"): ("proxy", "usage_log"),
    ("proxy", "credentials"): ("proxy", "credentials"),
}

#: Two sources holding one concept. The winner's shape is created; the loser
#: is not created at all, and the data migration folds its rows into the
#: winner.
#:
#: `credentials`: broker and lucent maintain the same per-holder bearer
#: registry twice — same holder, username, role, token_sha — with lucent
#: additionally carrying `mind_id`. lucent's is the superset, so it wins.
#: `users`: hive-tools keeps a second password table holding one row, and its
#: `is_admin` flag folds into the console's roles.
MERGES: dict[tuple[str, str], tuple[str, str]] = {
    ("lucent", "credentials"): ("core", "credentials"),   # winner: creates the table
    ("broker", "credentials"): ("core", "credentials"),   # folded in
    ("console", "users"): ("core", "users"),              # winner, already placed above
    ("tools", "users"): ("core", "users"),                # folded in
}

#: The table whose shape is created, per merge target.
MERGE_WINNER: dict[tuple[str, str], tuple[str, str]] = {
    ("core", "credentials"): ("lucent", "credentials"),
    ("core", "users"): ("console", "users"),
}

#: Not carried, with the reason. A dropped table is named here so that
#: "it isn't in the new database" is a decision somebody can read, rather
#: than an omission nobody noticed.
DROPS: dict[tuple[str, str], str] = {
    ("broker", "user_credentials"):
        "Empty (0 rows) and superseded by core.credentials, which carries a "
        "username per holder.",
    ("console", "terminal_labels"):
        "Carried as data, not as a table: label_backfill moves every kept row "
        "onto its own core.sessions row, which is where names and colours live. "
        "The table is dropped only once no label is left unplaced.",
    ("proxy", "alembic_version"):
        "A migration bookkeeping row for the proxy's own SQLite history. The "
        "unified database keeps its own schema history.",
}

#: Whole health.db, verbatim in shape — the one context a corporate install
#: does not create. Listed as a prefix rule rather than 22 lines.
WHOLE_SOURCE_SCHEMA: dict[str, str] = {"health": "health"}


def placement(source: str, table: str) -> tuple[str, str] | None:
    """Where this source table lands, or None if it is deliberately dropped."""
    key = (source, table)
    if key in DROPS:
        return None
    if key in MERGES:
        return MERGES[key]
    if key in PLACEMENT:
        return PLACEMENT[key]
    if source in WHOLE_SOURCE_SCHEMA:
        return (WHOLE_SOURCE_SCHEMA[source], table)
    return None


def unmapped(source: str, tables: list[str]) -> list[str]:
    """Tables this map says nothing about.

    Distinct from a drop: a drop is decided and carries its reason. This is
    the gate that stops a store growing a table the migration silently leaves
    behind.
    """
    if source in WHOLE_SOURCE_SCHEMA:
        return []
    known = set()
    known |= {t for (s, t) in PLACEMENT if s == source}
    known |= {t for (s, t) in MERGES if s == source}
    known |= {t for (s, t) in DROPS if s == source}
    return sorted(t for t in tables if t not in known)
