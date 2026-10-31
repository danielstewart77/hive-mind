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

from dataclasses import dataclass

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
    # Who started an operation, so its status is theirs to read. The console
    # creates this table on demand and had never been asked, so it reached
    # the cutover with no rows and — until the console's store declared it —
    # no home in the new database either. It is placed rather than dropped
    # because the two routes that write it are live: a hive-tools answer
    # carrying a request id is recorded against the signed-in user, and
    # `GET /api/operations/{request_id}` is scoped to whoever that was.
    ("console", "operation_requesters"): ("board", "operation_requesters"),

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
        "Retired. Names and colours live on core.sessions' own name and color "
        "columns, where 171 rows carry them. Its single remaining row is a "
        "superseded name whose conversation was renamed on the live row it "
        "rotated into, so nothing is lost by dropping it.",
    ("proxy", "alembic_version"):
        "A migration bookkeeping row for the proxy's own SQLite history. The "
        "unified database keeps its own schema history.",
    ("sessions", "group_sessions"):
        "0 rows since it was written. The group-session experiment put "
        "several minds in one conversation behind a moderator; it was tried "
        "once, went badly, and nothing has written a row since. The two "
        "declared relationships that named it go with it, and "
        "`sessions.group_session_id` is dropped as a column.",
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


# ---------------------------------------------------------------------------
# Relationships
# ---------------------------------------------------------------------------
#
# SQLite could not express most of these. A foreign key cannot span two files,
# so a session could not reference the mind it ran on, and every relationship
# between the nervous system's three stores survived only as a convention in
# application code. The ones a single file *could* declare are read from the
# source itself (`PRAGMA foreign_key_list`) and remapped through `PLACEMENT`;
# the ones below are the ones the file split made impossible.
#
# Nothing here is emitted on faith. `build_schema` counts the rows that would
# break each one against the live stores, and a relationship with so much as
# one offender is reported with its count and left undeclared rather than
# producing DDL that cannot load.


@dataclass(frozen=True)
class Relationship:
    """One foreign key in the unified schema.

    `child` and `parent` are `(schema, table)` in the *unified* layout, not
    source names. `on_delete` is Postgres's own vocabulary.
    """

    child: tuple[str, str]
    child_columns: tuple[str, ...]
    parent: tuple[str, str]
    parent_columns: tuple[str, ...]
    on_delete: str
    why: str

    @property
    def name(self) -> str:
        return (f"{self.child[1]}_{'_'.join(self.child_columns)}"
                f"_fk_{self.parent[1]}")


#: Relationships that could not be declared while each store was its own file.
CROSS_FILE: tuple[Relationship, ...] = (
    Relationship(
        ("core", "sessions"), ("mind_id",), ("core", "minds"), ("mind_id",),
        "RESTRICT",
        "A session ran on a mind. The mind is not a part of the session and "
        "deleting one out from under 8,140 sessions is never the intent.",
    ),
    Relationship(
        ("core", "context_overhead"), ("mind_id",), ("core", "minds"),
        ("mind_id",), "CASCADE",
        "Observed per-mind prompt overhead. It is a measurement of that mind "
        "and means nothing once the mind is gone.",
    ),
    Relationship(
        ("core", "surface_claims"), ("mind_id",), ("core", "minds"),
        ("mind_id",), "CASCADE",
        "Who a mind's bot answers. A claim is part of the mind, not a thing "
        "that outlives it.",
    ),
    Relationship(
        ("memory", "edges"), ("mind_id",), ("core", "minds"), ("mind_id",),
        "RESTRICT",
        "Which mind's graph this edge belongs to. Memory is not deleted as a "
        "side effect of retiring a mind.",
    ),
    Relationship(
        ("memory", "soul_changes"), ("node_id",), ("memory", "nodes"), ("id",),
        "RESTRICT",
        "The audit trail of a soul node's edits. `node_id` is NOT NULL so it "
        "cannot be blanked, and the history must not be destroyed as a side "
        "effect of a node deletion — that deletion becomes a conversation.",
    ),
    Relationship(
        ("board", "design_sessions"), ("mind_id",), ("core", "minds"),
        ("mind_id",), "RESTRICT",
        "The mind a design session is being run with.",
    ),
    Relationship(
        ("memory", "nodes"), ("type",), ("memory", "schema_types"), ("name",),
        "RESTRICT",
        "A node's type against the registry that validates it. "
        "`_validate_label` already refuses a type the registry does not hold, "
        "so the key states a rule the graph enforces in code.",
    ),
    Relationship(
        ("memory", "schema_edges"), ("source_type",), ("memory", "schema_types"),
        ("name",), "RESTRICT",
        "The registry's own shape: an edge type relates two node types.",
    ),
    Relationship(
        ("memory", "schema_edges"), ("target_type",), ("memory", "schema_types"),
        ("name",), "RESTRICT",
        "As `source_type`.",
    ),
    Relationship(
        ("proxy", "models"), ("provider_id",), ("proxy", "providers"), ("id",),
        "SET NULL",
        "Declared as a foreign key by the proxy's own ORM "
        "(app/orm.py, ondelete SET NULL) and absent from the file, because "
        "the column arrived by ALTER TABLE ADD COLUMN and SQLite cannot "
        "attach a key that way. The file never carried what the application "
        "always meant.",
    ),
    Relationship(
        ("board", "design_sessions"), ("conversation_id",),
        ("core", "sessions"), ("id",), "SET NULL",
        "The conversation a design session is attached to. The design "
        "outlives the conversation.",
    ),
)

#: Where the unified schema deliberately differs from what the source file
#: declared, keyed by `(child schema, child table, child columns)`. A source
#: saying `NO ACTION` mostly means nobody thought about it rather than that
#: refusal was chosen, and three of these are parts of their parent: ending a
#: session is meant to take the session with it.
ON_DELETE_OVERRIDE: dict[tuple[str, str, tuple[str, ...]], tuple[str, str]] = {
    ("core", "session_turns", ("session_id",)): (
        "CASCADE", "A turn is part of its session's ledger."),
    ("core", "active_sessions", ("session_id",)): (
        "CASCADE", "A surface binding is part of its session."),
    ("core", "messages", ("conversation_id",)): (
        "CASCADE", "A message is part of its broker conversation."),
    ("board", "design_notices", ("design_id",)): (
        "CASCADE", "A notice is part of its design session."),
    ("memory", "edges", ("source_id",)): (
        "CASCADE", "An edge cannot exist without both of its nodes."),
    ("memory", "edges", ("target_id",)): (
        "CASCADE", "An edge cannot exist without both of its nodes."),
}

#: Defaults the source carries that no row could satisfy under the key now
#: declared above the column. Dropped rather than translated, because a
#: default is only reached by an insert that omits the column - and the row it
#: would write is one the constraint refuses. `sessions.mind_id` defaults to
#: the literal `'ada'` while `core.minds.mind_id` holds UUIDs, so the default
#: has been writing a value that matches no mind for as long as it has
#: existed; it is simply never reached, because every caller supplies the id.
DROPPED_DEFAULTS: dict[tuple[str, str, str], str] = {
    ("core", "sessions", "mind_id"):
        "Literal 'ada' against a column of UUIDs.",
}

#: Columns that look like references and are not, with the reason. Reported by
#: the build so that "there is no foreign key here" is a decision somebody can
#: read rather than an omission nobody noticed.
UNKEYED: dict[tuple[str, str, str], str] = {
    ("memory", "memories", "mind_id"):
        "Holds the sentinel 'shared' on 7 rows, meaning hive-wide rather than "
        "any one mind. A key would refuse the sentinel.",
    ("memory", "nodes", "mind_id"):
        "NOT NULL, and one row holds the empty string. Postgres treats '' as "
        "a value, so a key would refuse that row.",
    ("memory", "soul_changes", "mind_id"):
        "One row holds the short name 'skippy' where a UUID belongs.",
    ("core", "minds", "owner"):
        "The person a mind belongs to, as free text. Two values name people "
        "with no console account. Becomes a real reference in the "
        "multi-user story, which creates the accounts first.",
    ("core", "credentials", "holder"):
        "17 of 28 holders are services (the scheduler, the voice server, "
        "hive-tools) rather than minds. Not a mind reference.",
    ("core", "messages", "from_mind"):
        "Mostly a mind id, but also service names such as 'sms-gateway'. A "
        "free-text sender, not a reference.",
    ("core", "messages", "to_mind"):
        "As `from_mind`.",
    ("core", "messages", "recipient_session_id"):
        "5 of 7,367 non-null values name sessions that have been swept. The "
        "column records where a reply was delivered, not a live reference.",
    ("core", "session_memory", "conversation_id"):
        "Holds a harness conversation id, not a session row id. Read by "
        "(mind_id, client_ref) and never by session: a provenance stamp. "
        "The name it carried in SQLite, `session_id`, is what invited the "
        "key; `COLUMN_RENAMES` fixes the name and this entry records that "
        "there is still nothing to reference.",
    ("memory", "edges", "type"):
        "An edge's relation. Every one of the 112 rows happens to be in "
        "`schema_edges`, but `_validate_relation` checks only that it is "
        "uppercase letters and underscores - the registry is not consulted - "
        "so a key here would start refusing edges the graph accepts today.",
    ("memory", "credentials", "mind_id"):
        "The mind a credential was minted for. `holder_access` names the "
        "value `provenance` and stores it nullable; same reasoning as "
        "`core.session_memory.mind_id`.",
    ("tools", "hitl_requests", "tool_name"):
        "All 103 rows name a tool with a settings row, but nothing seeds "
        "`tool_hitl_settings` when a tool is added, so the first request for "
        "a new tool would be refused rather than defaulted.",
    ("core", "session_memory", "mind_id"):
        "Whose carried-forward memory this is - but registration is "
        "deliberately non-fatal and comms refuses a mind that names no owner, "
        "so a mind can be running and serving while holding no broker row. "
        "`POST /sessions/{id}/session-memory` writes the id whether or not "
        "one exists, and a key would turn that into a 500 on the mind that "
        "most needs its memory kept.",
    ("tools", "hitl_requests", "caller_mind"):
        "Holds the sentinel 'internal' on 3 rows, and names a mind by name "
        "rather than id.",
    ("tools", "sudo_grants", "caller_mind"):
        "As `hitl_requests.caller_mind`.",
    ("proxy", "usage_log", "model_name"):
        "The model as named on the request, kept verbatim for the audit. 4 "
        "rows name a model the proxy no longer lists; a key would rewrite "
        "history when a model is retired.",
    ("board", "capability_node_settings", "node_id"):
        "Declared TEXT against memory.nodes.id, which is an integer, and both "
        "rows name nodes that no longer exist. A stale settings table, not a "
        "reference.",
}


# ---------------------------------------------------------------------------
# The final column shape
# ---------------------------------------------------------------------------
#
# A SQLite column name travelled into the unified schema unexamined, because
# the generator reads `PRAGMA table_info` and emits what it finds. Four names
# on `core.sessions` and one on `core.session_memory` are wrong about what
# they hold, three columns are not worth carrying at all, and two are integers
# only because SQLite has no boolean.
#
# All of it is declared here rather than fixed afterwards. The map is a
# declaration, so the final shape costs one edit; renaming after the data
# access layer is written means two passes over every call site, and renaming
# after the cut-over means two passes over every live service.
#
# Every entry below is keyed by the **source** column name, because that is
# what the generator reads out of the file. The build refuses if an entry
# names a column no source carries — the same rule as `unmapped` and
# `UNKEYED`, for the same reason: a decision written against a column that no
# longer exists is a decision nobody can act on, and it must not read as a
# column that was handled.

#: (schema, table, source column) -> (new name, why).
COLUMN_RENAMES: dict[tuple[str, str, str], tuple[str, str]] = {
    ("core", "sessions", "claude_sid"): (
        "conversation_id",
        "The gateway mints this id for every harness and hands it to claude, "
        "codex and dsh alike. The vendor's name on a harness-neutral thing "
        "is the defect: a reader has to know that `claude_sid` on a codex "
        "conversation is not a claude anything.",
    ),
    ("core", "sessions", "owner_type"): (
        "surface",
        "The kind of the single endpoint currently holding the conversation, "
        "not an owner and not a from-and-to pair. `surface` is the word used "
        "everywhere else in the system - `HIVE_SURFACE`, `_surface_label`, "
        "the surface claims, the per-turn `<message-surface>` block - so "
        "this makes the table agree with the vocabulary instead of being the "
        "one place that disagrees.",
    ),
    ("core", "sessions", "owner_ref"): (
        "surface_id",
        "The address on that surface. The reply goes back to the address the "
        "message arrived from, which is why it is one column and not two. "
        "`source`/`source_id` was considered and dropped for underselling "
        "the reply direction.",
    ),
    ("core", "sessions", "summary"): (
        "opening_line",
        "The first 100 characters of the first user message, written once "
        "under an `if summary == 'New session'` guard and never updated; 176 "
        "live rows still hold the default. Calling it a summary invites "
        "someone to write a summarizer into it, or to show it to a user as "
        "one. It stays because the gateway holds no transcript and the "
        "session picker needs something for the conversations that were "
        "never named.",
    ),
    ("core", "session_memory", "session_id"): (
        "conversation_id",
        "It holds a harness conversation id, never a session row id, and the "
        "table is read by (mind_id, client_ref). The name is the whole "
        "reason this column kept being mistaken for a reference - see its "
        "`UNKEYED` entry.",
    ),
}

#: (schema, table, source column) -> why it is not carried. A dropped column
#: is named here so that "it isn't in the new database" is a decision
#: somebody can read rather than an omission nobody noticed. Everything that
#: has to be deleted from the services alongside each one is enumerated in
#: the cleanup story, not here: this file decides the shape, not the code.
DROPPED_COLUMNS: dict[tuple[str, str, str], str] = {
    ("core", "sessions", "autopilot"): (
        "0 of 8,142 rows have ever held 1. One spawn flag for one harness, "
        "reached by a route nothing calls."
    ),
    ("core", "sessions", "rotated_from"): (
        "Exists only because a chat rotation inserts a successor row rather "
        "than rotating in place. It records which row a rotation replaced, "
        "and every reader of it reads it after the row it names is gone."
    ),
    ("core", "sessions", "group_session_id"): (
        "The group-session experiment, which has 0 rows. Dropped with "
        "`core.group_sessions` itself."
    ),
}

#: (schema, table, source column) -> (Postgres type, why). The source type is
#: not wrong so much as unable to say what was meant: SQLite has no boolean,
#: so a flag was declared INTEGER and the type map carried that across as
#: `bigint`.
#:
#: `core.sessions.rotation_armed` is deliberately absent. It looks like the
#: same thing and is not: it is three-state - 0 unarmed, 1 a chat surface
#: finalized into a successor row, 2 a staged terminal only the pane's own
#: fire may swap - and collapsing it to a boolean destroys a permanent
#: session row, because a staged terminal read as merely "armed" is retired
#: for a successor instead of being respawned in place.
TYPE_OVERRIDES: dict[tuple[str, str, str], tuple[str, str]] = {
    ("core", "sessions", "terminal_held"): (
        "boolean",
        "Whether a browser terminal holds this conversation. A flag, "
        "INTEGER only because SQLite had nothing better.",
    ),
    ("core", "sessions", "context_has_images"): (
        "boolean",
        "Whether the history holds images, which the gateway reads to decide "
        "whether a model that cannot take them may be offered. A flag.",
    ),
    ("memory", "memories", "embedding"): (
        "vector(4096)",
        "A 4096-dimension embedding, a BLOB only because SQLite had no "
        "vector type and lucent therefore scanned every row into numpy to "
        "score cosine in Python. The dimension is declared rather than left "
        "open: a bare `vector` accepts a row of any width and the mismatch "
        "then arrives at query time, on every recall, as an error about a "
        "row nobody can identify. All live rows are 16384 bytes, which is "
        "4096 float32s exactly. No approximate index is possible at this "
        "width - pgvector caps HNSW and IVFFlat at 2000 dimensions and "
        "halfvec at 4000 - so recall is an exact scan, which is both "
        "accurate and, measured at 190ms warm over the live corpus, faster "
        "than shipping every row to Python.",
    ),
}

#: The text search configuration the searchable columns are built with, and
#: the dictionaries behind it.
#:
#: Not stock `english`, and not `simple`. The configuration has to match what
#: FTS5 is doing today, which is `tokenize='porter unicode61'`:
#:
#: * It stems. The live index holds `rotat` (1,151 rows), `restart` (983) and
#:   `run` (1,599) and holds no `rotation`, `restarting` or `running` at all,
#:   so a query for "rotation" matches every inflection today. `simple` would
#:   match only the literal word and silently narrow every keyword result.
#: * It folds diacritics. `unicode61` defaults to `remove_diacritics=1`, so
#:   the index holds `jose`, and 4,097 of the live rows carry non-ASCII
#:   bytes. Hence `unaccent`, which is a trusted extension.
#: * It keeps stopwords. FTS5 indexes every token; lucent filters its own
#:   80-word stoplist out of the *query* before it is ever run. Stock
#:   `english` removes its own stopwords from the *documents* too, so a query
#:   token in Postgres's list but not in lucent's (`same`, `than`, `most`,
#:   `such`, `about`, `against`, ...) would match nothing where it matches
#:   now. So the snowball stemmer is declared with no stopword file.
TEXT_CONFIG = "hive_text"
TEXT_CONFIG_SCHEMA = "memory"

#: (schema, table) -> the generated searchable column, the source column it
#: is built from, how much of it is indexed, and why.
#:
#: A generated column rather than an expression index, because the column is
#: what makes "stored and findable in one write" true without the writer
#: knowing anything about text search - and because the drift check can see
#: a column.
SEARCH_COLUMNS: dict[tuple[str, str], tuple[str, str, int, str]] = {
    ("memory", "memories"): (
        "content_search", "content", 100_000,
        "Replaces lucent's `memories_fts`, an FTS5 virtual table that exists "
        "on disk and in no source file, with a column this generator can "
        "declare. Capped, because a tsvector is hard-limited to 1MB and "
        "`to_tsvector` over a large enough paste raises - which, on a "
        "generated column, fails the INSERT and loses the memory outright. "
        "A memory past the cap is stored whole and findable by its first "
        "100,000 characters, because an unfindable memory is recoverable and "
        "a refused one is gone.",
    ),
}


def search_column(schema: str, table: str) -> tuple[str, str, int, str] | None:
    """The generated searchable column for a table, if the map declares one."""
    return SEARCH_COLUMNS.get((schema, table))


def column_name(schema: str, table: str, column: str) -> str:
    """The unified name for a source column."""
    renamed = COLUMN_RENAMES.get((schema, table, column))
    return renamed[0] if renamed else column


def column_dropped(schema: str, table: str, column: str) -> str | None:
    """Why this source column is not carried, or None if it is."""
    return DROPPED_COLUMNS.get((schema, table, column))


def column_type(schema: str, table: str, column: str) -> tuple[str, str] | None:
    """The declared Postgres type for a source column, if the map sets one."""
    return TYPE_OVERRIDES.get((schema, table, column))


def column_decisions() -> set[tuple[str, str, str]]:
    """Every (schema, table, source column) this file decides something about.

    The build checks these against the columns it actually read, so an entry
    naming a column that has since been renamed or dropped in the source
    refuses the build rather than sitting here looking handled.
    """
    return (set(COLUMN_RENAMES) | set(DROPPED_COLUMNS) | set(TYPE_OVERRIDES))
