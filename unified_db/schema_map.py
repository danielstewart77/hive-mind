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

#: The six schemas and what each is for. Part of the decided layout, so the
#: generator can create them rather than leaving them to a hand step.
SCHEMAS: dict[str, str] = {
    "core": "Nervous system and identity: minds, sessions, conversations, "
            "users, credentials, surface claims.",
    "memory": "lucent: memories, knowledge graph nodes and edges, soul changes.",
    "board": "Console board and design sessions.",
    "tools": "hive-tools: API tokens and human-in-the-loop approvals.",
    "health": "hive-health. The one context a corporate install does not create.",
    "proxy": "Inference proxy: providers, models, keys, usage metering.",
}

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

#: Files that sat beside the sources and are deliberately not stores. The
#: three zero-byte strays were deleted on 2026-10-23 and are recorded here so
#: that their absence is a decision somebody can read rather than a gap.
NOT_STORES: dict[str, str] = {
    "Dev/hive_mind/nervous-system/data/comms.db":
        "Was zero bytes; nothing opened it. Deleted 2026-10-23.",
    "Dev/hive_mind/nervous-system/data/hive.db":
        "Was zero bytes, and shared a name with the real console store at "
        "Dev/hive/data/hive.db — pointing SOURCES at the wrong one would have "
        "emptied core identity and the whole board schema. Deleted 2026-10-23.",
    "hive-tools/data/hive_tools.db":
        "Was zero bytes; hivetools.db is the real one. Deleted 2026-10-23.",
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
    ("broker", "credentials"): ("core", "credentials"),
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
    # Not the same credential as core's: see MERGES for the digest check.
    ("lucent", "credentials"): ("memory", "credentials"),

    # board — the console's board and design pages.
    ("console", "work_items"): ("board", "work_items"),
    ("console", "design_sessions"): ("board", "design_sessions"),
    ("console", "design_notices"): ("board", "design_notices"),
    ("console", "capability_node_settings"): ("board", "capability_node_settings"),
    ("console", "edge_mind_settings"): ("board", "edge_mind_settings"),

    # tools — hive-tools.
    ("tools", "users"): ("tools", "users"),
    ("tools", "api_tokens"): ("tools", "api_tokens"),
    ("tools", "tool_hitl_settings"): ("tools", "tool_hitl_settings"),
    ("tools", "hitl_requests"): ("tools", "hitl_requests"),
    ("tools", "sudo_grants"): ("tools", "sudo_grants"),

    # health — hive-health, unchanged in shape. Enumerated rather than
    # swept by a prefix rule: a rule that auto-places anything puts a
    # third of the database outside the "named exactly once" invariant,
    # so a future _backup copy or an alembic_version here would be
    # created without anybody deciding on it.
    ("health", "activity_sessions"): ("health", "activity_sessions"),
    ("health", "agent_tokens"): ("health", "agent_tokens"),
    ("health", "alerts"): ("health", "alerts"),
    ("health", "briefings"): ("health", "briefings"),
    ("health", "coach_reactions"): ("health", "coach_reactions"),
    ("health", "conversation"): ("health", "conversation"),
    ("health", "corrections"): ("health", "corrections"),
    ("health", "events"): ("health", "events"),
    ("health", "import_ledger"): ("health", "import_ledger"),
    ("health", "import_state"): ("health", "import_state"),
    ("health", "intraday"): ("health", "intraday"),
    ("health", "measurements"): ("health", "measurements"),
    ("health", "notifications"): ("health", "notifications"),
    ("health", "push_subscriptions"): ("health", "push_subscriptions"),
    ("health", "rescore_pending"): ("health", "rescore_pending"),
    ("health", "scores"): ("health", "scores"),
    ("health", "sleep_effective"): ("health", "sleep_effective"),
    ("health", "sleep_history"): ("health", "sleep_history"),
    ("health", "sleep_naps"): ("health", "sleep_naps"),
    ("health", "sleep_sessions"): ("health", "sleep_sessions"),
    ("health", "sleep_stages"): ("health", "sleep_stages"),
    ("health", "status"): ("health", "status"),

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

#: Two sources holding one concept, folded into one table. **Empty, and the
#: reason is worth more than the entries would be.**
#:
#: An earlier draft merged `broker.credentials` into `lucent.credentials` as
#: "the same per-holder bearer registry maintained twice". That was wrong on
#: the data: 12 holders appear in both and **none of the 12 share a
#: `token_sha`**. They are two different credentials per holder — one
#: authenticating to the gateway, one to the memory store. `holder` is the
#: primary key, so folding them would have discarded 12 live bearer digests,
#: and the `token_sha` uniqueness that would have caught the collision was
#: itself being dropped at the time.
#:
#: Unifying them into one registry keyed by holder *and audience* is real
#: design work, and it is the separate story "One credential store every
#: service verifies against" rather than a line in this map.
#:
#: `tools.users` likewise stays put. It holds one row whose `is_admin` has no
#: counterpart in `core.users`, and the console's roles live in `core.groups`
#: / `core.group_members`, which are `(name, created_at)` and
#: `(group_name, user_id)` — no role column anywhere for it to fold into.
#: Both rows are `id = 1` with incompatible password hashes, so the merge also
#: collides on the primary key. It folds in once the people tables exist.
MERGES: dict[tuple[str, str], tuple[str, str]] = {}

#: The table whose shape is created, per merge target. Empty while MERGES is.
MERGE_WINNER: dict[tuple[str, str], tuple[str, str]] = {}

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

def placement(source: str, table: str) -> tuple[str, str] | None:
    """Where this source table lands, or None if it is deliberately dropped."""
    key = (source, table)
    if key in DROPS:
        return None
    if key in MERGES:
        return MERGES[key]
    if key in PLACEMENT:
        return PLACEMENT[key]
    return None


def unmapped(source: str, tables: list[str]) -> list[str]:
    """Tables this map says nothing about.

    Distinct from a drop: a drop is decided and carries its reason. This is
    the gate that stops a store growing a table the migration silently leaves
    behind.
    """
    known = set()
    known |= {t for (s, t) in PLACEMENT if s == source}
    known |= {t for (s, t) in MERGES if s == source}
    known |= {t for (s, t) in DROPS if s == source}
    return sorted(t for t in tables if t not in known)
